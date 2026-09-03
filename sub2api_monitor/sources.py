"""Validated multi-source configuration for the Sub2API monitor."""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from typing import Any, Mapping

from .client import Sub2APIClient, Sub2APIError

_SOURCE_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_MAX_SOURCES = 32
_MAX_NOTIFY_GROUPS = 128
_RESERVED_SOURCE_IDS = {"all"}
_ALLOWED_SOURCE_FIELDS = {
    "id",
    "display_name",
    "enabled",
    "base_url",
    "api_base_path",
    "login_path",
    "refresh_path",
    "logout_path",
    "subscriptions_path",
    "group_rates_path",
    "timezone",
    "timeout",
    "allow_insecure_http",
    "inherit_notify_group_ids",
    "notify_group_ids",
    "email",
    "password",
}


@dataclass(frozen=True, slots=True)
class SourceConfig:
    """One independently authenticated Sub2API deployment."""

    id: str
    display_name: str
    enabled: bool
    base_url: str
    api_base_path: str
    login_path: str
    refresh_path: str
    logout_path: str
    subscriptions_path: str
    group_rates_path: str
    timezone: str
    timeout: float
    allow_insecure_http: bool
    inherit_notify_group_ids: bool
    notify_group_ids: tuple[str, ...]
    email: str = ""
    password: str = ""
    legacy: bool = False

    @property
    def env_prefix(self) -> str:
        """Return the deterministic environment-variable prefix for this source."""
        return "SUB2API" if self.legacy else f"SUB2API_{self.id.upper()}"

    @property
    def email_env(self) -> str:
        return f"{self.env_prefix}_EMAIL"

    @property
    def password_env(self) -> str:
        return f"{self.env_prefix}_PASSWORD"

    def credentials(self) -> tuple[str, str]:
        """Resolve credentials from WebUI-persisted config first, env as fallback."""
        return (
            (self.email or os.getenv(self.email_env, "")).strip(),
            self.password or os.getenv(self.password_env, ""),
        )

    def client_kwargs(self) -> dict[str, Any]:
        email, password = self.credentials()
        return {
            "base_url": self.base_url,
            "api_base_path": self.api_base_path,
            "login_path": self.login_path,
            "refresh_path": self.refresh_path,
            "logout_path": self.logout_path,
            "subscriptions_path": self.subscriptions_path,
            "group_rates_path": self.group_rates_path,
            "email": email,
            "password": password,
            "timezone": self.timezone,
            "timeout": self.timeout,
            "allow_insecure_http": self.allow_insecure_http,
        }

    def validate_endpoints(self) -> None:
        """Run the same fail-closed URL validation used by the HTTP client."""
        client = Sub2APIClient(**self.client_kwargs())
        client._validate_transport_security()
        for endpoint in (
            client.login_path,
            client.refresh_path,
            client.logout_path,
            client.subscriptions_path,
            client.group_rates_path,
        ):
            if endpoint:
                client.resolve_url(endpoint)


def parse_sources(config: Mapping[str, Any]) -> list[SourceConfig]:
    """Parse the preferred source list or one legacy top-level source."""
    if "sources" in config:
        raw_sources = config.get("sources")
        if not isinstance(raw_sources, list):
            raise Sub2APIError("sources 必须是站点对象列表")
        if len(raw_sources) > _MAX_SOURCES:
            raise Sub2APIError(f"sources 最多允许 {_MAX_SOURCES} 个站点")
        sources = [_parse_source(raw, index) for index, raw in enumerate(raw_sources)]
        _validate_unique_ids(sources)
        return sources

    legacy_values = {
        key: config.get(key)
        for key in (
            "base_url",
            "api_base_path",
            "login_path",
            "refresh_path",
            "logout_path",
            "subscriptions_path",
            "group_rates_path",
            "timezone",
            "timeout",
            "allow_insecure_http",
        )
    }
    if not any(value not in (None, "") for value in legacy_values.values()):
        raise Sub2APIError("缺少配置：sources")
    raw_legacy: dict[str, Any] = {
        "id": "default",
        "display_name": "Sub2API",
        "enabled": True,
        "base_url": legacy_values["base_url"],
        "api_base_path": legacy_values["api_base_path"] or "/api/v1",
        "login_path": legacy_values["login_path"] or "/auth/login",
        "refresh_path": (
            "/auth/refresh"
            if legacy_values["refresh_path"] is None
            else legacy_values["refresh_path"]
        ),
        "logout_path": (
            "/auth/logout"
            if legacy_values["logout_path"] is None
            else legacy_values["logout_path"]
        ),
        "subscriptions_path": legacy_values["subscriptions_path"],
        "group_rates_path": legacy_values["group_rates_path"],
        "timezone": (
            "Asia/Shanghai"
            if legacy_values["timezone"] is None
            else legacy_values["timezone"]
        ),
        "timeout": 20 if legacy_values["timeout"] is None else legacy_values["timeout"],
        "allow_insecure_http": (
            False
            if legacy_values["allow_insecure_http"] is None
            else legacy_values["allow_insecure_http"]
        ),
        "inherit_notify_group_ids": True,
    }
    return [_parse_source(raw_legacy, 0, legacy=True)]


