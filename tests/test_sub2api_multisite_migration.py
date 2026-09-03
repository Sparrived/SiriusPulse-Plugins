from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sub2api_monitor import (  # noqa: E402
    Sub2APIError,
    Sub2APIMonitorPlugin,
    parse_sources,
)

from sirius_pulse.plugins.context import PluginDataStore  # noqa: E402
from sirius_pulse.plugins.models import ArgNode, CommandAST  # noqa: E402


def _source(source_id: str, *, enabled: bool = True) -> dict[str, Any]:
    return {
        "id": source_id,
        "display_name": f"站点 {source_id}",
        "enabled": enabled,
        "base_url": f"https://{source_id}.example.invalid/portal",
        "api_base_path": "/api/v1",
        "login_path": "/auth/login",
        "refresh_path": "/auth/refresh",
        "logout_path": "/auth/logout",
        "subscriptions_path": "/your/subscriptions",
        "group_rates_path": "/your/rates",
        "timezone": "Asia/Shanghai",
        "timeout": 20,
        "allow_insecure_http": False,
        "inherit_notify_group_ids": True,
        "notify_group_ids": [],
    }


def _config(*sources: dict[str, Any]) -> dict[str, Any]:
    return {
        "sources": list(sources),
        "poll_seconds": 300,
        "notify_group_ids": ["10001"],
        "adapter_type": "napcat",
        "run_on_persona": "alice",
        "visual_report_enabled": False,
    }


def _plugin(tmp_path: Path, config: dict[str, Any], *, persona: str = "alice"):
    plugin = Sub2APIMonitorPlugin()
    plugin._ctx = SimpleNamespace(
        config=config,
        data_store=PluginDataStore(tmp_path, "sub2api_monitor"),
        engine=SimpleNamespace(get_persona_name=lambda: persona),
    )
    return plugin


def test_explicit_empty_sources_disables_legacy_fallback_and_reserved_selector_ids():
    assert (
        parse_sources(
            {
                "sources": [],
                "base_url": "https://legacy.example.invalid/portal",
                "subscriptions_path": "/legacy/subscriptions",
                "group_rates_path": "/legacy/rates",
            }
        )
        == []
    )
    with pytest.raises(Sub2APIError, match="保留字"):
        parse_sources({"sources": [_source("all")]})
    with pytest.raises(Sub2APIError, match="login_path"):
        parse_sources({"sources": [{**_source("alpha"), "login_path": ""}]})


@pytest.mark.asyncio
async def test_reset_all_removes_enabled_disabled_and_unconfigured_source_states(
    tmp_path,
):
    plugin = _plugin(
        tmp_path,
        _config(_source("enabled"), _source("disabled", enabled=False)),
    )
    store = plugin.get_data_store()
    store.update(
        {
            "source_states": {
                "enabled": {"subscriptions_snapshot": [{"id": "a"}]},
                "disabled": {"subscriptions_snapshot": [{"id": "b"}]},
                "removed_from_config": {"subscriptions_snapshot": [{"id": "c"}]},
            },
            "notification_cursors": {"subscriptions:legacy": 1},
        }
    )

    await plugin._reset_sources("all")

    assert store.get("source_states") == {}
    assert store.get("notification_cursors") is None


