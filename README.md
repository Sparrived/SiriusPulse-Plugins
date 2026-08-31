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

Install a compatible Sirius Pulse framework before loading a Plugin. PluginLoader enforces each Plugin's declared minimum framework version. A trusted runtime lifecycle can install declared Plugin dependencies; metadata browsing must not be treated as a dependency-install action.

For Docker deployments, keep this checkout on the host and mount it read-write at `/app/plugins` (the official Compose file does this). The image and PyPI distributions intentionally do not contain this repository. Ensure the host directory is writable by container UID `10001` so WebUI settings can update `plugins/_config.json`, but preserve the host Git user's normal ownership and write access. A host ACL is usually appropriate, for example `sudo setfacl -R -m u:10001:rwX plugins`; plugin source updates happen on the host, followed by a container restart or plugin reload.

## GitHub Monitor

Configure the plugin through the host WebUI under Plugin settings. A repository entry contains:

- `owner` and `repo`;
- notification target group IDs;
- enabled event types: `issues`, `pulls`, `releases`, `comments`, `pushes`;
- per-repository mode (`poll` or `webhook`) and an optional `github_token_env` **environment-variable name**.

Use `github_token_env` per repository and `webhook_secret_env` globally; never enter a GitHub Token or Webhook Secret into WebUI/settings or commit it to `plugins/_config.json`. The deployment must explicitly map the selected variable names into the Sirius Pulse process (a Compose `.env` file alone is not process injection). See [`github_monitor/README.md`](github_monitor/README.md) for a safe override example and Webhook details.

Use `/github status` to inspect the configured repository count and `/github poll` to trigger one immediate poll. Polling persists event cursors in the per-persona PluginDataStore and skips historical events on first synchronization. Webhook delivery uses the configured `sha256=` HMAC secret and the same notification pipeline.

Push events are grouped for one notification and enriched through the Compare API when available. Issue/PR/review/comment/release pages can receive Playwright screenshots; screenshot failure does not prevent text delivery. Playwright is also used by generic core facilities, so it remains a core dependency in addition to the Plugin declaration. Event-bridge callbacks remain available for coding-agent integrations.

## Sub2API Monitor

Configure non-secret runtime settings through the host WebUI: `base_url`, API/login paths,
**required** `subscriptions_path` and `group_rates_path`, polling interval, explicit
`notify_group_ids` allowlist, and the one `run_on_persona` that is allowed to poll. Endpoint
paths are runtime settings; the plugin has no hardcoded monitor endpoint or station URL. Use
inert `example.invalid` values only as examples, then replace them with paths for your own
station.

`SUB2API_EMAIL` and `SUB2API_PASSWORD` are the supported credential mechanism. Set both in
the Sirius Pulse process environment; never store a password through WebUI or plugin settings
(`plugins/_config.json`). `run_on_persona` must name the sole polling Persona. When blank, it
disables both background polling and `/sub2api poll`.

Use `/sub2api status`, `/sub2api poll`, `/sub2api subscriptions`, or `/sub2api rates`. The
first snapshot and a changed monitoring source are silent. For later changes, per-group
notification ACK state is retained when dispatch fails or is unconfirmed, so the next poll
retries only the unacknowledged groups. Framework confirmation can mean adapter/platform
admission or send confirmation, not that an end user read the message. See
[`sub2api_monitor/README.md`](sub2api_monitor/README.md) for configuration and security
details.
