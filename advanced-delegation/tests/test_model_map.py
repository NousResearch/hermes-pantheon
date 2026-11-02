"""The ``model_map`` provider's contract: it reads its settings from ``delegation.backends.model_map``, declares
a per-task ``option`` parameter, adds guidance, and answers each task's route."""

from __future__ import annotations

import json
import logging

import pytest

pytest.importorskip("agent.delegation_provider", reason="Hermes core without pluggable delegation providers")

from advanced_delegation_plugin.model_map import ModelMapDelegationProvider, _warned  # noqa: E402

OPTIONS = [
    {"name": "fast", "description": "Cheap and quick.", "provider": "openrouter",
     "model": "google/gemini-3-flash-preview"},
    {"name": "deep", "description": "Slow and\n careful.", "provider": "anthropic", "model": "claude-opus-5",
     "reasoning_effort": "high", "service_tier": "fast", "api_mode": "", "base_url": None},
    {"name": "local", "description": "Runs on this machine.", "base_url": "http://localhost:8080/v1",
     "model": "qwen3-coder", "api_key": "local-key", "api_mode": "chat_completions"},
]


def _config(**settings):
    """A ``delegation`` section selecting this backend, with ``settings`` under ``backends.model_map``."""
    return {"backend": "model_map", "backends": {"model_map": settings}}


@pytest.fixture
def provider():
    _warned.clear()
    return ModelMapDelegationProvider()


def test_name_matches_the_backend_value_and_the_settings_key(provider):
    assert provider.name == "model_map"
    assert provider.settings(_config(options=OPTIONS)) == {"options": OPTIONS}


def test_option_parameter_is_an_enum_of_names_with_their_descriptions(provider):
    config = _config(options=OPTIONS)
    params = provider.task_parameters(config)
    assert list(params) == ["option"]
    option = params["option"]
    assert option["type"] == "string" and option["enum"] == ["fast", "deep", "local"]
    first, *entries = option["description"].split("\n")
    assert "model" in first and "Omit" in first
    assert entries == ["fast: Cheap and quick.", "deep: Slow and careful.", "local: Runs on this machine."]
    assert json.dumps(provider.task_parameters(config)) == json.dumps(params)  # cached prefix: same bytes


def test_raw_route_fields_never_reach_the_schema(provider):
    text = json.dumps(provider.task_parameters(_config(options=OPTIONS)))
    for secret_or_route in ("openrouter", "claude-opus-5", "localhost", "local-key", "chat_completions"):
        assert secret_or_route not in text


@pytest.mark.parametrize("config", [
    {}, {"backends": None}, {"backends": {"model_map": None}}, {"backends": {"other": {"options": OPTIONS}}},
    _config(), _config(options=[]), _config(options=None), _config(options={"fast": {}}),
])
def test_no_options_means_no_parameter_and_no_guidance(provider, config):
    assert provider.task_parameters(config) == {}
    assert provider.description(config) == ""


def test_settings_at_the_old_location_beside_core_keys_are_not_read(provider):
    config = {"backend": "model_map", "model_map": OPTIONS, "model_map_default": "fast", "options": OPTIONS,
              "default": "fast", "model": "m"}
    assert provider.task_parameters(config) == {}
    assert provider.description(config) == ""
    assert provider.route_task({"goal": "g", "index": 0}, config) == {}


def test_description_tells_the_model_it_may_set_option(provider):
    text = provider.description(_config(options=OPTIONS))
    assert "`option`" in text and text.count(".") == 1


def test_malformed_and_duplicate_options_are_skipped_with_one_warning_each_naming_the_new_path(provider, caplog):
    config = _config(options=[
        "fast",                                                                   # not a mapping
        {"name": "a", "provider": "openrouter", "model": "m"},                    # no description
        {"description": "d", "provider": "openrouter", "model": "m"},             # no name
        {"name": "b", "description": "d", "provider": "openrouter"},              # no model
        {"name": "c", "description": "d", "model": "m"},                          # no provider/base_url
        {"name": "ok", "description": "d", "provider": "openrouter", "model": "m"},
        {"name": "ok", "description": "again", "base_url": "http://x/v1", "model": "m2"},  # duplicate
    ])
    with caplog.at_level(logging.WARNING):
        params = provider.task_parameters(config)
        provider.route_task({"goal": "g", "index": 0, "option": "ok"}, config)
    assert params["option"]["enum"] == ["ok"]
    warnings = [r.getMessage() for r in caplog.records]
    assert len(warnings) == 6  # one per skipped entry, not repeated by the later route_task
    assert "delegation.backends.model_map.options[4] needs a provider or a base_url; skipped." in warnings
    assert "delegation.backends.model_map.options[6] repeats option 'ok'; skipped." in warnings


def test_options_that_are_not_a_list_are_ignored_with_a_warning_naming_the_new_path(provider, caplog):
    with caplog.at_level(logging.WARNING):
        assert provider.task_parameters(_config(options={"fast": {}})) == {}
    assert "delegation.backends.model_map.options must be a list" in caplog.text


