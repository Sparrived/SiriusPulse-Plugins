from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from github_monitor import event_bridge  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_event_bridge() -> None:
    """Keep module-level bridge state from crossing test cases."""
    event_bridge.reset_handlers()
    yield
    event_bridge.reset_handlers()


@pytest.mark.asyncio
async def test_owner_scoped_notifications_include_matching_and_legacy_handlers() -> None:
    received: list[tuple[str, str]] = []

    async def legacy_handler(_body: dict[str, object], repo_name: str) -> None:
        received.append(("legacy", repo_name))

    async def alpha_handler(_body: dict[str, object], repo_name: str) -> None:
        received.append(("alpha", repo_name))

    async def beta_handler(_body: dict[str, object], repo_name: str) -> None:
        received.append(("beta", repo_name))

    # This models an existing coding-agent integration which did not know
    # about owner scopes yet.
    event_bridge.register_issue_handler(legacy_handler)
    alpha_registration = event_bridge.register_issue_handler(
        alpha_handler, owner="persona-alpha"
    )
    event_bridge.register_issue_handler(beta_handler, owner="persona-beta")

    assert alpha_registration.owner == "persona-alpha"
    assert await event_bridge.notify_issue_opened({}, "org/repo", owner="persona-alpha")
    assert set(received) == {("legacy", "org/repo"), ("alpha", "org/repo")}

    received.clear()
    assert await event_bridge.notify_issue_opened({}, "org/repo", owner="persona-beta")
    assert set(received) == {("legacy", "org/repo"), ("beta", "org/repo")}

    received.clear()
    assert await event_bridge.notify_issue_opened({}, "org/repo")
    assert received == [("legacy", "org/repo")]

    alpha_registration.unregister()
    assert not alpha_registration.active
    received.clear()
    assert await event_bridge.notify_issue_opened({}, "org/repo", owner="persona-alpha")
    assert received == [("legacy", "org/repo")]


@pytest.mark.asyncio
async def test_owner_scoped_settings_and_cleanup_do_not_affect_other_personas() -> None:
    called: list[str] = []

    async def shared_handler(_body: dict[str, object], repo_name: str) -> None:
        called.append(repo_name)

    async def alpha_pr_handler(
        _body: dict[str, object], repo_name: str, _action: str
    ) -> None:
        called.append(f"pr:{repo_name}")

    alpha_issue = event_bridge.register_issue_handler(
        shared_handler, owner="persona-alpha"
    )
    beta_issue = event_bridge.register_issue_handler(
        shared_handler, owner="persona-beta"
    )
    alpha_pr = event_bridge.register_pr_handler(alpha_pr_handler, owner="persona-alpha")

    event_bridge.set_issue_repos({"legacy/repo"})
    event_bridge.set_issue_repos({"alpha/repo"}, owner="persona-alpha")
    event_bridge.set_issue_repos({"beta/repo"}, owner="persona-beta")
    event_bridge.set_coding_bot_login("legacy-bot")
    event_bridge.set_coding_bot_login("alpha-bot", owner="persona-alpha")
    event_bridge.set_coding_bot_login("beta-bot", owner="persona-beta")

    assert event_bridge.get_issue_repos() == frozenset({"legacy/repo"})
    assert event_bridge.get_issue_repos(owner="persona-alpha") == frozenset(
        {"alpha/repo"}
    )
    assert event_bridge.get_issue_repos(owner="persona-beta") == frozenset(
        {"beta/repo"}
    )
    assert event_bridge.get_coding_bot_login() == "legacy-bot"
    assert event_bridge.get_coding_bot_login(owner="persona-alpha") == "alpha-bot"
    assert event_bridge.get_coding_bot_login(owner="persona-beta") == "beta-bot"

    # Callback-based unregistration can target one owner even when the same
    # callable was registered for a different persona.
    event_bridge.unregister_issue_handler(shared_handler, owner="persona-alpha")
    assert not alpha_issue.active
    assert beta_issue.active

    assert await event_bridge.notify_issue_opened(
        {}, "alpha/repo", owner="persona-alpha"
    )
    assert called == []
    assert await event_bridge.notify_issue_opened({}, "beta/repo", owner="persona-beta")
    assert called == ["beta/repo"]

    event_bridge.reset_handlers(owner="persona-alpha")
    assert not alpha_pr.active
    assert beta_issue.active
    assert event_bridge.get_issue_repos(owner="persona-alpha") == frozenset()
    assert event_bridge.get_coding_bot_login(owner="persona-alpha") == ""
    assert event_bridge.get_issue_repos(owner="persona-beta") == frozenset(
        {"beta/repo"}
    )
    assert event_bridge.get_coding_bot_login(owner="persona-beta") == "beta-bot"
    assert event_bridge.get_issue_repos() == frozenset({"legacy/repo"})
    assert event_bridge.get_coding_bot_login() == "legacy-bot"

    assert await event_bridge.notify_pr_event(
        {}, "alpha/repo", "opened", owner="persona-alpha"
    )
    assert called == ["beta/repo"]


@pytest.mark.asyncio
async def test_notification_handlers_start_concurrently_and_return_ack() -> None:
    first_started = asyncio.Event()
    second_started = asyncio.Event()

    async def first_handler(_body: dict[str, object], _repo_name: str) -> None:
        first_started.set()
        await asyncio.wait_for(second_started.wait(), timeout=0.05)

    async def second_handler(_body: dict[str, object], _repo_name: str) -> None:
        second_started.set()
        await asyncio.wait_for(first_started.wait(), timeout=0.05)

    event_bridge.register_issue_handler(first_handler)
    event_bridge.register_issue_handler(second_handler)

    # A serial bridge would time out the first handler before the second starts.
    assert await event_bridge.notify_issue_opened({}, "org/repo")
    assert first_started.is_set()
    assert second_started.is_set()


@pytest.mark.asyncio
async def test_notification_timeout_is_bounded_and_returns_nack(monkeypatch) -> None:
    monkeypatch.setattr(event_bridge, "_HANDLER_TIMEOUT_SECONDS", 0.1)
    fast_completed = asyncio.Event()

    async def stalled_handler(_body: dict[str, object], _repo_name: str) -> None:
        await asyncio.sleep(60)

    async def fast_handler(_body: dict[str, object], _repo_name: str) -> None:
        fast_completed.set()

    event_bridge.register_issue_handler(stalled_handler)
    event_bridge.register_issue_handler(fast_handler)

    loop = asyncio.get_running_loop()
    started_at = loop.time()
    assert not await event_bridge.notify_issue_opened({}, "org/repo")
    assert fast_completed.is_set()
    # The stalled handler has an individual timeout, while the aggregate wait
    # is bounded by the same fixed timeout rather than handler count.
    assert loop.time() - started_at < 0.2
