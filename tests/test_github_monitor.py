from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from github_monitor import GitHubMonitorPlugin  # noqa: E402
from sirius_pulse.plugins.context import PluginDataStore


@pytest.mark.asyncio
async def test_first_poll_only_initializes_cursor(monkeypatch, tmp_path):
    payload = [
        {
            "id": "event-2",
            "type": "IssuesEvent",
            "actor": {"login": "alice"},
            "payload": {"action": "opened", "issue": {"title": "Old issue"}},
        }
    ]
    sent: list[dict] = []
    store = PluginDataStore(tmp_path, "github_monitor")
    plugin = GitHubMonitorPlugin()

    async def dispatch(**kwargs):
        sent.append(kwargs)

    plugin._ctx = SimpleNamespace(
        config={}, data_store=store, dispatch_proactive_message=dispatch
    )

    async def fake_fetch(*_args, **_kwargs):
        return payload

    monkeypatch.setattr("github_monitor.fetch_repo_events", fake_fetch)

    count = await plugin._poll_repo(object(), "owner", "repo", ["g1"], {"issues"})

    assert count == 0
    assert sent == []
    assert store.get("last_event_id:owner/repo") == "event-2"


@pytest.mark.asyncio
async def test_poll_sends_new_enabled_events(monkeypatch, tmp_path):
    payload = [
        {
            "id": "event-2",
            "type": "IssuesEvent",
            "actor": {"login": "alice"},
            "payload": {"action": "opened", "issue": {"title": "New issue"}},
        },
        {
            "id": "event-1",
            "type": "PushEvent",
            "actor": {"login": "bob"},
            "payload": {"commits": [{"message": "change"}]},
        },
    ]
    sent: list[dict] = []
    store = PluginDataStore(tmp_path, "github_monitor")
    store.set("last_event_id:owner/repo", "event-1")
    plugin = GitHubMonitorPlugin()

    async def dispatch(**kwargs):
        sent.append(kwargs)

    plugin._ctx = SimpleNamespace(
        config={}, data_store=store, dispatch_proactive_message=dispatch
    )

    async def fake_fetch(*_args, **_kwargs):
        return payload

    monkeypatch.setattr("github_monitor.fetch_repo_events", fake_fetch)

    count = await plugin._poll_repo(object(), "owner", "repo", ["g1", "g2"], {"issues"})

    assert count == 2
    assert len(sent) == 2
    assert {item["group_id"] for item in sent} == {"g1", "g2"}
    assert all("New issue" in item["text"] for item in sent)
    assert all(item["event_id"] == "github:owner/repo:event-2" for item in sent)
    assert store.get("last_event_id:owner/repo") == "event-2"


def test_format_event_includes_repository_actor_action_and_title():
    text = GitHubMonitorPlugin._format_event(
        {
            "type": "PullRequestEvent",
            "actor": {"display_login": "alice"},
            "payload": {
                "action": "opened",
                "pull_request": {"title": "Improve plugin API"},
            },
        },
        "owner/repo",
    )

    assert text == "【GitHub】owner/repo：alice opened Improve plugin API"
