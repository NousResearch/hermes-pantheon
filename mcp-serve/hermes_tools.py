"""``hermes mcp-serve --tools``: a curated slice of Hermes' own tool surface over MCP.

Off by default. With ``--tools`` an MCP client (Claude Code, Codex, Cursor, a custom harness) can
call Hermes' configured web, browser, vision, image, skills and TTS tools, under this profile's
credentials, next to the messaging bridge. Ported from hermes-agent PR #83364.

What is never exposed, whatever the profile enables:

* terminal / file / code execution / process tools: an MCP client would get a shell on this machine
  outside Hermes' approval prompts.
* kanban tools: ``kanban_create`` hands work to a dispatcher that runs a full agent (terminal
  included), so it is the shell by another route.
* delegate_task / memory / session_search / todo / clarify: they need a running agent.

The exposed tools stay off this machine's files: ``browser_navigate`` / ``web_extract`` take only web
URLs (no ``file:``), ``vision_analyze`` only web or ``data:`` images (no local paths), and
``text_to_speech`` writes only into Hermes' own cache (``output_path`` is not offered). Web URLs include
local-network addresses, as they do for the Hermes model.

Only toolsets enabled for the CLI (``hermes tools``) are offered, and a tool whose backend is not
configured (no web provider, no image key, ...) is skipped: the availability rules the model sees.
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import re
from typing import Any, Optional

logger = logging.getLogger("hermes.mcp_serve.tools")

# Each name must be a registered Hermes tool ``model_tools.handle_function_call`` can dispatch.
EXPOSED_TOOLS: tuple[str, ...] = (
    "web_search", "web_extract",
    "browser_navigate", "browser_click", "browser_type", "browser_press", "browser_snapshot", "browser_scroll",
    "browser_back", "browser_get_images", "browser_console", "browser_vision",
    "vision_analyze", "image_generate", "skill_view", "skills_list", "text_to_speech",
)

# Arguments an MCP client may not set: they write outside Hermes' cache.
_DROPPED_ARGS: dict[str, tuple[str, ...]] = {"text_to_speech": ("output_path",)}
# (argument, data: URLs allowed) that must be web URLs, never local paths or file:/javascript: URLs.
_URL_ARGS: dict[str, tuple[str, bool, bool]] = {
    "browser_navigate": ("url", False, True), "web_extract": ("urls", False, True), "vision_analyze": ("image_url", True, False),
}
_SCHEME_RE = re.compile(r"^\s*([a-zA-Z][a-zA-Z0-9+.-]*):(?!\d)")
_JSON_TO_PY = {"string": str, "integer": int, "number": float, "boolean": bool, "array": list, "object": dict}


def _signature_from_schema(schema: dict | None) -> tuple[inspect.Signature, dict[str, type]]:
    """KEYWORD_ONLY signature + annotations from a JSON schema (optional params default to None).

    The MCP SDK derives each tool's input schema from the handler's signature, so this is what makes
    clients see the same parameters the Hermes model does."""
    props = (schema or {}).get("properties") or {}
    required = set((schema or {}).get("required") or [])
    params, annots = [], {}
    for pname, pspec in props.items():
        if pname.startswith("_"):
            continue
        py = _JSON_TO_PY.get((pspec or {}).get("type"), Any)
        ann, default = (py, inspect.Parameter.empty) if pname in required else (Optional[py], None)
        annots[pname] = ann
        params.append(inspect.Parameter(pname, inspect.Parameter.KEYWORD_ONLY, annotation=ann, default=default))
    return inspect.Signature(params, return_annotation=str), annots


def _is_web_url(value: Any, *, allow_data: bool, allow_bare_host: bool = True) -> bool:
    """http(s) URL or a bare host (``example.com``, ``localhost:8080``); ``data:`` only if allowed.

    A bare host is only accepted when *allow_bare_host* is true.  Tools such as
    ``vision_analyze`` must never read a local file named without a scheme, so
    they require an explicit ``http:``, ``https:`` or ``data:`` scheme.
    """
    text = str(value or "")
    match = _SCHEME_RE.match(text)
    if match is None:
        if not allow_bare_host:
            return False
        return bool(text.strip()) and not text.lstrip().startswith(("/", "~", "\\", "."))
    return match.group(1).lower() in ({"http", "https", "data"} if allow_data else {"http", "https"})


def _refusal(tool_name: str, args: dict) -> Optional[str]:
    """Why *args* may not reach *tool_name* from an MCP client, else None."""
    arg, allow_data, allow_bare_host = _URL_ARGS.get(tool_name, ("", False, True))
    value = args.get(arg) if arg else None
    if value is None:
        return None
    values = value if isinstance(value, list) else [value]
    bad = [v for v in values if not _is_web_url(v, allow_data=allow_data, allow_bare_host=allow_bare_host)]
    if not bad:
        return None
    kinds = "http(s) or data: URLs" if allow_data else "http(s) URLs"
    return f"{tool_name} over MCP accepts only {kinds}, not local files: {bad[0]!r}"


def _make_handler(tool_name: str, schema: dict | None, description: str):
    from model_tools import handle_function_call

    sig, annots = _signature_from_schema(schema)

    def _dispatch(**kwargs: Any) -> str:
        args = {k: v for k, v in kwargs.items() if v is not None and k not in _DROPPED_ARGS.get(tool_name, ())}
        refusal = _refusal(tool_name, args)
        if refusal:
            return json.dumps({"error": refusal, "tool": tool_name})
        try:
            # Unset optionals (None) are not forwarded to the handler.
            return handle_function_call(tool_name, args)
        except Exception as exc:  # an MCP tool call reports the failure, it never kills the server
            logger.exception("tool %s raised", tool_name)
            return json.dumps({"error": str(exc), "tool": tool_name})

    _dispatch.__name__ = tool_name
    _dispatch.__doc__ = description
    _dispatch.__signature__ = sig
    _dispatch.__annotations__ = {**annots, "return": str}
    return _dispatch


def available_tool_specs() -> dict[str, dict]:
    """``{name: function spec}`` for every :data:`EXPOSED_TOOLS` entry this process can dispatch, within
    the toolsets the profile enables for the CLI. Dropped arguments are removed from the parameters."""
    from hermes_cli.config import load_config
    from hermes_cli.tools_config import _get_platform_tools
    from model_tools import get_tool_definitions

    toolsets = sorted(_get_platform_tools(load_config(), "cli", include_default_mcp_servers=False))
    defs = {
        td["function"]["name"]: td["function"]
        for td in (get_tool_definitions(enabled_toolsets=toolsets, quiet_mode=True) or [])
        if isinstance(td, dict) and td.get("type") == "function"
    }
    specs = {}
    for name in EXPOSED_TOOLS:
        if name not in defs:
            continue
        spec = json.loads(json.dumps(defs[name]))  # never mutate the registry's schema
        params = spec.setdefault("parameters", {"type": "object", "properties": {}})
        for arg in _DROPPED_ARGS.get(name, ()):
            (params.get("properties") or {}).pop(arg, None)
            if isinstance(params.get("required"), list) and arg in params["required"]:
                params["required"].remove(arg)
        specs[name] = spec
    return specs


def register_hermes_tools(mcp: Any, *, reserved: tuple[str, ...] = ()) -> int:
    """Register the available :data:`EXPOSED_TOOLS` on *mcp*; returns how many were registered.

    A name in *reserved* (the bridge's own tools) is never replaced."""
    count = 0
    for name, spec in available_tool_specs().items():
        if name in reserved:
            continue
        description = spec.get("description") or f"Hermes {name} tool"
        params = spec.get("parameters") or {"type": "object", "properties": {}}
        mcp.add_tool(_make_handler(name, params, description), name=name, description=description)
        # The SDK advertises a schema derived from the handler signature, which loses descriptions,
        # enums and item types; clients see Hermes' own schema instead (call validation is unchanged).
        mcp._tool_manager.get_tool(name).parameters = params
        count += 1
    logger.info("registered %d/%d Hermes tools", count, len(EXPOSED_TOOLS))
    return count


def prepare_tool_process(*, accept_hooks: bool = False) -> None:
    """Process setup for ``--tools``, before any tool module loads.

    Keeps Hermes' own banners off stdout (it is the MCP wire) and redacts secrets in tool output, and
    registers the profile's shell hooks and outbound webhooks the way core did for ``hermes mcp serve``,
    so a ``pre_tool_call`` hook that blocks a tool still blocks it when an MCP client calls it."""
    os.environ.setdefault("HERMES_QUIET", "1")
    os.environ.setdefault("HERMES_REDACT_SECRETS", "true")
    from agent.outbound_webhooks import register_from_config as register_outbound_webhooks
    from agent.shell_hooks import register_from_config
    from hermes_cli.config import load_config

    cfg = load_config()
    register_from_config(cfg, accept_hooks=accept_hooks)
    register_outbound_webhooks(cfg)
