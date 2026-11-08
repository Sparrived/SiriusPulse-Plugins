"""手帐上的小画：天气、称号徽章用的图标，以及花朵时钟、时间河、糖豆日历、话题泡泡、群友星座。

全部输出内联 SVG 字符串，不依赖 emoji 字体（容器里的 chromium 常常没有彩色 emoji）。
"""

from __future__ import annotations

import html
import math
import random
from typing import Any, Sequence

# ── 色板（已用 dataviz 校验器校验过浅色纸面上的分类色）─────────────────

PAPER = "#FFF9F1"
INK = "#4A3F55"
SUB = "#8C7F96"
FAINT = "#D9CFDC"
LINE = "#EFE6E9"

PINK = "#E0607E"
BLUE = "#3E86D6"
ORANGE = "#D9822B"
VIOLET = "#8A63D2"
GREEN = "#2F9E6E"
GOLD = "#B8901A"
CATEGORICAL = (PINK, BLUE, ORANGE, VIOLET, GREEN, GOLD)
TINTS = {
    PINK: "#FDE7EC",
    BLUE: "#E4F0FB",
    ORANGE: "#FCEBD9",
    VIOLET: "#EEE8FA",
    GREEN: "#E2F3EB",
    GOLD: "#F7EFD6",
}
#: 单色顺序色阶（量的多少）：粉，浅 → 深
RAMP = ("#FBE3E9", "#F7C3CF", "#EF97AB", "#E0607E", "#B9405D")


def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def ramp(value: float, top: float) -> str:
    if top <= 0 or value <= 0:
        return "#F4ECEE"
    ratio = min(1.0, value / top)
    return RAMP[min(len(RAMP) - 1, int(ratio * (len(RAMP) - 0.001)))]


# ── 图标（viewBox 0 0 64 64）────────────────────────────────────────────


def _svg(body: str, size: int, extra: str = "") -> str:
    return (
        f'<svg width="{size}" height="{size}" viewBox="0 0 64 64" {extra} '
        f'xmlns="http://www.w3.org/2000/svg">{body}</svg>'
    )


def _face(cx: float, cy: float, scale: float = 1.0, color: str = INK) -> str:
    s = scale
    return (
        f'<circle cx="{cx - 5 * s}" cy="{cy}" r="{1.8 * s}" fill="{color}"/>'
        f'<circle cx="{cx + 5 * s}" cy="{cy}" r="{1.8 * s}" fill="{color}"/>'
        f'<path d="M{cx - 3 * s} {cy + 4 * s} q{3 * s} {3 * s} {6 * s} 0" stroke="{color}" '
        f'stroke-width="{1.6 * s}" fill="none" stroke-linecap="round"/>'
        f'<ellipse cx="{cx - 9 * s}" cy="{cy + 3 * s}" rx="{2.4 * s}" ry="{1.4 * s}" fill="#F7A8B8" opacity=".8"/>'
        f'<ellipse cx="{cx + 9 * s}" cy="{cy + 3 * s}" rx="{2.4 * s}" ry="{1.4 * s}" fill="#F7A8B8" opacity=".8"/>'
    )


def _cloud(x: float, y: float, s: float, fill: str = "#FFFFFF", stroke: str = "#C9D6E8") -> str:
    return (
        f'<g transform="translate({x} {y}) scale({s})">'
        f'<path d="M10 34 a10 10 0 0 1 2-19.8 a14 14 0 0 1 26.5-3.2 a11 11 0 0 1 15.5 10 '
        f'a8 8 0 0 1-1 16 H12 a10 10 0 0 1-2-3z" fill="{fill}" stroke="{stroke}" stroke-width="2.4" '
        f'stroke-linejoin="round"/></g>'
    )


def _sun(cx: float, cy: float, r: float, face: bool = True) -> str:
    rays = "".join(
        f'<line x1="{cx + math.cos(a) * (r + 4):.1f}" y1="{cy + math.sin(a) * (r + 4):.1f}" '
        f'x2="{cx + math.cos(a) * (r + 10):.1f}" y2="{cy + math.sin(a) * (r + 10):.1f}" '
        f'stroke="#F2B33D" stroke-width="3.2" stroke-linecap="round"/>'
        for a in (i * math.pi / 4 for i in range(8))
    )
    body = f'{rays}<circle cx="{cx}" cy="{cy}" r="{r}" fill="#FFD166" stroke="#F2B33D" stroke-width="2.4"/>'
    return body + (_face(cx, cy, r / 13) if face else "")


