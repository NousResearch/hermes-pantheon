"""``hermes delegation config``: driven headlessly through core's real curses menus.

Each curses menu the flow opens pops the next fake screen (its queued key presses); typed answers
come from ``input()``. Only the provider inventory and the price lookup are replaced, so the
test never needs credentials or the network.
"""

from __future__ import annotations

import argparse
import sys

import pytest

if sys.platform == "win32":
    pytest.skip("curses is not available on Windows", allow_module_level=True)
import curses  # noqa: E402

import hermes_yaml as yaml  # noqa: E402

from advanced_delegation_plugin import cli  # noqa: E402
from hermes_cli import auth_model_picker  # noqa: E402

# The route editor opens core's public, titled model picker (the same core release as the delegation seam).
needs_model_picker = pytest.mark.skipif(not hasattr(auth_model_picker, "prompt_model_selection"),
                                        reason="core without hermes_cli.auth_model_picker.prompt_model_selection")

DOWN, UP, ENTER, SPACE, QUIT = curses.KEY_DOWN, curses.KEY_UP, 10, ord(" "), ord("q")
PROVIDER_ROWS = [
    {"slug": "openrouter", "name": "OpenRouter", "models": ["google/gemini-3-flash-preview", "qwen/qwen3-coder"],
     "total_models": 2},
    {"slug": "anthropic", "name": "Anthropic", "models": ["claude-opus-5", "claude-haiku-5"], "total_models": 2},
]


class Screen:
    """One curses menu: replays queued keys and records what was drawn."""

    def __init__(self, keys):
        self.keys = list(keys)
        self.text = []

    def getch(self):
        if not self.keys:
            raise AssertionError("a menu asked for more keys than the test queued")
        return self.keys.pop(0)

    def timeout(self, ms):
        pass

    def clear(self):
        pass

    def getmaxyx(self):
        return (40, 140)

    def addnstr(self, y, x, text, *rest):
        self.text.append(text)

    def refresh(self):
        pass


@pytest.fixture
def home(tmp_path):
    return tmp_path / "os-home" / ".hermes"


@pytest.fixture
def terminal(monkeypatch):
    """Queue menus with ``terminal.menu(keys...)`` and typed lines with ``terminal.type(text...)``."""
    class Terminal:
        screens: list[Screen] = []
        drawn: list[Screen] = []
        lines: list[str] = []

        def menu(self, *keys):
            self.screens.append(Screen(keys))

        def type(self, *lines):
            self.lines.extend(lines)

        def shown(self, index):
            return "\n".join(self.drawn[index].text)

    t = Terminal()
    t.screens, t.drawn, t.lines = [], [], []

    def wrapper(draw):
        screen = t.screens.pop(0)
        t.drawn.append(screen)
        return draw(screen)

    def fake_input(prompt=""):
        if not t.lines:
            raise AssertionError(f"unexpected prompt: {prompt!r}")
        return t.lines.pop(0)

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)  # core's curses menus check stdin
    monkeypatch.setattr(cli, "_has_terminal", lambda: True)
    monkeypatch.setattr(curses, "wrapper", wrapper)
    monkeypatch.setattr(curses, "curs_set", lambda _v: None)
    monkeypatch.setattr(curses, "has_colors", lambda: False)
    monkeypatch.setattr("hermes_cli.curses_ui.flush_stdin", lambda: None)
    monkeypatch.setattr("builtins.input", fake_input)
    monkeypatch.setattr("hermes_cli.inventory.build_aux_picker_rows", lambda **kw: [dict(r) for r in PROVIDER_ROWS])
    monkeypatch.setattr("hermes_cli.models_pricing.get_pricing_for_provider", lambda slug, **kw: {})
    yield t
    assert not t.screens, "queued menus were never opened"
    assert not t.lines, "queued answers were never read"


def _run(*argv):
    parser = argparse.ArgumentParser(prog="hermes delegation")
    cli.setup_parser(parser)
    args = parser.parse_args(list(argv))
    return args.func(args) if hasattr(args, "func") else cli.handle(args)


def _config(home):
    path = home / "config.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}


def _write(home, data):
    (home / "config.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")


def test_interactive_editing_without_a_terminal_is_refused_with_the_way_out(monkeypatch, capsys, home):
    monkeypatch.setattr(cli, "_has_terminal", lambda: False)
    assert _run("config") == 1
    err = capsys.readouterr().err
    assert "interactive terminal" in err and "--show" in err
    assert not (home / "config.yaml").exists()


