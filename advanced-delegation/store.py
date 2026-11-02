"""Read and write the delegation settings this plugin owns, for ``hermes delegation config`` and
``/delegation``.

Reads use the effective config (``load_config_readonly``); writes go through core's
``atomic_config_write`` on the profile's own ``config.yaml`` (comment-preserving, and it refuses a
write that would drop existing settings). Only ``delegation.backend`` and
``delegation.backends.<mode>`` are ever written.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Mapping, Sequence

#: ``delegation.backend`` value -> the name users see.
MODES: dict[str, str] = {"default": "Default", "model_map": "Model routes", "profile_routes": "Profile routes"}
MODE_BLURBS: dict[str, str] = {
    "default": "every subagent uses delegation.model / delegation.provider (else your main model)",
    "model_map": "the agent picks a named model route for each subagent",
    "profile_routes": "the agent runs a subagent as one of your Hermes profiles",
}
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
#: Route keys the editor writes, in the order they are saved.
ROUTE_KEYS = ("name", "description", "provider", "base_url", "model", "reasoning_effort", "service_tier")


def delegation_section(config: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
    """The ``delegation`` section of the active profile's effective config (read-only)."""
    if config is None:
        from hermes_cli.config import load_config_readonly
        config = load_config_readonly() or {}
    section = config.get("delegation") if isinstance(config, Mapping) else None
    return section if isinstance(section, Mapping) else {}


def current_mode(section: Mapping[str, Any]) -> str:
    raw = section.get("backend")
    return raw.strip().lower() if isinstance(raw, str) and raw.strip() else "default"


def _block(section: Mapping[str, Any], mode: str) -> Mapping[str, Any]:
    backends = section.get("backends")
    block = backends.get(mode) if isinstance(backends, Mapping) else None
    return block if isinstance(block, Mapping) else {}