def _moon(cx: float, cy: float, r: float, face: bool = True) -> str:
    return (
        f'<path d="M{cx + r * 0.35} {cy - r} a{r} {r} 0 1 0 {r * 0.65} {r * 1.55} '
        f'a{r * 0.82} {r * 0.82} 0 1 1 {-r * 0.65} {-r * 1.55}z" fill="#FFE8A3" stroke="#E8C25A" '
        f'stroke-width="2.4" stroke-linejoin="round"/>'
        + (_face(cx - r * 0.12, cy + r * 0.05, r / 18) if face else "")
    )


def _drops(x: float, y: float, n: int, color: str = "#6FA8E8") -> str:
    return "".join(
        f'<path d="M{x + i * 12} {y + (i % 2) * 5} q-3 6 0 8 q3-2 0-8z" fill="{color}"/>'
        for i in range(n)
    )


def weather_icon(kind: str, size: int = 64, face: bool = True) -> str:
    if kind == "sunny":
        body = _sun(32, 32, 14, face)
    elif kind == "rainbow":
        arcs = "".join(
            f'<path d="M6 46 a26 26 0 0 1 52 0" fill="none" stroke="{c}" stroke-width="4" '
            f'transform="translate(0 {i * 4.2}) scale(1 {1 - i * 0.04})" stroke-linecap="round"/>'
            for i, c in enumerate(
                ("#F28BA8", "#F7B267", "#F7D86A", "#8FD3A8", "#8CB8F0", "#B79CEB")
            )
        )
        body = (
            arcs
            + _cloud(-2, 34, 0.42)
            + _cloud(34, 34, 0.42)
            + (_face(32, 36, 0.6) if face else "")
        )
    elif kind == "cloudy":
        body = _sun(42, 22, 10, False) + _cloud(2, 14, 0.95) + (_face(28, 34, 0.75) if face else "")
    elif kind == "drizzle":
        body = (
            _cloud(2, 6, 0.95, "#EEF2F8", "#A9B8CF")
            + _drops(18, 46, 3)
            + (
                f'<circle cx="23" cy="26" r="1.7" fill="{INK}"/><circle cx="33" cy="26" r="1.7" fill="{INK}"/>'
                f'<path d="M25 32 q3-2.4 6 0" stroke="{INK}" stroke-width="1.6" fill="none" stroke-linecap="round"/>'
                if face
                else ""
            )
        )
    elif kind == "storm":
        body = _cloud(
            2, 4, 0.95, "#E3E7EF", "#8E9AB2"
        ) + '<path d="M32 38 l-7 12 h7 l-4 11 l12-15 h-7 l5-8z" fill="#FFD166" stroke="#E0A92E" ' 'stroke-width="1.6" stroke-linejoin="round"/>' + _drops(
            12, 44, 1
        ) + _drops(
            46, 42, 1
        )
    elif kind == "breeze":
        body = (
            '<path d="M8 24 h30 a7 7 0 1 0-7-7" fill="none" stroke="#8CC7B5" stroke-width="3.4" stroke-linecap="round"/>'
            '<path d="M8 36 h42 a7 7 0 1 1-7 7" fill="none" stroke="#8CC7B5" stroke-width="3.4" stroke-linecap="round"/>'
            '<path d="M14 48 h16" stroke="#B6DDD0" stroke-width="3.4" stroke-linecap="round"/>'
        )
    else:  # moon / 静夜
        stars = "".join(
            f'<path d="M{x} {y - 4} l1.2 2.8 l2.8 1.2 l-2.8 1.2 l-1.2 2.8 l-1.2-2.8 l-2.8-1.2 l2.8-1.2z" fill="#F7D86A"/>'
            for x, y in ((50, 14), (54, 34), (12, 50))
        )
        body = _moon(28, 32, 18, face) + stars
    return _svg(body, size)


