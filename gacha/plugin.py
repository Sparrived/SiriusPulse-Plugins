"""群友图鉴：她给群友画收藏卡，大家每天抽一张，还能送、能换、能升星、能觉醒。

- R 常驻卡：每个在群里说话够多的人一张，卡面由她根据这个人在本群的公开发言来写；
- SR 心情卡：她心情好的时候，给已经有卡的人随手再画一张，换个角度；
- SSR 觉醒卡：三星卡可以觉醒，她专门为收藏者画一张插画，只此一份；
- UR 名场面卡：群里一段很多人一起说、情绪很高的十分钟，她觉得值得，就做成一张
  引用原话的限量卡，并配一张插画；
- 她心情好的时候大家手气也好一点。

**她的声音**：所有字都走宿主的对话生成链路（带完整人设），常说的话来自她每周自己
写的台词本，关键时刻（抽到 SR 以上、升星、觉醒画好）现场说。

卡都在后台慢慢画（每次心跳、每天都有上限），只在有人玩过的群里画；插画由另一个
后台任务一张张画，画好了她在群里叫人。

**画我**：插画里默认没有真实的人。卡上的人自己发 ``/我的卡 画我`` 同意、并且在框架的
视觉资产系统里登记了形象图，他的觉醒卡插画才照他本人的样子画——同意记成视觉资产
授权，画的时候走框架的受管人物生图入口（授权、额度、预检、隔离都在框架里）。
``/我的卡 不画我`` 或 ``退出`` 撤回：以后不再这样画，已经照他画好的插画立刻从卡上撤下。
"""

from __future__ import annotations

import asyncio
import base64
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sirius_pulse.adapters.models import ImageSegment, MessageGroup, TextSegment
from sirius_pulse.extension_runtime import BackgroundTaskSpec
from sirius_pulse.plugins.api import (
    CommandAST,
    PersonImageError,
    PluginBase,
    PluginResponse,
    command,
)

from .members import (
    Member,
    compute_stats,
    find_moment,
    gather_members,
    recent_lines,
    resolve_person,
)
from .rules import PITY_AT, RARITIES, SR, SSR, UR, R, draw, has_high_rarity, local_today
from .store import (
    KIND_AWAKENED,
    KIND_MEMBER,
    KIND_MOMENT,
    KIND_MOOD,
    MAX_STARS,
    Card,
    GachaStore,
    Owned,
    SoldOut,
)
from .visual import render_card, render_collection
from .voice import Script, build_script_prompt, parse_script
from .writer import (
    build_awaken_prompt,
    build_moment_prompt,
    build_prompt,
    build_reaction_prompt,
    build_scene_prompt,
    build_star_prompt,
    image_prompt,
    parse_awaken,
    parse_card_text,
    parse_line,
    parse_moment,
    parse_scene,
)

_HELP = (
    "群友图鉴\n"
    "/抽卡：每天一抽，抽到群友的收藏卡\n"
    "/图鉴：看看你收集了哪些卡\n"
    "/卡 <名字或编号>：看一张你已经抽到的卡\n"
    "/送卡 <人> <编号>：把一张卡送给别人\n"
    "/交换 <人> <我的编号> <他的编号>：提出交换，对方发 /同意交换 或 /拒绝交换\n"
    "/升星 <编号>：用一张重复的卡升星，我会在你那张上多写一句\n"
    "/觉醒 <编号>：三星卡觉醒成 SSR 超级卡，我专门为你画一张插画\n"
    "/我的卡：看看我给你画的卡；/我的卡 退出 不想被画进图鉴，/我的卡 加入 回来\n"
    "/我的卡 画我：同意我画觉醒插画时照你本人登记的形象画你；/我的卡 不画我 撤回"
)

# 受管人物生图没做成时，觉醒卡照旧用象征物画；这几种原因要在群里如实说一句。
_LIKENESS_FALLBACK_NOTES = {
    "quota_exceeded": "人物生图的额度用完了",
    "required_asset_missing": "还没有登记形象图",
    "asset_invalid": "登记的形象图停用了",
}

_AVATAR_URL = "https://q1.qlogo.cn/g?b=qq&nk={uid}&s=640"
_AVATAR_TTL_SECONDS = 7 * 24 * 3600
_AVATAR_MAX_BYTES = 2 * 1024 * 1024

#: 她的心情效价到这里算"心情好"（基线是 0.2）。
_GOOD_MOOD = 0.45
_OFFER_TTL = timedelta(minutes=10)
_MOOD_CARD_GAP = timedelta(days=30)
_MOMENT_GAP = timedelta(days=7)
_SOLD_OUT_RETRIES = 3
_ART_MAX_ATTEMPTS = 2
_ART_TIMEOUT_SECONDS = 300.0
_ART_REFERENCES = 2


def _now() -> datetime:
    """当前时间。测试固定时钟的入口。"""
    return datetime.now(timezone.utc)