def parse_sources_partial(
    config: Mapping[str, Any],
) -> tuple[list[SourceConfig], list[str]]:
    """Parse usable sources while isolating errors in individual enabled entries."""
    if "sources" not in config:
        try:
            return parse_sources(config), []
        except Sub2APIError as exc:
            return [], [str(exc)]
    raw_sources = config.get("sources")
    if not isinstance(raw_sources, list):
        return [], ["sources 必须是站点对象列表"]
    if len(raw_sources) > _MAX_SOURCES:
        return [], [f"sources 最多允许 {_MAX_SOURCES} 个站点"]

    candidate_ids = [
        str(raw.get("id", "")).strip()
        for raw in raw_sources
        if isinstance(raw, dict) and isinstance(raw.get("id"), str)
    ]
    duplicate_ids = {
        source_id for source_id in candidate_ids if candidate_ids.count(source_id) > 1
    }
    sources: list[SourceConfig] = []
    errors: list[str] = [
        f"sources 包含重复站点 ID：{source_id}" for source_id in sorted(duplicate_ids)
    ]
    for index, raw in enumerate(raw_sources):
        if isinstance(raw, dict) and str(raw.get("id", "")).strip() in duplicate_ids:
            continue
        try:
            source = _parse_source(raw, index)
        except Sub2APIError as exc:
            errors.append(str(exc))
            continue
        sources.append(source)
    return sources, errors


def source_by_selector(
    sources: list[SourceConfig],
    selector: str,
    *,
    require_one: bool,
    include_disabled: bool = False,
) -> list[SourceConfig]:
    """Select one source by ID/display name, or all applicable sources."""
    selectable = (
        sources
        if include_disabled
        else [source for source in sources if source.enabled]
    )
    query = str(selector or "").strip()
    if not query or query.casefold() == "all":
        if require_one and len(selectable) != 1:
            available = "、".join(source.id for source in selectable)
            raise Sub2APIError(f"请指定站点 ID，可选：{available}")
        return selectable

    exact_id = [source for source in selectable if source.id == query.casefold()]
    if exact_id:
        return exact_id
    named = [
        source
        for source in selectable
        if source.display_name.casefold() == query.casefold()
    ]
    if len(named) == 1:
        return named
    if len(named) > 1:
        raise Sub2APIError("显示名称不唯一，请改用站点 ID")
    available = "、".join(source.id for source in selectable)
    raise Sub2APIError(f"未找到站点 {query}，可选：{available}")


