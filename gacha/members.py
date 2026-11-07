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


def recent_lines(engine: Any, group_id: str, member: Member) -> None:
    """这个人最近在本群说过的话（跳过命令），顺便补上 QQ 号给卡面头像用。"""
    path = _archive_path(engine, group_id)
    if path is None or not path.exists():
        return
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - _ARCHIVE_TAIL_BYTES))
            raw = handle.read().decode("utf-8", errors="ignore")
    except OSError:
        return
    lines: list[str] = []
    for line in raw.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("user_id") != member.user_id:
            continue
        if entry.get("role") != "human":
            continue
        uid = str(entry.get("channel_user_id", "") or "")
        if uid.isdigit():
            member.uid = uid
        text = " ".join(str(entry.get("content", "") or "").split())
        if not text or text.startswith(_COMMAND_PREFIXES):
            continue
        lines.append(text[:_LINE_CHARS])
    member.lines = lines[-_MAX_LINES:]


def _archive_path(engine: Any, group_id: str) -> Path | None:
    store = getattr(engine, "basic_store", None)
    work_path = getattr(engine, "work_path", None)
    if store is None or work_path is None:
        return None
    return Path(work_path) / "archive" / f"{store._safe_name(group_id)}.jsonl"


def familiarity(member: Member) -> float:
    """和语义画像同一条曲线：50 条消息约等于满。"""
    return min(1.0, math.log1p(member.messages) / math.log1p(50))
