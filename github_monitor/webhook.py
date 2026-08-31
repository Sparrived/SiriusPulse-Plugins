"""Bounded, authenticated GitHub Webhook server.

The server deliberately defaults to a local-only deployment.  It is intended to
be put behind a separately configured TLS/reverse-proxy boundary when it must
receive requests from outside the host; this module does not silently turn an
HTTP listener into a production internet endpoint.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import ipaddress
import json
import logging
import math
import re
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from aiohttp import web
from aiohttp.web_protocol import RequestHandler as _AiohttpRequestHandler
from aiohttp.web_server import Server as _AiohttpServer

logger = logging.getLogger(__name__)

WebhookHandler = Callable[[str, dict[str, Any]], Awaitable[None]]
RepoFilter = Callable[[str], bool]
EventFilter = Callable[[str], bool]

_MAX_BODY_BYTES = 2 * 1024 * 1024
_MAX_QUEUE_SIZE = 64
_MAX_WORKERS = 8
_MAX_REPLAY_IDS = 4096
_MAX_DEAD_LETTERS = 1024
_MAX_RATE_KEYS = 4096
_MAX_HEADER_VALUE_BYTES = 1024
_MAX_EVENT_TYPE_LENGTH = 100
_MAX_REPO_NAME_LENGTH = 201
_MAX_DELIVERY_ID_LENGTH = 200
_REPLAY_TTL_SECONDS = 15 * 60
_RATE_WINDOW_SECONDS = 60.0
_RATE_LIMIT = 120
_REQUEST_TIMEOUT_SECONDS = 15.0
_HANDLER_TIMEOUT_SECONDS = 60.0
_MAX_RETRY_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = 0.25
_MAX_RETRY_BACKOFF_SECONDS = 5.0
_MAX_HEADER_LINE_BYTES = 4096
_MAX_HEADER_COUNT = 64
_MAX_CONFIG_BODY_BYTES = 16 * 1024 * 1024
_MAX_CONFIG_QUEUE_SIZE = 1024
_MAX_CONFIG_TTL_SECONDS = 24 * 60 * 60
_MAX_CONFIG_REQUEST_TIMEOUT_SECONDS = 5 * 60
_MAX_CONFIG_HANDLER_TIMEOUT_SECONDS = 15 * 60
_MAX_CONFIG_RATE_WINDOW_SECONDS = 60 * 60
_MAX_CONFIG_RATE_LIMIT = 100_000
_MAX_CONFIG_CONCURRENT_REQUESTS = 1024
_MAX_RETAINED_ITEMS = _MAX_REPLAY_IDS + _MAX_CONFIG_QUEUE_SIZE + _MAX_WORKERS
_SHUTDOWN_TIMEOUT_SECONDS = 15.0
_MAX_CONFIG_SHUTDOWN_TIMEOUT_SECONDS = 300.0
_GITHUB_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")


def _finite_limit(value: Any, minimum: float, maximum: float, default: float) -> float:
    """Return a finite configuration value within a safe range."""

    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(number):
        return default
    return min(max(number, minimum), maximum)


def _bounded_int(value: Any, minimum: int, maximum: int, default: int) -> int:
    """Return an integer configuration value within a safe range."""

    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return min(max(number, minimum), maximum)


def _reject_json_constant(value: str) -> None:
    """Reject non-standard JSON constants such as NaN and Infinity."""

    raise ValueError(f"non-standard JSON constant: {value}")


class _WebhookValidationError(ValueError):
    """Raised when a webhook payload does not satisfy the basic contract."""


class _HeaderTimeoutRequestHandler(_AiohttpRequestHandler):
    """aiohttp protocol handler that bounds time spent waiting for headers."""

    def __init__(
        self,
        manager: _AiohttpServer,
        *,
        loop: asyncio.AbstractEventLoop,
        header_timeout_seconds: float,
        **kwargs: Any,
    ) -> None:
        self._header_timeout_seconds = header_timeout_seconds
        self._header_timeout_handle: asyncio.TimerHandle | None = None
        super().__init__(manager, loop=loop, **kwargs)

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        super().connection_made(transport)
        self._arm_header_timeout()

    def connection_lost(self, exc: BaseException | None) -> None:
        self._cancel_header_timeout()
        super().connection_lost(exc)

    def data_received(self, data: bytes) -> None:
        super().data_received(data)
        if self._headers_complete():
            self._cancel_header_timeout()

    async def _handle_request(self, *args: Any, **kwargs: Any) -> Any:
        try:
            return await super()._handle_request(*args, **kwargs)
        finally:
            # Keep-alive connections need a fresh deadline for their next
            # request, but never arm one while a body or pipelined message is
            # still being consumed.
            if not self._headers_complete() and not self._force_close:
                self._arm_header_timeout()

    def _headers_complete(self) -> bool:
        return bool(self._messages or self._payload_parser is not None or self._upgrade)

    def _arm_header_timeout(self) -> None:
        self._cancel_header_timeout()
        if self._force_close or self._headers_complete():
            return
        self._header_timeout_handle = self._loop.call_later(
            self._header_timeout_seconds, self._header_timeout_expired
        )

    def _cancel_header_timeout(self) -> None:
        if self._header_timeout_handle is not None:
            self._header_timeout_handle.cancel()
            self._header_timeout_handle = None

    def _header_timeout_expired(self) -> None:
        self._header_timeout_handle = None
        if not self._headers_complete() and not self._request_in_progress:
            self.log_debug("Request header read timed out")
            self.force_close()


class _HeaderTimeoutServer(_AiohttpServer):
    """aiohttp Server using :class:`_HeaderTimeoutRequestHandler`."""

    def __init__(self, *args: Any, header_timeout_seconds: float, **kwargs: Any) -> None:
        self._header_timeout_seconds = header_timeout_seconds
        super().__init__(*args, **kwargs)

    def __call__(self) -> _HeaderTimeoutRequestHandler:
        try:
            return _HeaderTimeoutRequestHandler(
                self,
                loop=self._loop,
                header_timeout_seconds=self._header_timeout_seconds,
                **self._kwargs,
            )
        except TypeError as exc:
            # Never silently drop parser limits on an unsupported aiohttp
            # version: a weaker fallback would defeat the hardening.
            raise RuntimeError(
                "aiohttp does not support the required bounded webhook server options"
            ) from exc


class _HeaderTimeoutAppRunner(web.AppRunner):
    """AppRunner variant that installs the bounded-header server."""

    def __init__(
        self, app: web.Application, *, header_timeout_seconds: float, **kwargs: Any
    ) -> None:
        self._header_timeout_seconds = header_timeout_seconds
        super().__init__(app, **kwargs)

    async def _make_server(self) -> _HeaderTimeoutServer:
        loop = asyncio.get_event_loop()
        self._app._set_loop(loop)
        self._app.on_startup.freeze()
        await self._app.startup()
        self._app.freeze()

        kwargs = dict(self._kwargs)
        access_log_class = kwargs.get("access_log_class", web.AccessLogger)
        kwargs["debug"] = self._app._debug
        kwargs["access_log_class"] = access_log_class
        handler_args = self._app._handler_args
        if handler_args:
            kwargs.update(handler_args)
        return _HeaderTimeoutServer(
            self._app._handle,  # type: ignore[arg-type]
            request_factory=self._app._make_request,
            loop=loop,
            header_timeout_seconds=self._header_timeout_seconds,
            **kwargs,
        )


def _is_loopback(host: str) -> bool:
    """Return whether *host* is a literal loopback address or localhost.

    Hostnames other than ``localhost`` are intentionally not resolved.  DNS
    resolution can change and is not a safe basis for deciding whether an
    unauthenticated local mode is acceptable.
    """

    value = str(host or "").strip().casefold().strip("[]")
    if value == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _strict_bool(value: Any, default: bool = False) -> bool:
    """Parse a configuration boolean without treating arbitrary strings true."""

    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "1", "yes", "y", "on"}:
            return True
        if normalized in {"false", "0", "no", "n", "off", ""}:
            return False
    if isinstance(value, int) and not isinstance(value, bool):
        if value == 1:
            return True
        if value == 0:
            return False
    return default


def verify_signature(payload: bytes, signature: str, secret: str) -> bool:
    """Verify GitHub's HMAC-SHA256 signature; empty secrets fail closed."""

    if not isinstance(payload, bytes) or not isinstance(secret, str) or not secret.strip():
        return False
    if not isinstance(signature, str):
        return False
    signature = signature.strip()
    if len(signature) != len("sha256=") + 64 or not signature.startswith("sha256="):
        return False
    digest = signature[len("sha256=") :]
    try:
        int(digest, 16)
    except ValueError:
        return False
    try:
        secret_bytes = secret.encode("utf-8")
    except UnicodeEncodeError:
        return False
    expected = "sha256=" + hmac.new(secret_bytes, payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


@dataclass(slots=True)
class _WebhookItem:
    event_type: str
    body: dict[str, Any]
    delivery_id: str
    repo_name: str


class GitHubWebhookServer:
    """aiohttp webhook server with bounded, retryable asynchronous processing.

    ``verify_signature`` and the constructor remain compatible with the former
    implementation.  Additional keyword-only limits can be tuned for a local
    deployment.  Non-loopback binds are rejected because this class does not
    own a TLS configuration; callers should terminate TLS in a trusted proxy
    and bind this server to loopback instead.
    """

    def __init__(
        self,
        secret: str = "",
        host: str = "127.0.0.1",
        port: int = 0,
        *,
        allow_unsigned_local: bool = False,
        max_body_bytes: int = _MAX_BODY_BYTES,
        queue_size: int = _MAX_QUEUE_SIZE,
        replay_ttl_seconds: float = _REPLAY_TTL_SECONDS,
        worker_count: int = 4,
        request_timeout_seconds: float = _REQUEST_TIMEOUT_SECONDS,
        handler_timeout_seconds: float = _HANDLER_TIMEOUT_SECONDS,
        max_retry_attempts: int = _MAX_RETRY_ATTEMPTS,
        retry_backoff_seconds: float = _RETRY_BACKOFF_SECONDS,
        rate_window_seconds: float = _RATE_WINDOW_SECONDS,
        rate_limit: int = _RATE_LIMIT,
        max_concurrent_requests: int = 32,
        tls_enabled: bool = False,
        shutdown_timeout_seconds: float = _SHUTDOWN_TIMEOUT_SECONDS,
    ) -> None:
        self._secret_config_valid = isinstance(secret, str)
        if self._secret_config_valid:
            try:
                secret.encode("utf-8")
            except UnicodeEncodeError:
                self._secret_config_valid = False
        if not self._secret_config_valid or not secret.strip():
            secret = ""
        self._secret = secret
        self._host = str(host or "").strip()
        self._configured_port = _bounded_int(port, 0, 65535, 0)
        self._port = self._configured_port
        # Kept as an accepted compatibility keyword, but this class never
        # claims to provide TLS without an SSLContext and therefore still
        # rejects non-loopback binds.
        self._tls_enabled = _strict_bool(tls_enabled)
        self._loopback_bind = _is_loopback(self._host)
        self._lifecycle_lock = asyncio.Lock()
        self._admission_lock = asyncio.Lock()
        self._lifecycle_state = "stopped"
        self._ever_started = False
        # Unsigned operation is only meaningful when explicitly enabled, with
        # an empty secret, and for a loopback-bound server.  A remote request
        # is checked independently in _handle as defense in depth.
        self._allow_unsigned_local = (
            _strict_bool(allow_unsigned_local)
            and self._loopback_bind
            and self._secret_config_valid
        )

        self._max_body_bytes = _bounded_int(
            max_body_bytes, 1, _MAX_CONFIG_BODY_BYTES, _MAX_BODY_BYTES
        )
        self._queue_size = _bounded_int(queue_size, 1, _MAX_CONFIG_QUEUE_SIZE, _MAX_QUEUE_SIZE)
        self._replay_ttl_seconds = _finite_limit(
            replay_ttl_seconds, 1.0, _MAX_CONFIG_TTL_SECONDS, _REPLAY_TTL_SECONDS
        )
        self._worker_count = _bounded_int(worker_count, 1, _MAX_WORKERS, 4)
        self._request_timeout_seconds = _finite_limit(
            request_timeout_seconds,
            0.01,
            _MAX_CONFIG_REQUEST_TIMEOUT_SECONDS,
            _REQUEST_TIMEOUT_SECONDS,
        )
        self._handler_timeout_seconds = _finite_limit(
            handler_timeout_seconds,
            0.01,
            _MAX_CONFIG_HANDLER_TIMEOUT_SECONDS,
            _HANDLER_TIMEOUT_SECONDS,
        )
        self._max_retry_attempts = _bounded_int(max_retry_attempts, 1, 10, _MAX_RETRY_ATTEMPTS)
        self._retry_backoff_seconds = _finite_limit(
            retry_backoff_seconds, 0.0, _MAX_RETRY_BACKOFF_SECONDS, _RETRY_BACKOFF_SECONDS
        )
        self._rate_window_seconds = _finite_limit(
            rate_window_seconds, 1.0, _MAX_CONFIG_RATE_WINDOW_SECONDS, _RATE_WINDOW_SECONDS
        )
        self._rate_limit = _bounded_int(rate_limit, 1, _MAX_CONFIG_RATE_LIMIT, _RATE_LIMIT)
        self._max_concurrent_requests = _bounded_int(
            max_concurrent_requests, 1, _MAX_CONFIG_CONCURRENT_REQUESTS, 32
        )
        self._shutdown_timeout_seconds = _finite_limit(
            shutdown_timeout_seconds,
            0.1,
            _MAX_CONFIG_SHUTDOWN_TIMEOUT_SECONDS,
            _SHUTDOWN_TIMEOUT_SECONDS,
        )

        self._queue: asyncio.Queue[_WebhookItem] = asyncio.Queue(maxsize=self._queue_size)
        self._workers: list[asyncio.Task[None]] = []
        # Keep the old private name available to integrations/tests that may
        # have inspected it, while the implementation now uses a worker pool.
        self._worker: asyncio.Task[None] | None = None
        self._request_semaphore: asyncio.Semaphore | None = None
        self._worker_stop_event: asyncio.Event | None = None
        self._retained_items: deque[_WebhookItem] = deque(maxlen=_MAX_RETAINED_ITEMS)
        self._handler_progress: dict[str, set[int]] = {}
        self._app = self._build_app()
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._handlers: dict[str, list[WebhookHandler]] = {}
        self._repo_filter: RepoFilter | None = None
        self._event_filter: EventFilter | None = None

        # Delivery IDs are reserved only while queued/processing.  They move
        # to _completed_deliveries after every handler succeeds.  A failed
        # delivery is removed from the active set so GitHub can retry it.
        self._pending_deliveries: dict[str, float] = {}
        self._completed_deliveries: dict[str, float] = {}
        self._dead_letters: dict[str, tuple[float, str]] = {}
        self._rate_state: dict[str, tuple[float, int]] = {}
        self._last_rate_prune = 0.0

    def _build_app(self) -> web.Application:
        """Create an unfrozen aiohttp app with bounded parser settings."""

        app = web.Application(
            client_max_size=self._max_body_bytes,
            handler_args={
                "auto_decompress": False,
                "max_line_size": _MAX_HEADER_LINE_BYTES,
                "max_field_size": _MAX_HEADER_VALUE_BYTES,
                "max_headers": _MAX_HEADER_COUNT,
                "keepalive_timeout": self._request_timeout_seconds,
                "lingering_time": min(self._request_timeout_seconds, 2.0),
            },
        )
        app.router.add_post("/webhook/github", self._handle)
        return app

    def _retain_item(self, item: _WebhookItem) -> None:
        """Retain one dequeued item for a future start without double queue accounting."""

        if any(existing.delivery_id == item.delivery_id for existing in self._retained_items):
            return
        if len(self._retained_items) >= _MAX_RETAINED_ITEMS:
            # The normal queue is much smaller than this cap.  If an operator
            # configures an unusually large queue, do not silently evict a
            # delivery; leave the existing queue item in place and record the
            # failed delivery for operator visibility.
            self._record_dead_letter(item, "retained-item-cap")
            logger.error(
                "Webhook delivery could not be retained during shutdown (event=%s, repo=%s)",
                item.event_type,
                item.repo_name,
            )
            return
        self._retained_items.append(item)
        self._pending_deliveries[item.delivery_id] = time.monotonic()

    def _restore_retained_items(self) -> None:
        """Move retained items back into the bounded queue after startup."""

        while self._retained_items and not self._queue.full():
            item = self._retained_items.popleft()
            if item.delivery_id in self._completed_deliveries:
                continue
            try:
                self._queue.put_nowait(item)
            except asyncio.QueueFull:
                self._retained_items.appendleft(item)
                break

    def set_repo_filter(self, filter_fn: RepoFilter | None) -> None:
        """Set an optional repository predicate used before queueing."""

        self._repo_filter = filter_fn

    def set_event_filter(self, filter_fn: EventFilter | None) -> None:
        """Set an optional event predicate used before queueing."""

        self._event_filter = filter_fn

    def add_handler(self, event_type: str, handler: WebhookHandler) -> None:
        """Register an asynchronous handler for a GitHub event header."""

        event = str(event_type).strip()
        if not event or len(event) > _MAX_EVENT_TYPE_LENGTH:
            raise ValueError("event_type is empty or too long")
        self._handlers.setdefault(event, []).append(handler)

    async def start(self) -> int:
        """Start the HTTP listener and bounded worker pool.

        The lifecycle lock is held through setup so concurrent callers cannot
        observe a half-started runner or replace it with a second runner.
        """

        async with self._lifecycle_lock:
            if self._lifecycle_state == "running":
                return self._port
            if self._lifecycle_state != "stopped":
                raise RuntimeError("GitHub Webhook server lifecycle is busy")
            if not self._loopback_bind:
                # A TLS flag alone is not an implementation of TLS.  Keep this
                # check explicit so a plain HTTP socket is never exposed.
                logger.error(
                    "Refusing non-loopback GitHub Webhook bind without an implemented TLS boundary: %s",
                    self._host,
                )
                raise RuntimeError("GitHub Webhook server only supports loopback binds")
            if not self._secret and not self._allow_unsigned_local:
                logger.error(
                    "Refusing to start GitHub Webhook server without a non-empty HMAC secret"
                )
                raise RuntimeError("GitHub Webhook HMAC secret is required")
            if self._secret and self._allow_unsigned_local:
                # Unsigned operation can never weaken a configured HMAC secret.
                self._allow_unsigned_local = False

            self._lifecycle_state = "starting"
            self._request_semaphore = asyncio.Semaphore(self._max_concurrent_requests)
            self._worker_stop_event = asyncio.Event()
            # An aiohttp Application is frozen by AppRunner.setup().  Build a
            # fresh one on every start so a stopped server can be restarted.
            self._app = self._build_app()
            runner = _HeaderTimeoutAppRunner(
                self._app,
                header_timeout_seconds=self._request_timeout_seconds,
            )
            self._runner = runner
            self._site = None
            self._port = self._configured_port
            try:
                await runner.setup()
                site = web.TCPSite(runner, self._host, self._configured_port)
                self._site = site
                await site.start()
                sockets = site._server.sockets if site._server is not None else []  # type: ignore[union-attr]
                actual_port = sockets[0].getsockname()[1] if sockets else self._configured_port
                self._port = int(actual_port)
                self._workers = [
                    asyncio.create_task(
                        self._run_worker(index), name=f"github-webhook-worker-{index}"
                    )
                    for index in range(self._worker_count)
                ]
                # Compatibility for code that used to use this as a started flag.
                self._worker = self._workers[0] if self._workers else None
                self._restore_retained_items()
                self._ever_started = True
                self._lifecycle_state = "running"
                logger.info(
                    "GitHub Webhook 服务已启动: http://%s:%s/webhook/github",
                    self._host,
                    self._port,
                )
                return self._port
            except BaseException:
                self._workers.clear()
                self._worker = None
                try:
                    await runner.cleanup()
                finally:
                    self._runner = None
                    self._site = None
                    self._request_semaphore = None
                    self._worker_stop_event = None
                    self._port = self._configured_port
                    self._lifecycle_state = "stopped"
                raise

    async def stop(self) -> None:
        """Stop intake and workers while retaining unfinished queue items."""

        async with self._lifecycle_lock:
            if self._lifecycle_state == "stopped":
                return
            if self._lifecycle_state != "running":
                raise RuntimeError("GitHub Webhook server lifecycle is busy")
            # Serialize the state transition with admission.  A request that
            # already owns this lock may finish its atomic enqueue; every later
            # request observes stopping and is rejected.
            async with self._admission_lock:
                self._lifecycle_state = "stopping"

            # Close the listening socket before waiting for existing requests.
            site = self._site
            runner = self._runner
            try:
                if site is not None:
                    await site.stop()
                try:
                    if runner is not None:
                        await runner.cleanup()
                finally:
                    await self._stop_workers()
            finally:
                self._runner = None
                self._site = None
                self._request_semaphore = None
                self._worker_stop_event = None
                self._port = self._configured_port
                self._lifecycle_state = "stopped"
                logger.info("GitHub Webhook 服务已停止")

    async def _stop_workers(self) -> None:
        """Drain the queue, then cancel only workers that exceed the bound."""

        workers = list(self._workers)
        if not workers:
            self._drain_queue_to_retained()
            return
        stop_event = self._worker_stop_event
        if stop_event is not None:
            stop_event.set()

        done, pending = await asyncio.wait(
            workers,
            timeout=self._shutdown_timeout_seconds,
        )
        if pending:
            for worker in pending:
                worker.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        for worker in done:
            try:
                await worker
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.error(
                    "Webhook worker stopped unexpectedly (error_type=%s)",
                    type(exc).__name__,
                )
        # A timed-out worker may have been processing one item and retained it
        # in its cancellation path.  Items that never reached a worker remain
        # in the queue and must be retained as well.  Each get is paired with
        # exactly one task_done here.
        self._drain_queue_to_retained()
        self._workers.clear()
        self._worker = None

    def _drain_queue_to_retained(self) -> None:
        """Move all queued-but-unprocessed deliveries to the retained inbox."""

        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                self._retain_item(item)
            finally:
                self._queue.task_done()

    async def _run_worker(self, worker_index: int = 0) -> None:
        """Consume queue items; each item receives exactly one task_done call."""

        del worker_index  # only used to give each task a useful name
        # Capture this generation's stop event.  The server clears its field
        # after stop; a worker that is still unwinding must still see the event.
        stop_event = self._worker_stop_event
        while True:
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=0.1)
            except asyncio.TimeoutError:
                if stop_event is not None and stop_event.is_set():
                    return
                continue
            try:
                succeeded = await self._process_item(item)
                if succeeded:
                    now = time.monotonic()
                    self._pending_deliveries.pop(item.delivery_id, None)
                    self._completed_deliveries[item.delivery_id] = now
                    self._handler_progress.pop(item.delivery_id, None)
                else:
                    self._pending_deliveries.pop(item.delivery_id, None)
            except asyncio.CancelledError:
                # The item has already been removed from the queue.  Reinsert
                # it before propagating cancellation so stop/restart cannot
                # lose it; the finally block accounts for the original get.
                self._retain_item(item)
                raise
            except Exception as exc:
                self._pending_deliveries.pop(item.delivery_id, None)
                self._record_dead_letter(item, type(exc).__name__)
                logger.error(
                    "Webhook worker failed unexpectedly (event=%s, repo=%s, error_type=%s)",
                    item.event_type,
                    item.repo_name,
                    type(exc).__name__,
                )
            finally:
                self._queue.task_done()
                if self._lifecycle_state == "running" and self._retained_items:
                    self._restore_retained_items()

    @staticmethod
    def _handler_accepts_delivery_id(handler: WebhookHandler) -> bool:
        """Detect the optional third argument without masking handler errors."""
        try:
            signature = inspect.signature(handler)
        except (TypeError, ValueError):
            return False
        positional = [
            parameter
            for parameter in signature.parameters.values()
            if parameter.kind
            in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        return any(
            parameter.kind == inspect.Parameter.VAR_POSITIONAL
            for parameter in signature.parameters.values()
        ) or len(positional) >= 3

    async def _call_handler(self, handler: WebhookHandler, item: _WebhookItem) -> Any:
        if self._handler_accepts_delivery_id(handler):
            return await handler(item.event_type, item.body, item.delivery_id)  # type: ignore[misc]
        return await handler(item.event_type, item.body)

    async def _process_item(self, item: _WebhookItem) -> bool:
        """Run each handler with independent bounded retries.

        Progress is retained by delivery ID when a later handler fails.  A
        retry therefore skips handlers that already completed successfully and
        avoids repeating their side effects.
        """

        handlers = tuple(self._handlers.get(item.event_type, ()))
        if not handlers:
            # The request path normally filters this case, but treating it as a
            # successful no-op prevents an item from remaining reserved forever
            # if configuration changes after queueing.
            return True

        completed = self._handler_progress.setdefault(item.delivery_id, set())
        for index, handler in enumerate(handlers):
            if index in completed:
                continue
            last_error: Exception | None = None
            for attempt in range(1, self._max_retry_attempts + 1):
                try:
                    await asyncio.wait_for(
                        self._call_handler(handler, item),
                        timeout=self._handler_timeout_seconds,
                    )
                    completed.add(index)
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    last_error = exc
                    if attempt >= self._max_retry_attempts:
                        break
                    delay = min(
                        self._retry_backoff_seconds * (2 ** (attempt - 1)),
                        _MAX_RETRY_BACKOFF_SECONDS,
                    )
                    logger.warning(
                        "Webhook handler failed; retrying (attempt=%d/%d, handler=%d, event=%s, repo=%s, delay=%.2fs, error_type=%s)",
                        attempt,
                        self._max_retry_attempts,
                        index,
                        item.event_type,
                        item.repo_name,
                        delay,
                        type(exc).__name__,
                    )
                    if delay:
                        await asyncio.sleep(delay)
            else:  # pragma: no cover - loop exits via break on success
                last_error = None

            if index not in completed:
                reason = type(last_error).__name__ if last_error is not None else "handler failure"
                self._record_dead_letter(item, reason)
                logger.error(
                    "Webhook delivery moved to dead-letter state (attempts=%d, handler=%d, event=%s, repo=%s, error_type=%s)",
                    self._max_retry_attempts,
                    index,
                    item.event_type,
                    item.repo_name,
                    reason,
                )
                return False

        return True

    def _record_dead_letter(self, item: _WebhookItem, reason: str) -> None:
        now = time.monotonic()
        self._dead_letters[item.delivery_id] = (now, reason)
        if len(self._dead_letters) > _MAX_DEAD_LETTERS:
            oldest = sorted(self._dead_letters, key=lambda key: self._dead_letters[key][0])
            for delivery_id in oldest[: len(self._dead_letters) - _MAX_DEAD_LETTERS]:
                self._dead_letters.pop(delivery_id, None)

    def _prune_delivery_state(self, now: float) -> None:
        cutoff = now - self._replay_ttl_seconds
        # Pending deliveries are active reservations, not replay history.  They
        # must never expire while a worker is still processing them.
        for delivery_id, seen_at in list(self._completed_deliveries.items()):
            if seen_at < cutoff:
                self._completed_deliveries.pop(delivery_id, None)
                self._handler_progress.pop(delivery_id, None)
        for delivery_id, (seen_at, _reason) in list(self._dead_letters.items()):
            if seen_at < cutoff:
                self._dead_letters.pop(delivery_id, None)
                self._handler_progress.pop(delivery_id, None)

        if len(self._completed_deliveries) > _MAX_REPLAY_IDS:
            candidates = sorted(
                (
                    (seen_at, delivery_id)
                    for delivery_id, seen_at in self._completed_deliveries.items()
                ),
                key=lambda pair: pair[0],
            )
            remove_count = len(self._completed_deliveries) - _MAX_REPLAY_IDS
            for _seen_at, delivery_id in candidates[:remove_count]:
                self._completed_deliveries.pop(delivery_id, None)
                self._handler_progress.pop(delivery_id, None)

    def _prune_rate_state(self, now: float) -> None:
        if now - self._last_rate_prune < 1.0 and len(self._rate_state) <= _MAX_RATE_KEYS:
            return
        self._last_rate_prune = now
        cutoff = now - self._rate_window_seconds
        for key, (window_start, _count) in list(self._rate_state.items()):
            if window_start < cutoff:
                self._rate_state.pop(key, None)
        if len(self._rate_state) > _MAX_RATE_KEYS:
            oldest = sorted(self._rate_state, key=lambda key: self._rate_state[key][0])
            for key in oldest[: len(self._rate_state) - _MAX_RATE_KEYS]:
                self._rate_state.pop(key, None)

    def _rate_limited(self, key: str, now: float) -> bool:
        self._prune_rate_state(now)
        window_start, count = self._rate_state.get(key, (now, 0))
        if now - window_start >= self._rate_window_seconds:
            window_start, count = now, 0
        count += 1
        self._rate_state[key] = (window_start, count)
        return count > self._rate_limit

    async def _read_body(self, request: web.Request) -> bytes:
        """Read at most max_body_bytes with an overall and idle-read bound."""

        if request.content_length is not None and request.content_length > self._max_body_bytes:
            raise web.HTTPRequestEntityTooLarge(
                max_size=self._max_body_bytes, actual_size=request.content_length
            )
        chunks: list[bytes] = []
        total = 0
        iterator = request.content.iter_chunked(64 * 1024)
        while True:
            try:
                chunk = await asyncio.wait_for(iterator.__anext__(), self._request_timeout_seconds)
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError as exc:
                raise web.HTTPRequestTimeout(text="request body read timeout") from exc
            total += len(chunk)
            if total > self._max_body_bytes:
                raise web.HTTPRequestEntityTooLarge(
                    max_size=self._max_body_bytes, actual_size=total
                )
            chunks.append(chunk)
        return b"".join(chunks)

    @staticmethod
    def _header(request: web.Request, name: str, *, required: bool = False) -> str:
        value = request.headers.get(name, "")
        if not isinstance(value, str):
            return ""
        value = value.strip()
        if len(value.encode("utf-8", errors="ignore")) > _MAX_HEADER_VALUE_BYTES:
            raise _WebhookValidationError(f"header too long: {name}")
        if any(ord(char) < 0x21 or ord(char) > 0x7E for char in value):
            raise _WebhookValidationError(f"invalid header: {name}")
        if required and not value:
            raise _WebhookValidationError(f"missing header: {name}")
        return value

    @staticmethod
    def _validate_payload(event_type: str, body: Mapping[str, Any]) -> str:
        repository = body.get("repository")
        if not isinstance(repository, Mapping):
            raise _WebhookValidationError("missing repository")
        repo_name = repository.get("full_name")
        if not isinstance(repo_name, str) or not repo_name:
            raise _WebhookValidationError("missing repository full_name")
        # Validate, rather than normalize, the value used for authorization.
        # GitHub repository names contain no whitespace/control characters and
        # have exactly one owner/repository separator.
        if len(repo_name) > _MAX_REPO_NAME_LENGTH or not _GITHUB_REPO_RE.fullmatch(repo_name):
            raise _WebhookValidationError("invalid repository full_name")

        sender = body.get("sender")
        if sender is not None and not isinstance(sender, Mapping):
            raise _WebhookValidationError("invalid sender")
        action = body.get("action")
        if event_type in {
            "issues",
            "pull_request",
            "release",
            "issue_comment",
            "pull_request_review_comment",
            "commit_comment",
        }:
            if not isinstance(action, str) or not action:
                raise _WebhookValidationError("missing action")
        elif action is not None and not isinstance(action, str):
            raise _WebhookValidationError("invalid action")

        if event_type == "issues":
            if not isinstance(body.get("issue"), Mapping):
                raise _WebhookValidationError("missing issue")
        elif event_type == "pull_request":
            if not isinstance(body.get("pull_request"), Mapping):
                raise _WebhookValidationError("missing pull_request")
        elif event_type == "release":
            if not isinstance(body.get("release"), Mapping):
                raise _WebhookValidationError("missing release")
        elif event_type == "issue_comment":
            if not isinstance(body.get("issue"), Mapping):
                raise _WebhookValidationError("missing issue")
            if not isinstance(body.get("comment"), Mapping):
                raise _WebhookValidationError("missing comment")
        elif event_type == "pull_request_review_comment":
            if not isinstance(body.get("pull_request"), Mapping):
                raise _WebhookValidationError("missing pull_request")
            if not isinstance(body.get("comment"), Mapping):
                raise _WebhookValidationError("missing comment")
        elif event_type == "commit_comment":
            if not isinstance(body.get("comment"), Mapping):
                raise _WebhookValidationError("missing comment")
        elif event_type == "push":
            ref = body.get("ref")
            if not isinstance(ref, str) or not ref:
                raise _WebhookValidationError("missing push ref")
            commits = body.get("commits")
            if commits is not None:
                if not isinstance(commits, list):
                    raise _WebhookValidationError("invalid push commits")
                if any(not isinstance(commit, Mapping) for commit in commits):
                    raise _WebhookValidationError("invalid push commit")
        return repo_name

    def _request_is_local(self, request: web.Request) -> bool:
        # request.remote is populated from the TCP peer by aiohttp.  Do not
        # accept a hostname or forwarded header as proof of loopback origin.
        remote = str(request.remote or "").strip().strip("[]")
        if not remote or remote.casefold() == "localhost":
            return False
        try:
            return ipaddress.ip_address(remote).is_loopback and self._loopback_bind
        except ValueError:
            return False

    async def _handle(self, request: web.Request) -> web.Response:
        """Apply the request deadline while processing one webhook request."""

        try:
            async with asyncio.timeout(self._request_timeout_seconds):
                return await self._handle_inner(request)
        except (asyncio.TimeoutError, web.HTTPRequestTimeout):
            return web.json_response({"error": "request timeout"}, status=408)

    async def _handle_inner(self, request: web.Request) -> web.Response:
        """Authenticate, validate and enqueue one bounded webhook request."""

        # A listener that has been stopped must not accept direct calls made
        # after its first lifecycle.  The pre-start exception preserves the
        # useful direct-handler compatibility of the old implementation.
        if self._lifecycle_state != "running" and self._ever_started:
            return web.json_response(
                {"error": "webhook server is not accepting requests"},
                status=503,
                headers={"Retry-After": "1"},
            )

        remote = str(request.remote or "unknown").strip()
        now = time.monotonic()
        if self._rate_limited(remote[:256], now):
            return web.json_response(
                {"error": "rate limit exceeded"},
                status=429,
                headers={"Retry-After": "1"},
            )

        semaphore = self._request_semaphore
        if semaphore is None:
            # Primarily protects direct unit calls before start(); deployed
            # requests always go through start() and use the cap.
            semaphore = asyncio.Semaphore(self._max_concurrent_requests)
            self._request_semaphore = semaphore
        if semaphore.locked():
            return web.json_response(
                {"error": "request concurrency limit"},
                status=503,
                headers={"Retry-After": "1"},
            )
        await semaphore.acquire()
        try:
            encoding = request.headers.get("Content-Encoding", "").strip().casefold()
            if encoding not in {"", "identity"}:
                # Raw bytes are intentionally not decompressed: GitHub signs
                # the wire representation and decompressing before HMAC would
                # invalidate that guarantee.  Ask the sender to send JSON.
                return web.json_response(
                    {"error": "content encoding not supported"},
                    status=415,
                )
            try:
                # The per-read deadline in _read_body stops slowloris bodies;
                # this outer deadline also bounds body reading, validation and
                # response preparation for a single request.
                async with asyncio.timeout(self._request_timeout_seconds):
                    body_bytes = await self._read_body(request)
            except web.HTTPRequestEntityTooLarge:
                # aiohttp's exception headers include Content-Type, which
                # conflicts with json_response's JSON content type.
                return web.json_response({"error": "payload too large"}, status=413)
            except (web.HTTPRequestTimeout, asyncio.TimeoutError):
                return web.json_response({"error": "request timeout"}, status=408)

            signature = request.headers.get("X-Hub-Signature-256", "")
            signed = verify_signature(body_bytes, signature, self._secret)
            unsigned_local = (
                self._allow_unsigned_local
                and not self._secret
                and self._request_is_local(request)
            )
            if not signed and not unsigned_local:
                logger.warning("Webhook signature verification failed")
                return web.json_response({"error": "signature mismatch"}, status=401)

            try:
                event_type = self._header(request, "X-GitHub-Event", required=True)
                delivery_id = self._header(request, "X-GitHub-Delivery", required=True)
                # Validate the optional signature header's size/content without
                # ever logging it.  It is already cryptographically checked.
                self._header(request, "X-Hub-Signature-256")
                if len(event_type) > _MAX_EVENT_TYPE_LENGTH:
                    raise _WebhookValidationError("event header too long")
                if len(delivery_id) > _MAX_DELIVERY_ID_LENGTH:
                    raise _WebhookValidationError("delivery header too long")
            except _WebhookValidationError as exc:
                return web.json_response({"error": str(exc)}, status=400)

            try:
                body = json.loads(
                    body_bytes.decode("utf-8"),
                    parse_constant=_reject_json_constant,
                )
            except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
                return web.json_response({"error": "invalid JSON"}, status=400)
            if not isinstance(body, dict):
                return web.json_response({"error": "JSON object required"}, status=400)
            try:
                repo_name = self._validate_payload(event_type, body)
            except _WebhookValidationError as exc:
                return web.json_response({"error": str(exc)}, status=400)

            if self._event_filter is not None:
                try:
                    event_allowed = bool(self._event_filter(event_type))
                except Exception as exc:
                    logger.error(
                        "Webhook event filter failed (error_type=%s)", type(exc).__name__
                    )
                    return web.json_response({"error": "event filter failure"}, status=500)
                if not event_allowed:
                    return web.json_response(
                        {"status": "ignored", "reason": "event filtered out"}
                    )
            if self._repo_filter is not None:
                try:
                    repo_allowed = bool(self._repo_filter(repo_name))
                except Exception as exc:
                    logger.error(
                        "Webhook repository filter failed (error_type=%s)", type(exc).__name__
                    )
                    return web.json_response({"error": "repository filter failure"}, status=500)
                if not repo_allowed:
                    logger.debug("Webhook repository %s is filtered out", repo_name)
                    return web.json_response(
                        {"status": "ignored", "reason": "repo filtered out"}
                    )

            if not self._handlers.get(event_type):
                return web.json_response(
                    {"status": "ignored", "reason": f"no handler for event: {event_type}"}
                )

            # Admission is serialized with stop().  This is the linearization
            # point for delivery deduplication and prevents enqueue-after-stop.
            async with self._admission_lock:
                accepting = self._lifecycle_state == "running" or (
                    self._lifecycle_state == "stopped" and not self._ever_started
                )
                if not accepting:
                    return web.json_response(
                        {"error": "webhook server is not accepting requests"},
                        status=503,
                        headers={"Retry-After": "1"},
                    )
                self._prune_delivery_state(time.monotonic())
                if delivery_id in self._pending_deliveries:
                    return web.json_response(
                        {"status": "duplicate", "delivery_id": delivery_id}, status=202
                    )
                if delivery_id in self._completed_deliveries:
                    return web.json_response(
                        {"status": "duplicate", "delivery_id": delivery_id}, status=200
                    )
                if self._queue.full():
                    return web.json_response(
                        {"error": "webhook queue full"},
                        status=503,
                        headers={"Retry-After": "1"},
                    )

                admitted_at = time.monotonic()
                self._pending_deliveries[delivery_id] = admitted_at
                item = _WebhookItem(event_type, body, delivery_id, repo_name)
                try:
                    self._queue.put_nowait(item)
                except asyncio.QueueFull:
                    self._pending_deliveries.pop(delivery_id, None)
                    return web.json_response(
                        {"error": "webhook queue full"},
                        status=503,
                        headers={"Retry-After": "1"},
                    )
                # Do not erase this marker until the retry was actually
                # admitted; a queue-full response must preserve the DLQ state.
                self._dead_letters.pop(delivery_id, None)
                return web.json_response(
                    {"status": "accepted", "event": event_type, "delivery_id": delivery_id},
                    status=202,
                )
        finally:
            semaphore.release()
