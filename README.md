<p align="center">
  <img src="assets/hermes-official-plugins-banner.jpg" alt="Hermes Official Plugins, built and maintained by Nous Research" width="100%">
</p>

<h1 align="center">Hermes Official Plugins ☤</h1>

<p align="center">
  <b>The official plugins for <a href="https://github.com/NousResearch/hermes-agent">Hermes Agent</a>, built and maintained by <a href="https://nousresearch.com">Nous Research</a>.</b><br>
  One repo, every first-party plugin, installed in one line from the Hermes plugin catalog.
</p>

<p align="center">
  <a href="https://hermes-agent.nousresearch.com/docs/plugins"><img src="https://img.shields.io/badge/Plugin_Catalog-browse-FFDE1A?style=for-the-badge&labelColor=0B0C0E" alt="Plugin Catalog"></a>
  <a href="https://hermes-agent.nousresearch.com/docs/"><img src="https://img.shields.io/badge/Docs-hermes--agent-E5DEC9?style=for-the-badge&labelColor=0B0C0E" alt="Docs"></a>
  <a href="https://github.com/NousResearch/hermes-official-plugins/actions/workflows/tests.yml"><img src="https://img.shields.io/github/actions/workflow/status/NousResearch/hermes-official-plugins/tests.yml?branch=main&style=for-the-badge&label=tests&labelColor=0B0C0E&color=103CE7" alt="Tests"></a>
  <a href="https://discord.gg/NousResearch"><img src="https://img.shields.io/badge/Discord-join-E62B28?style=for-the-badge&logo=discord&logoColor=white&labelColor=0B0C0E" alt="Discord"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-E5DEC9?style=for-the-badge&labelColor=0B0C0E" alt="MIT"></a>
</p>

---

## What is Hermes Agent?

