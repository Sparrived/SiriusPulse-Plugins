"""把日报 / 周报数据排成一张横版手帐（HTML），再交给 chromium 截图。

版式是严格的网格：左边一张「封面」（标题、天气、插画、她的一句话、四个数字）贯穿整页高，
右边三列等宽、同一行的卡片等高，所有边都对齐。可爱靠配色、图标和字体，不靠歪斜和胶带。
字体内嵌（站酷快乐体做标题与大数字，正文用系统黑体），不外链任何资源，离线、容器里都能出一样的图。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
from datetime import datetime
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from typing import Any, Sequence

from . import art
from .art import (
    AWARD_ICONS,
    BLUE,
    CATEGORICAL,
    GOLD,
    GREEN,
    INK,
    ORANGE,
    PINK,
    SUB,
    TINTS,
    VIOLET,
    WEEKDAYS,
    esc,
    icon,
    short,
    weather_icon,
)
from .metrics import WEATHERS, fmt_clock
from .source import CN_TZ
from .writer import MENTION_RE

logger = logging.getLogger(__name__)

#: 版面尺寸（px）：左封面 + 右边三列等宽网格
COVER = 520
CELL = 496
_GAP = 24
_PAD = 40
_CARD_INSET = 52  # 卡片左右内边距 + 边框
PAGE_WIDTH = _PAD * 2 + COVER + _GAP + CELL * 3 + _GAP * 2
_MAX_HEIGHT = 6000

WEATHER_LINES = {
    "sunny": "开开心心的一天",
    "rainbow": "笑声多到出彩虹",
    "cloudy": "平平稳稳，偶有小波澜",
    "breeze": "安安静静，微风拂过",
    "drizzle": "有点小情绪，抱抱大家",
    "storm": "吵吵闹闹，电闪雷鸣",
    "moon": "一片安静，大家都在忙",
}


@lru_cache(maxsize=1)
def _cute_font_face() -> str:
    """把站酷快乐体内嵌成 @font-face；找不到字体就返回空串，退回系统字体。"""
    try:
        path = Path(
            str(
                files("sirius_pulse").joinpath(
                    "tools/builtin/_internal/assets/ZCOOLKuaiLe-Regular.ttf"
                )
            )
        )
        data = path.read_bytes()
    except Exception:  # noqa: BLE001 - 字体是锦上添花
        return ""
    encoded = base64.b64encode(data).decode("ascii")
    return (
        "@font-face{font-family:'Cute';src:url(data:font/ttf;base64,"
        + encoded
        + ") format('truetype');font-display:block;}"
    )


_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{width:%(width)dpx;background:#FFF9F1;color:%(ink)s;
  font-family:'Microsoft YaHei','PingFang SC','Noto Sans CJK SC','Noto Sans SC','WenQuanYi Zen Hei',sans-serif;
  -webkit-font-smoothing:antialiased}
.cute{font-family:'Cute','Microsoft YaHei','Noto Sans CJK SC',sans-serif}
.page{padding:%(pad)dpx %(pad)dpx 22px;
  background-image:radial-gradient(#F1E6E0 1.2px,transparent 1.2px);background-size:24px 24px}
.spread{display:grid;grid-template-columns:%(cover)dpx repeat(3,%(cell)dpx);gap:%(gap)dpx}
.span2{grid-column:span 2}.span3{grid-column:span 3}
.card{display:flex;flex-direction:column;background:#FFFFFF;border-radius:22px;padding:20px 24px 22px;
  border:2px solid #F3E8E4;box-shadow:0 4px 14px rgba(150,110,120,.07);min-width:0}
.card .body{flex:1;display:flex;flex-direction:column;min-height:0}
.card .body.mid{justify-content:center;align-items:center}
.title{display:flex;align-items:center;gap:10px;margin-bottom:16px;padding-bottom:12px;
  border-bottom:2px dashed #F3E8E4}
.title .ti{width:38px;height:38px;border-radius:12px;display:flex;align-items:center;justify-content:center;
  flex:none}
.title h2{font-family:'Cute','Microsoft YaHei',sans-serif;font-weight:400;font-size:25px;letter-spacing:1px}
.title .hint{margin-left:auto;font-size:13px;color:%(sub)s}
.cover-box{background:#FFFFFF;border-radius:18px;padding:14px 16px 12px}
.cover-box .minor:first-child{margin-top:0}
.minor{display:flex;align-items:center;gap:8px;margin:18px 0 10px;font-family:'Cute',sans-serif;font-size:20px}
.cover{grid-column:1;display:flex;flex-direction:column;gap:18px;border-radius:26px;padding:28px 26px;
  background:linear-gradient(180deg,#FFEFF3 0%%,#FFF6F0 55%%,#FFF9F1 100%%);border:2px solid #F6DDE3}
.kicker{display:inline-flex;align-items:center;gap:7px;font-size:15px;color:%(sub)s;letter-spacing:2px;
  background:#FFFFFF;border-radius:999px;padding:5px 14px;max-width:100%%;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cover h1{font-family:'Cute','Microsoft YaHei',sans-serif;font-weight:400;font-size:54px;line-height:1.1;
  margin-top:14px}
.cover .date{font-size:17px;color:%(sub)s;margin-top:6px}
.weather{display:flex;align-items:center;gap:12px;background:#FFFFFF;border-radius:20px;padding:10px 18px 10px 10px}
.weather .w-name,.weather .w-temp{font-family:'Cute',sans-serif;font-size:28px}
.weather .w-temp{color:%(pink)s}
.weather .w-line{font-size:13px;color:%(sub)s;margin-top:1px}
.polaroid{background:#FFFFFF;padding:12px 12px 42px;border-radius:8px;position:relative;
  box-shadow:0 8px 22px rgba(120,90,100,.12)}
.polaroid img{width:100%%;display:block;border-radius:4px;aspect-ratio:1/1;object-fit:cover}
.polaroid .cap{position:absolute;left:0;right:0;bottom:11px;text-align:center;font-family:'Cute',sans-serif;
  font-size:18px;color:%(sub)s}
.bigweather{background:#FFFFFF;border-radius:20px;aspect-ratio:1/1;display:flex;align-items:center;
  justify-content:center}
.quote-line{background:#FFFFFF;border-radius:18px;padding:14px 18px;font-size:17px;line-height:1.6;
  border-left:6px solid #F7C3CF}
.quote-line .who{font-size:12px;color:%(pink)s;margin-bottom:2px}
.stats{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:auto}
.stat{border-radius:18px;padding:12px 14px;position:relative;background:#FFFFFF}
.stat .ico{position:absolute;right:10px;top:10px}
.stat .num{font-family:'Cute',sans-serif;font-size:36px;line-height:1.1;margin-top:2px}
.stat .num small{font-size:16px;margin-left:3px}
.stat .lab{font-size:13px;color:%(sub)s}
.stat .foot{font-size:12px;margin-top:2px;color:%(sub)s}
.up{color:%(green)s}.down{color:%(pink)s}
.chapters{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:14px}
.chapter{display:flex;gap:12px;background:#FFFBF8;border:2px solid #F6ECE8;border-radius:16px;padding:12px 14px}
.chapter .pin{flex:none;width:28px;height:28px;border-radius:50%%;color:#FFFFFF;font-weight:700;font-size:14px;
  display:flex;align-items:center;justify-content:center}
.chapter .t{font-family:'Cute',sans-serif;font-size:20px;line-height:1.25}
.chapter .when{font-size:12px;color:%(sub)s;margin:2px 0 4px}
.chapter .s{font-size:14px;line-height:1.55}
.faces{display:flex;align-items:center;margin-top:8px}
.faces img{width:26px;height:26px;border-radius:50%%;border:2px solid #FFFFFF;margin-right:-7px;object-fit:cover;
  background:#F4ECEE}
.chips{display:flex;flex-wrap:wrap;gap:5px;margin-left:14px}
.chip{font-size:12px;padding:1px 9px;border-radius:999px;background:#F7F0F3;color:%(sub)s}
.awards{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.award{display:flex;align-items:center;gap:10px;border-radius:16px;padding:10px}
.award .av{position:relative;flex:none;width:54px;height:54px}
.award .av img{width:54px;height:54px;border-radius:50%%;border:3px solid #FFFFFF;object-fit:cover;
  background:#F4ECEE}
.award .av .badge{position:absolute;right:-8px;bottom:-5px;background:#FFFFFF;border-radius:50%%;padding:2px;
  line-height:0}
.award .info{min-width:0}
.award .ttl{font-family:'Cute',sans-serif;font-size:18px;line-height:1.2;white-space:nowrap}
.award .nm{font-size:13px;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.award .dt{font-size:12px;color:%(sub)s}
.pairs{display:flex;flex-direction:column;gap:10px}
.pair{display:flex;align-items:center;gap:8px;background:#FFFBF8;border:2px solid #F6ECE8;border-radius:16px;
  padding:8px 12px}
.pair img{width:42px;height:42px;border-radius:50%%;object-fit:cover;border:3px solid #FFFFFF;
  box-shadow:0 0 0 2px #F7C3CF}
.pair .nm{font-size:13px;font-weight:700;width:88px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
.pair .thread{flex:1;display:flex;flex-direction:column;align-items:center;min-width:56px}
.pair .thread .line{width:100%%;border-top:3px dotted #F09AAE;position:relative;top:13px}
.pair .thread .sc{font-size:12px;color:%(sub)s;margin-top:2px}
.quotes{display:flex;flex-direction:column;gap:10px}
.quote{position:relative;background:#FFFBF8;border:2px solid #F6ECE8;border-radius:16px;padding:9px 14px 8px 44px}
.quote .q{position:absolute;left:11px;top:10px;line-height:0}
.quote .txt{font-size:15px;line-height:1.45}
.quote .by{font-size:12px;color:%(sub)s;margin-top:2px}
.rank{display:flex;flex-direction:column;gap:9px}
.rank .r{display:flex;align-items:center;gap:10px;font-size:14px}
.rank .r .no{width:18px;text-align:center;font-family:'Cute',sans-serif;font-size:17px;color:%(sub)s}
.rank .r img{width:28px;height:28px;border-radius:50%%;object-fit:cover;background:#F4ECEE}
.rank .r .n{width:110px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
.rank .r .bar{flex:1;height:12px;border-radius:0 6px 6px 0;background:#F7F0F3;position:relative}
.rank .r .bar i{position:absolute;left:0;top:0;bottom:0;border-radius:0 6px 6px 0}
.rank .r .v{width:44px;text-align:right;color:%(sub)s;font-variant-numeric:tabular-nums}
.tag{display:inline-flex;align-items:center;gap:4px;vertical-align:middle;margin:0 2px;padding:1px 9px 1px 2px;
  border-radius:999px;background:#FDE7EC;color:#B9405D;font-size:.88em;font-weight:700;line-height:1.5}
.tag img{width:1.45em;height:1.45em;border-radius:50%%;object-fit:cover;background:#FFFFFF}
.diary .lines{flex:1;min-height:108px;font-size:18px;line-height:36px;white-space:pre-wrap;
  background-image:linear-gradient(transparent 34px,#F3E1E6 34px,#F3E1E6 36px);background-size:100%% 36px}
.diary-wrap{display:flex;gap:22px;flex:1}
.diary-wrap .main{flex:1;display:flex;flex-direction:column;min-width:0}
.her{width:250px;flex:none;display:flex;flex-direction:column;gap:10px;background:#FBF7FD;border-radius:18px;
  padding:14px}
.her .ht{display:flex;align-items:center;gap:8px;font-family:'Cute',sans-serif;font-size:19px;color:%(ink)s}
.her .ht img{width:34px;height:34px;border-radius:50%%;object-fit:cover;border:2px solid #FFFFFF;
  box-shadow:0 0 0 2px #D8CBF2}
.her .minis{margin-top:0;padding-top:0}
.her .fav{font-size:13px;color:%(sub)s;line-height:1.9}
.diary .sign{display:flex;align-items:center;justify-content:flex-end;gap:8px;margin-top:auto;padding-top:12px;
  font-family:'Cute',sans-serif;font-size:18px;color:%(sub)s}
.diary .sign img{width:36px;height:36px;border-radius:50%%;object-fit:cover;border:2px solid #FFFFFF;
  box-shadow:0 0 0 2px #F7C3CF}
.forecast{display:grid;grid-template-columns:repeat(7,1fr);gap:10px;width:100%%}
.day{position:relative;background:#FFFBF8;border:2px solid #F6ECE8;border-radius:16px;padding:10px 4px;
  text-align:center}
.day.best{background:#FFF4D6;border-color:#EEDDA6}
.day .wd{font-family:'Cute',sans-serif;font-size:20px}
.day .dd{font-size:11px;color:%(sub)s}
.day .wn{font-size:12px}
.day .tp{font-family:'Cute',sans-serif;font-size:22px;color:%(pink)s}
.day .ct{font-size:11px;color:%(sub)s}
.day .crown{position:absolute;top:-14px;left:50%%;margin-left:-12px;line-height:0}
.card.sky{background:linear-gradient(180deg,#2B2F5C 0%%,#3E3A75 60%%,#5A4B8C 100%%);border-color:#3E3A75;
  color:#F3EEFF}
.card.sky .title{border-bottom-color:rgba(255,255,255,.15)}
.card.sky .title h2{color:#FFF3C4}.card.sky .title .hint{color:#C9C1EA}
.people-row{display:flex;flex-wrap:wrap;gap:8px 6px}
.person{display:flex;flex-direction:column;align-items:center;width:66px;font-size:12px;text-align:center}
.person img{width:44px;height:44px;border-radius:50%%;object-fit:cover;border:3px solid #FFFFFF;
  box-shadow:0 0 0 2px #BCE3CF;margin-bottom:4px}
.person span{max-width:66px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
.rising{display:flex;flex-wrap:wrap;gap:8px}
.rising .rw{background:#FFF4E6;border-radius:999px;padding:4px 12px;font-size:13px}
.rising .rw b{font-family:'Cute',sans-serif;font-weight:400;font-size:17px;margin-right:5px}
.rising .rw small{color:%(sub)s}
.minis{display:grid;gap:10px;margin-top:auto;padding-top:16px}
.mini{border-radius:14px;padding:9px 12px 8px;min-width:0}
.mini b{display:block;font-family:'Cute',sans-serif;font-weight:400;font-size:23px;line-height:1.15;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.mini b.txt{font-family:'Microsoft YaHei','Noto Sans CJK SC',sans-serif;font-weight:700;font-size:17px;
  line-height:1.6}
.mini span{display:block;font-size:12px;color:%(sub)s;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.card.sky .mini{background:rgba(255,255,255,.10) !important}
.card.sky .mini b{color:#FFF3C4 !important}.card.sky .mini span{color:#C9C1EA}
.parts{display:flex;flex-direction:column;gap:7px;width:100%%;margin-top:6px}
.part{display:flex;align-items:center;gap:10px;font-size:13px}
.part .pl{width:34px;color:%(sub)s}
.part .pb{flex:1;height:12px;border-radius:0 6px 6px 0;background:#F7F0F3;position:relative}
.part .pb i{position:absolute;left:0;top:0;bottom:0;border-radius:0 6px 6px 0}
.part .pv{width:76px;text-align:right;color:%(sub)s;font-variant-numeric:tabular-nums}
.also{display:flex;flex-wrap:wrap;align-items:center;gap:6px;margin-top:12px;font-size:13px;color:%(sub)s}
.also .chip{font-size:13px}
.empty{color:%(sub)s;font-size:14px;padding:14px 4px;text-align:center}
.footer{display:flex;justify-content:space-between;margin-top:16px;font-size:13px;color:#B9AEBF}
"""


