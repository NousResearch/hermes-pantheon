"""Setup for the provider is driven by the Claude CLI itself: its auth status gates the flow and its
own model picker (initialize handshake) supplies the list, with the pinned catalog as fallback."""
import json
import os
import sys
import textwrap

import pytest


def _install(path):
    binary = path / ("claude.exe" if os.name == "nt" else "claude")
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("")
    binary.chmod(0o755)
    return binary


def _service_env(tmp_path):
    """A LaunchAgent/Desktop-spawned backend: the user's home, but a PATH that carries no install prefix."""
    return {"HOME": str(tmp_path), "USERPROFILE": str(tmp_path), "PATH": str(tmp_path / "empty")}


@pytest.mark.parametrize("prefix", [".local/bin", ".claude/local", "bin", ".npm-global/bin", ".bun/bin", ".volta/bin"])
def test_resolves_claude_outside_service_path(tmp_path, prefix):
    from directsdk_setup import _resolve

    binary = _install(tmp_path / prefix)
    resolved = _resolve(["claude", "--verbose"], _service_env(tmp_path))
    assert resolved is not None
    assert os.path.normcase(resolved[0]) == os.path.normcase(str(binary))
    assert resolved[1:] == ["--verbose"]


def test_path_and_explicit_commands_take_precedence_over_the_probe(tmp_path):
    from directsdk_setup import _resolve

    _install(tmp_path / ".local/bin")
    on_path = _install(tmp_path / "path-bin")
    env = {**_service_env(tmp_path), "PATH": str(tmp_path / "path-bin")}
    assert os.path.normcase(_resolve(None, env)[0]) == os.path.normcase(str(on_path))
    override = str(tmp_path / ".local/bin" / on_path.name)
    assert _resolve(None, {**env, "CLAUDE_SUBSCRIPTION_DIRECTSDK_COMMAND": override}) == [override]
    assert _resolve([str(tmp_path / "missing" / on_path.name)], env) is None
    assert _resolve(["custom-claude-wrapper"], env) is None


