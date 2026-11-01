"""Tests for the bubblewrap backend wired into the terminal tool: the
environment factory, the unknown-backend error string, the
host-path defaults, file access through the host path, and execute_code
dispatch (the script runs inside the sandbox, never on the host).

Unit tests never spawn bwrap. Integration tests are skipped as a module
when bwrap is missing or its runtime probe fails, so CI without bwrap
stays green.
"""

import json
import os
import shutil
import subprocess
import textwrap
from unittest.mock import MagicMock, patch

import pytest

from bubblewrap_plugin import sandbox as bubblewrap
from bubblewrap_plugin.sandbox import BubblewrapEnvironment
from tools.environments.local import LocalEnvironment
from tools.terminal_tool_backends import _create_environment
from tools.terminal_tool_lifecycle import get_active_env


def _bwrap_usable() -> bool:
    if shutil.which("bwrap") is None:
        return False
    try:
        probe = subprocess.run(
            ["bwrap", "--unshare-user", "--ro-bind", "/", "/", "true"],
            capture_output=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0


BWRAP_USABLE = _bwrap_usable()
needs_bwrap = pytest.mark.skipif(not BWRAP_USABLE, reason="bwrap missing or its namespace probe failed")


@pytest.fixture
def work_dir(tmp_path):
    d = tmp_path / "work"
    d.mkdir()
    return d


@pytest.fixture
def isolated_tool(tmp_path, work_dir, monkeypatch, bwrap_provider):
    """terminal_tool with backend=bubblewrap (the plugin's provider registered),
    cwd=work_dir, an isolated HERMES_HOME and a clean environment cache."""
    import tools.terminal_tool as tt

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setenv("TERMINAL_SANDBOX_DIR", str(tmp_path / "sandboxes"))
    monkeypatch.setattr(tt, "_terminal_config_bridge_attempted", True)
    monkeypatch.setenv("TERMINAL_ENV", "bubblewrap")
    monkeypatch.setenv("TERMINAL_CWD", str(work_dir))

    def _clear():
        with tt._env_lock:
            for env in list(tt._active_environments.values()):
                try:
                    env.cleanup()
                except Exception:
                    pass
            tt._active_environments.clear()
            tt._last_activity.clear()
        # Core's own cleanup drops the file tools' cached ShellFileOperations with the environment;
        # clearing the dict above does not, and a shell-path file op would reuse a dead sandbox.
        from tools.file_tools import clear_file_ops_cache
        clear_file_ops_cache()

    _clear()
    yield tt
    _clear()


def _no_spawn(monkeypatch):
    monkeypatch.setattr(bubblewrap, "_probed_bwrap_path", shutil.which("bwrap") or "/usr/bin/bwrap")
    return patch.object(LocalEnvironment, "init_session", autospec=True, return_value=None)


class TestFactory:
    def test_factory_returns_bubblewrap_environment_with_cwd_timeout_and_config(self, isolated_tool, work_dir,
                                                                                 monkeypatch, bwrap_provider):
        _provider, settings = bwrap_provider
        settings.update(profile="restricted", memory_mb=512)
        with _no_spawn(monkeypatch):
            env = _create_environment("bubblewrap", "", str(work_dir), 42)
        try:
            assert isinstance(env, BubblewrapEnvironment)
            assert env.cwd == str(work_dir)
            assert env.timeout == 42
            assert env._config.profile == "restricted"
            assert env._config.memory_mb == 512
        finally:
            env.cleanup()

    def test_unknown_backend_error_lists_bubblewrap(self, isolated_tool, work_dir):
        with pytest.raises(ValueError, match="bubblewrap") as excinfo:
            _create_environment("no-such-backend", "", str(work_dir), 10)
        assert "Unknown environment type: no-such-backend" in str(excinfo.value)

    @needs_bwrap
    def test_without_terminal_cwd_commands_start_in_the_host_cwd(self, isolated_tool, work_dir, monkeypatch):
        # Hermes 0.22.3+ asks the provider (default_cwd); older cores hand it /root, which the
        # environment maps to the same host default. Either way the command runs where Hermes started.
        monkeypatch.delenv("TERMINAL_CWD", raising=False)
        monkeypatch.chdir(work_dir)
        r = json.loads(isolated_tool.terminal_tool("pwd", task_id="default-cwd"))
        assert r["exit_code"] == 0, r
        assert r["output"].strip() == os.path.realpath(work_dir)
        r = json.loads(isolated_tool.terminal_tool("touch here && pwd", task_id="default-cwd"))
        assert r["exit_code"] == 0, r
        assert (work_dir / "here").is_file()

    @needs_bwrap
    def test_started_under_a_hidden_path_commands_start_in_home(self, isolated_tool, tmp_path, monkeypatch):
        # A gateway started from its checkout under HOME/.hermes: no command could run there.
        home = tmp_path / "home"
        checkout = home / ".hermes" / "hermes-agent"
        checkout.mkdir(parents=True)
        monkeypatch.setenv("HERMES_REAL_HOME", str(home))
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("TERMINAL_CWD", raising=False)
        monkeypatch.chdir(checkout)
        r = json.loads(isolated_tool.terminal_tool("pwd", task_id="hidden-start"))
        assert r["exit_code"] == 0, r
        assert r["output"].strip() == os.path.realpath(home)

    def test_a_cwd_that_is_not_a_host_directory_starts_in_the_default(self, isolated_tool, work_dir, monkeypatch):
        # Hermes before 0.22.3 hands plugin backends the container default /root, and keeps passing
        # it with every command; the environment starts in the host default and maps it there.
        monkeypatch.chdir(work_dir)
        with _no_spawn(monkeypatch):
            env = _create_environment("bubblewrap", "", "/nonexistent-root", 10)
        try:
            assert env.cwd == os.path.realpath(work_dir)
            assert env._replaced_cwd == "/nonexistent-root"
        finally:
            env.cleanup()

    def test_check_terminal_requirements_follows_the_probe(self, isolated_tool, monkeypatch):
        monkeypatch.setattr(bubblewrap, "run_probe", lambda: ("/usr/bin/bwrap", None))
        assert isolated_tool.check_terminal_requirements() is True
        monkeypatch.setattr(bubblewrap, "run_probe", lambda: (None, "bubblewrap (bwrap) is not on PATH"))
        assert isolated_tool.check_terminal_requirements() is False


@needs_bwrap
class TestToolIntegration:
    def test_terminal_creates_a_file_and_read_file_returns_it_via_the_host_path(self, isolated_tool, work_dir):
        from tools.file_tools import read_file_tool

        task_id = "host-path"
        r = json.loads(isolated_tool.terminal_tool(
            "printf hello > probe.txt && ls /proc | grep -c '^[0-9]'", task_id=task_id,
        ))
        assert r["exit_code"] == 0, r
        # A pid namespace with a handful of processes: the command ran sandboxed.
        assert int(r["output"].strip().splitlines()[-1]) < 10
        assert isinstance(get_active_env(task_id), BubblewrapEnvironment)

        host_file = work_dir / "probe.txt"
        assert host_file.read_text() == "hello"
        assert "hello" in read_file_tool(str(host_file), task_id=task_id)

    @pytest.mark.parametrize("native", ["1", "0"])
    def test_file_tools_reach_past_the_sandbox_only_on_the_native_path(self, isolated_tool, tmp_path, monkeypatch,
                                                                        native):
        """The prompt note's claim, live: with core's in-process file path (the default) the file tools
        write and read a path outside the writable set that a command cannot write; with
        HERMES_NATIVE_FILE_READ=0 they run through the sandbox and get the same refusal."""
        from tools.file_tools import read_file_tool, write_file_tool

        monkeypatch.setenv("HERMES_NATIVE_FILE_READ", native)
        outside = tmp_path / "outside"
        outside.mkdir()
        target = outside / "note.txt"
        task_id = f"file-tools-{native}"
        r = json.loads(isolated_tool.terminal_tool(f"printf cmd > {target}", task_id=task_id))
        assert r["exit_code"] != 0 and "Read-only file system" in r["output"], r
        w = json.loads(write_file_tool(str(target), "FILE-TOOL\n", task_id=task_id))
        if native == "1":
            assert w.get("bytes_written") == 10, w
            assert target.read_text() == "FILE-TOOL\n"
            assert "FILE-TOOL" in read_file_tool(str(target), task_id=task_id)
        else:
            assert "Read-only file system" in w.get("error", ""), w
            assert not target.exists()

    def test_ca_bundle_under_hermes_home_is_readable_inside_the_sandbox(self, isolated_tool, tmp_path, monkeypatch):
        """SSL_CERT_FILE pointing into the hidden HERMES_HOME (where hermes puts certifi's bundle) must
        still be readable, or every TLS client in the sandbox fails; the rest of HERMES_HOME stays hidden."""
        hermes_home = tmp_path / ".hermes"
        bundle = hermes_home / "venv" / "certifi" / "cacert.pem"
        bundle.parent.mkdir(parents=True)
        bundle.write_text("TRUST-ANCHORS\n")
        (hermes_home / "config.yaml").write_text("secret: yes\n")
        monkeypatch.setenv("SSL_CERT_FILE", str(bundle))
        # The venv stands in for the Python environment Hermes runs from, the one part of
        # a Hermes home a trust anchor may be bound back from.
        monkeypatch.setattr(bubblewrap, "python_env_roots", lambda: (str(hermes_home / "venv"),))
        r = json.loads(isolated_tool.terminal_tool(
            'cat "$SSL_CERT_FILE"; ls -A "$(dirname "$SSL_CERT_FILE")/../.."', task_id="ca-bundle",
        ))
        assert r["exit_code"] == 0, r
        assert "TRUST-ANCHORS" in r["output"]
        assert "config.yaml" not in r["output"]

    def test_background_launch_is_refused_with_a_clear_error(self, isolated_tool, work_dir, monkeypatch):
        """terminal(background=true) used to report "Background process started" with a pollable
        session id while the job died with the launching command's sandbox (#236): a model that
        trusts tool results waited on a process that no longer existed. Hermes core has no
        "no background processes" flag for plugin backends, so the environment refuses core's
        launch wrapper in execute(); the tool result is the refusal, not a live process. The job
        runs nowhere: never spawn_local on the host, and no marker is ever written."""
        from tools.process_registry import process_registry

        def forbidden(*args, **kwargs):
            raise AssertionError("spawn_local must not run under bubblewrap")

        monkeypatch.setattr(process_registry, "spawn_local", forbidden)
        r = json.loads(isolated_tool.terminal_tool("sleep 2; touch bg-marker", background=True, task_id="background"))
        assert r.get("output") != "Background process started", r
        assert not r.get("session_id"), r
        assert "background processes are not supported by this backend" in r.get("error", ""), r
        assert not (work_dir / "bg-marker").exists()

@needs_bwrap
class TestWritableRootIsConfiguration:
    """Hermes recreates an environment from the session's recorded cwd (after the idle timeout, for
    the file tools, for a delegated child). The writable root must stay terminal.cwd: a model's own
    `cd` moves where commands start, never what they may write."""

    def test_a_cd_does_not_become_writable_when_the_environment_is_recreated(self, isolated_tool, work_dir,
                                                                               tmp_path):
        from tools.terminal_tool_lifecycle import _cleanup_inactive_envs

        other = tmp_path / "other"
        other.mkdir()
        task = "recreated"
        r = json.loads(isolated_tool.terminal_tool(f"cd {other} && touch before", task_id=task))
        assert r["exit_code"] != 0 and "Read-only file system" in r["output"], r
        _cleanup_inactive_envs(0)
        assert get_active_env(task) is None
        r = json.loads(isolated_tool.terminal_tool("pwd; touch after", task_id=task))
        assert r["output"].splitlines()[0] == str(other), r
        assert r["exit_code"] != 0 and "Read-only file system" in r["output"], r
        assert not (other / "after").exists()
        r = json.loads(isolated_tool.terminal_tool(f"cd {work_dir} && touch inside", task_id=task))
        assert r["exit_code"] == 0, r
        assert (work_dir / "inside").is_file()

    def test_cd_to_root_then_recreation_keeps_working(self, isolated_tool, work_dir):
        from tools.terminal_tool_lifecycle import _cleanup_inactive_envs

        task = "root-cd"
        assert json.loads(isolated_tool.terminal_tool("cd /", task_id=task))["exit_code"] == 0
        _cleanup_inactive_envs(0)
        r = json.loads(isolated_tool.terminal_tool(f"cd {work_dir} && touch ok && pwd", task_id=task))
        assert r["exit_code"] == 0, r
        assert (work_dir / "ok").is_file()

    def test_a_missing_terminal_cwd_fails_closed(self, isolated_tool, tmp_path, monkeypatch):
        # A typo in terminal.cwd must not quietly make the process cwd writable.
        proc_cwd = tmp_path / "proc_cwd"
        proc_cwd.mkdir()
        monkeypatch.chdir(proc_cwd)
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path / "work_typo"))
        r = json.loads(isolated_tool.terminal_tool("touch written_here && pwd", task_id="typo"))
        assert r["exit_code"] != 0, r
        assert not (proc_cwd / "written_here").exists()


