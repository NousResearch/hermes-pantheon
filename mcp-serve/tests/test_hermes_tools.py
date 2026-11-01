"""``--tools``: the Hermes tool surface on the MCP wire, gated off by default and limited to the
curated allowlist (never terminal/file/kanban), dispatched through Hermes' real tool registry."""

from __future__ import annotations

import asyncio
import json

import pytest

from mcp_serve_plugin import bridge, hermes_tools

_BRIDGE_TOOLS = set(bridge._TOOL_NAMES)


def _listed(server) -> dict:
    return {t.name: t for t in asyncio.run(server.list_tools())}


@pytest.fixture
def real_tools(monkeypatch, require_mcp_2_sdk):
    """Real core registry, with two allowlisted tools and two dangerous ones made available."""
    import model_tools

    params = {"type": "object", "properties": {
        "query": {"type": "string", "description": "what to look for"},
        "limit": {"type": "integer"}, "url": {"type": "string"}, "image_url": {"type": "string"},
        "output_path": {"type": "string"}}, "required": ["query"]}
    defs = [{"type": "function", "function": {"name": n, "description": f"{n} desc", "parameters": params}}
            for n in ("web_search", "skills_list", "browser_navigate", "vision_analyze", "text_to_speech",
                      "terminal", "kanban_create")]
    calls = []
    monkeypatch.setattr(model_tools, "get_tool_definitions", lambda **_: defs)
    monkeypatch.setattr(model_tools, "handle_function_call",
                        lambda name, args, *a, **k: calls.append((name, args)) or json.dumps({"ok": name}))
    return calls


def test_default_server_exposes_only_the_bridge(real_tools):
    server = bridge.create_mcp_server(event_bridge=bridge.EventBridge())
    assert set(_listed(server)) == _BRIDGE_TOOLS


def test_tools_mode_adds_only_allowlisted_available_tools(real_tools):
    server = bridge.create_mcp_server(event_bridge=bridge.EventBridge(), include_tools=True)
    listed = _listed(server)
    assert set(listed) == _BRIDGE_TOOLS | {"web_search", "skills_list", "browser_navigate", "vision_analyze",
                                           "text_to_speech"}
    assert "terminal" not in listed and "kanban_create" not in listed
    schema = listed["web_search"].input_schema  # Hermes' own schema, descriptions included
    assert schema["required"] == ["query"] and schema["properties"]["query"]["description"] == "what to look for"
    assert "output_path" not in listed["text_to_speech"].input_schema["properties"]


def test_tools_mode_dispatches_through_handle_function_call(real_tools):
    server = bridge.create_mcp_server(event_bridge=bridge.EventBridge(), include_tools=True)
    result = asyncio.run(server.call_tool("web_search", {"query": "hermes"}))
    text = "".join(getattr(block, "text", "") for block in (getattr(result, "content", None) or []))
    assert real_tools == [("web_search", {"query": "hermes"})]  # unset optional `limit` not forwarded
    assert json.loads(text) == {"ok": "web_search"}


def _call(server, name, args):
    result = asyncio.run(server.call_tool(name, args))
    return json.loads("".join(getattr(b, "text", "") for b in (getattr(result, "content", None) or [])))


@pytest.mark.parametrize(("tool", "args"), [
    ("browser_navigate", {"query": "q", "url": "file:///home/me/notes.txt"}),
    ("browser_navigate", {"query": "q", "url": "javascript:alert(1)"}),
    ("vision_analyze", {"query": "q", "image_url": "/home/me/.hermes/.env"}),
    ("vision_analyze", {"query": "q", "image_url": "~/secret.png"}),
    ("vision_analyze", {"query": "q", "image_url": "secret.png"}),
    ("vision_analyze", {"query": "q", "image_url": "screenshots/secret.png"}),
])
def test_tools_mode_refuses_local_files_before_dispatch(real_tools, tool, args):
    server = bridge.create_mcp_server(event_bridge=bridge.EventBridge(), include_tools=True)
    assert "not local files" in _call(server, tool, args)["error"]
    assert real_tools == []


def test_tools_mode_passes_web_urls_and_drops_output_path(real_tools):
    server = bridge.create_mcp_server(event_bridge=bridge.EventBridge(), include_tools=True)
    _call(server, "browser_navigate", {"query": "q", "url": "https://example.com"})
    _call(server, "browser_navigate", {"query": "q", "url": "localhost:8080"})
    _call(server, "vision_analyze", {"query": "q", "image_url": "data:image/png;base64,AAAA"})
    _call(server, "text_to_speech", {"query": "q", "output_path": "/home/me/notes.mp3"})
    assert real_tools == [
        ("browser_navigate", {"query": "q", "url": "https://example.com"}),
        ("browser_navigate", {"query": "q", "url": "localhost:8080"}),
        ("vision_analyze", {"query": "q", "image_url": "data:image/png;base64,AAAA"}),
        ("text_to_speech", {"query": "q"}),
    ]


def test_tools_mode_follows_the_profiles_cli_toolsets(tmp_path, monkeypatch, require_mcp_2_sdk):
    """Real registry: a profile that enabled only the skills toolset for the CLI exposes no web tools."""
    (tmp_path / "hermes" / "config.yaml").write_text("platform_toolsets:\n  cli: [skills]\n", encoding="utf-8")
    names = set(hermes_tools.available_tool_specs())
    assert "skills_list" in names
    assert not names & {"web_search", "web_extract", "text_to_speech", "image_generate"}


def test_allowlist_excludes_shell_reaching_tools():
    exposed = set(hermes_tools.EXPOSED_TOOLS)
    assert not exposed & {"terminal", "process", "execute_code", "read_file", "write_file", "patch",
                          "search_files", "delegate_task", "kanban_create", "kanban_complete"}
