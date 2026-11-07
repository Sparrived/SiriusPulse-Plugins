"""卡面上的事实：谁够格进图鉴、他在这个群里是什么样、属性怎么算。

只读引擎已有的数据，而且**只读这一个群**：

- 语义画像（``semantic_profiles``）：发言数、和她的互动、第一次说话的时间；
- 认知事件（``cognition_events``，只保留 30 天）：几点说话、情绪起伏；
- 聊天归档（``archive/<群>.jsonl``）：这个人最近在本群说过的话，给她写卡面用。

私聊（``private_*``）和别的群的东西一律不碰：卡池按群隔离，卡面也只用本群公开发言。
"""

from __future__ import annotations

import json
import math
import re
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .rules import LOCAL_TIMEZONE

#: 卡面属性，顺序即画在卡上的顺序。
STAT_KEYS = ("话痨", "夜行", "情绪", "默契", "资历")

_COMMAND_PREFIXES = ("/", "#", "!", "！")
_MIN_EVENTS_FOR_HABITS = 10
_LINE_CHARS = 80
_MAX_LINES = 30
_ARCHIVE_TAIL_BYTES = 2 * 1024 * 1024

# 夜行：凌晨 0–5 点说话的占比，到这个比例就算拉满。
_NIGHT_FULL_SHARE = 0.3
# 情绪：情绪效价的标准差，到这个值就算拉满。
_MOOD_FULL_STDEV = 0.5
# 资历：认识满一年就拉满。
_SENIORITY_FULL_DAYS = 365


@dataclass(slots=True)
class Member:
    user_id: str
    name: str
    uid: str = ""
    messages: int = 0
    engagement: float = 0.0
    first_at: str = ""
    night_share: float | None = None
    mood_swing: float | None = None
    lines: list[str] = field(default_factory=list)


def _parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _score(value: float) -> int:
    return max(1, min(99, round(value)))


def compute_stats(member: Member, everyone: list[Member], now: datetime) -> dict[str, int | None]:
    """把事实换成 1–99 的属性。数据不够的属性是 None（卡面上画成 ??）。"""
    counts = sorted(m.messages for m in everyone) or [member.messages]
    below = sum(1 for c in counts if c < member.messages)
    equal = sum(1 for c in counts if c == member.messages)
    talk = 100 * (below + 0.5 * equal) / len(counts)

    first = _parse_time(member.first_at)
    days = (now - first).days if first else 0
    return {
        "话痨": _score(talk),
        "夜行": (
            None
            if member.night_share is None
            else _score(100 * min(1.0, member.night_share / _NIGHT_FULL_SHARE))
        ),
        "情绪": (
            None
            if member.mood_swing is None
            else _score(100 * min(1.0, member.mood_swing / _MOOD_FULL_STDEV))
        ),
        "默契": _score(100 * max(0.0, min(1.0, member.engagement))),
        "资历": _score(100 * min(1.0, max(0, days) / _SENIORITY_FULL_DAYS)),
    }


# ---------------------------------------------------------------------------
# 从引擎读
# ---------------------------------------------------------------------------


def gather_members(engine: Any, group_id: str, *, min_messages: int) -> list[Member]:
    """本群里发言够多、可以进图鉴的人（不含她自己），带上算属性要的事实。"""
    semantic = getattr(engine, "semantic_memory", None)
    if semantic is None or not group_id or group_id.startswith("private_"):
        return []
    members: list[Member] = []
    for profile in semantic.list_group_user_profiles(group_id):
        user_id = str(getattr(profile, "user_id", "") or "")
        if not user_id or user_id in {"assistant", "system"}:
            continue
        if int(getattr(profile, "interaction_count", 0) or 0) < min_messages:
            continue
        members.append(
            Member(
                user_id=user_id,
                name=str(getattr(profile, "name", "") or "").strip() or user_id,
                messages=int(profile.interaction_count),
                engagement=float(getattr(profile, "engagement_rate", 0.0) or 0.0),
                first_at=str(getattr(profile, "first_interaction_at", "") or ""),
            )
        )
    _attach_habits(engine, group_id, members)
    return members


