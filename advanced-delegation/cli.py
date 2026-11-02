"""``hermes delegation config``: pick the delegation mode and set it up.

* **Default**: sets ``delegation.backend: default``; ``delegation.model`` / ``delegation.provider``
  apply again.
* **Model routes**: a route editor built from ``hermes model``'s own pieces (the curses menus, the
  configured-provider inventory, the model picker with its selection guards, the reasoning picker).
  Each route is a name + description (what the agent reads), a provider or endpoint, a model and an
  optional reasoning effort and speed tier, validated before it is saved.
* **Profile routes**: a multi-select of your other profiles, an optional line about each, a default.

Every write goes through ``store`` (core's ``atomic_config_write``). ``--show`` prints the current
setup and needs no terminal; interactive editing refuses to run without one.
"""

from __future__ import annotations

import sys
from typing import Any, Mapping, Optional

from . import store
from .model_map import ROUTE_LIMITS

#: Stored route keys the editor never asks about but keeps through any edit (the rest survive only a same-endpoint edit).
_KEPT_ON_ANY_EDIT = ("fallback_providers", *ROUTE_LIMITS)

_NO_TTY = ("`hermes delegation config` needs an interactive terminal to edit routes.\n"
           "Print the current setup with `hermes delegation config --show`, or set keys directly with\n"
           "`hermes config set delegation.backend default|model_map|profile_routes`.")
_TIERS = (("fast", "priority"), ("ultrafast", "ultrafast"))  # word users write -> tier core checks


def _has_terminal() -> bool:
    # Both ends: Windows reports the NUL device as a tty, so stdin alone lets a piped run into curses.
    return sys.stdin.isatty() and sys.stdout.isatty()


def setup_parser(parser) -> None:
    sub = parser.add_subparsers(dest="delegation_command", metavar="<command>")
    config = sub.add_parser(
        "config", help="Pick the delegation mode and edit its routes",
        description="Pick how delegate_task routes subagents (Default, Model routes, Profile routes) "
                    "and edit the routes. Changes apply to new sessions.")
    config.add_argument("--show", action="store_true", help="Print the current mode and routes, then exit")
    parser.set_defaults(func=handle)


def handle(args) -> int:
    if getattr(args, "delegation_command", None) != "config":
        print("usage: hermes delegation config [--show]")
        return 0
    if getattr(args, "show", False):
        print(store.describe(store.delegation_section()))
        return 0
    if not _has_terminal():
        print(_NO_TTY, file=sys.stderr)
        return 1
    try:
        run()
    except RuntimeError as exc:  # store refused the write (managed install, unreadable config)
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    return 0


# --- menus (core's curses components) ---------------------------------------------------------

def _radio(title: str, rows: list, selected: int = 0, description: Optional[str] = None,
           searchable: bool = False) -> int:
    """Index picked in core's radio list, -1 when cancelled."""
    from hermes_cli.curses_ui import curses_radiolist
    return curses_radiolist(title, rows, selected=selected, cancel_returns=-1, description=description,
                            searchable=searchable)


def _ask(question: str, default: str = "") -> str:
    from hermes_cli.cli_output import prompt
    return prompt(question, default or None).strip()


def _text(value: Any) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def run() -> None:
    section = store.delegation_section()
    mode = store.current_mode(section)
    modes = list(store.MODES)
    rows = [[(f"{store.MODES[m]:<16}", None), (store.MODE_BLURBS[m], "dim"),
             *([("  ← current", "yellow")] if m == mode else [])] for m in modes]
    idx = _radio("Delegation mode: how delegate_task picks each subagent's route", rows,
                 selected=modes.index(mode) if mode in modes else 0,
                 description=_mode_context(section))
    if idx < 0:
        print("No change.")
        return
    {"default": _use_default, "model_map": _model_routes, "profile_routes": _profile_routes}[modes[idx]]()


