"""群友图鉴：真实插件管线、真实 PNG、OneBot 发送边界，假的只有模型和引擎数据。

业务视角：
- 第一次 /抽卡 时卡池是空的，她当场给群里最活跃的人画一张，抽卡回复里有她的台词和卡面图；
- 卡面只用这个人在本群的公开发言写成，私聊和别的群的话不会进到她写卡的材料里；
- 每人每天一抽，按中国时间的自然日重置；
- 后台按额度慢慢给够格的人画卡，发言太少的人不画；
- /我的卡 退出 会把这个人的卡连同大家手里的那几张一起撕掉，以后也不再画，/我的卡 加入 能回来；
- 没抽到的卡不能偷看；数据不够的属性画成 ??；
- 写坏的卡面不会发出去；私聊里不能玩。
"""

from __future__ import annotations

import base64
import io
import json
import random
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw

# 让 tests 目录既能按仓库根、也能按 plugins 目录被收集。
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from sirius_pulse.memory.basic import BasicMemoryFileStore
from sirius_pulse.memory.basic.models import BasicMemoryEntry
from sirius_pulse.platforms.onebot_v11.napcat.adapter import NapCatAdapter
from sirius_pulse.plugins.context import MessageContext
from sirius_pulse.plugins.dispatcher import OutputDispatcher
from sirius_pulse.plugins.executor import PluginExecutor
from sirius_pulse.plugins.lexer import CommandParser, Lexer, PluginMatcher
from sirius_pulse.plugins.loader import PluginLoader
from sirius_pulse.plugins.registry import PluginRegistry

GROUP = "730101"
OTHER_GROUP = "730202"
_CN = timezone(timedelta(hours=8))

# 群友：内部 user_id → (名字, QQ 号, 本群发言数)
PEOPLE = {
    "u-ming": ("阿明", "10001", 60),
    "u-hong": ("小红", "10002", 40),
    "u-wang": ("老王", "10003", 25),
    "u-new": ("新来的", "10004", 3),
}


def _cn(day: int, hour: int) -> datetime:
    return datetime(2026, 11, day, hour, tzinfo=_CN).astimezone(timezone.utc)


def card_reply(title="凌晨三点的哲学家") -> str:
    return json.dumps(
        {
            "title": title,
            "skill_name": "明天一定早睡",
            "skill_text": "每晚 23:00 发动，成功率 0%。",
            "note": "这句话他说了第 41 次。",
        },
        ensure_ascii=False,
    )


class Engine:
    """只提供图鉴要读的那几样：语义画像、认知事件、聊天归档、人设和模型。"""

    def __init__(self, work_path: Path) -> None:
        self.work_path = work_path
        self.basic_store = BasicMemoryFileStore(work_path)
        self.persona = SimpleNamespace(name="月白", full_system_prompt="你是月白。")
        self.model_router = None
        self.token_store = None
        self.prompts: list[str] = []
        self.replies: list[str] = []
        self.events: list[tuple[str, str, float, float]] = []
        self.semantic_memory = SimpleNamespace(list_group_user_profiles=self._profiles)
        self.cognition_store = SimpleNamespace(execute=self._events)
        self.provider_async = SimpleNamespace(generate_async=self._generate)

    def _profiles(self, group_id):
        if group_id != GROUP:
            return []
        return [
            SimpleNamespace(
                user_id=user_id,
                name=name,
                interaction_count=count,
                engagement_rate=0.5,
                first_interaction_at="2026-05-01T00:00:00+00:00",
            )
            for user_id, (name, _uid, count) in PEOPLE.items()
        ]

    def _events(self, _sql, params):
        rows = [(u, ts, v) for g, u, ts, v in self.events if g == params[0]]
        return SimpleNamespace(fetchall=lambda: rows)

    async def _generate(self, request, return_reasoning=False):
        self.prompts.append("\n".join(str(m["content"]) for m in request.messages))
        reply = self.replies.pop(0) if self.replies else card_reply()
        return SimpleNamespace(content=reply)

    def get_active_groups(self):
        return [GROUP, "private_10001"]

    def say(self, group_id: str, user_id: str, text: str) -> None:
        name, uid, _ = PEOPLE[user_id]
        self.basic_store.append(
            BasicMemoryEntry(
                entry_id=f"{group_id}-{len(text)}-{text[:4]}",
                group_id=group_id,
                user_id=user_id,
                role="human",
                content=text,
                timestamp="2026-11-01T12:00:00+00:00",
                speaker_name=name,
                channel_user_id=uid,
            )
        )


