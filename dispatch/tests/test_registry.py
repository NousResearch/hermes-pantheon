"""Through the real terminal-environment registry: the plugin's registration makes
``terminal.backend: dispatch`` resolve, with the classification every core policy site reads."""

import pytest

from agent import terminal_env_registry as reg
import dispatch_plugin


class _Ctx:
    def __init__(self, settings):
        self.settings = settings

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_terminal_environment_provider(self, provider):
        reg.register_provider(provider)


@pytest.fixture
def register():
    previous = reg.snapshot_registration("dispatch")
    current = []

    def _register(settings):
        dispatch_plugin.register(_Ctx(settings))
        current.append(reg.get_provider("dispatch"))
        return current[-1]

    yield _register
    reg.restore_registration("dispatch", current[-1] if current else None, previous)


def test_backend_resolves_and_approvals_stay_on_unless_declared_disposable(register, tmp_path):
    from tools.approval import _should_skip_container_guards

    program = tmp_path / "dispatch.sh"
    program.write_text("#!/bin/sh\n")
    program.chmod(0o755)
    settings = {"program": str(program)}
    provider = register(settings)
    assert provider.name == "dispatch" and "dispatch" in reg.plugin_backend_names()
    assert reg.provider_flag("dispatch", "is_remote") is True
    assert reg.provider_flag("dispatch", "supports_streams") is True
    assert provider.probe() == ("ready", "")
    # A dispatch program may well run commands on this host, so approvals stay on by default.
    assert _should_skip_container_guards("dispatch") is False
    settings["disposable"] = True
    assert _should_skip_container_guards("dispatch") is True
    settings["program"] = str(tmp_path / "missing.sh")
    assert provider.probe()[0] == "needs_setup" and not provider.is_available()
