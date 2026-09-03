from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sub2api_monitor.plugin as plugin_module  # noqa: E402
from sub2api_monitor import (  # noqa: E402
    SourceConfig,
    Sub2APIError,
    Sub2APIMonitorPlugin,
    parse_sources,
    parse_sources_partial,
    source_by_selector,
)
from sub2api_monitor.visual import (  # noqa: E402
    build_change_card_html,
    build_dashboard_html,
    prune_artifacts,
    validated_artifact_image,
)

from sirius_pulse.plugins.context import PluginDataStore  # noqa: E402


def _source(
    source_id: str,
    *,
    display_name: str = "",
    base_url: str | None = None,
    groups: list[str] | None = None,
    inherit_groups: bool = True,
    enabled: bool = True,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": source_id,
        "display_name": display_name,
        "enabled": enabled,
        "base_url": base_url or f"https://{source_id}.example.invalid/console",
        "api_base_path": "/api/v1",
        "login_path": "/auth/login",
        "refresh_path": "/auth/refresh",
        "logout_path": "/auth/logout",
        "subscriptions_path": "/monitor/subscriptions",
        "group_rates_path": "/monitor/rates",
        "timezone": "Asia/Shanghai",
        "timeout": 20,
        "allow_insecure_http": False,
        "inherit_notify_group_ids": inherit_groups,
        "notify_group_ids": groups or [],
    }
    return item


def _config(*sources: dict[str, Any]) -> dict[str, Any]:
    return {
        "sources": list(sources),
        "poll_seconds": 300,
        "notify_group_ids": ["global"],
        "adapter_type": "napcat",
        "run_on_persona": "alice",
        "visual_report_enabled": False,
    }


def test_source_parser_maps_ids_display_names_credentials_and_selectors(monkeypatch):
    sources = parse_sources(
        _config(
            _source("alpha", display_name="上海主站", groups=["alpha-only"]),
            _source("beta", display_name="上海主站", inherit_groups=False),
        )
    )
    monkeypatch.setenv("SUB2API_ALPHA_EMAIL", "alpha@example.invalid")
    monkeypatch.setenv("SUB2API_ALPHA_PASSWORD", " alpha password ")
    monkeypatch.setenv("SUB2API_BETA_EMAIL", "beta@example.invalid")
    monkeypatch.setenv("SUB2API_BETA_PASSWORD", "beta-password")

    assert isinstance(sources[0], SourceConfig)
    assert sources[0].display_name == "上海主站"
    assert sources[0].email_env == "SUB2API_ALPHA_EMAIL"
    assert sources[0].password_env == "SUB2API_ALPHA_PASSWORD"
    assert sources[0].credentials() == (
        "alpha@example.invalid",
        " alpha password ",
    )
    assert sources[1].credentials() == ("beta@example.invalid", "beta-password")
    assert source_by_selector(sources, "alpha", require_one=True) == [sources[0]]
    with pytest.raises(Sub2APIError, match="显示名称不唯一"):
        source_by_selector(sources, "上海主站", require_one=True)


@pytest.mark.parametrize(
    ("sources", "message"),
    [
        ([_source("Alpha")], "id"),
        ([_source("1alpha")], "id"),
        ([_source("alpha"), _source("alpha")], "重复"),
        ([{**_source("alpha"), "email": "bad@example.invalid"}], "未声明字段"),
        ([{**_source("alpha"), "enabled": "true"}], "enabled"),
        ([{**_source("alpha"), "allow_insecure_http": "false"}], "allow_insecure"),
        ([{**_source("alpha"), "inherit_notify_group_ids": "true"}], "inherit"),
        ([{**_source("alpha"), "notify_group_ids": [True]}], "notify_group_ids"),
        ([{**_source("alpha"), "timeout": float("inf")}], "timeout"),
    ],
)
def test_source_parser_rejects_unsafe_or_ambiguous_configuration(sources, message):
    with pytest.raises(Sub2APIError, match=message):
        parse_sources({"sources": sources})


