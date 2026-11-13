"""用 Pillow 在内存里画卡面和图鉴，不落临时文件。

风格跟着「月白的小屋」走：深夜的蓝、月光的银，称号用一点暖金。
"""

from __future__ import annotations

import io
import os
import random
import re
from functools import lru_cache
from importlib.resources import files
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .members import STAT_KEYS
from .store import KIND_AWAKENED, KIND_MOMENT, KIND_MOOD, MAX_STARS, Card, Owned

CARD_W, CARD_H = 600, 900
#: 插画卡比普通卡高出的部分，全部给主图。
_ART_LIFT = 120

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
_INVISIBLE_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff\u00ad]")


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
    return not (current.size == missing.size and current.tobytes() == missing.tobytes())


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
        initial = _clean_text(name)[:1] or "?"
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


def _stars(stars: int) -> str:
    return "★" * stars + "☆" * (MAX_STARS - stars)


def _version(card: Card) -> str:
    stamp = card.minted_at[:10].replace("-", ".") if card.minted_at else ""
    if card.kind == KIND_MOMENT:
        return f"名场面 · {stamp}" if stamp else "名场面"
    if card.kind == KIND_AWAKENED:
        owner = card.cast_names[0] if card.cast_names else ""
        return f"觉醒 · 专属给{owner[:8]}" if owner else "觉醒"
    label = "心情卡" if card.kind == KIND_MOOD else "初版"
    return f"{label} · {stamp[:7]}" if stamp else label


