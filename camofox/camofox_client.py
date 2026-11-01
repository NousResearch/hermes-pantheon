"""Camofox REST client: configuration, identity, per-task tab sessions and HTTP helpers.

Camofox-browser (https://github.com/jo-inc/camofox-browser) is a self-hosted Node.js server wrapping
Camoufox (a Firefox fork with C++ fingerprint spoofing). Its REST API maps onto the browser tool
interface: accessibility snapshots with element refs, click/type/scroll by ref, screenshots.

Every value is read per call through the active profile: ``CAMOFOX_*`` through
``agent.secret_scope.get_secret`` (the profile's ``.env`` under a multiplexed gateway, the process
env otherwise) and ``browser.camofox.*`` / ``browser.command_timeout`` from that profile's
``config.yaml``. Tab sessions are keyed by ``(hermes_home_key(), task_id)`` so one process serving
several profiles never hands one profile's tab to another.
"""

from __future__ import annotations

import ipaddress
import logging
import threading
import uuid
from pathlib import Path
from typing import Any, Optional
from urllib.parse import SplitResult, urlsplit, urlunsplit

import requests

from agent.secret_scope import get_secret
from hermes_cli.config import cfg_get, read_raw_config
from hermes_constants import get_hermes_home, hermes_home_key

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30  # browser.command_timeout fallback (seconds)
NO_SESSION_ERROR = "No browser session. Call browser_navigate first."
_DOCKER_HINT = "docker run -p 9377:9377 -e CAMOFOX_PORT=9377 jo-inc/camofox-browser"


# ---- Configuration ----

def _env(name: str) -> str:
    """``CAMOFOX_*`` value from the active profile scope (stripped), or ""."""
    return (get_secret(name, "") or "").strip()


def get_camofox_url() -> str:
    """The configured Camofox server URL (``CAMOFOX_URL``, trailing slash dropped), or ""."""
    return _env("CAMOFOX_URL").rstrip("/")


def _auth_headers() -> dict[str, str]:
    """``Authorization: Bearer`` when ``CAMOFOX_API_KEY`` is set."""
    key = _env("CAMOFOX_API_KEY")
    return {"Authorization": f"Bearer {key}"} if key else {}


def get_camofox_config() -> dict[str, Any]:
    """The profile's ``browser.camofox`` block, or ``{}``."""
    try:
        block = cfg_get(read_raw_config(), "browser", "camofox", default={})
    except Exception as exc:
        logger.warning("camofox config read failed, using defaults: %s", exc)
        return {}
    return block if isinstance(block, dict) else {}


def get_command_timeout() -> int:
    """``browser.command_timeout`` (floor 5 s, default 30 s) for the active profile."""
    try:
        val = cfg_get(read_raw_config(), "browser", "command_timeout")
        if val is not None:
            return max(int(val), 5)
    except Exception as exc:
        logger.debug("Could not read browser.command_timeout: %s", exc)
    return DEFAULT_TIMEOUT


def _managed_persistence_enabled(camofox_cfg: dict[str, Any]) -> bool:
    """``browser.camofox.managed_persistence``: stable profile-scoped userId vs random per session."""
    return bool(camofox_cfg.get("managed_persistence"))


def _env_or_cfg(env_name: str, camofox_cfg: dict[str, Any], cfg_key: str) -> str:
    """Env/secret-scope value first, then the ``browser.camofox`` config key, else ""."""
    return _env(env_name) or str(camofox_cfg.get(cfg_key) or "").strip()


def _flag(env_name: str, camofox_cfg: dict[str, Any], cfg_key: str) -> bool:
    """Boolean toggle: the env var wins when set to a valid value, else the config key."""
    raw = _env(env_name).lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    if raw:
        logger.debug("Ignoring invalid boolean env %s=%r", env_name, raw)
    return bool(camofox_cfg.get(cfg_key))


# ---- Identity ----

def get_camofox_state_dir() -> Path:
    """Profile-scoped root directory for Camofox persistence."""
    return get_hermes_home() / "browser_auth" / "camofox"


def get_camofox_identity(task_id: Optional[str] = None) -> dict[str, str]:
    """Stable Hermes-managed identity: the userId is profile-scoped (same Firefox profile across
    restarts), the session key is scoped to the logical browser task. Byte-identical to the
    identity core derived, so a migrated profile keeps its Camofox login state."""
    scope_root = str(get_camofox_state_dir())
    user_digest = uuid.uuid5(uuid.NAMESPACE_URL, f"camofox-user:{scope_root}").hex[:10]
    session_digest = uuid.uuid5(uuid.NAMESPACE_URL, f"camofox-session:{scope_root}:{task_id or 'default'}").hex[:16]
    return {"user_id": f"hermes_{user_digest}", "session_key": f"task_{session_digest}"}