def _mode_context(section: Mapping[str, Any]) -> str:
    mode = store.current_mode(section)
    detail = ""
    if mode == "model_map":
        n, default = len(store.routes(section)), store.route_default(section)
        detail = f" · {n} route{'s' * (n != 1)}{f', default {default}' if default else ''}"
    elif mode == "profile_routes":
        n, default = len(store.listed_profiles(section)), store.profile_default(section)
        detail = f" · {n} profile{'s' * (n != 1)}{f', default {default}' if default else ''}"
    return f"  Current mode: {store.mode_label(mode)}{detail}\n"


def _use_default() -> None:
    store.set_mode("default")
    section = store.delegation_section()
    model = _text(section.get("model")) or "the parent's model"
    where = _text(section.get("provider")) or _text(section.get("base_url"))
    print(f"Delegation mode: Default. delegation.model / delegation.provider apply again: subagents use "
          f"{model}{f' on {where}' if where else ''}. New sessions pick this up.")


# --- model routes -----------------------------------------------------------------------------

def _route_row(route: Mapping[str, Any], default: str, width: int, desc_width: int) -> list:
    name = _text(route.get("name"))
    return [(f"{name.ljust(width)}  ", None), (_text(route.get("description")).ljust(desc_width), None),
            (f"  {store.route_summary(route)}", "dim"), *([("  ★ default", "yellow")] if name == default else [])]


def _model_routes() -> None:
    while True:
        section = store.delegation_section()
        options, default = store.routes(section), store.route_default(section)
        width = max((len(_text(o.get("name"))) for o in options), default=4)
        desc_width = min(60, max((len(_text(o.get("description"))) for o in options), default=0))
        rows = [_route_row(o, default, width, desc_width) for o in options] + [
            "+ Add a route", "Done: use Model routes"]
        idx = _radio("Model routes: the agent picks one per subagent", rows, description=(
            "  Each route: a name and a one-line description the agent reads, plus the provider/model\n"
            "  it runs on. Enter on a route to edit, delete or make it the default.\n"))
        if idx < 0:
            print("Route changes are saved; the delegation mode is unchanged.")
            return
        if idx == len(options) + 1:
            if _finish_model_routes(options, default):
                return
            continue
        if idx == len(options):
            route = _edit_route({}, options)
            if route is not None:
                store.save_routes([*options, route], default)
                print(f"Saved route '{route['name']}'.")
            continue
        _route_actions(options, default, idx)


def _route_actions(options: list[dict[str, Any]], default: str, idx: int) -> None:
    from hermes_cli.curses_ui import curses_single_select
    route = options[idx]
    name = _text(route.get("name"))
    is_default = name == default
    choice = curses_single_select(f"Route '{name}': {_text(route.get('description'))}", [
        "Edit", "Stop using it as the default" if is_default else "Make it the default", "Delete"],
        cancel_label="Back")
    if choice == 0:
        edited = _edit_route(route, options[:idx] + options[idx + 1:])
        if edited is not None:
            store.save_routes([*options[:idx], edited, *options[idx + 1:]],
                              edited["name"] if is_default else default)
            print(f"Saved route '{edited['name']}'.")
    elif choice == 1:
        store.save_routes(options, "" if is_default else name)
    elif choice == 2:
        store.save_routes(options[:idx] + options[idx + 1:], "" if is_default else default)
        print(f"Deleted route '{name}'.")


_NO_DEFAULT_ROUTE = "No default — unrouted tasks use the main agent's model"


def _finish_model_routes(options: list[dict[str, Any]], default: str) -> bool:
    """Switch to Model routes; False sends the user back to the routes list (default question cancelled)."""
    if not options:
        print("No routes yet, so Model routes would give the agent nothing to pick. The mode is unchanged.")
        return True
    names = [_text(o.get("name")) for o in options]
    if default not in names:
        # delegation.model is inactive in this mode, so make the fallback for unrouted tasks a visible choice.
        idx = _radio("Default route for tasks that pick none:", [*names, _NO_DEFAULT_ROUTE])
        if idx < 0:
            return False
        default = names[idx] if idx < len(names) else ""
        store.save_routes(options, default)
    store.set_mode("model_map")
    shown = ", ".join(n + (" (default)" if n == default else "") for n in names)
    print(f"Delegation mode: Model routes ({shown}{'' if default else '; no default'}). delegation.model / "
          "delegation.provider are inactive in this mode. New sessions pick this up; switch modes in a chat "
          "with /delegation.")
    return True


