"""只读取 AMKR 已归一化的账号用量与状态监控读数，不接触上游凭据或推理接口。"""

from __future__ import annotations

import asyncio
import json
from urllib.parse import urlsplit

import httpx

from sirius_pulse.providers.amkr import AmkrSettings

_MAX_ACCOUNT_BYTES = 4 * 1024 * 1024
_MAX_STATUS_BYTES = 8 * 1024 * 1024


class AMKRAccountError(ValueError):
    """可以直接展示给用户、不包含上游响应或凭据的错误。"""


def _valid_base_url(settings: AmkrSettings) -> None:
    if not settings.configured:
        raise AMKRAccountError("请先在全局设置中配置 AMKR 地址与管理员 Key。")
    try:
        url = urlsplit(settings.base_url)
        valid = (
            url.scheme in {"http", "https"}
            and url.hostname
            and url.port != 0
            and not url.username
            and not url.password
            and not url.query
            and not url.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        raise AMKRAccountError("AMKR 地址无效；请使用不含凭据、查询参数或片段的 HTTP(S) 地址。")


async def _fetch_json(
    settings: AmkrSettings,
    path: str,
    params: dict[str, str] | None,
    *,
    feature: str,
    max_bytes: int,
    not_found: str,
    timeout: float = 45,
) -> dict:
    _valid_base_url(settings)
    try:
        async with asyncio.timeout(timeout + 5):
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(timeout, connect=5),
                follow_redirects=False,
                trust_env=False,
                headers={
                    "Authorization": f"Bearer {settings.api_key}",
                    "Accept": "application/json",
                },
            ) as client:
                async with client.stream(
                    "GET", f"{settings.base_url.rstrip('/')}{path}", params=params
                ) as response:
                    if response.status_code in {401, 403}:
                        raise AMKRAccountError(f"AMKR 认证失败：{feature}需要管理员 Key，不能使用面板 Key 或推理 Key。")
                    if response.status_code == 404:
                        raise AMKRAccountError(not_found)
                    if response.status_code == 422:
                        raise AMKRAccountError(f"AMKR {feature}查询参数无效，请检查时间范围。")
                    if response.status_code != 200:
                        raise AMKRAccountError(
                            f"AMKR {feature}查询失败（HTTP {response.status_code}），请检查服务状态。"
                        )
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > max_bytes:
                            raise AMKRAccountError(
                                f"AMKR {feature}响应超过 {max_bytes // 1024 // 1024} MiB 安全上限。"
                            )
        payload = json.loads(content)
    except (TimeoutError, httpx.TimeoutException) as exc:
        raise AMKRAccountError(f"AMKR {feature}查询超时，请稍后重试或检查服务状态。") from exc
    except httpx.HTTPError as exc:
        raise AMKRAccountError("无法连接 AMKR，请检查服务地址、证书与网络。") from exc
    except (ValueError, UnicodeError) as exc:
        if isinstance(exc, AMKRAccountError):
            raise
        raise AMKRAccountError(f"AMKR 返回的不是有效的{feature} JSON。") from exc
    if not isinstance(payload, dict):
        raise AMKRAccountError(f"AMKR {feature}响应格式错误。")
    return payload


async def fetch_accounts(settings: AmkrSettings) -> dict:
    """获取一次全局资源快照；AMKR 的上游扇出预算为 60 秒。"""
    return await _fetch_json(
        settings,
        "/api/cpa-accounts",
        None,
        feature="账号资源",
        max_bytes=_MAX_ACCOUNT_BYTES,
        not_found="AMKR 未提供账号资源接口，请升级到支持 /api/cpa-accounts 的版本。",
        timeout=70,
    )


async def fetch_status(settings: AmkrSettings, hours: int) -> dict:
    """读取 AMKR 6.9+ 状态监控：概览、趋势与渠道健康矩阵共用同一时间窗口。"""
    return await _fetch_json(
        settings,
        "/ui/status-monitor.json",
        {"hours": str(hours), "group_by": "provider"},
        feature="状态监控",
        max_bytes=_MAX_STATUS_BYTES,
        not_found="AMKR 未提供状态监控接口：请升级到 6.9.0+ 并开启 WebUI。",
        timeout=40,
    )