def _entries(block: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
    raw = block.get(key)
    return [dict(e) for e in raw if isinstance(e, Mapping)] if isinstance(raw, list) else []


def _text(value: Any) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def routes(section: Mapping[str, Any]) -> list[dict[str, Any]]:
    return _entries(_block(section, "model_map"), "options")


def route_default(section: Mapping[str, Any]) -> str:
    return _text(_block(section, "model_map").get("default"))


def listed_profiles(section: Mapping[str, Any]) -> list[dict[str, Any]]:
    return _entries(_block(section, "profile_routes"), "profiles")


def profile_default(section: Mapping[str, Any]) -> str:
    return _text(_block(section, "profile_routes").get("default"))


# --- writes -----------------------------------------------------------------------------------

def _write(mutate: Callable[[dict[str, Any]], None]) -> None:
    """Raises RuntimeError (with the user-facing reason) when the config must not be written: a
    package-manager-managed install, or an unreadable / would-lose-settings file."""
    from hermes_cli.config import (atomic_config_write, format_managed_message, get_config_path, is_managed,
                                   require_readable_config_before_write)
    if is_managed():
        raise RuntimeError(format_managed_message("change delegation settings"))
    path = get_config_path()
    data = require_readable_config_before_write(path)
    section = data.get("delegation")
    if not isinstance(section, dict):
        section = data["delegation"] = {}
    mutate(section)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_config_write(path, data)


def _backend_block(section: dict[str, Any], mode: str) -> dict[str, Any]:
    backends = section.get("backends")
    if not isinstance(backends, dict):
        backends = section["backends"] = {}
    block = backends.get(mode)
    if not isinstance(block, dict):
        block = backends[mode] = {}
    return block


def set_mode(mode: str) -> None:
    """Persist ``delegation.backend`` (``default`` | ``model_map`` | ``profile_routes``)."""
    if mode not in MODES:
        raise ValueError(f"Unknown delegation mode {mode!r}; use one of: {', '.join(MODES)}.")
    _write(lambda section: section.__setitem__("backend", mode))


def _save_list(mode: str, key: str, entries: list[dict[str, Any]], default: str) -> None:
    def mutate(section: dict[str, Any]) -> None:
        block = _backend_block(section, mode)
        block[key] = entries
        if default or "default" in block:
            # Blank, never removed: atomic_config_write refuses a write that drops an existing key.
            block["default"] = default
    _write(mutate)


def save_routes(options: list[dict[str, Any]], default: str = "") -> None:
    _save_list("model_map", "options", options, default)


def save_profiles(entries: list[dict[str, Any]], default: str = "") -> None:
    _save_list("profile_routes", "profiles", entries, default)


# --- validation -------------------------------------------------------------------------------

def _provider_known(provider: str) -> bool:
    from hermes_cli.config import get_compatible_custom_providers, load_config_readonly
    from hermes_cli.providers import resolve_provider_full
    config = load_config_readonly() or {}
    return resolve_provider_full(provider, config.get("providers"), get_compatible_custom_providers(config)) is not None


def _tier_problem(route: Mapping[str, Any]) -> str:
    word = _text(route.get("service_tier"))
    if not word:
        return ""
    from agent.fast_mode import parse_exact_service_tier
    try:
        tier = parse_exact_service_tier(word)
    except ValueError:
        return f"Unknown speed tier {word!r}; use normal, fast or ultrafast."
    if not tier:
        return ""
    if not offered_tier(route, tier):
        return (f"Speed tier {word!r} is not offered for {route.get('model')} on "
                f"{route.get('provider') or route.get('base_url')}; every spawn on this route would be refused.")
    return ""


def offered_tier(route: Mapping[str, Any], tier: str) -> bool:
    """Whether the route's model/provider offers ``tier`` (``priority`` or ``ultrafast``), by the same
    gate as ``/fast``. Core re-checks at spawn (for ultrafast also against the Codex account)."""
    from hermes_cli.models import resolve_fast_mode_overrides
    return bool(resolve_fast_mode_overrides(
        _text(route.get("model")) or None, provider=_text(route.get("provider")) or None,
        base_url=_text(route.get("base_url")) or None, tier=tier))


def validate_route(route: Mapping[str, Any], others: Sequence[Mapping[str, Any]]) -> list[str]:
    """Problems that would make core skip or refuse this route (empty list = savable). ``others`` are
    the other routes, for the unique-name check."""
    problems: list[str] = []
    name = _text(route.get("name"))
    if not _NAME_RE.match(name):
        problems.append("A route needs a short name (letters, digits, '.', '_' or '-'; no spaces): "
                        "it is the value the agent picks.")
    elif name in {_text(o.get("name")) for o in others}:
        problems.append(f"Another route is already named {name!r}.")
    if not _text(route.get("description")):
        problems.append("A route needs a one-line description: it is what the agent reads to choose.")
    if not _text(route.get("model")):
        problems.append("A route needs a model.")
    provider = _text(route.get("provider"))
    if not provider and not _text(route.get("base_url")):
        problems.append("A route needs a provider or a base_url.")
    elif provider and not _provider_known(provider):
        problems.append(f"Unknown provider {provider!r} (see `hermes model` for the providers Hermes knows).")
    effort = route.get("reasoning_effort")
    if effort not in (None, ""):
        from hermes_constants import VALID_REASONING_EFFORTS, parse_reasoning_effort
        if parse_reasoning_effort(effort) is None:
            problems.append(f"Unknown reasoning effort {effort!r}; use none or one of: "
                            f"{', '.join(VALID_REASONING_EFFORTS)}.")
    tier = _tier_problem(route) if not problems else ""
    if tier:
        problems.append(tier)
    return problems


# --- display ----------------------------------------------------------------------------------

def route_summary(route: Mapping[str, Any]) -> str:
    """``provider · model [· reasoning X] [· tier] [· N turns] [· N s idle]`` for one route."""
    where = _text(route.get("provider")) or _text(route.get("base_url"))
    parts = [where, _text(route.get("model"))]
    if _text(route.get("reasoning_effort")):
        parts.append(f"reasoning {_text(route.get('reasoning_effort'))}")
    if _text(route.get("service_tier")) and _text(route.get("service_tier")) != "normal":
        parts.append(_text(route.get("service_tier")))
    # The editor never asks about the budget keys but keeps them, so the list must show what it keeps.
    if route.get("max_iterations") not in (None, ""):
        parts.append(f"{route['max_iterations']} turns")
    if route.get("child_timeout_seconds") not in (None, ""):
        parts.append(f"{route['child_timeout_seconds']} s idle")
    return " · ".join(p for p in parts if p)


def mode_label(mode: str) -> str:
    return MODES.get(mode, mode)


def describe(section: Mapping[str, Any]) -> str:
    """The ``--show`` text: the mode and what it routes."""
    mode = current_mode(section)
    lines = [f"Delegation mode: {mode_label(mode)} (delegation.backend: {mode})"]
    if mode == "model_map":
        options, default = routes(section), route_default(section)
        lines.append(f"  Model routes ({len(options)}){f', default: {default}' if default else ''}:")
        width = max((len(_text(o.get('name'))) for o in options), default=0)
        for option in options:
            name = _text(option.get("name"))
            mark = "  (default)" if name == default else ""
            lines.append(f"    {name.ljust(width)}  {_text(option.get('description'))}")
            lines.append(f"    {''.ljust(width)}  {route_summary(option)}{mark}")
        if not options:
            lines.append("    (none yet: run `hermes delegation config`)")
    elif mode == "profile_routes":
        entries, default = listed_profiles(section), profile_default(section)
        lines.append(f"  Profile routes ({len(entries)}){f', default: {default}' if default else ''}:")
        width = max((len(_text(e.get('name'))) for e in entries), default=0)
        for entry in entries:
            name = _text(entry.get("name"))
            mark = "  (default)" if name == default else ""
            lines.append(f"    {name.ljust(width)}  {_text(entry.get('description'))}{mark}".rstrip())
        if not entries:
            lines.append("    (none yet: run `hermes delegation config`)")
    if mode == "default":
        model = _text(section.get("model")) or "your main model"
        provider = _text(section.get("provider")) or _text(section.get("base_url"))
        lines.append(f"  Subagents use delegation.model: {model}{f' on {provider}' if provider else ''}")
    else:
        lines.append("  Tasks that pick no route run on the mode's default, else the parent's model; "
                     "delegation.model / delegation.provider are inactive in this mode.")
    return "\n".join(lines)
