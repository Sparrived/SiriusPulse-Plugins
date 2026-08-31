from __future__ import annotations

import asyncio
import hashlib
import hmac
import importlib.util
import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from aiohttp import ClientSession

_MODULE_PATH = Path(__file__).resolve().parents[1] / "github_monitor" / "webhook.py"
_SPEC = importlib.util.spec_from_file_location(
    "github_monitor_webhook_test", _MODULE_PATH
)
assert _SPEC is not None and _SPEC.loader is not None
webhook = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = webhook
_SPEC.loader.exec_module(webhook)


def _push_body(**extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "repository": {"full_name": "owner/repo"},
        "ref": "refs/heads/main",
        "commits": [],
    }
    body.update(extra)
    return body


def _signed_headers(body: dict[str, Any], event: str, delivery: str) -> dict[str, str]:
    raw = json.dumps(body, separators=(",", ":")).encode("utf-8")
    digest = hmac.new(b"test-secret", raw, hashlib.sha256).hexdigest()
    return {
        "X-Hub-Signature-256": f"sha256={digest}",
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery,
    }


async def _post(
    port: int, body: dict[str, Any], event: str, delivery: str
) -> tuple[int, dict[str, Any]]:
    raw = json.dumps(body, separators=(",", ":")).encode("utf-8")
    async with ClientSession() as client:
        response = await client.post(
            f"http://127.0.0.1:{port}/webhook/github",
            data=raw,
            headers=_signed_headers(body, event, delivery),
        )
        return response.status, await response.json()


@pytest.mark.asyncio
async def test_oversized_body_returns_json_413_without_header_conflict():
    server = webhook.GitHubWebhookServer(
        secret="test-secret",
        max_body_bytes=32,
        request_timeout_seconds=1,
    )
    server.add_handler("push", lambda _event, _body: asyncio.sleep(0))
    port = await server.start()
    try:
        status, payload = await _post(port, _push_body(), "push", "oversized")
        assert status == 413
        assert payload == {"error": "payload too large"}
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_review_and_commit_comment_schemas_are_accepted():
    seen: list[str] = []

    async def handler(event: str, _body: dict[str, Any]) -> None:
        seen.append(event)

    server = webhook.GitHubWebhookServer(secret="test-secret", worker_count=2)
    server.add_handler("pull_request_review_comment", handler)
    server.add_handler("commit_comment", handler)
    port = await server.start()
    try:
        review = {
            "repository": {"full_name": "owner/repo"},
            "action": "created",
            "pull_request": {},
            "comment": {},
        }
        commit = {
            "repository": {"full_name": "owner/repo"},
            "action": "created",
            "comment": {},
        }
        review_status, _ = await _post(
            port, review, "pull_request_review_comment", "review-1"
        )
        commit_status, _ = await _post(port, commit, "commit_comment", "commit-1")
        assert review_status == 202
        assert commit_status == 202
        await asyncio.wait_for(server._queue.join(), timeout=2)
        assert sorted(seen) == ["commit_comment", "pull_request_review_comment"]
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_comment_schema_rejects_wrong_or_missing_entity():
    server = webhook.GitHubWebhookServer(secret="test-secret")
    server.add_handler("issue_comment", lambda _event, _body: None)
    port = await server.start()
    try:
        wrong = {
            "repository": {"full_name": "owner/repo"},
            "action": "created",
            "pull_request": {},
        }
        status, payload = await _post(port, wrong, "issue_comment", "wrong-1")
        assert status == 400
        assert payload == {"error": "missing issue"}
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_body_json_depth_is_bounded_before_queueing():
    server = webhook.GitHubWebhookServer(secret="test-secret", max_json_depth=3)
    server.add_handler("push", lambda _event, _body: None)
    port = await server.start()
    try:
        body = _push_body(extra={"a": {"b": {"c": {"d": 1}}}})
        status, payload = await _post(port, body, "push", "deep-1")
        assert status == 400
        assert payload == {"error": "JSON nesting is too deep"}
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_admission_persists_before_202_and_recovers_after_restart(tmp_path):
    state_path = tmp_path / "webhook-state.json"
    server = webhook.GitHubWebhookServer(
        secret="test-secret", state_path=state_path, worker_count=1
    )
    blocker = asyncio.Event()

    async def handler(_event: str, _body: dict[str, Any]) -> None:
        await blocker.wait()

    server.add_handler("push", handler)
    port = await server.start()
    try:
        status, payload = await _post(port, _push_body(), "push", "durable-1")
        assert status == 202
        assert payload["status"] == "accepted"
        persisted = json.loads(state_path.read_text(encoding="utf-8"))
        assert persisted["pending"][0]["delivery_id"] == "durable-1"
        await server.stop()
    finally:
        blocker.set()
        if server._lifecycle_state == "running":
            await server.stop()

    seen: list[str] = []

    async def replay_handler(_event: str, _body: dict[str, Any]) -> None:
        seen.append("replayed")

    recovered = webhook.GitHubWebhookServer(secret="test-secret", state_path=state_path)
    recovered.add_handler("push", replay_handler)
    await recovered.start()
    try:
        await asyncio.wait_for(recovered._queue.join(), timeout=2)
        assert seen == ["replayed"]
    finally:
        await recovered.stop()