[Hermes Agent](https://github.com/NousResearch/hermes-agent) is the self-improving AI agent from Nous Research. It writes skills from experience and keeps improving them as it works, remembers what matters across sessions, and runs where you are: the terminal, the Hermes Desktop app, Telegram, Discord, Slack, WhatsApp, Signal and more, all through one gateway. You can bring any model (Nous Portal, OpenRouter, Anthropic, OpenAI, your own endpoint) and run it on a laptop, a $5 VPS or serverless infrastructure.

```bash
curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash
```

## A lean core, with plugins at the edges

The Hermes core keeps the parts every user needs: the agent loop, memory, skills, the gateway, model routing and the tool runtime. Everything specific to one product, one vendor or one hobby belongs in a **plugin**, which you install when you want it and which ships on its own schedule.

That gives you:

- **Smaller, faster installs.** You don't download a Spotify client to run a coding agent.
- **Fixes without waiting for a release.** A plugin ships a fix as soon as it's reviewed. It doesn't wait for the next Hermes version.
- **A clear owner for each integration.** Vendor plugins live with the vendors who build them, community plugins with their authors, and Nous's own plugins live here.

## The plugin catalog

The [**Hermes plugin catalog**](https://hermes-agent.nousresearch.com/docs/plugins) is the reviewed index of everything you can install: model providers, memory providers, messaging platforms, Desktop extensions, voice, web and automation tools. It has hundreds of entries from Nous, vendors and the community. Each entry is **pinned to a reviewed commit**, so what you install is exactly what was reviewed, and an update moves you only when a new pin passes review.

```bash
hermes plugins search            # browse the catalog
hermes plugins info spotify      # what it does, what it needs, which commit
hermes plugins install spotify   # install at the reviewed pin
hermes plugins update spotify    # follow the catalog to the next reviewed pin
```

Hermes Desktop has the same catalog under **Capabilities → Plugins**.

Catalog entries come in two tiers:

| Tier | Who maintains it | Where it lives |
|---|---|---|
| **official** | Nous Research | **This repo**, one directory per plugin |
| **community** | Vendors and independent authors | Their own repos, reviewed when they enter the catalog |

## The plugins

<table>
<tr>
<td width="50%" valign="top">
<a href="homeassistant/"><img src="assets/homeassistant.jpg" alt="Home Assistant plugin"></a>
<h3><a href="homeassistant/">Home Assistant</a></h3>
<code>hermes plugins install homeassistant</code>
<p>A gateway platform plus four tools. Hermes watches your Home Assistant event bus, reacts to the state changes you care about, controls devices over the REST API, and replies as Home Assistant notifications. Cron jobs can deliver straight to your phone with <code>deliver=homeassistant</code>.</p>
<p><code>ha_list_entities</code> · <code>ha_get_state</code> · <code>ha_list_services</code> · <code>ha_call_service</code></p>
</td>
<td width="50%" valign="top">
<a href="spotify/"><img src="assets/spotify.jpg" alt="Spotify plugin"></a>
<h3><a href="spotify/">Spotify</a></h3>
<code>hermes plugins install spotify</code>
<p>Seven tools over the Spotify Web API: playback, Spotify Connect devices, the queue, search, playlists, albums and your library, plus <code>hermes spotify login</code> for a loopback PKCE login with your own Spotify developer app. Playback control needs Premium.</p>
<p><code>spotify_playback</code> · <code>spotify_devices</code> · <code>spotify_queue</code> · <code>spotify_search</code> · <code>spotify_playlists</code> · <code>spotify_albums</code> · <code>spotify_library</code></p>
</td>
</tr>
<tr>
<td width="50%" valign="top">
<a href="claude-subscription-directsdk/"><img src="assets/claude-subscription-directsdk.jpg" alt="Claude Subscription DirectSDK plugin"></a>
<h3><a href="claude-subscription-directsdk/">Claude Subscription DirectSDK</a> <sup>experimental</sup></h3>
<code>hermes plugins install claude-subscription-directsdk</code>
<p>A model provider that runs Hermes turns on your Claude Pro or Max subscription through the official Claude Code CLI, with no separate API key or per-token bill. Hermes keeps its own agent loop, tools, approvals and compaction; the plugin drives the logged-in <code>claude</code> binary as a request-scoped model client, and refuses cleanly with an install hint when Claude Code is missing.</p>
<p>Pick it in <code>hermes model</code> → <b>Claude Subscription DirectSDK (Experimental)</b></p>
</td>
<td width="50%" valign="top">
<a href="memory-wiki/"><img src="assets/memory-wiki.jpg" alt="Memory Wiki plugin"></a>
<h3><a href="memory-wiki/">Memory Wiki</a></h3>
<code>hermes plugins install hermes-memory-wiki</code>
<p>A dashboard and Desktop tab that turns your local session history into browsable subject pages and daily logs, plus a read-only audit panel for <code>MEMORY.md</code> and <code>USER.md</code>.</p>
<p>Dashboard tab · reads local history, writes nothing</p>
</td>
</tr>
<tr>
<td width="50%" valign="top">
<a href="blender/"><img src="assets/blender.jpg" alt="Blender plugin"></a>
<h3><a href="blender/">Blender</a></h3>
<code>hermes plugins install blender</code>
<p>The official Blender Lab MCP server over stdio, pinned to upstream, with a Blender workflow skill, so Hermes can read and change your Blender scene. Needs Blender 5.1+ with its MCP add-on, Git and uv. Its tools run Python inside Blender, so use trusted workflows.</p>
<p>MCP server + skill · Windows, macOS, Linux</p>
</td>
<td width="50%" valign="top">
<a href="snyk/"><img src="assets/snyk.jpg" alt="Snyk plugin"></a>
<h3><a href="snyk/">Snyk</a></h3>
<code>hermes plugins install snyk</code>
<p>Scan code (SAST), dependencies, container images, IaC and SBOMs through Snyk's first-party MCP server, with the snyk-security-scan workflow skill. Needs Node.js/npm and a free Snyk account.</p>
<p>MCP server + skill · browser login via <code>snyk_auth</code></p>
</td>
</tr>
<tr>
<td width="50%" valign="top">
<a href="touchdesigner/"><img src="assets/touchdesigner.jpg" alt="TouchDesigner plugin"></a>
<h3><a href="touchdesigner/">TouchDesigner</a></h3>
<code>hermes plugins install touchdesigner</code>
<p>Drive a live TouchDesigner session through 404.zero's free twozero MCP server, which runs inside TouchDesigner itself, with the touchdesigner-mcp workflow skill and 20+ reference files (operators, GLSL, audio-reactive, instancing) bundled.</p>
<p>MCP server + skill · TouchDesigner on macOS or Windows</p>
</td>
<td width="50%" valign="top">
<a href="accent-picker/"><img src="assets/accent-picker.jpg" alt="Accent Picker plugin"></a>
<h3><a href="accent-picker/">Accent Picker</a> <sup>desktop</sup></h3>
<code>hermes plugins install accent-picker</code>
<p>An OKLCH colour wheel in the Hermes Desktop status bar. Drag it and the whole theme retints live, so theme authors can try accents in place. The colour is scratch state and never persisted.</p>
<p>Desktop plugin · status bar + palette commands</p>
</td>
</tr>
<tr>
<td width="50%" valign="top">
<a href="langfuse/"><img src="assets/langfuse.jpg" alt="Langfuse plugin"></a>
<h3><a href="langfuse/">Langfuse</a></h3>
<code>hermes plugins install langfuse</code>
<p>Traces every Hermes turn to Langfuse: one span per turn, one generation per LLM call with usage and cost from Hermes' own pricing (cache and reasoning tokens included), one observation per tool call and subagent. Content capture is sanitized by default, with metadata-only and full modes.</p>
<p>Observability hooks · Langfuse Cloud or self-hosted</p>
</td>
<td width="50%" valign="top">
<a href="rea/"><img src="assets/rea.jpg" alt="REA plugin"></a>
<h3><a href="rea/">REA</a></h3>
<code>hermes plugins install rea</code>
<p>Reverse engineer programs with REA's MCP server: native binaries, .NET assemblies, Electron and JavaScript apps, Android packages, firmware and browser activity, with REA's reverse-engineer-anything workflow skill. Static JavaScript and archive inspection work out of the box; native decompilation needs your own Ghidra, Hopper or IDA. Needs Node.js 22.19+.</p>
<p>MCP server + skill · about 170 tools, loaded on demand</p>
</td>
</tr>
<tr>
<td width="50%" valign="top">
<a href="xchat/"><img src="assets/xchat.jpg" alt="X Chat plugin"></a>
<h3><a href="xchat/">X Chat</a></h3>
<code>hermes plugins install xchat</code>
<p>Talk to Hermes in X's end-to-end encrypted DMs. Hermes runs as the X account you connect: chat with it in your own self-chat ("message yourself"), or give it a separate account and DM that. Messages are decrypted and encrypted locally with X's official Chat XDK, so X only routes ciphertext. Supports encrypted media, threaded replies, group chats with mention gating, and cron delivery. <code>hermes xchat setup</code> shows which account Hermes will run as and registers its keys. Needs an X developer app with chat API access.</p>
<p>Gateway platform <code>xchat</code> · end-to-end encrypted</p>
</td>
<td width="50%" valign="top">
<a href="singularity/"><img src="assets/singularity.jpg" alt="Singularity/Apptainer plugin"></a>
<h3><a href="singularity/">Singularity/Apptainer</a></h3>
<code>hermes plugins install singularity</code>
<p>A terminal backend that runs Hermes's terminal, file and code tools inside a hardened, rootless Apptainer or Singularity container on this machine, with CPU/memory limits and an optional persistent overlay. Built for HPC clusters and shared machines where Docker isn't available. Formerly built into Hermes core.</p>
<p>Terminal backend <code>terminal.backend: singularity</code> · Linux</p>
</td>
</tr>
<tr>
<td width="50%" valign="top">
<a href="agent-importers/"><img src="assets/agent-importers.jpg" alt="Agent Importers plugin"></a>
<h3><a href="agent-importers/">Agent Importers</a></h3>
<code>hermes plugins install agent-importers</code>
<p><code>hermes import-agent</code> brings an OpenClaw, Claude Code, Codex CLI, Cursor or Grok Bot setup into Hermes in one command (Grok Bot routines become Hermes cron jobs): instructions, Cursor rules and memories become memory entries, Bash permission rules become allow/deny lists, MCP servers land in <code>config.yaml</code> and skills are copied. Always previews first and never imports API keys (OpenClaw's allowlisted secrets only with <code>--migrate-secrets</code>); <code>--sync</code> re-imports sources that changed. The old <code>hermes claw migrate</code> / <code>hermes claw cleanup</code> spelling and the openclaw-migration skill come along. Formerly built into Hermes core.</p>
<p>CLI command · <code>hermes import-agent [openclaw|claude-code|codex|cursor|grok-bot]</code></p>
</td>
<td width="50%" valign="top">
<a href="mcp-serve/"><img src="assets/mcp-serve.jpg" alt="MCP Serve plugin"></a>
<h3><a href="mcp-serve/">MCP Serve</a></h3>
<code>hermes plugins install mcp-serve</code>
<p>Run Hermes as an MCP server: <code>hermes mcp-serve</code> gives Claude Code, Claude Desktop, Cursor, Codex or any MCP client your messaging conversations across every connected platform, live new-message events and sending. <code>--tools</code> (off by default) adds Hermes' web, browser, vision, image generation, skills and TTS tools; terminal, file and kanban tools are never exposed. Formerly <code>hermes mcp serve</code> in core.</p>
<p>stdio MCP server · 10 conversation tools, +17 with <code>--tools</code></p>
</td>
</tr>
<tr>
<td width="50%" valign="top">
<a href="bubblewrap/"><img src="assets/bubblewrap.jpg" alt="Bubblewrap plugin"></a>
<h3><a href="bubblewrap/">Bubblewrap</a></h3>
<code>hermes plugins install bubblewrap</code>
<p>A terminal backend that runs every <code>terminal</code> and <code>execute_code</code> command in its own bwrap sandbox on this machine, at the same paths: read-only root, writable working directory, SSH keys, cloud credentials and the Hermes home hidden, private <code>/tmp</code> and <code>/proc</code>, agent sockets masked, memory, CPU and process limits. No image, no daemon. The file tools (<code>read_file</code>, <code>write_file</code>, <code>patch</code>, <code>search_files</code>) run on the host, outside the sandbox.</p>
<p>Terminal backend <code>terminal.backend: bubblewrap</code> · Linux</p>
</td>
<td width="50%" valign="top">
<a href="meshy/"><img src="assets/meshy.jpg" alt="Meshy plugin"></a>
<h3><a href="meshy/">Meshy</a></h3>
<code>hermes plugins install meshy</code>
<p>Make 3D models with Meshy's first-party MCP server: text or image to 3D, texturing, rigging and animation, format conversion, and 3D-print preparation (printability repair, multicolor 3MF, split into parts, slicer handoff), with a meshy-3d workflow skill. Hermes asks before every credit-spending call. Needs Node.js 18+ and a Meshy API key (Pro plan).</p>
<p>MCP server + skill · 28 tools, loaded on demand</p>
</td>
</tr>
<tr>
<td width="50%" valign="top">
<a href="advanced-delegation/"><img src="assets/advanced-delegation.jpg" alt="Advanced Delegation plugin"></a>
<h3><a href="advanced-delegation/">Advanced Delegation</a></h3>
<code>hermes plugins install advanced-delegation</code>
<p>Two more ways for <code>delegate_task</code> to route subagents. <b>Model routes</b>: the agent picks a named model route per subagent (fast, deep, local...) from a list you write. <b>Profile routes</b>: it runs a subagent as one of your Hermes profiles, with that profile's model, skills and credentials. Only the names and your one-line descriptions reach the model.</p>
<p><code>hermes delegation config</code> · <code>/delegation</code> to switch modes in a chat · Hermes 0.22.5+</p>
</td>
<td width="50%" valign="top">
<a href="camofox/"><img src="assets/camofox.jpg" alt="Camofox plugin"></a>
<h3><a href="camofox/">Camofox</a></h3>
<code>hermes plugins install camofox</code>
<p>Browse with <a href="https://github.com/jo-inc/camofox-browser">Camofox</a>, a self-hosted anti-detection Firefox (Camoufox) behind a local REST server: navigate, accessibility snapshots with clickable refs, click, type, scroll, keys, screenshots analysed by your vision model, read-only JavaScript, and cookie import. Optional persistent profiles keep logins across restarts. Your existing <code>CAMOFOX_URL</code> and <code>browser.camofox</code> settings carry over. Formerly built into Hermes core.</p>
<p>11 browser tools · toolset <code>camofox</code> · needs a running camofox-browser server</p>
</td>
</tr>
<tr>
<td width="50%" valign="top">
<a href="elevenlabs-sound-effects/"><img src="assets/elevenlabs-sound-effects.jpg" alt="ElevenLabs Sound Effects plugin"></a>
<h3><a href="elevenlabs-sound-effects/">ElevenLabs Sound Effects</a></h3>
<code>hermes plugins install elevenlabs-sound-effects</code>
<p>Sound effects on demand from <a href="https://elevenlabs.io/docs/capabilities/sound-effects">ElevenLabs</a>: describe a sound ("glass shattering on a tile floor", "90s hip-hop drum loop, 90 BPM") and Hermes generates it, up to 30 seconds and optionally a seamless loop, and sends you the audio file. Uses the same <code>ELEVENLABS_API_KEY</code> as Hermes's ElevenLabs voice; model, format and endpoint are plugin settings.</p>
<p><code>elevenlabs_sound_effect</code> · billed to your ElevenLabs account</p>
</td>
<td width="50%" valign="top">
<a href="elevenlabs-music/"><img src="assets/elevenlabs-music.jpg" alt="ElevenLabs Music plugin"></a>
<h3><a href="elevenlabs-music/">ElevenLabs Music</a></h3>
<code>hermes plugins install elevenlabs-music</code>
<p>Songs and instrumentals from <a href="https://elevenlabs.io/docs/overview/capabilities/music">ElevenLabs Music</a>: describe a track or paste your lyrics and Hermes writes, generates and sends it, 3 seconds to 10 minutes. It can redo one section, extend or loop a song, split it into vocal, drum and bass stems, and score a video. Long songs finish in the background and arrive in the chat. In Hermes Desktop each track gets a music card with synced lyrics and a library tab.</p>
<p><code>elevenlabs_music_compose</code> and 7 more · billed to your ElevenLabs account</p>
</td>
</tr>
<tr>
<td width="50%" valign="top">
<h3>More on the way</h3>
<p>Official plugins move in here as they leave Hermes core or join from their own repos. Browse everything else, from Nous, vendors and the community, in the <a href="https://hermes-agent.nousresearch.com/docs/plugins">plugin catalog</a>. Experimental plugins we built but don't support live in <a href="https://github.com/NousResearch/hermes-labs-plugins">hermes-labs-plugins</a>.</p>
</td>
<td width="50%" valign="top">
</td>
</tr>
</table>


### Already used one of these?

Most plugins here used to live somewhere else: Home Assistant and Spotify were built into Hermes core and then briefly had their own repos, Langfuse shipped bundled with core as `observability/langfuse`, the Singularity/Apptainer terminal backend and the Camofox browser backend were built into core, MCP Serve was core's `hermes mcp serve`, and the rest each had a standalone `NousResearch/hermes-*` repo. **Except for MCP Serve, you don't need to do anything.**

- **You used the built-in Home Assistant, Spotify, Langfuse, Singularity or Camofox backend:** `hermes update`, or the first start after updating, installs the plugin from the catalog with your existing config, env vars and login.
- **You installed it from the catalog:** the catalog now points here, so `hermes plugins update <name>` moves your install to this repo and keeps your files.
- **You used the built-in `hermes import-agent`:** profiles that registered an import for `--sync` get the `agent-importers` plugin on update; anywhere else, running `hermes import-agent` prints the one-line install (`hermes plugins install agent-importers`).
- **You used `hermes claw migrate` (OpenClaw):** it now comes with the same plugin. After `hermes plugins install agent-importers`, both `hermes import-agent openclaw` and the old `hermes claw migrate` / `hermes claw cleanup` work with the same flags.
- **You ran `hermes mcp serve`:** install with `hermes plugins install mcp-serve` and change your MCP client's args from `["mcp", "serve"]` to `["mcp-serve"]`. MCP clients are configured outside Hermes, so this one is not installed for you.
- **You installed it by URL** (for example `hermes plugins install NousResearch/hermes-homeassistant`): reinstall once from the catalog with `hermes plugins install <name> --force` to follow future updates. The old repos are archived and get no new fixes.

## Repository layout

```
hermes-official-plugins/
├── assets/                         # README and catalog art
├── homeassistant/                  # one self-contained plugin per directory
│   ├── plugin.yaml                 #   manifest: name, tools, env vars, Hermes version
│   ├── __init__.py                 #   register(ctx)
│   ├── pyproject.toml              #   plugin dependencies
│   └── tests/                      #   the plugin's own suite
├── blender/                        # portable plugins: plugin.json + mcp.json + skills/
├── accent-picker/                  # Desktop plugins: plugin.yaml + desktop/plugin.js
└── ...
```

Each directory is a complete Hermes plugin. The catalog installs one by `repo` + `subdir` at a pinned commit, and Hermes fetches only that directory (a blobless sparse clone), so installing Spotify never downloads Home Assistant.

## Development

Every plugin is tested against **current Hermes core on Linux, macOS and Windows**, on every push and pull request. A nightly run catches core changes that break a plugin before users see them.

```bash
git clone https://github.com/NousResearch/hermes-agent
git clone https://github.com/NousResearch/hermes-official-plugins
cd hermes-official-plugins/spotify
HERMES_AGENT_REPO=../../hermes-agent PYTHONPATH=../../hermes-agent python -m pytest -q
```

To try your local checkout in a real Hermes, install the directory: `hermes plugins install ./spotify --force`.

**Releasing:** merge to `main`, then open a PR on [hermes-agent](https://github.com/NousResearch/hermes-agent) that bumps the plugin's `sha:` in `plugin-catalog/<name>.yaml`. The catalog pin is the release; every installed copy moves on its next `hermes plugins update`.

**Issues:** open them here and name the plugin in the title (`[spotify] ...`). Bugs in Hermes core belong on [hermes-agent](https://github.com/NousResearch/hermes-agent/issues).

## Build your own

Plugins are plain Python with a `plugin.yaml`. Start with the [plugin guide](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/) and the [example plugins](https://github.com/NousResearch/hermes-example-plugins), then [submit yours to the catalog](https://github.com/NousResearch/hermes-agent/tree/main/plugin-catalog).

---

<p align="center">
  <sub>MIT licensed · Built by <a href="https://nousresearch.com">Nous Research</a> · ☤</sub>
</p>