def test_partial_source_parser_reports_malformed_disabled_entries():
    valid = _source("alpha")
    malformed_disabled = {
        "id": "disabled",
        "enabled": False,
        "base_url": "https://disabled.example.invalid",
    }

    sources, errors = parse_sources_partial({"sources": [valid, malformed_disabled]})

    assert [source.id for source in sources] == ["alpha"]
    assert any("subscriptions_path" in error for error in errors)


def test_source_group_inheritance_is_explicit(tmp_path):
    plugin = Sub2APIMonitorPlugin()
    plugin._ctx = SimpleNamespace(
        config=_config(
            _source("alpha", groups=["alpha-only"]),
            _source("beta", groups=[], inherit_groups=False),
        ),
        data_store=PluginDataStore(tmp_path, "sub2api_monitor"),
    )
    alpha, beta = plugin._sources()

    assert plugin._notify_groups(alpha) == ["global", "alpha-only"]
    assert plugin._notify_groups(beta) == []


@pytest.mark.asyncio
async def test_stale_clients_close_concurrently_and_each_timeout_is_bounded(
    tmp_path,
    monkeypatch,
):
    started = 0
    all_started = asyncio.Event()

    class HangingClient:
        async def aclose(self, *, logout: bool) -> None:
            nonlocal started
            if logout:
                started += 1
                if started == 4:
                    all_started.set()
                await all_started.wait()
            await asyncio.Event().wait()

    plugin = Sub2APIMonitorPlugin()
    plugin._ctx = SimpleNamespace(
        config=_config(),
        data_store=PluginDataStore(tmp_path, "sub2api_monitor"),
        logger=SimpleNamespace(warning=lambda *_args, **_kwargs: None),
    )
    plugin._clients = {f"removed_{index}": HangingClient() for index in range(4)}
    plugin._client_fingerprints = {source_id: "old" for source_id in plugin._clients}
    monkeypatch.setattr(Sub2APIMonitorPlugin, "_CLIENT_CLOSE_TIMEOUT_SECONDS", 0.01)

    await asyncio.wait_for(plugin._reconcile_clients([]), timeout=0.15)

    assert all_started.is_set()
    assert plugin._clients == {}
    assert plugin._client_fingerprints == {}


class _SourceClient:
    def __init__(
        self,
        subscriptions: list[list[dict[str, Any]] | BaseException],
        rates: list[list[dict[str, Any]] | BaseException],
    ) -> None:
        self.subscriptions = subscriptions
        self.rates = rates
        self.subscription_index = 0
        self.rate_index = 0
        self.closed = False

    async def fetch_subscriptions(self) -> list[dict[str, Any]]:
        value = self.subscriptions[self.subscription_index]
        self.subscription_index += 1
        if isinstance(value, BaseException):
            raise value
        return value

    async def fetch_group_rates(self) -> list[dict[str, Any]]:
        value = self.rates[self.rate_index]
        self.rate_index += 1
        if isinstance(value, BaseException):
            raise value
        return value

    async def aclose(self, *, logout: bool) -> None:
        assert logout is True
        self.closed = True