def _css() -> str:
    return _cute_font_face() + _CSS % {
        "width": PAGE_WIDTH,
        "pad": _PAD,
        "gap": _GAP,
        "cover": COVER,
        "cell": CELL,
        "ink": INK,
        "sub": SUB,
        "pink": PINK,
        "green": GREEN,
    }


def _inner(cells: int = 1) -> int:
    """跨 ``cells`` 列的卡片里，图表能用的宽度。"""
    return CELL * cells + _GAP * (cells - 1) - _CARD_INSET


# ── 小部件 ─────────────────────────────────────────────────────────────


def _avatar(view: dict[str, Any], uid: str) -> str:
    avatars = view.get("avatars") or {}
    if uid in avatars:
        return avatars[uid]
    return art.initial_avatar(_name(view, uid), uid)


def _name(view: dict[str, Any], uid: str) -> str:
    return str((view.get("names") or {}).get(uid) or f"qq_{uid}")


def _tag(view: dict[str, Any], uid: str) -> str:
    return f'<span class="tag"><img src="{_avatar(view, uid)}"/>{esc(_name(view, uid))}</span>'


def _name_variants(names: dict[str, str]) -> list[tuple[str, str]]:
    """群友名字的几种写法 → uid：全名、去掉首尾标点的名字、中英混写名字里的中文部分。

    她写日记时常把「临雀。」写成「临雀」、把「汝且候Attends un peu」写成「汝且候」。
    同一个写法对上不止一个人就不用它；名字越长越先匹配，避免「小明」吃掉「小明同学」。
    """
    owners: dict[str, set[str]] = {}
    for uid, name in names.items():
        full = str(name or "").strip()
        forms = {full, re.sub(r"^[\W_]+|[\W_]+$", "", full)}
        if re.search(r"[A-Za-z]", full):
            forms.update(run for run in re.findall(r"[一-鿿]{2,}", full))
        for form in forms:
            if len(form) >= 2:
                owners.setdefault(form, set()).add(uid)
    unique = [(form, next(iter(uids))) for form, uids in owners.items() if len(uids) == 1]
    return sorted(unique, key=lambda item: -len(item[0]))