@pytest.fixture(scope="module")
def definition():
    path = Path(__file__).resolve().parents[1] / "gacha"
    loaded = PluginLoader(path.parent).load_definition(path)
    assert loaded is not None, "真实 PluginLoader 未能加载群友图鉴"
    return loaded


class Chat:
    def __init__(self, definition, root: Path, monkeypatch) -> None:
        self.definition = definition
        self.engine = Engine(root)
        self.sent: list[list[dict]] = []
        self.drawn: list[str] = []
        self.texts: list[str] = []
        self.clock = [_cn(1, 12)]
        self.adapter = NapCatAdapter(
            "ws://qq.invalid/unconnected", work_path=root, config={"allowed_group_ids": [GROUP]}
        )
        self.adapter.call_api = self._capture
        self.registry = PluginRegistry()
        self.registry.register(definition)
        self.executor = PluginExecutor(
            self.registry, persona_data_path=root, adapter=self.adapter, engine=self.engine
        )
        self.monkeypatch = monkeypatch

    async def _capture(self, action, params):
        assert action == "send_group_msg"
        self.sent.append(params["message"])
        return {"status": "ok", "retcode": 0, "data": {"message_id": 1}}

    async def plugin(self):
        instance = self.registry.get_instance("gacha")
        if instance is None:
            instance = await self.executor.instantiate(self.definition)
            self.registry.set_instance("gacha", instance)
        module = sys.modules[type(instance).__module__]
        self.monkeypatch.setattr(module, "_now", lambda: self.clock[0])
        instance._rng = random.Random(7)
        return instance

    async def send(self, text: str, *, user: str = "u-hong", group_id: str = GROUP) -> str:
        await self.plugin()
        self.sent.clear()
        self.drawn.clear()
        name, uid, _ = PEOPLE[user]
        matched = PluginMatcher().match(
            text, self.definition.commands, self.definition.name, Lexer()
        )
        assert matched is not None and matched.lexed is not None, text
        cmd = CommandParser().parse(matched.lexed, self.definition)
        responses = await self.executor.execute(
            matched.plugin_name,
            cmd,
            group_id=group_id,
            user_id=user,
            engine=self.engine,
            message_context=MessageContext(
                group_id=group_id,
                user_id=user,
                channel="qq",
                channel_user_id=uid,
                message_id="1",
                content=text,
                speaker_name=name,
            ),
        )
        replies = []
        for response in responses:
            output = await OutputDispatcher().dispatch(
                response,
                self.definition,
                engine=self.engine,
                adapter=self.adapter,
                group_id=group_id,
                user_id=user,
            )
            # 与宿主一致：有 message_group 时只发它，否则发文字。
            message = output.message_group or output.text
            if message and not group_id.startswith("private_"):
                await self.adapter.send_group_message(group_id, message)
            replies.append(output.text or "")
        return "\n".join(replies)

    def said(self) -> str:
        """真正发到群里的文字。"""
        return "".join(
            seg["data"]["text"] for msg in self.sent for seg in msg if seg["type"] == "text"
        )

    def images(self) -> list[Image.Image]:
        out = []
        for msg in self.sent:
            for seg in msg:
                if seg["type"] == "image":
                    ref = seg["data"]["file"]
                    assert ref.startswith("base64://")
                    image = Image.open(io.BytesIO(base64.b64decode(ref.removeprefix("base64://"))))
                    image.load()
                    out.append(image)
        return out


@pytest.fixture
def chat(monkeypatch, definition):
    with tempfile.TemporaryDirectory(prefix="sirius-gacha-") as folder:
        rig = Chat(definition, Path(folder), monkeypatch)
        # 头像从不真的联网：拿不到时卡面画名字的第一个字。
        import httpx

        class _Offline:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, *a, **k):
                raise httpx.ConnectError("offline")

        monkeypatch.setattr(httpx, "AsyncClient", _Offline)
        paint = ImageDraw.ImageDraw.text

        def record(draw, xy, text, *args, **kwargs):
            rig.drawn.append(str(text))
            return paint(draw, xy, text, *args, **kwargs)

        monkeypatch.setattr(ImageDraw.ImageDraw, "text", record)
        yield rig
        rig.registry.get_instance("gacha")._store = None


