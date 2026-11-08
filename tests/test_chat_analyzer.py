"""群聊手帐验收：真实插件管线，假的只有模型、生图、头像下载和截图。

业务视角：
- 群友发 `/ca daily`、`/ca weekly`、`/ca analyse`，群里收到一张手帐图 + 她的一句话；
- 手帐里写着她给每段剧情起的标题、群友的称号、笑点和复读；排行榜里没有她自己；
- 模型写的标签不会以 HTML 形式进图；模型不在时照样出图，用程序写的那句话；
- 今日一画：同一天重复要图不重复画，超过每日额度就不画，关掉插画就不画；
- 定时：到了设置里的时间给每个够热闹的白名单群发一次，同一周期不重发，冷清群不打扰，周报只在设定的星期发；
- 归档按时间窗从尾部读，不会把窗口外的旧消息算进来。
"""

from __future__ import annotations

import base64
import io
import json
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from sirius_pulse.memory.basic import BasicMemoryFileStore
from sirius_pulse.memory.basic.models import BasicMemoryEntry
from sirius_pulse.platforms.onebot_v11.napcat.adapter import NapCatAdapter
from sirius_pulse.plugins.context import MessageContext
from sirius_pulse.plugins.dispatcher import OutputDispatcher
from sirius_pulse.plugins.executor import PluginExecutor
from sirius_pulse.plugins.lexer import parse_command
from sirius_pulse.plugins.loader import PluginLoader
from sirius_pulse.plugins.registry import PluginRegistry

GROUP = "730301"
OTHER_GROUP = "730302"
QUIET_GROUP = "730303"
BOT_QQ = "10000"
_CN = timezone(timedelta(hours=8))
#: 测试里的「现在」：周日晚上 22:30（周报默认周日 21:30、日报默认 22:00）
NOW = datetime(2026, 11, 8, 22, 30, tzinfo=_CN)

PEOPLE = [("10001", "小明"), ("10002", "阿离"), ("10003", "雀雀"), ("10004", "糖糖")]


