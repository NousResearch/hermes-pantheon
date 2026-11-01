"""Bubblewrap terminal backend for Hermes Agent (Linux).

    hermes plugins install bubblewrap
    hermes config set terminal.backend bubblewrap

Every ``terminal`` and ``execute_code`` command runs in its own bwrap sandbox on
this machine, at the same paths: read-only root, writable working directory,
credential directories hidden, private /tmp and /proc, resource limits. The
sandbox itself lives in ``sandbox.py``; this file is the provider Hermes talks
to and the system prompt note that tells the model where the boundary is.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from typing import Any, Callable, Dict, List, Optional, Tuple

from agent.terminal_env_provider import TerminalEnvironmentProvider

logger = logging.getLogger(__name__)

BACKEND_NAME = "bubblewrap"
PROMPT_SECTION_ID = "bubblewrap-sandbox"
SETTING_KEYS = ("profile", "binds", "memory_mb", "cpu_seconds", "max_procs", "home_allow", "hide")

_PROFILE_CHOICES = (
    ("restricted", "restricted - read-only working directory, no network"),
    ("workspace", "workspace - writable working directory, no network"),
    ("network", "network - writable working directory, host network (default)"),
)


def _sandbox():
    # sandbox.py imports ``resource`` and LocalEnvironment; load it only when the backend is used,
    # since plugin discovery imports this file on every ``hermes`` invocation.
    from . import sandbox

    return sandbox


def _file_tools_bypass_sandbox() -> bool:
    """Whether Hermes' file tools skip this sandbox and touch files from the Hermes process itself.

    Core gives ``read_file``, ``write_file``, ``patch`` and ``search_files`` an in-process path (Python
    reads and writes, ripgrep spawned directly) on any POSIX ``LocalEnvironment``, which
    ``BubblewrapEnvironment`` is; ``HERMES_NATIVE_FILE_READ`` off sends every file operation through
    ``env.execute``, so into the sandbox (``ShellFileOperations._native_read_enabled`` in
    ``tools/file_operations.py``, same parse).
    """
    return os.environ.get("HERMES_NATIVE_FILE_READ", "1").strip().lower() not in ("0", "false", "no", "off")


def _terminal_env(name: str, default: str = "") -> str:
    """Read a terminal setting through the per-turn terminal scope (the routed profile's under a multiplexed gateway)."""
    from tools.terminal_scope import terminal_env

    return terminal_env(name, default)


class BubblewrapProvider(TerminalEnvironmentProvider):
    """``terminal.backend: bubblewrap``: host paths, every command boxed by bwrap."""

    name = BACKEND_NAME
    display_name = "Bubblewrap"
    # Commands run on this host at the host's own paths, so Hermes keeps its host hints, host
    # path resolution for the file tools, and the dangerous-command approval prompts: the cwd and
    # any read-write bind are real host directories, so the sandbox is not disposable.
    is_remote = False
    is_container = False

    def __init__(self, get_config: Optional[Callable[..., Any]] = None,
                 set_config: Optional[Callable[[str, Any], None]] = None) -> None:
        self._get_config = get_config
        self._set_config = set_config

    @property
    def description(self) -> str:
        return "Run every command in a bwrap sandbox on this machine (read-only root, secrets hidden)."

    @property
    def env_description(self) -> str:
        return "this machine, inside a bubblewrap sandbox"

    @property
    def default_cwd(self) -> Optional[str]:
        # Host paths are the sandbox's paths: start where the local backend would.
        return _sandbox().default_host_cwd()

    # -- settings --
    def settings(self) -> Dict[str, Any]:
        """The plugin's settings (``plugins.entries.bubblewrap.settings``), read now so each profile gets its own."""
        if self._get_config is None:
            return {}
        return {key: self._get_config(key, None) for key in SETTING_KEYS}

    def load_config(self):
        return _sandbox().load_bubblewrap_config(self.settings(), _terminal_env("TERMINAL_HOME_MODE", ""))

    # -- availability --
    def is_available(self) -> bool:
        return sys.platform.startswith("linux") and bool(shutil.which("bwrap")) and bool(shutil.which("prlimit"))

    def check_requirements(self, config: dict[str, Any]) -> bool:
        if not sys.platform.startswith("linux"):
            logger.error("bubblewrap backend unavailable: bwrap sandboxes need Linux")
            return False
        _path, failure = _sandbox().run_probe()
        if failure:
            logger.error("bubblewrap backend unavailable: %s", failure)
        return failure is None

    def probe(self) -> Tuple[str, str]:
        if not sys.platform.startswith("linux"):
            return ("unavailable", "Bubblewrap needs Linux.")
        if not shutil.which("bwrap"):
            return ("needs_setup", "bwrap not found on PATH: install the bubblewrap package.")
        try:
            _path, failure = _sandbox().run_probe()
        except Exception as exc:
            return ("unavailable", f"bwrap probe failed: {exc}")
        return ("ready", "") if failure is None else ("needs_setup", failure)

    def doctor_checks(self) -> List[Tuple[bool, str, str]]:
        bwrap = shutil.which("bwrap")
        if not bwrap:
            return [(False, "bwrap", "(not found: install the bubblewrap package, apt/dnf/pacman: bubblewrap)")]
        try:
            ver = subprocess.run([bwrap, "--version"], capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=5)
            version = (ver.stdout or ver.stderr).strip() or "version unknown"
        except (OSError, subprocess.TimeoutExpired):
            version = "version unknown"
        rows = [(True, "bwrap", f"(found: {version})")]
        try:
            _path, failure = _sandbox().run_probe()
        except Exception as exc:
            failure = f"probe could not run: {exc}"
        rows.append((failure is None, "bwrap sandbox probe",
                     "(ok)" if failure is None else f"({failure}; allow unprivileged user namespaces for this user)"))
        return rows

    def setup_instructions(self) -> List[str]:
        return [
            "Every command runs inside a bwrap sandbox on this machine: read-only root, fresh /tmp,",
            "SSH keys, cloud credentials and the Hermes home hidden, memory, CPU and process limits per command.",
            "The working directory is the writable set: point terminal.cwd at a project or scratch",
            "directory rather than your home directory.",
            "Profile: hermes config set plugins.entries.bubblewrap.settings.profile network|workspace|restricted",
        ]

    def post_setup(self) -> None:
        if self._set_config is None:
            return
        from hermes_cli.setup import prompt_choice

        names = [name for name, _label in _PROFILE_CHOICES]
        current = str((self._get_config("profile", None) if self._get_config else None) or "network")
        default_idx = names.index(current) if current in names else names.index("network")
        idx = prompt_choice("Bubblewrap profile:", [label for _name, label in _PROFILE_CHOICES], default_idx)
        profile = names[idx] if 0 <= idx < len(names) else names[default_idx]
        self._set_config("profile", profile)
        print(f"Bubblewrap profile: {profile}")

    # -- the environment --
    def create_environment(self, *, cwd: str, timeout: int, task_id: str = "default", image: Optional[str] = None,
                           container_config: Optional[dict[str, Any]] = None, **kwargs: Any):
        sandbox = _sandbox()
        # The writable root comes from terminal.cwd (or where Hermes runs), never from *cwd*: Hermes
        # passes the session's recorded cwd, which follows the model's own `cd`.
        root = sandbox.writable_root_for(_terminal_env("TERMINAL_CWD", ""))
        start, replaced = sandbox.start_cwd_for(cwd, root)
        return sandbox.BubblewrapEnvironment(cwd=start, timeout=timeout, config=self.load_config(),
                                             replaced_cwd=replaced, root=root, session_key=task_id)

    # -- the model's view --
    def prompt_note(self, _session_info: Any = None) -> str:
        """Confinement notice for a new session on this backend; empty on any other backend.

        Without it a ``Read-only file system`` refusal or a missing ``~/.ssh`` reads as a broken host,
        and the model detours (sudo, another path, another tool) instead of reporting the boundary.
        """
        if (_terminal_env("TERMINAL_ENV", "local") or "local").strip().lower() != BACKEND_NAME:
            return ""
        sandbox = _sandbox()
        try:
            config = self.load_config()
            profile = sandbox.resolve_profile(config.profile)
        except ValueError:
            config, profile = None, sandbox.resolve_profile(sandbox.DEFAULT_PROFILE)
        extra = []
        if config is not None:
            extra += [bind.dest for bind in config.binds if not bind.readonly]
            if config.home_mode in sandbox.PROFILE_HOME_MODES:
                extra.append("the profile home ($HOME)")
        if profile.writable_cwd:
            writable = ", ".join(["the working directory", "the scratch directory ($TMPDIR)", *extra])
        elif extra:
            writable = ", ".join(extra) + " (the working directory is read-only under this profile)"
        else:
            writable = "nothing (the working directory is read-only under this profile)"
        network = "the host network" if profile.share_net else "no network (loopback only)"
        if _file_tools_bypass_sandbox():
            file_tools = (
                "`read_file`, `write_file`, `patch` and `search_files` do not run in this sandbox: Hermes performs them "
                "itself on the host, with the host user's permissions and its own file-safety rules, so they can read "
                "and change paths that are hidden or read-only for commands. The scratch directory ($TMPDIR) is not "
                "the same directory for them: a file a command writes there is not visible to `read_file`, and one "
                "`write_file` puts there is not visible to commands, so hand files over through the working directory."
            )
        else:
            file_tools = (
                "`read_file`, `write_file`, `patch` and `search_files` run through this sandbox too, so the same paths "
                "are hidden and read-only for them."
            )
        return (
            f"Terminal sandbox: bubblewrap, profile `{profile.name}`. Every `terminal` and `execute_code` call runs "
            f"in its own bwrap sandbox on this host with the same paths: the filesystem is read-only except {writable}; "
            "dot entries of the home directory that are not on the allowlist do not exist, the Hermes home is hidden, "
            "and credential paths below a visible entry appear empty; the Hermes scratch directory ($TMPDIR) is this "
            "sandbox's own and shows none of the files other sessions keep there; /tmp and /proc are private to "
            f"each command; it has {network}. A `Read-only file system` error, a missing or empty credential path or a "
            "refused connection is that boundary, not a broken host: report it instead of working around it. "
            "Background processes are not available on this backend: each command's sandbox ends with it. "
            f"{file_tools}"
        )


def register(ctx) -> None:
    """Plugin entry point."""
    provider = BubblewrapProvider(get_config=ctx.get_config, set_config=ctx.set_config)
    ctx.register_terminal_environment_provider(provider)
    ctx.register_system_prompt_section(PROMPT_SECTION_ID, provider.prompt_note)


__all__ = ["BubblewrapProvider", "register"]