@pytest.mark.asyncio
async def test_multisite_poll_isolates_state_accounts_and_partial_failures(
    tmp_path, monkeypatch
):
    alpha_client = _SourceClient(
        subscriptions=[
            [{"id": "a", "name": "Alpha", "price": 10}],
            [{"id": "a", "name": "Alpha", "price": 20}],
        ],
        rates=[
            [{"id": "a-rate", "rate_multiplier": 1}],
            [{"id": "a-rate", "rate_multiplier": 2}],
        ],
    )
    beta_client = _SourceClient(
        subscriptions=[
            [{"id": "b", "name": "Beta", "price": 30}],
            Sub2APIError("beta-password must not leak"),
        ],
        rates=[
            [{"id": "b-rate", "rate_multiplier": 1}],
            Sub2APIError("beta rate unavailable"),
        ],
    )
    clients = {"alpha": alpha_client, "beta": beta_client}
    seen_credentials: dict[str, tuple[str, str]] = {}

    async def fake_get_client(self, source):
        seen_credentials[source.id] = source.credentials()
        return clients[source.id]

    monkeypatch.setattr(Sub2APIMonitorPlugin, "_get_client", fake_get_client)
    monkeypatch.setenv("SUB2API_ALPHA_EMAIL", "alpha@example.invalid")
    monkeypatch.setenv("SUB2API_ALPHA_PASSWORD", "alpha-password")
    monkeypatch.setenv("SUB2API_BETA_EMAIL", "beta@example.invalid")
    monkeypatch.setenv("SUB2API_BETA_PASSWORD", "beta-password")

    store = PluginDataStore(tmp_path, "sub2api_monitor")
    plugin = Sub2APIMonitorPlugin()
    plugin._ctx = SimpleNamespace(
        config=_config(_source("alpha"), _source("beta")),
        data_store=store,
    )

    initialized = await plugin.poll_once(notify=False)
    changed = await plugin.poll_once(notify=False)

    assert sorted(initialized.initialized) == [
        "alpha:group_rates",
        "alpha:subscriptions",
        "beta:group_rates",
        "beta:subscriptions",
    ]
    assert changed.subscription_changed == 1
    assert changed.rates_changed == 1
    assert any("beta" in error.casefold() for error in changed.errors)
    assert all("beta-password" not in error for error in changed.errors)
    assert seen_credentials == {
        "alpha": ("alpha@example.invalid", "alpha-password"),
        "beta": ("beta@example.invalid", "beta-password"),
    }

    states = store.get("source_states")
    assert states["alpha"]["subscriptions_snapshot"][0]["price"] == 20
    assert states["alpha"]["group_rates_snapshot"][0]["rate_multiplier"] == 2
    assert states["beta"]["subscriptions_snapshot"][0]["price"] == 30
    assert states["beta"]["group_rates_snapshot"][0]["rate_multiplier"] == 1
    persisted = json.dumps(store.all(), ensure_ascii=False)
    for secret in (
        "alpha@example.invalid",
        "alpha-password",
        "beta@example.invalid",
        "beta-password",
    ):
        assert secret not in persisted


@pytest.mark.asyncio
async def test_multisite_notification_acks_and_images_are_source_scoped(
    tmp_path, monkeypatch
):
    clients = {
        "alpha": _SourceClient(
            subscriptions=[
                [{"id": "a", "name": "A", "price": 1}],
                [{"id": "a", "name": "A", "price": 2}],
                [{"id": "a", "name": "A", "price": 2}],
            ],
            rates=[[], [], []],
        ),
        "beta": _SourceClient(
            subscriptions=[
                [{"id": "b", "name": "B", "price": 3}],
                [{"id": "b", "name": "B", "price": 4}],
                [{"id": "b", "name": "B", "price": 4}],
            ],
            rates=[[], [], []],
        ),
    }

    async def fake_get_client(self, source):
        return clients[source.id]

    async def fake_render_change_card(**kwargs):
        output = Path(kwargs["artifact_dir"]) / f"sub2api_{kwargs['source_id']}.png"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"\x89PNG\r\n\x1a\ncard")
        return str(output.resolve())

    monkeypatch.setattr(Sub2APIMonitorPlugin, "_get_client", fake_get_client)
    monkeypatch.setattr(plugin_module, "render_change_card", fake_render_change_card)
    for source_id in ("ALPHA", "BETA"):
        monkeypatch.setenv(f"SUB2API_{source_id}_EMAIL", f"{source_id}@example.invalid")
        monkeypatch.setenv(f"SUB2API_{source_id}_PASSWORD", f"{source_id}-password")

    calls: list[dict[str, Any]] = []
    beta_attempts = 0

    async def dispatch(**kwargs):
        nonlocal beta_attempts
        calls.append(kwargs)
        if kwargs["group_id"] == "beta-group":
            beta_attempts += 1
            return beta_attempts > 1
        return True

    store = PluginDataStore(tmp_path, "sub2api_monitor")
    plugin = Sub2APIMonitorPlugin()
    plugin._ctx = SimpleNamespace(
        config={
            **_config(
                _source(
                    "alpha",
                    display_name="甲站",
                    groups=["alpha-group"],
                    inherit_groups=False,
                ),
                _source(
                    "beta",
                    display_name="乙站",
                    groups=["beta-group"],
                    inherit_groups=False,
                ),
            ),
            "notify_group_ids": [],
            "visual_report_enabled": True,
        },
        data_store=store,
        dispatch_proactive_message=dispatch,
    )

    await plugin.poll_once()
    failed = await plugin.poll_once()

    assert failed.errors
    assert {(call["group_id"], Path(call["image_path"]).name) for call in calls} == {
        ("alpha-group", "sub2api_alpha.png"),
        ("beta-group", "sub2api_beta.png"),
    }
    assert any("【甲站】" in call["text"] for call in calls)
    assert any("【乙站】" in call["text"] for call in calls)
    assert all("alpha-group" not in call["event_id"] for call in calls)
    assert all("beta-group" not in call["event_id"] for call in calls)

    states = store.get("source_states")
    assert states["alpha"]["subscriptions_snapshot"][0]["price"] == 2
    assert states["beta"]["subscriptions_snapshot"][0]["price"] == 3
    assert states["beta"]["notification_acks"] == {}

    recovered = await plugin.poll_once()
    assert not recovered.errors
    assert [call["group_id"] for call in calls] == [
        "alpha-group",
        "beta-group",
        "beta-group",
    ]
    states = store.get("source_states")
    assert states["beta"]["subscriptions_snapshot"][0]["price"] == 4


