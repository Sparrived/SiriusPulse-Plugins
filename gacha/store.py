"""群友图鉴的持久化：一个人格一份 SQLite（``artifacts/gacha/gacha.db``）。

不用 PluginDataStore：它每次写入都整份重写 JSON、也没有锁，抽卡、送卡和交换都需要
"扣掉一边 + 加到另一边"在同一个事务里完成，一次失败不能只做一半。

卡池按群隔离：同一个人在两个群里是两张不同的卡，各自只用那个群里的公开发言写成。

卡有三种：

- ``member``：每人一张的常驻卡（R）；
- ``mood``：她心情好时给某人画的变体（SR）；
- ``moment``：群里的名场面（UR），引用一句原话，限量发行；
- ``awakened``：三星卡觉醒出的超级卡（SSR），她专门为收藏者画了一张插画，只此一份。

名场面和觉醒卡的插画由后台的画图任务（``art_jobs``）慢慢画，画好再挂到卡上。

「画我」的同意（``likeness_consent``）只是插件这边的备忘：真正的授权记在框架的视觉资产
系统里（``grant_id`` 指向那条授权），画的时候由框架核验。照本人形象画的插画在卡上
标 ``art_likeness``，本人撤回同意时据此撤下。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from .refresh import in_period

KIND_MEMBER = "member"
KIND_MOOD = "mood"
KIND_MOMENT = "moment"
KIND_AWAKENED = "awakened"

MAX_STARS = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    card_id      TEXT PRIMARY KEY,
    group_id     TEXT NOT NULL,
    no           INTEGER NOT NULL,
    subject_id   TEXT NOT NULL,
    subject_name TEXT NOT NULL,
    subject_uid  TEXT NOT NULL DEFAULT '',
    rarity       TEXT NOT NULL,
    title        TEXT NOT NULL,
    skill_name   TEXT NOT NULL,
    skill_text   TEXT NOT NULL,
    note         TEXT NOT NULL,
    stats        TEXT NOT NULL DEFAULT '{}',
    minted_at    TEXT NOT NULL,
    UNIQUE (group_id, no)
);
CREATE INDEX IF NOT EXISTS idx_cards_subject ON cards(group_id, subject_id);

CREATE TABLE IF NOT EXISTS owned (
    group_id  TEXT NOT NULL,
    owner_id  TEXT NOT NULL,
    card_id   TEXT NOT NULL,
    copies    INTEGER NOT NULL DEFAULT 1,
    first_at  TEXT NOT NULL,
    PRIMARY KEY (group_id, owner_id, card_id)
);

CREATE TABLE IF NOT EXISTS players (
    group_id   TEXT NOT NULL,
    owner_id   TEXT NOT NULL,
    draw_date  TEXT NOT NULL DEFAULT '',
    draws_today INTEGER NOT NULL DEFAULT 0,
    total      INTEGER NOT NULL DEFAULT 0,
    pity       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (group_id, owner_id)
);

CREATE TABLE IF NOT EXISTS group_settings (
    group_id          TEXT PRIMARY KEY,
    refresh_times     TEXT NOT NULL DEFAULT '',
    manual_refresh_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS playing (
    group_id TEXT PRIMARY KEY,
    since    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS opted_out (
    group_id   TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    at         TEXT NOT NULL,
    PRIMARY KEY (group_id, subject_id)
);

CREATE TABLE IF NOT EXISTS art_jobs (
    job_id     TEXT PRIMARY KEY,
    group_id   TEXT NOT NULL,
    kind       TEXT NOT NULL,
    card_id    TEXT NOT NULL,
    owner_id   TEXT NOT NULL DEFAULT '',
    owner_name TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL DEFAULT 'pending',
    attempts   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS likeness_consent (
    group_id   TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    grant_id   TEXT NOT NULL,
    at         TEXT NOT NULL,
    PRIMARY KEY (group_id, subject_id)
);

CREATE TABLE IF NOT EXISTS offers (
    offer_id   TEXT PRIMARY KEY,
    group_id   TEXT NOT NULL,
    from_id    TEXT NOT NULL,
    from_name  TEXT NOT NULL,
    to_id      TEXT NOT NULL,
    give_card  TEXT NOT NULL,
    want_card  TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

# 第一期之后加的列：老库打开时补上，已有的卡保持原样（都是 member）。
_ADDED_COLUMNS = {
    "cards": {
        "kind": "TEXT NOT NULL DEFAULT 'member'",
        "quote": "TEXT NOT NULL DEFAULT ''",
        "cast_ids": "TEXT NOT NULL DEFAULT '[]'",
        "cast_names": "TEXT NOT NULL DEFAULT '[]'",
        "supply": "INTEGER NOT NULL DEFAULT 0",
        "issued": "INTEGER NOT NULL DEFAULT 0",
        "art": "TEXT NOT NULL DEFAULT ''",
        "base_card": "TEXT NOT NULL DEFAULT ''",
        "art_likeness": "INTEGER NOT NULL DEFAULT 0",
    },
    "owned": {
        "stars": "INTEGER NOT NULL DEFAULT 1",
        "notes": "TEXT NOT NULL DEFAULT '[]'",
    },
}


class SoldOut(Exception):
    """限量卡已经全部发出。"""


def _json_list(value: Any) -> list[str]:
    try:
        loaded = json.loads(value or "[]")
    except ValueError:
        return []
    return [str(item) for item in loaded] if isinstance(loaded, list) else []


@dataclass(slots=True)
class Card:
    card_id: str
    group_id: str
    no: int
    subject_id: str
    subject_name: str
    subject_uid: str
    rarity: str
    title: str
    skill_name: str
    skill_text: str
    note: str
    stats: dict[str, int | None] = field(default_factory=dict)
    minted_at: str = ""
    kind: str = KIND_MEMBER
    quote: str = ""
    cast_ids: list[str] = field(default_factory=list)
    cast_names: list[str] = field(default_factory=list)
    supply: int = 0
    issued: int = 0
    #: 插画文件名（在 ``artifacts/gacha/art/`` 下），没有插画是空串。
    art: str = ""
    #: 觉醒卡是从哪张卡觉醒来的。
    base_card: str = ""
    #: 插画是不是照卡上这个人本人的形象画的（他同意了「画我」）。
    art_likeness: bool = False

    @property
    def label(self) -> str:
        return f"No.{self.no:03d}"

    @property
    def name(self) -> str:
        """在台词和图鉴里怎么称呼这张卡：人物卡是人名，名场面是它的名字。"""
        return self.title if self.kind == KIND_MOMENT else self.subject_name

    @property
    def sold_out(self) -> bool:
        return self.supply > 0 and self.issued >= self.supply

    def involves(self, user_id: str) -> bool:
        return self.subject_id == user_id or user_id in self.cast_ids

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Card":
        try:
            stats = json.loads(row["stats"] or "{}")
        except ValueError:
            stats = {}
        return cls(
            card_id=row["card_id"],
            group_id=row["group_id"],
            no=int(row["no"]),
            subject_id=row["subject_id"],
            subject_name=row["subject_name"],
            subject_uid=row["subject_uid"],
            rarity=row["rarity"],
            title=row["title"],
            skill_name=row["skill_name"],
            skill_text=row["skill_text"],
            note=row["note"],
            stats=stats if isinstance(stats, dict) else {},
            minted_at=row["minted_at"],
            kind=row["kind"],
            quote=row["quote"],
            cast_ids=_json_list(row["cast_ids"]),
            cast_names=_json_list(row["cast_names"]),
            supply=int(row["supply"]),
            issued=int(row["issued"]),
            art=row["art"],
            base_card=row["base_card"],
            art_likeness=bool(row["art_likeness"]),
        )


@dataclass(slots=True)
class Player:
    #: 上次抽卡的时刻（UTC ISO）。列名是历史遗留：早期按自然日刷新，存的是日期。
    draw_date: str = ""
    draws_today: int = 0
    total: int = 0
    pity: int = 0

    def draws_left(self, period_start: datetime, per_period: int) -> int:
        used = self.draws_today if in_period(self.draw_date, period_start) else 0
        return max(0, per_period - used)


@dataclass(slots=True)
class GroupSettings:
    #: 本群单独设置的刷新时刻（root 用指令设的）；空表示跟插件配置走。
    refresh_times: str = ""
    #: 本群最近一次手动刷新（UTC ISO），没有是空串。
    manual_refresh_at: str = ""


@dataclass(slots=True)
class Owned:
    card: Card
    copies: int
    stars: int = 1
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Offer:
    offer_id: str
    group_id: str
    from_id: str
    from_name: str
    to_id: str
    give_card: str
    want_card: str
    created_at: str


class GachaStore:
    """一个人格的卡池、收藏与抽卡记录。所有写入都在锁内的事务里完成。"""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            for table, columns in _ADDED_COLUMNS.items():
                have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
                for name, decl in columns.items():
                    if name not in have:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = sqlite3.connect(self.path)
            conn.row_factory = sqlite3.Row
            try:
                with conn:
                    yield conn
            finally:
                conn.close()

    # ── 卡池 ──────────────────────────────────────────────────────────

    def add_card(
        self,
        *,
        group_id: str,
        subject_id: str,
        subject_name: str,
        subject_uid: str,
        rarity: str,
        title: str,
        skill_name: str,
        skill_text: str,
        note: str,
        stats: dict[str, Any],
        now: str,
        kind: str = KIND_MEMBER,
        quote: str = "",
        cast_ids: list[str] | None = None,
        cast_names: list[str] | None = None,
        supply: int = 0,
        issued: int = 0,
        base_card: str = "",
    ) -> Card:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(no), 0) + 1 FROM cards WHERE group_id = ?", (group_id,)
            ).fetchone()
            card = Card(
                card_id=uuid.uuid4().hex,
                group_id=group_id,
                no=int(row[0]),
                subject_id=subject_id,
                subject_name=subject_name,
                subject_uid=subject_uid,
                rarity=rarity,
                title=title,
                skill_name=skill_name,
                skill_text=skill_text,
                note=note,
                stats=dict(stats),
                minted_at=now,
                kind=kind,
                quote=quote,
                cast_ids=list(cast_ids or []),
                cast_names=list(cast_names or []),
                supply=supply,
                issued=issued,
                base_card=base_card,
            )
            conn.execute(
                """INSERT INTO cards (card_id, group_id, no, subject_id, subject_name,
                    subject_uid, rarity, title, skill_name, skill_text, note, stats, minted_at,
                    kind, quote, cast_ids, cast_names, supply, issued, art, base_card)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?)""",
                (
                    card.card_id,
                    card.group_id,
                    card.no,
                    card.subject_id,
                    card.subject_name,
                    card.subject_uid,
                    card.rarity,
                    card.title,
                    card.skill_name,
                    card.skill_text,
                    card.note,
                    json.dumps(card.stats, ensure_ascii=False),
                    card.minted_at,
                    card.kind,
                    card.quote,
                    json.dumps(card.cast_ids, ensure_ascii=False),
                    json.dumps(card.cast_names, ensure_ascii=False),
                    card.supply,
                    card.issued,
                    card.base_card,
                ),
            )
        return card

    def visible_cards(self, group_id: str, viewer_id: str) -> list[Card]:
        """某人在图鉴里看得到的卡：别人专属的觉醒卡不算进他的收集目标。"""
        mine = {item.card.card_id for item in self.collection(group_id, viewer_id)}
        return [
            card
            for card in self.cards(group_id)
            if card.kind != KIND_AWAKENED or card.card_id in mine
        ]

    def awakened_from(self, group_id: str, owner_id: str, base_card: str) -> Card | None:
        """这个人从这张卡觉醒出来的那张（还在他手里的话）。"""
        for item in self.collection(group_id, owner_id):
            if item.card.kind == KIND_AWAKENED and item.card.base_card == base_card:
                return item.card
        return None

    def set_art(self, card_id: str, name: str, *, likeness: bool = False) -> None:
        """给卡挂上插画；``name`` 为空串就是撤下插画。"""
        with self._connect() as conn:
            conn.execute(
                "UPDATE cards SET art = ?, art_likeness = ? WHERE card_id = ?",
                (name, int(bool(name) and likeness), card_id),
            )

    def likeness_art(self, group_id: str, subject_id: str) -> list[Card]:
        """本群里照这个人本人形象画了插画的卡。"""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM cards WHERE group_id = ? AND subject_id = ? AND art != ''
                AND art_likeness = 1""",
                (group_id, subject_id),
            ).fetchall()
        return [Card.from_row(row) for row in rows]

    # ── 「画我」的同意 ────────────────────────────────────────────────

    def set_likeness_consent(self, group_id: str, subject_id: str, grant_id: str, now: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO likeness_consent VALUES (?, ?, ?, ?)",
                (group_id, subject_id, grant_id, now),
            )

    def likeness_consent(self, group_id: str, subject_id: str) -> str:
        """这个人在本群同意「画我」时那条视觉授权的编号；没同意是空串。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT grant_id FROM likeness_consent WHERE group_id = ? AND subject_id = ?",
                (group_id, subject_id),
            ).fetchone()
        return str(row[0]) if row else ""

    def clear_likeness_consent(self, group_id: str, subject_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM likeness_consent WHERE group_id = ? AND subject_id = ?",
                (group_id, subject_id),
            )
        return cursor.rowcount > 0

    def grant(self, *, group_id: str, owner_id: str, card_id: str, now: str) -> None:
        """直接把一份卡放进某人的收藏（觉醒卡生来就是他的）。"""
        with self._connect() as conn:
            _give(conn, group_id, owner_id, card_id, now)

    # ── 画图任务 ──────────────────────────────────────────────────────

    def add_art_job(
        self,
        *,
        group_id: str,
        kind: str,
        card_id: str,
        now: str,
        owner_id: str = "",
        owner_name: str = "",
    ) -> str:
        job_id = uuid.uuid4().hex
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO art_jobs (job_id, group_id, kind, card_id, owner_id, owner_name,
                    status, attempts, created_at) VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?)""",
                (job_id, group_id, kind, card_id, owner_id, owner_name, now),
            )
        return job_id

    def pending_art_job(self, group_id: str, owner_id: str, card_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT 1 FROM art_jobs WHERE group_id = ? AND owner_id = ? AND card_id = ?
                AND status = 'pending'""",
                (group_id, owner_id, card_id),
            ).fetchone()
        return row is not None

    def next_art_job(self) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM art_jobs WHERE status = 'pending' ORDER BY created_at LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def art_job_tried(self, job_id: str, *, status: str = "pending") -> int:
        """记一次尝试并更新状态，返回累计尝试次数。"""
        with self._connect() as conn:
            conn.execute(
                "UPDATE art_jobs SET attempts = attempts + 1, status = ? WHERE job_id = ?",
                (status, job_id),
            )
            row = conn.execute(
                "SELECT attempts FROM art_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return int(row[0]) if row else 0

    def finish_art_job(self, job_id: str, status: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE art_jobs SET status = ? WHERE job_id = ?", (status, job_id))

    def cards(self, group_id: str) -> list[Card]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM cards WHERE group_id = ? ORDER BY no", (group_id,)
            ).fetchall()
        return [Card.from_row(row) for row in rows]

    def card(self, group_id: str, card_id: str) -> Card | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM cards WHERE group_id = ? AND card_id = ?", (group_id, card_id)
            ).fetchone()
        return Card.from_row(row) if row else None

    def card_of(self, group_id: str, subject_id: str) -> Card | None:
        """这个人的常驻卡（不含心情卡和名场面）。"""
        with self._connect() as conn:
            row = conn.execute(
                """SELECT * FROM cards WHERE group_id = ? AND subject_id = ? AND kind = ?
                ORDER BY no LIMIT 1""",
                (group_id, subject_id, KIND_MEMBER),
            ).fetchone()
        return Card.from_row(row) if row else None

    def subjects_with_cards(self, group_id: str) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT subject_id FROM cards WHERE group_id = ? AND kind = ?",
                (group_id, KIND_MEMBER),
            ).fetchall()
        return {row[0] for row in rows}

    def last_minted(self, group_id: str, kind: str, subject_id: str = "") -> str:
        """某类卡（可限定某个人）最近一次发行的时间，没有则为空串。"""
        sql = "SELECT MAX(minted_at) FROM cards WHERE group_id = ? AND kind = ?"
        params: tuple[str, ...] = (group_id, kind)
        if subject_id:
            sql += " AND subject_id = ?"
            params += (subject_id,)
        with self._connect() as conn:
            row = conn.execute(sql, params).fetchone()
        return str(row[0] or "")

    # ── 刷新设置 ──────────────────────────────────────────────────────

    def group_settings(self, group_id: str) -> GroupSettings:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT refresh_times, manual_refresh_at FROM group_settings WHERE group_id = ?",
                (group_id,),
            ).fetchone()
        if row is None:
            return GroupSettings()
        return GroupSettings(refresh_times=row[0], manual_refresh_at=row[1])

    def set_refresh_times(self, group_id: str, times: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO group_settings (group_id, refresh_times) VALUES (?, ?)
                ON CONFLICT (group_id) DO UPDATE SET refresh_times = excluded.refresh_times""",
                (group_id, times),
            )

    def refresh_now(self, group_id: str, now: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO group_settings (group_id, manual_refresh_at) VALUES (?, ?)
                ON CONFLICT (group_id) DO UPDATE SET
                    manual_refresh_at = excluded.manual_refresh_at""",
                (group_id, now),
            )

    # ── 哪些群在玩 ────────────────────────────────────────────────────

    def mark_playing(self, group_id: str, now: str) -> None:
        """有人在这个群里 /抽卡 过，这个群才算开张，后台才会给这里的人画卡。"""
        with self._connect() as conn:
            conn.execute("INSERT OR IGNORE INTO playing VALUES (?, ?)", (group_id, now))

    def playing_groups(self) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT group_id FROM playing").fetchall()
        return {row[0] for row in rows}

    # ── 退出与加入 ────────────────────────────────────────────────────

    def opt_out(self, group_id: str, subject_id: str, now: str) -> int:
        """退出图鉴：删掉本群里画了这个人的所有卡（包括他出演的名场面），
        连同所有人手里的那几张；「画我」的同意也一并删掉。返回删掉的张数。"""
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO opted_out VALUES (?, ?, ?)", (group_id, subject_id, now)
            )
            conn.execute(
                "DELETE FROM likeness_consent WHERE group_id = ? AND subject_id = ?",
                (group_id, subject_id),
            )
            rows = conn.execute("SELECT * FROM cards WHERE group_id = ?", (group_id,)).fetchall()
            ids = [row["card_id"] for row in rows if Card.from_row(row).involves(subject_id)]
            for card_id in ids:
                conn.execute("DELETE FROM owned WHERE card_id = ?", (card_id,))
                conn.execute(
                    "DELETE FROM offers WHERE give_card = ? OR want_card = ?", (card_id,) * 2
                )
                conn.execute("DELETE FROM cards WHERE card_id = ?", (card_id,))
        return len(ids)

    def opt_in(self, group_id: str, subject_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM opted_out WHERE group_id = ? AND subject_id = ?",
                (group_id, subject_id),
            )
        return cursor.rowcount > 0

    def opted_out(self, group_id: str) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT subject_id FROM opted_out WHERE group_id = ?", (group_id,)
            ).fetchall()
        return {row[0] for row in rows}

    # ── 抽卡与收藏 ────────────────────────────────────────────────────

    def player(self, group_id: str, owner_id: str) -> Player:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM players WHERE group_id = ? AND owner_id = ?", (group_id, owner_id)
            ).fetchone()
        if row is None:
            return Player()
        return Player(
            draw_date=row["draw_date"],
            draws_today=int(row["draws_today"]),
            total=int(row["total"]),
            pity=int(row["pity"]),
        )

    def record_draw(
        self,
        *,
        group_id: str,
        owner_id: str,
        card: Card,
        period: datetime,
        pity_after: int,
        now: str,
    ) -> int:
        """一次抽卡：扣当天次数、更新保底、把卡放进收藏。返回这张卡现在有几份。

        限量卡在同一个事务里占一份名额；名额已满时抛 SoldOut，什么也不扣。
        """
        with self._connect() as conn:
            if card.supply > 0:
                taken = conn.execute(
                    "UPDATE cards SET issued = issued + 1 WHERE card_id = ? AND issued < supply",
                    (card.card_id,),
                ).rowcount
                if not taken:
                    raise SoldOut(card.card_id)
            row = conn.execute(
                "SELECT draw_date, draws_today FROM players WHERE group_id = ? AND owner_id = ?",
                (group_id, owner_id),
            ).fetchone()
            used = int(row["draws_today"]) if row and in_period(row["draw_date"], period) else 0
            conn.execute(
                """INSERT INTO players (group_id, owner_id, draw_date, draws_today, total, pity)
                VALUES (?, ?, ?, ?, 1, ?)
                ON CONFLICT (group_id, owner_id) DO UPDATE SET
                    draw_date = excluded.draw_date,
                    draws_today = excluded.draws_today,
                    total = total + 1,
                    pity = excluded.pity""",
                (group_id, owner_id, now, used + 1, pity_after),
            )
            _give(conn, group_id, owner_id, card.card_id, now)
            copies = conn.execute(
                "SELECT copies FROM owned WHERE group_id = ? AND owner_id = ? AND card_id = ?",
                (group_id, owner_id, card.card_id),
            ).fetchone()[0]
        return int(copies)

    def collection(self, group_id: str, owner_id: str) -> list[Owned]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT cards.*, owned.copies AS copies, owned.stars AS stars,
                    owned.notes AS notes FROM owned
                JOIN cards ON cards.card_id = owned.card_id
                WHERE owned.group_id = ? AND owned.owner_id = ?
                ORDER BY cards.no""",
                (group_id, owner_id),
            ).fetchall()
        return [
            Owned(
                card=Card.from_row(row),
                copies=int(row["copies"]),
                stars=int(row["stars"]),
                notes=_json_list(row["notes"]),
            )
            for row in rows
        ]

    def owned(self, group_id: str, owner_id: str, card_id: str) -> Owned | None:
        for item in self.collection(group_id, owner_id):
            if item.card.card_id == card_id:
                return item
        return None

    # ── 送卡、交换、升星 ──────────────────────────────────────────────

    def give(self, *, group_id: str, from_id: str, to_id: str, card_id: str, now: str) -> bool:
        """把一份卡从 from 手里移到 to 手里。from 没有这张卡时什么也不做，返回 False。"""
        with self._connect() as conn:
            if not _take(conn, group_id, from_id, card_id):
                return False
            _give(conn, group_id, to_id, card_id, now)
        return True

    def offer(
        self,
        *,
        group_id: str,
        from_id: str,
        from_name: str,
        to_id: str,
        give_card: str,
        want_card: str,
        now: str,
    ) -> Offer:
        """发起一次交换。同一对人之间只保留最新的一份。"""
        offer = Offer(
            offer_id=uuid.uuid4().hex,
            group_id=group_id,
            from_id=from_id,
            from_name=from_name,
            to_id=to_id,
            give_card=give_card,
            want_card=want_card,
            created_at=now,
        )
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM offers WHERE group_id = ? AND from_id = ? AND to_id = ?",
                (group_id, from_id, to_id),
            )
            conn.execute(
                "INSERT INTO offers VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    offer.offer_id,
                    group_id,
                    from_id,
                    from_name,
                    to_id,
                    give_card,
                    want_card,
                    now,
                ),
            )
        return offer

    def pending_offer(
        self, group_id: str, to_id: str, *, now: datetime, ttl: timedelta
    ) -> Offer | None:
        """别人发给我的、还没过期的最新一份交换。"""
        horizon = (now - ttl).isoformat()
        with self._connect() as conn:
            row = conn.execute(
                """SELECT * FROM offers WHERE group_id = ? AND to_id = ? AND created_at >= ?
                ORDER BY created_at DESC LIMIT 1""",
                (group_id, to_id, horizon),
            ).fetchone()
        return Offer(**dict(row)) if row else None

    def close_offer(self, offer_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM offers WHERE offer_id = ?", (offer_id,))

    def swap(self, offer: Offer, now: str) -> bool:
        """成交：双方各交出一份、各收到一份。任何一方已经没有那张卡就整笔作废。"""
        try:
            with self._connect() as conn:
                # 任何一边拿不出卡都抛 _Abort，让整个事务回滚，谁也不少一张。
                if not _take(conn, offer.group_id, offer.from_id, offer.give_card):
                    raise _Abort()
                if not _take(conn, offer.group_id, offer.to_id, offer.want_card):
                    raise _Abort()
                _give(conn, offer.group_id, offer.to_id, offer.give_card, now)
                _give(conn, offer.group_id, offer.from_id, offer.want_card, now)
                conn.execute("DELETE FROM offers WHERE offer_id = ?", (offer.offer_id,))
        except _Abort:
            self.close_offer(offer.offer_id)
            return False
        return True

    def star_up(self, *, group_id: str, owner_id: str, card_id: str, note: str) -> int:
        """消耗一份重复卡升一星并记下她追加的那句话，返回新的星级；条件不满足返回 0。"""
        with self._connect() as conn:
            row = conn.execute(
                """SELECT copies, stars, notes FROM owned
                WHERE group_id = ? AND owner_id = ? AND card_id = ?""",
                (group_id, owner_id, card_id),
            ).fetchone()
            if row is None or int(row["copies"]) < 2 or int(row["stars"]) >= MAX_STARS:
                return 0
            notes = [*_json_list(row["notes"]), note]
            stars = int(row["stars"]) + 1
            conn.execute(
                """UPDATE owned SET copies = copies - 1, stars = ?, notes = ?
                WHERE group_id = ? AND owner_id = ? AND card_id = ?""",
                (stars, json.dumps(notes, ensure_ascii=False), group_id, owner_id, card_id),
            )
        return stars


class _Abort(Exception):
    pass


def _take(conn: sqlite3.Connection, group_id: str, owner_id: str, card_id: str) -> bool:
    row = conn.execute(
        "SELECT copies FROM owned WHERE group_id = ? AND owner_id = ? AND card_id = ?",
        (group_id, owner_id, card_id),
    ).fetchone()
    if row is None or int(row[0]) < 1:
        return False
    if int(row[0]) == 1:
        conn.execute(
            "DELETE FROM owned WHERE group_id = ? AND owner_id = ? AND card_id = ?",
            (group_id, owner_id, card_id),
        )
    else:
        conn.execute(
            """UPDATE owned SET copies = copies - 1
            WHERE group_id = ? AND owner_id = ? AND card_id = ?""",
            (group_id, owner_id, card_id),
        )
    return True


def _give(conn: sqlite3.Connection, group_id: str, owner_id: str, card_id: str, now: str) -> None:
    conn.execute(
        """INSERT INTO owned (group_id, owner_id, card_id, copies, first_at)
        VALUES (?, ?, ?, 1, ?)
        ON CONFLICT (group_id, owner_id, card_id) DO UPDATE SET copies = copies + 1""",
        (group_id, owner_id, card_id, now),
    )
