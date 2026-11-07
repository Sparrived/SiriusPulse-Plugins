"""群友图鉴：她给群友画收藏卡，大家每天抽一张。

第一期只有 R 常驻卡：每个在群里说话够多的人一张，卡面由她根据这个人在本群的
公开发言来写，属性来自真实数据。卡在后台慢慢画好（每次心跳最多几张、每天有上限），
抽卡本身不调用模型。
"""

from __future__ import annotations

import asyncio
import base64
import random
import time
from datetime import datetime, timezone
from pathlib import Path

from sirius_pulse.adapters.models import ImageSegment, MessageGroup, TextSegment
from sirius_pulse.extension_runtime import BackgroundTaskSpec
from sirius_pulse.plugins.api import CommandAST, PluginBase, PluginResponse, command

from .members import Member, compute_stats, gather_members, recent_lines
from .rules import PITY_AT, R, draw, has_high_rarity, local_today
from .store import Card, GachaStore
from .visual import render_card, render_collection
from .writer import build_prompt, parse_card_text

_HELP = (
    "群友图鉴\n"
    "/抽卡：每天一抽，抽到群友的收藏卡\n"
    "/图鉴：看看你收集了哪些卡\n"
    "/卡 <名字或编号>：看一张你已经抽到的卡\n"
    "/我的卡：看看我给你画的卡；/我的卡 退出 不想被画进图鉴，/我的卡 加入 回来"
)

_AVATAR_URL = "https://q1.qlogo.cn/g?b=qq&nk={uid}&s=640"
_AVATAR_TTL_SECONDS = 7 * 24 * 3600
_AVATAR_MAX_BYTES = 2 * 1024 * 1024

_DRAW_LINES = [
    "抽到了 {name}！「{title}」，这张是我画的，收好哦。",
    "是 {name}～「{title}」。画这张的时候我笑了好久。",
    "{name}来了！「{title}」——你觉得我画得像不像？",
    "噔噔——{name}，「{title}」。",
]
_DUPLICATE_LINES = [
    "又是 {name}？这已经是你的第 {copies} 张了，你们俩是不是有点缘分。",
    "{name}（第 {copies} 张）。这张你真的很缺吗……",
]
_SELF_LINES = [
    "抽到了你自己？！「{title}」……我当初就是这么看你的，不许生气。",
    "哈哈哈，是你自己。「{title}」，好好收藏这张自画像吧。",
]


def _now() -> datetime:
    """当前时间。测试固定时钟的入口。"""
    return datetime.now(timezone.utc)