def icon(name: str, size: int = 28, color: str = PINK) -> str:
    """称号、统计贴纸用的小图标。"""
    tint = TINTS.get(color, "#FFFFFF")
    shapes = {
        "chat": (
            f'<path d="M10 14 h44 a6 6 0 0 1 6 6 v20 a6 6 0 0 1-6 6 H28 l-12 10 v-10 h-6 a6 6 0 0 1-6-6 V20 '
            f'a6 6 0 0 1 6-6z" fill="{tint}" stroke="{color}" stroke-width="3.4" stroke-linejoin="round"/>'
            f'<circle cx="22" cy="30" r="3" fill="{color}"/><circle cx="32" cy="30" r="3" fill="{color}"/>'
            f'<circle cx="42" cy="30" r="3" fill="{color}"/>'
        ),
        "laugh": (
            f'<circle cx="32" cy="32" r="24" fill="{tint}" stroke="{color}" stroke-width="3.4"/>'
            f'<path d="M20 26 q4-5 8 0 M36 26 q4-5 8 0" stroke="{color}" stroke-width="3.2" fill="none" stroke-linecap="round"/>'
            f'<path d="M19 35 h26 a13 13 0 0 1-26 0z" fill="{color}"/>'
        ),
        "spark": (
            f'<path d="M32 6 l6 18 l18 8 l-18 8 l-6 18 l-6-18 l-18-8 l18-8z" fill="{tint}" stroke="{color}" '
            f'stroke-width="3.4" stroke-linejoin="round"/>'
        ),
        "catch": (
            f'<path d="M6 18 h30 a5 5 0 0 1 5 5 v12 a5 5 0 0 1-5 5 h-16 l-8 7 v-7 h-6 a5 5 0 0 1-5-5 v-12 '
            f'a5 5 0 0 1 5-5z" fill="{tint}" stroke="{color}" stroke-width="3" stroke-linejoin="round"/>'
            f'<path d="M58 30 h-20 a5 5 0 0 0-5 5 v10 a5 5 0 0 0 5 5 h12 l7 6 v-6 h1 a5 5 0 0 0 5-5 v-10 '
            f'a5 5 0 0 0-5-5z" fill="{color}"/>'
        ),
        "heart": (
            f'<path d="M32 54 C10 40 6 28 10 20 a12 12 0 0 1 22-2 a12 12 0 0 1 22 2 c4 8 0 20-22 34z" '
            f'fill="{tint}" stroke="{color}" stroke-width="3.4" stroke-linejoin="round"/>'
            f'<path d="M18 22 q3-4 8-3" stroke="#FFFFFF" stroke-width="3" fill="none" stroke-linecap="round"/>'
        ),
        "sticker": (
            f'<path d="M12 8 h40 a4 4 0 0 1 4 4 v28 l-16 16 h-28 a4 4 0 0 1-4-4 v-40 a4 4 0 0 1 4-4z" '
            f'fill="{tint}" stroke="{color}" stroke-width="3.4" stroke-linejoin="round"/>'
            f'<path d="M56 40 h-12 a4 4 0 0 0-4 4 v12" fill="{color}" opacity=".35"/>'
            + _face(32, 27, 1.0, color)
        ),
        "moon": _moon(32, 32, 20, False).replace("#FFE8A3", tint).replace("#E8C25A", color),
        "sun": (
            f'<circle cx="32" cy="32" r="13" fill="{tint}" stroke="{color}" stroke-width="3.4"/>'
            + "".join(
                f'<line x1="{32 + math.cos(a) * 19:.1f}" y1="{32 + math.sin(a) * 19:.1f}" '
                f'x2="{32 + math.cos(a) * 26:.1f}" y2="{32 + math.sin(a) * 26:.1f}" stroke="{color}" '
                f'stroke-width="3.4" stroke-linecap="round"/>'
                for a in (i * math.pi / 4 for i in range(8))
            )
        ),
        "question": (
            f'<circle cx="32" cy="32" r="24" fill="{tint}" stroke="{color}" stroke-width="3.4"/>'
            f'<path d="M24 25 a8 8 0 1 1 11 7.5 c-2.5 1-3 2.5-3 5.5" stroke="{color}" stroke-width="4" '
            f'fill="none" stroke-linecap="round"/><circle cx="32" cy="46" r="2.8" fill="{color}"/>'
        ),
        "pencil": (
            f'<path d="M14 50 l4-14 l26-26 l10 10 l-26 26z" fill="{tint}" stroke="{color}" stroke-width="3.4" '
            f'stroke-linejoin="round"/><path d="M38 16 l10 10" stroke="{color}" stroke-width="3.4"/>'
            f'<path d="M14 50 l4-14 l10 10z" fill="{color}"/>'
        ),
        "camera": (
            f'<rect x="6" y="18" width="52" height="34" rx="7" fill="{tint}" stroke="{color}" stroke-width="3.4"/>'
            f'<path d="M22 18 l4-7 h12 l4 7" fill="{tint}" stroke="{color}" stroke-width="3.4" stroke-linejoin="round"/>'
            f'<circle cx="32" cy="35" r="10" fill="#FFFFFF" stroke="{color}" stroke-width="3.4"/>'
            f'<circle cx="32" cy="35" r="4" fill="{color}"/>'
        ),
        "fire": (
            f'<path d="M32 6 c4 10 16 16 16 30 a16 16 0 0 1-32 0 c0-8 4-12 8-16 c0 6 2 9 5 10 c-1-10 1-17 3-24z" '
            f'fill="{tint}" stroke="{color}" stroke-width="3.4" stroke-linejoin="round"/>'
            f'<path d="M32 34 c3 4 7 6 7 11 a7 7 0 0 1-14 0 c0-4 4-6 7-11z" fill="{color}"/>'
        ),
        "people": (
            f'<circle cx="24" cy="22" r="9" fill="{tint}" stroke="{color}" stroke-width="3.2"/>'
            f'<path d="M8 52 a16 16 0 0 1 32 0z" fill="{tint}" stroke="{color}" stroke-width="3.2" stroke-linejoin="round"/>'
            f'<circle cx="44" cy="24" r="7" fill="{color}"/><path d="M34 52 a12 12 0 0 1 24 0z" fill="{color}"/>'
        ),
        "star": (
            f'<path d="M32 6 l7.6 15.6 l17.2 2.4 l-12.4 12.2 l3 17.1 l-15.4-8.1 l-15.4 8.1 l3-17.1 '
            f'l-12.4-12.2 l17.2-2.4z" fill="{tint}" stroke="{color}" stroke-width="3.2" stroke-linejoin="round"/>'
        ),
        "crown": (
            f'<path d="M8 22 l12 12 l12-20 l12 20 l12-12 l-5 28 h-38z" fill="{tint}" stroke="{color}" '
            f'stroke-width="3.4" stroke-linejoin="round"/><circle cx="32" cy="12" r="3.4" fill="{color}"/>'
        ),
        "repeat": (
            f'<path d="M14 26 a16 16 0 0 1 30-6 l4-6 v16 h-16 l6-5 a10 10 0 0 0-18 3z" fill="{color}"/>'
            f'<path d="M50 38 a16 16 0 0 1-30 6 l-4 6 v-16 h16 l-6 5 a10 10 0 0 0 18-3z" fill="{color}" opacity=".55"/>'
        ),
        "new": (
            f'<path d="M32 6 l6 8 l10-2 l-1 10 l9 5 l-7 7 l4 9 l-10 2 l-2 10 l-9-5 l-9 5 l-2-10 l-10-2 '
            f'l4-9 l-7-7 l9-5 l-1-10 l10 2z" fill="{tint}" stroke="{color}" stroke-width="3" stroke-linejoin="round"/>'
            f'<circle cx="32" cy="32" r="6" fill="{color}"/>'
        ),
    }
    return _svg(shapes.get(name, shapes["star"]), size)


