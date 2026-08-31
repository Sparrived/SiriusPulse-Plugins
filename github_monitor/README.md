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
- Conservative migration from legacy `tool_data/github_monitor.json`, `skill_data/github_monitor.json`, and `.persona_skills.json` layouts.

## Commands

- `/github status`
- `/github poll`

## Configuration and secrets

Configure non-secret settings through the Sirius Pulse WebUI Plugin page:

- `poll_seconds`: polling interval, 30–3600 seconds.
- `api_base_url` and optional `api_allowed_hosts`: HTTPS GitHub/GitHub Enterprise API endpoint controls.
- `webhook_host` and `webhook_port`: loopback listener address and port.
- `webhook_secret_env`: the **name** of the process environment variable holding the Webhook HMAC secret.
- `repos`: owner, repository, mode, event types, target groups, and a per-repository `github_token_env` variable name.

`github_token_env` and `webhook_secret_env` contain variable names, never credential values. The WebUI masks existing secret placeholders and rejects new plaintext GitHub Tokens and Webhook secrets instead of writing them to `plugins/_config.json`. Inject the configured names into the actual Persona Worker process environment (or map them explicitly in a non-committed Compose override); a Compose `.env` file alone is only interpolation input and is not automatic container environment injection.

Example Compose override, using the same variable names selected in Plugin settings:

```yaml
services:
  sirius-pulse:
    environment:
      SIRIUS_GITHUB_TOKEN_SIRIUS_PULSE: ${SIRIUS_GITHUB_TOKEN_SIRIUS_PULSE}
      SIRIUS_GITHUB_WEBHOOK_SECRET: ${SIRIUS_GITHUB_WEBHOOK_SECRET}
```

Use a fixed non-zero Webhook port behind a controlled host-side reverse proxy or tunnel. The listener accepts loopback peers only; production must use `sha256=` HMAC validation. `allow_unsigned_local` is exclusively for loopback development and must remain disabled in production. Do not run the retired built-in Tool and this Plugin against the same repositories during migration, or both can notify.

## Dependencies and deployment

Plugin version `1.1.0` declares Sirius Pulse `>=1.3.0`; PluginLoader enforces `_plugin_min_framework_version`/`plugin.json` compatibility before runtime registration. The Plugin declares its direct Python dependencies (`aiohttp`, `httpx`, and `playwright`) so a trusted Plugin lifecycle can provision an incomplete host environment. `httpx` and the Playwright Python package are also **shared core dependencies**: generic Provider HTTP calls and core rendering/screenshot facilities use them, so extracting GitHub Monitor does not remove them from Sirius Pulse.

The Docker image pre-installs Chromium for the shared Playwright capability. In a non-Docker environment, provision the Playwright browser runtime deliberately (for example, `python -m playwright install chromium`); a trusted PluginLoader dependency-install action may also request Chromium. Screenshots are optional: a browser failure falls back to text notification.

Keep this repository on the host and mount it read-write at `/app/plugins`; the image and PyPI distribution intentionally exclude it. The host `plugins/` directory must allow container UID `10001` to update only runtime settings such as `plugins/_config.json`, while retaining the host Git user's ownership/workflow. An ACL is usually safer than changing repository ownership, for example:

```bash
sudo setfacl -R -m u:10001:rwX plugins
```

## Durable Webhook state and migration

Webhook intake is bounded and durable on a best-effort at-least-once basis. Pending deliveries, retry progress, and replayable dead-letter payloads are persisted in the per-persona PluginDataStore. Raw GitHub event payloads can therefore remain on disk until delivery state is pruned; those bodies can include repository names, commit/Issue/PR/comment content, and GitHub user/account data. Replayable dead-letter bodies are bounded to 32 MiB. Attempting mode `0600` is best-effort permission hardening, **not encryption**; it does not protect backups, snapshots, or a host-level reader. Restrict `plugin_data/` and its backups to trusted operators.

One Plugin process/server must exclusively own each Webhook `state_path`. There is no cross-process shared-state lock or coordination: pointing multiple processes at one state file can overwrite delivery, retry, and dead-letter state and can duplicate handling. There is also no WebUI replay console.

The API and screenshot URL DNS preflight rejects obvious unsafe resolutions before a request, but it is **not socket pinning** and does not replace connection-time egress policy. Production operators must still apply connect-time network controls—such as a firewall, allowlisted proxy, or restricted network namespace—so only intended GitHub/GHE hosts and ports are reachable.

Legacy source files are read but never deleted or rewritten. If a historical plaintext `github_token` or `webhook_secret` is found, migration remains incomplete and preserves the usable source rather than replacing it with an unprovisioned environment-variable name. Rotate the exposed credential manually, inject a replacement through the process environment, update settings to its `*_env` reference, and only then consider the migration complete. Runtime state remains isolated per persona in `plugin_data/_plugin_github_monitor_data.json`.
