"""Safe, local-only Playwright visualizations for the Sub2API monitor."""

from __future__ import annotations

import asyncio
import html
import logging
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

_EVENT_LABELS = {
    "subscription_added": ("SUBSCRIPTION / ONLINE", "订阅上架", "positive"),
    "subscription_removed": ("SUBSCRIPTION / OFFLINE", "订阅下架", "danger"),
    "subscription_changed": ("SUBSCRIPTION / REVISED", "订阅更新", "warning"),
    "rate_added": ("RATE / ONLINE", "倍率新增", "positive"),
    "rate_removed": ("RATE / OFFLINE", "倍率移除", "danger"),
    "rate_changed": ("RATE / SHIFT", "倍率变化", "warning"),
}
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
    "rate_multiplier": "主倍率",
    "rate": "倍率",
    "multiplier": "乘数",
    "model_ratio": "模型倍率",
    "completion_ratio": "输出倍率",
    "input_ratio": "输入倍率",
    "output_ratio": "输出倍率",
    "ratio": "比率",
    "weight": "权重",
    "value": "值",
    "peak_rate_enabled": "峰值倍率",
    "peak_start": "峰值开始",
    "peak_end": "峰值结束",
}

_BASE_CSS = """
:root {
  --ink: #071b2b;
  --deep: #0b2f48;
  --ocean: #124f6a;
  --ice: #eaf7fa;
  --paper: #fbfeff;
  --line: #bdd6df;
  --muted: #668391;
  --mint: #23b6a7;
  --amber: #f4b942;
  --coral: #ef6a67;
}
* { box-sizing: border-box; }
html, body { margin: 0; background: transparent; color: var(--ink); }
body {
  padding: 20px;
  font-family: "Microsoft YaHei", "PingFang SC", ui-sans-serif, sans-serif;
}
"""

