"""GitHub REST client with constrained, DNS-checked API egress."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import Any
from urllib.parse import urlsplit

import httpx

_GITHUB_API_BASE = "https://api.github.com"
_DEFAULT_TIMEOUT = 30.0
_DEFAULT_ALLOWED_HOSTS = frozenset({"api.github.com"})
# Cloud metadata endpoints are rejected explicitly even where an address is
# represented differently by a platform resolver.  The broader address
# predicates below reject the private/link-local ranges that contain most of
# these endpoints as well.
_METADATA_NETWORKS = (
    ipaddress.ip_network("169.254.169.254/32"),  # AWS/GCP/Azure IMDS
    ipaddress.ip_network("169.254.170.2/32"),  # AWS ECS task metadata
    ipaddress.ip_network("100.100.100.0/24"),  # Alibaba Cloud metadata
    ipaddress.ip_network("168.63.129.16/32"),  # Azure WireServer
    ipaddress.ip_network("192.0.0.192/32"),  # cloud metadata compatibility endpoint
    ipaddress.ip_network("fd00:ec2::254/128"),  # AWS IMDS IPv6
)
_METADATA_HOSTNAMES = frozenset(
    {
        "metadata.google.internal",
        "metadata.google",
        "instance-data.ec2.internal",
    }
)

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def _is_safe_ip_address(value: str | IPAddress) -> bool:
    """Return whether an address is globally routable and not metadata.

    IPv4-mapped IPv6 values are checked as their embedded IPv4 address.  This
    prevents a non-public IPv4 endpoint from being hidden behind an IPv6 spelling.
    """
    try:
        address = (
            value
            if isinstance(value, (ipaddress.IPv4Address, ipaddress.IPv6Address))
            else ipaddress.ip_address(value)
        )
    except (TypeError, ValueError):
        return False

    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped

    if (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
        or not address.is_global
    ):
        return False
    return not any(address in network for network in _METADATA_NETWORKS)


async def _resolve_public_addresses(
    host: str, *, port: int = 443
) -> tuple[IPAddress, ...]:
    """Resolve every A/AAAA record and reject unsafe destinations.

    This asynchronous preflight is intentionally not presented as complete
    connection-level DNS pinning: the hostname can still change between this
    lookup and the socket connect (DNS TOCTOU).  Production deployments need an
    egress firewall to enforce the same public-destination policy at connect
    time.
    """
    normalized_host = str(host or "").strip().casefold().rstrip(".")
    if not normalized_host:
        raise ValueError("GitHub API 主机不能为空")
    if normalized_host in _METADATA_HOSTNAMES:
        raise ValueError("GitHub API 主机不得使用 metadata 主机名")
    try:
        records = await asyncio.to_thread(
            socket.getaddrinfo,
            normalized_host,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
    except (OSError, socket.gaierror) as exc:
        raise ValueError("GitHub API 主机 DNS 解析失败") from exc

    addresses: list[IPAddress] = []
    seen: set[str] = set()
    for record in records:
        try:
            family = record[0]
            sockaddr = record[4]
            address_text = sockaddr[0]
        except (IndexError, TypeError, KeyError) as exc:
            raise ValueError("GitHub API 主机 DNS 地址无效") from exc
        if family not in (socket.AF_INET, socket.AF_INET6):
            raise ValueError("GitHub API 主机 DNS 返回了非 A/AAAA 地址")
        try:
            address = ipaddress.ip_address(str(address_text))
        except (TypeError, ValueError) as exc:
            raise ValueError("GitHub API 主机 DNS 地址无效") from exc
        address_key = str(address)
        if address_key not in seen:
            seen.add(address_key)
            addresses.append(address)

    if not addresses:
        raise ValueError("GitHub API 主机未解析出 A/AAAA 地址")
    if any(not _is_safe_ip_address(address) for address in addresses):
        raise ValueError("GitHub API 主机解析到非公网或 metadata 地址")
    return tuple(addresses)


def github_headers(
    token: str = "", *, extra_accept: str | None = None
) -> dict[str, str]:
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
        for item in (*_DEFAULT_ALLOWED_HOSTS, *(allowed_hosts or ()))
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
    """Small async GitHub REST client with a fixed, validated API origin.

    Every request performs a fresh A/AAAA preflight for the fixed host.  This
    is not complete connection-level IP pinning: DNS can change between the
    preflight and socket connection (DNS TOCTOU), so deployments also need an
    egress firewall to enforce the public-destination policy at connect time.
    """

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
            trust_env=False,
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

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Preflight the fixed API host before every network connection.

        The lookup is a destination policy check, not connection-level DNS
        pinning.  Deployments still need an egress firewall or proxy policy to
        close the DNS time-of-check/time-of-use gap.
        """
        relative_path = self._path(path)
        host = urlsplit(self._validated_base_url).hostname or ""
        await _resolve_public_addresses(host, port=443)
        request = getattr(self._client, method)
        return await request(relative_path, **kwargs)

    async def get(self, path: str, **kwargs: Any) -> httpx.Response:
        """GET a relative API path without following redirects."""
        return await self._request("get", path, **kwargs)

    async def post(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self._request("post", path, **kwargs)

    async def put(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self._request("put", path, **kwargs)

    async def patch(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self._request("patch", path, **kwargs)

    async def delete(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self._request("delete", path, **kwargs)

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