def _rich(view: dict[str, Any], text: str) -> str:
    """她写的字：``<@QQ号>`` 和直接写出来的群友名字都画成带头像的小名牌，其余转义。"""
    by_name = _name_variants(view.get("names") or {})
    out: list[str] = []
    for index, part in enumerate(MENTION_RE.split(text or "")):
        if index % 2:  # 标记里的 QQ 号
            out.append(_tag(view, part))
            continue
        cursor = 0
        while cursor < len(part):
            hit = next(
                ((name, uid) for name, uid in by_name if part.startswith(name, cursor)), None
            )
            if hit is None:
                out.append(esc(part[cursor]))
                cursor += 1
                continue
            out.append(_tag(view, hit[1]))
            cursor += len(hit[0])
    return "".join(out)


def _title(text: str, icon_name: str, color: str, hint: str = "") -> str:
    hint_html = f'<span class="hint">{esc(hint)}</span>' if hint else ""
    return (
        f'<div class="title"><span class="ti" style="background:{TINTS[color]}">'
        f"{icon(icon_name, 26, color)}</span><h2>{esc(text)}</h2>{hint_html}</div>"
    )


def _minor(text: str, icon_name: str, color: str) -> str:
    return f'<div class="minor">{icon(icon_name, 24, color)}{esc(text)}</div>'


