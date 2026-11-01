"""The provider as Hermes sees it: real plugin discovery from a profile's plugins dir, the backend
registry entry, the settings path, and the system prompt note. Runs on every OS: off Linux it checks
that the backend reports itself unavailable instead of failing on missing bwrap pieces.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

import bubblewrap_plugin as plugin

_ROOT = Path(plugin.__file__).resolve().parent
LINUX = sys.platform.startswith("linux")


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _install(home: Path, settings: dict | None = None) -> None:
    shutil.copytree(_ROOT, home / "plugins" / "bubblewrap",
                    ignore=shutil.ignore_patterns("tests", "__pycache__", ".pytest_cache"))
    config = {"plugins": {"enabled": ["bubblewrap"]}}
    if settings:
        config["plugins"]["entries"] = {"bubblewrap": {"settings": settings}}
    # JSON is YAML; core's test env does not ship PyYAML.
    (home / "config.yaml").write_text(json.dumps(config))


@pytest.fixture
def discovered(hermes_home):
    """A fresh PluginManager that discovered the installed plugin; the registry is restored after."""
    import agent.terminal_env_registry as registry
    from hermes_cli.plugins import PluginManager

    previous = registry.snapshot_registration("bubblewrap")
    manager = PluginManager()

    def load(settings=None):
        _install(hermes_home, settings)
        manager.discover_and_load(force=True)
        return manager, registry.get_provider("bubblewrap")

    try:
        yield load
    finally:
        current = registry.snapshot_registration("bubblewrap")
        if current is not None:
            registry.restore_registration("bubblewrap", current, previous)


class TestProvider:
    def test_identity_and_flags(self):
        provider = plugin.BubblewrapProvider()
        assert provider.name == "bubblewrap"
        assert provider.is_remote is False
        assert provider.is_container is False
        assert provider.skip_container_guards is False

    def test_prompt_note_is_empty_on_another_backend(self, monkeypatch):
        monkeypatch.setenv("TERMINAL_ENV", "local")
        assert plugin.BubblewrapProvider().prompt_note({}) == ""

    @pytest.mark.skipif(LINUX, reason="off-Linux behaviour")
    def test_unavailable_off_linux(self):
        provider = plugin.BubblewrapProvider()
        assert provider.is_available() is False
        assert provider.probe()[0] == "unavailable"
        assert provider.check_requirements({}) is False


@pytest.mark.skipif(not LINUX, reason="the plugin declares platforms: [linux]")
class TestDiscovery:
    def test_discovery_registers_the_backend_and_the_prompt_note(self, discovered):
        manager, provider = discovered()
        # Loaded under the loader's own package name, so compare by class name, not identity.
        assert type(provider).__name__ == "BubblewrapProvider"
        assert provider.name == "bubblewrap"
        assert "bubblewrap-sandbox" in manager._system_prompt_sections

    def test_settings_come_from_the_profile_config(self, discovered, monkeypatch):
        _manager, provider = discovered({"profile": "restricted", "memory_mb": 512,
                                         "binds": [{"src": "/srv/data"}]})
        monkeypatch.setenv("TERMINAL_HOME_MODE", "profile")
        config = provider.load_config()
        assert config.profile == "restricted"
        assert config.memory_mb == 512
        assert config.cpu_seconds == 30
        assert [b.src for b in config.binds] == ["/srv/data"]
        assert config.home_mode == "profile"

    def test_prompt_note_follows_the_backend_and_the_profile(self, discovered, monkeypatch):
        manager, _provider = discovered({"profile": "workspace"})
        monkeypatch.setenv("TERMINAL_ENV", "bubblewrap")
        sections = {s.id: s.content for s in manager.render_system_prompt_sections({})}
        note = sections["bubblewrap-sandbox"]
        assert "profile `workspace`" in note
        assert "no network" in note
        assert "Background processes are not available" in note
        monkeypatch.setenv("TERMINAL_ENV", "local")
        assert "bubblewrap-sandbox" not in {s.id for s in manager.render_system_prompt_sections({})}

    @pytest.mark.parametrize("native", [None, "1", "0"])
    def test_prompt_note_says_where_the_file_tools_run(self, discovered, monkeypatch, native):
        manager, _provider = discovered({})
        monkeypatch.setenv("TERMINAL_ENV", "bubblewrap")
        if native is None:
            monkeypatch.delenv("HERMES_NATIVE_FILE_READ", raising=False)
        else:
            monkeypatch.setenv("HERMES_NATIVE_FILE_READ", native)
        note = {s.id: s.content for s in manager.render_system_prompt_sections({})}["bubblewrap-sandbox"]
        assert "`search_files` run through this sandbox too" in note
        assert "do not run in this sandbox" not in note

    @pytest.mark.parametrize("native,bypasses", [("1", True), ("0", False)])
    def test_legacy_core_prompt_follows_native_file_setting(self, discovered, monkeypatch, native, bypasses):
        from tools.environments import base

        manager, _provider = discovered({})
        monkeypatch.delattr(base, "env_uses_host_filesystem")
        monkeypatch.setenv("TERMINAL_ENV", "bubblewrap")
        monkeypatch.setenv("HERMES_NATIVE_FILE_READ", native)
        note = {s.id: s.content for s in manager.render_system_prompt_sections({})}["bubblewrap-sandbox"]
        assert ("`search_files` do not run in this sandbox" in note) is bypasses
        assert ("`search_files` run through this sandbox too" in note) is not bypasses

    def test_bad_settings_name_the_setting(self, discovered):
        _manager, provider = discovered({"memory_mb": "lots"})
        with pytest.raises(ValueError, match="plugins.entries.bubblewrap.settings.memory_mb"):
            provider.load_config()

    def test_each_profile_reads_its_own_settings(self, discovered, tmp_path, monkeypatch):
        # One registration serves every profile of a multiplexed gateway: settings are read per call.
        _manager, provider = discovered({"profile": "restricted", "memory_mb": 512})
        assert (provider.load_config().profile, provider.load_config().memory_mb) == ("restricted", 512)
        other = tmp_path / "other-profile"
        other.mkdir()
        (other / "config.yaml").write_text(json.dumps({"plugins": {"enabled": ["bubblewrap"], "entries": {
            "bubblewrap": {"settings": {"profile": "workspace", "memory_mb": 2048}}}}}))
        monkeypatch.setenv("HERMES_HOME", str(other))
        assert (provider.load_config().profile, provider.load_config().memory_mb) == ("workspace", 2048)

    def test_prompt_note_names_extra_writable_paths(self, discovered, monkeypatch):
        manager, _provider = discovered({"profile": "restricted",
                                         "binds": [{"src": "/srv/out", "readonly": False}, {"src": "/srv/ref"}]})
        monkeypatch.setenv("TERMINAL_ENV", "bubblewrap")
        monkeypatch.setenv("TERMINAL_HOME_MODE", "profile")
        note = {s.id: s.content for s in manager.render_system_prompt_sections({})}["bubblewrap-sandbox"]
        assert "read-only except /srv/out, the profile home ($HOME) (the working directory is read-only" in note
        assert "/srv/ref" not in note
