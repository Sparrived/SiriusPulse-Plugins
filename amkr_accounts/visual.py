"""用 Pillow 在内存中绘制「曜石黑 · 数据星图」看板，无浏览器、网络或临时图片。

设计语言：曜石纯黑背景 + 双色极光光晕；额度窗口用「轨道环」仪表而非进度条；
账号卡用「能量脊柱」+ HUD 取景框边角；数字统一 Cascadia Mono 等宽，中文雅黑。
全部为程序化矢量绘制，无素材、无 IO。
"""

from __future__ import annotations

import io
import math
import os
from functools import lru_cache
from importlib.resources import files
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .client import AMKRAccountError
from .data import Account, QuotaWindow, StatusSnapshot, local_time

# ---------------------------------------------------------------------------
# 设计系统：曜石黑 + 青/紫极光
# ---------------------------------------------------------------------------

_BG = "#05070A"  # 曜石黑
_WHITE = "#F1F5F9"  # 主文字
_MUTED = "#94A3B8"  # 次文字
_FAINT = "#475569"  # 弱提示

_CYAN = "#22D3EE"  # 主色·青
_VIOLET = "#A78BFA"  # 辅色·紫（极光/装饰）
_GREEN = "#34D399"  # 可用
_AMBER = "#FBBF24"  # 预警
_RED = "#FB7185"  # 危险

_W = 1600
_MARGIN = 64  # 页面外边距
_GRID_X = 470  # 账号卡内右栏起点
_COLUMNS = 4  # 轨道环列数
_CELL_W = 258
_CELL_STEP = 266
_CELL_H = 132  # 单个轨道环单元高度


def _rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _rgba(hex_color: str, alpha: int) -> tuple[int, int, int, int]:
    return (*_rgb(hex_color), alpha)


def _tone(remaining: float | None) -> str:
    """额度剩余 -> 语义色。未知为灰。"""
    if remaining is None:
        return _FAINT
    if remaining <= 0.05:
        return _RED
    if remaining <= 0.2:
        return _AMBER
    return _CYAN


# ---------------------------------------------------------------------------
# 字体：中文雅黑；数字/英文/ID 用 Cascadia Mono 等宽（仪表感）
# ---------------------------------------------------------------------------


@lru_cache(maxsize=48)
def _font(size: int, bold: bool = False, mono: bool = False) -> ImageFont.FreeTypeFont:
    windows = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    if mono:
        candidates = [
            windows / "CascadiaCode.ttf",
            windows / ("consolab.ttf" if bold else "consola.ttf"),
            windows / ("arialbd.ttf" if bold else "arial.ttf"),
            Path("/app/data/fonts/cascadia-code/CascadiaCode.ttf"),
            Path("/usr/local/share/fonts/cascadia-code/CascadiaCode.ttf"),
        ]
    else:
        candidates = [
            windows / ("msyhbd.ttc" if bold else "msyh.ttc"),
            Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
            Path("/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc"),
            Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
            Path("/System/Library/Fonts/PingFang.ttc"),
            Path(
                str(
                    files("sirius_pulse").joinpath(
                        "tools/builtin/_internal/assets/ZCOOLKuaiLe-Regular.ttf"
                    )
                )
            ),
        ]
    for path in candidates:
        if path.is_file():
            return ImageFont.truetype(str(path), size)
    if mono:
        return _font(size, bold)  # Linux 没有 Windows 等宽字体时复用现有中文字体。
    raise OSError("缺少中文字体，请安装 Noto Sans CJK。")


def _text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    value: str,
    size: int = 20,
    fill: str = _WHITE,
    width: int = 1100,
    bold: bool = False,
    mono: bool = False,
) -> None:
    font = _font(size, bold, mono)
    if draw.textlength(value, font=font) > width:
        while value and draw.textlength(value + "…", font=font) > width:
            value = value[:-1]
        value += "…"
    draw.text(xy, value, font=font, fill=fill)


# ---------------------------------------------------------------------------
# 背景与氛围
# ---------------------------------------------------------------------------


