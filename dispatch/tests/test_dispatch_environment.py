"""A real ``DispatchEnvironment`` driving a real program through the dispatch contract.

The fake program keeps each "sandbox" as a directory under ``$FAKE_SANDBOX_ROOT``: ``spawn`` creates it,
``exec`` runs the argv with that directory as ``$HOME``, ``pause``/``delete`` are logged (``delete`` also
removes it). Everything Hermes does goes through ``<program> <operation> <id> [argv...]``.
"""

import sys
import textwrap

import pytest

from tools.environments.base import EnvironmentConnectionError
from dispatch_plugin.dispatch_env import DispatchEnvironment

FAKE = textwrap.dedent("""
    import os, shutil, sys
    op, sid, argv = sys.argv[1], sys.argv[2], sys.argv[3:]
    root = os.environ["FAKE_SANDBOX_ROOT"]
    home = os.path.join(root, sid)
    with open(os.path.join(root, "jobs.log"), "a") as log:
        log.write(f"{op} {sid}\\n")
    if op == "spawn":
        if os.environ.get("FAKE_FAIL_SPAWN"):
            sys.exit("quota exceeded for " + sid)
        os.makedirs(home, exist_ok=True)
    elif op == "exec":
        env = dict(os.environ, HOME=home)
        os.chdir(home)
        os.execvpe(argv[0], argv, env)
    elif op == "delete":
        shutil.rmtree(home, ignore_errors=True)
""")


@pytest.fixture
def program(tmp_path, monkeypatch):
    root = tmp_path / "sandboxes"
    root.mkdir()
    script = tmp_path / "fake_dispatch.py"
    script.write_text(FAKE)
    monkeypatch.setenv("FAKE_SANDBOX_ROOT", str(root))
    return [sys.executable, str(script)], root


def _jobs(root):
    return [line.split()[0] for line in (root / "jobs.log").read_text().splitlines() if not line.startswith("exec")]


def test_persistent_sandbox_pauses_and_resumes_by_id_throwaway_and_torn_down_ones_are_deleted(program):
    argv, root = program
    env = DispatchEnvironment(program=argv, cwd="~", timeout=30, task_id="session-1")
    sandbox = root / env.sandbox_id
    assert env.cwd == str(sandbox)  # "~" resolves to the sandbox's own home
    out = env.execute("echo hi > note.txt && mkdir sub && cd sub && pwd")
    assert out["returncode"] == 0 and out["output"].strip() == str(sandbox / "sub")
    assert (sandbox / ".hermes" / "skills").is_dir()  # sync base created over exec
    env.cleanup()
    env.cleanup()  # idempotent: one pause job, not two

    again = DispatchEnvironment(program=argv, cwd="~", timeout=30, task_id="session-1")
    assert again.sandbox_id == env.sandbox_id
    assert again.execute("cat note.txt")["output"].strip() == "hi"
    again.cleanup()
    assert _jobs(root) == ["spawn", "pause", "spawn", "pause"]

    throwaway = DispatchEnvironment(program=argv, cwd="~", timeout=30, task_id="session-1",
                                    persistent=False, sync_files=False)
    assert throwaway.sandbox_id != env.sandbox_id
    throwaway.cleanup()
    assert _jobs(root)[-2:] == ["spawn", "delete"] and not (root / throwaway.sandbox_id).exists()

    # An explicit teardown (the prompt-time backend probe, a user-initiated cleanup) deletes even a persistent one.
    probe = DispatchEnvironment(program=argv, cwd="~", timeout=30, task_id="prompt-backend-probe", sync_files=False)
    probe.cleanup(force_remove=True)
    assert _jobs(root)[-2:] == ["spawn", "delete"] and not (root / probe.sandbox_id).exists()


def test_failed_spawn_reports_the_programs_stderr(program, monkeypatch):
    argv, _root = program
    monkeypatch.setenv("FAKE_FAIL_SPAWN", "1")
    with pytest.raises(EnvironmentConnectionError, match="`spawn` .* exited 1: quota exceeded"):
        DispatchEnvironment(program=argv, cwd="~", timeout=30, task_id="s")
