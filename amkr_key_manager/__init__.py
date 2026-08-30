"""AMKR key management plugin for Sirius Pulse.

Usage (chat commands):
    /amkrkey help
    /amkrkey models
    /amkrkey show <model> [key_name]
    /amkrkey create_model <model> <key_name> <api_key>
    /amkrkey update_model <model> [--aliases gpt-4o,4o] [--enabled true]
    /amkrkey delete_model <model>
    /amkrkey add <model> <key_name> <api_key>
    /amkrkey update <model> <key_name> [--api_key sk-...] [--enabled true] [--allow_visitor false]
    /amkrkey enable <model> <key_name>
    /amkrkey disable <model> <key_name>
    /amkrkey visitor_on <model> <key_name>
    /amkrkey visitor_off <model> <key_name>
    /amkrkey delete <model> <key_name>
    /amkrkey switch <model> <key_name>

Configuration:
    Environment variables:
      AMKR_BASE_URL / AMKR_URL       Management API base URL, default http://127.0.0.1:8000
      AMKR_LOCAL_API_KEY             Local management API key used as Bearer auth
      AMKR_CLI                       CLI executable for the switch action, default amkr

    Per-command options can override env values:
      --base_url http://127.0.0.1:8000
      --local_api_key xxxxx

Notes:
    - The managed API key value is intentionally masked in responses.
    - Use underscores in option names because the project command parser maps kwargs by name.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from sirius_pulse.plugins import CommandAST, PluginBase, PluginResponse, command


_TRUE_VALUES = {"1", "true", "yes", "y", "on", "enable", "enabled", "允许", "是", "开"}
_FALSE_VALUES = {"0", "false", "no", "n", "off", "disable", "disabled", "禁止", "否", "关"}
_SECRET_FIELD_NAMES = {"api_key", "apikey", "access_token", "token", "secret", "local_api_key"}


class AmkrKeyManagerPlugin(PluginBase):
    """Manage AMKR models and API keys through AMKR's local management API."""

    _plugin_name = "amkr_key_manager"
    _plugin_display_name = "AMKR Key Manager"
    _plugin_description = "通过聊天命令管理 AMKR 的模型与 API Key。"
    _plugin_version = "1.0.0"
    _plugin_author = "AION"
    _plugin_permissions = {
        "developer_only": True,
        "hidden_from_intent": False,
        "rate_limit": {"calls_per_minute": 20, "calls_per_hour": 300},
    }
    _plugin_parameters = [
        {
            "name": "base_url",
            "type": "str",
            "description": "AMKR 管理 API 地址。也可用 AMKR_BASE_URL/AMKR_URL 覆盖。",
            "required": False,
            "default": "http://127.0.0.1:8000",
            "position": 100,
            "group": "AMKR 连接",
        },
        {
            "name": "local_api_key",
            "type": "password",
            "description": "AMKR 本地管理 API Key，会作为 Bearer Token 发送。也可用 AMKR_LOCAL_API_KEY 覆盖。",
            "required": False,
            "default": "",
            "position": 101,
            "group": "AMKR 连接",
        },
        {
            "name": "cli_executable",
            "type": "str",
            "description": "switch 动作调用的 AMKR CLI 可执行文件。也可用 AMKR_CLI 覆盖。",
            "required": False,
            "default": "amkr",
            "position": 102,
            "group": "AMKR 连接",
        },
        {
            "name": "timeout",
            "type": "float",
            "description": "AMKR API/CLI 调用超时时间（秒）。",
            "required": False,
            "default": 20,
            "position": 103,
            "group": "AMKR 连接",
        },
    ]
    _plugin_prompt_inject = "可使用 /amkrkey 命令管理 AMKR 模型和 API Key；涉及密钥的回复必须脱敏。"

    @command(
        "amkrkey",
        patterns=["/amkrkey", "/amkr_key", "amkrkey"],
        pattern_type="prefix",
        render_mode="direct",
        description="管理 AMKR 模型与 API Key。",
        examples=[
            "/amkrkey help",
            "/amkrkey models",
            "/amkrkey show gpt-4o main",
            "/amkrkey add gpt-4o backup sk-xxxx",
            "/amkrkey update gpt-4o backup --enabled false",
            "/amkrkey switch gpt-4o backup",
        ],
    )
    def register_amkrkey_command(self) -> PluginResponse:
        """Only used for command discovery; execution is handled by execute_async()."""
        return PluginResponse.ok(text="请使用 /amkrkey help 查看 AMKR Key 管理命令。")

    async def execute_async(self, cmd: CommandAST) -> list[PluginResponse]:
        try:
            response = await self._handle(cmd)
        except Exception as exc:  # noqa: BLE001 - Plugin boundary should never leak exceptions.
            self.logger.exception("AMKR Key 管理插件执行失败")
            response = PluginResponse.fail(f"AMKR Key 管理失败：{exc}")
        return [response]

    async def _handle(self, cmd: CommandAST) -> PluginResponse:
        action = self._arg(cmd, 0, "help").strip().lower().replace("-", "_")
        aliases = {
            "ls": "models",
            "list": "models",
            "model": "show",
            "get": "show",
            "create": "create_model",
            "new_model": "create_model",
            "rm_model": "delete_model",
            "remove_model": "delete_model",
            "del_model": "delete_model",
            "add_key": "add",
            "new_key": "add",
            "put": "update",
            "set": "update",
            "rm": "delete",
            "remove": "delete",
            "del": "delete",
            "on": "enable",
            "off": "disable",
            "visitor_enable": "visitor_on",
            "visitor_disable": "visitor_off",
            "use": "switch",
            "switch_key": "switch",
        }
        action = aliases.get(action, action)

        if action in {"help", "?", ""}:
            return PluginResponse.ok(text=self._help_text())

        if action == "switch":
            return await self._switch_key(cmd)

        client = _AmkrClient(
            base_url=self._base_url(cmd),
            local_api_key=self._local_api_key(cmd),
            timeout=float(self._option(cmd, "timeout") or self._config_value("timeout", default="20") or 20),
        )

        if action == "models":
            data = await asyncio.to_thread(client.request, "GET", "/api/keys")
            return PluginResponse.ok(text=self._format_data("AMKR 模型/Key 列表", data), data=_sanitize(data))

        if action == "show":
            model = self._require_arg(cmd, 1, "model")
            key_name = self._arg(cmd, 2, "") or self._option(cmd, "key", "key_name", "name")
            path = f"/api/keys/{_q(model)}" + (f"/{_q(key_name)}" if key_name else "")
            data = await asyncio.to_thread(client.request, "GET", path)
            title = f"AMKR Key：{model}/{key_name}" if key_name else f"AMKR 模型：{model}"
            return PluginResponse.ok(text=self._format_data(title, data), data=_sanitize(data))

        if action == "create_model":
            model = self._require_arg(cmd, 1, "model")
            key_name = self._require_arg(cmd, 2, "key_name")
            api_key = self._api_key_value(cmd, positional_index=3)
            payload = self._model_payload(cmd, include_empty=True)
            payload.update({"model_name": model, "key_name": key_name, "api_key": api_key})
            data = await asyncio.to_thread(client.request, "POST", "/api/keys", payload)
            return PluginResponse.ok(text=self._format_data(f"已创建 AMKR 模型：{model}", data), data=_sanitize(data))

        if action == "update_model":
            model = self._require_arg(cmd, 1, "model")
            payload = self._model_payload(cmd, include_empty=False)
            if not payload:
                return PluginResponse.fail("update_model 至少需要一个选项：--aliases/--enabled/--routing_mode/--reasoning_effort")
            data = await asyncio.to_thread(client.request, "PUT", f"/api/keys/{_q(model)}", payload)
            return PluginResponse.ok(text=self._format_data(f"已更新 AMKR 模型：{model}", data), data=_sanitize(data))

        if action == "delete_model":
            model = self._require_arg(cmd, 1, "model")
            data = await asyncio.to_thread(client.request, "DELETE", f"/api/keys/{_q(model)}")
            return PluginResponse.ok(text=self._format_data(f"已删除 AMKR 模型：{model}", data), data=_sanitize(data))

        if action == "add":
            model = self._require_arg(cmd, 1, "model")
            key_name = self._require_arg(cmd, 2, "key_name")
            api_key = self._api_key_value(cmd, positional_index=3)
            payload = self._key_payload(cmd, include_empty=True)
            payload.update({"key_name": key_name, "api_key": api_key})
            data = await asyncio.to_thread(client.request, "POST", f"/api/keys/{_q(model)}", payload)
            return PluginResponse.ok(text=self._format_data(f"已为 {model} 添加 Key：{key_name}", data), data=_sanitize(data))

        if action == "update":
            model = self._require_arg(cmd, 1, "model")
            key_name = self._require_arg(cmd, 2, "key_name")
            payload = self._key_payload(cmd, include_empty=False)
            if not payload:
                return PluginResponse.fail("update 至少需要一个选项：--api_key/--enabled/--allow_visitor")
            data = await asyncio.to_thread(client.request, "PUT", f"/api/keys/{_q(model)}/{_q(key_name)}", payload)
            return PluginResponse.ok(text=self._format_data(f"已更新 {model}/{key_name}", data), data=_sanitize(data))

        if action in {"enable", "disable"}:
            model = self._require_arg(cmd, 1, "model")
            key_name = self._require_arg(cmd, 2, "key_name")
            enabled = action == "enable"
            data = await asyncio.to_thread(
                client.request,
                "PUT",
                f"/api/keys/{_q(model)}/{_q(key_name)}",
                {"enabled": enabled},
            )
            label = "启用" if enabled else "禁用"
            return PluginResponse.ok(text=self._format_data(f"已{label} {model}/{key_name}", data), data=_sanitize(data))

        if action in {"visitor_on", "visitor_off"}:
            model = self._require_arg(cmd, 1, "model")
            key_name = self._require_arg(cmd, 2, "key_name")
            allow_visitor = action == "visitor_on"
            data = await asyncio.to_thread(
                client.request,
                "PUT",
                f"/api/keys/{_q(model)}/{_q(key_name)}",
                {"allow_visitor": allow_visitor},
            )
            label = "允许访客使用" if allow_visitor else "禁止访客使用"
            return PluginResponse.ok(text=self._format_data(f"已{label} {model}/{key_name}", data), data=_sanitize(data))

        if action == "delete":
            model = self._require_arg(cmd, 1, "model")
            key_name = self._require_arg(cmd, 2, "key_name")
            data = await asyncio.to_thread(client.request, "DELETE", f"/api/keys/{_q(model)}/{_q(key_name)}")
            return PluginResponse.ok(text=self._format_data(f"已删除 {model}/{key_name}", data), data=_sanitize(data))

        return PluginResponse.fail(f"未知动作：{action}\n\n{self._help_text()}")

    async def _switch_key(self, cmd: CommandAST) -> PluginResponse:
        model = self._require_arg(cmd, 1, "model")
        key_name = self._require_arg(cmd, 2, "key_name")
        cli = self._option(
            cmd,
            "cli",
            "cli_executable",
            default=self._config_value("cli_executable", "cli", default=os.getenv("AMKR_CLI", "amkr")),
        )

        def run_switch() -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [cli, "switch-key", model, key_name],
                text=True,
                capture_output=True,
                timeout=float(self._option(cmd, "timeout") or self._config_value("timeout", default="20") or 20),
                check=False,
            )

        completed = await asyncio.to_thread(run_switch)
        stdout = (completed.stdout or "").strip()
        stderr = (completed.stderr or "").strip()
        if completed.returncode != 0:
            return PluginResponse.fail(
                "AMKR CLI switch-key 执行失败："
                f"exit={completed.returncode}\n{stderr or stdout or '无输出'}"
            )
        text = f"已切换 AMKR Key：{model}/{key_name}"
        if stdout:
            text += f"\n{stdout}"
        return PluginResponse.ok(text=text)

    def _model_payload(self, cmd: CommandAST, *, include_empty: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        aliases = self._option(cmd, "aliases")
        if aliases or include_empty:
            payload["aliases"] = _split_csv(aliases)
        for name in ("enabled", "routing_mode", "reasoning_effort"):
            value = self._option(cmd, name)
            if value != "":
                payload[name] = _parse_bool(value) if name == "enabled" else value
        return payload

    def _key_payload(self, cmd: CommandAST, *, include_empty: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        api_key = self._option(cmd, "api_key", "apikey")
        if api_key or include_empty:
            payload["api_key"] = api_key
        for name in ("enabled", "allow_visitor"):
            value = self._option(cmd, name)
            if value != "":
                payload[name] = _parse_bool(value)
        return payload

    def _api_key_value(self, cmd: CommandAST, *, positional_index: int) -> str:
        value = self._option(cmd, "api_key", "apikey") or self._arg(cmd, positional_index, "")
        if not value:
            raise ValueError("缺少必填参数 api_key，请作为位置参数传入或使用 --api_key")
        return value

    def _base_url(self, cmd: CommandAST) -> str:
        return (
            self._option(cmd, "base_url", "base-url")
            or self._config_value("base_url", "base-url")
            or os.getenv("AMKR_BASE_URL")
            or os.getenv("AMKR_URL")
            or "http://127.0.0.1:8000"
        )

    def _local_api_key(self, cmd: CommandAST) -> str:
        return (
            self._option(cmd, "local_api_key", "local-api-key", "admin_key", "admin-key")
            or self._config_value("local_api_key", "local-api-key", "admin_key", "admin-key")
            or os.getenv("AMKR_LOCAL_API_KEY")
            or os.getenv("AMKR_ADMIN_KEY")
            or ""
        )

    def _config_value(self, *names: str, default: str = "") -> str:
        try:
            config = self.ctx.config
        except RuntimeError:
            return default
        for name in names:
            candidates = {name, name.replace("-", "_"), name.replace("_", "-")}
            for candidate in candidates:
                value = config.get(candidate)
                if value not in (None, ""):
                    return str(value)
        return default

    @staticmethod
    def _arg(cmd: CommandAST, index: int, default: str = "") -> str:
        if 0 <= index < len(cmd.args):
            return str(cmd.args[index].value)
        return default

    @classmethod
    def _require_arg(cls, cmd: CommandAST, index: int, name: str) -> str:
        value = cls._arg(cmd, index, "").strip()
        if not value:
            raise ValueError(f"缺少必填参数 {name}")
        return value

    @staticmethod
    def _option(cmd: CommandAST, *names: str, default: str = "") -> str:
        """Return only explicitly supplied command options.

        Plugin parameters are registered for WebUI configuration, and the command parser
        may also copy default/positional values into cmd.kwargs.  To avoid treating
        `/amkrkey models` as `base_url=models`, only values whose option token is
        present in raw_text are considered command overrides.
        """
        raw_text = cmd.raw_text or ""
        for name in names:
            candidates = {name, name.replace("-", "_"), name.replace("_", "-")}
            for candidate in candidates:
                if candidate in cmd.flags and _has_option_token(raw_text, candidate):
                    return "true"
                if candidate in cmd.kwargs and _has_option_token(raw_text, candidate):
                    return str(cmd.kwargs[candidate].value)
        return default

    @staticmethod
    def _format_data(title: str, data: Any) -> str:
        safe_data = _sanitize(data)
        body = json.dumps(safe_data, ensure_ascii=False, indent=2)
        if len(body) > 3500:
            body = body[:3500] + "\n...（输出过长，已截断）"
        return f"{title}\n```json\n{body}\n```"

    @staticmethod
    def _help_text() -> str:
        return """AMKR Key 管理命令：
/amkrkey models
  列出所有模型与 Key 概览。
/amkrkey show <model> [key_name]
  查看模型或某个 Key 的详情。
/amkrkey create_model <model> <key_name> <api_key> [--aliases a,b] [--enabled true]
  创建模型，并写入首个 Key。
/amkrkey update_model <model> [--aliases a,b] [--enabled true|false] [--routing_mode ...] [--reasoning_effort ...]
  更新模型配置。
/amkrkey delete_model <model>
  删除模型。
/amkrkey add <model> <key_name> <api_key> [--enabled true] [--allow_visitor false]
  为现有模型添加 Key。
/amkrkey update <model> <key_name> [--api_key sk-...] [--enabled true|false] [--allow_visitor true|false]
  更新 Key。
/amkrkey enable|disable <model> <key_name>
  启用或禁用 Key。
/amkrkey visitor_on|visitor_off <model> <key_name>
  开启或关闭访客使用权限。
/amkrkey delete <model> <key_name>
  删除 Key。
/amkrkey switch <model> <key_name>
  调用 AMKR CLI：amkr switch-key <model> <key_name>。

可选连接参数：--base_url http://127.0.0.1:8000 --local_api_key xxxxx
也可通过环境变量设置：AMKR_BASE_URL / AMKR_URL / AMKR_LOCAL_API_KEY / AMKR_CLI
注意：回复会自动脱敏 api_key/token/secret 字段。"""


class _AmkrClient:
    def __init__(self, *, base_url: str, local_api_key: str = "", timeout: float = 20.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.local_api_key = local_api_key
        self.timeout = timeout

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        headers = {"Accept": "application/json"}
        body: bytes | None = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if self.local_api_key:
            headers["Authorization"] = f"Bearer {self.local_api_key}"
        request = Request(url, data=body, headers=headers, method=method.upper())
        try:
            with urlopen(request, timeout=self.timeout) as response:  # noqa: S310 - local/admin URL is user configured.
                raw = response.read().decode("utf-8")
                if not raw.strip():
                    return {"status": response.status, "ok": 200 <= response.status < 300}
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    return {"status": response.status, "text": raw}
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(detail)
                detail = json.dumps(_sanitize(parsed), ensure_ascii=False)
            except json.JSONDecodeError:
                pass
            raise RuntimeError(f"AMKR API HTTP {exc.code}: {detail or exc.reason}") from exc
        except URLError as exc:
            raise RuntimeError(f"无法连接 AMKR API：{exc.reason}") from exc


def _has_option_token(raw_text: str, option_name: str) -> bool:
    # Long-option names may use '-' or '_' interchangeably in chat commands.
    variants = {option_name, option_name.replace("-", "_"), option_name.replace("_", "-")}
    return any(re.search(rf"(?<!\S)--{re.escape(variant)}(?:\s|=|$)", raw_text) for variant in variants)


def _q(value: str) -> str:
    return quote(value, safe="")


def _split_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _parse_bool(value: str | bool) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise ValueError(f"无法解析布尔值：{value!r}")


def _mask_secret(value: Any) -> str:
    text = str(value)
    if len(text) <= 8:
        return "****"
    return f"{text[:4]}...{text[-4:]}"


def _sanitize(value: Any, *, field_name: str = "") -> Any:
    normalized_name = field_name.lower().replace("-", "_")
    if normalized_name in _SECRET_FIELD_NAMES:
        return _mask_secret(value)
    if isinstance(value, dict):
        return {str(k): _sanitize(v, field_name=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize(item, field_name=field_name) for item in value]
    return value
