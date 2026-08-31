from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sub2api_monitor import (  # noqa: E402
    Sub2APIClient,
    Sub2APIError,
    Sub2APIMonitorPlugin,
    normalize_group_rates,
    normalize_subscriptions,
)
from sub2api_monitor.data import (  # noqa: E402
    DataNormalizationError,
    diff_records,
    redact,
)

from sirius_pulse.plugins.context import PluginDataStore  # noqa: E402
from sirius_pulse.plugins.loader import PluginLoader  # noqa: E402


def test_plugin_loader_discovers_metadata_and_runtime_class():
    plugin_path = Path(__file__).resolve().parents[1] / "sub2api_monitor"
    loader = PluginLoader(plugin_path.parent)

    definition = loader.load_definition(plugin_path)
    plugin_class = loader.import_plugin_class(plugin_path)

    assert definition is not None
    assert definition.name == "sub2api_monitor"
    assert definition.dependencies == ["httpx>=0.24.0"]
    assert {parameter.name for parameter in definition.parameters} >= {
        "base_url",
        "subscriptions_path",
        "group_rates_path",
        "notify_group_ids",
    }
    parameters = {parameter.name: parameter for parameter in definition.parameters}
    assert parameters["poll_seconds"].minimum == 30
    assert parameters["poll_seconds"].maximum == 86400
    assert parameters["timeout"].minimum == 1
    assert parameters["timeout"].maximum == 300
    assert plugin_class is not None
    assert plugin_class.__name__ == Sub2APIMonitorPlugin.__name__
    assert plugin_class._plugin_name == "sub2api_monitor"


@pytest.mark.asyncio
async def test_client_derives_api_root_authenticates_once_and_sends_bearer_token():
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v1/auth/login":
            assert json.loads(request.content) == {
                "email": "bot@example.invalid",
                "password": "test-password",
            }
            return httpx.Response(
                200,
                json={"code": 0, "data": {"access_token": "test-access-token"}},
            )
        assert request.headers["Authorization"] == "Bearer test-access-token"
        assert request.url.params["timezone"] == "Asia/Shanghai"
        if request.url.path == "/api/v1/payment/checkout-info":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "plans": [
                            {
                                "id": 2,
                                "name": "周订阅",
                                "group_name": "pro",
                                "price": 19.9,
                            }
                        ]
                    },
                },
            )
        if request.url.path == "/api/v1/groups/rates":
            return httpx.Response(
                200,
                json={"code": 0, "data": {"pro": 0.25}},
            )
        raise AssertionError(f"Unexpected request: {request.url}")

    client = Sub2APIClient(
        base_url="https://station.example/keys",
        api_base_path="/api/v1",
        login_path="/auth/login",
        refresh_path="/auth/refresh",
        logout_path="/auth/logout",
        subscriptions_path="/payment/checkout-info",
        group_rates_path="/groups/rates",
        email="bot@example.invalid",
        password="test-password",
        timezone="Asia/Shanghai",
        transport=httpx.MockTransport(handler),
    )
    async with client:
        subscriptions, rates = await asyncio.gather(
            client.fetch_subscriptions(), client.fetch_group_rates()
        )

    assert subscriptions == [
        {
            "id": "2",
            "name": "周订阅",
            "group_name": "pro",
            "price": 19.9,
        }
    ]
    assert rates == [{"id": "pro", "rate_multiplier": 0.25}]
    assert [request.url.path for request in requests].count("/api/v1/auth/login") == 1


@pytest.mark.asyncio
async def test_client_reauthenticates_once_after_unauthorized_response():
    login_count = 0
    get_count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal login_count, get_count
        if request.url.path == "/api/v1/auth/login":
            login_count += 1
            return httpx.Response(
                200,
                json={"code": 0, "data": {"access_token": f"token-{login_count}"}},
            )
        get_count += 1
        if get_count == 1:
            assert request.headers["Authorization"] == "Bearer token-1"
            return httpx.Response(401, json={"message": "expired"})
        assert request.headers["Authorization"] == "Bearer token-2"
        return httpx.Response(200, json={"code": 0, "data": {"plans": []}})

    client = Sub2APIClient(
        base_url="https://station.example/keys",
        api_base_path="/api/v1",
        login_path="auth/login",
        refresh_path="auth/refresh",
        logout_path="auth/logout",
        subscriptions_path="payment/checkout-info",
        group_rates_path="groups/rates",
        email="bot@example.invalid",
        password="test-password",
        transport=httpx.MockTransport(handler),
    )
    async with client:
        assert await client.fetch_subscriptions() == []

    assert login_count == 2
    assert get_count == 2


