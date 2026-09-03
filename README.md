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

Sub2API Monitor 0.3.0 requires Sirius Pulse 1.3.0+ and exposes a Chinese visual WebUI schema.
Deployment operators add and edit `sources` as site cards; the Plugin author declares stable
identity, field types, validation, labels, and layout once, and the presentation schema is never
persisted as settings. Each source has a stable lowercase `id`, optional `display_name`,
runtime-configured API/login paths, and required runtime `subscriptions_path` /
`group_rates_path`; no station or monitor endpoint is hardcoded.
A source ID such as `primary` derives `SUB2API_PRIMARY_EMAIL` and
`SUB2API_PRIMARY_PASSWORD`. `display_name` is presentation-only. Secrets must enter the actual
Persona Worker environment and must never be stored through WebUI/settings or
`plugins/_config.json`.

Top-level `notify_group_ids` is an explicit allowlist. A source with
`inherit_notify_group_ids: true` merges that list with its own `notify_group_ids`; `false` uses
only the source list. `run_on_persona` names the sole polling Persona, and a blank value disables
background polling and `/sub2api poll`. An explicitly present `"sources": []` disables every
source and never falls back to legacy top-level station fields.

Commands accept a source ID, a unique `display_name`, or `all` where applicable:
`/sub2api status [selector]`, `/sub2api poll [selector]`,
`/sub2api subscriptions <selector>`, `/sub2api rates <selector>`,
`/sub2api report [selector]`, and `/sub2api reset [selector]`. The first snapshot and source
identity changes are silent. Failed or unconfirmed dispatches retain source-scoped, per-group
ACK state so only unacknowledged groups retry; framework confirmation can mean adapter/platform
admission or send confirmation, not end-user reading.

The Plugin declares Playwright, while non-Docker hosts must also provision Chromium (for
example, `python -m playwright install chromium`); the official Docker image includes it.
Automatic image failures fall back to authoritative text notifications. The explicit
`/sub2api report` command instead reports a visualization failure. Legacy single-source
configuration remains compatible while `sources` is absent and continues using
`SUB2API_EMAIL` / `SUB2API_PASSWORD` plus legacy top-level state. On the first explicit-source
poll, matching legacy collections migrate only when there is exactly one usable target, its
credentials are complete, it has no existing new state, and that collection's legacy
endpoint/account/timezone fingerprint matches exactly. Multi-source, existing-state, or
fingerprint-mismatch cases are never guessed; legacy top-level data is retained. Collections
with more than 20 detailed changes use one summary. One poll permits at most 200 physical
dispatches, pre-allocates that budget fairly by selected source and collection, and rotates retry
order so an early failing group cannot starve later groups. See [`sub2api_monitor/README.md`](sub2api_monitor/README.md) for the deterministic
migration procedure, 4 MiB response / 2000-record limits, security constraints, and
troubleshooting.