_CHANGE_CSS = (
    _BASE_CSS
    + """
#sub2api-card {
  width: 1120px;
  min-height: 650px;
  display: grid;
  grid-template-columns: 184px 1fr;
  overflow: hidden;
  background: var(--paper);
  border: 1px solid #8db4c3;
  box-shadow: 0 22px 60px rgba(4, 32, 50, 0.22);
}
.rail {
  position: relative;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  padding: 34px 24px;
  overflow: hidden;
  background: var(--deep);
  color: #fff;
}
.rail::after {
  position: absolute;
  inset: 0;
  content: "";
  background: repeating-linear-gradient(
    0deg,
    transparent 0 37px,
    rgba(255, 255, 255, 0.045) 38px 39px
  );
}
.brand, .source, .pulse, .rail-foot { position: relative; z-index: 1; }
.brand {
  color: #9ed8e2;
  font: 700 12px/1.3 ui-monospace, monospace;
  letter-spacing: 0.18em;
}
.source {
  max-height: 330px;
  overflow: hidden;
  font-size: 31px;
  font-weight: 800;
  line-height: 1.05;
  letter-spacing: 0.06em;
  text-overflow: ellipsis;
  writing-mode: vertical-rl;
  transform: rotate(180deg);
}
.source small {
  margin-inline-start: 13px;
  color: #8fc5d1;
  font: 600 11px/1.2 ui-monospace, monospace;
  letter-spacing: 0.12em;
}
.pulse {
  height: 62px;
  display: flex;
  align-items: center;
  gap: 5px;
  border-block: 1px solid rgba(255, 255, 255, 0.18);
}
.pulse i { width: 5px; display: block; background: var(--mint); }
.pulse i:nth-child(3n + 1) { height: 13px; }
.pulse i:nth-child(3n + 2) { height: 31px; }
.pulse i:nth-child(3n) { height: 20px; }
.pulse i:nth-child(5) { height: 44px; background: var(--amber); }
.rail-foot {
  color: #8fc5d1;
  font: 600 10px/1.55 ui-monospace, monospace;
  letter-spacing: 0.1em;
}
.main {
  display: flex;
  flex-direction: column;
  gap: 28px;
  padding: 42px 48px 38px;
  background: linear-gradient(130deg, rgba(234, 247, 250, 0.58), transparent 46%);
}
.header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 30px;
  padding-bottom: 25px;
  border-bottom: 2px solid var(--ink);
}
.eyebrow {
  color: var(--ocean);
  font: 700 11px/1.4 ui-monospace, monospace;
  letter-spacing: 0.16em;
}
h1 {
  max-width: 650px;
  margin: 9px 0 0;
  overflow-wrap: anywhere;
  font-size: 42px;
  line-height: 1.06;
  letter-spacing: -0.045em;
}
.stamp {
  color: var(--muted);
  font: 600 11px/1.65 ui-monospace, monospace;
  text-align: right;
  white-space: nowrap;
}
.badge {
  display: inline-block;
  margin-top: 8px;
  padding: 7px 11px;
  border: 1px solid var(--ink);
  color: var(--ink);
  font: 800 11px/1 ui-monospace, monospace;
  letter-spacing: 0.09em;
}
.badge.positive { background: var(--mint); }
.badge.warning { background: var(--amber); }
.badge.danger { background: var(--coral); }
.compare {
  display: grid;
  grid-template-columns: 1fr 44px 1fr;
  align-items: stretch;
}
.panel {
  min-height: 330px;
  padding: 22px 24px;
  border: 1px solid var(--line);
  background: #fff;
}
.panel.after { border-color: #6fa9b6; background: var(--ice); }
.panel-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 17px;
  color: var(--muted);
  font: 800 11px/1.2 ui-monospace, monospace;
  letter-spacing: 0.13em;
}
.arrow {
  display: grid;
  place-items: center;
  color: var(--ocean);
  font: 700 25px/1 ui-monospace, monospace;
}
.row {
  display: grid;
  grid-template-columns: 120px 1fr;
  gap: 14px;
  padding: 10px 0;
  border-top: 1px solid #d8e8ed;
  font-size: 14px;
  line-height: 1.4;
}
.row:first-of-type { border-top: 0; }
.key {
  overflow: hidden;
  color: var(--muted);
  font: 700 11px/1.4 ui-monospace, monospace;
  text-overflow: ellipsis;
}
.value { overflow-wrap: anywhere; font-weight: 650; }
.empty {
  height: 220px;
  display: grid;
  place-items: center;
  border: 1px dashed var(--line);
  color: var(--muted);
  font: 700 12px/1.5 ui-monospace, monospace;
  letter-spacing: 0.08em;
}
.footer {
  display: flex;
  justify-content: space-between;
  gap: 25px;
  color: var(--muted);
  font: 600 10px/1.4 ui-monospace, monospace;
  letter-spacing: 0.08em;
}
"""
)

