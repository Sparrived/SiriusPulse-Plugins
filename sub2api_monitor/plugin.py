"""Sirius Pulse plugin that monitors Sub2API subscriptions and group rates."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import time
from typing import Any
from urllib.parse import urlsplit

from sirius_pulse.plugins.api import (
    BackgroundTaskSpec,
    PluginBase,
    PluginResponse,
    command,
)
from sirius_pulse.plugins.models import CommandAST

from .client import Sub2APIClient, Sub2APIError
from .data import (
    PollResult,
    RecordChange,
    canonical_record,
    comparison_record,
    diff_records,
)


class Sub2APIMonitorPlugin(PluginBase):
    """Poll a configured Sub2API deployment and proactively report changes."""

    _plugin_name = "sub2api_monitor"
    _plugin_display_name = "Sub2API 订阅监控"
    _plugin_description = "监控 Sub2API 中转站订阅上架/下架和分组倍率变化。"
    _plugin_version = "0.1.0"
    _plugin_author = "Sirius Pulse"
    _plugin_dependencies = ["httpx>=0.24.0"]
    _plugin_permissions = {
        "developer_only": True,
        "hidden_from_intent": True,
        "rate_limit": {"calls_per_minute": 10, "calls_per_hour": 60},
    }
    _plugin_parameters = [
        {
            "name": "base_url",
            "type": "str",
            "description": "中转站页面地址，例如 https://example.invalid/keys。",
            "default": "",
            "group": "Sub2API 连接",
        },
        {
            "name": "api_base_path",
            "type": "str",
            "description": "API 根路径；接口路径均相对于它解析。",
            "default": "/api/v1",
            "group": "Sub2API 连接",
        },
        {
            "name": "login_path",
            "type": "str",
            "description": "登录接口路径，可填相对 API 路径或同源完整 URL。",
            "default": "/auth/login",
            "group": "Sub2API 连接",
        },
        {
            "name": "refresh_path",
            "type": "str",
            "description": "令牌刷新接口路径；留空则在 token 失效后重新登录。",
            "default": "/auth/refresh",
            "group": "Sub2API 连接",
        },
        {
            "name": "logout_path",
            "type": "str",
            "description": "当前会话注销接口路径；插件卸载时尽力调用。",
            "default": "/auth/logout",
            "group": "Sub2API 连接",
        },
        {
            "name": "subscriptions_path",
            "type": "str",
            "description": "订阅列表接口路径（必填，不在插件中硬编码）。",
            "default": "",
            "required": True,
            "group": "Sub2API 连接",
        },
        {
            "name": "group_rates_path",
            "type": "str",
            "description": "分组倍率接口路径（必填，不在插件中硬编码）。",
            "default": "",
            "required": True,
            "group": "Sub2API 连接",
        },
        {
            "name": "timezone",
            "type": "str",
            "description": "GET 接口的 timezone 查询参数；留空则不发送。",
            "default": "Asia/Shanghai",
            "group": "Sub2API 连接",
        },
        {
            "name": "poll_seconds",
            "type": "int",
            "description": "轮询间隔（秒），最小 30 秒。",
            "default": 300,
            "minimum": 30,
            "maximum": 86400,
            "group": "Sub2API 监控",
        },
        {
            "name": "timeout",
            "type": "float",
            "description": "单次 HTTP 请求超时（秒）。",
            "default": 20,
            "minimum": 1,
            "maximum": 300,
            "group": "Sub2API 连接",
        },
        {
            "name": "allow_insecure_http",
            "type": "bool",
            "description": "仅允许 localhost/127.0.0.1 调试时使用 HTTP。",
            "default": False,
            "group": "Sub2API 连接",
        },
        {
            "name": "notify_group_ids",
            "type": "list",
            "description": "主动通知目标群号列表。",
            "default": [],
            "group": "Sub2API 监控",
        },
        {
            "name": "adapter_type",
            "type": "str",
            "description": "主动通知使用的平台类型；留空则由引擎自动选择。",
            "default": "napcat",
            "group": "Sub2API 监控",
        },
        {
            "name": "run_on_persona",
            "type": "str",
            "description": "必填：唯一负责轮询的 Persona 名称；留空将禁用后台与手动轮询。",
            "default": "",
            "group": "Sub2API 监控",
        },
    ]

    def __init__(self) -> None:
        super().__init__()
        self._poll_lock = asyncio.Lock()
        self._client: Sub2APIClient | None = None
        self._client_fingerprint = ""

    @command(
        "sub2api",
        prefix="/",
        patterns=["sub2api", "sub2api-monitor"],
        render_mode="direct",
        description="查看或立即执行 Sub2API 订阅、分组倍率监控。",
        hidden_from_intent=True,
        examples=[
            "/sub2api status",
            "/sub2api poll",
            "/sub2api subscriptions",
            "/sub2api rates",
        ],
    )
    def sub2api_command(self) -> PluginResponse:
        """Register command metadata; ``execute_async`` handles sub-actions."""
        return PluginResponse.ok(text="请使用 /sub2api status 查看监控状态。")

    def create_background_tasks(self) -> list[BackgroundTaskSpec]:
        """Create the polling loop only when this persona can run it."""
        if not self._is_designated_persona() or not self._configuration_ready():
            return []
        return [BackgroundTaskSpec("poll", self._poll_seconds(), self._poll_background)]

    async def on_unload(self) -> None:
        """Close the reusable HTTP client without persisting its access token."""
        async with self._poll_lock:
            client = self._client
            self._client = None
            self._client_fingerprint = ""
            if client is not None:
                try:
                    await self._close_client(client)
                except Exception as exc:  # noqa: BLE001 - unload must continue
                    self.logger.warning("关闭 Sub2API 客户端失败：%s", self._safe_error(exc))

    async def _poll_background(self) -> None:
        try:
            result = await self.poll_once(notify=True)
            if result.errors:
                self.logger.warning("Sub2API 监控部分失败：%s", "；".join(result.errors))
        except Exception as exc:  # noqa: BLE001 - background task boundary
            self.logger.warning("Sub2API 监控轮询失败：%s", self._safe_error(exc))

    async def execute_async(self, cmd: CommandAST) -> list[PluginResponse]:
        """Handle explicit monitor commands."""
        try:
            action = self._arg(cmd, 0, "status").strip().casefold()
            if action in {"status", "状态", ""}:
                return [PluginResponse.ok(text=self._status_text())]
            if action in {"poll", "check", "检查", "轮询"}:
                if not self._is_designated_persona():
                    return [PluginResponse.fail("当前人格未被配置为 Sub2API 轮询执行者")]
                result = await self.poll_once(notify=True)
                return [PluginResponse.ok(text=self._poll_text(result))]
            if action in {"subscriptions", "plans", "订阅"}:
                data = await self._fetch_one("subscriptions")
                return [
                    PluginResponse.ok(text=_format_records("当前订阅", data), data=data)
                ]
            if action in {"rates", "groups", "倍率", "分组"}:
                data = await self._fetch_one("group_rates")
                return [
                    PluginResponse.ok(text=_format_records("当前分组倍率", data), data=data)
                ]
            if action in {"reset", "重置"}:
                async with self._poll_lock:
                    store = self.get_data_store()
                    clear = getattr(store, "clear", None)
                    if callable(clear):
                        clear()
                    else:
                        store.delete_many(
                            [
                                "subscriptions_snapshot",
                                "subscriptions_source",
                                "group_rates_snapshot",
                                "group_rates_source",
                                "notification_acks",
                                "last_poll_attempt_at",
                                "last_poll_at",
                                "last_poll_success_at",
                                "last_poll_error",
                            ]
                        )
                return [
                    PluginResponse.ok(text=("Sub2API 监控快照已重置；下一次轮询只建立新快照，" "不发送历史变化。"))
                ]
            return [
                PluginResponse.fail("用法：/sub2api status|poll|subscriptions|rates|reset")
            ]
        except Exception as exc:  # noqa: BLE001 - plugin command boundary
            safe_error = self._safe_error(exc)
            self.logger.warning("Sub2API 监控命令失败：%s", safe_error)
            return [PluginResponse.fail(f"Sub2API 监控失败：{safe_error}")]

    async def poll_once(self, *, notify: bool = True) -> PollResult:
        """Poll both endpoints and compare successful responses with snapshots."""
        async with self._poll_lock:
            self._validate_config()
            result = PollResult()
            store = self.get_data_store()
            load_error = getattr(store, "load_error", None)
            if load_error:
                result.errors.append(str(load_error))
                return result

            attempt_at = int(time.time())
            store.update({"last_poll_attempt_at": attempt_at, "last_poll_error": ""})
            client = await self._get_client()
            poll_values: tuple[Any, Any] = await asyncio.gather(
                client.fetch_subscriptions(),
                client.fetch_group_rates(),
                return_exceptions=True,
            )

            await self._process_poll_value(
                "subscriptions",
                poll_values[0],
                result,
                notify=notify,
            )
            await self._process_poll_value(
                "group_rates",
                poll_values[1],
                result,
                notify=notify,
            )
            if result.errors:
                store.update({"last_poll_error": ";".join(result.errors)[:1000]})
                return result

            success_at = int(time.time())
            store.update(
                {
                    "last_poll_at": success_at,
                    "last_poll_success_at": success_at,
                    "last_poll_error": "",
                }
            )
            return result

    async def _process_poll_value(
        self,
        name: str,
        value: list[dict[str, Any]] | BaseException,
        result: PollResult,
        *,
        notify: bool,
    ) -> None:
        if isinstance(value, asyncio.CancelledError):
            raise value
        if isinstance(value, BaseException):
            label = "订阅接口" if name == "subscriptions" else "分组倍率接口"
            result.errors.append(f"{label}：{self._safe_error(value)}")
            return
        try:
            await self._process_collection(name, value, result, notify=notify)
        except Exception as exc:  # noqa: BLE001 - keep the other endpoint useful
            label = "订阅通知" if name == "subscriptions" else "分组倍率通知"
            result.errors.append(f"{label}：{self._safe_error(exc)}")

    async def _process_collection(
        self,
        name: str,
        current: list[dict[str, Any]],
        result: PollResult,
        *,
        notify: bool,
    ) -> None:
        store = self.get_data_store()
        load_error = getattr(store, "load_error", None)
        if load_error:
            raise Sub2APIError(str(load_error))

        source = self._source_fingerprint(name)
        stored_source = store.get(f"{name}_source")
        old_raw = store.get(f"{name}_snapshot")
        old = old_raw if isinstance(old_raw, list) and stored_source == source else None
        ack_state = self._load_notification_acks(store)
        if old is None:
            self._discard_collection_acks(ack_state, name)
            store.update(
                {
                    f"{name}_snapshot": current,
                    f"{name}_source": source,
                    "notification_acks": ack_state,
                }
            )
            result.initialized.append(name)
            return

        ignored_keys = (
            {"group_name", "name", "platform", "slug"}
            if name == "group_rates"
            else None
        )
        added, removed, changed = diff_records(
            old,
            current,
            ignored_keys=ignored_keys,
        )
        events = self._collection_events(name, added, removed, changed)
        self._add_change_counts(name, added, removed, changed, result)
        groups = self._notify_groups()
        if notify and events and not groups:
            result.errors.append("未配置通知群，变化快照暂不提交")
            return

        sent = 0
        notification_errors: list[str] = []
        if notify:
            for event_type, before, after in events:
                event_key = self._notification_event_key(
                    name, source, event_type, before, after
                )
                event_sent, failures = await self._notify_change(
                    name,
                    event_type,
                    before,
                    after,
                    event_key,
                    ack_state,
                )
                sent += event_sent
                if failures:
                    notification_errors.append(
                        f"{event_type} 未确认群组：{', '.join(failures)}"
                    )
        if notification_errors:
            result.errors.extend(notification_errors)
            result.notifications_sent += sent
            return

        self._discard_collection_acks(ack_state, name)
        store.update(
            {
                f"{name}_snapshot": current,
                f"{name}_source": source,
                "notification_acks": ack_state,
            }
        )
        result.notifications_sent += sent

    @staticmethod
    def _add_change_counts(
        name: str,
        added: list[dict[str, Any]],
        removed: list[dict[str, Any]],
        changed: list[RecordChange],
        result: PollResult,
    ) -> None:
        if name == "subscriptions":
            result.subscription_added += len(added)
            result.subscription_removed += len(removed)
            result.subscription_changed += len(changed)
        else:
            result.rates_added += len(added)
            result.rates_removed += len(removed)
            result.rates_changed += len(changed)

    @staticmethod
    def _collection_events(
        name: str,
        added: list[dict[str, Any]],
        removed: list[dict[str, Any]],
        changed: list[RecordChange],
    ) -> list[tuple[str, dict[str, Any] | None, dict[str, Any] | None]]:
        if name == "subscriptions":
            return (
                [("subscription_added", None, item) for item in added]
                + [("subscription_removed", item, None) for item in removed]
                + [
                    ("subscription_changed", change.before, change.after)
                    for change in changed
                ]
            )
        return (
            [("rate_added", None, item) for item in added]
            + [("rate_removed", item, None) for item in removed]
            + [("rate_changed", change.before, change.after) for change in changed]
        )

    @staticmethod
    def _notification_event_key(
        name: str,
        source: str,
        event_type: str,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
    ) -> str:
        ignored_keys = (
            {"group_name", "name", "platform", "slug"}
            if name == "group_rates"
            else None
        )
        fingerprint = canonical_record(
            {
                "name": name,
                "source": source,
                "event_type": event_type,
                "before": comparison_record(before or {}, ignored_keys=ignored_keys),
                "after": comparison_record(after or {}, ignored_keys=ignored_keys),
            }
        )
        digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:24]
        return f"{name}:{source}:{event_type}:{digest}"

    @staticmethod
    def _load_notification_acks(store: Any) -> dict[str, list[str]]:
        raw = store.get("notification_acks", {})
        if not isinstance(raw, dict):
            return {}
        result: dict[str, list[str]] = {}
        for event_key, group_ids in raw.items():
            if not isinstance(event_key, str) or not isinstance(group_ids, list):
                continue
            cleaned = list(
                dict.fromkeys(
                    str(group_id).strip()
                    for group_id in group_ids
                    if str(group_id).strip()
                )
            )
            if cleaned:
                result[event_key] = cleaned
        return result

    @staticmethod
    def _discard_collection_acks(ack_state: dict[str, list[str]], name: str) -> None:
        prefix = f"{name}:"
        for event_key in list(ack_state):
            if event_key.startswith(prefix):
                del ack_state[event_key]

    async def _notify_change(
        self,
        name: str,
        event_type: str,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
        event_key: str,
        ack_state: dict[str, list[str]],
    ) -> tuple[int, list[str]]:
        groups = self._notify_groups()
        if not groups:
            return 0, []
        text = _format_change(event_type, before, after)
        acknowledged = set(ack_state.get(event_key, []))
        sent = 0
        failures: list[str] = []
        adapter_type = self._config_value(
            "adapter_type",
            default="napcat",
            allow_empty=True,
        )
        for group_id in groups:
            if group_id in acknowledged:
                continue
            try:
                accepted = await self.ctx.dispatch_proactive_message(
                    group_id=group_id,
                    text=text,
                    adapter_type=adapter_type,
                    event_id=f"sub2api:{event_key}:{group_id}",
                )
                if accepted is False:
                    failures.append(f"{group_id}（投递未确认）")
                    continue
                acknowledged.add(group_id)
                ack_state[event_key] = sorted(acknowledged)
                self.get_data_store().update({"notification_acks": ack_state})
                sent += 1
            except Exception as exc:  # noqa: BLE001 - one group must not block others
                acknowledged.discard(group_id)
                if acknowledged:
                    ack_state[event_key] = sorted(acknowledged)
                else:
                    ack_state.pop(event_key, None)
                failures.append(f"{group_id}（{self._safe_error(exc)}）")
        return sent, failures

    async def _fetch_one(self, name: str) -> list[dict[str, Any]]:
        async with self._poll_lock:
            self._validate_config()
            client = await self._get_client()
            if name == "subscriptions":
                return await client.fetch_subscriptions()
            return await client.fetch_group_rates()

    @staticmethod
    async def _close_client(client: Any) -> None:
        """Close a reusable client, including lightweight test doubles."""
        close = getattr(client, "aclose", None)
        if callable(close):
            result = close(logout=True)
            if asyncio.iscoroutine(result):
                await result
            return
        close = getattr(client, "close", None)
        if callable(close):
            result = close()
            if asyncio.iscoroutine(result):
                await result

    async def _get_client(self) -> Sub2APIClient:
        fingerprint = self._client_config_fingerprint()
        # Only clients created by this plugin can safely be recreated on a
        # settings change.  A host may inject a compatible client (and tests
        # use lightweight doubles); never replace such an object with a real
        # network client merely because the source URL changed.
        if (
            isinstance(self._client, Sub2APIClient)
            and self._client_fingerprint != fingerprint
        ):
            old_client = self._client
            self._client = None
            self._client_fingerprint = ""
            await self._close_client(old_client)
        if self._client is None:
            client = Sub2APIClient(**self._client_kwargs())
            await client.__aenter__()
            self._client = client
            self._client_fingerprint = fingerprint
        assert self._client is not None
        return self._client

    def _client_kwargs(self) -> dict[str, Any]:
        return {
            "base_url": self._config_value("base_url"),
            "api_base_path": self._config_value("api_base_path", default="/api/v1"),
            "login_path": self._config_value("login_path", default="/auth/login"),
            "refresh_path": self._config_value(
                "refresh_path", default="/auth/refresh", allow_empty=True
            ),
            "logout_path": self._config_value(
                "logout_path", default="/auth/logout", allow_empty=True
            ),
            "subscriptions_path": self._config_value("subscriptions_path"),
            "group_rates_path": self._config_value("group_rates_path"),
            "email": os.getenv("SUB2API_EMAIL", "").strip(),
            "password": os.getenv("SUB2API_PASSWORD", "").strip(),
            "timezone": self._config_value(
                "timezone", default="Asia/Shanghai", allow_empty=True
            ),
            "timeout": self._config_float("timeout", 20.0),
            "allow_insecure_http": self._config_bool("allow_insecure_http", False),
        }

    def _client_config_fingerprint(self) -> str:
        values = self._client_kwargs()
        password = str(values.pop("password", ""))
        try:
            client = Sub2APIClient(**values, password=password)
            resolved_login = client.resolve_url(client.login_path)
            parsed_login = urlsplit(resolved_login)
            values["base_url"] = f"{parsed_login.scheme}://{parsed_login.netloc}"
            for key in (
                "login_path",
                "refresh_path",
                "logout_path",
                "subscriptions_path",
                "group_rates_path",
            ):
                endpoint = str(values.get(key, ""))
                values[key] = client.resolve_url(endpoint) if endpoint else ""
            values["api_base_path"] = ""
        except Sub2APIError:
            # Validation reports the user-facing configuration error; retaining
            # raw values here merely ensures a subsequent valid edit recreates
            # the client rather than reusing stale transport settings.
            pass
        values["password_digest"] = hashlib.sha256(password.encode("utf-8")).hexdigest()
        serialized = json.dumps(values, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]

    def _source_fingerprint(self, name: str) -> str:
        endpoint_key = (
            "subscriptions_path" if name == "subscriptions" else "group_rates_path"
        )
        client = Sub2APIClient(**self._client_kwargs())
        endpoint = client.resolve_url(getattr(client, endpoint_key))
        account = os.getenv("SUB2API_EMAIL", "").strip()
        source = {
            "endpoint": endpoint,
            "account_digest": hashlib.sha256(account.encode("utf-8")).hexdigest(),
            "timezone": client.timezone,
        }
        serialized = json.dumps(source, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]

    def _configuration_ready(self) -> bool:
        try:
            self._validate_config()
        except Sub2APIError:
            return False
        credentials_ready = bool(os.getenv("SUB2API_EMAIL", "").strip()) and bool(
            os.getenv("SUB2API_PASSWORD", "").strip()
        )
        return credentials_ready and bool(self._notify_groups())

    def _validate_config(self) -> None:
        missing = [
            label
            for label, value in (
                ("base_url", self._config_value("base_url")),
                ("subscriptions_path", self._config_value("subscriptions_path")),
                ("group_rates_path", self._config_value("group_rates_path")),
            )
            if not value
        ]
        if missing:
            raise Sub2APIError(f"缺少配置：{', '.join(missing)}")

        legacy_credentials = [
            key for key in ("email", "password") if self._legacy_credential(key)
        ]
        if legacy_credentials:
            names = "、".join(legacy_credentials)
            raise Sub2APIError(
                f"{names} 不支持写入插件设置；请改用 SUB2API_EMAIL 和 SUB2API_PASSWORD 环境变量"
            )

        poll_seconds = self._poll_seconds()
        if not math.isfinite(poll_seconds) or not 30.0 <= poll_seconds <= 86400.0:
            raise Sub2APIError("poll_seconds 必须是 30 到 86400 秒之间的有限数字")
        timeout = self._config_float("timeout", 20.0)
        if not math.isfinite(timeout) or not 1.0 <= timeout <= 300.0:
            raise Sub2APIError("timeout 必须是 1 到 300 秒之间的有限数字")

        client = Sub2APIClient(**self._client_kwargs())
        client._validate_transport_security()
        for endpoint in (
            client.login_path,
            client.refresh_path,
            client.logout_path,
            client.subscriptions_path,
            client.group_rates_path,
        ):
            if endpoint:
                client.resolve_url(endpoint)

    def _is_designated_persona(self) -> bool:
        configured = self._config_value("run_on_persona")
        if not configured:
            return False
        return self.ctx.engine.get_persona_name().casefold() == configured.casefold()

    def _background_status(self) -> str:
        configured = self._config_value("run_on_persona")
        if not configured:
            return "未指定执行人格"
        if not self._is_designated_persona():
            return "由指定人格负责"
        if not self._configuration_ready():
            return "等待有效配置、凭据或通知群"
        return "启用"

    def _status_text(self) -> str:
        store = self.get_data_store()
        subscriptions = store.get("subscriptions_snapshot")
        rates = store.get("group_rates_snapshot")
        last_poll = store.get("last_poll_at")
        last_attempt = store.get("last_poll_attempt_at")
        last_success = store.get("last_poll_success_at")
        last_error = store.get("last_poll_error")
        required = (
            self._config_value("base_url"),
            self._config_value("subscriptions_path"),
            self._config_value("group_rates_path"),
        )
        return (
            "Sub2API 监控状态："
            f"{'已配置' if all(required) else '未完整配置'}；"
            f"订阅快照 {len(subscriptions) if isinstance(subscriptions, list) else 0} 条；"
            f"分组倍率快照 {len(rates) if isinstance(rates, list) else 0} 条；"
            f"通知群 {len(self._notify_groups())} 个；"
            f"后台轮询 {self._background_status()}；"
            f"上次尝试 {last_attempt or '尚未执行'}；"
            f"上次成功 {last_success or last_poll or '尚未成功'}。"
            f"{f'当前错误 {last_error}。' if last_error else ''}"
        )

    def _safe_error(self, exc: BaseException) -> str:
        text = str(exc) or type(exc).__name__
        secrets = (
            self._legacy_credential("password"),
            self._legacy_credential("email"),
            os.getenv("SUB2API_PASSWORD", ""),
            os.getenv("SUB2API_EMAIL", ""),
        )
        for secret in secrets:
            if secret:
                text = text.replace(secret, "[已隐藏]")
        return text[:500]

    @staticmethod
    def _arg(cmd: CommandAST, index: int, default: str = "") -> str:
        if 0 <= index < len(cmd.args):
            return str(cmd.args[index].value)
        return default

    def _config_value(
        self,
        key: str,
        *,
        default: str = "",
        allow_empty: bool = False,
    ) -> str:
        try:
            value = self.ctx.config.get(key, default)
        except RuntimeError:
            return default
        if value is None:
            return default
        text = str(value).strip()
        return text if text or allow_empty else default

    def _legacy_credential(self, key: str) -> str:
        """Return a stored credential solely to reject and redact legacy settings."""
        try:
            value = self.ctx.config.get(key, "")
        except RuntimeError:
            return ""
        return "" if value is None else str(value).strip()

    def _config_float(self, key: str, default: float) -> float:
        try:
            raw = self.ctx.config.get(key, default)
        except RuntimeError:
            raw = default
        try:
            return float(raw)
        except (TypeError, ValueError, OverflowError):
            return float("nan")

    def _config_bool(self, key: str, default: bool) -> bool:
        try:
            value = self.ctx.config.get(key, default)
        except RuntimeError:
            value = default
        if isinstance(value, bool):
            return value
        return str(value).strip().casefold() in {"1", "true", "yes", "on", "是", "开"}

    def _poll_seconds(self) -> float:
        try:
            raw = self.ctx.config.get("poll_seconds", 300)
        except RuntimeError:
            raw = 300
        try:
            return float(raw)
        except (TypeError, ValueError, OverflowError):
            return float("nan")

    def _notify_groups(self) -> list[str]:
        try:
            raw: Any = self.ctx.config.get("notify_group_ids", [])
        except RuntimeError:
            raw = []
        if isinstance(raw, str):
            values = raw.replace("，", ",").split(",")
        elif isinstance(raw, list):
            values = raw
        else:
            values = []
        return list(
            dict.fromkeys(str(value).strip() for value in values if str(value).strip())
        )

    @staticmethod
    def _poll_text(result: PollResult) -> str:
        parts = [
            "Sub2API 轮询完成。",
            (
                f"订阅：上架 {result.subscription_added}、下架 "
                f"{result.subscription_removed}、更新 {result.subscription_changed}。"
            ),
            (
                f"分组倍率：新增 {result.rates_added}、移除 "
                f"{result.rates_removed}、变化 {result.rates_changed}。"
            ),
        ]
        if result.initialized:
            parts.append(f"已初始化快照：{', '.join(result.initialized)}。")
        if result.notifications_sent:
            parts.append(f"已发送 {result.notifications_sent} 条通知。")
        if result.errors:
            parts.append("部分操作失败：" + "；".join(result.errors))
        if not result.change_count and not result.errors and not result.initialized:
            parts.append("没有检测到变化。")
        return "".join(parts)


def _format_change(
    event_type: str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> str:
    record = after or before or {}
    if event_type.startswith("subscription"):
        title = (
            record.get("name")
            or record.get("product_name")
            or record.get("id")
            or "未命名订阅"
        )
        group = record.get("group_name") or record.get("group_id") or "未知分组"
        price = record.get("price")
        suffix = f"，价格 {price}" if price not in (None, "") else ""
        labels = {
            "subscription_added": "订阅上架",
            "subscription_removed": "订阅下架",
            "subscription_changed": "订阅更新",
        }
        return f"【Sub2API】{labels[event_type]}：{title}（分组：{group}{suffix}）"

    group = record.get("group_name") or record.get("name") or record.get("id") or "未知分组"
    labels = {
        "rate_added": "分组倍率新增",
        "rate_removed": "分组倍率移除",
        "rate_changed": "分组倍率变化",
    }
    if event_type == "rate_changed":
        detail = _rate_change_detail(before or {}, after or {})
    elif event_type == "rate_removed":
        detail = f"{_primary_rate_value(before)} → 已移除"
    else:
        detail = f"新增 {_primary_rate_value(after)}"
    return f"【Sub2API】{labels[event_type]}：{group}，{detail}"


def _rate_change_detail(before: dict[str, Any], after: dict[str, Any]) -> str:
    identity_keys = {"id", "group_id", "group_name", "name", "platform", "slug"}
    changed = []
    for key in sorted((before.keys() | after.keys()) - identity_keys):
        old_value = before.get(key, "未设置")
        new_value = after.get(key, "未设置")
        if old_value != new_value:
            changed.append(f"{key}: {old_value} → {new_value}")
    return "；".join(changed) or "倍率配置已更新"


def _primary_rate_value(record: dict[str, Any] | None) -> Any:
    if not record:
        return "未知"
    for key in (
        "rate_multiplier",
        "rate",
        "multiplier",
        "model_ratio",
        "completion_ratio",
        "input_ratio",
        "output_ratio",
        "ratio",
        "weight",
        "value",
    ):
        value = record.get(key)
        if value not in (None, ""):
            return value
    return "未知"


def _format_records(title: str, records: list[dict[str, Any]]) -> str:
    body = json.dumps(records, ensure_ascii=False, indent=2)
    if len(body) > 3500:
        body = body[:3500] + "\n...（输出过长，已截断）"
    return f"{title}（{len(records)} 条）\n```json\n{body}\n```"
