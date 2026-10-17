"""通过群聊命令查询 AMKR 账号资源与渠道状态，并返回本地绘制的图片。"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path

from sirius_pulse.adapters.models import ImageSegment, MessageGroup
from sirius_pulse.plugins.api import CommandAST, PluginBase, PluginResponse, command
from sirius_pulse.providers.amkr import load_amkr_settings

from .client import AMKRAccountError, fetch_accounts, fetch_status
from .data import local_time, project_accounts, project_status
from .visual import render_accounts, render_status

_HELP = (
    "AMKR 账号与状态\n"
    "/amkr account [all]：查询全部 CPA 账号和订阅资源\n"
    "/amkr account <名称或完整ID>：查询指定账号，也可填写 CPA 实例 ID\n"
    "/amkr status [小时]：渠道健康矩阵与请求趋势，默认最近 24 小时\n"
    "一次展示全部匹配资源，一个账号一行。\n"
    "同名账号请用完整 ID；名称含空格可直接输入或加引号。\n"
    "所有群成员均可查询；每次查询都会让 AMKR 重新获取全部数据。"
)


def _parse_hours(args: list[str]) -> int:
    if len(args) > 1:
        raise AMKRAccountError("用法：/amkr status [1–8760 的小时数]，例如 /amkr status 6。")
    if not args:
        return 24
    raw = args[0].strip()
    if not raw.isascii() or not raw.isdigit():
        raise AMKRAccountError("小时数需为 1–8760 的整数，例如 /amkr status 6。")
    hours = int(raw)
    if not 1 <= hours <= 8760:
        raise AMKRAccountError("小时数需在 1–8760 之间，例如 /amkr status 24。")
    return hours


class AMKRAccountsPlugin(PluginBase):
    """只读、群聊限定、无后台轮询的 AMKR 账号与状态看板。"""

    _plugin_name = "amkr_accounts"
    _plugin_display_name = "AMKR 账号资源"
    _plugin_description = "通过 /amkr account 查询账号用量，/amkr status 查看渠道健康与请求趋势。"
    _plugin_version = "1.1.1"
    _plugin_author = "Sirius Pulse"
    _plugin_min_framework_version = "1.3.0"
    _plugin_dependencies = ["httpx>=0.24.0", "Pillow>=9.0.0"]
    _plugin_permissions = {
        "developer_only": False,
        "hidden_from_intent": True,
        "rate_limit": {"calls_per_minute": 2, "calls_per_hour": 30},
    }
    _plugin_parameters = [
        {
            "name": "global_data_path",
            "type": "str",
            "default": "",
            "description": "全局 data 目录的绝对路径；标准 data/personas/<人格> 布局可留空自动解析。",
        },
    ]
    _plugin_ui_schema = {
        "version": 1,
        "title": "AMKR 账号资源",
        "description": "复用全局 AMKR 管理连接；所有群成员均可查询。所有图片在内存生成，不保存账号快照。",
        "parameters": {
            "global_data_path": {
                "label": "全局数据目录（可选）",
                "widget": "path",
                "help": "标准部署留空；自定义目录布局时填写绝对路径。不要填写任何 Key。",
            },
        },
    }

    def __init__(self) -> None:
        super().__init__()
        self._query_lock = asyncio.Lock()

    @command(
        "amkr",
        prefix="/",
        patterns=["amkr"],
        render_mode="direct",
        description="AMKR 全局账号资源与用量图片。",
        hidden_from_intent=True,
        examples=[
            "/amkr account",
            "/amkr account cpa-main/0",
            '/amkr account "我的 主力账号"',
        ],
    )
    async def amkr(self) -> PluginResponse:
        return PluginResponse.ok(text=_HELP)

    def _global_data_path(self) -> Path:
        configured = str(self.ctx.config.get("global_data_path") or "").strip()
        if configured:
            path = Path(configured).expanduser()
            if not path.is_absolute():
                raise AMKRAccountError("插件 global_data_path 必须填写绝对路径。")
            return path
        engine = self.ctx.engine.get_engine()
        work_path = getattr(engine, "work_path", None)
        if work_path is None and self.ctx.data_store is not None:
            work_path = self.ctx.data_store.store_path.parent
        if work_path is None:
            raise AMKRAccountError("无法确定全局数据目录，请配置插件 global_data_path。")
        path = Path(work_path).resolve()
        return path.parent.parent if path.parent.name == "personas" else path

    async def execute_async(self, cmd: CommandAST) -> list[PluginResponse]:
        if cmd.command != "amkr":
            return [PluginResponse.fail("未识别的 AMKR 命令。")]
        args = [str(arg.value) for arg in cmd.args]
        if not args or args[0].casefold() in {"help", "帮助"}:
            return [PluginResponse.ok(text=_HELP)]
        action = args[0].casefold()
        if action not in {"account", "status"}:
            return [PluginResponse.fail(_HELP)]
        group_id = self.ctx.message.group_id
        if not group_id or group_id.startswith("private_"):
            return [PluginResponse.fail("请在群聊中使用 /amkr account 或 /amkr status。")]
        try:
            if self._query_lock.locked():
                raise AMKRAccountError("正在查询 AMKR，请等待当前查询完成。")
            async with self._query_lock:
                settings = await asyncio.to_thread(load_amkr_settings, self._global_data_path())
                if action == "status":
                    hours = _parse_hours(args[1:])
                    payload = await fetch_status(settings, hours)
                    status = project_status(payload, hours, (settings.api_key,))
                    png = await asyncio.to_thread(render_status, status)
                    rate = (
                        f"{status.success_rate * 100:.1f}%"
                        if status.success_rate is not None
                        else None
                    )
                    if rate is None:
                        text = f"AMKR 状态监控 · 近 {hours} 小时 · 窗口内没有请求（已脱敏）"
                    else:
                        text = f"AMKR 状态监控 · 近 {hours} 小时 · 成功率 {rate}（已脱敏）"
                else:
                    selector = " ".join(args[1:]).strip()
                    if (
                        len(selector) >= 2
                        and selector[0] == selector[-1]
                        and selector[0] in {"'", '"'}
                    ):
                        selector = selector[1:-1].strip()
                    payload = await fetch_accounts(settings)
                    accounts = project_accounts(payload, selector, (settings.api_key,))
                    png = await asyncio.to_thread(
                        render_accounts,
                        accounts,
                        local_time(payload.get("fetched_at"), full=True),
                    )
                    text = f"AMKR 账号资源 · {len(accounts)} 项（已脱敏）"
                image = ImageSegment(file_path="base64://" + base64.b64encode(png).decode("ascii"))
            return [
                PluginResponse.ok(
                    text=text, message_group=MessageGroup([image]), render_mode="direct"
                )
            ]
        except AMKRAccountError as exc:
            return [PluginResponse.fail(str(exc))]
        except Exception as exc:
            # 原始异常/上游响应可能含 URL、凭据或账号，不进入群消息及日志。
            feature = "状态" if action == "status" else "账号"
            self.logger.warning("AMKR %s图片生成失败：%s", feature, type(exc).__name__)
            return [PluginResponse.fail(f"{feature}图片生成失败，请检查中文字体安装与 AMKR 响应格式。")]