AWARD_ICONS = {
    "chatter": ("chat", PINK),
    "mood_maker": ("laugh", ORANGE),
    "icebreaker": ("spark", BLUE),
    "catcher": ("catch", VIOLET),
    "popular": ("heart", PINK),
    "sticker": ("sticker", GREEN),
    "night_owl": ("moon", VIOLET),
    "early_bird": ("sun", ORANGE),
    "curious": ("question", BLUE),
    "writer": ("pencil", GOLD),
    "photographer": ("camera", GREEN),
}


# ── 花朵时钟：24 片花瓣，长度 = 那一小时的消息量 ───────────────────────


def flower_clock(
    hours: Sequence[int], *, size: int = 400, center_label: str = "", center_sub: str = ""
) -> str:
    cx = cy = size / 2
    inner = size * 0.16
    outer = size * 0.46
    top = max(hours) if hours else 0
    parts: list[str] = [
        f'<circle cx="{cx}" cy="{cy}" r="{outer + 10}" fill="#FFFFFF" stroke="{LINE}" stroke-width="2"/>',
        f'<circle cx="{cx}" cy="{cy}" r="{(inner + outer) / 2}" fill="none" stroke="{LINE}" stroke-width="1"/>',
    ]
    peak = hours.index(top) if top else -1
    for hour, count in enumerate(hours):
        angle = (hour + 0.5) / 24 * 2 * math.pi - math.pi / 2
        length = inner + 8 + (outer - inner - 8) * (count / top if top else 0)
        ratio = count / top if top else 0
        tip_x, tip_y = cx + math.cos(angle) * length, cy + math.sin(angle) * length
        base_x, base_y = cx + math.cos(angle) * inner, cy + math.sin(angle) * inner
        nx, ny = -math.sin(angle), math.cos(angle)
        mid = inner + (length - inner) * 0.5
        mx, my = cx + math.cos(angle) * mid, cy + math.sin(angle) * mid
        # 花瓣宽度跟着所在半径的弧长走，热闹的那几瓣更饱满
        w = mid * math.sin(math.pi / 24) * (1.25 + 0.5 * ratio)
        d = (
            f"M{base_x:.1f} {base_y:.1f} "
            f"C{mx + nx * w:.1f} {my + ny * w:.1f} {tip_x + nx * w * 0.5:.1f} {tip_y + ny * w * 0.5:.1f} "
            f"{tip_x:.1f} {tip_y:.1f} "
            f"C{tip_x - nx * w * 0.5:.1f} {tip_y - ny * w * 0.5:.1f} {mx - nx * w:.1f} {my - ny * w:.1f} "
            f"{base_x:.1f} {base_y:.1f}z"
        )
        fill = ramp(count, top) if count else "#F4ECEE"
        stroke = "#FFFFFF"
        parts.append(f'<path d="{d}" fill="{fill}" stroke="{stroke}" stroke-width="2"/>')
        if hour == peak and count:
            px, py = cx + math.cos(angle) * (length + 18), cy + math.sin(angle) * (length + 18)
            parts.append(
                f'<g transform="translate({px - 11:.1f} {py - 11:.1f})">{icon("crown", 22, GOLD)}</g>'
            )
    for hour, label in ((0, "0"), (6, "6"), (12, "12"), (18, "18")):
        angle = hour / 24 * 2 * math.pi - math.pi / 2
        lx, ly = cx + math.cos(angle) * (outer + 26), cy + math.sin(angle) * (outer + 26)
        parts.append(
            f'<text x="{lx:.1f}" y="{ly + 5:.1f}" text-anchor="middle" font-size="14" fill="{SUB}">{label}</text>'
        )
    # 白天太阳、夜里月亮
    sun_angle = 12.9 / 24 * 2 * math.pi - math.pi / 2
    moon_angle = -1.1 / 24 * 2 * math.pi - math.pi / 2
    sx, sy = cx + (outer + 30) * math.cos(sun_angle), cy + (outer + 30) * math.sin(sun_angle)
    mx2, my2 = cx + (outer + 30) * math.cos(moon_angle), cy + (outer + 30) * math.sin(moon_angle)
    parts.append(
        f'<g transform="translate({mx2 - 14:.1f} {my2 - 14:.1f})">{weather_icon("moon", 28, False)}</g>'
    )
    parts.append(
        f'<g transform="translate({sx - 14:.1f} {sy - 14:.1f})">{weather_icon("sunny", 28, False)}</g>'
    )
    parts.append(f'<circle cx="{cx}" cy="{cy}" r="{inner - 6}" fill="{TINTS[PINK]}"/>')
    if center_label:
        parts.append(
            f'<text x="{cx}" y="{cy + 2}" text-anchor="middle" class="cute" font-size="30" fill="{INK}">'
            f"{esc(center_label)}</text>"
        )
    if center_sub:
        parts.append(
            f'<text x="{cx}" y="{cy + 24}" text-anchor="middle" font-size="13" fill="{SUB}">{esc(center_sub)}</text>'
        )
    pad = 46
    return (
        f'<svg width="{size + pad * 2}" height="{size + pad * 2}" viewBox="{-pad} {-pad} {size + pad * 2} '
        f'{size + pad * 2}" xmlns="http://www.w3.org/2000/svg">{"".join(parts)}</svg>'
    )


