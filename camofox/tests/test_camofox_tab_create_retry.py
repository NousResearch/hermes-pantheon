"""Cold-start one-retry on the tab-create POST (``ensure_tab``).

After camofox-browser's idle shutdown the first ``POST /tabs`` pays for launching Camoufox and
building a context with the persisted storage state; that exceeds the server's own tab-create
route budget, so it answers 500 ``tab create timed out after 30000ms`` while the coalesced build
is still finishing — and the immediate retry lands warm. ``ensure_tab`` must absorb that once,
transparently, without changing raise-for-status semantics for anything else.
"""

from __future__ import annotations

import io

import pytest
import requests

from camofox_plugin import camofox_client as client


def _cold_then_warm(camofox):
    """Answer the first ``POST /tabs`` with the real cold-start 500 body; serve the rest normally."""
    real_route = camofox.route
    state = {"first": True}

    def route(method, path, query, body):
        if method == "POST" and path == "/tabs" and state["first"]:
            state["first"] = False
            return 500, {"error": "tab create timed out after 30000ms"}
        return real_route(method, path, query, body)

    camofox.route = route


def _tab_creates(camofox):
    return camofox.calls("POST", "/tabs")


def test_cold_start_500_is_retried_once_and_tab_created(camofox):
    _cold_then_warm(camofox)
    session = client.ensure_tab("t1")
    assert session["tab_id"]
    assert len(_tab_creates(camofox)) == 2


def test_persistent_500_keeps_raising(camofox):
    camofox.overrides[("POST", "/tabs")] = (500, {"error": "tab create timed out after 30000ms"})
    with pytest.raises(requests.HTTPError):
        client.ensure_tab("t1")
    # exactly the first attempt plus the single retry — no retry storm
    assert len(_tab_creates(camofox)) == 2


def test_4xx_on_tab_create_is_not_retried(camofox):
    camofox.overrides[("POST", "/tabs")] = (400, {"error": "bad userId"})
    with pytest.raises(requests.HTTPError):
        client.ensure_tab("t1")
    assert len(_tab_creates(camofox)) == 1


def test_warm_create_unchanged_single_post(camofox):
    session = client.ensure_tab("t1")
    assert session["tab_id"]
    assert len(_tab_creates(camofox)) == 1


def test_client_timeout_is_retried_with_raised_budget(monkeypatch, camofox):
    """The same race seen from the client side: if the caller's command_timeout expires while the
    server still builds the session, the retry gets a raised per-call budget (>= 60 s) so it can
    actually wait the cold path out."""
    calls: list = []
    real_post = requests.post

    def flaky_post(url, **kwargs):
        calls.append(kwargs.get("timeout"))
        if len(calls) == 1:
            raise requests.ReadTimeout("simulated client-side expiry during server cold build")
        return real_post(url, **kwargs)

    monkeypatch.setattr(client.requests, "post", flaky_post)
    session = client.ensure_tab("t1")
    assert session["tab_id"]
    assert len(calls) == 2
    assert calls[1] >= 60