def _paint_background(base: Image.Image) -> None:
    """曜石黑底 + 顶部双色极光光晕（青左、紫右），克制不喧宾夺主。"""
    w, h = base.size
    ImageDraw.Draw(base).rectangle((0, 0, w, h), fill=_rgb(_BG))

    def glow(cx: int, cy: int, radius: int, color: str, peak: int) -> None:
        layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        cr, cg, cb = _rgb(color)
        steps = 28
        for i in range(steps, 0, -1):
            r = radius * i / steps
            alpha = int(peak * (1 - i / steps) ** 1.8)
            ld.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(cr, cg, cb, alpha))
        base.alpha_composite(layer.filter(ImageFilter.GaussianBlur(radius // 5)))

    glow(int(w * 0.08), int(h * 0.02), 460, _CYAN, 30)
    glow(int(w * 0.96), int(h * 0.05), 420, _VIOLET, 26)
    glow(int(w * 0.5), -160, 700, "#1E293B", 40)  # 顶部中央冷色环境光


# ---------------------------------------------------------------------------
# HUD 取景框边角（卡片四角的 L 形括弧，替代完整描边）
# ---------------------------------------------------------------------------


def _hud_frame(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    length: int = 18,
    alpha: int = 90,
) -> None:
    x0, y0, x1, y1 = box
    c = (148, 197, 255, alpha)
    w = 2
    # 四角各一对横/竖线，形成取景框括弧
    for cx, cy, sx, sy in (
        (x0, y0, 1, 1),
        (x1, y0, -1, 1),
        (x0, y1, 1, -1),
        (x1, y1, -1, -1),
    ):
        draw.line((cx, cy, cx + sx * length, cy), fill=c, width=w)
        draw.line((cx, cy, cx, cy + sy * length), fill=c, width=w)


# ---------------------------------------------------------------------------
# 轨道环（核心创意：每个额度窗口一个环形仪表）
# ---------------------------------------------------------------------------


def _orbital_ring(
    base: Image.Image, cx: int, cy: int, radius: int, frac: float | None, color: str
) -> None:
    """环形仪表：暗色轨道 + 彩色弧 + 圆头端点 + 末端光点。frac=None 显示空轨道。

    辉光在局部小贴片内做高斯模糊再贴回，避免全画布模糊在大批量时过慢。
    """
    width = max(5, radius // 7)
    box = (cx - radius, cy - radius, cx + radius, cy + radius)

    # 局部贴片（环外扩 pad，覆盖辉光与端点）
    pad = width + 14
    px0, py0 = cx - radius - pad, cy - radius - pad
    size = (radius + pad) * 2

    # 轨道（整圈暗色），直接画在 base 上（无模糊，便宜）
    d = ImageDraw.Draw(base)
    d.arc(box, 0, 360, fill=(148, 163, 184, 28), width=width)

    if frac is None:
        return
    frac = max(0.0, min(1.0, frac))
    start = -90.0
    end = start + 360.0 * frac
    main = _rgb(color)

    # 局部贴片坐标（相对贴片原点）
    def L(box_):
        return (box_[0] - px0, box_[1] - py0, box_[2] - px0, box_[3] - py0)

    # 辉光底层（局部模糊）
    glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(glow).arc(L(box), start, end, fill=(*main, 110), width=width + 6)
    base.alpha_composite(glow.filter(ImageFilter.GaussianBlur(4)), (px0, py0))

    # 主弧
    d.arc(box, start, end, fill=(*main, 255), width=width)
    # 圆头端点
    for ang in (start, end):
        x = cx + radius * math.cos(math.radians(ang))
        y = cy + radius * math.sin(math.radians(ang))
        d.ellipse(
            (x - width / 2, y - width / 2, x + width / 2, y + width / 2),
            fill=(*main, 255),
        )
    # 末端高光点（局部模糊）
    ex = cx + radius * math.cos(math.radians(end))
    ey = cy + radius * math.sin(math.radians(end))
    tip = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(tip).ellipse(
        (ex - 5 - px0, ey - 5 - py0, ex + 5 - px0, ey + 5 - py0), fill=(*main, 220)
    )
    base.alpha_composite(tip.filter(ImageFilter.GaussianBlur(3)), (px0, py0))
    d.ellipse((ex - 2, ey - 2, ex + 2, ey + 2), fill=(255, 255, 255, 240))


# ---------------------------------------------------------------------------
# 能量脊柱（账号卡左侧的垂直渐变能量柱，按最低额度变色）
# ---------------------------------------------------------------------------


def _energy_spine(
    base: Image.Image, x: int, y0: int, y1: int, frac: float | None, color: str
) -> None:
    """垂直渐变能量柱：底部实、向上渐隐，柱旁带辉光（局部贴片实现）。"""
    h = y1 - y0
    if h <= 0:
        return
    main = _rgb(color)
    w = 5
    pad = 12
    pw, ph = w + pad * 2, h + pad * 2
    px, py = x - pad, y0 - pad
    # 辉光底层（先模糊贴回）
    glow = Image.new("RGBA", (pw, ph), (0, 0, 0, 0))
    ImageDraw.Draw(glow).rounded_rectangle(
        (pad - 2, pad, pad + w + 2, pad + h), radius=4, fill=(*main, 70)
    )
    base.alpha_composite(glow.filter(ImageFilter.GaussianBlur(4)), (px, py))
    # 渐变主体（清晰，不模糊）
    body = Image.new("RGBA", (pw, ph), (0, 0, 0, 0))
    bd = ImageDraw.Draw(body)
    for i in range(h):
        t = i / h
        level = 0.35 + 0.65 * (frac if frac is not None else 0.0)
        alpha = int(235 * level * (1 - t) ** 0.9 + 18)
        ly = (y1 - i) - py
        bd.line((pad, ly, pad + w, ly), fill=(*main, min(255, alpha)))
    base.alpha_composite(body, (px, py))


# ---------------------------------------------------------------------------
# 布局常量推导
# ---------------------------------------------------------------------------


def _ring_rows(account: Account) -> int:
    return max(1, math.ceil(len(account.windows) / _COLUMNS))


def _row_height(account: Account) -> int:
    rings_h = _ring_rows(account) * _CELL_H
    metrics_h = math.ceil(len(account.summary) / _COLUMNS) * 26 if account.summary else 0
    warn_h = 30 if account.warning else 0
    return max(190, 34 + rings_h + metrics_h + warn_h + 20)


# ---------------------------------------------------------------------------
# 单个额度窗口：轨道环 + 环心百分比 + 底部标签
# ---------------------------------------------------------------------------


def _window_cell(base: Image.Image, window: QuotaWindow, x: int, y: int) -> None:
    """轨道环在左、文字在右。环心放百分比，环下放来源；右侧放窗口名与重置时间。"""
    draw = ImageDraw.Draw(base)
    color = _tone(window.remaining)
    frac = window.remaining
    ring_r = 36
    ring_cx = x + 44
    ring_cy = y + 52

    _orbital_ring(base, ring_cx, ring_cy, ring_r, frac, color)

    # 环心：剩余百分比（等宽大数字 + 小 %）
    if frac is not None:
        pct = f"{math.floor(frac * 100 + 1e-9)}"
        font = _font(24, bold=True, mono=True)
        tw = draw.textlength(pct, font=font)
        draw.text((ring_cx - tw / 2, ring_cy - 18), pct, font=font, fill=_rgb(_WHITE))
        small = _font(11, mono=True)
        sw = draw.textlength("%", font=small)
        draw.text((ring_cx - sw / 2, ring_cy + 8), "%", font=small, fill=_rgb(_FAINT))
    else:
        font = _font(18, bold=True, mono=True)
        tw = draw.textlength("--", font=font)
        draw.text((ring_cx - tw / 2, ring_cy - 12), "--", font=font, fill=_rgb(_FAINT))

    # 环右侧文字区（紧凑标签：把 " / " 收窄为 "/"，省横向空间）
    tx = x + 88
    tw_width = _CELL_W - 92
    label = window.label.replace(" / ", "/")
    _text(draw, (tx, y + 26), label, 14, _WHITE, tw_width, bold=True)
    # 来源与重置合并一行（兼容"被动快照 · 重置 10/15 10:59"的同行断言）
    _text(draw, (tx, y + 50), f"{window.source} · 重置 {window.reset}", 12, _MUTED, tw_width)
    if frac is not None:
        _text(draw, (tx, y + 72), f"剩余 {math.floor(frac*100+1e-9)}%", 12, color, tw_width)
    else:
        _text(draw, (tx, y + 72), "未知", 12, _FAINT, tw_width)


# ---------------------------------------------------------------------------
# 账号卡
# ---------------------------------------------------------------------------


def _account_row(base: Image.Image, account: Account, y: int, height: int) -> None:
    draw = ImageDraw.Draw(base)
    x0, x1 = _MARGIN, _W - _MARGIN

    # 卡片底：极淡的玻璃面板（几乎透明，仅分隔层级）
    panel = Image.new("RGBA", base.size, (0, 0, 0, 0))
    ImageDraw.Draw(panel).rounded_rectangle(
        (x0, y, x1, y + height), radius=16, fill=(148, 163, 184, 9)
    )
    base.alpha_composite(panel)
    _hud_frame(draw, (x0 + 6, y + 6, x1 - 6, y + height - 6))

    # 能量脊柱（按最低额度变色）
    lowest = account.lowest
    spine_color = _tone(lowest)
    _energy_spine(base, x0 + 6, y + 20, y + height - 20, lowest, spine_color)

    # ---- 左栏身份 ----
    lx = x0 + 32
    _text(draw, (lx, y + 18), account.name, 24, _WHITE, 320, bold=True)
    _text(draw, (lx, y + 52), f"{account.provider}  ·  {account.plan}", 13, _MUTED, 210)

    # 状态徽记（小圆点 + 文字）
    status_color = {"可用": _GREEN, "读取失败": _RED}.get(account.status, _AMBER)
    dot = Image.new("RGBA", base.size, (0, 0, 0, 0))
    ImageDraw.Draw(dot).ellipse(
        (lx + 216, y + 54, lx + 224, y + 62), fill=(*_rgb(status_color), 255)
    )
    base.alpha_composite(dot.filter(ImageFilter.GaussianBlur(1)))
    _text(draw, (lx + 230, y + 50), account.status, 13, status_color, 90, bold=True)

    _text(draw, (lx, y + 78), f"ID {account.id}", 13, _CYAN, 320, mono=True)
    _text(draw, (lx, y + 100), account.source, 12, _FAINT, 320)
    _text(draw, (lx, y + 120), f"观测 {account.observed}", 12, _FAINT, 320)
    if account.success is not None or account.failed is not None:
        s = account.success if account.success is not None else "—"
        f = account.failed if account.failed is not None else "—"
        _text(draw, (lx, y + 140), f"成功 {s}  ·  失败 {f}", 12, _FAINT, 320)

    # 左栏右侧垂直分隔微光
    sep = Image.new("RGBA", base.size, (0, 0, 0, 0))
    ImageDraw.Draw(sep).line(
        (_GRID_X - 26, y + 20, _GRID_X - 26, y + height - 20), fill=(148, 163, 184, 16)
    )
    base.alpha_composite(sep)

    # ---- 右栏：轨道环矩阵 ----
    for index, window in enumerate(account.windows):
        _window_cell(
            base,
            window,
            _GRID_X + (index % _COLUMNS) * _CELL_STEP,
            y + 8 + (index // _COLUMNS) * _CELL_H,
        )
    if not account.windows:
        _text(draw, (_GRID_X, y + 40), "上游未提供可读额度窗口", 20, _MUTED)
        _text(draw, (_GRID_X, y + 74), "未知不代表 0%，也不代表无限额度。", 14, _FAINT)

    # ---- 摘要指标 ----
    metrics_y = y + 16 + _ring_rows(account) * _CELL_H
    for index, metric in enumerate(account.summary):
        _text(
            draw,
            (
                _GRID_X + (index % _COLUMNS) * _CELL_STEP,
                metrics_y + (index // _COLUMNS) * 26,
            ),
            metric,
            14,
            _MUTED,
            _CELL_W,
        )

    # ---- 警告 ----
    if account.warning:
        warn_y = y + height - 30
        ImageDraw.Draw(base).ellipse(
            (_GRID_X, warn_y + 5, _GRID_X + 8, warn_y + 13), fill=(*_rgb(_AMBER), 255)
        )
        _text(draw, (_GRID_X + 16, warn_y), account.warning, 13, _AMBER, _W - _GRID_X - 90)


# ---------------------------------------------------------------------------
# 主渲染
# ---------------------------------------------------------------------------


def render_accounts(accounts: list[Account], fetched_at: str) -> bytes:
    """一个账号一条横向行，全部窗口在行内排列；超大查询明确拒绝。"""
    if len(accounts) > 64:
        raise AMKRAccountError("匹配资源超过单张图片 64 个账号的安全上限，请指定账号或实例缩小查询。")
    heights = [_row_height(account) for account in accounts]
    header_h = 300
    height = header_h + max(190, sum(heights) + max(0, len(accounts) - 1) * 16) + 96
    if height > 20000:
        raise AMKRAccountError("匹配资源的图片高度超过 20000 像素安全上限，请指定账号或实例缩小查询。")

    base = Image.new("RGBA", (_W, height), (0, 0, 0, 255))
    _paint_background(base)
    draw = ImageDraw.Draw(base)

    # ======================= Header =======================
    hx = _MARGIN
    # 顶部小标 + 主标题（数字/英文混排：AMKR 用等宽，中文用雅黑）
    _text(draw, (hx, 40), "AMKR", 15, _CYAN, 200, bold=True, mono=True)
    _text(draw, (hx + 72, 40), "/  RESOURCE TELEMETRY", 15, _VIOLET, 400, mono=True)
    _text(draw, (hx - 2, 62), "账号资源 · 用量看板", 40, _WHITE, bold=True)
    _text(draw, (hx, 122), f"读取 {fetched_at}  UTC+08:00   ·   全局资源 / 已脱敏", 14, _MUTED)
    _text(draw, (hx, 146), "一个账号一行 · 轨道环表示剩余额度 · 被动快照不代表此刻实时额度", 13, _FAINT)

    # Header 右侧：品牌星图标记（同心轨道环 + 中心光点，呼应账号卡的轨道环）
    bcx, bcy = _W - 150, 92
    _orbital_ring(base, bcx, bcy, 40, 0.72, _CYAN)
    _orbital_ring(base, bcx, bcy, 56, 0.4, _VIOLET)
    tip = Image.new("RGBA", base.size, (0, 0, 0, 0))
    ImageDraw.Draw(tip).ellipse((bcx - 6, bcy - 6, bcx + 6, bcy + 6), fill=(*_rgb(_CYAN), 255))
    base.alpha_composite(tip.filter(ImageFilter.GaussianBlur(4)))
    ImageDraw.Draw(base).ellipse((bcx - 3, bcy - 3, bcx + 3, bcy + 3), fill=(255, 255, 255, 255))

    # ======================= 统计卡 =======================
    stats = [
        ("CPA 账号", sum(a.kind == "cpa" for a in accounts), _CYAN),
        ("订阅资源", sum(a.kind == "subscription" for a in accounts), _VIOLET),
        (
            "额度 ≤ 20%",
            sum(a.lowest is not None and a.lowest <= 0.2 for a in accounts),
            _AMBER,
        ),
        ("读取异常", sum(bool(a.warning) or a.status == "读取失败" for a in accounts), _RED),
    ]
    card_y, card_h, gap = 190, 96, 18
    card_w = (_W - 2 * _MARGIN - 3 * gap) // 4
    for i, (label, value, color) in enumerate(stats):
        cx0 = _MARGIN + i * (card_w + gap)
        panel = Image.new("RGBA", base.size, (0, 0, 0, 0))
        ImageDraw.Draw(panel).rounded_rectangle(
            (cx0, card_y, cx0 + card_w, card_y + card_h),
            radius=14,
            fill=(148, 163, 184, 10),
        )
        base.alpha_composite(panel)
        _hud_frame(
            draw,
            (cx0 + 5, card_y + 5, cx0 + card_w - 5, card_y + card_h - 5),
            length=12,
            alpha=70,
        )
        # 左侧色条
        ImageDraw.Draw(base).rounded_rectangle(
            (cx0 + 16, card_y + 18, cx0 + 20, card_y + card_h - 18),
            radius=2,
            fill=(*_rgb(color), 220),
        )
        _text(draw, (cx0 + 34, card_y + 18), label, 15, _MUTED, card_w - 120)
        _text(
            draw,
            (cx0 + 32, card_y + 40),
            f"{value:02d}",
            44,
            color if value else _WHITE,
            card_w - 50,
            bold=True,
            mono=True,
        )

    # ======================= 账号行 =======================
    y = header_h
    for account, row_height in zip(accounts, heights):
        _account_row(base, account, y, row_height)
        y += row_height + 16
    if not accounts:
        _text(draw, (_MARGIN, header_h + 40), "暂无账号资源", 28, _WHITE, bold=True)
        _text(draw, (_MARGIN, header_h + 88), "请先在 AMKR 添加 CPA 实例或受支持的订阅供应商。", 17, _MUTED)

    # ======================= 页脚 =======================
    foot_y = height - 80
    ImageDraw.Draw(base).line((_MARGIN, foot_y, _W - _MARGIN, foot_y), fill=(148, 163, 184, 24))
    _text(draw, (_MARGIN, foot_y + 16), f"{len(accounts)} 项资源   /   全部匹配资源", 14, _CYAN)
    _text(
        draw,
        (_W - _MARGIN - 250, foot_y + 14),
        "SIRIUS PULSE × AMKR",
        15,
        _WHITE,
        250,
        bold=True,
        mono=True,
    )
    _text(draw, (_MARGIN, foot_y + 42), "完整 ID 精确查询  /amkr account <ID>", 12, _FAINT)

    final = base.convert("RGB")
    output = io.BytesIO()
    final.save(output, format="PNG", optimize=True)
    return output.getvalue()


# ---------------------------------------------------------------------------
# 状态监控看板
# ---------------------------------------------------------------------------

_BAND_TEXT = {
    "good": "健康",
    "warn": "注意",
    "bad": "异常",
    "unknown": "样本不足",
    "no-traffic": "无流量",
}
_BAND_COLOR = {
    "good": _GREEN,
    "warn": _AMBER,
    "bad": _RED,
    "unknown": _FAINT,
    "no-traffic": _FAINT,
}


def _percent(value: float | None, digits: int = 1) -> str:
    return "—" if value is None else f"{value * 100:.{digits}f}%"


def _latency(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.0f} ms" if value < 1000 else f"{value / 1000:.1f} s"


def _bucket_text(seconds: int) -> str:
    if seconds >= 86400 and seconds % 86400 == 0:
        return f"{seconds // 86400} 天"
    if seconds >= 3600 and seconds % 3600 == 0:
        return f"{seconds // 3600} 小时"
    if seconds >= 60 and seconds % 60 == 0:
        return f"{seconds // 60} 分钟"
    return f"{seconds} 秒"


def _blend(hex_color: str, alpha: float, background: str = _BG) -> tuple[int, int, int]:
    """ImageDraw 在 RGBA 画布上不会自动混合，半透明色需先与底色实算。"""
    fore = _rgb(hex_color)
    back = _rgb(background)
    return (
        round(fore[0] * alpha + back[0] * (1 - alpha)),
        round(fore[1] * alpha + back[1] * (1 - alpha)),
        round(fore[2] * alpha + back[2] * (1 - alpha)),
    )


def _status_panel(base: Image.Image, box: tuple[int, int, int, int], radius: int = 16) -> None:
    panel = Image.new("RGBA", base.size, (0, 0, 0, 0))
    ImageDraw.Draw(panel).rounded_rectangle(box, radius=radius, fill=(148, 163, 184, 9))
    base.alpha_composite(panel)
    _hud_frame(ImageDraw.Draw(base), (box[0] + 6, box[1] + 6, box[2] - 6, box[3] - 6), alpha=55)


def _kpi(
    base: Image.Image,
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    width: int,
    label: str,
    value: str,
    sub: str,
    color: str,
) -> None:
    _status_panel(base, (x, y, x + width, y + 104))
    _text(draw, (x + 18, y + 14), label, 14, _MUTED, width - 36)
    _text(draw, (x + 18, y + 36), value, 32, color, width - 36, bold=True, mono=True)
    _text(draw, (x + 18, y + 79), sub, 11, _FAINT, width - 36)


def _trend_card(
    base: Image.Image,
    draw: ImageDraw.ImageDraw,
    status: StatusSnapshot,
    y: int,
) -> None:
    box = (_MARGIN, y, _W - _MARGIN, y + 236)
    _status_panel(base, box)
    _text(
        draw,
        (_MARGIN + 22, y + 14),
        f"请求趋势 · 每桶 {_bucket_text(status.bucket_seconds)}",
        16,
        _WHITE,
        500,
        bold=True,
    )
    points = status.trend
    total = sum(point.requests for point in points)
    if not points or total == 0:
        _text(draw, (_MARGIN + 22, y + 92), "窗口内没有请求", 24, _MUTED, bold=True)
        _text(draw, (_MARGIN + 22, y + 130), "没有请求不等于服务异常；请检查时间范围与流量来源。", 13, _FAINT)
        return
    peak = max(point.requests for point in points)
    _text(draw, (_W - _MARGIN - 220, y + 14), f"峰值 {peak:,} 次 / 桶", 12, _FAINT, 198)
    x0, y0, x1, y1 = _MARGIN + 22, y + 54, _W - _MARGIN - 22, y + 188
    area_w, area_h = x1 - x0, y1 - y0
    for index in range(5):
        gy = y0 + area_h * index / 4
        draw.line((x0, gy, x1, gy), fill=_blend(_MUTED, 0.12))
    step = area_w / len(points)
    for index, point in enumerate(points):
        left = x0 + index * step
        right = max(left + 1, left + step - 1)
        if point.requests:
            top = y1 - area_h * point.requests / peak
            draw.rectangle((left, top, right, y1), fill=_blend(_CYAN, 0.62))
        if point.failures:
            top = y1 - area_h * min(point.failures, point.requests) / peak
            draw.rectangle((left, top, right, y1), fill=_blend(_RED, 0.85))
    _text(draw, (x0, y1 + 8), local_time(points[0].start), 12, _FAINT, 260)
    last = local_time(points[-1].start)
    _text(draw, (x1 - 260, y1 + 8), last, 12, _FAINT, 260)


def _errors_card(
    base: Image.Image,
    draw: ImageDraw.ImageDraw,
    status: StatusSnapshot,
    y: int,
) -> None:
    _status_panel(base, (_MARGIN, y, _W - _MARGIN, y + 72))
    _text(draw, (_MARGIN + 22, y + 12), "错误归因", 15, _WHITE, 140, bold=True)
    if not status.errors:
        _text(draw, (_MARGIN + 140, y + 15), "窗口内没有非 2xx 响应", 15, _GREEN, 500)
        return
    x = _MARGIN + 140
    for code, count in status.errors:
        label = f"{code} × {count:,}"
        width = int(draw.textlength(label, font=_font(13, mono=True)) + 0.999) + 28
        if x + width > _W - _MARGIN - 20:
            break
        draw.rounded_rectangle((x, y + 10, x + width, y + 40), radius=8, fill=_blend(_RED, 0.16))
        draw.rounded_rectangle((x, y + 10, x + width, y + 40), radius=8, outline=_blend(_RED, 0.55))
        _text(draw, (x + 14, y + 15), label, 13, _RED, width - 26, mono=True)
        x += width + 10
    coded = sum(count for _, count in status.errors)
    _text(
        draw,
        (_MARGIN + 22, y + 44),
        f"共 {status.failures:,} 次失败；{coded:,} 次带非 2xx 状态码，其余失败没有状态码记录。",
        11,
        _FAINT,
        _W - 2 * _MARGIN - 44,
    )


def _channels_card(
    base: Image.Image,
    draw: ImageDraw.ImageDraw,
    status: StatusSnapshot,
    y: int,
) -> int:
    rows = status.channels
    height = 70 + max(1, len(rows)) * 38 + (28 if status.rows_truncated else 0) + 12
    if not rows:
        height = 170
    box = (_MARGIN, y, _W - _MARGIN, y + height)
    _status_panel(base, box)
    _text(draw, (_MARGIN + 22, y + 14), "渠道健康 · 按供应商", 16, _WHITE, 420, bold=True)
    if not rows:
        _text(draw, (_MARGIN + 22, y + 62), "窗口内没有请求，健康矩阵没有可评估的渠道。", 17, _MUTED)
        return height
    columns = [
        ("渠道", _MARGIN + 22, 410),
        ("健康", _MARGIN + 470, 150),
        ("请求", _MARGIN + 640, 130),
        ("成功率", _MARGIN + 810, 140),
        ("平均首字", _MARGIN + 1000, 150),
        ("缓存命中", _MARGIN + 1200, 150),
    ]
    for label, x, width in columns:
        _text(draw, (x, y + 46), label, 12, _FAINT, width)
    top = y + 68
    for index, row in enumerate(rows):
        row_y = top + index * 38
        color = _BAND_COLOR[row.band]
        _text(draw, (_MARGIN + 22, row_y + 9), row.label, 15, _WHITE, 410)
        draw.ellipse((_MARGIN + 470, row_y + 15, _MARGIN + 478, row_y + 23), fill=_rgb(color))
        _text(
            draw,
            (_MARGIN + 486, row_y + 8),
            f"{_BAND_TEXT[row.band]} {row.health}"
            if row.health is not None
            else _BAND_TEXT[row.band],
            14,
            color,
            140,
            bold=True,
        )
        _text(draw, (_MARGIN + 640, row_y + 9), f"{row.requests:,}", 14, _WHITE, 130, mono=True)
        _text(
            draw, (_MARGIN + 810, row_y + 9), _percent(row.success_rate), 14, _MUTED, 140, mono=True
        )
        _text(
            draw, (_MARGIN + 1000, row_y + 9), _latency(row.avg_ttft_ms), 14, _MUTED, 150, mono=True
        )
        _text(
            draw,
            (_MARGIN + 1200, row_y + 9),
            _percent(row.cache_rate, 0),
            14,
            _MUTED,
            150,
            mono=True,
        )
        if index < len(rows) - 1:
            draw.line(
                (_MARGIN + 22, row_y + 36, _W - _MARGIN - 22, row_y + 36),
                fill=_blend(_MUTED, 0.1),
            )
    if status.rows_truncated:
        _text(
            draw,
            (_MARGIN + 22, top + len(rows) * 38 + 4),
            f"另有 {status.rows_truncated} 行未显示（按请求量截断，非查询失败）",
            12,
            _AMBER,
            _W - 2 * _MARGIN - 44,
        )
    return height


def render_status(status: StatusSnapshot) -> bytes:
    """渠道状态看板：KPI、请求趋势、错误归因与按供应商的健康矩阵。"""
    if len(status.channels) > 60:
        raise AMKRAccountError("状态监控渠道超过 60 行安全上限，请缩小时间范围。")
    table_y = 726
    table_height = 170
    if status.channels:
        table_height = 70 + len(status.channels) * 38 + (28 if status.rows_truncated else 0) + 12
    height = table_y + table_height + 92
    if height > 20000:
        raise AMKRAccountError("状态监控图片高度超过 20000 像素安全上限，请缩小时间范围。")

    base = Image.new("RGBA", (_W, height), (0, 0, 0, 255))
    _paint_background(base)
    draw = ImageDraw.Draw(base)

    _text(draw, (_MARGIN, 40), "AMKR", 15, _CYAN, 120, bold=True, mono=True)
    _text(draw, (_MARGIN + 80, 40), "/  STATUS MONITOR", 15, _VIOLET, 380, mono=True)
    _text(draw, (_MARGIN - 2, 62), "渠道状态 · 健康监控", 40, _WHITE, bold=True)
    window = (
        f"近 {status.hours:g} 小时 · "
        f"{local_time(status.window_from, full=True)} — {local_time(status.window_to, full=True)}"
    )
    _text(draw, (_MARGIN, 122), window, 14, _MUTED, 980)
    _text(
        draw,
        (_MARGIN, 146),
        "请求数按上游调用尝试计数 · 健康分 = 成功率 60% + 首字 20% + 缓存 20%",
        13,
        _FAINT,
    )
    _orbital_ring(base, _W - 150, 92, 40, 0.72, _CYAN)
    _orbital_ring(base, _W - 150, 92, 56, 0.4, _VIOLET)

    band_color = _BAND_COLOR[status.health_band]
    cards = [
        (
            "健康分",
            f"{status.health}" if status.health is not None else "—",
            f"{_BAND_TEXT[status.health_band]} · 综合健康分",
            band_color,
        ),
        (
            "成功率",
            _percent(status.success_rate),
            f"成功 {status.successes:,} · 失败 {status.failures:,}",
            band_color if status.requests else _FAINT,
        ),
        ("平均首字", _latency(status.avg_ttft_ms), "按请求数加权", _WHITE),
        ("缓存命中", _percent(status.cache_rate, 0), "缓存 Token / 输入 Token", _WHITE),
        (
            "请求数",
            f"{status.requests:,}",
            f"当前 RPM {status.current_rpm:.1f}" if status.current_rpm is not None else "当前 RPM —",
            _WHITE,
        ),
    ]
    gap = 18
    card_width = (_W - 2 * _MARGIN - gap * 4) // 5
    for index, (label, value, sub, color) in enumerate(cards):
        _kpi(
            base,
            draw,
            _MARGIN + index * (card_width + gap),
            250,
            card_width,
            label,
            value,
            sub,
            color,
        )
    _trend_card(base, draw, status, 378)
    _errors_card(base, draw, status, 638)
    _channels_card(base, draw, status, table_y)

    foot_y = height - 70
    draw.line((_MARGIN, foot_y, _W - _MARGIN, foot_y), fill=_blend(_MUTED, 0.14))
    _text(
        draw,
        (_MARGIN, foot_y + 14),
        f"{status.requests:,} 次请求 · {len(status.channels)} 个渠道 · 已脱敏",
        13,
        _CYAN,
    )
    _text(
        draw,
        (_W - _MARGIN - 250, foot_y + 12),
        "SIRIUS PULSE × AMKR",
        14,
        _WHITE,
        250,
        bold=True,
        mono=True,
    )
    _text(
        draw,
        (_MARGIN, foot_y + 40),
        "来源 AMKR 状态监控 · UTC+08:00 · /amkr status [小时]",
        11,
        _FAINT,
    )
    output = io.BytesIO()
    base.convert("RGB").save(output, format="PNG", optimize=True)
    return output.getvalue()
