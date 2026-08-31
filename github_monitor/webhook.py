"""Bounded, authenticated GitHub Webhook server.

The listener is intentionally local-only.  Deployments that receive GitHub
traffic from outside the host should terminate TLS in a trusted component and
forward to this loopback listener while retaining HMAC verification.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import ipaddress
import json
import logging
import marshal
import math
import os
import re
import tempfile
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any, Awaitable, Callable

from aiohttp import web
from aiohttp.web_protocol import RequestHandler as _AiohttpRequestHandler
from aiohttp.web_server import Server as _AiohttpServer

logger = logging.getLogger(__name__)

WebhookHandler = Callable[..., Any]
RepoFilter = Callable[[str], bool]
EventFilter = Callable[[str], bool]

_MAX_BODY_BYTES = 2 * 1024 * 1024
_MAX_QUEUE_SIZE = 64
_MAX_WORKERS = 8
_MAX_REPLAY_IDS = 4096
_MAX_DEAD_LETTERS = 1024
_MAX_HANDLERS_PER_EVENT = 256
# Dead-letter metadata is always retained within its count/TTL limit, while
# original bodies are retained only up to this aggregate budget for explicit
# local replay.  This avoids turning a burst of valid 2 MiB deliveries into an
# unbounded durable-data store.
_MAX_REPLAYABLE_DEAD_LETTER_BYTES = 32 * 1024 * 1024
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
_WORKER_CANCEL_GRACE_SECONDS = 1.0
_MAX_JSON_DEPTH = 64
_MAX_JSON_NODES = 100_000
_MAX_STATE_BYTES = 128 * 1024 * 1024
_MAX_STATE_NODES = 1_000_000
_MAX_STATE_JSON_DEPTH = 256
_GITHUB_REPO_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$"
)


class _WebhookValidationError(ValueError):
    """Raised when a webhook payload does not satisfy the basic contract."""


class _StatePersistenceError(RuntimeError):
    """Raised when a durable inbox snapshot cannot be written atomically."""


class _StateLoadError(RuntimeError):
    """Raised when a durable inbox snapshot is malformed or unsafe."""


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


def _scan_json_limits(text: str, max_depth: int, max_nodes: int) -> None:
    """Bound JSON structure before handing it to the recursive stdlib decoder.

    The scanner is deliberately lexical: strings and escaped characters are
    skipped, while containers and primitive tokens count toward the node
    budget.  The stdlib decoder still performs the authoritative syntax check.
    """

    depth = 0
    nodes = 0
    in_string = False
    escaped = False
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue

        if char == '"':
            in_string = True
            nodes += 1
        elif char in "[{":
            depth += 1
            if depth > max_depth:
                raise _WebhookValidationError("JSON nesting is too deep")
            nodes += 1
        elif char in "]}":
            depth -= 1
            if depth < 0:
                raise _WebhookValidationError("invalid JSON structure")
        elif char == "-" or char.isdigit():
            nodes += 1
            index += 1
            while index < length and text[index] not in ",]} \t\r\n":
                index += 1
            continue
        elif char in "tfn":
            nodes += 1
            index += 1
            while index < length and text[index].isalpha():
                index += 1
            continue

        if nodes > max_nodes:
            raise _WebhookValidationError("JSON node limit exceeded")
        index += 1

    if nodes > max_nodes:
        raise _WebhookValidationError("JSON node limit exceeded")


def _loads_bounded_json(
    raw: bytes,
    *,
    max_depth: int,
    max_nodes: int,
) -> Any:
    """Decode JSON only after applying bounded depth and node checks."""

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _WebhookValidationError("invalid JSON") from exc
    _scan_json_limits(text, max_depth, max_nodes)
    try:
        return json.loads(text, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise _WebhookValidationError("invalid JSON") from exc


def _is_loopback(host: str) -> bool:
    """Return whether *host* is a literal loopback address or localhost."""

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

    if (
        not isinstance(payload, bytes)
        or not isinstance(secret, str)
        or not secret.strip()
    ):
        return False
    if not isinstance(signature, str):
        return False
    signature = signature.strip()
    prefix = "sha256="
    if len(signature) != len(prefix) + 64 or not signature.startswith(prefix):
        return False
    digest = signature[len(prefix) :]
    try:
        int(digest, 16)
        secret_bytes = secret.encode("utf-8")
    except (ValueError, UnicodeEncodeError):
        return False
    expected = prefix + hmac.new(secret_bytes, payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


@dataclass(slots=True)
class _WebhookItem:
    event_type: str
    body: dict[str, Any]
    delivery_id: str
    repo_name: str


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

    def __init__(
        self, *args: Any, header_timeout_seconds: float, **kwargs: Any
    ) -> None:
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


class GitHubWebhookServer:
    """Bounded aiohttp server with retryable and optionally durable delivery."""

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
        state_path: str | os.PathLike[str] | None = None,
        max_json_depth: int = _MAX_JSON_DEPTH,
        max_json_nodes: int = _MAX_JSON_NODES,
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
        self._tls_enabled = _strict_bool(tls_enabled)
        self._loopback_bind = _is_loopback(self._host)
        self._lifecycle_lock = asyncio.Lock()
        self._admission_lock = asyncio.Lock()
        self._lifecycle_state = "stopped"
        self._ever_started = False
        self._allow_unsigned_local = (
            _strict_bool(allow_unsigned_local)
            and self._loopback_bind
            and self._secret_config_valid
        )

        self._max_body_bytes = _bounded_int(
            max_body_bytes, 1, _MAX_CONFIG_BODY_BYTES, _MAX_BODY_BYTES
        )
        self._queue_size = _bounded_int(
            queue_size, 1, _MAX_CONFIG_QUEUE_SIZE, _MAX_QUEUE_SIZE
        )
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
        self._max_retry_attempts = _bounded_int(
            max_retry_attempts, 1, 10, _MAX_RETRY_ATTEMPTS
        )
        self._retry_backoff_seconds = _finite_limit(
            retry_backoff_seconds,
            0.0,
            _MAX_RETRY_BACKOFF_SECONDS,
            _RETRY_BACKOFF_SECONDS,
        )
        self._rate_window_seconds = _finite_limit(
            rate_window_seconds,
            1.0,
            _MAX_CONFIG_RATE_WINDOW_SECONDS,
            _RATE_WINDOW_SECONDS,
        )
        self._rate_limit = _bounded_int(
            rate_limit, 1, _MAX_CONFIG_RATE_LIMIT, _RATE_LIMIT
        )
        self._max_concurrent_requests = _bounded_int(
            max_concurrent_requests, 1, _MAX_CONFIG_CONCURRENT_REQUESTS, 32
        )
        self._shutdown_timeout_seconds = _finite_limit(
            shutdown_timeout_seconds,
            0.1,
            _MAX_CONFIG_SHUTDOWN_TIMEOUT_SECONDS,
            _SHUTDOWN_TIMEOUT_SECONDS,
        )
        self._max_json_depth = _bounded_int(max_json_depth, 1, 256, _MAX_JSON_DEPTH)
        self._max_json_nodes = _bounded_int(
            max_json_nodes, 1, 2_000_000, _MAX_JSON_NODES
        )
        if state_path is None or not str(state_path).strip():
            self._state_path: Path | None = None
        else:
            self._state_path = Path(state_path)

        self._queue: asyncio.Queue[_WebhookItem] = asyncio.Queue(
            maxsize=self._queue_size
        )
        self._workers: list[asyncio.Task[None]] = []
        # Keep the old private name available to integrations/tests.
        self._worker: asyncio.Task[None] | None = None
        self._request_semaphore: asyncio.Semaphore | None = None
        self._worker_stop_event: asyncio.Event | None = None
        self._retained_items: deque[_WebhookItem] = deque(maxlen=_MAX_RETAINED_ITEMS)
        self._orphan_workers: set[asyncio.Task[None]] = set()
        self._handlers: dict[str, list[WebhookHandler]] = {}
        self._repo_filter: RepoFilter | None = None
        self._event_filter: EventFilter | None = None

        # _pending_items is the durable inbox's authoritative item registry.
        # It includes queued, retained, and currently-processing deliveries.
        self._pending_deliveries: dict[str, float] = {}
        self._pending_items: dict[str, _WebhookItem] = {}
        self._completed_deliveries: dict[str, float] = {}
        self._dead_letters: dict[str, tuple[float, str]] = {}
        # Retain a bounded redelivery record for dead letters.  This keeps a
        # GitHub 202 delivery recoverable after local retries are exhausted;
        # the snapshot can contain repository/user data and is therefore
        # documented as sensitive local state with best-effort 0600 mode.
        self._dead_letter_items: dict[str, _WebhookItem] = {}
        self._handler_progress: dict[str, set[int]] = {}
        # Handler ordering is an in-memory registration detail.  Persist a
        # bounded topology fingerprint alongside progress so a deployment that
        # replaces/reorders callbacks cannot silently skip a new handler.
        self._handler_progress_fingerprints: dict[str, str] = {}
        self._rate_state: dict[str, tuple[float, int]] = {}
        self._last_rate_prune = 0.0
        self._state_loaded = False
        # A failed snapshot must never be treated as a successful delivery
        # transition.  Admission will recover only after a later full snapshot
        # write succeeds.
        self._durability_failed = False
        self._state_io_lock = RLock()
        self._app = self._build_app()
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

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

    def set_repo_filter(self, filter_fn: RepoFilter | None) -> None:
        """Set an optional repository predicate used before queueing."""

        self._repo_filter = filter_fn

    def set_event_filter(self, filter_fn: EventFilter | None) -> None:
        """Set an optional event predicate used before queueing."""

        self._event_filter = filter_fn

    def add_handler(self, event_type: str, handler: WebhookHandler) -> None:
        """Register a handler for a GitHub event header.

        Async handlers are preferred, but a synchronous callback is accepted
        for compatibility; its return value follows the same ``None``/explicit
        ``False`` acknowledgement contract.
        """

        event = str(event_type).strip()
        if not event or len(event) > _MAX_EVENT_TYPE_LENGTH:
            raise ValueError("event_type is empty or too long")
        if not callable(handler):
            raise TypeError("webhook handler must be callable")
        handlers = self._handlers.setdefault(event, [])
        if len(handlers) >= _MAX_HANDLERS_PER_EVENT:
            raise ValueError("too many webhook handlers for event")
        handlers.append(handler)

    def _validate_restored_handlers(self) -> None:
        """Refuse startup if durable deliveries have no consumer yet.

        Marking a restored item complete merely because registration order was
        wrong would silently lose a GitHub delivery.  The caller can register
        handlers and retry ``start()`` without modifying the snapshot.
        """
        missing = sorted(
            {
                item.event_type
                for item in self._pending_items.values()
                if not self._handlers.get(item.event_type)
            }
        )
        if missing:
            raise RuntimeError(
                "GitHub Webhook handlers must be registered before restoring: "
                + ", ".join(missing)
            )

    # ------------------------------------------------------------------
    # Durable inbox
    # ------------------------------------------------------------------

    @staticmethod
    def _state_text(value: Any, maximum: int, *, allow_spaces: bool = False) -> str:
        if not isinstance(value, str) or not value or len(value) > maximum:
            raise _StateLoadError("invalid persisted text")
        minimum = 0x20 if allow_spaces else 0x21
        if any(ord(char) < minimum or ord(char) > 0x7E for char in value):
            raise _StateLoadError("invalid persisted text")
        return value

    def _handler_fingerprint(self, event_type: str) -> str:
        """Return a stable digest of the registered handler implementation order.

        Numeric progress is safe to restore only for the same callbacks in the
        same order.  Module/qualname alone is insufficient across deployments,
        because a function can keep its name while its implementation changes.
        Include a digest of its code object and an optional explicit version for
        callable objects whose implementation cannot be introspected.
        """
        parts: list[str] = []
        for handler in self._handlers.get(event_type, ()):
            module = str(getattr(handler, "__module__", "") or "")
            qualname = str(
                getattr(handler, "__qualname__", getattr(handler, "__name__", "")) or ""
            )
            explicit_version = str(
                getattr(handler, "__webhook_handler_version__", "") or ""
            )[:128]
            code = getattr(handler, "__code__", None)
            if code is None:
                call = getattr(handler, "__call__", None)
                code = getattr(call, "__code__", None)
            try:
                code_digest = (
                    hashlib.sha256(marshal.dumps(code)).hexdigest() if code else ""
                )
            except (TypeError, ValueError):
                code_digest = ""
            parts.append(
                ":".join(
                    (
                        module,
                        qualname,
                        self._handler_delivery_id_mode(handler),
                        explicit_version,
                        code_digest,
                    )
                )
            )
        return hashlib.sha256(
            "|".join(parts).encode("utf-8", errors="replace")
        ).hexdigest()

    @staticmethod
    def _state_progress(value: Any) -> set[int]:
        if value is None:
            return set()
        if not isinstance(value, list) or len(value) > 256:
            raise _StateLoadError("invalid persisted handler progress")
        progress: set[int] = set()
        for index in value:
            if not isinstance(index, int) or isinstance(index, bool) or index < 0:
                raise _StateLoadError("invalid persisted handler progress")
            progress.add(index)
        return progress

    def _state_snapshot(self) -> dict[str, Any]:
        pending: list[dict[str, Any]] = []
        for delivery_id in sorted(self._pending_items):
            item = self._pending_items[delivery_id]
            pending.append(
                {
                    "delivery_id": item.delivery_id,
                    "event_type": item.event_type,
                    "repo_name": item.repo_name,
                    "body": item.body,
                    "progress": sorted(self._handler_progress.get(delivery_id, set())),
                    "handler_fingerprint": self._handler_progress_fingerprints.get(
                        delivery_id, self._handler_fingerprint(item.event_type)
                    ),
                }
            )
        dead_letters = {
            delivery_id: {
                "timestamp": timestamp,
                "reason": reason,
                "progress": sorted(self._handler_progress.get(delivery_id, set())),
                "handler_fingerprint": self._handler_progress_fingerprints.get(
                    delivery_id,
                    self._handler_fingerprint(
                        self._dead_letter_items[delivery_id].event_type
                    )
                    if delivery_id in self._dead_letter_items
                    else "",
                ),
                **(
                    {
                        "event_type": item.event_type,
                        "repo_name": item.repo_name,
                        "body": item.body,
                    }
                    if (item := self._dead_letter_items.get(delivery_id)) is not None
                    else {}
                ),
            }
            for delivery_id, (timestamp, reason) in sorted(self._dead_letters.items())
        }
        return {
            "version": 1,
            "pending": pending,
            "completed": {
                delivery_id: timestamp
                for delivery_id, timestamp in sorted(self._completed_deliveries.items())
            },
            "dead_letters": dead_letters,
        }

    def _persist_state(self) -> None:
        """Atomically write the durable inbox without logging payload contents."""

        path = self._state_path
        if path is None:
            return
        with self._state_io_lock:
            try:
                encoded = json.dumps(
                    self._state_snapshot(),
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            except (TypeError, ValueError, RecursionError) as exc:
                raise _StatePersistenceError("state serialization failed") from exc
            if len(encoded) > _MAX_STATE_BYTES:
                raise _StatePersistenceError("state size limit exceeded")

            temporary_path: str | None = None
            try:
                parent = path.parent
                parent.mkdir(parents=True, exist_ok=True)
                descriptor, temporary_path = tempfile.mkstemp(
                    prefix=f".{path.name}.", suffix=".tmp", dir=str(parent)
                )
                os.chmod(temporary_path, 0o600)
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary_path, path)
                temporary_path = None
                try:
                    os.chmod(path, 0o600)
                except OSError:
                    pass
                if os.name != "nt":
                    directory_flag = getattr(os, "O_DIRECTORY", 0)
                    try:
                        directory_fd = os.open(
                            str(parent), os.O_RDONLY | directory_flag
                        )
                    except OSError:
                        directory_fd = -1
                    if directory_fd >= 0:
                        try:
                            os.fsync(directory_fd)
                        except OSError:
                            # ``os.replace`` already committed the visible
                            # snapshot.  Do not tell callers it failed and make
                            # them restore old in-memory maps, which would fork
                            # memory from disk.  A filesystem that cannot sync
                            # its directory still needs operator attention for
                            # crash-durability guarantees.
                            logger.warning(
                                "Webhook state directory fsync failed after atomic replace"
                            )
                        finally:
                            os.close(directory_fd)
            except (OSError, ValueError) as exc:
                raise _StatePersistenceError("state write failed") from exc
            finally:
                if temporary_path is not None:
                    try:
                        os.unlink(temporary_path)
                    except OSError:
                        pass

    def _persist_state_safe(self, operation: str) -> bool:
        try:
            self._persist_state()
        except Exception as exc:
            logger.error(
                "Webhook durable state update failed (operation=%s, error_type=%s)",
                operation,
                type(exc).__name__,
            )
            return False
        return True

    def _capture_delivery_state(
        self,
    ) -> tuple[
        dict[str, float],
        dict[str, _WebhookItem],
        dict[str, float],
        dict[str, tuple[float, str]],
        dict[str, _WebhookItem],
        dict[str, set[int]],
        dict[str, str],
    ]:
        """Capture the mutable durable-delivery maps before a state transition."""
        return (
            dict(self._pending_deliveries),
            dict(self._pending_items),
            dict(self._completed_deliveries),
            dict(self._dead_letters),
            dict(self._dead_letter_items),
            {key: set(value) for key, value in self._handler_progress.items()},
            dict(self._handler_progress_fingerprints),
        )

    def _restore_delivery_state(
        self,
        backup: tuple[
            dict[str, float],
            dict[str, _WebhookItem],
            dict[str, float],
            dict[str, tuple[float, str]],
            dict[str, _WebhookItem],
            dict[str, set[int]],
            dict[str, str],
        ],
    ) -> None:
        """Restore a pre-transition snapshot after a failed atomic write."""
        (
            pending_deliveries,
            pending_items,
            completed_deliveries,
            dead_letters,
            dead_letter_items,
            handler_progress,
            handler_progress_fingerprints,
        ) = backup
        self._pending_deliveries = pending_deliveries
        self._pending_items = pending_items
        self._completed_deliveries = completed_deliveries
        self._dead_letters = dead_letters
        self._dead_letter_items = dead_letter_items
        self._handler_progress = handler_progress
        self._handler_progress_fingerprints = handler_progress_fingerprints

    def _load_state(self) -> None:
        """Load and validate the durable inbox exactly once."""

        if self._state_loaded:
            return
        path = self._state_path
        if path is None:
            self._state_loaded = True
            return
        # Do not partially merge a corrupt snapshot (or a failed prune rewrite)
        # into a running in-memory inbox.  The only visible result of a failed
        # load is the original state and a retryable startup failure.
        backup = self._capture_delivery_state()
        retained_backup = deque(self._retained_items, maxlen=_MAX_RETAINED_ITEMS)
        state_loaded_before = self._state_loaded
        try:
            if not path.exists():
                self._state_loaded = True
                return
            if path.stat().st_size > _MAX_STATE_BYTES:
                raise _StateLoadError("state size limit exceeded")
            raw = path.read_bytes()
            data = _loads_bounded_json(
                raw,
                max_depth=_MAX_STATE_JSON_DEPTH,
                max_nodes=_MAX_STATE_NODES,
            )
            if not isinstance(data, Mapping) or data.get("version") != 1:
                raise _StateLoadError("unsupported state format")

            pending_data = data.get("pending", [])
            completed_data = data.get("completed", {})
            dead_data = data.get("dead_letters", {})
            if (
                not isinstance(pending_data, list)
                or not isinstance(completed_data, Mapping)
                or not isinstance(dead_data, Mapping)
            ):
                raise _StateLoadError("invalid state containers")
            if len(pending_data) > _MAX_RETAINED_ITEMS:
                raise _StateLoadError("too many pending deliveries")
            if len(completed_data) > _MAX_REPLAY_IDS:
                raise _StateLoadError("too many completed deliveries")
            if len(dead_data) > _MAX_DEAD_LETTERS:
                raise _StateLoadError("too many dead letters")

            pending_items: dict[str, _WebhookItem] = {}
            pending_times: dict[str, float] = {}
            progress_map: dict[str, set[int]] = {}
            fingerprint_map: dict[str, str] = {}
            now = time.time()
            for entry in pending_data:
                if not isinstance(entry, Mapping):
                    raise _StateLoadError("invalid pending delivery")
                delivery_id = self._state_text(
                    entry.get("delivery_id"), _MAX_DELIVERY_ID_LENGTH
                )
                event_type = self._state_text(
                    entry.get("event_type"), _MAX_EVENT_TYPE_LENGTH
                )
                repo_name = self._state_text(
                    entry.get("repo_name"), _MAX_REPO_NAME_LENGTH
                )
                body = entry.get("body")
                if not isinstance(body, dict):
                    raise _StateLoadError("invalid pending body")
                validated_repo = self._validate_payload(event_type, body)
                if validated_repo != repo_name:
                    raise _StateLoadError("pending repository mismatch")
                try:
                    body_json = json.dumps(
                        body,
                        ensure_ascii=False,
                        allow_nan=False,
                        separators=(",", ":"),
                    )
                    body_bytes = body_json.encode("utf-8")
                    _scan_json_limits(
                        body_json,
                        self._max_json_depth,
                        self._max_json_nodes,
                    )
                except (
                    TypeError,
                    ValueError,
                    RecursionError,
                    _WebhookValidationError,
                ) as exc:
                    raise _StateLoadError("invalid pending body") from exc
                if len(body_bytes) > self._max_body_bytes:
                    raise _StateLoadError("pending body too large")
                if delivery_id in pending_items:
                    raise _StateLoadError("duplicate pending delivery")
                progress = self._state_progress(entry.get("progress"))
                saved_fingerprint = entry.get("handler_fingerprint", "")
                if saved_fingerprint:
                    saved_fingerprint = self._state_text(saved_fingerprint, 64)
                current_fingerprint = self._handler_fingerprint(event_type)
                # Older snapshots lack a topology fingerprint.  Repeating a
                # successful handler is safer than silently skipping a newly
                # deployed/reordered handler at the same numeric index.
                if saved_fingerprint != current_fingerprint:
                    progress = set()
                pending_items[delivery_id] = _WebhookItem(
                    event_type, body, delivery_id, repo_name
                )
                pending_times[delivery_id] = now
                progress_map[delivery_id] = progress
                fingerprint_map[delivery_id] = current_fingerprint

            completed: dict[str, float] = {}
            for raw_id, raw_timestamp in completed_data.items():
                delivery_id = self._state_text(raw_id, _MAX_DELIVERY_ID_LENGTH)
                if (
                    not isinstance(raw_timestamp, (int, float))
                    or isinstance(raw_timestamp, bool)
                    or not math.isfinite(float(raw_timestamp))
                ):
                    raise _StateLoadError("invalid completed timestamp")
                if delivery_id in pending_items:
                    raise _StateLoadError("delivery appears in multiple states")
                completed[delivery_id] = float(raw_timestamp)

            dead_letters: dict[str, tuple[float, str]] = {}
            dead_letter_items: dict[str, _WebhookItem] = {}
            for raw_id, raw_entry in dead_data.items():
                delivery_id = self._state_text(raw_id, _MAX_DELIVERY_ID_LENGTH)
                if not isinstance(raw_entry, Mapping):
                    raise _StateLoadError("invalid dead letter")
                timestamp = raw_entry.get("timestamp")
                reason = self._state_text(
                    raw_entry.get("reason", "handler failure"), 100, allow_spaces=True
                )
                if (
                    not isinstance(timestamp, (int, float))
                    or isinstance(timestamp, bool)
                    or not math.isfinite(float(timestamp))
                ):
                    raise _StateLoadError("invalid dead-letter timestamp")
                if delivery_id in pending_items or delivery_id in completed:
                    raise _StateLoadError("delivery appears in multiple states")
                dead_letters[delivery_id] = (float(timestamp), reason)
                progress_map[delivery_id] = self._state_progress(
                    raw_entry.get("progress")
                )
                saved_fingerprint = raw_entry.get("handler_fingerprint", "")
                if saved_fingerprint:
                    saved_fingerprint = self._state_text(saved_fingerprint, 64)
                # Snapshots written by earlier versions have metadata-only dead
                # letters.  Keep them visible but only allow replay when the
                # bounded original delivery was persisted.
                if any(key in raw_entry for key in ("event_type", "repo_name", "body")):
                    event_type = self._state_text(
                        raw_entry.get("event_type"), _MAX_EVENT_TYPE_LENGTH
                    )
                    repo_name = self._state_text(
                        raw_entry.get("repo_name"), _MAX_REPO_NAME_LENGTH
                    )
                    body = raw_entry.get("body")
                    if not isinstance(body, dict):
                        raise _StateLoadError("invalid dead-letter body")
                    if self._validate_payload(event_type, body) != repo_name:
                        raise _StateLoadError("dead-letter repository mismatch")
                    try:
                        body_json = json.dumps(
                            body,
                            ensure_ascii=False,
                            allow_nan=False,
                            separators=(",", ":"),
                        )
                        _scan_json_limits(
                            body_json, self._max_json_depth, self._max_json_nodes
                        )
                    except (
                        TypeError,
                        ValueError,
                        RecursionError,
                        _WebhookValidationError,
                    ) as exc:
                        raise _StateLoadError("invalid dead-letter body") from exc
                    if len(body_json.encode("utf-8")) > self._max_body_bytes:
                        raise _StateLoadError("dead-letter body too large")
                    dead_letter_items[delivery_id] = _WebhookItem(
                        event_type, body, delivery_id, repo_name
                    )
                    current_fingerprint = self._handler_fingerprint(event_type)
                    if saved_fingerprint != current_fingerprint:
                        progress_map[delivery_id] = set()
                    fingerprint_map[delivery_id] = current_fingerprint
                else:
                    # Metadata-only legacy DLQs cannot be replayed, so retained
                    # numeric progress is immaterial and should not survive a
                    # future handler topology change.
                    progress_map[delivery_id] = set()

            # Merge only after the complete file has passed validation.  This
            # avoids partially restoring a corrupt state file.
            for delivery_id in (*pending_items, *completed, *dead_letters):
                if (
                    delivery_id in self._pending_items
                    or delivery_id in self._completed_deliveries
                    or delivery_id in self._dead_letters
                ):
                    raise _StateLoadError("delivery conflicts with in-memory state")
            self._pending_items.update(pending_items)
            self._pending_deliveries.update(pending_times)
            self._completed_deliveries.update(completed)
            self._dead_letters.update(dead_letters)
            self._dead_letter_items.update(dead_letter_items)
            self._handler_progress.update(progress_map)
            self._handler_progress_fingerprints.update(fingerprint_map)
            for item in pending_items.values():
                self._retained_items.append(item)
            self._state_loaded = True

            changed = self._prune_delivery_state(now)
            if changed:
                self._persist_state()
        except _StatePersistenceError as exc:
            self._restore_delivery_state(backup)
            self._retained_items = retained_backup
            self._state_loaded = state_loaded_before
            raise _StateLoadError("unable to prune persisted webhook state") from exc
        except _StateLoadError:
            self._restore_delivery_state(backup)
            self._retained_items = retained_backup
            self._state_loaded = state_loaded_before
            raise
        except _WebhookValidationError as exc:
            self._restore_delivery_state(backup)
            self._retained_items = retained_backup
            self._state_loaded = state_loaded_before
            raise _StateLoadError("invalid persisted webhook payload") from exc
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            RecursionError,
            ValueError,
        ) as exc:
            self._restore_delivery_state(backup)
            self._retained_items = retained_backup
            self._state_loaded = state_loaded_before
            raise _StateLoadError("unable to load persisted webhook state") from exc

    # ------------------------------------------------------------------
    # Lifecycle and queue management
    # ------------------------------------------------------------------

    def _retain_item(self, item: _WebhookItem) -> None:
        """Retain one dequeued item for a future start without dropping it."""

        if any(
            existing.delivery_id == item.delivery_id
            for existing in self._retained_items
        ):
            return
        if len(self._retained_items) >= _MAX_RETAINED_ITEMS:
            self._record_dead_letter(item, "retained-item-cap")
            current = self._pending_items.get(item.delivery_id)
            if current is item:
                self._pending_items.pop(item.delivery_id, None)
                self._pending_deliveries.pop(item.delivery_id, None)
            logger.error(
                "Webhook delivery moved to dead-letter state during shutdown (event=%s, repo=%s)",
                item.event_type,
                item.repo_name,
            )
            self._persist_state_safe("retained-item-cap")
            return
        self._retained_items.append(item)
        self._pending_deliveries.setdefault(item.delivery_id, time.time())
        self._pending_items.setdefault(item.delivery_id, item)

    def _restore_retained_items(self) -> None:
        """Move retained items back into the bounded queue after startup."""

        while self._retained_items and not self._queue.full():
            item = self._retained_items.popleft()
            if item.delivery_id in self._completed_deliveries:
                self._pending_items.pop(item.delivery_id, None)
                self._pending_deliveries.pop(item.delivery_id, None)
                self._handler_progress.pop(item.delivery_id, None)
                self._handler_progress_fingerprints.pop(item.delivery_id, None)
                continue
            try:
                self._queue.put_nowait(item)
            except asyncio.QueueFull:
                self._retained_items.appendleft(item)
                break

    async def start(self) -> int:
        """Start the listener and bounded worker pool serially."""

        async with self._lifecycle_lock:
            if self._lifecycle_state == "running":
                return self._port
            if self._lifecycle_state != "stopped":
                raise RuntimeError("GitHub Webhook server lifecycle is busy")
            if self._orphan_workers:
                raise RuntimeError("GitHub Webhook workers are still stopping")
            if not self._loopback_bind:
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
                self._allow_unsigned_local = False

            try:
                self._load_state()
                self._validate_restored_handlers()
            except Exception as exc:
                logger.error(
                    "Unable to load GitHub Webhook durable state (error_type=%s)",
                    type(exc).__name__,
                )
                raise RuntimeError(
                    "GitHub Webhook durable state could not be loaded"
                ) from None

            self._lifecycle_state = "starting"
            self._request_semaphore = asyncio.Semaphore(self._max_concurrent_requests)
            self._worker_stop_event = asyncio.Event()
            self._app = self._build_app()
            runner = _HeaderTimeoutAppRunner(
                self._app,
                header_timeout_seconds=self._request_timeout_seconds,
                shutdown_timeout=self._shutdown_timeout_seconds,
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
                actual_port = (
                    sockets[0].getsockname()[1] if sockets else self._configured_port
                )
                self._port = int(actual_port)
                # Restore before workers can run so startup is deterministic.
                self._restore_retained_items()
                # A configured durable inbox must be writable before this
                # listener is considered available to callers or workers.
                self._persist_state()
                self._durability_failed = False
                self._workers = [
                    self._spawn_worker(index) for index in range(self._worker_count)
                ]
                self._worker = self._workers[0] if self._workers else None
                self._ever_started = True
                self._lifecycle_state = "running"
                logger.info(
                    "GitHub Webhook 服务已启动: http://%s:%s/webhook/github",
                    self._host,
                    self._port,
                )
                return self._port
            except BaseException:
                self._lifecycle_state = "stopping"
                if self._worker_stop_event is not None:
                    self._worker_stop_event.set()
                workers = list(self._workers)
                for worker in workers:
                    worker.cancel()
                if workers:
                    await asyncio.gather(*workers, return_exceptions=True)
                self._workers.clear()
                self._worker = None
                try:
                    await runner.cleanup()
                except Exception as exc:
                    logger.error(
                        "Webhook startup cleanup failed (error_type=%s)",
                        type(exc).__name__,
                    )
                self._runner = None
                self._site = None
                self._request_semaphore = None
                self._worker_stop_event = None
                self._port = self._configured_port
                self._lifecycle_state = "stopped"
                raise

    async def stop(self) -> None:
        """Stop intake and workers while retaining unfinished deliveries."""

        async with self._lifecycle_lock:
            if self._lifecycle_state == "stopped":
                return
            if self._lifecycle_state != "running":
                raise RuntimeError("GitHub Webhook server lifecycle is busy")
            # This is the admission linearization point: no later request can
            # enqueue after the state changes to stopping.
            async with self._admission_lock:
                self._lifecycle_state = "stopping"

            site = self._site
            runner = self._runner

            async def _cleanup() -> BaseException | None:
                first_error: BaseException | None = None
                if site is not None:
                    try:
                        await site.stop()
                    except BaseException as exc:
                        first_error = exc
                        logger.error(
                            "Webhook listener stop failed (error_type=%s)",
                            type(exc).__name__,
                        )
                if runner is not None:
                    try:
                        await runner.cleanup()
                    except BaseException as exc:
                        if first_error is None:
                            first_error = exc
                        logger.error(
                            "Webhook runner cleanup failed (error_type=%s)",
                            type(exc).__name__,
                        )
                try:
                    await self._stop_workers()
                except BaseException as exc:
                    if first_error is None:
                        first_error = exc
                    logger.error(
                        "Webhook worker cleanup failed (error_type=%s)",
                        type(exc).__name__,
                    )
                return first_error

            # Shield cleanup from caller cancellation.  We still propagate the
            # cancellation, but only after the listener is closed and every
            # worker either exits or is explicitly tracked as an orphan.  This
            # prevents a cancelled stop() from publishing a false "stopped"
            # lifecycle while untracked old workers can mutate durable state.
            cleanup_task = asyncio.create_task(_cleanup())
            cancellation: asyncio.CancelledError | None = None
            first_error: BaseException | None = None
            try:
                while True:
                    try:
                        first_error = await asyncio.shield(cleanup_task)
                        break
                    except asyncio.CancelledError as exc:
                        cancellation = cancellation or exc
                        if cleanup_task.done():
                            first_error = cleanup_task.result()
                            break
                        continue
            finally:
                self._runner = None
                self._site = None
                self._request_semaphore = None
                self._worker_stop_event = None
                self._port = self._configured_port
                self._lifecycle_state = "stopped"
                self._persist_state_safe("stop")
                logger.info("GitHub Webhook 服务已停止")
            if cancellation is not None:
                raise cancellation
            if first_error is not None:
                raise first_error

    async def retry_dead_letter(self, delivery_id: str) -> bool:
        """Requeue one locally retained dead letter after operator remediation.

        This is intentionally an explicit API rather than an automatic endless
        retry: GitHub has already received ``202`` and local handler failures
        must remain observable.  Only dead letters written by this version have
        their bounded original payload available for replay.
        """
        normalized_id = str(delivery_id or "").strip()
        if not normalized_id:
            return False
        async with self._admission_lock:
            if self._lifecycle_state != "running" or self._durability_failed:
                return False
            backup = self._capture_delivery_state()
            if self._prune_delivery_state(time.time()):
                try:
                    self._persist_state()
                except _StatePersistenceError:
                    self._restore_delivery_state(backup)
                    self._durability_failed = True
                    return False
            item = self._dead_letter_items.get(normalized_id)
            if (
                item is None
                or self._queue.full()
                or not self._handlers.get(item.event_type)
            ):
                return False
            backup = self._capture_delivery_state()
            self._dead_letters.pop(normalized_id, None)
            self._dead_letter_items.pop(normalized_id, None)
            # Progress uses registration indexes; a manual replay can occur
            # after handlers changed, so rerun the bounded fan-out rather than
            # silently skipping a replacement callback at an old index.
            self._handler_progress.pop(normalized_id, None)
            self._handler_progress_fingerprints.pop(normalized_id, None)
            self._pending_items[normalized_id] = item
            self._pending_deliveries[normalized_id] = time.time()
            try:
                self._persist_state()
                self._queue.put_nowait(item)
            except asyncio.QueueFull:
                self._restore_delivery_state(backup)
                if not self._persist_state_safe("dead-letter-replay-rollback"):
                    self._durability_failed = True
                return False
            except _StatePersistenceError:
                self._restore_delivery_state(backup)
                self._durability_failed = True
                return False
            return True

    def _spawn_worker(self, worker_index: int) -> asyncio.Task[None]:
        worker = asyncio.create_task(
            self._run_worker(worker_index), name=f"github-webhook-worker-{worker_index}"
        )
        worker.add_done_callback(self._worker_finished)
        return worker

    def _worker_finished(self, worker: asyncio.Task[None]) -> None:
        """Replace an unexpectedly terminated worker while accepting traffic."""

        if (
            self._lifecycle_state != "running"
            or self._durability_failed
            or worker not in self._workers
        ):
            return
        self._workers.remove(worker)
        if worker is self._worker:
            self._worker = self._workers[0] if self._workers else None
        if worker.cancelled():
            logger.warning("GitHub Webhook worker cancelled; starting a replacement")
        else:
            try:
                error = worker.exception()
            except asyncio.CancelledError:
                error = None
            if error is not None:
                logger.error(
                    "GitHub Webhook worker exited; starting a replacement (error_type=%s)",
                    type(error).__name__,
                )
        replacement = self._spawn_worker(len(self._workers))
        self._workers.append(replacement)
        if self._worker is None:
            self._worker = replacement
        self._restore_retained_items()

    async def _stop_workers(self) -> None:
        workers = list(self._workers)
        if not workers:
            self._drain_queue_to_retained()
            self._persist_state_safe("shutdown-drain")
            return
        stop_event = self._worker_stop_event
        if stop_event is not None:
            stop_event.set()
        done, pending = await asyncio.wait(
            workers, timeout=self._shutdown_timeout_seconds
        )
        if pending:
            for worker in pending:
                worker.cancel()
            cancelled_done, still_running = await asyncio.wait(
                pending, timeout=_WORKER_CANCEL_GRACE_SECONDS
            )
            done.update(cancelled_done)
            for worker in still_running:
                logger.error(
                    "Webhook worker did not stop within the cancellation grace period"
                )
                self._orphan_workers.add(worker)
                worker.add_done_callback(self._orphan_worker_finished)
        for worker in done:
            self._consume_worker_result(worker)
        self._drain_queue_to_retained()
        self._workers.clear()
        self._worker = None
        self._persist_state_safe("shutdown")

    def _orphan_worker_finished(self, worker: asyncio.Task[None]) -> None:
        self._orphan_workers.discard(worker)
        self._consume_worker_result(worker)

    @staticmethod
    def _consume_worker_result(worker: asyncio.Task[None]) -> None:
        try:
            worker.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error(
                "Webhook worker stopped unexpectedly (error_type=%s)",
                type(exc).__name__,
            )

    def _drain_queue_to_retained(self) -> None:
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
        del worker_index
        stop_event = self._worker_stop_event
        while True:
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=0.1)
            except asyncio.TimeoutError:
                if stop_event is not None and stop_event.is_set():
                    return
                continue
            try:
                if self._durability_failed:
                    self._retain_item(item)
                    return
                succeeded, failure_reason = await self._process_item(item)
                if succeeded:
                    self._complete_item(item)
                else:
                    self._fail_item(item, failure_reason)
            except asyncio.CancelledError:
                self._retain_item(item)
                self._persist_state_safe("worker-cancel")
                raise
            except _StatePersistenceError:
                # The handler may already have performed an external side
                # effect, but we have no durable acknowledgement.  Stop local
                # consumption and preserve the inbox for an operator/restart;
                # this intentionally provides at-least-once, never silent-loss,
                # semantics under a storage outage.
                self._durability_failed = True
                self._retain_item(item)
                if stop_event is not None:
                    stop_event.set()
                logger.error(
                    "Webhook durable state failed; intake is paused (event=%s, repo=%s)",
                    item.event_type,
                    item.repo_name,
                )
                return
            except Exception as exc:
                try:
                    self._fail_item(item, type(exc).__name__)
                except _StatePersistenceError:
                    self._durability_failed = True
                    self._retain_item(item)
                    if stop_event is not None:
                        stop_event.set()
                logger.error(
                    "Webhook worker failed unexpectedly (event=%s, repo=%s, error_type=%s)",
                    item.event_type,
                    item.repo_name,
                    type(exc).__name__,
                )
            finally:
                self._queue.task_done()
                if (
                    self._lifecycle_state == "running"
                    and not self._durability_failed
                    and self._retained_items
                ):
                    self._restore_retained_items()

    def _complete_item(self, item: _WebhookItem) -> None:
        current = self._pending_items.get(item.delivery_id)
        if current is not None and current is not item:
            # A stale worker must never finalize a newer retry reservation.
            logger.warning("Ignoring stale webhook worker completion")
            return
        backup = self._capture_delivery_state()
        self._pending_items.pop(item.delivery_id, None)
        self._pending_deliveries.pop(item.delivery_id, None)
        self._completed_deliveries[item.delivery_id] = time.time()
        self._dead_letters.pop(item.delivery_id, None)
        self._dead_letter_items.pop(item.delivery_id, None)
        self._handler_progress.pop(item.delivery_id, None)
        self._handler_progress_fingerprints.pop(item.delivery_id, None)
        self._prune_delivery_state(time.time())
        try:
            self._persist_state()
        except _StatePersistenceError:
            # Do not acknowledge a completion that cannot survive a restart.
            self._durability_failed = True
            self._restore_delivery_state(backup)
            raise

    def _fail_item(self, item: _WebhookItem, reason: str = "handler failure") -> None:
        """Atomically transition one pending delivery into the dead-letter inbox."""
        current = self._pending_items.get(item.delivery_id)
        if current is not None and current is not item:
            logger.warning("Ignoring stale webhook worker failure")
            return
        # Capture *before* creating the dead letter.  A failed durable write
        # must restore the sole pre-transition pending state, never pending plus
        # DLQ for the same delivery ID.
        backup = self._capture_delivery_state()
        self._pending_items.pop(item.delivery_id, None)
        self._pending_deliveries.pop(item.delivery_id, None)
        self._record_dead_letter(item, reason)
        try:
            self._persist_state()
        except _StatePersistenceError:
            self._durability_failed = True
            self._restore_delivery_state(backup)
            raise

    @staticmethod
    def _handler_delivery_id_mode(handler: WebhookHandler) -> str:
        try:
            signature = inspect.signature(handler)
        except (TypeError, ValueError):
            return "none"
        parameters = list(signature.parameters.values())
        positional = [
            parameter
            for parameter in parameters
            if parameter.kind
            in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            )
        ]
        if any(
            parameter.kind == inspect.Parameter.VAR_POSITIONAL
            for parameter in parameters
        ):
            return "positional"
        if len(positional) >= 3:
            return "positional"
        if any(
            parameter.kind == inspect.Parameter.KEYWORD_ONLY
            and parameter.name == "delivery_id"
            for parameter in parameters
        ) or any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters
        ):
            return "keyword"
        return "none"

    async def _call_handler(self, handler: WebhookHandler, item: _WebhookItem) -> Any:
        mode = self._handler_delivery_id_mode(handler)
        if mode == "positional":
            result = handler(item.event_type, item.body, item.delivery_id)
        elif mode == "keyword":
            result = handler(item.event_type, item.body, delivery_id=item.delivery_id)
        else:
            result = handler(item.event_type, item.body)
        if inspect.isawaitable(result):
            return await result
        return result

    async def _process_item(self, item: _WebhookItem) -> tuple[bool, str]:
        """Run handlers independently and return success plus a failure reason."""

        handlers = tuple(self._handlers.get(item.event_type, ()))
        if not handlers:
            # A 202 without an actual handler is unrecoverable from GitHub's
            # perspective.  The caller performs the pending→DLQ state
            # transition as one transaction.
            return False, "missing handler"
        fingerprint = self._handler_fingerprint(item.event_type)
        if self._handler_progress_fingerprints.get(item.delivery_id) != fingerprint:
            self._handler_progress[item.delivery_id] = set()
            self._handler_progress_fingerprints[item.delivery_id] = fingerprint
        completed = self._handler_progress.setdefault(item.delivery_id, set())
        for index, handler in enumerate(handlers):
            if index in completed:
                continue
            last_error: Exception | None = None
            for attempt in range(1, self._max_retry_attempts + 1):
                try:
                    result = await asyncio.wait_for(
                        self._call_handler(handler, item),
                        timeout=self._handler_timeout_seconds,
                    )
                    # ``None`` is the historical successful return value;
                    # only an explicit False is a negative acknowledgement.
                    if result is False:
                        raise RuntimeError(
                            "webhook handler returned negative acknowledgement"
                        )
                    backup = self._capture_delivery_state()
                    completed.add(index)
                    # Persist progress so a process restart does not repeat a
                    # side effect that already completed successfully.  If the
                    # snapshot fails, roll memory back and pause intake; this
                    # deliberately becomes at-least-once rather than silently
                    # advancing an unrecorded external side effect.
                    try:
                        self._persist_state()
                    except _StatePersistenceError:
                        self._durability_failed = True
                        self._restore_delivery_state(backup)
                        raise
                    break
                except asyncio.CancelledError:
                    raise
                except _StatePersistenceError:
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
            if index not in completed:
                reason = (
                    type(last_error).__name__
                    if last_error is not None
                    else "handler failure"
                )
                logger.error(
                    "Webhook delivery moved to dead-letter state (attempts=%d, handler=%d, event=%s, repo=%s, error_type=%s)",
                    self._max_retry_attempts,
                    index,
                    item.event_type,
                    item.repo_name,
                    reason,
                )
                return False, reason
        return True, ""

    @staticmethod
    def _replayable_item_size(item: _WebhookItem) -> int:
        """Return the serialized body footprint, or zero when not serializable."""
        try:
            return len(
                json.dumps(
                    item.body,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
        except (TypeError, ValueError, RecursionError):
            return 0

    def _trim_dead_letter_replay_bodies(self) -> None:
        """Bound payload retention independently from dead-letter metadata."""
        total = sum(
            self._replayable_item_size(item)
            for item in self._dead_letter_items.values()
        )
        if total <= _MAX_REPLAYABLE_DEAD_LETTER_BYTES:
            return
        for delivery_id in sorted(
            self._dead_letter_items,
            key=lambda key: self._dead_letters.get(key, (0.0, ""))[0],
        ):
            item = self._dead_letter_items.pop(delivery_id)
            total -= self._replayable_item_size(item)
            if total <= _MAX_REPLAYABLE_DEAD_LETTER_BYTES:
                return

    def _record_dead_letter(self, item: _WebhookItem, reason: str) -> None:
        """Record a bounded, locally replayable terminal delivery failure."""
        raw_reason = str(reason).strip()
        # State text is deliberately ASCII-only; preserve a bounded class-like
        # diagnostic without letting arbitrary exception text/payload enter the
        # durable snapshot or make its own reader reject it.
        safe_reason = re.sub(r"[^A-Za-z0-9_.:-]+", "_", raw_reason)[:100].strip("_")
        safe_reason = safe_reason or "handler_failure"
        self._dead_letters[item.delivery_id] = (time.time(), safe_reason)
        self._dead_letter_items[item.delivery_id] = item
        if len(self._dead_letters) > _MAX_DEAD_LETTERS:
            oldest = sorted(
                self._dead_letters, key=lambda key: self._dead_letters[key][0]
            )
            for delivery_id in oldest[: len(self._dead_letters) - _MAX_DEAD_LETTERS]:
                self._dead_letters.pop(delivery_id, None)
                self._dead_letter_items.pop(delivery_id, None)
                self._handler_progress.pop(delivery_id, None)
                self._handler_progress_fingerprints.pop(delivery_id, None)
        self._trim_dead_letter_replay_bodies()

    def _prune_delivery_state(self, now: float | None = None) -> bool:
        current_time = time.time() if now is None else now
        cutoff = current_time - self._replay_ttl_seconds
        changed = False
        # Active pending reservations intentionally never expire.
        for delivery_id, timestamp in list(self._completed_deliveries.items()):
            if timestamp < cutoff:
                self._completed_deliveries.pop(delivery_id, None)
                self._handler_progress.pop(delivery_id, None)
                self._handler_progress_fingerprints.pop(delivery_id, None)
                changed = True
        for delivery_id, (timestamp, _reason) in list(self._dead_letters.items()):
            if timestamp < cutoff:
                self._dead_letters.pop(delivery_id, None)
                self._dead_letter_items.pop(delivery_id, None)
                self._handler_progress.pop(delivery_id, None)
                self._handler_progress_fingerprints.pop(delivery_id, None)
                changed = True
        if len(self._completed_deliveries) > _MAX_REPLAY_IDS:
            oldest = sorted(
                self._completed_deliveries, key=self._completed_deliveries.get
            )
            for delivery_id in oldest[
                : len(self._completed_deliveries) - _MAX_REPLAY_IDS
            ]:
                self._completed_deliveries.pop(delivery_id, None)
                self._handler_progress.pop(delivery_id, None)
                self._handler_progress_fingerprints.pop(delivery_id, None)
                changed = True
        return changed

    # ------------------------------------------------------------------
    # Request validation and admission
    # ------------------------------------------------------------------

    def _prune_rate_state(self, now: float) -> None:
        if (
            now - self._last_rate_prune < 1.0
            and len(self._rate_state) <= _MAX_RATE_KEYS
        ):
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

        if (
            request.content_length is not None
            and request.content_length > self._max_body_bytes
        ):
            raise web.HTTPRequestEntityTooLarge(
                max_size=self._max_body_bytes, actual_size=request.content_length
            )
        chunks: list[bytes] = []
        total = 0
        iterator = request.content.iter_chunked(64 * 1024)
        while True:
            try:
                chunk = await asyncio.wait_for(
                    iterator.__anext__(), self._request_timeout_seconds
                )
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
        if len(repo_name) > _MAX_REPO_NAME_LENGTH or not _GITHUB_REPO_RE.fullmatch(
            repo_name
        ):
            raise _WebhookValidationError("invalid repository full_name")

        sender = body.get("sender")
        if sender is not None and not isinstance(sender, Mapping):
            raise _WebhookValidationError("invalid sender")
        action = body.get("action")
        action_events = {
            "issues",
            "pull_request",
            "release",
            "issue_comment",
            "pull_request_review_comment",
            "commit_comment",
        }
        if event_type in action_events:
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
        remote = str(request.remote or "").strip().strip("[]")
        if not remote or remote.casefold() == "localhost":
            return False
        try:
            return ipaddress.ip_address(remote).is_loopback and self._loopback_bind
        except ValueError:
            return False

    @staticmethod
    def _retry_response(error: str, status: int = 503) -> web.Response:
        return web.json_response(
            {"error": error}, status=status, headers={"Retry-After": "1"}
        )

    async def _handle(self, request: web.Request) -> web.Response:
        """Apply the request deadline while processing one webhook request."""

        try:
            async with asyncio.timeout(self._request_timeout_seconds):
                return await self._handle_inner(request)
        except (asyncio.TimeoutError, web.HTTPRequestTimeout):
            return web.json_response({"error": "request timeout"}, status=408)

    async def _handle_inner(self, request: web.Request) -> web.Response:
        """Authenticate, validate, durably record, and enqueue one request."""

        if self._lifecycle_state != "running" and self._ever_started:
            return self._retry_response("webhook server is not accepting requests")
        if self._durability_failed:
            return self._retry_response("webhook state unavailable")
        try:
            self._load_state()
        except Exception as exc:
            logger.error(
                "Webhook durable state unavailable (error_type=%s)", type(exc).__name__
            )
            return self._retry_response("webhook state unavailable")

        remote = str(request.remote or "unknown").strip()
        if self._rate_limited(remote[:256], time.monotonic()):
            return self._retry_response("rate limit exceeded", status=429)

        semaphore = self._request_semaphore
        if semaphore is None:
            # Preserve direct pre-start invocation compatibility.  A real
            # listener always initializes this in start().
            semaphore = asyncio.Semaphore(self._max_concurrent_requests)
            self._request_semaphore = semaphore
        if semaphore.locked():
            return self._retry_response("request concurrency limit")
        await semaphore.acquire()
        try:
            encoding = request.headers.get("Content-Encoding", "").strip().casefold()
            if encoding not in {"", "identity"}:
                return web.json_response(
                    {"error": "content encoding not supported"}, status=415
                )
            try:
                async with asyncio.timeout(self._request_timeout_seconds):
                    body_bytes = await self._read_body(request)
            except web.HTTPRequestEntityTooLarge:
                # Do not pass aiohttp's Content-Type header to json_response:
                # doing so conflicts with its JSON content type and causes 500.
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
                self._header(request, "X-Hub-Signature-256")
                if len(event_type) > _MAX_EVENT_TYPE_LENGTH:
                    raise _WebhookValidationError("event header too long")
                if len(delivery_id) > _MAX_DELIVERY_ID_LENGTH:
                    raise _WebhookValidationError("delivery header too long")
            except _WebhookValidationError as exc:
                return web.json_response({"error": str(exc)}, status=400)

            try:
                body = _loads_bounded_json(
                    body_bytes,
                    max_depth=self._max_json_depth,
                    max_nodes=self._max_json_nodes,
                )
            except _WebhookValidationError as exc:
                return web.json_response({"error": str(exc)}, status=400)
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
                        "Webhook event filter failed (error_type=%s)",
                        type(exc).__name__,
                    )
                    return web.json_response(
                        {"error": "event filter failure"}, status=500
                    )
                if not event_allowed:
                    return web.json_response(
                        {"status": "ignored", "reason": "event filtered out"}
                    )
            if self._repo_filter is not None:
                try:
                    repo_allowed = bool(self._repo_filter(repo_name))
                except Exception as exc:
                    logger.error(
                        "Webhook repository filter failed (error_type=%s)",
                        type(exc).__name__,
                    )
                    return web.json_response(
                        {"error": "repository filter failure"}, status=500
                    )
                if not repo_allowed:
                    return web.json_response(
                        {"status": "ignored", "reason": "repo filtered out"}
                    )

            if not self._handlers.get(event_type):
                # GitHub must see a retryable failure rather than a successful
                # acknowledgement for a delivery that has no consumer.
                logger.error("Webhook delivery has no handler (event=%s)", event_type)
                return self._retry_response("webhook handler unavailable")

            async with self._admission_lock:
                accepting = self._lifecycle_state == "running" or (
                    self._lifecycle_state == "stopped" and not self._ever_started
                )
                if not accepting:
                    return self._retry_response(
                        "webhook server is not accepting requests"
                    )
                # A worker can discover a durable-write outage while this
                # request is reading/validating its body.  Recheck at the
                # actual admission linearization point.
                if self._durability_failed:
                    return self._retry_response("webhook state unavailable")
                prune_backup = self._capture_delivery_state()
                if self._prune_delivery_state(time.time()):
                    try:
                        self._persist_state()
                    except _StatePersistenceError:
                        self._restore_delivery_state(prune_backup)
                        self._durability_failed = True
                        logger.error(
                            "Webhook durable state prune could not be persisted"
                        )
                        return self._retry_response("webhook state unavailable")
                if delivery_id in self._pending_deliveries:
                    return web.json_response(
                        {"status": "duplicate", "delivery_id": delivery_id}, status=202
                    )
                if delivery_id in self._completed_deliveries:
                    return web.json_response(
                        {"status": "duplicate", "delivery_id": delivery_id}, status=200
                    )
                if self._queue.full():
                    return self._retry_response("webhook queue full")

                item = _WebhookItem(event_type, body, delivery_id, repo_name)
                backup = self._capture_delivery_state()
                self._dead_letters.pop(delivery_id, None)
                self._dead_letter_items.pop(delivery_id, None)
                self._pending_deliveries[delivery_id] = time.time()
                self._pending_items[delivery_id] = item
                try:
                    # Durable persistence is deliberately before the queue put
                    # and before the 202 response.  A crash after this point
                    # leaves a recoverable pending inbox record.
                    self._persist_state()
                    self._queue.put_nowait(item)
                except asyncio.QueueFull:
                    self._restore_delivery_state(backup)
                    # Queue capacity changed only after the snapshot write.  A
                    # best-effort rollback snapshot prevents stale pending state
                    # from suppressing GitHub's retry; if it cannot be written,
                    # stop accepting rather than report a false success.
                    if not self._persist_state_safe("admission-rollback"):
                        self._durability_failed = True
                    return self._retry_response("webhook queue full")
                except _StatePersistenceError:
                    self._restore_delivery_state(backup)
                    self._durability_failed = True
                    logger.error("Webhook durable state admission failed")
                    return self._retry_response("webhook state unavailable")
                return web.json_response(
                    {
                        "status": "accepted",
                        "event": event_type,
                        "delivery_id": delivery_id,
                    },
                    status=202,
                )
        finally:
            semaphore.release()
