# GitHub Monitor Plugin

Full GitHub repository monitor for Sirius Pulse.

## Features

- Poll and Webhook modes per repository.
- Issue, Pull Request, Release, Issue Comment, Review Comment, Commit Comment, and Push events.
- Events API and Compare API integration, including commit and changed-file details.
- PR merge Push de-duplication and related-event aggregation.
- Playwright screenshots with retry and per-persona artifact storage.
- Persona-styled LLM notifications with deterministic fallback text.
- Multi-group delivery, private-chat activation, reply/sticker/poke-compatible proactive message payloads.
- Existing event bridge semantics for coding-agent integrations.
- Migration from legacy `tool_data/github_monitor.json`, `skill_data/github_monitor.json`, and `.persona_skills.json` layouts.

## Commands

- `/github status`
- `/github poll`

## Configuration

Configure the Plugin through Sirius Pulse WebUI:

- `poll_seconds`: polling interval, 30-3600 seconds.
- `api_base_url`: GitHub REST API endpoint.
- `webhook_secret`, `webhook_host`, `webhook_port`.
- `repos`: repository entries with owner, repo, mode, event types, target groups, and optional per-repository token.

Do not configure both the legacy built-in Tool and this Plugin for the same repositories during migration, or duplicate notifications may be produced.

## Dependencies

The Plugin declares `httpx` and `playwright`. Sirius Pulse PluginLoader installs Python dependencies and the Playwright Chromium runtime when needed.

## Data migration

Legacy source files are read but never deleted. A versioned migration marker is written only after all existing source files parse successfully. Runtime state remains isolated per persona in `plugin_data/_plugin_github_monitor_data.json`.

Secrets remain sensitive: rotate any token that has previously been stored or copied outside an approved secret store.
