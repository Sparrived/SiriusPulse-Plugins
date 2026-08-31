"""Lifecycle-aware bridge for GitHub events consumed by other plugins.

The module keeps the original module-level API for existing coding-agent
plugins.  Registrations and related coding-agent settings may additionally be
scoped to an owner (normally a plugin/persona identity).  An unscoped
registration is a compatibility registration: it receives events for every
owner, while scoped registrations receive only events for their own owner.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal

logger = logging.getLogger(__name__)

IssueHandler = Callable[[dict[str, Any], str], Awaitable[None]]
PrHandler = Callable[[dict[str, Any], str, str], Awaitable[None]]
CommentHandler = Callable[[dict[str, Any], str], Awaitable[None]]
HandlerKind = Literal["issue", "pr", "comment"]
_Handler = Callable[..., Any]

_HANDLER_TIMEOUT_SECONDS = 30.0
# Give independently timed handlers a short scheduling/cancellation grace while
# retaining a fixed upper bound for the complete fan-out.
_DISPATCH_TIMEOUT_GRACE_SECONDS = 0.05


# Registrations are records rather than bare callables so duplicate callbacks
# can be revoked independently and an unload cannot accidentally unregister a
# callback belonging to another owner.
_issue_handlers: list[HandlerRegistration] = []
_pr_handlers: list[HandlerRegistration] = []
_comment_handlers: list[HandlerRegistration] = []

# The empty owner is the legacy module-level scope.  Keeping settings in maps
# avoids a persona's configuration replacing another persona's values.
_issue_repos_by_owner: dict[str, set[str]] = {}
_coding_bot_logins_by_owner: dict[str, str] = {}


def _normalize_owner(owner: object) -> str:
    """Convert an optional owner value to its stable bridge scope."""
    return str(owner or "").strip()


def _handlers_for(kind: HandlerKind) -> list[HandlerRegistration]:
    """Return the registration list for one event kind."""
    if kind == "issue":
        return _issue_handlers
    if kind == "pr":
        return _pr_handlers
    return _comment_handlers


@dataclass(slots=True, eq=False)
class HandlerRegistration:
    """A revocable, owner-scoped event-bridge registration."""

    kind: HandlerKind
    handler: _Handler
    # Keep ``active`` before ``owner`` to retain positional compatibility with
    # the previous public dataclass constructor.
    active: bool = True
    owner: str = ""

    def __post_init__(self) -> None:
        self.owner = _normalize_owner(self.owner)

    def unregister(self) -> None:
        """Remove exactly this registration; repeated calls are harmless."""
        if not self.active:
            return
        handlers = _handlers_for(self.kind)
        for index, registration in enumerate(handlers):
            if registration is self:
                del handlers[index]
                break
        self.active = False


# Backwards-compatible alias for callers that prefer a shorter name.
RegistrationHandle = HandlerRegistration


def set_issue_repos(
    repos: set[str] | frozenset[str] | list[str] | tuple[str, ...], *, owner: str = ""
) -> None:
    """Declare repositories covered by an Issue handler in one owner scope.

    Calling this without ``owner`` updates the original, unscoped API state.
    """
    _issue_repos_by_owner[_normalize_owner(owner)] = {
        str(repo).strip() for repo in repos if str(repo).strip()
    }


def set_coding_bot_login(login: str, *, owner: str = "") -> None:
    """Set the coding-agent GitHub login in one owner scope.

    Calling this without ``owner`` updates the original, unscoped API state.
    """
    _coding_bot_logins_by_owner[_normalize_owner(owner)] = str(login or "").strip()


def get_coding_bot_login(*, owner: str = "") -> str:
    """Return the coding-agent login configured for one owner scope."""
    return _coding_bot_logins_by_owner.get(_normalize_owner(owner), "")


def get_issue_repos(*, owner: str = "") -> frozenset[str]:
    """Return an immutable repository snapshot for one owner scope."""
    return frozenset(_issue_repos_by_owner.get(_normalize_owner(owner), ()))


def _register_handler(
    kind: HandlerKind, handler: _Handler, owner: str
) -> HandlerRegistration:
    """Create and retain one owner-scoped registration."""
    registration = HandlerRegistration(kind, handler, owner=_normalize_owner(owner))
    _handlers_for(kind).append(registration)
    return registration


def register_issue_handler(
    handler: IssueHandler, *, owner: str = ""
) -> HandlerRegistration:
    """Register an Issue handler and return a lifecycle handle."""
    return _register_handler("issue", handler, owner)


def register_pr_handler(handler: PrHandler, *, owner: str = "") -> HandlerRegistration:
    """Register a Pull Request handler and return a lifecycle handle."""
    return _register_handler("pr", handler, owner)


def register_comment_handler(
    handler: CommentHandler, *, owner: str = ""
) -> HandlerRegistration:
    """Register an Issue/PR/commit comment handler and return its handle."""
    return _register_handler("comment", handler, owner)


def _unregister_handler(
    kind: HandlerKind,
    handler: _Handler | HandlerRegistration | None,
    owner: str | None,
) -> None:
    """Unregister a handle/callback, or all callbacks in an owner scope."""
    if isinstance(handler, HandlerRegistration):
        if handler.kind != kind:
            return
        if owner is None or handler.owner == _normalize_owner(owner):
            handler.unregister()
        return

    target_owner = _normalize_owner(owner)
    for registration in tuple(_handlers_for(kind)):
        if registration.owner != target_owner:
            continue
        if handler is not None and registration.handler is not handler:
            continue
        registration.unregister()
        # The legacy callback-based API removed one occurrence.  Handles are
        # available when callers need to remove one of several duplicates.
        if handler is not None:
            return


def unregister_issue_handler(
    handler: IssueHandler | HandlerRegistration | None = None,
    *,
    owner: str | None = None,
) -> None:
    """Remove an Issue handler, or all Issue handlers for ``owner``.

    A callback without an explicit owner targets the legacy empty-owner scope;
    a registration handle always revokes its own scope.
    """
    _unregister_handler("issue", handler, owner)


def unregister_pr_handler(
    handler: PrHandler | HandlerRegistration | None = None, *, owner: str | None = None
) -> None:
    """Remove a Pull Request handler, or all Pull Request handlers for ``owner``."""
    _unregister_handler("pr", handler, owner)


def unregister_comment_handler(
    handler: CommentHandler | HandlerRegistration | None = None,
    *,
    owner: str | None = None,
) -> None:
    """Remove a comment handler, or all comment handlers for ``owner``."""
    _unregister_handler("comment", handler, owner)


def reset_handlers(*, owner: str | None = None) -> None:
    """Reset bridge state globally or only for one owner.

    ``reset_handlers()`` retains its historical test/process-shutdown role and
    clears every registration and coding-agent setting.  Passing an owner
    removes only that owner's registrations, covered repositories, and bot
    login, which is suitable for a plugin/persona unload.
    """
    target_owner = _normalize_owner(owner) if owner is not None else None
    for handlers in (_issue_handlers, _pr_handlers, _comment_handlers):
        retained: list[HandlerRegistration] = []
        for registration in handlers:
            if target_owner is None or registration.owner == target_owner:
                registration.active = False
            else:
                retained.append(registration)
        handlers[:] = retained

    if target_owner is None:
        _issue_repos_by_owner.clear()
        _coding_bot_logins_by_owner.clear()
    else:
        _issue_repos_by_owner.pop(target_owner, None)
        _coding_bot_logins_by_owner.pop(target_owner, None)


def _matching_handlers(
    handlers: list[HandlerRegistration], owner: str
) -> tuple[HandlerRegistration, ...]:
    """Snapshot active handlers visible to a notification owner."""
    normalized_owner = _normalize_owner(owner)
    return tuple(
        registration
        for registration in handlers
        if registration.active
        and (registration.owner == normalized_owner or registration.owner == "")
    )


async def _run_handler(
    registration: HandlerRegistration,
    *args: Any,
    label: str,
) -> bool:
    """Run one handler with its own timeout and convert failures to NACKs."""
    # A registration may be revoked between the notification snapshot and task
    # execution.  It no longer owns work in that case, so it is acknowledged as
    # intentionally skipped rather than being invoked after plugin unload.
    if not registration.active:
        return True

    try:
        result = registration.handler(*args)
        if inspect.isawaitable(result):
            await asyncio.wait_for(result, timeout=_HANDLER_TIMEOUT_SECONDS)
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        logger.warning("%s handler timed out", label)
        return False
    except Exception:
        logger.exception("%s handler 异常", label)
        return False
    return True


def _cancel_tasks(tasks: set[asyncio.Task[bool]] | list[asyncio.Task[bool]]) -> None:
    """Cancel tasks without waiting for cancellation-insensitive handlers."""
    for task in tasks:
        if task.done():
            continue
        task.cancel()
        task.add_done_callback(_consume_cancelled_task)


def _consume_cancelled_task(task: asyncio.Task[bool]) -> None:
    """Retrieve a late task result so cancellation cannot emit task warnings."""
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception:
        # _run_handler normally converts handler failures to False.  This is a
        # defensive guard for unexpected failures during forced cleanup.
        logger.exception("event bridge handler cleanup failed")


async def _notify_handlers(
    handlers: list[HandlerRegistration],
    *args: Any,
    label: str,
    owner: str = "",
) -> bool:
    """Notify matching handlers concurrently and report aggregate acknowledgement.

    Every handler has its own timeout.  All tasks are started before waiting,
    and the enclosing wait has one fixed timeout plus a small scheduling grace,
    so dispatch cannot become N times slower as registrations grow or if
    cancellation is ignored.
    """
    registrations = _matching_handlers(handlers, owner)
    if not registrations:
        return True

    tasks = {
        asyncio.create_task(_run_handler(registration, *args, label=label))
        for registration in registrations
    }
    try:
        done, pending = await asyncio.wait(
            tasks,
            timeout=_HANDLER_TIMEOUT_SECONDS + _DISPATCH_TIMEOUT_GRACE_SECONDS,
        )
    except asyncio.CancelledError:
        _cancel_tasks(tasks)
        raise

    acknowledged = not pending
    if pending:
        logger.warning("%s handlers exceeded dispatch timeout", label)
        _cancel_tasks(pending)

    for task in done:
        try:
            if not task.result():
                acknowledged = False
        except asyncio.CancelledError:
            _cancel_tasks(tasks)
            raise
        except Exception:
            # _run_handler handles ordinary failures, but preserve a boolean
            # NACK if an unexpected task-level error still escapes.
            acknowledged = False
            logger.exception("%s handler task 异常", label)
    return acknowledged


async def notify_issue_opened(
    body: dict[str, Any], repo_name: str, *, owner: str = ""
) -> bool:
    """Notify Issue consumers in ``owner`` plus legacy empty-owner consumers."""
    return await _notify_handlers(
        _issue_handlers, body, repo_name, label=f"Issue({repo_name})", owner=owner
    )


async def notify_pr_event(
    body: dict[str, Any], repo_name: str, action: str, *, owner: str = ""
) -> bool:
    """Notify PR consumers in ``owner`` plus legacy empty-owner consumers."""
    return await _notify_handlers(
        _pr_handlers,
        body,
        repo_name,
        action,
        label=f"PR({repo_name}/{action})",
        owner=owner,
    )


async def notify_issue_comment(
    body: dict[str, Any], repo_name: str, *, owner: str = ""
) -> bool:
    """Notify comment consumers in ``owner`` plus legacy empty-owner consumers."""
    return await _notify_handlers(
        _comment_handlers, body, repo_name, label=f"Comment({repo_name})", owner=owner
    )


__all__ = [
    "HandlerRegistration",
    "RegistrationHandle",
    "get_coding_bot_login",
    "get_issue_repos",
    "notify_issue_comment",
    "notify_issue_opened",
    "notify_pr_event",
    "register_comment_handler",
    "register_issue_handler",
    "register_pr_handler",
    "reset_handlers",
    "set_coding_bot_login",
    "set_issue_repos",
    "unregister_comment_handler",
    "unregister_issue_handler",
    "unregister_pr_handler",
]