def _attach_habits(engine: Any, group_id: str, members: list[Member]) -> None:
    store = getattr(engine, "cognition_store", None)
    if store is None or not members:
        return
    try:
        flush = getattr(store, "flush", None)
        if callable(flush):
            flush()
        rows = store.execute(
            "SELECT user_id, timestamp, valence FROM cognition_events WHERE group_id = ?",
            (group_id,),
        ).fetchall()
    except Exception:
        return
    by_user: dict[str, list[tuple[float, float]]] = {}
    for row in rows:
        by_user.setdefault(str(row[0]), []).append((float(row[1]), float(row[2])))
    for member in members:
        events = by_user.get(member.user_id, [])
        if len(events) < _MIN_EVENTS_FOR_HABITS:
            continue
        night = sum(
            1 for stamp, _ in events if datetime.fromtimestamp(stamp, LOCAL_TIMEZONE).hour < 5
        )
        member.night_share = night / len(events)
        member.mood_swing = statistics.pstdev(valence for _, valence in events)


@dataclass(frozen=True, slots=True)
class Said:
    """归档里一句群友说的话。"""

    user_id: str
    name: str
    uid: str
    text: str
    at: datetime | None


def _archive_tail(engine: Any, group_id: str) -> list[Said]:
    """本群归档末尾的群友发言（跳过命令和她自己），按时间顺序。"""
    path = _archive_path(engine, group_id)
    if path is None or not path.exists():
        return []
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - _ARCHIVE_TAIL_BYTES))
            raw = handle.read().decode("utf-8", errors="ignore")
    except OSError:
        return []
    said: list[Said] = []
    for line in raw.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("role") != "human":
            continue
        text = " ".join(str(entry.get("content", "") or "").split())
        if not text or text.startswith(_COMMAND_PREFIXES):
            continue
        uid = str(entry.get("channel_user_id", "") or "")
        said.append(
            Said(
                user_id=str(entry.get("user_id", "") or ""),
                name=str(entry.get("speaker_name", "") or "").strip(),
                uid=uid if uid.isdigit() else "",
                text=text[:_LINE_CHARS],
                at=_parse_time(str(entry.get("timestamp", "") or "")),
            )
        )
    return said


def recent_lines(engine: Any, group_id: str, member: Member) -> None:
    """这个人最近在本群说过的话（跳过命令），顺便补上 QQ 号给卡面头像用。"""
    mine = [item for item in _archive_tail(engine, group_id) if item.user_id == member.user_id]
    for item in mine:
        if item.uid:
            member.uid = item.uid
    member.lines = [item.text for item in mine][-_MAX_LINES:]


# ---------------------------------------------------------------------------
# 名场面：一段很多人一起说、情绪很高的十分钟
# ---------------------------------------------------------------------------

MOMENT_WINDOW_SECONDS = 600
MOMENT_MIN_MESSAGES = 12
MOMENT_MIN_SPEAKERS = 3
_MOMENT_LOOKBACK_SECONDS = 24 * 3600
_MOMENT_MIN_LINES = 6
_MOMENT_MAX_LINES = 40


@dataclass(slots=True)
class Moment:
    start: datetime
    end: datetime
    lines: list[Said]

    @property
    def cast(self) -> list[tuple[str, str]]:
        """出演的人（user_id, 名字），按第一次开口的顺序。"""
        seen: dict[str, str] = {}
        for item in self.lines:
            seen.setdefault(item.user_id, item.name or item.user_id)
        return list(seen.items())


