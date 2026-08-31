"""Small, lifecycle-aware bridge for GitHub events consumed by other plugins.

The bridge remains import-compatible with the original module-level API, but
registrations now return handles that can be revoked during plugin unload.  A
handler is isolated behind a timeout so a coding-agent integration cannot
stall polling or the Webhook worker forever.
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

_HANDLER_TIMEOUT_SECONDS = 30.0

_issue_handlers: list[IssueHandler] = []
_pr_handlers: list[PrHandler] = []
_comment_handlers: list[CommentHandler] = []

# 哪些仓库注册了 Issue 处理器（由插件在 on_load 时填入）
_issue_repos: set[str] = set()
_coding_bot_login: str = ""


@dataclass(slots=True)
class HandlerRegistration:
    """A revocable event-bridge registration."""

    kind: HandlerKind
    handler: Callable[..., Awaitable[None]]
    active: bool = True

    def unregister(self) -> None:
        """Remove this handler; repeated calls are harmless."""
        if not self.active:
            return
        handlers: list[Callable[..., Awaitable[None]]]
        if self.kind == "issue":
            handlers = _issue_handlers
        elif self.kind == "pr":
            handlers = _pr_handlers
        else:
            handlers = _comment_handlers
        try:
            handlers.remove(self.handler)
        except ValueError:
            pass
        self.active = False


# Backwards-compatible alias for callers that prefer a shorter name.
RegistrationHandle = HandlerRegistration


def set_issue_repos(repos: set[str] | frozenset[str] | list[str] | tuple[str, ...]) -> None:
    """Declare repositories covered by a coding-agent Issue handler."""
    global _issue_repos
    _issue_repos = {str(repo).strip() for repo in repos if str(repo).strip()}


def set_coding_bot_login(login: str) -> None:
    """Set the GitHub login used by coding-agent comment filtering."""
    global _coding_bot_login
    _coding_bot_login = str(login or "").strip()


def get_coding_bot_login() -> str:
    """Return the configured coding-agent login."""
    return _coding_bot_login


def get_issue_repos() -> frozenset[str]:
    """Return an immutable snapshot of coding-agent-covered repositories."""
    return frozenset(_issue_repos)


def register_issue_handler(
    handler: IssueHandler, *, owner: str = ""
) -> HandlerRegistration:
    """Register an Issue handler and return a lifecycle handle."""
    del owner  # Reserved for a future per-persona bridge registry.
    _issue_handlers.append(handler)
    return HandlerRegistration("issue", handler)


def register_pr_handler(handler: PrHandler, *, owner: str = "") -> HandlerRegistration:
    """Register a Pull Request handler and return a lifecycle handle."""
    del owner
    _pr_handlers.append(handler)
    return HandlerRegistration("pr", handler)


def register_comment_handler(
    handler: CommentHandler, *, owner: str = ""
) -> HandlerRegistration:
    """Register an Issue/PR/commit comment handler."""
    del owner
    _comment_handlers.append(handler)
    return HandlerRegistration("comment", handler)


def unregister_issue_handler(handler: IssueHandler | HandlerRegistration) -> None:
    """Remove an Issue handler or registration handle."""
    if isinstance(handler, HandlerRegistration):
        handler.unregister()
        return
    try:
        _issue_handlers.remove(handler)
    except ValueError:
        pass


def unregister_pr_handler(handler: PrHandler | HandlerRegistration) -> None:
    """Remove a Pull Request handler or registration handle."""
    if isinstance(handler, HandlerRegistration):
        handler.unregister()
        return
    try:
        _pr_handlers.remove(handler)
    except ValueError:
        pass


def unregister_comment_handler(handler: CommentHandler | HandlerRegistration) -> None:
    """Remove a comment handler or registration handle."""
    if isinstance(handler, HandlerRegistration):
        handler.unregister()
        return
    try:
        _comment_handlers.remove(handler)
    except ValueError:
        pass


def reset_handlers() -> None:
    """Clear all registrations (primarily useful for test/process shutdown)."""
    _issue_handlers.clear()
    _pr_handlers.clear()
    _comment_handlers.clear()


async def _notify_handlers(
    handlers: list[Callable[..., Awaitable[None]]],
    *args: Any,
    label: str,
) -> bool:
    """Run a snapshot of handlers and report whether all acknowledged."""
    acknowledged = True
    for handler in tuple(handlers):
        try:
            result = handler(*args)
            if inspect.isawaitable(result):
                await asyncio.wait_for(result, timeout=_HANDLER_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception:
            acknowledged = False
            logger.exception("%s handler 异常", label)
    return acknowledged


async def notify_issue_opened(body: dict[str, Any], repo_name: str) -> bool:
    """Notify all currently registered Issue consumers."""
    return await _notify_handlers(_issue_handlers, body, repo_name, label=f"Issue({repo_name})")


async def notify_pr_event(body: dict[str, Any], repo_name: str, action: str) -> bool:
    """Notify all currently registered Pull Request consumers."""
    return await _notify_handlers(
        _pr_handlers,
        body,
        repo_name,
        action,
        label=f"PR({repo_name}/{action})",
    )


async def notify_issue_comment(body: dict[str, Any], repo_name: str) -> bool:
    """Notify all currently registered comment consumers."""
    return await _notify_handlers(
        _comment_handlers,
        body,
        repo_name,
        label=f"Comment({repo_name})",
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