# ── 时间河：一天的消息密度画成一条河，章节是河上的小船 ─────────────────


def time_river(
    stamps: Sequence[int],
    *,
    start: int,
    end: int,
    chapters: Sequence[dict[str, Any]],
    width: int = 940,
    height: int = 150,
    tick_labels: Sequence[tuple[int, str]] = (),
) -> str:
    span = max(end - start, 60)
    bins = 96
    counts = [0] * bins
    for ts in stamps:
        if start <= ts <= end:
            counts[min(bins - 1, int((ts - start) / span * bins))] += 1
    # 平滑一下，像水
    smooth = [
        (counts[max(i - 1, 0)] + 2 * counts[i] + counts[min(i + 1, bins - 1)]) / 4
        for i in range(bins)
    ]
    top = max(smooth) or 1
    base = height - 34
    amp = height - 70
    step = width / (bins - 1)
    upper = [(i * step, base - 6 - amp * (v / top) / 2) for i, v in enumerate(smooth)]
    lower = [(i * step, base + 6 + amp * (v / top) / 2 * 0.35) for i, v in enumerate(smooth)]

    def path(points: Sequence[tuple[float, float]]) -> str:
        out = f"M{points[0][0]:.1f} {points[0][1]:.1f}"
        for (x0, y0), (x1, y1) in zip(points, points[1:]):
            mx = (x0 + x1) / 2
            out += f" C{mx:.1f} {y0:.1f} {mx:.1f} {y1:.1f} {x1:.1f} {y1:.1f}"
        return out

    body = path(upper) + " L" + path(list(reversed(lower)))[1:] + "z"
    parts = [
        '<defs><linearGradient id="river" x1="0" x2="0" y1="0" y2="1">'
        '<stop offset="0" stop-color="#9CC6F2"/><stop offset="1" stop-color="#D6E8FA"/></linearGradient></defs>',
        f'<path d="{body}" fill="url(#river)" stroke="#7DB1EA" stroke-width="2"/>',
    ]
    # 河上的波纹
    for i in range(6, bins - 6, 11):
        x, y = upper[i]
        parts.append(
            f'<path d="M{x - 8:.1f} {base:.1f} q4-4 8 0 q4 4 8 0" stroke="#FFFFFF" stroke-width="2" '
            f'fill="none" stroke-linecap="round" opacity=".8"/>'
        )
    for ts, label in tick_labels:
        x = (ts - start) / span * width
        if -1 <= x <= width + 1:
            parts.append(
                f'<text x="{x:.1f}" y="{height - 6}" text-anchor="middle" font-size="13" fill="{SUB}">{esc(label)}</text>'
            )
    for number, chapter in enumerate(chapters, 1):
        mid = (chapter["start"] + chapter["end"]) / 2
        x = (mid - start) / span * width
        index = min(bins - 1, max(0, int((mid - start) / span * bins)))
        y = upper[index][1] - 12
        color = CATEGORICAL[(number - 1) % len(CATEGORICAL)]
        parts.append(
            f'<g transform="translate({x:.1f} {y:.1f})">'
            f'<path d="M0 0 l-11-16 a13 13 0 1 1 22 0z" fill="{color}" stroke="#FFFFFF" stroke-width="2.4"/>'
            f'<text x="0" y="-15" text-anchor="middle" font-size="14" font-weight="700" fill="#FFFFFF">{number}</text>'
            f"</g>"
        )
    return (
        f'<svg width="{width}" height="{height + 24}" viewBox="0 -24 {width} {height + 24}" '
        f'xmlns="http://www.w3.org/2000/svg" overflow="visible">{"".join(parts)}</svg>'
    )


