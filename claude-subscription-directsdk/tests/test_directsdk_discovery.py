"""Installed-plugin discovery, the `claude` presence check, and offline native metadata contracts."""
import logging
import os
import sys
from pathlib import Path

import pytest

from conftest import PLUGIN_NAME


def test_installed_plugin_registers_and_constructs_client_without_native_process(profile, tmp_path):
    client = profile.create_client(command="unused-offline-native", env={})
    try:
        module = sys.modules[type(client).__module__]
        assert Path(module.__file__).resolve().parent == (Path(os.environ["HERMES_HOME"]) / "plugins" / PLUGIN_NAME).resolve()
        assert callable(client.chat.completions.create)
        assert profile.auth_type == "external_process" and profile.process_command == "claude"
        assert profile.supports_health_check is False
    finally:
        client.close()


def test_missing_claude_is_reported_on_every_path(profile, tmp_path, caplog):
    """No `claude` binary: setup says so, the picker degrades to the catalog, a request refuses with
    the install hint instead of a Popen traceback, and plugin load logged a warning."""
    env = {"PATH": str(tmp_path / "empty-bin"), "HOME": str(tmp_path)}
    status = profile.setup_status(env=env)
    assert status["available"] is False and "npm install -g @anthropic-ai/claude-code" in status["detail"]
    assert profile.discover_models(env=env) is None
    assert profile.fetch_models(env=env) is None

    client = profile.create_client(env=env)
    try:
        with pytest.raises(RuntimeError, match="not installed"):
            client.chat.completions.create(model="sonnet", messages=[{"role": "user", "content": "hi"}])
    finally:
        client.close()

    # Load-time check: importing the plugin logs the install hint (never raises) when Claude Code
    # is absent, so the provider stays selectable and `hermes model` can explain what is missing.
    import providers
    module_name = type(profile).__module__
    assert module_name.startswith("_hermes_user_provider_")
    del sys.modules[module_name]
    with caplog.at_level(logging.WARNING):
        providers._import_plugin_dir(Path(os.environ["HERMES_HOME"]) / "plugins" / PLUGIN_NAME, "user")
    assert any("Claude Code is not installed" in rec.getMessage() for rec in caplog.records)
    assert providers.get_provider_profile(PLUGIN_NAME) is not None


def test_resolve_honors_the_env_dict_not_the_process_path(profile, tmp_path):
    """_resolve must look up `claude` on the PATH of the env it is handed. The process PATH of a
    test/CI runner is unrelated: a host that happens to have claude installed must not make an
    env-scoped 'no claude' probe find it (the bug behind the flaky missing-CLI gate)."""
    from directsdk_setup import _resolve

    # Windows resolves `claude` through PATHEXT, so the fake needs a launcher extension there.
    fake = tmp_path / ("claude.cmd" if os.name == "nt" else "claude")
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    # Env PATH points at the fake CLI; the interpreter's process PATH does not contain tmp_path.
    env = {"PATH": str(tmp_path)}
    assert [p.lower() for p in _resolve(None, env)] == [str(fake).lower()]
    # And an env without any usable PATH resolves to nothing, even when the process PATH has claude.
    assert _resolve(None, {"PATH": str(tmp_path / "nowhere")}) is None
    # No PATH key at all falls back to exec's own default search path, not the interpreter's PATH.
    assert _resolve(None, {}) == _resolve(None, {"PATH": os.defpath})


def test_native_alias_metadata_is_bounded_and_never_claims_subscription_invoice(profile):
    from decimal import Decimal
    from agent.model_metadata import get_model_context_length
    from agent.usage_pricing import CanonicalUsage, estimate_usage_cost, normalize_usage

    for alias in profile.fallback_models:
        metadata = profile.model_metadata[alias]
        assert metadata["canonical_model"].startswith("claude-")
        assert 0 < profile.get_model_context_length(alias) <= metadata["context_window"]
        assert profile.get_model_context_length(metadata["canonical_model"]) == profile.get_model_context_length(alias)
        assert get_model_context_length(alias, provider=profile.name, base_url=profile.base_url) == profile.get_model_context_length(alias)
        assert get_model_context_length(alias, provider=profile.name, config_context_length=123456) == 123456
        cost = estimate_usage_cost(alias, CanonicalUsage(input_tokens=1000, output_tokens=100),
                                   provider=profile.name, base_url=profile.base_url)
        assert cost.status == "unknown"
        assert cost.amount_usd is None
    reported = {"prompt_tokens": 50, "completion_tokens": 10,
                "native_cost": {"total_cost_usd": .012345, "modelUsage": {"claude-sonnet-5": {"costBasis": "list"}}}}
    usage = normalize_usage(reported, provider=profile.name)
    cost = estimate_usage_cost("sonnet", usage, provider=profile.name, base_url=profile.base_url)
    # Subscription-included (Claude Max): $0 out-of-pocket, native list price on the gauge.
    assert cost.amount_usd == Decimal("0")
    assert cost.status == "included"
    assert cost.source == "subscription_included"
    assert cost.list_price_usd == Decimal("0.012345")
    assert any("list-price equivalent" in note for note in cost.notes)
    for invalid in (float("nan"), float("inf"), -1, True, None):
        reported["native_cost"]["total_cost_usd"] = invalid
        cost = estimate_usage_cost("sonnet", normalize_usage(reported), provider=profile.name)
        assert cost.status == "unknown" and cost.amount_usd is None
