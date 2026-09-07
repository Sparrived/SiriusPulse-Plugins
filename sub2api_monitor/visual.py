"""Safe, local-only Ukiyo-e visualizations for the Sub2API monitor.

Reports use opaque washi paper, indigo outlines, vermilion seals, gold fields,
and hard-edged woodblock shadows. All rendering is local (network blocked,
JavaScript disabled, CSP enforced).
"""

from __future__ import annotations

import asyncio
import html
import logging
import math
import os
import re
import time
import weakref
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .data import redact

logger = logging.getLogger(__name__)

_MAX_ARTIFACT_AGE_SECONDS = 24 * 60 * 60
_MAX_ARTIFACT_FILES = 32
_MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
_MAX_SCREENSHOT_BYTES = 4 * 1024 * 1024
_MAX_HTML_BYTES = 128 * 1024
_MAX_SCREENSHOT_HEIGHT = 6000
_MAX_SCREENSHOT_WIDTH = 1600
_RENDER_TIMEOUT_SECONDS = 30.0
_RENDER_SLOT_TIMEOUT_SECONDS = 3.0
_CLEANUP_TIMEOUT_SECONDS = 0.5
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_RENDER_SLOTS: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, asyncio.Semaphore
] = weakref.WeakKeyDictionary()

_MAX_BOARD_ROWS = 16
_MAX_COMPARE_ROWS = 12

_EVENT_LABELS = {
    "subscription_added": ("SUBSCRIPTION / ONLINE", "订阅上架", "good", "plus"),
    "subscription_removed": ("SUBSCRIPTION / OFFLINE", "订阅下架", "bad", "minus"),
    "subscription_changed": ("SUBSCRIPTION / REVISED", "订阅更新", "warn", "repeat"),
    "rate_added": ("RATE / ONLINE", "倍率新增", "good", "plus"),
    "rate_removed": ("RATE / OFFLINE", "倍率移除", "bad", "minus"),
    "rate_changed": ("RATE / SHIFT", "倍率变化", "warn", "swap"),
}
_FALLBACK_EVENT = ("MONITOR / CHANGE", "配置变化", "warn", "swap")

_SUBSCRIPTION_FIELDS = (
    "id",
    "name",
    "product_name",
    "productName",
    "group_name",
    "groupName",
    "group_id",
    "groupId",
    "price",
    "currency",
    "period",
    "billing_cycle",
    "billingCycle",
    "duration",
    "quota",
    "stock",
    "status",
    "enabled",
)
_RATE_FIELDS = (
    "id",
    "group_name",
    "groupName",
    "group_id",
    "groupId",
    "name",
    "platform",
    "rate_multiplier",
    "rate",
    "multiplier",
    "model_ratio",
    "completion_ratio",
    "input_ratio",
    "output_ratio",
    "ratio",
    "weight",
    "value",
    "peak_rate_enabled",
    "peak_start",
    "peak_end",
)
_FIELD_LABELS = {
    "id": "ID",
    "name": "名称",
    "product_name": "产品",
    "productName": "产品",
    "group_name": "分组",
    "groupName": "分组",
    "group_id": "分组 ID",
    "groupId": "分组 ID",
    "price": "价格",
    "currency": "币种",
    "period": "周期",
    "billing_cycle": "计费周期",
    "billingCycle": "计费周期",
    "duration": "时长",
    "quota": "额度",
    "stock": "库存",
    "status": "状态",
    "enabled": "启用",
    "platform": "平台",
    "rate_multiplier": "倍率",
    "rate": "倍率",
    "multiplier": "倍率",
    "model_ratio": "模型倍率",
    "completion_ratio": "补全倍率",
    "input_ratio": "输入倍率",
    "output_ratio": "输出倍率",
    "ratio": "倍率",
    "weight": "权重",
    "value": "数值",
    "peak_rate_enabled": "峰值启用",
    "peak_start": "峰值开始",
    "peak_end": "峰值结束",
}

# Lucide stroke icons (ISC-licensed, lucide.dev). Inner SVG for 24x24 viewBox.
_ICONS: dict[str, str] = {
    "activity": (
        '<path d="M22 12h-2.48a2 2 0 0 0-1.93 1.46l-2.35 8.36a.25.25 0 0 1-.48 0'
        'L9.24 2.18a.25.25 0 0 0-.48 0l-2.35 8.36A2 2 0 0 1 4.49 12H2"/>'
    ),
    "percent": (
        '<line x1="19" x2="5" y1="5" y2="19"/>'
        '<circle cx="6.5" cy="6.5" r="2.5"/><circle cx="17.5" cy="17.5" r="2.5"/>'
    ),
    "trending-up": '<path d="M16 7h6v6"/><path d="m22 7-8.5 8.5-5-5L2 17"/>',
    "trending-down": '<path d="M16 17h6v-6"/><path d="m22 17-8.5-8.5-5 5L2 7"/>',
    "swap": (
        '<path d="m16 3 4 4-4 4"/><path d="M20 7H4"/>'
        '<path d="m8 21-4-4 4-4"/><path d="M4 17h16"/>'
    ),
    "plus": '<path d="M5 12h14"/><path d="M12 5v14"/>',
    "minus": '<path d="M5 12h14"/>',
    "credit-card": (
        '<rect width="20" height="14" x="2" y="5" rx="2"/>'
        '<line x1="2" x2="22" y1="10" y2="10"/>'
    ),
    "wallet": (
        '<path d="M19 7V4a1 1 0 0 0-1-1H5a2 2 0 0 0 0 4h15a1 1 0 0 1 1 1v4h-3'
        'a2 2 0 0 0 0 4h3a1 1 0 0 0 1-1v-2a1 1 0 0 0-1-1"/>'
        '<path d="M3 5v14a2 2 0 0 0 2 2h15a1 1 0 0 0 1-1v-4"/>'
    ),
    "repeat": (
        '<path d="m17 2 4 4-4 4"/><path d="M3 11v-1a4 4 0 0 1 4-4h14"/>'
        '<path d="m7 22-4-4 4-4"/><path d="M21 13v1a4 4 0 0 1-4 4H3"/>'
    ),
    "clock": '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
    "server": (
        '<rect width="20" height="8" x="2" y="2" rx="2" ry="2"/>'
        '<rect width="20" height="8" x="2" y="14" rx="2" ry="2"/>'
        '<line x1="6" x2="6.01" y1="6" y2="6"/><line x1="6" x2="6.01" y1="18" y2="18"/>'
    ),
    "layers": (
        '<path d="m12.83 2.18a2 2 0 0 0-1.66 0L2.6 6.08a1 1 0 0 0 0 1.83l8.58 3.91'
        'a2 2 0 0 0 1.66 0l8.58-3.9a1 1 0 0 0 0-1.83Z"/>'
        '<path d="m22 17.65-9.17 4.16a2 2 0 0 1-1.66 0L2 17.65"/>'
        '<path d="m22 12.65-9.17 4.16a2 2 0 0 1-1.66 0L2 12.65"/>'
    ),
    "gauge": '<path d="m12 14 4-4"/><path d="M3.34 19a10 10 0 1 1 17.32 0"/>',
    "triangle-alert": (
        '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16'
        'a2 2 0 0 0 1.73-3Z"/><path d="M12 9v4"/><path d="M12 17h.01"/>'
    ),
}


