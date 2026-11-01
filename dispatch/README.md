<p align="center"><img src="../assets/dispatch.jpg" alt="Dispatch: official Hermes Agent plugin" width="100%"></p>

# Dispatch for Hermes Agent

> Part of the [Hermes Official Plugins](../README.md), Nous Research's official plugins for Hermes Agent.

Bring your own sandbox. A terminal backend for [Hermes Agent](https://github.com/NousResearch/hermes-agent)
whose sandbox comes from a program **you** write. Hermes runs that program to spawn, exec in, pause and
delete one sandbox per session. Docker, Kubernetes, Apple containers, E2B, Modal, Firecracker, a pool of VMs:
if you can script "start it, run this in it, stop it", it is a Hermes backend, with no Hermes code.

- **One contract, any platform.** Four operations, passed as arguments. The two examples in
  [`examples/`](examples/) are a complete Docker backend in 30 lines and a Kubernetes one in 45.
- **Pause, don't destroy.** When a session goes idle, Hermes pauses its sandbox. The next command resumes the
  same one, files intact. Throwaway sessions (`terminal.container_persistent: false`) and explicit teardowns
  delete it instead.
- **Your skills come along.** Skills, registered credential files and the cache sync into `~/.hermes` inside
  the sandbox through the exec job (one `tar` stream), the same as the SSH backend.
- **Screen and browser follow.** Bot Desktop, `computer_use` and the browser run inside the sandbox too, given an
  image with the desktop stack.

## Install

```bash
hermes plugins install dispatch
hermes plugins enable dispatch
```

## Use it

Write a dispatch program (or copy one from [`examples/`](examples/)), make it executable, and point Hermes at it:

```bash
cp ~/.hermes/plugins/dispatch/examples/docker.sh ~/.hermes/dispatch.sh
hermes config set plugins.entries.dispatch.settings.program ~/.hermes/dispatch.sh
hermes config set terminal.backend dispatch
hermes doctor   # shows "Dispatch program (...)" with a check mark when it can start
```

| Setting (`plugins.entries.dispatch.settings.*`) | Default | What it does |
|---|---|---|
| `program` | | Path or command line of your program. |
| `image` | Hermes' sandbox image | Passed as `HERMES_SANDBOX_IMAGE`; what it means is up to your program. |
| `disposable` | `false` | Skip dangerous-command approval prompts, as Docker does. Turn it on only when your program never runs commands on this machine. |
| `sync_files` | `true` | Sync skills, credential files and the cache into the sandbox (needs `tar` in it). |
| `spawn_timeout` | `300` | Seconds a `spawn` may take (image pulls, cold starts). |

`terminal.container_cpu`, `container_memory`, `container_disk` and `container_persistent` apply as for Docker.

## The dispatch contract

Hermes runs your program as `<program> <operation> <sandbox-id> [argv...]`:

| Operation | When | Your program must |
|---|---|---|
| `spawn` | A session runs its first command, or its next one after a pause | Create the sandbox, or start it again when one with that id exists. Idempotent. |
| `exec` | Every command, file sync and probe | Run `argv` (e.g. `bash -c '<script>'`) inside the sandbox with stdin, stdout and stderr connected, and exit with its status. Pass argv through intact (`docker exec`, `kubectl exec --` and `container exec` do; plain `ssh` does not). |
| `pause` | The session went idle (`TERMINAL_LIFETIME_SECONDS`, default 300) or closed | Stop it but keep its disk. |
| `delete` | A throwaway session ends, or an explicit teardown | Remove it for good. |

A non-zero exit from `spawn`, `pause` or `delete` is reported with your program's stderr. Every job also gets:

| Variable | Value |
|---|---|
| `HERMES_SANDBOX_OPERATION`, `HERMES_SANDBOX_ID` | Same as the arguments. |
| `HERMES_SANDBOX_IMAGE` | The `image` setting, a per-task image from batch runs, or Hermes' default sandbox image. |
| `HERMES_SANDBOX_PERSISTENT` | `1` when a pause should keep the sandbox for a later resume. |
| `HERMES_SANDBOX_CPU`, `HERMES_SANDBOX_MEMORY_MB`, `HERMES_SANDBOX_DISK_MB` | From `terminal.container_*`; `0` means no limit. |
| `HERMES_SANDBOX_ENV` (exec only) | Space-separated names of skill and `terminal.env_passthrough` variables. Their values are in your program's environment; forward each by name (`docker exec -e NAME`) so secrets never appear on a command line. |

A persistent session keeps the same id for its whole life, derived from the profile and session, so pausing and
resuming lands in the same sandbox. Garbage-collecting sandboxes of sessions you deleted is up to your platform
(the examples label theirs `hermes.dispatch=1`).

## Safety

The plugin cannot know what your program does with a command: it might run it in a fresh VM, or on this
machine. So Hermes keeps its dangerous-command approval prompts on, as for the SSH backend, until you set
`disposable: true`.

## Development

```bash
cd dispatch
HERMES_AGENT_REPO=../../hermes-agent PYTHONPATH=../../hermes-agent python -m pytest -q
```

The environment tests run a real fake dispatch program (a Python script that keeps each "sandbox" as a
directory) and so run on Linux and macOS only.