def _parse_source(raw: Any, index: int, *, legacy: bool = False) -> SourceConfig:
    if not isinstance(raw, dict):
        raise Sub2APIError(f"sources[{index}] 必须是对象")
    unknown = set(raw) - _ALLOWED_SOURCE_FIELDS
    if unknown:
        raise Sub2APIError(f"sources[{index}] 包含未声明字段：{sorted(unknown)[0]}")
    source_id = _required_text(raw, "id", index)
    if not _SOURCE_ID_RE.fullmatch(source_id):
        raise Sub2APIError(f"sources[{index}].id 必须以小写字母开头，且只含小写字母、数字、下划线，最长 32 位")
    if source_id in _RESERVED_SOURCE_IDS:
        raise Sub2APIError(f"sources[{index}].id 不能使用保留字：{source_id}")
    display_name = _optional_text(raw, "display_name", source_id, index) or source_id
    if len(display_name) > 80 or _has_control_character(display_name):
        raise Sub2APIError(f"sources[{index}].display_name 格式无效")

    enabled = raw.get("enabled", True)
    if type(enabled) is not bool:
        raise Sub2APIError(f"sources[{index}].enabled 必须是布尔值")
    insecure = raw.get("allow_insecure_http", False)
    if type(insecure) is not bool:
        raise Sub2APIError(f"sources[{index}].allow_insecure_http 必须是布尔值")
    inherit_groups = raw.get("inherit_notify_group_ids", True)
    if type(inherit_groups) is not bool:
        raise Sub2APIError(f"sources[{index}].inherit_notify_group_ids 必须是布尔值")

    timeout_raw = raw.get("timeout", 20)
    if isinstance(timeout_raw, bool):
        raise Sub2APIError(f"sources[{index}].timeout 必须是 1 到 300 之间的有限数字")
    try:
        timeout = float(timeout_raw)
    except (TypeError, ValueError, OverflowError) as exc:
        raise Sub2APIError(f"sources[{index}].timeout 必须是 1 到 300 之间的有限数字") from exc
    if not math.isfinite(timeout) or not 1 <= timeout <= 300:
        raise Sub2APIError(f"sources[{index}].timeout 必须是 1 到 300 之间的有限数字")

    source = SourceConfig(
        id=source_id,
        display_name=display_name,
        enabled=enabled,
        base_url=_required_text(raw, "base_url", index),
        api_base_path=_optional_text(raw, "api_base_path", "/api/v1", index),
        login_path=_required_or_default_text(raw, "login_path", "/auth/login", index),
        refresh_path=_optional_text(raw, "refresh_path", "/auth/refresh", index),
        logout_path=_optional_text(raw, "logout_path", "/auth/logout", index),
        subscriptions_path=_required_text(raw, "subscriptions_path", index),
        group_rates_path=_required_text(raw, "group_rates_path", index),
        timezone=_optional_text(raw, "timezone", "Asia/Shanghai", index),
        timeout=timeout,
        allow_insecure_http=insecure,
        inherit_notify_group_ids=inherit_groups,
        notify_group_ids=tuple(
            _parse_group_ids(raw.get("notify_group_ids", []), index)
        ),
        email=str(raw.get("email", "") or ""),
        password=str(raw.get("password", "") or ""),
        legacy=legacy,
    )
    source.validate_endpoints()
    return source


def _required_text(raw: Mapping[str, Any], key: str, index: int) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise Sub2APIError(f"sources[{index}].{key} 不能为空")
    text = value.strip()
    if _has_control_character(text):
        raise Sub2APIError(f"sources[{index}].{key} 包含非法字符")
    return text


def _required_or_default_text(
    raw: Mapping[str, Any], key: str, default: str, index: int
) -> str:
    if key not in raw:
        return default
    return _required_text(raw, key, index)


def _optional_text(
    raw: Mapping[str, Any],
    key: str,
    default: str,
    index: int,
) -> str:
    value = raw.get(key, default)
    if value is None:
        value = default
    if not isinstance(value, str):
        raise Sub2APIError(f"sources[{index}].{key} 必须是字符串")
    text = value.strip()
    if _has_control_character(text):
        raise Sub2APIError(f"sources[{index}].{key} 包含非法字符")
    return text


def _parse_group_ids(value: Any, index: int) -> list[str]:
    if not isinstance(value, list):
        raise Sub2APIError(f"sources[{index}].notify_group_ids 必须是字符串列表")
    if len(value) > _MAX_NOTIFY_GROUPS:
        raise Sub2APIError(
            f"sources[{index}].notify_group_ids 最多允许 {_MAX_NOTIFY_GROUPS} 项"
        )
    result: list[str] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (str, int)):
            raise Sub2APIError(f"sources[{index}].notify_group_ids 包含无效群号")
        group_id = str(item).strip()
        if not group_id or len(group_id) > 128 or _has_control_character(group_id):
            raise Sub2APIError(f"sources[{index}].notify_group_ids 包含无效群号")
        if group_id not in result:
            result.append(group_id)
    return result


def _validate_unique_ids(sources: list[SourceConfig]) -> None:
    seen: set[str] = set()
    for source in sources:
        if source.id in seen:
            raise Sub2APIError(f"sources 包含重复站点 ID：{source.id}")
        seen.add(source.id)


def _has_control_character(value: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in value)