class GachaPlugin(PluginBase):
    """群友图鉴：按群划分卡池，每天一抽，卡面和台词都由人格自己写。"""

    _plugin_name = "gacha"
    _plugin_display_name = "群友图鉴"
    _plugin_description = "她给群友画收藏卡：/抽卡 每天一抽，/图鉴 看收集进度，/送卡 /交换 /升星 /觉醒，" "/我的卡 退出 随时不再入画。"
    _plugin_version = "0.4.0"
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
            "description": "她每天最多新画几张卡（整个人格合计，含心情卡和名场面）。",
        },
        {
            "name": "moment_supply",
            "type": "int",
            "default": 3,
            "description": "每张名场面卡（UR）一共能被抽出几份。",
        },
        {
            "name": "art_per_day",
            "type": "int",
            "default": 3,
            "description": "她每天最多画几张插画（觉醒卡和名场面合计，每张一次生图）；0 表示不画。",
        },
    ]
    _plugin_ui_schema = {
        "version": 1,
        "title": "群友图鉴",
        "description": (
            "她只用群友在本群的公开发言写卡；私聊和别的群的内容不会出现在卡上。"
            "插画里默认不出现真实的人，群友只用象征物表示；只有本人发 /我的卡 画我 同意、"
            "并登记了形象图，她才照他本人的样子画觉醒插画（经视觉资产系统授权，随时可撤回）。"
            "群友发 /我的卡 退出 即可删掉自己的卡和自己出演的名场面。"
        ),
        "parameters": {
            "draws_per_day": {"label": "每人每天抽几次"},
            "min_messages": {"label": "发言多少句才入画"},
            "cards_per_beat": {"label": "每次心跳最多画几张"},
            "cards_per_day": {"label": "每天最多画几张"},
            "moment_supply": {"label": "名场面卡限量几份"},
            "art_per_day": {"label": "每天最多画几张插画"},
        },
    }

    def __init__(self) -> None:
        super().__init__()
        self._store: GachaStore | None = None
        self._mint_lock = asyncio.Lock()
        self._art_lock = asyncio.Lock()
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
        description="看她给你画的卡，退出/加入图鉴，或同意/撤回照你本人的样子画插画。",
        hidden_from_intent=True,
        examples=["/我的卡", "/我的卡 退出", "/我的卡 加入", "/我的卡 画我", "/我的卡 不画我"],
    )
    async def my_card(self) -> PluginResponse:
        return PluginResponse.ok(text=_HELP)

    @command(
        "送卡",
        prefix="/",
        patterns=["送卡"],
        render_mode="direct",
        description="把一张卡送给群友。",
        hidden_from_intent=True,
        examples=["/送卡 小红 17", "/送卡 @小红 17"],
    )
    async def gift_card(self) -> PluginResponse:
        return PluginResponse.ok(text=_HELP)

    @command(
        "交换",
        prefix="/",
        patterns=["交换"],
        render_mode="direct",
        description="向群友提出换卡。",
        hidden_from_intent=True,
        examples=["/交换 小红 17 32", "/交换 @小红 17 换 32"],
    )
    async def trade_card(self) -> PluginResponse:
        return PluginResponse.ok(text=_HELP)

    @command(
        "同意交换",
        prefix="/",
        patterns=["同意交换"],
        render_mode="direct",
        description="接受别人发给你的交换。",
        hidden_from_intent=True,
        examples=["/同意交换"],
    )
    async def accept_trade(self) -> PluginResponse:
        return PluginResponse.ok(text=_HELP)

    @command(
        "拒绝交换",
        prefix="/",
        patterns=["拒绝交换"],
        render_mode="direct",
        description="拒绝别人发给你的交换。",
        hidden_from_intent=True,
        examples=["/拒绝交换"],
    )
    async def reject_trade(self) -> PluginResponse:
        return PluginResponse.ok(text=_HELP)

    @command(
        "升星",
        prefix="/",
        patterns=["升星"],
        render_mode="direct",
        description="用重复的卡升星，解锁她追加的一句话。",
        hidden_from_intent=True,
        examples=["/升星 17"],
    )
    async def star_card(self) -> PluginResponse:
        return PluginResponse.ok(text=_HELP)

    @command(
        "觉醒",
        prefix="/",
        patterns=["觉醒"],
        render_mode="direct",
        description="三星卡觉醒成 SSR 超级卡，她专门为你画一张插画。",
        hidden_from_intent=True,
        examples=["/觉醒 17"],
    )
    async def awaken_card(self) -> PluginResponse:
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
            "送卡": self._gift,
            "交换": self._trade,
            "同意交换": self._accept,
            "拒绝交换": self._reject,
            "升星": self._star,
            "觉醒": self._awaken,
        }
        handler = handlers.get(cmd.command)
        if handler is None:
            return [PluginResponse.ok(text=_HELP)]
        try:
            return [await handler(group_id, args)]
        except OSError as exc:
            self.logger.warning("图鉴画图失败：%s", exc)
            return [PluginResponse.fail("画卡失败了，可能是缺少中文字体。")]

    # ── 抽卡 ──────────────────────────────────────────────────────────

    async def _draw(self, group_id: str, _args: list[str]) -> PluginResponse:
        store = self._gacha()
        owner = self._owner_id()
        today = local_today(_now())
        player = store.player(group_id, owner)
        per_day = self._option("draws_per_day", 1, low=1, high=20)
        if player.draws_left(today, per_day) <= 0:
            return PluginResponse.ok(text=self._line("drawn_today"))

        store.mark_playing(group_id, _now().isoformat())
        pool = store.cards(group_id)
        if not pool:
            async with self._mint_lock:
                pool = store.cards(group_id)
                if not pool:
                    await self._mint_some(group_id, limit=1)
                    pool = store.cards(group_id)
        lucky = self._lucky()
        for _ in range(_SOLD_OUT_RETRIES):
            result = draw(pool, self._rng, pity=player.pity, lucky=lucky)
            if result is None:
                return PluginResponse.ok(text=self._line("pool_empty"))
            try:
                copies = store.record_draw(
                    group_id=group_id,
                    owner_id=owner,
                    card=result.card,
                    today=today,
                    pity_after=result.pity_after,
                    now=_now().isoformat(),
                )
                break
            except SoldOut:
                # 刚好被别人抽走了最后一份：换一张重抽，这次不扣次数。
                pool = store.cards(group_id)
        else:
            return PluginResponse.ok(text="卡池正忙，等一下再抽吧。")

        card = result.card
        text = await self._draw_line(group_id, card, copies)
        if lucky:
            text += self._line("lucky")
        if has_high_rarity(pool):
            # 只有卡池里真的还有抽得到的 SSR 以上时，保底才有意义，才提。
            text += f"（距离保底还有 {PITY_AT - result.pity_after} 抽）"
        return self._image(
            text, await self._render(card, store.owned(group_id, owner, card.card_id))
        )

    async def _draw_line(self, group_id: str, card: Card, copies: int) -> str:
        """这一抽她说什么：SR 以上的新卡她现场说，其余从她的台词本里挑。"""
        if copies == 1 and RARITIES.index(card.rarity) >= RARITIES.index(SR):
            said = await self._say(
                build_reaction_prompt(
                    drawer=self._speaker_name(),
                    card_name=card.name,
                    title=card.title,
                    rarity=card.rarity,
                ),
                group_id,
            )
            if said:
                return said
        if card.kind != KIND_MOMENT and card.subject_id == self._owner_id():
            return self._line("draw_self", title=card.title)
        if copies > 1:
            return self._line("draw_duplicate", name=card.name, copies=copies)
        return self._line("draw_new", name=card.name, title=card.title)

    # ── 图鉴与看卡 ────────────────────────────────────────────────────

    async def _collection(self, group_id: str, _args: list[str]) -> PluginResponse:
        store = self._gacha()
        owner = self._owner_id()
        pool = store.visible_cards(group_id, owner)
        if not pool:
            return PluginResponse.ok(text="这个群的图鉴还是空的。先 /抽卡 试试？")
        owned = store.collection(group_id, owner)
        png = await asyncio.to_thread(
            render_collection,
            owner_name=self._speaker_name(),
            owned=owned,
            pool=pool,
            persona_name=self._persona_name(),
        )
        return self._image(f"已收集 {len(owned)}/{len(pool)}", png)

    async def _view(self, group_id: str, args: list[str]) -> PluginResponse:
        query = " ".join(args).strip()
        if not query:
            return PluginResponse.ok(text="想看哪张？用法：/卡 阿明 或 /卡 17")
        store = self._gacha()
        owner = self._owner_id()
        owned = {item.card.card_id: item for item in store.collection(group_id, owner)}
        mine = store.card_of(group_id, owner)
        candidates = [item.card for item in owned.values()] + ([mine] if mine else [])
        for card in candidates:
            if _matches(card, query):
                return self._image(
                    f"{card.label} · {card.name}", await self._render(card, owned.get(card.card_id))
                )
        if any(_matches(card, query) for card in store.visible_cards(group_id, owner)):
            return PluginResponse.ok(text="这张你还没抽到哦，偷看可不行。")
        return PluginResponse.ok(text="图鉴里没有这张卡。")

    async def _mine(self, group_id: str, args: list[str]) -> PluginResponse:
        action = " ".join(args).strip()
        store = self._gacha()
        owner = self._owner_id()
        now = _now().isoformat()
        if action in {"退出", "不要", "删除"}:
            # 先把插件这边的同意和照他画的插画处理掉，再去框架撤授权（那一步有 await）。
            doomed = [card for card in store.cards(group_id) if card.involves(owner)]
            store.opt_out(group_id, owner, now)
            self._forget_art(doomed)
            await self._revoke_likeness(group_id, owner)
            return PluginResponse.ok(text=self._line("opted_out"))
        if action in {"加入", "回来"}:
            if store.opt_in(group_id, owner):
                return PluginResponse.ok(text=self._line("opted_in"))
            return PluginResponse.ok(text="你本来就在图鉴里呀。")
        if action in {"画我", "照我画"}:
            return await self._consent_likeness(group_id, owner)
        if action in {"不画我", "别画我"}:
            return await self._withdraw_likeness(group_id, owner)
        if action:
            return PluginResponse.ok(text="用法：/我的卡、/我的卡 退出、/我的卡 加入、/我的卡 画我、/我的卡 不画我")
        if owner in store.opted_out(group_id):
            return PluginResponse.ok(text="你退出了图鉴，所以我没有画你。想回来的话发 /我的卡 加入。")
        card = store.card_of(group_id, owner)
        if card is None:
            return PluginResponse.ok(text=self._line("no_card_yet"))
        caption = f"我给你画的卡：「{card.title}」"
        if store.likeness_consent(group_id, owner):
            caption += "（你同意了觉醒插画照你本人的样子画，/我的卡 不画我 可以撤回）"
        return self._image(
            caption, await self._render(card, store.owned(group_id, owner, card.card_id))
        )

    # ── 画我：同意与撤回 ──────────────────────────────────────────────

    async def _consent_likeness(self, group_id: str, owner: str) -> PluginResponse:
        """本人同意：用他这条消息的来源证据在视觉资产系统里记一条授权。"""
        store = self._gacha()
        if owner in store.opted_out(group_id):
            return PluginResponse.ok(text="你退出了图鉴，我不会画你。想回来先发 /我的卡 加入。")
        try:
            consent = self.ctx.engine.grant_person_image_consent(
                user_id=owner, group_id=group_id, evidence_ref=self.ctx.message.evidence_ref
            )
        except PersonImageError as exc:
            self.logger.info("「画我」没有记下：%s", exc.code)
            if exc.code == "unavailable":
                return PluginResponse.ok(text="这里还没接上形象登记，我暂时没法照你本人的样子画。")
            return PluginResponse.ok(text="我没法确认这句话是你本人发的，过一会儿再说一次吧。")
        store.set_likeness_consent(group_id, owner, consent["grant_id"], _now().isoformat())
        text = self._line("likeness_on")
        if not consent.get("has_reference"):
            text += "（你还没在我这里登记过形象图，登记之前插画里还是用象征物代表你。）"
        return PluginResponse.ok(text=text + "不想了随时发 /我的卡 不画我。")

    async def _withdraw_likeness(self, group_id: str, owner: str) -> PluginResponse:
        store = self._gacha()
        had = bool(store.likeness_consent(group_id, owner))
        taken_down = self._take_down_likeness(group_id, owner)
        revoked = await self._revoke_likeness(group_id, owner)
        if not (had or taken_down or revoked):
            return PluginResponse.ok(text="我本来就没有照你本人的样子画你呀。")
        text = self._line("likeness_off")
        if taken_down:
            text += f"（已经照你画好的 {taken_down} 张插画也撤下了，卡还在。）"
        return PluginResponse.ok(text=text)

    def _take_down_likeness(self, group_id: str, subject_id: str) -> int:
        """撤回同意：插件这边的同意删掉，照他本人画的插画从卡上撤下、文件删掉。

        全程没有 await：正在画的那张在保存前会再查一次同意，不会在这之后又挂上去。
        """
        store = self._gacha()
        store.clear_likeness_consent(group_id, subject_id)
        cards = store.likeness_art(group_id, subject_id)
        self._forget_art(cards)
        for card in cards:
            store.set_art(card.card_id, "")
        return len(cards)

    async def _revoke_likeness(self, group_id: str, subject_id: str) -> int:
        """在框架的视觉资产系统里撤回授权（排队中的取消、已画的结果隔离）。"""
        try:
            result = await self.ctx.engine.revoke_person_image_consent(
                user_id=subject_id, group_id=group_id
            )
        except PersonImageError:
            return 0
        return len(result.get("revoked") or [])

    def _forget_art(self, cards: list[Card]) -> None:
        """删掉这些卡的插画文件和发到群里的那张卡面图。"""
        folder = self.get_artifact_dir()
        for card in cards:
            paths = [folder / "announce" / f"{card.card_id}.png"]
            if card.art:
                paths.append(folder / "art" / card.art)
            for path in paths:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    self.logger.warning("插画文件删不掉：%s", path.name)

    # ── 送卡、交换、升星 ──────────────────────────────────────────────

    async def _gift(self, group_id: str, args: list[str]) -> PluginResponse:
        if len(args) != 2:
            return PluginResponse.ok(text="用法：/送卡 <人> <编号>，比如 /送卡 小红 17")
        target = self._person(group_id, args[0])
        if target is None:
            return PluginResponse.ok(text=f"我不知道「{args[0]}」是群里的谁，换个名字或者直接 @ 他试试。")
        owner = self._owner_id()
        if target[0] == owner:
            return PluginResponse.ok(text="送给自己？这张本来就是你的呀。")
        store = self._gacha()
        item = self._find_owned(group_id, owner, args[1])
        if item is None:
            return PluginResponse.ok(text=f"你手里没有 {args[1]} 这张卡。")
        if item.card.kind == KIND_AWAKENED:
            return PluginResponse.ok(text="觉醒卡是专门画给你的，不能送人哦。")
        store.give(
            group_id=group_id,
            from_id=owner,
            to_id=target[0],
            card_id=item.card.card_id,
            now=_now().isoformat(),
        )
        return PluginResponse.ok(text=self._line("gift_done", card=_label(item.card), to=target[1]))

    async def _trade(self, group_id: str, args: list[str]) -> PluginResponse:
        args = [arg for arg in args if arg != "换"]
        if len(args) != 3:
            return PluginResponse.ok(text="用法：/交换 <人> <我的编号> <他的编号>，比如 /交换 小红 17 32")
        target = self._person(group_id, args[0])
        if target is None:
            return PluginResponse.ok(text=f"我不知道「{args[0]}」是群里的谁，换个名字或者直接 @ 他试试。")
        owner = self._owner_id()
        if target[0] == owner:
            return PluginResponse.ok(text="自己跟自己换？我看不懂，但我大受震撼。")
        give = self._find_owned(group_id, owner, args[1])
        if give is None:
            return PluginResponse.ok(text=f"你手里没有 {args[1]} 这张卡。")
        want = self._find_owned(group_id, target[0], args[2])
        if want is None:
            return PluginResponse.ok(text=f"{target[1]} 手里没有 {args[2]} 这张卡。")
        if KIND_AWAKENED in {give.card.kind, want.card.kind}:
            return PluginResponse.ok(text="觉醒卡是专门画给收藏者的，不能拿来换。")
        self._gacha().offer(
            group_id=group_id,
            from_id=owner,
            from_name=self._speaker_name(),
            to_id=target[0],
            give_card=give.card.card_id,
            want_card=want.card.card_id,
            now=_now().isoformat(),
        )
        minutes = int(_OFFER_TTL.total_seconds() // 60)
        said = self._line(
            "trade_offer",
            who=self._speaker_name(),
            give=_label(give.card),
            to=target[1],
            want=_label(want.card),
        )
        # 怎么同意由系统补上：这是规则，不交给台词本。
        return PluginResponse.ok(text=f"{said}\n{target[1]}，{minutes} 分钟内发 /同意交换 或 /拒绝交换。")

    async def _accept(self, group_id: str, _args: list[str]) -> PluginResponse:
        store = self._gacha()
        offer = store.pending_offer(group_id, self._owner_id(), now=_now(), ttl=_OFFER_TTL)
        if offer is None:
            return PluginResponse.ok(text="现在没有人要跟你换卡（或者已经过期了）。")
        give, want = store.card(group_id, offer.give_card), store.card(group_id, offer.want_card)
        if give is None or want is None or not store.swap(offer, _now().isoformat()):
            return PluginResponse.ok(text=self._line("trade_failed"))
        return PluginResponse.ok(
            text=self._line("trade_done", who=offer.from_name, give=_label(give), want=_label(want))
        )

    async def _reject(self, group_id: str, _args: list[str]) -> PluginResponse:
        store = self._gacha()
        offer = store.pending_offer(group_id, self._owner_id(), now=_now(), ttl=_OFFER_TTL)
        if offer is None:
            return PluginResponse.ok(text="现在没有人要跟你换卡。")
        store.close_offer(offer.offer_id)
        return PluginResponse.ok(text=self._line("trade_rejected", who=offer.from_name))

    async def _star(self, group_id: str, args: list[str]) -> PluginResponse:
        if len(args) != 1:
            return PluginResponse.ok(text="用法：/升星 <编号>，比如 /升星 17")
        owner = self._owner_id()
        item = self._find_owned(group_id, owner, args[0])
        if item is None:
            return PluginResponse.ok(text=f"你手里没有 {args[0]} 这张卡。")
        if item.stars >= MAX_STARS:
            return PluginResponse.ok(text=f"这张已经 {MAX_STARS} 星了，满了满了。可以试试 /觉醒。")
        if item.copies < 2:
            return PluginResponse.ok(text="升星要用一张重复的卡，你这张只有一份。")
        card = item.card
        note = await self._say(
            build_star_prompt(
                owner=self._speaker_name(),
                card_name=card.name,
                title=card.title,
                note=card.note,
                stars=item.stars + 1,
            ),
            group_id,
        )
        if not note:
            # 她没写成，就不扣卡：升星的意义就是这一句话。
            return PluginResponse.ok(text="我一时没想好要写什么……等会儿再来升星吧，卡没扣。")
        stars = self._gacha().star_up(
            group_id=group_id, owner_id=owner, card_id=card.card_id, note=note
        )
        if not stars:
            return PluginResponse.ok(text="升星没成功，卡没扣，再试一次吧。")
        upgraded = self._gacha().owned(group_id, owner, card.card_id)
        return self._image(
            f"{card.label} 升到 {stars} 星！我在你那张上多写了一句：{note}",
            await self._render(card, upgraded),
        )

    # ── 觉醒：三星卡变成她专门画的超级卡 ──────────────────────────────

    async def _awaken(self, group_id: str, args: list[str]) -> PluginResponse:
        if len(args) != 1:
            return PluginResponse.ok(text="用法：/觉醒 <编号>，比如 /觉醒 17")
        if self._option("art_per_day", 3, low=0, high=50) <= 0:
            return PluginResponse.ok(text="我现在不画插画，觉醒先等等吧。")
        owner = self._owner_id()
        store = self._gacha()
        item = self._find_owned(group_id, owner, args[0])
        if item is None:
            return PluginResponse.ok(text=f"你手里没有 {args[0]} 这张卡。")
        card = item.card
        if card.kind not in {KIND_MEMBER, KIND_MOOD}:
            return PluginResponse.ok(text="只有人物卡能觉醒。")
        if item.stars < MAX_STARS:
            return PluginResponse.ok(text=f"要先升到 {MAX_STARS} 星才能觉醒，这张现在 {item.stars} 星。")
        if store.awakened_from(group_id, owner, card.card_id) is not None:
            return PluginResponse.ok(text="这张已经觉醒过了，觉醒卡就在你的图鉴里。")
        if store.pending_art_job(group_id, owner, card.card_id):
            return PluginResponse.ok(text="我还在画呢，别催～画好了叫你。")
        store.add_art_job(
            group_id=group_id,
            kind=KIND_AWAKENED,
            card_id=card.card_id,
            owner_id=owner,
            owner_name=self._speaker_name(),
            now=_now().isoformat(),
        )
        return PluginResponse.ok(text=self._line("awaken_start", card=_label(card)))

    # ── 后台：慢慢给大家画卡、画插画、写台词本 ────────────────────────

    def create_background_tasks(self) -> list[BackgroundTaskSpec]:
        async def _beat() -> None:
            await self.mint_beat()

        async def _art() -> None:
            await self.art_beat()

        return [
            BackgroundTaskSpec(name="gacha_mint", interval_seconds=600, task_func=_beat),
            BackgroundTaskSpec(name="gacha_art", interval_seconds=30, task_func=_art),
        ]

    async def mint_beat(self) -> int:
        """一次后台心跳：先给还没有卡的人画常驻卡，再看心情画心情卡，最后找名场面。

        只在活跃、并且已经有人玩过的群里画：插件默认是启用的，只看"活跃"的话，
        部署后每个群都会被画，哪怕那里从没人想玩。返回这次一共画了几张。
        """
        if self._ctx is None:
            return 0
        made = 0
        budget = self._option("cards_per_beat", 2, low=0, high=10)
        playing = self._gacha().playing_groups()
        groups = [
            str(group_id)
            for group_id in self.ctx.get_active_groups()
            if str(group_id) in playing and not str(group_id).startswith("private_")
        ]
        if groups:
            await self._refresh_script()
        async with self._mint_lock:
            for group_id in groups:
                if made >= budget:
                    break
                made += await self._mint_some(group_id, limit=budget - made)
            if made < budget and self._lucky():
                for group_id in groups:
                    if await self._mint_mood(group_id):
                        made += 1
                        break
            for group_id in groups:
                if made >= budget:
                    break
                if await self._mint_moment(group_id):
                    made += 1
        return made

    async def _mint_some(self, group_id: str, *, limit: int) -> int:
        """在今日额度内，给本群还没有卡的人画至多 limit 张。调用方持有 _mint_lock。"""
        store = self._gacha()
        members = self._members(group_id)
        skip = store.subjects_with_cards(group_id) | store.opted_out(group_id)
        waiting = sorted(
            (m for m in members if m.user_id not in skip), key=lambda m: m.messages, reverse=True
        )
        made = 0
        # 按"尝试"计数而不是按"画成"计数：写坏的那位等下一次心跳，
        # 不然一次抽卡可能把全群挨个试一遍、调用一串模型。
        for member in waiting[:limit]:
            if not self._take_quota("mint_ledger", "cards_per_day", 20):
                break
            if await self._paint(group_id, member, members):
                made += 1
        return made

    async def _mint_mood(self, group_id: str) -> Card | None:
        """她心情好：给本群一位已经有常驻卡的人再画一张心情卡（SR）。

        每个群每天最多一张，同一个人 30 天内最多一张。
        """
        store = self._gacha()
        now = _now()
        last = _parse(store.last_minted(group_id, KIND_MOOD))
        if last is not None and local_today(last) == local_today(now):
            return None
        members = self._members(group_id)
        left_out = store.opted_out(group_id)
        candidates = []
        for member in members:
            base = store.card_of(group_id, member.user_id)
            if base is None or member.user_id in left_out:
                continue
            previous = _parse(store.last_minted(group_id, KIND_MOOD, member.user_id))
            if previous is not None and now - previous < _MOOD_CARD_GAP:
                continue
            candidates.append((member, base))
        if not candidates or not self._take_quota("mint_ledger", "cards_per_day", 20):
            return None
        member, base = self._rng.choice(candidates)
        return await self._paint(group_id, member, members, previous_title=base.title)

    async def _mint_moment(self, group_id: str) -> Card | None:
        """找过去一天里最热闹的十分钟，她觉得是名场面就做成限量 UR 卡，再排队配插画。

        每个群每天最多判断一次，一周最多发一张。
        """
        store = self._gacha()
        now = _now()
        last = _parse(store.last_minted(group_id, KIND_MOMENT))
        if last is not None and now - last < _MOMENT_GAP:
            return None
        data = self.get_data_store()
        checks = dict(data.get("moment_checks") or {})
        today = local_today(now)
        if checks.get(group_id) == today:
            return None
        engine = self.ctx.engine.get_engine()
        moment = find_moment(engine, group_id, now, left_out=store.opted_out(group_id))
        if moment is None:
            return None
        # 找到了候选才记一次"今天判断过"，并且先记账再调用：失败也不在每次心跳上重试。
        checks[group_id] = today
        data.set("moment_checks", checks)
        if not self._take_quota("mint_ledger", "cards_per_day", 20):
            return None
        system, user = build_moment_prompt(moment)
        text = parse_moment(
            await self._generate(user, system, group_id), line_count=len(moment.lines)
        )
        if text is None:
            return None
        quoted = moment.lines[text.line]
        cast = moment.cast
        card = store.add_card(
            group_id=group_id,
            subject_id=quoted.user_id,
            subject_name=quoted.name or "群友",
            subject_uid=quoted.uid,
            rarity=UR,
            title=text.title,
            skill_name=text.skill_name,
            skill_text=text.skill_text,
            note=text.note,
            stats={},
            now=moment.start.isoformat(),
            kind=KIND_MOMENT,
            quote=quoted.text,
            cast_ids=[user_id for user_id, _ in cast],
            cast_names=[name for _, name in cast],
            supply=self._option("moment_supply", 3, low=1, high=100),
        )
        if self._option("art_per_day", 3, low=0, high=50) > 0:
            store.add_art_job(
                group_id=group_id, kind=KIND_MOMENT, card_id=card.card_id, now=_now().isoformat()
            )
        return card

    async def _paint(
        self, group_id: str, member: Member, everyone: list[Member], *, previous_title: str = ""
    ) -> Card | None:
        """给一个人写一张卡：没有 previous_title 是常驻卡（R），有就是心情卡（SR）。"""
        engine = self.ctx.engine.get_engine()
        recent_lines(engine, group_id, member)
        now = _now()
        stats = compute_stats(member, everyone, now)
        system, user = build_prompt(member, stats, previous_title=previous_title)
        text = parse_card_text(await self._generate(user, system, group_id))
        if text is None:
            self.logger.info("卡面回复无法解析，这张下次再画")
            return None
        return self._gacha().add_card(
            group_id=group_id,
            subject_id=member.user_id,
            subject_name=member.name,
            subject_uid=member.uid,
            rarity=SR if previous_title else R,
            title=text.title,
            skill_name=text.skill_name,
            skill_text=text.skill_text,
            note=text.note,
            stats=stats,
            now=now.isoformat(),
            kind=KIND_MOOD if previous_title else KIND_MEMBER,
        )

    # ── 插画 ──────────────────────────────────────────────────────────

    async def art_beat(self) -> bool:
        """画一张排着队的插画（觉醒卡或名场面）。返回这次有没有画成。

        一次只画一张；当天插画额度用完就等到明天，不算失败。每张最多试两次。
        """
        if self._ctx is None or self._art_lock.locked():
            return False
        async with self._art_lock:
            store = self._gacha()
            job = store.next_art_job()
            if job is None:
                return False
            base = store.card(job["group_id"], job["card_id"])
            if base is None:
                # 卡已经不在了（比如本人退出了图鉴）：这张不画了。
                store.finish_art_job(job["job_id"], "dropped")
                return False
            if not self._take_quota("art_ledger", "art_per_day", 3, high=50):
                return False
            try:
                if job["kind"] == KIND_AWAKENED:
                    done = await self._draw_awakened(job, base)
                else:
                    done = await self._draw_moment(job, base)
            except Exception as exc:
                self.logger.warning("画插画失败：%s", type(exc).__name__)
                done = False
            if done:
                store.finish_art_job(job["job_id"], "done")
                return True
            if store.art_job_tried(job["job_id"]) >= _ART_MAX_ATTEMPTS:
                store.finish_art_job(job["job_id"], "failed")
                if job["kind"] == KIND_AWAKENED:
                    await self.ctx.dispatch_proactive_message(
                        group_id=job["group_id"],
                        text=f"{job['owner_name']}，{_label(base)} 的觉醒插画我画了两次都没画好……"
                        "过一会儿再 /觉醒 一次吧，什么都没扣。",
                        event_id=f"gacha-art-failed:{job['job_id']}",
                    )
            return False

    async def _draw_awakened(self, job: dict[str, Any], base: Card) -> bool:
        group_id = job["group_id"]
        store = self._gacha()
        subject = base.subject_id
        # 同意过、并且现在还在图鉴里，才试着照他本人画；有没有形象图由框架判断。
        likeness = bool(store.likeness_consent(group_id, subject)) and (
            subject not in store.opted_out(group_id)
        )
        reply = await self._generate(
            "（觉醒这张卡）",
            build_awaken_prompt(
                owner=job["owner_name"] or "他",
                card_name=base.name,
                title=base.title,
                skill=base.skill_name,
                note=base.note,
                likeness=likeness,
            ),
            group_id,
        )
        text = parse_awaken(reply)
        if text is None:
            return False
        note = ""
        art: bytes | None = None
        if likeness:
            art, note = await self._paint_likeness(text.scene, base)
            # 画的这几分钟里他可能撤回了：保存前再查一次（到保存都没有 await）。
            if art is not None and not store.likeness_consent(group_id, subject):
                art, note = None, ""
        drew_likeness = art is not None
        if art is None:
            art = await self._paint_art(text.scene)
        now = _now().isoformat()
        card = store.add_card(
            group_id=group_id,
            subject_id=base.subject_id,
            subject_name=base.subject_name,
            subject_uid=base.subject_uid,
            rarity=SSR,
            title=text.title,
            skill_name=text.skill_name,
            skill_text=text.skill_text,
            note=text.note,
            stats=dict(base.stats),
            now=now,
            kind=KIND_AWAKENED,
            cast_names=[job["owner_name"]],
            supply=1,
            issued=1,
            base_card=base.card_id,
        )
        self._save_art(card, art, likeness=drew_likeness)
        store.grant(group_id=group_id, owner_id=job["owner_id"], card_id=card.card_id, now=now)
        card = store.card(group_id, card.card_id) or card
        said = await self._say(
            build_reaction_prompt(
                drawer=job["owner_name"] or "他",
                card_name=card.name,
                title=card.title,
                rarity=SSR,
            ),
            group_id,
        )
        line = said or f"{job['owner_name']}，你的觉醒卡画好了：「{card.title}」。"
        if note:
            line += f"（这次没能照 {base.subject_name} 本人的样子画：{note}，先用象征物代替。）"
        await self._announce(
            group_id, line, card, store.owned(group_id, job["owner_id"], card.card_id)
        )
        return True

    async def _draw_moment(self, job: dict[str, Any], card: Card) -> bool:
        group_id = job["group_id"]
        scene = parse_scene(
            await self._generate(
                "（画这张名场面）",
                build_scene_prompt(title=card.title, quote=card.quote, cast=len(card.cast_names)),
                group_id,
            )
        )
        if not scene:
            return False
        art = await self._paint_art(scene)
        self._save_art(card, art)
        card = self._gacha().card(group_id, card.card_id) or card
        await self._announce(group_id, f"名场面「{card.title}」入卡池了，限量 {card.supply} 份。", card, None)
        return True

    async def _paint_art(self, scene: str) -> bytes:
        references = self.ctx.engine.get_persona_reference_images(limit=_ART_REFERENCES)
        result = await self.ctx.engine.generate_image(
            image_prompt(scene, persona_name=self._persona_name(), has_reference=bool(references)),
            reference_images=references,
            timeout_seconds=_ART_TIMEOUT_SECONDS,
        )
        data = result.get("image_bytes") or b""
        if not data:
            raise ValueError("生图没有返回图片")
        return data

    async def _paint_likeness(self, scene: str, base: Card) -> tuple[bytes | None, str]:
        """照卡上这个人本人的样子画：走框架的受管人物生图（同意、额度、预检、隔离都在框架）。

        返回 (插画, 要如实告诉大家的原因)。没画成就是 (None, 原因)，调用方改用象征物画；
        撤回、没同意这类本人自己的选择不用在群里说。网络之类的故障照常抛出，算一次失败。
        """
        try:
            result = await self.ctx.engine.generate_person_image(
                image_prompt(
                    scene,
                    persona_name=self._persona_name(),
                    has_reference=True,
                    likeness_name=base.subject_name,
                ),
                user_id=base.subject_id,
                group_id=base.group_id,
                persona_references=_ART_REFERENCES,
                timeout_seconds=_ART_TIMEOUT_SECONDS,
            )
        except PersonImageError as exc:
            self.logger.info("没能照本人画觉醒插画：%s", exc.code)
            if exc.code in {"consent_required", "cancelled_by_revocation"}:
                # 授权在框架那边已经不在了（比如在别处撤回）：插件这边的同意也作废。
                self._gacha().clear_likeness_consent(base.group_id, base.subject_id)
                return None, ""
            return None, _LIKENESS_FALLBACK_NOTES.get(exc.code, "这次没法核验授权")
        data = result.get("image_bytes") or b""
        if not data:
            raise ValueError("生图没有返回图片")
        return data, ""

    def _save_art(self, card: Card, art: bytes, *, likeness: bool = False) -> None:
        folder = self.get_artifact_dir() / "art"
        folder.mkdir(parents=True, exist_ok=True)
        name = f"{card.card_id}.img"
        (folder / name).write_bytes(art)
        self._gacha().set_art(card.card_id, name, likeness=likeness)

    async def _announce(self, group_id: str, text: str, card: Card, owned: Owned | None) -> None:
        """插画画好了：在群里说一声，附上新的卡面。"""
        png = await self._render(card, owned)
        folder = self.get_artifact_dir() / "announce"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{card.card_id}.png"
        path.write_bytes(png)
        await self.ctx.dispatch_proactive_message(
            group_id=group_id,
            text=text,
            image_path=str(path),
            event_id=f"gacha-art:{card.card_id}",
        )

    # ── 她的声音 ──────────────────────────────────────────────────────

    async def _generate(self, user: str, system: str, group_id: str = "") -> str:
        """让她写点东西。走宿主的对话生成链路，人设、说话方式都和聊天时一样。"""
        try:
            reply = await self.ctx.engine.generate_text(
                system,
                group_id=group_id,
                messages=[{"role": "user", "content": user}],
                task_name="plugin_generate",
            )
        except Exception as exc:
            self.logger.warning("图鉴的模型调用失败：%s", type(exc).__name__)
            return ""
        return str(reply or "")

    async def _say(self, system: str, group_id: str = "") -> str:
        """让她现场说一句话。失败就是空串。"""
        return parse_line(await self._generate("（说一句）", system, group_id))

    def _script(self) -> Script:
        return Script.from_dict(self.get_data_store().get("script"))

    def _line(self, occasion: str, **values: Any) -> str:
        """常说的话：从她的台词本里挑一句；她还没写的场合用保底句。"""
        return self._script().say(occasion, self._rng, **values)

    async def _refresh_script(self) -> None:
        """台词本一周一换；写失败了当天不再重试。"""
        data = self.get_data_store()
        now = _now()
        if not self._script().stale(now) or data.get("script_tried") == local_today(now):
            return
        data.set("script_tried", local_today(now))
        system, user = build_script_prompt()
        script = parse_script(await self._generate(user, system), now)
        if script is not None:
            data.set("script", script.to_dict())

    def _lucky(self) -> bool:
        """她现在心情好吗。"""
        emotion = getattr(self.ctx.engine.get_engine(), "assistant_emotion", None)
        try:
            return float(getattr(emotion, "valence", 0.0)) >= _GOOD_MOOD
        except (TypeError, ValueError):
            return False

    # ── 小工具 ────────────────────────────────────────────────────────

    def _take_quota(self, ledger_key: str, option: str, default: int, *, high: int = 500) -> bool:
        """每天的额度（整个人格），记在 PluginDataStore：画卡和画插画各一本账。"""
        data = self.get_data_store()
        today = local_today(_now())
        ledger = data.get(ledger_key) or {}
        used = int(ledger.get("count", 0)) if ledger.get("date") == today else 0
        if used >= self._option(option, default, low=0, high=high):
            return False
        data.set(ledger_key, {"date": today, "count": used + 1})
        return True

    def _gacha(self) -> GachaStore:
        if self._store is None:
            self._store = GachaStore(self.get_artifact_dir() / "gacha.db")
        return self._store

    def _members(self, group_id: str) -> list[Member]:
        return gather_members(
            self.ctx.engine.get_engine(),
            group_id,
            min_messages=self._option("min_messages", 20, low=1, high=10_000),
        )

    def _person(self, group_id: str, token: str) -> tuple[str, str] | None:
        return resolve_person(self.ctx.engine.get_engine(), group_id, token)

    def _find_owned(self, group_id: str, owner_id: str, query: str) -> Owned | None:
        for item in self._gacha().collection(group_id, owner_id):
            if _matches(item.card, query):
                return item
        return None

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

    async def _render(self, card: Card, owned: Owned | None = None) -> bytes:
        avatar = await self._avatar(card.subject_uid)
        art = None
        if card.art:
            try:
                art = (self.get_artifact_dir() / "art" / card.art).read_bytes()
            except OSError:
                art = None
        return await asyncio.to_thread(
            render_card,
            card,
            avatar=avatar,
            persona_name=self._persona_name(),
            stars=owned.stars if owned else 1,
            extra_notes=owned.notes if owned else [],
            art=art,
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


def _label(card: Card) -> str:
    return f"{card.label}「{card.title}」"


def _matches(card: Card, query: str) -> bool:
    """编号（17、017、No.17）或名字（人名、名场面的名字）。"""
    query = query.strip()
    number = query.lower().removeprefix("no.").lstrip("0")
    if number.isdigit():
        return card.no == int(number)
    return query in {card.subject_name, card.name} and bool(query)


def _parse(stamp: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _write_cache(path: Path, data: bytes) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    except OSError:
        pass