def find_moment(engine: Any, group_id: str, now: datetime, *, left_out: set[str]) -> Moment | None:
    """过去一天里最热闹的那十分钟，够格就返回，连同那段时间的原话。

    只用认知事件找"哪一段"，原话只取本群归档；退出图鉴的人说的话不进材料。
    """
    store = getattr(engine, "cognition_store", None)
    if store is None:
        return None
    try:
        flush = getattr(store, "flush", None)
        if callable(flush):
            flush()
        rows = store.execute(
            """SELECT user_id, timestamp, arousal FROM cognition_events
            WHERE group_id = ? AND timestamp >= ? ORDER BY timestamp""",
            (group_id, now.timestamp() - _MOMENT_LOOKBACK_SECONDS),
        ).fetchall()
    except Exception:
        return None
    events = [
        (str(row[0]), float(row[1]), float(row[2]))
        for row in rows
        if str(row[0]) and str(row[0]) not in {"assistant", "system"}
    ]
    best: tuple[float, int, int] | None = None
    j = 0
    for i in range(len(events)):
        j = max(j, i)
        while j + 1 < len(events) and events[j + 1][1] - events[i][1] <= MOMENT_WINDOW_SECONDS:
            j += 1
        window = events[i : j + 1]
        if len(window) < MOMENT_MIN_MESSAGES:
            continue
        if len({user for user, _, _ in window}) < MOMENT_MIN_SPEAKERS:
            continue
        score = len(window) * (0.5 + statistics.fmean(a for _, _, a in window))
        if best is None or score > best[0]:
            best = (score, i, j)
    if best is None:
        return None
    _, i, j = best
    start = datetime.fromtimestamp(events[i][1] - 60, timezone.utc)
    end = datetime.fromtimestamp(events[j][1] + 60, timezone.utc)
    lines = [
        item
        for item in _archive_tail(engine, group_id)
        if item.at is not None
        and start <= item.at <= end
        and item.user_id
        and item.user_id not in left_out
    ][:_MOMENT_MAX_LINES]
    if len(lines) < _MOMENT_MIN_LINES or len({item.user_id for item in lines}) < 2:
        return None
    return Moment(start=start, end=end, lines=lines)


def _archive_path(engine: Any, group_id: str) -> Path | None:
    store = getattr(engine, "basic_store", None)
    work_path = getattr(engine, "work_path", None)
    if store is None or work_path is None:
        return None
    return Path(work_path) / "archive" / f"{store._safe_name(group_id)}.jsonl"


def familiarity(member: Member) -> float:
    """和语义画像同一条曲线：50 条消息约等于满。"""
    return min(1.0, math.log1p(member.messages) / math.log1p(50))


# ---------------------------------------------------------------------------
# 送卡、交换时找人：名字、群昵称或 @ 出来的 qq_号
# ---------------------------------------------------------------------------

_NICK_RE = re.compile(r"^(?P<nick>.+?)\(群昵称为(?P<card>.+)\)$")


def resolve_person(engine: Any, group_id: str, token: str) -> tuple[str, str] | None:
    """把指令里的"那个人"解析成本群的 (user_id, 名字)，找不到或有歧义返回 None。

    @ 某人在消息里会变成 ``@昵称(群昵称为xx)`` 或 ``@qq_123456``；也可以直接写名字。
    只在本群认识的人里找。
    """
    token = token.strip().removeprefix("@")
    if not token:
        return None
    said = _archive_tail(engine, group_id)
    if token.startswith("qq_") and token[3:].isdigit():
        for item in reversed(said):
            if item.uid == token[3:] and item.user_id:
                return item.user_id, item.name or item.user_id
        return None
    match = _NICK_RE.match(token)
    names = [match["nick"], match["card"]] if match else [token]
    known: dict[str, str] = {}
    semantic = getattr(engine, "semantic_memory", None)
    if semantic is not None:
        for profile in semantic.list_group_user_profiles(group_id):
            user_id = str(getattr(profile, "user_id", "") or "")
            name = str(getattr(profile, "name", "") or "").strip()
            if user_id and name and user_id not in {"assistant", "system"}:
                known[user_id] = name
    for item in said:
        if item.user_id and item.name:
            known.setdefault(item.user_id, item.name)
    hits = {user_id for user_id, name in known.items() if name in names}
    if len(hits) != 1:
        return None
    user_id = hits.pop()
    return user_id, known[user_id]
