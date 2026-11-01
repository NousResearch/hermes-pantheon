# Bubblewrap terminal backend

![Bubblewrap](../assets/bubblewrap.jpg)

Part of [Hermes Official Plugins](https://github.com/NousResearch/hermes-official-plugins): built and supported by Nous Research.

The `bubblewrap` backend runs every `terminal` and `execute_code` command inside its own
[bubblewrap](https://github.com/containers/bubblewrap) (`bwrap`) sandbox on the machine Hermes runs on.
It is not a container: the sandbox sees the host filesystem read-only at the host paths, can write to
the working directory, and cannot read your credentials. It needs no image, no daemon and no network
round trip, so it fits a personal machine or a small server where the `local` backend is too open and
Docker is too heavy.

**Linux only.** Needs bubblewrap 0.9.0 or later, `prlimit` from util-linux (installed on every
mainstream distribution) and unprivileged user namespaces (or a setuid `bwrap`).

## Install

```sh
sudo apt install bubblewrap     # Debian, Ubuntu
sudo dnf install bubblewrap     # Fedora
sudo pacman -S bubblewrap       # Arch

hermes plugins install bubblewrap
hermes config set terminal.backend bubblewrap
```

`hermes plugins install NousResearch/hermes-official-plugins#bubblewrap` installs straight from this
repo's `main`. `hermes setup` lists the backend once the plugin is installed and asks for the profile.
`hermes doctor` reports whether `bwrap` is found, its version, and whether the sandbox probe passes. If
the probe fails, unprivileged user namespaces are disabled for your user: check your distribution's
notes on enabling them for bubblewrap (on Ubuntu 24.04 and later this is the AppArmor
`kernel.apparmor_restrict_unprivileged_userns` restriction).

When `bwrap` is missing or the probe fails, the terminal tool returns a degraded result that names the
package (or an error under `terminal.degraded_mode: fail`). Commands are never run outside the sandbox.

## Settings

All settings live under `plugins.entries.bubblewrap.settings` (Desktop: Settings ▸ Plugins ▸
Bubblewrap). From the command line:

```sh
hermes config set plugins.entries.bubblewrap.settings.profile workspace    # restricted | workspace | network
hermes config set plugins.entries.bubblewrap.settings.memory_mb 2048       # 0 disables
hermes config set plugins.entries.bubblewrap.settings.cpu_seconds 600      # 0 disables
hermes config set plugins.entries.bubblewrap.settings.max_procs 256        # 0 disables
hermes config set plugins.entries.bubblewrap.settings.binds '[{"src": "/data/models"}, {"src": "/srv/scratch", "readonly": false}]'
hermes config set plugins.entries.bubblewrap.settings.home_allow '[".config/nvim"]'
hermes config set plugins.entries.bubblewrap.settings.hide '["~/Documents/keys"]'
```

Settings are read each time a sandbox environment is created, per profile. `terminal.cwd`,
`terminal.home_mode`, `terminal.timeout` and `terminal.sandbox_dir` are the usual core terminal keys.

## What bwrap isolates

Each command gets:

- **The host root, read-only**, at the same paths as on the host. `/usr/bin`, `/etc`, your project
  checkouts and your installed toolchains are all there; nothing outside the writable set can be
  changed.
- **A private pid namespace and `/proc`**: the command cannot see or signal host processes. Every other
  namespace is unshared too (IPC, UTS, cgroup, user, and network unless the profile shares it).
- **A fresh `/dev` and a fresh `/tmp` per command.** Nothing written to `/tmp` survives the command; use
  the working directory, or `$TMPDIR` (the Hermes scratch directory, which inside the sandbox is the
  sandbox's own and persists between its commands).
- **No agent or bus sockets**: `/run/user/<uid>` is an empty tmpfs, so the gpg-agent, ssh-agent,
  keyring and D-Bus sockets there are unreachable, and `SSH_AUTH_SOCK`, `GPG_AGENT_INFO` and
  `DBUS_SESSION_BUS_ADDRESS` are removed from the environment (again after your shell rc files run).
  The docker socket, if present, is replaced by an empty file.
- **Your home directory with its dot entries hidden** unless they are on an allowlist, and credential
  stores hidden always (below).
- **Resource limits** per command (memory, CPU time, processes), applied by `prlimit`: memory and CPU
  before `bwrap` starts, the process limit inside the sandbox. Nothing runs in between.
- **`--die-with-parent` and `--new-session`**: a sandbox dies with Hermes and cannot inject input into
  the controlling terminal.

`cd` and exported variables carry over between commands exactly as with the `local` backend; the
environment is the one the `local` backend builds (`env_passthrough` applies, provider API keys stay
out, `HOME` follows `terminal.home_mode`).

### Your home directory

Credentials live in dot entries of the home directory, and no list of them is complete: every tool
that stores a token picks its own name. So the sandbox hides every dot entry of your home directory
and shows only what is on an allowlist.

- **Non-dot entries** (`~/projects`, `~/Documents`, `~/bin`) are visible, read-only unless the working
  directory or a read-write bind covers them.
- **Dot entries** (`~/.pgpass`, `~/.mozilla`, `~/.zz-some-tool`) do not exist inside the sandbox unless
  they are allowed. An allowed dot entry is read-only always, so a command cannot edit `~/.bashrc` or
  `~/.gitconfig`, which would run code in your own shells later.
- **`~/.config`, `~/.local` and `~/.local/share`** follow the same rule one level down: only an allowed
  child is visible.
- **Nothing new can be made** at the top of the home directory or of those three directories.

The allowlist has three sources: a shipped list (the shell startup files, `.gitconfig`, `.cache`, the
common toolchain directories such as `.cargo`, `.rustup`, `.nvm` and `.pyenv`, and the common children
of `.config`, `.local` and `.local/share`), every directory on `PATH` that lies under the home
directory, and the `home_allow` setting for anything else:

```yaml
plugins:
  entries:
    bubblewrap:
      settings:
        home_allow:
          - .zz-tool       # a dot entry at the top of the home directory
          - .config/nvim   # or one child of .config, .local or .local/share
```

Credential stores stay hidden whatever the allowlist says: `.ssh`, `.aws`, `.gnupg`, `.kube`,
`.docker`, `.netrc`, `.npmrc`, `.pypirc`, `.pgpass`, `.git-credentials`, `.config/gh`,
`.config/gcloud`, the browser profiles, the desktop keyrings and the other paths that Hermes' own
file tools refuse to write. A `home_allow` entry that names one is ignored with a warning. Below an
allowed entry the same holds: `~/.cargo` is visible, `~/.cargo/credentials.toml` shows as an empty
file, and so do `~/.gem/credentials`, `~/.m2/settings.xml` and `~/.config/npm/npmrc`. The `hide`
setting hides more paths, absolute or relative to the home directory (a non-dot directory that holds
keys, say). A `hide` entry that names the home directory or a parent of it is ignored with a warning.

An allowed dot entry that is a symlink stays a symlink inside the sandbox, so it resolves only when
its target is visible. A dotfiles repository in a dot directory (`~/.gitconfig` linked to
`~/.dotfiles/git/gitconfig`) and a CA bundle kept in one (`SSL_CERT_FILE=~/.certs/corp.pem`) need that
directory in `home_allow` (`[".dotfiles"]`, `[".certs"]`): it then shows read-only, with the credential
stores below it still hidden. A repository in a non-dot directory (`~/dotfiles`) needs nothing.

A dot entry that is a symlink is also checked when the backend starts. When the link, any directory
on the way or its final target lies in the working directory, in a read-write bind or in the profile
home, a command could rewrite a file your next login shell reads, so the backend refuses to start and
names the entry. Use a project directory as the working directory, or make the bind read-only.

The allowlist and the hidden set are fixed when the backend starts, so a command cannot widen them by
changing `PATH`. The listing of the home directory is read at every command, so a directory you create
on the host shows up in the next command.

### The Hermes home

The Hermes home (`HERMES_HOME`, normally `.hermes` in your home directory) is hidden as well. Two
directories under it stay reachable: the backend's own state directory
(`HERMES_HOME/sandboxes/bwrap-*`) and the Hermes scratch directory (`HERMES_HOME/cache/scratch`, the
path Hermes names in its system prompt and points `TMPDIR`, `TMP` and `TEMP` at). On the host the
scratch directory is shared by every Hermes process and session, so the sandbox mounts its own
directory there instead: `hermes-bwrap-session-<digest>` in the shared directory on the host, named
after the terminal session and the Hermes process (`hermes-bwrap-<id>` for an environment built
without a session). Inside the sandbox the scratch path is writable whenever the working directory
is, read-only under `restricted`, and keeps its files between commands, also when Hermes makes the
session's sandbox again after the idle timeout. Files other sessions and Hermes processes keep in the
shared directory are not visible. "Session" is the terminal session Hermes keys environments by: the
CLI, cron jobs and subagents share their parent's, so they share one scratch directory as they share
one sandbox. Unused for a day, the directory is removed by Hermes' scratch pruning. With
`terminal.home_mode: profile`, `HERMES_HOME/home` is the subprocess `HOME` and stays readable and
writable; the rest of `HERMES_HOME` stays hidden, the files other sessions staged there (attachments,
cached documents, images, pastes) included. When `HERMES_HOME` points elsewhere (a profile under
`.hermes/profiles/<name>`), the default `.hermes` is hidden too, so a profile's sandbox cannot read the
default home's `.env` or `auth.json`.

Each sandbox sees only its own state directory: when `terminal.sandbox_dir` lies outside the hidden
set, it is an empty read-only directory inside the sandbox with that one entry, so no sandbox reads
another one's shell snapshot or `execute_code` files. The same holds where a bind of a parent
directory shows the sandbox directory again at another path. Such a sandbox directory must not hold
the working directory, the home directory or a bind destination, or the backend refuses to start.

A hidden path that does not exist on the host is skipped. An entry that is a symlink (a dotfiles
repository linking `.ssh` to `dotfiles/ssh`) is hidden at its target, resolved when the backend
starts, and the parents of hidden paths inside a writable directory are pinned so a command cannot
rename them out from under the overlay. Writes into a hidden directory land in the sandbox's copy and
never reach the host.

One exception: a CA bundle named by `SSL_CERT_FILE`, `SSL_CERT_DIR`, `HERMES_CA_BUNDLE`,
`REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, `NODE_EXTRA_CA_CERTS`, `GIT_SSL_CAINFO` or `PIP_CERT` that sits
inside a hidden path (for example `SSL_CERT_FILE` pointing at the certifi bundle in a venv under the
Hermes home) is bound back read-only, so TLS keeps working under the `network` profile.

### Paths named by settings and variables

The CA bundle variables above, the Hermes scratch directory, the profile home and the `binds` sources
name host paths that the backend mounts. Each one is resolved once, when the backend starts, and the
mount uses the resolved path, so a symlink changed later cannot redirect it. A path is refused when it:

- is missing, or for a CA bundle or the scratch directory is not a regular file (a directory for
  `SSL_CERT_DIR` and the scratch directory); a `binds` source may be any existing path, a Unix socket,
  a FIFO or a device node included;
- lies in a directory a sandbox of this profile can write to (the working directory of any of its
  sandboxes, a read-write bind, the profile home, the Hermes scratch directory, the sandbox directory),
  where a command could swap it before the next command; a `binds` source bound at its own path
  directly under such a directory is the exception, since it cannot be renamed while mounted;
- for a CA bundle, the scratch directory or the profile home: resolves into a credential store, into a
  dot entry of the home directory that is not allowed, or into the Hermes home outside the Python
  environment Hermes runs from (credentials, transcripts and the other profiles live there).

A refused read-only `binds` entry or profile home stops the backend with an error naming it: skipping
a read-only bind would leave the path as writable as the directory around it. Any other refused path
is skipped with one warning in the log, and the sandbox runs without it.

A CA bundle you keep in the Hermes home therefore needs to move elsewhere (for example
`/usr/local/share/ca-certificates`), and one in a dot directory needs that directory in `home_allow`.
Unset and missing variables change nothing and log nothing.

## What it does not isolate

- **File tools run on the host.** Hermes gives its file tools an in-process fast path on a local
  backend, and this backend is one, so the sandbox does not confine them. Each can reach a dot entry
  the sandbox hides, the Hermes home, and paths outside the writable set:

  | Tool | What runs on the host |
  |---|---|
  | `read_file` | The read itself, in the Hermes process. |
  | `write_file` | The write (temp file and rename), in the Hermes process. |
  | `patch` | The read and the write-back, in the Hermes process. |
  | `search_files` | ripgrep, started by the Hermes process. Its fallbacks (`find`/`grep` when there is no ripgrep, and a few helper probes) run in the sandbox. |

  Hermes' own file-safety rules still apply (writes to `~/.ssh` and the other protected credential
  paths are refused), but reads are not limited to what a command can see. The scratch directory is
  the other way round: commands see their sandbox's own scratch dir at the `$TMPDIR` path, the file
  tools see the shared one on the host, so a file one side puts there is invisible to the other;
  hand files over through the working directory. Running Hermes with
  `HERMES_NATIVE_FILE_READ=0` (core's switch for that fast path, not a setting of this plugin) sends
  every file operation through the sandbox, at the cost of a sandbox spawn per operation; the same
  paths are then hidden and read-only for the file tools. The model is told which case applies in its
  system prompt.
- **The rest of the host filesystem is readable.** Anything your user can read outside the hidden set
  (`/etc`, other dotfiles, secrets kept inside project directories) is readable by a command. Keep
  secrets in the hidden paths.
- **No seccomp filter.** System calls are not filtered; this is a filesystem and process boundary, not
  a defense against kernel exploits.
- **No cgroup limits.** The limits are per-process rlimits; a command that forks can use more memory in
  total than `memory_mb`.
- **Network is all or nothing.** The `network` profile shares the host network with no egress
  filtering; the other two have loopback only.
- **Other Unix sockets stay connectable** (a read-only mount does not block `connect()`): anything
  outside `/run/user/<uid>`, `/tmp` and the docker socket, if a command knows its path.
- **No background processes.** Each command is its own sandbox, and bwrap tears it down (pid namespace
  included) when the command's shell exits, so a `background: true` job dies right after it starts.
  The model is told background processes are unavailable on this backend.
- **No persistent `execute_code` kernel.** Hermes tries its remote session kernel first, which cannot
  outlive the sandbox that starts it; it then falls back to running each `execute_code` call as its
  own script (inside the sandbox, under the same limits). Python state does not carry over between
  calls, and Hermes logs one `remote kernel died at spawn` warning per call.

## Working directory

The working directory (`terminal.cwd`, else the directory Hermes was started in, as for the `local`
backend) is the writable set: everything under it can be changed, everything else is read-only. Point
it at a project or scratch directory. Only that setting decides what is writable: a `cd` (the model's,
or a Desktop/TUI workspace outside it) moves where commands start, and they run read-only there, also
after Hermes recreates the environment. A `terminal.cwd` that does not exist runs commands read-only in
its nearest existing parent. With your home directory as the working directory, the existing non-dot
entries of it are writable and nothing else is: the dot entries are read-only or hidden, and no new
file or directory can be made at the top of the home directory. Hermes logs a warning in that case. A working directory of
`/` is refused, and so is one under the Hermes home or a hidden path, since no command could run there.
When Hermes was started from such a place (a gateway launched from its checkout under `.hermes`), the
backend starts in your home directory instead, as the gateway does for the `local` backend.

Set `terminal.cwd` for every profile that uses this backend, a gateway profile above all: without it
the sandbox starts in your home directory, and your checkouts and `~/bin` are writable. Give each
profile its own directory, so one profile's commands cannot change another's files:

```sh
hermes -p <profile> config set terminal.cwd "$HOME/work/<profile>"
```

If the working directory is deleted on the host (for example by the command's own `rm -rf`), later
commands run in the nearest existing parent directory, read-only, until it exists again.

## Profiles

| Profile | Working directory | Network | Use it for |
|---------|-------------------|---------|------------|
| `restricted` | read-only | none (loopback only) | Inspection and read-only analysis |
| `workspace` | writable | none (loopback only) | Builds and edits that must not reach the network |
| `network` | writable | host network | Everything else (the default) |

## Extra binds

`binds` mounts more host directories into the sandbox, read-only unless `readonly: false`; `dest`
defaults to `src`:

```yaml
plugins:
  entries:
    bubblewrap:
      settings:
        binds:
          - {src: /data/models}
          - {src: /srv/scratch, dest: /srv/scratch, readonly: false}
```

A source under a hidden path is ignored with a warning. So is a source that contains a hidden path
(your home directory, say) when `dest` differs from `src`: the hidden paths are covered only at their
own location, so a copy of the tree elsewhere would show them. Bind such a source at its own path
instead. Because the root is read-only, a `dest` must already exist on the host or sit under a
writable mount.

A source may be a directory, a file, a Unix socket (a service or agent socket you choose to share), a
FIFO or a device node.

A read-write bind (or a writable working directory) that contains a credential path which does not
exist on the host is refused when the backend starts: the backend hides a credential path by mounting
over it, a path that is not there cannot be mounted over, and a command could then create it. A
read-write bind of all of `~/.config` is refused while `~/.config/gh` does not exist. Bind the one
directory a tool writes to instead (`~/.cache/pip`, `~/.cargo/registry`), at its own path.

A bind whose source lies inside the working directory of a sandbox of this profile, inside another
read-write bind or inside the profile home is refused, and the backend does not start, unless it has
`dest` equal to `src` and sits directly under that directory with no symlink on the way: bwrap
resolves a bind source on the host for every command, so a source a command can rename or replace
with a symlink would let it choose what the next command mounts (see
[Paths named by settings and variables](#paths-named-by-settings-and-variables)). A missing source
stops the backend too when the bind is read-only, and is skipped with a warning when it is read-write.
Under `terminal.home_mode: profile`, `HERMES_HOME/home` must be a plain directory or a link to a
directory outside the hidden set and outside every directory a sandbox can write to, or the backend
refuses to start.

## Resource limits

| Setting | Default | Limit |
|---------|---------|-------|
| `memory_mb` | `256` | Virtual memory per process (`RLIMIT_AS`) |
| `cpu_seconds` | `30` | CPU time per process (`RLIMIT_CPU`) |
| `max_procs` | `256` | Processes and threads one command may run (`RLIMIT_NPROC`, counted for that sandbox alone) |

A value of `0` disables that limit. The defaults are deliberately tight and some everyday tools exceed
them: `pip`, `node`, `cargo` and most compilers need more than 256 MB of address space (the command
fails with `MemoryError` or a similar message: raise `memory_mb` to 1024 or 2048), and a long compile
or test run uses more than 30 seconds of CPU (killed, exit 137: raise `cpu_seconds`). `max_procs` is
set inside the sandbox, where the kernel counts only the processes of that sandbox: a fork bomb stops
at 256 processes whatever else your user runs, and a command cannot raise the limit. This needs Linux
5.14 or later. On an older kernel the backend sets no process limit, logs one warning and keeps the
memory and CPU limits.

## Approval

Because the sandbox writes to real host paths, Hermes' dangerous-command approval applies exactly as
for the `local` backend, including the hardline floor.

## Compared with hermes-agent#116579

This plugin is that PR's backend (and the
[#102875](https://github.com/NousResearch/hermes-agent/pull/102875) work it salvaged, by
[@TacoTakumi](https://github.com/TacoTakumi)) moved onto Hermes' terminal backend plugin interface, with
no core changes:

- Settings moved from `terminal.bubblewrap_*` to `plugins.entries.bubblewrap.settings.*`.
- The sandbox notice reaches the model as a plugin system prompt section instead of a core prompt edit.
- Background jobs are not refused up front (core has no plugin flag for that yet); they die with their
  sandbox, as described above.
- `execute_code` reaches the per-call path by falling back from the session kernel, instead of a core
  branch that skips it.
- The gateway starts in the process directory, or your home directory when that is hidden, instead of
  `MESSAGING_CWD`.

Tested with bubblewrap 0.11.1 on Linux 7.0; the original work was tested with 0.9.0 on kernel 6.8.
