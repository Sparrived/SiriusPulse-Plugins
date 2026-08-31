"""Full GitHub repository monitor plugin for Sirius Pulse."""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from sirius_pulse.plugins.api import (
    BackgroundTaskSpec,
    PluginBase,
    PluginResponse,
    command,
)

from . import monitor

logger = logging.getLogger(__name__)

_CONFIG_DEFAULTS: dict[str, Any] = {
    "poll_seconds": 120,
    "api_base_url": "https://api.github.com",
    "api_allowed_hosts": [],
    "webhook_secret": "",
    "webhook_secret_env": "",
    "webhook_host": "127.0.0.1",
    "webhook_port": 0,
    "allow_unsigned_local": False,
    "repos": [],
}
_CONFIG_KEYS = frozenset(_CONFIG_DEFAULTS)
_ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_MASKED_SECRETS = {"********", "[已隐藏]", "••••••••"}


def _strict_bool(value: Any, default: bool = False) -> bool:
    """Parse persisted booleans without treating ``\"false\"`` as true."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"1", "true", "yes", "on", "enabled"}:
            return True
        if normalized in {"0", "false", "no", "off", "disabled", ""}:
            return False
    return default


class _MonitorStore:
    """Adapt PluginDataStore and Plugin settings to the legacy monitor store API."""

    def __init__(self, plugin: GitHubMonitorPlugin) -> None:
        self._plugin = plugin

    @property
    def artifact_dir(self) -> Path:
        return self._plugin.get_artifact_dir()

    @property
    def webhook_state_path(self) -> Path:
        """Return a private durable inbox snapshot path for this persona."""
        store_path = self._plugin.get_data_store().store_path
        return store_path.with_name("_plugin_github_monitor_webhook_state.json")

    def reload(self) -> None:
        self._plugin.get_data_store().reload()

    def save(self) -> None:
        # PluginDataStore persists each mutation atomically.
        return None

    def get(self, key: str, default: Any = None) -> Any:
        # WebUI settings are authoritative only when explicitly present; this
        # keeps migrated per-persona state usable for omitted fields.
        settings = self._plugin.ctx.config
        stored_data = self._plugin.get_data_store()
        stored = stored_data.get(key, None)
        value = (
            settings[key] if key in settings and settings[key] is not None else stored
        )
        if value is None and key in _CONFIG_DEFAULTS:
            value = _CONFIG_DEFAULTS[key]
        if key == "webhook_secret":
            env_name = settings.get("webhook_secret_env") or stored_data.get(
                "webhook_secret_env", ""
            )
            if isinstance(env_name, str) and _ENV_NAME_PATTERN.fullmatch(env_name):
                return os.getenv(env_name, "").strip()
        return value if value is not None else default

    def set(self, key: str, value: Any) -> None:
        self._plugin.get_data_store().set(key, value)

    def update(self, values: dict[str, Any]) -> None:
        """Persist a state batch through the atomic PluginDataStore API."""
        data_store = self._plugin.get_data_store()
        update = getattr(data_store, "update", None)
        if callable(update):
            update(values)
            return
        for key, value in values.items():
            data_store.set(key, value)

    def delete(self, key: str) -> None:
        self._plugin.get_data_store().delete(key)

    def all(self) -> dict[str, Any]:
        data = self._plugin.get_data_store().all()
        data.update(
            {
                key: value
                for key, value in self._plugin.ctx.config.items()
                if key in _CONFIG_KEYS
            }
        )
        return data


class _MonitorContext:
    """Expose the original passive Tool context contract to the Plugin implementation."""

    def __init__(self, plugin: GitHubMonitorPlugin) -> None:
        self._plugin = plugin
        self._store = _MonitorStore(plugin)

    def get_data_store(self, _name: str = "github_monitor") -> _MonitorStore:
        return self._store

    def get_active_groups(self) -> list[str]:
        return self._plugin.ctx.get_active_groups()

    def get_current_adapter_type(self) -> str:
        getter = getattr(self._plugin.ctx.engine, "get_current_adapter_type", None)
        if callable(getter):
            value = getter()
            return value if isinstance(value, str) else ""
        return ""

    def get_bridge_owner(self) -> str:
        """Return a stable scope for this persona's bridge registrations."""
        persona_name = ""
        getter = getattr(self._plugin.ctx.engine, "get_persona_name", None)
        if callable(getter):
            try:
                persona_name = str(getter() or "").strip()
            except Exception:
                persona_name = ""
        if not persona_name:
            engine = self._plugin.ctx.engine.get_engine()
            persona = getattr(engine, "persona", None) if engine is not None else None
            persona_name = str(getattr(persona, "name", "") or "").strip()
        return f"{persona_name}:github_monitor" if persona_name else "github_monitor"

    def get_persona(self) -> Any:
        engine = self._plugin.ctx.engine.get_engine()
        return getattr(engine, "persona", None) if engine is not None else None

    def log_inner_thought(self, text: str) -> None:
        engine = self._plugin.ctx.engine.get_engine()
        handler = (
            getattr(engine, "_log_inner_thought", None) if engine is not None else None
        )
        if callable(handler):
            handler(text)

    async def generate_text(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        group_id: str,
        task_name: str = "plugin_generate",
        **kwargs: Any,
    ) -> str:
        return await self._plugin.ctx.engine.generate_text(
            system_prompt,
            messages=messages,
            group_id=group_id,
            task_name=task_name,
            **kwargs,
        )

    def queue_pending_message(
        self, group_id: str, text: str, adapter_type: str = ""
    ) -> None:
        # The following reminder_triggered event performs the actual queueing.
        return None

    async def emit_event(self, event_type: str, data: dict[str, Any]) -> bool:
        if event_type != "reminder_triggered":
            result = await self._plugin.ctx.engine.emit_event(event_type, data)
            return result is not False
        return await self._plugin.ctx.dispatch_proactive_message(
            group_id=str(data.get("group_id", "")),
            text=str(data.get("reply", "")),
            adapter_type=str(data.get("adapter_type", "") or ""),
            event_id=str(data.get("reminder_id", "") or data.get("id", "")),
            image_path=str(data.get("image_path", "")),
            reply_references=data.get("reply_references", []),
            sticker_names=data.get("sticker_names", []),
            poke_user_ids=data.get("poke_user_ids", []),
        )

    def activate_private_group(self, _group_id: str) -> None:
        # dispatch_proactive_message performs private-group activation.
        return None


