<p align="center"><img src="../assets/agent-importers.jpg" alt="Agent Importers: official Hermes Agent plugin" width="100%"></p>

# Agent Importers for Hermes Agent

> Part of the [Hermes Official Plugins](../README.md), Nous Research's official plugins for Hermes Agent. Formerly built into Hermes Agent core (`hermes_cli/agent_import.py`, and for OpenClaw `hermes_cli/claw.py` with the `openclaw-migration` optional skill).

`hermes import-agent` brings an existing **OpenClaw** (`~/.openclaw`), **Claude Code** (`~/.claude`), **OpenAI Codex CLI** (`~/.codex`), **Cursor** (`~/.cursor`), **Grok Bot** or **OpenBot** setup into Hermes with one command. Every run shows a per-item preview first and `--dry-run` never touches disk. API keys and credentials are never imported, with one opt-in exception: OpenClaw's allowlisted secrets when you pass `--migrate-secrets`.

| Agent | Command | Default source |
|---|---|---|
| OpenClaw (also legacy Clawdbot / Moltbot) | `hermes import-agent openclaw` (old spelling: `hermes claw migrate`) | `~/.openclaw`, `~/.clawdbot`, `~/.moltbot` |
| Claude Code | `hermes import-agent claude-code` | `~/.claude` |
| Codex CLI | `hermes import-agent codex` | `~/.codex` |
| Cursor | `hermes import-agent cursor` | `~/.cursor` |
| Grok Bot | `hermes import-agent grok-bot` | an export the Bot writes (`~/Downloads/grok-bot-export*.json`) |
| OpenBot (nightly-labs/openbot desktop app) | `hermes import-agent openbot` | its data folder: `~/Library/Application Support/OpenBot`, `~/.config/OpenBot` or `%APPDATA%\OpenBot` |

## Install

```bash
hermes plugins install agent-importers
# or straight from this repo:
hermes plugins install NousResearch/hermes-official-plugins#agent-importers
```

Upgrading from a Hermes version that had `import-agent` built in: the command, its flags and your
sync manifest (`import-sync.json` in the profile) are unchanged. Profiles that registered a sync
source get the plugin automatically on update; everyone else sees the install line when they run
`hermes import-agent`.

## Usage

```bash
hermes import-agent                    # auto-detect ~/.openclaw, ~/.claude, ~/.codex or ~/.cursor
hermes import-agent openclaw           # import from ~/.openclaw (see "OpenClaw" below)
hermes import-agent claude-code        # import from ~/.claude
hermes import-agent codex              # import from ~/.codex
hermes import-agent cursor             # import from ~/.cursor
hermes import-agent claude-code --dry-run          # preview only
hermes import-agent codex --source /path/to/.codex # custom location
hermes import-agent claude-code --overwrite --yes  # replace conflicts, skip prompts
hermes import-agent --sync             # re-import every registered source that changed
hermes import-agent --sync --dry-run   # preview what a sync would do
```

It imports into the active profile (`hermes -p <name> import-agent ...` for another one).

## OpenClaw

`hermes import-agent openclaw` is the former `hermes claw migrate`, with the same flags and defaults. `hermes claw migrate ...` keeps working and runs the same code.

```bash
hermes import-agent openclaw                       # preview, confirm, back up, migrate
hermes import-agent openclaw --dry-run             # preview only
hermes import-agent openclaw --preset user-data    # user data and compatible config only
hermes import-agent openclaw --preset full --migrate-secrets --yes   # include allowlisted secrets
hermes claw cleanup --dry-run                      # what cleanup would archive
hermes claw cleanup                                # rename ~/.openclaw to ~/.openclaw.pre-migration
```

| Flag | Meaning |
|---|---|
| `--source <path>` | OpenClaw directory (default: the first of `~/.openclaw`, `~/.clawdbot`, `~/.moltbot` that exists) |
| `--dry-run` | Preview only |
| `--preset {user-data,full}` | `full` (default) is `user-data` plus the secret categories (bot tokens, provider API keys), which still import only with `--migrate-secrets`; `user-data` leaves them out entirely |
| `--overwrite` | Replace existing Hermes targets; without it a plan with conflicts is refused before anything is written |
| `--migrate-secrets` | Import allowlisted secrets (`TELEGRAM_BOT_TOKEN`, provider API keys, ...). Required even under `--preset full` |
| `--no-backup` | Skip the pre-migration snapshot |
| `--workspace-target <path>` | Absolute path to copy OpenClaw's `AGENTS.md` workspace instructions into |
| `--skill-conflict {skip,overwrite,rename}` | Imported skill name clashes: keep the existing one (default), replace it, or import as `<name>-imported` |
| `-y`, `--yes` | Skip the confirmation prompt |

These flags only apply to OpenClaw; passing them with another agent prints an error and imports nothing. OpenClaw is not part of `--sync`.

