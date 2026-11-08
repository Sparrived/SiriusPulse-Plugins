"""群友的 QQ 头像：走公开的 qlogo 接口拿真头像，落盘缓存三天，拿不到就让版面画首字头像。"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

AVATAR_URL = "https://q1.qlogo.cn/g?b=qq&nk={uid}&s=140"
_TTL_SECONDS = 3 * 24 * 3600
_MAX_BYTES = 512 * 1024
_TIMEOUT = 5.0
_CONCURRENCY = 8


def sniff_mime(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def data_uri(data: bytes) -> str:
    mime = sniff_mime(data)
    if not mime:
        return ""
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")


async def fetch_avatar(uid: str) -> bytes | None:
    """下载一张 QQ 头像；任何失败都返回 None（版面会退回首字头像）。"""
    try:
        import httpx

        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            response = await client.get(AVATAR_URL.format(uid=uid))
    except Exception as exc:  # noqa: BLE001 - 头像是锦上添花
        logger.debug("头像下载失败 [%s]: %s", uid, type(exc).__name__)
        return None
    data = response.content
    if response.status_code != 200 or not data or len(data) > _MAX_BYTES or not sniff_mime(data):
        return None
    return data


async def load_avatars(uids: Iterable[str], cache_dir: Path | None) -> dict[str, str]:
    """批量拿头像，返回 uid → data URI（只含拿到了的）。"""
    wanted = [
        uid for uid in dict.fromkeys(str(u) for u in uids) if uid.isdigit() and 5 <= len(uid) <= 11
    ]
    if not wanted:
        return {}
    semaphore = asyncio.Semaphore(_CONCURRENCY)
    result: dict[str, str] = {}

    async def one(uid: str) -> None:
        cache = cache_dir / f"{uid}.img" if cache_dir else None
        if cache is not None and cache.is_file():
            try:
                if time.time() - cache.stat().st_mtime < _TTL_SECONDS:
                    uri = data_uri(cache.read_bytes())
                    if uri:
                        result[uid] = uri
                        return
            except OSError:
                pass
        async with semaphore:
            data = await fetch_avatar(uid)
        if not data:
            return
        uri = data_uri(data)
        if not uri:
            return
        result[uid] = uri
        if cache is not None:
            try:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_bytes(data)
            except OSError:
                pass

    await asyncio.gather(*(one(uid) for uid in wanted))
    return result