# --- 抽卡 ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_draw_paints_the_most_active_member_from_this_group_only(chat):
    chat.engine.say(GROUP, "u-ming", "明天一定早睡")
    chat.engine.say(GROUP, "u-ming", "/抽卡")
    chat.engine.say(OTHER_GROUP, "u-ming", "别的群里的秘密：我要跳槽了")
    chat.engine.say("private_10001", "u-ming", "私聊里的秘密：其实我怕黑")

    await chat.send("/抽卡")

    assert len(chat.engine.prompts) == 1, "第一次抽卡只当场画一张"
    prompt = chat.engine.prompts[0]
    assert "阿明" in prompt and "明天一定早睡" in prompt
    assert "跳槽" not in prompt and "怕黑" not in prompt and "/抽卡" not in prompt
    assert "月白" in prompt, "卡面要用她的口气写"
    # 回复里既有她的台词也有卡面，台词随图一起真正发到群里。
    assert "阿明" in chat.said() and "凌晨三点的哲学家" in chat.said()
    [card] = chat.images()
    assert card.size == (600, 900)
    assert "「凌晨三点的哲学家」" in chat.drawn and "No.001" in chat.drawn


@pytest.mark.asyncio
async def test_one_draw_a_day_and_the_day_turns_at_midnight_china_time(chat):
    await chat.send("/抽卡")
    await chat.send("/抽卡")
    assert "今天已经抽过" in chat.said() and not chat.images()

    chat.clock[0] = _cn(1, 23)
    await chat.send("/抽卡")
    assert "今天已经抽过" in chat.said()

    chat.clock[0] = _cn(2, 0)
    await chat.send("/抽卡")
    assert len(chat.images()) == 1


@pytest.mark.asyncio
async def test_a_garbled_card_is_never_issued(chat):
    chat.engine.replies = ["他是个好人！"]

    await chat.send("/抽卡")

    assert "卡池还空着" in chat.said()
    assert not chat.images()


@pytest.mark.asyncio
async def test_private_chat_cannot_play(chat):
    await chat.send("/抽卡", group_id="private_10002")
    assert chat.engine.prompts == [] and chat.sent == []


# --- 后台画卡 -------------------------------------------------------------------


async def _open(chat):
    """群里第一次有人抽卡（新来的群友，自己还不够格入画）：当场画出阿明那张。"""
    await chat.send("/抽卡", user="u-new")
    return await chat.plugin()


def _painted(chat) -> list[str]:
    return [p.split("群友：")[1].split("\n")[0] for p in chat.engine.prompts]


@pytest.mark.asyncio
async def test_nobody_is_painted_in_a_group_where_nobody_plays(chat):
    plugin = await chat.plugin()

    assert await plugin.mint_beat() == 0
    assert chat.engine.prompts == [], "插件默认启用，但没人玩的群不该花模型去画人"


@pytest.mark.asyncio
async def test_she_paints_everyone_eligible_a_few_at_a_time_and_skips_the_quiet(chat):
    plugin = await _open(chat)

    made = [await plugin.mint_beat() for _ in range(2)]

    assert made == [2, 0], "每次心跳最多两张，画完就停"
    assert _painted(chat) == ["阿明", "小红", "老王"], "最活跃的先画；发言太少的不画"


@pytest.mark.asyncio
async def test_her_daily_painting_budget_holds(chat):
    plugin = await chat.plugin()
    plugin.ctx.config["cards_per_day"] = 2
    await _open(chat)

    assert await plugin.mint_beat() == 1
    assert await plugin.mint_beat() == 0

    chat.clock[0] = _cn(2, 9)
    assert await plugin.mint_beat() == 1