@pytest.mark.asyncio
async def test_concurrent_unauthorized_responses_trigger_only_one_relogin():
    login_count = 0
    stale_get_count = 0
    both_stale_requests_started = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal login_count, stale_get_count
        if request.url.path == "/api/v1/auth/login":
            login_count += 1
            return httpx.Response(
                200,
                json={"code": 0, "data": {"access_token": f"token-{login_count}"}},
            )
        if request.headers["Authorization"] == "Bearer token-1":
            stale_get_count += 1
            if stale_get_count == 2:
                both_stale_requests_started.set()
            await asyncio.wait_for(both_stale_requests_started.wait(), timeout=0.2)
            return httpx.Response(401, json={"message": "expired"})
        assert request.headers["Authorization"] == "Bearer token-2"
        if request.url.path == "/api/v1/payment/checkout-info":
            return httpx.Response(200, json={"code": 0, "data": {"plans": []}})
        return httpx.Response(200, json={"code": 0, "data": {"rates": {}}})

    client = Sub2APIClient(
        base_url="https://station.example/keys",
        api_base_path="/api/v1",
        login_path="auth/login",
        refresh_path="auth/refresh",
        logout_path="auth/logout",
        subscriptions_path="payment/checkout-info",
        group_rates_path="groups/rates",
        email="bot@example.invalid",
        password="test-password",
        transport=httpx.MockTransport(handler),
    )
    async with client:
        subscriptions, rates = await asyncio.gather(
            client.fetch_subscriptions(),
            client.fetch_group_rates(),
        )

    assert subscriptions == []
    assert rates == []
    assert stale_get_count == 2
    assert login_count == 2


@pytest.mark.asyncio
async def test_client_refreshes_expired_token_and_logs_out_current_session():
    paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/api/v1/auth/login":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "access_token": "token-1",
                        "refresh_token": "refresh-1",
                        "expires_in": 60,
                    },
                },
            )
        if request.url.path == "/api/v1/auth/refresh":
            assert json.loads(request.content) == {"refresh_token": "refresh-1"}
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "access_token": "token-2",
                        "refresh_token": "refresh-2",
                        "expires_in": 60,
                    },
                },
            )
        if request.url.path == "/api/v1/payment/checkout-info":
            assert request.headers["Authorization"] == "Bearer token-2"
            return httpx.Response(200, json={"code": 0, "data": {"plans": []}})
        if request.url.path == "/api/v1/auth/logout":
            assert request.headers["Authorization"] == "Bearer token-2"
            assert json.loads(request.content) == {"refresh_token": "refresh-2"}
            return httpx.Response(200, json={"code": 0, "data": {}})
        raise AssertionError(f"Unexpected request: {request.url}")

    client = Sub2APIClient(
        base_url="https://station.example/keys",
        api_base_path="/api/v1",
        login_path="auth/login",
        refresh_path="auth/refresh",
        logout_path="auth/logout",
        subscriptions_path="payment/checkout-info",
        group_rates_path="groups/rates",
        email="bot@example.invalid",
        password="test-password",
        transport=httpx.MockTransport(handler),
    )
    await client.__aenter__()
    await client.login()
    client._expires_at = 0
    assert await client.fetch_subscriptions() == []
    await client.aclose(logout=True)

    assert paths == [
        "/api/v1/auth/login",
        "/api/v1/auth/refresh",
        "/api/v1/payment/checkout-info",
        "/api/v1/auth/logout",
    ]
    assert client._access_token is None
    assert client._refresh_token is None


def test_client_rejects_cross_origin_endpoint():
    client = Sub2APIClient(
        base_url="https://station.example/keys",
        api_base_path="/api/v1",
        login_path="auth/login",
        refresh_path="auth/refresh",
        logout_path="auth/logout",
        subscriptions_path="https://attacker.example/collect",
        group_rates_path="groups/rates",
        email="bot@example.invalid",
        password="test-password",
    )

    with pytest.raises(Sub2APIError, match="同源"):
        client.resolve_url(client.subscriptions_path)


