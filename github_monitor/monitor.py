"""GitHub 仓库活动监控被动 TOOL。

通过后台任务周期性轮询或 Webhook 实时推送两种模式监控指定 GitHub 仓库的事件
（Issues、PR、Release、Commit、Comment 等），检测到新活动后使用 Playwright 截取对应
页面截图，并生成人格风格的通知消息。

每个仓库可独立选择模式（poll 或 webhook），同一 TOOL 实例同时支持两种模式。

配置由 WebUI 写入 data_store（tool_data/github_monitor.json）：
{
    "api_base_url": "https://api.github.com",
    "poll_seconds": 120,
    "webhook_secret": "",
    "webhook_host": "127.0.0.1",
    "webhook_port": 0,
    "repos": [
        {
            "owner": "Sparrived",
            "repo": "SiriusChat",
            "mode": "poll",
            "events": ["issues", "pulls", "releases", "comments", "pushes"],
            "groups": ["gid_xxx"],
            "github_token": ""
        }
    ],
    "last_event_timestamps": {},
    "_last_poll_at": {}
}
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import re
import socket
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from .client import GitHubClient, validate_api_base_url
from .event_bridge import (
    get_coding_bot_login,
    get_issue_repos,
    notify_issue_comment,
    notify_issue_opened,
    notify_pr_event,
)
from .events import fetch_compare_details, fetch_repo_events
from .webhook import GitHubWebhookServer

logger = logging.getLogger(__name__)

_GITHUB_API_BASE = "https://api.github.com"
_DEFAULT_POLL_SECONDS = 120
_MIN_BG_INTERVAL = 30
_MAX_EVENTS_PER_PAGE = 30
_MAX_COMMITS_IN_BODY = 5
_MAX_CHANGED_FILES_IN_BODY = 12
_MAX_SCREENSHOT_RETRIES = 3
_MAX_WEBHOOK_BODY_BYTES = 2 * 1024 * 1024
_MAX_SCREENSHOT_HEIGHT = 6000
_MAX_SCREENSHOT_BYTES = 8 * 1024 * 1024
_MAX_ARTIFACT_FILES = 100
_MAX_ARTIFACT_BYTES = 100 * 1024 * 1024
_SCREENSHOT_TIMEOUT_SECONDS = 45.0
_MAX_CURSOR_IDS = 1000
_REPO_PART_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_GITHUB_WEB_HOSTS = frozenset({"github.com", "www.github.com"})
_GITHUB_ASSET_HOSTS = frozenset(
    {
        "github.com",
        "www.github.com",
        "github.githubassets.com",
        "avatars.githubusercontent.com",
        "user-images.githubusercontent.com",
        "images.githubusercontent.com",
        "raw.githubusercontent.com",
        "objects.githubusercontent.com",
    }
)
_screenshot_slots: asyncio.Semaphore | None = None

# Webhook 模式运行时状态（模块级，由 on_load/on_unload 管理）。
# The actual request context is installed before the listener starts so a
# request arriving in the bind/start window cannot be acknowledged and lost.
_webhook_server: GitHubWebhookServer | None = None
_webhook_ctx: Any = None


def _poll_lock_for(ctx: Any) -> asyncio.Lock:
    """Return one in-process lock attached to the current context.

    Keeping the lock on the context avoids retaining every historical persona
    context forever and prevents an ``id(ctx)`` reuse from crossing event loops.
    """
    lock = getattr(ctx, "_github_monitor_poll_lock", None)
    if not isinstance(lock, asyncio.Lock):
        lock = asyncio.Lock()
        try:
            setattr(ctx, "_github_monitor_poll_lock", lock)
        except Exception:
            # A slots-only compatibility context can still use this lock for
            # the current call; normal Plugin contexts retain it for reuse.
            return lock
    return lock


def _clear_poll_lock(ctx: Any) -> None:
    """Release the context-owned lock reference during plugin unload."""
    try:
        delattr(ctx, "_github_monitor_poll_lock")
    except (AttributeError, TypeError):
        pass

# PR 合并提交的消息模式（GitHub 自动生成）
_PR_MERGE_COMMIT_PATTERN = re.compile(r"^Merge pull request #\d+ from ")

# 用户配置的事件类型 → GitHub Event API type 集合
_EVENT_TYPE_FILTER: dict[str, set[str]] = {
    "issues": {"IssuesEvent"},
    "pulls": {"PullRequestEvent"},
    "releases": {"ReleaseEvent"},
    "comments": {
        "IssueCommentEvent",
        "PullRequestReviewCommentEvent",
        "CommitCommentEvent",
    },
    "pushes": {"PushEvent"},
}

# 事件类型 → 中文描述
_TYPE_DESC: dict[str, str] = {
    "IssuesEvent": "Issue",
    "PullRequestEvent": "Pull Request",
    "ReleaseEvent": "Release",
    "IssueCommentEvent": "评论 (Issue)",
    "PullRequestReviewCommentEvent": "评论 (PR Review)",
    "CommitCommentEvent": "评论 (Commit)",
    "PushEvent": "推送",
}

# 动作 → 中文描述
_ACTION_DESC: dict[str, str] = {
    "opened": "新建了",
    "closed": "关闭了",
    "reopened": "重新打开了",
    "edited": "编辑了",
    "deleted": "删除了",
    "published": "发布了",
    "created": "创建了",
    "merged": "合并了",
    "synchronize": "更新了",
}


def _normalize_repo_config(value: Any) -> dict[str, Any] | None:
    """Validate one repository entry at the monitor boundary."""
    if not isinstance(value, dict):
        return None
    owner = str(value.get("owner", "")).strip()
    repo = str(value.get("repo", "")).strip()
    if not _REPO_PART_PATTERN.fullmatch(owner) or not _REPO_PART_PATTERN.fullmatch(repo):
        return None
    mode = str(value.get("mode", "poll")).strip().casefold()
    if mode not in {"poll", "webhook"}:
        return None
    configured_events = value.get("events", [])
    if not isinstance(configured_events, (list, tuple, set)):
        return None
    events = [str(event).strip().casefold() for event in configured_events]
    events = list(dict.fromkeys(event for event in events if event in _EVENT_TYPE_FILTER))
    groups = _normalize_groups(value.get("groups", []))
    token = str(value.get("github_token", "") or "").strip()
    token_env = str(value.get("github_token_env", "") or "").strip()
    if token.startswith("env:") and not token_env:
        token_env = token[4:].strip()
        token = ""
    if token_env and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", token_env):
        token_env = ""
    return {
        **value,
        "owner": owner,
        "repo": repo,
        "mode": mode,
        "events": events,
        "groups": groups,
        # Tokens are accepted for backwards compatibility but are never
        # written by migration; new deployments should use github_token_env.
        "github_token": token,
        "github_token_env": token_env,
    }


def _normalize_groups(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return list(dict.fromkeys(str(group).strip() for group in value if str(group).strip()))


def _allowed_types(events: Any) -> set[str]:
    if not isinstance(events, (list, tuple, set)):
        return set()
    result: set[str] = set()
    for configured in events:
        result.update(_EVENT_TYPE_FILTER.get(str(configured).strip().casefold(), set()))
    return result


def _token_for_repo(repo_cfg: dict[str, Any]) -> str:
    """Resolve a repository token without accepting arbitrary env syntax."""
    env_name = str(repo_cfg.get("github_token_env", "") or "").strip()
    if env_name and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", env_name):
        return os.getenv(env_name, "").strip()
    token = str(repo_cfg.get("github_token", "") or "").strip()
    return token if not token.startswith("env:") else ""


def _safe_event_repo_name(repo_name: Any) -> str:
    text = str(repo_name or "").strip()
    owner, separator, repo = text.partition("/")
    if not separator or not _REPO_PART_PATTERN.fullmatch(owner) or not _REPO_PART_PATTERN.fullmatch(repo):
        return ""
    return f"{owner}/{repo}"


def _safe_github_url(url: Any, *, expected_repo: str = "") -> str:
    """Accept only a canonical public GitHub page for the configured repo."""
    text = str(url or "").strip()
    if not text or any(ord(char) < 0x20 for char in text):
        return ""
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError:
        return ""
    if (
        parsed.scheme.casefold() != "https"
        or parsed.username
        or parsed.password
        or parsed.query
        or port not in (None, 443)
    ):
        return ""
    host = (parsed.hostname or "").casefold().rstrip(".")
    if host not in _GITHUB_WEB_HOSTS:
        return ""
    path = parsed.path or "/"
    decoded_path = unquote(path)
    if (
        "//" in path
        or ".." in path
        or "\\" in path
        or "//" in decoded_path
        or ".." in decoded_path
        or "\\" in decoded_path
    ):
        return ""
    if expected_repo:
        expected_path = f"/{expected_repo}"
        if path != expected_path and not path.startswith(f"{expected_path}/"):
            return ""
    fragment = parsed.fragment
    if any(ord(char) < 0x20 for char in fragment):
        return ""
    return f"https://{host}{path.rstrip('/') or '/'}"


def _github_page_url(
    repo_name: Any,
    kind: str = "",
    identifier: Any = "",
    *,
    suffix: str = "",
) -> str:
    """Build a GitHub URL from validated repository and opaque identifiers."""
    repo = _safe_event_repo_name(repo_name)
    if not repo:
        return ""
    path = f"/{repo}"
    if kind:
        safe_kind = {
            "issues": "issues",
            "pull": "pull",
            "commit": "commit",
            "compare": "compare",
            "releases": "releases",
        }.get(kind)
        if safe_kind is None:
            return ""
        path += f"/{safe_kind}"
        if identifier:
            value = str(identifier).strip()
            if not value or len(value) > 200 or any(
                char in value for char in ("/", "\\", "?", "#")
            ):
                return ""
            if safe_kind == "compare":
                revisions = value.split("...")
                if len(revisions) != 2:
                    return ""
                before, head = (_safe_revision(item) for item in revisions)
                if not before or not head:
                    return ""
                path += f"/{before}...{head}"
            elif ".." in value:
                return ""
            else:
                path += f"/{quote(value, safe='.-_')}"
    if suffix:
        if not suffix.startswith("/") or ".." in suffix or "\\" in suffix:
            return ""
        path += suffix
    return _safe_github_url(f"https://github.com{path}", expected_repo=repo)


def _safe_text(value: Any, max_len: int = 500) -> str:
    """Normalize untrusted GitHub text before logging or sending to an LLM."""
    text = str(value or "")
    text = "".join(char if char in "\n\t" or ord(char) >= 0x20 else " " for char in text)
    return _truncate_text(text, max_len)


def _safe_revision(value: Any) -> str:
    """Return a Git object id suitable for a GitHub path, or an empty string."""
    text = str(value or "").strip()
    if re.fullmatch(r"[0-9a-fA-F]{7,64}", text):
        return text.lower()
    return ""


def _safe_number(value: Any) -> str:
    """Return a positive decimal issue/PR/comment identifier."""
    if isinstance(value, bool):
        return ""
    text = str(value or "").strip()
    if re.fullmatch(r"[1-9][0-9]{0,11}", text):
        return text
    return ""


def _normalize_configured_repos(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in value:
        config = _normalize_repo_config(item)
        if config is None:
            continue
        key = f"{config['owner']}/{config['repo']}"
        if key in seen:
            continue
        seen.add(key)
        normalized.append(config)
    return normalized


def _normalize_allowed_hosts(value: Any) -> set[str]:
    """Return explicit API hosts, always retaining the public GitHub API."""
    hosts = {"api.github.com"}
    if isinstance(value, (list, tuple, set)):
        for item in value:
            host = str(item or "").strip().casefold().rstrip(".")
            if re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}[a-z0-9]", host):
                hosts.add(host)
    return hosts


def _event_identifier(event: dict[str, Any]) -> str:
    """Return a stable bounded event key, including events without GitHub IDs."""
    value = str(event.get("id", "") or "").strip()
    if value:
        return value[:200]
    try:
        import json

        encoded = json.dumps(event, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        encoded = repr(event)
    return "fingerprint:" + hashlib.sha256(encoded.encode("utf-8", errors="replace")).hexdigest()


def _event_timestamp(event: dict[str, Any]) -> str:
    """Return only a bounded, ISO-like timestamp suitable for cursor ordering."""
    value = event.get("created_at", "")
    if not isinstance(value, str):
        return ""
    timestamp = value.strip()
    if not timestamp or len(timestamp) > 40 or any(ord(char) < 0x20 for char in timestamp):
        return ""
    # GitHub uses UTC RFC3339 timestamps.  Keeping a strict shape prevents a
    # hostile payload from changing lexical cursor ordering.
    if not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:?\d{2})",
        timestamp,
    ):
        return ""
    return timestamp


def _persist_state(store: Any, values: dict[str, Any]) -> None:
    """Persist state atomically when the store supports a real update method."""
    update = getattr(type(store), "update", None)
    if callable(update):
        update(store, values)
        return
    for key, value in values.items():
        store.set(key, value)


def create_background_tasks(ctx: Any) -> list[Any]:
    """注册周期性 GitHub 事件轮询后台任务。

    后台以最小间隔唤醒（30s），由 _poll_github_events 内部根据
    tool 配置中的 poll_seconds 自行节流。
    """
    from sirius_pulse.extension_runtime import BackgroundTaskSpec

    async def _check() -> None:
        await _poll_github_events(ctx)

    return [
        BackgroundTaskSpec(
            name="github_monitor_poll",
            interval_seconds=_MIN_BG_INTERVAL,
            task_func=_check,
        )
    ]


async def create_on_load(ctx: Any) -> None:
    """Start (or atomically replace) the loopback Webhook listener."""
    global _webhook_server, _webhook_ctx
    old_server = _webhook_server
    _webhook_server = None
    _webhook_ctx = None
    if old_server is not None:
        await old_server.stop()

    store = ctx.get_data_store("github_monitor")
    store.reload()
    repos = _normalize_configured_repos(store.get("repos", []))
    webhook_repos = [r for r in repos if r.get("mode") == "webhook"]
    if not webhook_repos:
        logger.debug("github_monitor: 无 webhook 模式仓库，不启动 Webhook 服务器")
        return

    secret = str(store.get("webhook_secret", "") or "").strip()
    host = str(store.get("webhook_host", "127.0.0.1") or "127.0.0.1").strip()
    try:
        port = int(store.get("webhook_port", 0) or 0)
    except (TypeError, ValueError):
        logger.warning("github_monitor: webhook_port 无效，使用随机端口")
        port = 0
    if not 0 <= port <= 65535:
        logger.warning("github_monitor: webhook_port 超出范围，使用随机端口")
        port = 0
    loopback_hosts = {"127.0.0.1", "localhost", "::1"}
    if not host or host.casefold() not in loopback_hosts:
        logger.error("github_monitor: Webhook 仅允许监听回环地址")
        return
    allow_unsigned_local = _strict_bool(store.get("allow_unsigned_local", False), default=False)
    if not secret and not allow_unsigned_local:
        logger.error("github_monitor: Webhook 未配置 secret，拒绝启动")
        return

    server = GitHubWebhookServer(
        secret=secret,
        host=host,
        port=port,
        allow_unsigned_local=allow_unsigned_local,
    )
    webhook_repo_names = {f"{r['owner']}/{r['repo']}" for r in webhook_repos}
    server.set_repo_filter(lambda repo_name: repo_name in webhook_repo_names)
    server.set_event_filter(
        lambda event_name: event_name
        in {
            "issues",
            "pull_request",
            "push",
            "release",
            "issue_comment",
            "pull_request_review_comment",
            "commit_comment",
        }
    )
    for event_name in (
        "issues",
        "pull_request",
        "push",
        "release",
        "issue_comment",
        "pull_request_review_comment",
        "commit_comment",
    ):
        server.add_handler(event_name, _handle_webhook_event)

    try:
        actual_port = await server.start()
    except Exception:
        await server.stop()
        logger.exception("github_monitor: Webhook 启动失败")
        return
    _webhook_server = server
    _webhook_ctx = ctx
    try:
        store.set("_webhook_port", actual_port)
        store.save()
    except Exception:
        await server.stop()
        _webhook_server = None
        _webhook_ctx = None
        logger.exception("github_monitor: Webhook 端口状态保存失败")
        return
    logger.info(
        "github_monitor: Webhook 模式已启动，端口 %s，监控 %d 个仓库",
        actual_port,
        len(webhook_repos),
    )


async def create_on_unload(ctx: Any) -> None:
    """停止 GitHub Webhook 服务器。

    该函数由引擎在 TOOL 卸载时通过 asyncio.ensure_future 调度执行。
    """
    global _webhook_server, _webhook_ctx
    if _webhook_server is not None:
        await _webhook_server.stop()
        _webhook_server = None
        _webhook_ctx = None
        logger.info("github_monitor: Webhook 模式已停止")


# ═══════════════════════════════════════════════════════════════════════
# 主轮询逻辑
# ═══════════════════════════════════════════════════════════════════════


async def _poll_github_events(ctx: Any) -> None:
    """Run one serialized poll for this persona/context."""
    lock = _poll_lock_for(ctx)
    if lock.locked():
        logger.debug("github_monitor: 已有轮询运行，跳过并发调用")
        return
    async with lock:
        await _poll_github_events_unlocked(ctx)


async def _poll_github_events_unlocked(ctx: Any) -> None:
    """遍历所有监控仓库，拉取新事件并触发通知。

    从 tool data_store 读取 poll_seconds（默认 120s）控制实际 API 调用频率，
    防止频繁请求触发 GitHub 速率限制。
    """
    try:
        store = ctx.get_data_store("github_monitor")
        # 每轮先从磁盘重载，以便 WebUI 修改 poll_seconds / repos 后无需重启即生效
        store.reload()
        repos = _normalize_configured_repos(store.get("repos", []))
        if not repos:
            return

        try:
            poll_seconds = min(
                3600.0,
                max(30.0, float(store.get("poll_seconds", _DEFAULT_POLL_SECONDS))),
            )
        except (TypeError, ValueError):
            poll_seconds = float(_DEFAULT_POLL_SECONDS)
        api_base_url = str(store.get("api_base_url", "") or "").strip() or _GITHUB_API_BASE
        allowed_hosts = _normalize_allowed_hosts(store.get("api_allowed_hosts", []))
        try:
            api_base_url = validate_api_base_url(
                api_base_url,
                allowed_hosts=allowed_hosts,
            )
        except ValueError as exc:
            logger.error("github_monitor: 拒绝使用不安全 API 地址: %s", exc)
            return

        last_ts: dict[str, str] = dict(store.get("last_event_timestamps", {}) or {})
        last_ids: dict[str, list[str]] = {
            str(key): [str(item) for item in value if str(item)]
            for key, value in dict(store.get("last_event_ids", {}) or {}).items()
            if isinstance(value, (list, tuple, set))
        }
        last_poll: dict[str, float] = dict(store.get("_last_poll_at", {}) or {})

        # This value is persisted, so it must remain comparable after a restart.
        now = time.time()

        async with GitHubClient(
            base_url=api_base_url,
            timeout=30.0,
            allowed_hosts=allowed_hosts,
        ) as client:
            for repo_cfg in repos:
                # 跳过 webhook 模式的仓库（由 Webhook 服务器实时推送处理）
                if repo_cfg.get("mode") == "webhook":
                    continue

                owner = repo_cfg["owner"]
                repo = repo_cfg["repo"]
                events_config = repo_cfg.get("events", [])
                target_groups = _normalize_groups(repo_cfg.get("groups", []))
                github_token = _token_for_repo(repo_cfg)

                if not events_config or not target_groups:
                    continue

                allowed_types = _allowed_types(events_config)
                if not allowed_types:
                    continue

                repo_key = f"{owner}/{repo}"

                # 按 poll_seconds 节流：距离上次 API 调用未满 poll_seconds 则跳过
                prev = last_poll.get(repo_key, 0.0)
                if now - prev < poll_seconds:
                    continue

                since = last_ts.get(repo_key)

                # 拉取事件（带重试，per-repo token 通过 extra_headers 注入）
                logger.debug("github_monitor: 正在获取 %s 事件... (%s)", repo_key, api_base_url)
                extra_headers: dict[str, str] = {}
                if github_token:
                    extra_headers["Authorization"] = f"Bearer {github_token}"
                events = await fetch_repo_events(client, owner, repo, extra_headers=extra_headers)
                if not getattr(events, "success", True):
                    # API 错误与空成功响应必须区分；失败不得确认游标。
                    logger.warning(
                        "github_monitor: %s Events API 失败，保留游标: %s",
                        repo_key,
                        getattr(events, "error", "unknown error"),
                    )
                    last_poll[repo_key] = now
                    _persist_state(store, {"_last_poll_at": last_poll})
                    continue
                if not getattr(events, "complete", True):
                    # 分页预算耗尽时结果不完整，绝不推进游标或发送部分批次。
                    logger.warning("github_monitor: %s Events API 结果不完整，等待下次轮询", repo_key)
                    last_poll[repo_key] = now
                    _persist_state(store, {"_last_poll_at": last_poll})
                    continue
                if not events:
                    logger.debug("github_monitor: %s 无新事件", repo_key)
                    last_poll[repo_key] = now
                    _persist_state(store, {"_last_poll_at": last_poll})
                    continue

                # Events API 按时间倒序返回，但事件 ID 才是可靠的去重键。
                # 对旧配置继续使用时间游标，并允许同一时间戳下的新 ID 通过。
                previous_ids = set(last_ids.get(repo_key, []))
                cursor_candidates: list[dict[str, Any]] = []
                new_events: list[dict[str, Any]] = []
                skipped_pr_merges = 0
                for event in events:
                    if not isinstance(event, dict):
                        continue
                    created_at = _event_timestamp(event)
                    event_id = _event_identifier(event)
                    if since:
                        if created_at and created_at < since:
                            continue
                        # Timestamp-less events cannot be ordered reliably, but
                        # their fingerprint/ID is still a durable de-dup key.
                        if event_id in previous_ids:
                            continue
                        if created_at == since and event_id in previous_ids:
                            continue
                    elif event_id in previous_ids:
                        continue
                    cursor_candidates.append(event)
                    payload_repo = event.get("repo", {})
                    payload_repo_name = (
                        _safe_event_repo_name(payload_repo.get("name", ""))
                        if isinstance(payload_repo, dict)
                        else ""
                    )
                    # The request is scoped to one configured repository.  A
                    # malformed/mismatched item is acknowledged as observed,
                    # but must never be rendered with another repository URL.
                    if payload_repo_name and payload_repo_name != repo_key:
                        continue
                    if event.get("type", "") not in allowed_types:
                        continue
                    if _is_pr_merge_push_event(event):
                        skipped_pr_merges += 1
                        continue
                    new_events.append(event)

                new_events.sort(
                    key=lambda item: (_event_timestamp(item), _event_identifier(item)),
                    reverse=True,
                )
                cursor_candidates.sort(
                    key=lambda item: (_event_timestamp(item), _event_identifier(item)),
                    reverse=True,
                )

                if skipped_pr_merges:
                    logger.debug(
                        "github_monitor: %s 跳过了 %d 条 PR 合并 Push 事件（与 PullRequestEvent 重复）",
                        repo_key,
                        skipped_pr_merges,
                    )

                def _cursor_update() -> dict[str, Any]:
                    if not cursor_candidates:
                        return {"_last_poll_at": last_poll}
                    newest = cursor_candidates[0]
                    newest_ts = _event_timestamp(newest)
                    previous_cursor_ts = since or ""
                    if newest_ts:
                        last_ts[repo_key] = newest_ts
                        ids_at_cursor = [
                            _event_identifier(item)
                            for item in cursor_candidates
                            if _event_timestamp(item) == newest_ts
                        ]
                        # When the cursor remains on one timestamp, retain IDs
                        # from the previous page/run; otherwise the next poll
                        # would rediscover older events at that same timestamp.
                        prior = previous_ids if newest_ts == previous_cursor_ts else set()
                        ids_at_cursor = list(dict.fromkeys(
                            [*ids_at_cursor, *sorted(prior)]
                        ))
                    else:
                        # A malformed/missing timestamp cannot advance the time
                        # cursor.  Persist bounded fingerprints so it is still
                        # acknowledged exactly once after a successful delivery.
                        ids_at_cursor = [
                            _event_identifier(item) for item in cursor_candidates
                        ]
                        ids_at_cursor = list(dict.fromkeys(
                            [*ids_at_cursor, *sorted(previous_ids)]
                        ))
                    if ids_at_cursor:
                        last_ids[repo_key] = ids_at_cursor[:_MAX_CURSOR_IDS]
                    last_poll[repo_key] = now
                    return {
                        "last_event_timestamps": last_ts,
                        "last_event_ids": last_ids,
                        "_last_poll_at": last_poll,
                    }

                if not new_events:
                    # API 调用成功，新事件筛选后为空；过滤掉的事件仍应推进游标，
                    # 否则一个不关注的最新事件会在每轮被重复读取。
                    _persist_state(store, _cursor_update())
                    continue

                is_first_poll = not since

                # 首次轮询（未有历史时间戳）：仅更新时间戳，跳过本次通知，
                # 避免把历史事件全部播报导致刷屏。
                if is_first_poll:
                    _persist_state(store, _cursor_update())
                    logger.info(
                        "github_monitor: %s 首次同步完成，已跳过 %d 条历史事件",
                        repo_key,
                        len(new_events),
                    )
                    continue

                # 通过 event_bridge 通知插件（coding_agent 等）。桥接失败时
                # 不推进游标，避免下游自动化丢失事件。
                bridge_failed = False
                for event in new_events:
                    etype = event.get("type", "")
                    payload = event.get("payload", {})
                    if not isinstance(payload, dict):
                        continue
                    bridge_body = {
                        "action": payload.get("action", ""),
                        "repository": {"full_name": repo_key},
                        "sender": event.get("actor", {}),
                    }
                    acknowledged = True
                    if etype == "IssuesEvent" and payload.get("action") == "opened":
                        bridge_body["issue"] = payload.get("issue", {})
                        acknowledged = await notify_issue_opened(bridge_body, repo_key)
                    elif etype == "PullRequestEvent" and payload.get("action") in (
                        "opened",
                        "synchronize",
                    ):
                        bridge_body["pull_request"] = payload.get("pull_request", {})
                        acknowledged = await notify_pr_event(
                            bridge_body, repo_key, str(payload.get("action", ""))
                        )
                    elif etype in {
                        "IssueCommentEvent",
                        "PullRequestReviewCommentEvent",
                        "CommitCommentEvent",
                    } and payload.get("action") == "created":
                        bridge_body["comment"] = payload.get("comment", {})
                        if etype == "IssueCommentEvent":
                            bridge_body["issue"] = payload.get("issue", {})
                        elif etype == "PullRequestReviewCommentEvent":
                            bridge_body["pull_request"] = payload.get("pull_request", {})
                        acknowledged = await notify_issue_comment(bridge_body, repo_key)
                    if not acknowledged:
                        bridge_failed = True
                if bridge_failed:
                    logger.warning("github_monitor: %s event bridge 未确认，保留游标", repo_key)
                    continue

                # 将 PushEvent 与其他事件分离。Push 只能在同一 ref 且
                # before/head 连续时合并，避免跨分支生成错误的 Compare 范围。
                push_raw: list[dict[str, Any]] = [
                    e for e in new_events if e.get("type") == "PushEvent"
                ]
                other_events: list[dict[str, Any]] = [
                    e for e in new_events if e.get("type") != "PushEvent"
                ]

                # 非 PushEvent：按规范 URL 分组合并
                grouped: dict[str, list[dict[str, Any]]] = {}
                bot_login = get_coding_bot_login()
                coding_repos = get_issue_repos()
                for event in reversed(other_events):
                    event_info = _extract_event_info(event, expected_repo=repo_key)
                    # coding 接管仓库：仅当评论作者是 AI bot 或非评论事件时才推送通知
                    if bot_login and event_info.get("repo", "") in coding_repos:
                        if event.get("type", "") in {
                            "IssueCommentEvent",
                            "PullRequestReviewCommentEvent",
                            "CommitCommentEvent",
                        }:
                            actor = event.get("actor")
                            actor_login = actor.get("login", "") if isinstance(actor, dict) else ""
                            if actor_login and actor_login != bot_login:
                                logger.debug(
                                    "github_monitor: %s 跳过非AI评论 @%s",
                                    repo_key,
                                    actor_login,
                                )
                                continue
                    canonical = event_info.get("canonical_url", event_info.get("url", ""))
                    grouped.setdefault(canonical, []).append(event_info)

                # Push 仅按同一 ref 的连续范围分别聚合。Compare API 失败或
                # 数据缺失时仍发送原始提交摘要，但绝不构造不可信的范围。
                push_groups = _group_contiguous_push_events(push_raw)
                for push_index, push_group in enumerate(push_groups):
                    ordered_group = sorted(
                        push_group,
                        key=lambda item: (_event_timestamp(item), _event_identifier(item)),
                    )
                    oldest_payload = ordered_group[0].get("payload")
                    oldest_payload = oldest_payload if isinstance(oldest_payload, dict) else {}
                    newest_payload = ordered_group[-1].get("payload")
                    newest_payload = newest_payload if isinstance(newest_payload, dict) else {}
                    before = _push_sha(oldest_payload, "before")
                    head = _push_sha(newest_payload, "head", "after")
                    compare_data: dict[str, Any] | None = None
                    if before and head and before != "0" * 40:
                        compare_data = await fetch_compare_details(
                            client,
                            owner,
                            repo,
                            before,
                            head,
                            extra_headers=extra_headers,
                        )
                    commit_count = (
                        compare_data.get("total_commits")
                        if isinstance(compare_data, dict)
                        and isinstance(compare_data.get("total_commits"), int)
                        and not isinstance(compare_data.get("total_commits"), bool)
                        else None
                    )
                    compare_commits = (
                        compare_data.get("commits", [])
                        if isinstance(compare_data, dict)
                        and isinstance(compare_data.get("commits"), list)
                        else []
                    )
                    changed_files = (
                        compare_data.get("files", [])
                        if isinstance(compare_data, dict)
                        and isinstance(compare_data.get("files"), list)
                        else []
                    )
                    merged_push = _merge_push_events(
                        ordered_group,
                        expected_repo=repo_key,
                        commit_count=commit_count,
                        commit_details=compare_commits,
                        changed_files=changed_files,
                    )
                    if merged_push:
                        push_id = str(merged_push.get("event_id", push_index))
                        grouped[f"__push_{repo_key}_{push_id}"] = [merged_push]
                        logger.info(
                            "github_monitor: %s 合并了 %d 笔连续 %s 推送",
                            repo_key,
                            len(ordered_group),
                            _push_ref(ordered_group[0]) or "未知分支",
                        )

                logger.info(
                    "github_monitor: %s 发现 %d 条新事件，合并为 %d 组",
                    repo_key,
                    len(new_events),
                    len(grouped),
                )

                delivery_failed = False
                for canonical_url, group in grouped.items():
                    merged_info = _merge_event_group(group)

                    # coding 接管仓库：跳过标签添加/删除事件，AI会自动管理标签
                    if merged_info.get("type") == "IssuesEvent" and merged_info.get("action") in (
                        "labeled",
                        "unlabeled",
                    ):
                        if repo_key in get_issue_repos():
                            logger.debug(
                                "github_monitor: %s 跳过标签事件 %s",
                                repo_key,
                                merged_info.get("action"),
                            )
                            continue

                    # 截图：PR 事件截 /files diff 页，Push 截 compare 页，其余截主页面
                    screenshot_path: str | None = None
                    screenshot_url = _safe_github_url(
                        merged_info.get("screenshot_url", "")
                        or merged_info.get("url", "")
                        or canonical_url,
                        expected_repo=repo_key,
                    )
                    event_id = str(merged_info.get("event_id", "") or "").strip()
                    if not event_id:
                        event_id = hashlib.sha256(
                            f"{repo_key}|{canonical_url}".encode("utf-8")
                        ).hexdigest()[:24]
                    if screenshot_url:
                        try:
                            screenshot_path = await _take_screenshot(
                                screenshot_url, store, event_id=event_id
                            )
                        except Exception as exc:
                            logger.warning("github_monitor: 截图失败 (event=%s): %s", event_id, exc)

                    # LLM 生成：每个合并组仅调用一次
                    notification = await _generate_notification_text(ctx, merged_info)

                    if not notification:
                        continue

                    merged_count = merged_info.get("merged_count", 1)
                    ctx.log_inner_thought(
                        f"github_monitor: [{merged_info['repo']}] {merged_info['actor']} "
                        f"{'、'.join(merged_info.get('merged_actions', [merged_info.get('action_cn', '') + merged_info.get('type_desc', '')]))} "
                        f"({'合并' + str(merged_count) + '条事件' if merged_count > 1 else '1条事件'})"
                        f" - 通知已生成，分发到 {len(target_groups)} 个群"
                    )

                    # 分发给所有订阅群
                    for gid in target_groups:
                        try:
                            await _dispatch_notification(
                                ctx, gid, notification, screenshot_path, event_id=event_id
                            )
                        except Exception as exc:
                            delivery_failed = True
                            logger.warning(
                                "github_monitor: 分发 %s 失败 (gid=%s): %s",
                                repo_key,
                                gid,
                                exc,
                            )

                # 只有所有已生成通知均成功交给统一主动消息管线后才推进游标；
                # 可重试的投递失败不能被永久确认。
                if delivery_failed:
                    logger.warning("github_monitor: %s 下游投递失败，保留游标等待重试", repo_key)
                    continue
                _persist_state(store, _cursor_update())
    except Exception as exc:
        logger.error(
            "github_monitor: 轮询异常 (%s)，将在下一周期重试: %s",
            exc.__class__.__name__,
            exc,
        )


def _is_pr_merge_push_event(event: dict[str, Any]) -> bool:
    """判断一个 PushEvent 是否全部由 PR 合并提交构成。

    PR 合并后 GitHub 会自动生成 "Merge pull request #XX from ..." 提交并推送，
    这些 PushEvent 与 PullRequestEvent（merged）重复，应跳过以去噪。
    """
    if event.get("type", "") != "PushEvent":
        return False
    payload = event.get("payload", {}) or {}
    if not isinstance(payload, dict):
        return False
    raw_commits = payload.get("commits", [])
    if not isinstance(raw_commits, list):
        return False
    commits = [item for item in raw_commits if isinstance(item, dict)]
    if not commits or len(commits) != len(raw_commits):
        return False
    return all(_PR_MERGE_COMMIT_PATTERN.match(_commit_message(item)) for item in commits)


# ═══════════════════════════════════════════════════════════════════════
# 事件信息提取
# ═══════════════════════════════════════════════════════════════════════


def _clean_canonical_url(url: str) -> str:
    """规范化 URL 用于分组合并：去除 fragment (#xxx) 和尾部斜杠。

    确保如 /pull/2 与 /pull/2#issuecomment-xxx 能正确归入同一组。
    """
    if not url:
        return url
    # 去除 fragment 锚点
    cleaned = url.split("#")[0]
    # 去除尾部斜杠
    cleaned = cleaned.rstrip("/")
    return cleaned


def _commit_message(commit: dict[str, Any]) -> str:
    nested = commit.get("commit", {})
    nested_message = nested.get("message", "") if isinstance(nested, dict) else ""
    message = commit.get("message") or nested_message
    return str(message or "").strip()


def _commit_author(commit: dict[str, Any]) -> str:
    nested = commit.get("commit", {})
    nested_author = nested.get("author", {}) if isinstance(nested, dict) else {}
    author = commit.get("author") or nested_author
    if not isinstance(author, dict):
        return "未知作者"
    return str(author.get("login") or author.get("name") or "未知作者")


def _commit_subject(commit: dict[str, Any]) -> str:
    message = _commit_message(commit)
    return message.splitlines()[0][:160] if message else "未提供提交说明"


def _extract_event_info(
    event: dict[str, Any],
    *,
    expected_repo: str = "",
) -> dict[str, Any]:
    """Extract an Events API payload without trusting its URL fields.

    GitHub payload URLs are treated as display-only untrusted data.  Links used
    for navigation are rebuilt from the repository selected by configuration and
    validated by :func:`_safe_github_url`.
    """
    etype = _safe_text(event.get("type", "未知事件"), 80)
    repo_info = event.get("repo", {})
    actor = event.get("actor", {})
    payload = event.get("payload", {}) or {}
    if not isinstance(repo_info, dict):
        repo_info = {}
    if not isinstance(actor, dict):
        actor = {}
    if not isinstance(payload, dict):
        payload = {}

    configured_repo = _safe_event_repo_name(expected_repo)
    payload_repo = _safe_event_repo_name(repo_info.get("name", ""))
    repo_name = configured_repo or payload_repo or "未知仓库"
    actor_name = _safe_text(actor.get("display_login") or actor.get("login", "未知用户"), 120)
    html_url = ""
    screenshot_url = ""
    title = ""
    body = ""
    action = _safe_text(payload.get("action", ""), 40)
    action_cn = _ACTION_DESC.get(action, action)
    branch = ""
    commit_count: int | None = None
    commits: list[dict[str, Any]] = []
    type_desc = _TYPE_DESC.get(etype, etype)

    if etype == "IssuesEvent":
        issue = payload.get("issue", {})
        if not isinstance(issue, dict):
            issue = {}
        title = _safe_text(issue.get("title", ""), 300)
        body = _safe_text(issue.get("body", ""))
        html_url = _github_page_url(repo_name, "issues", _safe_number(issue.get("number")))
    elif etype == "PullRequestEvent":
        pr_data = payload.get("pull_request", {})
        if not isinstance(pr_data, dict):
            pr_data = {}
        title = _safe_text(pr_data.get("title", ""), 300)
        body = _safe_text(pr_data.get("body", ""))
        html_url = _github_page_url(repo_name, "pull", _safe_number(pr_data.get("number")))
        if pr_data.get("merged") and action == "closed":
            action_cn = "合并了"
    elif etype == "ReleaseEvent":
        release = payload.get("release", {})
        if not isinstance(release, dict):
            release = {}
        title = _safe_text(release.get("name") or release.get("tag_name", ""), 300)
        body = _safe_text(release.get("body", ""))
        # Do not put an arbitrary tag into a URL; the repository releases page
        # is a safe, useful fallback even when a tag contains slash characters.
        html_url = _github_page_url(repo_name, "releases")
    elif etype in (
        "IssueCommentEvent",
        "PullRequestReviewCommentEvent",
        "CommitCommentEvent",
    ):
        comment = payload.get("comment", {})
        if not isinstance(comment, dict):
            comment = {}
        body = _safe_text(comment.get("body", ""))
        if etype == "IssueCommentEvent":
            issue = payload.get("issue", {})
            if not isinstance(issue, dict):
                issue = {}
            title = _safe_text(issue.get("title", ""), 300)
            number = _safe_number(issue.get("number"))
            is_pr = bool(issue.get("pull_request"))
            type_desc = "评论 (PR)" if is_pr else "评论 (Issue)"
            html_url = _github_page_url(repo_name, "pull" if is_pr else "issues", number)
        elif etype == "PullRequestReviewCommentEvent":
            pr_data = payload.get("pull_request", {})
            if not isinstance(pr_data, dict):
                pr_data = {}
            title = _safe_text(pr_data.get("title", ""), 300)
            html_url = _github_page_url(repo_name, "pull", _safe_number(pr_data.get("number")))
            type_desc = "评论 (PR Review)"
        else:
            html_url = _github_page_url(
                repo_name,
                "commit",
                _safe_revision((comment.get("commit_id") or comment.get("sha"))),
            )
    elif etype == "PushEvent":
        raw_commits = payload.get("commits")
        commits = [item for item in raw_commits if isinstance(item, dict)] if isinstance(raw_commits, list) else []
        commit_count = len(commits) if isinstance(raw_commits, list) else None
        ref = _safe_text(payload.get("ref", ""), 200)
        branch = ref.replace("refs/heads/", "") if ref.startswith("refs/heads/") else ref
        commit_summary = f"{len(commits)} 个提交" if isinstance(raw_commits, list) else "提交数量未知"
        title = f"{commit_summary} → {branch}"
        body = "\n".join(f"- {_safe_text(_commit_subject(c), 200)}" for c in commits[:_MAX_COMMITS_IN_BODY])
        null_sha = "0" * 40
        before_sha = _safe_revision(payload.get("before"))
        head_sha = _safe_revision(payload.get("head"))
        compare_url = ""
        if before_sha and head_sha and before_sha != null_sha:
            compare_url = _github_page_url(repo_name, "compare", f"{before_sha}...{head_sha}")
        first_sha = (
            _safe_revision(commits[0].get("sha") or commits[0].get("id"))
            if commits
            else ""
        )
        html_url = compare_url or _github_page_url(repo_name, "commit", first_sha) or _github_page_url(repo_name)
        screenshot_url = compare_url or html_url

    if etype in ("PullRequestEvent", "PullRequestReviewCommentEvent"):
        screenshot_url = f"{html_url}/files" if html_url else ""
    elif etype != "PushEvent":
        screenshot_url = html_url

    safe_url = _safe_github_url(html_url, expected_repo=repo_name) if html_url else ""
    safe_screenshot = _safe_github_url(screenshot_url, expected_repo=repo_name) if screenshot_url else ""
    event_id = _event_identifier(event)
    return {
        "repo": repo_name,
        "type": etype,
        "type_desc": type_desc,
        "actor": actor_name,
        "action": action,
        "action_cn": action_cn,
        "title": title,
        "body": body,
        "url": safe_url,
        "screenshot_url": safe_screenshot,
        "canonical_url": _clean_canonical_url(safe_url),
        "created_at": _safe_text(event.get("created_at", ""), 40),
        "event_id": event_id,
        "branch": branch,
        "commit_count": commit_count,
        "commits": commits[:_MAX_COMMITS_IN_BODY],
    }


def _truncate_text(text: str, max_len: int = 500) -> str:
    """截断过长文本，用于 body 摘要。"""
    if not text:
        return ""
    cleaned = re.sub(r"```[\s\S]*?```", "[代码块已省略]", text)
    cleaned = re.sub(r"!\[.*?\]\(.*?\)", "[图片已省略]", cleaned)
    cleaned = re.sub(r"\[([^\]]*)\]\([^)]+\)", r"\1", cleaned)
    if len(cleaned) > max_len:
        return cleaned[:max_len] + "..."
    return cleaned


# ═══════════════════════════════════════════════════════════════════════
# 事件合并
# ═══════════════════════════════════════════════════════════════════════


def _merge_event_group(events: list[dict[str, Any]]) -> dict[str, Any]:
    """将同一规范页面（同一 canonical_url）的多个事件合并为一个。

    合并规则：
    - 以第一个事件为基础，保留 repo / title / url / canonical_url 等页面级字段
    - 汇总所有事件的 actor 列表（去重）和动作描述列表（去重）
    - 若只有一个事件则原样返回，不做额外包装
    """
    if len(events) == 1:
        return events[0]

    primary = dict(events[0])

    # 汇总所有参与者（去重保序）
    actors: list[str] = []
    seen_actors: set[str] = set()
    for e in events:
        actor = e.get("actor", "")
        if actor and actor not in seen_actors:
            actors.append(actor)
            seen_actors.add(actor)

    # 汇总所有动作描述（去重保序）
    merged_actions: list[str] = []
    seen_actions: set[str] = set()
    for e in events:
        desc = f"{e.get('action_cn', '')}{e.get('type_desc', '')}"
        if desc and desc not in seen_actions:
            merged_actions.append(desc)
            seen_actions.add(desc)

    # 汇总 body：拼接所有非空 body
    bodies = [e.get("body", "") for e in events if e.get("body", "")]
    merged_body = "\n---\n".join(bodies) if bodies else primary.get("body", "")

    primary["actor"] = (
        "、".join(actors) if len(actors) > 1 else (actors[0] if actors else primary.get("actor", ""))
    )
    primary["merged_actions"] = merged_actions
    primary["merged_count"] = len(events)
    primary["body"] = merged_body
    # url 设为规范页面 URL（合并组内所有事件共享的页面链接）
    primary["url"] = primary.get("canonical_url", primary.get("url", ""))
    # screenshot_url 若未设则退回到 url（合并后截图仍用规范页面）
    if not primary.get("screenshot_url"):
        primary["screenshot_url"] = primary["url"]

    return primary


def _push_ref(event: dict[str, Any]) -> str:
    """Return a bounded branch/ref value for a PushEvent."""
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return ""
    ref = payload.get("ref")
    if not isinstance(ref, str):
        return ""
    ref = _safe_text(ref, 200).strip()
    if not ref:
        return ""
    return ref


def _push_sha(payload: Any, *keys: str) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in keys:
        value = _safe_revision(payload.get(key))
        if value:
            return value
    return ""


def _group_contiguous_push_events(
    events: list[dict[str, Any]],
) -> list[list[dict[str, Any]]]:
    """Group PushEvents by ref and contiguous before/head ranges.

    Events API responses are newest first.  A range is contiguous only when the
    older event's head equals the newer event's before.  Unknown revisions are
    isolated rather than combined into a potentially misleading Compare URL.
    """
    by_ref: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        ref = _push_ref(event)
        if ref:
            by_ref.setdefault(ref, []).append(event)
    groups: list[list[dict[str, Any]]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        if not _push_ref(event):
            # A malformed/missing ref is still isolated so it cannot block
            # valid events or be combined into a misleading Compare range.
            groups.append([event])
    for ref_events in by_ref.values():
        ordered = sorted(
            ref_events,
            key=lambda item: (_event_timestamp(item), _event_identifier(item)),
        )
        current: list[dict[str, Any]] = []
        previous_head = ""
        for event in ordered:
            payload = event.get("payload")
            payload = payload if isinstance(payload, dict) else {}
            before = _push_sha(payload, "before")
            head = _push_sha(payload, "head", "after")
            if current and (not before or not previous_head or before != previous_head):
                groups.append(current)
                current = []
            current.append(event)
            previous_head = head
        if current:
            groups.append(current)
    return groups


def _merge_push_events(
    raw_events: list[dict[str, Any]],
    *,
    expected_repo: str = "",
    commit_count: int | None = None,
    commit_details: list[dict[str, Any]] | None = None,
    changed_files: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """将同一轮 API 轮询中的多个 PushEvent 合并为一条事件信息。

    合并策略：
    - 汇集所有 commit
    - 优先使用 Compare API 返回的提交详情和文件变更摘要
    - 使用最早推送的 before SHA 作为起始点、最新推送的 head SHA 作为终点，
      构建跨全部推送的 compare URL，使得截图可展示从最早到最新的完整 diff
    - 合并多笔推送的参与者和分支信息

    Args:
        raw_events: 原始 PushEvent JSON 列表（new_events 子集，最新在前）。

    Returns:
        合并后的事件信息 dict，格式与 _extract_event_info 输出兼容。
    """
    if not raw_events:
        return None

    expected = _safe_event_repo_name(expected_repo)
    first = raw_events[0] if isinstance(raw_events[0], dict) else {}
    repo_info = first.get("repo", {})
    payload_repo = (
        _safe_event_repo_name(repo_info.get("name", ""))
        if isinstance(repo_info, dict)
        else ""
    )
    repo_name = expected or payload_repo
    if not repo_name or (expected and payload_repo and payload_repo != expected):
        return None

    # Events API normally returns newest first.  Sort defensively so malformed
    # or test responses cannot make a cross-range compare URL.
    ordered_events = sorted(
        (event for event in raw_events if isinstance(event, dict)),
        key=lambda item: (_event_timestamp(item), _event_identifier(item)),
    )
    if not ordered_events:
        return None
    oldest_payload = ordered_events[0].get("payload", {})
    oldest_payload = oldest_payload if isinstance(oldest_payload, dict) else {}
    newest_payload = ordered_events[-1].get("payload", {})
    newest_payload = newest_payload if isinstance(newest_payload, dict) else {}
    min_before = _push_sha(oldest_payload, "before")
    max_head = _push_sha(newest_payload, "head", "after")

    all_commits: list[dict[str, Any]] = []
    actors: list[str] = []
    seen_actors: set[str] = set()
    branches: set[str] = set()

    for event in ordered_events:
        payload = event.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        raw_commits = payload.get("commits")
        if isinstance(raw_commits, list):
            all_commits.extend(item for item in raw_commits if isinstance(item, dict))

        actor = event.get("actor")
        actor = actor if isinstance(actor, dict) else {}
        actor_name = _safe_text(actor.get("display_login") or actor.get("login", "未知用户"), 120)
        if actor_name and actor_name not in seen_actors:
            actors.append(actor_name)
            seen_actors.add(actor_name)

        ref = payload.get("ref")
        ref = _safe_text(ref, 200) if isinstance(ref, str) else ""
        branch = ref.replace("refs/heads/", "") if ref.startswith("refs/heads/") else ref
        if branch:
            branches.add(branch)

    branch_str = (
        "、".join(sorted(branches))
        if len(branches) > 1
        else (branches.pop() if branches else "未知分支")
    )
    if isinstance(commit_details, list):
        all_commits = [item for item in commit_details if isinstance(item, dict)]
    effective_commit_count = (
        max(0, min(commit_count, 10000))
        if isinstance(commit_count, int) and not isinstance(commit_count, bool)
        else (len(all_commits) or None)
    )
    safe_changed_files = (
        [item for item in changed_files if isinstance(item, dict)]
        if isinstance(changed_files, list)
        else []
    )
    commit_summary = (
        f"{effective_commit_count} 个提交" if effective_commit_count is not None else "提交数量未知"
    )
    title = f"{commit_summary} → {branch_str}"

    commit_lines: list[str] = []
    for c in all_commits[:_MAX_COMMITS_IN_BODY]:
        commit_lines.append(f"- {_commit_subject(c)}")
    body_lines = "\n".join(commit_lines)

    # 构建横跨所有推送的 compare URL；只使用经过校验的 SHA。
    min_before = _safe_revision(min_before)
    max_head = _safe_revision(max_head)
    compare_url = ""
    if min_before and max_head and min_before != "0" * 40:
        compare_url = _github_page_url(repo_name, "compare", f"{min_before}...{max_head}")

    html_url = compare_url or _github_page_url(repo_name)

    return {
        "repo": repo_name,
        "type": "PushEvent",
        "type_desc": "推送",
        "actor": "、".join(actors) if len(actors) > 1 else (actors[0] if actors else "未知用户"),
        "action": "",
        "action_cn": "推送了",
        "title": title,
        "body": body_lines,
        "url": html_url,
        "screenshot_url": compare_url or html_url,
        "canonical_url": _clean_canonical_url(html_url),
        "merged_count": len(raw_events),
        "merged_actions": [f"推送了 {branch_str}"],
        "branch": branch_str,
        "commit_count": effective_commit_count,
        "commits": all_commits[:_MAX_COMMITS_IN_BODY],
        "changed_files": safe_changed_files[:_MAX_CHANGED_FILES_IN_BODY],
        "event_id": "aggregate:"
        + hashlib.sha256(
            "|".join(
                _event_identifier(event) for event in ordered_events
            ).encode("utf-8", errors="replace")
        ).hexdigest()[:32],
        "before": min_before,
        "head": max_head,
    }


# ═══════════════════════════════════════════════════════════════════════
# 事件分发给群
# ═══════════════════════════════════════════════════════════════════════


async def _dispatch_notification(
    ctx: Any,
    group_id: str,
    text: str,
    screenshot_path: str | None,
    *,
    event_id: str = "",
) -> bool:
    """交给宿主的通用主动消息管线，不绑定任何具体平台。"""
    normalized_group_id = str(group_id or "").strip()
    if not normalized_group_id or not text:
        return False
    queue_pending = getattr(ctx, "queue_pending_message", None)
    if callable(queue_pending):
        # The Plugin compatibility context intentionally implements this as a
        # no-op; retaining the hook keeps direct legacy-context callers safe.
        queue_pending(normalized_group_id, str(text))
    adapter_type = ""
    getter = getattr(ctx, "get_current_adapter_type", None)
    if callable(getter):
        try:
            value = getter()
            if isinstance(value, str):
                adapter_type = value.strip()
        except Exception:
            logger.debug("github_monitor: 获取 adapter 类型失败", exc_info=True)
    event_data = {
        "group_id": normalized_group_id,
        "reply": str(text),
        "image_path": str(screenshot_path or ""),
        "adapter_type": adapter_type,
    }
    if event_id:
        event_data["reminder_id"] = str(event_id)
    result = await ctx.emit_event("reminder_triggered", event_data)
    # Legacy contexts returned None after accepting the event.  Treat only an
    # explicit False as a negative acknowledgement for compatibility.
    return result is not False


# ═══════════════════════════════════════════════════════════════════════
# Playwright 页面截图
# ═══════════════════════════════════════════════════════════════════════


def _get_screenshot_slots() -> asyncio.Semaphore:
    """Lazily create the bounded browser-work semaphore on the active loop."""
    global _screenshot_slots
    if _screenshot_slots is None:
        _screenshot_slots = asyncio.Semaphore(2)
    return _screenshot_slots


def _host_is_literal_public(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


async def _host_resolves_public(host: str) -> bool:
    """Fail closed when a browser destination resolves to a private address."""
    host = str(host or "").casefold().rstrip(".")
    if not host or not _host_is_literal_public(host):
        return False
    try:
        records = await asyncio.to_thread(socket.getaddrinfo, host, 443, type=socket.SOCK_STREAM)
    except (OSError, socket.gaierror):
        return False
    if not records:
        return False
    for record in records:
        address_text = record[4][0] if len(record) > 4 and record[4] else ""
        try:
            address = ipaddress.ip_address(address_text)
        except ValueError:
            return False
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
        ):
            return False
    return True


async def _screenshot_route_allowed(route: Any) -> None:
    """Allow only HTTPS GitHub pages/assets with public DNS destinations."""
    request = route.request
    raw_url = str(getattr(request, "url", "") or "")
    try:
        parsed = urlsplit(raw_url)
        port = parsed.port
    except ValueError:
        await route.abort()
        return
    host = (parsed.hostname or "").casefold().rstrip(".")
    if (
        parsed.scheme.casefold() != "https"
        or host not in _GITHUB_ASSET_HOSTS
        or parsed.username
        or parsed.password
        or parsed.query
        or port not in (None, 443)
        or not await _host_resolves_public(host)
    ):
        await route.abort()
        return
    await route.continue_()


async def _prune_artifacts(output_dir: Path) -> None:
    """Keep screenshot artifacts bounded by count and total bytes."""
    try:
        files = sorted(
            (item for item in output_dir.glob("github_*.png") if item.is_file()),
            key=lambda item: item.stat().st_mtime,
        )
        total = 0
        kept: list[Path] = []
        for path in reversed(files):
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if len(kept) >= _MAX_ARTIFACT_FILES or total + size > _MAX_ARTIFACT_BYTES:
                path.unlink(missing_ok=True)
                continue
            kept.append(path)
            total += size
    except OSError:
        logger.debug("github_monitor: 清理截图 artifact 失败", exc_info=True)


async def _take_screenshot(
    url: str,
    store: Any,
    *,
    event_id: str = "",
) -> str | None:
    """Take a bounded screenshot only after strict URL/network validation."""
    safe_url = _safe_github_url(url)
    if not safe_url:
        logger.warning("github_monitor: 拒绝不安全截图地址")
        return None
    try:
        from playwright.async_api import async_playwright  # type: ignore[import-untyped]
    except ImportError:
        logger.warning("github_monitor: playwright 未安装，跳过截图")
        return None

    output_dir = _get_artifact_dir(store)
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        await _prune_artifacts(output_dir)
    except OSError:
        logger.warning("github_monitor: 无法创建截图 artifact 目录")
        return None
    suffix = re.sub(r"[^A-Za-z0-9_-]", "", str(event_id or ""))[:40] or uuid.uuid4().hex
    output_path = output_dir / f"github_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}_{suffix}_{uuid.uuid4().hex}.png"
    slots = _get_screenshot_slots()
    acquired = False
    try:
        await asyncio.wait_for(slots.acquire(), timeout=5.0)
        acquired = True
        async with async_playwright() as p:
            browser = await asyncio.wait_for(p.chromium.launch(headless=True), timeout=15.0)
            try:
                context: Any | None = None
                context = await browser.new_context(
                    viewport={"width": 1280, "height": 900},
                    service_workers="block",
                    java_script_enabled=False,
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                )
                await context.route("**/*", _screenshot_route_allowed)
                page = await context.new_page()
                await asyncio.wait_for(
                    page.goto(safe_url, wait_until="domcontentloaded", timeout=15000),
                    timeout=20.0,
                )
                final_url = _safe_github_url(getattr(page, "url", ""), expected_repo="")
                if not final_url:
                    raise ValueError("截图页面发生了不安全跳转")
                final_host = (urlsplit(final_url).hostname or "").casefold().rstrip(".")
                if final_host not in _GITHUB_WEB_HOSTS:
                    raise ValueError("截图页面跳转到非 GitHub 页面")
                await asyncio.sleep(0.25)
                try:
                    dimensions = await page.evaluate(
                        """() => ({width: Math.min(Math.max(document.documentElement.scrollWidth, 1), 1280), height: Math.min(Math.max(document.documentElement.scrollHeight, 1), 6000)})"""
                    )
                except Exception:
                    dimensions = {"width": 1280, "height": 900}
                width = max(1, min(1280, int((dimensions or {}).get("width", 1280))))
                height = max(1, min(_MAX_SCREENSHOT_HEIGHT, int((dimensions or {}).get("height", 900))))
                await page.set_viewport_size({"width": width, "height": min(height, 900)})

                last_error: Exception | None = None
                for attempt in range(1, _MAX_SCREENSHOT_RETRIES + 1):
                    try:
                        await asyncio.wait_for(
                            page.screenshot(
                                path=str(output_path),
                                full_page=False,
                                clip={"x": 0, "y": 0, "width": width, "height": height},
                            ),
                            timeout=10.0,
                        )
                        if output_path.stat().st_size > _MAX_SCREENSHOT_BYTES:
                            raise ValueError("截图文件超过大小限制")
                        last_error = None
                        break
                    except Exception as exc:
                        last_error = exc
                        output_path.unlink(missing_ok=True)
                        if attempt < _MAX_SCREENSHOT_RETRIES:
                            await asyncio.sleep(0.25 * attempt)
                if last_error is not None:
                    raise last_error
                logger.info("github_monitor: 截图已保存")
                return str(output_path)
            finally:
                if context is not None:
                    await context.close()
                await browser.close()
    except Exception as exc:
        output_path.unlink(missing_ok=True)
        logger.warning("github_monitor: Playwright 截图失败 (%s): %s", type(exc).__name__, exc)
        return None
    finally:
        if acquired:
            slots.release()
        await _prune_artifacts(output_dir)


def _get_artifact_dir(store: Any) -> Path:
    """获取 TOOL artifact 目录路径。"""
    artifact_dir = getattr(store, "artifact_dir", None)
    if isinstance(artifact_dir, Path):
        return artifact_dir
    if artifact_dir:
        return Path(str(artifact_dir))
    return Path("data") / "tool_data" / "artifacts" / "github_monitor"


# ═══════════════════════════════════════════════════════════════════════
# 人格风格通知生成
# ═══════════════════════════════════════════════════════════════════════


async def _generate_notification_text(
    ctx: Any,
    event_info: dict[str, Any],
) -> str | None:
    """调用 LLM 生成人格风格的通知消息（不绑定群，不写记忆）。

    仅将人格身份和结构化事件详情传给模型；页面截图由群消息分发链路单独发送。
    """
    try:
        persona = ctx.get_persona()
        identity = persona.build_system_prompt() if persona else ""

        # 构建事件描述
        event_desc = _build_event_section(event_info)

        system_prompt = (
            f"{identity}\n\n"
            f"【GitHub 仓库动态播报】\n"
            f"{event_desc}\n\n"
            f"请用你的人格风格，自然地向群友们播报这条 GitHub 仓库动态。\n"
            f"要求：\n"
            f"- 不要机械复述，像朋友分享新鲜事一样自然\n"
            f"- 简短即可，2-4 句话\n"
            f"- 必须明确提到「操作者」是谁（不要混淆为你人格设定中的人），"
            f"这个操作者是真实的 GitHub 用户\n"
            f"- 提到关键信息：谁、做了什么、涉及什么仓库\n"
            f"- 如果有「提交详情」或「变更文件摘要」，优先根据实际内容概括变更，"
            f"不要只复述提交数量\n"
            f"- 必须在播报末尾附带「链接」中的网址，让群友可以直接点击跳转\n"
            f"- 可以表达你的感受（惊讶、期待、好奇等），但要符合你的人设"
        )

        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": f"（{event_info['repo']} 仓库有新动态，请播报一下）",
            }
        ]

        # 使用第一个活跃群作为 generate_text 的 group_id（仅用于 token 统计/路由）
        active_groups = ctx.get_active_groups()
        group_id = active_groups[0] if active_groups else "github_monitor"

        raw_reply = await ctx.generate_text(
            system_prompt,
            messages,
            group_id,
            task_name="plugin_generate",
        )

        reply = raw_reply.strip()
        return reply or None
    except Exception as exc:
        logger.warning("github_monitor: 生成通知失败: %s", exc)
        return _build_fallback_notification(event_info)


def _build_event_section(
    event_info: dict[str, Any],
) -> str:
    """构建注入 prompt 的事件描述 section。"""
    lines = [
        f"仓库: {event_info['repo']}",
        f"事件: {event_info['type_desc']}",
        f"操作者: {event_info['actor']}",
    ]

    if event_info.get("type"):
        lines.append(f"Event 类型: {event_info['type']}")

    # 合并事件：列出所有动作
    merged_actions = event_info.get("merged_actions")
    if merged_actions:
        lines.append(f"合并动作: {'、'.join(merged_actions)}")
        lines.append(f"（本组共合并了 {event_info.get('merged_count', 1)} 条关联事件）")
    elif event_info.get("action_cn"):
        action = event_info.get("action", "")
        action_text = event_info["action_cn"]
        if action and action != action_text:
            action_text += f"（{action}）"
        lines.append(f"动作: {action_text}")

    if event_info.get("created_at"):
        lines.append(f"发生时间: {event_info['created_at']}")
    if event_info.get("branch"):
        lines.append(f"分支: {event_info['branch']}")
    if event_info.get("commit_count") is not None:
        lines.append(f"提交数: {event_info['commit_count']}")

    if event_info.get("title"):
        lines.append(f"标题: {event_info['title']}")
    if event_info.get("body"):
        lines.append(f"内容: {event_info['body']}")

    commits = event_info.get("commits") or []
    if commits:
        lines.append("提交详情:")
        for commit in commits[:_MAX_COMMITS_IN_BODY]:
            sha = str(commit.get("sha", ""))[:10] or "未知 SHA"
            message = _truncate_text(_commit_message(commit), 400)
            lines.append(f"- {sha} | 作者: {_commit_author(commit)}")
            lines.append(f"  提交说明: {message or '未提供提交说明'}")

    changed_files = event_info.get("changed_files") or []
    if changed_files:
        lines.append("变更文件摘要:")
        for changed_file in changed_files[:_MAX_CHANGED_FILES_IN_BODY]:
            filename = changed_file.get("filename", "未知文件")
            status = changed_file.get("status", "")
            additions = changed_file.get("additions", 0)
            deletions = changed_file.get("deletions", 0)
            lines.append(f"- {filename}（{status}，+{additions}/-{deletions}）")
    if event_info.get("url"):
        lines.append(f"链接: {event_info['url']}")
    return "\n".join(lines)


def _build_fallback_notification(event_info: dict[str, Any]) -> str:
    """LLM 调用失败时的降级纯文本通知。"""
    repo = event_info.get("repo", "未知仓库")
    actor = event_info.get("actor", "有人")
    title = event_info.get("title", "")
    url = event_info.get("url", "")

    # 合并事件：列出所有动作
    merged_actions = event_info.get("merged_actions")
    if merged_actions:
        action_desc = "、".join(merged_actions)
        parts = [f"🔔 [{repo}] {actor} {action_desc}"]
    else:
        action_cn = event_info.get("action_cn", "")
        type_desc = event_info.get("type_desc", "")
        parts = [f"🔔 [{repo}] {actor} {action_cn}{type_desc}"]

    if title:
        parts.append(f"「{title}」")
    if url:
        parts.append(f"🔗 {url}")
    return " ".join(parts)


# ═══════════════════════════════════════════════════════════════════════
# Webhook 模式事件处理
# ═══════════════════════════════════════════════════════════════════════


# 仅对通知有价值的 webhook 动作
_WEBHOOK_ISSUE_ACTIONS = {"opened", "closed", "reopened"}
_WEBHOOK_PR_ACTIONS = {"opened", "closed", "reopened", "synchronize"}
_WEBHOOK_COMMENT_ACTIONS = {"created"}


async def _handle_webhook_event(
    event_type: str,
    body: dict[str, Any],
    delivery_id: str = "",
) -> None:
    """Authorize a Webhook event, then run bridge/screenshot/LLM/delivery."""
    if _webhook_ctx is None or not isinstance(body, dict):
        return

    ctx = _webhook_ctx
    store = ctx.get_data_store("github_monitor")
    store.reload()
    repos = _normalize_configured_repos(store.get("repos", []))
    event_category = {
        "issues": "issues",
        "pull_request": "pulls",
        "push": "pushes",
        "release": "releases",
        "issue_comment": "comments",
        "pull_request_review_comment": "comments",
        "commit_comment": "comments",
    }.get(str(event_type).strip())
    repository = body.get("repository")
    raw_repo = repository.get("full_name", "") if isinstance(repository, dict) else ""
    repo_name = _safe_event_repo_name(raw_repo)
    repo_cfg = next(
        (
            item
            for item in repos
            if f"{item['owner']}/{item['repo']}" == repo_name
        ),
        None,
    )
    if repo_cfg is None or repo_cfg.get("mode") != "webhook":
        logger.debug("github_monitor (webhook): 忽略未授权仓库 %s", repo_name)
        return
    if event_category is None or event_category not in set(repo_cfg.get("events", [])):
        logger.debug("github_monitor (webhook): %s 未订阅事件 %s", repo_name, event_type)
        return
    target_groups = _normalize_groups(repo_cfg.get("groups", []))
    if not target_groups:
        return

    # Authorization and configuration checks must precede every downstream side effect.
    event_info = _extract_webhook_event_info(event_type, body)
    if event_info is None:
        return
    event_id = str(delivery_id or body.get("delivery_id", "") or "").strip()
    if not event_id:
        event_id = hashlib.sha256(
            json.dumps(body, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
        ).hexdigest()[:24]
    event_info["event_id"] = event_id

    action = str(body.get("action", ""))
    if event_type == "issues" and action in {"labeled", "unlabeled"}:
        if repo_name in get_issue_repos():
            return
    bot_login = get_coding_bot_login()
    if event_type in {"issue_comment", "pull_request_review_comment"} and bot_login:
        if repo_name in get_issue_repos():
            sender = body.get("sender", {})
            comment_login = sender.get("login", "") if isinstance(sender, dict) else ""
            if comment_login != bot_login:
                return

    bridge_ack = True
    if event_type == "issues" and action == "opened":
        bridge_ack = await notify_issue_opened(body, repo_name)
    elif event_type == "pull_request" and action in {"opened", "synchronize"}:
        bridge_ack = await notify_pr_event(body, repo_name, action)
    elif event_type in {"issue_comment", "pull_request_review_comment"} and action == "created":
        bridge_ack = await notify_issue_comment(body, repo_name)
    if not bridge_ack:
        raise RuntimeError(f"event bridge 未确认 delivery={event_id}")

    screenshot_path: str | None = None
    screenshot_url = _safe_github_url(
        event_info.get("screenshot_url", "") or event_info.get("url", ""),
        expected_repo=repo_name,
    )
    if screenshot_url:
        screenshot_path = await _take_screenshot(screenshot_url, store, event_id=event_id)

    notification = await _generate_notification_text(ctx, event_info)
    if not notification:
        return
    ctx.log_inner_thought(
        f"github_monitor (webhook): [{repo_name}] {event_info['actor']} "
        f"{event_info.get('action_cn', '')}{event_info.get('type_desc', '')}"
        f" - 通知已生成，分发到 {len(target_groups)} 个群"
    )
    for gid in target_groups:
        await _dispatch_notification(
            ctx, gid, notification, screenshot_path, event_id=event_id
        )


def _extract_webhook_event_info(
    event_type: str,
    body: dict[str, Any],
) -> dict[str, Any] | None:
    """Extract a Webhook payload while deriving all navigable URLs locally."""
    repository = body.get("repository", {})
    raw_repo_name = repository.get("full_name", "") if isinstance(repository, dict) else ""
    repo_name = _safe_event_repo_name(raw_repo_name)
    if not repo_name:
        return None
    sender = body.get("sender", {})
    sender = sender if isinstance(sender, dict) else {}
    actor_name = _safe_text(sender.get("login", "未知用户"), 120)
    action = _safe_text(body.get("action", ""), 40)
    event_info: dict[str, Any] = {
        "repo": repo_name,
        "type": event_type,
        "actor": actor_name,
        "action": action,
        "created_at": _safe_text(body.get("created_at", ""), 40),
        "commit_count": None,
        "commits": [],
    }

    if event_type == "issues":
        if action not in _WEBHOOK_ISSUE_ACTIONS:
            return None
        issue = body.get("issue", {})
        if not isinstance(issue, dict):
            return None
        event_info.update(
            {
                "type_desc": "Issue",
                "action_cn": _ACTION_DESC.get(action, action),
                "title": _safe_text(issue.get("title", ""), 300),
                "body": _safe_text(issue.get("body", "")),
                "url": _github_page_url(repo_name, "issues", _safe_number(issue.get("number"))),
            }
        )
    elif event_type == "pull_request":
        if action not in _WEBHOOK_PR_ACTIONS:
            return None
        pr_data = body.get("pull_request", {})
        if not isinstance(pr_data, dict):
            return None
        action_cn = _ACTION_DESC.get(action, action)
        if pr_data.get("merged") and action == "closed":
            action_cn = "合并了"
        event_info.update(
            {
                "type_desc": "Pull Request",
                "action_cn": action_cn,
                "title": _safe_text(pr_data.get("title", ""), 300),
                "body": _safe_text(pr_data.get("body", "")),
                "url": _github_page_url(repo_name, "pull", _safe_number(pr_data.get("number"))),
            }
        )
    elif event_type == "push":
        raw_commits = body.get("commits")
        commits = [item for item in raw_commits if isinstance(item, dict)] if isinstance(raw_commits, list) else []
        if commits and all(_PR_MERGE_COMMIT_PATTERN.match(_commit_message(item)) for item in commits):
            logger.debug("github_monitor (webhook): 跳过 PR 合并 Push 事件")
            return None
        ref = _safe_text(body.get("ref", ""), 200)
        branch = ref.replace("refs/heads/", "") if ref.startswith("refs/heads/") else ref
        before_sha = _safe_revision(body.get("before"))
        head_sha = _safe_revision(body.get("after"))
        compare_url = ""
        if before_sha and head_sha and before_sha != "0" * 40:
            compare_url = _github_page_url(repo_name, "compare", f"{before_sha}...{head_sha}")
        first_sha = _safe_revision(commits[0].get("id") or commits[0].get("sha")) if commits else ""
        event_info.update(
            {
                "type_desc": "推送",
                "action_cn": "推送了",
                "action": "",
                "title": f"{len(commits) if isinstance(raw_commits, list) else '提交数量未知'} 个提交 → {branch}",
                "body": "\n".join(
                    f"- {_safe_text(_commit_subject(item), 200)}"
                    for item in commits[:_MAX_COMMITS_IN_BODY]
                ),
                "url": compare_url or _github_page_url(repo_name, "commit", first_sha) or _github_page_url(repo_name),
                "branch": branch,
                "commit_count": len(commits) if isinstance(raw_commits, list) else None,
                "commits": commits[:_MAX_COMMITS_IN_BODY],
            }
        )
    elif event_type == "release":
        if action != "published":
            return None
        release = body.get("release", {})
        if not isinstance(release, dict):
            return None
        event_info.update(
            {
                "type_desc": "Release",
                "action_cn": "发布了",
                "title": _safe_text(release.get("name") or release.get("tag_name", ""), 300),
                "body": _safe_text(release.get("body", "")),
                "url": _github_page_url(repo_name, "releases"),
            }
        )
    elif event_type == "issue_comment":
        if action not in _WEBHOOK_COMMENT_ACTIONS:
            return None
        issue = body.get("issue", {})
        comment = body.get("comment", {})
        if not isinstance(issue, dict) or not isinstance(comment, dict):
            return None
        is_pr = bool(issue.get("pull_request"))
        event_info.update(
            {
                "type_desc": "评论 (PR)" if is_pr else "评论 (Issue)",
                "action_cn": "评论了",
                "title": _safe_text(issue.get("title", ""), 300),
                "body": _safe_text(comment.get("body", "")),
                "url": _github_page_url(
                    repo_name, "pull" if is_pr else "issues", _safe_number(issue.get("number"))
                ),
            }
        )
    elif event_type == "pull_request_review_comment":
        if action not in _WEBHOOK_COMMENT_ACTIONS:
            return None
        pr_data = body.get("pull_request", {})
        comment = body.get("comment", {})
        if not isinstance(pr_data, dict) or not isinstance(comment, dict):
            return None
        event_info.update(
            {
                "type_desc": "评论 (PR Review)",
                "action_cn": "评论了",
                "title": _safe_text(pr_data.get("title", ""), 300),
                "body": _safe_text(comment.get("body", "")),
                "url": _github_page_url(repo_name, "pull", _safe_number(pr_data.get("number"))),
            }
        )
    elif event_type == "commit_comment":
        if action not in _WEBHOOK_COMMENT_ACTIONS:
            return None
        comment = body.get("comment", {})
        if not isinstance(comment, dict):
            return None
        event_info.update(
            {
                "type_desc": "评论 (Commit)",
                "action_cn": "评论了",
                "title": "",
                "body": _safe_text(comment.get("body", "")),
                "url": _github_page_url(
                    repo_name,
                    "commit",
                    _safe_revision(comment.get("commit_id") or comment.get("sha")),
                ),
            }
        )
    else:
        return None

    safe_url = _safe_github_url(event_info.get("url", ""), expected_repo=repo_name)
    event_info["url"] = safe_url
    event_info["screenshot_url"] = (
        f"{safe_url}/files"
        if event_type in {"pull_request", "pull_request_review_comment"} and safe_url
        else safe_url
    )
    event_info["canonical_url"] = _clean_canonical_url(safe_url)
    return event_info
