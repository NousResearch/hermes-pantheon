"""The ``model_map`` delegation provider.

Settings live in ``delegation.backends.model_map`` of config.yaml::

    delegation:
      backend: model_map
      backends:
        model_map:
          options:
            - name: fast                    # what the model picks
              description: Cheap and quick; lookups, summaries, simple edits.
              provider: openrouter          # provider and/or base_url, plus model
              model: google/gemini-3-flash-preview
            - name: deep
              description: Slow and careful; hard debugging and design work.
              provider: anthropic
              model: claude-opus-5
              reasoning_effort: high
              service_tier: fast            # optional; core refuses the spawn if the route has no fast tier
              max_iterations: 60            # optional; turn limit for this route's children
              child_timeout_seconds: 300    # optional; idle timeout for this route's children
            - name: local
              description: Private; runs on this machine.
              base_url: http://localhost:8080/v1
              model: qwen3-coder
          default: fast                     # optional: option for tasks that set none

``delegate_task`` gains a per-task ``option`` enum of those names. A task that sets one runs on that
entry's route; a task without one runs on ``default`` when set, else on the parent's own route
(in this mode ``delegation.model`` / ``delegation.provider`` are inactive). The raw
provider/model/endpoint/key never become model-facing parameters.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from agent.delegation_provider import DelegationProvider

logger = logging.getLogger(__name__)

BACKEND = "model_map"
SETTINGS_PATH = f"delegation.backends.{BACKEND}"
PARAMETER = "option"
#: Entry keys copied into the route (same meaning as the matching core route fields).
ROUTE_FIELDS = ("provider", "model", "base_url", "api_key", "api_mode", "reasoning_effort", "service_tier")
#: Per-route budget keys forwarded as written: a turn limit and an idle timeout for this route's children,
#: overriding ``delegation.max_iterations`` / ``delegation.child_timeout_seconds``. Core owns the rules.
ROUTE_LIMITS = ("max_iterations", "child_timeout_seconds")
_warned: set[str] = set()


def _warn_once(message: str, *args: Any) -> None:
    # task_parameters/route_task run on every session build and every task; say each problem once.
    text = message % args
    if text not in _warned:
        _warned.add(text)
        logger.warning(text)


def _text(entry: Mapping[str, Any], key: str) -> str:
    value = entry.get(key)
    return " ".join(value.split()) if isinstance(value, str) else ""


def _entry_problem(entry: Any) -> str:
    if not isinstance(entry, Mapping):
        return "is not a mapping"
    if not _text(entry, "name") or not _text(entry, "description"):
        return "needs a name and a description"
    if not _text(entry, "model"):
        return "needs a model"
    if not (_text(entry, "provider") or _text(entry, "base_url")):
        return "needs a provider or a base_url"
    return ""


def configured_options(settings: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """``{name: entry}`` from the backend settings' ``options`` in config order, skipping malformed
    entries and repeated names (each skipped entry is warned once). Each entry keeps its description
    plus the non-empty route fields."""
    raw = settings.get("options")
    if raw is None:
        return {}
    if not isinstance(raw, list):
        _warn_once("%s.options must be a list of {name, description, provider/base_url, model} entries; "
                   "ignoring it.", SETTINGS_PATH)
        return {}
    options: dict[str, dict[str, Any]] = {}
    for i, entry in enumerate(raw):
        problem = _entry_problem(entry)
        if problem:
            _warn_once("%s.options[%d] %s; skipped.", SETTINGS_PATH, i, problem)
            continue
        name = _text(entry, "name")
        if name in options:
            _warn_once("%s.options[%d] repeats option %r; skipped.", SETTINGS_PATH, i, name)
            continue
        route = {key: entry[key].strip() for key in ROUTE_FIELDS
                 if isinstance(entry.get(key), str) and entry[key].strip()}
        if isinstance(entry.get("fallback_providers"), list):
            route["fallback_providers"] = list(entry["fallback_providers"])
        for limit in ROUTE_LIMITS:
            if entry.get(limit) not in (None, ""):
                route[limit] = entry[limit]  # core validates it and refuses a bad value for that task
        options[name] = {"description": _text(entry, "description"), "route": route}
    return options


class ModelMapDelegationProvider(DelegationProvider):
    """``delegation.backend: model_map``: the model picks a named model option per subagent, from the
    options in ``delegation.backends.model_map``."""

    @property
    def name(self) -> str:
        return BACKEND

    @property
    def display_name(self) -> str:
        return "Model routes"

    @property
    def summary(self) -> str:
        return "The agent picks a named model route for each subagent."

    def task_parameters(self, config: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        options = configured_options(self.settings(config))
        if not options:
            return {}
        lines = [
            "Which configured model runs this subagent. Omit for the default subagent route.",
            *(f"{name}: {option['description']}" for name, option in options.items()),
        ]
        return {PARAMETER: {"type": "string", "enum": list(options), "description": "\n".join(lines)}}

    def description(self, config: Mapping[str, Any]) -> str:
        if not configured_options(self.settings(config)):
            return ""
        return "Each task may set `option` to run that subagent on one of the listed models."

    def route_task(self, task: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
        settings = self.settings(config)
        options = configured_options(settings)
        chosen = task.get(PARAMETER)
        if not chosen:
            default = settings.get("default")
            chosen = default.strip() if isinstance(default, str) else ""
            if chosen and chosen not in options:
                _warn_once("%s.default %r is not an option in %s.options; tasks without an option use the "
                           "default route.", SETTINGS_PATH, chosen, SETTINGS_PATH)
                chosen = ""
        if not chosen:
            return {}  # the parent's route: delegation.model / delegation.provider are inactive in this mode
        # Core already refused a chosen value outside the enum before calling us.
        return {**options[chosen]["route"], "label": chosen}