class TestExecuteCodeDispatch:
    def test_execute_code_dispatches_bubblewrap_to_the_env_backed_path(self, isolated_tool):
        """Pins the dispatch: under bubblewrap execute_code must take
        the env.execute() file-RPC path, which runs the script under the bwrap
        prefix. The host UDS path would spawn the script unsandboxed, so
        routing bubblewrap beside local fails this test."""
        import tools.code_execution_tool as cet

        sentinel = json.dumps({"status": "success", "output": "via-env"})
        remote = MagicMock(return_value=sentinel)
        local_tmp = MagicMock()
        local_tmp.mkdtemp.side_effect = AssertionError("host UDS path must not run under bubblewrap")

        with patch.object(cet, "_execute_remote", remote), \
             patch.object(cet, "tempfile", local_tmp), \
             patch("tools.process_registry._is_supervised_gateway_process", return_value=False), \
             patch("tools.approval.check_execute_code_guard", return_value={"approved": True}):
            assert isolated_tool._get_env_config()["env_type"] == "bubblewrap"
            result = cet.execute_code("print(1)", task_id="dispatch", enabled_tools=["terminal"])

        assert result == sentinel
        remote.assert_called_once_with("print(1)", "dispatch", ["terminal"], reset=False)
        local_tmp.mkdtemp.assert_not_called()