@pytest.mark.asyncio
async def test_restored_delivery_requires_registered_handler(tmp_path):
    state_path = tmp_path / "missing-handler.json"
    writer = webhook.GitHubWebhookServer(secret="test-secret", state_path=state_path)
    item = webhook._WebhookItem("push", _push_body(), "missing-1", "owner/repo")
    writer._pending_items[item.delivery_id] = item
    writer._pending_deliveries[item.delivery_id] = time.time()
    writer._persist_state()

    reader = webhook.GitHubWebhookServer(secret="test-secret", state_path=state_path)
    with pytest.raises(RuntimeError, match="durable state"):
        await reader.start()


@pytest.mark.asyncio
async def test_restore_pending_item_then_completes_after_handler_registration(tmp_path):
    state_path = tmp_path / "restore.json"
    writer = webhook.GitHubWebhookServer(secret="test-secret", state_path=state_path)
    item = webhook._WebhookItem("push", _push_body(), "recover-1", "owner/repo")
    writer._pending_items[item.delivery_id] = item
    writer._pending_deliveries[item.delivery_id] = time.time()
    writer._persist_state()

    seen: list[str] = []

    async def handler(event: str, _body: dict[str, Any]) -> None:
        seen.append(event)

    reader = webhook.GitHubWebhookServer(secret="test-secret", state_path=state_path)
    reader.add_handler("push", handler)
    await reader.start()
    try:
        await asyncio.wait_for(reader._queue.join(), timeout=2)
        assert seen == ["push"]
        persisted = json.loads(state_path.read_text(encoding="utf-8"))
        assert persisted["pending"] == []
        assert "recover-1" in persisted["completed"]
    finally:
        await reader.stop()


@pytest.mark.asyncio
async def test_dead_letter_state_is_persisted_without_payload_or_exception_text(
    tmp_path,
):
    state_path = tmp_path / "dead-letter-state.json"

    async def failing_handler(_event: str, _body: dict[str, Any]) -> None:
        raise RuntimeError("sensitive exception payload")

    server = webhook.GitHubWebhookServer(
        secret="test-secret",
        state_path=state_path,
        max_retry_attempts=1,
        retry_backoff_seconds=0,
    )
    server.add_handler("push", failing_handler)
    port = await server.start()
    try:
        status, _ = await _post(port, _push_body(), "push", "dead-1")
        assert status == 202
        await asyncio.wait_for(server._queue.join(), timeout=2)
        text = state_path.read_text(encoding="utf-8")
        persisted = json.loads(text)
        assert "dead-1" in persisted["dead_letters"]
        assert persisted["pending"] == []
        assert "sensitive exception payload" not in text
        assert "test-secret" not in text
    finally:
        await server.stop()


