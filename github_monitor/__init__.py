"""GitHub repository activity monitor plugin for Sirius Pulse."""

from __future__ import annotations

from typing import Any

from sirius_pulse.github import GitHubClient, fetch_repo_events
from sirius_pulse.plugins.api import BackgroundTaskSpec, PluginBase, PluginResponse, command


class GitHubMonitorPlugin(PluginBase):
    """Poll GitHub repositories and deliver updates to configured chats.

    The plugin intentionally keeps GitHub-specific state in PluginDataStore and
    uses the host's proactive message API for delivery. It does not depend on
    NapCat internals, so a later Webhook implementation can share the parser.
    """

    _plugin_name = "github_monitor"
    _plugin_display_name = "GitHub Monitor"
    _plugin_description = "监控 GitHub 仓库的 Issue、PR、Release 和 Push 动态。"
    _plugin_version = "0.1.0"
    _plugin_author = "Sparrived"
    _plugin_dependencies = ["httpx>=0.24.0"]
    _plugin_permissions = {
        "hidden_from_intent": True,
        "rate_limit": {"calls_per_minute": 20, "calls_per_hour": 300},
    }
    _plugin_parameters = [
        {
            "name": "poll_seconds",
            "type": "int",
            "description": "轮询间隔（秒），最小 30 秒。",
            "default": 120,
            "group": "GitHub 监控",
        },
        {
            "name": "api_base_url",
            "type": "str",
            "description": "GitHub API 地址。",
            "default": "https://api.github.com",
            "group": "GitHub 监控",
        },
        {
            "name": "github_token",
            "type": "password",
            "description": "GitHub Token（可选，用于提高 API 额度）。",
            "default": "",
            "group": "GitHub 监控",
        },
        {
            "name": "repos",
            "type": "object_array",
            "description": "监控仓库列表。",
            "fields": [
                {"name": "owner", "type": "str", "description": "仓库所有者"},
                {"name": "repo", "type": "str", "description": "仓库名称"},
                {"name": "groups", "type": "list", "description": "通知目标群号"},
                {
                    "name": "events",
                    "type": "checkbox_group",
                    "description": "事件类型",
                    "choices": ["issues", "pulls", "releases", "pushes"],
                },
            ],
            "group": "GitHub 监控",
        },
    ]

    @command(
        "github",
        prefix="/",
        patterns=["github", "github-monitor"],
        render_mode="direct",
        description="查看 GitHub 监控状态或立即执行一次轮询",
        hidden_from_intent=True,
    )
    async def github(self, action: str = "status") -> PluginResponse:
        """Handle explicit status/poll commands."""
        action = action.strip().lower()
        if action in {"poll", "check", "检查", "轮询"}:
            count = await self.poll_once()
            return PluginResponse.ok(text=f"GitHub 监控已完成一次轮询，发送 {count} 条通知。")
        if action in {"status", "状态", ""}:
            repos = self._configured_repos()
            return PluginResponse.ok(
                text=f"GitHub 监控运行中，已配置 {len(repos)} 个仓库。"
            )
        return PluginResponse.fail("用法：/github status 或 /github poll")

    def create_background_tasks(self) -> list[BackgroundTaskSpec]:
        """Run polling under PluginExecutor lifecycle management."""
        interval = max(30, self._poll_seconds())
        return [BackgroundTaskSpec("poll", interval, self.poll_once)]

    def _poll_seconds(self) -> int:
        try:
            return int(self.ctx.config.get("poll_seconds", 120))
        except (TypeError, ValueError):
            return 120

    def _configured_repos(self) -> list[dict[str, Any]]:
        raw = self.ctx.config.get("repos", [])
        if not isinstance(raw, list):
            return []
        return [item for item in raw if isinstance(item, dict)]

    async def poll_once(self) -> int:
        """Fetch configured repositories once and send new event summaries."""
        repos = self._configured_repos()
        if not repos:
            return 0

        base_url = str(self.ctx.config.get("api_base_url", "https://api.github.com")).rstrip("/")
        token = str(self.ctx.config.get("github_token", "")).strip()
        sent = 0
        async with GitHubClient(token, base_url=base_url, timeout=30.0) as client:
            for repo_cfg in repos:
                owner = str(repo_cfg.get("owner", "")).strip()
                repo = str(repo_cfg.get("repo", "")).strip()
                if not owner or not repo:
                    continue
                groups = [
                    str(value).strip()
                    for value in repo_cfg.get("groups", [])
                    if str(value).strip()
                ]
                events = {str(value).strip() for value in repo_cfg.get("events", [])}
                if not groups or not events:
                    continue
                sent += await self._poll_repo(client, owner, repo, groups, events)
        return sent

    async def _poll_repo(
        self,
        client: GitHubClient,
        owner: str,
        repo: str,
        groups: list[str],
        enabled_events: set[str],
    ) -> int:
        store = self.get_data_store()
        key = f"{owner}/{repo}"
        last_event_id = str(store.get(f"last_event_id:{key}", ""))
        payload = await fetch_repo_events(client, owner, repo, per_page=30)

        new_events: list[dict[str, Any]] = []
        cursor_found = not last_event_id
        for event in reversed(payload):
            if not isinstance(event, dict):
                continue
            event_id = str(event.get("id", ""))
            if not event_id:
                continue
            if event_id == last_event_id:
                cursor_found = True
                continue
            if cursor_found and self._event_enabled(event, enabled_events):
                new_events.append(event)

        if payload:
            newest_id = str(payload[0].get("id", ""))
            if newest_id:
                if not last_event_id:
                    # 首次同步只建立游标，不播报已有历史活动。
                    store.set(f"last_event_id:{key}", newest_id)
                    return 0
                store.set(f"last_event_id:{key}", newest_id)

        sent = 0
        for event in new_events:
            text = self._format_event(event, key)
            for group_id in groups:
                await self.ctx.dispatch_proactive_message(
                    group_id=group_id,
                    text=text,
                    adapter_type="napcat",
                    event_id=f"github:{key}:{event.get('id', '')}",
                )
                sent += 1
        return sent

    @staticmethod
    def _event_enabled(event: dict[str, Any], enabled_events: set[str]) -> bool:
        event_type = str(event.get("type", ""))
        mapping = {
            "IssuesEvent": "issues",
            "PullRequestEvent": "pulls",
            "ReleaseEvent": "releases",
            "PushEvent": "pushes",
        }
        return mapping.get(event_type, "") in enabled_events

    @staticmethod
    def _format_event(event: dict[str, Any], repo_key: str) -> str:
        event_type = str(event.get("type", "GitHubEvent"))
        actor = (event.get("actor") or {}).get("display_login") or (event.get("actor") or {}).get("login", "未知用户")
        payload = event.get("payload") or {}
        action = str(payload.get("action", "更新"))
        title = ""
        if event_type == "IssuesEvent":
            title = str((payload.get("issue") or {}).get("title", ""))
        elif event_type == "PullRequestEvent":
            title = str((payload.get("pull_request") or {}).get("title", ""))
        elif event_type == "ReleaseEvent":
            release = payload.get("release") or {}
            title = str(release.get("name") or release.get("tag_name", ""))
        elif event_type == "PushEvent":
            commits = payload.get("commits") or []
            title = f"{len(commits)} 个提交"
        return f"【GitHub】{repo_key}：{actor} {action} {title}".strip()