def _png(color=(90, 80, 160)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (48, 64), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _entry(
    group_id: str, seq: int, when: datetime, qq: str, name: str, content: str, **extra
) -> BasicMemoryEntry:
    return BasicMemoryEntry(
        entry_id=f"{group_id}-{seq}",
        group_id=group_id,
        user_id=extra.pop("user_id", f"u-{qq}"),
        role=extra.pop("role", "human"),
        content=content,
        timestamp=when.isoformat(),
        speaker_name=name,
        channel_user_id=qq,
        **extra,
    )


def seed_lively(store: BasicMemoryFileStore, group_id: str, day: datetime) -> None:
    """一晚上的热闹群聊：有话题、有笑点、有复读、有 @，还有她自己的一句。"""
    base = day.replace(hour=20, minute=0, second=0, microsecond=0)
    lines = [
        ("10001", "今晚去吃烧烤吗"),
        ("10002", "烧烤好耶"),
        ("10003", "烧烤烧烤"),
        ("10001", "我请客烧烤"),
        ("10004", "我刚下班累死了"),
        ("10002", "@小明 你说的那家烧烤在哪"),
        ("10001", "就在公司楼下的烧烤摊"),
        ("10003", "上次那家烧烤老板把羊肉串烤成了煤球"),
        ("10002", "哈哈哈哈哈"),
        ("10004", "哈哈哈笑死"),
        ("10001", "哈哈哈哈"),
        ("10003", "冲冲冲"),
        ("10002", "冲冲冲"),
        ("10004", "冲冲冲"),
        ("10001", "【动画表情：AB12.jpg】"),
        ("10002", "[图片: x.png]"),
        ("10003", "烧烤配可乐才对"),
        ("10004", "可乐要冰的"),
        ("10001", "烧烤摊老板认识我"),
        ("10002", "那必须打折"),
    ]
    seq = 0
    for round_ in range(3):
        for offset, (qq, text) in enumerate(lines):
            when = base + timedelta(minutes=round_ * 30, seconds=offset * 40)
            name = dict(PEOPLE)[qq]
            store.append(_entry(group_id, seq, when, qq, name, text))
            seq += 1
    store.append(
        _entry(
            group_id,
            seq,
            base + timedelta(minutes=95),
            BOT_QQ,
            "月白",
            "我也想吃烧烤",
            user_id="assistant",
            role="assistant",
        )
    )
    # 前一天也聊过几句：日报要能和昨天比
    for i in range(6):
        store.append(
            _entry(group_id, 1000 + i, base - timedelta(days=1, minutes=i), "10001", "小明", "昨天的闲聊")
        )


def seed_quiet(store: BasicMemoryFileStore, group_id: str, day: datetime) -> None:
    base = day.replace(hour=21, minute=0)
    for i in range(3):
        store.append(_entry(group_id, i, base + timedelta(minutes=i), "10009", "路人", "有人吗"))


class Engine:
    """群聊手帐要用的那几样：归档、群名、她写字和画画的链路、主动发消息。"""

    def __init__(self, work_path: Path) -> None:
        self.work_path = work_path
        self.basic_store = BasicMemoryFileStore(work_path)
        self._bot_platform_uids = {"qq": BOT_QQ}
        self.persona = SimpleNamespace(name="月白", full_system_prompt="你是月白。")
        self.semantic_memory = SimpleNamespace(
            get_group_profile=lambda gid: SimpleNamespace(group_name="星空观测站")
        )
        self.prompts: list[str] = []
        self.image_prompts: list[str] = []
        self.writing: str | None = None
        self.brain = SimpleNamespace(
            generate_text=self._generate, generate_image_async=self._paint, persona_assets_dir=None
        )
        self.announced: list[dict] = []

    async def _generate(self, **kwargs):
        prompt = kwargs.get("system_prompt", "")
        self.prompts.append(prompt)
        if self.writing is not None:
            return self.writing
        ids = re.findall(r"【第 (\d+) 段】", prompt)
        return json.dumps(
            {
                "headline": "今晚大家约了烧烤，<b>小明</b>说要请客～",
                "chapters": [
                    {"id": int(i), "title": f"烧烤之约{i}", "summary": "大家约好下班去楼下烧烤摊。"} for i in ids
                ],
                "diary": "今天群里一直在聊烧烤，<img src=x>我也好想去。",
                "scene": "月白坐在烧烤摊边，手里拿着一串烤玉米，天上挂着月亮。",
            },
            ensure_ascii=False,
        )

    async def _paint(self, request):
        self.image_prompts.append(request.prompt)
        return SimpleNamespace(image_bytes=_png((250, 200, 210)), mime_type="image/png")

    async def dispatch_proactive_message(self, **kwargs):
        self.announced.append(kwargs)
        return True


@pytest.fixture(scope="module")
def definition():
    path = Path(__file__).resolve().parents[1] / "chat_analyzer"
    loaded = PluginLoader(path.parent).load_definition(path)
    assert loaded is not None, "真实 PluginLoader 未能加载群聊手帐插件"
    return loaded


class Rig:
    def __init__(self, definition, root: Path, monkeypatch) -> None:
        self.definition = definition
        self.engine = Engine(root)
        self.sent: list[list[dict]] = []
        self.pages: list[str] = []
        self.adapter = NapCatAdapter(
            "ws://qq.invalid/unconnected",
            work_path=root,
            config={"allowed_group_ids": [GROUP, OTHER_GROUP], "root": BOT_QQ},
        )
        self.adapter.call_api = self._capture
        self.registry = PluginRegistry()
        self.registry.register(definition)
        self.executor = PluginExecutor(
            self.registry, persona_data_path=root, adapter=self.adapter, engine=self.engine
        )
        self.monkeypatch = monkeypatch
        self.settings: dict = {}

    async def _capture(self, action, params):
        if action != "send_group_msg":
            return {"status": "failed", "retcode": 1, "data": None}
        self.sent.append(params["message"])
        return {"status": "ok", "retcode": 0, "data": {"message_id": 1}}

    async def plugin(self):
        instance = self.registry.get_instance("chat_analyzer")
        if instance is None:
            instance = await self.executor.instantiate(self.definition)
            self.registry.set_instance("chat_analyzer", instance)
        module = sys.modules[type(instance).__module__]

        async def _shot(html: str) -> bytes:
            self.pages.append(html)
            return _png()

        async def _no_network(uid: str):
            return _png((200, 220, 240))

        self.monkeypatch.setattr(module.page, "html_to_png", _shot)
        self.monkeypatch.setattr(module.avatars, "fetch_avatar", _no_network)
        self.monkeypatch.setattr(module, "_now", lambda: NOW)
        instance._settings = lambda: self.settings
        return instance

    async def send(self, text: str, *, group_id: str = GROUP) -> str:
        await self.plugin()
        self.sent.clear()
        cmd = parse_command(text, self.definition)
        assert cmd is not None, text
        responses = await self.executor.execute(
            self.definition.name,
            cmd,
            group_id=group_id,
            user_id="u-10001",
            engine=self.engine,
            message_context=MessageContext(
                group_id=group_id,
                user_id="u-10001",
                channel="qq",
                channel_user_id="10001",
                message_id="1",
                content=text,
                speaker_name="小明",
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
                user_id="u-10001",
            )
            message = output.message_group or output.text
            if message and not group_id.startswith("private_"):
                await self.adapter.send_group_message(group_id, message)
            replies.append(output.text or "")
        return "\n".join(replies)

    def said(self) -> str:
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
def rig(monkeypatch, definition):
    with tempfile.TemporaryDirectory(prefix="sirius-chat-diary-") as folder:
        rig = Rig(definition, Path(folder), monkeypatch)
        seed_lively(rig.engine.basic_store, GROUP, NOW)
        seed_lively(rig.engine.basic_store, OTHER_GROUP, NOW)
        seed_quiet(rig.engine.basic_store, QUIET_GROUP, NOW)
        yield rig


# ── 指令 ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    ["/ca daily", "/ca 日报", "/ca report daily", "/ca weekly", "/ca 周报", "/ca analyse 240"],
)
async def test_report_command_sends_diary_image_with_her_line(rig, command):
    await rig.send(command)

    assert len(rig.images()) == 1, rig.sent
    assert "烧烤" in rig.said() and "<b>" not in rig.said()
    assert len(rig.pages) == 1


@pytest.mark.asyncio
async def test_bare_command_explains_usage(rig):
    await rig.send("/ca")

    assert rig.images() == []
    assert "/ca daily" in rig.said() and "/ca weekly" in rig.said()


@pytest.mark.asyncio
async def test_daily_page_shows_story_titles_awards_and_moments_but_not_her(rig):
    await rig.send("/ca daily")
    html = rig.pages[0]

    assert "烧烤之约" in html  # 她给剧情起的标题
    assert "话痨之星" in html and "气氛组组长" in html  # 称号
    assert "冲冲冲" in html  # 复读
    assert "上次那家烧烤老板把羊肉串烤成了煤球" in html  # 引来笑声的那句
    assert "今日一画" in html  # 插画贴上了
    ranking = html.split("冒泡排行", 1)[1]
    assert "月白</span>" not in ranking, "她自己不该进排行"
    assert "<img src=x>" not in html and "<b>小明</b>" not in html


@pytest.mark.asyncio
async def test_weekly_page_has_forecast_calendar_and_constellation(rig):
    await rig.send("/ca weekly")
    html = rig.pages[0]

    assert "一周群聊天气" in html and "糖豆日历" in html and "群友星座" in html
    assert "第 45 周" in html


@pytest.mark.asyncio
async def test_without_model_she_still_sends_a_factual_line(rig):
    rig.engine.writing = "抱歉我现在不太想写"

    await rig.send("/ca daily")

    assert len(rig.images()) == 1
    assert "4 个人聊了" in rig.said()
    assert "她的日记" not in rig.pages[0]


@pytest.mark.asyncio
async def test_whitelist_and_private_chat_are_refused(rig):
    rig.settings = {"allowed_groups": ["999999"]}
    reply = await rig.send("/ca daily")
    assert rig.images() == [] and "白名单" in reply

    rig.settings = {}
    reply = await rig.send("/ca daily", group_id="private_10001")
    assert rig.images() == [] and "群里" in reply


# ── 今日一画 ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_illustration_is_reused_within_the_day(rig):
    await rig.send("/ca daily")
    await rig.send("/ca daily")

    assert len(rig.engine.image_prompts) == 1
    assert "烤玉米" in rig.engine.image_prompts[0]
    assert "不要出现任何文字" in rig.engine.image_prompts[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("settings", [{"illustration": False}, {"art_per_day": 0}])
async def test_illustration_can_be_turned_off_or_capped(rig, settings):
    rig.settings = settings

    await rig.send("/ca daily")

    assert rig.engine.image_prompts == []
    assert len(rig.images()) == 1
    assert "今日一画" not in rig.pages[0]


# ── 定时 ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scheduled_daily_goes_once_to_each_lively_group(rig):
    instance = await rig.plugin()
    rig.settings = {"weekly_enabled": False}

    first = await instance.tick(NOW.replace(hour=22, minute=1))
    again = await instance.tick(NOW.replace(hour=22, minute=20))

    assert sorted(first) == [("day", GROUP), ("day", OTHER_GROUP)]
    assert again == []
    assert sorted(item["group_id"] for item in rig.engine.announced) == [GROUP, OTHER_GROUP]
    for item in rig.engine.announced:
        assert Path(item["image_path"]).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
        assert "烧烤" in item["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("settings", "when", "expected"),
    [
        ({"weekly_enabled": False}, (21, 59), []),  # 还没到点
        ({"weekly_enabled": False, "daily_time": "19:00"}, (22, 1), []),  # 过了补发窗口
        ({"weekly_enabled": False, "allowed_groups": [GROUP]}, (22, 1), [("day", GROUP)]),
        (
            {"daily_enabled": False, "min_messages": 10},
            (21, 31),
            [("week", GROUP), ("week", OTHER_GROUP)],
        ),
        ({"daily_enabled": False}, (21, 31), []),  # 一周才 60 条，不到默认门槛的 3 倍
        ({"daily_enabled": False, "weekly_day": 6}, (21, 31), []),  # 今天是周日，周报设在周六
    ],
)
async def test_schedule_respects_time_whitelist_and_weekday(rig, settings, when, expected):
    instance = await rig.plugin()
    rig.settings = settings

    sent = await instance.tick(NOW.replace(hour=when[0], minute=when[1]))

    assert sorted(sent) == expected


# ── 数据 ───────────────────────────────────────────────────────────────


def test_archive_window_reads_only_the_window(tmp_path):
    from plugins.chat_analyzer import source

    archive = tmp_path / "g.jsonl"
    base = datetime(2026, 11, 1, tzinfo=_CN)
    with archive.open("w", encoding="utf-8") as handle:
        for i in range(3000):
            when = base + timedelta(minutes=i)
            handle.write(
                json.dumps({"timestamp": when.isoformat(), "content": str(i), "pad": "x" * 300})
                + "\n"
            )

    start = base + timedelta(minutes=2500)
    rows = source.read_archive_window(
        archive, int(start.timestamp()), int((start + timedelta(minutes=99)).timestamp())
    )

    assert [r["content"] for r in rows] == [str(i) for i in range(2500, 2600)]


def test_messages_are_understood_as_text_sticker_image_command():
    from plugins.chat_analyzer import source

    kinds = [
        source.classify(text)[0]
        for text in ("哈哈", "【动画表情：AB.jpg】", "[图片: a.png]", "/ca daily", "[表情：271]")
    ]

    assert kinds == ["text", "sticker", "image", "command", "sticker"]


def test_topic_bubbles_skip_member_names_and_filler():
    from plugins.chat_analyzer import metrics

    texts = ["临雀哥哥今天又在改代码", "代码又炸了", "雀哥哥的代码", "就是这个代码", "临雀在吗", "代码代码"] * 2
    words = [w for w, _ in metrics.extract_keywords(texts, names=["临雀。"], min_count=2)]

    assert "代码" in words
    assert not any("雀" in w for w in words)
    assert "就是" not in words


@pytest.mark.parametrize(
    ("count", "mood", "weather"),
    [
        (0, (0, 0, 0), "moon"),
        (5, (0, 0, 0), "breeze"),
        (300, (0.2, 0.01, 0.05), "rainbow"),
        (60, (0.1, 0.02, 0.0), "sunny"),
        (60, (0.01, 0.1, 0.0), "drizzle"),
        (400, (0.01, 0.1, 0.0), "storm"),
        (60, (0.0, 0.0, 0.0), "cloudy"),
    ],
)
def test_group_weather_follows_mood_and_liveliness(count, mood, weather):
    from plugins.chat_analyzer import metrics

    info = {"positive": mood[0], "negative": mood[1], "laugh": mood[2]}

    assert metrics.weather(count, info) == weather
    assert metrics.temperature(30) < metrics.temperature(300) < metrics.temperature(3000) <= 39
