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
from sirius_pulse.people import PersonService
from sirius_pulse.platforms.onebot_v11.napcat.adapter import NapCatAdapter
from sirius_pulse.plugins.context import MessageContext
from sirius_pulse.plugins.dispatcher import OutputDispatcher
from sirius_pulse.plugins.executor import PluginExecutor
from sirius_pulse.plugins.lexer import CommandParser, Lexer, PluginMatcher
from sirius_pulse.plugins.loader import PluginLoader
from sirius_pulse.plugins.registry import PluginRegistry
from sirius_pulse.visual import VisualService, policies

from plugins.gacha.voice import LIVE_MARK

GROUP = "730101"
#: 适配器配置里的 root：阿明的 QQ 号。
ROOT_QQ = "10001"
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


def _png(color=(60, 70, 120)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (64, 96), color).save(buffer, format="PNG")
    return buffer.getvalue()


class Engine:
    """只提供图鉴要读的那几样：语义画像、认知事件、聊天归档、她说话的链路和画笔。"""

    def __init__(self, work_path: Path) -> None:
        self.work_path = work_path
        self.basic_store = BasicMemoryFileStore(work_path)
        self.persona = SimpleNamespace(name="月白", full_system_prompt="你是月白。")
        self.prompts: list[str] = []
        self.tasks: list[str] = []
        self.replies: list[str] = []  # 写卡、判断名场面、觉醒（JSON）
        self.lines: list[str] = []  # 她写在卡上的一句话（升星悄悄话、插画画面）
        self.voice: list[str] = []  # 她现场开口回大家的话（每条指令的回复、画好时叫人）
        self.script = ""  # 她写的台词本
        self.events: list[tuple[str, str, float, float]] = []
        self.paintings: list = []  # 生图请求
        self.paint_error: Exception | None = None
        self.announced: list[dict] = []  # 她主动发到群里的话
        self.semantic_memory = SimpleNamespace(list_group_user_profiles=self._profiles)
        self.cognition_store = SimpleNamespace(execute=self._events)
        assets = work_path / "assets"
        assets.mkdir(parents=True, exist_ok=True)
        (assets / "turnaround.png").write_bytes(_png((200, 200, 230)))
        self.brain = SimpleNamespace(
            generate_text=self._generate,
            generate_image_async=self._paint,
            persona_assets_dir=assets,
        )
        self.assistant_emotion = SimpleNamespace(valence=0.2)
        # 人物与视觉资产服务用框架的真实实现：「画我」的授权、额度、隔离都由它们判定。
        self.person_service = PersonService(None, persona_name="月白")
        self.visual_service = VisualService(
            work_path / "visual", people=self.person_service, conn=self.person_service.conn
        )
        self._evidence_seq = 0

    def record(self, group_id: str, user_id: str, text: str, attachments=()) -> str:
        """宿主感知层为群友的每条消息记下来源证据（插件指令也一样）。"""
        _name, uid, _ = PEOPLE[user_id]
        self._evidence_seq += 1
        return self.person_service.record_evidence(
            group_id=group_id,
            user_id=user_id,
            platform="qq",
            platform_uid=uid,
            message_id=f"msg-{self._evidence_seq}",
            content=text,
            attachments=[str(path) for path in attachments],
        )

    async def register_look(self, user_id: str) -> None:
        """群友本人发一张照片说“记一下我的样子”，在视觉资产系统里登记成正式形象。"""
        from sirius_pulse.people import PersonAccess

        photo = self.work_path / f"look-{user_id}.png"
        photo.write_bytes(_png((210, 40, 40)))
        ref = self.record(GROUP, user_id, "记一下我的样子", [photo])
        person_id = self.person_service.person_id_for_user(user_id)
        access = PersonAccess(actor_id=person_id, group_id=GROUP, evidence_refs=(ref,))
        self.person_service.update_names(person_id, access, ref, preferred_name=PEOPLE[user_id][0])
        staged = await self.visual_service.manage(
            "stage",
            access=access,
            evidence_ref=ref,
            target={"ref": "requester"},
            source={"kind": "attachment", "index": 0},
            identity_description="短发，戴眼镜",
            default_outfit="格子衬衫",
            view="正面",
            activate=True,
        )
        assert staged["success"], staged

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

    async def _generate(self, *, system_prompt, messages, group_id, task_name, **_):
        """她的对话生成链路（真实环境里由 Brain 注入完整人设）。"""
        self.prompts.append("\n".join([system_prompt, *(str(m["content"]) for m in messages)]))
        self.tasks.append(task_name)
        if "【写你的台词本】" in system_prompt:
            return self.script
        if LIVE_MARK in system_prompt:
            return self.voice.pop(0) if self.voice else ""
        if "只输出一个 JSON" in system_prompt:
            return self.replies.pop(0) if self.replies else card_reply()
        return self.lines.pop(0) if self.lines else ""

    async def _paint(self, request):
        from sirius_pulse.providers.base import ImageGenerationResult

        self.paintings.append(request)
        if self.paint_error is not None:
            raise self.paint_error
        return ImageGenerationResult(image_bytes=_png(), mime_type="image/png")

    async def dispatch_proactive_message(self, *, group_id, text, image_path="", **_):
        self.announced.append({"group_id": group_id, "text": text, "image_path": image_path})
        return True

    def get_active_groups(self):
        return [GROUP, "private_10001"]

    def say(self, group_id: str, user_id: str, text: str, at: datetime | None = None) -> None:
        name, uid, _ = PEOPLE[user_id]
        self.basic_store.append(
            BasicMemoryEntry(
                entry_id=f"{group_id}-{len(text)}-{text[:4]}",
                group_id=group_id,
                user_id=user_id,
                role="human",
                content=text,
                timestamp=(at or datetime(2026, 11, 1, 2, tzinfo=timezone.utc)).isoformat(),
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
        self.rng = random.Random(7)
        self.adapter = NapCatAdapter(
            "ws://qq.invalid/unconnected",
            work_path=root,
            config={"allowed_group_ids": [GROUP], "root": ROOT_QQ},
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
        instance._rng = self.rng
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
                evidence_ref=self.engine.record(group_id, user, text),
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

    # 第一次抽卡：当场画一张卡，再现场说一句。
    assert len(chat.engine.prompts) == 2
    prompt, spoken = chat.engine.prompts
    assert "阿明" in prompt and "明天一定早睡" in prompt
    assert "跳槽" not in prompt and "怕黑" not in prompt and "/抽卡" not in prompt
    assert LIVE_MARK in spoken and "跳槽" not in spoken and "怕黑" not in spoken
    # 卡面和她说的话都走她平时说话的那条生成链路：人设、说话方式由宿主注入，和聊天时一样。
    assert chat.engine.tasks == ["plugin_generate", "plugin_generate"]
    assert "用你自己平时说话的方式" in prompt
    # 回复里既有她的台词也有卡面，台词随图一起真正发到群里。
    assert "阿明" in chat.said() and "凌晨三点的哲学家" in chat.said()
    [card] = chat.images()
    assert card.size == (600, 900)
    assert "「凌晨三点的哲学家」" in chat.drawn and "No.001" in chat.drawn


@pytest.mark.asyncio
async def test_one_draw_a_day_and_the_day_turns_at_midnight_china_time(chat):
    await chat.send("/抽卡")
    await chat.send("/抽卡")
    assert "这一轮已经抽过" in chat.said() and not chat.images()
    assert "下次刷新：明天 00:00" in chat.said()

    chat.clock[0] = _cn(1, 23)
    await chat.send("/抽卡")
    assert "这一轮已经抽过" in chat.said()

    chat.clock[0] = _cn(2, 0)
    await chat.send("/抽卡")
    assert len(chat.images()) == 1


@pytest.mark.asyncio
async def test_several_refresh_times_a_day_each_start_a_new_round(chat):
    plugin = await chat.plugin()
    plugin.ctx.config["refresh_times"] = "20:00, 08：00 12:00, 25:00"  # 写错的被跳过
    chat.clock[0] = _cn(1, 9)

    await chat.send("/抽卡")
    await chat.send("/抽卡")
    assert "下次刷新：12:00" in chat.said()

    chat.clock[0] = _cn(1, 12)
    await chat.send("/抽卡")
    assert len(chat.images()) == 1
    await chat.send("/抽卡")
    assert "下次刷新：20:00" in chat.said()

    chat.clock[0] = _cn(1, 21)
    await chat.send("/抽卡")
    assert len(chat.images()) == 1
    await chat.send("/抽卡")
    assert "下次刷新：明天 08:00" in chat.said()

    # 凌晨两点仍是昨晚 20:00 那一轮。
    chat.clock[0] = _cn(2, 2)
    await chat.send("/抽卡")
    assert not chat.images()

    await chat.send("/抽卡 刷新时间")
    assert "08:00、12:00、20:00" in chat.said()


@pytest.mark.asyncio
async def test_root_can_refresh_a_group_by_hand_and_nobody_else_can(chat):
    await chat.send("/抽卡", user="u-hong")
    await chat.send("/抽卡", user="u-wang")

    await chat.send("/抽卡 刷新", user="u-hong")
    assert "只有 root" in chat.said()
    await chat.send("/抽卡", user="u-hong")
    assert not chat.images(), "不是 root 刷新不了"

    chat.clock[0] += timedelta(minutes=1)
    await chat.send("/抽卡 刷新", user="u-ming")  # 阿明是 root
    assert "刷新好了" in chat.said()
    await chat.send("/抽卡", user="u-hong")
    await chat.send("/抽卡", user="u-wang")
    assert len(chat.images()) == 1, "老王这一轮也回来了"
    await chat.send("/抽卡", user="u-hong")
    assert not chat.images(), "刷新一次只多一轮"

    # 同一天可以刷新好几次。
    chat.clock[0] += timedelta(hours=1)
    await chat.send("/抽卡 刷新", user="u-ming")
    await chat.send("/抽卡", user="u-hong")
    assert len(chat.images()) == 1


@pytest.mark.asyncio
async def test_root_sets_this_groups_refresh_times_without_handing_out_free_rounds(chat):
    chat.clock[0] = _cn(1, 14)
    await chat.send("/抽卡", user="u-hong")

    await chat.send("/抽卡 刷新时间 08:00 20:00", user="u-hong")
    assert "只有 root" in chat.said()

    await chat.send("/抽卡 刷新时间 8:00，20:00", user="u-ming")
    assert "每天 08:00、20:00" in chat.said() and "下次是 20:00" in chat.said()
    await chat.send("/抽卡", user="u-hong")
    assert not chat.images(), "改时间不会白送一轮：14 点前一个刷新是 08:00，她已经抽过了"

    await chat.send("/抽卡 刷新时间 吃饭的时候", user="u-ming")
    assert "没看懂" in chat.said()

    chat.clock[0] = _cn(1, 20)
    await chat.send("/抽卡", user="u-hong")
    assert len(chat.images()) == 1

    await chat.send("/抽卡 刷新时间 默认", user="u-ming")
    assert "每天 00:00" in chat.said()


@pytest.mark.asyncio
async def test_a_garbled_card_is_never_issued(chat):
    chat.engine.replies = ["他是个好人！"]

    await chat.send("/抽卡")

    assert "卡池还空着" in chat.said()
    assert not chat.images()


@pytest.mark.asyncio
async def test_private_chat_cannot_play(chat):
    reply = await chat.send("/抽卡", group_id="private_10002")
    assert "群里" in reply and chat.sent == []
    assert all(LIVE_MARK in prompt for prompt in chat.engine.prompts), "私聊里不画卡"


# --- 后台画卡 -------------------------------------------------------------------


async def _open(chat):
    """群里第一次有人抽卡（新来的群友，自己还不够格入画）：当场画出阿明那张。"""
    await chat.send("/抽卡", user="u-new")
    return await chat.plugin()


def _painted(chat) -> list[str]:
    """她写过常驻卡或心情卡的人（按写卡的顺序）。"""
    return [p.split("群友：")[1].split("\n")[0] for p in chat.engine.prompts if "群友：" in p]


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
    assert "不画你" in chat.said()
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


# --- 送卡、交换、升星 -----------------------------------------------------------


async def _three_cards(chat):
    """阿明 No.001（开张时画的，新来的手里有一张）、小红 No.002、老王 No.003。"""
    plugin = await _open(chat)
    await plugin.mint_beat()
    return plugin


def _grant(plugin, user: str, no: int, copies: int = 1) -> None:
    """安排收藏：相当于这个人以前抽到过这张卡（记在很久以前的某一天）。"""
    store = plugin._gacha()
    card = next(card for card in store.cards(GROUP) if card.no == no)
    for _ in range(copies):
        store.record_draw(
            group_id=GROUP,
            owner_id=user,
            card=card,
            period=datetime(2026, 1, 1, tzinfo=timezone.utc),
            pity_after=0,
            now="2026-01-01T00:00:00+00:00",
        )


@pytest.mark.asyncio
async def test_a_gift_moves_one_copy_to_someone_found_by_mention(chat):
    plugin = await _three_cards(chat)
    _grant(plugin, "u-hong", 1)
    chat.engine.say(GROUP, "u-wang", "大家好")  # 归档里有老王的 QQ 号，@ 才认得出来

    await chat.send("/送卡 @qq_10003 1", user="u-hong")
    assert "老王" in chat.said()
    await chat.send("/卡 1", user="u-hong")
    assert "还没抽到" in chat.said(), "送出去就不在自己手里了"
    await chat.send("/卡 1", user="u-wang")
    assert len(chat.images()) == 1

    await chat.send("/送卡 路人甲 1", user="u-wang")
    assert "不知道" in chat.said()
    await chat.send("/送卡 小红 9", user="u-wang")
    assert "没有 9" in chat.said()


@pytest.mark.asyncio
async def test_a_trade_needs_the_other_side_and_never_half_happens(chat):
    plugin = await _three_cards(chat)
    _grant(plugin, "u-hong", 1)
    _grant(plugin, "u-wang", 2)

    await chat.send("/交换 老王 1 换 2", user="u-hong")
    assert "/同意交换" in chat.said()
    await chat.send("/同意交换", user="u-wang")
    assert "成交" in chat.said()
    await chat.send("/卡 2", user="u-hong")
    assert len(chat.images()) == 1
    await chat.send("/卡 1", user="u-wang")
    assert len(chat.images()) == 1

    # 提出交换后自己把那张送走了：对方同意时整笔作废，对方的卡不会少。
    await chat.send("/交换 老王 2 换 1", user="u-hong")
    await chat.send("/送卡 阿明 2", user="u-hong")
    await chat.send("/同意交换", user="u-wang")
    assert "做不成" in chat.said()
    await chat.send("/卡 1", user="u-wang")
    assert len(chat.images()) == 1

    # 过了十分钟还没回，交换就作废了。
    _grant(plugin, "u-hong", 3)
    await chat.send("/交换 老王 3 换 1", user="u-hong")
    chat.clock[0] += timedelta(minutes=11)
    await chat.send("/同意交换", user="u-wang")
    assert "没有人要跟你换卡" in chat.said()


@pytest.mark.asyncio
async def test_star_up_spends_a_duplicate_and_she_writes_one_more_line_for_you(chat):
    plugin = await _three_cards(chat)
    _grant(plugin, "u-hong", 1, copies=2)
    chat.engine.lines = ["你攒了两张他，是不是偷偷崇拜他。"]

    await chat.send("/升星 1", user="u-hong")
    assert "2 星" in chat.said() and "偷偷崇拜他" in chat.said()
    assert "★★☆" in chat.drawn
    assert any(text.startswith("悄悄话：") for text in chat.drawn)

    await chat.send("/升星 1", user="u-hong")
    assert "只有一份" in chat.said(), "升星吃掉了那张重复的"

    # 她没写出来就不扣卡。
    _grant(plugin, "u-hong", 1)
    await chat.send("/升星 1", user="u-hong")
    assert "卡没扣" in chat.said()
    await chat.send("/卡 1", user="u-hong")
    assert "★★☆" in chat.drawn


# --- 心情 -----------------------------------------------------------------------


class _Always:
    """掷骰永远是 0.1：落在 SR 档（UR 0.5%、SSR 4.5% 之后的 20% 里）。"""

    def random(self):
        return 0.1

    def choice(self, seq):
        return seq[0]

    def choices(self, population, weights=None):
        return [population[0]]


@pytest.mark.asyncio
async def test_in_a_good_mood_she_paints_a_mood_card_once_a_day_and_luck_goes_up(chat):
    chat.engine.assistant_emotion.valence = 0.8
    plugin = await _open(chat)

    assert await plugin.mint_beat() == 2  # 先把还没有卡的人画完
    assert await plugin.mint_beat() == 1  # 再随手画一张心情卡
    assert "心情卡" in chat.engine.prompts[-1]
    assert await plugin.mint_beat() == 0, "心情卡一个群一天最多一张"

    await chat.send("/抽卡", user="u-wang")
    assert "心情好" in chat.said()

    # 心情回落后，抽到 SR 时她会亲口说一句。
    chat.engine.assistant_emotion.valence = 0.2
    chat.rng = _Always()
    chat.engine.voice = ["你抽到了我心情最好的那张！"]
    await chat.send("/抽卡", user="u-hong")
    assert chat.said().startswith("你抽到了我心情最好的那张"), "她现场说，不用模板"
    assert "SR" in chat.engine.prompts[-1], "她知道这张很难得"
    assert "心情好，手气" not in chat.said()


# --- 名场面 ---------------------------------------------------------------------


def _moment(chat, *, with_secret: bool = True) -> None:
    """11 月 1 日上午十点：三个人十分钟里接了 15 句火锅梗。"""
    start = _cn(1, 10)
    speakers = ["u-ming", "u-hong", "u-wang"]
    lines = [
        "我宣布，本群从今天起改名叫阿明火锅店",
        "那我要一份毛肚",
        "老板，鸭血还有吗",
    ]
    for i in range(15):
        user = speakers[i % 3]
        at = start + timedelta(seconds=30 * i)
        chat.engine.events.append((GROUP, user, at.timestamp(), 0.8))
        text = lines[i % 3] if i < 3 else f"第{i}句接梗"
        if user == "u-wang" and with_secret:
            text = "老王的秘密台词"
        chat.engine.say(GROUP, user, text, at=at)


def moment_reply(worthy: bool = True) -> str:
    if not worthy:
        return json.dumps({"worthy": False})
    return json.dumps(
        {
            "worthy": True,
            "line": 0,
            "title": "火锅店开业事件",
            "skill_name": "全员下锅",
            "skill_text": "全群开始报菜名，持续三小时。",
            "note": "那天我也想吃火锅了。",
        },
        ensure_ascii=False,
    )


def _judged(chat) -> int:
    return sum("这段聊天" in prompt for prompt in chat.engine.prompts)


@pytest.mark.asyncio
async def test_a_highlight_becomes_a_limited_ur_card_quoting_what_was_really_said(chat):
    _moment(chat)
    plugin = await _open(chat)
    await chat.send("/我的卡 退出", user="u-wang")  # 老王不想入画：他的话不进材料
    chat.engine.replies = [card_reply(), moment_reply()]

    await plugin.mint_beat()  # 小红的常驻卡 + 判断名场面

    assert _judged(chat) == 1
    judged = next(prompt for prompt in chat.engine.prompts if "这段聊天" in prompt)
    assert "改名叫阿明火锅店" in judged and "秘密台词" not in judged
    await chat.send("/图鉴", user="u-new")
    assert "已收集 1/3" in chat.said()
    assert "限量 0/3" in chat.drawn, "没抽到的限量卡也看得到还剩几份"

    await plugin.mint_beat()
    assert _judged(chat) == 1, "一个群一天只判断一次"

    # 名场面卡排队配插画：她先写画面，再画，画好在群里说一声。
    chat.engine.lines = ["一口冒着热气的大火锅摆在群聊气泡做的桌子上，月白举着漏勺在旁边"]
    assert await plugin.art_beat() is True
    assert "大火锅" in chat.engine.paintings[0].prompt
    [news] = chat.engine.announced
    assert "火锅店开业事件" in news["text"] and news["image_path"]

    # 出演的人退出图鉴：名场面也一起撕掉。
    await chat.send("/我的卡 退出", user="u-hong")
    await chat.send("/图鉴", user="u-new")
    assert "已收集 1/1" in chat.said()


@pytest.mark.asyncio
async def test_a_dull_stretch_is_not_a_highlight_and_is_judged_again_tomorrow(chat):
    _moment(chat, with_secret=False)
    plugin = await _three_cards(chat)  # 常驻卡画完，这次心跳的额度也用完了
    pool = len(plugin._gacha().cards(GROUP))
    chat.engine.replies = [moment_reply(worthy=False), moment_reply(worthy=False)]

    await plugin.mint_beat()
    assert _judged(chat) == 1 and len(plugin._gacha().cards(GROUP)) == pool
    await plugin.mint_beat()
    assert _judged(chat) == 1, "当天不再重判"

    chat.clock[0] = _cn(2, 9)  # 第二天，那段聊天仍在 24 小时内
    await plugin.mint_beat()
    assert _judged(chat) == 2


# --- 她的台词本 -----------------------------------------------------------------


def script_reply(**lines) -> str:
    return json.dumps(lines, ensure_ascii=False)


def _scripts(chat) -> int:
    return sum("【写你的台词本】" in prompt for prompt in chat.engine.prompts)


@pytest.mark.asyncio
async def test_when_she_cannot_speak_live_her_weekly_script_fills_in(chat):
    chat.engine.script = script_reply(
        draw_new=["诶嘿，{name}来啦——「{title}」！", "这句少了占位符"],
        drawn_out=["{nope}今天不行"],
    )
    # 这个测试里她现场开不了口（模型没回话），才轮到台词本。
    plugin = await _open(chat)  # 她还没写台词本：先用保底句
    assert "抽到了" in chat.said()

    await plugin.mint_beat()
    assert _scripts(chat) == 1
    await chat.send("/抽卡", user="u-wang")
    assert "诶嘿" in chat.said(), "她写的那句"
    await chat.send("/抽卡", user="u-wang")
    assert "这一轮已经抽过" in chat.said(), "占位符不对的句子被丢掉，退回保底句"

    await plugin.mint_beat()
    assert _scripts(chat) == 1, "一周只写一本"
    chat.clock[0] += timedelta(days=8)
    await plugin.mint_beat()
    assert _scripts(chat) == 2


@pytest.mark.asyncio
async def test_a_script_she_could_not_write_is_not_retried_the_same_day(chat):
    chat.engine.script = "台词本？我想想……"
    plugin = await _open(chat)

    await plugin.mint_beat()
    await plugin.mint_beat()
    assert _scripts(chat) == 1

    chat.clock[0] += timedelta(days=1)
    await plugin.mint_beat()
    assert _scripts(chat) == 2


# --- 她的声音：每句回复都现场说 ------------------------------------------------------


def _spoken(chat) -> list[str]:
    """她现场开口的那几次调用。"""
    return [prompt for prompt in chat.engine.prompts if LIVE_MARK in prompt]


@pytest.mark.asyncio
async def test_every_reply_is_said_live_in_her_own_words_and_rules_are_never_lost(chat):
    chat.engine.assistant_emotion.valence = 0.5
    await _open(chat)  # 新来的群友抽了这一轮的那一次

    chat.engine.voice = ["哼哼，这轮你已经抽完啦，乖乖等着吧"]
    await chat.send("/抽卡", user="u-new")
    assert chat.said().startswith("哼哼，这轮你已经抽完啦"), "是她现场说的，不是模板"
    assert "（下次刷新：明天 00:00）" in chat.said(), "她没说到的刷新时间由系统补上"
    prompt = _spoken(chat)[-1]
    assert "新来的" in prompt and "/抽卡" in prompt, "她知道是谁、发了什么"
    assert "用完" in prompt and "下次刷新：明天 00:00" in prompt
    assert "很好" in prompt, "她此刻的心情也带进去"

    chat.engine.voice = ["说了等明天 00:00 嘛，急什么"]
    await chat.send("/抽卡", user="u-new")
    assert chat.said().startswith("说了等明天 00:00 嘛") and "（下次刷新" not in chat.said()
    assert "哼哼，这轮你已经抽完啦" in _spoken(chat)[-1], "她记得刚说过什么，会换个说法"

    # 指令写错：她自己说，用法由系统整段接在后面。
    chat.engine.voice = ["送谁？送哪张？你得告诉我呀"]
    await chat.send("/送卡 小红", user="u-wang")
    assert chat.said().startswith("送谁？") and "/送卡 小红 17" in chat.said()

    # 帮助：她先说一句，再附上一张玩法卡，指令和本群眼下的规则都画在上面。
    chat.engine.voice = ["想玩图鉴？我教你～"]
    await chat.send("/抽卡 帮助", user="u-wang")
    assert chat.said() == "想玩图鉴？我教你～"
    [guide] = chat.images()
    assert guide.width == 900
    assert "/升星 <编号>" in chat.drawn and "/同意交换" in " ".join(chat.drawn)
    assert any("每天 00:00 刷新" in text and "第 40 抽保底" in text for text in chat.drawn)


@pytest.mark.asyncio
async def test_with_live_voice_off_she_only_uses_her_script(chat):
    plugin = await chat.plugin()
    plugin.ctx.config["live_voice"] = False
    await _open(chat)
    await chat.send("/抽卡", user="u-new")
    assert "这一轮已经抽过" in chat.said() and "下次刷新" in chat.said()
    assert not _spoken(chat), "关掉现场说话就不为说话调用模型"


# --- 超级卡：觉醒与插画 -----------------------------------------------------------


def awaken_reply() -> str:
    return json.dumps(
        {
            "title": "月下守夜的哲人",
            "skill_name": "永夜不眠",
            "skill_text": "群里每有一个人说晚安，他就多清醒一小时。",
            "note": "这张是专门画给你的。",
            "scene": "月光下一只戴眼镜的猫头鹰站在钟楼上，月白坐在旁边陪它看星星",
        },
        ensure_ascii=False,
    )


async def _three_stars(chat, plugin, user: str = "u-hong", no: int = 1) -> None:
    _grant(plugin, user, no, copies=3)
    chat.engine.lines += ["第一句悄悄话", "第二句悄悄话"]
    await chat.send(f"/升星 {no}", user=user)
    await chat.send(f"/升星 {no}", user=user)


@pytest.mark.asyncio
async def test_a_three_star_card_awakens_into_an_ssr_she_paints_just_for_you(chat):
    plugin = await _three_cards(chat)
    _grant(plugin, "u-hong", 1)
    await chat.send("/觉醒 1", user="u-hong")
    assert "3 星" in chat.said(), "不到三星不能觉醒"

    await _three_stars(chat, plugin)
    await chat.send("/觉醒 1", user="u-hong")
    assert "画好叫你" in chat.said()
    await chat.send("/觉醒 1", user="u-hong")
    assert "还在画" in chat.said()

    chat.engine.replies = [awaken_reply()]
    chat.engine.voice = ["小红！你的觉醒卡画好啦！"]
    assert await plugin.art_beat() is True

    [painting] = chat.engine.paintings
    assert "猫头鹰" in painting.prompt and "真实人物" in painting.prompt
    assert [name for name, _ in painting.reference_images] == ["turnaround.png"], "她按自己的形象图画"
    [news] = chat.engine.announced
    assert news["group_id"] == GROUP and news["text"] == "小红！你的觉醒卡画好啦！"
    with Image.open(news["image_path"]) as image:
        assert image.height > 900, "插画卡比普通卡高"

    await chat.send("/图鉴", user="u-hong")
    assert "已收集 2/4" in chat.said()
    await chat.send("/图鉴", user="u-wang")
    assert "/3" in chat.said(), "别人的专属觉醒卡不算进我的收集目标"
    await chat.send("/送卡 老王 4", user="u-hong")
    assert "不能送人" in chat.said()
    await chat.send("/觉醒 1", user="u-hong")
    assert "已经觉醒过" in chat.said()


@pytest.mark.asyncio
async def test_a_painting_that_keeps_failing_is_dropped_and_she_says_so(chat):
    plugin = await _three_cards(chat)
    await _three_stars(chat, plugin)
    await chat.send("/觉醒 1", user="u-hong")
    chat.engine.paint_error = RuntimeError("upstream")
    chat.engine.replies = [awaken_reply(), awaken_reply()]

    assert await plugin.art_beat() is False
    assert await plugin.art_beat() is False

    [news] = chat.engine.announced
    assert "没画好" in news["text"] and not news["image_path"]
    await chat.send("/图鉴", user="u-hong")
    assert "已收集 1/3" in chat.said(), "画失败不会凭空多出一张卡"
    assert await plugin.art_beat() is False and len(chat.engine.paintings) == 2, "不再重画"


@pytest.mark.asyncio
async def test_her_daily_painting_budget_makes_the_rest_wait_until_tomorrow(chat):
    plugin = await _three_cards(chat)
    plugin.ctx.config["art_per_day"] = 1
    await _three_stars(chat, plugin, user="u-hong", no=1)
    await _three_stars(chat, plugin, user="u-wang", no=2)
    await chat.send("/觉醒 1", user="u-hong")
    await chat.send("/觉醒 2", user="u-wang")
    chat.engine.replies = [awaken_reply(), awaken_reply()]

    assert await plugin.art_beat() is True
    assert await plugin.art_beat() is False and len(chat.engine.paintings) == 1

    chat.clock[0] = _cn(2, 9)
    assert await plugin.art_beat() is True


# --- 画我：本人同意后照他本人的样子画觉醒插画 ------------------------------------------


async def _awaken_ming(chat, plugin, owner: str = "u-hong") -> None:
    """小红（或别人）把阿明那张 No.001 升到三星并觉醒，她把插画画完。"""
    await _three_stars(chat, plugin, user=owner, no=1)
    await chat.send("/觉醒 1", user=owner)
    chat.engine.replies.append(awaken_reply())
    assert await plugin.art_beat() is True


def _person_jobs(chat, user: str) -> list[dict]:
    engine = chat.engine
    person_id = engine.person_service.person_id_for_user(user)
    return engine.visual_service.store.jobs_for(person_id, limit=10)


def _consent(chat, user: str) -> dict:
    proxy = chat.registry.get_instance("gacha").ctx.engine
    return proxy.get_person_image_consent(user_id=user, group_id=GROUP)


@pytest.mark.asyncio
async def test_a_member_who_says_draw_me_is_painted_from_their_own_registered_look(chat):
    plugin = await _three_cards(chat)
    await chat.engine.register_look("u-ming")

    await chat.send("/我的卡 画我", user="u-ming")
    assert "本人的样子" in chat.said() and "还没在我这里登记" not in chat.said()
    await chat.send("/我的卡", user="u-ming")
    assert "不画我 可以撤回" in chat.said()

    await _awaken_ming(chat, plugin)

    [awaken_prompt] = [p for p in chat.engine.prompts if "【觉醒】" in p]
    assert "本人同意了被画进来" in awaken_prompt and "象征物" not in awaken_prompt
    [painting] = chat.engine.paintings
    # 走框架的受管人物生图：他登记的形象 + 她自己的形象图，提示词由视觉资产系统编译。
    assert len(painting.reference_images) == 2
    assert "严格按他本人的人物参考图" in painting.prompt and "阿明" in painting.prompt
    [job] = _person_jobs(chat, "u-ming")
    assert job["queue_state"] == "done" and job["grant_id"] == _consent(chat, "u-ming")["grant_id"]
    [news] = chat.engine.announced
    assert "没能照" not in news["text"]
    awakened = next(c for c in plugin._gacha().cards(GROUP) if c.kind == "awakened")
    assert awakened.art and awakened.art_likeness


@pytest.mark.parametrize(
    ("registered", "says_draw_me", "told"),
    [
        (True, False, ""),  # 登记了形象，但本人没同意：照旧象征物，不提他
        (False, True, "还没有登记形象图"),  # 同意了但没登记：象征物，并如实说一句
    ],
)
@pytest.mark.asyncio
async def test_without_consent_or_a_registered_look_he_stays_a_symbol(
    chat, registered, says_draw_me, told
):
    plugin = await _three_cards(chat)
    if registered:
        await chat.engine.register_look("u-ming")
    if says_draw_me:
        await chat.send("/我的卡 画我", user="u-ming")
        assert "还没在我这里登记过形象图" in chat.said()

    await _awaken_ming(chat, plugin)

    [painting] = chat.engine.paintings
    assert [name for name, _ in painting.reference_images] == ["turnaround.png"]
    assert "象征物" in painting.prompt and "真实人物" in painting.prompt
    assert _person_jobs(chat, "u-ming") == [], "没有提交任何人物生图任务"
    [news] = chat.engine.announced
    assert (told in news["text"]) if told else "没能照" not in news["text"]


@pytest.mark.asyncio
async def test_draw_me_must_be_said_by_the_member_themself(chat):
    await _three_cards(chat)
    await chat.engine.register_look("u-ming")
    plugin = await chat.plugin()
    plugin.ctx.message.evidence_ref = ""  # 宿主没能为这条消息记下来源（比如人物模块故障）

    response = await plugin._consent_likeness(GROUP, "u-ming")

    assert "没法确认这句话是你本人发的" in response.text
    assert _consent(chat, "u-ming")["consented"] is False


@pytest.mark.asyncio
async def test_after_draw_me_is_withdrawn_his_portrait_comes_down_and_is_not_drawn_again(chat):
    plugin = await _three_cards(chat)
    await chat.engine.register_look("u-ming")
    await chat.send("/我的卡 画我", user="u-ming")
    await _awaken_ming(chat, plugin)
    store = plugin._gacha()
    awakened = next(c for c in store.cards(GROUP) if c.kind == "awakened")
    art_file = plugin.get_artifact_dir() / "art" / awakened.art
    assert art_file.is_file()

    await chat.send("/我的卡 不画我", user="u-ming")

    assert "撤下了" in chat.said()
    assert not art_file.exists()
    assert store.card(GROUP, awakened.card_id).art == "", "卡还在，插画撤下"
    await chat.send(f"/卡 {awakened.no}", user="u-hong")
    [card_image] = chat.images()
    assert card_image.height == 900, "撤下插画后回到普通卡面"
    # 框架那边：授权撤回，依据它画出的结果隔离。
    assert _consent(chat, "u-ming")["consented"] is False
    [job] = _person_jobs(chat, "u-ming")
    asset = chat.engine.visual_service.store.asset(job["result_asset_id"])
    assert asset["status"] == "isolated"

    # 老王也把阿明的卡觉醒：这次只用象征物画他。
    await _awaken_ming(chat, plugin, owner="u-wang")
    painting = chat.engine.paintings[-1]
    assert [name for name, _ in painting.reference_images] == ["turnaround.png"]
    assert len(_person_jobs(chat, "u-ming")) == 1


@pytest.mark.asyncio
async def test_withdrawing_while_she_is_painting_keeps_him_out_of_the_picture(chat):
    plugin = await _three_cards(chat)
    await chat.engine.register_look("u-ming")
    await chat.send("/我的卡 画我", user="u-ming")
    await _three_stars(chat, plugin)
    await chat.send("/觉醒 1", user="u-hong")
    chat.engine.replies.append(awaken_reply())
    paint = chat.engine._paint

    async def paint_while_he_withdraws(request):
        if len(chat.engine.paintings) == 0:
            await chat.send("/我的卡 不画我", user="u-ming")
        return await paint(request)

    chat.engine.brain.generate_image_async = paint_while_he_withdraws
    assert await plugin.art_beat() is True

    first, second = chat.engine.paintings
    assert len(first.reference_images) == 2, "开始画时他还同意着"
    assert [name for name, _ in second.reference_images] == ["turnaround.png"], "撤回后改画象征物"
    [job] = _person_jobs(chat, "u-ming")
    assert job["queue_state"] == "isolated", "画到一半撤回：那张结果隔离，不交付"
    awakened = next(c for c in plugin._gacha().cards(GROUP) if c.kind == "awakened")
    assert awakened.art and not awakened.art_likeness


@pytest.mark.asyncio
async def test_when_the_person_image_quota_is_used_up_she_says_so_and_uses_a_symbol(chat):
    plugin = await _three_cards(chat)
    await chat.engine.register_look("u-ming")
    await chat.send("/我的卡 画我", user="u-ming")
    store = chat.engine.visual_service.store
    store.set_setting(policies.SETTING_TOTAL_QUOTA, 0, actor_id="admin")

    await _awaken_ming(chat, plugin)

    [painting] = chat.engine.paintings
    assert [name for name, _ in painting.reference_images] == ["turnaround.png"]
    [news] = chat.engine.announced
    assert "没能照 阿明 本人的样子画：人物生图的额度用完了" in news["text"]
    assert _person_jobs(chat, "u-ming") == []
    assert _consent(chat, "u-ming")["consented"] is True, "额度不够不等于撤回同意"


@pytest.mark.asyncio
async def test_opting_out_also_withdraws_draw_me(chat):
    plugin = await _three_cards(chat)
    await chat.engine.register_look("u-ming")
    await chat.send("/我的卡 画我", user="u-ming")
    await _awaken_ming(chat, plugin)
    art_dir = plugin.get_artifact_dir()
    assert list((art_dir / "art").iterdir())

    await chat.send("/我的卡 退出", user="u-ming")
    await chat.send("/我的卡 加入", user="u-ming")

    assert _consent(chat, "u-ming")["consented"] is False
    assert plugin._gacha().likeness_consent(GROUP, "u-ming") == ""
    assert not list((art_dir / "art").iterdir()), "画了他的卡和插画文件一起删掉"


# --- 规则 -----------------------------------------------------------------------


def _card(no: int, rarity: str, *, supply: int = 0, issued: int = 0):
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
        supply=supply,
        issued=issued,
    )


@pytest.mark.parametrize(
    ("pool", "pity", "expected", "pity_after"),
    [
        ([_card(1, "R")], 39, "R", 40),  # 卡池里没有 SSR：保底落空，只能给 R，保底继续累计
        ([_card(1, "R"), _card(2, "SSR")], 39, "SSR", 0),  # 第 40 抽必出 SSR 以上，拿到后清零
        ([_card(1, "SR")], 0, "SR", 1),  # 卡池里只有 SR：掷到 R 也只能给 SR
        # 限量 UR 已经发完：抽不到它，保底继续累计
        ([_card(1, "R"), _card(2, "UR", supply=3, issued=3)], 39, "R", 40),
    ],
)
def test_pity_rarity_fallback_and_sold_out_limits(pool, pity, expected, pity_after):
    from plugins.gacha.rules import draw

    result = draw(pool, random.Random(1), pity=pity)
    assert result.card.rarity == expected and result.pity_after == pity_after


def test_a_good_mood_lifts_every_rare_tier_by_a_fifth():
    from plugins.gacha.rules import rates

    calm, lucky = rates(), rates(lucky=True)
    assert sum(lucky.values()) == pytest.approx(1.0)
    for rarity in ("SR", "SSR", "UR"):
        assert lucky[rarity] == pytest.approx(calm[rarity] * 1.2)


def test_a_phase_one_database_keeps_its_cards_after_the_upgrade(tmp_path):
    """第一期部署留下的库：升级后老卡照常可用，都是常驻卡，收藏也在。"""
    import sqlite3

    from plugins.gacha.store import KIND_MEMBER, GachaStore

    path = tmp_path / "gacha.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE cards (card_id TEXT PRIMARY KEY, group_id TEXT NOT NULL,
                no INTEGER NOT NULL, subject_id TEXT NOT NULL, subject_name TEXT NOT NULL,
                subject_uid TEXT NOT NULL DEFAULT '', rarity TEXT NOT NULL, title TEXT NOT NULL,
                skill_name TEXT NOT NULL, skill_text TEXT NOT NULL, note TEXT NOT NULL,
                stats TEXT NOT NULL DEFAULT '{}', minted_at TEXT NOT NULL,
                UNIQUE (group_id, no));
            CREATE TABLE owned (group_id TEXT NOT NULL, owner_id TEXT NOT NULL,
                card_id TEXT NOT NULL, copies INTEGER NOT NULL DEFAULT 1,
                first_at TEXT NOT NULL, PRIMARY KEY (group_id, owner_id, card_id));
            INSERT INTO cards VALUES ('c1', 'g', 1, 'u-ming', '阿明', '', 'R', '哲学家',
                '早睡', '失败', '批注', '{}', '2026-11-01T00:00:00+00:00');
            INSERT INTO owned VALUES ('g', 'u-hong', 'c1', 2, '2026-11-01T00:00:00+00:00');
            """
        )

    store = GachaStore(path)

    [card] = store.cards("g")
    assert card.kind == KIND_MEMBER and card.supply == 0
    [item] = store.collection("g", "u-hong")
    assert item.copies == 2 and item.stars == 1 and item.notes == []


@pytest.mark.parametrize(
    ("stored", "now", "used"),
    [
        ("2026-11-01", _cn(1, 15), True),  # 升级前当天抽过：升级后这一轮仍算抽过
        ("2026-11-01", _cn(2, 0), False),  # 过了零点：新的一轮
        ("", _cn(1, 15), False),
        ("乱码", _cn(1, 15), False),
    ],
)
def test_a_draw_recorded_before_the_upgrade_still_counts_for_that_day(stored, now, used):
    from plugins.gacha.refresh import Schedule, in_period

    assert in_period(stored, Schedule().period_start(now)) is used


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


# --- 被画的人：有人收藏了你 -------------------------------------------------------


def _ats(chat) -> list[str]:
    """真正发到群里的 @。"""
    return [seg["data"]["qq"] for msg in chat.sent for seg in msg if seg["type"] == "at"]


@pytest.mark.asyncio
async def test_the_person_on_the_card_hears_when_people_collect_them(chat):
    # 卡上的人在群里说过话，她认得他的 QQ 号，才能 @ 到他。
    chat.engine.say(GROUP, "u-ming", "今天也要早睡")
    plugin = await _three_cards(chat)  # 新来的抽走了阿明 No.001
    assert _ats(chat) == ["10001"], "阿明的卡第一次被收藏：@ 阿明"
    assert "第一次被人收进图鉴" in _spoken(chat)[-1] and "新来的" in _spoken(chat)[-1]

    # 同一天又到了一个里程碑（第 3 个收藏家）：她照样提，但一天只 @ 他一次。
    _grant(plugin, "u-hong", 1, copies=2)
    chat.clock[0] += timedelta(hours=1)
    await chat.send("/送卡 老王 1", user="u-hong")
    assert _ats(chat) == [] and "已经有 3 个人收藏了" in _spoken(chat)[-1]

    # 阿明说别叫他：第二天老王成了头号收藏家，她只在回复里提一句，不 @。
    await chat.send("/我的卡 别叫我", user="u-ming")
    assert "/我的卡 叫我" in chat.said()
    chat.clock[0] += timedelta(days=1)
    await chat.send("/送卡 老王 1", user="u-hong")
    assert _ats(chat) == [] and "老王 刚成了 阿明 的头号收藏家" in _spoken(chat)[-1]

    # 没有新的里程碑就不提他。
    await chat.send("/送卡 小红 1", user="u-wang")
    assert "阿明" not in _spoken(chat)[-1].split("现在的情况")[1]

    # 阿明看自己的卡：卡面底下写着谁收藏了他；名单里头号收藏家排第一，没有他自己。
    await chat.send("/我的卡", user="u-ming")
    assert "3 个人" in chat.said()
    assert "被 3 人收藏 · 头号收藏家 新来的" in chat.drawn
    await chat.send("/我的卡 收藏家", user="u-ming")
    listing = chat.said()
    assert "1. 新来的" in listing and "小红" in listing and "老王" in listing
    assert "阿明：" not in listing

    # 又想被叫了：小红收下老王送的那张、成了头号收藏家，这回 @ 他。
    await chat.send("/我的卡 叫我", user="u-ming")
    chat.clock[0] += timedelta(days=1)
    _grant(plugin, "u-hong", 1, copies=1)
    await chat.send("/送卡 小红 1", user="u-new")
    assert _ats(chat) == ["10001"] and "小红 刚成了 阿明 的头号收藏家" in _spoken(chat)[-1]