def _card(title: str, body: str, *, span: int = 1, mid: bool = False, extra: str = "") -> str:
    cls = "card" + (f" span{span}" if span > 1 else "") + (f" {extra}" if extra else "")
    body_cls = "body mid" if mid else "body"
    return f'<div class="{cls}">{title}<div class="{body_cls}">{body}</div></div>'


def _stat(label: str, number: str, unit: str, foot: str, color: str, icon_name: str) -> str:
    return (
        f'<div class="stat"><span class="ico">{icon(icon_name, 30, color)}</span>'
        f'<div class="lab">{esc(label)}</div>'
        f'<div class="num" style="color:{color}">{esc(number)}<small>{esc(unit)}</small></div>'
        f'<div class="foot">{foot}</div></div>'
    )


def _delta_html(delta: float | None, word: str) -> str:
    if delta is None:
        return f"{esc(word)}没有记录"
    pct = round(delta * 100)
    if pct == 0:
        return f"和{esc(word)}差不多"
    arrow, cls = ("↑", "up") if pct > 0 else ("↓", "down")
    return f'比{esc(word)} <b class="{cls}">{arrow}{abs(pct)}%</b>'


def _cover(
    view: dict[str, Any],
    report: dict[str, Any],
    *,
    kicker: str,
    title: str,
    date_line: str,
    caption: str,
    stats: Sequence[str],
    rows: int,
    extra: str = "",
) -> str:
    """左侧封面：贯穿上面几行，数字贴纸压在最下面。"""
    kind = report["weather"]
    headline = str((view.get("writing") or {}).get("headline") or "").strip()
    persona = view.get("persona_name") or "她"
    weather = (
        f'<div class="weather">{weather_icon(kind, 62)}<div>'
        f'<div><span class="w-name">{esc(WEATHERS.get(kind, "多云"))}</span>'
        f'<span class="w-temp"> · 群温 {report["temperature"]}°C</span></div>'
        f'<div class="w-line">{esc(WEATHER_LINES.get(kind, ""))}</div></div></div>'
    )
    if view.get("art"):
        picture = (
            f'<div class="polaroid"><img src="{view["art"]}"/>'
            f'<div class="cap">{esc(view.get("art_caption") or caption)}</div></div>'
        )
    else:
        picture = f'<div class="bigweather">{weather_icon(kind, 260)}</div>'
    line = (
        f'<div class="quote-line"><div class="who">{esc(persona)} 说</div>{_rich(view, headline)}</div>'
        if headline
        else ""
    )
    return (
        f'<div class="cover" style="grid-row:1 / span {rows}">'
        f'<div><span class="kicker">{icon("star", 16, PINK)}{esc(kicker)}</span>'
        f'<h1>{esc(title)}</h1><div class="date">{esc(date_line)}</div></div>'
        f'{weather}{picture}{line}{extra}<div class="stats">{"".join(stats)}</div></div>'
    )


def _chapters_html(view: dict[str, Any], report: dict[str, Any], *, weekly: bool) -> str:
    writing = (view.get("writing") or {}).get("chapters") or {}
    cards: list[str] = []
    for number, chapter in enumerate(report["sessions"], 1):
        color = CATEGORICAL[(number - 1) % len(CATEGORICAL)]
        written = writing.get(chapter["index"]) or writing.get(str(chapter["index"])) or {}
        fallback = "聊到" + "、".join(chapter["keywords"][:2]) if chapter["keywords"] else "一段热闹的闲聊"
        title = str(written.get("title") or "").strip() or fallback
        summary = str(written.get("summary") or "").strip()
        start = datetime.fromtimestamp(chapter["start"], CN_TZ)
        when = f"{WEEKDAYS[start.weekday()]} " if weekly else ""
        when += (
            f'{fmt_clock(chapter["start"])} – {fmt_clock(chapter["end"])} · {chapter["count"]} 条'
        )
        faces = "".join(f'<img src="{_avatar(view, uid)}"/>' for uid in chapter["people"][:5])
        chips = "".join(
            f'<span class="chip">{esc(word)}</span>' for word in chapter["keywords"][:3]
        )
        cards.append(
            f'<div class="chapter"><div class="pin" style="background:{color}">{number}</div><div>'
            f'<div class="t">{esc(title)}</div><div class="when">{esc(when)}</div>'
            + (f'<div class="s">{_rich(view, summary)}</div>' if summary else "")
            + f'<div class="faces">{faces}<div class="chips">{chips}</div></div></div></div>'
        )
    if not cards:
        return '<div class="empty">没有成段的聊天，大家都是路过打个招呼～</div>'
    return f'<div class="chapters">{"".join(cards)}</div>'


def _awards_html(view: dict[str, Any], report: dict[str, Any]) -> str:
    cards = []
    for award in report["awards"]:
        icon_name, color = AWARD_ICONS.get(award["key"], ("star", PINK))
        cards.append(
            f'<div class="award" style="background:{TINTS[color]}">'
            f'<div class="av"><img src="{_avatar(view, award["uid"])}"/>'
            f'<span class="badge">{icon(icon_name, 22, color)}</span></div><div class="info">'
            f'<div class="ttl" style="color:{color}">{esc(award["title"])}</div>'
            f'<div class="nm">{esc(award["name"])}</div>'
            f'<div class="dt">{esc(award["detail"])}</div></div></div>'
        )
    if not cards:
        return '<div class="empty">还没人拿到称号，多聊几句吧～</div>'
    return f'<div class="awards">{"".join(cards)}</div>'