_DASHBOARD_CSS = (
    _BASE_CSS
    + """
#sub2api-dashboard {
  width: 1180px;
  overflow: hidden;
  background: var(--paper);
  border: 1px solid #8db4c3;
  box-shadow: 0 22px 60px rgba(4, 32, 50, 0.22);
}
.top {
  display: grid;
  grid-template-columns: 1fr auto;
  align-items: end;
  gap: 30px;
  padding: 32px 38px;
  background: var(--deep);
  color: #fff;
}
.kicker {
  color: #9ed8e2;
  font: 700 11px/1.4 ui-monospace, monospace;
  letter-spacing: 0.18em;
}
h1 { margin: 10px 0 0; font-size: 38px; line-height: 1; letter-spacing: -0.04em; }
.meta {
  color: #a8d1da;
  font: 600 11px/1.6 ui-monospace, monospace;
  text-align: right;
}
.grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 18px;
  padding: 28px;
  background: linear-gradient(135deg, var(--ice), #fff 38%);
}
.card {
  --accent: var(--amber);
  position: relative;
  min-height: 255px;
  overflow: hidden;
  padding: 21px;
  border: 1px solid var(--line);
  background: #fff;
}
.card::before {
  position: absolute;
  inset: 0 auto 0 0;
  width: 7px;
  content: "";
  background: var(--accent);
}
.card.online { --accent: var(--mint); }
.card.degraded { --accent: var(--coral); }
.card-head {
  display: flex;
  justify-content: space-between;
  gap: 18px;
  margin-bottom: 15px;
  padding-bottom: 14px;
  border-bottom: 1px solid #d8e8ed;
}
.name { overflow-wrap: anywhere; font-size: 22px; font-weight: 800; line-height: 1.1; }
.sid {
  color: var(--muted);
  font: 700 10px/1.4 ui-monospace, monospace;
  letter-spacing: 0.12em;
}
.state {
  height: max-content;
  padding: 6px 8px;
  border: 1px solid var(--ink);
  background: var(--accent);
  font: 800 10px/1 ui-monospace, monospace;
}
.metrics { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }
.metric { padding: 12px 10px; border-top: 2px solid var(--deep); background: var(--ice); }
.metric b { display: block; font: 800 26px/1 ui-monospace, monospace; }
.metric span { display: block; margin-top: 7px; color: var(--muted); font-size: 11px; }
.rates { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 14px; }
.rate {
  padding: 6px 8px;
  border: 1px solid var(--line);
  background: #fff;
  font: 650 10px/1.25 ui-monospace, monospace;
}
.alert {
  margin-top: 14px;
  padding: 9px 10px;
  border-left: 3px solid var(--coral);
  background: #fff0ef;
  color: #713b39;
  font-size: 11px;
  line-height: 1.45;
}
.quiet {
  margin-top: 14px;
  color: var(--muted);
  font: 600 10px/1.4 ui-monospace, monospace;
}
.foot {
  display: flex;
  justify-content: space-between;
  gap: 24px;
  padding: 14px 38px;
  border-top: 1px solid var(--line);
  color: var(--muted);
  font: 600 10px/1.4 ui-monospace, monospace;
  letter-spacing: 0.08em;
}
"""
)


def build_change_card_html(
    *,
    source_id: str,
    display_name: str,
    event_type: str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    occurred_at: int | None = None,
) -> str:
    """Build an escaped change card containing only approved monitor fields."""
    code, title, tone = _EVENT_LABELS.get(
        event_type, ("MONITOR / CHANGE", "配置变化", "warning")
    )
    before_clean = _project_record(before, event_type)
    after_clean = _project_record(after, event_type)
    record = after_clean or before_clean
    before_rows = _record_rows(before_clean, empty_text="尚未存在")
    after_rows = _record_rows(after_clean, empty_text="已移除")
    pulse = "".join("<i></i>" for _ in range(12))
    body = (
        '<article id="sub2api-card">'
        '<aside class="rail">'
        '<div class="brand">SIRIUS / SUB2API</div>'
        f'<div class="source">{_e(_clip(display_name, 64))}'
        f"<small>{_e(_clip(source_id.upper(), 32))}</small></div>"
        f'<div class="pulse">{pulse}</div>'
        '<div class="rail-foot">SOURCE SIGNAL<br>CHANGE CAPTURE</div>'
        "</aside>"
        '<main class="main">'
        '<header class="header"><div>'
        f'<div class="eyebrow">{_e(code)}</div>'
        f"<h1>{_e(_subject(record, event_type))}</h1>"
        '</div><div class="stamp">CAPTURED<br>'
        f"{_e(_timestamp(occurred_at))}<br>"
        f'<span class="badge {tone}">{_e(title)}</span>'
        "</div></header>"
        '<section class="compare">'
        '<div class="panel"><div class="panel-title">'
        "<span>BEFORE / 变更前</span><span>01</span>"
        f"</div>{before_rows}</div>"
        '<div class="arrow">→</div>'
        '<div class="panel after"><div class="panel-title">'
        "<span>AFTER / 变更后</span><span>02</span>"
        f"</div>{after_rows}</div>"
        "</section>"
        '<footer class="footer">'
        "<span>ENV CREDENTIALS · SNAPSHOT DIFF · DELIVERY ACK</span>"
        "<span>APPROVED FIELDS ONLY</span>"
        "</footer></main></article>"
    )
    return _document(_CHANGE_CSS, body)