# ── 糖豆日历：7 天 × 24 小时，豆子大小和颜色都表示热闹程度 ──────────────


WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def candy_calendar(
    grid: Sequence[Sequence[int]], *, row_labels: Sequence[str] = WEEKDAYS, width: int = 930
) -> str:
    left, top_pad = 48, 26
    cell = (width - left - 4) / 24
    pitch = max(cell, 28.0)
    height = top_pad + pitch * len(grid) + 8
    top = max((max(row) for row in grid), default=0)
    parts: list[str] = []
    for hour in range(0, 24, 3):
        parts.append(
            f'<text x="{left + hour * cell + cell / 2:.1f}" y="16" text-anchor="middle" font-size="12" fill="{SUB}">{hour}</text>'
        )
    for r, row in enumerate(grid):
        y = top_pad + r * pitch + pitch / 2
        parts.append(
            f'<text x="{left - 12}" y="{y + 5}" text-anchor="end" font-size="14" fill="{INK}">{esc(row_labels[r])}</text>'
        )
        for hour, count in enumerate(row):
            x = left + hour * cell + cell / 2
            if count <= 0:
                parts.append(f'<circle cx="{x}" cy="{y}" r="3" fill="{FAINT}"/>')
                continue
            radius = 3.5 + (min(cell, pitch) * 0.5 - 3.5) * math.sqrt(count / top)
            parts.append(
                f'<circle cx="{x}" cy="{y}" r="{radius:.1f}" fill="{ramp(count, top)}" stroke="#FFFFFF" stroke-width="2"/>'
                f'<ellipse cx="{x - radius * 0.32:.1f}" cy="{y - radius * 0.38:.1f}" rx="{radius * 0.3:.1f}" '
                f'ry="{radius * 0.18:.1f}" fill="#FFFFFF" opacity=".55"/>'
            )
    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'xmlns="http://www.w3.org/2000/svg">{"".join(parts)}</svg>'
    )


