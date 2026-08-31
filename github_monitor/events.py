"""GitHub Events and Compare API helpers for the monitor plugin."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any
from urllib.parse import quote

from .client import GitHubClient

logger = logging.getLogger(__name__)

_MAX_EVENTS_PER_PAGE = 30
_MAX_EVENT_PAGES = 4
_DEFAULT_RETRIES = 3
_NULL_SHA = "0" * 40


class EventFetchResult(list[dict[str, Any]]):
    """List-compatible Events API result carrying acknowledgement metadata.

    Keeping this a list subclass preserves compatibility with integrations that
    iterate over or compare the old helper result, while allowing the monitor
    to distinguish an empty successful response from a failed/incomplete fetch.
    """

    def __init__(
        self,
        events: list[dict[str, Any]] | None = None,
        *,
        success: bool = True,
        complete: bool = True,
        error: str = "",
    ) -> None:
        super().__init__(events or [])
        self.success = bool(success)
        self.complete = bool(complete)
        self.error = str(error or "")


def _retry_delay(response: Any, attempt: int) -> float:
    """Return a bounded retry delay, preferring GitHub's Retry-After hint."""
    headers = getattr(response, "headers", {}) or {}
    raw = headers.get("Retry-After", "") if hasattr(headers, "get") else ""
    try:
        return min(30.0, max(0.5, float(raw)))
    except (TypeError, ValueError):
        return min(30.0, 2.0 * attempt)


def _has_next_link(response: Any) -> bool:
    headers = getattr(response, "headers", {}) or {}
    link = headers.get("Link", "") if hasattr(headers, "get") else ""
    return bool(re.search(r"rel\s*=\s*[\"']next[\"']", str(link), re.IGNORECASE))


async def fetch_repo_events(
    client: GitHubClient,
    owner: str,
    repo: str,
    *,
    per_page: int = _MAX_EVENTS_PER_PAGE,
    max_pages: int = _MAX_EVENT_PAGES,
    max_retries: int = _DEFAULT_RETRIES,
    extra_headers: dict[str, str] | None = None,
) -> EventFetchResult:
    """Fetch recent repository events with bounded Link-header pagination.

    ``success=False`` means the caller must retain its poll cursor.  When the
    page budget is exhausted while a ``next`` link remains, ``complete=False``
    likewise prevents acknowledging a partial backlog.
    """
    try:
        per_page = min(100, max(1, int(per_page)))
        max_pages = min(10, max(1, int(max_pages)))
    except (TypeError, ValueError) as exc:
        return EventFetchResult(success=False, complete=False, error=str(exc))
    path = f"/repos/{quote(str(owner), safe='')}/{quote(str(repo), safe='')}/events"
    headers = dict(extra_headers or {}) if extra_headers else None
    collected: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for page in range(1, max_pages + 1):
        params: dict[str, int] = {"per_page": per_page}
        if page > 1:
            params["page"] = page
        page_data: list[dict[str, Any]] | None = None
        response: Any = None
        last_error = ""
        for attempt in range(1, max_retries + 1):
            try:
                response = await client.get(path, params=params, headers=headers)
            except Exception as exc:
                last_error = f"request failed: {exc.__class__.__name__}"
                logger.warning("github: %s/%s Events API 请求失败: %s", owner, repo, exc)
                if attempt < max_retries:
                    await asyncio.sleep(2.0 * attempt)
                    continue
                return EventFetchResult(
                    collected, success=False, complete=False, error=last_error
                )
            status_code = getattr(response, "status_code", 0)
            if status_code in (403, 429):
                last_error = f"HTTP {status_code}"
                logger.warning(
                    "github: %s/%s API %d（可能触发速率限制，第 %d/%d 次）",
                    owner,
                    repo,
                    status_code,
                    attempt,
                    max_retries,
                )
                if attempt < max_retries:
                    await asyncio.sleep(_retry_delay(response, attempt))
                    continue
                return EventFetchResult(
                    collected, success=False, complete=False, error=last_error
                )
            if status_code == 200:
                try:
                    data = response.json()
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    return EventFetchResult(
                        collected,
                        success=False,
                        complete=False,
                        error=f"invalid JSON: {exc.__class__.__name__}",
                    )
                if not isinstance(data, list):
                    return EventFetchResult(
                        collected,
                        success=False,
                        complete=False,
                        error="Events API returned non-list",
                    )
                page_data = [item for item in data if isinstance(item, dict)]
                break
            last_error = f"HTTP {status_code}"
            logger.error("github: %s/%s Events API 返回 %d", owner, repo, status_code)
            if attempt < max_retries:
                await asyncio.sleep(2.0 * attempt)
                continue
            return EventFetchResult(
                collected, success=False, complete=False, error=last_error
            )
        if page_data is None:
            return EventFetchResult(
                collected, success=False, complete=False, error=last_error
            )
        for event in page_data:
            event_id = str(event.get("id", "")).strip()
            if not event_id:
                try:
                    event_id = json.dumps(event, sort_keys=True, ensure_ascii=False)[
                        :2000
                    ]
                except (TypeError, ValueError):
                    event_id = repr(event)
            if event_id not in seen_ids:
                seen_ids.add(event_id)
                collected.append(event)
        has_next = _has_next_link(response)
        if not page_data or not has_next:
            break
        if page >= max_pages:
            logger.warning("github: %s/%s Events API 达到分页预算，暂不确认游标", owner, repo)
            return EventFetchResult(
                collected, success=True, complete=False, error="page budget"
            )
    logger.debug("github: %s/%s 获取到 %d 条事件", owner, repo, len(collected))
    return EventFetchResult(collected, success=True, complete=True)


