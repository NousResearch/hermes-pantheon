"""Test bootstrap: import the plugin directory as the package ``dispatch_plugin``.

The Hermes plugin loader imports a directory plugin as a package (``hermes_plugins.<slug>``) so the
relative imports in ``__init__.py`` resolve; tests mirror that under a stable alias. Hermes core
(``agent``, ``tools``, ``hermes_cli``) must be importable: run with a hermes-agent checkout on ``PYTHONPATH``.

The environment tests spawn real ``bash`` commands through a fake dispatch program, so they run on
Linux and macOS only (the plugin declares ``platforms: [linux, macos]``).
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

PACKAGE = "dispatch_plugin"
_ROOT = Path(__file__).resolve().parents[1]

sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != _ROOT]

if os.name == "nt":
    collect_ignore_glob = ["test_dispatch_environment.py"]


def _load_plugin_package():
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    spec = importlib.util.spec_from_file_location(
        PACKAGE, _ROOT / "__init__.py", submodule_search_locations=[str(_ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = module
    spec.loader.exec_module(module)
    return module


_load_plugin_package()


@pytest.fixture(autouse=True)
def _hermes_home(tmp_path, monkeypatch):
    """Every test gets its own profile home, never the real one."""
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


class _RepoRootIsNotATestPackage:
    """The plugin directory *is* a package (``__init__.py`` with relative imports): collect it as a plain
    directory so pytest never imports that ``__init__.py`` standalone."""

    @pytest.hookimpl(tryfirst=True)
    def pytest_collect_directory(self, path, parent):
        if Path(path).resolve() == _ROOT:
            return pytest.Dir.from_parent(parent, path=path)
        return None


def pytest_configure(config):
    config.pluginmanager.register(_RepoRootIsNotATestPackage(), "dispatch-root-not-a-package")
