from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from github_monitor import GitHubMonitorPlugin  # noqa: E402
from sirius_pulse.plugins.context import PluginDataStore  # noqa: E402


def _plugin_context(
    data_dir: Path, config: dict | None = None
) -> tuple[GitHubMonitorPlugin, PluginDataStore]:
    store = PluginDataStore(data_dir, "github_monitor")
    plugin = GitHubMonitorPlugin()
    plugin._ctx = SimpleNamespace(config=config or {}, data_store=store)
    return plugin, store


def test_wrapper_reuses_full_monitor_background_task(tmp_path):
    plugin, _store = _plugin_context(tmp_path, {"poll_seconds": 45})

    specs = plugin.create_background_tasks()

    assert len(specs) == 1
    assert specs[0].name == "github_monitor_poll"
    assert specs[0].interval_seconds == 30


def test_wrapper_store_prefers_webui_settings_over_migrated_state(tmp_path):
    plugin, store = _plugin_context(
        tmp_path,
        {"poll_seconds": 90, "repos": [{"owner": "new", "repo": "repo"}]},
    )
    store.set("poll_seconds", 30)
    store.set("repos", [{"owner": "old", "repo": "repo"}])

    adapted = plugin._legacy_context().get_data_store()

    assert adapted.get("poll_seconds") == 90
    assert adapted.get("repos") == [{"owner": "new", "repo": "repo"}]


def test_legacy_tool_data_is_migrated_without_deleting_source(tmp_path):
    persona_dir = tmp_path / "persona"
    plugin_data_dir = persona_dir / "plugin_data"
    legacy_path = persona_dir / "tool_data" / "github_monitor.json"
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_text(
        json.dumps(
            {
                "poll_seconds": 60,
                "repos": [{"owner": "owner", "repo": "repo"}],
                "last_event_timestamps": {"owner/repo": "2026-01-01T00:00:00Z"},
            }
        ),
        encoding="utf-8",
    )
    plugin, store = _plugin_context(plugin_data_dir)

    plugin._migrate_legacy_store()

    assert legacy_path.is_file()
    assert store.get("poll_seconds") == 60
    assert store.get("repos") == [{"owner": "owner", "repo": "repo"}]
    assert store.get("last_event_timestamps") == {"owner/repo": "2026-01-01T00:00:00Z"}
    assert store.get("_legacy_migration_version") == 1


def test_legacy_skill_data_is_migrated(tmp_path):
    persona_dir = tmp_path / "persona"
    plugin_data_dir = persona_dir / "plugin_data"
    legacy_path = persona_dir / "skill_data" / ".persona_skills.json"
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_text(
        json.dumps(
            {
                "skills": {
                    "github_monitor": {
                        "webhook_port": 8123,
                        "repos": [{"owner": "owner", "repo": "repo"}],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    plugin, store = _plugin_context(plugin_data_dir)

    plugin._migrate_legacy_store()

    assert store.get("webhook_port") == 8123
    assert store.get("repos") == [{"owner": "owner", "repo": "repo"}]
    assert store.get("_legacy_migration_version") == 1


def test_invalid_legacy_data_does_not_mark_migration_complete(tmp_path):
    persona_dir = tmp_path / "persona"
    plugin_data_dir = persona_dir / "plugin_data"
    legacy_path = persona_dir / "skill_data" / "github_monitor.json"
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_text("{broken", encoding="utf-8")
    plugin, store = _plugin_context(plugin_data_dir)

    plugin._migrate_legacy_store()

    assert store.get("_legacy_migration_version") is None