def test_chosen_option_routes_with_only_its_non_empty_route_keys(provider):
    config = {**_config(options=OPTIONS), "model": "ignored", "provider": "ignored"}

    def route(option):
        return provider.route_task({"goal": "g", "context": None, "index": 0, "option": option}, config)

    assert route("fast") == {"provider": "openrouter", "model": "google/gemini-3-flash-preview", "label": "fast"}
    assert route("deep") == {"provider": "anthropic", "model": "claude-opus-5", "reasoning_effort": "high",
                             "service_tier": "fast", "label": "deep"}
    assert route("local") == {"base_url": "http://localhost:8080/v1", "model": "qwen3-coder", "api_key": "local-key",
                              "api_mode": "chat_completions", "label": "local"}


def test_an_options_fallback_providers_are_routed_to_core(provider):
    chain = [{"provider": "openrouter", "model": "google/gemini-3-flash-preview"}]
    config = _config(options=[{"name": "deep", "description": "d", "provider": "anthropic", "model": "claude-opus-5",
                               "fallback_providers": chain},
                              {"name": "bad", "description": "d", "provider": "anthropic", "model": "m",
                               "fallback_providers": "openrouter"}])
    assert provider.route_task({"goal": "g", "index": 0, "option": "deep"}, config)["fallback_providers"] == chain
    assert "fallback_providers" not in provider.route_task({"goal": "g", "index": 0, "option": "bad"}, config)


def test_an_options_service_tier_is_routed_to_core(provider):
    config = _config(options=[{"name": "quick", "description": "d", "provider": "openai-codex", "model": "gpt-5.5",
                               "service_tier": " ultrafast "}])
    route = provider.route_task({"goal": "g", "index": 0, "option": "quick"}, config)
    assert route["service_tier"] == "ultrafast"


def test_an_options_turn_limit_and_idle_timeout_are_routed_to_core_as_written(provider):
    config = _config(options=[{"name": "scout", "description": "d", "provider": "openrouter", "model": "m",
                               "max_iterations": 5, "child_timeout_seconds": 120.5},
                              {"name": "plain", "description": "d", "provider": "openrouter", "model": "m"}])
    route = provider.route_task({"goal": "g", "index": 0, "option": "scout"}, config)
    assert route["max_iterations"] == 5 and route["child_timeout_seconds"] == 120.5
    plain = provider.route_task({"goal": "g", "index": 1, "option": "plain"}, config)
    assert "max_iterations" not in plain and "child_timeout_seconds" not in plain


@pytest.mark.parametrize("bad", [0, "15", -1, True, 2.5])
def test_a_bad_limit_is_passed_through_for_core_to_refuse_not_hidden(provider, bad):
    # Core owns the rule and the refusal message; the plugin never silently drops the value.
    config = _config(options=[{"name": "scout", "description": "d", "provider": "openrouter", "model": "m",
                               "max_iterations": bad, "child_timeout_seconds": bad}])
    route = provider.route_task({"goal": "g", "index": 0, "option": "scout"}, config)
    assert route["max_iterations"] == bad and route["child_timeout_seconds"] == bad


def test_a_blank_limit_is_omitted_like_an_absent_one(provider):
    config = _config(options=[{"name": "scout", "description": "d", "provider": "openrouter", "model": "m",
                               "max_iterations": "  ", "child_timeout_seconds": None}])
    route = provider.route_task({"goal": "g", "index": 0, "option": "scout"}, config)
    assert "max_iterations" not in route and "child_timeout_seconds" not in route


def test_route_summary_shows_the_budget_the_editor_keeps():
    from advanced_delegation_plugin.store import route_summary
    assert route_summary({"provider": "openrouter", "model": "m", "max_iterations": 5, "child_timeout_seconds": 120}) \
        == "openrouter · m · 5 turns · 120 s idle"
    assert route_summary({"provider": "openrouter", "model": "m"}) == "openrouter · m"


def test_task_without_option_runs_on_the_parent_route_not_delegation_model(provider):
    # In this mode delegation.model / delegation.provider are inactive: an empty route inherits the parent.
    config = {**_config(options=OPTIONS), "model": "m", "provider": "openrouter", "base_url": "http://x/v1"}
    assert provider.route_task({"goal": "g", "context": None, "index": 1}, config) == {}


def test_task_without_option_uses_the_configured_default(provider):
    route = provider.route_task({"goal": "g", "index": 0}, _config(options=OPTIONS, default="local"))
    assert route["label"] == "local" and route["model"] == "qwen3-coder"


def test_default_outside_the_options_is_ignored_with_a_warning_naming_the_new_path(provider, caplog):
    config = {**_config(options=OPTIONS, default="huge"), "model": "m"}
    with caplog.at_level(logging.WARNING):
        assert provider.route_task({"goal": "g", "index": 0}, config) == {}
    assert ("delegation.backends.model_map.default 'huge' is not an option in "
            "delegation.backends.model_map.options") in caplog.text


def test_pickers_show_the_user_facing_name_and_one_line_summary(provider):
    # /delegation and core's backend list read these; the model never sees them.
    assert provider.display_name == "Model routes"
    assert provider.summary and "\n" not in provider.summary
