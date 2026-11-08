"""用 Pillow 在内存里画卡面和图鉴，不落临时文件。

风格跟着「月白的小屋」走：深夜的蓝、月光的银，称号用一点暖金。
"""

from __future__ import annotations

import io
import os
import random
from functools import lru_cache
from importlib.resources import files
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .members import STAT_KEYS
from .store import Card, Owned

CARD_W, CARD_H = 600, 900

_NIGHT_TOP = (22, 30, 56)
_NIGHT_BOTTOM = (46, 58, 99)
_SILVER = (214, 222, 242)
_SOFT = (168, 180, 214)
_FAINT = (118, 130, 168)
_GOLD = (242, 212, 138)
_PANEL = (255, 255, 255, 22)

RARITY_COLORS = {
    "R": (159, 180, 224),
    "SR": (196, 158, 238),
    "SSR": (242, 196, 102),
    "UR": (255, 128, 160),
}


@lru_cache(maxsize=32)
def _font(size: int, bold: bool = False, cute: bool = False) -> ImageFont.FreeTypeFont:
    bundled = Path(
        str(
            files("sirius_pulse").joinpath("tools/builtin/_internal/assets/ZCOOLKuaiLe-Regular.ttf")
        )
    )
    windows = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    candidates = [bundled] if cute else []
    candidates += [
        windows / ("msyhbd.ttc" if bold else "msyh.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
        Path("/System/Library/Fonts/PingFang.ttc"),
        bundled,
    ]
    for path in candidates:
        if path.is_file():
            return ImageFont.truetype(str(path), size)
    raise OSError("缺少中文字体，请安装 Noto Sans CJK。")


# 不可见的双向控制符/零宽字符：群昵称里带进来时，字体会画成方框或空白。
_INVISIBLE_RE = __import__("re").compile(r"[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff\u00ad]")


def _clean_text(text: object) -> str:
    return _INVISIBLE_RE.sub("", str(text or ""))


def _glyph_supported(font: ImageFont.FreeTypeFont, char: str) -> bool:
    """字体缺字时会画 .notdef 方框；用私用区字符的掩码当基准判断。"""
    if char.isspace():
        return True
    try:
        missing = Image.Image()._new(font.getmask("\ue000"))
        current = Image.Image()._new(font.getmask(char))
    except Exception:
        return True
    return not (
        current.size == missing.size
        and current.tobytes() == missing.tobytes()
    )


def _font_for(
    text: str, size: int, bold: bool = False, cute: bool = False
) -> ImageFont.FreeTypeFont:
    """可爱字体缺字（假名、生僻字）时退回完整中文字体，避免方框。"""
    if cute:
        candidate = _font(size, bold=bold, cute=True)
        if all(_glyph_supported(candidate, ch) for ch in text):
            return candidate
    return _font(size, bold=bold, cute=False)


def _wrap(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, width: int
) -> list[str]:
    lines: list[str] = []
    line = ""
    for char in text:
        if draw.textlength(line + char, font=font) > width and line:
            lines.append(line)
            line = char
        else:
            line += char
    if line:
        lines.append(line)
    return lines


def _centered(
    draw: ImageDraw.ImageDraw, y: int, text: str, font, fill, width: int = CARD_W
) -> None:
    x = (width - draw.textlength(text, font=font)) / 2
    draw.text((x, y), text, font=font, fill=fill)


def _night(size: tuple[int, int], seed: str) -> Image.Image:
    width, height = size
    image = Image.new("RGB", size, _NIGHT_TOP)
    draw = ImageDraw.Draw(image)
    for y in range(height):
        t = y / max(1, height - 1)
        color = tuple(round(a + (b - a) * t) for a, b in zip(_NIGHT_TOP, _NIGHT_BOTTOM))
        draw.line([(0, y), (width, y)], fill=color)
    rng = random.Random(seed)
    for _ in range(width * height // 6000):
        x, y = rng.randrange(width), rng.randrange(height)
        r = rng.choice((1, 1, 1, 2))
        shade = rng.randrange(150, 235)
        draw.ellipse([x - r, y - r, x + r, y + r], fill=(shade, shade, min(255, shade + 20)))
    return image


def _avatar(avatar: bytes | None, name: str, size: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, size - 1, size - 1], fill=255)
    face: Image.Image | None = None
    if avatar:
        try:
            with Image.open(io.BytesIO(avatar)) as loaded:
                face = loaded.convert("RGB").resize((size, size), Image.Resampling.LANCZOS)
        except Exception:
            face = None
    if face is None:
        # 没有头像时画名字的第一个字，像一枚月亮上的印章。
        face = Image.new("RGB", (size, size), (64, 78, 128))
        draw = ImageDraw.Draw(face)
        initial = (name or "?")[0]
        font = _font_for(initial, size // 2, cute=True)
        box = draw.textbbox((0, 0), initial, font=font)
        draw.text(
            ((size - (box[2] - box[0])) / 2 - box[0], (size - (box[3] - box[1])) / 2 - box[1]),
            initial,
            font=font,
            fill=_SILVER,
        )
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(face, (0, 0), mask)
    return out


def render_card(card: Card, *, avatar: bytes | None, persona_name: str) -> bytes:
    """一张收藏卡，600×900 PNG。"""
    rarity_color = RARITY_COLORS.get(card.rarity, RARITY_COLORS["R"])
    base = _night((CARD_W, CARD_H), card.card_id).convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # 外框与内框
    draw.rounded_rectangle(
        [10, 10, CARD_W - 11, CARD_H - 11], radius=28, outline=rarity_color, width=4
    )
    draw.rounded_rectangle(
        [22, 22, CARD_W - 23, CARD_H - 23], radius=20, outline=(*_SILVER, 90), width=1
    )

    # 顶栏：编号、稀有度、版本
    small = _font(20)
    draw.text((44, 40), card.label, font=_font(22, bold=True), fill=_SILVER)
    badge = _font(22, bold=True)
    badge_w = draw.textlength(card.rarity, font=badge) + 28
    draw.rounded_rectangle(
        [CARD_W - 44 - badge_w, 36, CARD_W - 44, 72], radius=18, fill=(*rarity_color, 230)
    )
    draw.text((CARD_W - 44 - badge_w + 14, 40), card.rarity, font=badge, fill=_NIGHT_TOP)
    version = f"初版 · {card.minted_at[:7].replace('-', '.')}" if card.minted_at else "初版"
    _centered(draw, 44, version, small, _FAINT)

    # 头像，带一圈月光
    size = 240
    cx, top = CARD_W // 2, 96
    glow = Image.new("RGBA", base.size, (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse(
        [cx - size // 2 - 18, top - 18, cx + size // 2 + 18, top + size + 18],
        fill=(*rarity_color, 120),
    )
    base = Image.alpha_composite(base, glow.filter(ImageFilter.GaussianBlur(16)))
    draw.ellipse(
        [cx - size // 2 - 5, top - 5, cx + size // 2 + 5, top + size + 5], outline=_SILVER, width=3
    )

    # 名字与称号
    subject_name = _clean_text(card.subject_name)
    title_text = f"「{_clean_text(card.title)}」"
    _centered(draw, 360, subject_name[:14], _font(36, bold=True), _SILVER)
    _centered(draw, 410, title_text, _font_for(title_text, 30, cute=True), _GOLD)

    # 属性条
    y = 470
    label_font, value_font = _font(21), _font(21, bold=True)
    for key in STAT_KEYS:
        value = card.stats.get(key)
        draw.text((64, y), key, font=label_font, fill=_SOFT)
        left, right = 130, CARD_W - 120
        draw.rounded_rectangle([left, y + 9, right, y + 21], radius=6, fill=(255, 255, 255, 28))
        if isinstance(value, int):
            filled = left + (right - left) * value / 99
            draw.rounded_rectangle(
                [left, y + 9, filled, y + 21], radius=6, fill=(*rarity_color, 235)
            )
        text = "??" if value is None else str(value)
        draw.text(
            (CARD_W - 64 - draw.textlength(text, font=value_font), y),
            text,
            font=value_font,
            fill=_SILVER,
        )
        y += 36

    # 技能
    skill_top = y + 14
    skill_font = _font(21)
    skill_lines = _wrap(draw, _clean_text(card.skill_text), skill_font, CARD_W - 140)[:3]
    skill_bottom = skill_top + 48 + 30 * len(skill_lines)
    draw.rounded_rectangle([48, skill_top, CARD_W - 48, skill_bottom], radius=16, fill=_PANEL)
    draw.text(
        (70, skill_top + 12),
        f"技能【{_clean_text(card.skill_name)}】",
        font=_font(23, bold=True),
        fill=_GOLD,
    )
    for index, line in enumerate(skill_lines):
        draw.text((70, skill_top + 46 + 30 * index), line, font=skill_font, fill=_SILVER)

    # 她的批注
    note_text = f"—— {_clean_text(persona_name)}：{_clean_text(card.note)}"
    note_font = _font_for(note_text, 21, cute=True)
    note_lines = _wrap(draw, note_text, note_font, CARD_W - 120)[:3]
    note_top = CARD_H - 50 - 30 * len(note_lines)
    for index, line in enumerate(note_lines):
        draw.text((60, note_top + 30 * index), line, font=note_font, fill=_SOFT)

    base = Image.alpha_composite(base, overlay)
    base.alpha_composite(_avatar(avatar, subject_name, size), (cx - size // 2, top))
    out = io.BytesIO()
    base.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()


_CELL_W, _CELL_H, _GAP, _COLS = 220, 132, 16, 4


def render_collection(
    *, owner_name: str, owned: list[Owned], pool: list[Card], persona_name: str
) -> bytes:
    """图鉴：本群卡池里的每一张，收到的亮着，没收到的是问号。"""
    have = {item.card.card_id: item.copies for item in owned}
    rows = max(1, -(-len(pool) // _COLS))
    width = _COLS * _CELL_W + (_COLS + 1) * _GAP
    height = 150 + rows * (_CELL_H + _GAP) + 40
    base = _night((width, height), owner_name).convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    owner = _clean_text(owner_name)
    persona = _clean_text(persona_name)
    draw.text((_GAP + 8, 30), f"{owner[:12]}的图鉴", font=_font(36, bold=True), fill=_SILVER)
    progress = f"已收集 {len(have)}/{len(pool)}"
    draw.text((_GAP + 10, 86), progress, font=_font(22), fill=_GOLD)
    sign = f"—— {persona}画的卡"
    small = _font_for(sign, 19, cute=True)
    draw.text(
        (width - _GAP - 8 - draw.textlength(sign, font=small), 92), sign, font=small, fill=_FAINT
    )

    for index, card in enumerate(pool):
        col, row = index % _COLS, index // _COLS
        x = _GAP + col * (_CELL_W + _GAP)
        y = 150 + row * (_CELL_H + _GAP)
        color = RARITY_COLORS.get(card.rarity, RARITY_COLORS["R"])
        copies = have.get(card.card_id)
        if copies:
            draw.rounded_rectangle(
                [x, y, x + _CELL_W, y + _CELL_H],
                radius=14,
                fill=(255, 255, 255, 30),
                outline=color,
                width=2,
            )
            draw.text((x + 14, y + 10), f"{card.label} · {card.rarity}", font=_font(17), fill=color)
            draw.text(
                (x + 14, y + 38),
                _clean_text(card.subject_name)[:8],
                font=_font(24, bold=True),
                fill=_SILVER,
            )
            cell_title = _clean_text(card.title)[:10]
            draw.text(
                (x + 14, y + 74),
                cell_title,
                font=_font_for(cell_title, 18, cute=True),
                fill=_GOLD,
            )
            if copies > 1:
                mark = f"×{copies}"
                font = _font(18, bold=True)
                draw.text(
                    (x + _CELL_W - 14 - draw.textlength(mark, font=font), y + 100),
                    mark,
                    font=font,
                    fill=_SOFT,
                )
        else:
            draw.rounded_rectangle(
                [x, y, x + _CELL_W, y + _CELL_H],
                radius=14,
                fill=(255, 255, 255, 10),
                outline=(*_FAINT, 120),
                width=1,
            )
            draw.text((x + 14, y + 10), card.label, font=_font(17), fill=_FAINT)
            _centered_in(draw, x, y + 40, _CELL_W, "？？？", _font(30, bold=True), _FAINT)

    base = Image.alpha_composite(base, overlay)
    out = io.BytesIO()
    base.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()


def _centered_in(
    draw: ImageDraw.ImageDraw, x: int, y: int, width: int, text: str, font, fill
) -> None:
    draw.text((x + (width - draw.textlength(text, font=font)) / 2, y), text, font=font, fill=fill)
