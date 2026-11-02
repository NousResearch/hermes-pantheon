# Advanced Delegation for Hermes Agent

> Part of the [Hermes Official Plugins](../README.md), Nous Research's official plugins for Hermes Agent.

Official [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that adds two
**delegation modes** to `delegate_task`, next to the built-in default:

| Mode | `delegation.backend` | What the agent gets |
|---|---|---|
| **Default** (built into Hermes) | `default` | Every subagent runs on `delegation.model` / `delegation.provider`, else the parent's model. |
| **Model routes** | `model_map` | A per-task `option` choice: a named model route from a list you define (e.g. `fast`, `deep`, `local`). |
| **Profile routes** | `profile_routes` | A per-task `profile` choice: the subagent runs as one of your Hermes profiles, with that profile's own model, credentials, skills and `SOUL.md`. |

One mode is active at a time: you pick Default, Model routes or Profile routes, never two at once.
Model routes and Profile routes stay separate backends with their own config blocks, so switching
modes keeps the other mode's settings for when you switch back.

Only the names and the one-line descriptions you write reach the model. The raw provider, model
id, endpoint and key never become tool parameters, so the agent cannot invent a route.

> [!IMPORTANT]
> Needs Hermes 0.22.5 or newer, the first release with pluggable delegation providers
> (`ctx.register_delegation_provider`). The plugin's manifest declares `requires_hermes: ">=0.22.5"`,
> so Hermes 0.22.0 to 0.22.4 refuses to install or enable it (`requires hermes >=0.22.5, running
> <version>`) and skips a copy that is already installed. Cores older than 0.22.0 ignore that field;
> there the plugin loads but does nothing (it logs one warning).

## How it works

The plugin registers two *delegation providers*. Hermes core keeps the whole subagent lifecycle
(spawning, isolation, depth and concurrency limits, background runs, results); a provider only
answers "which route does this task take".

- **Schema.** `delegate_task`'s task items gain one property, `option` (model routes) or `profile`
  (profile routes): an `enum` of your names in config order, whose description lists
  `name: description` for each. Hermes refuses any other value before a subagent starts. With
  nothing configured there is no parameter at all.
- **A task that picks a route** runs on it. A model route resolves exactly like the
  `delegation.*` keys: `provider` uses the credentials you already configured for that provider;
  `base_url` + `model` targets an OpenAI-compatible endpoint. A profile route runs the subagent as
  that profile, exactly as if you had started `hermes -p <profile>` for that task.
- **A task that picks none** runs on the mode's `default` when you set one, otherwise on the
  parent's own model. In these two modes `delegation.model` / `delegation.provider` are not used;
  switching back to Default makes them apply again.
- **Each result reports its route.** A routed task's result carries `route`: the route name
  (`label`) plus the provider, model, reasoning effort and speed tier the subagent actually ran on.

The schema is built when a session starts, so route changes apply to new sessions (the prompt
cache of a running conversation is never disturbed).

## Install

From the [Hermes plugin catalog](https://hermes-agent.nousresearch.com/docs/plugins) (pinned to a
reviewed commit; needs Hermes 0.22.5 or newer):

```bash
hermes plugins install advanced-delegation
```

In the Desktop app: **Settings → Model → Subagents → Install plugin** (the button shows under the
mode picker while the plugin is missing).

To follow this repo's `main` instead of the catalog pin:
`hermes plugins install NousResearch/hermes-official-plugins#advanced-delegation --enable`.

Plugins are installed per profile; install and configure it in the profile that delegates. Target
profiles of profile routes need nothing installed: they only have to exist (`hermes profile list`)
with a working model.

## Set it up: `hermes delegation config`

```bash
hermes delegation config          # pick the mode, then edit its routes
hermes delegation config --show   # print the current mode and routes (no terminal needed)
```

1. **Pick the mode**: Default, Model routes or Profile routes (the cursor opens on the current one).
2. **Model routes** opens a route editor built from `hermes model`'s own pickers. The list shows each
   route's name, description and `provider · model · reasoning · tier`; pick a route to edit it,
   delete it or make it the default, or **+ Add a route**: a name (what the agent picks), a one-line
   description (what the agent reads), a provider from the ones you have credentials for (or a
   custom endpoint), a model from `hermes model`'s model picker, an optional reasoning effort, and a
   speed tier when the route offers one. A route is validated before it is saved: the provider must
   be known, the effort a real level, and the tier one the route offers. **Done** switches the mode;
   with no default set it first asks for one, with *No default — unrouted tasks use the main agent's
   model* as an explicit choice.
3. **Profile routes** is just a checklist of your other profiles, an optional line about each newly
   checked one (listed profiles keep theirs), and a default. Profiles keep their own models, so there
   is nothing else to pick. Unchecking every profile clears the list after a confirmation (and an
   active Profile routes mode goes back to Default).
4. **Default** switches back; `delegation.model` / `delegation.provider` apply again.

Everything is written to the profile's `config.yaml` with Hermes's own config writer (comments and
other settings are kept). Changes apply to new sessions.

## Switch modes in a chat: `/delegation`

```
/delegation                          # current mode, what each mode routes to, a picker to switch
/delegation model_map                # save it; it takes effect from the next session (/new)
/delegation profiles --now           # save it AND apply it to this chat now
/delegation default                  # back to the built-in behaviour (from the next session)
```

Switching **saves** the mode as this profile's `delegation.backend`, and it takes effect from the
next session (`/new`). The running chat's tool list keeps the choices of the mode it started with
(the per-task `option` / `profile`): the tool list is part of the prompt cache, and changing it
mid-chat makes the next request re-read the whole conversation at full price. Until `/new`, a task in
that chat that still picks a route or profile of the previous mode is refused with a message naming
`--now` and `/new`, never run on the wrong route.

`--now` is the opt-in for when you want it in this chat anyway: Hermes saves the mode and rebuilds
this chat's tools at your next message, so the agent's choices follow the new mode right away (one
prompt-cache break). It needs a Hermes that can rebuild a running chat's tools; on an older one the
mode is saved for the next session and the reply says so.

Bare `/delegation` opens a picker with the modes that have something set up, using the surface's own
choice prompt: buttons on Telegram, Discord and Slack (like approvals), a numbered list on other
chats, the clarify picker in the CLI, TUI and Desktop. A second question asks when: **Next session
(recommended)** or **Now (rebuilds this chat's tools)**. Where nothing can ask, it answers with a
numbered text listing and both commands instead. Mode words: `default`, `model_map` (or `models`,
`2`), `profile_routes` (or `profiles`, `3`). A mode with nothing set up is refused with a pointer to
`hermes delegation config`.

When the configured delegation backend cannot run (for example the plugin was disabled), Hermes
refuses the subagent with an error naming `/delegation`: the listing flags the broken backend, and
switching to another mode is the fix.

## Configure by hand

Each mode's settings live under `delegation.backends.<mode>`; `delegation.backend` picks the mode.

### Model routes (`model_map`)

```yaml
delegation:
  backend: model_map
  backends:
    model_map:
      options:
        - name: fast                     # what the agent picks
          description: "Cheap and quick: lookups, summaries, simple edits."
          provider: openrouter           # provider and/or base_url, plus model
          model: google/gemini-3-flash-preview
        - name: deep
          description: "Slow and careful: hard debugging and design work."
          provider: anthropic
          model: claude-opus-5
          reasoning_effort: high
          service_tier: fast             # optional speed tier: normal, fast or ultrafast
          max_iterations: 60             # optional turn limit for this route's subagents
          child_timeout_seconds: 300     # optional idle timeout (seconds) for this route's subagents
        - name: local
          description: "Private: runs on this machine."
          base_url: http://localhost:8080/v1
          model: qwen3-coder
      default: fast                      # optional: route for tasks that pick none
```

Route keys: `name` and `description` (required), `model` (required), `provider` and/or `base_url`
(at least one), and optionally `reasoning_effort`, `service_tier`, `fallback_providers`, `api_key`,
`api_mode`, `max_iterations` and `child_timeout_seconds`, each meaning the same as the matching `delegation.*` key. Prefer `provider:` over `api_key:`: provider
credentials stay in `.env` / `auth.json`, while an `api_key` here is stored in `config.yaml` in plain
text. Malformed routes and repeated names are skipped with a warning in `hermes logs`.

`service_tier` is checked like `/fast` against the route (and, for `ultrafast`, your Codex
account): a tier the route does not offer refuses the spawn with a tool error instead of silently
running at another tier. An unknown `reasoning_effort` is refused the same way.

`max_iterations` (a whole number of 1 or more) and `child_timeout_seconds` (a number of 0 or more;
`0` disables the idle timeout, values under 30 are raised to 30) give this route's subagents their own
budget, overriding `delegation.max_iterations` / `delegation.child_timeout_seconds` — a cheap "scout"
route can get 5 turns while a coding route keeps 60. A bad value refuses that task alone; the other
tasks in the batch still run. Needs a Hermes core that reads the two keys (`ROUTE_KEYS` in
`agent/delegation_provider.py`); older cores ignore them.

### Profile routes (`profile_routes`)

```yaml
delegation:
  backend: profile_routes
  backends:
    profile_routes:
      profiles:                 # allowlist AND what the agent reads about each profile
        - name: coder
          description: Writes and refactors code; runs tests.
        - name: researcher
          description: Web research and long-form summaries.
      default: coder            # optional: profile for tasks that pick none
```

The list is also the allowlist: profiles you do not list can never be picked. A listed name with no
matching profile on disk is refused with a pointer to `hermes profile list`.

> [!WARNING]
> Listing a profile lets this profile's subagents use that profile's credentials, credential pool,
> `SOUL.md` and terminal policy. Only list profiles whose keys and terminal access you are happy for
> this profile's agent to use.

### From the command line

```bash
hermes config set delegation.backend model_map            # or profile_routes, or default
hermes config set delegation.backends.model_map.options '[{name: fast, description: "Cheap and quick.", provider: openrouter, model: google/gemini-3-flash-preview}]'
hermes config set delegation.backends.model_map.default fast
hermes config set delegation.backends.profile_routes.profiles '[{name: coder, description: "Writes and refactors code."}]'
```

## Development

```bash
cd advanced-delegation
HERMES_AGENT_REPO=~/src/hermes-agent PYTHONPATH=~/src/hermes-agent python -m pytest -q
```

Tests that need the core seam skip themselves on a core without `agent.delegation_provider`; the
old-core test always runs.