def _icon(name: str, cls: str = "") -> str:
    """Render one Lucide icon as an inline stroke SVG."""
    body = _ICONS.get(name) or _ICONS["activity"]
    css_class = " ".join(part for part in ("ic", cls) if part)
    return (
        f'<svg class="{_e(css_class)}" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" '
        'aria-hidden="true">'
        f"{body}</svg>"
    )


_BASE_CSS = """
:root {
  --paper: #f5f0e1;
  --indigo: #1a3055;
  --vermilion: #d4553a;
  --gold: #c9a227;
  --wave: #2a5a8c;
  --ink: #1a3055;
  --ink2: #f5f0e1;
  --ink3: #c9a227;
  --line: #1a3055;
  --line2: #1a3055;
  --text: #1a3055;
  --dim: #1a3055;
  --faint: #1a3055;
  --moon: #1a3055;
  --good: #1a3055;
  --warn: #c9a227;
  --bad: #d4553a;
  --sans: "PingFang SC", "Microsoft YaHei", "Noto Sans CJK SC", sans-serif;
  --display: "Songti SC", "STSong", "SimSun", serif;
  --mono: "Cascadia Mono", Consolas, "Microsoft YaHei", monospace;
}
* { box-sizing: border-box; }
html, body { margin: 0; background: var(--paper); color: var(--indigo); }
body { padding: 0; font-family: var(--sans); }
article {
  position: relative;
  width: 620px;
  overflow: hidden;
  background: var(--paper);
  border: 3px solid var(--indigo);
  box-shadow: 6px 6px 0 var(--indigo);
}
svg.ic { width: 13px; height: 13px; flex: none; }
.wave {
  display: block;
  width: 100%;
  height: 25px;
  color: var(--wave);
  background: var(--gold);
  border-top: 2px solid var(--indigo);
  border-bottom: 2px solid var(--indigo);
}
.wave svg { display: block; width: 100%; height: 100%; }
.brand {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 12px 18px;
  background: var(--indigo);
  border-bottom: 3px solid var(--indigo);
  color: var(--paper);
  font: 700 10px/1 var(--mono);
  letter-spacing: .16em;
}
.brand span {
  display: block;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.foot {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 12px 18px;
  background: var(--gold);
  border-top: 3px solid var(--indigo);
  color: var(--indigo);
  font: 700 9px/1.35 var(--mono);
  letter-spacing: .1em;
  white-space: nowrap;
}
.foot span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.head {
  position: relative;
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: end;
  gap: 18px;
  padding: 17px 18px 14px;
  background: var(--paper);
}
.kicker {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  color: var(--vermilion);
  font: 800 10px/1 var(--mono);
  letter-spacing: .16em;
}
h1 {
  margin: 0;
  min-width: 0;
  overflow: hidden;
  color: var(--indigo);
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: var(--display);
  font-size: 31px;
  font-weight: 800;
  letter-spacing: .08em;
  line-height: 1.16;
}
.count {
  color: var(--vermilion);
  font: 800 40px/1 var(--mono);
  font-variant-numeric: tabular-nums;
  letter-spacing: -.06em;
  text-align: right;
}
.count small {
  margin-left: 3px;
  color: var(--indigo);
  font-size: 14px;
  font-weight: 800;
}
.count-label {
  margin-top: 5px;
  color: var(--indigo);
  font: 800 8.5px/1 var(--mono);
  letter-spacing: .14em;
  text-align: right;
}
"""

_BOARD_CSS = (
    _BASE_CSS
    + """
.rows { position: relative; padding: 0 18px 12px; }
.row {
  display: grid;
  grid-template-columns: 28px minmax(0, 1fr) auto;
  align-items: start;
  gap: 10px;
  padding: 12px 0;
  border-top: 2px solid var(--indigo);
}
.rows .row:first-child { border-top: 0; }
.idx {
  display: grid;
  place-items: center;
  min-height: 25px;
  background: var(--gold);
  border: 2px solid var(--indigo);
  color: var(--indigo);
  font: 800 9px/1 var(--mono);
  letter-spacing: .06em;
}
.who { min-width: 0; }
.name {
  display: flex;
  align-items: center;
  gap: 7px;
  min-width: 0;
  color: var(--indigo);
  font-size: 15px;
  font-weight: 800;
  line-height: 1.3;
  white-space: nowrap;
}
.name b { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.chip, .seal {
  display: inline-flex;
  align-items: center;
  flex: none;
  min-height: 20px;
  padding: 3px 5px;
  border: 2px solid var(--indigo);
  background: var(--paper);
  color: var(--indigo);
  font: 800 8px/1 var(--mono);
  letter-spacing: .08em;
  font-style: normal;
  text-transform: uppercase;
}
.seal { background: var(--vermilion); color: var(--paper); box-shadow: 2px 2px 0 var(--indigo); }
.chip.t-warn { background: var(--gold); color: var(--indigo); }
.chip.t-good { background: var(--wave); color: var(--paper); }
.chip.t-bad { background: var(--vermilion); color: var(--paper); }
.details {
  display: flex;
  flex-wrap: nowrap;
  gap: 5px 10px;
  min-width: 0;
  overflow: hidden;
  margin-top: 7px;
  color: var(--indigo);
  font: 700 9px/1.35 var(--mono);
  letter-spacing: .02em;
  white-space: nowrap;
}
.details span { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.details b { color: var(--vermilion); font-weight: 800; }
.track { height: 6px; margin-top: 8px; border: 2px solid var(--indigo); background: var(--paper); }
.track i { display: block; height: 100%; min-width: 2px; background: var(--wave); }
.track .t-good { background: var(--wave); }
.track .t-warn { background: var(--gold); }
.track .t-bad { background: var(--vermilion); }
.track .t-moon { background: var(--indigo); }
.val {
  padding-top: 1px;
  color: var(--vermilion);
  font: 800 23px/1 var(--mono);
  font-variant-numeric: tabular-nums;
  letter-spacing: -.05em;
  text-align: right;
  white-space: nowrap;
}
.val i { margin-left: 2px; color: var(--indigo); font-style: normal; font-size: 12px; }
.val.price { font-size: 20px; }
.period { margin-top: 4px; color: var(--indigo); font: 800 8.5px/1.3 var(--mono); letter-spacing: .06em; text-align: right; white-space: nowrap; }
.t-good { color: var(--wave); }
.t-warn { color: var(--gold); }
.t-bad { color: var(--vermilion); }
.t-moon { color: var(--indigo); }
.t-dim { color: var(--indigo); }
.more { padding: 10px 18px 2px; color: var(--vermilion); font: 800 9.5px/1.2 var(--mono); letter-spacing: .1em; }
.empty {
  display: grid;
  place-items: center;
  gap: 10px;
  margin: 10px 18px 16px;
  padding: 28px 18px;
  border: 3px dashed var(--indigo);
  background: var(--gold);
  color: var(--indigo);
  text-align: center;
}
.empty svg.ic { width: 20px; height: 20px; }
.empty b { font: 800 11px/1 var(--mono); letter-spacing: .14em; }
.empty small { font: 700 9px/1.5 var(--mono); letter-spacing: .08em; }
"""
)