def test_active_pending_delivery_is_not_ttl_pruned():
    server = webhook.GitHubWebhookServer(secret="test-secret", replay_ttl_seconds=1)
    item = webhook._WebhookItem("push", _push_body(), "active-1", "owner/repo")
    server._pending_items[item.delivery_id] = item
    server._pending_deliveries[item.delivery_id] = time.time()

    server._prune_delivery_state(time.time() + 10_000)

    assert "active-1" in server._pending_deliveries
    assert "active-1" in server._pending_items


@pytest.mark.asyncio
async def test_handler_retry_preserves_successful_handler_progress():
    calls: list[str] = []
    second_attempt = False

    async def first(_event: str, _body: dict[str, Any]) -> None:
        calls.append("first")

    async def second(_event: str, _body: dict[str, Any]) -> None:
        nonlocal second_attempt
        calls.append("second")
        if not second_attempt:
            second_attempt = True
            raise RuntimeError("retry")

    server = webhook.GitHubWebhookServer(
        secret="test-secret", max_retry_attempts=1, retry_backoff_seconds=0
    )
    server.add_handler("push", first)
    server.add_handler("push", second)
    item = webhook._WebhookItem("push", _push_body(), "progress-1", "owner/repo")

    assert (await server._process_item(item))[0] is False
    assert (await server._process_item(item))[0] is True
    assert calls == ["first", "second", "second"]


@pytest.mark.asyncio
async def test_failed_dead_letter_transition_rolls_back_without_dual_state(monkeypatch):
    server = webhook.GitHubWebhookServer(secret="test-secret")
    item = webhook._WebhookItem("push", _push_body(), "rollback-1", "owner/repo")
    server._pending_items[item.delivery_id] = item
    server._pending_deliveries[item.delivery_id] = time.time()

    def fail_snapshot() -> None:
        raise webhook._StatePersistenceError("simulated")

    monkeypatch.setattr(server, "_persist_state", fail_snapshot)
    with pytest.raises(webhook._StatePersistenceError):
        server._fail_item(item, "handler failure")

    assert item.delivery_id in server._pending_items
    assert item.delivery_id in server._pending_deliveries
    assert item.delivery_id not in server._dead_letters
    assert item.delivery_id not in server._dead_letter_items


@pytest.mark.asyncio
async def test_dead_letter_replay_prunes_expired_entries_and_requires_handler():
    server = webhook.GitHubWebhookServer(secret="test-secret", replay_ttl_seconds=1)
    item = webhook._WebhookItem("push", _push_body(), "expired-dlq", "owner/repo")
    server._record_dead_letter(item, "handler failure")
    server._dead_letters[item.delivery_id] = (time.time() - 10, "handler_failure")
    server._lifecycle_state = "running"

    assert await server.retry_dead_letter(item.delivery_id) is False
    assert item.delivery_id not in server._dead_letters
    assert server._queue.empty()

    server._record_dead_letter(item, "handler failure")
    assert await server.retry_dead_letter(item.delivery_id) is False
    assert item.delivery_id in server._dead_letters
    assert server._queue.empty()


@pytest.mark.asyncio
async def test_handler_progress_is_reset_when_topology_changes():
    calls: list[str] = []

    async def old_handler(_event: str, _body: dict[str, Any]) -> None:
        calls.append("old")

    async def failing_handler(_event: str, _body: dict[str, Any]) -> None:
        raise RuntimeError("retry")

    async def replacement_handler(_event: str, _body: dict[str, Any]) -> None:
        calls.append("replacement")

    server = webhook.GitHubWebhookServer(secret="test-secret", max_retry_attempts=1)
    server.add_handler("push", old_handler)
    server.add_handler("push", failing_handler)
    item = webhook._WebhookItem("push", _push_body(), "topology-1", "owner/repo")

    assert (await server._process_item(item))[0] is False
    server._handlers["push"] = [replacement_handler]
    assert (await server._process_item(item))[0] is True
    assert calls == ["old", "replacement"]


