# SiriusPulse Plugins

External plugins for [Sirius Pulse](https://github.com/Sparrived/SiriusPulse).

## Included plugins

- `github_monitor`: polls configured GitHub repositories and sends Issue, Pull Request, Release, and Push notifications to configured chats.
- `amkr_key_manager`: manages AMKR models and API keys through its local management API.

## Installation

This repository is intended to be mounted as the Sirius Pulse workspace `plugins/` directory:

```bash
git submodule add https://github.com/Sparrived/SiriusPulse-Plugins.git plugins
git submodule update --init --recursive
```

The host framework must be installed first. Plugin dependencies declared by the plugin are handled by Sirius Pulse's PluginLoader.

## GitHub Monitor

Configure the plugin through the host WebUI under Plugin settings. A repository entry contains:

- `owner` and `repo`;
- notification target group IDs;
- enabled event types: `issues`, `pulls`, `releases`, `pushes`.

Use `/github status` to inspect the configured repository count and `/github poll` to trigger one immediate poll. The plugin persists event cursors in its per-persona PluginDataStore and skips historical events on first synchronization.

The current release is a polling MVP. Webhook delivery, screenshots, event aggregation, Compare API details, and migration from the built-in monitor remain follow-up work.