def _pairs_html(view: dict[str, Any], report: dict[str, Any]) -> str:
    rows = []
    for pair in report["pairs"]:
        rows.append(
            f'<div class="pair"><img src="{_avatar(view, pair["a"])}"/>'
            f'<span class="nm">{esc(_name(view, pair["a"]))}</span>'
            f'<div class="thread"><div class="line"></div>{icon("heart", 26, PINK)}'
            f'<div class="sc">来回 {pair["score"]} 次</div></div>'
            f'<span class="nm" style="text-align:right">{esc(_name(view, pair["b"]))}</span>'
            f'<img src="{_avatar(view, pair["b"])}"/></div>'
        )
    if not rows:
        return '<div class="empty">还没有特别聊得来的两个人</div>'
    return f'<div class="pairs">{"".join(rows)}</div>'


def _quotes_html(
    view: dict[str, Any], report: dict[str, Any], *, weekly: bool, limit: int = 4
) -> str:
    rows = []
    for moment in report["laughs"]:
        when = datetime.fromtimestamp(moment["ts"], CN_TZ)
        stamp = f"{WEEKDAYS[when.weekday()]} " if weekly else ""
        who = moment["name"] or _name(view, moment["uid"])
        rows.append(
            f'<div class="quote"><span class="q">{icon("laugh", 24, ORANGE)}</span>'
            f'<div class="txt">「{esc(moment["text"])}」</div>'
            f'<div class="by">—— {esc(who)} · {stamp}{fmt_clock(moment["ts"])}'
            f' · 引来 {moment["laughers"]} 人笑出声</div></div>'
        )
    for echo in report["echoes"]:
        rows.append(
            f'<div class="quote"><span class="q">{icon("repeat", 24, VIOLET)}</span>'
            f'<div class="txt">「{esc(short(echo["text"], 20))}」 × {echo["times"]}</div>'
            f'<div class="by">{echo["people"]} 个人一起复读 · {esc(echo["starter"])} 起的头</div></div>'
        )
    if not rows:
        return '<div class="empty">没有抓到笑点和复读，是认真聊天的一段时间</div>'
    return f'<div class="quotes">{"".join(rows[:limit])}</div>'


def _rank_html(view: dict[str, Any], report: dict[str, Any], limit: int = 7) -> str:
    people = report["people"]
    ranking = report["ranking"][:limit]
    if not ranking:
        return '<div class="empty">没人说话</div>'
    top = people[ranking[0]].count or 1
    rows = []
    for number, uid in enumerate(ranking, 1):
        person = people[uid]
        width = max(3, round(person.count / top * 100))
        rows.append(
            f'<div class="r"><span class="no">{number}</span><img src="{_avatar(view, uid)}"/>'
            f'<span class="n">{esc(_name(view, uid))}</span>'
            f'<span class="bar"><i style="width:{width}%;background:{PINK}"></i></span>'
            f'<span class="v">{person.count}</span></div>'
        )
    return f'<div class="rank">{"".join(rows)}</div>'


def _her_panel(view: dict[str, Any], report: dict[str, Any], *, period: str) -> str:
    """日记旁边的「她的今天」：她自己说了几句、接了几个人的话、最常接谁的话。"""
    her = report.get("her") or {}
    persona = view.get("persona_name") or "她"
    face = view.get("persona_avatar") or art.initial_avatar(persona, "persona")
    peak = her.get("peak_hour")
    fav = her.get("favorite_uid")
    fav_html = (
        f'<div class="fav">最常接 {_tag(view, fav)} 的话 · {her.get("favorite_count", 0)} 次</div>'
        if fav
        else '<div class="fav">今天没怎么插话，安静地看着大家</div>'
    )
    return (
        f'<div class="her"><div class="ht"><img src="{face}"/>{esc(persona)}的{esc(period)}</div>'
        + _minis([(f"{her.get('count', 0)}句", "她说的话", VIOLET)])
        + _minis(
            [
                (f"{her.get('answered_people', 0)}人", "接过谁的话", PINK),
                (f"{peak}点" if peak is not None else "—", "她最活跃", BLUE),
            ]
        )
        + fav_html
        + "</div>"
    )


def _diary_card(
    view: dict[str, Any],
    report: dict[str, Any],
    *,
    title: str,
    sign_time: str,
    period: str,
    span: int = 1,
) -> str:
    diary = str((view.get("writing") or {}).get("diary") or "").strip()
    if not diary:
        return ""
    persona = view.get("persona_name") or "她"
    face = view.get("persona_avatar") or art.initial_avatar(persona, "persona")
    text = (
        f'<div class="lines">{_rich(view, diary)}</div>'
        f'<div class="sign"><img src="{face}"/>—— {esc(persona)} · {esc(sign_time)}</div>'
    )
    if span >= 2:
        panel = _her_panel(view, report, period=period)
        body = f'<div class="diary-wrap"><div class="main">{text}</div>{panel}</div>'
    else:
        # 一格宽时把「她的这周」压成底部一条小统计，免得把整行撑高
        her = report.get("her") or {}
        fav = her.get("favorite_uid")
        body = text + _minis(
            [
                (f"{her.get('count', 0)}句", "她说的话", VIOLET),
                (f"{her.get('answered_people', 0)}人", "接过谁的话", PINK),
                (_name(view, fav) if fav else "—", "最常接谁的话", BLUE),
            ]
        )
    return _card(_title(title, "pencil", VIOLET), body, span=span, extra="diary")


def _minis(items: Sequence[tuple[str, str, str]]) -> str:
    """卡片底部那一行小统计：(数值, 说明, 颜色)。同一行卡片的这一条都压在底边上，对得齐。"""
    items = [item for item in items if item[0]]
    if not items:
        return ""
    cells = "".join(
        f'<div class="mini" style="background:{TINTS[color]}">'
        f'<b class="{"" if value[:1].isdigit() else "txt"}" style="color:{color}">{esc(value)}</b>'
        f"<span>{esc(label)}</span></div>"
        for value, label, color in items
    )
    return (
        f'<div class="minis" style="grid-template-columns:repeat({len(items)},1fr)">{cells}</div>'
    )


def _duration(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes}分钟"
    return f"{minutes // 60}时{minutes % 60:02d}分"


def _pct(value: float) -> str:
    return f"{round(value * 100)}%"