def build_dashboard_html(
    sources: list[dict[str, Any]], *, generated_at: int | None = None
) -> str:
    """Build a bounded overview dashboard for up to ten monitor sources."""
    visible = sources[:10]
    omitted = max(0, len(sources) - len(visible))
    cards = "".join(_dashboard_source_card(source) for source in visible)
    if not cards:
        cards = '<article class="card"><div class="name">尚无站点快照</div></article>'
    omitted_text = f" · 另有 {omitted} 个站点未展开" if omitted else ""
    body = (
        '<article id="sub2api-dashboard">'
        '<header class="top"><div>'
        '<div class="kicker">SIRIUS PULSE / MULTI-SOURCE TELEMETRY</div>'
        "<h1>Sub2API 站点运行图</h1>"
        '</div><div class="meta">'
        f"{len(sources)} SOURCES<br>{_e(_timestamp(generated_at))}"
        "</div></header>"
        f'<section class="grid">{cards}</section>'
        '<footer class="foot">'
        "<span>ISOLATED ACCOUNTS · ISOLATED SNAPSHOTS</span>"
        f"<span>LOCAL PLAYWRIGHT RENDER{_e(omitted_text)}</span>"
        "</footer></article>"
    )
    return _document(_DASHBOARD_CSS, body)


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


