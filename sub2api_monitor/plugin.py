"""Multi-source Sub2API subscription and group-rate monitor plugin."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import time
from pathlib import Path
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
    redact,
    redact_runtime_secrets,
)
from .sources import (
    SourceConfig,
    parse_sources,
    parse_sources_partial,
    source_by_selector,
)
from .visual import (
    prune_artifacts,
    render_change_card,
    render_rates_card,
    render_subscriptions_card,
    validated_artifact_image,
)
from sirius_pulse.tools.builtin._internal._markdown_image import to_image_reference


class Sub2APIMonitorPlugin(PluginBase):
    """Poll independently authenticated Sub2API sources and report changes."""

    _MAX_CONCURRENT_SOURCES = 4
    _MAX_DETAILED_EVENTS = 20
    _MAX_DISPATCHES_PER_POLL = 200
    _MAX_NOTIFY_GROUPS = 128
    _MAX_EFFECTIVE_NOTIFY_GROUPS = 256
    _MAX_RESULT_ERRORS = 100
    _MAX_ACK_EVENTS = 4096
    _MAX_SOURCE_STATES = 64
    _MAX_SOURCE_STATE_BYTES = 12 * 1024 * 1024
    _MAX_SOURCE_STATES_BYTES = 64 * 1024 * 1024
    _CLIENT_CLOSE_TIMEOUT_SECONDS = 5.0
    _DISPATCH_TIMEOUT_SECONDS = 10.0

    _plugin_name = "sub2api_monitor"
    _plugin_display_name = "Sub2API 多站监控"
    _plugin_description = "监控多个 Sub2API 站点的订阅与分组倍率，并生成可视化变化图。"
    _plugin_version = "0.3.0"
    _plugin_author = "Sirius Pulse"
    _plugin_min_framework_version = "1.3.0"
    _plugin_dependencies = ["httpx>=0.24.0", "playwright>=1.57.0"]
    _plugin_permissions = {
        "developer_only": True,
        "hidden_from_intent": True,
        "rate_limit": {"calls_per_minute": 10, "calls_per_hour": 60},
    }
    _plugin_parameters = [
        {
            "name": "sources",
            "type": "object_array",
            "description": "Sub2API 站点列表；每个 ID 对应独立环境变量、快照和 ACK。",
            "default": [],
            "group": "多站点",
            "fields": [
                {
                    "name": "id",
                    "type": "str",
                    "required": True,
                    "identity": True,
                    "description": "小写字母开头，只含小写字母、数字和下划线",
                },
                {
                    "name": "display_name",
                    "type": "str",
                    "description": "通知与图表显示名称；留空使用 ID",
                },
                {"name": "enabled", "type": "bool", "default": True},
                {"name": "base_url", "type": "str", "required": True},
                {"name": "api_base_path", "type": "str", "default": "/api/v1"},
                {"name": "login_path", "type": "str", "default": "/auth/login"},
                {
                    "name": "refresh_path",
                    "type": "str",
                    "default": "/auth/refresh",
                },
                {"name": "logout_path", "type": "str", "default": "/auth/logout"},
                {
                    "name": "subscriptions_path",
                    "type": "str",
                    "required": True,
                    "description": "订阅接口路径，无内置默认端点",
                },
                {
                    "name": "group_rates_path",
                    "type": "str",
                    "required": True,
                    "description": "分组倍率接口路径，无内置默认端点",
                },
                {"name": "timezone", "type": "str", "default": "Asia/Shanghai"},
                {
                    "name": "timeout",
                    "type": "float",
                    "default": 20,
                    "minimum": 1,
                    "maximum": 300,
                },
                {"name": "allow_insecure_http", "type": "bool", "default": False},
                {
                    "name": "inherit_notify_group_ids",
                    "type": "bool",
                    "default": True,
                    "description": "同时继承全局通知群允许列表",
                },
                {
                    "name": "notify_group_ids",
                    "type": "list",
                    "default": [],
                    "description": "站点专属通知群；与继承的全局列表合并",
                },
                {
                    "name": "email",
                    "type": "str",
                    "default": "",
                    "description": "登录账号（邮箱）；留空时回退到环境变量",
                },
                {
                    "name": "password",
                    "type": "password",
                    "default": "",
                    "persist_secret": True,
                    "description": "登录密码；留空时回退到环境变量",
                },
            ],
        },
        {
            "name": "poll_seconds",
            "type": "int",
            "description": "所有站点的轮询间隔（秒）。",
            "default": 300,
            "minimum": 30,
            "maximum": 86400,
            "group": "监控",
        },
        {
            "name": "notify_group_ids",
            "type": "list",
            "description": "全局通知群允许列表；站点可单独覆盖。",
            "default": [],
            "group": "监控",
        },
        {
            "name": "adapter_type",
            "type": "str",
            "description": "主动通知平台；留空由引擎选择。",
            "default": "napcat",
            "group": "监控",
        },
        {
            "name": "run_on_persona",
            "type": "str",
            "description": "唯一负责全部站点轮询的 Persona；留空禁用后台和手动轮询。",
            "default": "",
            "group": "监控",
        },
        {
            "name": "visual_report_enabled",
            "type": "bool",
            "description": "使用 Playwright 为查询与变化通知生成本地可视化图；失败时降级为文字。",
            "default": True,
            "group": "可视化",
        },
        # Legacy single-source fields remain declared so existing installations
        # can migrate through the WebUI without their configuration being dropped.
        {"name": "base_url", "type": "str", "default": "", "group": "旧版兼容"},
        {
            "name": "api_base_path",
            "type": "str",
            "default": "/api/v1",
            "group": "旧版兼容",
        },
        {
            "name": "login_path",
            "type": "str",
            "default": "/auth/login",
            "group": "旧版兼容",
        },
        {
            "name": "refresh_path",
            "type": "str",
            "default": "/auth/refresh",
            "group": "旧版兼容",
        },
        {
            "name": "logout_path",
            "type": "str",
            "default": "/auth/logout",
            "group": "旧版兼容",
        },
        {
            "name": "subscriptions_path",
            "type": "str",
            "default": "",
            "group": "旧版兼容",
        },
        {
            "name": "group_rates_path",
            "type": "str",
            "default": "",
            "group": "旧版兼容",
        },
        {
            "name": "timezone",
            "type": "str",
            "default": "Asia/Shanghai",
            "group": "旧版兼容",
        },
        {
            "name": "timeout",
            "type": "float",
            "default": 20,
            "minimum": 1,
            "maximum": 300,
            "group": "旧版兼容",
        },
        {
            "name": "allow_insecure_http",
            "type": "bool",
            "default": False,
            "group": "旧版兼容",
        },
    ]
    _plugin_ui_schema = {
        "version": 1,
        "layout": "wide",
        "title": "Sub2API 监控矩阵",
        "description": ("集中维护多个独立站点。站点 ID 决定凭据环境变量、状态命名空间和 ACK；" "显示名称只用于通知与图表。"),
        "sections": [
            {
                "id": "sources",
                "title": "监控站点",
                "description": "按站点卡片配置连接、监控接口与通知范围；可单独停用而不删除状态。",
                "parameters": ["sources"],
                "columns": 1,
                "collapsed": False,
                "tone": "accent",
            },
            {
                "id": "runtime",
                "title": "轮询与主动通知",
                "description": "选择唯一运行 Persona、轮询节奏、通知平台和全局允许群。",
                "parameters": [
                    "run_on_persona",
                    "poll_seconds",
                    "adapter_type",
                    "notify_group_ids",
                ],
                "columns": 2,
                "collapsed": False,
                "tone": "default",
            },
            {
                "id": "visual",
                "title": "可视化输出",
                "description": "倍率、订阅与变化通知均以本地渲染图片输出；失败时自动降级为文字。",
                "parameters": ["visual_report_enabled"],
                "columns": 1,
                "collapsed": False,
                "tone": "default",
            },
            {
                "id": "legacy",
                "title": "旧版单站兼容",
                "description": ("仅用于迁移旧配置；推荐改用上方站点卡片。显式空站点列表会禁用全部站点，" "不会回退到这些字段。"),
                "parameters": [
                    "base_url",
                    "api_base_path",
                    "login_path",
                    "refresh_path",
                    "logout_path",
                    "subscriptions_path",
                    "group_rates_path",
                    "timezone",
                    "timeout",
                    "allow_insecure_http",
                ],
                "columns": 2,
                "collapsed": True,
                "tone": "muted",
            },
        ],
        "parameters": {
            "sources": {
                "label": "站点列表",
                "help": (
                    "每个站点可填写登录账号与密码；留空时回退到环境变量 "
                    "SUB2API_{ID}_EMAIL 与 SUB2API_{ID}_PASSWORD。"
                    "密码已保存后再次打开显示为空（留空保持不变）。"
                ),
                "span": 12,
                "add_label": "添加 Sub2API 站点",
                "item_placeholder": "未命名站点",
                "empty_title": "尚未配置监控站点",
                "empty_description": (
                    "添加第一张站点卡片后填写运行时 URL 和两个监控接口路径；" "框架不会内置真实站点或监控端点。"
                ),
                "item_title_field": "display_name",
                "item_fallback_field": "id",
                "item_subtitle_field": "base_url",
                "item_badge_field": "id",
                "item_status_field": "enabled",
                "fields": {
                    "enabled": {
                        "label": "站点状态",
                        "help": "停用后保留快照与 ACK，但不参与常规轮询。",
                        "widget": "switch",
                        "true_label": "监控中",
                        "false_label": "已停用",
                        "span": 4,
                    },
                    "id": {
                        "label": "站点 ID",
                        "help": "稳定唯一；小写字母开头，只含小写字母、数字和下划线。",
                        "placeholder": "例如：primary",
                        "widget": "code",
                        "span": 4,
                    },
                    "display_name": {
                        "label": "显示名称",
                        "help": "用于通知与图表，可包含空格，也可与其他站点重名。",
                        "placeholder": "例如：主站",
                        "widget": "text",
                        "span": 4,
                    },
                    "base_url": {
                        "label": "站点地址",
                        "help": "只填写 origin；禁止携带查询、片段、用户信息或凭据。",
                        "placeholder": "https://your-sub2api-host.invalid",
                        "widget": "url",
                        "span": 12,
                    },
                    "api_base_path": {
                        "label": "API 基础路径",
                        "placeholder": "/api/v1",
                        "widget": "path",
                        "span": 6,
                    },
                    "login_path": {
                        "label": "登录路径",
                        "placeholder": "/auth/login",
                        "widget": "path",
                        "span": 6,
                    },
                    "refresh_path": {
                        "label": "刷新路径",
                        "placeholder": "/auth/refresh",
                        "widget": "path",
                        "span": 6,
                    },
                    "logout_path": {
                        "label": "注销路径",
                        "placeholder": "/auth/logout",
                        "widget": "path",
                        "span": 6,
                    },
                    "subscriptions_path": {
                        "label": "订阅监控路径",
                        "help": "必填；没有内置真实端点。",
                        "placeholder": "/your/subscriptions-path",
                        "widget": "path",
                        "span": 6,
                    },
                    "group_rates_path": {
                        "label": "倍率监控路径",
                        "help": "必填；没有内置真实端点。",
                        "placeholder": "/your/group-rates-path",
                        "widget": "path",
                        "span": 6,
                    },
                    "timezone": {
                        "label": "站点时区",
                        "placeholder": "Asia/Shanghai",
                        "widget": "code",
                        "span": 4,
                    },
                    "timeout": {
                        "label": "请求超时",
                        "help": "允许范围 1–300 秒。",
                        "unit": "秒",
                        "span": 4,
                    },
                    "allow_insecure_http": {
                        "label": "允许 HTTP",
                        "help": "仅用于 localhost、127.0.0.1 或 ::1 本机调试。",
                        "widget": "switch",
                        "true_label": "已允许",
                        "false_label": "仅 HTTPS",
                        "span": 4,
                    },
                    "inherit_notify_group_ids": {
                        "label": "继承全局群列表",
                        "help": "启用后与该站专属群列表合并。",
                        "widget": "switch",
                        "true_label": "继承",
                        "false_label": "不继承",
                        "span": 4,
                    },
                    "notify_group_ids": {
                        "label": "站点专属通知群",
                        "help": "只向明确列出的群主动通知。",
                        "add_label": "添加群号",
                        "item_placeholder": "QQ群号",
                        "span": 8,
                    },
                    "email": {
                        "label": "登录账号",
                        "help": "留空时回退到环境变量。",
                        "placeholder": "account@example.com",
                        "span": 6,
                    },
                    "password": {
                        "label": "登录密码",
                        "help": "留空时回退到环境变量；已保存后留空保持不变。",
                        "placeholder": "已保存（留空保持不变）",
                        "span": 6,
                    },
                },
                "fieldsets": [
                    {
                        "id": "identity",
                        "title": "身份与入口",
                        "description": "稳定 ID、显示名称和站点 origin",
                        "fields": ["enabled", "id", "display_name", "base_url"],
                        "collapsed": False,
                    },
                    {
                        "id": "session",
                        "title": "会话接口",
                        "description": "认证会话路径；均相对同一站点 origin",
                        "fields": [
                            "api_base_path",
                            "login_path",
                            "refresh_path",
                            "logout_path",
                        ],
                        "collapsed": True,
                    },
                    {
                        "id": "credentials",
                        "title": "登录凭据",
                        "description": "可直接填写；留空回退到环境变量 SUB2API_{ID}_EMAIL / SUB2API_{ID}_PASSWORD",
                        "fields": ["email", "password"],
                        "collapsed": False,
                    },
                    {
                        "id": "monitoring",
                        "title": "监控接口与网络",
                        "description": "监控路径由部署者填写，插件不提供真实端点",
                        "fields": [
                            "subscriptions_path",
                            "group_rates_path",
                            "timezone",
                            "timeout",
                            "allow_insecure_http",
                        ],
                        "collapsed": False,
                    },
                    {
                        "id": "notifications",
                        "title": "通知范围",
                        "description": "全局允许列表与站点专属群的组合规则",
                        "fields": ["inherit_notify_group_ids", "notify_group_ids"],
                        "collapsed": False,
                    },
                ],
            },
            "poll_seconds": {
                "label": "轮询间隔",
                "help": "全部启用站点共享；允许范围 30–86400 秒。",
                "unit": "秒",
                "span": 6,
            },
            "notify_group_ids": {
                "label": "全局通知群允许列表",
                "help": "只有站点选择继承时才会合并；空列表不会自动广播。",
                "add_label": "添加群号",
                "item_placeholder": "QQ群号",
                "span": 6,
            },
            "adapter_type": {
                "label": "主动通知平台",
                "help": "留空时由引擎选择；NapCat 部署通常填写 napcat。",
                "placeholder": "napcat",
                "widget": "code",
                "span": 6,
            },
            "run_on_persona": {
                "label": "运行 Persona",
                "help": "必须精确匹配唯一 Persona；留空会禁用后台轮询和手动轮询。",
                "placeholder": "例如：main",
                "widget": "text",
                "span": 6,
            },
            "visual_report_enabled": {
                "label": "图片可视化",
                "help": "倍率/订阅查询与变化通知以本地渲染 PNG 输出；渲染失败时仍发送权威文字结果。",
                "widget": "switch",
                "true_label": "生成图片",
                "false_label": "仅文字",
                "span": 12,
            },
            "base_url": {
                "label": "旧版站点地址",
                "placeholder": "https://your-sub2api-host.invalid",
                "widget": "url",
                "span": 6,
            },
            "api_base_path": {
                "label": "旧版 API 基础路径",
                "placeholder": "/api/v1",
                "widget": "path",
                "span": 6,
            },
            "login_path": {
                "label": "旧版登录路径",
                "placeholder": "/auth/login",
                "widget": "path",
                "span": 6,
            },
            "refresh_path": {
                "label": "旧版刷新路径",
                "placeholder": "/auth/refresh",
                "widget": "path",
                "span": 6,
            },
            "logout_path": {
                "label": "旧版注销路径",
                "placeholder": "/auth/logout",
                "widget": "path",
                "span": 6,
            },
            "subscriptions_path": {
                "label": "旧版订阅监控路径",
                "help": "没有内置真实端点。",
                "placeholder": "/your/subscriptions-path",
                "widget": "path",
                "span": 6,
            },
            "group_rates_path": {
                "label": "旧版倍率监控路径",
                "help": "没有内置真实端点。",
                "placeholder": "/your/group-rates-path",
                "widget": "path",
                "span": 6,
            },
            "timezone": {
                "label": "旧版时区",
                "placeholder": "Asia/Shanghai",
                "widget": "code",
                "span": 6,
            },
            "timeout": {
                "label": "旧版请求超时",
                "unit": "秒",
                "span": 6,
            },
            "allow_insecure_http": {
                "label": "旧版允许 HTTP",
                "help": "仅限明确了解风险的内网测试环境。",
                "widget": "switch",
                "true_label": "已允许",
                "false_label": "仅 HTTPS",
                "span": 12,
            },
        },
    }

    def __init__(self) -> None:
        super().__init__()
        self._poll_lock = asyncio.Lock()
        self._clients: dict[str, Sub2APIClient] = {}
        self._client_fingerprints: dict[str, str] = {}
        self._remaining_visual_renders = 0
        self._remaining_dispatches_by_collection: dict[tuple[str, str], int] = {}
        # Compatibility/test injection for a legacy singleton.
        self._client: Any = None
        self._client_fingerprint = ""

    @command(
        "sub2api",
        prefix="/",
        patterns=["sub2api", "sub2api_monitor"],
        render_mode="direct",
        description="查看、轮询或可视化输出 Sub2API 多站点监控数据。",
        hidden_from_intent=True,
        examples=[
            "/sub2api status",
            "/sub2api poll all",
            "/sub2api subscriptions alpha",
            "/sub2api rates alpha",
        ],
    )
    def sub2api_command(self) -> PluginResponse:
        return PluginResponse.ok(text="请使用 /sub2api status 查看多站点监控状态。")

    def create_background_tasks(self) -> list[BackgroundTaskSpec]:
        if not self._is_designated_persona() or not self._configuration_ready():
            return []
        return [BackgroundTaskSpec("poll", self._poll_seconds(), self._poll_background)]

    async def on_unload(self) -> None:
        async with self._poll_lock:
            clients: list[Any] = list(self._clients.values())
            if self._client is not None and self._client not in clients:
                clients.append(self._client)
            self._clients.clear()
            self._client_fingerprints.clear()
            self._client = None
            self._client_fingerprint = ""
            outcomes = await asyncio.gather(
                *(self._close_client(client) for client in clients),
                return_exceptions=True,
            )
            for outcome in outcomes:
                if isinstance(outcome, BaseException):
                    self.logger.warning(
                        "关闭 Sub2API 客户端失败：%s",
                        self._safe_error(outcome),
                    )
            await prune_artifacts(self._artifact_dir())

    async def _poll_background(self) -> None:
        try:
            result = await self.poll_once(notify=True)
            if result.errors:
                self.logger.warning("Sub2API 多站监控部分失败：%s", "；".join(result.errors))
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("Sub2API 多站监控轮询失败：%s", self._safe_error(exc))

    async def execute_async(self, cmd: CommandAST) -> list[PluginResponse]:
        try:
            if cmd.command.casefold() not in {"sub2api", "sub2api_monitor"}:
                return [PluginResponse.fail("未识别的 Sub2API 命令")]
            action = self._arg(cmd, 0, "status").strip().casefold()
            selector = " ".join(str(arg.value) for arg in cmd.args[1:]).strip()
            stateful_actions = {
                "status",
                "状态",
                "",
                "poll",
                "check",
                "检查",
                "轮询",
                "reset",
                "重置",
                "subscriptions",
                "plans",
                "订阅",
                "rates",
                "groups",
                "倍率",
                "分组",
            }
            if action in stateful_actions and not self._is_designated_persona():
                return [PluginResponse.fail("当前人格不是 Sub2API 监控执行者")]
            if action in {"status", "状态", ""}:
                return [PluginResponse.ok(text=self._status_text(selector))]
            if action in {"poll", "check", "检查", "轮询"}:
                if not self._is_designated_persona():
                    return [PluginResponse.fail("当前人格未被配置为 Sub2API 轮询执行者")]
                result = await self.poll_once(notify=True, selector=selector)
                return [PluginResponse.ok(text=self._poll_text(result))]
            if action in {"subscriptions", "plans", "订阅"}:
                source, data = await self._fetch_one("subscriptions", selector)
                return [
                    await self._send_board_image(
                        source,
                        records=data,
                        kind="subscription",
                        fallback_title=f"{source.display_name} 当前订阅",
                    )
                ]
            if action in {"rates", "groups", "倍率", "分组"}:
                source, data = await self._fetch_one("group_rates", selector)
                return [
                    await self._send_board_image(
                        source,
                        records=data,
                        kind="rate",
                        fallback_title=f"{source.display_name} 当前分组倍率",
                    )
                ]
            if action in {"reset", "重置"}:
                await self._reset_sources(selector)
                target = selector or "all"
                return [
                    PluginResponse.ok(
                        text=(f"Sub2API 站点 {target} 的监控快照已重置；" "下一次轮询静默初始化。")
                    )
                ]
            return [
                PluginResponse.fail(
                    "用法：/sub2api status|poll [id|all]|subscriptions <id>|"
                    "rates <id>|reset [id|all]"
                )
            ]
        except Exception as exc:  # noqa: BLE001
            safe_error = self._safe_error(exc)
            self.logger.warning("Sub2API 多站监控命令失败：%s", safe_error)
            return [PluginResponse.fail(f"Sub2API 监控失败：{safe_error}")]

    async def poll_once(
        self,
        *,
        notify: bool = True,
        selector: str = "",
    ) -> PollResult:
        """Poll enabled sources independently and aggregate observable counters."""
        async with self._poll_lock:
            sources, config_errors = self._validate_config_partial()
            await self._reconcile_clients(sources)
            selected = source_by_selector(sources, selector, require_one=False)
            result = PollResult(errors=list(config_errors))
            self._remaining_visual_renders = 12 if self._visual_report_enabled() else 0
            per_collection_budget = max(
                1,
                self._MAX_DISPATCHES_PER_POLL // max(1, len(selected) * 2),
            )
            self._remaining_dispatches_by_collection = {
                (source.id, collection): per_collection_budget
                for source in selected
                for collection in ("subscriptions", "group_rates")
            }
            store = self.get_data_store()
            load_error = getattr(store, "load_error", None)
            if load_error:
                result.errors.append(str(load_error))
                return result

            self._migrate_legacy_state_if_unambiguous(sources)
            attempt_at = int(time.time())
            if not selected:
                message = "没有可轮询的启用站点"
                result.errors.append(message)
                store.update(
                    {
                        "last_poll_attempt_at": attempt_at,
                        "last_poll_error": message,
                    }
                )
                return result
            store.update({"last_poll_attempt_at": attempt_at, "last_poll_error": ""})
            semaphore = asyncio.Semaphore(self._MAX_CONCURRENT_SOURCES)

            async def poll_source(source: SourceConfig) -> PollResult:
                local_result = PollResult()
                async with semaphore:
                    timeout = min(330.0, max(15.0, source.timeout + 30.0))
                    try:
                        async with asyncio.timeout(timeout):
                            await self._poll_source(
                                source,
                                local_result,
                                notify=notify,
                                attempt_at=attempt_at,
                            )
                    except asyncio.CancelledError:
                        raise
                    except TimeoutError:
                        message = f"{source.display_name}：站点轮询超过 {timeout:g} 秒"
                        local_result.errors.append(message)
                        state = self._source_state(source)
                        state["last_poll_error"] = message
                        self._save_source_state(source, state)
                    except Exception as exc:  # noqa: BLE001
                        message = f"{source.display_name}：{self._safe_error(exc)}"
                        local_result.errors.append(message)
                        state = self._source_state(source)
                        state["last_poll_error"] = message
                        self._save_source_state(source, state)
                return local_result

            partial_results = await asyncio.gather(
                *(poll_source(source) for source in selected)
            )
            for partial in partial_results:
                self._merge_poll_result(result, partial)

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

    @classmethod
    def _merge_poll_result(cls, target: PollResult, partial: PollResult) -> None:
        for field_name in (
            "subscription_added",
            "subscription_removed",
            "subscription_changed",
            "rates_added",
            "rates_removed",
            "rates_changed",
            "notifications_sent",
        ):
            setattr(
                target,
                field_name,
                int(getattr(target, field_name)) + int(getattr(partial, field_name)),
            )
        target.initialized.extend(partial.initialized)
        remaining = max(0, cls._MAX_RESULT_ERRORS - len(target.errors))
        target.errors.extend(partial.errors[:remaining])
        if (
            len(partial.errors) > remaining
            and len(target.errors) < cls._MAX_RESULT_ERRORS + 1
        ):
            target.errors.append("更多错误已截断")

    async def _poll_source(
        self,
        source: SourceConfig,
        result: PollResult,
        *,
        notify: bool,
        attempt_at: int,
    ) -> None:
        state = self._source_state(source)
        state["last_poll_attempt_at"] = attempt_at
        state["last_poll_error"] = ""
        self._save_source_state(source, state)
        email, password = source.credentials()
        injected_legacy_client = (
            source.legacy
            and self._client is not None
            and not isinstance(self._client, Sub2APIClient)
        )
        if (not email or not password) and not injected_legacy_client:
            message = f"{source.display_name}：缺少环境变量 {source.email_env} 或 {source.password_env}"
            result.errors.append(message)
            state["last_poll_error"] = message
            self._save_source_state(source, state)
            return

        try:
            client = await self._get_client(source)
        except Exception as exc:  # noqa: BLE001
            message = f"{source.display_name}：{self._safe_error(exc)}"
            result.errors.append(message)
            state["last_poll_error"] = message
            self._save_source_state(source, state)
            return

        values: tuple[Any, Any] = await asyncio.gather(
            client.fetch_subscriptions(),
            client.fetch_group_rates(),
            return_exceptions=True,
        )
        source_errors_before = len(result.errors)
        await self._process_poll_value(
            source,
            "subscriptions",
            values[0],
            result,
            notify=notify,
        )
        await self._process_poll_value(
            source,
            "group_rates",
            values[1],
            result,
            notify=notify,
        )
        state = self._source_state(source)
        if len(result.errors) == source_errors_before:
            success_at = int(time.time())
            state.update(
                {
                    "last_poll_at": success_at,
                    "last_poll_success_at": success_at,
                    "last_poll_error": "",
                }
            )
        else:
            source_errors = result.errors[source_errors_before:]
            state["last_poll_error"] = ";".join(source_errors)[:1000]
        self._save_source_state(source, state)

    async def _process_poll_value(
        self,
        source: SourceConfig,
        name: str,
        value: list[dict[str, Any]] | BaseException,
        result: PollResult,
        *,
        notify: bool,
    ) -> None:
        if isinstance(value, asyncio.CancelledError):
            raise value
        source_prefix = "" if source.legacy else f"{source.display_name} "
        if isinstance(value, BaseException):
            label = "订阅接口" if name == "subscriptions" else "分组倍率接口"
            result.errors.append(f"{source_prefix}{label}：{self._safe_error(value)}")
            return
        try:
            sanitized = redact_runtime_secrets(redact(value), source.credentials())
            if not isinstance(sanitized, list) or not all(
                isinstance(item, dict) for item in sanitized
            ):
                raise Sub2APIError("规范化结果不是有效记录列表")
            await self._process_collection(
                source,
                name,
                sanitized,
                result,
                notify=notify,
            )
        except Exception as exc:  # noqa: BLE001
            label = "订阅通知" if name == "subscriptions" else "分组倍率通知"
            result.errors.append(f"{source_prefix}{label}：{self._safe_error(exc)}")

    async def _process_collection(
        self,
        source: SourceConfig,
        name: str,
        current: list[dict[str, Any]],
        result: PollResult,
        *,
        notify: bool,
    ) -> None:
        state = self._source_state(source)
        source_fingerprint = self._source_fingerprint(name, source)
        stored_source = state.get(f"{name}_source")
        old_raw = state.get(f"{name}_snapshot")
        old = (
            old_raw
            if isinstance(old_raw, list) and stored_source == source_fingerprint
            else None
        )
        ack_state = self._load_notification_acks_from_state(state)
        cursor_state = self._load_notification_cursors_from_state(state)
        if old is None:
            self._discard_collection_acks(ack_state, name)
            self._discard_collection_cursors(cursor_state, name)
            state.update(
                {
                    f"{name}_snapshot": current,
                    f"{name}_source": source_fingerprint,
                    "notification_acks": ack_state,
                    "notification_cursors": cursor_state,
                }
            )
            self._save_source_state(source, state)
            result.initialized.append(name if source.legacy else f"{source.id}:{name}")
            return

        ignored_keys = (
            {"group_name", "name", "platform", "slug"}
            if name == "group_rates"
            else None
        )
        added, removed, changed = diff_records(old, current, ignored_keys=ignored_keys)
        events = self._collection_events(name, added, removed, changed)
        self._add_change_counts(name, added, removed, changed, result)
        if not events:
            # A partially delivered transition may temporarily disappear when
            # upstream data reverts to the committed snapshot.  Keep its ACK
            # and cursor ledger so a later recurrence resumes rather than
            # duplicating already confirmed groups.
            state.update(
                {
                    f"{name}_snapshot": current,
                    f"{name}_source": source_fingerprint,
                    "notification_acks": ack_state,
                    "notification_cursors": cursor_state,
                }
            )
            self._save_source_state(source, state)
            return
        groups = self._notify_groups(source)
        if notify and events and not groups:
            result.errors.append(f"{source.display_name} 未配置通知群，变化快照暂不提交")
            return

        collection_budget = self._remaining_dispatches_by_collection.get(
            (source.id, name),
            self._MAX_DISPATCHES_PER_POLL,
        )
        should_summarize = len(events) > self._MAX_DETAILED_EVENTS or (
            bool(events) and len(events) * max(1, len(groups)) > collection_budget
        )
        sent = 0
        notification_errors: list[str] = []
        if notify and should_summarize:
            event_key = self._summary_event_key(
                source,
                name,
                source_fingerprint,
                old,
                current,
            )
            sent, failures = await self._notify_summary(
                source,
                name,
                added=len(added),
                removed=len(removed),
                changed=len(changed),
                event_key=event_key,
                ack_state=ack_state,
                cursor_state=cursor_state,
            )
            if failures:
                notification_errors.append(f"批量变化未确认群组：{', '.join(failures)}")
        elif notify:
            for event_type, before, after in events:
                event_key = self._notification_event_key(
                    name,
                    source_fingerprint,
                    event_type,
                    before,
                    after,
                    source_id=source.id,
                )
                self._merge_legacy_event_acks(
                    state,
                    ack_state,
                    name=name,
                    event_type=event_type,
                    before=before,
                    after=after,
                    new_event_key=event_key,
                )
                event_sent, failures = await self._notify_change(
                    source,
                    name,
                    event_type,
                    before,
                    after,
                    event_key,
                    ack_state,
                    cursor_state,
                )
                sent += event_sent
                if failures:
                    notification_errors.append(
                        f"{event_type} 未确认群组：{', '.join(failures)}"
                    )
        if notification_errors:
            state["notification_acks"] = ack_state
            state["notification_cursors"] = cursor_state
            self._save_source_state(source, state)
            remaining = max(0, self._MAX_RESULT_ERRORS - len(result.errors))
            result.errors.extend(
                f"{source.display_name} {message}"
                for message in notification_errors[:remaining]
            )
            result.notifications_sent += sent
            return

        self._discard_collection_acks(ack_state, name)
        self._discard_collection_cursors(cursor_state, name)
        state.update(
            {
                f"{name}_snapshot": current,
                f"{name}_source": source_fingerprint,
                "notification_acks": ack_state,
                "notification_cursors": cursor_state,
            }
        )
        self._save_source_state(source, state)
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
        *,
        source_id: str = "default",
    ) -> str:
        ignored_keys = (
            {"group_name", "name", "platform", "slug"}
            if name == "group_rates"
            else None
        )
        fingerprint = canonical_record(
            {
                "source_id": source_id,
                "name": name,
                "source": source,
                "event_type": event_type,
                "before": comparison_record(before or {}, ignored_keys=ignored_keys),
                "after": comparison_record(after or {}, ignored_keys=ignored_keys),
            }
        )
        digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:24]
        return f"{name}:{source_id}:{source}:{event_type}:{digest}"

    @staticmethod
    def _legacy_notification_event_key(
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
    def _load_notification_acks_from_state(
        state: dict[str, Any]
    ) -> dict[str, list[str]]:
        raw = state.get("notification_acks", {})
        if not isinstance(raw, dict):
            return {}
        result: dict[str, list[str]] = {}
        max_events = Sub2APIMonitorPlugin._MAX_ACK_EVENTS
        items = list(raw.items())[-max_events:]
        for event_key, group_ids in items:
            if not isinstance(event_key, str) or not isinstance(group_ids, list):
                continue
            max_groups = Sub2APIMonitorPlugin._MAX_EFFECTIVE_NOTIFY_GROUPS
            capped_group_ids = group_ids[:max_groups]
            cleaned = list(
                dict.fromkeys(
                    str(group_id).strip()
                    for group_id in capped_group_ids
                    if str(group_id).strip()
                )
            )
            if cleaned:
                result[event_key] = cleaned
        return result

    @staticmethod
    def _load_notification_acks(store: Any) -> dict[str, list[str]]:
        return Sub2APIMonitorPlugin._load_notification_acks_from_state(store.all())

    @staticmethod
    def _load_notification_cursors_from_state(
        state: dict[str, Any],
    ) -> dict[str, int]:
        raw = state.get("notification_cursors", {})
        if not isinstance(raw, dict):
            return {}
        result: dict[str, int] = {}
        max_events = Sub2APIMonitorPlugin._MAX_ACK_EVENTS
        items = list(raw.items())[-max_events:]
        for event_key, value in items:
            if not isinstance(event_key, str) or isinstance(value, bool):
                continue
            try:
                parsed = int(value)
            except (TypeError, ValueError, OverflowError):
                continue
            if parsed >= 0:
                result[event_key] = parsed
        return result

    @staticmethod
    def _discard_collection_acks(ack_state: dict[str, list[str]], name: str) -> None:
        prefix = f"{name}:"
        for event_key in list(ack_state):
            if event_key.startswith(prefix):
                del ack_state[event_key]

    @staticmethod
    def _discard_collection_cursors(cursor_state: dict[str, int], name: str) -> None:
        prefix = f"{name}:"
        for event_key in list(cursor_state):
            if event_key.startswith(prefix):
                del cursor_state[event_key]

    def _merge_legacy_event_acks(
        self,
        state: dict[str, Any],
        ack_state: dict[str, list[str]],
        *,
        name: str,
        event_type: str,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
        new_event_key: str,
    ) -> None:
        raw_sources = state.get("legacy_source_fingerprints", {})
        if not isinstance(raw_sources, dict):
            return
        legacy_source = raw_sources.get(name)
        if not isinstance(legacy_source, str) or not legacy_source:
            return
        legacy_key = self._legacy_notification_event_key(
            name, legacy_source, event_type, before, after
        )
        legacy_groups = ack_state.pop(legacy_key, [])
        if legacy_groups:
            ack_state[new_event_key] = list(
                dict.fromkeys([*ack_state.get(new_event_key, []), *legacy_groups])
            )

    @staticmethod
    def _summary_event_key(
        source: SourceConfig,
        name: str,
        source_fingerprint: str,
        before: list[dict[str, Any]],
        after: list[dict[str, Any]],
    ) -> str:
        payload = canonical_record(
            {
                "source_id": source.id,
                "name": name,
                "source": source_fingerprint,
                "before": before,
                "after": after,
            }
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
        return f"{name}:{source.id}:{source_fingerprint}:summary:{digest}"

    async def _notify_summary(
        self,
        source: SourceConfig,
        name: str,
        *,
        added: int,
        removed: int,
        changed: int,
        event_key: str,
        ack_state: dict[str, list[str]],
        cursor_state: dict[str, int],
    ) -> tuple[int, list[str]]:
        label = "订阅" if name == "subscriptions" else "分组倍率"
        text = (
            f"【{source.display_name}】{label}批量变化：新增 {added}、"
            f"移除 {removed}、更新 {changed}。为控制通知量，已合并为一条摘要。"
        )
        return await self._dispatch_notification(
            source,
            collection=name,
            text=text,
            event_key=event_key,
            ack_state=ack_state,
            cursor_state=cursor_state,
            image_path="",
        )

    async def _notify_change(
        self,
        source: SourceConfig,
        name: str,
        event_type: str,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
        event_key: str,
        ack_state: dict[str, list[str]],
        cursor_state: dict[str, int],
    ) -> tuple[int, list[str]]:
        groups = self._notify_groups(source)
        if not groups:
            return 0, []
        text = _format_change(
            event_type,
            before,
            after,
            source_name=source.display_name,
        )
        acknowledged = set(ack_state.get(event_key, []))
        pending_groups = [
            group_id for group_id in groups if group_id not in acknowledged
        ]
        image_path = ""
        artifact_dir = self._artifact_dir()
        if pending_groups and self._remaining_visual_renders > 0:
            self._remaining_visual_renders -= 1
            rendered = await render_change_card(
                source_id=source.id,
                display_name=source.display_name,
                event_type=event_type,
                before=before,
                after=after,
                artifact_dir=artifact_dir,
                occurred_at=int(time.time()),
            )
            image_path = validated_artifact_image(artifact_dir, rendered)

        return await self._dispatch_notification(
            source,
            collection=name,
            text=text,
            event_key=event_key,
            ack_state=ack_state,
            cursor_state=cursor_state,
            image_path=image_path,
        )

    async def _dispatch_notification(
        self,
        source: SourceConfig,
        *,
        collection: str,
        text: str,
        event_key: str,
        ack_state: dict[str, list[str]],
        cursor_state: dict[str, int],
        image_path: str,
    ) -> tuple[int, list[str]]:
        groups = self._notify_groups(source)
        acknowledged = set(ack_state.get(event_key, []))
        start = cursor_state.get(event_key, 0) % max(1, len(groups))
        rotated_groups = [*groups[start:], *groups[:start]]
        pending_groups = [
            group_id for group_id in rotated_groups if group_id not in acknowledged
        ]
        sent = 0
        failures: list[str] = []
        adapter_type = self._config_value(
            "adapter_type", default="napcat", allow_empty=True
        )
        budget_key = (source.id, collection)
        for pending_index, group_id in enumerate(pending_groups):
            remaining = self._remaining_dispatches_by_collection.get(
                budget_key,
                self._MAX_DISPATCHES_PER_POLL,
            )
            if remaining <= 0:
                unattempted = len(pending_groups) - pending_index
                failures.append(f"还有 {unattempted} 个群达到本轮投递安全上限")
                break
            self._remaining_dispatches_by_collection[budget_key] = remaining - 1
            cursor_state[event_key] = (groups.index(group_id) + 1) % max(1, len(groups))
            state = self._source_state(source)
            state["notification_acks"] = ack_state
            state["notification_cursors"] = cursor_state
            self._save_source_state(source, state)
            try:
                group_digest = hashlib.sha256(group_id.encode("utf-8")).hexdigest()[:12]
                async with asyncio.timeout(self._DISPATCH_TIMEOUT_SECONDS):
                    accepted = await self.ctx.dispatch_proactive_message(
                        group_id=group_id,
                        text=text,
                        adapter_type=adapter_type,
                        event_id=f"sub2api:{event_key}:{group_digest}",
                        image_path=image_path,
                    )
                if accepted is not True:
                    failures.append(f"{group_id}（投递未确认）")
                    continue
                acknowledged.add(group_id)
                ack_state[event_key] = sorted(acknowledged)
                state = self._source_state(source)
                state["notification_acks"] = ack_state
                state["notification_cursors"] = cursor_state
                self._save_source_state(source, state)
                sent += 1
            except Exception as exc:  # noqa: BLE001
                acknowledged.discard(group_id)
                if acknowledged:
                    ack_state[event_key] = sorted(acknowledged)
                else:
                    ack_state.pop(event_key, None)
                failures.append(f"{group_id}（{self._safe_error(exc)}）")
        return sent, failures

    async def _fetch_one(
        self,
        name: str,
        selector: str = "",
    ) -> tuple[SourceConfig, list[dict[str, Any]]]:
        async with self._poll_lock:
            sources, errors = self._validate_config_partial()
            if not sources and errors:
                raise Sub2APIError("；".join(errors))
            source = source_by_selector(sources, selector, require_one=True)[0]
            client = await self._get_client(source)
            if name == "subscriptions":
                return source, await client.fetch_subscriptions()
            return source, await client.fetch_group_rates()

    async def _send_board_image(
        self,
        source: SourceConfig,
        *,
        records: list[dict[str, Any]],
        kind: str,
        fallback_title: str,
    ) -> PluginResponse:
        """Render a records board and send it as a raw image via the adapter.

        Falls back to human-readable text when Playwright rendering or the
        direct adapter path is unavailable.
        """
        artifact_dir = self._artifact_dir()
        generated_at = int(time.time())
        rendered = None
        # 用户主动查询不受轮询渲染预算限制，始终尝试出图；
        # 预算仅约束后台轮询的通知渲染（见 _notify_change）。
        if kind == "subscription":
            rendered = await render_subscriptions_card(
                records,
                source_id=source.id,
                display_name=source.display_name,
                artifact_dir=artifact_dir,
                generated_at=generated_at,
            )
        else:
            rendered = await render_rates_card(
                records,
                source_id=source.id,
                display_name=source.display_name,
                artifact_dir=artifact_dir,
                generated_at=generated_at,
            )
        image_path = validated_artifact_image(artifact_dir, rendered)
        group_id = str(
            getattr(getattr(self.ctx, "message", None), "group_id", "") or ""
        )
        adapter = getattr(self.ctx, "adapter", None)
        if image_path and group_id and adapter is not None and hasattr(
            adapter, "send_group_msg"
        ):
            try:
                image_ref = to_image_reference(image_path)
                segments: list[dict[str, Any]] = [
                    {"type": "image", "data": {"file": image_ref}},
                ]
                await adapter.send_group_msg(group_id, segments)
                # 图片已直接发送；静默返回避免框架再发一条提示文字
                return PluginResponse.ok(render_mode="silent")
            except Exception as exc:  # noqa: BLE001
                self.logger.warning(
                    "Sub2API 可视化直发失败，回退文本：%s", self._safe_error(exc)
                )
        if not records:
            return PluginResponse.ok(text=f"{fallback_title}：暂无数据")
        return PluginResponse.ok(
            text=_format_records(fallback_title, records, kind=kind)
        )

    async def _reset_sources(self, selector: str) -> None:
        async with self._poll_lock:
            sources, errors = self._validate_config_partial()
            query = str(selector or "all").strip().casefold()
            selected = source_by_selector(
                sources,
                query,
                require_one=False,
                include_disabled=True,
            )
            if not selected and query != "all" and errors:
                raise Sub2APIError("；".join(errors))
            store = self.get_data_store()
            if query == "all" or any(source.legacy for source in selected):
                store.delete_many(
                    [
                        "subscriptions_snapshot",
                        "subscriptions_source",
                        "group_rates_snapshot",
                        "group_rates_source",
                        "notification_acks",
                        "notification_cursors",
                        "last_poll_attempt_at",
                        "last_poll_at",
                        "last_poll_success_at",
                        "last_poll_error",
                        "legacy_state_migration",
                    ]
                )
            states = self._all_source_states()
            if query == "all":
                states = {}
            else:
                for source in selected:
                    if not source.legacy:
                        states.pop(source.id, None)
            store.update({"source_states": states})

    @classmethod
    async def _close_client(cls, client: Any) -> None:
        async def close_client(*, logout: bool) -> None:
            close = getattr(client, "aclose", None)
            if callable(close):
                result = close(logout=logout)
                if asyncio.iscoroutine(result):
                    await result
                return
            close = getattr(client, "close", None)
            if callable(close):
                result = close()
                if asyncio.iscoroutine(result):
                    await result

        try:
            async with asyncio.timeout(cls._CLIENT_CLOSE_TIMEOUT_SECONDS):
                await close_client(logout=True)
        except TimeoutError:
            async with asyncio.timeout(cls._CLIENT_CLOSE_TIMEOUT_SECONDS):
                await close_client(logout=False)

    async def _reconcile_clients(self, sources: list[SourceConfig]) -> None:
        active = {source.id: source for source in sources if source.enabled}
        stale_ids = {
            source_id
            for source_id in self._clients
            if source_id not in active
            or self._client_fingerprints.get(source_id)
            != self._client_config_fingerprint(active[source_id])
        }
        stale_clients: list[Any] = []
        for source_id in stale_ids:
            stale_clients.append(self._clients.pop(source_id))
            self._client_fingerprints.pop(source_id, None)
        if stale_clients:
            outcomes = await asyncio.gather(
                *(self._close_client(client) for client in stale_clients),
                return_exceptions=True,
            )
            for outcome in outcomes:
                if isinstance(outcome, BaseException):
                    self.logger.warning(
                        "关闭已失效的 Sub2API 客户端失败：%s",
                        self._safe_error(outcome),
                    )

    async def _get_client(self, source: SourceConfig) -> Any:
        if (
            source.legacy
            and self._client is not None
            and not isinstance(self._client, Sub2APIClient)
        ):
            return self._client
        fingerprint = self._client_config_fingerprint(source)
        current = self._clients.get(source.id)
        if (
            current is not None
            and self._client_fingerprints.get(source.id) != fingerprint
        ):
            self._clients.pop(source.id, None)
            self._client_fingerprints.pop(source.id, None)
            await self._close_client(current)
            current = None
        if current is None:
            current = Sub2APIClient(**source.client_kwargs())
            await current.__aenter__()
            self._clients[source.id] = current
            self._client_fingerprints[source.id] = fingerprint
        if source.legacy:
            self._client = current
            self._client_fingerprint = fingerprint
        return current

    def _client_config_fingerprint(self, source: SourceConfig | None = None) -> str:
        source = source or self._sources()[0]
        values = source.client_kwargs()
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
            pass
        values["source_id"] = source.id
        values["password_digest"] = hashlib.sha256(password.encode("utf-8")).hexdigest()
        serialized = json.dumps(values, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]

    def _source_fingerprint(
        self,
        name: str,
        source: SourceConfig | None = None,
    ) -> str:
        source = source or self._sources()[0]
        endpoint_key = (
            "subscriptions_path" if name == "subscriptions" else "group_rates_path"
        )
        client = Sub2APIClient(**source.client_kwargs())
        endpoint = client.resolve_url(getattr(client, endpoint_key))
        account, _password = source.credentials()
        payload = {
            "source_id": source.id,
            "endpoint": endpoint,
            "account_digest": hashlib.sha256(account.encode("utf-8")).hexdigest(),
            "timezone": client.timezone,
        }
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]

    def _configuration_ready(self) -> bool:
        try:
            sources, _errors = self._validate_config_partial()
        except Sub2APIError:
            return False
        return any(
            source.enabled
            and all(source.credentials())
            and bool(self._notify_groups(source))
            for source in sources
        )

    def _validate_common_config(self) -> None:
        legacy_credentials = [
            key for key in ("email", "password") if self._legacy_credential(key)
        ]
        if legacy_credentials:
            names = "、".join(legacy_credentials)
            raise Sub2APIError(f"{names} 不支持写入插件设置；所有凭据必须使用环境变量")
        poll_seconds = self._poll_seconds()
        if not math.isfinite(poll_seconds) or not 30 <= poll_seconds <= 86400:
            raise Sub2APIError("poll_seconds 必须是 30 到 86400 秒之间的有限数字")
        self._validate_global_groups()
        visual = self._config_raw("visual_report_enabled", True)
        if type(visual) is not bool:
            raise Sub2APIError("visual_report_enabled 必须是布尔值")

    def _validate_config(self) -> list[SourceConfig]:
        self._validate_common_config()
        return self._sources()

    def _validate_config_partial(self) -> tuple[list[SourceConfig], list[str]]:
        self._validate_common_config()
        sources, errors = parse_sources_partial(self._config_mapping())
        return sources, [
            self._safe_text(error) for error in errors[: self._MAX_RESULT_ERRORS]
        ]

    def _config_mapping(self) -> dict[str, Any]:
        try:
            return dict(self.ctx.config)
        except (RuntimeError, TypeError, ValueError):
            return {}

    def _sources(self) -> list[SourceConfig]:
        return parse_sources(self._config_mapping())

    def _is_designated_persona(self) -> bool:
        configured = self._config_value("run_on_persona")
        if not configured:
            return False
        try:
            current = str(self.ctx.engine.get_persona_name() or "")
        except (AttributeError, RuntimeError, TypeError):
            return False
        return current.casefold() == configured.casefold()

    def _background_status(self) -> str:
        configured = self._config_value("run_on_persona")
        if not configured:
            return "未指定执行人格"
        if not self._is_designated_persona():
            return "由指定人格负责"
        if not self._configuration_ready():
            return "等待有效站点、凭据或通知群"
        return "启用"

    def _status_text(self, selector: str = "") -> str:
        try:
            configured_sources, config_errors = self._validate_config_partial()
            sources = (
                source_by_selector(
                    configured_sources,
                    selector,
                    require_one=False,
                    include_disabled=True,
                )
                if selector
                else configured_sources
            )
        except Sub2APIError as exc:
            return f"Sub2API 多站监控状态：配置无效（{self._safe_error(exc)}）"
        lines = [
            f"Sub2API 多站监控：{len([source for source in sources if source.enabled])} 个启用站点；",
            f"通知群 {len(self._global_notify_groups())} 个；后台轮询 {self._background_status()}。",
        ]
        if config_errors:
            lines.append("配置警告：" + "；".join(config_errors))
        for source in sources:
            state = self._source_state(source)
            credentials = (
                "凭据就绪"
                if all(source.credentials())
                else (f"等待 {source.email_env}/{source.password_env}")
            )
            subscriptions = state.get("subscriptions_snapshot")
            rates = state.get("group_rates_snapshot")
            has_error = bool(state.get("last_poll_error"))
            lines.append(
                f"- [{source.id}] {source.display_name}："
                f"{'启用' if source.enabled else '停用'}，{credentials}，"
                f"订阅 {len(subscriptions) if isinstance(subscriptions, list) else 0}，"
                f"倍率 {len(rates) if isinstance(rates, list) else 0}，"
                f"上次成功 {state.get('last_poll_success_at') or '尚未成功'}"
                f"{'，最近一次轮询失败' if has_error else ''}。"
            )
        return "\n".join(lines)

    def _legacy_source_fingerprint(self, name: str, source: SourceConfig) -> str:
        return self._provenance_fingerprint(name, source, source_id="")

    def _legacy_versioned_source_fingerprints(
        self, name: str, source: SourceConfig
    ) -> set[str]:
        """Return exact fingerprints written by supported legacy state versions."""
        return {
            self._provenance_fingerprint(name, source, source_id=""),
            self._provenance_fingerprint(name, source, source_id="default"),
        }

    @staticmethod
    def _provenance_fingerprint(
        name: str, source: SourceConfig, *, source_id: str
    ) -> str:
        endpoint_key = (
            "subscriptions_path" if name == "subscriptions" else "group_rates_path"
        )
        client = Sub2APIClient(**source.client_kwargs())
        endpoint = client.resolve_url(getattr(client, endpoint_key))
        account, _password = source.credentials()
        payload = {
            "endpoint": endpoint,
            "account_digest": hashlib.sha256(account.encode("utf-8")).hexdigest(),
            "timezone": client.timezone,
        }
        if source_id:
            payload["source_id"] = source_id
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]

    def _migrate_legacy_state_if_unambiguous(self, sources: list[SourceConfig]) -> None:
        """Move legacy top-level state only when one explicit target is certain."""
        config = self._config_mapping()
        raw_sources = config.get("sources")
        if not isinstance(raw_sources, list) or len(raw_sources) != 1:
            return
        try:
            strict_sources = parse_sources(config)
        except Sub2APIError:
            return
        if len(strict_sources) != 1 or len(sources) != 1:
            return
        source = strict_sources[0]
        if source.legacy or not source.enabled or not all(source.credentials()):
            return
        store = self.get_data_store()
        if store.get("legacy_state_migration") is not None:
            return
        raw = store.all()
        has_legacy_snapshot = any(
            isinstance(raw.get(f"{name}_snapshot"), list)
            for name in ("subscriptions", "group_rates")
        )
        if not has_legacy_snapshot:
            return

        states = self._all_source_states()
        if source.id in states and states[source.id]:
            store.update(
                {
                    "legacy_state_migration": {
                        "status": "skipped_existing_state",
                        "source_id": source.id,
                    }
                }
            )
            return

        migrated: dict[str, Any] = {}
        legacy_sources: dict[str, str] = {}
        migrated_names: set[str] = set()
        for name in ("subscriptions", "group_rates"):
            snapshot = raw.get(f"{name}_snapshot")
            stored_source = raw.get(f"{name}_source")
            if not isinstance(snapshot, list) or not isinstance(stored_source, str):
                continue
            expected = self._legacy_versioned_source_fingerprints(name, source)
            if stored_source not in expected:
                continue
            sanitized_snapshot = redact_runtime_secrets(
                redact(snapshot),
                source.credentials(),
            )
            if not isinstance(sanitized_snapshot, list):
                continue
            migrated[f"{name}_snapshot"] = sanitized_snapshot
            migrated[f"{name}_source"] = self._source_fingerprint(name, source)
            legacy_sources[name] = stored_source
            migrated_names.add(name)

        if not migrated_names:
            store.update(
                {
                    "legacy_state_migration": {
                        "status": "skipped_source_mismatch",
                        "source_id": source.id,
                    }
                }
            )
            return

        raw_acks = self._load_notification_acks(store)
        migrated_acks = {
            key: value
            for key, value in raw_acks.items()
            if key.split(":", 1)[0] in migrated_names
        }
        migrated.update(
            {
                key: raw[key]
                for key in (
                    "last_poll_attempt_at",
                    "last_poll_at",
                    "last_poll_success_at",
                )
                if key in raw
            }
        )
        if raw.get("last_poll_error"):
            migrated["last_poll_error"] = "迁移前最近一次轮询失败"
        migrated["notification_acks"] = migrated_acks
        migrated["legacy_source_fingerprints"] = legacy_sources
        self._save_source_state(source, migrated)
        store.update(
            {
                "legacy_state_migration": {
                    "status": "migrated",
                    "source_id": source.id,
                    "collections": sorted(migrated_names),
                    "migrated_at": int(time.time()),
                }
            }
        )

    def _source_state(self, source: SourceConfig) -> dict[str, Any]:
        store = self.get_data_store()
        if source.legacy:
            return {
                key: store.get(key)
                for key in (
                    "subscriptions_snapshot",
                    "subscriptions_source",
                    "group_rates_snapshot",
                    "group_rates_source",
                    "notification_acks",
                    "notification_cursors",
                    "last_poll_attempt_at",
                    "last_poll_at",
                    "last_poll_success_at",
                    "last_poll_error",
                )
                if store.get(key) is not None
            }
        state = self._all_source_states().get(source.id, {})
        return dict(state) if isinstance(state, dict) else {}

    def _save_source_state(self, source: SourceConfig, state: dict[str, Any]) -> None:
        store = self.get_data_store()
        if source.legacy:
            store.update(state)
            return
        candidate_state = dict(state)
        if self._json_bytes(candidate_state) > self._MAX_SOURCE_STATE_BYTES:
            raise Sub2APIError("单站监控状态超过安全大小上限；请重置该站状态")
        states = self._all_source_states()
        states.pop(source.id, None)
        states[source.id] = candidate_state
        configured_ids = self._configured_source_ids()
        for stale_id in list(states):
            if len(states) <= self._MAX_SOURCE_STATES:
                break
            if stale_id not in configured_ids:
                states.pop(stale_id, None)
        for stale_id in list(states):
            if self._json_bytes(states) <= self._MAX_SOURCE_STATES_BYTES:
                break
            if stale_id not in configured_ids:
                states.pop(stale_id, None)
        if self._json_bytes(states) > self._MAX_SOURCE_STATES_BYTES:
            raise Sub2APIError("多站监控状态超过安全大小上限；请重置不再使用的站点状态")
        store.update({"source_states": states})

    @staticmethod
    def _json_bytes(value: Any) -> int:
        return len(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        )

    def _configured_source_ids(self) -> set[str]:
        raw_sources = self._config_mapping().get("sources")
        if not isinstance(raw_sources, list):
            return set()
        return {
            str(raw.get("id", "")).strip()
            for raw in raw_sources
            if isinstance(raw, dict) and isinstance(raw.get("id"), str)
        }

    def _all_source_states(self) -> dict[str, dict[str, Any]]:
        raw = self.get_data_store().get("source_states", {})
        if not isinstance(raw, dict):
            return {}
        configured_ids = self._configured_source_ids()
        selected: set[str] = {
            source_id
            for source_id in configured_ids
            if source_id in raw and isinstance(raw[source_id], dict)
        }
        for key in reversed(raw):
            if len(selected) >= self._MAX_SOURCE_STATES:
                break
            if isinstance(key, str) and isinstance(raw[key], dict):
                selected.add(key)
        return {
            str(key): dict(value)
            for key, value in raw.items()
            if key in selected and isinstance(key, str) and isinstance(value, dict)
        }

    def _notify_groups(self, source: SourceConfig | None = None) -> list[str]:
        inherited = (
            self._global_notify_groups()
            if source is None or source.inherit_notify_group_ids
            else []
        )
        dedicated = list(source.notify_group_ids) if source is not None else []
        return list(dict.fromkeys([*inherited, *dedicated]))

    def _global_notify_groups(self) -> list[str]:
        raw = self._config_raw("notify_group_ids", [])
        if isinstance(raw, str):
            values = raw.replace("，", ",").split(",")
        elif isinstance(raw, list):
            values = raw
        else:
            values = []
        return list(
            dict.fromkeys(
                str(value).strip() for value in values if self._valid_group_id(value)
            )
        )

    def _validate_global_groups(self) -> None:
        raw = self._config_raw("notify_group_ids", [])
        if not isinstance(raw, (str, list)):
            raise Sub2APIError("notify_group_ids 必须是字符串列表")
        values = raw.replace("，", ",").split(",") if isinstance(raw, str) else raw
        if len(values) > self._MAX_NOTIFY_GROUPS:
            raise Sub2APIError(f"notify_group_ids 最多允许 {self._MAX_NOTIFY_GROUPS} 项")
        if any(not self._valid_group_id(value) for value in values):
            raise Sub2APIError("notify_group_ids 包含无效群号")

    def _visual_report_enabled(self) -> bool:
        configured = self._config_raw("visual_report_enabled", None)
        if configured is None:
            return bool(self._config_raw("sources", []))
        return configured is True

    def _artifact_dir(self) -> Path:
        getter = getattr(self.ctx, "get_artifact_dir", None)
        if callable(getter):
            return Path(getter())
        return Path(self.get_data_store().artifact_dir)

    def _safe_error(self, exc: BaseException) -> str:
        text = str(exc) or type(exc).__name__
        secrets: list[str] = [
            self._legacy_credential("password"),
            self._legacy_credential("email"),
            os.getenv("SUB2API_PASSWORD", ""),
            os.getenv("SUB2API_EMAIL", ""),
        ]
        try:
            sources, _errors = parse_sources_partial(self._config_mapping())
            for source in sources:
                secrets.extend(source.credentials())
        except (Sub2APIError, TypeError, ValueError):
            pass
        redacted = redact_runtime_secrets(text, secrets)
        return str(redacted)[:500]

    def _safe_text(self, value: Any) -> str:
        return self._safe_error(RuntimeError(str(value))) if value else ""

    @staticmethod
    def _arg(cmd: CommandAST, index: int, default: str = "") -> str:
        if 0 <= index < len(cmd.args):
            return str(cmd.args[index].value)
        return default

    def _config_raw(self, key: str, default: Any = None) -> Any:
        try:
            return self.ctx.config.get(key, default)
        except RuntimeError:
            return default

    def _config_value(
        self,
        key: str,
        *,
        default: str = "",
        allow_empty: bool = False,
    ) -> str:
        value = self._config_raw(key, default)
        if value is None:
            return default
        text = str(value).strip()
        return text if text or allow_empty else default

    def _legacy_credential(self, key: str) -> str:
        value = self._config_raw(key, "")
        return "" if value is None else str(value).strip()

    def _poll_seconds(self) -> float:
        raw = self._config_raw("poll_seconds", 300)
        if isinstance(raw, bool):
            return float("nan")
        try:
            return float(raw)
        except (TypeError, ValueError, OverflowError):
            return float("nan")

    @staticmethod
    def _valid_group_id(value: Any) -> bool:
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            return False
        text = str(value).strip()
        return (
            bool(text)
            and len(text) <= 128
            and not any(ord(char) < 32 or ord(char) == 127 for char in text)
        )

    @staticmethod
    def _poll_text(result: PollResult) -> str:
        parts = [
            "Sub2API 多站点轮询完成。",
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
            parts.append(f"已静默初始化：{', '.join(result.initialized)}。")
        if result.notifications_sent:
            parts.append(f"已确认 {result.notifications_sent} 次群投递。")
        if result.errors:
            parts.append("部分操作失败：" + "；".join(result.errors))
        if not result.change_count and not result.errors and not result.initialized:
            parts.append("没有检测到变化。")
        return "".join(parts)


def _format_change(
    event_type: str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    *,
    source_name: str = "Sub2API",
) -> str:
    record = after or before or {}
    prefix = f"【{source_name}】"
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
        return f"{prefix}{labels[event_type]}：{title}（分组：{group}{suffix}）"

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
    return f"{prefix}{labels[event_type]}：{group}，{detail}"


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


def _format_rate_line(record: dict[str, Any]) -> str:
    """格式化单条倍率记录为人类易读的一行。"""
    name = (
        record.get("name")
        or record.get("group_name")
        or record.get("group")
        or record.get("slug")
        or f"分组 {record.get('id', '?')}"
    )
    rate = _primary_rate_value(record)
    platform = record.get("platform")
    status = record.get("status")
    extras = []
    if platform:
        extras.append(str(platform))
    if status and str(status) != "active":
        extras.append(str(status))
    extra_text = f"（{' · '.join(extras)}）" if extras else ""
    try:
        rate_text = f"{float(rate):g}x"
    except (TypeError, ValueError):
        rate_text = str(rate)
    return f"· {name}：{rate_text}{extra_text}"


def _format_subscription_line(record: dict[str, Any]) -> str:
    """格式化单条订阅记录为人类易读的一行。"""
    name = (
        record.get("name")
        or record.get("plan_name")
        or record.get("product_name")
        or record.get("slug")
        or f"订阅 {record.get('id', '?')}"
    )
    extras = []
    for key in ("plan", "plan_id", "status", "expires_at", "expire_at", "quota"):
        value = record.get(key)
        if value not in (None, ""):
            extras.append(f"{key}: {value}")
    extra_text = f"（{'，'.join(extras)}）" if extras else ""
    return f"· {name}{extra_text}"


def _format_records(title: str, records: list[dict[str, Any]], *, kind: str = "") -> str:
    """把记录列表格式化为人类易读文本（不再输出原始 JSON）。"""
    if not records:
        return f"{title}：暂无数据"
    if kind == "rate":
        lines = [_format_rate_line(record) for record in records]
    elif kind == "subscription":
        lines = [_format_subscription_line(record) for record in records]
    else:
        lines = [
            _format_rate_line(record)
            if record.get("rate_multiplier") is not None
            else _format_subscription_line(record)
            for record in records
        ]
    body = "\n".join(lines)
    if len(body) > 3500:
        kept = []
        total = 0
        for line in lines:
            if total + len(line) + 1 > 3400:
                kept.append("…（其余已省略）")
                break
            kept.append(line)
            total += len(line) + 1
        body = "\n".join(kept)
    return f"{title}（{len(records)} 个）\n{body}"