@pytest.mark.asyncio
async def test_client_rejects_malformed_subscription_success_payload():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/auth/login":
            return httpx.Response(
                200,
                json={"code": 0, "data": {"access_token": "test-token"}},
            )
        return httpx.Response(200, json={"code": 0, "data": {"methods": {}}})

    client = Sub2APIClient(
        base_url="https://station.example/keys",
        api_base_path="/api/v1",
        login_path="auth/login",
        refresh_path="auth/refresh",
        logout_path="auth/logout",
        subscriptions_path="payment/checkout-info",
        group_rates_path="groups/rates",
        email="bot@example.invalid",
        password="test-password",
        transport=httpx.MockTransport(handler),
    )
    async with client:
        with pytest.raises(Sub2APIError, match="缺少有效的列表字段"):
            await client.fetch_subscriptions()


def test_normalizers_support_wrapped_and_mapping_payloads_and_redact_secrets():
    subscriptions = normalize_subscriptions(
        {
            "data": {
                "plans": [
                    {
                        "plan_id": 7,
                        "name": "月订阅",
                        "access_token": "must-not-persist",
                    }
                ]
            }
        }
    )
    rates = normalize_group_rates(
        {
            "data": {
                "rates": {
                    "group-a": {"group_name": "A", "multiplier": 0.5},
                    "group-b": 1,
                }
            }
        }
    )

    assert subscriptions == [
        {
            "plan_id": 7,
            "name": "月订阅",
            "access_token": "[已隐藏]",
            "id": "7",
        }
    ]
    assert rates == [
        {
            "group_name": "A",
            "multiplier": 0.5,
            "id": "group-a",
            "rate_multiplier": 0.5,
        },
        {"id": "group-b", "value": 1, "rate_multiplier": 1},
    ]


def _client_for_validation(
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    **overrides: Any,
) -> Sub2APIClient:
    values: dict[str, Any] = {
        "base_url": "https://station.example/keys",
        "api_base_path": "/api/v1",
        "login_path": "auth/login",
        "refresh_path": "auth/refresh",
        "logout_path": "auth/logout",
        "subscriptions_path": "plans",
        "group_rates_path": "rates",
        "email": "bot@example.invalid",
        "password": "test-password",
        "transport": transport,
    }
    values.update(overrides)
    return Sub2APIClient(**values)


@pytest.mark.asyncio
async def test_client_rejects_failure_envelopes_and_redacts_remote_error_secrets():
    responses = [
        httpx.Response(
            400,
            json={
                "message": {
                    "api_key": "remote-api-key",
                    "nested": {"access_token": "remote-access-token"},
                }
            },
        ),
        httpx.Response(
            200,
            json={"success": 0, "detail": {"refresh_token": "remote-refresh-token"}},
        ),
        httpx.Response(200, json={"code": [], "error": {"token": "remote-token"}}),
    ]

    async def handler(_request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    client = _client_for_validation(transport=httpx.MockTransport(handler))
    async with client:
        for forbidden_values in (
            ("remote-api-key", "remote-access-token"),
            ("remote-refresh-token",),
            ("remote-token",),
        ):
            with pytest.raises(Sub2APIError) as error:
                await client._request_json("GET", "plans", authenticated=False)
            assert all(secret not in str(error.value) for secret in forbidden_values)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://host.invalid/path?api_keys=secret&access_tokens=other", "[已隐藏]"),
        (
            "https://secret@host.invalid/path?token=secret",
            "https://host.invalid/path?token=%5B%E5%B7%B2%E9%9A%90%E8%97%8F%5D",
        ),
        (" https://[bad/path?token=secret", "[已隐藏的 URL]"),
        ("https:?token=secret", "[已隐藏的 URL]"),
        ("http:/path?api_key=secret", "[已隐藏的 URL]"),
    ],
)
def test_redaction_fails_closed_for_secret_urls(value, expected):
    redacted = redact(value)
    if expected == "[已隐藏]":
        assert "secret" not in redacted
        assert "other" not in redacted
    else:
        assert redacted == expected