def _compare_path(owner: str, repo: str, before: str, head: str) -> str:
    return (
        f"/repos/{quote(str(owner), safe='')}/{quote(str(repo), safe='')}/compare/"
        f"{quote(str(before), safe='')}...{quote(str(head), safe='')}"
    )


async def fetch_compare_details(
    client: GitHubClient,
    owner: str,
    repo: str,
    before: str,
    head: str,
    *,
    max_retries: int = _DEFAULT_RETRIES,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """Return raw commit and file details for a push range."""
    if not before or not head or before == _NULL_SHA:
        return None
    path = _compare_path(owner, repo, before, head)
    headers = dict(extra_headers or {}) if extra_headers else None
    for attempt in range(1, max_retries + 1):
        try:
            resp = await client.get(path, headers=headers)
        except Exception as exc:
            logger.warning("github: %s/%s Compare API 请求失败: %s", owner, repo, exc)
            if attempt < max_retries:
                await asyncio.sleep(2.0 * attempt)
                continue
            return None
        if resp.status_code in (403, 429):
            logger.warning(
                "github: %s/%s Compare API %d（第 %d/%d 次）",
                owner,
                repo,
                resp.status_code,
                attempt,
                max_retries,
            )
            if attempt < max_retries:
                await asyncio.sleep(_retry_delay(resp, attempt))
                continue
            return None
        if resp.status_code == 200:
            try:
                data = resp.json()
            except (TypeError, ValueError, json.JSONDecodeError):
                return None
            return data if isinstance(data, dict) else None
        logger.warning("github: %s/%s Compare API 返回 %d", owner, repo, resp.status_code)
        if attempt < max_retries:
            await asyncio.sleep(2.0 * attempt)
            continue
        return None
    return None


async def fetch_compare_commit_count(
    client: GitHubClient,
    owner: str,
    repo: str,
    before: str,
    head: str,
    *,
    max_retries: int = _DEFAULT_RETRIES,
    extra_headers: dict[str, str] | None = None,
) -> int | None:
    """Return the commit count for a push range from Compare API."""
    data = await fetch_compare_details(
        client,
        owner,
        repo,
        before,
        head,
        max_retries=max_retries,
        extra_headers=extra_headers,
    )
    total_commits = data.get("total_commits") if data else None
    return total_commits if isinstance(total_commits, int) else None
