"""Normalization and snapshot comparison helpers for Sub2API responses."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_SECRET_KEYS = {
    "access_token",
    "api_key",
    "authorization",
    "key",
    "password",
    "refresh_token",
    "secret",
    "token",
}
_COLLECTION_KEYS = {
    "subscriptions": ("plans", "subscriptions", "items", "products"),
    "group_rates": ("groups", "rates", "items"),
}
_RATE_FIELDS = (
    "rate_multiplier",
    "multiplier",
    "model_ratio",
    "completion_ratio",
    "input_ratio",
    "output_ratio",
    "ratio",
    "weight",
    "rate",
    "value",
)
_SECRET_SUFFIXES = (
    "_token",
    "_tokens",
    "_key",
    "_keys",
    "_secret",
    "_secrets",
    "_password",
    "_passwords",
)
_RATE_EXCLUDED_MARKERS = (
    "limit",
    "quota",
    "capacity",
    "concurr",
    "rpm",
    "tpm",
    "rpd",
    "tpd",
)
_RATE_BOOLEAN_FIELDS = {"peak_rate_enabled"}


class DataNormalizationError(ValueError):
    """Raised when an upstream response cannot be safely snapshotted."""


@dataclass(slots=True)
class PollResult:
    """Observable result of one plugin poll."""

    subscription_added: int = 0
    subscription_removed: int = 0
    subscription_changed: int = 0
    rates_added: int = 0
    rates_removed: int = 0
    rates_changed: int = 0
    initialized: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    notifications_sent: int = 0

    @property
    def change_count(self) -> int:
        """Return the total number of detected changes."""
        return sum(
            (
                self.subscription_added,
                self.subscription_removed,
                self.subscription_changed,
                self.rates_added,
                self.rates_removed,
                self.rates_changed,
            )
        )


@dataclass(frozen=True, slots=True)
class RecordChange:
    """A record that exists in both snapshots but changed content."""

    before: dict[str, Any]
    after: dict[str, Any]


def normalize_subscriptions(payload: Any) -> list[dict[str, Any]]:
    """Normalize common checkout/subscription response shapes."""
    data = _unwrap_data(payload)
    if isinstance(data, dict):
        records: list[Any] | None = None
        for key in _COLLECTION_KEYS["subscriptions"]:
            collection = data.get(key)
            if isinstance(collection, (list, dict)):
                records = _extract_records(data, (key,))
                break
        if records is None:
            raise DataNormalizationError("订阅响应缺少有效列表")
    elif isinstance(data, list):
        records = data
    else:
        raise DataNormalizationError("订阅响应不是列表或映射")
    return _normalize_records(records, kind="subscription")


def normalize_group_rates(payload: Any) -> list[dict[str, Any]]:
    """Normalize list, wrapped-list, and id-to-rate map response shapes."""
    data = _unwrap_data(payload)
    records: list[Any]
    if isinstance(data, dict):
        records = []
        for wrapper_key in _COLLECTION_KEYS["group_rates"]:
            if wrapper_key not in data:
                continue
            collection = data[wrapper_key]
            if isinstance(collection, (list, dict)):
                records = _extract_records(
                    data,
                    (wrapper_key,),
                    allow_scalar_map_values=True,
                )
                break
        else:
            for record_id, value in data.items():
                if isinstance(value, dict):
                    record = dict(value)
                    if _normalized_identity(record.get("id")) is None:
                        record["id"] = record_id
                elif isinstance(value, (int, float, str)) and not isinstance(
                    value, bool
                ):
                    record = {"id": record_id, "rate_multiplier": value}
                else:
                    raise DataNormalizationError("分组倍率映射包含非法记录")
                records.append(record)
    elif isinstance(data, list):
        records = data
    else:
        raise DataNormalizationError("分组倍率响应不是列表或映射")
    return _normalize_records(records, kind="group_rate")


def diff_records(
    old_records: list[dict[str, Any]],
    new_records: list[dict[str, Any]],
    *,
    ignored_keys: set[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[RecordChange]]:
    """Return added, removed, and changed records by normalized ``id``."""
    old_map = _index_records(old_records)
    new_map = _index_records(new_records)
    old_json = _canonical_records(old_records, ignored_keys=ignored_keys)
    new_json = _canonical_records(new_records, ignored_keys=ignored_keys)
    added = [new_map[key] for key in sorted(new_map.keys() - old_map.keys())]
    removed = [old_map[key] for key in sorted(old_map.keys() - new_map.keys())]
    changed = [
        RecordChange(before=old_map[key], after=new_map[key])
        for key in sorted(new_map.keys() & old_map.keys())
        if old_json[key] != new_json[key]
    ]
    return added, removed, changed


def _index_records(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for item in records:
        identity = _normalized_identity(item.get("id"))
        if identity is None:
            raise DataNormalizationError("快照记录缺少身份字段")
        if identity in indexed:
            raise DataNormalizationError(f"快照包含重复记录身份: {identity}")
        indexed[identity] = item
    return indexed


def canonical_record(record: dict[str, Any]) -> str:
    """Return a stable serialized representation for event fingerprints."""
    return json.dumps(
        record,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _extract_records(
    payload: Any,
    keys: Iterable[str],
    *,
    allow_scalar_map_values: bool = False,
) -> list[Any]:
    data = _unwrap_data(payload)
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    for key in keys:
        value = data.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            return [
                (
                    {
                        **item,
                        "id": (
                            item.get("id")
                            if _normalized_identity(item.get("id")) is not None
                            else record_id
                        ),
                    }
                    if isinstance(item, dict)
                    else (
                        {"id": record_id, "value": item}
                        if allow_scalar_map_values
                        else (_ for _ in ()).throw(DataNormalizationError("响应映射包含非法记录"))
                    )
                )
                for record_id, item in value.items()
            ]
    return []


def _normalize_records(records: Iterable[Any], *, kind: str) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw in records:
        if not isinstance(raw, dict):
            raise DataNormalizationError("响应列表包含非法记录")
        item = redact(raw)
        if kind == "group_rate":
            item = _normalize_rate_fields(item)
            item = _project_group_rate(item)
            if not _has_rate_field(item):
                raise DataNormalizationError("分组倍率记录缺少有效倍率")
        identity = _record_identity(item, kind=kind)
        if identity is None:
            raise DataNormalizationError("记录缺少稳定身份字段")
        if identity in seen_ids:
            raise DataNormalizationError(f"响应包含重复记录身份: {identity}")
        seen_ids.add(identity)
        item["id"] = identity
        if kind == "group_rate":
            item["rate_multiplier"] = _rate_value(item)
        normalized.append(item)
    return sorted(normalized, key=lambda item: str(item.get("id", "")))


def _project_group_rate(item: dict[str, Any]) -> dict[str, Any]:
    identity_keys = {
        "id",
        "group_id",
        "groupId",
        "group_name",
        "groupName",
        "group",
        "name",
        "platform",
        "slug",
    }
    rate_keys = {key for key in item if _is_group_rate_field(key)}
    value_keys = {"value"} if "value" in item else set()
    selected = identity_keys | rate_keys | value_keys
    return {key: item[key] for key in item if key in selected}


def is_group_rate_field(key: Any) -> bool:
    """Return whether a field is a numeric multiplier accepted by snapshots."""
    normalized = _normalize_field_name(key)
    if normalized in _RATE_BOOLEAN_FIELDS:
        return False
    if any(marker in normalized for marker in _RATE_EXCLUDED_MARKERS):
        return False
    return normalized in _RATE_FIELDS or any(
        marker in normalized for marker in ("ratio", "multiplier", "rate", "weight")
    )


def _is_group_rate_field(key: Any) -> bool:
    """Return whether a field is relevant to a group-rate snapshot."""
    normalized = _normalize_field_name(key)
    return is_group_rate_field(normalized) or normalized in {
        "peak_start",
        "peak_end",
        "peak_rate_enabled",
    }


def _normalized_identity(value: Any) -> str | None:
    if value is None:
        return None
    identity = str(value).strip()
    return identity if identity and identity != "[已隐藏]" else None


def _record_identity(item: dict[str, Any], *, kind: str) -> str | None:
    candidates: tuple[str, ...]
    if kind == "subscription":
        candidates = (
            "id",
            "plan_id",
            "planId",
            "subscription_id",
            "subscriptionId",
            "slug",
            "name",
            "product_name",
            "productName",
        )
    else:
        candidates = (
            "id",
            "group_id",
            "groupId",
            "group_name",
            "groupName",
            "group",
            "name",
            "slug",
        )
    for key in candidates:
        identity = _normalized_identity(item.get(key))
        if identity is not None:
            return identity
    return None


def _normalize_rate_fields(item: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(item)
    for key, value in list(normalized.items()):
        if not is_group_rate_field(key):
            continue
        if not _finite_number(value):
            raise DataNormalizationError(f"分组倍率字段无效: {key}")
        normalized[key] = float(value)
    return normalized


def _has_rate_field(item: dict[str, Any]) -> bool:
    return any(is_group_rate_field(key) for key in item)


def _rate_value(item: dict[str, Any]) -> float:
    candidates = sorted(
        (
            (_rate_field_priority(key), _normalize_field_name(key), str(key), value)
            for key, value in item.items()
            if is_group_rate_field(key)
        ),
        key=lambda value: value[:3],
    )
    if candidates:
        return float(candidates[0][3])
    raise DataNormalizationError("分组倍率记录缺少有效倍率")


def _rate_field_priority(key: Any) -> int:
    normalized = _normalize_field_name(key)
    try:
        return _RATE_FIELDS.index(normalized)
    except ValueError:
        return len(_RATE_FIELDS)


def _finite_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def _unwrap_data(payload: Any) -> Any:
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def _first_non_empty(mapping: Any, *keys: str) -> Any:
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return None


def _normalize_field_name(field_name: Any) -> str:
    text = str(field_name)
    camel_case = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text)
    return re.sub(r"[^a-z0-9]+", "_", camel_case.casefold()).strip("_")


def redact(value: Any, *, field_name: str = "") -> Any:
    """Recursively remove token/password-like fields before persistence/output."""
    normalized = _normalize_field_name(field_name)
    if (
        normalized in _SECRET_KEYS
        or normalized
        in {
            "keys",
            "tokens",
            "secrets",
            "passwords",
        }
        or normalized.endswith(_SECRET_SUFFIXES)
    ):
        return "[已隐藏]"
    if isinstance(value, dict):
        return {
            str(key): redact(item, field_name=str(key)) for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item, field_name=field_name) for item in value]
    if isinstance(value, str):
        return _redact_url(value)
    return value


def _is_secret_query_key(key: str) -> bool:
    normalized = _normalize_field_name(key)
    return (
        normalized in _SECRET_KEYS
        or normalized
        in {
            "keys",
            "tokens",
            "secrets",
            "passwords",
        }
        or normalized.endswith(_SECRET_SUFFIXES)
    )


def _redact_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return (
            "[已隐藏的 URL]"
            if value.lstrip().casefold().startswith(("http://", "https://"))
            else value
        )
    if parsed.scheme.casefold() not in {"http", "https"}:
        return value
    if not parsed.netloc:
        return "[已隐藏的 URL]"
    query = parse_qsl(parsed.query, keep_blank_values=True)
    safe_query = [
        (key, "[已隐藏]" if _is_secret_query_key(key) else item) for key, item in query
    ]
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host
    if port is not None:
        netloc += f":{port}"
    return urlunsplit(
        (
            parsed.scheme,
            netloc,
            parsed.path,
            urlencode(safe_query),
            "[已隐藏]" if parsed.fragment else "",
        )
    )


def comparison_record(
    record: dict[str, Any], *, ignored_keys: set[str] | None = None
) -> dict[str, Any]:
    """Project a record into the stable representation used for comparisons."""
    ignored = {_normalize_field_name(key) for key in (ignored_keys or set())}
    comparable = {
        key: value
        for key, value in record.items()
        if _normalize_field_name(key) not in ignored
    }
    identity = _normalized_identity(record.get("id"))
    if identity is not None:
        comparable["id"] = identity
    return comparable


def _canonical_records(
    records: list[dict[str, Any]],
    *,
    ignored_keys: set[str] | None,
) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in records:
        identity = _normalized_identity(item.get("id"))
        if identity is None:
            raise DataNormalizationError("快照记录缺少身份字段")
        result[identity] = canonical_record(
            comparison_record(item, ignored_keys=ignored_keys)
        )
    return result