def test_show_prints_the_mode_and_routes_without_a_terminal(monkeypatch, capsys, home):
    monkeypatch.setattr(cli, "_has_terminal", lambda: False)
    _write(home, {"delegation": {"backend": "model_map", "backends": {"model_map": {"options": [
        {"name": "fast", "description": "Quick lookups.", "provider": "openrouter", "model": "m1"}],
        "default": "fast"}}}})
    assert _run("config", "--show") in (None, 0)
    out = capsys.readouterr().out
    assert "Model routes" in out and "fast" in out and "Quick lookups." in out and "openrouter · m1" in out


def test_mode_picker_lists_the_three_modes_and_default_turns_delegation_model_back_on(terminal, capsys, home):
    _write(home, {"delegation": {"backend": "model_map", "model": "sub-model", "provider": "openrouter"}})
    terminal.menu(UP, ENTER)  # the cursor opens on the current mode (Model routes), like hermes model
    _run("config")
    shown = terminal.shown(0)
    assert "Default" in shown and "Model routes" in shown and "Profile routes" in shown
    assert _config(home)["delegation"]["backend"] == "default"
    out = capsys.readouterr().out
    assert "delegation.model" in out and "sub-model" in out


def test_cancelling_the_mode_picker_changes_nothing(terminal, home):
    _write(home, {"delegation": {"backend": "profile_routes"}})
    terminal.menu(QUIT)
    _run("config")
    assert _config(home) == {"delegation": {"backend": "profile_routes"}}


@needs_model_picker
def test_add_a_model_route_like_hermes_model_then_finish_in_model_routes_mode(terminal, home):
    terminal.menu(DOWN, ENTER)                 # mode picker -> Model routes
    terminal.menu(ENTER)                       # routes list (empty) -> "+ Add a route"
    terminal.type("fast", "Cheap and quick: lookups and summaries.")
    terminal.menu(ENTER)                       # provider picker -> OpenRouter (first configured provider)
    terminal.menu(ENTER)                       # hermes model's model picker -> first model
    terminal.menu(ENTER)                       # reasoning picker opens on "Inherit parent"
    terminal.menu(DOWN, DOWN, ENTER)           # routes list: fast, + Add a route, Done -> "Done"
    terminal.menu(ENTER)                       # no default yet: default route -> fast
    _run("config")
    delegation = _config(home)["delegation"]
    assert delegation["backend"] == "model_map"
    assert delegation["backends"]["model_map"]["default"] == "fast"
    assert delegation["backends"]["model_map"]["options"] == [{
        "name": "fast", "description": "Cheap and quick: lookups and summaries.", "provider": "openrouter",
        "model": "google/gemini-3-flash-preview"}]
    assert "OpenRouter" in terminal.shown(2) and "Anthropic" in terminal.shown(2)  # the provider list
    assert "Select the model for this route" in terminal.shown(3)  # core's model picker, titled for a route


def test_routes_list_shows_name_description_and_route_and_sets_the_default(terminal, home):
    _write(home, {"delegation": {"backends": {"model_map": {"options": [
        {"name": "fast", "description": "Quick lookups.", "provider": "openrouter", "model": "m1"},
        {"name": "deep", "description": "Careful design work.", "provider": "anthropic", "model": "claude-opus-5",
         "reasoning_effort": "high"}]}}}})
    terminal.menu(DOWN, ENTER)                 # Model routes
    terminal.menu(DOWN, ENTER)                 # routes list -> "deep"
    terminal.menu(DOWN, ENTER)                 # route actions -> "Make it the default"
    terminal.menu(*[DOWN] * 3, ENTER)          # routes list: fast, deep, + Add a route, Done -> "Done"
    _run("config")
    listing = terminal.shown(1)
    for text in ("fast", "Quick lookups.", "openrouter · m1", "deep", "Careful design work.",
                 "anthropic · claude-opus-5 · reasoning high"):
        assert text in listing
    model_map = _config(home)["delegation"]["backends"]["model_map"]
    assert model_map["default"] == "deep"
    assert _config(home)["delegation"]["backend"] == "model_map"


