"""A sandbox that your dispatch program provides.

Hermes runs the program once per job, as ``<program> <operation> <sandbox-id> [argv...]``:

* ``spawn``  create the sandbox, or resume it when one with that id exists. Must be idempotent.
* ``exec``   run ``argv`` inside the sandbox with stdin/stdout/stderr connected; exit with its status.
* ``pause``  stop it but keep its disk, so a later ``spawn`` with the same id resumes it.
* ``delete`` remove it for good.

Every job also gets ``HERMES_SANDBOX_*`` variables (operation, id, image, persistent, cpu, memory,
disk). Imported only by :meth:`DispatchProvider.create_environment`.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import shlex
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any, Optional

from hermes_constants import get_hermes_home
from tools.environments.base import BaseEnvironment, EnvironmentConnectionError
from tools.environments.base_output import _popen_bash
from tools.environments.file_sync import FileSyncManager, iter_sync_files, quoted_mkdir_command, quoted_rm_command
from tools.environments.remote_common import bash_argv, prepend_unset, resolve_passthrough_env

logger = logging.getLogger(__name__)

_JOB_TIMEOUT_S = 120
_STDERR_TAIL = 600


def resolve_image(task_image: Optional[str], configured: Optional[str]) -> str:
    """The per-task image Hermes passes (batch runs), else the plugin's ``image`` setting, else
    Hermes' default sandbox image. The program decides what an image means on its platform."""
    for value in (task_image, configured):
        if isinstance(value, str) and value.strip():
            return value.strip()
    from hermes_cli.config_defaults import DEFAULT_SANDBOX_IMAGE
    return DEFAULT_SANDBOX_IMAGE


def sandbox_id(task_id: str, persistent: bool) -> str:
    """A persistent session keeps one id for its whole life (per profile home), so ``pause`` then
    ``spawn`` lands back in the same sandbox; a non-persistent one gets a fresh id each time."""
    if not persistent:
        return f"hermes-{uuid.uuid4().hex[:16]}"
    digest = hashlib.sha256(f"{get_hermes_home()}\0{task_id}".encode()).hexdigest()[:16]
    return f"hermes-{digest}"


def _tail(text: str) -> str:
    text = (text or "").strip()
    return text if len(text) <= _STDERR_TAIL else "..." + text[-_STDERR_TAIL:]


