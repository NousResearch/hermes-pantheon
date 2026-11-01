"""Every spawn of the environment is the prlimit + bwrap argv around the shell, and the tracked cwd
never follows a command onto / or into the environment's own state dir.

Hermes core has no argv seam in LocalEnvironment, so the environment overrides ``_run_bash``; these
tests pin what that override must keep from the local one (login bootstrap included, no pre-exec
callback, cwd recovery before the wrap).
"""

import os
from unittest.mock import MagicMock, patch

import pytest

import tools.environments.local as local_mod
from bubblewrap_plugin import sandbox
from bubblewrap_plugin.sandbox import BubblewrapEnvironment

FAKE_BASH = "/opt/fake/bash"


@pytest.fixture
def env_factory(tmp_path, monkeypatch):
    monkeypatch.setenv("TERMINAL_SANDBOX_DIR", str(tmp_path / "sandboxes"))
    monkeypatch.setattr(sandbox, "_probed_bwrap_path", "/usr/bin/bwrap")
    # The process limit probe spawns bwrap once per process; pin its result so it never runs under popen_spy.
    monkeypatch.setattr(sandbox, "_process_limit_scoped", True)
    work = tmp_path / "work"
    work.mkdir()
    made = []

    def make():
        with patch.object(local_mod.LocalEnvironment, "init_session", autospec=True, return_value=None):
            env = BubblewrapEnvironment(cwd=str(work), timeout=10)
        made.append(env)
        return env

    yield make, work
    for env in made:
        env._remove_state()


@pytest.fixture
def popen_spy():
    fake_proc = MagicMock()
    fake_proc.pid = os.getpid()
    with patch.object(sandbox.subprocess, "Popen", return_value=fake_proc) as spy, \
            patch.object(sandbox, "_find_bash", return_value=FAKE_BASH), \
            patch.object(sandbox, "_resolve_shell_init_files", return_value=[]):
        yield spy


class TestRunBashWraps:
    def test_command_spawn_is_prlimit_then_bwrap_then_the_shell(self, env_factory, popen_spy):
        make, work = env_factory
        env = make()
        env._run_bash("echo hi")
        args, kwargs = popen_spy.call_args
        argv = args[0]
        assert argv[0] == env._prlimit_path
        i = argv.index(env._bwrap_path)
        assert argv[argv.index("--chdir") + 1] == str(work)
        assert argv[-3:] == [FAKE_BASH, "-c", "echo hi"]
        assert i < argv.index("--") < len(argv) - 3
        assert "preexec_fn" not in kwargs
        assert kwargs["start_new_session"] is True

    def test_login_bootstrap_is_wrapped_too(self, env_factory, popen_spy):
        make, _work = env_factory
        env = make()
        env._run_bash("true", login=True)
        argv = popen_spy.call_args[0][0]
        assert env._bwrap_path in argv
        assert argv[-4:] == [FAKE_BASH, "-l", "-c", "true"]

    def test_wrap_sees_the_recovered_cwd(self, env_factory, popen_spy, tmp_path):
        make, work = env_factory
        env = make()
        gone = work / "gone"
        gone.mkdir()
        env.cwd = str(gone)
        gone.rmdir()
        env._run_bash("pwd")
        argv = popen_spy.call_args[0][0]
        assert argv[argv.index("--chdir") + 1] == str(work)
        assert popen_spy.call_args[1]["cwd"] == str(work)


class TestTrackedCwd:
    def test_root_and_state_dir_are_not_followed(self, env_factory):
        make, work = env_factory
        env = make()
        marker = env._cwd_marker
        for elsewhere in ("/", os.path.join(env.get_temp_dir(), "hermes_exec_x")):
            result = {"output": f"out\n{marker}{elsewhere}{marker}\n"}
            env._extract_cwd_from_output(result)
            assert env.cwd == str(work)
            assert result["output"].strip() == "out"

    def test_a_real_cd_is_followed(self, env_factory, tmp_path):
        make, work = env_factory
        env = make()
        sub = work / "sub"
        sub.mkdir()
        marker = env._cwd_marker
        env._extract_cwd_from_output({"output": f"{marker}{sub}{marker}\n"})
        assert env.cwd == str(sub)

    def test_replaced_cwd_maps_to_the_tracked_cwd(self, env_factory, monkeypatch):
        make, work = env_factory
        env = make()
        env._replaced_cwd = "/root"
        seen = []
        monkeypatch.setattr(local_mod.LocalEnvironment, "execute",
                            lambda self, command, cwd="", **kw: seen.append(cwd) or {"output": "", "returncode": 0})
        env.execute("true", cwd="/root")
        env.execute("true", cwd=str(work))
        assert seen == ["", str(work)]
