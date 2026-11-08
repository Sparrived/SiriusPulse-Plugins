"""抽卡次数什么时候刷新。

一天可以有好几个刷新时刻（中国时间，比如 ``00:00、12:00、20:00``），每到一个就开始
新的一轮，每人每轮能抽 ``draws_per_day`` 次。root 还可以随时手动刷新本群。

一轮从"最近一次刷新"开始算：最近一个已经过去的刷新时刻，和本群最近一次手动刷新，
取较晚的那个。玩家记下的是他**上次真正抽卡的时刻**：只有在那之后真的过了一个刷新
时刻（或者手动刷新了一次），他的次数才清零——所以 root 改刷新时间不会白送一轮。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

LOCAL_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")

DEFAULT_TIMES = (0,)
MAX_TIMES = 24

_TIME_RE = re.compile(r"^([01]?\d|2[0-3])[:：]([0-5]\d)$")
_SPLIT_RE = re.compile(r"[\s,，、;；]+")


def parse_times(raw: object) -> tuple[int, ...]:
    """``"00:00, 12:00"`` / ``["8:00", "20:00"]`` → 当天的分钟数，去重排序。

    写错的时刻直接跳过；一个能用的都没有就返回空，由调用方决定退回默认。
    """
    if isinstance(raw, str):
        items: Iterable[object] = _SPLIT_RE.split(raw.strip())
    elif isinstance(raw, (list, tuple)):
        items = raw
    else:
        return ()
    minutes = set()
    for item in items:
        match = _TIME_RE.match(str(item or "").strip())
        if match:
            minutes.add(int(match[1]) * 60 + int(match[2]))
    return tuple(sorted(minutes))[:MAX_TIMES]


def format_times(times: Iterable[int]) -> str:
    return "、".join(f"{m // 60:02d}:{m % 60:02d}" for m in times)


def _local(now: datetime) -> datetime:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(LOCAL_TIMEZONE)


def _at(day: date, minutes: int) -> datetime:
    return datetime.combine(day, time(minutes // 60, minutes % 60), tzinfo=LOCAL_TIMEZONE)


@dataclass(frozen=True, slots=True)
class Schedule:
    """一个群的刷新安排：每天的刷新时刻，加上最近一次手动刷新。"""

    times: tuple[int, ...] = DEFAULT_TIMES
    manual_at: datetime | None = None

    def last_scheduled(self, now: datetime) -> datetime:
        """最近一个已经到了的刷新时刻（今天的，或者昨天最后一个）。"""
        local = _local(now)
        minute = local.hour * 60 + local.minute
        passed = [m for m in self.times if m <= minute]
        if passed:
            return _at(local.date(), passed[-1])
        return _at(local.date() - timedelta(days=1), self.times[-1])

    def period_start(self, now: datetime) -> datetime:
        """这一轮从什么时候开始（UTC）。"""
        start = self.last_scheduled(now)
        if self.manual_at is not None and start < self.manual_at <= _local(now):
            start = self.manual_at
        return start.astimezone(timezone.utc)

    def next_refresh(self, now: datetime) -> datetime:
        """下一个刷新时刻（本地时间）。手动刷新预测不了，不算在内。"""
        local = _local(now)
        minute = local.hour * 60 + local.minute
        upcoming = [m for m in self.times if m > minute]
        if upcoming:
            return _at(local.date(), upcoming[0])
        return _at(local.date() + timedelta(days=1), self.times[0])


def describe_next(schedule: Schedule, now: datetime) -> str:
    """ "12:00" / "明天 00:00"，给"这一轮抽完了"的回复用。"""
    upcoming = schedule.next_refresh(now)
    label = f"{upcoming:%H:%M}"
    return label if upcoming.date() == _local(now).date() else f"明天 {label}"


def started_at(stored: str) -> datetime | None:
    """玩家记下的上次抽卡时刻。

    早期版本记的是本地日期（``2026-11-08``），按那天本地零点理解，
    这样升级前当天抽过的人不会在升级后白得一轮。
    """
    stored = str(stored or "").strip()
    if not stored:
        return None
    try:
        if "T" not in stored:
            return _at(date.fromisoformat(stored), 0).astimezone(timezone.utc)
        parsed = datetime.fromisoformat(stored)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def in_period(stored: str, start: datetime) -> bool:
    """他上次抽卡是不是在这一轮开始之后。"""
    previous = started_at(stored)
    return previous is not None and previous >= start
