# Claude Subscription DirectSDK (Experimental) — Hermes plugin

> Part of [Hermes Official Plugins](../README.md), Nous Research's official plugins for Hermes Agent. Formerly [`NousResearch/hermes-plugin-claude-subscription-directsdk`](https://github.com/NousResearch/hermes-plugin-claude-subscription-directsdk), which is archived; this directory carries its full history.

![Claude Subscription DirectSDK — a Hermes model-provider plugin: Hermes → claude CLI → stream-json, on your Claude Pro/Max subscription](assets/hero-technohellenic.png)

Standalone Hermes Agent model-provider plugin: `claude-subscription-directsdk-experimental`, displayed as **Claude Subscription DirectSDK (Experimental)**. It drives the unmodified official Claude Code executable as a request-scoped model client for your Claude Pro/Max subscription. Hermes keeps its normal agent loop, tools, approvals and compaction. Despite the name, the implementation speaks native stream-json directly and does not require the Python Agent SDK package.

## Requirements

- Hermes Agent 0.21.4 or newer, or `main` at/after `118984d7a02f` (hermes-agent PR #117451, the core half of #105863). On an older Hermes the plugin fails to load with a clear `Failed to load user provider plugin` warning rather than half-working.
- Python 3.10+ on Linux, macOS or Windows. Native runs in its own process group (POSIX session / Windows `CREATE_NEW_PROCESS_GROUP`) and cancellation kills the whole tree (`killpg` / `taskkill /T`), so the `node` child behind the npm `claude.cmd` shim never outlives a cancelled request. The suite runs on all three in CI.
- **The official Claude Code CLI, installed and logged in.** This plugin has no credentials of its own; everything goes through `claude`. Haiku 5.5 (the `haiku` alias) needs Claude Code **2.1.293** or newer, Sonnet 5.5 (the `sonnet` alias) needs **2.1.284** or newer and Opus 5.5 (the `opus` alias) needs **2.1.280** or newer; an older CLI answers `400 Claude Code … does not support this model`, so run `claude update` first ([model configuration](https://code.claude.com/docs/en/model-config#model-aliases)).

The plugin checks for `claude` at every seam and never guesses:

| When | What happens without `claude` |
| --- | --- |
| plugin load | one warning in the log; the provider stays registered so setup can explain what is missing |
| `hermes model` → this provider | the flow stops with the install hint and writes nothing to config |
| `/model`, Desktop/TUI picker | the pinned catalog is listed (no live account picker) |
| a request | fails immediately with the install hint instead of a `Popen` traceback |

Install Claude Code with `npm install -g @anthropic-ai/claude-code`. `claude` is looked up on `PATH`, then in the usual install directories (`~/.local/bin`, `~/.claude/local`, `~/bin`, `~/.npm-global/bin`, `~/.bun/bin`, `~/.volta/bin`, `/opt/homebrew/bin`, `/usr/local/bin`), so a backend started by a LaunchAgent or Hermes Desktop with a minimal `PATH` still finds it. Point `CLAUDE_SUBSCRIPTION_DIRECTSDK_COMMAND` at the binary if it lives anywhere else. The `plugin.yaml` declares the same requirement under `external_dependencies`.

## Install

```sh
hermes plugins install claude-subscription-directsdk      # from the Hermes plugin catalog (pinned, reviewed)
# or straight from GitHub (tracks main):
hermes plugins install NousResearch/hermes-official-plugins#claude-subscription-directsdk

claude auth login
hermes model          # pick "Claude Subscription DirectSDK (Experimental)"
```

Per-profile installs go under `~/.hermes/profiles/<profile>/plugins/` instead. The plugin declares `kind: model-provider`, so Hermes' provider discovery imports it directly; no `plugins.enabled` entry is needed.

## Status

**Single-request admission is implemented and live-qualified.** Native 2.1.263 can attempt extra generations despite `--max-turns 1` and disabled HTTP retries. A request-scoped loopback relay now forwards only the first Messages request and rejects subsequent attempts locally. Hermes receives the first completed upstream response, its actual usage and native stop reason; blocked native recovery does not turn a completed response into an exception. Truncated/incomplete streams and upstream HTTP errors remain failures.

The integrated subscription-backed review completed seven Hermes calls with exactly seven upstream requests, twelve Hermes tool executions and twelve durable tool results. Context grew from 138,498 to 165,697 tokens; follow-up cache reads averaged 97.13%. Native accounting reported $0.8793728 in list-price equivalent, not a verified subscription charge. An earlier attempt began at 187K but exceeded the current native route's 200K bound after tools; that incomplete attempt is not counted as a successful review.

A real subscription-backed Hermes task built and tested a CSV auditor. Separate live qualifications exercised streaming tool rounds, restart/resume, host-side denial, authentic steering, cancellation during generation, and a real CLI subagent completion. This is a review build, not a full-parity or production-readiness claim. See the remaining limitations below.

Native Claude Code **2.1.263** is qualified. Replay acknowledgments and extra-body behavior are version-sensitive interfaces, not a public arbitrary-history SDK guarantee. The generic host support (external-process providers, replay carriers, setup gating) lives in Hermes core; this repository is only the provider.

## Login and select

```sh
claude auth login
hermes --provider claude-subscription-directsdk-experimental -m sonnet
```

Authentication belongs to the official CLI. The plugin never opens, copies, refreshes, or prints its credential files. No Hermes API key is required or sent by the plugin. The normal Hermes client path rejects inherited API-key, custom Anthropic endpoint, and cloud-backend overrides before spawning; the error names conflicting environment variables without printing their values. Remove those overrides from the launching environment when selecting OAuth. There is no silent HTTP/API-key fallback in this client.

Subscription entitlement and extra-usage settings still belong to the account and native service. Disable extra usage in the account if you do not want overage billing. A native list-price cost estimate is not proof of a subscription charge.

For a separately CLI-managed auth directory:

```sh
CLAUDE_CONFIG_DIR=/path/to/official-cli-config claude auth login
export CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR=/path/to/official-cli-config
```

An inherited `CLAUDE_CONFIG_DIR` also works; the per-profile `claude_config_dir` setting takes precedence over both environment directories. To select an executable outside PATH, set `CLAUDE_SUBSCRIPTION_DIRECTSDK_COMMAND` to its absolute path. There is no unrestricted public CLI-flags setting; isolation and denial flags are plugin-owned. The low-level Python `Client(env=...)` injection is available for explicitly controlled local fixtures and does not apply the inherited-environment guard. It is not the normal Hermes provider path or an OAuth certification mechanism.

**Supervised gateways (launchd, systemd, containers, cron):** Claude Code's local login check reads the process identity — without `USER` in its environment a logged-in CLI reports `loggedIn: false`, instantly and with no upstream request. The plugin derives `USER`/`LOGNAME` (`USERNAME` on Windows) from the parent process or the running uid before every spawn, so a filtered gateway environment no longer surfaces as a false `ClaudeCodeLoggedOut`. When a logged-out error does fire, it now carries native's first stderr lines, which makes a gateway-only failure diagnosable instead of silent (#44).

Persistent configuration:

```yaml
model:
  provider: claude-subscription-directsdk-experimental
  default: sonnet
```

Auxiliary/fallback routing remains owned by Hermes. Configure those routes explicitly if they must also use the subscription; this provider does not silently change other selected providers.

### Proxies

The relay opens the upstream connection itself, so it honors a dedicated proxy via `CLAUDE_SUBSCRIPTION_DIRECTSDK_PROXY` (takes precedence over generic proxy variables without affecting the parent process), or native's proxy variables from the Hermes process environment: `https_proxy`/`HTTPS_PROXY`, then `http_proxy`/`HTTP_PROXY`, with `NO_PROXY` exceptions (standard-library matching; comma- or space-separated). The proxy must be an `http://` CONNECT proxy; `user:pass@` in its URL becomes a `Proxy-Authorization` header. TLS still terminates at `api.anthropic.com`, so the subscription bearer never reaches the proxy. A SOCKS or TLS-to-proxy URL is ignored with a warning and the relay connects directly, as it did before proxy support.

## Settings

**Claude login config directory** (`claude_config_dir`, string, empty by default). Select a separately logged-in Claude config directory for the active Hermes profile, including profiles served by one multiplexed gateway:

```sh
hermes config set plugins.entries.claude-subscription-directsdk-experimental.settings.claude_config_dir <dir>
```

Read on every native spawn, including auth-status and model-discovery probes. Precedence: nonempty per-profile setting after trimming whitespace → nonempty `CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR` → inherited `CLAUDE_CONFIG_DIR` → Claude CLI default. The selected value gets `~` expansion only; the plugin does not check whether the directory exists. An empty or whitespace-only setting falls through. A missing login produces the usual native authentication error. The process environment is never changed.

**Plan limits (`/usage`)** are asked of Claude Code itself (its `get_usage` control request), so they follow this per-profile setting, work with `auth.adopt_external_logins: false`, and the plugin never reads or sends the Claude login. With `claude_code_telemetry` off, Claude Code skips that fetch and `/usage` says so.

**Claude Code telemetry and feature flags** (`claude_code_telemetry`, on by default). Claude Code sends its usage telemetry and fetches feature flags; the flags are what unlock newer and older models in its model picker, so with them off the picker can shrink from every model your account runs to each family's current one (#86). Turn it off to keep Claude Code quiet: the plugin then starts it with `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`, `DISABLE_TELEMETRY` and `DISABLE_ERROR_REPORTING` set, and the pinned model table fills in the picker. Change it in the Desktop app under **Capabilities → Plugins**: the settings gear on the `claude-subscription-directsdk-experimental` row opens the switch. Or from the CLI:

```sh
hermes config set plugins.entries.claude-subscription-directsdk-experimental.settings.claude_code_telemetry false
```

It is read for the active profile on every spawn, so it applies to the next request without a restart. Whatever the setting, the plugin never lets the CLI auto-update itself, use its feedback upload path or send its own session-title request (`DISABLE_AUTOUPDATER`, `DISABLE_FEEDBACK_COMMAND`, `CLAUDE_CODE_DISABLE_TERMINAL_TITLE`), and an opt-out you export yourself (`DISABLE_TELEMETRY`, `DO_NOT_TRACK`, ...) is passed through untouched.

## Ownership and replay

Each `chat.completions.create` starts a fresh process in a private temporary directory. Native tools, skills and setting sources are disabled. MCP advertises only the current Hermes tool inventory, has inert callbacks, and is denied execution by native `dontAsk`. Full descriptions and schemas are supplied through tools plus validated generation fields in `CLAUDE_CODE_EXTRA_BODY`, applied from a private native settings file; the system prompt uses a private file too. This avoids the OS per-argument/environment-string limit. Authentication and identity fields are never replaced.

For per-call replay, native moves today's date line from its environment message into a trailing `role: system` message, and puts its context reminder ahead of the newest user turn; neither recurs on the next request. The relay returns that exact date line to the end of the environment message (other trailing text stays put) and drops plain-text reminders before the newest queried user frame after an assistant turn, as native's own tool-result steps already do. Later requests then replay the context each step's signed thinking was generated against, and keep the previous turn's cache prefix (#40, #36). A frame that cannot be found exactly once is left as native built it. Removed cache markers transfer without increasing their count.

Canonical history is replayed in order. Historical user frames use `shouldQuery:false`, each with a zero-turn acknowledgment; the final user/tool-result frame queries. There is no parked native session or native approval wait, and the adapter adds no synthetic continue prompt. The local admission relay prevents native recovery from issuing another upstream request. The native token-budget reminder is disabled because Hermes owns budgets and replay reconstructs that reminder across the cache boundary. Other native annotations remain present, so the wire prompt is not byte-identical Hermes-only context. On the conversation's first request native prepends its per-request context (the account-email reminder) to Hermes' first user turn but replays that turn without it afterwards; the relay drops that plain text before Hermes' frame, so the first turn is sent exactly as later requests replay it, is cached once instead of being re-written on call #2 (#77), and Claude never reads Claude Code's context as part of the user's message (#195). Native's auto memory and CLAUDE.md files (including a managed one) stay off (`CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`, `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1`): `--setting-sources ''` does not cover auto memory, and Hermes owns memory and instructions.

The relay restores the newest tool-result frame from Hermes' query when it can match the native frame unambiguously, so a native account-email reminder cannot masquerade as tool output. A frame it cannot match losslessly is forwarded exactly as native built it, and the mismatch is reported in the response usage (`native_admission.unrestored`). Restoration is limited to queried frames containing tool results; ordinary user turns and separate native system messages are untouched. The relay treats omitted `is_error` as `false`, restores Hermes' exact original result shape after matching equivalent plain-text encodings, and admits only unmarked plain-text additions after the matched frame. Images, metadata-bearing text, unexpected results, ambiguous cache-marker mappings, or changed tool identity/content leave the native frame untouched (fail open), so a CLI that reshapes its tool results costs an uncached tail, never a failed turn. Marker-bearing additions are validated before breakpoint relocation, preventing cache-marker laundering onto host content. When native marks more than one message block (Claude Code 2.1.287+ marks the last `tool_use` and a trailing per-request `role: system` message), marks after the last block the next request replays are folded into one breakpoint on that block; marks inside the replayed span, and every system/tools mark, stay as native placed them. Once a user message carries more than tool results (the next turn, or a `/steer` row), cache entries written after the finished turn's first thinking block stop matching, and Anthropic's lookup walks back at most 20 blocks from a breakpoint, so after a longer tool loop the next turn's first call read only tools and system and wrote the whole conversation again (#122). The relay therefore keeps one more message breakpoint on the last block of the user message right before the open turn's first assistant message with thinking (usually the turn's opening frame, or a `/steer` row), where an earlier request of the turn already wrote an entry, once that block is more than 20 blocks (counted without merging tool runs) before the recurring breakpoint; the next turn then re-writes only the finished loop from its first thinking block. A shorter loop, or one without thinking, keeps the recurring breakpoint alone. Native 2.1.285 places three breakpoints (two system, one message), so the extra one is the fourth; on a request already at Anthropic's limit of four (2.1.287+), a native mark between it and the recurring breakpoint moves there instead, and with neither it is skipped. Every marker in the request takes a slot, including a top-level one and markers nested in tool-result content. So the count grows by at most one and never past four, the extra mark takes the TTL of the next mark after it, and system/tools marks are never touched. Synthetic loopback tests establish wire-history continuity across requests and client recreation, not vendor cache hits or savings.

The relay binds an ephemeral loopback port with a random per-request route. Native authorization headers pass through memory directly to the upstream; headers are not logged or persisted. The upstream request body and native identity headers are preserved, while HTTP transfer encoding is normalized. The relay captures streamed text, signed thinking, tool arguments, usage and stop reason before native recovery can replace them. Cancellation shuts down the active upstream connection and the native process; request teardown removes the listener. No external relay service or bundled vendor executable is required.

### Long-context caching qualification

A real Sonnet 5 Hermes review task reproduced poor cache reuse: its first request had 187,049 input tokens; the next tool round read only 6,989 of 190,840 input tokens from cache (3.66%), rewriting 183,849 tokens into the one-hour cache. Identical-request testing at 179K tokens had passed, but did not exercise this replay boundary.

Disabling the native reminder restored stable prefixes without adding cache markers or changing cache TTL. A completed seven-call review used ten real Hermes tool executions, ran six offline tests, and wrote its review artifacts. Context grew from 187K to 212K; follow-up requests averaged 97.99% cache reads, or 85.45% including the cold start. Native list-price accounting totalled $1.1030034, not a verified subscription charge. These are Sonnet 5 results: Fable 5.1 required usage credits on the qualification account and was not exercised with paid credits. Model entitlement and allowance consumption remain native-account dependent.

Text streams incrementally. A complete tool batch is published only after assistant completion, `message_stop`, final usage and native exit. Hermes then applies its own hooks, approvals, tools and persistence. Tool names map through `mcp__hermes__`; original names must be unique ASCII alphanumeric/underscore/hyphen identifiers of at most 64 characters. Names longer than 50 characters are shown to native under a stable hash-suffixed 50-character alias that maps back to the original name.

`--max-turns 1` is a logical native step; the relay supplies the HTTP admission boundary. `error_max_turns` is accepted with a complete tool batch, usage and exit code 1. Native `num_turns` may be 2 at that boundary. A completed first response also survives a locally denied recovery attempt or native refusal rendering; Hermes receives the actual refusal, not the CLI's synthetic error text. Other failures remain failures.

A versioned `reasoning_details` envelope retains ordered native assistant messages and signed thinking. Unchanged projections preserve native blocks, including harmless surrounding-whitespace normalization. Transformed assistant text/tool projections replay canonical text and tool-use blocks instead of stale signed thinking; foreign provider reasoning carriers are ignored. Edited-assistant replay passed against the real service. Native autocompaction is disabled so Hermes retains compaction ownership; this does not establish parity for every history transformation or cross-model signed replay.

Mid-turn `/steer` uses Hermes' standard delivery: a standalone typed user row appended after the newest tool result. This provider adds no steering-specific transport. Natural change-of-plan steering passed live in the real loop; transport fidelity cannot guarantee model obedience.

## Lifecycle and request support

Outside an event loop, `create` is synchronous; inside an event loop, it returns an offloaded coroutine. Streams also support `async for`. One client should belong to one independently cancellable Hermes owner.

`cancel()` kills the owned process tree (POSIX process group / Windows `taskkill /T`) without closing another thread's active descriptors. `close()` prevents new calls and finalizes idle/unstarted streams; active consumers unwind after cancellation. Early stream exit requires `close()` / `aclose()`. Live interruption stopped generation and the observed native PID exited.

Supported translation includes text, base64/native images and documents, canonical tools/results, output-token limits, stop sequences, reasoning enable/disable and effort, and JSON-schema response-format projection. Unsupported native sampling fields are omitted rather than forwarding deprecated `temperature` from auxiliary callers. Reasoning effort is clamped to native-supported levels, including Hermes minimal/ultra inputs. Native thinking deltas surface as `reasoning_content`. An upstream refusal (`stop_reason: refusal`) finishes as `content_filter` with `stop_details`' explanation, or its category, as `message.refusal`, so Hermes treats it as a terminal refusal (one fallback try) rather than retrying an empty reply. A tool call the refusal cut off is left out of `tool_calls` (Hermes never runs it, and shows the refusal's reason instead of "no explanation"); the signed native turn in the replay carrier keeps it. Model/service restrictions still apply.

Pinned routes declare vision, so user images attach as native image parts and `vision_analyze` returns its image inside the tool result instead of calling `auxiliary.vision`. Only base64 images are sent; a remote `http(s)` image URL becomes a text hint to call `vision_analyze`. An explicit `auxiliary.vision`, `agent.image_input_mode: text` or `model.supports_vision: false` still routes through text.

Unknown parameters fail explicitly. Hermes Relay's `extra_headers.traceparent` is accepted as host-local tracing metadata and discarded before native request translation; it is never sent to Claude Code or the upstream. Empty/null header containers also work. Any other header or invalid container is rejected, so authorization and native identity remain CLI-owned. Unsupported surfaces include assistant prefill, strict function mode, forced tool choice, `parallel_tool_calls=False`, `n>1`, JSON-object-only mode, arbitrary headers/body fields, remote image downloads, and cross-model signed-history parity. The read-idle timeout defaults to 180 seconds, resets on native output, and accepts Hermes' finite HTTPX read-timeout shape. Large prompts remain subject to native/OS limits.

Hermes chooses retry or fallback from an error's `status_code`, so failures carry one. An upstream rejection carries the first attempt's real HTTP status. An error native answers itself carries the status for native's own error code: `rate_limit` 429 (a plan or session limit), `billing_error` 402, `authentication_failed` and a missing login 401, `overloaded` 529, `server_error` 503; a missing Claude Code CLI is 503. Local request validation stays `ValueError`; malformed native output and other process failures carry no status. Hermes' non-streaming stale-call watchdog stops a hung call through `cancel()`; the provider profile classifies that kill as a timeout rather than an unknown error.

In the agent loop Hermes passes its own request timeout as that read-idle limit (`providers.<provider>.request_timeout_seconds`, 1800 seconds by default as of Hermes 0.21.5). Hermes treats `process://` as a local endpoint, so its non-streaming stale-call watchdog is off for a model id without a Hermes reasoning stale floor (as of 0.21.5, for example `claude-sonnet-5[1m]` and the Haiku ids) unless a stale timeout is configured. A native call that hangs without output then holds the turn for the whole read-idle limit before Hermes retries or falls back. To bound it, configure the stale timeout; on this non-streaming path it limits the whole call, not a silence, so choose a value above your longest healthy turn:

```yaml
providers:
  claude-subscription-directsdk-experimental:
    stale_timeout_seconds: 900
```

## Setup: `hermes model` → Claude Subscription DirectSDK (Experimental)

Selecting the provider asks the Claude CLI itself, never Anthropic, before anything is saved:

1. **Installed?** `claude` must resolve on PATH, in a usual install directory, or through `CLAUDE_SUBSCRIPTION_DIRECTSDK_COMMAND`. Otherwise one line: install with `npm install -g @anthropic-ai/claude-code`, and the flow stops without touching config.
2. **Logged in?** `claude auth status` (local credential store, ~0.3s). Logged in shows `credentials: ✓ (Claude Pro)`. Logged out on a terminal starts `claude auth login` inline; it opens the browser and takes the pasted code, then the flow re-checks and continues. Without a TTY it prints the instruction and stops.
3. **Which models?** The CLI's `initialize` handshake returns the account's own picker (verified through the admission relay: zero upstream requests). Every row is listed; the pinned catalog below only adds metadata to the models it knows. Pinned rows are mapped to Hermes route ids and deduplicated (`opus` and `opus[1m]` are one 1M route). A model the catalog does not pin yet keeps the id and label the CLI announced and carries a dim `· unpinned` note; it gets `[1m]` only when the CLI itself offers that form, and a plain + `[1m]` pair collapses onto `[1m]` as pinned models do. Rows the CLI marks "Draws from usage credits", plus Fable on non-Max plans per Anthropic's plan rule, carry a dim `· usage credits` note (`· usage credits · unpinned` on an unpinned row); nothing is hidden. If the handshake fails the pinned catalog below is used.

The same `discover_models()` feeds `provider_model_ids()`, so the TUI/Desktop pickers and `/model` list the account's picker too.

## Subscription usage

Claude Code names its caller on every request, and subscriptions meter the 5-hour window by that name: `claude -p` defaults to `sdk-cli`, which draws the window about 2x faster per token than the interactive CLI or a client's own name ([anthropics/claude-code#97074](https://github.com/anthropics/claude-code/issues/97074)). The plugin runs native as `hermes-agent`, overwriting any name inherited from a host (`sdk-py`, `claude-desktop`). Measured Oct 31 2026 on a Max plan with Opus 5.5 and Claude Code 2.1.316, four arms of four fresh ~43K-token requests each ($1.09 list-equivalent per arm, output pinned), 5-hour utilization read after each arm:

| arm | name on requests | 5-hour Δ (two arms) | total for $2.19 |
|---|---|---|---|
| plugin before this change | `sdk-cli` | +6% / +6% | 12% |
| plugin now | `hermes-agent` | +4% / +3% | 7% |

The same work used about 1.7x less of the window under the plugin's own name. Anthropic has not documented the weighting, so it can change.

The readings below predate the change and describe the `sdk-cli` rate.

Measured Sept 9 2026 on a Pro plan (Sonnet 5 1M, ~500K fresh input tokens per arm, output pinned to `Hello!`, session meter read on claude.ai before/after each arm):

| arm | list-$ pushed | session meter Δ | points per $ |
|---|---|---|---|
| `claude -p` (two windows) | 2.12 / 1.94 | +19 / +17 | 8.9 / 8.8 |
| this provider | 2.36 | +21 | 8.9 |
| Claude Code interactive TUI (two runs) | 2.22 / 2.25 | +12 / +11 | 5.4 / 4.9 |

This provider draws subscription usage at exactly the `claude -p` / Agent SDK rate; there is no Hermes-specific penalty. Interactive Claude Code is metered at roughly 0.58x that rate, so plan for about 60% of the TUI's throughput per 5-hour window (≈ $6.5 vs ≈ $11 list-equivalent of Sonnet 5 on Pro). The wire-level usage buckets are identical across arms, so the weighting is server-side and applies to every `-p`/SDK harness. In that Sonnet 5 comparison Hermes sent ~0.6x the tokens native Claude Code did on identical coding tasks. If your drain looks higher, compare Hermes `/usage` with Claude Code `/cost` on the same task.

Re-checked Oct 14–15 2026 on a Max plan with Opus 5.5 and Claude Code 2.1.299. The API reports 5-hour utilization in 0.01 steps, so the list dollars between consecutive steps were measured inside stretches of one kind of traffic: this provider $2.70 per step (4 intervals) and `claude -p` $2.87 and $2.44 (1 interval each), the same rate within noise. A block of interactive TUI conversations took about $6.65 per step, which suggests the TUI drew usage roughly 2.3x more slowly per list dollar than `claude -p` on this plan. That figure rests on about one meter step of TUI traffic, so treat it as an early reading.

## Model metadata and accounting

The picker exposes these explicit native routes:

| Model | Native selection | Context |
| --- | --- | --- |
| Sonnet 5.5 | `claude-sonnet-5-5[1m]` | 1,000,000 |
| Sonnet 5 | `claude-sonnet-5[1m]` | 1,000,000 |
| Haiku 5.5 | `claude-haiku-5-5[1m]` | 1,000,000 |
| Haiku 4.5 | `claude-haiku-4-5-20251001` | 200,000 |
| Opus 5.5 | `claude-opus-5-5[1m]` | 1,000,000 |
| Opus 5 | `claude-opus-5[1m]` | 1,000,000 |
| Opus 4.8 | `claude-opus-4-8[1m]` | 1,000,000 |
| Fable 5.1 | `claude-fable-5-1[1m]` | 1,000,000 |

Short names `sonnet`, `haiku`, `opus` and `fable` resolve to the corresponding pinned routes above. Known 1M model IDs also receive the native `[1m]` suffix automatically; Haiku 4.5 does not. Unknown model IDs pass through unchanged and are never promised 1M: a plain one reports the 200K window native Claude Code applies to an unverifiable id behind the relay, so Hermes' own family-name guess (which would size `claude-opus-5-5` at 1M before it was pinned) cannot budget past it; an unknown `[1m]` id reports nothing, and no Hermes estimate for it exceeds the native 1M. An explicit Hermes `model.context_length` still overrides the host's window, including a smaller compaction budget.

The local relay sets `ANTHROPIC_BASE_URL`, which makes Claude Code apply its gateway defaults. Its documented Sonnet 5 gateway default is 200K unless `[1m]` is selected; this was the cause of the earlier downgrade, not evidence of a general subscription limit. Both native argv and Hermes metadata now select the same window. See [Claude Code model configuration](https://code.claude.com/docs/en/model-config#sonnet-5-context-window).

Native initialization can enumerate the current picker without a Messages request, but its returned list did not include Opus 4.8. The plugin therefore keeps the version-pinned entries as the fallback catalog, and when the live picker answers it appends every pinned route the picker did not name after the advertised rows, so older pinned models (Opus 5, Opus 4.8) stay selectable instead of disappearing once the CLI moves the family alias. A model the live picker offers before this table pins it is listed, `unpinned`, the day the CLI ships it; pinning adds its 1M route and qualified window, it does not decide visibility. It does not spawn a model or query an HTTP `/models` endpoint when opening Hermes' picker. Existing installations may need **Refresh models** to discard the previous cached list; the live picker above is extended with this static list whenever the CLI is logged in.

The follow-up probes used native **2.1.258**. The Sonnet 5 1M route accepted **902,783 actual input tokens** and returned the requested response, with native `contextWindow: 1000000` and exactly one upstream request. Native list-price equivalent was **$3.611238**. A subsequent short `sonnet` request verified automatic routing to `claude-sonnet-5[1m]`; a real `haiku` request resolved to Haiku 4.5 with a 200K window. Opus 5 and Opus 4.8 were then smoke-tested through the integrated relay: each resolved to its `[1m]` route, reported a 1,000,000-token native window, returned the requested reply in exactly one upstream request, and cost $0.001485 list-equivalent each. Fable 5.1 was rejected by the native client on the qualification account: `Fable 5.1 requires usage credits`. That is Anthropic's documented plan rule, not a routing defect: on Pro (and Team standard seats) Fable 5 / 5.1 bill to pay-as-you-go usage credits from the first request, while Max plans include them up to 50% of weekly limits ([Claude Fable models on your plan](https://support.claude.com/en/articles/15424964-claude-fable-models-on-your-plan)). The entry stays in the picker because every paid plan can reach it; the provider surfaces the native error verbatim and never falls back to another model. Native discovery also labels Opus 1M as drawing usage credits on Pro. Plan-aware picker badges from the native `initialize` handshake (which reports `subscriptionType` without an upstream request) are a follow-up.

### Hermes compaction qualification

A real multi-turn run with a **200K Hermes window** and `compression.threshold: 0.75` automatically compacted after a turn with 154,038 input tokens, at an approximately 168K next-request preflight estimate. The real summarizer completed, SQLite archived fifteen original rows and retained a summary, and the next ordinary request succeeded at 144,568 input tokens. Two additional model calls executed a real `read_file` and returned both the remembered project codename and the exact file value; that tool result was verified in SQLite. This final-routing run used eleven upstream requests total and **$1.221155** native list-price equivalent. Native autocompaction remained disabled: the summary and durable commit were owned by Hermes. The earlier native-200K control also passed; no compressor implementation change was needed.

Token usage retains native uncached/cache-read/cache-write/output components. Completed responses also retain native `total_cost_usd` and `modelUsage`. When every reported model has `costBasis: list` and the total is finite and nonnegative, Hermes records that exact native amount as **estimated API list-price equivalent**, not an actual subscription invoice or extra-usage charge. It is never marked free/included or replaced with guessed alias prices. Missing or invalid final accounting remains unknown; interrupted requests must not be interpreted as free or zero-token service work. Hermes' iteration and runtime budgets remain host-owned; this provider does not add an account-level overage cap.

## Verification

```sh
# HERMES_AGENT_REPO defaults to ~/.hermes/hermes-agent (a hermes-agent checkout supplies `providers`/`agent`)
~/.hermes/hermes-agent/.venv/bin/python3 -m pytest tests/
python evals/directsdk_admission.py /path/to/claude
python evals/directsdk_cache_wire.py /path/to/claude
```

CI runs both evals on Linux and macOS through `evals/ci.sh`, against a pinned Claude Code installed from npm (`CLAUDE_CODE_VERSION`, default 2.1.292). They need no credentials and make no inference calls. Bump the pin to qualify a new Claude Code release: a change in native's wire shape fails here before it reaches users.

Transport invariant tests cover signed replay and harmless normalization, transformed projections, final tool batches/usage, async use, lazy failure, invalid parameters, conflicting auth, and active/paused/unstarted stream cleanup. The admission regression fails on the previous implementation (two upstream requests) and passes with one request, preserving first-response usage including zero values. Its cancellation control verifies upstream socket closure. A real-native ten-case loopback qualification covers normal text, tools, output/context limits, thinking-only recovery, refusal, HTTP errors, disconnects and cancellation, with one upstream request per call. Its responses are synthetic protocol fixtures, not paid-model evidence.

Installed-plugin discovery (`$HERMES_HOME/plugins/<name>/`) and every missing-`claude` path are covered against a temporary `HERMES_HOME`, including constructing the bundled client without spawning native or accessing auth. A fresh subscription-backed AIAgent loop completed two API calls with host `read_file` execution and SQLite persistence; separate service requests accepted edited-assistant replay. Native loopback qualification also accepted 182K of tool schemas plus a 176K system prompt through file-backed settings. Loopback responses remain fixtures, not paid-model evidence.

The subscription-backed task, CLI delegation, streaming, resume, denial, steering and interruption receipts are separate private artifacts. No auth data or trajectories are committed to this repository. Remaining qualification includes broader history transformations, cross-model signed replay, native versions/platforms, and adversarial steering reliability. Subscription invoice/overage reconciliation is not available from native list-price accounting.

## Attribution

Extracted from hermes-agent PR #105863 (`feat: add Claude Subscription DirectSDK (Experimental) provider`) by @teknium1 and @unsupportedpastels. The provider-agnostic parts of that PR (external-process provider profiles, provider-native replay carriers, setup-flow gating, picker plumbing) land in Hermes core via hermes-agent PR #117451; the provider itself lives here as a standalone plugin because Hermes does not bundle vendor-specific subscription transports in-tree. The `claude` presence checks were added during extraction.

MIT licensed; see `LICENSE`.