@needs_model_picker
def test_edit_keeps_unedited_keys_and_delete_removes_a_route(terminal, home):
    _write(home, {"delegation": {"backends": {"model_map": {"options": [
        {"name": "fast", "description": "Quick.", "provider": "openrouter", "model": "m1",
         "fallback_providers": [{"provider": "anthropic", "model": "claude-haiku-5"}],
         "max_iterations": 15, "child_timeout_seconds": 120},
        {"name": "old", "description": "Gone soon.", "provider": "openrouter", "model": "m2"}]}}}})
    terminal.menu(DOWN, ENTER)                 # Model routes
    terminal.menu(ENTER)                       # routes list -> "fast"
    terminal.menu(ENTER)                       # route actions -> "Edit"
    terminal.type("", "Quick lookups and summaries.")   # keep the name, new description
    terminal.menu(DOWN, ENTER)                 # provider -> Anthropic
    terminal.menu(ENTER)                       # model -> claude-opus-5
    terminal.menu(DOWN, ENTER)                 # reasoning: "Inherit parent" -> "Skip (keep current)"
    terminal.menu(ENTER)                       # speed tier (Opus offers fast) -> "Normal"
    terminal.menu(DOWN, ENTER)                 # routes list -> "old"
    terminal.menu(DOWN, DOWN, ENTER)           # route actions -> "Delete"
    terminal.menu(QUIT)                        # leave the editor without changing the mode
    _run("config")
    delegation = _config(home)["delegation"]
    assert delegation["backends"]["model_map"]["options"] == [{
        "name": "fast", "description": "Quick lookups and summaries.", "provider": "anthropic",
        "model": "claude-opus-5", "fallback_providers": [{"provider": "anthropic", "model": "claude-haiku-5"}],
        "max_iterations": 15, "child_timeout_seconds": 120}]
    assert "backend" not in delegation  # quitting the editor keeps the mode; route edits are saved


@needs_model_picker
def test_a_repeated_route_name_is_asked_again(terminal, capsys, home):
    _write(home, {"delegation": {"backends": {"model_map": {"options": [
        {"name": "fast", "description": "Quick.", "provider": "openrouter", "model": "m1"}]}}}})
    terminal.menu(DOWN, ENTER)                 # Model routes
    terminal.menu(DOWN, ENTER)                 # routes list -> "+ Add a route"
    terminal.type("fast", "deep", "Careful work.")
    terminal.menu(DOWN, ENTER)                 # provider -> Anthropic
    terminal.menu(ENTER)                       # model -> claude-opus-5
    terminal.menu(*[UP] * 5, ENTER)            # reasoning: "Inherit parent" -> high
    terminal.menu(DOWN, ENTER)                 # speed tier -> Fast
    terminal.menu(QUIT)                        # leave the editor
    _run("config")
    assert "already named 'fast'" in capsys.readouterr().out
    tiers = terminal.shown(5)
    assert "Fast" in tiers and "Ultrafast" not in tiers  # only the tiers this route offers
    options = _config(home)["delegation"]["backends"]["model_map"]["options"]
    assert [o["name"] for o in options] == ["fast", "deep"]
    assert (options[1]["reasoning_effort"], options[1]["service_tier"]) == ("high", "fast")


def test_done_without_a_default_asks_for_one_with_no_default_as_an_explicit_choice(terminal, capsys, home):
    _write(home, {"delegation": {"backends": {"model_map": {"options": [
        {"name": "fast", "description": "Quick lookups.", "provider": "openrouter", "model": "m1"},
        {"name": "deep", "description": "Careful work.", "provider": "anthropic", "model": "claude-opus-5"}]}}}})
    terminal.menu(DOWN, ENTER)                 # Model routes
    terminal.menu(*[DOWN] * 3, ENTER)          # routes list: fast, deep, + Add a route, Done -> "Done"
    terminal.menu(DOWN, DOWN, ENTER)           # default route: fast, deep, No default -> "No default"
    _run("config")
    asked = terminal.shown(2)
    assert "fast" in asked and "deep" in asked
    assert "No default — unrouted tasks use the main agent's model" in asked
    delegation = _config(home)["delegation"]
    assert delegation["backend"] == "model_map" and not delegation["backends"]["model_map"].get("default")
    assert "no default" in capsys.readouterr().out.lower()


def test_cancelling_the_default_question_goes_back_to_the_routes_list(terminal, home):
    _write(home, {"delegation": {"backends": {"model_map": {"options": [
        {"name": "fast", "description": "Quick lookups.", "provider": "openrouter", "model": "m1"}]}}}})
    terminal.menu(DOWN, ENTER)                 # Model routes
    terminal.menu(DOWN, DOWN, ENTER)           # routes list -> "Done"
    terminal.menu(QUIT)                        # default question cancelled -> back to the list
    terminal.menu(QUIT)                        # leave the editor
    _run("config")
    assert "backend" not in _config(home)["delegation"]