_IQ_CSS = (
    _BOARD_CSS
    + """
.iq-groups { padding: 0 18px 14px; }
.iq-model { padding: 14px 0 2px; border-top: 3px solid var(--indigo); }
.iq-model:first-child { border-top: 0; }
.iq-model-head {
  display: grid;
  grid-template-columns: 28px minmax(0, 1fr) auto;
  align-items: center;
  gap: 10px;
  padding-bottom: 8px;
}
.iq-model-head b { overflow: hidden; color: var(--indigo); font: 800 17px/1.2 var(--sans); text-overflow: ellipsis; white-space: nowrap; }
.iq-model-count { color: var(--vermilion); font: 800 9px/1 var(--mono); letter-spacing: .08em; white-space: nowrap; }
.iq-efforts { border: 2px solid var(--indigo); background: var(--paper); }
.iq-effort {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: center;
  gap: 12px;
  padding: 9px 10px;
  border-top: 2px solid var(--indigo);
}
.iq-effort:first-child { border-top: 0; }
.iq-effort-info { min-width: 0; }
.iq-effort-label { display: block; overflow: hidden; color: var(--vermilion); font: 800 9px/1.2 var(--mono); letter-spacing: .1em; text-overflow: ellipsis; white-space: nowrap; }
.iq-effort .details { margin-top: 5px; }
.iq-effort .val { font-size: 19px; }
"""
)


_CARD_CSS = (
    _BASE_CSS
    + """
.meta {
  margin-top: 8px;
  overflow: hidden;
  color: var(--indigo);
  font: 800 9px/1.5 var(--mono);
  letter-spacing: .08em;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.compare {
  position: relative;
  display: grid;
  grid-template-columns: 1fr 44px 1fr;
  align-items: stretch;
  padding: 0 18px 16px;
}
.panel { min-width: 0; padding: 12px; border: 3px solid var(--indigo); background: var(--paper); }
.panel.after { background: var(--gold); }
.ptitle { display: flex; justify-content: space-between; margin-bottom: 6px; color: var(--vermilion); font: 800 8.5px/1 var(--mono); letter-spacing: .12em; }
.prow { display: grid; grid-template-columns: minmax(0, 76px) minmax(0, 1fr); gap: 8px; align-items: baseline; padding: 7px 0; border-top: 2px solid var(--indigo); font-size: 12px; line-height: 1.35; }
.prow:first-of-type { border-top: 0; }
.pkey { overflow: hidden; color: var(--indigo); font: 800 9px/1.3 var(--mono); letter-spacing: .04em; text-overflow: ellipsis; white-space: nowrap; }
.pval { min-width: 0; overflow: hidden; color: var(--indigo); font-variant-numeric: tabular-nums; text-overflow: ellipsis; white-space: nowrap; }
.pval.cut { color: var(--vermilion); text-decoration: line-through; }
.pval.hot { color: var(--vermilion); font-weight: 800; }
.pval.none::before { content: "无"; color: var(--indigo); }
.mid { display: grid; place-items: center; }
.ring { display: grid; place-items: center; width: 30px; height: 30px; border: 3px solid var(--indigo); background: var(--vermilion); color: var(--paper); box-shadow: 2px 2px 0 var(--indigo); }
.ring svg.ic { width: 14px; height: 14px; }
"""
)


def _wave_motif() -> str:
    """Return a solid, woodblock-style wave band shared by every report."""
    return (
        '<div class="wave" aria-hidden="true"><svg viewBox="0 0 620 26" '
        'preserveAspectRatio="none"><path d="M0 18 C24 4 46 4 70 18 '
        "S116 32 140 18 S186 4 210 18 S256 32 280 18 S326 4 350 18 "
        'S396 32 420 18 S466 4 490 18 S536 32 560 18 S596 4 620 18" '
        'fill="none" stroke="currentColor" stroke-width="4"/></svg></div>'
    )