def _ask_name(current: str, others: list[dict[str, Any]]) -> str:
    while True:
        name = _ask("Route name (what the agent picks, e.g. fast)", current)
        if not name:
            return ""
        probe = {"name": name, "description": "-", "base_url": "-", "model": "-"}
        problems = [p for p in store.validate_route(probe, others) if "name" in p or "named" in p]
        if not problems:
            return name
        for problem in problems:
            print(f"  ✗ {problem}")


def _edit_route(existing: Mapping[str, Any], others: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Ask for each field (current values as defaults); the validated route, or None when cancelled."""
    name = _ask_name(_text(existing.get("name")), others)
    description = _ask("Description (one line the agent reads)", _text(existing.get("description"))) if name else ""
    if not name or not description:
        print("Route not saved.")
        return None
    where = _pick_where(existing)
    if where is None:
        print("Route not saved.")
        return None
    route = {"name": name, "description": description, **where}
    # Keys this editor does not ask about (fallback_providers, ...) survive an edit of the same endpoint; the
    # fallback chain and the route's budget keys are not about the endpoint, so they survive any edit.
    same_endpoint = where.get("provider", "") == _text(existing.get("provider")) and \
        where.get("base_url", "") == _text(existing.get("base_url"))
    for key, value in existing.items():
        if key not in route and key not in store.ROUTE_KEYS and (same_endpoint or key in _KEPT_ON_ANY_EDIT):
            route[key] = value
    _pick_effort(route, _text(existing.get("reasoning_effort")))
    _pick_tier(route, _text(existing.get("service_tier")))
    problems = store.validate_route(route, others)
    if problems:
        for problem in problems:
            print(f"  ✗ {problem}")
        print("Route not saved.")
        return None
    return route


def _pick_where(existing: Mapping[str, Any]) -> Optional[dict[str, str]]:
    """Provider + model the way ``hermes model`` picks them; ``{provider, model}`` or ``{base_url, model}``."""
    from hermes_cli.inventory import build_aux_picker_rows, format_aux_picker_entries
    current_provider, current_url = _text(existing.get("provider")), _text(existing.get("base_url"))
    current_model = _text(existing.get("model"))
    try:
        rows = build_aux_picker_rows(current_provider=current_provider, current_model=current_model,
                                     current_base_url=current_url)
    except Exception as exc:  # inventory reads credentials and catalogs; a broken one must not end the editor
        print(f"Could not list your providers: {exc}")
        rows = []
    entries = format_aux_picker_entries(rows, current_provider=current_provider, current_base_url=current_url)
    entries.append(("__custom__", "Custom endpoint (direct URL)" + ("  ← current" if current_url else ""), []))
    slugs = [slug for slug, _, _ in entries]
    selected = slugs.index(current_provider) if current_provider in slugs else (
        len(entries) - 1 if current_url else 0)
    idx = _radio("Provider for this route:", [label for _, label, _ in entries], selected=selected, searchable=True,
                 description="  Providers with credentials on this profile. Set up another with `hermes model`.\n")
    if idx < 0:
        return None
    slug, _, models = entries[idx]
    if slug == "__custom__":
        url = _ask("Base URL (OpenAI-compatible, e.g. http://localhost:8080/v1)", current_url)
        model = _ask("Model", current_model) if url else ""
        return {"base_url": url, "model": model} if url and model else None
    model = _pick_model(slug, models, current_model if slug == current_provider else "")
    return {"provider": slug, "model": model} if model else None


def _pick_model(provider: str, models: list[str], current: str) -> str:
    if not models:
        return _ask(f"Model on {provider}", current)
    from hermes_cli.auth_model_picker import prompt_model_selection
    from hermes_cli.models_pricing import get_pricing_for_provider
    try:
        pricing = get_pricing_for_provider(provider) or {}
    except Exception:  # prices are decoration; the picker works without them
        pricing = {}
    return prompt_model_selection(list(models), current_model=current, pricing=pricing,
                                  confirm_provider=provider, title="Select the model for this route:") or ""


def _pick_effort(route: dict[str, Any], current: str) -> None:
    from hermes_cli.main_provider_setup import _prompt_reasoning_effort_selection
    from hermes_constants import VALID_REASONING_EFFORTS
    effort = _prompt_reasoning_effort_selection(list(VALID_REASONING_EFFORTS), current_effort=current,
                                                default_label="Inherit parent")
    effort = current if effort is None else effort
    if effort:
        route["reasoning_effort"] = effort


def _pick_tier(route: dict[str, Any], current: str) -> None:
    """Offer only the speed tiers this route's model/provider has (the ``/fast`` gate)."""
    offered = [word for word, tier in _TIERS if store.offered_tier(route, tier)]
    if not offered:
        if current and current != "normal":
            print(f"  This route offers no speed tier; dropped service_tier '{current}'.")
        return
    rows = ["Normal (no speed tier)", *(word.capitalize() for word in offered)]
    idx = _radio("Speed tier for this route:", rows,
                 selected=1 + offered.index(current) if current in offered else 0)
    word = current if idx < 0 else ("" if idx == 0 else offered[idx - 1])
    if word in offered:
        route["service_tier"] = word


# --- profile routes ---------------------------------------------------------------------------

def _profile_routes() -> None:
    from hermes_cli.curses_ui import curses_checklist
    from hermes_cli.profiles import get_active_profile_name, list_profile_names
    section = store.delegation_section()
    listed = {_text(e.get("name")): _text(e.get("description")) for e in store.listed_profiles(section)}
    me = get_active_profile_name()
    names = [n for n in list_profile_names() if n != me]
    if not names:
        print("There are no other profiles to route to yet. Create one with `hermes profile create <name>`, "
              "give it a model, then run this again.")
        return
    items = [f"{n}  {listed[n]}".rstrip() if listed.get(n) else n for n in names]
    chosen = curses_checklist("Profile routes: which profiles may the agent run a subagent as?", items,
                              {i for i, n in enumerate(names) if n in listed}, cancel_returns={-1})
    if chosen == {-1}:
        print("No change.")
        return
    picked = [n for i, n in enumerate(names) if i in chosen]
    if not picked:
        _clear_profiles(section, bool(listed))
        return
    entries = []
    for name in picked:  # profiles already listed keep their line; only newly checked ones are asked
        description = listed[name] if name in listed else _ask(
            f"One line the agent reads about '{name}' (optional)")
        entries.append({"name": name, **({"description": description} if description else {})})
    current_default = store.profile_default(section)
    idx = _radio("Default profile for tasks that pick none:", [*picked, "No default (the parent's model)"],
                 selected=picked.index(current_default) if current_default in picked else 0)
    default = current_default if idx < 0 and current_default in picked else (
        picked[idx] if 0 <= idx < len(picked) else "")
    store.save_profiles(entries, default)
    store.set_mode("profile_routes")
    print(f"Delegation mode: Profile routes ({', '.join(n + (' (default)' if n == default else '') for n in picked)}). "
          "New sessions pick this up; switch modes in a chat with /delegation.")


def _clear_profiles(section: Mapping[str, Any], had_profiles: bool) -> None:
    """Every profile unchecked: empty the list after a confirmation. Profile routes with nothing to pick is
    never left active (``/delegation`` refuses it too), so that mode goes back to Default."""
    if not had_profiles:
        print("No profiles picked; nothing changed.")
        return
    from hermes_cli.curses_ui import curses_single_select
    active = store.current_mode(section) == "profile_routes"
    title = "Clear the profile list? The agent will have no profiles to pick" + (
        "; the mode goes back to Default." if active else ".")
    if curses_single_select(title, ["Clear the list"], cancel_label="Back") != 0:
        print("No change.")
        return
    store.save_profiles([], "")
    if active:
        store.set_mode("default")
    print("Profile list cleared." + (" Delegation mode: Default; delegation.model / delegation.provider apply "
                                     "again. New sessions pick this up." if active else ""))