def _dayparts_html(report: dict[str, Any]) -> str:
    parts = report.get("dayparts") or []
    top = max((part["count"] for part in parts), default=0) or 1
    colors = (VIOLET, ORANGE, PINK, BLUE)
    rows = "".join(
        f'<div class="part"><span class="pl">{esc(part["label"])}</span>'
        f'<span class="pb"><i style="width:{max(2, round(part["count"] / top * 100))}%;'
        f'background:{colors[index % 4]}"></i></span>'
        f'<span class="pv">{part["count"]} · {_pct(part["share"])}</span></div>'
        for index, part in enumerate(parts)
    )
    return f'<div class="parts">{rows}</div>'


def _rhythm_minis(report: dict[str, Any]) -> str:
    rhythm = report.get("rhythm") or {}
    silence = rhythm.get("silence") or {}
    reply = (report.get("social") or {}).get("reply_median")
    return _minis(
        [
            (f"{rhythm.get('sessions', 0)}段", "成段的聊天", ORANGE),
            (_duration(rhythm.get("longest_minutes", 0)), "最长的一段", PINK),
            (
                _duration(silence.get("minutes", 0)) if silence.get("minutes") else "",
                f"最长的安静 · {fmt_clock(silence.get('start', 0))} 起",
                VIOLET,
            ),
            (f"{reply}秒" if reply is not None else "", "接话的中位数", BLUE),
        ]
    )


def _laugh_minis(view: dict[str, Any], report: dict[str, Any]) -> str:
    laughs = report.get("laugh_stats") or {}
    peak = laughs.get("peak_hour")
    return _minis(
        [
            (f"{laughs.get('total', 0)}次", f"笑声 · 占 {_pct(laughs.get('share', 0))}", ORANGE),
            (
                _name(view, laughs["top_uid"]) if laughs.get("top_uid") else "",
                f"最爱笑 · {laughs.get('top_count', 0)} 次",
                PINK,
            ),
            (f"{peak}点" if peak is not None else "", "笑声最多", VIOLET),
        ]
    )


def _social_minis(view: dict[str, Any], report: dict[str, Any]) -> str:
    social = report.get("social") or {}
    hub = social.get("hub_uid")
    return _minis(
        [
            (f"{social.get('links', 0)}", "次你来我往", PINK),
            (
                _name(view, hub) if hub else "",
                f"社交中心 · 和 {social.get('hub_partners', 0)} 人聊过",
                BLUE,
            ),
            (f"{social.get('mentions', 0)}次", "互相 @", GREEN),
        ]
    )


def _crowd_minis(report: dict[str, Any]) -> str:
    crowd = report.get("crowd") or {}
    return _minis(
        [
            (_pct(crowd.get("top3_share", 0)), "前三名说的", PINK),
            (f"{round(crowd.get('average', 0))}条", "人均", BLUE),
            (f"{crowd.get('passers', 0)}人", "路过打招呼", GREEN),
        ]
    )


def _also_words(report: dict[str, Any]) -> str:
    words = report.get("more_words") or []
    if not words:
        return ""
    chips = "".join(f'<span class="chip">{esc(word)}</span>' for word in words[:7])
    return f'<div class="also">也聊到了 {chips}</div>'


def _people_row(view: dict[str, Any], uids: Sequence[str]) -> str:
    return (
        '<div class="people-row">'
        + "".join(
            f'<div class="person"><img src="{_avatar(view, uid)}"/>'
            f"<span>{esc(_name(view, uid))}</span></div>"
            for uid in uids
        )
        + "</div>"
    )


