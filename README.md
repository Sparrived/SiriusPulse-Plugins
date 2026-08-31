# SiriusPulse Plugins

External plugins for [Sirius Pulse](https://github.com/Sparrived/SiriusPulse).

## Included plugins

- `github_monitor`: monitors configured GitHub repositories through Poll or Webhook delivery and sends aggregated Issue, Pull Request, Release, Comment, and Push notifications with optional screenshots.
- `amkr_key_manager`: manages AMKR models and API keys through its local management API.
- `sub2api_monitor`: monitors Sub2API subscription listing changes and group rate multipliers, then notifies configured chats.

## Installation

This repository is intended to be mounted as the Sirius Pulse workspace `plugins/` directory:

```bash
git submodule add https://github.com/Sparrived/SiriusPulse-Plugins.git plugins
git submodule update --init --recursive
```

The host framework must be installed first. Plugin dependencies declared by the plugin are handled by Sirius Pulse's PluginLoader.

For Docker deployments, keep this checkout on the host and mount it read-write at `/app/plugins` (the official Compose file does this). The image and PyPI distributions intentionally do not contain this repository. Ensure the host directory is writable by container UID `10001` so WebUI settings can update `plugins/_config.json` (use a host ACL or another permission policy that preserves the host Git user's write access); plugin source updates happen on the host, followed by a container restart or plugin reload.

## GitHub Monitor

Configure the plugin through the host WebUI under Plugin settings. A repository entry contains:

- `owner` and `repo`;
- notification target group IDs;
- enabled event types: `issues`, `pulls`, `releases`, `comments`, `pushes`;
- per-repository mode (`poll` or `webhook`) and optional token.

Use `/github status` to inspect the configured repository count and `/github poll` to trigger one immediate poll. Polling persists event cursors in the per-persona PluginDataStore and skips historical events on first synchronization. Webhook delivery uses the configured HMAC secret and the same notification pipeline.

Push events are grouped for one notification and enriched through the Compare API when available. Issue/PR/review/comment/release pages can receive Playwright screenshots; screenshot failure does not prevent text delivery. Event-bridge callbacks remain available for coding-agent integrations.

## Sub2API Monitor

Configure `base_url`, `subscriptions_path`, `group_rates_path`, polling interval, and notification group IDs through the host WebUI. Endpoint paths are runtime settings rather than hardcoded station URLs. Prefer the `SUB2API_EMAIL` and `SUB2API_PASSWORD` process environment variables so credentials do not need to be stored in `plugins/_config.json`.

Use `/sub2api status`, `/sub2api poll`, `/sub2api subscriptions`, or `/sub2api rates`. The first successful synchronization initializes snapshots without announcing existing listings. See [`sub2api_monitor/README.md`](sub2api_monitor/README.md) for configuration and security details.
