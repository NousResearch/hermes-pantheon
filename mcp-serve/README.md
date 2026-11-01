<p align="center">
  <img src="../assets/mcp-serve.jpg" alt="MCP Serve plugin for Hermes Agent" width="100%">
</p>

# MCP Serve

Part of [Hermes Official Plugins](https://github.com/NousResearch/hermes-official-plugins). Formerly built into Hermes core as `hermes mcp serve`.

Run Hermes as an MCP server. Any MCP client (Claude Code, Claude Desktop, Cursor, Codex, your own harness) launches `hermes mcp-serve` over stdio and gets your Hermes messaging conversations as tools: list and read conversations across Telegram, Discord, Slack, WhatsApp, Signal, Matrix and the rest, poll or wait for new messages, send messages, and see approval requests. With `--tools` it also gets Hermes' own web, browser, vision, image generation, skills and text-to-speech tools.

## Install

```bash
hermes plugins install mcp-serve
# or straight from this repo (tracks main):
hermes plugins install NousResearch/hermes-official-plugins#mcp-serve
```

## Connect an MCP client

```json
{
  "mcpServers": {
    "hermes": {
      "command": "hermes",
      "args": ["mcp-serve"]
    }
  }
}
```

Use `["-p", "<profile>", "mcp-serve"]` to serve another profile's conversations; the plugin must be installed in that profile too (`hermes -p <profile> plugins install mcp-serve`).

**Coming from `hermes mcp serve`?** Change the client's `args` from `["mcp", "serve"]` to `["mcp-serve"]`. The ten tool names, their parameters and descriptions are unchanged. On a Hermes without the old command, `hermes mcp serve` prints this new spelling and the install line.

## Tools

| Tool | What it does |
|---|---|
| `conversations_list` | Active conversations with their session keys, platform, chat type and last activity |
| `conversation_get` | One conversation's details |
| `messages_read` | Recent messages of a conversation |
| `attachments_fetch` | Images, media and other non-text parts of one message |
| `events_poll` / `events_wait` | New messages since a cursor; `events_wait` long-polls |
| `messages_send` | Send to `platform:chat_id` (or a channel name), through Hermes' own send path |
| `channels_list` | Targets you can send to |
| `permissions_list_open` / `permissions_respond` | Approval requests seen during this session |

Messages come from the profile's `state.db`. Events start when the server starts; older history is read with `messages_read`. Reading needs no running gateway; `messages_send` does, since the platform connections live there.

## `--tools`: Hermes' tools for other agents

```json
{ "command": "hermes", "args": ["mcp-serve", "--tools"] }
```

Adds these Hermes tools next to the bridge, using this profile's configured providers and credentials:

`web_search` · `web_extract` · `browser_navigate` · `browser_click` · `browser_type` · `browser_press` · `browser_snapshot` · `browser_scroll` · `browser_back` · `browser_get_images` · `browser_console` · `browser_vision` · `vision_analyze` · `image_generate` · `skill_view` · `skills_list` · `text_to_speech`

- **Off by default.** Without `--tools` the server exposes only the conversation bridge.
- **A fixed allowlist.** Terminal, file, code execution, process, kanban and delegation tools are never exposed, whatever the profile enables: they would hand the client a shell on your machine outside Hermes' approval prompts (kanban tasks run as full agents).
- **No local files.** `browser_navigate` and `web_extract` take only web URLs (no `file:`), `vision_analyze` only `http(s):` or `data:` images (no local paths, including bare names such as `shot.png`), and `text_to_speech` writes only into Hermes' own cache (its `output_path` is not offered). Web URLs include local-network addresses such as `localhost:3000`, as they do for the Hermes model.
- **Your profile's choices apply.** Only toolsets enabled for the CLI in `hermes tools` are offered, and a tool whose backend is not configured (no web provider, no image key, no local browser) is left out, the same availability rules the Hermes model sees.
- **Your hooks still apply.** Shell hooks and plugin `pre_tool_call` hooks run around every call; `--accept-hooks` skips the first-use consent check for shell hooks, as it does for the gateway.
- Tool output is secret-redacted (`HERMES_REDACT_SECRETS`), and calls cost whatever the configured provider charges.

## Options

```bash
hermes mcp-serve                 # conversation bridge only
hermes mcp-serve --tools         # plus Hermes' tools (see above)
hermes mcp-serve --verbose       # debug logging on stderr
```

The server is stdio only. Logs go to stderr; stdout is the MCP wire.

## Development

```bash
cd hermes-official-plugins/mcp-serve
HERMES_AGENT_REPO=$HOME/src/hermes-agent PYTHONPATH=$HOME/src/hermes-agent python -m pytest -q
```

MIT licensed. Built by [Nous Research](https://nousresearch.com).
