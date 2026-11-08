"""群聊手帐（chat_analyzer）：把群聊做成可爱的横版日报 / 周报。

触发：
    /ca daily [昨天]      今日（或昨天）的群聊手帐
    /ca weekly [上周]     本周（或上周）的群聊周刊
    /ca analyse [分钟]    最近一段时间，按日报版式出图
    定时：插件自己每分钟看一眼设置里的日报 / 周报时间，到点给白名单里够热闹的群各发一份；
          每个群每个周期只发一次（记在插件数据里，重启也不会重发），错过时间两小时内会补发。

数据：引擎归档 ``{work_path}/archive/{group_id}.jsonl``（按时间窗从尾部倒读），归档缺失时退回
basic_memory 的内存窗口。她的文字一次模型调用写完；「今日一画」走框架生图，按天限额。
"""

from __future__ import annotations

import asyncio
import base64
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sirius_pulse.adapters.models import ImageSegment, MessageGroup, TextSegment
from sirius_pulse.plugins.api import (
    PluginBase,
    PluginResponse,
    command,
    command_group,
    group_command,
)

from . import avatars, metrics, page, partners, source, writer
from .source import CN_TZ

logger = logging.getLogger(__name__)

_VERSION = "5.0.0"
_RENDER_TIMEOUT = 90.0
_ART_TIMEOUT = 150.0
#: 到点之后多久之内还能补发（插件重启、机器休眠时别把当天的手帐丢了）
_CATCH_UP = timedelta(hours=2)
_KEEP_REPORTS = 30
_DEFAULT_BLOCKED = [
    "动画表情",
    "jpg",
    "png",
    "gif",
    "jpeg",
    "webp",
    "图片",
    "表情",
    "表情包",
    "动图",
    "截图",
    "文件",
]


def _now() -> datetime:
    return datetime.now(CN_TZ)


def _parse_hhmm(value: Any, default: str) -> tuple[int, int]:
    text = str(value or default).strip().replace("：", ":")
    try:
        hour, minute = (int(part) for part in text.split(":", 1))
        if 0 <= hour < 24 and 0 <= minute < 60:
            return hour, minute
    except (TypeError, ValueError):
        pass
    hour, minute = (int(part) for part in default.split(":"))
    return hour, minute


def day_window(now: datetime, *, yesterday: bool = False) -> tuple[datetime, datetime]:
    """日报窗口：当天 0 点到现在；凌晨六点前或指明「昨天」时给完整的昨天。"""
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if yesterday or now.hour < 6:
        return midnight - timedelta(days=1), midnight
    return midnight, now


def week_window(now: datetime, *, last_week: bool = False) -> tuple[datetime, datetime]:
    """周报窗口：本周一 0 点到现在；周一白天之前或指明「上周」时给完整的上周。"""
    monday = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    if last_week or now - monday < timedelta(hours=12):
        return monday - timedelta(days=7), monday
    return monday, now