def test_normalization_handles_camel_case_null_wrappers_and_stable_identities():
    rates = normalize_group_rates(
        {
            "data": {
                "groups": None,
                "rates": {
                    "pro": {
                        "groupId": "pro",
                        "modelRate": 0.5,
                        "peak_rate_enabled": True,
                    }
                },
            }
        }
    )
    assert rates == [
        {
            "groupId": "pro",
            "modelRate": 0.5,
            "peak_rate_enabled": True,
            "id": "pro",
            "rate_multiplier": 0.5,
        }
    ]
    assert (
        normalize_group_rates([{"id": "g", "rate": 1, "rate_multiplier": 2}])[0][
            "rate_multiplier"
        ]
        == 2
    )
    with pytest.raises(DataNormalizationError, match="稳定身份"):
        normalize_subscriptions([{"id": "   "}])
    with pytest.raises(DataNormalizationError, match="重复"):
        normalize_group_rates(
            [{"id": "g", "rate_multiplier": 1}, {"id": "g", "rate": 2}]
        )
    with pytest.raises(DataNormalizationError, match="无效"):
        normalize_group_rates([{"id": "g", "rate_multiplier": 10**1000}])

    _added, _removed, changed = diff_records(
        [{"id": " g ", "x": 1}], [{"id": "g", "x": 1}]
    )
    assert changed == []


def test_client_rejects_invalid_url_and_transport_configurations():
    with pytest.raises(Sub2APIError):
        _client_for_validation(
            base_url="https://station.example:0/keys"
        )._validate_transport_security()
    with pytest.raises(Sub2APIError, match="目录穿越"):
        _client_for_validation().resolve_url("%252e%252e/private")
    with pytest.raises(Sub2APIError, match="api_base_path"):
        _client_for_validation(api_base_path="//[").resolve_url("plans")
    with pytest.raises(Sub2APIError, match="timeout"):
        asyncio.run(_client_for_validation(timeout=0).__aenter__())

    ipv6 = _client_for_validation(base_url="https://[::1]:443/keys")
    assert (
        ipv6.resolve_url("https://[::1]/api/v1/plans") == "https://[::1]/api/v1/plans"
    )


def test_source_fingerprint_uses_effective_origin_and_endpoint(tmp_path):
    plugin = Sub2APIMonitorPlugin()
    config = {
        "base_url": "https://station.example/keys",
        "subscriptions_path": "plans",
        "group_rates_path": "rates",
    }
    plugin._ctx = SimpleNamespace(
        config=config, data_store=PluginDataStore(tmp_path, "sub2api_monitor")
    )

    initial = plugin._source_fingerprint("subscriptions")
    config["base_url"] = "https://station.example/dashboard"
    assert plugin._source_fingerprint("subscriptions") == initial
    config["subscriptions_path"] = "other-plans"
    assert plugin._source_fingerprint("subscriptions") != initial


def test_rate_event_key_ignores_display_name_changes():
    first = Sub2APIMonitorPlugin._notification_event_key(
        "group_rates",
        "source",
        "rate_changed",
        {"id": "g", "groupName": "旧名", "rate_multiplier": 1},
        {"id": "g", "groupName": "旧名", "rate_multiplier": 2},
    )
    renamed = Sub2APIMonitorPlugin._notification_event_key(
        "group_rates",
        "source",
        "rate_changed",
        {"id": "g", "groupName": "新名", "rate_multiplier": 1},
        {"id": "g", "groupName": "新名", "rate_multiplier": 2},
    )
    assert first == renamed


class _SequenceClient:
    def __init__(
        self,
        subscriptions: list[list[dict[str, Any]]],
        rates: list[list[dict[str, Any]]],
    ) -> None:
        self.subscriptions = subscriptions
        self.rates = rates
        self.subscription_index = 0
        self.rate_index = 0

    async def fetch_subscriptions(self) -> list[dict[str, Any]]:
        value = self.subscriptions[self.subscription_index]
        self.subscription_index += 1
        return value

    async def fetch_group_rates(self) -> list[dict[str, Any]]:
        value = self.rates[self.rate_index]
        self.rate_index += 1
        return value


