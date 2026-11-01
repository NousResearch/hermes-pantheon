"""The ``dispatch`` terminal backend provider: settings, health checks and environment creation."""

from __future__ import annotations

import logging
import os
import shlex
import shutil
from typing import Any, Callable, Optional

from agent.terminal_env_provider import TerminalEnvironmentProvider

logger = logging.getLogger(__name__)

BACKEND_NAME = "dispatch"
SETTING_KEYS = ("program", "image", "disposable", "sync_files", "spawn_timeout")
_SETTINGS_PATH = f"plugins.entries.{BACKEND_NAME}.settings"


def parse_program(value: Any) -> list[str]:
    """``program`` as an argv: a list is used as given, a string is split like a shell would.

    ``~`` in the first word expands, so ``~/.hermes/dispatch.sh`` works from any cwd."""
    if isinstance(value, (list, tuple)):
        argv = [str(part) for part in value if str(part)]
    elif isinstance(value, str) and value.strip():
        argv = shlex.split(value, posix=os.name != "nt")
    else:
        return []
    if argv:
        argv[0] = os.path.expanduser(argv[0])
    return argv


def program_missing_reason(argv: list[str]) -> Optional[str]:
    """Why *argv* cannot start, or None. Cheap: a PATH lookup and a stat, no process."""
    if not argv:
        return (f"no dispatch program configured: hermes config set {_SETTINGS_PATH}.program "
                "/path/to/dispatch.sh")
    exe = argv[0]
    if os.sep in exe or (os.altsep and os.altsep in exe):
        if not os.path.isfile(exe):
            return f"dispatch program {exe} does not exist"
        if os.name != "nt" and not os.access(exe, os.X_OK):
            return f"dispatch program {exe} is not executable (chmod +x {shlex.quote(exe)})"
        return None
    return None if shutil.which(exe) else f"dispatch program {exe} is not on PATH"


class DispatchProvider(TerminalEnvironmentProvider):
    """Bring your own sandbox: a program you write creates, runs commands in, pauses and deletes it."""

    # Commands run in a machine the program provides, never on this host: host OS/home/cwd hints are
    # suppressed and host-looking cwds are sanitized. Approval prompts stay on unless the user declares
    # the sandbox disposable, since nothing stops a dispatch program from running commands on the host.
    is_remote = True
    is_container = True
    supports_streams = True

    def __init__(self, get_config: Optional[Callable[..., Any]] = None) -> None:
        self._get_config = get_config

    @property
    def name(self) -> str:
        return BACKEND_NAME

    @property
    def display_name(self) -> str:
        return "Dispatch program"

    def settings(self) -> dict[str, Any]:
        """``plugins.entries.dispatch.settings``, read at call time so each profile gets its own."""
        if self._get_config is None:
            return {}
        return {key: self._get_config(key, None) for key in SETTING_KEYS}

    def program(self) -> list[str]:
        return parse_program(self.settings().get("program"))

    @property
    def description(self) -> str:
        return "Your own sandbox: a dispatch program creates it and runs each command in it."

    @property
    def env_description(self) -> str:
        return "a sandbox started by your dispatch program (likely Linux)"

    @property
    def default_cwd(self) -> Optional[str]:
        return "~"

    @property
    def skip_container_guards(self) -> bool:
        return bool(self.settings().get("disposable"))

    @property
    def cache_path_base(self) -> Optional[str]:
        return "~/.hermes" if self.settings().get("sync_files", True) is not False else None

    def is_available(self) -> bool:
        return program_missing_reason(self.program()) is None

    def check_requirements(self, config: dict[str, Any]) -> bool:
        reason = program_missing_reason(self.program())
        if reason:
            logger.error("dispatch backend selected but %s", reason)
        return reason is None

    def probe(self) -> tuple[str, str]:
        reason = program_missing_reason(self.program())
        return ("ready", "") if reason is None else ("needs_setup", reason[0].upper() + reason[1:] + ".")

    def setup_instructions(self) -> list[str]:
        argv = self.program()
        reason = program_missing_reason(argv)
        lines = [f"Dispatch program: {shlex.join(argv)}" if argv else "No dispatch program configured yet."]
        if reason:
            lines.append(reason[0].upper() + reason[1:] + ".")
        lines.append(f"Set it with: hermes config set {_SETTINGS_PATH}.program /path/to/dispatch.sh "
                     "(Docker and Kubernetes examples ship in the plugin's examples/ directory).")
        return lines

    def doctor_checks(self) -> list[tuple[bool, str, str]]:
        argv = self.program()
        reason = program_missing_reason(argv)
        return [(reason is None, "Dispatch program", f"({shlex.join(argv)})" if reason is None else f"({reason})")]

    def create_environment(self, *, cwd: str, timeout: int, task_id: str = "default",
                           image: Optional[str] = None, container_config: Optional[dict[str, Any]] = None,
                           **kwargs: Any):
        from .dispatch_env import DispatchEnvironment, resolve_image

        settings = self.settings()
        argv = self.program()
        reason = program_missing_reason(argv)
        if reason:
            raise RuntimeError(f"terminal.backend is dispatch but {reason}")
        cc = container_config or {}
        return DispatchEnvironment(
            program=argv, cwd=cwd, timeout=timeout, task_id=task_id,
            image=resolve_image(image, settings.get("image")),
            persistent=cc.get("container_persistent", True) is not False,
            resources={"cpu": cc.get("container_cpu", 0), "memory_mb": cc.get("container_memory", 0),
                       "disk_mb": cc.get("container_disk", 0)},
            sync_files=settings.get("sync_files", True) is not False,
            spawn_timeout=int(settings.get("spawn_timeout") or 300))