@pytest.mark.asyncio
async def test_stop_retains_queued_and_inflight_items_and_rejects_after_stop():
    started = asyncio.Event()
    release = asyncio.Event()
    seen: list[str] = []

    async def handler(_event: str, body: dict[str, Any]) -> None:
        delivery_id = str(body["delivery_id"])
        seen.append(delivery_id)
        if delivery_id == "stop-1":
            started.set()
            await release.wait()

    server = webhook.GitHubWebhookServer(
        secret="test-secret",
        queue_size=2,
        worker_count=1,
        handler_timeout_seconds=10,
        shutdown_timeout_seconds=0.1,
    )

    # Put the delivery ID into the body only for test observation; the server
    # passes the original body unchanged to handlers.
    server.add_handler("push", handler)
    port = await server.start()
    try:
        first = _push_body(delivery_id="stop-1")
        second = _push_body(delivery_id="stop-2")
        first_status, _ = await _post(port, first, "push", "stop-1")
        second_status, _ = await _post(port, second, "push", "stop-2")
        assert first_status == 202
        assert second_status == 202
        await asyncio.wait_for(started.wait(), timeout=2)

        await server.stop()
        retained = {item.delivery_id for item in server._retained_items}
        assert retained == {"stop-1", "stop-2"}
        assert server._queue._unfinished_tasks == 0

        response = await server._handle_inner(object())
        assert response.status == 503

        release.set()
        await server.start()
        await asyncio.wait_for(server._queue.join(), timeout=2)
        assert sorted(seen) == ["stop-1", "stop-1", "stop-2"]
    finally:
        release.set()
        await server.stop()


@pytest.mark.asyncio
async def test_cancelled_stop_waits_for_worker_cleanup_before_publishing_stopped(
    monkeypatch,
):
    server = webhook.GitHubWebhookServer(secret="test-secret")
    server._lifecycle_state = "running"
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()

    async def stubborn_cleanup() -> None:
        cleanup_started.set()
        await release_cleanup.wait()

    monkeypatch.setattr(server, "_stop_workers", stubborn_cleanup)
    stop_task = asyncio.create_task(server.stop())
    await asyncio.wait_for(cleanup_started.wait(), timeout=1)
    stop_task.cancel()
    await asyncio.sleep(0)

    assert server._lifecycle_state == "stopping"
    assert not stop_task.done()

    release_cleanup.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(stop_task, timeout=1)
    assert server._lifecycle_state == "stopped"


@pytest.mark.asyncio
async def test_load_state_prune_write_failure_rolls_back_all_in_memory_state(
    tmp_path, monkeypatch
):
    state_path = tmp_path / "expired-state.json"
    writer = webhook.GitHubWebhookServer(secret="test-secret", state_path=state_path)
    writer._completed_deliveries["old-delivery"] = time.time() - 10_000
    writer._persist_state()

    reader = webhook.GitHubWebhookServer(
        secret="test-secret", state_path=state_path, replay_ttl_seconds=1
    )

    def fail_prune_snapshot() -> None:
        raise webhook._StatePersistenceError("simulated prune write failure")

    monkeypatch.setattr(reader, "_persist_state", fail_prune_snapshot)
    with pytest.raises(webhook._StateLoadError, match="prune"):
        reader._load_state()

    assert reader._state_loaded is False
    assert reader._pending_deliveries == {}
    assert reader._pending_items == {}
    assert reader._completed_deliveries == {}
    assert reader._dead_letters == {}
    assert list(reader._retained_items) == []


@pytest.mark.asyncio
async def test_concurrent_start_calls_share_one_lifecycle():
    server = webhook.GitHubWebhookServer(secret="test-secret")
    server.add_handler("push", lambda _event, _body: asyncio.sleep(0))
    ports = await asyncio.gather(server.start(), server.start())
    try:
        assert ports[0] == ports[1]
        assert len(server._workers) == server._worker_count
    finally:
        await server.stop()