@pytest.mark.asyncio
async def test_poll_initializes_then_notifies_subscription_and_rate_changes(tmp_path):
    sent: list[dict[str, Any]] = []
    store = PluginDataStore(tmp_path, "sub2api_monitor")
    plugin = Sub2APIMonitorPlugin()
    plugin._client = _SequenceClient(  # type: ignore[assignment]
        subscriptions=[
            [
                {"id": "a", "name": "A", "group_name": "pro", "price": 10},
                {"id": "b", "name": "B", "group_name": "pro", "price": 20},
            ],
            [
                {"id": "b", "name": "B", "group_name": "pro", "price": 25},
                {"id": "c", "name": "C", "group_name": "pro", "price": 30},
            ],
        ],
        rates=[
            [
                {"id": "g1", "group_name": "一组", "rate_multiplier": 0.25},
                {"id": "g2", "group_name": "二组", "rate_multiplier": 1},
            ],
            [
                {"id": "g1", "group_name": "一组", "rate_multiplier": 0.5},
                {"id": "g3", "group_name": "三组", "rate_multiplier": 2},
            ],
        ],
    )

    async def dispatch(**kwargs: Any) -> None:
        sent.append(kwargs)

    plugin._ctx = SimpleNamespace(
        config={
            "base_url": "https://station.example/keys",
            "subscriptions_path": "payment/checkout-info",
            "group_rates_path": "groups/rates",
            "notify_group_ids": ["10001", "10002"],
        },
        data_store=store,
        dispatch_proactive_message=dispatch,
    )

    first = await plugin.poll_once()
    second = await plugin.poll_once()

    assert sorted(first.initialized) == ["group_rates", "subscriptions"]
    assert first.change_count == 0
    assert first.notifications_sent == 0
    assert second.subscription_added == 1
    assert second.subscription_removed == 1
    assert second.subscription_changed == 1
    assert second.rates_added == 1
    assert second.rates_removed == 1
    assert second.rates_changed == 1
    assert second.notifications_sent == 12
    assert len(sent) == 12
    assert {item["group_id"] for item in sent} == {"10001", "10002"}
    assert any("订阅上架" in item["text"] for item in sent)
    assert any("订阅下架" in item["text"] for item in sent)
    assert any("0.25 → 0.5" in item["text"] for item in sent)
    assert len({item["event_id"] for item in sent}) == 12

    persisted = json.dumps(store.all(), ensure_ascii=False)
    assert "test-password" not in persisted
    assert "bot@example.invalid" not in persisted


@pytest.mark.asyncio
async def test_source_change_silently_reinitializes_snapshots(tmp_path):
    store = PluginDataStore(tmp_path, "sub2api_monitor")
    plugin = Sub2APIMonitorPlugin()
    plugin._client = _SequenceClient(  # type: ignore[assignment]
        subscriptions=[
            [{"id": "old", "name": "旧站订阅"}],
            [{"id": "new", "name": "新站订阅"}],
        ],
        rates=[
            [{"id": "old-rate", "rate_multiplier": 1}],
            [{"id": "new-rate", "rate_multiplier": 2}],
        ],
    )

    async def dispatch(**_kwargs: Any) -> None:
        raise AssertionError("Source reinitialization must not notify")

    config = {
        "base_url": "https://old.example/keys",
        "subscriptions_path": "payment/checkout-info",
        "group_rates_path": "groups/rates",
        "notify_group_ids": ["10001"],
    }
    plugin._ctx = SimpleNamespace(
        config=config,
        data_store=store,
        dispatch_proactive_message=dispatch,
    )

    await plugin.poll_once()
    config["base_url"] = "https://new.example/keys"
    second = await plugin.poll_once()

    assert sorted(second.initialized) == ["group_rates", "subscriptions"]
    assert second.change_count == 0
    assert store.get("subscriptions_snapshot") == [{"id": "new", "name": "新站订阅"}]