# ── 话题泡泡：贪心圆堆积 ────────────────────────────────────────────────


def bubbles(words: Sequence[tuple[str, int]], *, width: int = 440, height: int = 360) -> str:
    if not words:
        return ""
    top = max(count for _, count in words)
    items = []
    for index, (word, count) in enumerate(words):
        radius = 26 + 46 * math.sqrt(count / top)
        radius = max(radius, 11 + len(word) * 7.5)
        items.append((word, count, radius, CATEGORICAL[index % len(CATEGORICAL)]))
    placed: list[tuple[float, float, float]] = []
    out: list[str] = []
    cx, cy = width / 2, height / 2
    for word, count, radius, color in items:
        spot = None
        for step in range(0, 2400):
            angle = step * 0.31
            dist = step * 0.55
            x = cx + math.cos(angle) * dist
            y = cy + math.sin(angle) * dist * 0.8
            if (
                x - radius < 2
                or x + radius > width - 2
                or y - radius < 2
                or y + radius > height - 2
            ):
                continue
            if all(math.hypot(x - px, y - py) >= radius + pr + 4 for px, py, pr in placed):
                spot = (x, y)
                break
        if spot is None:
            continue
        x, y = spot
        placed.append((x, y, radius))
        font = max(13, min(30, radius * 0.5, radius * 1.7 / max(len(word), 1)))
        out.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{radius:.1f}" fill="{TINTS[color]}" stroke="{color}" stroke-width="2.4"/>'
            f'<ellipse cx="{x - radius * 0.4:.1f}" cy="{y - radius * 0.45:.1f}" rx="{radius * 0.22:.1f}" '
            f'ry="{radius * 0.12:.1f}" fill="#FFFFFF" transform="rotate(-30 {x - radius * 0.4:.1f} {y - radius * 0.45:.1f})"/>'
            f'<text x="{x:.1f}" y="{y + font * 0.18:.1f}" text-anchor="middle" font-size="{font:.1f}" '
            f'class="cute" fill="{INK}">{esc(word)}</text>'
            f'<text x="{x:.1f}" y="{y + font * 0.18 + 16:.1f}" text-anchor="middle" font-size="12" fill="{SUB}">{count}</text>'
        )
    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'xmlns="http://www.w3.org/2000/svg">{"".join(out)}</svg>'
    )


# ── 群友星座：力导向的小网，节点是头像星星 ──────────────────────────────