def _safe_scalar(record: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = record.get(key)
        if value not in (None, "") and not isinstance(value, (dict, list, tuple, set)):
            return _clip(value, 28)
    return ""


def _detail_html(items: list[tuple[str, str]]) -> str:
    visible = [(label, value) for label, value in items if value]
    if not visible:
        return ""
    return (
        '<div class="details">'
        + "".join(
            f"<span><b>{_e(label)}</b>{_e(value)}</span>"
            for label, value in visible[:5]
        )
        + "</div>"
    )


def _subscription_details(record: dict[str, Any]) -> str:
    return _detail_html(
        [
            ("编号 ", _safe_scalar(record, "id", "subscription_id", "subscriptionId")),
            ("套餐 ", _safe_scalar(record, "plan", "plan_name", "plan_id", "planId")),
            ("额度 ", _safe_scalar(record, "quota", "limit", "capacity")),
            ("库存 ", _safe_scalar(record, "stock", "remaining", "available")),
            ("到期 ", _safe_scalar(record, "expires_at", "expire_at", "expired_at")),
        ]
    )


def _rate_details(record: dict[str, Any]) -> str:
    peak_start = _safe_scalar(record, "peak_start", "peakStart")
    peak_end = _safe_scalar(record, "peak_end", "peakEnd")
    peak_window = f"{peak_start} 至 {peak_end}" if peak_start and peak_end else ""
    peak_enabled = _safe_scalar(record, "peak_rate_enabled", "peakRateEnabled")
    peak = peak_window or (
        "已启用" if peak_enabled.casefold() in {"true", "1", "yes"} else ""
    )
    return _detail_html(
        [
            ("输入 ", _safe_scalar(record, "input_ratio", "inputRatio")),
            (
                "输出 ",
                _safe_scalar(
                    record,
                    "output_ratio",
                    "outputRatio",
                    "completion_ratio",
                    "completionRatio",
                ),
            ),
            ("模型 ", _safe_scalar(record, "model_ratio", "modelRatio")),
            ("峰值 ", peak),
        ]
    )


def _subscription_status(record: dict[str, Any]) -> tuple[str, str]:
    enabled = record.get("enabled")
    raw = _safe_scalar(record, "status")
    inactive = {"inactive", "disabled", "false", "0", "offline", "sold_out"}
    active = {"active", "enabled", "true", "1", "online", "available"}
    if enabled is False or raw.casefold() in inactive:
        return "已停用", "bad"
    if enabled is True or raw.casefold() in active:
        return "可用", "good"
    return (raw, "warn") if raw else ("", "")


def _safe_error_text(value: Any) -> str:
    """Return a clipped error summary without credential-like fragments."""
    text = str(redact(value) if value is not None else "")
    return re.sub(
        r"(?i)(?:password|passwd|token|secret|api[_-]?key|authorization)\s*[:=]\s*[^\s,;]+",
        "[已隐藏]",
        text,
    )


_IQ_EFFORT_ORDER = {"low": 0, "medium": 1, "high": 2, "xhigh": 3, "max": 4, "ultra": 5}


def _iq_number(value: Any, *, decimals: int = 2, suffix: str = "") -> str:
    parsed = _finite_rate(value)
    if parsed is None:
        return "—"
    rendered = f"{parsed:.{decimals}f}".rstrip("0").rstrip(".")
    return f"{rendered}{suffix}"


def _iq_details(record: dict[str, Any]) -> str:
    cache = _finite_rate(record.get("cache_hit_rate"))
    cache_text = f"{cache * 100:.1f}%" if cache is not None else "—"
    price = _iq_number(record.get("average_price_usd"), decimals=3)
    return _detail_html(
        [
            ("均价 ", f"USD {price}"),
            ("耗时 ", _iq_number(record.get("average_minutes"), suffix=" 分")),
            ("缓存 ", cache_text),
        ]
    )


def _iq_record_sort_key(record: dict[str, Any]) -> tuple[int, str]:
    effort = _safe_scalar(record, "effort").casefold()
    return (_IQ_EFFORT_ORDER.get(effort, len(_IQ_EFFORT_ORDER)), effort)


def build_iq_html(
    *,
    records: list[dict[str, Any]],
    query: str = "",
    generated_at: int | None = None,
) -> str:
    """Build a safe Ukiyo-e board grouped by model and its effort levels."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        model = _safe_scalar(record, "model")
        if model:
            groups.setdefault(model, []).append(record)
    ordered_groups = sorted(
        groups.items(),
        key=lambda item: (
            -max(
                value if value is not None else float("-inf")
                for value in (_finite_rate(record.get("iq")) for record in item[1])
            ),
            item[0].casefold(),
        ),
    )
    shown_records = sum(len(model_records) for _model, model_records in ordered_groups)

    group_html: list[str] = []
    for index, (model, model_records) in enumerate(ordered_groups, start=1):
        efforts: list[str] = []
        for record in sorted(model_records, key=_iq_record_sort_key):
            effort = _safe_scalar(record, "effort") or "—"
            iq = _iq_number(record.get("iq"))
            tone = _rate_tone(_finite_rate(record.get("iq")))
            efforts.append(
                '<div class="iq-effort">'
                '<div class="iq-effort-info">'
                f'<span class="iq-effort-label">EFFORT / {_e(effort)}</span>'
                f"{_iq_details(record)}</div>"
                f'<span class="val t-{tone}">{_e(iq)}<i>IQ</i></span>'
                "</div>"
            )
        group_html.append(
            '<section class="iq-model">'
            '<header class="iq-model-head">'
            f'<span class="idx">{index:02d}</span>'
            f"<b>{_e(_clip(model, 52))}</b>"
            f'<span class="iq-model-count">{len(model_records)} 档</span>'
            "</header>"
            f'<div class="iq-efforts">{"".join(efforts)}</div>'
            "</section>"
        )
    groups_html = "".join(group_html) or _empty_state("NO IQ MATCHES", "未找到匹配的模型效率记录")
    heading = _clip(query, 40) if query else "模型效率榜"
    label = "模糊搜索结果" if query else "按 IQ 排序"
    body = (
        '<article id="sub2api-iq">'
        '<header class="brand">'
        "<span>INTELLIGENCE IQ / 智能效率</span>"
        f"<span>{_e(_timestamp(generated_at))}</span>"
        "</header>"
        f"{_wave_motif()}"
        '<header class="head">'
        f"<div><h1>{_e(heading)}</h1></div>"
        f'<div><div class="count">{len(ordered_groups)}<small>模型</small></div>'
        f'<div class="count-label">{shown_records} 档 / {_e(label)}</div></div>'
        "</header>"
        f'<section class="iq-groups">{groups_html}</section>'
        f"{_wave_motif()}"
        '<footer class="foot">'
        "<span>基准 / DEEP-SWE</span>"
        "<span>IQ、力度、均价、耗时与缓存命中率</span>"
        "</footer></article>"
    )
    return _document(_IQ_CSS, body)


def build_rates_html(
    *,
    source_id: str,
    display_name: str,
    records: list[dict[str, Any]],
    generated_at: int | None = None,
) -> str:
    """Build a Ukiyo-e group-rate query board with operational detail."""
    parsed: list[tuple[float | None, dict[str, Any], str, str]] = []
    for record in records[: _MAX_BOARD_ROWS + 40]:
        if not isinstance(record, dict):
            continue
        cleaned = redact(record)
        if not isinstance(cleaned, dict):
            continue
        label = _subject_label(cleaned)
        if label:
            parsed.append(
                (
                    _finite_rate(_primary_rate_value(cleaned)),
                    cleaned,
                    label,
                    _safe_scalar(cleaned, "platform"),
                )
            )
    parsed.sort(
        key=lambda item: (item[0] is None, item[0] if item[0] is not None else 0.0)
    )
    shown = parsed[:_MAX_BOARD_ROWS]
    omitted = max(0, len(parsed) - len(shown))
    finite_values = [value for value, *_rest in shown if value is not None]
    peak = max(finite_values) if finite_values else 0.0

    rows: list[str] = []
    for index, (value, record, label, platform) in enumerate(shown, start=1):
        tone = _rate_tone(value)
        chips = f'<em class="chip">{_e(platform)}</em>' if platform else ""
        if value is None:
            value_html = '<span class="val t-dim">无</span>'
            track_html = ""
        else:
            width = 2 if peak <= 0 else max(2, round(value / peak * 100))
            value_html = f'<span class="val t-{tone}">{_fmt_rate(value)}<i>×</i></span>'
            track_html = f'<div class="track"><i class="t-{tone}" style="width:{width}%"></i></div>'
        rows.append(
            '<div class="row">'
            f'<span class="idx">{index:02d}</span>'
            f'<div class="who"><div class="name"><b>{_e(_clip(label, 44))}</b>{chips}</div>'
            f"{_rate_details(record)}{track_html}</div>{value_html}"
            "</div>"
        )
    rows_html = "".join(rows) or _empty_state("NO RATES CAPTURED", "尚未捕获任何分组倍率")
    more_html = f'<div class="more">另有 {omitted} 个分组未展示</div>' if omitted else ""
    body = (
        '<article id="sub2api-board">'
        '<header class="brand">'
        "<span>GROUP RATES / 分组倍率</span>"
        f"<span>{_e(_timestamp(generated_at))}</span>"
        "</header>"
        f"{_wave_motif()}"
        '<header class="head">'
        f"<div><h1>{_e(_clip(display_name, 40))}</h1></div>"
        f'<div><div class="count">{len(parsed)}<small>组</small></div>'
        '<div class="count-label">可用分组</div></div>'
        "</header>"
        f'<section class="rows">{rows_html}</section>{more_html}'
        f"{_wave_motif()}"
        '<footer class="foot">'
        f"<span>站点 / {_e(_clip(source_id.upper(), 24))}</span>"
        "<span>倍率含输入、输出、模型与峰值信息</span>"
        "</footer></article>"
    )
    return _document(_BOARD_CSS, body)


def build_subscriptions_html(
    *,
    source_id: str,
    display_name: str,
    records: list[dict[str, Any]],
    generated_at: int | None = None,
) -> str:
    """Build a Ukiyo-e subscription query board with decision-useful fields."""
    parsed: list[dict[str, Any]] = []
    for record in records[: _MAX_BOARD_ROWS + 40]:
        if not isinstance(record, dict):
            continue
        cleaned = redact(record)
        if isinstance(cleaned, dict) and _subject_label(cleaned):
            parsed.append(cleaned)
    shown = parsed[:_MAX_BOARD_ROWS]
    omitted = max(0, len(parsed) - len(shown))

    rows: list[str] = []
    for index, record in enumerate(shown, start=1):
        label = _subject_label(record)
        group = _safe_scalar(record, "group_name", "groupName", "group_id", "groupId")
        status_text, status_tone = _subscription_status(record)
        chips = f'<em class="chip">{_e(group)}</em>' if group else ""
        if status_text:
            chips += f'<em class="chip t-{status_tone}">{_e(status_text)}</em>'
        rows.append(
            '<div class="row">'
            f'<span class="idx">{index:02d}</span>'
            f'<div class="who"><div class="name"><b>{_e(_clip(label, 44))}</b>{chips}</div>'
            f"{_subscription_details(record)}</div>"
            f"<div>{_price_html(record)}</div>"
            "</div>"
        )
    rows_html = "".join(rows) or _empty_state("NO SUBSCRIPTIONS", "暂无可售订阅")
    more_html = f'<div class="more">另有 {omitted} 个订阅未展示</div>' if omitted else ""
    body = (
        '<article id="sub2api-board">'
        '<header class="brand">'
        "<span>SUBSCRIPTIONS / 可售订阅</span>"
        f"<span>{_e(_timestamp(generated_at))}</span>"
        "</header>"
        f"{_wave_motif()}"
        '<header class="head">'
        f"<div><h1>{_e(_clip(display_name, 40))}</h1></div>"
        f'<div><div class="count">{len(parsed)}<small>项</small></div>'
        '<div class="count-label">订阅目录</div></div>'
        "</header>"
        f'<section class="rows">{rows_html}</section>{more_html}'
        f"{_wave_motif()}"
        '<footer class="foot">'
        f"<span>站点 / {_e(_clip(source_id.upper(), 24))}</span>"
        "<span>价格、周期、套餐、额度、库存与到期</span>"
        "</footer></article>"
    )
    return _document(_BOARD_CSS, body)


def build_change_card_html(
    *,
    source_id: str,
    display_name: str,
    event_type: str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    occurred_at: int | None = None,
) -> str:
    """Build a Ukiyo-e before/after change card from approved fields only."""
    _code, title, _tone, icon = _EVENT_LABELS.get(event_type, _FALLBACK_EVENT)
    category = "RATE" if event_type.startswith("rate_") else "SUBSCRIPTION"
    if event_type not in _EVENT_LABELS:
        category = "MONITOR"
    header_label = f"{category} CHANGE / {title}"
    if event_type == "rate_changed":
        direction = _rate_direction(before, after)
        if direction == "up":
            icon = "trending-up"
        elif direction == "down":
            icon = "trending-down"
    before_clean = _project_record(before, event_type)
    after_clean = _project_record(after, event_type)
    subject = _subject(after_clean or before_clean, event_type)
    before_rows = _compare_rows(before_clean, after_clean, changed_side="before")
    after_rows = _compare_rows(after_clean, before_clean, changed_side="after")
    body = (
        '<article id="sub2api-card">'
        '<header class="brand">'
        f"<span>{_e(header_label)}</span>"
        f"<span>{_e(_timestamp(occurred_at))}</span>"
        "</header>"
        f"{_wave_motif()}"
        '<header class="head">'
        f"<div><h1>{_e(subject)}</h1>"
        f'<div class="meta">站点 / {_e(_clip(display_name, 48))}'
        f" · {_e(_clip(source_id.upper(), 24))}</div></div>"
        "</header>"
        '<section class="compare">'
        f'<div class="panel"><div class="ptitle"><span>变更前</span><span>旧</span></div>{before_rows}</div>'
        f'<div class="mid"><span class="ring">{_icon(icon)}</span></div>'
        f'<div class="panel after"><div class="ptitle"><span>变更后</span><span>新</span></div>{after_rows}</div>'
        "</section>"
        f"{_wave_motif()}"
        '<footer class="foot">'
        "<span>快照差异已跟踪</span><span>仅显示已批准字段</span>"
        "</footer></article>"
    )
    return _document(_CARD_CSS, body)


def build_dashboard_html(
    sources: list[dict[str, Any]], *, generated_at: int | None = None
) -> str:
    """Build a redacted multi-source monitor overview for the legacy report path."""
    rows: list[str] = []
    for index, source in enumerate(sources[:_MAX_BOARD_ROWS], start=1):
        if not isinstance(source, dict):
            continue
        cleaned = redact(source)
        if not isinstance(cleaned, dict):
            continue
        name = _safe_scalar(cleaned, "display_name", "id") or "未命名站点"
        source_id = _safe_scalar(cleaned, "id")
        ready = cleaned.get("ready") is True
        subscriptions = cleaned.get("subscriptions")
        rates = cleaned.get("rates")
        subscription_count = (
            len(subscriptions) if isinstance(subscriptions, list) else 0
        )
        rate_count = len(rates) if isinstance(rates, list) else 0
        error = _safe_error_text(cleaned.get("error"))
        state = "可用" if ready and not error else "需检查"
        tone = "good" if ready and not error else "bad"
        details = _detail_html(
            [
                ("订阅 ", str(subscription_count)),
                ("分组 ", str(rate_count)),
                ("编号 ", source_id),
                ("状态 ", state),
            ]
        )
        error_html = (
            f'<div class="details"><span><b>提示 </b>{_e(_clip(error, 80))}</span></div>'
            if error
            else ""
        )
        rows.append(
            '<div class="row">'
            f'<span class="idx">{index:02d}</span>'
            f'<div class="who"><div class="name"><b>{_e(_clip(name, 44))}</b>'
            f'<em class="chip t-{tone}">{_e(state)}</em></div>{details}{error_html}</div>'
            f'<span class="val t-{tone}">{subscription_count}<i>订阅</i></span>'
            "</div>"
        )
    rows_html = "".join(rows) or _empty_state("NO SOURCES", "尚未配置可用站点")
    body = (
        '<article id="sub2api-dashboard">'
        '<header class="brand">'
        f'<span>{_icon("activity")} SIRIUS PULSE / SUB2API</span>'
        f'<span>{_icon("clock")} {_e(_timestamp(generated_at))}</span>'
        "</header>"
        f"{_wave_motif()}"
        '<header class="head"><div><div class="kicker">'
        f'{_icon("server")} MONITOR / 站点概览</div><h1>监控总览</h1></div>'
        f'<div><div class="count">{len(rows)}<small>站</small></div>'
        '<div class="count-label">已配置站点</div></div></header>'
        f'<section class="rows">{rows_html}</section>{_wave_motif()}'
        '<footer class="foot"><span>轮询数据已脱敏</span><span>最近一次轮询存在错误时标记需检查</span></footer>'
        "</article>"
    )
    return _document(_BOARD_CSS, body)


async def render_rates_card(
    records: list[dict[str, Any]],
    *,
    source_id: str,
    display_name: str,
    artifact_dir: Path,
    generated_at: int | None = None,
) -> str | None:
    """Render the group-rates board, returning ``None`` on optional failure."""
    return await _render_html(
        build_rates_html(
            source_id=source_id,
            display_name=display_name,
            records=records,
            generated_at=generated_at,
        ),
        selector="#sub2api-board",
        artifact_dir=artifact_dir,
        filename_prefix=f"sub2api_rates_{_safe_slug(source_id)}",
    )


async def render_subscriptions_card(
    records: list[dict[str, Any]],
    *,
    source_id: str,
    display_name: str,
    artifact_dir: Path,
    generated_at: int | None = None,
) -> str | None:
    """Render the subscriptions board, returning ``None`` on optional failure."""
    return await _render_html(
        build_subscriptions_html(
            source_id=source_id,
            display_name=display_name,
            records=records,
            generated_at=generated_at,
        ),
        selector="#sub2api-board",
        artifact_dir=artifact_dir,
        filename_prefix=f"sub2api_subs_{_safe_slug(source_id)}",
    )


async def render_iq_card(
    records: list[dict[str, Any]],
    *,
    query: str,
    artifact_dir: Path,
    generated_at: int | None = None,
) -> str | None:
    """Render the public intelligence-efficiency query board."""
    return await _render_html(
        build_iq_html(records=records, query=query, generated_at=generated_at),
        selector="#sub2api-iq",
        artifact_dir=artifact_dir,
        filename_prefix="sub2api_iq",
    )


async def render_change_card(**kwargs: Any) -> str | None:
    """Render one notification card, returning ``None`` on optional failure."""
    artifact_dir = Path(kwargs.pop("artifact_dir"))
    source_id = str(kwargs.get("source_id", "source"))
    return await _render_html(
        build_change_card_html(**kwargs),
        selector="#sub2api-card",
        artifact_dir=artifact_dir,
        filename_prefix=f"sub2api_change_{_safe_slug(source_id)}",
    )


async def _render_html(
    html_text: str,
    *,
    selector: str,
    artifact_dir: Path,
    filename_prefix: str,
) -> str | None:
    if len(html_text.encode("utf-8")) > _MAX_HTML_BYTES:
        logger.warning("sub2api_monitor: 可视化 HTML 超过安全上限")
        return None
    slots = _get_render_slots()
    acquired = False
    try:
        async with asyncio.timeout(_RENDER_TIMEOUT_SECONDS):
            async with asyncio.timeout(_RENDER_SLOT_TIMEOUT_SECONDS):
                await slots.acquire()
                acquired = True
            return await _render_html_impl(
                html_text,
                selector=selector,
                artifact_dir=artifact_dir,
                filename_prefix=filename_prefix,
            )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("sub2api_monitor: Playwright 可视化生成失败 (%s)", type(exc).__name__)
        return None
    finally:
        if acquired:
            slots.release()


async def _render_html_impl(
    html_text: str,
    *,
    selector: str,
    artifact_dir: Path,
    filename_prefix: str,
) -> str:
    try:
        from playwright.async_api import async_playwright  # type: ignore[import-untyped]
    except ImportError as exc:
        raise RuntimeError("Playwright 未安装") from exc

    root = _artifact_root(artifact_dir)
    await prune_artifacts(root)
    nonce = uuid4().hex
    output_path = root / f"{filename_prefix}_{nonce}.png"
    temporary_path = root / f"{filename_prefix}_{nonce}.tmp.png"
    _assert_safe_child(root, output_path)
    _assert_safe_child(root, temporary_path)
    manager: Any | None = None
    manager_entered = False
    browser: Any | None = None
    context: Any | None = None
    try:
        manager = async_playwright()
        playwright = await asyncio.wait_for(manager.__aenter__(), timeout=5.0)
        manager_entered = True
        launched_browser = await asyncio.wait_for(
            playwright.chromium.launch(headless=True), timeout=10.0
        )
        browser = launched_browser
        context = await launched_browser.new_context(
            viewport={"width": 680, "height": 1400},
            device_scale_factor=2,
            java_script_enabled=False,
            service_workers="block",
        )

        async def _abort(route: Any) -> None:
            await route.abort()

        await context.route("**/*", _abort)
        page = await context.new_page()
        await asyncio.wait_for(
            page.set_content(html_text, wait_until="load"), timeout=5.0
        )
        locator = page.locator(selector)
        bounds = await asyncio.wait_for(locator.bounding_box(), timeout=5.0)
        if (
            not bounds
            or not 1 <= float(bounds.get("height", 0)) <= _MAX_SCREENSHOT_HEIGHT
            or not 1 <= float(bounds.get("width", 0)) <= _MAX_SCREENSHOT_WIDTH
        ):
            raise ValueError("可视化尺寸无效")
        await asyncio.wait_for(
            locator.screenshot(path=str(temporary_path), animations="disabled"),
            timeout=10.0,
        )
        _validate_png(root, temporary_path)
        os.replace(temporary_path, output_path)
        _validate_png(root, output_path)
        try:
            os.chmod(output_path, 0o600)
        except OSError:
            pass
        return str(output_path.resolve(strict=True))
    finally:
        if context is not None:
            await _bounded_cleanup(context.close, "context")
        if browser is not None:
            await _bounded_cleanup(browser.close, "browser")
        if manager is not None and manager_entered:
            await _bounded_cleanup(
                lambda: manager.__aexit__(None, None, None),
                "playwright",
            )
        temporary_path.unlink(missing_ok=True)
        if output_path.exists():
            try:
                _validate_png(root, output_path)
            except (OSError, ValueError):
                output_path.unlink(missing_ok=True)
        await prune_artifacts(root)


async def _bounded_cleanup(action: Any, label: str) -> None:
    """Run one Playwright cleanup action without letting it hang the renderer."""
    try:
        task = asyncio.create_task(action())
    except Exception:
        logger.debug("创建 Sub2API 可视化 %s 清理任务失败", label, exc_info=True)
        return
    try:
        done, _pending = await asyncio.wait({task}, timeout=_CLEANUP_TIMEOUT_SECONDS)
        if task not in done:
            task.cancel()
            task.add_done_callback(_consume_task_result)
            logger.warning("sub2api_monitor: Playwright %s 清理超时", label)
            return
        await task
    except asyncio.CancelledError:
        task.cancel()
        task.add_done_callback(_consume_task_result)
        raise
    except Exception:
        logger.debug("关闭 Sub2API 可视化 %s 失败", label, exc_info=True)


def _consume_task_result(task: asyncio.Task[Any]) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.debug("Sub2API 可视化清理任务异常: %s", type(exc).__name__)


async def prune_artifacts(output_dir: Path) -> None:
    """Delete stale and oversized artifacts, keeping recent bounded PNG files."""
    root = _artifact_root(output_dir)
    try:
        candidates = [path for path in root.iterdir() if path.is_file()]
    except OSError:
        return
    keep: list[tuple[float, int, Path]] = []
    freed = 0
    for path in candidates:
        try:
            stat = path.stat()
        except OSError:
            continue
        if path.name.endswith(".tmp.png") or not path.name.endswith(".png"):
            freed += stat.st_size
            _quiet_unlink(path)
            continue
        keep.append((stat.st_mtime, stat.st_size, path))
    keep.sort(reverse=True)
    now = time.time()
    total = 0
    survivors: list[tuple[float, int, Path]] = []
    for position, (mtime, size, path) in enumerate(keep):
        expired = now - mtime > _MAX_ARTIFACT_AGE_SECONDS
        overflow = position >= _MAX_ARTIFACT_FILES or total + size > _MAX_ARTIFACT_BYTES
        if expired or overflow:
            freed += size
            _quiet_unlink(path)
            continue
        total += size
        survivors.append((mtime, size, path))
    for path in root.glob("*.tmp.png"):
        _quiet_unlink(path)
    if survivors:
        newest = survivors[0][0]
        if now - newest > _MAX_ARTIFACT_AGE_SECONDS:
            pass  # keep newest regardless; prune never empties the directory
    logger.debug("sub2api_monitor: 产物清理完成，保留 %d 个文件", len(survivors))


def _quiet_unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def validated_artifact_image(output_dir: Path, candidate: str | Path | None) -> str:
    """Return the candidate path when it is a validated PNG inside the sandbox."""
    if not candidate:
        return ""
    try:
        path = Path(candidate)
        root = _artifact_root(output_dir)
        _assert_safe_child(root, path)
        _validate_png(root, path)
        return str(path)
    except (OSError, ValueError):
        return ""


def _get_render_slots() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    slots = _RENDER_SLOTS.get(loop)
    if slots is None:
        slots = asyncio.Semaphore(1)
        _RENDER_SLOTS[loop] = slots
    return slots


def _artifact_root(output_dir: Path) -> Path:
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _assert_safe_child(root: Path, path: Path) -> None:
    resolved_root = root.resolve()
    resolved = path.resolve()
    if resolved_root != resolved and resolved_root not in resolved.parents:
        raise ValueError("可视化产物路径越界")


def _validate_png(root: Path, path: Path) -> None:
    _assert_safe_child(root, path)
    size = path.stat().st_size
    if not 64 <= size <= _MAX_SCREENSHOT_BYTES:
        raise ValueError("可视化 PNG 尺寸无效")
    with path.open("rb") as handle:
        signature = handle.read(8)
    if signature != _PNG_SIGNATURE:
        raise ValueError("可视化 PNG 签名无效")


def _project_record(record: dict[str, Any] | None, event_type: str) -> dict[str, str]:
    if not isinstance(record, dict):
        return {}
    cleaned = redact(record)
    if not isinstance(cleaned, dict):
        return {}
    fields = _RATE_FIELDS if event_type.startswith("rate") else _SUBSCRIPTION_FIELDS
    projected: dict[str, str] = {}
    for key in fields:
        if key not in cleaned:
            continue
        value = cleaned[key]
        if isinstance(value, (dict, list, tuple, set)):
            continue
        projected[key] = _clip(value, 160)
    return projected


def _compare_rows(
    own: dict[str, str],
    other: dict[str, str],
    *,
    changed_side: str,
) -> str:
    if not own:
        return (
            '<div class="prow"><span class="pkey">—</span>'
            '<span class="pval none"></span></div>'
        )
    rows: list[str] = []
    for key, value in list(own.items())[:_MAX_COMPARE_ROWS]:
        changed = other.get(key) != value
        other_missing = key not in other
        if changed_side == "before":
            emphasis = "cut" if changed else ""
        else:
            emphasis = "hot" if changed else ""
        if other_missing and changed_side == "after":
            emphasis = "hot"
        if other_missing and changed_side == "before":
            emphasis = "cut"
        rows.append(
            '<div class="prow">'
            f'<span class="pkey">{_e(_FIELD_LABELS.get(key, key))}</span>'
            f'<span class="pval {_e(emphasis)}">{_e(value)}</span>'
            "</div>"
        )
    return "".join(rows)


def _subject(record: dict[str, str], event_type: str) -> str:
    if not record:
        return _EVENT_LABELS.get(event_type, _FALLBACK_EVENT)[1]
    value = (
        record.get("name")
        or record.get("product_name")
        or record.get("productName")
        or record.get("group_name")
        or record.get("groupName")
        or record.get("group_id")
        or record.get("groupId")
        or record.get("id")
        or "未命名记录"
    )
    return _clip(value, 120)


def _subject_label(record: dict[str, Any]) -> str:
    value = (
        record.get("name")
        or record.get("product_name")
        or record.get("productName")
        or record.get("group_name")
        or record.get("groupName")
        or record.get("slug")
        or record.get("id")
    )
    return _clip(value, 60) if value not in (None, "") else ""


def _primary_rate_value(record: dict[str, Any] | dict[str, str]) -> Any:
    if not isinstance(record, dict):
        return None
    for key in (
        "rate_multiplier",
        "rate",
        "multiplier",
        "model_ratio",
        "completion_ratio",
        "input_ratio",
        "output_ratio",
        "ratio",
        "weight",
        "value",
    ):
        value = record.get(key)
        if value not in (None, ""):
            return value
    return None


def _finite_rate(value: Any) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _fmt_rate(value: float) -> str:
    text = f"{value:g}"
    return text


def _rate_tone(value: float | None) -> str:
    if value is None:
        return "dim"
    if value <= 0.5:
        return "good"
    if value <= 1.5:
        return "moon"
    if value <= 3.0:
        return "warn"
    return "bad"


def _rate_direction(before: dict[str, Any] | None, after: dict[str, Any] | None) -> str:
    old = _finite_rate(_primary_rate_value(before or {}))
    new = _finite_rate(_primary_rate_value(after or {}))
    if old is None or new is None or old == new:
        return "flat"
    return "up" if new > old else "down"


def _price_html(record: dict[str, Any]) -> str:
    raw_price = record.get("price")
    currency = str(record.get("currency") or "").strip().upper()
    symbols = {"CNY": "¥", "RMB": "¥", "USD": "$", "EUR": "€", "JPY": "¥"}
    symbol = symbols.get(currency, "")
    period = (
        record.get("period")
        or record.get("billing_cycle")
        or record.get("billingCycle")
        or record.get("duration")
    )
    price_text = ""
    if raw_price not in (None, ""):
        parsed = _finite_rate(raw_price)
        if parsed is None:
            price_text = _clip(raw_price, 12)
        else:
            amount = f"{parsed:g}" if parsed == int(parsed) else f"{parsed:.2f}"
            price_text = (
                f"{symbol}{amount}"
                if symbol
                else f"{_clip(currency, 4)} {amount}".strip()
            )
    else:
        price_text = "—"
    period_text = f"/ {_clip(period, 10)}" if period else ""
    return (
        f'<div class="val price t-moon">{_e(price_text)}</div>'
        f'<div class="period">{_e(period_text)}</div>'
    )


def _empty_state(title: str, subtitle: str) -> str:
    return (
        '<div class="empty">'
        f"{_icon('layers')}"
        f"<b>{_e(title)}</b>"
        f"<small>{_e(subtitle)}</small>"
        "</div>"
    )


def _timestamp(value: int | None) -> str:
    timestamp = value if isinstance(value, int) else int(time.time())
    try:
        moment = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        moment = datetime.now(tz=timezone.utc)
    return moment.strftime("%Y-%m-%d %H:%M UTC")


def _bounded_nonnegative_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, min(parsed, 9999))


def _document(css: str, body: str) -> str:
    csp = (
        "default-src 'none'; style-src 'unsafe-inline'; img-src 'none'; font-src 'none'"
    )
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        f'<meta http-equiv="Content-Security-Policy" content="{csp}">'
        f"<style>{css}</style></head><body>{body}</body></html>"
    )


def _clip(value: Any, limit: int) -> str:
    text = str(value if value is not None else "")
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def _safe_slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "", str(value))[:32] or "source"


def _e(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)