class GitHubMonitorPlugin(PluginBase):
    """Monitor GitHub repositories through Poll or Webhook delivery."""

    _plugin_name = "github_monitor"
    _plugin_display_name = "GitHub Monitor"
    _plugin_description = "监控 GitHub Issue、PR、Release、Comment 和 Push 动态。"
    _plugin_version = "1.1.0"
    _plugin_author = "Sparrived"
    _plugin_min_framework_version = "1.3.0"
    _plugin_dependencies = ["aiohttp>=3.9.0", "httpx>=0.24.0", "playwright>=1.57.0"]
    _plugin_permissions = {
        "hidden_from_intent": True,
        "rate_limit": {"calls_per_minute": 20, "calls_per_hour": 300},
    }
    _plugin_parameters = [
        {
            "name": "poll_seconds",
            "type": "int",
            "description": "轮询间隔（秒），范围 30-3600。",
            "default": 120,
            "group": "基本设置",
        },
        {
            "name": "api_base_url",
            "type": "str",
            "description": "GitHub/GHE API origin；只允许 HTTPS。",
            "default": "https://api.github.com",
            "group": "基本设置",
        },
        {
            "name": "api_allowed_hosts",
            "type": "list",
            "description": "允许的 GHE 主机名；使用自建 GHE 时填写，不要填写路径或端口。",
            "default": [],
            "group": "基本设置",
        },
        {
            "name": "webhook_secret",
            "type": "password",
            "description": "GitHub Webhook HMAC Secret；建议只通过环境变量提供。",
            "default": "",
            "group": "Webhook",
        },
        {
            "name": "webhook_secret_env",
            "type": "str",
            "description": "读取 Webhook Secret 的环境变量名。",
            "default": "",
            "group": "Webhook",
        },
        {
            "name": "webhook_host",
            "type": "str",
            "description": "Webhook 监听地址。",
            "default": "127.0.0.1",
            "group": "Webhook",
        },
        {
            "name": "webhook_port",
            "type": "int",
            "description": "Webhook 监听端口，0 表示随机端口。",
            "default": 0,
            "group": "Webhook",
        },
        {
            "name": "allow_unsigned_local",
            "type": "bool",
            "description": "仅回环地址开发调试时允许无签名请求（生产环境请关闭）。",
            "default": False,
            "group": "Webhook",
        },
        {
            "name": "repos",
            "type": "object_array",
            "description": "要监控的 GitHub 仓库。",
            "fields": [
                {"name": "owner", "type": "str", "description": "仓库所有者"},
                {"name": "repo", "type": "str", "description": "仓库名称"},
                {
                    "name": "mode",
                    "type": "str",
                    "description": "poll 或 webhook",
                    "default": "poll",
                    "choices": ["poll", "webhook"],
                },
                {
                    "name": "events",
                    "type": "checkbox_group",
                    "description": "事件类型",
                    "choices": ["issues", "pulls", "releases", "comments", "pushes"],
                },
                {"name": "groups", "type": "list", "description": "通知目标群号"},
                {
                    "name": "github_token",
                    "type": "password",
                    "description": "仓库 Token（仅兼容旧配置；建议改用环境变量）",
                },
                {
                    "name": "github_token_env",
                    "type": "str",
                    "description": "读取 Token 的环境变量名（推荐，避免明文落盘）",
                },
            ],
            "group": "监控仓库",
        },
    ]

    def __init__(self) -> None:
        super().__init__()
        self._monitor_ctx: _MonitorContext | None = None
        self._legacy_enabled = True

    def _legacy_context(self) -> _MonitorContext:
        if self._monitor_ctx is None:
            self._monitor_ctx = _MonitorContext(self)
        return self._monitor_ctx

    async def on_load(self) -> None:
        self._migrate_legacy_store()
        self._legacy_enabled = _strict_bool(
            self.get_data_store().get("_enabled", True), default=True
        )
        if self._legacy_enabled:
            await monitor.create_on_load(self._legacy_context())

    async def on_unload(self) -> None:
        await monitor.create_on_unload(self._legacy_context())

    def create_background_tasks(self) -> list[BackgroundTaskSpec]:
        if not self._legacy_enabled:
            return []
        specs = monitor.create_background_tasks(self._legacy_context())
        return [spec for spec in specs if isinstance(spec, BackgroundTaskSpec)]

    @command(
        "github",
        prefix="/",
        patterns=["github", "github-monitor"],
        render_mode="direct",
        description="查看 GitHub 监控状态或立即轮询",
        hidden_from_intent=True,
    )
    async def github(self, action: str = "status") -> PluginResponse:
        action = action.strip().lower()
        if action in {"poll", "check", "检查", "轮询"}:
            await monitor._poll_github_events(self._legacy_context())
            return PluginResponse.ok(text="GitHub 监控已完成一次轮询。")
        if action in {"status", "状态", ""}:
            store = self._legacy_context().get_data_store()
            repos = list(store.get("repos", []) or [])
            webhook_port = int(store.get("_webhook_port", 0) or 0)
            suffix = f"，Webhook 端口 {webhook_port}" if webhook_port else ""
            return PluginResponse.ok(text=f"GitHub 监控运行中，已配置 {len(repos)} 个仓库{suffix}。")
        return PluginResponse.fail("用法：/github status 或 /github poll")

    def _migrate_legacy_store(self) -> None:
        """Import only non-secret legacy state through one atomic store update.

        A legacy credential cannot be safely copied into a process environment
        or an unknown secret manager from inside the Plugin.  Therefore this
        migration is deliberately fail-closed: whenever a plaintext GitHub
        credential still exists on any known persistence surface, it preserves
        that source unchanged, records a non-secret pending marker, and does
        *not* claim completion.  Operators must provision an environment/secret
        reference, rotate the old credential, and remove the plaintext before a
        subsequent load can finalize the non-secret state migration.
        """
        store = self.get_data_store()
        try:
            migration_version = int(store.get("_legacy_migration_version", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            migration_version = 0
        if migration_version >= 1:
            return

        plugin_data_dir = store.store_path.parent
        persona_dir = plugin_data_dir.parent
        candidates = [
            persona_dir / "tool_data" / "github_monitor.json",
            persona_dir / "skill_data" / "github_monitor.json",
            persona_dir / "skill_data" / ".persona_skills.json",
        ]
        existing = [path for path in candidates if path.is_file()]
        merged: dict[str, Any] = {}
        source_payloads: list[dict[str, Any]] = []
        try:
            for path in existing:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError(f"旧配置根值不是对象: {path}")
                payload = self._legacy_payload(raw)
                if payload:
                    source_payloads.append(payload)
                    merged.update(payload)
                    logger.info("发现旧 GitHub 监控数据: %s", path)
        except Exception:
            # Leave the marker untouched so the next load can retry after repair.
            logger.exception("迁移旧 GitHub 监控数据失败")
            return

        current = store.all()
        runtime_settings = self.ctx.config if isinstance(self.ctx.config, dict) else {}
        source_has_plaintext = any(
            self._contains_plaintext_secret(payload) for payload in source_payloads
        )
        current_has_plaintext = self._contains_plaintext_secret(current)
        runtime_has_plaintext = self._contains_plaintext_secret(runtime_settings)
        if source_has_plaintext or current_has_plaintext or runtime_has_plaintext:
            # Updating a PluginDataStore that already contains a legacy secret
            # serializes that same secret again.  Do not perform even a marker
            # write in that case; preserve it untouched for an operator-led,
            # externally provisioned rotation.
            if not current_has_plaintext:
                self._record_pending_credential_migration(store)
            logger.warning(
                "检测到旧 GitHub 明文凭据；保留原配置且不标记迁移完成。" "请先在受支持的 Secret 管理器或进程环境中配置引用并轮换旧凭据。"
            )
            return

        # No source file is rewritten here.  Keeping legacy files intact avoids
        # a multi-file secret handoff transaction that could otherwise erase the
        # only usable credential when a later store write fails.
        merged = self._sanitize_payload(merged)
        conflicts = sorted(
            key
            for key, value in merged.items()
            if key in current and current[key] != value
        )
        if conflicts:
            logger.warning(
                "旧 GitHub 监控状态与现有 Plugin 状态冲突；保留现有字段: %s",
                ", ".join(conflicts),
            )
        updates = {key: value for key, value in merged.items() if key not in current}
        # The marker is deliberately part of the one atomic PluginDataStore
        # update.  Do not fall back to a sequence of set() calls here.
        updates["_legacy_migration_version"] = 1
        updates["_legacy_migration_pending"] = ""
        update = getattr(store, "update", None)
        if not callable(update):
            logger.error("PluginDataStore 不支持原子 update，拒绝标记旧配置迁移完成")
            return
        try:
            update(updates)
        except Exception:
            logger.warning("写入 GitHub 监控迁移状态失败，将在下次加载重试", exc_info=True)

    @staticmethod
    def _contains_plaintext_secret(value: Any) -> bool:
        """Detect legacy GitHub credential fields without logging their values."""
        if isinstance(value, list):
            return any(
                GitHubMonitorPlugin._contains_plaintext_secret(item) for item in value
            )
        if not isinstance(value, dict):
            return False
        for raw_key, item in value.items():
            key = str(raw_key).strip().casefold()
            if key in {"github_token", "webhook_secret"}:
                if not isinstance(item, str):
                    if item:
                        return True
                elif item.strip() and item.strip() not in _MASKED_SECRETS:
                    return True
            if GitHubMonitorPlugin._contains_plaintext_secret(item):
                return True
        return False

    @staticmethod
    def _record_pending_credential_migration(store: Any) -> None:
        """Persist a non-secret marker while retaining the usable old source."""
        update = getattr(store, "update", None)
        if not callable(update):
            logger.error("PluginDataStore 不支持原子 update，无法记录凭据迁移待处理状态")
            return
        try:
            update({"_legacy_migration_pending": "credential_handoff_required"})
        except Exception:
            logger.warning("记录 GitHub 凭据迁移待处理状态失败", exc_info=True)

    @classmethod
    def _sanitize_repo(cls, value: Any) -> dict[str, Any]:
        """Normalize only non-secret repository fields during migration.

        This helper deliberately never converts, copies, or clears a credential.
        The caller must first prove no plaintext credential exists anywhere in
        the migration surfaces; environmental values are never read here.
        """
        if not isinstance(value, dict):
            return {}
        result = dict(value)
        token_env = str(result.get("github_token_env", "") or "").strip()
        if token_env and _ENV_NAME_PATTERN.fullmatch(token_env):
            result["github_token_env"] = token_env
        elif "github_token_env" in result:
            result.pop("github_token_env", None)
        return result

    @classmethod
    def _sanitize_payload(cls, payload: dict[str, Any]) -> dict[str, Any]:
        """Normalize non-secret legacy state after the fail-closed credential scan."""
        result = dict(payload)
        nested_config = result.get("config")
        if isinstance(nested_config, dict):
            result["config"] = cls._sanitize_payload(nested_config)
        repos = result.get("repos")
        if isinstance(repos, list):
            result["repos"] = [cls._sanitize_repo(item) for item in repos]
        secret_env = str(result.get("webhook_secret_env", "") or "").strip()
        if secret_env and _ENV_NAME_PATTERN.fullmatch(secret_env):
            result["webhook_secret_env"] = secret_env
        elif "webhook_secret_env" in result:
            result.pop("webhook_secret_env", None)
        if "enabled" in result and "_enabled" not in result:
            result["_enabled"] = _strict_bool(result["enabled"], default=True)
        return result

    @classmethod
    def _sanitize_payload_tree(cls, value: Any) -> Any:
        if isinstance(value, list):
            return [cls._sanitize_payload_tree(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {
            str(key): cls._sanitize_payload_tree(item) for key, item in value.items()
        }
        for key in ("github_monitor", "config"):
            nested = result.get(key)
            if isinstance(nested, dict):
                result[key] = cls._sanitize_payload(nested)
        repos = result.get("repos")
        if isinstance(repos, list):
            result["repos"] = [cls._sanitize_repo(item) for item in repos]
        if "github_token" in result or "webhook_secret" in result:
            result.update(cls._sanitize_payload(result))
        return result

    @staticmethod
    def _legacy_payload(raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            return {}
        direct_keys = _CONFIG_KEYS | {
            "config",
            "last_event_timestamps",
            "last_event_ids",
            "_last_poll_at",
            "_webhook_port",
            "_enabled",
            "enabled",
        }
        direct = {key: value for key, value in raw.items() if key in direct_keys}
        if direct:
            # Real persona skill files wrap the old tool state as
            # {"github_monitor": {"config": {...}, "enabled": ...}}.
            if isinstance(direct.get("config"), dict):
                nested = GitHubMonitorPlugin._legacy_payload(direct["config"])
                nested.update(
                    {key: value for key, value in direct.items() if key != "config"}
                )
                return nested
            return direct
        for key in ("github_monitor", "skills", "plugins", "config"):
            nested = raw.get(key)
            if isinstance(nested, dict):
                if key in {"skills", "plugins"}:
                    nested = nested.get("github_monitor", nested)
                payload = GitHubMonitorPlugin._legacy_payload(nested)
                if payload:
                    return payload
        return {}


from .client import GitHubClient, github_headers
from .event_bridge import (
    get_coding_bot_login,
    get_issue_repos,
    register_comment_handler,
    register_issue_handler,
    register_pr_handler,
    set_coding_bot_login,
    set_issue_repos,
)
from .events import fetch_compare_commit_count, fetch_compare_details, fetch_repo_events
from .webhook import GitHubWebhookServer, verify_signature

__all__ = [
    "GitHubClient",
    "GitHubMonitorPlugin",
    "GitHubWebhookServer",
    "fetch_compare_commit_count",
    "fetch_compare_details",
    "fetch_repo_events",
    "get_coding_bot_login",
    "get_issue_repos",
    "github_headers",
    "register_comment_handler",
    "register_issue_handler",
    "register_pr_handler",
    "set_coding_bot_login",
    "set_issue_repos",
    "verify_signature",
]