def _river_ticks(start: int, end: int, *, weekly: bool) -> list[tuple[int, str]]:
    ticks: list[tuple[int, str]] = []
    first = datetime.fromtimestamp(start, CN_TZ)
    if weekly:
        cursor = first.replace(hour=12, minute=0, second=0, microsecond=0)
        while cursor.timestamp() <= end:
            if cursor.timestamp() >= start:
                ticks.append((int(cursor.timestamp()), WEEKDAYS[cursor.weekday()]))
            cursor = datetime.fromtimestamp(cursor.timestamp() + 86400, CN_TZ)
        return ticks
    span_hours = max(1, (end - start) // 3600)
    step = 3 if span_hours > 12 else (2 if span_hours > 6 else 1)
    cursor = first.replace(minute=0, second=0, microsecond=0)
    while cursor.timestamp() <= end:
        if cursor.timestamp() >= start and cursor.hour % step == 0:
            ticks.append((int(cursor.timestamp()), f"{cursor.hour}:00"))
        cursor = datetime.fromtimestamp(cursor.timestamp() + 3600, CN_TZ)
    return ticks


def _river(view: dict[str, Any], report: dict[str, Any], *, weekly: bool) -> str:
    return art.time_river(
        view.get("stamps") or [],
        start=report["start"],
        end=report["end"],
        chapters=report["sessions"],
        width=_inner(2),
        height=100,
        tick_labels=_river_ticks(report["start"], report["end"], weekly=weekly),
    )


def _page(cells: Sequence[str], view: dict[str, Any], label: str) -> str:
    generated = view.get("generated_at") or datetime.now(CN_TZ)
    footer = (
        f'<div class="footer"><span>{esc(label)} · v{esc(view.get("version", ""))}</span>'
        f'<span>{esc(generated.strftime("%Y-%m-%d %H:%M"))} 生成</span></div>'
    )
    spread = '<div class="spread">' + "".join(cell for cell in cells if cell) + "</div>"
    return (
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
        f'<style>{_css()}</style></head><body><div class="page" id="page">{spread}{footer}</div>'
        "</body></html>"
    )


# ── 日报 ───────────────────────────────────────────────────────────────


def render_day_html(report: dict[str, Any], view: dict[str, Any]) -> str:
    """封面跨两行：剧情（两格）| 花朵时钟；称号 | 笑点 | 话题；
    最后一行贯穿整页：排行 | CP | 日记（两格）。"""
    start = datetime.fromtimestamp(report["start"], CN_TZ)
    end = datetime.fromtimestamp(report["end"], CN_TZ)
    custom = view.get("window_label")
    date_line = (
        f"{start.strftime('%m月%d日 %H:%M')} – {end.strftime('%m月%d日 %H:%M')}"
        if custom
        else f"{end.year}年{end.month}月{end.day}日 · {WEEKDAYS[end.weekday()]}"
    )
    peak = report["peak"]
    peak_foot = f"{fmt_clock(peak['start'])} 开始" if peak["count"] else "一直很安静"
    stats = [
        _stat(
            "消息",
            f"{report['total']}",
            "条",
            _delta_html(report["delta"], "上一段" if custom else "昨天"),
            PINK,
            "chat",
        ),
        _stat(
            "冒泡",
            f"{report['people_count']}",
            "人",
            f"她说了 {report['bot_count']} 句",
            BLUE,
            "people",
        ),
        _stat("最热十分钟", f"{peak['count']}", "条", esc(peak_foot), ORANGE, "fire"),
        _stat(
            "表情和图",
            f"{report['stickers'] + report['images']}",
            "张",
            f"笑声 {int(report['mood']['laughs'])} 次",
            GREEN,
            "sticker",
        ),
    ]
    cover = _cover(
        view,
        report,
        kicker=f"{view.get('group_name', '')} · 群聊手帐",
        title=custom or "今日份群聊",
        date_line=date_line,
        caption="今日一画",
        stats=stats,
        rows=2,
    )
    hours = report["hourly"]
    peak_hour = hours.index(max(hours)) if any(hours) else None
    clock = art.flower_clock(
        hours,
        size=260,
        center_label=f"{peak_hour}点" if peak_hour is not None else "安静",
        center_sub="最热闹" if peak_hour is not None else "",
    )
    first, last = report.get("first"), report.get("last")
    clock_body = (
        f'<div style="display:flex;justify-content:center">{clock}</div>'
        + _dayparts_html(report)
        + _minis(
            [
                (
                    fmt_clock(first["ts"]) if first else "",
                    f"第一句 · {short(first['name'], 6)}" if first else "",
                    ORANGE,
                ),
                (
                    fmt_clock(last["ts"]) if last else "",
                    f"最后一句 · {short(last['name'], 6)}" if last else "",
                    VIOLET,
                ),
            ]
        )
    )
    bubbles = art.bubbles(report["keywords"][:12], width=_inner(), height=330)
    diary = _diary_card(
        view,
        report,
        title="她的日记",
        sign_time=end.strftime("%m月%d日 %H:%M"),
        period="今天",
        span=2,
    )
    crowd = report.get("crowd") or {}
    cells = [
        cover,
        _card(
            _title("今日剧情", "spark", ORANGE, "河越宽，聊得越凶"),
            _river(view, report, weekly=False)
            + _chapters_html(view, report, weekly=False)
            + _rhythm_minis(report),
            span=2,
        ),
        _card(_title("花朵时钟", "sun", PINK, "花瓣越长越热闹"), clock_body),
        _card(
            _title("今日称号", "crown", GOLD, "每人最多一个"),
            _awards_html(view, report)
            + _minis(
                [
                    (f"{len(report['awards'])}人", "拿到称号", GOLD),
                    (f"{round(crowd.get('chars_per_msg', 0))}字", "平均一句", BLUE),
                    (f"{report['stickers']}张", "表情包", GREEN),
                ]
            ),
        ),
        _card(
            _title("笑点与复读", "laugh", ORANGE),
            _quotes_html(view, report, weekly=False, limit=5) + _laugh_minis(view, report),
        ),
        _card(
            _title("话题泡泡", "chat", BLUE, "反复出现的词"),
            (
                f'<div style="display:flex;justify-content:center">{bubbles}</div>'
                if bubbles
                else '<div class="empty">没什么高频词</div>'
            )
            + _also_words(report),
        ),
        # 最后一行贯穿封面下方：排行 | CP | 日记（两格）
        _card(
            _title("冒泡排行", "people", BLUE, f"共 {report['people_count']} 人"),
            _rank_html(view, report, 9) + _crowd_minis(report),
            span=1 if diary else 2,
        ),
        _card(
            _title("聊得来的两个人", "heart", PINK),
            _pairs_html(view, report) + _social_minis(view, report),
            span=1 if diary else 2,
        ),
        diary,
    ]
    return _page(cells, view, "群聊手帐 · 日报")


# ── 周报 ───────────────────────────────────────────────────────────────


def render_week_html(report: dict[str, Any], view: dict[str, Any]) -> str:
    """封面（含全勤与新面孔）跨三行：一周天气（三格）；大事记（两格）| 星座；糖豆日历 | 本周之星 | 话题；
    最后一行贯穿整页：排行 | CP | 笑点 | 周记。"""
    start = datetime.fromtimestamp(report["start"], CN_TZ)
    end = datetime.fromtimestamp(report["end"], CN_TZ)
    newcomers = report.get("newcomers") or []
    regulars = report.get("regulars") or []
    busiest = report.get("busiest_day") or {}
    busiest_name = (
        WEEKDAYS[datetime.strptime(busiest["date"], "%Y-%m-%d").weekday()]
        if busiest.get("count")
        else "—"
    )
    stats = [
        _stat(
            "本周消息",
            f"{report['total']}",
            "条",
            _delta_html(report["delta"], "上周"),
            PINK,
            "chat",
        ),
        _stat(
            "冒泡",
            f"{report['people_count']}",
            "人",
            f"她说了 {report['bot_count']} 句",
            BLUE,
            "people",
        ),
        _stat(
            "新面孔",
            f"{len(newcomers)}",
            "位",
            "上周没见过的人" if report["previous_count"] is not None else "没有上周的记录",
            GREEN,
            "new",
        ),
        _stat("最热的一天", busiest_name, "", f"{busiest.get('count', 0)} 条", ORANGE, "fire"),
    ]
    days_html = []
    for day in report["days"]:
        date = datetime.strptime(day["date"], "%Y-%m-%d")
        best = bool(busiest) and day["date"] == busiest.get("date") and day["count"] > 0
        crown = f'<span class="crown">{icon("crown", 24, GOLD)}</span>' if best else ""
        days_html.append(
            f'<div class="day{" best" if best else ""}">{crown}'
            f'<div class="wd">{WEEKDAYS[date.weekday()]}</div>'
            f'<div class="dd">{date.month}/{date.day}</div>'
            f'<div>{weather_icon(day["weather"], 58)}</div>'
            f'<div class="wn">{esc(WEATHERS.get(day["weather"], ""))}</div>'
            f'<div class="tp">{day["temperature"]}°</div><div class="ct">{day["count"]} 条</div></div>'
        )

    crowd = ""
    if regulars:
        crowd += _minor("全勤打卡", "star", GREEN) + _people_row(view, regulars[:6])
    if newcomers:
        crowd += _minor("新面孔", "new", GREEN) + _people_row(view, newcomers[:6])
    crowd = f'<div class="cover-box">{crowd}</div>' if crowd else ""

    cover = _cover(
        view,
        report,
        kicker=f"{view.get('group_name', '')} · 群聊周刊",
        title=f"第 {start.isocalendar()[1]} 周的我们",
        date_line=f"{start.strftime('%m月%d日')} – {end.strftime('%m月%d日')}",
        caption="本周一画",
        stats=stats,
        rows=3,
        extra=crowd,
    )

    stars = report["constellation"]["stars"]
    if stars:
        sky_body = art.constellation(
            stars,
            report["constellation"]["edges"],
            names={uid: _name(view, uid) for uid in stars},
            avatars={uid: _avatar(view, uid) for uid in stars},
            weights={uid: report["people"][uid].count for uid in stars if uid in report["people"]},
            width=_inner(),
            height=400,
        )
    else:
        sky_body = '<div class="empty">星星还太少</div>'

    rising = report.get("rising") or []
    rising_html = ""
    if rising:
        rising_html = (
            _minor("悄悄升温", "fire", ORANGE)
            + '<div class="rising">'
            + "".join(
                f'<span class="rw"><b>{esc(word)}</b>{count} <small>上周 {before}</small></span>'
                for word, count, before in rising[:4]
            )
            + "</div>"
        )
    bubbles = art.bubbles(
        report["keywords"][:10], width=_inner(), height=270 if rising_html else 320
    )
    topics = (
        (f'<div style="display:flex;justify-content:center">{bubbles}</div>' if bubbles else "")
        + rising_html
        + _also_words(report)
    )
    rhythm = report.get("week_rhythm") or {}
    hot = rhythm.get("hot_slot") or {}
    calendar_minis = _minis(
        [
            (
                f"{WEEKDAYS[hot['weekday']]}{hot['hour']}点" if hot.get("count") else "",
                f"最热的钟点 · {hot.get('count', 0)} 条",
                PINK,
            ),
            (f"{round(rhythm.get('weekday_avg', 0))}条", "工作日日均", BLUE),
            (f"{round(rhythm.get('weekend_avg', 0))}条", "周末日均", ORANGE),
        ]
    )
    social = report.get("social") or {}
    sky_minis = _minis(
        [
            (
                _name(view, social["hub_uid"]) if social.get("hub_uid") else "",
                f"社交中心 · 和 {social.get('hub_partners', 0)} 人聊过",
                GOLD,
            ),
            (f"{social.get('links', 0)}", "次你来我往", PINK),
        ]
    )

    diary = _diary_card(view, report, title="她的周记", sign_time=end.strftime("%m月%d日"), period="这周")
    rank_card = _card(
        _title("冒泡排行", "people", BLUE, f"共 {report['people_count']} 人"),
        _rank_html(view, report, 10) + _crowd_minis(report),
    )
    crowd_stats = report.get("crowd") or {}
    cells = [
        cover,
        _card(
            _title("一周群聊天气", "sun", BLUE, "温度是热闹，天气是心情"),
            f'<div class="forecast">{"".join(days_html)}</div>',
            span=3,
            mid=True,
        ),
        _card(
            _title("本周大事记", "spark", ORANGE, "每天最热的那一段"),
            _river(view, report, weekly=True)
            + _chapters_html(view, report, weekly=True)
            + _rhythm_minis(report),
            span=2,
        ),
        _card(
            _title("群友星座", "star", GOLD, "连线越亮聊得越多"),
            f'<div style="display:flex;justify-content:center">{sky_body}</div>' + sky_minis,
            extra="sky",
        ),
        _card(
            _title("糖豆日历", "star", PINK, "豆子越大越热闹"),
            art.candy_calendar(report["grid"], width=_inner())
            + _dayparts_html(report)
            + calendar_minis,
        ),
        _card(
            _title("本周之星", "crown", GOLD),
            _awards_html(view, report)
            + _minis(
                [
                    (f"{len(report['awards'])}人", "拿到称号", GOLD),
                    (f"{round(crowd_stats.get('chars_per_msg', 0))}字", "平均一句", BLUE),
                    (f"{report['stickers']}张", "表情包", GREEN),
                ]
            ),
        ),
        _card(_title("本周话题", "chat", BLUE), topics or '<div class="empty">没有高频词</div>'),
        # 最后一行贯穿封面下方：排行 | CP | 笑点 | 周记
        rank_card,
        _card(
            _title("聊得来的两个人", "heart", PINK),
            _pairs_html(view, report) + _social_minis(view, report),
        ),
        _card(
            _title("笑点与复读", "laugh", ORANGE),
            _quotes_html(view, report, weekly=True, limit=5) + _laugh_minis(view, report),
            span=1 if diary else 2,
        ),
        diary,
    ]
    return _page(cells, view, "群聊手帐 · 周报")


# ── 截图 ───────────────────────────────────────────────────────────────


async def html_to_png(html: str) -> bytes:
    """用 headless chromium 截整页；没装 playwright / chromium 时抛 RuntimeError。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover - 依赖缺失
        raise RuntimeError("需要 playwright 才能出图：python -m playwright install chromium") from exc

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(args=["--disable-gpu", "--hide-scrollbars"])
        try:
            page = await browser.new_page(
                viewport={"width": PAGE_WIDTH, "height": 1200}, device_scale_factor=1
            )
            await page.set_content(html, wait_until="load", timeout=30000)
            try:
                await asyncio.wait_for(page.evaluate("document.fonts.ready.then(() => true)"), 8.0)
            except Exception:  # noqa: BLE001
                logger.debug("等字体超时，按当前状态截图")
            height = await page.evaluate("document.getElementById('page').scrollHeight")
            height = int(min(max(height, 600), _MAX_HEIGHT))
            await page.set_viewport_size({"width": PAGE_WIDTH, "height": height})
            await asyncio.sleep(0.15)
            return await page.screenshot(
                clip={"x": 0, "y": 0, "width": PAGE_WIDTH, "height": height}, type="png"
            )
        except Exception as exc:
            raise RuntimeError(f"报告渲染失败：{exc}") from exc
        finally:
            await browser.close()
