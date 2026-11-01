<p align="center"><img src="../assets/memory-wiki.jpg" alt="Memory Wiki: official Hermes Agent plugin" width="100%"></p>

# hermes-memory-wiki

> Part of [Hermes Official Plugins](../README.md), Nous Research's official plugins for Hermes Agent. Formerly [`NousResearch/hermes-memory-wiki`](https://github.com/NousResearch/hermes-memory-wiki), which is archived; this directory carries its full history.

Memory Wiki dashboard and native Desktop plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent):
a browsable **session-history wiki** (subject pages and daily logs derived deterministically from
your local `state.db`) plus a read-only **Persistent Memory audit panel** showing the `MEMORY.md` /
`USER.md` entries the agent actually carries into every session.

No LLM calls, no writes: the plugin opens `state.db` read-only through the public
`hermes_state.SessionDB` API and only reads memory files. Editing memory stays with the `memory`
tool (prompt-cache safe).

## What you get

A **Memory Wiki** tab in `hermes dashboard` (after *Sessions*) with:

- **Subjects** — topics derived primarily from every user message, with session titles as
  secondary evidence and a fallback when user content is absent. Plain-language phrases,
  explicitly discussed tool identifiers, paths, packages and commands are supported. Assistant
  text can supply matching context only; it cannot introduce or score topics. Tool, system,
  developer and tool-call metadata are excluded, as are unverified session previews.
  Subject snippets prefer user text; daily subjects and work items follow the same policy.
- **Daily logs** — per-day activity with sessions, subjects and "what we did" work items.
- **Recent activity** — the ten most recently active sessions.
- **Persistent Memory** — agent notes and user profile entries, rendered exactly as the memory tool
  parses them, so you can audit what the agent remembers.