def constellation(
    stars: Sequence[str],
    edges: Sequence[dict[str, Any]],
    *,
    names: dict[str, str],
    avatars: dict[str, str],
    weights: dict[str, int],
    width: int = 940,
    height: int = 460,
) -> str:
    if not stars:
        return ""
    rng = random.Random("|".join(stars))
    count = len(stars)
    pos = {
        uid: [
            width / 2 + math.cos(i / count * 2 * math.pi) * width * 0.32,
            height / 2 + math.sin(i / count * 2 * math.pi) * height * 0.3,
        ]
        for i, uid in enumerate(stars)
    }
    top_edge = max((e["score"] for e in edges), default=1)
    for _ in range(260):
        force = {uid: [0.0, 0.0] for uid in stars}
        for i, a in enumerate(stars):
            for b in stars[i + 1 :]:
                dx, dy = pos[a][0] - pos[b][0], pos[a][1] - pos[b][1]
                dist = max(math.hypot(dx, dy), 1.0)
                push = 26000 / (dist * dist)
                force[a][0] += dx / dist * push
                force[a][1] += dy / dist * push
                force[b][0] -= dx / dist * push
                force[b][1] -= dy / dist * push
        for edge in edges:
            a, b = edge["a"], edge["b"]
            dx, dy = pos[b][0] - pos[a][0], pos[b][1] - pos[a][1]
            dist = max(math.hypot(dx, dy), 1.0)
            pull = (dist - 170) * 0.02 * (0.4 + edge["score"] / top_edge)
            force[a][0] += dx / dist * pull
            force[a][1] += dy / dist * pull
            force[b][0] -= dx / dist * pull
            force[b][1] -= dy / dist * pull
        for uid in stars:
            force[uid][0] += (width / 2 - pos[uid][0]) * 0.01
            force[uid][1] += (height / 2 - pos[uid][1]) * 0.012
            pos[uid][0] = min(width - 70, max(70, pos[uid][0] + max(-12, min(12, force[uid][0]))))
            pos[uid][1] = min(height - 62, max(52, pos[uid][1] + max(-12, min(12, force[uid][1]))))
    parts = []
    for _ in range(70):
        x, y = rng.uniform(0, width), rng.uniform(0, height)
        parts.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{rng.choice((0.8, 1.1, 1.6)):.1f}" fill="#FFFFFF" '
            f'opacity="{rng.uniform(0.3, 0.9):.2f}"/>'
        )
    for edge in edges:
        a, b = pos[edge["a"]], pos[edge["b"]]
        strength = edge["score"] / top_edge
        parts.append(
            f'<line x1="{a[0]:.1f}" y1="{a[1]:.1f}" x2="{b[0]:.1f}" y2="{b[1]:.1f}" stroke="#F7D86A" '
            f'stroke-width="{1 + 4 * strength:.1f}" stroke-linecap="round" opacity="{0.35 + 0.55 * strength:.2f}"/>'
        )
    top_weight = max(weights.values(), default=1) or 1
    for uid in stars:
        x, y = pos[uid]
        radius = 20 + 14 * math.sqrt(weights.get(uid, 0) / top_weight)
        clip = f"clip-{abs(hash(uid)) % 10_000_000}"
        parts.append(
            f'<defs><clipPath id="{clip}"><circle cx="{x:.1f}" cy="{y:.1f}" r="{radius:.1f}"/></clipPath></defs>'
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{radius + 7:.1f}" fill="#F7D86A" opacity=".18"/>'
            f'<image href="{avatars.get(uid, "")}" x="{x - radius:.1f}" y="{y - radius:.1f}" '
            f'width="{radius * 2:.1f}" height="{radius * 2:.1f}" clip-path="url(#{clip})" preserveAspectRatio="xMidYMid slice"/>'
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{radius:.1f}" fill="none" stroke="#FFF3C4" stroke-width="3"/>'
            f'<text x="{x:.1f}" y="{y + radius + 20:.1f}" text-anchor="middle" font-size="14" fill="#F3EEFF">'
            f"{esc(_short(names.get(uid, uid), 8))}</text>"
        )
    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'xmlns="http://www.w3.org/2000/svg">{"".join(parts)}</svg>'
    )


def _short(text: str, limit: int) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


short = _short


def initial_avatar(name: str, uid: str) -> str:
    """头像拿不到时，用名字首字画一个圆头像（data URI）。"""
    import base64

    color = CATEGORICAL[sum(map(ord, uid or name or "?")) % len(CATEGORICAL)]
    letter = esc((str(name or "?").strip() or "?")[0])
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96" viewBox="0 0 96 96">'
        f'<circle cx="48" cy="48" r="48" fill="{TINTS[color]}"/>'
        f'<text x="48" y="62" text-anchor="middle" font-size="42" font-family="sans-serif" '
        f'font-weight="700" fill="{color}">{letter}</text></svg>'
    )
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")
