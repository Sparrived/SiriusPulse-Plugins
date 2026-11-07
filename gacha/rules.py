"""抽卡规则：稀有度、出率、保底，以及"今天"按哪天算。

稀有度跟着"发生过的事"走，不跟着人走：每个人起步都是一张 R 常驻卡，SR 是她心情好
时画的变体，UR 是群里的名场面（限量），SSR 留给生日、周年这类事件。出率表是完整的，
但某个稀有度在卡池里还没有卡时，结果会往下落到最近一档有卡的稀有度。

她心情好的时候手气会好一点：SR 以上的出率乘以 ``MOOD_BOOST``，多出来的从 R 里扣。
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .store import Card

LOCAL_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")

R, SR, SSR, UR = "R", "SR", "SSR", "UR"
#: 从低到高。
RARITIES = (R, SR, SSR, UR)
RATES = {R: 0.75, SR: 0.20, SSR: 0.045, UR: 0.005}
#: 第这么多抽仍没出 SSR 以上时，这一抽必出 SSR 以上（卡池里有的话）。
PITY_AT = 40
#: 她心情好时 SR 以上出率的倍数。
MOOD_BOOST = 1.2


def local_today(now: datetime) -> str:
    """每日抽卡次数按中国时间的自然日重置。"""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(LOCAL_TIMEZONE).date().isoformat()


def rates(*, lucky: bool = False) -> dict[str, float]:
    """这一抽的出率。心情好时 SR 以上整体乘 MOOD_BOOST，R 补足剩下的。"""
    if not lucky:
        return dict(RATES)
    boosted = {rarity: RATES[rarity] * MOOD_BOOST for rarity in (SR, SSR, UR)}
    return {R: 1.0 - sum(boosted.values()), **boosted}


def roll_rarity(rng: random.Random, *, pity: int, lucky: bool = False) -> str:
    table = rates(lucky=lucky)
    if pity + 1 >= PITY_AT:
        return rng.choices((SSR, UR), weights=(table[SSR], table[UR]))[0]
    point = rng.random()
    for rarity in reversed(RARITIES):
        if point < table[rarity]:
            return rarity
        point -= table[rarity]
    return R


@dataclass(slots=True)
class DrawResult:
    card: Card
    rolled: str
    pity_after: int


def draw(
    cards: Sequence[Card], rng: random.Random, *, pity: int, lucky: bool = False
) -> DrawResult | None:
    """从卡池里抽一张。卡池为空（或只剩发完的限量卡）时返回 None。

    掷出的稀有度没有卡时往下落；保底只在真的拿到 SSR 以上时才清零。
    """
    cards = [card for card in cards if not card.sold_out]
    if not cards:
        return None
    by_rarity: dict[str, list[Card]] = {}
    for card in cards:
        by_rarity.setdefault(card.rarity, []).append(card)
    rolled = roll_rarity(rng, pity=pity, lucky=lucky)
    order = RARITIES[: RARITIES.index(rolled) + 1][::-1]
    pool = next((by_rarity[r] for r in order if by_rarity.get(r)), None)
    if pool is None:
        # 比掷出的还低的档都没有卡（例如卡池里只有 SR）：就从最低的有卡档里抽。
        pool = next(by_rarity[r] for r in RARITIES if by_rarity.get(r))
    card = rng.choice(pool)
    high = RARITIES.index(card.rarity) >= RARITIES.index(SSR)
    return DrawResult(card=card, rolled=rolled, pity_after=0 if high else pity + 1)


def has_high_rarity(cards: Sequence[Card]) -> bool:
    """卡池里还有抽得到的 SSR 以上吗（发完的限量卡不算）。"""
    return any(
        RARITIES.index(card.rarity) >= RARITIES.index(SSR) and not card.sold_out for card in cards
    )
