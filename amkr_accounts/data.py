"""AMKR 资源选择与面向群聊的白名单投影；原始响应不进入图片或持久化。"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .client import AMKRAccountError

_CST = timezone(timedelta(hours=8))
_EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
_SECRET = re.compile(
    r"(?i)\b(?:amkr_[\w-]+|sk-[\w-]+|gh[pousr]_[\w-]+|eyJ[\w-]+(?:\.[\w-]+){1,2})\b"
)
_URL = re.compile(r"(?i)https?://\S+")


def safe_text(value: Any, secrets: tuple[str, ...] = ()) -> str:
    """清除控制符、密钥、地址，并掩码邮箱（含文件名中的邮箱）。"""
    text = "".join(c for c in str(value if value is not None else "") if c.isprintable())
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[已隐藏]")
    text = _URL.sub("[地址已隐藏]", text)
    text = _SECRET.sub("[密钥已隐藏]", text)
    text = _EMAIL.sub(lambda m: m.group()[0] + "***@" + m.group().split("@", 1)[1], text)
    return text.strip()[:120]


def finite_number(value: Any) -> float | None:
    """布尔值、缺失值和非有限数不构成用量。"""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def local_time(value: Any, offset_ms: float = 0, *, full: bool = False) -> str:
    """RFC3339 转中国标准时间；CPA 的重置时刻扣除其服务器时差。"""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return "未知"
        parsed -= timedelta(milliseconds=offset_ms)
        return parsed.astimezone(_CST).strftime("%Y-%m-%d %H:%M:%S" if full else "%m/%d %H:%M")
    except (ValueError, TypeError, OverflowError):
        return "未知"


@dataclass
class TrendPoint:
    start: str
    requests: int
    failures: int


@dataclass
class ChannelHealth:
    label: str
    requests: int
    successes: int
    failures: int
    success_rate: float | None
    avg_ttft_ms: float | None
    cache_rate: float | None
    health: int | None
    band: str


@dataclass
class StatusSnapshot:
    hours: float
    window_from: str
    window_to: str
    bucket_seconds: int
    requests: int
    successes: int
    failures: int
    success_rate: float | None
    avg_ttft_ms: float | None
    cache_rate: float | None
    current_rpm: float | None
    health: int | None
    health_band: str
    trend: list[TrendPoint]
    channels: list[ChannelHealth]
    rows_truncated: int
    errors: list[tuple[str, int]]


@dataclass
class QuotaWindow:
    label: str
    remaining: float | None
    reset: str
    source: str


@dataclass
class Account:
    id: str
    name: str
    source: str
    provider: str
    plan: str
    status: str
    kind: str
    observed: str
    success: int | None = None
    failed: int | None = None
    warning: str = ""
    windows: list[QuotaWindow] = field(default_factory=list)
    summary: list[str] = field(default_factory=list)

    @property
    def lowest(self) -> float | None:
        values = [w.remaining for w in self.windows if w.remaining is not None]
        return min(values) if values else None


def _count(value: Any) -> int:
    number = finite_number(value)
    return 0 if number is None or number < 0 else min(int(number), 10**15)


def _health_score(
    requests: int,
    success_rate: float | None,
    avg_ttft_ms: float | None,
    cache_rate: float | None,
) -> int | None:
    """与 AMKR 状态监控页同一口径：成功率 60% + 首字 20% + 缓存 20%。"""
    if requests < 3:
        return None
    success = (success_rate or 0) * 100
    if avg_ttft_ms is None:
        ttft = 0.0
    elif avg_ttft_ms <= 1500:
        ttft = 100.0
    elif avg_ttft_ms >= 8000:
        ttft = 0.0
    else:
        ttft = 100 * (8000 - avg_ttft_ms) / 6500
    cache = 0.0 if cache_rate is None else min(cache_rate / 0.3, 1) * 100
    return round(0.6 * success + 0.2 * ttft + 0.2 * cache)


def _health_band(requests: int, health: int | None) -> str:
    if requests <= 0:
        return "no-traffic"
    if requests < 3 or health is None:
        return "unknown"
    return "good" if health >= 80 else "warn" if health >= 50 else "bad"


def _cell_totals(cells: list[dict]) -> tuple[int, int, int, int, int, float]:
    requests = successes = failures = prompt = cached = total_first = 0
    for cell in cells:
        request_count = _count(cell.get("requests"))
        requests += request_count
        successes += _count(cell.get("successes"))
        failures += _count(cell.get("failures"))
        prompt += _count(cell.get("prompt_tokens"))
        cached += _count(cell.get("cached_tokens"))
        total_first += _count(cell.get("total_first_token_ms"))
    if failures == 0:
        failures = max(0, requests - successes)
    return requests, successes, failures, prompt, cached, total_first


def project_status(payload: dict, hours: float, secrets: tuple[str, ...] = ()) -> StatusSnapshot:
    """把状态监控响应投影成群聊需要的最少字段，并按 AMKR 同一口径计算健康分。"""
    snapshot = payload.get("snapshot")
    series = payload.get("series")
    matrix = payload.get("matrix")
    if (
        not isinstance(snapshot, dict)
        or not isinstance(series, dict)
        or not isinstance(matrix, dict)
    ):
        raise AMKRAccountError("AMKR 状态监控响应格式错误，请检查服务版本。")
    total = snapshot.get("total")
    if not isinstance(total, dict):
        raise AMKRAccountError("AMKR 状态监控响应格式错误，请检查服务版本。")
    points = series.get("points")
    rows = matrix.get("rows")
    if not isinstance(points, list) or not isinstance(rows, list):
        raise AMKRAccountError("AMKR 状态监控响应格式错误，请检查服务版本。")
    if len(points) > 500 or len(rows) > 60:
        raise AMKRAccountError("AMKR 状态监控数据超过单张图片安全上限，请缩小时间范围。")

    requests = _count(total.get("requests"))
    successes = min(_count(total.get("successes")), requests)
    failures = _count(total.get("failures"))
    if failures == 0:
        failures = max(0, requests - successes)
    prompt_tokens = _count(total.get("prompt_tokens"))
    cached_tokens = _count(total.get("cached_tokens"))
    total_first = _count(total.get("total_first_token_ms"))
    success_rate = successes / requests if requests else None
    avg_ttft = total_first / requests if requests and total_first > 0 else None
    cache_rate = cached_tokens / prompt_tokens if prompt_tokens else None
    health = _health_score(requests, success_rate, avg_ttft, cache_rate)
    band = _health_band(requests, health)

    window = payload.get("window") or snapshot.get("window")
    if not isinstance(window, dict):
        window = {}
    window_hours = finite_number(window.get("hours"))
    bucket = _count(payload.get("bucket_seconds") or series.get("bucket_seconds"))

    trend: list[TrendPoint] = []
    errors: dict[str, int] = {}
    for point in points:
        if not isinstance(point, dict):
            raise AMKRAccountError("AMKR 状态监控响应格式错误，请检查服务版本。")
        trend.append(
            TrendPoint(
                start=str(point.get("started_at") or ""),
                requests=_count(point.get("requests")),
                failures=_count(point.get("failures")),
            )
        )
        codes = point.get("status_codes")
        if isinstance(codes, dict):
            for code, count in codes.items():
                label = safe_text(code, secrets)
                if label[:1] != "2":
                    errors[label] = errors.get(label, 0) + _count(count)

    channels: list[ChannelHealth] = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("cells"), list):
            raise AMKRAccountError("AMKR 状态监控响应格式错误，请检查服务版本。")
        cells = row["cells"]
        if any(not isinstance(cell, dict) for cell in cells):
            raise AMKRAccountError("AMKR 状态监控响应格式错误，请检查服务版本。")
        row_requests, row_successes, row_failures, row_prompt, row_cached, row_first = _cell_totals(
            cells
        )
        row_rate = row_successes / row_requests if row_requests else None
        row_ttft = row_first / row_requests if row_requests and row_first > 0 else None
        row_cache = row_cached / row_prompt if row_prompt else None
        row_health = _health_score(row_requests, row_rate, row_ttft, row_cache)
        channels.append(
            ChannelHealth(
                label=safe_text(row.get("provider_id") or "未归属", secrets),
                requests=row_requests,
                successes=row_successes,
                failures=row_failures,
                success_rate=row_rate,
                avg_ttft_ms=row_ttft,
                cache_rate=row_cache,
                health=row_health,
                band=_health_band(row_requests, row_health),
            )
        )

    return StatusSnapshot(
        hours=window_hours if window_hours and window_hours > 0 else hours,
        window_from=str(window.get("from") or ""),
        window_to=str(window.get("to") or ""),
        bucket_seconds=bucket,
        requests=requests,
        successes=successes,
        failures=failures,
        success_rate=success_rate,
        avg_ttft_ms=avg_ttft,
        cache_rate=cache_rate,
        current_rpm=finite_number(snapshot.get("current_rpm")),
        health=health,
        health_band=band,
        trend=trend,
        channels=channels,
        rows_truncated=_count(matrix.get("rows_truncated")),
        errors=sorted(errors.items(), key=lambda item: (-item[1], item[0]))[:5],
    )


def _records(value: Any) -> list[dict]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise AMKRAccountError("AMKR 账号资源响应格式错误，请检查服务版本。")
    if len(value) > 2000:
        raise AMKRAccountError("AMKR 单类资源超过 2000 条安全上限。")
    return value


def project_accounts(payload: dict, selector: str, secrets: tuple[str, ...] = ()) -> list[Account]:
    """按账号 ID / 唯一名称或 CPA 实例筛选，然后仅保留可展示字段。"""
    instances = _records(payload.get("instances"))
    subscriptions = _records(payload.get("subscriptions"))
    candidates: list[tuple[dict, dict, str, str]] = []
    for instance in instances:
        accounts = _records(instance.get("accounts"))
        if not accounts:
            candidates.append((instance, instance, "instance", str(instance.get("id", ""))))
        for account in accounts:
            index = account.get("auth_index")
            account_id = str(index) if index not in (None, "") else str(account.get("name", ""))
            candidates.append((account, instance, "cpa", f"{instance.get('id', '')}/{account_id}"))
    for subscription in subscriptions:
        candidates.append(
            (
                subscription,
                subscription,
                "subscription",
                str(subscription.get("id", "")),
            )
        )
    if len(candidates) > 2000:
        raise AMKRAccountError("AMKR 资源超过 2000 条安全上限。")
    query = selector.strip().casefold()
    if query and query != "all":
        matches = [item for item in candidates if query == item[3].casefold()]
        if not matches:
            matches = [
                item
                for item in candidates
                if query
                in {
                    str(item[0].get(key) or "").casefold()
                    for key in ("name", "label", "email", "account", "key_name")
                }
            ]
        if len(matches) > 1:
            ids = "、".join(safe_text(item[3], secrets) for item in matches[:6])
            raise AMKRAccountError(f"账号名称不唯一，请使用完整 ID：{ids}")
        if not matches:
            matches = [
                item
                for item in candidates
                if item[2] != "subscription"
                and query
                in {
                    str(item[1].get("id", "")).casefold(),
                    str(item[1].get("label", "")).casefold(),
                }
            ]
        if not matches:
            raise AMKRAccountError("未找到指定账号或 CPA 实例，请用 /amkr account 查看名称和完整 ID。")
        candidates = matches

    def clean(value: Any) -> str:
        return safe_text(value, secrets)

    result: list[Account] = []
    for raw, source, kind, identifier in candidates:
        is_cpa = kind == "cpa"
        if kind == "instance":
            status = "暂无账号" if raw.get("ok") is True else "读取失败"
        elif not is_cpa and raw.get("ok") is not True:
            status = "读取失败"
        elif raw.get("disabled") is True:
            status = "已停用"
        elif raw.get("unavailable") is True:
            status = "不可用"
        else:
            status = {"active": "可用", "ready": "可用", "ok": "可用", "": "状态未知"}.get(
                str(raw.get("status") or "").casefold(), clean(raw.get("status"))
            )
        account = Account(
            id=clean(identifier),
            name=clean(
                raw.get("label")
                or raw.get("name")
                or raw.get("account")
                or raw.get("key_name")
                or identifier
            ),
            source=clean(source.get("label") or source.get("provider_id") or source.get("id")),
            provider=clean(raw.get("provider") or raw.get("vendor") or raw.get("kind") or "CPA"),
            plan=clean(raw.get("plan") or raw.get("tier_id") or "未提供套餐"),
            status=status,
            kind=kind,
            observed=local_time(source.get("observed_at")),
        )
        if is_cpa:
            for key in ("success", "failed"):
                number = finite_number(raw.get(key))
                if number is not None and 0 <= number <= 10**15:
                    setattr(account, key, int(number))
        offset = finite_number(raw.get("server_time_offset_ms")) or 0
        windows = [(None, w) for w in _records(raw.get("windows", []))]
        models = raw.get("model_quotas", {})
        if not isinstance(models, dict):
            raise AMKRAccountError("AMKR 模型额度格式错误。")
        for model, quota in models.items():
            if not isinstance(quota, dict):
                raise AMKRAccountError("AMKR 模型额度格式错误。")
            windows.extend((model, w) for w in _records(quota.get("windows", [])))
        if len(windows) > 128:
            raise AMKRAccountError("单个账号的额度窗口超过 128 个，请在 AMKR 查看详情。")
        for model, window in windows:
            remaining = finite_number(window.get("remaining"))
            if remaining is not None and not 0 <= remaining <= 1:
                remaining = None
            label = " / ".join(
                clean(v)
                for v in (
                    model,
                    window.get("group"),
                    window.get("label") or window.get("window"),
                )
                if v
            )
            account.windows.append(
                QuotaWindow(
                    label=label or "额度窗口",
                    remaining=remaining,
                    reset=local_time(window.get("reset_at"), offset),
                    source={"passive": "被动快照", "quota": "现场查询"}.get(window.get("source"), "未知来源"),
                )
            )
        for metric in _records(raw.get("summary", [])):
            number = finite_number(metric.get("value"))
            if number is not None:
                unit = " ".join(
                    dict.fromkeys(
                        clean(value)
                        for value in (metric.get("currency"), metric.get("unit"))
                        if value
                    )
                )
                value = str(metric["value"])
                account.summary.append(
                    f"{clean(metric.get('label') or metric.get('key'))}  {value} {unit}"
                )
        if raw.get("quota_error"):
            account.warning = "额度查询失败，请在 AMKR 检查额度提供者；缺失额度不代表耗尽。"
        if isinstance(raw.get("signals"), dict) and raw["signals"].get("读取失败"):
            account.warning = "部分用量读取失败，请在 AMKR 查看详情。"
        if status == "读取失败":
            account.warning = "资源读取失败，请在 AMKR 检查该实例或订阅的连接与凭据。"
        result.append(account)
    return result