@pytest.mark.asyncio
async def test_dispatch_budget_is_fair_across_sources_and_rotates_failed_groups(
    tmp_path, monkeypatch
):
    source_ids = ["alpha", "beta", "gamma", "delta", "epsilon"]
    clients = {
        source_id: _SourceClient(
            subscriptions=[
                [{"id": "plan", "name": source_id, "price": 1}],
                [{"id": "plan", "name": source_id, "price": 2}],
                [{"id": "plan", "name": source_id, "price": 2}],
            ],
            rates=[
                [{"id": "rate", "rate_multiplier": 1}],
                [{"id": "rate", "rate_multiplier": 1}],
                [{"id": "rate", "rate_multiplier": 1}],
            ],
        )
        for source_id in source_ids
    }

    async def fake_get_client(self, source):
        return clients[source.id]

    calls: list[str] = []

    async def dispatch(**kwargs):
        calls.append(kwargs["group_id"])
        return kwargs["group_id"].startswith("epsilon-")

    monkeypatch.setattr(Sub2APIMonitorPlugin, "_get_client", fake_get_client)
    monkeypatch.setattr(Sub2APIMonitorPlugin, "_MAX_DISPATCHES_PER_POLL", 20)
    for source_id in source_ids:
        env_prefix = source_id.upper()
        monkeypatch.setenv(
            f"SUB2API_{env_prefix}_EMAIL", f"{source_id}@example.invalid"
        )
        monkeypatch.setenv(f"SUB2API_{env_prefix}_PASSWORD", f"{source_id}-password")
    sources = [
        _source(
            source_id,
            groups=(
                [f"{source_id}-1", f"{source_id}-2", f"{source_id}-3"]
                if source_id == "alpha"
                else [f"{source_id}-1", f"{source_id}-2"]
            ),
            inherit_groups=False,
        )
        for source_id in source_ids
    ]
    store = PluginDataStore(tmp_path, "sub2api_monitor")
    plugin = Sub2APIMonitorPlugin()
    plugin._ctx = SimpleNamespace(
        config={
            **_config(*sources),
            "notify_group_ids": [],
            "visual_report_enabled": False,
        },
        data_store=store,
        dispatch_proactive_message=dispatch,
    )

    await plugin.poll_once()
    changed = await plugin.poll_once()

    assert {"epsilon-1", "epsilon-2"}.issubset(calls)
    assert changed.notifications_sent == 2
    assert (
        store.get("source_states")["epsilon"]["subscriptions_snapshot"][0]["price"] == 2
    )
    assert (
        store.get("source_states")["alpha"]["subscriptions_snapshot"][0]["price"] == 1
    )

    calls.clear()
    await plugin.poll_once()
    alpha_calls = [group_id for group_id in calls if group_id.startswith("alpha-")]
    assert alpha_calls == ["alpha-3", "alpha-1"]