def test_single_source_migrates_matching_legacy_state_without_guessing(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("SUB2API_PRIMARY_EMAIL", "account@example.invalid")
    monkeypatch.setenv("SUB2API_PRIMARY_PASSWORD", "runtime-password")
    plugin = _plugin(tmp_path, _config(_source("primary")))
    source = plugin._sources()[0]
    old_subscription = [{"id": "plan", "name": "Legacy"}]
    old_rates = [{"id": "group", "rate_multiplier": 1}]
    subscription_source = plugin._legacy_source_fingerprint("subscriptions", source)
    rate_source = plugin._legacy_source_fingerprint("group_rates", source)
    legacy_event_key = plugin._legacy_notification_event_key(
        "subscriptions",
        subscription_source,
        "subscription_changed",
        {"id": "plan", "name": "Before"},
        {"id": "plan", "name": "After"},
    )
    store = plugin.get_data_store()
    store.update(
        {
            "subscriptions_snapshot": old_subscription,
            "subscriptions_source": subscription_source,
            "group_rates_snapshot": old_rates,
            "group_rates_source": rate_source,
            "notification_acks": {legacy_event_key: ["10001"]},
            "last_poll_success_at": 1_700_000_000,
        }
    )

    plugin._migrate_legacy_state_if_unambiguous(plugin._sources())

    migrated = store.get("source_states")["primary"]
    assert migrated["subscriptions_snapshot"] == old_subscription
    assert migrated["group_rates_snapshot"] == old_rates
    assert migrated["subscriptions_source"] == plugin._source_fingerprint(
        "subscriptions", source
    )
    assert migrated["group_rates_source"] == plugin._source_fingerprint(
        "group_rates", source
    )
    assert migrated["notification_acks"] == {legacy_event_key: ["10001"]}
    assert migrated["last_poll_success_at"] == 1_700_000_000
    assert store.get("legacy_state_migration")["status"] == "migrated"


def test_current_legacy_fingerprint_and_error_migrate_without_disclosure(
    tmp_path, monkeypatch
):
    email = "account@example.invalid"
    password = "runtime-password"
    monkeypatch.setenv("SUB2API_PRIMARY_EMAIL", email)
    monkeypatch.setenv("SUB2API_PRIMARY_PASSWORD", password)
    plugin = _plugin(tmp_path, _config(_source("primary")))
    source = plugin._sources()[0]
    versioned_source = plugin._provenance_fingerprint(
        "subscriptions", source, source_id="default"
    )
    store = plugin.get_data_store()
    store.update(
        {
            "subscriptions_snapshot": [{"id": "legacy"}],
            "subscriptions_source": versioned_source,
            "last_poll_error": f"remote reflected {email} and {password}",
        }
    )

    plugin._migrate_legacy_state_if_unambiguous(plugin._sources())

    migrated = store.get("source_states")["primary"]
    assert migrated["subscriptions_snapshot"] == [{"id": "legacy"}]
    assert migrated["last_poll_error"] == "迁移前最近一次轮询失败"
    assert email not in plugin._status_text("primary")
    assert password not in plugin._status_text("primary")


def test_migration_rejects_partial_parse_that_hides_a_second_source(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("SUB2API_ALPHA_EMAIL", "account@example.invalid")
    monkeypatch.setenv("SUB2API_ALPHA_PASSWORD", "runtime-password")
    malformed = {**_source("beta"), "login_path": ""}
    plugin = _plugin(tmp_path, _config(_source("alpha"), malformed))
    source = parse_sources({"sources": [_source("alpha")]})[0]
    store = plugin.get_data_store()
    store.update(
        {
            "subscriptions_snapshot": [{"id": "legacy"}],
            "subscriptions_source": plugin._legacy_source_fingerprint(
                "subscriptions", source
            ),
        }
    )
    usable, _errors = plugin._validate_config_partial()

    plugin._migrate_legacy_state_if_unambiguous(usable)

    assert store.get("source_states") is None
    assert store.get("legacy_state_migration") is None


def test_multisource_migration_never_guesses_a_legacy_target(tmp_path, monkeypatch):
    for source_id in ("ALPHA", "BETA"):
        monkeypatch.setenv(f"SUB2API_{source_id}_EMAIL", "account@example.invalid")
        monkeypatch.setenv(f"SUB2API_{source_id}_PASSWORD", "runtime-password")
    plugin = _plugin(tmp_path, _config(_source("alpha"), _source("beta")))
    store = plugin.get_data_store()
    store.update(
        {
            "subscriptions_snapshot": [{"id": "legacy"}],
            "subscriptions_source": "unknown-source",
        }
    )

    plugin._migrate_legacy_state_if_unambiguous(plugin._sources())

    assert store.get("source_states") is None
    assert store.get("legacy_state_migration") is None


@pytest.mark.asyncio
async def test_status_command_accepts_display_name_with_spaces(tmp_path):
    source = _source("alpha")
    source["display_name"] = "华东 主站"
    plugin = _plugin(tmp_path, _config(source))
    cmd = CommandAST(
        command="sub2api",
        raw_text="/sub2api status 华东 主站",
        args=[
            ArgNode(value="status", raw="status"),
            ArgNode(value="华东", raw="华东"),
            ArgNode(value="主站", raw="主站"),
        ],
    )

    response = (await plugin.execute_async(cmd))[0]

    assert response.success is True
    assert "[alpha] 华东 主站" in response.text


@pytest.mark.asyncio
async def test_stateful_commands_are_rejected_outside_designated_persona(tmp_path):
    plugin = _plugin(tmp_path, _config(_source("alpha")), persona="bob")
    for action in ("status", "poll", "subscriptions", "rates", "report", "reset"):
        cmd = CommandAST(
            command="sub2api",
            raw_text=f"/sub2api {action}",
            args=[ArgNode(value=action, raw=action)],
        )
        response = (await plugin.execute_async(cmd))[0]
        assert response.success is False
        assert "不是 Sub2API 监控执行者" in response.error


def test_historical_source_states_are_bounded_while_configured_state_is_kept(
    tmp_path,
):
    plugin = _plugin(tmp_path, _config(_source("active")))
    store = plugin.get_data_store()
    store.update(
        {
            "source_states": {
                f"removed_{index}": {"last_poll_success_at": index}
                for index in range(80)
            }
        }
    )
    source = plugin._sources()[0]

    plugin._save_source_state(source, {"last_poll_success_at": 999})

    states = store.get("source_states")
    assert len(states) == plugin._MAX_SOURCE_STATES
    assert states["active"]["last_poll_success_at"] == 999


def test_source_state_byte_limits_reject_oversized_single_and_aggregate_state(
    tmp_path,
    monkeypatch,
):
    plugin = _plugin(tmp_path, _config(_source("alpha"), _source("beta")))
    alpha, beta = plugin._sources()
    monkeypatch.setattr(plugin, "_MAX_SOURCE_STATE_BYTES", 128)

    with pytest.raises(Sub2APIError, match="单站监控状态超过"):
        plugin._save_source_state(
            alpha, {"subscriptions_snapshot": [{"name": "x" * 256}]}
        )

    monkeypatch.setattr(plugin, "_MAX_SOURCE_STATE_BYTES", 1024)
    monkeypatch.setattr(plugin, "_MAX_SOURCE_STATES_BYTES", 220)
    plugin._save_source_state(alpha, {"subscriptions_snapshot": [{"name": "a" * 64}]})
    with pytest.raises(Sub2APIError, match="多站监控状态超过"):
        plugin._save_source_state(
            beta, {"subscriptions_snapshot": [{"name": "b" * 128}]}
        )


def test_status_keeps_healthy_sources_visible_when_another_source_is_invalid(
    tmp_path,
):
    invalid = {**_source("broken"), "login_path": ""}
    plugin = _plugin(tmp_path, _config(_source("healthy"), invalid))

    status = plugin._status_text("all")

    assert "[healthy]" in status
    assert "login_path" in status
    assert "配置警告" in status
