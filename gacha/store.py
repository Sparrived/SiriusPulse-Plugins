"""群友图鉴的持久化：一个人格一份 SQLite（``artifacts/gacha/gacha.db``）。

不用 PluginDataStore：它每次写入都整份重写 JSON、也没有锁，抽卡与收藏需要
"扣次数 + 发卡"在同一个事务里完成，一次失败不能只做一半。

卡池按群隔离：同一个人在两个群里是两张不同的卡，各自只用那个群里的公开发言写成。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

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
"""


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

    @property
    def label(self) -> str:
        return f"No.{self.no:03d}"

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
        )


@dataclass(slots=True)
class Player:
    draw_date: str = ""
    draws_today: int = 0
    total: int = 0
    pity: int = 0

    def draws_left(self, today: str, per_day: int) -> int:
        used = self.draws_today if self.draw_date == today else 0
        return max(0, per_day - used)


@dataclass(slots=True)
class Owned:
    card: Card
    copies: int


class GachaStore:
    """一个人格的卡池、收藏与抽卡记录。所有写入都在锁内的事务里完成。"""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

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
            )
            conn.execute(
                "INSERT INTO cards VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                ),
            )
        return card

    def cards(self, group_id: str) -> list[Card]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM cards WHERE group_id = ? ORDER BY no", (group_id,)
            ).fetchall()
        return [Card.from_row(row) for row in rows]

    def card_of(self, group_id: str, subject_id: str) -> Card | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM cards WHERE group_id = ? AND subject_id = ? ORDER BY no LIMIT 1",
                (group_id, subject_id),
            ).fetchone()
        return Card.from_row(row) if row else None

    def subjects_with_cards(self, group_id: str) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT subject_id FROM cards WHERE group_id = ?", (group_id,)
            ).fetchall()
        return {row[0] for row in rows}

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
        """退出图鉴：删掉这个人在本群的卡，连同所有人手里的那几张。返回删掉的张数。"""
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO opted_out VALUES (?, ?, ?)", (group_id, subject_id, now)
            )
            ids = [
                row[0]
                for row in conn.execute(
                    "SELECT card_id FROM cards WHERE group_id = ? AND subject_id = ?",
                    (group_id, subject_id),
                ).fetchall()
            ]
            for card_id in ids:
                conn.execute("DELETE FROM owned WHERE card_id = ?", (card_id,))
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
        today: str,
        pity_after: int,
        now: str,
    ) -> int:
        """一次抽卡：扣当天次数、更新保底、把卡放进收藏。返回这张卡现在有几份。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT draw_date, draws_today FROM players WHERE group_id = ? AND owner_id = ?",
                (group_id, owner_id),
            ).fetchone()
            used = int(row["draws_today"]) if row and row["draw_date"] == today else 0
            conn.execute(
                """INSERT INTO players (group_id, owner_id, draw_date, draws_today, total, pity)
                VALUES (?, ?, ?, ?, 1, ?)
                ON CONFLICT (group_id, owner_id) DO UPDATE SET
                    draw_date = excluded.draw_date,
                    draws_today = excluded.draws_today,
                    total = total + 1,
                    pity = excluded.pity""",
                (group_id, owner_id, today, used + 1, pity_after),
            )
            conn.execute(
                """INSERT INTO owned (group_id, owner_id, card_id, copies, first_at)
                VALUES (?, ?, ?, 1, ?)
                ON CONFLICT (group_id, owner_id, card_id) DO UPDATE SET copies = copies + 1""",
                (group_id, owner_id, card.card_id, now),
            )
            copies = conn.execute(
                "SELECT copies FROM owned WHERE group_id = ? AND owner_id = ? AND card_id = ?",
                (group_id, owner_id, card.card_id),
            ).fetchone()[0]
        return int(copies)

    def collection(self, group_id: str, owner_id: str) -> list[Owned]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT cards.*, owned.copies AS copies FROM owned
                JOIN cards ON cards.card_id = owned.card_id
                WHERE owned.group_id = ? AND owned.owner_id = ?
                ORDER BY cards.no""",
                (group_id, owner_id),
            ).fetchall()
        return [Owned(card=Card.from_row(row), copies=int(row["copies"])) for row in rows]