def _camofox_identity_override(task_id: Optional[str], camofox_cfg: dict[str, Any]) -> Optional[dict[str, str]]:
    """Externally configured identity (an integration owning the visible Camofox browser shares a
    user ID so Hermes works in the same browser profile), or None."""
    user_id = _env_or_cfg("CAMOFOX_USER_ID", camofox_cfg, "user_id")
    if not user_id:
        return None
    session_key = _env_or_cfg("CAMOFOX_SESSION_KEY", camofox_cfg, "session_key")
    return {"user_id": user_id, "session_key": session_key or f"task_{(task_id or 'default')[:16]}"}


# ---- Docker loopback rewrite ----

def _is_loopback_hostname(hostname: Optional[str]) -> bool:
    if not hostname:
        return False
    host = hostname.strip().strip("[]").lower()
    if host in {"localhost", "localhost.localdomain"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def rewrite_loopback_url(url: str) -> tuple[str, Optional[dict[str, str]]]:
    """Rewrite loopback page URLs for a Docker-hosted Camofox, when configured.

    ``CAMOFOX_URL`` may point at a host-published Docker port, but page URLs are opened by the
    browser *inside* the container, where loopback is the container. Opt-in
    (``CAMOFOX_REWRITE_LOOPBACK_URLS`` / ``browser.camofox.rewrite_loopback_urls``) because native
    installs run the browser on the host. Returns ``(url, metadata)``; ``metadata`` is set only when
    a rewrite happened so the tool result can disclose it to the model.
    """
    camofox_cfg = get_camofox_config()
    if not _flag("CAMOFOX_REWRITE_LOOPBACK_URLS", camofox_cfg, "rewrite_loopback_urls"):
        return url, None
    try:
        parsed = urlsplit(url)
    except ValueError:
        return url, None
    alias = _env_or_cfg("CAMOFOX_LOOPBACK_HOST_ALIAS", camofox_cfg, "loopback_host_alias") or "host.docker.internal"
    if parsed.scheme not in {"http", "https"} or not _is_loopback_hostname(parsed.hostname):
        return url, None
    userinfo = (parsed.username + (f":{parsed.password}" if parsed.password else "") + "@") if parsed.username else ""
    host_part = f"[{alias}]" if ":" in alias and not alias.startswith("[") else alias
    port_part = f":{parsed.port}" if parsed.port else ""
    rewritten = urlunsplit(
        SplitResult(parsed.scheme, f"{userinfo}{host_part}{port_part}", parsed.path, parsed.query, parsed.fragment))
    return rewritten, {"from": parsed.hostname or "", "to": alias, "original_url": url, "rewritten_url": rewritten}


# ---- Health / VNC ----

# One VNC address per Camofox server URL: routed profiles may each point CAMOFOX_URL at their own server.
_vnc_url_by_camofox_url: dict[str, Optional[str]] = {}


def _vnc_url_from_health(url: str, resp: Any) -> Optional[str]:
    try:
        vnc_port = resp.json().get("vncPort")
        if isinstance(vnc_port, int) and 1 <= vnc_port <= 65535:
            return f"http://{urlsplit(url).hostname or 'localhost'}:{vnc_port}"
    except (ValueError, KeyError, AttributeError):
        pass
    return None


def check_camofox_available() -> bool:
    """Whether the configured Camofox server answers ``/health`` (records its VNC URL once)."""
    url = get_camofox_url()
    if not url:
        return False
    try:
        with requests.get(f"{url}/health", timeout=5) as resp:
            if resp.status_code == 200 and url not in _vnc_url_by_camofox_url:
                _vnc_url_by_camofox_url[url] = _vnc_url_from_health(url, resp)
            return resp.status_code == 200
    except Exception:
        return False


def get_vnc_url() -> Optional[str]:
    """The VNC URL the configured Camofox server exposes, or None."""
    url = get_camofox_url()
    if url not in _vnc_url_by_camofox_url:
        check_camofox_available()
    return _vnc_url_by_camofox_url.get(url)


# ---- HTTP helpers ----

def _error_detail(resp: requests.Response) -> str:
    """The server's own explanation (``error`` / ``code`` / ``recovery``), e.g. ``stale_refs``."""
    try:
        body = resp.json()
    except ValueError:
        return ""
    if not isinstance(body, dict):
        return ""
    return "; ".join(f"{k}: {body[k]}" for k in ("error", "code", "recovery") if body.get(k))


def request(method: str, path: str, timeout: Optional[int] = None, **kwargs: Any) -> requests.Response:
    """Authenticated request to the Camofox server; returns the response, or raises ``HTTPError``
    carrying the server's error text (``str(HTTPError)`` alone drops the body)."""
    resp = getattr(requests, method)(f"{get_camofox_url()}{path}", headers=_auth_headers(),
                                     timeout=get_command_timeout() if timeout is None else timeout, **kwargs)
    try:
        resp.raise_for_status()
    except requests.HTTPError as exc:
        detail = _error_detail(resp)
        if detail:
            raise requests.HTTPError(f"{exc} ({detail})", response=resp) from exc
        raise
    return resp


def post(path: str, body: dict, timeout: Optional[int] = None) -> dict:
    return request("post", path, timeout, json=body).json()


def get(path: str, params: Optional[dict] = None, timeout: Optional[int] = None) -> dict:
    return request("get", path, timeout, params=params).json()


def get_raw(path: str, params: Optional[dict] = None, timeout: Optional[int] = None) -> requests.Response:
    """GET returning the raw response (binary payloads such as screenshots)."""
    return request("get", path, timeout, params=params)


def delete(path: str, body: Optional[dict] = None, timeout: Optional[int] = None) -> dict:
    return request("delete", path, timeout, json=body).json()


# Cold-start retry budget (seconds) for the first tab create of an idle server: the retry must
# outlast the server's own in-flight context build even when browser.command_timeout is small.
_TAB_CREATE_RETRY_TIMEOUT_S = 60


def _is_cold_start_tab_create_error(exc: requests.RequestException) -> bool:
    """True for the cold-start failure signatures on ``POST /tabs``: a 5xx answer, or a client-side
    timeout while the server is still building the browser session. 4xx is a request error and is
    never retried."""
    if isinstance(exc, requests.HTTPError):
        status = getattr(exc.response, "status_code", None)
        return status is not None and status >= 500
    return isinstance(exc, requests.Timeout)


def create_tab(body: dict) -> dict:
    """``POST /tabs`` with one transparent retry on the cold-start failure.

    After the server's idle shutdown the first ``POST /tabs`` pays for launching Camoufox and
    building a context with the persisted storage state — measured 25-30 s against the server's
    30 s tab-create route budget (camofox-browser ``tabCreateRequestTimeoutMs``), so it answers
    500 ``tab create timed out after 30000ms`` *while the coalesced build is still finishing*.
    The build completes just after the 500, so a single retry lands on the warm path (~1 s
    measured). Other routes are unaffected; a second failure keeps the original raise-for-status
    semantics.
    """
    try:
        return post("/tabs", body)
    except requests.RequestException as exc:
        if not _is_cold_start_tab_create_error(exc):
            raise
        logger.info("camofox tab create failed cold (%s); retrying once", exc)
        return post("/tabs", body, timeout=max(get_command_timeout(), _TAB_CREATE_RETRY_TIMEOUT_S))


def connection_error_message() -> str:
    return (f"Cannot connect to Camofox at {get_camofox_url()}. Is the server running? "
            f"Start with: npm start (in camofox-browser dir) or: {_DOCKER_HINT}")


# ---- Sessions ----

# (profile home key, task_id) -> {"user_id", "tab_id", "session_key", "managed", "adopt_existing_tab"}
_sessions: dict[tuple[str, str], dict[str, Any]] = {}
_sessions_lock = threading.Lock()


def _session_key(task_id: Optional[str]) -> tuple[str, str]:
    return hermes_home_key(), task_id or "default"


def tab_path(session: dict[str, Any], suffix: str) -> str:
    return f"/tabs/{session['tab_id']}/{suffix}"


def user_params(session: dict[str, Any]) -> dict[str, str]:
    return {"userId": session["user_id"]}


def _adopt_existing_tab(session: dict[str, Any]) -> dict[str, Any]:
    """Rehydrate ``tab_id`` from an already-open tab with this session's key: a restart empties the
    in-memory map while Camofox still holds the integration-owned tab."""
    if session.get("tab_id") or not session.get("adopt_existing_tab") or not get_camofox_url():
        return session
    try:
        tabs = get("/tabs", params=user_params(session), timeout=5).get("tabs", [])
    except Exception as exc:
        logger.debug("Camofox tab adoption failed for %s: %s", session.get("user_id"), exc)
        return session
    dict_tabs = [tab for tab in tabs if isinstance(tab, dict)] if isinstance(tabs, list) else []
    # Only this session's own tabs: falling back to any tab of a shared userId attached Hermes to a tab
    # another session was driving. No match means browser_navigate opens a tab under our session key.
    candidates = [tab for tab in dict_tabs if tab.get("listItemId") == session.get("session_key")]
    tab_id = candidates[-1].get("tabId") if candidates else None
    if isinstance(tab_id, str) and tab_id:
        session["tab_id"] = tab_id
        logger.debug("Adopted existing Camofox tab %s for %s", tab_id, session.get("user_id"))
    return session


def get_session(task_id: Optional[str]) -> dict[str, Any]:
    """Get or create the task's session. Identity precedence: external override
    (``CAMOFOX_USER_ID`` / config) → profile-scoped identity when managed persistence is on →
    random ephemeral userId."""
    key = _session_key(task_id)
    with _sessions_lock:
        session = _sessions.get(key)
    if session is not None:
        return _adopt_existing_tab(session)  # network I/O stays outside the lock
    with _sessions_lock:
        if key in _sessions:
            return _sessions[key]
        camofox_cfg = get_camofox_config()
        identity = _camofox_identity_override(key[1], camofox_cfg)
        if identity is None and _managed_persistence_enabled(camofox_cfg):
            identity = get_camofox_identity(key[1])
        if identity is None:
            identity = {"user_id": f"hermes_{uuid.uuid4().hex[:10]}", "session_key": f"task_{key[1][:16]}"}
            managed, adopt = False, False
        else:
            managed, adopt = True, _flag("CAMOFOX_ADOPT_EXISTING_TAB", camofox_cfg, "adopt_existing_tab")
        session = {"user_id": identity["user_id"], "tab_id": None, "session_key": identity["session_key"],
                   "managed": managed, "adopt_existing_tab": adopt}
        _sessions[key] = session
    return _adopt_existing_tab(session)


def resync_tab(session: dict[str, Any]) -> bool:
    """After the server answered 404/410 for the cached tab: True when a live tab is tracked again.

    The server reaps idle tabs and drops them all on a browser restart while the session map keeps
    the dead id. Keep the tracked tab if the server still lists it (the error was about something
    else), else take a live tab carrying this session's key (an integration may have reopened it);
    otherwise forget the dead id so the caller can say "navigate again"."""
    try:
        tabs = get("/tabs", params=user_params(session), timeout=5).get("tabs", [])
    except Exception as exc:
        logger.debug("Camofox tab resync failed for %s: %s", session.get("user_id"), exc)
        return False
    live = [tab for tab in tabs if isinstance(tab, dict) and tab.get("tabId")] if isinstance(tabs, list) else []
    if any(tab.get("tabId") == session.get("tab_id") for tab in live):
        return True
    mine = [tab for tab in live if tab.get("listItemId") == session.get("session_key")]
    session["tab_id"] = mine[-1]["tabId"] if mine else None
    return bool(mine)


def ensure_tab(task_id: Optional[str], url: str = "about:blank") -> dict[str, Any]:
    """The task's session with a tab, creating one at ``url`` if needed."""
    session = get_session(task_id)
    if not session["tab_id"]:
        data = create_tab({"userId": session["user_id"], "listItemId": session["session_key"], "url": url})
        session["tab_id"] = data.get("tabId")
    return session


def drop_session(task_id: Optional[str]) -> Optional[dict[str, Any]]:
    """Remove and return the task's session entry for the active profile."""
    with _sessions_lock:
        return _sessions.pop(_session_key(task_id), None)


def soft_cleanup(task_id: Optional[str] = None) -> bool:
    """Drop only the local entry (``True``) for managed identities, whose browser profile must
    survive across agent tasks; ``False`` for ephemeral sessions so the caller closes them."""
    camofox_cfg = get_camofox_config()
    if _managed_persistence_enabled(camofox_cfg) or _camofox_identity_override(task_id, camofox_cfg):
        drop_session(task_id)
        logger.debug("Camofox soft cleanup for task %s (managed persistence)", task_id)
        return True
    return False


def close_session(task_id: Optional[str] = None) -> Optional[str]:
    """Drop the task's session and delete it on the server; returns a warning string on failure."""
    session = drop_session(task_id)
    if not session:
        return None
    try:
        delete(f"/sessions/{session['user_id']}")
    except Exception as exc:
        return str(exc)
    return None