**Secrets.** Secrets migration is a separate, explicit step: a run without `--migrate-secrets` reports detected API keys and bot tokens as skipped and tells you how to add them. Even with it, only secrets with a clean Hermes destination are imported; other auth blobs are reported as skipped.

**Before it writes.** It warns when OpenClaw is still running, or when this profile's Hermes gateway is connected to messaging platforms (Telegram, Discord and Slack allow one session per bot token). Non-interactive runs stop after the preview unless you pass `--yes`. Before applying, it writes one restore-point zip of the Hermes home to its `backups/` directory (`pre-migration-<timestamp>.zip`, the last 5 kept); restore it with `hermes import <archive>`. The OpenClaw directory itself is never modified.

**Cleanup.** `hermes claw cleanup` (alias `clean`; `--source`, `--dry-run`, `-y`) lists each OpenClaw directory's workspaces and state files and renames the directory to `<name>.pre-migration` (date-stamped if that exists). Nothing is deleted; rename it back to undo. While OpenClaw is still running it asks before archiving (a non-interactive session without `--yes` aborts), since the running service would recreate an empty directory.

**What maps where, in short:** `SOUL.md` → the profile's `SOUL.md`; `MEMORY.md`, `USER.md` and daily memory files → memory entries; command approvals → `command_allowlist`; skills from the workspace, `~/.openclaw/skills`, `~/.agents/skills` → `skills/openclaw-imports/`; default model, custom providers, agent behaviour, MCP servers, TTS and messaging settings → `config.yaml` / `.env` (OpenClaw's workspace folder becomes `terminal.cwd`, unless it is inside the OpenClaw directory); docs with no Hermes equivalent are archived in the migration report directory. The full mapping is in the [Migrate from OpenClaw guide](https://hermes-agent.nousresearch.com/docs/guides/migrate-from-openclaw).

**The skill.** The plugin ships the `openclaw-migration` skill (`agent-importers:openclaw-migration`): ask Hermes to migrate from OpenClaw and it runs a guided dry run, asks about conflicts, workspace instructions and secrets, then executes the same migration script (`skills/openclaw-migration/scripts/openclaw_to_hermes.py`).

## Grok Bot

Grok Bot keeps your Bots, routines and skills in its cloud account and has no export button, so each
Bot exports itself:

1. Run `hermes import-agent grok-bot`. With no export yet, it prints a message (also saved as
   `grok-bot-import/export-prompt.txt` in your Hermes home).
2. Paste that message into a Bot's chat. The Bot writes `grok-bot-export.json` and attaches it, or
   replies with one JSON code block. Nothing in Grok Bot changes. Repeat for each Bot.
3. Download the file (or paste the Bot's whole reply into a text file) and run
   `hermes import-agent grok-bot --source <file>`. Files saved as `~/Downloads/grok-bot-export*`
   are found without `--source`; `--source` also takes a folder of exports.

| Grok Bot | Hermes |
|---|---|
| Routine on a schedule | Cron job `<Bot>: <routine>` with the same cron expression, time zone and skills, **created paused** |
| Routine on an event (Slack, GitHub, Linear, Sentry, PagerDuty, email, webhook) | Not scheduled: its full instruction and trigger go to `grok-bot-import/<bot>-event-routines.md` to rebuild |
| Skills | `skills/grok-bot-imports/<name>/SKILL.md` |
| What the Bot remembers | Memory entries in `memories/MEMORY.md` |
| Facts about you | `memories/USER.md` |
| The Bot's role and lasting rules | `SOUL.md` when it still holds Hermes' default, else memory entries |
| Apps with a remote MCP URL | `mcp_servers` in `config.yaml` (sign in again in Hermes) |
| Other connected apps | Listed in the report; their logins stay in Grok Bot |

Routines arrive paused because the Grok copy keeps running until you turn it off; two live copies
would do the work twice. Turn the routine off in Grok Bot, then `hermes cron resume <id>` (the import
prints each id). `--activate-routines` creates them running instead. Cron output stays in Hermes
until you pick a destination: `hermes cron edit <id> --deliver telegram` (or discord, slack, ...).

| Flag | Meaning |
|---|---|
| `--source <path>` | An export file, a pasted reply, or a folder of them |
| `--bot <name>` | Only this Bot from an export holding several (repeatable) |
| `--activate-routines` | Create the cron jobs running instead of paused |

Run it once per Bot into the profile that should own that Bot's work: `hermes -p <profile> import-agent
grok-bot --source <file>`. An export carrying credential-looking keys is refused whole, and
token-shaped strings inside text are replaced with `[redacted]`. Re-running skips routines whose cron
job already exists. Grok exports are one-off files, so they are not part of `--sync`.

## OpenBot

`hermes import-agent openbot` reads the OpenBot desktop app's database (`openbot.db` in its data
folder, opened read-only, so OpenBot can stay running) and each agent's workspace under
`~/OpenBot/Agents`. Every agent imports into the active profile; `--bot <name>` picks agents, and
`hermes -p <profile> import-agent openbot --bot <name>` gives one agent its own profile.

| OpenBot | Hermes |
|---|---|
| Routine (hourly, daily, weekdays, weekly, monthly, interval, advanced, custom cron, one-time) | Cron job `<agent>: <routine>` with the same schedule and time zone, **created paused** (`--activate-routines` to create running) |
| Routine with no schedule (run by hand, a script or a webhook) | Listed in `openbot-import/<agent>-event-routines.md` |
| Agent description (its role) | `SOUL.md` when it still holds Hermes' default and one agent is imported, else memory entries |
| Agent memories | Memory entries in `memories/MEMORY.md` |
| Workspace skills (`.agents/skills`, `.claude/skills`) | `skills/openbot-imports/<name>/` (OpenBot's bundled skills stay behind) |
| MCP servers | `mcp_servers` in `config.yaml`, secret-looking env vars and headers stripped and listed |

Chats, provider accounts, channels and the browser profile are not imported. OpenBot keeps running
its routines until you turn them off, so turn each off there, then `hermes cron resume <id>`.

## What maps where (Claude Code, Codex CLI, Cursor)

### Claude Code (`~/.claude`)

| Claude Code | Hermes |
|---|---|
| `CLAUDE.md` | Memory entries in `memories/MEMORY.md` |
| `settings.json` `permissions.allow` (`Bash(...)` rules) | `command_allowlist` in `config.yaml` |
| `settings.json` `permissions.deny` (`Bash(...)` rules) | `approvals.deny` in `config.yaml` |
| `~/.claude.json` / `settings.json` `mcpServers` | `mcp_servers` in `config.yaml` |
| `skills/<name>/` (dirs with `SKILL.md`) | `skills/claude-code-imports/<name>/` |
| `commands/*.md` | Reported as skipped (no direct equivalent) |

Claude's `Bash(npm run test:*)` prefix rules become `npm run test*` globs. Non-`Bash` rules and bare `Bash` are reported as not mapped.

### Codex CLI (`~/.codex`)

| Codex | Hermes |
|---|---|
| `AGENTS.md` | Memory entries in `memories/MEMORY.md` |
| `config.toml` `[mcp_servers.*]` | `mcp_servers` in `config.yaml` |
| `memories/*.md` | Memory entries in `memories/MEMORY.md` |
| `skills/<name>/` (dirs with `SKILL.md`) | `skills/codex-imports/<name>/` |

### Cursor (`~/.cursor`)

| Cursor | Hermes |
|---|---|
| `AGENTS.md` | Memory entries in `memories/MEMORY.md` |
| `rules/**/*.md`, `rules/**/*.mdc` (User Rules) | Memory entries in `memories/MEMORY.md` |
| `mcp.json` `mcpServers` | `mcp_servers` in `config.yaml` |
| `skills/**/<name>/` (dirs with `SKILL.md`, category folders allowed) | `skills/cursor-imports/<name>/` |

`.mdc` frontmatter (`description`, `globs`, `alwaysApply`) is Cursor routing metadata and is stripped; only the rule body is imported. A skill is the folder holding `SKILL.md`, so `skills/shipping/land-it/` imports as `cursor-imports/land-it/`; two skills with the same name in different folders are reported as a conflict.

## What is never imported

Credential files (`~/.claude/.credentials.json`, `~/.codex/auth.json`, `~/.cursor/cli-config.json`) are never read, and MCP server environment variables or headers with secret-looking names (`*_TOKEN`, `*_API_KEY`, `Authorization`, ...) are stripped and listed in the report so you can re-add them deliberately. Run `hermes setup` for providers, or put secrets in the profile's `.env`.

## Behaviour

- Existing MCP servers and skills with the same name are conflicts and skipped unless `--overwrite`.
- Memory entries are merged and deduplicated into the existing store; the previous `MEMORY.md` is backed up as `MEMORY.md.bak.<timestamp>` first.
- A `config.yaml` that cannot be read or parsed is never rewritten: the import records an error and leaves the file byte-identical. Writes keep your comments.
- Non-interactive runs (no TTY) stop after the preview unless you pass `--yes`.

## Sync

Every successful import registers its source in the profile's `import-sync.json`. `hermes import-agent --sync` re-imports any registered source whose files changed, without prompts, so it fits a cron job:

- The change check is a digest over exactly the files the importer reads; credential files never trigger a sync.
- Skills previously imported are refreshed in place when the source copy changes, unless you edited the imported copy (then it is a conflict and left alone).
- A run with errors keeps the old digest, so the next sync retries.

`hermes profile create <name> --clone --sync-imports` carries the manifest into a new profile, so `hermes -p <name> import-agent --sync` keeps pulling from the same trees.

## Compatibility

On a Hermes version that still has `import-agent` built in, this plugin's `import-agent` stays inert and the built-in command runs. Likewise, on a version that still has `hermes claw` built in, the plugin registers neither `claw` nor its copy of the `openclaw-migration` skill; the built-in ones run.

## License

MIT