@pytest.mark.asyncio
async def test_merged_256_group_ack_ledger_converges_across_budgeted_polls(
    tmp_path, monkeypatch
):
    client = _SourceClient(
        subscriptions=[
            [{"id": "plan", "price": 1}],
            [{"id": "plan", "price": 2}],
            [{"id": "plan", "price": 2}],
            [{"id": "plan", "price": 2}],
        ],
        rates=[[], [], [], []],
    )

    async def fake_get_client(self, source):
        return client

    calls: list[str] = []

    async def dispatch(**kwargs):
        calls.append(kwargs["group_id"])
        return True

    monkeypatch.setattr(Sub2APIMonitorPlugin, "_get_client", fake_get_client)
    monkeypatch.setenv("SUB2API_ALPHA_EMAIL", "alpha@example.invalid")
    monkeypatch.setenv("SUB2API_ALPHA_PASSWORD", "alpha-password")
    global_groups = [f"global-{index:03d}" for index in range(128)]
    dedicated_groups = [f"alpha-{index:03d}" for index in range(128)]
    store = PluginDataStore(tmp_path, "sub2api_monitor")
    plugin = Sub2APIMonitorPlugin()
    plugin._ctx = SimpleNamespace(
        config={
            **_config(
                _source(
                    "alpha",
                    groups=dedicated_groups,
                    inherit_groups=True,
                )
            ),
            "notify_group_ids": global_groups,
            "visual_report_enabled": False,
        },
        data_store=store,
        dispatch_proactive_message=dispatch,
    )

    await plugin.poll_once()
    first_retry = await plugin.poll_once()
    second_retry = await plugin.poll_once()
    converged = await plugin.poll_once()

    assert first_retry.errors
    assert second_retry.errors
    assert not converged.errors
    assert len(calls) == 256
    assert len(set(calls)) == 256
    state = store.get("source_states")["alpha"]
    assert state["subscriptions_snapshot"] == [{"id": "plan", "price": 2}]
    assert state["notification_acks"] == {}
    assert state["notification_cursors"] == {}


@pytest.mark.asyncio
async def test_cancelled_dispatch_persists_rotation_cursor_before_retry(
    tmp_path, monkeypatch
):
    client = _SourceClient(
        subscriptions=[
            [{"id": "plan", "price": 1}],
            [{"id": "plan", "price": 2}],
            [{"id": "plan", "price": 2}],
        ],
        rates=[[], [], []],
    )

    async def fake_get_client(self, source):
        return client

    first_calls: list[str] = []

    async def cancel_dispatch(**kwargs):
        first_calls.append(kwargs["group_id"])
        raise asyncio.CancelledError

    monkeypatch.setattr(Sub2APIMonitorPlugin, "_get_client", fake_get_client)
    monkeypatch.setenv("SUB2API_ALPHA_EMAIL", "alpha@example.invalid")
    monkeypatch.setenv("SUB2API_ALPHA_PASSWORD", "alpha-password")
    store = PluginDataStore(tmp_path, "sub2api_monitor")
    plugin = Sub2APIMonitorPlugin()
    plugin._ctx = SimpleNamespace(
        config={
            **_config(
                _source(
                    "alpha",
                    groups=["g1", "g2", "g3"],
                    inherit_groups=False,
                )
            ),
            "notify_group_ids": [],
            "visual_report_enabled": False,
        },
        data_store=store,
        dispatch_proactive_message=cancel_dispatch,
    )
    await plugin.poll_once()

    with pytest.raises(asyncio.CancelledError):
        await plugin.poll_once()

    assert first_calls == ["g1"]
    persisted = store.get("source_states")["alpha"]["notification_cursors"]
    assert list(persisted.values()) == [1]

    retry_calls: list[str] = []

    async def reject_dispatch(**kwargs):
        retry_calls.append(kwargs["group_id"])
        return False

    plugin._ctx.dispatch_proactive_message = reject_dispatch
    await plugin.poll_once()
    assert retry_calls == ["g2", "g3", "g1"]


