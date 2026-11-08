"""把一段群聊算成手帐上的东西：天气、群温、花朵时钟、称号、笑点、CP、剧情章节、话题泡泡。

全部是纯函数，只吃 :class:`~.source.Msg` 列表，方便测试；昵称、头像、她的文字由插件层补。
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, Sequence

from .source import CN_TZ, KIND_IMAGE, KIND_STICKER, Msg

# ── 词表 ───────────────────────────────────────────────────────────────

_LAUGH_RE = re.compile(r"哈{2,}|h{3,}|233+|笑死|xswl|绷不住|蚌埠住|乐死|草{1,3}$|笑不活|wwww", re.I)
_QUESTION_RE = re.compile(r"[?？]|吗$|么$|呢$|啥|什么|怎么|为啥|咋")
_POSITIVE = (
    "哈哈",
    "好耶",
    "可爱",
    "喜欢",
    "谢谢",
    "感谢",
    "厉害",
    "牛",
    "绝了",
    "好看",
    "好吃",
    "开心",
    "舒服",
    "爱了",
    "太棒",
    "不错",
    "好听",
    "恭喜",
    "冲",
    "贴贴",
    "晚安",
    "早安",
    "辛苦",
)
_NEGATIVE = (
    "烦",
    "累",
    "难受",
    "哭",
    "寄",
    "崩",
    "气死",
    "无语",
    "唉",
    "炸了",
    "完蛋",
    "麻了",
    "破防",
    "emo",
    "难过",
    "生气",
    "痛",
    "裂开",
    "倒霉",
    "离谱",
    "焦虑",
    "失眠",
    "加班",
)
_STOP_GRAMS = frozenset(
    """什么 怎么 这个 那个 就是 不是 没有 可以 一个 我们 你们 他们 她们 现在 感觉 知道 还是 然后 因为
    所以 真的 已经 可能 时候 自己 这么 那么 不会 不要 应该 一下 一样 东西 为什么 有点 好像 觉得 其实
    还有 但是 如果 直接 一直 这样 那样 不过 而且 只是 只有 怎么办 不行 不能 出来 起来 一点 这种 那种
    是不是 有没有 要不 对吧 是吧 吧吧 啊啊 呢呢 哦哦 嗯嗯 好的 好吧 确实 是的 不知道 没事 那就 就行
    这边 那边 的话 的时候 大家 今天 明天 昨天 刚才 他的 我的 你的 她的 这是 那是 也是 都是 还是
    人家 有人 没人 怎样 多少 一些 一会 有些 还要 想要 需要 不用 不了 了吧 了啊 了吗 一起 这里 那里""".split()
)
_FUNCTION_CHARS = set("的了是我你他她它这那就都也还在有吗吧啊呢么嘛呀哦嗯哈嘿诶欸噢喔啦咯哇")
_BARE_HONORIFICS = frozenset({"大人", "哥哥", "姐姐", "妹妹", "弟弟", "宝宝", "大佬", "主人"})
_HONORIFIC_TAIL = "(?:哥哥|姐姐|妹妹|弟弟|大人|老师|酱|君|哥|姐|宝|佬)"
_HONORIFIC_RE = re.compile(r"^(.{1,3}?)(哥哥|姐姐|妹妹|弟弟|大人|老师|酱|君|哥|姐|宝|佬)$")
_CJK_RUN = re.compile(r"[一-鿿]{2,}")
_ASCII_WORD = re.compile(r"[A-Za-z][A-Za-z0-9+#.\-]{2,19}")
_ASCII_STOP = frozenset(
    {"the", "and", "you", "for", "but", "not", "are", "this", "that", "www", "com", "http", "https"}
)

# ── 结构 ───────────────────────────────────────────────────────────────


@dataclass(slots=True)
class Person:
    uid: str
    name: str
    count: int = 0
    chars: int = 0
    text_count: int = 0
    stickers: int = 0
    images: int = 0
    laughs: int = 0
    questions: int = 0
    starters: int = 0
    replies: int = 0
    mentioned: int = 0
    first_ts: int = 0
    last_ts: int = 0
    days: set[str] = field(default_factory=set)
    late_ts: int = 0  # 最晚的一条「夜里」发言（0-4 点算前一天的深夜）
    early_ts: int = 0  # 最早的一条「早上」发言（5-10 点）
    hours: Counter = field(default_factory=Counter)


@dataclass(slots=True)
class Session:
    """一段连续的聊天（中间沉默不超过阈值）。"""

    index: int
    start: int
    end: int
    messages: list[Msg]
    people: list[str]  # 按发言数排的 uid
    keywords: list[str]

    @property
    def count(self) -> int:
        return len(self.messages)


# ── 小工具 ─────────────────────────────────────────────────────────────


def is_laugh(text: str) -> bool:
    return bool(text) and bool(_LAUGH_RE.search(text))


def mood_of(text: str) -> int:
    """+1 / 0 / -1：一句话偏开心还是偏丧。"""
    if not text:
        return 0
    pos = sum(1 for word in _POSITIVE if word in text) + (1 if is_laugh(text) else 0)
    neg = sum(1 for word in _NEGATIVE if word in text)
    return (pos > neg) - (neg > pos)


def fmt_clock(ts: int) -> str:
    return datetime.fromtimestamp(ts, CN_TZ).strftime("%H:%M") if ts else "--:--"


def _night_key(ts: int) -> tuple[int, int]:
    """把 0-4 点挪到 24-28 点，比较「谁睡得最晚」。"""
    local = datetime.fromtimestamp(ts, CN_TZ)
    hour = local.hour + (24 if local.hour < 5 else 0)
    return hour, local.minute


# ── 关键词：无分词的 n-gram 抽取 ─────────────────────────────────────


def _name_runs(names: Iterable[str]) -> list[str]:
    return [run for name in names for run in re.findall(r"[一-鿿]+", str(name or ""))]


def name_tokens(names: Iterable[str]) -> set[str]:
    """把群友昵称拆成用来屏蔽的小片段：汉字两两一组、英文单词整词。

    话题泡泡里不该全是人名（「临雀」「前世哥哥」这种），人名已经在称号和 CP 里露脸了。
    """
    tokens: set[str] = set()
    for name in names:
        for run in _CJK_RUN.findall(str(name or "")):
            tokens.update(run[i : i + 2] for i in range(len(run) - 1))
        for word in _ASCII_WORD.findall(str(name or "")):
            tokens.add(word.lower())
    return tokens


def extract_keywords(
    texts: Iterable[str],
    *,
    blocked: Iterable[str] = (),
    names: Iterable[str] = (),
    top: int = 14,
    min_count: int = 3,
) -> list[tuple[str, int]]:
    """从一堆短句里抽出群里反复出现的词。

    不依赖分词库：每句话的汉字串切 2–4 字 n-gram（同一句只计一次），去掉虚词串和常见口水词，
    再让长词吞掉几乎总和它一起出现的短词（「月白姐姐」出现时「月白」不再单独占位）。
    """
    blocked_set = {word.strip().lower() for word in blocked if str(word).strip()}
    names = list(names)
    name_set = name_tokens(names)
    name_runs = _name_runs(names)
    grams: Counter[str] = Counter()
    for text in texts:
        if not text:
            continue
        seen: set[str] = set()
        for run in _CJK_RUN.findall(text):
            for size in (2, 3, 4, 5, 6):
                for i in range(len(run) - size + 1):
                    gram = run[i : i + size]
                    if gram in seen:
                        continue
                    seen.add(gram)
        for word in _ASCII_WORD.findall(text):
            lowered = word.lower().strip(".-")
            if len(lowered) >= 3 and lowered not in _ASCII_STOP:
                seen.add(lowered)
        grams.update(seen)

    def useful(gram: str) -> bool:
        if gram in _STOP_GRAMS or gram in _BARE_HONORIFICS or gram.lower() in blocked_set:
            return False
        if any(block and (block in gram or gram in block) for block in blocked_set):
            return False
        if gram.isascii():
            return gram.lower() not in name_set
        if any(gram[i : i + 2] in name_set for i in range(len(gram) - 1)):
            return False
        honorific = _HONORIFIC_RE.match(gram)
        if honorific and any(run.endswith(honorific.group(1)) for run in name_runs):
            return False  # 「雀哥哥」「世哥哥」：昵称尾字 + 称呼
        if any(re.search(re.escape(run[-1]) + _HONORIFIC_TAIL, gram) for run in name_runs if run):
            return False  # 「雀哥哥今」这类夹着昵称尾字 + 称呼的半截词
        if re.match(r"^(哥|姐|妹妹|弟弟|人家|才不|要不)", gram):
            return False
        if len(gram) >= 5 and grams[gram] < 3:
            return False
        if gram[0] in _FUNCTION_CHARS or gram[-1] in _FUNCTION_CHARS:
            return False
        if len(set(gram)) == 1:
            return False
        if is_laugh(gram):
            return False
        return True

    candidates = {
        gram: count for gram, count in grams.items() if count >= min_count and useful(gram)
    }
    # 长词吞短词：短词的出现几乎都来自某个长词 → 丢短词；长词明显少于短词 → 丢长词
    ranked = sorted(candidates.items(), key=lambda item: (-len(item[0]), -item[1]))
    kept: dict[str, int] = {}
    for gram, count in ranked:
        absorbed = False
        for longer, longer_count in kept.items():
            if gram in longer and longer_count >= count * 0.6:
                absorbed = True
                break
        if not absorbed:
            kept[gram] = count
    result = sorted(kept.items(), key=lambda item: (-item[1], -len(item[0]), item[0]))
    # 去掉被更高频短词包住、但出现得少很多的长词（「今天吃什」这类半截词）
    final: list[tuple[str, int]] = []
    for gram, count in result:
        if any(short in gram and short != gram and c >= count * 1.8 for short, c in final):
            continue
        final.append((gram, count))
        if len(final) >= top:
            break
    return final


# ── 核心统计 ───────────────────────────────────────────────────────────


def people_stats(messages: Sequence[Msg], *, silence_gap: int = 30 * 60) -> dict[str, Person]:
    """每个群友的发言画像（不含她自己）。"""
    people: dict[str, Person] = {}
    previous: Msg | None = None
    for msg in messages:
        if msg.is_bot:
            previous = msg
            continue
        person = people.get(msg.uid)
        if person is None:
            person = people[msg.uid] = Person(uid=msg.uid, name=msg.name or f"qq_{msg.uid}")
        elif msg.name:
            person.name = msg.name
        person.count += 1
        person.hours[msg.hour] += 1
        person.days.add(msg.local.strftime("%Y-%m-%d"))
        if not person.first_ts:
            person.first_ts = msg.ts
        person.last_ts = msg.ts
        if msg.kind == KIND_STICKER:
            person.stickers += 1
        elif msg.kind == KIND_IMAGE:
            person.images += 1
        if msg.is_chat_text:
            person.text_count += 1
            person.chars += len(msg.text)
            if is_laugh(msg.text):
                person.laughs += 1
            if _QUESTION_RE.search(msg.text):
                person.questions += 1
        hour = msg.hour
        if hour < 5 or hour >= 23:
            if not person.late_ts or _night_key(msg.ts) >= _night_key(person.late_ts):
                person.late_ts = msg.ts
        if 5 <= hour < 10:
            if (
                not person.early_ts
                or msg.local.time() < datetime.fromtimestamp(person.early_ts, CN_TZ).time()
            ):
                person.early_ts = msg.ts
        if previous is None or msg.ts - previous.ts >= silence_gap:
            person.starters += 1
        elif previous.uid != msg.uid and not previous.is_bot and msg.ts - previous.ts <= 90:
            person.replies += 1
        for target in msg.mentions:
            people.setdefault(target, Person(uid=target, name=f"qq_{target}"))
        previous = msg
    for msg in messages:
        if msg.is_bot:
            continue
        for target in msg.mentions:
            if target in people:
                people[target].mentioned += 1
    # 只被 @、自己没说话的人不进榜
    return {uid: p for uid, p in people.items() if p.count > 0}


def hourly(messages: Sequence[Msg]) -> list[int]:
    bins = [0] * 24
    for msg in messages:
        if not msg.is_bot:
            bins[msg.hour] += 1
    return bins


def peak_window(messages: Sequence[Msg], width: int = 600) -> tuple[int, int]:
    """最热闹的 10 分钟：返回 (起点 ts, 条数)。"""
    stamps = [m.ts for m in messages if not m.is_bot]
    best_count, best_start = 0, 0
    left = 0
    for right, ts in enumerate(stamps):
        while ts - stamps[left] > width:
            left += 1
        if right - left + 1 > best_count:
            best_count, best_start = right - left + 1, stamps[left]
    return best_start, best_count


def sessions(
    messages: Sequence[Msg],
    *,
    gap: int = 20 * 60,
    min_count: int = 6,
    blocked: Iterable[str] = (),
    names: Iterable[str] = (),
) -> list[Session]:
    """按沉默切段，留下像样的几段对话，按时间排序。"""
    blocks: list[list[Msg]] = []
    current: list[Msg] = []
    for msg in messages:
        if current and msg.ts - current[-1].ts > gap:
            blocks.append(current)
            current = []
        current.append(msg)
    if current:
        blocks.append(current)

    blocked = list(blocked)
    names = list(names)
    result: list[Session] = []
    for block in blocks:
        humans = [m for m in block if not m.is_bot]
        if len(humans) < min_count:
            continue
        ranking = Counter(m.uid for m in humans)
        keywords = [
            word
            for word, _ in extract_keywords(
                (m.text for m in humans if m.is_chat_text),
                blocked=blocked,
                names=names,
                top=4,
                min_count=2,
            )
        ]
        result.append(
            Session(
                index=len(result),
                start=block[0].ts,
                end=block[-1].ts,
                messages=block,
                people=[uid for uid, _ in ranking.most_common()],
                keywords=keywords,
            )
        )
    return result


def laugh_moments(
    messages: Sequence[Msg], *, window: int = 120, top: int = 3
) -> list[dict[str, Any]]:
    """笑点：一句正常发言之后，别人在两分钟内接连笑出声。"""
    moments: list[dict[str, Any]] = []
    for index, msg in enumerate(messages):
        if msg.is_bot or not msg.is_chat_text or is_laugh(msg.text) or len(msg.text) < 4:
            continue
        laughers: set[str] = set()
        laughs = 0
        for follow in messages[index + 1 : index + 16]:
            if follow.ts - msg.ts > window:
                break
            if follow.uid == msg.uid or follow.is_bot:
                continue
            if follow.is_chat_text and is_laugh(follow.text):
                laughs += 1
                laughers.add(follow.uid)
        if len(laughers) >= 2 or laughs >= 3:
            moments.append(
                {
                    "uid": msg.uid,
                    "name": msg.name,
                    "text": msg.text[:60],
                    "ts": msg.ts,
                    "laughs": laughs,
                    "laughers": len(laughers),
                }
            )
    moments.sort(key=lambda item: (-item["laughers"], -item["laughs"], item["ts"]))
    picked: list[dict[str, Any]] = []
    for moment in moments:
        if any(
            abs(moment["ts"] - other["ts"]) < window or moment["text"] == other["text"]
            for other in picked
        ):
            continue
        picked.append(moment)
        if len(picked) >= top:
            break
    return picked


def echoes(messages: Sequence[Msg], *, window: int = 300, top: int = 3) -> list[dict[str, Any]]:
    """复读：同一句话（≤20 字）在五分钟里被至少三个人发出来。"""
    chains: list[dict[str, Any]] = []
    open_chains: dict[str, dict[str, Any]] = {}
    for msg in messages:
        if msg.is_bot or not msg.is_chat_text or len(msg.text) > 20:
            continue
        key = msg.text
        chain = open_chains.get(key)
        if chain and msg.ts - chain["last"] <= window:
            chain["uids"].add(msg.uid)
            chain["times"] += 1
            chain["last"] = msg.ts
        else:
            chain = {
                "text": key,
                "uids": {msg.uid},
                "times": 1,
                "first": msg.ts,
                "last": msg.ts,
                "starter": msg.name,
            }
            open_chains[key] = chain
            chains.append(chain)
    good = [c for c in chains if len(c["uids"]) >= 3]
    good.sort(key=lambda c: (-len(c["uids"]), -c["times"]))
    return [
        {
            "text": c["text"],
            "people": len(c["uids"]),
            "times": c["times"],
            "ts": c["first"],
            "starter": c["starter"],
        }
        for c in good[:top]
    ]


def pair_scores(messages: Sequence[Msg], *, window: int = 120) -> Counter:
    """两人之间的来回：挨着说话（两分钟内）算 1，@ 对方算 2。key 是排好序的 (uid, uid)。"""
    scores: Counter = Counter()
    previous: Msg | None = None
    for msg in messages:
        if msg.is_bot:
            previous = None
            continue
        if previous is not None and previous.uid != msg.uid and msg.ts - previous.ts <= window:
            scores[tuple(sorted((previous.uid, msg.uid)))] += 1
        for target in msg.mentions:
            if target != msg.uid:
                scores[tuple(sorted((msg.uid, target)))] += 2
        previous = msg
    return scores


def top_pairs(scores: Counter, people: dict[str, Person], *, top: int = 3) -> list[dict[str, Any]]:
    """CP 榜：互动最多的几对，尽量不让同一个人霸榜。"""
    picked: list[dict[str, Any]] = []
    used: Counter = Counter()
    for (a, b), score in scores.most_common():
        if a not in people or b not in people or score < 3:
            continue
        if used[a] >= 1 and used[b] >= 1:
            continue
        picked.append({"a": a, "b": b, "score": score})
        used[a] += 1
        used[b] += 1
        if len(picked) >= top:
            break
    return picked


def mood(messages: Sequence[Msg]) -> dict[str, float]:
    pos = neg = total = laughs = 0
    for msg in messages:
        if msg.is_bot or not msg.is_chat_text:
            continue
        total += 1
        value = mood_of(msg.text)
        pos += value > 0
        neg += value < 0
        laughs += is_laugh(msg.text)
    return {
        "positive": pos / total if total else 0.0,
        "negative": neg / total if total else 0.0,
        "laugh": laughs / total if total else 0.0,
        "laughs": float(laughs),
        "texts": float(total),
    }


# ── 群聊天气 ───────────────────────────────────────────────────────────

WEATHERS = {
    "sunny": "晴",
    "rainbow": "彩虹",
    "cloudy": "多云",
    "breeze": "微风",
    "drizzle": "小雨",
    "storm": "雷阵雨",
    "moon": "静夜",
}


def temperature(count: int, hours: float = 24.0) -> int:
    """群温：把消息量折成一个看着舒服的温度，一天 30 条约 15°，300 条约 28°，千条约 35°。"""
    per_day = count * 24.0 / max(hours, 1.0)
    return int(round(min(39.0, max(-5.0, 3.0 + 4.2 * math.log2(1 + per_day / 5.0)))))


def weather(count: int, mood_info: dict[str, float], *, quiet: int = 20) -> str:
    """开心和丧气的比例定晴雨，热闹程度定雨势和彩虹。"""
    if count < quiet:
        return "moon" if count == 0 else "breeze"
    pos, neg = mood_info["positive"], mood_info["negative"]
    balance = (pos - neg) / (pos + neg) if pos + neg > 0 else 0.0
    if balance <= -0.15 and neg >= 0.02:
        return "storm" if count >= 200 else "drizzle"
    if balance >= 0.4 and pos >= 0.06 and mood_info["laugh"] >= 0.02 and count >= 120:
        return "rainbow"
    if balance >= 0.2:
        return "sunny"
    return "cloudy"


# ── 称号 ───────────────────────────────────────────────────────────────

AWARD_ORDER = (
    "chatter",
    "mood_maker",
    "icebreaker",
    "catcher",
    "popular",
    "sticker",
    "night_owl",
    "early_bird",
    "curious",
    "writer",
    "photographer",
)

AWARD_TITLES = {
    "chatter": "话痨之星",
    "mood_maker": "气氛组组长",
    "icebreaker": "破冰小能手",
    "catcher": "接话王",
    "popular": "人气王",
    "sticker": "表情包仓库",
    "night_owl": "夜猫子",
    "early_bird": "早起鸟",
    "curious": "好奇宝宝",
    "writer": "长文作家",
    "photographer": "随手摄影师",
}


def awards(
    people: dict[str, Person], *, limit: int = 6, weekly: bool = False
) -> list[dict[str, Any]]:
    """给群友发称号：每个人最多拿一个，让更多人上榜。"""
    scale = 3 if weekly else 1
    rules: dict[str, tuple[Any, Any, int]] = {
        # key: (排序值, 文案, 最低门槛)
        "chatter": (lambda p: p.count, lambda p: f"说了 {p.count} 句", 5 * scale),
        "mood_maker": (lambda p: p.laughs, lambda p: f"笑了 {p.laughs} 次", 3 * scale),
        "icebreaker": (lambda p: p.starters, lambda p: f"{p.starters} 次把群叫醒", 2 * scale),
        "catcher": (lambda p: p.replies, lambda p: f"接住 {p.replies} 次话头", 5 * scale),
        "popular": (lambda p: p.mentioned, lambda p: f"被 @ 了 {p.mentioned} 次", 2 * scale),
        "sticker": (lambda p: p.stickers, lambda p: f"甩出 {p.stickers} 张表情", 3 * scale),
        "night_owl": (
            lambda p: _night_key(p.late_ts)[0] * 60 + _night_key(p.late_ts)[1] if p.late_ts else 0,
            lambda p: f"{fmt_clock(p.late_ts)} 还在线",
            1,
        ),
        "early_bird": (
            lambda p: (24 * 60 - _minutes(p.early_ts)) if p.early_ts else 0,
            lambda p: f"{fmt_clock(p.early_ts)} 就来了",
            1,
        ),
        "curious": (lambda p: p.questions, lambda p: f"问了 {p.questions} 个问题", 4 * scale),
        "writer": (
            lambda p: round(p.chars / p.text_count) if p.text_count >= 5 * scale else 0,
            lambda p: f"平均一句 {round(p.chars / max(p.text_count, 1))} 字",
            14,
        ),
        "photographer": (lambda p: p.images, lambda p: f"分享 {p.images} 张图", 3 * scale),
    }
    taken: set[str] = set()
    result: list[dict[str, Any]] = []
    for key in AWARD_ORDER:
        value_of, describe, floor = rules[key]
        ranked = sorted(people.values(), key=lambda p: (-value_of(p), -p.count, p.uid))
        for person in ranked:
            value = value_of(person)
            if value < floor or value <= 0:
                break
            if person.uid in taken:
                continue
            taken.add(person.uid)
            result.append(
                {
                    "key": key,
                    "title": AWARD_TITLES[key],
                    "uid": person.uid,
                    "name": person.name,
                    "detail": describe(person),
                }
            )
            break
        if len(result) >= limit:
            break
    return result


def _minutes(ts: int) -> int:
    local = datetime.fromtimestamp(ts, CN_TZ)
    return local.hour * 60 + local.minute


# ── 日报 / 周报 ────────────────────────────────────────────────────────


def speaker_names(messages: Iterable[Msg]) -> list[str]:
    """说过话的人和被 @ 到的人的名字：话题泡泡里不该出现人名。"""
    names: list[str] = []
    for msg in messages:
        if msg.name:
            names.append(msg.name)
        names.extend(msg.mention_names)
    return list(dict.fromkeys(names))


def _delta(now: int, before: int | None) -> float | None:
    if before is None or before <= 0:
        return None
    return (now - before) / before


def _session_dict(session: Session, people: dict[str, Person]) -> dict[str, Any]:
    return {
        "index": session.index,
        "start": session.start,
        "end": session.end,
        "count": session.count,
        "people": [uid for uid in session.people if uid in people][:5],
        "keywords": session.keywords,
    }


DAYPARTS = (("凌晨", 0, 6), ("上午", 6, 12), ("下午", 12, 18), ("晚上", 18, 24))


def extras(
    messages: Sequence[Msg],
    people: dict[str, Person],
    all_sessions: Sequence[Session],
    scores: Counter,
    keywords: Sequence[tuple[str, int]],
) -> dict[str, Any]:
    """卡片底部那一行小统计用的口径：时段分布、首末发言、笑声、互动、人群结构、节奏。"""
    humans = [m for m in messages if not m.is_bot]
    total = len(humans) or 1

    hours = hourly(messages)
    dayparts = [
        {"label": label, "count": sum(hours[lo:hi]), "share": sum(hours[lo:hi]) / total}
        for label, lo, hi in DAYPARTS
    ]
    first = humans[0] if humans else None
    last = humans[-1] if humans else None

    laughers = Counter(m.uid for m in humans if m.is_chat_text and is_laugh(m.text))
    laugh_hours = Counter(m.hour for m in humans if m.is_chat_text and is_laugh(m.text))
    texts = sum(1 for m in humans if m.is_chat_text) or 1

    partners: dict[str, set[str]] = defaultdict(set)
    for a, b in scores:
        partners[a].add(b)
        partners[b].add(a)
    hub = max(
        (uid for uid in partners if uid in people),
        key=lambda uid: (len(partners[uid]), people[uid].count),
        default="",
    )
    gaps = sorted(
        b.ts - a.ts
        for a, b in zip(humans, humans[1:])
        if a.uid != b.uid and 0 <= b.ts - a.ts <= 300
    )

    counts = sorted((p.count for p in people.values()), reverse=True)
    silence = {"minutes": 0, "start": 0, "end": 0}
    for a, b in zip(humans, humans[1:]):
        minutes = (b.ts - a.ts) // 60
        if minutes > silence["minutes"]:
            silence = {"minutes": minutes, "start": a.ts, "end": b.ts}
    longest = max(all_sessions, key=lambda s: s.end - s.start, default=None)

    # 她自己的今天：说了几句、接了谁的话、什么时候最活跃
    answered: Counter = Counter()
    her_hours: Counter = Counter()
    previous_human: Msg | None = None
    for msg in messages:
        if msg.is_bot:
            her_hours[msg.hour] += 1
            if previous_human is not None and 0 <= msg.ts - previous_human.ts <= 120:
                answered[previous_human.uid] += 1
        else:
            previous_human = msg
    favorite = next((uid for uid, _ in answered.most_common() if uid in people), "")

    return {
        "her": {
            "count": sum(her_hours.values()),
            "answered_people": len([uid for uid in answered if uid in people]),
            "favorite_uid": favorite,
            "favorite_count": answered.get(favorite, 0),
            "peak_hour": her_hours.most_common(1)[0][0] if her_hours else None,
        },
        "dayparts": dayparts,
        "first": {"ts": first.ts, "uid": first.uid, "name": first.name} if first else None,
        "last": {"ts": last.ts, "uid": last.uid, "name": last.name} if last else None,
        "laugh_stats": {
            "total": sum(laughers.values()),
            "share": sum(laughers.values()) / texts,
            "top_uid": laughers.most_common(1)[0][0] if laughers else "",
            "top_count": laughers.most_common(1)[0][1] if laughers else 0,
            "peak_hour": laugh_hours.most_common(1)[0][0] if laugh_hours else None,
        },
        "social": {
            "links": sum(scores.values()),
            "hub_uid": hub,
            "hub_partners": len(partners.get(hub, ())),
            "mentions": sum(len(m.mentions) for m in humans),
            "reply_median": gaps[len(gaps) // 2] if gaps else None,
        },
        "crowd": {
            "top3_share": sum(counts[:3]) / total if counts else 0.0,
            "average": total / len(counts) if counts else 0.0,
            "passers": sum(1 for c in counts if c <= 2),
            "chars_per_msg": sum(len(m.text) for m in humans if m.is_chat_text) / texts,
        },
        "rhythm": {
            "sessions": len(all_sessions),
            "longest_minutes": (longest.end - longest.start) // 60 if longest else 0,
            "silence": silence,
        },
        "more_words": [word for word, _ in keywords[12:20]],
    }


def build_day(
    messages: Sequence[Msg],
    *,
    start: datetime,
    end: datetime,
    previous_count: int | None = None,
    blocked: Iterable[str] = (),
) -> dict[str, Any]:
    """日报数据（不含她写的字）。"""
    blocked = list(blocked)
    names = speaker_names(messages)
    humans = [m for m in messages if not m.is_bot]
    people = people_stats(messages)
    mood_info = mood(messages)
    hours = max((end - start).total_seconds() / 3600.0, 1.0)
    all_sessions = sessions(messages, blocked=blocked, names=names)
    top_sessions = sorted(all_sessions, key=lambda s: -s.count)[:4]
    top_sessions.sort(key=lambda s: s.start)
    peak_start, peak_count = peak_window(messages)
    keywords = extract_keywords(
        (m.text for m in humans if m.is_chat_text), blocked=blocked, names=names
    )
    ranking = sorted(people.values(), key=lambda p: (-p.count, p.uid))
    scores = pair_scores(messages)
    return {
        **extras(messages, people, all_sessions, scores, keywords),
        "kind": "day",
        "start": int(start.timestamp()),
        "end": int(end.timestamp()),
        "total": len(humans),
        "bot_count": sum(1 for m in messages if m.is_bot),
        "people_count": len(people),
        "delta": _delta(len(humans), previous_count),
        "previous_count": previous_count,
        "temperature": temperature(len(humans), hours),
        "weather": weather(len(humans), mood_info, quiet=max(8, int(20 * hours / 24))),
        "mood": mood_info,
        "hourly": hourly(messages),
        "peak": {"start": peak_start, "count": peak_count},
        "people": people,
        "ranking": [p.uid for p in ranking],
        "awards": awards(people, limit=8),
        "laughs": laugh_moments(messages, top=4),
        "echoes": echoes(messages, top=3),
        "pairs": top_pairs(scores, people, top=4),
        "keywords": keywords,
        "sessions": [_session_dict(s, people) for s in top_sessions],
        "session_objs": top_sessions,
        "stickers": sum(1 for m in humans if m.kind == KIND_STICKER),
        "images": sum(1 for m in humans if m.kind == KIND_IMAGE),
        "chars": sum(len(m.text) for m in humans if m.is_chat_text),
    }


def build_week(
    messages: Sequence[Msg],
    *,
    start: datetime,
    end: datetime,
    previous: Sequence[Msg] = (),
    blocked: Iterable[str] = (),
) -> dict[str, Any]:
    """周报数据：在日报指标之上加一周天气、7×24 糖豆日历、全勤、新面孔、话题升温、群友星座。"""
    blocked = list(blocked)
    report = build_day(messages, start=start, end=end, previous_count=None, blocked=blocked)
    report["kind"] = "week"
    humans = [m for m in messages if not m.is_bot]
    previous_humans = [m for m in previous if not m.is_bot]
    report["previous_count"] = len(previous_humans) if previous else None
    report["delta"] = _delta(len(humans), len(previous_humans) if previous else None)
    report["awards"] = awards(report["people"], weekly=True, limit=8)
    report["pairs"] = top_pairs(pair_scores(messages), report["people"], top=5)

    # 一周天气预报
    by_day: dict[str, list[Msg]] = defaultdict(list)
    for msg in messages:
        by_day[msg.local.strftime("%Y-%m-%d")].append(msg)
    days: list[dict[str, Any]] = []
    cursor = start.astimezone(CN_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    while cursor < end and len(days) < 7:
        key = cursor.strftime("%Y-%m-%d")
        day_msgs = by_day.get(key, [])
        count = sum(1 for m in day_msgs if not m.is_bot)
        info = mood(day_msgs)
        days.append(
            {
                "date": key,
                "weekday": cursor.weekday(),
                "count": count,
                "weather": weather(count, info),
                "temperature": temperature(count),
            }
        )
        cursor += timedelta(days=1)
    report["days"] = days

    # 7×24 糖豆日历
    grid = [[0] * 24 for _ in range(7)]
    for msg in humans:
        grid[msg.local.weekday()][msg.hour] += 1
    report["grid"] = grid
    weekday_counts = [d["count"] for d in days if d["weekday"] < 5]
    weekend_counts = [d["count"] for d in days if d["weekday"] >= 5]
    hot_day, hot_hour = max(
        ((r, h) for r in range(7) for h in range(24)), key=lambda rh: grid[rh[0]][rh[1]]
    )
    report["week_rhythm"] = {
        "weekday_avg": sum(weekday_counts) / len(weekday_counts) if weekday_counts else 0.0,
        "weekend_avg": sum(weekend_counts) / len(weekend_counts) if weekend_counts else 0.0,
        "hot_slot": {"weekday": hot_day, "hour": hot_hour, "count": grid[hot_day][hot_hour]},
        "active_days": sum(1 for d in days if d["count"] > 0),
    }

    people = report["people"]
    active_days = len([d for d in days if d["count"] > 0]) or 1
    report["regulars"] = [
        uid for uid in report["ranking"] if len(people[uid].days) >= max(3, active_days - 1)
    ][:8]
    if previous:
        before = {m.uid for m in previous_humans}
        report["newcomers"] = [uid for uid in report["ranking"] if uid not in before][:6]
    else:
        report["newcomers"] = []

    # 话题升温：这周比上周多出来的词
    if previous:
        names = speaker_names([*messages, *previous])
        old = dict(
            extract_keywords(
                (m.text for m in previous_humans if m.is_chat_text),
                blocked=blocked,
                names=names,
                top=60,
                min_count=2,
            )
        )
        rising = []
        for word, count in extract_keywords(
            (m.text for m in humans if m.is_chat_text), blocked=blocked, names=names, top=40
        ):
            before_count = old.get(word, 0)
            if count >= 4 and count >= before_count * 2:
                rising.append((word, count, before_count))
        report["rising"] = rising[:6]
    else:
        report["rising"] = []

    # 群友星座：前 9 位 + 他们之间的连线
    stars = report["ranking"][:9]
    scores = pair_scores(messages)
    edges = [
        {"a": a, "b": b, "score": score}
        for (a, b), score in scores.most_common()
        if a in stars and b in stars and score >= 3
    ][:14]
    report["constellation"] = {"stars": stars, "edges": edges}

    # 一周的剧情：每天最热的一段，挑最热的 4 段
    all_sessions = sessions(messages, blocked=blocked, names=speaker_names(messages), min_count=10)
    best_per_day: dict[str, Session] = {}
    for session in all_sessions:
        key = datetime.fromtimestamp(session.start, CN_TZ).strftime("%Y-%m-%d")
        if key not in best_per_day or session.count > best_per_day[key].count:
            best_per_day[key] = session
    chosen = sorted(best_per_day.values(), key=lambda s: -s.count)[:4]
    chosen.sort(key=lambda s: s.start)
    for index, session in enumerate(chosen):
        session.index = index
    report["session_objs"] = chosen
    report["sessions"] = [_session_dict(s, people) for s in chosen]
    busiest = max(days, key=lambda d: d["count"]) if days else None
    report["busiest_day"] = busiest
    return report
