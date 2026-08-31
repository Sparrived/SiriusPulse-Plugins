"""Full GitHub repository monitor plugin for Sirius Pulse."""

from __future__ import annotations

import hashlib
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
        value = settings[key] if key in settings and settings[key] is not None else stored
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
            {key: value for key, value in self._plugin.ctx.config.items() if key in _CONFIG_KEYS}
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

    def get_persona(self) -> Any:
        engine = self._plugin.ctx.engine.get_engine()
        return getattr(engine, "persona", None) if engine is not None else None

    def log_inner_thought(self, text: str) -> None:
        engine = self._plugin.ctx.engine.get_engine()
        handler = getattr(engine, "_log_inner_thought", None) if engine is not None else None
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

    def queue_pending_message(self, group_id: str, text: str, adapter_type: str = "") -> None:
        # The following reminder_triggered event performs the actual queueing.
        return None

    async def emit_event(self, event_type: str, data: dict[str, Any]) -> None:
        if event_type != "reminder_triggered":
            await self._plugin.ctx.engine.emit_event(event_type, data)
            return
        await self._plugin.ctx.dispatch_proactive_message(
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
    _plugin_dependencies = ["httpx>=0.24.0", "playwright>=1.57.0"]
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
        """Import legacy monitor state, moving credentials to env references.

        The migration is deliberately all-or-nothing: a marker is written only
        after every existing candidate parsed successfully.  Legacy files are
        retained for user inspection, but credential fields are rewritten in
        place so a successful migration does not leave another plaintext copy.
        """
        store = self.get_data_store()
        if int(store.get("_legacy_migration_version", 0) or 0) >= 1:
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
        parsed_sources: list[tuple[Path, dict[str, Any]]] = []
        try:
            for path in existing:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError(f"旧配置根值不是对象: {path}")
                parsed_sources.append((path, raw))
                payload = self._legacy_payload(raw)
                if payload:
                    merged.update(payload)
                    logger.info("发现旧 GitHub 监控数据: %s", path)
        except Exception:
            # Leave the marker untouched so the next load can retry after repair.
            logger.exception("迁移旧 GitHub 监控数据失败")
            return

        # Remove credentials from every source before acknowledging migration.
        # A failed rewrite leaves the marker untouched, so the next load can
        # retry instead of claiming that a plaintext source was handled.
        merged = self._sanitize_payload(merged)
        try:
            from sirius_pulse.config.file_io import atomic_json_save

            for path, raw in parsed_sources:
                sanitized = self._sanitize_payload_tree(raw)
                atomic_json_save(path, sanitized)
        except Exception:
            logger.warning("清理旧 GitHub 凭据失败，迁移将在下次加载重试", exc_info=True)
            return

        current = store.all()
        updates = {key: value for key, value in merged.items() if key not in current}
        # The marker is deliberately part of the final atomic store update.
        updates["_legacy_migration_version"] = 1
        update = getattr(store, "update", None)
        if callable(update):
            update(updates)
        else:
            for key, value in updates.items():
                store.set(key, value)

    @staticmethod
    def _environment_name(kind: str, owner: str = "", repo: str = "") -> str:
        suffix = re.sub(r"[^A-Za-z0-9]+", "_", f"{owner}_{repo}").strip("_").upper()
        if suffix:
            # Include a stable digest so punctuation variants cannot make two
            # repositories share one secret variable accidentally.
            identity = f"{owner}/{repo}".encode("utf-8", errors="ignore")
            suffix = f"{suffix}_{hashlib.sha256(identity).hexdigest()[:10].upper()}"
        return f"SIRIUS_GITHUB_{kind.upper()}_{suffix}" if suffix else f"SIRIUS_GITHUB_{kind.upper()}"

    @classmethod
    def _sanitize_repo(cls, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        result = dict(value)
        owner = str(result.get("owner", "")).strip()
        repo = str(result.get("repo", "")).strip()
        token = str(result.get("github_token", "") or "").strip()
        token_env = str(result.get("github_token_env", "") or "").strip()
        if token and token not in _MASKED_SECRETS:
            token_env = (
                token_env
                if _ENV_NAME_PATTERN.fullmatch(token_env)
                else cls._environment_name("TOKEN", owner, repo)
            )
            # Never copy a legacy credential into os.environ: the worker
            # environment can be inspected by diagnostics or child processes.
            # Operators must provision the generated variable out-of-band.
        if "github_token" in result:
            result["github_token"] = ""
        if token_env and _ENV_NAME_PATTERN.fullmatch(token_env):
            result["github_token_env"] = token_env
        elif "github_token_env" in result:
            result.pop("github_token_env", None)
        return result

    @classmethod
    def _sanitize_payload(cls, payload: dict[str, Any]) -> dict[str, Any]:
        result = dict(payload)
        nested_config = result.get("config")
        if isinstance(nested_config, dict):
            result["config"] = cls._sanitize_payload(nested_config)
        repos = result.get("repos")
        if isinstance(repos, list):
            result["repos"] = [cls._sanitize_repo(item) for item in repos]
        secret = str(result.get("webhook_secret", "") or "").strip()
        secret_env = str(result.get("webhook_secret_env", "") or "").strip()
        if secret and secret not in _MASKED_SECRETS:
            secret_env = (
                secret_env
                if _ENV_NAME_PATTERN.fullmatch(secret_env)
                else cls._environment_name("WEBHOOK_SECRET")
            )
            # Do not place a legacy secret in the worker environment.  The
            # generated reference is only useful after the operator provisions
            # it in the process supervisor/secret manager.
            result["webhook_secret"] = ""
        elif "webhook_secret" in result:
            result["webhook_secret"] = ""
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
        result = {str(key): cls._sanitize_payload_tree(item) for key, item in value.items()}
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
                nested.update({key: value for key, value in direct.items() if key != "config"})
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