def test_finishing_with_no_routes_leaves_the_mode_alone(terminal, capsys, home):
    terminal.menu(DOWN, ENTER)                 # Model routes
    terminal.menu(DOWN, ENTER)                 # empty routes list -> "Done"
    _run("config")
    assert "backend" not in _config(home).get("delegation", {})
    assert "No routes yet" in capsys.readouterr().out


def _profile(home, name):
    (home / "profiles" / name).mkdir(parents=True)
    (home / "profiles" / name / "config.yaml").write_text("model:\n  default: m\n", encoding="utf-8")


def test_profile_routes_is_a_multi_select_of_existing_profiles_not_the_model_editor(terminal, home):
    _profile(home, "coder")
    _profile(home, "researcher")
    terminal.menu(DOWN, DOWN, ENTER)           # mode picker -> Profile routes
    terminal.menu(SPACE, DOWN, SPACE, ENTER)   # checklist of the other profiles: [coder], [researcher]
    terminal.type("Writes and refactors code.", "")   # one optional line per picked profile
    terminal.menu(ENTER)                       # default -> coder
    _run("config")
    assert "coder" in terminal.shown(1) and "researcher" in terminal.shown(1)
    delegation = _config(home)["delegation"]
    assert delegation["backend"] == "profile_routes"
    assert delegation["backends"]["profile_routes"] == {
        "profiles": [{"name": "coder", "description": "Writes and refactors code."}, {"name": "researcher"}],
        "default": "coder"}


def test_only_newly_checked_profiles_are_asked_for_a_description(terminal, home):
    _profile(home, "coder")
    _profile(home, "researcher")
    _write(home, {"delegation": {"backends": {"profile_routes": {
        "profiles": [{"name": "coder", "description": "Writes code."}], "default": "coder"}}}})
    terminal.menu(DOWN, DOWN, ENTER)           # Profile routes
    terminal.menu(DOWN, SPACE, ENTER)          # coder stays checked, check researcher
    terminal.type("Web research.")             # asked for researcher only; coder keeps its line
    terminal.menu(ENTER)                       # default -> coder (current)
    _run("config")
    assert _config(home)["delegation"]["backends"]["profile_routes"]["profiles"] == [
        {"name": "coder", "description": "Writes code."}, {"name": "researcher", "description": "Web research."}]


def test_unchecking_every_profile_clears_the_list_after_confirming(terminal, capsys, home):
    _profile(home, "coder")
    _write(home, {"delegation": {"backend": "profile_routes", "backends": {"profile_routes": {
        "profiles": [{"name": "coder", "description": "Writes code."}], "default": "coder"}}}})
    terminal.menu(ENTER)                       # mode picker opens on the current mode, Profile routes
    terminal.menu(SPACE, ENTER)                # uncheck coder, the only one
    terminal.menu(ENTER)                       # confirm -> "Clear the list"
    _run("config")
    assert "Clear" in terminal.shown(2)
    delegation = _config(home)["delegation"]
    assert delegation["backends"]["profile_routes"]["profiles"] == []
    assert not delegation["backends"]["profile_routes"].get("default")
    assert delegation["backend"] == "default"  # a mode with nothing to pick is never left active
    assert "nothing changed" not in capsys.readouterr().out


def test_profile_routes_without_other_profiles_says_how_to_make_one(terminal, capsys, home):
    terminal.menu(DOWN, DOWN, ENTER)           # Profile routes
    _run("config")
    assert "hermes profile create" in capsys.readouterr().out
    assert "backend" not in _config(home).get("delegation", {})


def test_a_refused_write_ends_with_the_reason_and_exit_1(terminal, capsys, home, monkeypatch):
    cli.store.delegation_section()  # create the home layout first: a managed install never creates it
    monkeypatch.setattr("hermes_cli.config.is_managed", lambda: True)
    monkeypatch.setattr("hermes_cli.config.get_managed_system", lambda: "nix")
    terminal.menu(ENTER)                       # Default
    assert _run("config") == 1
    assert "nix" in capsys.readouterr().err.lower()
    assert not (home / "config.yaml").exists()
