"""Async HTTP client for configurable Sub2API deployments."""

from __future__ import annotations

import asyncio
import json
import math
import time
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

import httpx

from .data import (
    DataNormalizationError,
    is_group_rate_field,
    normalize_group_rates,
    normalize_subscriptions,
    redact,
)


class Sub2APIError(RuntimeError):
    """A Sub2API error whose message is safe to show to plugin users."""


class Sub2APIClient:
    """Authenticate against Sub2API and fetch monitor data.

    ``base_url`` may be a frontend page such as ``https://host.example/keys``.
    Only its origin is used. Every endpoint remains configurable and may be a
    path relative to ``api_base_path``, an origin-relative API path, or a full
    URL on the same origin.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_base_path: str,
        login_path: str,
        refresh_path: str,
        logout_path: str,
        subscriptions_path: str,
        group_rates_path: str,
        email: str,
        password: str,
        timezone: str = "",
        timeout: float = 20.0,
        allow_insecure_http: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.strip()
        self.api_base_path = api_base_path.strip()
        self.login_path = login_path.strip()
        self.refresh_path = refresh_path.strip()
        self.logout_path = logout_path.strip()
        self.subscriptions_path = subscriptions_path.strip()
        self.group_rates_path = group_rates_path.strip()
        self.email = email
        self.password = password
        self.timezone = timezone.strip()
        try:
            timeout_value = float(timeout)
        except (TypeError, ValueError, OverflowError):
            timeout_value = float("nan")
        self.timeout = timeout_value
        self.allow_insecure_http = allow_insecure_http
        self._transport = transport
        self._http: httpx.AsyncClient | None = None
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._expires_at = 0.0
        self._login_lock = asyncio.Lock()

    async def __aenter__(self) -> Sub2APIClient:
        self._validate_transport_security()
        if not math.isfinite(self.timeout) or not 1.0 <= self.timeout <= 300.0:
            raise Sub2APIError("timeout 必须是 1 到 300 秒之间的有限数字")
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout),
            follow_redirects=False,
            headers={"Accept": "application/json"},
            transport=self._transport,
        )
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        await self.aclose(logout=True)

    async def aclose(self, *, logout: bool) -> None:
        """Close the client and optionally revoke its refresh-token session."""
        try:
            if logout:
                await self.logout()
        finally:
            http = self._http
            self._http = None
            self._clear_tokens()
            if http is not None:
                await http.aclose()

    async def logout(self) -> None:
        """Best-effort logout of only the session created by this client."""
        if self._http is None or not self._refresh_token or not self.logout_path:
            self._clear_tokens()
            return
        try:
            await self._request_json(
                "POST",
                self.logout_path,
                json_body={"refresh_token": self._refresh_token},
                authenticated=bool(self._access_token),
            )
        except Sub2APIError:
            pass
        finally:
            self._clear_tokens()

    async def login(self, *, force: bool = False) -> None:
        """Log in once and keep only the access token in process memory."""
        async with self._login_lock:
            if self._access_token and not force:
                return
            await self._login_unlocked()

    async def _login_unlocked(self) -> None:
        if not self.email or not self.password:
            raise Sub2APIError(
                "未配置 Sub2API 登录凭据，请设置 SUB2API_EMAIL 和 SUB2API_PASSWORD 环境变量"
            )
        payload = await self._request_json(
            "POST",
            self.login_path,
            json_body={"email": self.email, "password": self.password},
            authenticated=False,
        )
        self._set_tokens(_unwrap_data(payload), require_refresh=False)

    async def _ensure_auth(self) -> None:
        if self._access_token and time.monotonic() < self._expires_at:
            return
        async with self._login_lock:
            if self._access_token and time.monotonic() < self._expires_at:
                return
            await self._renew_auth_unlocked()

    async def _renew_auth_unlocked(self) -> None:
        if self._refresh_token and self.refresh_path:
            try:
                payload = await self._request_json(
                    "POST",
                    self.refresh_path,
                    json_body={"refresh_token": self._refresh_token},
                    authenticated=False,
                )
                self._set_tokens(_unwrap_data(payload), require_refresh=True)
                return
            except Sub2APIError:
                self._clear_tokens()
        await self._login_unlocked()

    async def fetch_subscriptions(self) -> list[dict[str, Any]]:
        """Fetch and normalize currently listed subscriptions."""
        payload = await self._get(self.subscriptions_path)
        data = _validated_data(payload, endpoint_name="订阅")
        if isinstance(data, dict):
            collection = next(
                (
                    data[key]
                    for key in ("plans", "subscriptions", "items", "products")
                    if isinstance(data.get(key), (list, dict))
                ),
                None,
            )
            if collection is None:
                raise Sub2APIError("Sub2API 订阅响应缺少有效的列表字段")
        elif not isinstance(data, list):
            raise Sub2APIError("Sub2API 订阅响应格式无效")
        try:
            return normalize_subscriptions(payload)
        except DataNormalizationError as exc:
            raise Sub2APIError(f"Sub2API 订阅响应格式无效：{exc}") from exc

    async def fetch_group_rates(self) -> list[dict[str, Any]]:
        """Fetch and normalize current group rate multipliers."""
        payload = await self._get(self.group_rates_path)
        data = _validated_data(payload, endpoint_name="分组倍率")
        if not _is_group_rate_payload(data):
            raise Sub2APIError("Sub2API 分组倍率响应缺少有效的倍率字段")
        try:
            return normalize_group_rates(payload)
        except DataNormalizationError as exc:
            raise Sub2APIError(f"Sub2API 分组倍率响应格式无效：{exc}") from exc

    async def _get(self, endpoint: str) -> Any:
        await self._ensure_auth()
        return await self._request_json(
            "GET", endpoint, authenticated=True, retry_auth=True
        )

    async def _request_json(
        self,
        method: str,
        endpoint: str,
        *,
        json_body: dict[str, Any] | None = None,
        authenticated: bool,
        retry_auth: bool = False,
    ) -> Any:
        if self._http is None:
            raise Sub2APIError("Sub2API HTTP 客户端尚未启动")
        url = self.resolve_url(endpoint)
        headers: dict[str, str] = {}
        request_token = self._access_token if authenticated else None
        if request_token:
            headers["Authorization"] = f"Bearer {request_token}"
        if self.timezone and method.upper() == "GET":
            parsed_url = httpx.URL(url)
            if "timezone" not in parsed_url.params:
                url = str(parsed_url.copy_add_param("timezone", self.timezone))

        try:
            response = await self._http.request(
                method.upper(), url, json=json_body, headers=headers
            )
        except httpx.TimeoutException as exc:
            raise Sub2APIError("Sub2API 请求超时") from exc
        except httpx.RequestError as exc:
            raise Sub2APIError(f"无法连接 Sub2API：{type(exc).__name__}") from exc

        if response.status_code == 401 and authenticated and retry_auth:
            # One retry handles revoked/expired tokens without creating an auth loop.
            failed_token = request_token
            async with self._login_lock:
                if not self._access_token or self._access_token == failed_token:
                    self._access_token = None
                    self._expires_at = 0.0
                    await self._renew_auth_unlocked()
            return await self._request_json(
                method,
                endpoint,
                json_body=json_body,
                authenticated=True,
                retry_auth=False,
            )

        if not response.is_success:
            message = self._redact_secrets(_response_message(response))[:300]
            raise Sub2APIError(f"Sub2API HTTP {response.status_code}：{message}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise Sub2APIError("Sub2API 返回的不是 JSON") from exc
        if isinstance(payload, dict) and _payload_indicates_failure(payload):
            detail = (
                payload.get("message")
                or payload.get("error")
                or payload.get("detail")
                or "接口返回失败"
            )
            message = self._redact_secrets(_safe_error_detail(detail))[:300]
            raise Sub2APIError(f"Sub2API 接口失败：{message}")
        return payload

    def _redact_secrets(self, message: str) -> str:
        for secret in (
            self.password,
            self.email,
            self._access_token,
            self._refresh_token,
        ):
            if secret:
                message = message.replace(secret, "[已隐藏]")
        return message

    def _set_tokens(self, data: Any, *, require_refresh: bool) -> None:
        token = _token_string(
            _first_non_empty(data, "access_token", "accessToken", "token")
        )
        if token is None:
            raise Sub2APIError("Sub2API 认证响应中没有有效的 access token")
        refresh = _token_string(_first_non_empty(data, "refresh_token", "refreshToken"))
        if require_refresh and refresh is None:
            refresh = self._refresh_token
        self._access_token = token
        if refresh is not None or not require_refresh:
            self._refresh_token = refresh
        expires_raw = _first_non_empty(data, "expires_in", "expiresIn")
        if expires_raw is None:
            expires_in = None
        else:
            expires_in = _positive_float(expires_raw)
            if expires_in is None:
                raise Sub2APIError("Sub2API 认证响应中的 expires_in 无效")
        if expires_in is None:
            self._expires_at = float("inf")
        else:
            refresh_skew = min(60.0, expires_in * 0.1)
            self._expires_at = time.monotonic() + max(1.0, expires_in - refresh_skew)

    def _clear_tokens(self) -> None:
        self._access_token = None
        self._refresh_token = None
        self._expires_at = 0.0

    def _validate_transport_security(self) -> None:
        parsed = _parse_origin(self.base_url)
        if parsed is None:
            raise Sub2APIError("base_url 必须是有效的 http(s) 地址，且不能包含用户名、密码或控制字符")
        scheme, host, _port, _origin = parsed
        if scheme == "https":
            return
        if self.allow_insecure_http and host in {"127.0.0.1", "localhost", "::1"}:
            return
        raise Sub2APIError("base_url 必须使用 HTTPS；仅本机调试可显式启用 allow_insecure_http")

    def resolve_url(self, endpoint: str) -> str:
        """Resolve a configured endpoint and prevent cross-origin credential leaks."""
        endpoint = endpoint.strip()
        if not endpoint or _has_control_character(endpoint):
            raise Sub2APIError("未配置有效的 Sub2API 接口路径")
        base_origin = _parse_origin(self.base_url)
        if base_origin is None:
            raise Sub2APIError("base_url 必须是有效的 http(s) 地址")
        _scheme, _host, _port, origin = base_origin
        origin_url = urlsplit(origin)
        api_prefix = _safe_api_prefix(self.api_base_path)

        try:
            parsed_endpoint = urlsplit(endpoint)
        except ValueError as exc:
            raise Sub2APIError("Sub2API 接口 URL 格式无效") from exc
        if _is_absolute_url(endpoint):
            endpoint_origin = _parse_origin(endpoint)
            if endpoint_origin is None or endpoint_origin[:3] != base_origin[:3]:
                raise Sub2APIError("Sub2API 接口 URL 必须与 base_url 同源")
        elif (
            parsed_endpoint.fragment
            or parsed_endpoint.netloc
            or endpoint.startswith("\\\\")
        ):
            raise Sub2APIError("Sub2API 接口路径不得包含 fragment、userinfo 或其他主机")
        if parsed_endpoint.fragment:
            raise Sub2APIError("Sub2API 接口路径不得包含 fragment")
        if _has_control_character(parsed_endpoint.path) or "\\" in parsed_endpoint.path:
            raise Sub2APIError("Sub2API 接口路径包含非法字符")
        if not parsed_endpoint.path:
            raise Sub2APIError("Sub2API 接口路径不能为空")
        endpoint_path = _normal_path(parsed_endpoint.path)

        if _is_absolute_url(endpoint):
            path = endpoint_path
        else:
            if (
                endpoint_path.startswith("/")
                and api_prefix
                and not _under_prefix(endpoint_path, api_prefix)
            ):
                if endpoint_path.startswith("/api/"):
                    raise Sub2APIError("Sub2API 接口路径必须位于配置的 API 根路径下")
                path = _join_api_path(api_prefix, endpoint_path)
            else:
                path = endpoint_path
                if not path.startswith("/"):
                    path = _join_api_path(api_prefix, path)
                elif api_prefix and not _under_prefix(path, api_prefix):
                    path = _join_api_path(api_prefix, path)

        if api_prefix and not _under_prefix(path, api_prefix):
            raise Sub2APIError("Sub2API 接口路径必须位于配置的 API 根路径下")
        return urlunsplit(
            (origin_url.scheme, origin_url.netloc, path, parsed_endpoint.query, "")
        )


def _validated_data(payload: Any, *, endpoint_name: str) -> Any:
    data = _unwrap_data(payload)
    if data is None:
        raise Sub2APIError(f"Sub2API {endpoint_name}响应缺少 data")
    return data


def _is_group_rate_payload(data: Any) -> bool:
    if isinstance(data, list):
        return all(_is_group_rate_record(item) for item in data)
    if not isinstance(data, dict):
        return False
    if not data:
        return True
    found_wrapper = False
    for wrapper in ("groups", "rates", "items"):
        if wrapper in data:
            found_wrapper = True
            if _is_group_rate_collection(data[wrapper]):
                return True
    if found_wrapper:
        return False
    return all(
        _is_group_rate_record(value, allow_scalar=True) for value in data.values()
    )


def _is_group_rate_collection(value: Any) -> bool:
    if isinstance(value, list):
        return all(_is_group_rate_record(item) for item in value)
    if isinstance(value, dict):
        return all(
            _is_group_rate_record(item, allow_scalar=True) for item in value.values()
        )
    return False


def _is_group_rate_record(value: Any, *, allow_scalar: bool = False) -> bool:
    if (
        allow_scalar
        and isinstance(value, (int, float, str))
        and not isinstance(value, bool)
    ):
        return _finite_number(value)
    if not isinstance(value, dict):
        return False
    rate_fields = [
        field_value for key, field_value in value.items() if is_group_rate_field(key)
    ]
    return bool(rate_fields) and all(_finite_number(item) for item in rate_fields)


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


def _token_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    token = value.strip()
    return token or None


def _is_success_code(value: Any) -> bool:
    if value is None or value is True:
        return True
    if value is False:
        return False
    if isinstance(value, str):
        return value.strip().casefold() in {"0", "200", "ok", "success"}
    return isinstance(value, (int, float)) and value in (0, 200)


def _payload_indicates_failure(payload: dict[str, Any]) -> bool:
    if "code" in payload and not _is_success_code(payload.get("code")):
        return True
    for key in ("success", "ok"):
        if key not in payload:
            continue
        value = payload[key]
        if value is False or (isinstance(value, (int, float)) and value == 0):
            return True
        if isinstance(value, str) and value.strip().casefold() in {
            "0",
            "false",
            "fail",
            "failed",
            "error",
        }:
            return True
    status = payload.get("status")
    return isinstance(status, str) and status.strip().casefold() in {
        "error",
        "failed",
        "failure",
    }


def _positive_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _is_absolute_url(value: str) -> bool:
    return value.casefold().startswith(("http://", "https://"))


def _has_control_character(value: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in value)


def _parse_origin(value: str) -> tuple[str, str, int, str] | None:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or _has_control_character(value)
        or "\\" in parsed.netloc
        or parsed.hostname is None
    ):
        return None
    scheme = parsed.scheme.casefold()
    host = parsed.hostname.casefold().rstrip(".")
    if not host:
        return None
    effective_port = port if port is not None else (443 if scheme == "https" else 80)
    if not 1 <= effective_port <= 65535:
        return None
    display_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    display_port = (
        ""
        if effective_port == (443 if scheme == "https" else 80)
        else f":{effective_port}"
    )
    return scheme, host, effective_port, f"{scheme}://{display_host}{display_port}"


def _origin(value: str) -> str:
    parsed = _parse_origin(value)
    return parsed[3] if parsed is not None else ""


def _normal_path(path: str) -> str:
    """Validate an origin-relative path without normalizing traversal away."""
    path = path or "/"
    if not path.startswith("/"):
        path = "/" + path
    decoded = path
    for _ in range(4):
        unescaped = unquote(decoded)
        if unescaped == decoded:
            break
        decoded = unescaped
    if "\\" in decoded or _has_control_character(decoded):
        raise Sub2APIError("Sub2API 接口路径包含非法字符")
    segments = decoded.split("/")
    if any(segment in {".", ".."} for segment in segments):
        raise Sub2APIError("Sub2API 接口路径不得包含目录穿越")
    return path


def _safe_api_prefix(value: str) -> str:
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise Sub2APIError("api_base_path 必须是 origin-relative 路径") from exc
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        raise Sub2APIError("api_base_path 必须是 origin-relative 路径")
    path = _normal_path(parsed.path)
    return path.rstrip("/") if path != "/" else ""


def _join_api_path(prefix: str, path: str) -> str:
    if not prefix:
        return path if path.startswith("/") else f"/{path}"
    return f"{prefix}/{path.lstrip('/')}"


def _under_prefix(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(f"{prefix}/")


def _safe_error_detail(value: Any) -> str:
    """Serialize a remote error after recursive field-name-based redaction."""
    sanitized = redact(value, field_name="error")
    if isinstance(sanitized, (dict, list)):
        return json.dumps(sanitized, ensure_ascii=False, sort_keys=True, default=str)
    return str(sanitized)


def _response_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return "请求失败"
    if not isinstance(payload, dict):
        return "请求失败"
    detail = payload.get("message") or payload.get("error") or payload.get("detail")
    return _safe_error_detail(detail) if detail else "请求失败"