@pytest.mark.asyncio
async def test_poll_retries_only_unconfirmed_groups_and_commits_atomically(tmp_path):
    store = PluginDataStore(tmp_path, "sub2api_monitor")
    plugin = Sub2APIMonitorPlugin()
    plugin._client = _SequenceClient(
        subscriptions=[
            [{"id": "plan", "name": "基础", "price": 10}],
            [{"id": "plan", "name": "基础", "price": 20}],
            [{"id": "plan", "name": "基础", "price": 20}],
        ],
        rates=[
            [{"id": "group", "group_name": "一组", "rate_multiplier": 1}],
            [{"id": "group", "group_name": "一组", "rate_multiplier": 1}],
            [{"id": "group", "group_name": "一组", "rate_multiplier": 1}],
        ],
    )
    calls: list[str] = []
    bad_attempts = 0
    config = {
        "base_url": "https://station.example/keys",
        "subscriptions_path": "plans",
        "group_rates_path": "rates",
        "notify_group_ids": ["good", "bad"],
        "run_on_persona": "alice",
    }

    async def dispatch(**kwargs: Any) -> bool:
        nonlocal bad_attempts
        group_id = kwargs["group_id"]
        calls.append(group_id)
        if group_id == "bad":
            bad_attempts += 1
            return bad_attempts > 1
        return True

    plugin._ctx = SimpleNamespace(
        config=config,
        data_store=store,
        dispatch_proactive_message=dispatch,
    )

    await plugin.poll_once()
    initial_success = store.get("last_poll_success_at")
    failed = await plugin.poll_once()

    assert failed.errors
    assert store.get("subscriptions_snapshot") == [
        {"id": "plan", "name": "基础", "price": 10}
    ]
    assert calls == ["good", "bad"]
    assert store.get("last_poll_success_at") == initial_success
    assert store.get("last_poll_at") == initial_success

    recovered = await plugin.poll_once()
    assert not recovered.errors
    assert calls == ["good", "bad", "bad"]
    assert store.get("subscriptions_snapshot") == [
        {"id": "plan", "name": "基础", "price": 20}
    ]
    assert store.get("last_poll_success_at") is not None


@pytest.mark.asyncio
async def test_corrupt_store_is_reported_until_reset(tmp_path):
    store_file = tmp_path / "_plugin_sub2api_monitor_data.json"
    store_file.write_text("{broken", encoding="utf-8")
    store = PluginDataStore(tmp_path, "sub2api_monitor")
    plugin = Sub2APIMonitorPlugin()
    plugin._client = _SequenceClient(
        subscriptions=[[{"id": "plan"}]],
        rates=[[{"id": "group", "rate_multiplier": 1}]],
    )
    plugin._ctx = SimpleNamespace(
        config={
            "base_url": "https://station.example/keys",
            "subscriptions_path": "plans",
            "group_rates_path": "rates",
            "notify_group_ids": ["10001"],
        },
        data_store=store,
    )

    result = await plugin.poll_once(notify=False)
    assert result.errors and "损坏" in result.errors[0]
    assert store.get("subscriptions_snapshot") is None
    assert store.load_error

    store.clear()
    assert store.load_error is None
    assert store.all() == {}


def test_background_task_requires_credentials_target_and_designated_persona(
    tmp_path, monkeypatch
):
    store = PluginDataStore(tmp_path, "sub2api_monitor")
    plugin = Sub2APIMonitorPlugin()
    persona_name = "alice"
    engine = SimpleNamespace(get_persona_name=lambda: persona_name)
    config = {
        "base_url": "https://station.example/keys",
        "subscriptions_path": "payment/checkout-info",
        "group_rates_path": "groups/rates",
        "notify_group_ids": [],
        "run_on_persona": "alice",
    }
    plugin._ctx = SimpleNamespace(config=config, data_store=store, engine=engine)

    assert plugin.create_background_tasks() == []

    monkeypatch.setenv("SUB2API_EMAIL", "bot@example.invalid")
    monkeypatch.setenv("SUB2API_PASSWORD", "test-password")
    config["notify_group_ids"] = ["10001"]
    assert len(plugin.create_background_tasks()) == 1

    persona_name = "bob"
    assert plugin.create_background_tasks() == []

    persona_name = "alice"
    config["email"] = "legacy@example.invalid"
    assert plugin.create_background_tasks() == []
    config.pop("email")
    config["password"] = "legacy-secret"
    assert plugin.create_background_tasks() == []