def render_card(
    card: Card,
    *,
    avatar: bytes | None,
    persona_name: str,
    stars: int = 1,
    extra_notes: list[str] | None = None,
    art: bytes | None = None,
) -> bytes:
    """一张收藏卡（600 宽）。升过星的卡会多出她写给收藏者的话，卡也跟着变长。"""
    rarity_color = RARITY_COLORS.get(card.rarity, RARITY_COLORS["R"])
    measure = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    subject_name = _clean_text(card.subject_name)
    note_texts = [f"—— {_clean_text(persona_name)}：{_clean_text(card.note)}"]
    note_texts += [f"悄悄话：{_clean_text(note)}" for note in (extra_notes or [])]
    note_font = _font_for("".join(note_texts), 21, cute=True)
    note_lines = _wrap(measure, note_texts[0], note_font, CARD_W - 120)[:3]
    extra_lines = [
        line
        # 可爱字体里没有 ✦ 这类符号，前缀只用汉字。
        for text in note_texts[1:]
        for line in _wrap(measure, text, note_font, CARD_W - 120)[:2]
    ]
    # 插画卡的主图更高一些，卡面整体跟着变长。
    lift = _ART_LIFT if art else 0
    height = CARD_H + 30 * len(extra_lines) + lift

    base = _night((CARD_W, height), card.card_id).convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # 外框与内框；UR 多一圈
    draw.rounded_rectangle(
        [10, 10, CARD_W - 11, height - 11], radius=28, outline=rarity_color, width=4
    )
    draw.rounded_rectangle(
        [22, 22, CARD_W - 23, height - 23], radius=20, outline=(*_SILVER, 90), width=1
    )
    if card.kind == KIND_MOMENT:
        draw.rounded_rectangle(
            [16, 16, CARD_W - 17, height - 17], radius=24, outline=(*rarity_color, 140), width=2
        )

    # 顶栏：编号与星级、版本、稀有度
    small = _font(20)
    draw.text((44, 40), card.label, font=_font(22, bold=True), fill=_SILVER)
    if stars > 1:
        draw.text((44, 68), _stars(stars), font=_font(18, bold=True), fill=_GOLD)
    badge = _font(22, bold=True)
    badge_w = draw.textlength(card.rarity, font=badge) + 28
    draw.rounded_rectangle(
        [CARD_W - 44 - badge_w, 36, CARD_W - 44, 72], radius=18, fill=(*rarity_color, 230)
    )
    draw.text((CARD_W - 44 - badge_w + 14, 40), card.rarity, font=badge, fill=_NIGHT_TOP)
    _centered(draw, 44, _version(card), small, _FAINT)
    if card.supply and card.kind != KIND_AWAKENED:
        _centered(draw, 70, f"限量 {card.issued}/{card.supply}", _font(17), rarity_color)

    size = 160 if card.kind == KIND_MOMENT else 240
    cx, top = CARD_W // 2, 100 if card.kind == KIND_MOMENT else 96
    painting = _art_panel(art, size=(CARD_W - 80, size + 6 + lift)) if art else None
    glow = Image.new("RGBA", base.size, (0, 0, 0, 0))
    if painting is not None:
        # 插画卡：头像换成她画的那张图，四周一圈稀有度的光。
        ImageDraw.Draw(glow).rounded_rectangle(
            [32, top - 12, CARD_W - 32, top + painting.height + 8],
            radius=22,
            fill=(*rarity_color, 150),
        )
    else:
        ImageDraw.Draw(glow).ellipse(
            [cx - size // 2 - 18, top - 18, cx + size // 2 + 18, top + size + 18],
            fill=(*rarity_color, 120),
        )
    base = Image.alpha_composite(base, glow.filter(ImageFilter.GaussianBlur(16)))
    if painting is not None:
        draw.rounded_rectangle(
            [38, top - 6, CARD_W - 38, top + painting.height + 2],
            radius=18,
            outline=_SILVER,
            width=3,
        )
    else:
        draw.ellipse(
            [cx - size // 2 - 5, top - 5, cx + size // 2 + 5, top + size + 5],
            outline=_SILVER,
            width=3,
        )

    if card.kind == KIND_MOMENT:
        y = _moment_body(draw, card, rarity_color, dy=lift if painting is not None else 0)
    else:
        y = _member_body(draw, card, rarity_color, dy=lift if painting is not None else 0)

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

    # 她的批注，以及升星后只写给这位收藏者的话
    note_top = height - 50 - 30 * (len(note_lines) + len(extra_lines))
    for index, line in enumerate(note_lines):
        draw.text((60, note_top + 30 * index), line, font=note_font, fill=_SOFT)
    for index, line in enumerate(extra_lines):
        draw.text((60, note_top + 30 * (len(note_lines) + index)), line, font=note_font, fill=_GOLD)

    base = Image.alpha_composite(base, overlay)
    if painting is not None:
        base.alpha_composite(painting, (40, top - 4))
    else:
        base.alpha_composite(_avatar(avatar, subject_name, size), (cx - size // 2, top))
    out = io.BytesIO()
    base.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()


def _member_body(draw: ImageDraw.ImageDraw, card: Card, rarity_color, *, dy: int = 0) -> int:
    """人物卡：名字、称号、五项属性。返回下一块的起点。"""
    title_text = f"「{_clean_text(card.title)}」"
    _centered(draw, 360 + dy, _clean_text(card.subject_name)[:14], _font(36, bold=True), _SILVER)
    _centered(draw, 410 + dy, title_text, _font_for(title_text, 30, cute=True), _GOLD)
    y = 470 + dy
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
    return y


def _moment_body(draw: ImageDraw.ImageDraw, card: Card, rarity_color, *, dy: int = 0) -> int:
    """名场面：名字、那句原话和说话的人、出演名单。返回下一块的起点。"""
    title_text = f"「{_clean_text(card.title)}」"
    _centered(draw, 284 + dy, title_text, _font_for(title_text, 34, cute=True), _GOLD)
    quote_font = _font(25, bold=True)
    quote_lines = _wrap(draw, f"“{_clean_text(card.quote)}”", quote_font, CARD_W - 130)[:4]
    y = 344 + dy
    for line in quote_lines:
        _centered(draw, y, line, quote_font, _SILVER)
        y += 36
    who = f"—— {_clean_text(card.subject_name)}"
    font = _font(20)
    draw.text((CARD_W - 70 - draw.textlength(who, font=font), y + 4), who, font=font, fill=_SOFT)
    y += 44
    cast_font = _font(19)
    cast_text = "出演：" + "、".join(_clean_text(name) for name in card.cast_names)
    for line in _wrap(draw, cast_text, cast_font, CARD_W - 130)[:2]:
        draw.text((64, y), line, font=cast_font, fill=rarity_color)
        y += 28
    return max(y, 560 + dy)


_CELL_W, _CELL_H, _GAP, _COLS = 220, 132, 16, 4


def render_collection(
    *, owner_name: str, owned: list[Owned], pool: list[Card], persona_name: str
) -> bytes:
    """图鉴：本群卡池里的每一张，收到的亮着，没收到的是问号。"""
    have = {item.card.card_id: item for item in owned}
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
        item = have.get(card.card_id)
        if item:
            draw.rounded_rectangle(
                [x, y, x + _CELL_W, y + _CELL_H],
                radius=14,
                fill=(255, 255, 255, 30),
                outline=color,
                width=2,
            )
            draw.text((x + 14, y + 10), f"{card.label} · {card.rarity}", font=_font(17), fill=color)
            if item.stars > 1:
                mark = _stars(item.stars)
                font = _font(16, bold=True)
                draw.text(
                    (x + _CELL_W - 14 - draw.textlength(mark, font=font), y + 12),
                    mark,
                    font=font,
                    fill=_GOLD,
                )
            draw.text(
                (x + 14, y + 38),
                _clean_text(card.name)[:8],
                font=_font(24, bold=True),
                fill=_SILVER,
            )
            subtitle = card.subject_name if card.kind == KIND_MOMENT else card.title
            subtitle_text = _clean_text(subtitle)[:10]
            draw.text(
                (x + 14, y + 74),
                subtitle_text,
                font=_font_for(subtitle_text, 18, cute=True),
                fill=_GOLD,
            )
            if item.copies > 1:
                mark = f"×{item.copies}"
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
            if card.supply:
                # 没抽到的限量卡也让人知道它还剩几份：这是交换的筹码。
                hint = "已发完" if card.sold_out else f"限量 {card.issued}/{card.supply}"
                _centered_in(draw, x, y + 92, _CELL_W, hint, _font(16), color)

    base = Image.alpha_composite(base, overlay)
    out = io.BytesIO()
    base.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()


def _centered_in(
    draw: ImageDraw.ImageDraw, x: int, y: int, width: int, text: str, font, fill
) -> None:
    draw.text((x + (width - draw.textlength(text, font=font)) / 2, y), text, font=font, fill=fill)


def _art_panel(art: bytes, *, size: tuple[int, int]) -> Image.Image | None:
    """把插画裁成卡面主图的尺寸（居中裁切），圆角。读不了就当没有插画。"""
    width, height = size
    try:
        with Image.open(io.BytesIO(art)) as loaded:
            image = loaded.convert("RGB")
    except Exception:
        return None
    scale = max(width / image.width, height / image.height)
    resized = image.resize(
        (max(width, round(image.width * scale)), max(height, round(image.height * scale))),
        Image.Resampling.LANCZOS,
    )
    left = (resized.width - width) // 2
    top = (resized.height - height) // 2
    cropped = resized.crop((left, top, left + width, top + height))
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, width - 1, height - 1], radius=16, fill=255)
    panel = Image.new("RGBA", size, (0, 0, 0, 0))
    panel.paste(cropped, (0, 0), mask)
    return panel


# ---------------------------------------------------------------------------
# 玩法卡：/抽卡 帮助
# ---------------------------------------------------------------------------

HELP_W = 900
_HELP_PAD = 36
_HELP_CMD_W = 320
_HELP_LINE = 30
#: 玩法卡的面板比卡面的深一些，盖住背后的星星，字更清楚。
_HELP_PANEL = (14, 20, 42, 175)


def _wrap_words(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, width: int
) -> list[str]:
    """按空格折行（指令和参数不从中间断开）；单个词太长才逐字折。"""
    lines: list[str] = []
    line = ""
    for word in text.split(" "):
        candidate = f"{line} {word}" if line else word
        if draw.textlength(candidate, font=font) <= width:
            line = candidate
            continue
        if line:
            lines.append(line)
        if draw.textlength(word, font=font) <= width:
            line = word
        else:
            *head, line = _wrap(draw, word, font, width)
            lines.extend(head)
    if line:
        lines.append(line)
    return lines


def _help_layout(
    draw: ImageDraw.ImageDraw,
    sections: list[tuple[str, list[tuple[str, str]]]],
) -> list[tuple[str, list[tuple[list[str], list[str]]]]]:
    """每一条拆成 (指令的几行, 说明的几行)，量好宽度再画。"""
    cmd_font, desc_font = _font(22, bold=True), _font(21)
    desc_w = HELP_W - 2 * _HELP_PAD - 48 - _HELP_CMD_W
    laid = []
    for title, items in sections:
        rows = []
        for cmd, desc in items:
            rows.append(
                (
                    _wrap_words(draw, _clean_text(cmd), cmd_font, _HELP_CMD_W - 16),
                    _wrap(draw, _clean_text(desc), desc_font, desc_w),
                )
            )
        laid.append((title, rows))
    return laid


def render_help(
    sections: list[tuple[str, list[tuple[str, str]]]],
    *,
    persona_name: str,
    facts: list[str],
) -> bytes:
    """玩法卡：一张图讲清楚图鉴怎么玩。

    ``sections`` 是 (分组标题, [(指令, 说明), ...])；``facts`` 是本群眼下的规则
    （每轮几抽、几点刷新、保底），画在顶上。
    """
    measure = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    laid = _help_layout(measure, sections)
    fact_font = _font(20)
    fact_lines = _wrap(
        measure, "  ·  ".join(_clean_text(f) for f in facts), fact_font, HELP_W - 120
    )

    header_h = 150 + _HELP_LINE * len(fact_lines)
    body_h = 0
    for _title, rows in laid:
        body_h += 62 + sum(max(len(c), len(d)) * _HELP_LINE + 12 for c, d in rows) + 18
    rarity_h = 120
    height = header_h + body_h + rarity_h + 70

    base = _night((HELP_W, height), "gacha-help").convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    draw.rounded_rectangle([10, 10, HELP_W - 11, height - 11], radius=28, outline=_GOLD, width=3)
    draw.rounded_rectangle(
        [22, 22, HELP_W - 23, height - 23], radius=20, outline=(*_SILVER, 90), width=1
    )

    # 顶栏：标题、她的签名、本群眼下的规则
    title = "群友图鉴 · 怎么玩"
    _centered(draw, 46, title, _font_for(title, 44, cute=True), _SILVER, width=HELP_W)
    sign = f"—— 卡都是{_clean_text(persona_name)[:10]}画的"
    _centered(draw, 104, sign, _font_for(sign, 20, cute=True), _FAINT, width=HELP_W)
    for index, line in enumerate(fact_lines):
        _centered(draw, 140 + _HELP_LINE * index, line, fact_font, _GOLD, width=HELP_W)

    cmd_font, desc_font = _font(22, bold=True), _font(21)
    y = header_h
    for title, rows in laid:
        block_h = 62 + sum(max(len(c), len(d)) * _HELP_LINE + 12 for c, d in rows)
        draw.rounded_rectangle(
            [_HELP_PAD, y, HELP_W - _HELP_PAD, y + block_h], radius=18, fill=_HELP_PANEL
        )
        section_title = _clean_text(title)
        draw.text(
            (_HELP_PAD + 24, y + 16),
            section_title,
            font=_font_for(section_title, 26, cute=True),
            fill=_GOLD,
        )
        row_y = y + 60
        for cmd_lines, desc_lines in rows:
            for index, line in enumerate(cmd_lines):
                draw.text(
                    (_HELP_PAD + 24, row_y + _HELP_LINE * index), line, font=cmd_font, fill=_SILVER
                )
            for index, line in enumerate(desc_lines):
                draw.text(
                    (_HELP_PAD + 24 + _HELP_CMD_W, row_y + _HELP_LINE * index),
                    line,
                    font=desc_font,
                    fill=_SOFT,
                )
            row_y += max(len(cmd_lines), len(desc_lines)) * _HELP_LINE + 12
        y += block_h + 18

    # 四种卡
    chips = [
        ("R", "常驻卡", "每位群友一张"),
        ("SR", "心情卡", "她心情好时画"),
        ("SSR", "觉醒卡", "三星卡觉醒"),
        ("UR", "名场面", "限量，引用原话"),
    ]
    chip_w = (HELP_W - 2 * _HELP_PAD - 3 * 14) // 4
    for index, (rarity, name, hint) in enumerate(chips):
        x = _HELP_PAD + index * (chip_w + 14)
        color = RARITY_COLORS[rarity]
        draw.rounded_rectangle(
            [x, y + 4, x + chip_w, y + rarity_h - 10],
            radius=16,
            fill=(*color, 40),
            outline=color,
            width=2,
        )
        _centered_in(draw, x, y + 14, chip_w, rarity, _font(26, bold=True), color)
        _centered_in(draw, x, y + 50, chip_w, name, _font(20, bold=True), _SILVER)
        _centered_in(draw, x, y + 78, chip_w, hint, _font(16), _SOFT)

    footer = "不想被画进图鉴？发 /我的卡 退出，随时可以回来"
    _centered(draw, height - 52, footer, _font(18), _FAINT, width=HELP_W)

    base = Image.alpha_composite(base, overlay)
    out = io.BytesIO()
    base.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()