@needs_bwrap
class TestExecuteCodeIntegration:
    def test_execute_code_runs_inside_the_sandbox_under_the_default_rlimits(self, isolated_tool, work_dir):
        from tools.code_execution_tool import execute_code

        task_id = "execute-code"
        code = textwrap.dedent("""
            import os
            print("PIDS", len([n for n in os.listdir("/proc") if n.isdigit()]))
            try:
                bytearray(400 * 1024 * 1024)
                print("ALLOC ok")
            except MemoryError:
                print("ALLOC MemoryError")
        """)
        r = json.loads(execute_code(code, task_id=task_id, enabled_tools=["terminal"]))
        assert r["status"] == "success", r
        lines = r["output"].strip().splitlines()
        pids = int(next(l for l in lines if l.startswith("PIDS ")).split()[1])
        # A pid namespace with a handful of processes: the script ran sandboxed.
        assert pids < 10, r["output"]
        # RLIMIT_AS from the default memory_mb applies to the script too.
        assert "ALLOC MemoryError" in lines, r["output"]
        env = get_active_env(task_id)
        assert isinstance(env, BubblewrapEnvironment)
        # The RPC commands ran with cwd=/; the terminal's tracked cwd is untouched
        # and the next terminal write lands in the project directory.
        assert env.cwd == str(work_dir)
        t = json.loads(isolated_tool.terminal_tool("touch ./after-execute-code && pwd", task_id=task_id))
        assert t["exit_code"] == 0, t
        assert t["output"].strip() == str(work_dir)
        assert (work_dir / "after-execute-code").is_file()