class ChatAnalyzerPlugin(PluginBase):
    """群聊手帐：日报、周报、任意时段的群聊回顾。"""

    _plugin_name = "chat_analyzer"
    _plugin_display_name = "群聊手帐"
    _plugin_description = (
        "把群聊做成可爱的横版手帐：群聊天气与群温、花朵时钟、今日剧情、称号、CP、笑点与复读、" "话题泡泡，周报还有一周天气、糖豆日历和群友星座；她写日记，还能画一张今日一画。"
    )
    _plugin_version = "5.0.0"  # 与 _VERSION 保持一致（加载器要求字面量）
    _plugin_author = "sirius-chat"
    _plugin_dependencies = ["playwright"]
    _plugin_nl_examples = ["看看今天的群聊日报", "出一份群聊周报", "今天群里聊了啥"]
    _plugin_prompt_inject = "群聊手帐：群友可以用 /ca daily、/ca weekly 让我把群聊做成日报或周报。"
    _plugin_events = [
        {
            "type": "timer.tick",
            "interval_seconds": 60,
            "description": "每分钟看一眼是否到了日报 / 周报时间",
        }
    ]
    _plugin_parameters = [
        {
            "name": "daily_enabled",
            "type": "bool",
            "description": "每天自动发日报",
            "group": "定时",
            "default": True,
        },
        {
            "name": "daily_time",
            "type": "str",
            "description": "日报时间（HH:MM，凌晨六点前表示发前一天的）",
            "group": "定时",
            "default": "22:00",
        },
        {
            "name": "weekly_enabled",
            "type": "bool",
            "description": "每周自动发周报",
            "group": "定时",
            "default": True,
        },
        {
            "name": "weekly_day",
            "type": "int",
            "description": "周报在星期几发（1=周一 … 7=周日）",
            "group": "定时",
            "default": 7,
        },
        {
            "name": "weekly_time",
            "type": "str",
            "description": "周报时间（HH:MM）",
            "group": "定时",
            "default": "21:30",
        },
        {
            "name": "min_messages",
            "type": "int",
            "description": "当天少于多少条消息就不自动发日报（周报按 3 倍算）",
            "group": "定时",
            "default": 30,
        },
        {
            "name": "illustration",
            "type": "bool",
            "description": "给日报 / 周报画一张总结插画（走 AMKR 生图）",
            "group": "插画",
            "default": True,
        },
        {
            "name": "art_per_day",
            "type": "int",
            "description": "每天最多画几张插画（所有群合计）",
            "group": "插画",
            "default": 3,
        },
        {
            "name": "allowed_groups",
            "type": "list",
            "description": "允许出手帐的群号（留空表示所有群）",
            "group": "群范围",
            "default": [],
        },
        {
            "name": "blocked_words",
            "type": "list",
            "description": "话题泡泡里不想看到的词",
            "group": "话题",
            "default": [
                "动画表情",
                "jpg",
                "png",
                "gif",
                "jpeg",
                "webp",
                "图片",
                "表情",
                "表情包",
                "动图",
                "截图",
                "文件",
            ],
        },
    ]

    def on_load(self) -> None:
        self._busy: set[str] = set()
        self._tick_lock = asyncio.Lock()
        self._art_lock = asyncio.Lock()

    # ── 设置 ───────────────────────────────────────────────────────────

    def _settings(self) -> dict[str, Any]:
        settings = getattr(self.ctx, "config", None)
        return settings if isinstance(settings, dict) else {}

    def _option(self, key: str, default: Any) -> Any:
        value = self._settings().get(key, default)
        return default if value is None else value

    def _int_option(self, key: str, default: int, low: int, high: int) -> int:
        try:
            return max(low, min(high, int(self._option(key, default))))
        except (TypeError, ValueError):
            return default

    def _allowed(self, group_id: str) -> bool:
        groups = self._option("allowed_groups", [])
        allowed = (
            {str(g).strip() for g in groups if str(g).strip()}
            if isinstance(groups, list)
            else set()
        )
        return not allowed or group_id in allowed

    def _blocked(self) -> list[str]:
        words = self._option("blocked_words", _DEFAULT_BLOCKED)
        words = words if isinstance(words, list) else _DEFAULT_BLOCKED
        persona = self._persona_name()
        return [str(w).strip() for w in [*words, persona] if str(w).strip()]

    def _daily_time(self) -> tuple[int, int]:
        configured = self._settings().get("daily_time")
        if not configured:
            # 兼容 4.x 的 schedule: [{"time": "22:00", "duration": 1440}]
            legacy = self._settings().get("schedule")
            if isinstance(legacy, list) and legacy and isinstance(legacy[0], dict):
                configured = legacy[0].get("time")
        return _parse_hhmm(configured, "22:00")

    # ── 指令 ───────────────────────────────────────────────────────────

    @command(
        "ca",
        prefix="/",
        patterns=["ca"],
        pattern_type="prefix",
        render_mode="direct",
        description="群聊手帐用法",
        examples=["/ca", "/ca daily", "/ca weekly"],
        hidden_from_intent=True,
    )
    def cmd_help(self) -> PluginResponse:
        dh, dm = self._daily_time()
        wd = self._int_option("weekly_day", 7, 1, 7)
        wh, wm = _parse_hhmm(self._option("weekly_time", "21:30"), "21:30")
        week_names = "一二三四五六日"
        return PluginResponse.ok(
            text=(
                "📒 群聊手帐\n"
                "/ca daily —— 今天的群聊手帐（/ca daily 昨天 看昨天的）\n"
                "/ca weekly —— 这周的群聊周刊（/ca weekly 上周 看上周的）\n"
                "/ca analyse [分钟] —— 最近一段时间的手帐，默认 1440 分钟\n"
                f"自动发送：每天 {dh:02d}:{dm:02d} 日报，每周{week_names[wd - 1]} {wh:02d}:{wm:02d} 周报"
            )
        )

    @command_group(
        "ca",
        prefix="/",
        patterns=["ca"],
        pattern_type="prefix",
        description="群聊手帐",
        examples=["/ca daily", "/ca weekly", "/ca analyse 360"],
        hidden_from_intent=True,
    )
    def ca_group(self) -> None:
        """指令组入口。"""

    @group_command(
        "daily",
        patterns=["daily", "日报", "report daily", "每日"],
        pattern_type="prefix",
        parent="ca",
        render_mode="direct",
        description="今天（或昨天）的群聊手帐",
        examples=["daily", "daily 昨天"],
        hidden_from_intent=True,
        timeout=300.0,
    )
    async def cmd_daily(self, when: str = "") -> PluginResponse:
        group_id = self._command_group()
        if isinstance(group_id, PluginResponse):
            return group_id
        start, end = day_window(_now(), yesterday="昨" in when)
        return await self._respond("day", group_id, start, end)

    @group_command(
        "weekly",
        patterns=["weekly", "周报", "report weekly", "每周"],
        pattern_type="prefix",
        parent="ca",
        render_mode="direct",
        description="本周（或上周）的群聊周刊",
        examples=["weekly", "weekly 上周"],
        hidden_from_intent=True,
        timeout=300.0,
    )
    async def cmd_weekly(self, when: str = "") -> PluginResponse:
        group_id = self._command_group()
        if isinstance(group_id, PluginResponse):
            return group_id
        start, end = week_window(_now(), last_week="上" in when)
        return await self._respond("week", group_id, start, end)

    @group_command(
        "analyse",
        patterns=["analyse", "analyze", "群聊分析", "聊天分析"],
        pattern_type="prefix",
        parent="ca",
        render_mode="direct",
        description="最近一段时间的群聊手帐",
        examples=["analyse", "analyse 360"],
        hidden_from_intent=True,
        timeout=300.0,
    )
    async def cmd_analyze(self, duration: int = 1440) -> PluginResponse:
        group_id = self._command_group()
        if isinstance(group_id, PluginResponse):
            return group_id
        duration = max(10, min(int(duration or 1440), 10080))
        end = _now()
        start = end - timedelta(minutes=duration)
        label = f"最近 {duration // 60} 小时" if duration % 60 == 0 else f"最近 {duration} 分钟"
        return await self._respond("day", group_id, start, end, window_label=label)

    def _command_group(self) -> str | PluginResponse:
        group_id = str(self.ctx.message.group_id or "")
        if not group_id or group_id.startswith("private_"):
            return PluginResponse.fail("群聊手帐只能在群里用哦")
        if not self._allowed(group_id):
            return PluginResponse.fail("这个群还没开群聊手帐，请先在插件设置里把群号加进白名单。")
        if group_id in self._busy:
            return PluginResponse.fail("手帐正在画，等我一下下～")
        return group_id

    async def _respond(
        self, kind: str, group_id: str, start: datetime, end: datetime, *, window_label: str = ""
    ) -> PluginResponse:
        self._busy.add(group_id)
        try:
            result = await self._make(
                kind, group_id, start, end, window_label=window_label, fresh_art=False
            )
        finally:
            self._busy.discard(group_id)
        if result is None:
            return PluginResponse.fail("这段时间群里还没有聊天记录，手帐是空白的～")
        say = result["say"]
        if result["png"] is None:
            return PluginResponse.ok(text=say + "\n" + result["text"])
        image = ImageSegment(
            file_path="base64://" + base64.b64encode(result["png"]).decode("ascii")
        )
        return PluginResponse.ok(
            text=say,
            message_group=MessageGroup([TextSegment(say), image]),
            render_mode="direct",
            data={"kind": kind, "total": result["report"]["total"]},
        )

    # ── 定时 ───────────────────────────────────────────────────────────

    async def on_event(self, event_type: str, event_data: dict) -> None:
        if event_type not in ("timer.tick", "timer.schedule"):
            return
        if self._tick_lock.locked():
            return
        async with self._tick_lock:
            try:
                await self.tick(_now())
            except Exception as exc:  # noqa: BLE001 - 定时任务不能把调度器拖进退避
                logger.warning("群聊手帐定时检查失败: %s", exc)

    def _due(self, now: datetime, kind: str) -> tuple[str, datetime, datetime] | None:
        """现在该不该发：返回 (周期键, 窗口起, 窗口止)。"""
        if kind == "day":
            if not self._option("daily_enabled", True):
                return None
            hour, minute = self._daily_time()
            fire = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if not fire <= now < fire + _CATCH_UP:
                return None
            start, end = day_window(fire)
            return start.strftime("%Y-%m-%d"), start, end
        if not self._option("weekly_enabled", True):
            return None
        weekday = self._int_option("weekly_day", 7, 1, 7) - 1
        hour, minute = _parse_hhmm(self._option("weekly_time", "21:30"), "21:30")
        if now.weekday() != weekday and (now - _CATCH_UP).weekday() != weekday:
            return None
        base = now if now.weekday() == weekday else now - _CATCH_UP
        fire = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if not fire <= now < fire + _CATCH_UP:
            return None
        start, end = week_window(fire)
        return start.strftime("%G-W%V"), start, end

    async def tick(self, now: datetime) -> list[tuple[str, str]]:
        """检查一次定时；返回这次发出去的 (类型, 群号)。"""
        plans = [(kind, plan) for kind in ("week", "day") if (plan := self._due(now, kind))]
        if not plans:
            return []
        engine = self._engine()
        if engine is None:
            return []
        store = self.get_data_store()
        sent: list[tuple[str, str]] = []
        floor = self._int_option("min_messages", 30, 0, 100000)
        for kind, (period, start, end) in plans:
            for group_id in self._groups(engine):
                key = f"sent:{kind}:{group_id}"
                if (
                    store.get(key) == period
                    or not self._allowed(group_id)
                    or group_id in self._busy
                ):
                    continue
                self._busy.add(group_id)
                try:
                    result = await self._make(
                        kind,
                        group_id,
                        start,
                        end,
                        fresh_art=True,
                        min_total=floor * (3 if kind == "week" else 1),
                    )
                    # 太冷清也记一笔：这个周期不再反复检查
                    store.set(key, period)
                    if result is None or result["png"] is None:
                        continue
                    if await self._push(group_id, kind, result):
                        sent.append((kind, group_id))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("群聊手帐 [%s/%s] 生成失败: %s", kind, group_id, exc)
                finally:
                    self._busy.discard(group_id)
        return sent

    async def _push(self, group_id: str, kind: str, result: dict[str, Any]) -> bool:
        folder = self.get_artifact_dir()
        stamp = _now().strftime("%Y%m%d_%H%M%S")
        path = folder / f"{kind}_{group_id}_{stamp}.png"
        path.write_bytes(result["png"])
        self._prune(folder)
        ok = await self.ctx.engine.dispatch_proactive_message(
            group_id=group_id, text=result["say"], image_path=str(path)
        )
        if not ok:
            logger.warning("群聊手帐发送失败 [%s]，图片留在 %s", group_id, path)
        return bool(ok)

    @staticmethod
    def _prune(folder: Path) -> None:
        try:
            images = sorted(
                (p for p in folder.glob("*.png") if p.name[:4] in ("day_", "week")),
                key=lambda p: p.stat().st_mtime,
            )
        except OSError:
            return
        for path in images[:-_KEEP_REPORTS]:
            try:
                path.unlink()
            except OSError:
                pass

    # ── 引擎与数据 ─────────────────────────────────────────────────────

    def _engine(self) -> Any:
        try:
            return self.ctx.engine.get_engine() if self.ctx.engine else None
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _groups(engine: Any) -> list[str]:
        groups: list[str] = []
        try:
            groups.extend(str(g) for g in engine.basic_memory.list_groups())
        except Exception:  # noqa: BLE001
            pass
        groups.extend(source.list_archived_groups(getattr(engine, "work_path", None)))
        return [g for g in dict.fromkeys(groups) if g and not g.startswith("private_")]

    def _persona_name(self) -> str:
        try:
            return str(self.ctx.engine.get_persona_name() or "")
        except Exception:  # noqa: BLE001
            return ""

    def _self_uid(self) -> str:
        try:
            for uid in (self.ctx.engine.get_bot_platform_uids() or {}).values():
                value = str(uid or "")
                if value.isdigit() and 5 <= len(value) <= 11:
                    return value
        except Exception:  # noqa: BLE001
            pass
        return ""

    async def _messages(
        self, engine: Any, group_id: str, start: datetime, end: datetime
    ) -> list[source.Msg]:
        start_ts, end_ts = int(start.timestamp()), int(end.timestamp())
        path = source.archive_path(getattr(engine, "work_path", None), group_id)
        entries: list[Any] = []
        if path is not None:
            try:
                entries = await asyncio.to_thread(
                    source.read_archive_window, path, start_ts, end_ts
                )
            except OSError as exc:
                logger.warning("读取归档失败 [%s]: %s", group_id, exc)
        if not entries:
            try:
                memory = engine.basic_memory.get_all(group_id)
            except Exception:  # noqa: BLE001
                memory = []
            entries = [
                e
                for e in memory
                if start_ts <= source.parse_ts(getattr(e, "timestamp", "")) <= end_ts
            ]
        return source.normalize(entries, self_uid=self._self_uid())

    async def _group_name(self, group_id: str) -> str:
        adapter = self.ctx.adapter
        if adapter is not None:
            try:
                info = await adapter.get_group_info(group_id)
                name = str((info or {}).get("group_name", "") or "").strip()
                if name:
                    return name
            except Exception:  # noqa: BLE001
                pass
        try:
            profile = self._engine().semantic_memory.get_group_profile(group_id)
            name = str(getattr(profile, "group_name", "") or "").strip()
            if name:
                return name
        except Exception:  # noqa: BLE001
            pass
        return f"群 {group_id}"

    # ── 生成一份手帐 ───────────────────────────────────────────────────

    async def _make(
        self,
        kind: str,
        group_id: str,
        start: datetime,
        end: datetime,
        *,
        window_label: str = "",
        fresh_art: bool,
        min_total: int = 1,
    ) -> dict[str, Any] | None:
        engine = self._engine()
        if engine is None:
            return None
        span = end - start
        messages = await self._messages(engine, group_id, start, end)
        humans = sum(1 for m in messages if not m.is_bot)
        if humans < max(1, min_total):
            logger.info("群聊手帐 [%s/%s] 只有 %d 条，跳过", kind, group_id, humans)
            return None
        previous = await self._messages(engine, group_id, start - span, end - span)
        blocked = self._blocked()
        if kind == "week":
            report = metrics.build_week(
                messages, start=start, end=end, previous=previous, blocked=blocked
            )
        else:
            report = metrics.build_day(
                messages,
                start=start,
                end=end,
                previous_count=sum(1 for m in previous if not m.is_bot) or None,
                blocked=blocked,
            )

        persona = self._persona_name() or "她"
        self_uid = self._self_uid()
        names = {m.uid: m.name for m in [*previous, *messages] if m.name and not m.is_bot}
        names.update({uid: p.name for uid, p in report["people"].items()})
        group_name = await self._group_name(group_id)
        # 这一期出现过、又存了形象设定图的 AI 伙伴：可以画进今日一画
        friends = partners.present_partners(engine, metrics.speaker_names(messages))

        writing_task = asyncio.create_task(
            self._write(
                report,
                names=names,
                group_name=group_name,
                persona=persona,
                window_label=window_label,
                group_id=group_id,
                partners=[friend.name for friend in friends],
            )
        )
        faces_task = asyncio.create_task(self._faces(report, self_uid))
        writing = await writing_task
        art_uri = await self._art(
            kind,
            group_id,
            start,
            writer.image_prompt_inputs(report, writing),
            persona,
            friends,
            fresh=fresh_art,
        )
        faces = await faces_task

        view = {
            "group_name": group_name,
            "names": names,
            "avatars": faces,
            "writing": writing,
            "persona_name": persona,
            "persona_avatar": faces.get(self_uid, ""),
            "art": art_uri,
            "art_caption": "本周一画" if kind == "week" else "今日一画",
            "window_label": window_label,
            "stamps": [m.ts for m in messages if not m.is_bot],
            "version": _VERSION,
            "generated_at": _now(),
        }
        html = (
            page.render_week_html(report, view)
            if kind == "week"
            else page.render_day_html(report, view)
        )
        try:
            png: bytes | None = await asyncio.wait_for(page.html_to_png(html), _RENDER_TIMEOUT)
        except (asyncio.TimeoutError, RuntimeError) as exc:
            logger.warning("群聊手帐出图失败，改发文字版: %s", exc)
            png = None
        return {
            "report": report,
            "png": png,
            "say": writer.plain(writing["headline"], names),
            "text": self._text_version(report, names),
        }

    async def _write(
        self, report: dict[str, Any], *, group_id: str, **kwargs: Any
    ) -> dict[str, Any]:
        window_label = kwargs.get("window_label", "")
        prompt = writer.build_prompt(report, **kwargs)
        ids = [s.index for s in report.get("session_objs", [])]
        try:
            raw = await self.ctx.engine.generate_text(
                prompt,
                group_id=group_id,
                messages=[{"role": "user", "content": "（请写这一期的群聊手帐）"}],
                task_name="plugin_generate",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("群聊手帐的文字生成失败: %s", type(exc).__name__)
            raw = ""
        parsed = writer.parse(str(raw or ""), ids)
        base = writer.fallback(report, window_label=window_label)
        if parsed is None:
            return base
        return {key: parsed.get(key) or base[key] for key in base}

    async def _faces(self, report: dict[str, Any], self_uid: str) -> dict[str, str]:
        wanted: list[str] = list(report["ranking"][:12])
        wanted += [a["uid"] for a in report["awards"]]
        wanted += [uid for pair in report["pairs"] for uid in (pair["a"], pair["b"])]
        wanted += [uid for s in report["sessions"] for uid in s["people"]]
        wanted += report.get("regulars", []) + report.get("newcomers", [])
        wanted += (report.get("constellation") or {}).get("stars", [])
        wanted += writer.mentionable(report)
        if self_uid:
            wanted.append(self_uid)
        try:
            cache = self.get_artifact_dir() / "avatars"
        except RuntimeError:
            cache = None
        return await avatars.load_avatars(wanted, cache)

    # ── 今日一画 ───────────────────────────────────────────────────────

    def _take_art_quota(self) -> bool:
        limit = self._int_option("art_per_day", 3, 0, 50)
        if limit <= 0:
            return False
        store = self.get_data_store()
        today = _now().strftime("%Y-%m-%d")
        ledger = store.get("art_ledger") or {}
        used = int(ledger.get("count", 0)) if ledger.get("date") == today else 0
        if used >= limit:
            return False
        store.set("art_ledger", {"date": today, "count": used + 1})
        return True

    async def _art(
        self,
        kind: str,
        group_id: str,
        start: datetime,
        inputs: dict[str, Any],
        persona: str,
        friends: list[partners.Partner],
        *,
        fresh: bool,
    ) -> str:
        """拿一张插画（data URI）。同一群同一周期画过就复用；定时那次会重画最新的。

        参考图顺序固定：先是她自己的形象图，再是这一期出现过的伙伴；提示词里按同样的顺序说明谁是谁。
        """
        if not self._option("illustration", True) or not inputs.get("scene"):
            return ""
        try:
            folder = self.get_artifact_dir() / "art"
        except RuntimeError:
            return ""
        period = start.strftime("%G-W%V") if kind == "week" else start.strftime("%Y-%m-%d")
        cache = folder / f"{kind}_{group_id}_{period}.img"
        if cache.is_file() and not fresh:
            return avatars.data_uri(cache.read_bytes())
        async with self._art_lock:
            if not self._take_art_quota():
                return avatars.data_uri(cache.read_bytes()) if cache.is_file() else ""
            own = self.ctx.engine.get_persona_reference_images(limit=2)
            loaded = partners.read_references(friends)
            references = [*own, *(image for _name, image in loaded)]
            prompt = writer.image_prompt(
                inputs["scene"],
                persona=persona,
                persona_refs=len(own),
                partners=[name for name, _image in loaded],
                story=inputs["story"],
                weather=inputs["weather"],
            )
            try:
                result = await asyncio.wait_for(
                    self.ctx.engine.generate_image(
                        prompt,
                        reference_images=references,
                        timeout_seconds=_ART_TIMEOUT,
                    ),
                    _ART_TIMEOUT + 10,
                )
            except Exception as exc:  # noqa: BLE001 - 画不出来就贴天气
                logger.warning("群聊手帐插画失败: %s", type(exc).__name__)
                return avatars.data_uri(cache.read_bytes()) if cache.is_file() else ""
        data = (result or {}).get("image_bytes") or b""
        uri = avatars.data_uri(data)
        if uri:
            folder.mkdir(parents=True, exist_ok=True)
            cache.write_bytes(data)
        return uri

    # ── 文字版（出图失败时）────────────────────────────────────────────

    @staticmethod
    def _text_version(report: dict[str, Any], names: dict[str, str]) -> str:
        people = report["people"]
        lines = [
            f"消息 {report['total']} 条 · {report['people_count']} 人冒泡 · "
            f"群聊天气 {metrics.WEATHERS.get(report['weather'], '多云')} {report['temperature']}°C"
        ]
        top = "、".join(
            f"{names.get(uid, uid)} {people[uid].count}" for uid in report["ranking"][:3]
        )
        if top:
            lines.append(f"话最多：{top}")
        for award in report["awards"][:4]:
            lines.append(f"{award['title']}：{award['name']}（{award['detail']}）")
        return "\n".join(lines)
