"""Bubblewrap (bwrap) terminal backend: the argv builder and the environment.

Every terminal command under ``terminal.backend: bubblewrap`` runs inside a
bwrap sandbox. This module builds the bwrap argv prefix from the plugin's
settings (``plugins.entries.bubblewrap.settings.*``) and construction-time
inputs only. Nothing produced inside a sandbox (the
tracked cwd, the shell snapshot, command output) feeds a mount or a flag
argument; the tracked cwd is used for ``--chdir`` alone.

Layout of the argv (later mounts overlay earlier ones):

1. namespace and process-safety flags
2. read-only root, fresh /dev, /proc and a tmpfs /tmp, then a tmpfs over
   the user's runtime dir (/run/user/<uid>: agent and bus sockets) and an
   empty file over the docker socket
3. the initial cwd, read-write for workspace and network, read-only for
   restricted; a ``-try`` bind, so a cwd deleted on the host (the agent's
   own ``rm -rf``) skips the bind and the command runs in the recovered
   cwd instead of wedging every later spawn
4. operator binds from the ``binds`` setting, minus sensitive sources
5. the pins: each ancestor of a sensitive path or of HERMES_HOME that lies
   strictly inside a writable bind (the cwd under the workspace and
   network profiles, a read-write operator bind) is bound over itself, so
   a command cannot rename the parent of a hidden path out from under its
   overlay
6. the HOME layout (bubblewrap_home.home_layout_args), which makes HOME
   default-deny for dot entries: a tmpfs over HOME, the non-dot entries
   and the allowed dot entries bound back, a tmpfs with the allowed
   children for HOME/.config, HOME/.local and HOME/.local/share, an
   overlay for each hidden path that is still visible, and at the very
   end a read-only remount of each tmpfs. The cwd, the operator binds and
   the pins of steps 3 to 5 that land under HOME, and the binds of steps
   8 to 10 that do, are emitted inside the layout, before the remount: a
   mount point under a sealed HOME could not be made
7. the sensitive overlays for hidden paths outside HOME: a tmpfs over a
   directory and an empty file over a file that exists on the host.
   HERMES_HOME is hidden as a whole, and so is the default HOME/.hermes
   when HERMES_HOME points elsewhere (a profile under
   HOME/.hermes/profiles, or any other directory), so a profile's sandbox
   cannot read the default home's credentials
8. a tmpfs over the sandbox dir when no hidden path covers it, so a
   sandbox sees no other environment's state (read-only after step 10);
   the trust-anchor files the overlays hide (CA_BUNDLE_VARS), read-only
   from the paths they resolved to; this environment's own scratch dir
   mounted at the Hermes scratch dir, which every Hermes process and
   session shares on the host (TMPDIR of a command points there)
9. under terminal.home_mode=profile, HERMES_HOME/home read-write on top of
   that overlay (it is the subprocess HOME then), from the directory it
   resolved to at construction. The trust anchors, the scratch dir, the
   profile home and the operator binds of step 4 are host paths named by
   configuration or the environment: each is resolved and checked once
   at construction by bubblewrap_binds.bind_refusal. A refused read-only
   operator bind or profile home stops the construction with an error;
   any other refused bind is skipped with one warning
10. the per-environment state dir read-write at the same path; between
    commands it holds the shell snapshot and the cwd file, and for the
    duration of an execute_code call the hermes_exec_<id> dir with the
    script, the tools module and the rpc files
11. ``--chdir`` to the tracked cwd, then ``--`` so the caller can append
    the shell argv

The paths in the argv are fixed at construction: the hidden set and the
HOME allowlist are resolved once, so a symlinked entry is hidden at its
target and a command cannot widen the allowlist, and the cwd, state dir and operator bind destinations use their
real paths (bwrap resolves a mount destination inside the sandbox root,
where an absolute symlink points nowhere), so the pins are computed in
the real tree of each bind. What varies per spawn is presence only: the
listing of the top of HOME, an overlay is emitted for a sensitive path that exists on the host at spawn
time and never for one that does not, a pin for an ancestor directory that
exists inside a bind that is writable anyway, and nothing at all for a
path that has become a symlink since construction. So host changes (or a
sandbox with a writable HOME planting a symlink) can only add hiding
mounts and pins, never expose anything. The pins are what
keep that true: a hidden entry is a mount point and cannot be renamed
from inside the sandbox, but without the pins a writable cwd covering its
parent, or a writable bind whose destination resolves into the real tree
above it, lets a command rename the parent, and the next spawn then finds
nothing to hide at the old path while the secret is readable under the
new one.

Resource limits are applied by prlimit(1) from util-linux, which the
environment places in front of the bwrap argv above: prlimit sets
RLIMIT_AS, RLIMIT_CPU and RLIMIT_NPROC on itself and execs bwrap, so the
limits are in place before the sandbox starts and no Python code runs in
the forked child. (Popen's pre-exec callback is documented as unsafe in
a threaded process, and the gateway is one.) A value above the inherited
hard limit is clamped to it in the parent, since raising a hard limit
needs CAP_SYS_RESOURCE.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import resource
import shlex
import shutil
import signal
import sys
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, replace
from typing import Iterable, Mapping, Sequence

from hermes_constants import SCRATCH_DIR_MARKER_ENV, SCRATCH_TMP_ENV_VARS, get_hermes_home, get_real_home
from tools.environments.base import EnvironmentConnectionError, get_sandbox_dir
from tools.environments.local import (
    LocalEnvironment,
    _find_bash,
    _make_run_env,
    _pipe_stdin,
    _prepend_shell_init,
    _resolve_local_initial_cwd,
    _resolve_shell_init_files,
)

from . import binds as bubblewrap_binds
from . import home as bubblewrap_home

logger = logging.getLogger(__name__)

# Credential stores under HOME, relative to it. The set and the rule that
# hides every other dot entry of HOME by default live in bubblewrap_home.
SENSITIVE_HOME_PATHS: tuple[str, ...] = bubblewrap_home.DENIED_HOME_PATHS

# The default Hermes home under the real HOME (the Linux default of
# hermes_constants.get_hermes_home). It joins the hidden set beside the
# resolved HERMES_HOME so a relocated HERMES_HOME (a profile at
# HOME/.hermes/profiles/<name>, or any other directory) does not leave the
# default home's .env and auth.json readable inside the sandbox.
DEFAULT_HERMES_HOME_NAME = ".hermes"

# Variables that name host agent or bus sockets. LocalEnvironment passes them
# through; the bwrap prefix unsets them so the sandbox env is local's minus
# exactly these, with no change to LocalEnvironment.
HOST_SOCKET_VARS: tuple[str, ...] = ("SSH_AUTH_SOCK", "GPG_AGENT_INFO", "DBUS_SESSION_BUS_ADDRESS")

# Variables that name a trust-anchor file (or directory, for SSL_CERT_DIR)
# on the host. Hermes itself exports SSL_CERT_FILE=certifi's bundle at
# startup, and on the standard install that file lives in the venv under
# HERMES_HOME; hidden, every TLS client in the sandbox fails with "error
# adding trust anchors" under the network profile. Trust anchors are
# public, so a bundle the hidden set would mask is bound back read-only,
# once bubblewrap_binds.validated_bind_source has checked where it leads.
CA_BUNDLE_VARS: tuple[str, ...] = (
    "HERMES_CA_BUNDLE", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS", "GIT_SSL_CAINFO", "PIP_CERT",
)

DEFAULT_PROFILE = "network"
DEFAULT_HOME_MODE = "auto"
DEFAULT_MEMORY_MB = 256
DEFAULT_CPU_SECONDS = 30
DEFAULT_MAX_PROCS = 256

# The plugin's settings keys (plugins.entries.bubblewrap.settings.<key>).
SETTING_PROFILE = "profile"
SETTING_BINDS = "binds"
SETTING_MEMORY_MB = "memory_mb"
SETTING_CPU_SECONDS = "cpu_seconds"
SETTING_MAX_PROCS = "max_procs"
SETTING_HOME_ALLOW = "home_allow"
SETTINGS_PREFIX = "plugins.entries.bubblewrap.settings"
# terminal.home_mode is a core setting; core bridges it to this env name and
# hermes_constants.get_subprocess_home reads it. The spellings that
# get_subprocess_home treats as "profile".
ENV_HOME_MODE = "TERMINAL_HOME_MODE"
PROFILE_HOME_MODES: frozenset[str] = frozenset({"profile", "isolated", "profile_home", "profile-home"})


@dataclass(frozen=True)
class Profile:
    """What a named profile allows: a writable cwd and/or host networking."""

    name: str
    writable_cwd: bool
    share_net: bool


PROFILES: dict[str, Profile] = {
    "restricted": Profile("restricted", writable_cwd=False, share_net=False),
    "workspace": Profile("workspace", writable_cwd=True, share_net=False),
    "network": Profile("network", writable_cwd=True, share_net=True),
}
PROFILE_NAMES: tuple[str, ...] = tuple(PROFILES)


def resolve_profile(name: str) -> Profile:
    """Return the profile for *name* or raise ValueError listing the valid names."""
    try:
        return PROFILES[name]
    except KeyError:
        raise ValueError(
            f"Unknown {SETTINGS_PREFIX}.{SETTING_PROFILE} {name!r}. "
            f"Valid profiles: {', '.join(PROFILE_NAMES)}"
        ) from None


@dataclass(frozen=True)
class BindMount:
    """One operator-supplied bind: host *src* mounted at *dest* in the sandbox."""

    src: str
    dest: str
    readonly: bool = True


@dataclass(frozen=True)
class BubblewrapConfig:
    """The plugin settings and terminal.home_mode, with the documented defaults."""

    profile: str = DEFAULT_PROFILE
    binds: tuple[BindMount, ...] = ()
    memory_mb: int = DEFAULT_MEMORY_MB
    cpu_seconds: int = DEFAULT_CPU_SECONDS
    max_procs: int = DEFAULT_MAX_PROCS
    # terminal.home_mode rides along because it decides whether HERMES_HOME/home
    # is the subprocess HOME and so must be bound back over the overlay.
    home_mode: str = DEFAULT_HOME_MODE
    # The home_allow setting: more allowlist units under HOME
    # (bubblewrap_home.allow_unit), shown read-only.
    home_allow: tuple[str, ...] = ()


def _setting_name(key: str) -> str:
    return f"{SETTINGS_PREFIX}.{key}"


def _parse_binds(raw: object) -> tuple[BindMount, ...]:
    """The ``binds`` setting: a list of {src, dest, readonly} mappings, or that list as a JSON string."""
    name = _setting_name(SETTING_BINDS)
    if raw is None or raw == "" or raw == []:
        return ()
    entries = raw
    if isinstance(raw, str):
        try:
            entries = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{name} must be a list of {{src, dest, readonly}} objects: {exc}") from None
    if not isinstance(entries, list):
        raise ValueError(f"{name} must be a list, got {type(entries).__name__}")
    binds: list[BindMount] = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("src"), str) or not entry["src"]:
            raise ValueError(f"{name} entries need a non-empty 'src' string, got {entry!r}")
        src = entry["src"]
        dest = entry.get("dest") or src
        if not isinstance(dest, str):
            raise ValueError(f"{name} 'dest' must be a string, got {dest!r}")
        binds.append(BindMount(src=src, dest=dest, readonly=bool(entry.get("readonly", True))))
    return tuple(binds)


def _parse_string_list(key: str, raw: object) -> tuple[str, ...]:
    """A list of strings, or that list as a JSON string; missing or blank gives the empty tuple."""
    name = _setting_name(key)
    if raw is None or raw == "" or raw == []:
        return ()
    entries = raw
    if isinstance(raw, str):
        try:
            entries = json.loads(raw)
        except ValueError:
            raise ValueError(f"{name} must be a list of strings, got {raw!r}") from None
    if not isinstance(entries, list) or not all(isinstance(item, str) for item in entries):
        raise ValueError(f"{name} must be a list of strings, got {raw!r}")
    return tuple(entries)


def _parse_limit(key: str, raw: object, default: int) -> int:
    name = _setting_name(key)
    if raw is None:
        return default
    if isinstance(raw, bool):
        raise ValueError(f"{name} must be a non-negative integer (0 disables the limit), got {raw!r}")
    if isinstance(raw, int):
        parsed = raw
    else:
        value = str(raw).strip()
        if not value:
            return default
        try:
            parsed = int(value)
        except ValueError:
            raise ValueError(f"{name} must be a non-negative integer (0 disables the limit), got {raw!r}") from None
    if parsed < 0:
        raise ValueError(f"{name} must be a non-negative integer (0 disables the limit), got {raw!r}")
    return parsed


def load_bubblewrap_config(settings: Mapping[str, object] | None = None, home_mode: str = "") -> BubblewrapConfig:
    """Build the config from the plugin's settings mapping and terminal.home_mode.

    Missing or blank values take the documented defaults. Malformed values
    raise ValueError naming the offending setting. The profile name is not
    validated here; :func:`resolve_profile` rejects unknown names at
    environment construction.
    """
    settings = settings or {}
    profile = str(settings.get(SETTING_PROFILE) or "").strip().lower() or DEFAULT_PROFILE
    return BubblewrapConfig(
        profile=profile,
        binds=_parse_binds(settings.get(SETTING_BINDS)),
        memory_mb=_parse_limit(SETTING_MEMORY_MB, settings.get(SETTING_MEMORY_MB), DEFAULT_MEMORY_MB),
        cpu_seconds=_parse_limit(SETTING_CPU_SECONDS, settings.get(SETTING_CPU_SECONDS), DEFAULT_CPU_SECONDS),
        max_procs=_parse_limit(SETTING_MAX_PROCS, settings.get(SETTING_MAX_PROCS), DEFAULT_MAX_PROCS),
        home_mode=(home_mode or "").strip().lower() or DEFAULT_HOME_MODE,
        home_allow=_parse_string_list(SETTING_HOME_ALLOW, settings.get(SETTING_HOME_ALLOW)),
    )


def home_replaced_by_bind(home_root: str, binds: Iterable[BindMount]) -> bool:
    """True when an operator bind puts another directory over HOME or over a parent of it."""
    return any(_is_within(home_root, bind.dest) and not _same_host_path(bind.src, bind.dest) for bind in binds)


def sensitive_paths(home: str, hermes_home: str) -> tuple[str, ...]:
    """Real host paths that must stay hidden: the HOME set, HERMES_HOME and HOME/.hermes.

    The default HOME/.hermes rides along with the resolved HERMES_HOME so a
    relocated HERMES_HOME (a profile at HOME/.hermes/profiles/<name>) does
    not leave the default home readable; when the two are the same
    directory it is listed once. A missing HOME/.hermes stays in the set
    and emits no mount (sensitive_overlay_args skips absent paths).

    Each path goes through os.path.realpath, so an entry that is a symlink
    (a dotfiles repository linking HOME/.ssh to HOME/dotfiles/ssh) or that sits
    under a symlinked component names the directory or file holding the
    secret. That is also the only path bwrap can mount over: it resolves a
    mount destination inside the sandbox root, where an absolute symlink
    points nowhere. BubblewrapEnvironment resolves the set once at
    construction and keeps it for its life. Resolving per spawn would let
    a sandbox that can replace the symlink (HOME in its writable set)
    point the next overlay elsewhere and leave the secret bare.

    The HOME part is bubblewrap_home.denied_home_paths: the module list
    plus the HOME paths the file safety policy refuses. An entry under
    another one is dropped as covered by it.
    """
    home = os.path.abspath(os.path.expanduser(home))
    resolved = list(bubblewrap_home.denied_home_paths(home))
    for path in (os.path.abspath(os.path.expanduser(hermes_home)), os.path.join(home, DEFAULT_HERMES_HOME_NAME)):
        real = os.path.realpath(path)
        if real not in resolved:
            resolved.append(real)
    return tuple(p for p in resolved if not any(p != other and _is_within(p, other) for other in resolved))


def empty_file_path(state_dir: str) -> str:
    """Host path of the zero-length file bound over sensitive files.

    It sits beside the state dir, not inside it: the state dir is bound
    read-write into every spawn, so a file kept there could be rewritten
    from inside the sandbox and would then show at the hidden paths. That
    holds only while the parent of the state dir lies outside every
    writable bind, since bwrap follows a symlink put in the file's place
    when it resolves the bind source; BubblewrapEnvironment therefore
    refuses a sandbox dir inside the cwd or a read-write operator bind
    unless a hidden path covers it (_check_sandbox_root).
    """
    return state_dir.rstrip(os.sep) + ".empty"


def sensitive_overlay_args(hidden_paths: Sequence[str], state_dir: str) -> list[str]:
    """Mount directives that hide *hidden_paths*, the set from sensitive_paths.

    A directory gets a fresh tmpfs, a file gets the empty file bound over
    it, and a path missing on the host gets nothing so bwrap never fails
    on an absent mount target. A path that is a symlink at spawn time gets
    nothing either: the set holds real paths, so a symlink there was
    planted after construction where nothing hidden existed (by a sandbox
    whose writable set covers it), and a mount on it would follow it. On
    bwrap 0.9.0: ``--tmpfs`` on a file path fails with "Not a
    directory", and a ro-bind of /dev/null mounts but reads fail with
    EACCES because bwrap remounts binds nodev inside the user namespace;
    only the empty-file bind works for files.
    """
    empty = empty_file_path(state_dir)
    argv: list[str] = []
    for path in hidden_paths:
        if os.path.islink(path):
            continue
        if os.path.isdir(path):
            argv += ["--tmpfs", path]
        elif os.path.exists(path):
            argv += ["--ro-bind", empty, path]
    return argv


# Marks an argument the caller left out, where None already has a meaning.
_UNRESOLVED: object = object()


DOCKER_SOCKETS: tuple[str, ...] = ("/var/run/docker.sock", "/run/docker.sock")


def runtime_overlay_args(state_dir: str, uid: int) -> list[str]:
    """Hide the user's runtime dir and the docker socket.

    A read-only bind of / still lets a command connect() to unix sockets:
    the gpg-agent, keyring and ssh-agent sockets under /run/user/<uid>
    would sign and decrypt with the user's loaded keys, and the docker
    socket is a root-equivalent escape for a user in the docker group. The
    runtime dir gets a tmpfs (nothing a command needs lives there) and each
    docker socket that exists gets the empty file bound over it, which
    makes it a plain file.
    """
    argv: list[str] = []
    runtime_dir = f"/run/user/{uid}"
    if os.path.isdir(runtime_dir):
        argv += ["--tmpfs", runtime_dir]
    empty = empty_file_path(state_dir)
    seen: set[str] = set()
    for sock in DOCKER_SOCKETS:
        if not os.path.exists(sock):
            continue
        # Mount at the real path: bwrap cannot create a mount point through
        # the /var/run -> /run symlink, and the symlink resolves to it anyway.
        real = os.path.realpath(sock)
        if real in seen:
            continue
        seen.add(real)
        argv += ["--ro-bind", empty, real]
    return argv


def _is_within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def is_sensitive_source(src: str, hidden_paths: Sequence[str]) -> bool:
    """True when *src* (or what it symlinks to) is at or under a hidden path."""
    abs_src = os.path.abspath(os.path.expanduser(src))
    candidates = {abs_src, os.path.realpath(abs_src)}
    return any(_is_within(c, root) for c in candidates for root in hidden_paths)


def python_env_roots() -> tuple[str, ...]:
    """Real paths of the Python environment Hermes runs from.

    The one part of a hidden Hermes home a trust-anchor bind may come from:
    certifi's bundle lives in the venv, which the standard install keeps
    under HERMES_HOME. The rest of a Hermes home (credentials, transcripts,
    other profiles, staged files) never is.
    """
    return tuple(dict.fromkeys(os.path.realpath(p) for p in (sys.prefix, sys.exec_prefix, sys.base_prefix) if p))


def hermes_homes(home: str, hermes_home: str) -> tuple[str, ...]:
    """Real paths of HERMES_HOME and the default HOME/.hermes, the Hermes homes the overlays hide whole."""
    paths = (os.path.abspath(os.path.expanduser(hermes_home)), os.path.join(home, DEFAULT_HERMES_HOME_NAME))
    return tuple(dict.fromkeys(os.path.realpath(p) for p in paths))


def bind_guard(
    hidden_paths: Sequence[str],
    homes: Sequence[str],
    writable: Iterable[str],
    home_root: str | None,
    home_allow: Sequence[str],
) -> bubblewrap_binds.BindGuard:
    """The BindGuard for an environment: the hidden set split into credential stores and Hermes homes."""
    homes = tuple(homes)
    return bubblewrap_binds.BindGuard(
        secret=tuple(p for p in hidden_paths if not any(_is_within(p, h) for h in homes)),
        hermes_homes=homes,
        masks=tuple(dict.fromkeys((*hidden_paths, *homes))),
        writable=tuple(dict.fromkeys(os.path.realpath(p) for p in writable)),
        home_root=home_root,
        home_allow=tuple(home_allow),
    )


def ca_bundle_binds(
    guard: bubblewrap_binds.BindGuard, environ: Mapping[str, str] | None = None,
) -> tuple[tuple[str, str], ...]:
    """(source, destination) pairs for the trust anchors of CA_BUNDLE_VARS the overlays would hide."""
    return bubblewrap_binds.ca_bundle_binds(
        CA_BUNDLE_VARS, guard, environ=os.environ if environ is None else environ, public=python_env_roots(),
    )


def resolve_scratch_dir(hermes_home: str) -> str:
    """Real path of the Hermes scratch dir for *hermes_home* (``<home>/cache/scratch``), created if missing."""
    from hermes_constants import get_scratch_dir

    return os.path.realpath(str(get_scratch_dir(hermes_home, prune=False)))


def private_scratch_path(scratch_dir: str, state_dir: str, session_key: str | None = None) -> str:
    """Host path of one sandbox's own scratch dir, inside the Hermes scratch dir.

    The sandbox mounts it at the Hermes scratch dir itself, the path Hermes
    names in its system prompt and exports as TMPDIR, so a command writes
    there as with the local backend and sees nothing of the files other
    sandboxes and Hermes processes keep in the shared directory. With a
    *session_key* (the terminal task id Hermes creates the environment
    for) the name is a digest of that key and of this process, so the
    environment Hermes makes again for the same session after the idle
    timeout finds the files it left, while another session, or another
    Hermes process with the same key (every CLI uses ``default``), gets a
    directory of its own. Without one the name carries the id of the state
    dir. It is a top-level entry of the scratch dir, so the pruning of idle
    scratch entries removes it once no sandbox has used it for a day.
    """
    if session_key is None:
        return os.path.join(scratch_dir, "hermes-" + os.path.basename(state_dir.rstrip(os.sep)))
    digest = hashlib.sha256(f"{os.getpid()}\0{session_key}".encode()).hexdigest()[:16]
    return os.path.join(scratch_dir, f"hermes-bwrap-session-{digest}")


def hidden_path_under(src: str, hidden_paths: Sequence[str]) -> str | None:
    """The first hidden path strictly under *src* (or under what it symlinks to), else None."""
    abs_src = os.path.abspath(os.path.expanduser(src))
    for root in (abs_src, os.path.realpath(abs_src)):
        for hidden in hidden_paths:
            if hidden != root and _is_within(hidden, root):
                return hidden
    return None


def _real_host_path(path: str) -> str:
    return os.path.realpath(os.path.abspath(os.path.expanduser(path)))


def _same_host_path(a: str, b: str) -> bool:
    return _real_host_path(a) == _real_host_path(b)


def _swappable_link(path: str, roots: Iterable[str]) -> str | None:
    """The outermost symlink component of *path* whose parent resolves inside a writable root, else None.

    Such a link sits in a directory a sandbox can write to, so a command can
    replace it; a link outside the writable set is on the read-only root.
    """
    roots = list(roots)
    found: str | None = None
    while True:
        parent = os.path.dirname(path)
        if parent == path:
            return found
        if os.path.islink(path) and any(_is_within(os.path.realpath(parent), root) for root in roots):
            found = path
        path = parent


def filter_binds(binds: tuple[BindMount, ...], hidden_paths: Sequence[str]) -> list[BindMount]:
    """Drop binds that would expose a hidden path, logging a warning for each.

    A source at or under a hidden path would mount the secret itself. A
    source that contains a hidden path and lands at another destination
    would show the secret there: the overlays cover a hidden path only at
    its real location, and a mirror of an ancestor is a second view of the
    same host tree. With dest equal to src the bind
    is the cwd=HOME shape, and the overlays and pins land on top of it.
    """
    kept: list[BindMount] = []
    for bind in binds:
        if is_sensitive_source(bind.src, hidden_paths):
            logger.warning(
                "Ignoring plugins.entries.bubblewrap.settings.binds entry %s: source is under a sensitive path",
                bind.src,
            )
            continue
        hidden = hidden_path_under(bind.src, hidden_paths)
        if hidden is not None and not _same_host_path(bind.src, bind.dest):
            logger.warning(
                "Ignoring plugins.entries.bubblewrap.settings.binds entry %s -> %s: the source contains the "
                "hidden path %s, which would be readable at the destination. Bind it at its "
                "own path (dest equal to src) instead.",
                bind.src, bind.dest, hidden,
            )
            continue
        kept.append(bind)
    return kept


def expand_bind_srcs(binds: Iterable[BindMount]) -> list[BindMount]:
    """Return *binds* with each src taken through expanduser and abspath.

    bwrap does not expand a tilde, so a source written as ~/data made every
    spawn fail on a missing source path. The
    sensitivity checks expand the same way, so they and the emitted argv
    see one path. BubblewrapEnvironment applies this once at construction;
    build_bwrap_args emits a src as given and resolves nothing per spawn.
    """
    return [replace(bind, src=os.path.abspath(os.path.expanduser(bind.src))) for bind in binds]


def resolve_bind_dests(binds: Iterable[BindMount]) -> list[BindMount]:
    """Return *binds* with each dest taken through realpath on the host.

    bwrap resolves a mount destination inside the sandbox root: a relative
    symlink lands the mount on its target, an absolute one aborts the
    spawn. Naming the real path up front gives both the same mount, and
    the ancestor pins are then computed against the real tree the bind
    makes writable. BubblewrapEnvironment applies this once at
    construction; build_bwrap_args does not, so a symlink planted under
    a dest between spawns cannot move the mount.
    """
    return [
        replace(bind, dest=os.path.realpath(os.path.abspath(os.path.expanduser(bind.dest))))
        for bind in binds
    ]


def _ancestors_within(path: str, root: str) -> list[str]:
    """Ancestors of *path* (not *path* itself) strictly inside *root*, outermost first."""
    found: list[str] = []
    parent = os.path.dirname(path)
    while parent != root and _is_within(parent, root):
        found.append(parent)
        parent = os.path.dirname(parent)
    found.reverse()
    return found


def ancestor_pin_args(
    writable_binds: Sequence[tuple[str, str]],
    mount_points: Iterable[str],
    hidden_paths: Sequence[str],
) -> list[str]:
    """Bind over itself each ancestor of a hidden path that lies strictly inside a writable bind.

    A hidden entry is a mount point, so a command cannot rename or remove
    it, but a writable bind covering its parent (the cwd at HOME or above
    it, a read-write operator bind of the same) lets a command rename the
    parent. The next spawn then finds nothing at the hidden path, emits no
    overlay, and the secret is readable under the new name. Binding each
    such ancestor over itself makes it a mount point too: rename and rmdir
    fail with EBUSY while it stays writable, so the bind loses nothing.

    *writable_binds* are (src, dest) pairs in argv order; a pin's source is
    the host path the ancestor maps to through its bind, which is the
    ancestor itself when src and dest agree. No pin is emitted for an
    ancestor that is a mount point already (*mount_points*: the cwd and the
    operator bind destinations), nor for one missing on the host or a
    symlink there: a mount cannot pin a symlink and would bind its target
    instead. Nor for one reached through a symlinked component between
    the bind root and the ancestor: the pin must land inside the real tree
    of the bind (realpath of the host path equals realpath of the bind
    source plus the relative path), or a link leaving the bind would carry
    the pin, and write access, outside it. Presence is the only per-spawn
    input, and a pin never grants more than the bind around it already
    did.
    """
    normalize = lambda p: os.path.abspath(os.path.expanduser(p))
    seen: set[str] = {normalize(p) for p in mount_points}
    argv: list[str] = []
    for src, dest in writable_binds:
        root = normalize(dest)
        real_src = os.path.realpath(normalize(src))
        for path in hidden_paths:
            for ancestor in _ancestors_within(path, root):
                if ancestor in seen:
                    continue
                seen.add(ancestor)
                rel = os.path.relpath(ancestor, root)
                host = os.path.join(normalize(src), rel)
                if not os.path.isdir(host) or os.path.islink(host):
                    continue
                if os.path.realpath(host) != os.path.join(real_src, rel):
                    continue
                argv += ["--bind", host, ancestor]
    return argv


def _late_mounts(
    profile: Profile,
    state_dir: str,
    *,
    scratch_dir: str | None,
    scratch_private: str | None,
    ca_bundles: Sequence[tuple[str, str]],
    profile_home: tuple[str, str] | None,
    sandbox_root: str | None,
) -> list[tuple[str, ...]]:
    """The mounts that go on top of every overlay, in order: the sandbox dir, trust anchors, scratch, the profile home, the state dir."""
    late: list[tuple[str, ...]] = []
    # The sandbox dir holds the state dir, the empty file and the scratch
    # name of every environment. Outside the hidden set it gets an empty
    # tmpfs, read-only once this environment's state dir is bound into it
    # (below), so a sandbox sees its own state and nothing of another's.
    if sandbox_root:
        late.append(("--tmpfs", sandbox_root))
    # Trust anchors the overlays hid come back read-only, from the path
    # they resolved to at construction (see ca_bundle_binds); -try, so a
    # bundle removed from the host between spawns fails in the tool that
    # opens it, not in bwrap.
    late += [("--ro-bind-try", src, dest) for src, dest in ca_bundles]
    # The Hermes scratch dir lives under the hidden HERMES_HOME. It is
    # TMPDIR of every Hermes process and session, so it holds their temp
    # files, sockets and message payloads. The sandbox gets the scratch dir
    # of this environment alone mounted at that path: the path Hermes names
    # in its system prompt and exports as TMPDIR, writable with the cwd,
    # read-only under the restricted profile so a write fails loudly.
    if scratch_dir and scratch_private:
        late.append(("--bind-try" if profile.writable_cwd else "--ro-bind-try", scratch_private, scratch_dir))
    # Under home_mode=profile the subprocess HOME is HERMES_HOME/home
    # (hermes_constants.get_subprocess_home), so bind it back read-write on
    # top of the overlay, from the directory it resolved to at construction
    # (BubblewrapEnvironment._validate_host_binds); the rest of HERMES_HOME
    # stays hidden.
    if profile_home is not None:
        late.append(("--bind-try", *profile_home))
    # The state dir holds the shell snapshot and the cwd file between
    # commands (and the execute_code sandbox dir during a call); it is
    # bound after the overlays so it stays writable at the same path in
    # every spawn.
    late.append(("--bind", state_dir, state_dir))
    if sandbox_root:
        late.append(("--remount-ro", sandbox_root))
    return late


def build_bwrap_args(
    config: BubblewrapConfig,
    initial_cwd: str,
    state_dir: str,
    home: str,
    hermes_home: str,
    tracked_cwd: str,
    *,
    bwrap_path: str = "bwrap",
    hidden_paths: Sequence[str] | None = None,
    home_root: str | None | object = _UNRESOLVED,
    home_allow: Sequence[str] | None = None,
    ca_bundles: Sequence[tuple[str, str]] | None = None,
    scratch_dir: str | None = None,
    scratch_private: str | None = None,
    profile_home: tuple[str, str] | None | object = _UNRESOLVED,
    sandbox_root: str | None = None,
) -> list[str]:
    """Build the bwrap argv prefix; the caller appends the shell argv after the trailing ``--``.

    All arguments are fixed at environment construction except *tracked_cwd*,
    which only sets ``--chdir``. *hidden_paths*, *home_root* (the real HOME
    the default-deny layout is built over, None for no layout), *home_allow*
    (the allowlist units), *ca_bundles* ((source, destination) pairs
    from ca_bundle_binds), *scratch_dir* and *profile_home* ((source,
    destination) of the profile home bind, None for none) are what
    BubblewrapEnvironment resolved at construction; when omitted they are
    resolved from *home*, *hermes_home* and the process environment on this
    call, with no PATH and no operator items, which suits tests of the pure
    builder only. *scratch_private* is the environment's own scratch dir
    (private_scratch_path), mounted at *scratch_dir*; without it nothing of
    the scratch dir is bound. *sandbox_root*, when given, is hidden but for
    the state dir (the environment passes it when no hidden path covers
    it). The listing of the top of HOME is the one input read from the host
    at each call, so a directory made on the host later shows in the next
    spawn.
    """
    profile = resolve_profile(config.profile)
    if hidden_paths is None:
        hidden_paths = sensitive_paths(home, hermes_home)
    # Resolved first: get_scratch_dir makes the directory, and with it HOME
    # when HERMES_HOME lies under a HOME that does not exist yet.
    if scratch_dir is None:
        scratch_dir = resolve_scratch_dir(hermes_home)
    if home_root is _UNRESOLVED:
        home_root = bubblewrap_home.resolve_home_root(home)
    if home_allow is None and home_root is not None:
        home_allow = bubblewrap_home.resolve_allowlist(
            home_root, "", (),
            denied_names=bubblewrap_home.denied_home_names(home_root), denied_paths=tuple(hidden_paths),
        )
    if profile_home is _UNRESOLVED:
        profile_home = None
        if config.home_mode in PROFILE_HOME_MODES:
            path = os.path.join(os.path.abspath(os.path.expanduser(hermes_home)), "home")
            if os.path.isdir(path):
                profile_home = (os.path.realpath(path), path)
    if ca_bundles is None:
        writable = [initial_cwd] if profile.writable_cwd else []
        writable += [bind.src for bind in config.binds if not bind.readonly]
        writable += [scratch_dir, os.path.dirname(state_dir)]
        guard = bind_guard(
            hidden_paths, hermes_homes(home, hermes_home), writable,
            home_root, home_allow or (),  # type: ignore[arg-type]
        )
        ca_bundles = ca_bundle_binds(guard)

    argv: list[str] = [
        bwrap_path,
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--unshare-cgroup-try",
    ]
    if profile.share_net:
        argv.append("--share-net")
    for name in HOST_SOCKET_VARS:
        argv += ["--unsetenv", name]

    argv += [
        "--ro-bind", "/", "/",
        "--dev", "/dev",
        "--proc", "/proc",
        "--tmpfs", "/tmp",
    ]
    argv += runtime_overlay_args(state_dir, os.getuid())  # windows-footgun: ok — bwrap is Linux-only; never reached on Windows

    # The cwd is always bound at its own path so --chdir resolves even when
    # it sits under the masked /tmp; the profile decides whether it is
    # writable. The -try form skips the bind when the directory is gone
    # from the host, so LocalEnvironment's cwd recovery (a parent dir on
    # the read-only root) keeps commands running instead of every spawn
    # failing on a missing bind source.
    # Dests are emitted as given: BubblewrapEnvironment resolved them at
    # construction, and a realpath here would let a symlink planted under a
    # dest between spawns move the mount.
    binds = filter_binds(config.binds, hidden_paths)
    mounts: list[tuple[str, str, str]] = [
        ("--bind-try" if profile.writable_cwd else "--ro-bind-try", initial_cwd, initial_cwd),
    ]
    mounts += [("--ro-bind" if bind.readonly else "--bind", bind.src, bind.dest) for bind in binds]
    writable = [(initial_cwd, initial_cwd)] if profile.writable_cwd else []
    writable += [(bind.src, bind.dest) for bind in binds if not bind.readonly]
    mount_points = [initial_cwd, *(bind.dest for bind in binds)]

    # A bind that puts another directory over HOME (or over a parent of it)
    # replaces HOME on purpose. The layout would put the host entries back
    # on top of it, so it stands down and the plain overlays apply.
    if home_root is not None and home_replaced_by_bind(home_root, binds):  # type: ignore[arg-type]
        home_root = None

    late = _late_mounts(
        profile, state_dir,
        scratch_dir=scratch_dir, scratch_private=scratch_private, ca_bundles=ca_bundles,
        profile_home=profile_home,  # type: ignore[arg-type]
        sandbox_root=sandbox_root,
    )

    if home_root is None:
        for mount in mounts:
            argv += mount
        # Pin the parents of the hidden paths that sit inside a writable bind
        # (see ancestor_pin_args) after those binds, so the pins land on top
        # of them, and before the overlays, so the overlays land on the pins.
        argv += ancestor_pin_args(writable, mount_points, hidden_paths)
        # The overlays come after the cwd, operator binds and pins so a bind
        # of HOME itself still hides what sits under it, and before the late
        # mounts so those stay reachable under a hidden HERMES_HOME.
        argv += sensitive_overlay_args(hidden_paths, state_dir)
        for mount in late:
            argv += mount
    else:
        root: str = home_root  # type: ignore[assignment]

        def in_home(path: str) -> bool:
            return path != root and _is_within(path, root)

        # Listed once per spawn and shared by the pins and the layout, so
        # both see the same entries.
        try:
            listing = sorted(os.listdir(root))
        except OSError:
            listing = []
        # A writable bind that covers HOME makes the non-dot entries
        # writable and nothing else under HOME: the dot entries are
        # read-only or hidden, and the top of HOME is a read-only tmpfs.
        # So inside HOME the pins are computed against those entries, and
        # the covering bind itself only pins parents of hidden paths that
        # lie outside HOME.
        covering = [pair for pair in writable if _is_within(root, pair[1])]
        plain = [pair for pair in writable if pair not in covering]
        hidden_in = [path for path in hidden_paths if in_home(path)]
        hidden_out = [path for path in hidden_paths if not in_home(path)]
        pins = ancestor_pin_args(plain, mount_points, hidden_paths)
        pins += ancestor_pin_args(covering, mount_points, hidden_out)
        if covering:
            entries = [os.path.join(root, name) for name in listing if not name.startswith(".")]
            entries = [path for path in entries if os.path.isdir(path) and not os.path.islink(path)]
            pins += ancestor_pin_args([(path, path) for path in entries], [*mount_points, *entries], hidden_in)
        mounts += [(pins[i], pins[i + 1], pins[i + 2]) for i in range(0, len(pins), 3)]

        for mount in mounts:
            if not in_home(mount[2]):
                argv += mount
        argv += bubblewrap_home.home_layout_args(
            root,
            tuple(home_allow or ()),
            hidden_paths,
            empty_file=empty_file_path(state_dir),
            writable_roots=[dest for _src, dest in covering],
            listing=listing,
            binds=[mount for mount in mounts if in_home(mount[2])],
            late_args=[token for mount in late if in_home(mount[-1]) for token in mount],
        )
        # The overlays for hidden paths outside HOME (a HERMES_HOME kept
        # elsewhere, a credential directory that is a symlink out of HOME).
        argv += sensitive_overlay_args(hidden_out, state_dir)
        for mount in late:
            if not in_home(mount[-1]):
                argv += mount

    argv += ["--chdir", tracked_cwd, "--"]
    return argv


def _usable_dir(path: str) -> bool:
    return os.path.isdir(path) and os.access(path, os.R_OK | os.X_OK)


def default_host_cwd() -> str:
    """Where commands start when Hermes names no working directory.

    The process cwd, as for the local backend, unless it is / or lies under a
    path this backend hides (a gateway started from its checkout under
    ~/.hermes, say): no command could run there. Then the real home
    directory, which is what the gateway picks for the local backend.
    """
    home = os.path.realpath(get_real_home() or os.path.expanduser("~"))
    hidden = sensitive_paths(home, str(get_hermes_home()))
    try:
        cwd = os.path.realpath(os.getcwd())
    except OSError:
        return home
    if cwd != os.sep and _usable_dir(cwd) and not any(_is_within(cwd, root) for root in hidden):
        return cwd
    return home


# terminal.cwd values that mean "the directory Hermes runs in" (core's placeholders).
CWD_PLACEHOLDERS: frozenset[str] = frozenset({"", ".", "auto", "cwd"})


def writable_root_for(configured_cwd: str) -> str:
    """The writable root from configuration: ``terminal.cwd``, else :func:`default_host_cwd`.

    Only configuration decides what is writable, never where a session has
    ``cd``'d to: Hermes recreates environments (after the idle timeout, for
    the file tools, for a delegated child) from the session's recorded cwd,
    and taking that as the root would let a model's ``cd ~`` make the whole
    home directory writable on the next recreation. A configured path is kept
    as given even when it is missing: the environment then runs in its nearest
    existing parent, read-only, rather than somewhere writable.
    """
    value = (configured_cwd or "").strip()
    if value in CWD_PLACEHOLDERS:
        return default_host_cwd()
    return os.path.abspath(os.path.expanduser(value))


def start_cwd_for(cwd: str, root: str) -> tuple[str, str | None]:
    """Where the environment starts for the *cwd* Hermes passed, and the value to map.

    A host directory is kept (a session that ``cd``'d elsewhere resumes there,
    read-only outside the root). Anything else is not a place on this host a
    command can start in (the ``/root`` container default Hermes before
    0.22.3 gives every plugin backend, a deleted directory): start at *root*
    and return the passed value so commands that keep arriving with it are
    mapped to the tracked cwd.
    """
    expanded = os.path.expanduser(cwd) if cwd else ""
    if expanded and os.path.isabs(expanded) and _usable_dir(expanded):
        return expanded, None
    if cwd:
        logger.warning("bubblewrap: working directory %r is not a directory on this host; starting in %s", cwd, root)
    return root, cwd or None


PROBE_ARGS: tuple[str, ...] = ("--unshare-user", "--ro-bind", "/", "/", "true")
PROBE_TIMEOUT_SECONDS = 5
INSTALL_HINT = (
    "Install the bubblewrap package (apt, dnf or pacman: bubblewrap) and "
    "util-linux (for prlimit), and make sure unprivileged user namespaces are "
    "allowed on this host, then retry; or set terminal.backend to another backend."
)

# Path of the bwrap that passed the runtime probe, kept for the life of the
# process. A failed probe is not cached: the next construction probes again,
# so installing or fixing bwrap needs no restart.
_probed_bwrap_path: str | None = None


def run_probe() -> tuple[str | None, str | None]:
    """Run the bwrap probe once: ``(path, None)`` on success, ``(path, failure)`` otherwise.

    The probe is ``bwrap --unshare-user --ro-bind / / true`` with a
    5 s timeout: it fails where user namespaces are disabled, where bwrap
    is not setuid on a kernel that needs it, or where bwrap is missing.
    prlimit(1), which applies the resource limits, must be on PATH too.
    """
    path = shutil.which("bwrap")
    if path is None:
        return None, "bubblewrap (bwrap) is not on PATH"
    if shutil.which("prlimit") is None:
        return path, "prlimit (util-linux) is not on PATH"
    try:
        result = subprocess.run(
            [path, *PROBE_ARGS],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return path, f"bwrap probe timed out after {PROBE_TIMEOUT_SECONDS} s: {path} {' '.join(PROBE_ARGS)}"
    except OSError as exc:
        return path, f"bwrap probe could not start: {exc}"
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no output"
        return path, f"bwrap probe failed (exit {result.returncode}): {detail}"
    return path, None


def probe_bwrap() -> str:
    """Return the path of a bwrap that passed the probe, probing once per process.

    Raises EnvironmentConnectionError, with a retry hint naming the
    bubblewrap package, when bwrap is missing from PATH or the probe fails.
    """
    global _probed_bwrap_path
    if _probed_bwrap_path is None:
        path, failure = run_probe()
        if failure is not None:
            raise EnvironmentConnectionError(
                f"bubblewrap backend unavailable: {failure}",
                retry_hint=INSTALL_HINT,
            )
        _probed_bwrap_path = path
    return _probed_bwrap_path


def uid_thread_count(uid: int) -> int:
    """Threads owned by *uid* host-wide, counted from /proc.

    RLIMIT_NPROC counts threads, not processes (getrlimit(2)), and a desktop
    uid runs several threads per process.
    """
    count = 0
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        proc = os.path.join("/proc", name)
        try:
            if os.stat(proc).st_uid == uid:
                count += len(os.listdir(os.path.join(proc, "task")))
        except OSError:
            continue
    return count


# Processes the wrapper itself forks before the command runs: bwrap, its
# pid-1 init, the bash that runs the command, and that shell's command
# substitutions and mktemp for the snapshot; a python3 under execute_code
# counts too. RLIMIT_NPROC is checked against the uid's live thread count
# at each fork, so without headroom a tight max_procs (or host threads
# started between the /proc scan and the fork) fails the spawn before the
# command starts: "bwrap: Can't fork for pid 1: Resource temporarily
# unavailable".
WRAPPER_PROCESS_ALLOWANCE = 16


def rlimit_values(config: BubblewrapConfig, *, uid_threads: int) -> dict[int, int]:
    """The rlimits a spawn gets from the three terminal.bubblewrap_* keys.

    A key at 0 leaves its limit out. RLIMIT_NPROC is counted per uid
    host-wide, and a bwrap user namespace does not change that (kernel
    6.8: with the limit below the uid's thread count bwrap cannot even
    create its namespace), so max_procs is applied on top of the uid's
    current thread count: it bounds what the sandbox may add, and the
    documented default of 256 keeps working on a desktop that already runs
    more. WRAPPER_PROCESS_ALLOWANCE is added on top of that for the
    wrapper's own processes, so max_procs is what the command may add,
    not what the command and the wrapper share.
    """
    limits: dict[int, int] = {}
    if config.memory_mb:
        limits[resource.RLIMIT_AS] = config.memory_mb * 1024 * 1024
    if config.cpu_seconds:
        limits[resource.RLIMIT_CPU] = config.cpu_seconds
    if config.max_procs:
        limits[resource.RLIMIT_NPROC] = uid_threads + config.max_procs + WRAPPER_PROCESS_ALLOWANCE
    return limits


PRLIMIT_FLAGS: dict[int, str] = {
    resource.RLIMIT_AS: "--as", resource.RLIMIT_CPU: "--cpu", resource.RLIMIT_NPROC: "--nproc",
}


def prlimit_args(limits: Mapping[int, int], prlimit_path: str) -> list[str]:
    """The prlimit(1) argv prefix applying *limits* as soft and hard, or [] when empty.

    prlimit sets each limit on itself and execs the command that follows,
    so the limits are in place before bwrap starts and no Python code runs
    between fork and exec. It stops parsing at the first non-option
    argument, so the bwrap argv follows without a separator. A value above
    the inherited hard limit is clamped to it: raising a hard limit needs
    CAP_SYS_RESOURCE and would fail the spawn.
    """
    if not limits:
        return []
    argv = [prlimit_path]
    for res, value in limits.items():
        _, hard = resource.getrlimit(res)
        if hard != resource.RLIM_INFINITY:
            value = min(value, hard)
        argv.append(f"{PRLIMIT_FLAGS[res]}={value}")
    return argv


_MOUNT_ARITY: dict[str, int] = {
    "--bind": 2, "--ro-bind": 2, "--bind-try": 2, "--ro-bind-try": 2,
    "--tmpfs": 1, "--dev": 1, "--proc": 1,
}
# Directives that take path operands but place no directory tree: their
# operands are stepped over so they are not read as flags.
_SKIPPED_ARITY: dict[str, int] = {"--symlink": 2, "--remount-ro": 1}


def masked_inside(argv: Sequence[str], path: str) -> bool:
    """True when host directory *path* is hidden inside the sandbox *argv* builds.

    Mounts stack in argv order, so the last directive whose destination is
    *path* or an ancestor of it decides what shows there: a mount root is
    visible (bwrap creates the mount point), a fresh --tmpfs, --dev or
    --proc hides everything below its root, and a bind shows what its
    source holds at the same relative path (the root bind of / shows the
    host itself). A mount below *path* makes *path* exist. A -try bind
    whose source is missing is skipped, as bwrap skips it. Reads only the
    host presence of directories, as the pins do.
    """
    visible = True
    i = 0
    while i < len(argv):
        arity = _MOUNT_ARITY.get(argv[i])
        if arity is None:
            i += 1 + _SKIPPED_ARITY.get(argv[i], 0)
            continue
        operands = argv[i + 1:i + 1 + arity]
        i += 1 + arity
        dest = operands[-1]
        if dest == path:
            visible = True
        elif _is_within(dest, path):
            # A mount below *path* makes bwrap create every directory on
            # the way to it, *path* included.
            if arity == 2 and argv[i - 1 - arity].endswith("-try") and not os.path.exists(operands[0]):
                continue
            visible = True
        elif _is_within(path, dest):
            if arity == 1:
                visible = False
                continue
            src = operands[0]
            if argv[i - 1 - arity].endswith("-try") and not os.path.exists(src):
                continue
            visible = os.path.isdir(os.path.join(src, os.path.relpath(path, dest)))
    return not visible


def chdir_failed(result: Mapping[str, object], tracked_cwd: str) -> bool:
    """True when the result looks like bwrap failing to enter *tracked_cwd*.

    bwrap prints one line, ``Can't chdir to <dir>: ...``, and exits 1
    before the command starts; the wrapper then never prints the cwd
    marker (``cwd_observed`` stays unset). A command that ran and failed
    has the marker, and a timed-out one has the timeout note appended, so
    neither is a single bwrap line. A command can forge the shape by
    printing the line and replacing its shell, so the caller must not
    treat a match as proof that nothing ran: masked_inside decides that
    before the spawn, and this is only the backstop.
    """
    if result.get("returncode", 0) == 0 or result.get("cwd_observed"):
        return False
    lines = str(result.get("output", "")).strip().splitlines()
    return len(lines) == 1 and lines[0].startswith(f"bwrap: Can't chdir to {tracked_cwd}:")


# The pid file of Hermes core's background-job triplet (``hermes_bg_<session>``
# as .log/.pid/.exit in the environment's temp dir); core's own local backend
# matches the same name when sweeping stale artifacts.
_BG_PID_FILE_RE = re.compile(r"hermes_bg_[A-Za-z0-9_-]+\.pid")


def is_background_launch(command: str) -> bool:
    """True when *command* is Hermes core's background-job launch wrapper.

    Core starts a ``background=true`` job on a non-local backend by executing
    one wrapper command in the environment (``spawn_via_env`` in
    tools/process_registry.py): it nohups the command with its output going
    to the triplet's .log and echoes the child's pid out of the .pid file,
    which the launcher reads back as proof the job started. Poll, wait and
    kill of an already-started job reference the triplet too but run no
    nohup, so only the launch matches. Like chdir_failed this reads a shape
    core generates, never a user command: a foreground command would have to
    name another session's pid file and run nohup to be refused, and
    refusing it is the safe direction anyway.
    """
    return "nohup" in command and _BG_PID_FILE_RE.search(command) is not None


#: What execute() answers a background launch with. The text reaches the
#: model inside core's failed-launch error, so it names the backend, why
#: the job cannot live, and what to do instead. It must never contain a
#: line of digits alone: the launcher scans the output for the pid.
BACKGROUND_LAUNCH_REFUSAL = (
    "bubblewrap: background processes are not supported by this backend: "
    "each command runs in its own sandbox, which is torn down when the "
    "command returns, so a background job would die with the sandbox "
    "instead of running to completion. Run the command in the foreground, "
    "or use a backend that keeps processes."
)


# Directories the sandboxes this process built can write to, per Hermes home
# (real path): the working directories, read-write bind sources and profile
# homes. Sessions of one profile can run with different working
# directories (a session moved to a project), and the binds setting can
# change between constructions; a source one sandbox may read must not lie
# where another can swap it. Kept for the life of the process.
_WRITABLE_ROOTS: dict[str, set[str]] = {}
_WRITABLE_ROOTS_LOCK = threading.Lock()


class BubblewrapEnvironment(LocalEnvironment):
    """LocalEnvironment whose every spawn runs inside a bwrap sandbox.

    Bash resolution, the run env, missing-cwd recovery and process-group
    kill come from LocalEnvironment. This class adds the argv prefix (the
    prlimit limits, then bwrap), a per-instance state dir for the shell
    snapshot and cwd file, the empty file bound over sensitive files, and
    their removal on cleanup.
    """

    def __init__(
        self,
        cwd: str = "",
        timeout: int = 60,
        env: dict | None = None,
        *,
        config: BubblewrapConfig | None = None,
        replaced_cwd: str | None = None,
        root: str | None = None,
        session_key: str | None = None,
    ):
        # Without a config (direct construction), the defaults plus terminal.home_mode as core
        # exports it: that is what decides the subprocess HOME the overlays must leave reachable.
        self._config = load_bubblewrap_config({}, os.environ.get(ENV_HOME_MODE, "")) if config is None else config
        # The cwd Hermes asked for when it was not a host directory (Hermes
        # before 0.22.3 hands plugin backends /root); execute() maps it to the
        # tracked cwd, since commands keep arriving with it.
        self._replaced_cwd = replaced_cwd
        # The terminal task id the environment serves; it names the scratch dir
        # (private_scratch_path), which then outlives the environment.
        self._session_key = session_key
        # Reject an unknown profile and an unusable bwrap before anything is
        # created on disk; the probe raises EnvironmentConnectionError, which
        # the terminal tool turns into its degraded or error result.
        resolve_profile(self._config.profile)
        self._bwrap_path = probe_bwrap()
        self._prlimit_path = shutil.which("prlimit") or "prlimit"
        # The OS user's home anchors the sensitive set even when this process
        # runs with HOME pointed at the profile home. Every mount path is
        # taken through realpath: bwrap resolves a mount destination inside
        # the sandbox root, where an absolute symlink points nowhere.
        self._home = os.path.realpath(get_real_home() or os.path.expanduser("~"))
        self._hermes_home = os.path.realpath(str(get_hermes_home()))
        # Resolved once and kept for the life of the environment: the set
        # never follows a symlink swapped in later.
        self._hidden_paths = sensitive_paths(self._home, self._hermes_home)
        # HOME is default-deny for dot entries. What a sandbox may see is
        # fixed here too: a command cannot widen it by changing PATH.
        self._home_root = bubblewrap_home.resolve_home_root(self._home)
        self._home_allow: tuple[str, ...] = ()
        if self._home_root is not None:
            self._home_allow = bubblewrap_home.resolve_allowlist(
                self._home_root, os.environ.get("PATH", ""), self._config.home_allow,
                denied_names=bubblewrap_home.denied_home_names(self._home_root),
                denied_paths=self._hidden_paths,
            )
        self._scratch_dir: str | None = resolve_scratch_dir(self._hermes_home)
        # The operator binds are filtered, their sources expanded and their
        # destinations resolved once here, like the hidden set and the cwd,
        # so the mount paths are fixed for the life of the environment
        # and a dropped bind warns once; the builder's own filter
        # then drops nothing.
        self._config = replace(
            self._config,
            binds=tuple(resolve_bind_dests(expand_bind_srcs(filter_binds(self._config.binds, self._hidden_paths)))),
        )
        self._check_profile_home()
        # The mount paths are fixed here; only --chdir follows the tracked cwd.
        # *root* is the writable root (the bound cwd) and *cwd* only where the
        # tracked cwd starts; without a root they are the same directory.
        self._initial_cwd = os.path.realpath(_resolve_local_initial_cwd(root or cwd))
        self._check_initial_cwd()
        # Checked and recorded under one lock, so two environments built at
        # once each see the other's writable directories.
        with _WRITABLE_ROOTS_LOCK:
            own = self._own_writable_roots()
            self._writable = {
                **{path: "a directory another sandbox of this profile can write to"
                   for path in _WRITABLE_ROOTS.get(self._hermes_home, ())},
                **own,
            }
            self._check_bind_sources()
            sandbox_root = os.path.realpath(get_sandbox_dir())
            self._check_sandbox_root(sandbox_root)
            self._validate_host_binds(sandbox_root)
            _WRITABLE_ROOTS.setdefault(self._hermes_home, set()).update(own)
        # Hidden but for this environment's state dir, unless a hidden path
        # covers it already (the default HERMES_HOME/sandboxes).
        self._sandbox_root: str | None = (
            None if any(_is_within(sandbox_root, hidden) for hidden in self._hidden_paths) else sandbox_root
        )
        # BaseEnvironment.__init__ derives the snapshot and cwd file paths
        # from get_temp_dir() and LocalEnvironment.__init__ runs the login
        # bootstrap straight away, so the state dir must exist first.
        self._state_dir = os.path.join(sandbox_root, f"bwrap-{uuid.uuid4().hex[:12]}")
        os.makedirs(self._state_dir, mode=0o700)
        # Read-only and outside the state dir: nothing in a sandbox can
        # write to what shows at the hidden file paths.
        self._empty_file = empty_file_path(self._state_dir)
        os.close(os.open(self._empty_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o400))
        try:
            self._make_private_scratch()
        except BaseException:
            self._remove_state()
            raise
        start = self._initial_cwd
        if root and cwd:
            candidate = os.path.realpath(_resolve_local_initial_cwd(cwd))
            if os.path.isdir(candidate) and not masked_inside(self._bwrap_prefix(candidate), candidate):
                start = candidate
        try:
            super().__init__(cwd=start, timeout=timeout, env=env)
        except BaseException:
            self._remove_state()
            raise

    def _own_writable_roots(self) -> dict[str, str]:
        """Real paths this environment's sandboxes can write to, each with what makes it writable.

        The working directory under a writable profile (this environment's,
        and the configured terminal.cwd every session of the profile starts
        from), the read-write bind sources and the profile home under a
        profile home_mode. The shared scratch dir and the sandbox dir join
        where the bind guard is built.
        """
        roots: dict[str, str] = {}
        if resolve_profile(self._config.profile).writable_cwd:
            roots[self._initial_cwd] = "terminal.cwd"
            from tools.terminal_scope import terminal_env

            configured = os.path.realpath(writable_root_for(terminal_env("TERMINAL_CWD", "")))
            roots.setdefault(configured, "the configured terminal.cwd")
        for bind in self._config.binds:
            if not bind.readonly:
                roots.setdefault(_real_host_path(bind.src), "the read-write bind source")
        if self._config.home_mode in PROFILE_HOME_MODES:
            roots.setdefault(os.path.realpath(os.path.join(self._hermes_home, "home")), "the profile home")
        return roots

    def _validate_host_binds(self, sandbox_root: str) -> None:
        """Resolve and check every bind that configuration or the environment names, once.

        The operator binds, the profile home, the trust anchors of
        CA_BUNDLE_VARS and the Hermes scratch dir each go through
        bubblewrap_binds.bind_refusal, which returns the real path the argv
        then carries. A refused read-only operator bind or profile home
        stops the construction with a ValueError: skipped, the first would
        leave a path the operator meant to protect as writable as its
        surroundings, and the second is the subprocess HOME. The other
        refusals drop the bind with one warning. The writable set is every
        directory a sandbox of this profile can write to (self._writable),
        plus the shared scratch dir and the sandbox dir, where the scratch
        dirs and state dirs of every environment live.
        """
        writable = [*self._writable, *(path for path in (self._scratch_dir, sandbox_root) if path)]
        # The HOME layout stands down when a bind puts another directory over HOME (build_bwrap_args).
        home_root = self._home_root
        if home_root is not None and home_replaced_by_bind(home_root, self._config.binds):
            home_root = None
        guard = bind_guard(
            self._hidden_paths, hermes_homes(self._home, self._hermes_home), writable, home_root, self._home_allow,
        )
        kept: list[BindMount] = []
        for bind in self._config.binds:
            label = f"{_setting_name(SETTING_BINDS)} entry {bind.src} -> {bind.dest}"
            real, reason = bubblewrap_binds.bind_refusal(
                bind.src, guard, kind=bubblewrap_binds.ANY, dest=bind.dest, hidden_rules=False,
            )
            if reason is None:
                kept.append(replace(bind, src=real))
            elif bind.readonly:
                raise ValueError(
                    f"{label} is read-only, but the bubblewrap backend cannot bind it safely: {reason}. Bind it "
                    "from a directory no sandbox can write to, or at its own path (dest equal to src) directly "
                    "under terminal.cwd or a read-write bind source, or remove the entry."
                )
            else:
                logger.warning("bubblewrap: not binding %s: %s", label, reason)
        self._config = replace(self._config, binds=tuple(kept))
        self._profile_home_bind = self._validated_profile_home(guard)
        self._ca_bundles = ca_bundle_binds(guard)
        if self._scratch_dir:
            self._scratch_dir = bubblewrap_binds.validated_bind_source(
                "the Hermes scratch dir", self._scratch_dir, guard, kind=bubblewrap_binds.DIRECTORY,
                public=(os.path.join(self._hermes_home, "cache", "scratch"),),
            )

    def _validated_profile_home(self, guard: bubblewrap_binds.BindGuard) -> tuple[str, str] | None:
        """(source, destination) of the profile home bind under a profile home_mode, else None.

        Resolved once: every spawn binds the directory HERMES_HOME/home
        resolved to here, at HERMES_HOME/home, never the name. A missing
        profile home gets no bind. A refused one (a link to a directory a
        sandbox can write to, where a command could swap it for a link to
        another tree, or into a hidden dot entry of HOME) stops the
        construction like the other profile home checks
        (_check_profile_home). The plain directory lies in the hidden
        Hermes home and is the one public part of it.
        """
        if self._config.home_mode not in PROFILE_HOME_MODES:
            return None
        link = os.path.join(self._hermes_home, "home")
        if not os.path.exists(link):
            return None
        real, reason = bubblewrap_binds.bind_refusal(
            link, guard, kind=bubblewrap_binds.DIRECTORY, dest=link,
            public=() if os.path.islink(link) else (link,),
        )
        if reason is not None:
            raise ValueError(
                f"terminal.home_mode={self._config.home_mode} binds the profile home {link} read-write inside "
                f"the sandbox with the bubblewrap backend, but it cannot be bound safely: {reason}. Make "
                "HERMES_HOME/home a plain directory, or a link to a directory outside every hidden path and "
                "outside every directory a sandbox can write to, or set terminal.home_mode to auto or real."
            )
        return real, link

    def _make_private_scratch(self) -> None:
        """Make this environment's own scratch dir, which the sandbox mounts at the shared one.

        Also records every other spelling of the shared scratch dir that
        TMPDIR, TMP, TEMP and HERMES_SCRATCH_DIR can carry (Hermes exports
        them at boot), so that _wrap_command can point a command at the
        real path, where the mount is.
        """
        self._scratch_private: str | None = None
        self._scratch_names: tuple[str, ...] = ()
        if not self._scratch_dir:
            return
        private = private_scratch_path(self._scratch_dir, self._state_dir, self._session_key)
        try:
            os.mkdir(private, mode=0o700)
        except FileExistsError:
            # The session's dir from an earlier environment; anything else at
            # that path (a symlink bwrap would follow) gets no bind.
            if os.path.islink(private) or not os.path.isdir(private):
                logger.warning("bubblewrap: %s is not a plain directory; the sandbox gets no scratch dir", private)
                return
        self._scratch_private = private
        spellings = [
            self._scratch_dir,
            os.path.join(str(get_hermes_home()), "cache", "scratch"),
            *(os.environ.get(name, "") for name in (*SCRATCH_TMP_ENV_VARS, SCRATCH_DIR_MARKER_ENV)),
        ]
        self._scratch_names = tuple(dict.fromkeys(
            spelling for spelling in spellings
            if spelling and spelling != self._scratch_dir and os.path.realpath(spelling) == self._scratch_dir
        ))

    def _check_profile_home(self) -> None:
        """Refuse a profile home that holds a hidden path or lies under one.

        Under a profile home_mode the builder binds HERMES_HOME/home
        read-write on top of every overlay, and bwrap resolves the bind
        source on the host. When that path is a symlink to a tree holding
        a hidden path (HOME, HOME/.config, HERMES_HOME itself or a parent
        of it), or to a directory at or under one (HOME/.ssh), the bind is
        a second view of the same host tree and shows the secrets at
        HERMES_HOME/home, the shape filter_binds drops for operator binds.
        Only the plain directory
        HERMES_HOME/home is exempt from the lies-under test: it sits under
        HERMES_HOME, and under the standard profile layout under the hidden
        default HOME/.hermes as well, and binding it back is the
        designed carve-out. A symlinked profile home must resolve outside
        every hidden path: a link to another directory under HERMES_HOME,
        another profile at HOME/.hermes/profiles/<name> in the default
        layout included, is a second view of the hidden tree and is
        refused. A link to a clean
        directory outside every hidden path stays allowed.
        """
        if self._config.home_mode not in PROFILE_HOME_MODES:
            return
        link = os.path.join(self._hermes_home, "home")
        profile_home = os.path.realpath(link)
        plain = not os.path.islink(link)
        for hidden in self._hidden_paths:
            if _is_within(hidden, profile_home):
                relation = f"contains {hidden}"
            elif not plain and _is_within(profile_home, hidden):
                relation = f"lies under {hidden}"
            else:
                continue
            raise ValueError(
                f"terminal.home_mode={self._config.home_mode} binds the profile home "
                f"{link} read-write inside the sandbox with the bubblewrap backend, but "
                f"it resolves to {profile_home}, which {relation}, a path the backend "
                "hides: the bind would show it again. Make HERMES_HOME/home a plain "
                "directory, or a link to a directory outside every hidden path (HOME's "
                "hidden dotfiles, the default HOME/.hermes and all of HERMES_HOME), or "
                "set terminal.home_mode to auto or real."
            )

    def _check_initial_cwd(self) -> None:
        """Refuse a cwd of / or under a hidden path, warn about HOME: the cwd is the writable set."""
        cwd = self._initial_cwd.rstrip(os.sep) or os.sep
        if cwd == os.sep:
            raise ValueError(
                "terminal.cwd must not be / with the bubblewrap backend: the whole "
                "root would be writable inside the sandbox. Set terminal.cwd to a "
                "project or scratch directory."
            )
        # The overlays land after the cwd bind, so a cwd under a hidden path
        # is masked in every spawn and no command could run there. The profile
        # home is bound back on top of the HERMES_HOME overlay under
        # home_mode=profile, so a cwd under it is fine
        # when HERMES_HOME/home is a plain directory. The bind lands at the
        # link path when it is a symlink, so a cwd under a link target inside
        # HERMES_HOME (or another hidden path) stays masked and gets no
        # exemption; a link target outside every hidden path needs none.
        profile_home = os.path.join(self._hermes_home, "home")
        in_profile_home = (
            self._config.home_mode in PROFILE_HOME_MODES
            and not os.path.islink(profile_home)
            and _is_within(cwd, os.path.realpath(profile_home))
        )
        if not in_profile_home:
            for hidden in self._hidden_paths:
                if _is_within(cwd, hidden):
                    raise ValueError(
                        f"terminal.cwd {self._initial_cwd} lies under {hidden}, which the "
                        "bubblewrap backend hides inside every sandbox: no command could run "
                        "there. Set terminal.cwd to a project or scratch directory outside "
                        "HERMES_HOME and the hidden dotfiles (a checkout under ~/.hermes "
                        "needs to be launched from elsewhere or moved)."
                    )
        home = os.path.abspath(self._home).rstrip(os.sep) or os.sep
        if cwd == home or _is_within(home, cwd):
            logger.warning(
                "bubblewrap cwd %s covers the home directory: every existing non-dot "
                "entry of it is writable inside the sandbox, and no new entry can be made "
                "at the top of it (its dot entries are read-only or hidden). Set "
                "terminal.cwd to a project or scratch directory for a smaller writable set.",
                self._initial_cwd,
            )

    def _check_bind_sources(self) -> None:
        """Refuse a read-write bind whose source a sandbox could swap.

        bwrap resolves a bind source on the host at every spawn. When a
        component of the source path sits inside the writable set (the cwd
        under a writable profile, another read-write bind's source, the
        profile home under a profile home_mode) and is a symlink or a
        directory a command can rename, the command replaces it and
        chooses the next spawn's mount source, gaining read-write access
        to any host directory without a hidden path below it. A source
        bound at its own real path is a
        mount point inside, and the kernel refuses to rename or move a
        mount point from any path alias (EBUSY), so directly under a
        writable root the source is fixed: its parent is the root itself,
        a mount point (the cwd, a self-bound source) or a read-only
        directory entry (a source bound elsewhere, a symlinked profile
        home). One level deeper the parent is a plain writable directory:
        renamed, recreated and given a relative symlink at the source
        path, it steers the next spawn's mount. A read-only bind shows
        nothing the root bind does not.
        """
        writable = self._writable
        for bind in self._config.binds:
            if bind.readonly:
                continue
            given = os.path.abspath(os.path.expanduser(bind.src))
            real = os.path.realpath(given)
            link = _swappable_link(given, writable)
            if link is not None:
                problem = f"its source path goes through the symlink {link}, which a command could replace"
            else:
                inside = [
                    root for root in writable
                    if any(path != root and _is_within(path, root) for path in (given, real))
                ]
                if not inside or (bind.dest == real and os.path.dirname(real) in writable):
                    continue
                problem = (
                    f"its source lies inside {writable[inside[0]]} {inside[0]}, which is "
                    "writable inside the sandbox"
                )
            raise ValueError(
                f"plugins.entries.bubblewrap.settings.binds entry {bind.src} -> {bind.dest} is read-write "
                f"and {problem} with the bubblewrap backend: a command could swap the "
                "source and choose the next spawn's mount. Bind it read-write only at "
                "its own path (dest equal to src) directly under terminal.cwd, a "
                "read-write bind source or the profile home, with no symlink on the "
                "way: bound at its own path the source is a mount point, which no "
                "command can rename or move from any path; or make it read-only."
            )

    def _check_sandbox_root(self, sandbox_root: str) -> None:
        """Refuse a sandbox dir a sandbox could write to.

        The empty file bound over hidden files sits in the sandbox dir
        beside the state dir. Inside a writable bind a command could
        replace it with a symlink to a hidden file, and bwrap resolves the
        bind source on the next spawn, showing the whole secret. Under a
        hidden path (the default HERMES_HOME/sandboxes) the overlay covers
        it and nothing in a sandbox can reach it, unless a later bind lands
        on top of the overlay. The profile home under home_mode=profile
        is the one such bind, so a sandbox dir under it is refused
        first, whether the profile home is a directory under HERMES_HOME
        or a symlink to a directory outside every hidden path. An operator
        bind cannot re-expose the dir: filter_binds
        drops a source under a hidden path and a source containing one
        that maps elsewhere, and a source containing one at its own path
        sits below the overlay.
        """
        profile_home = os.path.realpath(os.path.join(self._hermes_home, "home"))
        if self._config.home_mode in PROFILE_HOME_MODES and _is_within(sandbox_root, profile_home):
            raise ValueError(
                f"terminal.sandbox_dir {sandbox_root} lies inside the profile home "
                f"{profile_home}, which terminal.home_mode={self._config.home_mode} binds "
                "read-write inside the sandbox with the bubblewrap backend: a command "
                "could replace the empty file bound over hidden files. Set "
                "terminal.sandbox_dir to a directory outside HERMES_HOME/home; the "
                "default HERMES_HOME/sandboxes is covered by the HERMES_HOME overlay."
            )
        if any(_is_within(sandbox_root, hidden) for hidden in self._hidden_paths):
            return
        for root, what in self._writable.items():
            if _is_within(sandbox_root, root):
                raise ValueError(
                    f"terminal.sandbox_dir {sandbox_root} lies inside {what} {root}, which is "
                    "writable inside the sandbox with the bubblewrap backend: a command "
                    "could replace the empty file bound over hidden files. Set "
                    "terminal.sandbox_dir to a directory outside terminal.cwd and "
                    "outside every read-write plugins.entries.bubblewrap.settings.binds source; the "
                    "default HERMES_HOME/sandboxes is covered by the HERMES_HOME overlay."
                )
        # Outside the hidden set the sandbox dir is hidden by a tmpfs on top
        # of the cwd and the operator binds, so it must hold none of them.
        held = [
            (what, path) for what, path in (
                ("terminal.cwd", self._initial_cwd),
                ("the home directory", self._home),
                *((f"the {_setting_name(SETTING_BINDS)} destination", bind.dest) for bind in self._config.binds),
            )
            if _is_within(path, sandbox_root)
        ]
        if held:
            what, path = held[0]
            raise ValueError(
                f"terminal.sandbox_dir {sandbox_root} holds {what} {path}. The bubblewrap backend "
                "hides the sandbox dir inside every sandbox, so that each one sees only its own state, "
                "and that would hide it too. Set terminal.sandbox_dir to a directory of its own; the "
                "default HERMES_HOME/sandboxes is covered by the HERMES_HOME overlay."
            )

    def get_temp_dir(self) -> str:
        return self._state_dir

    def execute(self, command: str, cwd: str = "", **kwargs) -> dict:
        """Run a command; report a tracked cwd that the sandbox mounts hide.

        A background launch (terminal(background=true), which core runs
        through this method as a nohup wrapper) is refused before anything
        spawns: the job would die with this command's sandbox, while the
        wrapper's pid echo would make core report a live, pollable process.
        The refusal result carries no pid-shaped line, so core reports the
        launch as failed with BACKGROUND_LAUNCH_REFUSAL as the reason.

        The tracked cwd is checked against the host (LocalEnvironment's
        missing-cwd recovery), not against what the overlays mask
        inside the sandbox. After ``cd`` into a host directory under a
        hidden path, or under the fresh /tmp, ``--chdir`` would fail on
        every later spawn before the shell runs, and no cwd marker could
        move the tracked cwd again. _reset_masked_cwd decides that from the
        fixed mount layout before the wrapper and the spawn are built and
        resets the tracked cwd to the initial cwd, so the command runs
        exactly once there; the note tells the caller. The chdir_failed
        backstop below only resets the tracked cwd and never re-runs the
        command: its shape can be forged by a command that prints the
        bwrap line and replaces its shell.
        """
        if is_background_launch(command):
            logger.warning("bubblewrap: refusing a background launch; this backend cannot keep processes")
            return {"output": BACKGROUND_LAUNCH_REFUSAL, "returncode": 1}
        if cwd and cwd == self._replaced_cwd:
            cwd = ""
        note = self._reset_masked_cwd()
        result = super().execute(command, cwd, **kwargs)
        if note is not None:
            result["output"] = note + result.get("output", "")
        elif self.cwd != self._initial_cwd and chdir_failed(result, self.cwd):
            stale = self.cwd
            logger.warning(
                "bubblewrap could not enter the tracked cwd %s; resetting the working "
                "directory to %s for the next command.",
                stale, self._initial_cwd,
            )
            self.cwd = self._initial_cwd
            result["output"] = (
                result.get("output", "")
                + f"\n[bubblewrap: could not enter working directory {stale}; reset to "
                f"{self._initial_cwd}, run the command again]"
            )
        return result

    def _bwrap_prefix(self, tracked_cwd: str) -> list[str]:
        return build_bwrap_args(
            self._config,
            self._initial_cwd,
            self._state_dir,
            self._home,
            self._hermes_home,
            tracked_cwd,
            bwrap_path=self._bwrap_path,
            hidden_paths=self._hidden_paths,
            home_root=self._home_root,
            home_allow=self._home_allow,
            ca_bundles=self._ca_bundles,
            scratch_dir=self._scratch_dir,
            scratch_private=self._scratch_private,
            profile_home=self._profile_home_bind,
            sandbox_root=self._sandbox_root,
        )

    def _reset_masked_cwd(self) -> str | None:
        """Reset a tracked cwd the mounts hide to the initial cwd; return the note.

        Runs before the command wrapper (which cd's to the tracked cwd) and
        the argv (whose --chdir carries it) are built. The initial cwd is
        never reset: it is bound at its own path. A tracked cwd gone from
        the host is not reset either: LocalEnvironment's recovery in
        _run_bash lands it on the nearest existing parent, as for the local
        backend, and when the mounts mask that parent the chdir_failed
        backstop in execute resets it on that spawn.
        """
        if self.cwd == self._initial_cwd or not os.path.isdir(self.cwd):
            return None
        if not masked_inside(self._bwrap_prefix(self.cwd), self.cwd):
            return None
        stale = self.cwd
        logger.warning(
            "bubblewrap: the tracked cwd %s is not visible inside the sandbox; resetting "
            "the working directory to %s.",
            stale, self._initial_cwd,
        )
        self.cwd = self._initial_cwd
        return (
            f"[bubblewrap: working directory {stale} is not visible inside the sandbox; "
            f"reset to {self._initial_cwd}]\n"
        )

    def _refresh_private_scratch(self) -> None:
        """Keep this environment's scratch dir in place for the next spawn.

        It is an entry of the Hermes scratch dir, whose idle entries any
        Hermes process prunes. A spawn marks it as used, and makes it again
        when a prune took it while the environment sat idle.
        """
        path = self._scratch_private
        if not path:
            return
        try:
            if os.path.islink(path):
                # bwrap would follow it; the sandbox goes without its scratch dir instead.
                logger.warning("bubblewrap: %s became a symlink; the sandbox no longer binds it", path)
                self._scratch_private = None
                return
            os.makedirs(path, mode=0o700, exist_ok=True)
            os.utime(path)
        except OSError:
            logger.debug("bubblewrap: could not refresh the scratch dir %s", path, exc_info=True)

    def _wrap_popen_args(self, args: list[str]) -> list[str]:
        self._refresh_private_scratch()
        return self._prlimit_prefix() + self._bwrap_prefix(self.cwd) + list(args)

    def _run_bash(self, cmd_string: str, *, login: bool = False, timeout: int = 120,
                  stdin_data: str | None = None) -> subprocess.Popen:
        """LocalEnvironment._run_bash with the shell argv wrapped in prlimit + bwrap.

        Every spawn goes through here, the login bootstrap that builds the
        shell snapshot included, so the user's rc files are sourced inside
        the sandbox too. The wrap happens after the cwd recovery so --chdir
        carries the recovered directory, not a deleted one. Nothing runs in
        the child between fork and exec: the limits ride the argv (prlimit).
        """
        bash = _find_bash()
        if login:
            cmd_string = _prepend_shell_init(cmd_string, _resolve_shell_init_files())
        args = [bash, *(["-l"] if login else []), "-c", cmd_string]
        self._recover_cwd()
        args = self._wrap_popen_args(args)
        proc = subprocess.Popen(
            args, text=True, env=_make_run_env(self.env), encoding="utf-8", errors="replace",
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
            start_new_session=True, cwd=self.cwd,
        )
        with contextlib.suppress(ProcessLookupError):
            proc._hermes_pgid = os.getpgid(proc.pid)
            from gateway.status import get_process_start_time

            proc._hermes_pgid_start = get_process_start_time(proc.pid)
        if stdin_data is not None:
            _pipe_stdin(proc, stdin_data)
        return proc

    def _extract_cwd_from_output(self, result: dict):
        """Keep the tracked cwd off / and out of this environment's state dir.

        execute_code runs its helper commands with cwd="/" and its script from
        a directory under the state dir, and every command reports where it
        ended up. Followed, the tracked cwd would land on / (read-only, and
        refused as a working directory by this backend) or in a directory that
        is deleted when the call ends, and relative paths in the file tools
        and the next --chdir would follow it there. The result still reports
        where the command ran.
        """
        prev_cwd = self.cwd
        super()._extract_cwd_from_output(result)
        if self.cwd != prev_cwd and (self.cwd == os.sep or _is_within(os.path.realpath(self.cwd), self._state_dir)):
            self.cwd = prev_cwd

    def _wrap_command(self, command: str, cwd: str) -> str:
        # --unsetenv strips the socket variables from the environment bwrap
        # receives, but the login bootstrap sources the shell init files
        # into the snapshot afterwards, and a 1Password or gpg-agent setup
        # exports SSH_AUTH_SOCK from ~/.bashrc. Unset them in front of the
        # command: that runs after the snapshot is sourced, and the export
        # dump that follows the command then omits them too.
        # Hermes points TMPDIR, TMP and TEMP of its processes at the shared
        # scratch dir, where this environment's own scratch dir is mounted.
        # A variable that names it by another spelling (through a symlink,
        # which may lead through a hidden path) is pointed at the real path;
        # one the operator set to another place is left alone, as Hermes
        # leaves it alone.
        retarget = ""
        if self._scratch_private and self._scratch_names and self._scratch_dir:
            real = shlex.quote(self._scratch_dir)
            spelled = "|".join(shlex.quote(path) for path in self._scratch_names)
            retarget = "".join(
                f'case "${{{name}-}}" in {spelled}) export {name}={real};; esac; '
                for name in (*SCRATCH_TMP_ENV_VARS, SCRATCH_DIR_MARKER_ENV)
            )
        return super()._wrap_command(f"unset {' '.join(HOST_SOCKET_VARS)}; {retarget}{command}", cwd)

    def _prlimit_prefix(self) -> list[str]:
        # uid_thread_count scans /proc once per spawn, and only when max_procs
        # is non-zero (the one limit that needs it). The count must be fresh:
        # the kernel checks RLIMIT_NPROC against the uid's live thread count
        # when the sandbox forks, so a count taken at construction goes stale
        # as the host starts threads, and a limit that falls below the live
        # count stops bwrap from creating its namespace at all.
        uid_threads = uid_thread_count(os.getuid()) if self._config.max_procs else 0  # windows-footgun: ok — Linux-only backend
        return prlimit_args(rlimit_values(self._config, uid_threads=uid_threads), self._prlimit_path)

    def _live_sandbox_pids(self) -> list[int]:
        """PIDs of this instance's bwrap wrappers still running.

        A wrapper is a direct child of this process whose argv is the bwrap
        path with this instance's state dir bound; the state dir is unique
        per instance and fixed at construction, so nothing from inside a
        sandbox can forge it. Zombies are left for the thread that spawned
        them to reap. A child that has not exec'd bwrap yet (between fork
        and exec, or still running prlimit) shows another cmdline and is
        missed; it then fails to bind the removed state dir and exits,
        exposing nothing.
        """
        me = os.getpid()
        bwrap = self._bwrap_path.encode()
        state_dir = self._state_dir.encode()
        pids: list[int] = []
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            try:
                with open(f"/proc/{name}/stat", "rb") as fh:
                    fields = fh.read().rsplit(b")", 1)[1].split()
                if fields[0] == b"Z" or int(fields[1]) != me:
                    continue
                with open(f"/proc/{name}/cmdline", "rb") as fh:
                    argv = fh.read().split(b"\0")
            except (OSError, IndexError, ValueError):
                continue
            if argv and argv[0] == bwrap and state_dir in argv:
                pids.append(int(name))
        return pids

    def _kill_live_sandboxes(self, wait: float = 2.0) -> None:
        """SIGKILL this instance's running bwrap wrappers.

        --die-with-parent takes the sandboxed tree down with each wrapper,
        background children included, since the wrapper's death kills the
        pid namespace's init.
        """
        for pid in self._live_sandbox_pids():
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)  # windows-footgun: ok — Linux-only backend, POSIX process groups
            except (ProcessLookupError, PermissionError):
                continue
        deadline = time.monotonic() + wait
        while self._live_sandbox_pids() and time.monotonic() < deadline:
            time.sleep(0.05)

    def _remove_state(self) -> None:
        shutil.rmtree(self._state_dir, ignore_errors=True)
        # A session's scratch dir stays for the environment Hermes makes again
        # for it; the pruning of idle scratch entries removes it later.
        private = getattr(self, "_scratch_private", None)
        if private and getattr(self, "_session_key", None) is None:
            shutil.rmtree(private, ignore_errors=True)
        try:
            os.unlink(self._empty_file)
        except OSError:
            pass

    def cleanup(self):
        self._kill_live_sandboxes()
        super().cleanup()
        self._remove_state()