def test_core_finds_a_cli_that_is_only_in_an_install_prefix(tmp_path, monkeypatch):
    """Core checks `process_command` with a PATH-only which() before the plugin runs (#32): the agent build of a
    service-launched backend must not fail with "Could not find ... CLI command 'claude'"."""
    import shutil
    from pathlib import Path

    import providers
    from hermes_cli.auth import resolve_external_process_provider_credentials

    binary = _install(tmp_path / ".local/bin")
    monkeypatch.delenv("CLAUDE_SUBSCRIPTION_DIRECTSDK_COMMAND", raising=False)
    for key, value in _service_env(tmp_path).items():
        monkeypatch.setenv(key, value)
    # Install and discover the plugin under the service environment, as such a backend starting up would.
    home = tmp_path / "hermes-home"
    root = Path(__file__).resolve().parents[1]
    shutil.copytree(root, home / "plugins" / "claude-subscription-directsdk-experimental",
                    ignore=shutil.ignore_patterns(".git", "tests", "evals", "__pycache__"))
    (home / "config.yaml").write_text("plugins:\n  enabled: []\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    for name in tuple(sys.modules):
        if name.startswith("_hermes_user_provider_"):
            monkeypatch.delitem(sys.modules, name)
    monkeypatch.setattr(providers, "_REGISTRY", {})
    monkeypatch.setattr(providers, "_ALIASES", {})
    monkeypatch.setattr(providers, "_PROVIDER_LIST_CACHE", None)
    monkeypatch.setattr(providers, "_discovered", False)
    providers._discover_providers()
    creds = resolve_external_process_provider_credentials("claude-subscription-directsdk-experimental")
    assert os.path.normcase(creds["command"]) == os.path.normcase(str(binary))

FAKE_CLI = textwrap.dedent('''
    import json, os, sys
    state = json.loads(os.environ["FAKE_STATE"])
    if sys.argv[1:3] == ["auth", "status"]:
        print(json.dumps(state["auth"])); sys.exit(0 if state["auth"]["loggedIn"] else 1)
    assert "-p" in sys.argv and "--input-format" in sys.argv, sys.argv
    req = json.loads(sys.stdin.readline())
    assert req["request"]["subtype"] == "initialize"
    if state.get("hang_upstream"):
        import urllib.request
        urllib.request.urlopen(os.environ["ANTHROPIC_BASE_URL"] + "/v1/messages", data=b"{}")
    print(json.dumps({"type": "control_response", "response": {"subtype": "success", "request_id": req["request_id"],
          "response": {"models": state["models"], "account": state["account"]}}}))
''')


def _cli(tmp_path, state):
    path = tmp_path / "claude.py"
    path.write_text(FAKE_CLI)
    return [sys.executable, str(path)], {**os.environ, "FAKE_STATE": json.dumps(state), "PATH": os.defpath}


PRO = {"auth": {"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "pro"},
       "account": {"subscriptionType": "Claude Pro"}}
PINNED_PICKER = [
    {"value": "sonnet[1m]", "resolvedModel": "claude-sonnet-5[1m]", "displayName": "Sonnet 5 (1M context)", "description": "Sonnet 5 for long sessions"},
    {"value": "opus", "resolvedModel": "claude-opus-5-5", "displayName": "Opus", "description": "Opus 5.5 · Best for everyday, complex tasks"},
    {"value": "opus[1m]", "resolvedModel": "claude-opus-5-5[1m]", "displayName": "Opus (1M context)", "description": "Opus 5.5 with 1M context · Draws from usage credits · $4/$20 per Mtok"},
    {"value": "haiku", "resolvedModel": "claude-haiku-4-5-20251001", "displayName": "Haiku", "description": "Haiku 4.5 · Fastest for quick answers"},
]
# Models the pinned table has never heard of (fictitious on purpose): a plain + [1m] pair behind the
# CLI's own `opus` alias, a plain-only id in a family Hermes guesses at 1M, and a [1m]-only id.
UNPINNED_PICKER = [
    {"value": "opus", "resolvedModel": "claude-opus-9", "displayName": "Opus", "description": "Opus 9 · Best for everyday, complex tasks"},
    {"value": "opus[1m]", "resolvedModel": "claude-opus-9[1m]", "displayName": "Opus (1M context)", "description": "Opus 9 with 1M context · Draws from usage credits · $5/$25 per Mtok"},
    {"value": "claude-sonnet-5-9", "resolvedModel": "claude-sonnet-5-9", "displayName": "Sonnet", "description": "Sonnet 5.9 · Efficient for routine tasks"},
    {"value": "claude-fable-9[1m]", "resolvedModel": "claude-fable-9[1m]", "displayName": "Fable", "description": "Fable 9 · Most capable for your hardest tasks"},
    # An alias row the CLI leaves unresolved is not a model and must not become a route.
    {"value": "default", "displayName": "Default (recommended)", "description": "Opus 9 · Best for everyday, complex tasks"},
]


def _discover(profile, tmp_path, models):
    command, env = _cli(tmp_path, {**PRO, "models": models})
    return profile.discover_models(command=command, env=env)


def test_setup_status_reports_login_and_models_from_the_cli(profile, tmp_path):
    command, env = _cli(tmp_path, {**PRO, "models": PINNED_PICKER})
    status = profile.setup_status(command=command, env=env)
    assert status["available"] and status["logged_in"] and status["plan"] == "Claude Pro"
    assert status["login_command"] == command + ["auth", "login"]

    models = profile.discover_models(command=command, env=env)
    ids = [m["id"] for m in models]
    # Native picker rows are deduplicated to their Hermes route ids (opus and opus[1m] both -> opus 1M)
    assert ids[:3] == ["claude-sonnet-5[1m]", "claude-opus-5-5[1m]", "claude-haiku-4-5-20251001"]
    assert [m["label"] for m in models[:3]] == ["Sonnet 5 for long sessions", "Opus 5.5", "Haiku 4.5"]
    assert models[1]["note"] == "usage credits"
    assert models[2]["note"] == ""
    # Discovery goes through the admission relay with zero upstream requests
    assert all(m["upstream_requests"] == 0 for m in models)


def test_every_model_the_cli_advertises_is_selectable(profile, tmp_path):
    """The pinned table adds metadata (1M route, window, aliases); it never decides visibility. A model
    it does not know keeps the CLI's own id and label, gains no [1m], and is marked unpinned."""
    pinned = _discover(profile, tmp_path, PINNED_PICKER)
    models = _discover(profile, tmp_path, PINNED_PICKER + UNPINNED_PICKER)
    # Newcomers leave the pinned rows exactly as they were.
    assert [m for m in models if "unpinned" not in m["note"]] == pinned
    fresh = {m["id"]: m for m in models if "unpinned" in m["note"]}
    # Every advertised model is listed under an id the CLI announced: the plain-only id stays plain,
    # and a plain + [1m] pair collapses onto its [1m] form, as pinned 1M models do.
    assert sorted(fresh) == ["claude-fable-9[1m]", "claude-opus-9[1m]", "claude-sonnet-5-9"]
    assert {m["id"]: m["label"] for m in fresh.values()} == {
        "claude-opus-9[1m]": "Opus 9", "claude-sonnet-5-9": "Sonnet 5.9", "claude-fable-9[1m]": "Fable 9"}
    # Unpinned is visible, never at the cost of the CLI's own billing warning, which reads first.
    assert fresh["claude-opus-9[1m]"]["note"] == "usage credits · unpinned"
    assert fresh["claude-sonnet-5-9"]["note"] == fresh["claude-fable-9[1m]"]["note"] == "unpinned"
    assert all(m["upstream_requests"] == 0 for m in fresh.values())


def test_pinned_models_the_picker_omits_stay_selectable(profile, tmp_path):
    """The live picker names only each family's current model; older pinned models the account
    still runs (Opus 5, Opus 4.8) are appended after the advertised rows, never dropped."""
    from model_catalog import MODEL_METADATA
    models = _discover(profile, tmp_path, PINNED_PICKER + UNPINNED_PICKER)
    ids = [m["id"] for m in models]
    advertised = [m["id"] for m in _discover(profile, tmp_path, UNPINNED_PICKER) if "unpinned" in m["note"]]
    # Every pinned route is listed exactly once, whether or not the CLI announced it.
    assert set(MODEL_METADATA) <= set(ids) and len(ids) == len(set(ids))
    # Catalog-only rows follow every advertised row, in catalog order.
    appended = [r for r in MODEL_METADATA if r not in ("claude-sonnet-5[1m]", "claude-opus-5-5[1m]", "claude-haiku-4-5-20251001")]
    assert ids[-len(appended):] == appended
    assert set(advertised) <= set(ids[:-len(appended)])
    rows = {m["id"]: m for m in models}
    assert rows["claude-opus-4-8[1m]"]["label"] == "Opus 4.8"
    assert rows["claude-opus-5[1m]"]["label"] == "Opus 5"
    # Pro bills Fable to usage credits from the first request; the appended row says so too.
    assert rows["claude-fable-5-1[1m]"]["note"] == "usage credits"
    assert all(m["upstream_requests"] == 0 for m in models)


# What a CLI started with DISABLE_TELEMETRY / CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC advertises
# (#86): its feature-flag fetch is off, so only each family's current model is offered. The plugin
# sets those flags only when the user turns its claude_code_telemetry setting off (or exports one
# themselves); the pinned table fills the gap then.
PRIVACY_PICKER = [
    {"value": "opus", "resolvedModel": "claude-opus-5-5", "displayName": "Opus", "description": "Opus 5.5 · Best for everyday, complex tasks"},
    {"value": "opus[1m]", "resolvedModel": "claude-opus-5-5[1m]", "displayName": "Opus (1M context)", "description": "Opus 5.5 with 1M context"},
    {"value": "sonnet", "resolvedModel": "claude-sonnet-5", "displayName": "Sonnet", "description": "Sonnet 5 · Efficient for routine tasks"},
    {"value": "fable", "resolvedModel": "claude-fable-5-1", "displayName": "Fable", "description": "Fable 5.1 · Most capable for your hardest tasks"},
    # The undated alias id the CLI may report must land on the dated pinned route, not a second row.
    {"value": "haiku", "resolvedModel": "claude-haiku-4-5", "displayName": "Haiku", "description": "Haiku 4.5 · Fastest for quick answers"},
]


def test_privacy_flags_shrunk_picker_is_unioned_with_the_pinned_table(profile, tmp_path):
    """#86: the 5-row picker the privacy-flagged CLI returns is the floor, never the whole list. Every
    advertised model comes first, then each pinned route it omitted, once per canonical model."""
    from model_catalog import MODEL_METADATA
    models = _discover(profile, tmp_path, PRIVACY_PICKER)
    ids = [m["id"] for m in models]
    advertised = ["claude-opus-5-5[1m]", "claude-sonnet-5[1m]", "claude-fable-5-1[1m]", "claude-haiku-4-5-20251001"]
    assert ids[:len(advertised)] == advertised
    assert set(ids) == set(advertised) | set(MODEL_METADATA)
    assert {"claude-opus-5[1m]", "claude-opus-4-8[1m]"} <= set(ids[len(advertised):])
    # No route twice and no canonical model twice (aliases and plain/[1m] pairs collapse).
    assert len(ids) == len(set(ids))
    canonical = [MODEL_METADATA[i]["canonical_model"] for i in ids]
    assert len(canonical) == len(set(canonical))
    # Opus 4.6 reaches 1M only via [1m], which bills usage credits on Pro and is not plan-checked
    # behind our relay; the plugin must never offer that route on its own.
    assert not any(i.startswith("claude-opus-4-6") for i in ids)
    assert all(m["upstream_requests"] == 0 for m in models)


def test_hermes_never_budgets_a_discovered_row_past_the_native_window(profile, tmp_path):
    """Behind the relay native Claude Code runs a plain id within its 200K default and a [1m] id
    within 1M. Left to itself Hermes sizes an id by family substring (claude-sonnet-5-9 -> 1M), so a
    plain unpinned row would let a conversation outgrow the window the native client enforces."""
    from agent.model_metadata import get_model_context_length
    models = _discover(profile, tmp_path, PINNED_PICKER + UNPINNED_PICKER)
    assert "claude-sonnet-5-9" in {m["id"] for m in models}
    for m in models:
        native = 1_000_000 if m["id"].endswith("[1m]") else 200_000
        assert get_model_context_length(m["id"], provider=profile.name, base_url=profile.base_url) <= native, m["id"]


@pytest.mark.parametrize("auth", [
    {"loggedIn": True, "authMethod": "api_key", "apiProvider": "firstParty"},
    {"loggedIn": True, "authMethod": "api_key_helper", "apiProvider": "firstParty"},
    {"loggedIn": True, "authMethod": "third_party", "apiProvider": "anthropicAws"},
    {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "gateway", "subscriptionType": "max"},
])
def test_a_login_that_bills_api_usage_is_not_a_subscription_login(profile, tmp_path, auth):
    """`auth status` reports loggedIn for a Console key or cloud backend too; setup must not pass it as a subscription."""
    command, env = _cli(tmp_path, {"auth": auth, "account": {}, "models": PINNED_PICKER})
    status = profile.setup_status(command=command, env=env)
    assert status["available"] and not status["logged_in"]
    assert "bills API usage instead of your Claude subscription" in status["detail"]
    assert status["login_command"] == command + ["auth", "login"]
    assert profile.discover_models(command=command, env=env) is None


def test_a_setup_token_login_without_a_plan_is_a_subscription_login(profile, tmp_path):
    """`claude setup-token` / CLAUDE_CODE_OAUTH_TOKEN reports oauth_token and no subscriptionType; it stays valid."""
    command, env = _cli(tmp_path, {"auth": {"loggedIn": True, "authMethod": "oauth_token", "apiProvider": "firstParty"},
                                   "account": {"subscriptionType": "Claude Max"}, "models": PINNED_PICKER})
    status = profile.setup_status(command=command, env=env)
    assert status["logged_in"] and status["detail"] == ""


def test_logged_out_or_missing_cli_degrades_to_pinned_catalog(profile, tmp_path):
    command, env = _cli(tmp_path, {"auth": {"loggedIn": False, "authMethod": "none"}, "account": {}, "models": []})
    status = profile.setup_status(command=command, env=env)
    assert status["available"] and not status["logged_in"]
    assert profile.discover_models(command=command, env=env) is None

    missing = profile.setup_status(command=[str(tmp_path / "nope")], env=env)
    assert not missing["available"] and not missing["logged_in"]
    assert "install" in missing["detail"].lower()