class DispatchEnvironment(BaseEnvironment):
    """Spawn-per-call: every command is one ``<program> exec <id> bash -c <script>`` process."""

    # Passthrough values ride the program's own environment on every exec (names in
    # HERMES_SANDBOX_ENV), so they stay out of the in-sandbox snapshot under multiplex.
    _profile_scoped_passthrough = True
    # A timed-out ``sudo -n true`` probe only kills its own exec job, never the sandbox.
    _passwordless_sudo_probe_supported = True

    def __init__(self, program: list[str], cwd: str = "~", timeout: int = 60, task_id: str = "default",
                 image: str = "", persistent: bool = True, resources: Optional[dict] = None,
                 sync_files: bool = True, spawn_timeout: int = 300):
        super().__init__(cwd=cwd, timeout=timeout)
        self.program = list(program)
        self.image = image
        self.persistent = persistent
        self.sandbox_id = sandbox_id(task_id, persistent)
        self._resources = resources or {}
        self._spawned = False
        self._sync_manager: Optional[FileSyncManager] = None

        self._job("spawn", timeout=spawn_timeout)
        self._spawned = True
        self._home = self._detect_home()
        if cwd == "~":
            self.cwd = self._home
        if sync_files:
            base = f"{self._home}/.hermes"
            self._exec_checked(quoted_mkdir_command([base, f"{base}/skills", f"{base}/credentials", f"{base}/cache"]),
                               "creating ~/.hermes in the sandbox")
            self._sync_manager = FileSyncManager(
                get_files_fn=lambda: iter_sync_files(base), upload_fn=self._upload_one,
                delete_fn=self._delete_paths, bulk_upload_fn=self._bulk_upload,
                bulk_download_fn=self._bulk_download)
            self._sync_manager.sync(force=True)
        self.init_session()

    # -- the dispatch contract --
    def _job_env(self, operation: str) -> dict[str, str]:
        return {
            **os.environ,
            "HERMES_SANDBOX_OPERATION": operation,
            "HERMES_SANDBOX_ID": self.sandbox_id,
            "HERMES_SANDBOX_IMAGE": self.image,
            "HERMES_SANDBOX_PERSISTENT": "1" if self.persistent else "0",
            "HERMES_SANDBOX_CPU": str(self._resources.get("cpu") or 0),
            "HERMES_SANDBOX_MEMORY_MB": str(self._resources.get("memory_mb") or 0),
            "HERMES_SANDBOX_DISK_MB": str(self._resources.get("disk_mb") or 0),
        }

    def _job(self, operation: str, *, timeout: float = _JOB_TIMEOUT_S) -> None:
        """Run a lifecycle job (spawn/pause/delete); a non-zero exit is a connection-class error."""
        cmd = [*self.program, operation, self.sandbox_id]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                    timeout=timeout, check=False, stdin=subprocess.DEVNULL,
                                    env=self._job_env(operation))
        except subprocess.TimeoutExpired:
            raise EnvironmentConnectionError(
                f"dispatch program `{operation}` for {self.sandbox_id} timed out after {timeout:.0f}s",
                retry_hint="Check the dispatch program and its platform, then retry.")
        except OSError as exc:
            raise EnvironmentConnectionError(f"dispatch program could not start: {exc}",
                                             retry_hint="Check plugins.entries.dispatch.settings.program.")
        if result.returncode != 0:
            raise EnvironmentConnectionError(
                f"dispatch program `{operation}` for {self.sandbox_id} exited {result.returncode}: "
                f"{_tail(result.stderr or result.stdout)}",
                retry_hint="Fix the dispatch program or its platform; the next command retries.")

    def _exec_argv(self, argv: list[str]) -> list[str]:
        return [*self.program, "exec", self.sandbox_id, *argv]

    def _exec(self, script: str, *, timeout: float = _JOB_TIMEOUT_S, stdin: Any = subprocess.DEVNULL,
              stdout: Any = subprocess.PIPE) -> subprocess.CompletedProcess:
        return subprocess.run(self._exec_argv(["bash", "-c", script]), stdin=stdin, stdout=stdout,
                              stderr=subprocess.PIPE, timeout=timeout, check=False, env=self._job_env("exec"))

    def _exec_checked(self, script: str, what: str, **kwargs) -> subprocess.CompletedProcess:
        result = self._exec(script, **kwargs)
        if result.returncode != 0:
            raise EnvironmentConnectionError(
                f"{what} failed (exit {result.returncode}): {_tail(result.stderr.decode(errors='replace'))}",
                retry_hint="Check that the dispatch program's exec job passes stdin and argv through.")
        return result

    def _detect_home(self) -> str:
        result = self._exec_checked('printf %s "$HOME"', "reading the sandbox home")
        home = result.stdout.decode(errors="replace").strip()
        return home if home.startswith("/") else "/root"

    # -- BaseEnvironment --
    def _before_execute(self) -> None:
        if self._sync_manager is not None:
            self._sync_manager.sync()  # rate-limited internally

    def _run_bash(self, cmd_string: str, *, login: bool = False, timeout: int = 120,
                  stdin_data: str | None = None) -> subprocess.Popen:
        """Skill and ``terminal.env_passthrough`` values travel in the program's environment with their
        names in ``HERMES_SANDBOX_ENV`` (``docker exec -e NAME`` copies one from there), never in argv.
        Profile-scoped names the active scope lacks are unset inside the sandbox."""
        values, unset_names = resolve_passthrough_env()
        cmd = self._exec_argv(bash_argv(prepend_unset(cmd_string, unset_names), login))
        env = {**self._job_env("exec"), **values, "HERMES_SANDBOX_ENV": " ".join(sorted(values))}
        return _popen_bash(cmd, stdin_data, env=env)

    def exec_prefix(self, *, user: Optional[str] = None, interactive: bool = True) -> Optional[list[str]]:
        """Local argv running its remainder in the sandbox, so Bot Desktop, ``computer_use`` and the
        browser follow the terminal here. *user* is the program's concern and is ignored."""
        return self._exec_argv([]) if self._spawned else None

    def cleanup(self, force_remove: bool = False):
        """Pause a persistent sandbox (idle reaping and session close; the next command resumes it).
        Delete a throwaway one, or any one on an explicit teardown (``force_remove``)."""
        if not self._spawned:
            return
        self._spawned = False
        if self._sync_manager is not None:
            with contextlib.suppress(Exception):
                self._sync_manager.sync_back()
        operation = "pause" if self.persistent and not force_remove else "delete"
        try:
            self._job(operation)
        except EnvironmentConnectionError as exc:
            logger.warning("dispatch: %s", exc)

    # -- file sync over exec (skills, credential files, cache) --
    def _upload_one(self, host_path: str, remote_path: str) -> None:
        script = f"mkdir -p {shlex.quote(str(Path(remote_path).parent))} && cat > {shlex.quote(remote_path)}"
        with open(host_path, "rb") as src:
            self._exec_checked(script, f"uploading {remote_path}", stdin=src, timeout=60)

    def _delete_paths(self, remote_paths: list[str]) -> None:
        self._exec_checked(quoted_rm_command(remote_paths), "removing synced files", timeout=30)

    def _bulk_upload(self, files: list[tuple[str, str]]) -> None:
        """All files in one ``tar`` stream through one exec job, staged as symlinks relative to ``~/.hermes``."""
        if not files:
            return
        base = f"{self._home}/.hermes"
        with tempfile.TemporaryDirectory(prefix="hermes-dispatch-sync-") as staging:
            for host_path, remote_path in files:
                rel = os.path.relpath(remote_path, base)
                if rel == "." or rel.startswith(".."):
                    raise RuntimeError(f"remote path {remote_path!r} is outside the sync base {base!r}")
                staged = os.path.join(staging, rel)
                os.makedirs(os.path.dirname(staged), exist_ok=True)
                try:
                    os.symlink(os.path.abspath(host_path), staged)
                except OSError:
                    shutil.copy2(host_path, staged)
            tar = subprocess.Popen(["tar", "-chf", "-", "-C", staging, "."], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                result = self._exec(f"tar xf - --no-overwrite-dir -C {shlex.quote(base)}", stdin=tar.stdout,
                                    timeout=120)
            finally:
                tar.stdout.close()
                _, tar_err = tar.communicate(timeout=30)
            if tar.returncode != 0:
                detail = _tail(tar_err.decode(errors="replace"))
                raise RuntimeError(f"tar create failed (rc={tar.returncode}): {detail}")
            if result.returncode != 0:
                raise EnvironmentConnectionError(
                    f"file sync into the sandbox failed (exit {result.returncode}): "
                    f"{_tail(result.stderr.decode(errors='replace'))}",
                    retry_hint="The sandbox needs tar; the exec job must pass stdin through.")
        logger.debug("dispatch: bulk-uploaded %d file(s) into %s", len(files), self.sandbox_id)

    def _bulk_download(self, dest: Path) -> None:
        rel_base = f"{self._home}/.hermes".lstrip("/")
        with open(dest, "wb") as out:
            result = self._exec(f"tar cf - --exclude='*.sock' -C / {shlex.quote(rel_base)}", stdout=out)
        if result.returncode != 0:
            raise EnvironmentConnectionError(
                f"file sync from the sandbox failed (exit {result.returncode}): "
                f"{_tail(result.stderr.decode(errors='replace'))}",
                retry_hint="The sandbox needs tar; check the exec job.")