Backend routes (mounted at `/api/plugins/memory-wiki/`, behind the dashboard's normal auth gate):

| Route | Returns |
|---|---|
| `GET /overview?subject_limit=&day_limit=` | subjects, daily logs, recent sessions, notes |
| `GET /subjects?q=&limit=` | subject list, optional substring filter |
| `GET /subjects/{slug}` | one subject (404 when unknown) |
| `GET /days?limit=` | daily logs |
| `GET /days/{date}` | one `YYYY-MM-DD` log (404 when unknown) |

Profile scoping is handled by the host (`?profile=<name>` on any REST call); the routes resolve
`HERMES_HOME` per request.

## Install

From the Nous plugin catalog (pinned, reviewed commit):

```bash
hermes plugins install hermes-memory-wiki --enable
```

Or directly from this repository:

```bash
hermes plugins install hermes-memory-wiki --enable
```

Then start (or restart) the dashboard — `hermes dashboard` — and open the **Memory Wiki** tab. The
plugin must be in `plugins.enabled` (the `--enable` flag or `hermes plugins enable memory-wiki`
does that); the web server never imports backend code from plugins you have not enabled.

Uninstall: `hermes plugins remove memory-wiki`.

Requires Hermes Agent ≥ 0.21 (dashboard plugin SDK with `fetchJSON`).

## Native Desktop UI (opt-in)

This package includes `desktop/plugin.js`, a native `/memory-wiki` page with a sidebar entry
and **Open Memory Wiki** command. It uses Desktop’s UI kit and the existing backend; no app
rebuild, iframe, or web dashboard is needed. Subjects and daily logs are deterministic views
of session history, not LLM-generated wiki pages or Hindsight memories. Persistent Memory
shows read-only agent notes and user profile entries. Session buttons open native history
when the host supports `openSession`.

Use a current Hermes Desktop with the unified-package disk-plugin SDK. Install the backend
with the command above on the machine running the gateway and enable it for the desired
profile (`hermes plugins enable memory-wiki`). Restart that gateway so its API routes mount.

The Desktop half must be installed on the **client machine**. For a local unified-package
install, Desktop discovers `plugins/memory-wiki/desktop/plugin.js` and copies it into its
local `desktop-plugins/memory-wiki/` directory. In **Capabilities → Plugins**, use **Rescan**
if needed, then enable **Memory Wiki**: it ships with `defaultEnabled: false`. The Python
backend enable switch is separate from the Desktop toggle.

For a **remote gateway**, installing the backend on the server does not install the UI on
the client. Install the same reviewed package locally with Desktop’s **Install from Git**
Desktop target, or copy this directory’s `desktop/plugin.js` to
`$HERMES_HOME/desktop-plugins/memory-wiki/plugin.js` on the client. Enable the Desktop plugin
and the server backend separately. Reads use the active connection and profile; switching
resets the page’s search and detail selection.

For checkout development, a locally linked unified package watches `desktop/plugin.js` for
hot reload. An ordinary installed package needs **Rescan** after edits to refresh Desktop’s
copy. Disable/re-enable or hot reload removes the page contributions and clears its queries.
No production installation is part of the development/test commands below.

This checkout’s Desktop addition is not a catalog release. After review and merge, a new
catalog commit pin is required before catalog installs receive it.

## Layout

```
hermes-memory-wiki/
├── plugin.yaml                  # plugin manifest (name: memory-wiki)
├── __init__.py                  # register(ctx) — no tools/hooks; UI/backend plugin
├── dashboard/
│   ├── manifest.json            # tab declaration (/memory-wiki, icon Database)
│   ├── plugin_api.py            # FastAPI router, mounted at /api/plugins/memory-wiki/
│   ├── memory_wiki_core.py      # stdlib-only aggregation over SessionDB rows
│   ├── src/index.jsx            # UI source (JSX, uses window.__HERMES_PLUGIN_SDK__)
│   └── dist/index.js            # committed build artefact the dashboard loads
├── desktop/plugin.js            # native Desktop disk plugin, plain ESM (no build)
├── tests/                       # node:test DOM tests and pytest backend tests
└── package.json                 # `npm run build` → dashboard/dist/index.js (esbuild)
```

## Development

Use Node.js 22.12+ for the native DOM tests.

```bash
npm ci                      # build/test dependencies only; runtime React comes from Desktop
npm run build               # rebuild dashboard/dist/index.js
npm run check               # dashboard and emitted Desktop ESM syntax
npm test                    # node:test DOM behavior against desktop/plugin.js
HERMES_AGENT_REPO=~/.hermes/hermes-agent \
  ~/.hermes/hermes-agent/.venv/bin/python -m pytest tests/ -o "addopts="
```

Desktop tests use a test-only SDK boundary with the native component prop contracts and a
small query lifecycle shim; they mount the actual production plugin in React/jsdom. They do
not replace a live Desktop integration check of host routing, theme rendering, or session
opening. No runtime dependencies are added to the disk file.

`dashboard/dist/index.js` is a plain IIFE; the dashboard caches it aggressively, so after a
rebuild restart the server or navigate away and back. Adding a manifest at runtime needs
`GET /api/dashboard/plugins/rescan`; changing `plugin_api.py` needs a server restart.

## Credits

- **Abdullah Raja ([@Araja119](https://github.com/Araja119))** — original author of the Memory Wiki
  (aggregation engine, API and dashboard page) in hermes-agent PR
  [#31244](https://github.com/NousResearch/hermes-agent/pull/31244). Authorship is preserved via
  `Co-authored-by` trailers on the commits that carry that code.
- **Teknium / Nous Research** — salvage and hardening in
  [#89940](https://github.com/NousResearch/hermes-agent/pull/89940) (Persistent Memory audit panel),
  and the conversion into this standalone plugin.
- The "show curated memory in the dashboard" idea was independently proposed earlier by
  [@scotty87](https://github.com/scotty87) in hermes-agent #33043.

## License

MIT — see [LICENSE](LICENSE).