class GachaPlugin(PluginBase):
    """群友图鉴：按群划分卡池，每天一抽，卡面由人格自己写。"""

    _plugin_name = "gacha"
    _plugin_display_name = "群友图鉴"
    _plugin_description = "她给群友画收藏卡：/抽卡 每天一抽，/图鉴 看收集进度，/我的卡 退出 随时不再入画。"
    _plugin_version = "0.1.0"
    _plugin_author = "Sirius Pulse"
    _plugin_min_framework_version = "1.3.0"
    _plugin_dependencies = ["httpx>=0.24.0", "Pillow>=9.0.0"]
    _plugin_permissions = {
        "developer_only": False,
        "hidden_from_intent": True,
        "rate_limit": {"calls_per_minute": 30, "calls_per_hour": 600},
    }
    _plugin_parameters = [
        {
            "name": "draws_per_day",
            "type": "int",
            "default": 1,
            "description": "每人每天在每个群能抽几次（按中国时间的自然日重置）。",
        },
        {
            "name": "min_messages",
            "type": "int",
            "default": 20,
            "description": "在群里至少说过多少句话，她才会给这个人画卡。",
        },
        {
            "name": "cards_per_beat",
            "type": "int",
            "default": 2,
            "description": "后台每次（约 10 分钟）最多新画几张卡；每张一次模型调用。",
        },
        {
            "name": "cards_per_day",
            "type": "int",
            "default": 20,
            "description": "她每天最多新画几张卡（整个人格合计）。",
        },
    ]
    _plugin_ui_schema = {
        "version": 1,
        "title": "群友图鉴",
        "description": "她只用群友在本群的公开发言写卡；私聊和别的群的内容不会出现在卡上。群友发 /我的卡 退出 即可删掉自己的卡。",
        "parameters": {
            "draws_per_day": {"label": "每人每天抽几次"},
            "min_messages": {"label": "发言多少句才入画"},
            "cards_per_beat": {"label": "每次心跳最多画几张"},
            "cards_per_day": {"label": "每天最多画几张"},
        },
    }

    def __init__(self) -> None:
        super().__init__()
        self._store: GachaStore | None = None
        self._mint_lock = asyncio.Lock()
        self._rng = random.Random()

    # ── 指令声明（匹配用；实际分发在 execute_async） ──────────────────

    @command(
        "抽卡",
        prefix="/",
        patterns=["抽卡"],
        render_mode="direct",
        description="每天一抽群友收藏卡。",
        hidden_from_intent=True,
        examples=["/抽卡"],
    )
    async def draw_card(self) -> PluginResponse:
        return PluginResponse.ok(text=_HELP)

    @command(
        "图鉴",
        prefix="/",
        patterns=["图鉴"],
        render_mode="direct",
        description="看自己的收集进度。",
        hidden_from_intent=True,
        examples=["/图鉴"],
    )
    async def collection(self) -> PluginResponse:
        return PluginResponse.ok(text=_HELP)

    @command(
        "卡",
        prefix="/",
        patterns=["卡"],
        render_mode="direct",
        description="看一张已经抽到的卡。",
        hidden_from_intent=True,
        examples=["/卡 阿明", "/卡 17"],
    )
    async def view_card(self) -> PluginResponse:
        return PluginResponse.ok(text=_HELP)

    @command(
        "我的卡",
        prefix="/",
        patterns=["我的卡"],
        render_mode="direct",
        description="看她给你画的卡，或退出/加入图鉴。",
        hidden_from_intent=True,
        examples=["/我的卡", "/我的卡 退出", "/我的卡 加入"],
    )
    async def my_card(self) -> PluginResponse:
        return PluginResponse.ok(text=_HELP)

    async def execute_async(self, cmd: CommandAST) -> list[PluginResponse]:
        group_id = self.ctx.message.group_id
        if not group_id or group_id.startswith("private_"):
            return [PluginResponse.fail("图鉴是按群收集的，请在群里玩。")]
        args = [str(arg.value) for arg in cmd.args]
        if args and args[0] in {"help", "帮助"}:
            return [PluginResponse.ok(text=_HELP)]
        handlers = {
            "抽卡": self._draw,
            "图鉴": self._collection,
            "卡": self._view,
            "我的卡": self._mine,
        }
        handler = handlers.get(cmd.command)
        if handler is None:
            return [PluginResponse.ok(text=_HELP)]
        try:
            return [await handler(group_id, " ".join(args).strip())]
        except OSError as exc:
            self.logger.warning("图鉴画图失败：%s", exc)
            return [PluginResponse.fail("画卡失败了，可能是缺少中文字体。")]

    # ── 抽卡 ──────────────────────────────────────────────────────────

    async def _draw(self, group_id: str, _arg: str) -> PluginResponse:
        store = self._gacha()
        owner = self._owner_id()
        today = local_today(_now())
        player = store.player(group_id, owner)
        per_day = self._option("draws_per_day", 1, low=1, high=20)
        if player.draws_left(today, per_day) <= 0:
            return PluginResponse.ok(text="今天已经抽过啦，明天再来。卡又不会跑掉的。")

        store.mark_playing(group_id, _now().isoformat())
        pool = store.cards(group_id)
        if not pool:
            async with self._mint_lock:
                pool = store.cards(group_id)
                if not pool:
                    await self._mint_some(group_id, limit=1)
                    pool = store.cards(group_id)
        result = draw(pool, self._rng, pity=player.pity)
        if result is None:
            return PluginResponse.ok(text="卡池还空着呢——我得先认识大家。群里多聊聊，我会慢慢给每个人画一张。")

        card = result.card
        copies = store.record_draw(
            group_id=group_id,
            owner_id=owner,
            card=card,
            today=today,
            pity_after=result.pity_after,
            now=_now().isoformat(),
        )
        if card.subject_id == owner:
            line = self._rng.choice(_SELF_LINES)
        elif copies > 1:
            line = self._rng.choice(_DUPLICATE_LINES)
        else:
            line = self._rng.choice(_DRAW_LINES)
        text = line.format(name=card.subject_name, title=card.title, copies=copies)
        if has_high_rarity(pool):
            # 只有卡池里真的有 SSR 以上时，保底才有意义，才提。
            text += f"（距离保底还有 {PITY_AT - result.pity_after} 抽）"
        png = await self._render(card)
        return self._image(text, png)

    # ── 图鉴与看卡 ────────────────────────────────────────────────────

    async def _collection(self, group_id: str, _arg: str) -> PluginResponse:
        store = self._gacha()
        pool = store.cards(group_id)
        if not pool:
            return PluginResponse.ok(text="这个群的图鉴还是空的。先 /抽卡 试试？")
        owned = store.collection(group_id, self._owner_id())
        png = await asyncio.to_thread(
            render_collection,
            owner_name=self._speaker_name(),
            owned=owned,
            pool=pool,
            persona_name=self._persona_name(),
        )
        return self._image(f"已收集 {len(owned)}/{len(pool)}", png)

    async def _view(self, group_id: str, query: str) -> PluginResponse:
        if not query:
            return PluginResponse.ok(text="想看哪张？用法：/卡 阿明 或 /卡 17")
        store = self._gacha()
        owner = self._owner_id()
        owned = store.collection(group_id, owner)
        mine = store.card_of(group_id, owner)
        candidates = [item.card for item in owned] + ([mine] if mine else [])
        number = query.lower().removeprefix("no.").lstrip("0")
        for card in candidates:
            if (number.isdigit() and card.no == int(number)) or card.subject_name == query:
                return self._image(f"{card.label} · {card.subject_name}", await self._render(card))
        in_pool = any(
            (number.isdigit() and card.no == int(number)) or card.subject_name == query
            for card in store.cards(group_id)
        )
        if in_pool:
            return PluginResponse.ok(text="这张你还没抽到哦，偷看可不行。")
        return PluginResponse.ok(text="图鉴里没有这张卡。")

    async def _mine(self, group_id: str, action: str) -> PluginResponse:
        store = self._gacha()
        owner = self._owner_id()
        now = _now().isoformat()
        if action in {"退出", "不要", "删除"}:
            removed = store.opt_out(group_id, owner, now)
            if removed:
                return PluginResponse.ok(text="好，我把你的卡收回来撕掉了，大家手里的那张也一起。以后不画你了。")
            return PluginResponse.ok(text="好，以后不画你。想回来的话发 /我的卡 加入。")
        if action in {"加入", "回来"}:
            if store.opt_in(group_id, owner):
                return PluginResponse.ok(text="欢迎回来！过一阵我会重新给你画一张。")
            return PluginResponse.ok(text="你本来就在图鉴里呀。")
        if action:
            return PluginResponse.ok(text="用法：/我的卡、/我的卡 退出、/我的卡 加入")
        if owner in store.opted_out(group_id):
            return PluginResponse.ok(text="你退出了图鉴，所以我没有画你。想回来的话发 /我的卡 加入。")
        card = store.card_of(group_id, owner)
        if card is None:
            return PluginResponse.ok(text="我还没给你画卡呢。在群里多聊聊，我会慢慢认识你的。")
        return self._image(f"我给你画的卡：「{card.title}」", await self._render(card))

    # ── 后台：慢慢给大家画卡 ──────────────────────────────────────────

    def create_background_tasks(self) -> list[BackgroundTaskSpec]:
        async def _beat() -> None:
            await self.mint_beat()

        return [BackgroundTaskSpec(name="gacha_mint", interval_seconds=600, task_func=_beat)]

    async def mint_beat(self) -> int:
        """给活跃、并且已经有人玩过的群里还没有卡的人画卡。返回这次画了几张。

        插件默认是启用的：只看"活跃"的话，部署后每个群都会被画，哪怕那里从没人
        想玩。所以一个群要有人 /抽卡 过，后台才去画那里的人。
        """
        if self._ctx is None:
            return 0
        made = 0
        budget = self._option("cards_per_beat", 2, low=0, high=10)
        playing = self._gacha().playing_groups()
        async with self._mint_lock:
            for group_id in self.ctx.get_active_groups():
                group_id = str(group_id)
                if made >= budget or group_id not in playing or group_id.startswith("private_"):
                    continue
                made += await self._mint_some(group_id, limit=budget - made)
        return made

    async def _mint_some(self, group_id: str, *, limit: int) -> int:
        """在今日额度内，给本群还没有卡的人画至多 limit 张。调用方持有 _mint_lock。"""
        store = self._gacha()
        engine = self.ctx.engine.get_engine()
        members = gather_members(
            engine, group_id, min_messages=self._option("min_messages", 20, low=1, high=10_000)
        )
        skip = store.subjects_with_cards(group_id) | store.opted_out(group_id)
        waiting = sorted(
            (m for m in members if m.user_id not in skip), key=lambda m: m.messages, reverse=True
        )
        made = 0
        # 按"尝试"计数而不是按"画成"计数：写坏的那位等下一次心跳，
        # 不然一次抽卡可能把全群挨个试一遍、调用一串模型。
        for member in waiting[:limit]:
            if not self._take_daily_quota():
                break
            if await self._mint(group_id, member, members):
                made += 1
        return made

    async def _mint(self, group_id: str, member: Member, everyone: list[Member]) -> Card | None:
        engine = self.ctx.engine.get_engine()
        recent_lines(engine, group_id, member)
        now = _now()
        stats = compute_stats(member, everyone, now)
        system, user = build_prompt(member, stats)
        try:
            reply = await self.ctx.engine.generate_raw(
                user,
                system_prompt=system,
                inject_persona=True,
                json_mode=True,
                task_name="plugin_generate",
            )
        except Exception as exc:
            self.logger.warning("给群友写卡面失败：%s", type(exc).__name__)
            return None
        text = parse_card_text(reply if isinstance(reply, str) else reply[1])
        if text is None:
            self.logger.info("卡面回复无法解析，这张下次再画")
            return None
        return self._gacha().add_card(
            group_id=group_id,
            subject_id=member.user_id,
            subject_name=member.name,
            subject_uid=member.uid,
            rarity=R,
            title=text.title,
            skill_name=text.skill_name,
            skill_text=text.skill_text,
            note=text.note,
            stats=stats,
            now=now.isoformat(),
        )

    def _take_daily_quota(self) -> bool:
        """每天画卡的总额度（整个人格），记在 PluginDataStore。"""
        data = self.get_data_store()
        today = local_today(_now())
        ledger = data.get("mint_ledger") or {}
        used = int(ledger.get("count", 0)) if ledger.get("date") == today else 0
        if used >= self._option("cards_per_day", 20, low=0, high=500):
            return False
        data.set("mint_ledger", {"date": today, "count": used + 1})
        return True

    # ── 小工具 ────────────────────────────────────────────────────────

    def _gacha(self) -> GachaStore:
        if self._store is None:
            self._store = GachaStore(self.get_artifact_dir() / "gacha.db")
        return self._store

    def _owner_id(self) -> str:
        message = self.ctx.message
        return str(message.user_id or message.channel_user_id or "")

    def _speaker_name(self) -> str:
        message = self.ctx.message
        return str(message.speaker_name or "") or "你"

    def _persona_name(self) -> str:
        return self.ctx.engine.get_persona_name() or "她"

    def _option(self, key: str, default: int, *, low: int, high: int) -> int:
        try:
            value = int(self.ctx.config.get(key, default))
        except (TypeError, ValueError):
            value = default
        return max(low, min(high, value))

    async def _render(self, card: Card) -> bytes:
        avatar = await self._avatar(card.subject_uid)
        return await asyncio.to_thread(
            render_card, card, avatar=avatar, persona_name=self._persona_name()
        )

    async def _avatar(self, uid: str) -> bytes | None:
        """群友的 QQ 头像（公开的那张），缓存一周；拿不到就画名字。"""
        if not uid.isdigit():
            return None
        cache = self.get_artifact_dir() / "avatars" / f"{uid}.img"
        if cache.is_file() and time.time() - cache.stat().st_mtime < _AVATAR_TTL_SECONDS:
            return cache.read_bytes()
        try:
            import httpx

            async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
                response = await client.get(_AVATAR_URL.format(uid=uid))
            if response.status_code != 200 or len(response.content) > _AVATAR_MAX_BYTES:
                return None
            data = response.content
        except Exception:
            return None
        _write_cache(cache, data)
        return data

    @staticmethod
    def _image(text: str, png: bytes) -> PluginResponse:
        # 有 message_group 时宿主只发它、不再发 text，所以台词要放进消息本身。
        image = ImageSegment(file_path="base64://" + base64.b64encode(png).decode("ascii"))
        return PluginResponse.ok(
            text=text,
            message_group=MessageGroup([TextSegment(text), image]),
            render_mode="direct",
        )


def _write_cache(path: Path, data: bytes) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    except OSError:
        pass
