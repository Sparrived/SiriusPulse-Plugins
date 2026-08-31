"""GitHub REST client used by the external monitor plugin."""

from __future__ import annotations

import ipaddress
from typing import Any
from urllib.parse import urlsplit

import httpx

_GITHUB_API_BASE = "https://api.github.com"
_DEFAULT_TIMEOUT = 30.0
_DEFAULT_ALLOWED_HOSTS = frozenset({"api.github.com"})


def github_headers(token: str = "", *, extra_accept: str | None = None) -> dict[str, str]:
    """Build standard GitHub REST headers without logging or persisting tokens."""
    headers: dict[str, str] = {
        "Accept": extra_accept or "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "SiriusPulse-GitHub/1.1",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def validate_api_base_url(
    value: str,
    *,
    allowed_hosts: set[str] | frozenset[str] | None = None,
) -> str:
    """Validate and normalize a GitHub/GHE API URL.

    ``allowed_hosts`` is required for a GitHub Enterprise host.  The path is
    retained because Enterprise Server commonly serves the API below
    ``/api/v3``; callers still use relative API paths and the client joins the
    prefix without allowing callers to escape it.
    """
    raw = str(value or "").strip()
    parsed = urlsplit(raw)
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or "\\" in parsed.netloc
        or parsed.hostname is None
    ):
        raise ValueError("GitHub API 地址必须是无凭据、无查询参数的 HTTPS 地址")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("GitHub API 地址端口无效") from exc
    if port not in (None, 443):
        raise ValueError("GitHub API 地址只允许 HTTPS 默认端口")
    path = parsed.path or ""
    if "//" in path or ".." in path or any(ord(char) < 0x20 for char in path):
        raise ValueError("GitHub API 地址路径无效")
    if len(path) > 128:
        raise ValueError("GitHub API 地址路径过长")
    host = parsed.hostname.casefold().rstrip(".")
    configured = {
        str(item).strip().casefold().rstrip(".")
        for item in (allowed_hosts or _DEFAULT_ALLOWED_HOSTS)
        if str(item).strip()
    }
    if host not in configured:
        raise ValueError("GitHub API 地址不在允许的 GitHub/GHE 主机列表中")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        raise ValueError("GitHub API 地址不得使用 IP 地址")
    return f"https://{host}{path.rstrip('/') if path != '/' else ''}"


class GitHubClient:
    """Small async GitHub REST client with a fixed, validated API origin."""

    def __init__(
        self,
        token: str = "",
        *,
        base_url: str = _GITHUB_API_BASE,
        timeout: float = _DEFAULT_TIMEOUT,
        extra_headers: dict[str, str] | None = None,
        allowed_hosts: set[str] | frozenset[str] | None = None,
    ) -> None:
        self._token = str(token or "").strip()
        validated = validate_api_base_url(base_url, allowed_hosts=allowed_hosts)
        parsed = urlsplit(validated)
        self._api_prefix = parsed.path.rstrip("/")
        self._base_url = f"{parsed.scheme}://{parsed.netloc}"
        self._validated_base_url = validated
        self._timeout = timeout
        self._extra_headers = dict(extra_headers or {})
        headers = github_headers(self._token)
        headers.update(self._extra_headers)
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=headers,
            timeout=httpx.Timeout(timeout),
            follow_redirects=False,
        )

    def _path(self, path: str) -> str:
        """Join a strictly relative API path below the configured prefix."""
        raw = str(path or "")
        parsed = urlsplit(raw)
        if (
            parsed.scheme
            or parsed.netloc
            or parsed.query
            or parsed.fragment
            or "\\" in raw
            or any(ord(char) < 0x20 for char in raw)
        ):
            raise ValueError("GitHub API 请求路径必须是无查询参数的相对路径")
        segments = [segment for segment in parsed.path.split("/") if segment]
        if any(segment in {".", ".."} for segment in segments):
            raise ValueError("GitHub API 请求路径不得包含路径遍历")
        relative = "/" + "/".join(segments)
        return f"{self._api_prefix}{relative}" if self._api_prefix else relative

    async def __aenter__(self) -> GitHubClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self._client.aclose()

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def get(self, path: str, **kwargs: Any) -> httpx.Response:
        """GET a relative API path without following redirects."""
        return await self._client.get(self._path(path), **kwargs)

    async def post(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self._client.post(self._path(path), **kwargs)

    async def put(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self._client.put(self._path(path), **kwargs)

    async def patch(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self._client.patch(self._path(path), **kwargs)

    async def delete(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self._client.delete(self._path(path), **kwargs)

    async def get_json(
        self, path: str, **kwargs: Any
    ) -> list[dict[str, Any]] | dict[str, Any] | None:
        resp = await self.get(path, **kwargs)
        if resp.status_code == 200:
            return resp.json()
        return None

    async def post_json(self, path: str, **kwargs: Any) -> dict[str, Any] | None:
        resp = await self.post(path, **kwargs)
        if resp.status_code in (200, 201):
            return resp.json()
        return None