async def render_dashboard(
    sources: list[dict[str, Any]],
    *,
    artifact_dir: Path,
    generated_at: int | None = None,
) -> str | None:
    """Render a multi-source overview card."""
    return await _render_html(
        build_dashboard_html(sources, generated_at=generated_at),
        selector="#sub2api-dashboard",
        artifact_dir=Path(artifact_dir),
        filename_prefix="sub2api_dashboard",
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
            viewport={"width": 1220, "height": 1000},
            device_scale_factor=1,
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
    try:
        task.exception()
    except (asyncio.CancelledError, Exception):
        pass


async def prune_artifacts(output_dir: Path) -> None:
    """Bound only this plugin's generated PNG artifacts by age, count, and bytes."""
    try:
        root = _artifact_root(output_dir)
        now = time.time()
        for temporary in root.glob("sub2api_*.tmp.png"):
            if temporary.is_symlink() or temporary.is_file():
                temporary.unlink(missing_ok=True)
        files: list[tuple[Path, os.stat_result]] = []
        for path in root.glob("sub2api_*.png"):
            if path.is_symlink():
                path.unlink(missing_ok=True)
                continue
            if not path.is_file():
                continue
            _assert_safe_child(root, path)
            files.append((path, path.stat()))
        files.sort(key=lambda item: item[1].st_mtime, reverse=True)
        kept = 0
        total = 0
        for path, stat in files:
            expired = now - stat.st_mtime > _MAX_ARTIFACT_AGE_SECONDS
            exceeds_budget = (
                kept >= _MAX_ARTIFACT_FILES
                or total + stat.st_size > _MAX_ARTIFACT_BYTES
            )
            if expired or exceeds_budget:
                path.unlink(missing_ok=True)
                continue
            kept += 1
            total += stat.st_size
    except (OSError, ValueError):
        logger.debug("sub2api_monitor: 清理可视化 artifact 失败", exc_info=True)


# Backward-compatible private alias used by focused tests and callers.
_prune_artifacts = prune_artifacts


def validated_artifact_image(output_dir: Path, candidate: str | Path | None) -> str:
    """Return a validated absolute PNG path or an empty string."""
    if not candidate:
        return ""
    try:
        root = _artifact_root(Path(output_dir))
        path = Path(candidate)
        if not path.is_absolute():
            return ""
        _validate_png(root, path)
        return str(path.resolve(strict=True))
    except (OSError, ValueError):
        logger.warning("sub2api_monitor: 拒绝不安全的可视化图片路径")
        return ""


def _get_render_slots() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    slots = _RENDER_SLOTS.get(loop)
    if slots is None:
        slots = asyncio.Semaphore(1)
        _RENDER_SLOTS[loop] = slots
    return slots


def _artifact_root(output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    if output_dir.is_symlink() or not output_dir.is_dir():
        raise ValueError("artifact 目录无效")
    return output_dir.resolve(strict=True)


def _assert_safe_child(root: Path, path: Path) -> None:
    resolved = path.resolve(strict=False)
    if resolved.parent != root or path.is_symlink():
        raise ValueError("artifact 路径越界")


def _validate_png(root: Path, path: Path) -> None:
    _assert_safe_child(root, path)
    if not path.is_file() or path.is_symlink():
        raise ValueError("可视化不是普通文件")
    size = path.stat().st_size
    if not 8 <= size <= _MAX_SCREENSHOT_BYTES:
        raise ValueError("可视化图片大小无效")
    with path.open("rb") as file:
        if file.read(8) != _PNG_SIGNATURE:
            raise ValueError("可视化图片格式无效")


def _dashboard_source_card(source: dict[str, Any]) -> str:
    name = _e(_clip(source.get("display_name") or source.get("id") or "Sub2API", 64))
    source_id = _e(_clip(source.get("id") or "source", 32).upper())
    subscriptions = source.get("subscriptions")
    rates = source.get("rates")
    subscription_count = len(subscriptions) if isinstance(subscriptions, list) else 0
    rate_count = len(rates) if isinstance(rates, list) else 0
    has_error = bool(source.get("error"))
    ready = bool(source.get("ready"))
    state = (
        "online" if ready and not has_error else "degraded" if has_error else "waiting"
    )
    state_text = {"online": "ONLINE", "degraded": "DEGRADED", "waiting": "WAITING"}[
        state
    ]
    rate_items: list[str] = []
    if isinstance(rates, list):
        for item in rates[:6]:
            projected = _project_record(
                item if isinstance(item, dict) else None, "rate_changed"
            )
            label = (
                projected.get("group_name")
                or projected.get("groupName")
                or projected.get("name")
                or projected.get("id")
                or "group"
            )
            value = _primary_rate_value(projected)
            rate_items.append(
                f'<span class="rate">{_e(_clip(label, 48))} · {_e(_clip(value, 32))}</span>'
            )
    rates_html = "".join(rate_items) or '<span class="rate">暂无倍率快照</span>'
    alert_html = '<div class="alert">最近一次轮询存在错误，请查看文字状态。</div>' if has_error else ""
    last_success = _timestamp_or_waiting(source.get("last_success"))
    pending = _bounded_nonnegative_int(source.get("pending_acks"))
    return (
        f'<article class="card {state}">'
        '<div class="card-head"><div>'
        f'<div class="name">{name}</div>'
        f'<div class="sid">SOURCE / {source_id}</div>'
        f'</div><span class="state">{state_text}</span></div>'
        '<div class="metrics">'
        f'<div class="metric"><b>{subscription_count}</b><span>可售订阅</span></div>'
        f'<div class="metric"><b>{rate_count}</b><span>倍率分组</span></div>'
        f'<div class="metric"><b>{pending}</b><span>待确认事件</span></div>'
        "</div>"
        f'<div class="rates">{rates_html}</div>{alert_html}'
        f'<div class="quiet">LAST SUCCESS / {_e(last_success)}</div>'
        "</article>"
    )


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


def _record_rows(record: dict[str, str], *, empty_text: str) -> str:
    if not record:
        return f'<div class="empty">{_e(empty_text)}</div>'
    rows = []
    for key, value in list(record.items())[:12]:
        label = _FIELD_LABELS.get(key, key)
        rows.append(
            '<div class="row">'
            f'<span class="key">{_e(label)}</span>'
            f'<span class="value">{_e(value)}</span>'
            "</div>"
        )
    return "".join(rows)


def _subject(record: dict[str, str], event_type: str) -> str:
    if not record:
        return _EVENT_LABELS.get(event_type, ("", "配置变化", ""))[1]
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


def _primary_rate_value(record: dict[str, str]) -> str:
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
        if record.get(key):
            return record[key]
    return "—"


def _timestamp(value: int | None) -> str:
    timestamp = value if isinstance(value, int) else int(time.time())
    try:
        moment = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        moment = datetime.now(tz=timezone.utc)
    return moment.strftime("%Y-%m-%d %H:%M:%S UTC")


def _timestamp_or_waiting(value: Any) -> str:
    if isinstance(value, bool):
        return "尚未成功"
    try:
        timestamp = int(value)
    except (TypeError, ValueError, OverflowError):
        return "尚未成功"
    return _timestamp(timestamp)


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