@pytest.mark.asyncio
async def test_transient_revert_preserves_partial_ack_and_none_is_not_acknowledged(
    tmp_path, monkeypatch
):
    client = _SourceClient(
        subscriptions=[
            [{"id": "plan", "price": 1}],
            [{"id": "plan", "price": 2}],
            [{"id": "plan", "price": 1}],
            [{"id": "plan", "price": 2}],
        ],
        rates=[[], [], [], []],
    )

    async def fake_get_client(self, source):
        return client

    calls: list[str] = []

    async def dispatch(**kwargs):
        group_id = kwargs["group_id"]
        calls.append(group_id)
        if group_id == "good":
            return True
        return None

    monkeypatch.setattr(Sub2APIMonitorPlugin, "_get_client", fake_get_client)
    monkeypatch.setenv("SUB2API_ALPHA_EMAIL", "alpha@example.invalid")
    monkeypatch.setenv("SUB2API_ALPHA_PASSWORD", "alpha-password")
    store = PluginDataStore(tmp_path, "sub2api_monitor")
    plugin = Sub2APIMonitorPlugin()
    plugin._ctx = SimpleNamespace(
        config={
            **_config(
                _source(
                    "alpha",
                    groups=["good", "unconfirmed"],
                    inherit_groups=False,
                )
            ),
            "notify_group_ids": [],
            "visual_report_enabled": False,
        },
        data_store=store,
        dispatch_proactive_message=dispatch,
    )

    await plugin.poll_once()
    failed = await plugin.poll_once()
    reverted = await plugin.poll_once()
    repeated = await plugin.poll_once()

    assert failed.errors
    assert not reverted.errors
    assert repeated.errors
    assert calls == ["good", "unconfirmed", "unconfirmed"]
    assert store.get("source_states")["alpha"]["subscriptions_snapshot"] == [
        {"id": "plan", "price": 1}
    ]


def test_visual_html_escapes_untrusted_values_and_projects_safe_fields():
    marker = '<script>alert("x")</script><img src=x onerror=alert(1)>'
    html_text = build_change_card_html(
        source_id="alpha",
        display_name=f"甲站 {marker}",
        event_type="subscription_changed",
        before={
            "id": "plan",
            "name": marker,
            "price": 1,
            "password": "secret-password",
            "email": "secret@example.invalid",
            "api_key": "secret-api-key",
            "unknown_html": marker,
        },
        after={"id": "plan", "name": marker, "price": 2},
        occurred_at=1_700_000_000,
    )
    dashboard = build_dashboard_html(
        [
            {
                "id": "alpha",
                "display_name": marker,
                "ready": True,
                "subscriptions": [],
                "rates": [{"id": marker, "rate_multiplier": 1, "token": "secret"}],
                "error": "password=secret",
            }
        ],
        generated_at=1_700_000_000,
    )

    assert marker not in html_text
    assert "&lt;script&gt;" in html_text
    assert "unknown_html" not in html_text
    for secret in ("secret-password", "secret@example.invalid", "secret-api-key"):
        assert secret not in html_text
    assert "Content-Security-Policy" in html_text
    assert "default-src 'none'" in html_text
    assert "password=secret" not in dashboard
    assert "最近一次轮询存在错误" in dashboard
    assert "&lt;script&gt;" in dashboard


def test_validated_artifact_image_rejects_non_png_and_outside_paths(tmp_path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    good = artifacts / "sub2api_good.png"
    good.write_bytes(b"\x89PNG\r\n\x1a\nvalid")
    bad = artifacts / "sub2api_bad.png"
    bad.write_bytes(b"not-png")
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"\x89PNG\r\n\x1a\nvalid")

    assert validated_artifact_image(artifacts, good) == str(good.resolve())
    assert validated_artifact_image(artifacts, bad) == ""
    assert validated_artifact_image(artifacts, outside) == ""
    assert (
        validated_artifact_image(artifacts, "https://example.invalid/report.png") == ""
    )