# --- 退出与查看 -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_opting_out_tears_up_the_card_everywhere_and_she_stops_painting(chat):
    await chat.send("/抽卡", user="u-hong")  # 小红抽到阿明
    assert "阿明" in chat.said()

    await chat.send("/我的卡 退出", user="u-ming")
    assert "撕掉" in chat.said()
    await chat.send("/图鉴", user="u-hong")
    assert "这个群的图鉴还是空的" in chat.said()

    plugin = await chat.plugin()
    await plugin.mint_beat()
    assert _painted(chat).count("阿明") == 1, "退出后不再画他"

    await chat.send("/我的卡 加入", user="u-ming")
    await plugin.mint_beat()
    assert _painted(chat).count("阿明") == 2


@pytest.mark.asyncio
async def test_cards_you_have_not_drawn_cannot_be_peeked_at(chat):
    plugin = await _open(chat)  # 阿明 No.001
    await plugin.mint_beat()  # 小红 No.002、老王 No.003

    await chat.send("/卡 阿明", user="u-wang")
    assert "还没抽到" in chat.said() and not chat.images()

    await chat.send("/我的卡", user="u-hong")
    assert len(chat.images()) == 1 and "我给你画的卡" in chat.said()

    await chat.send("/卡 2", user="u-hong")
    assert len(chat.images()) == 1, "自己的卡随时能看"

    await chat.send("/卡 不存在的人", user="u-wang")
    assert "没有这张卡" in chat.said()


@pytest.mark.asyncio
async def test_collection_shows_progress_with_unknown_cards_hidden(chat):
    plugin = await _open(chat)
    await plugin.mint_beat()

    await chat.send("/抽卡", user="u-hong")
    await chat.send("/图鉴", user="u-hong")

    assert "已收集 1/3" in chat.said()
    assert len(chat.images()) == 1
    assert chat.drawn.count("？？？") == 2


@pytest.mark.asyncio
async def test_stats_come_from_real_habits_and_unknowns_show_as_question_marks(chat):
    base = _cn(1, 2).timestamp()
    # 阿明：12 条认知事件，一半在凌晨两点、一半在下午两点 → 夜行拉满；
    # 小红的事件太少 → 夜行/情绪是 ??
    chat.engine.events = [
        (GROUP, "u-ming", base + (i % 2) * 12 * 3600 + i * 60, 0.4 if i % 2 else -0.4)
        for i in range(12)
    ]
    chat.engine.events += [(GROUP, "u-hong", base, 0.1)]
    plugin = await _open(chat)
    await plugin.mint_beat()

    await chat.send("/我的卡", user="u-ming")
    assert "99" in chat.drawn, "凌晨说话占一半，夜行拉满"
    await chat.send("/我的卡", user="u-hong")
    assert chat.drawn.count("??") == 2


# --- 规则 -----------------------------------------------------------------------


def _card(no: int, rarity: str):
    from plugins.gacha.store import Card

    return Card(
        card_id=f"c{no}",
        group_id=GROUP,
        no=no,
        subject_id=f"s{no}",
        subject_name=f"人{no}",
        subject_uid="",
        rarity=rarity,
        title="",
        skill_name="",
        skill_text="",
        note="",
    )


@pytest.mark.parametrize(
    ("pool", "pity", "expected", "pity_after"),
    [
        ([("R", 1)], 39, "R", 40),  # 卡池里没有 SSR：保底落空，只能给 R，保底继续累计
        ([("R", 1), ("SSR", 2)], 39, "SSR", 0),  # 第 40 抽必出 SSR 以上，拿到后清零
        ([("SR", 1)], 0, "SR", 1),  # 卡池里只有 SR：掷到 R 也只能给 SR
    ],
)
def test_pity_and_rarity_fallback(pool, pity, expected, pity_after):
    from plugins.gacha.rules import draw

    cards = [_card(no, rarity) for rarity, no in pool]
    result = draw(cards, random.Random(1), pity=pity)
    assert result.card.rarity == expected and result.pity_after == pity_after


def test_card_text_strips_invisible_bidi_marks():
    from plugins.gacha.visual import _clean_text

    assert _clean_text("池宇健\u2067なのだ\u2067(池宇健)") == "池宇健なのだ(池宇健)"


def test_cute_font_falls_back_when_it_lacks_kana():
    from plugins.gacha.visual import _font, _font_for, _glyph_supported

    cute = _font(30, cute=True)
    assert not _glyph_supported(cute, "な")

    fallback = _font_for("なのだ", 30, cute=True)
    assert _glyph_supported(fallback, "な")
    assert fallback.path != cute.path