@pytest.mark.asyncio
async def test_artifact_pruning_bounds_only_sub2api_images(tmp_path):
    unrelated = tmp_path / "keep.png"
    unrelated.write_bytes(b"unrelated")
    now = time.time()
    old = tmp_path / "sub2api_old.png"
    old.write_bytes(b"\x89PNG\r\n\x1a\nold")
    os.utime(old, (now - 90_000, now - 90_000))
    for index in range(35):
        path = tmp_path / f"sub2api_{index:02d}.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\nnew")
        os.utime(path, (now + index, now + index))

    await prune_artifacts(tmp_path)

    assert unrelated.exists()
    assert not old.exists()
    assert len(list(tmp_path.glob("sub2api_*.png"))) == 32


@pytest.mark.asyncio
async def test_visual_render_failure_keeps_authoritative_text_notification(
    tmp_path, monkeypatch
):
    client = _SourceClient(
        subscriptions=[
            [{"id": "a", "name": "A", "price": 1}],
            [{"id": "a", "name": "A", "price": 2}],
        ],
        rates=[[], []],
    )

    async def fake_get_client(self, source):
        return client

    async def failed_render(**_kwargs):
        return None

    monkeypatch.setattr(Sub2APIMonitorPlugin, "_get_client", fake_get_client)
    monkeypatch.setattr(plugin_module, "render_change_card", failed_render)
    monkeypatch.setenv("SUB2API_ALPHA_EMAIL", "alpha@example.invalid")
    monkeypatch.setenv("SUB2API_ALPHA_PASSWORD", "alpha-password")
    calls: list[dict[str, Any]] = []

    async def dispatch(**kwargs):
        calls.append(kwargs)
        return True

    plugin = Sub2APIMonitorPlugin()
    plugin._ctx = SimpleNamespace(
        config={
            **_config(_source("alpha", groups=["alpha-group"], inherit_groups=False)),
            "notify_group_ids": [],
            "visual_report_enabled": True,
        },
        data_store=PluginDataStore(tmp_path, "sub2api_monitor"),
        dispatch_proactive_message=dispatch,
    )

    await plugin.poll_once()
    result = await plugin.poll_once()

    assert not result.errors
    assert len(calls) == 1
    assert calls[0]["image_path"] == ""
    assert "订阅更新" in calls[0]["text"]


def test_sub2api_declares_visual_multisite_schema_without_credentials():
    from sirius_pulse.plugins.models import PluginDefinition

    definition = PluginDefinition.from_class(Sub2APIMonitorPlugin)
    schema = definition.ui_schema

    assert definition.version == "0.3.0"
    assert definition.min_framework_version == "1.3.0"
    assert schema["version"] == 1
    assert schema["layout"] == "wide"
    assert [section["id"] for section in schema["sections"]] == [
        "sources",
        "runtime",
        "visual",
        "legacy",
    ]
    sources = schema["parameters"]["sources"]
    assert sources["item_title_field"] == "display_name"
    assert sources["item_fallback_field"] == "id"
    assert sources["item_badge_field"] == "id"
    assert sources["item_status_field"] == "enabled"
    assert [fieldset["id"] for fieldset in sources["fieldsets"]] == [
        "identity",
        "session",
        "monitoring",
        "notifications",
    ]
    declared_parameter_names = {
        parameter.name.casefold() for parameter in definition.parameters
    }
    declared_source_fields = {
        str(field.get("name", "")).casefold()
        for parameter in definition.parameters
        if parameter.name == "sources"
        for field in parameter.fields or []
    }
    for forbidden in ("email", "password", "access_token", "refresh_token"):
        assert forbidden not in declared_parameter_names
        assert forbidden not in declared_source_fields
    assert "SUB2API_{ID}_EMAIL" in sources["help"]
    assert "SUB2API_{ID}_PASSWORD" in sources["help"]


def test_no_real_network_constants_are_baked_into_sources():
    source_text = (
        Path(__file__).resolve().parents[1] / "sub2api_monitor" / "plugin.py"
    ).read_text(encoding="utf-8")
    assert "example.invalid" not in source_text
    assert "/monitor/subscriptions" not in source_text
    assert "/monitor/rates" not in source_text
