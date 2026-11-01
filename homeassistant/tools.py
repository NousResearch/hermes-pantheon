"""Home Assistant tools for controlling smart home devices via the REST API.

Provides ``ha_list_entities``, ``ha_get_state``, ``ha_list_services``, ``ha_call_service`` in the
``homeassistant`` toolset. Auth is a Long-Lived Access Token (``HASS_TOKEN``); the instance URL comes
from ``HASS_URL`` (default http://homeassistant.local:8123).

Moved out of hermes-agent core (``tools/homeassistant_tool.py``) into the standalone
``hermes-homeassistant`` plugin with identical names, schemas and behaviour. Registration goes through
the public plugin API (``register_tools(ctx)`` -> ``ctx.register_tool``) instead of ``tools.registry``.

Optional operator configuration (all opt-in; unset preserves plain behaviour):
``HASS_ENTITY_ALLOWLIST`` / ``HASS_ENTITY_DENYLIST`` restrict which entities
``ha_list_entities`` may return, ``ha_get_state`` may read and ``ha_call_service``
may control (see ``_service_call_violation``), and
``HASS_MAX_ENTITIES`` caps list results. ``ha_list_entities`` also supports
targeted queries (``name``, ``entity_ids``, ``max``) and resolves areas and
devices through the WebSocket registries, so ``area`` filters work even when
an entity's name does not mention its room.
"""

from datetime import datetime, timezone
import asyncio
import fnmatch
import json
import logging
import re
from typing import Any, Dict, List, Optional

from agent.secret_scope import get_secret

logger = logging.getLogger(__name__)

TOOLSET = "homeassistant"


def tool_error(message: str) -> str:
    """Tool error envelope (same JSON shape as core's ``tools.registry.tool_error``)."""
    return json.dumps({"error": str(message)}, ensure_ascii=False)


def url_scheme_problem(url: str) -> Optional[str]:
    """Why ``url`` can't be the Home Assistant base URL, or None when it can.

    Without a scheme every request URL becomes ``host/api/...``, which aiohttp rejects with only that
    string as the reason, so the misconfiguration would surface as an endless series of opaque errors.
    """
    if url.startswith(("http://", "https://")):
        return None
    return (
        f"Home Assistant URL {url!r} has no http:// or https:// scheme. Set HASS_URL (or "
        "platforms.homeassistant.extra.url) to the full URL, e.g. https://ha.example.com or "
        "http://homeassistant.local:8123")


# Operator allow/deny lists and the optional entity cap. Module-level mirrors
# so tests can monkeypatch; real values are read from the active profile env
# at call time (via _get_entity_filter_config / _get_max_entities_config).
_HASS_ENTITY_ALLOWLIST: str = ""
_HASS_ENTITY_DENYLIST: str = ""
_HASS_MAX_ENTITIES: str = ""


def _get_config():
    """Return the active profile's Home Assistant URL and token; raise ValueError on a URL without a scheme."""
    hass_url = (get_secret("HASS_URL", "http://homeassistant.local:8123") or "").rstrip("/")
    if problem := url_scheme_problem(hass_url):
        raise ValueError(problem)
    return hass_url, (get_secret("HASS_TOKEN", "") or "").strip()


def _get_entity_filter_config():
    """Return (allowlist, denylist) entity-filter patterns from the active profile.

    Operators can permanently exclude known-dead entities (e.g. a zombie
    ``office_thermostat_*`` cluster left behind by a migration) without a code
    change:

    - ``HASS_ENTITY_DENYLIST`` — comma-separated entity_id prefixes or globs
      (e.g. ``office_thermostat_*``) to always exclude from ha_list_entities,
      ha_get_state and ha_call_service.
      Acts as a final veto: it wins even against the allowlist.
    - ``HASS_ENTITY_ALLOWLIST`` — same format; when non-empty it restricts
      results to matching entities (a whitelist).

    Values come from the profile-scoped env (same resolution as
    HASS_URL/HASS_TOKEN), with module-level mirrors for test monkeypatching.
    """
    allow = _HASS_ENTITY_ALLOWLIST or (get_secret("HASS_ENTITY_ALLOWLIST", "") or "")
    deny = _HASS_ENTITY_DENYLIST or (get_secret("HASS_ENTITY_DENYLIST", "") or "")
    return _parse_entity_filter_patterns(allow), _parse_entity_filter_patterns(deny)


def _get_max_entities_config() -> Optional[int]:
    """Return the operator-configured cap for ha_list_entities, or ``None``.

    ``HASS_MAX_ENTITIES`` is **opt-in**: unset means "no cap, return
    everything" — exactly the upstream behaviour, so existing callers see no
    change unless the operator opts in. Set to a positive integer, it caps
    every ha_list_entities result at that number and marks the response
    ``truncated`` when more matched. A per-call ``max`` parameter always
    overrides this value when supplied.
    """
    raw = (_HASS_MAX_ENTITIES or (get_secret("HASS_MAX_ENTITIES", "") or "") or "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.debug("Ignoring non-integer HASS_MAX_ENTITIES value %r", raw)
        return None
    return value if value > 0 else None


def _parse_entity_filter_patterns(value: str) -> List[str]:
    """Split a comma-separated env value into trimmed, non-empty patterns."""
    if not value:
        return []
    return [p.strip() for p in value.split(",") if p.strip()]


def _entity_matches(entity_id: str, patterns: List[str]) -> bool:
    """Return True if ``entity_id`` matches any operator pattern.

    Matching is deliberately forgiving so an operator can write
    ``office_thermostat_*`` (the way the device family is usually
    abbreviated) and have it cover ``office_thermostat.0_external_temperature``
    (a real HA entity id whose separator after the device name is a dot):

    - exact match
    - prefix match, treating ``.`` and ``_`` as equivalent at the boundary
      (``office_thermostat.0_x`` matches ``office_thermostat_*``)
    - shell-glob match (``*.valve*``, ``climate.office`` etc.)
    """
    for pat in patterns:
        if not pat:
            continue
        if entity_id == pat or fnmatch.fnmatch(entity_id, pat):
            return True
        # Family-prefix match. A pattern that ends in ``*`` or a separator
        # (``office_thermostat_*``, ``office_thermostat.``) is a family
        # prefix: it should cover ``office_thermostat.0_x`` by treating the
        # separator after the matched base (``.`` or ``_``) as
        # interchangeable, or the id to end exactly at the base. A bare id
        # like ``climate.office`` without a trailing separator is only an
        # exact/glob match, so it does NOT swallow ``climate.office_zombie``.
        if pat[-1] in "*._":
            base = pat.rstrip("*._")
            if base and entity_id.startswith(base) and (
                len(entity_id) == len(base) or entity_id[len(base)] in "._"
            ):
                return True
    return False


def _apply_operator_filters(
    states: List[Dict[str, Any]], allow: List[str], deny: List[str],
) -> List[Dict[str, Any]]:
    """Apply the operator allow/deny lists (see ``_get_entity_filter_config``).

    The allowlist narrows to a whitelist; the denylist is a final veto, so an
    entity matching both is excluded.
    """
    if not allow and not deny:
        return states
    out = []
    for s in states:
        entity_id = s.get("entity_id", "")
        if allow and not _entity_matches(entity_id, allow):
            continue  # not on the whitelist
        if deny and _entity_matches(entity_id, deny):
            continue  # denylist is a final veto
        out.append(s)
    return out


def _operator_filter_violation(entity_id: str) -> Optional[str]:
    """Return a human-readable reason an entity is excluded by operator config,
    or ``None`` if it may be read.

    Shared by the single-entity read path so a denylisted/whitelist-missed
    entity is refused *before* it is fetched — otherwise an operator who
    denies ``office_thermostat_*`` to keep it out of lists could still read
    the frozen zombie value directly and report it as live. Denylist is a
    final veto, matching ``_apply_operator_filters``.
    """
    allow, deny = _get_entity_filter_config()
    if deny and _entity_matches(entity_id, deny):
        return "excluded by HASS_ENTITY_DENYLIST"
    if allow and not _entity_matches(entity_id, allow):
        return "not in HASS_ENTITY_ALLOWLIST"
    return None


# ── Operator lists on control (ha_call_service) ──────────────────────────────
# Home Assistant target keys that select entities indirectly. They cannot be
# checked against entity-id patterns without resolving them on the server, so a
# filtered call that uses them is refused (see _service_call_violation).
_INDIRECT_TARGET_KEYS = ("area_id", "device_id", "floor_id", "label_id")
# Domains whose services act on no entity (they deliver a message), so a call
# with no entity target is allowed even when the operator lists are set.
_NON_ENTITY_DOMAINS = frozenset({"notify", "persistent_notification"})
# Built-in script services; any other ``script.<name>`` service runs the
# ``script.<name>`` entity itself.
_SCRIPT_BUILTIN_SERVICES = frozenset({"turn_on", "turn_off", "toggle", "reload"})


def _is_entity_key(key: str) -> bool:
    return key in ("entity_id", "entities", "snapshot_entities") or key.endswith(
        ("_entity_id", "_entity_ids"))


def _collect_service_targets(payload: Any, entity_ids: set, indirect: set) -> None:
    """Gather the entities a service payload names, recursing into nested dicts.

    ``entity_ids`` gets every value under an entity key (``entity_id``,
    ``*_entity_id(s)``, ``entities``: list items or dict keys,
    ``snapshot_entities``); ``indirect`` gets the names of area/device/floor/label
    target keys that carry a value.
    """
    if not isinstance(payload, dict):
        return
    for key, value in payload.items():
        if not isinstance(key, str) or value in (None, "", [], {}):
            continue
        if key in _INDIRECT_TARGET_KEYS:
            indirect.add(key)
        elif _is_entity_key(key):
            # Home Assistant lowercases entity ids before matching, so check them the same way.
            entity_ids.update(
                e.lower() for e in _normalize_id_list(list(value) if isinstance(value, dict) else value))
        elif isinstance(value, dict):
            _collect_service_targets(value, entity_ids, indirect)


def _service_call_violation(domain: str, service: str, payload: Any) -> Optional[str]:
    """Why the operator allow/deny lists refuse this service call, or ``None``.

    With neither list set nothing is checked (behaviour unchanged). Otherwise:

    1. Every entity the payload names must pass the same check as ha_get_state
       (deny is a final veto; with an allowlist, every entity must match it).
    2. Area/device/floor/label targets are refused: they select entities on the
       server, where the lists cannot be checked.
    3. ``entity_id: all`` is refused: it acts on every entity of the domain.
    4. A call that names no entity is refused (an entity service with no target
       can act on every entity of its domain), except ``notify.*`` /
       ``persistent_notification.*``, which act on no entity, and
       ``script.<name>``, which is checked as the entity ``script.<name>``.
    """
    allow, deny = _get_entity_filter_config()
    if not allow and not deny:
        return None
    hint = ("Call ha_call_service with explicit entity_id values that the operator "
            "allow/deny lists permit, or ask the operator to adjust the lists.")
    entity_ids: set = set()
    indirect: set = set()
    _collect_service_targets(payload, entity_ids, indirect)
    if indirect:
        return (f"{domain}.{service} targets by {', '.join(sorted(indirect))}, which cannot be "
                f"checked against HASS_ENTITY_ALLOWLIST/HASS_ENTITY_DENYLIST. {hint}")
    if "all" in entity_ids:
        return (f"{domain}.{service} with entity_id 'all' would act on every entity, which "
                f"HASS_ENTITY_ALLOWLIST/HASS_ENTITY_DENYLIST do not permit. {hint}")
    entity_ids.discard("none")
    if not entity_ids:
        if domain in _NON_ENTITY_DOMAINS:
            return None
        if domain == "script" and service not in _SCRIPT_BUILTIN_SERVICES:
            entity_ids = {f"script.{service}"}
        else:
            return (f"{domain}.{service} names no entity, so it could act on every {domain} "
                    f"entity while HASS_ENTITY_ALLOWLIST/HASS_ENTITY_DENYLIST are set. {hint}")
    for eid in sorted(entity_ids):
        if deny:
            hit = next((p for p in deny if _entity_matches(eid, [p])), None)
            if hit:
                return (f"{eid} is excluded by HASS_ENTITY_DENYLIST (pattern {hit!r}); "
                        f"{domain}.{service} refused. {hint}")
        if allow and not _entity_matches(eid, allow):
            return f"{eid} is not in HASS_ENTITY_ALLOWLIST; {domain}.{service} refused. {hint}"
    return None


def _normalize_id_list(value: Any) -> set:
    """Accept a list or a comma/space-delimited string of entity_ids; dedupe."""
    if value is None:
        return set()
    if isinstance(value, str):
        parts = re.split(r"[,\s]+", value.strip())
    elif isinstance(value, (list, tuple)):
        parts = [str(p).strip() for p in value]
    else:
        parts = [str(value).strip()]
    return {p for p in parts if p}


# Valid HA entity_id (e.g. "light.living_room", "sensor.temperature_1").
_ENTITY_ID_RE = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z0-9_]+$")

# Domain/service names are interpolated into /api/services/{domain}/{service}, so only
# [a-z0-9_] is allowed: anything else enables SSRF via path traversal
# (domain="../../api/config") or blocklist bypass (domain="shell_command/../light").
_SERVICE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# Domains that allow arbitrary code/command execution on the HA host or SSRF on the
# local network. HA has zero service-level access control; all safety lives here.
_BLOCKED_DOMAINS = frozenset({
    "shell_command",    # arbitrary shell commands as root in HA container
    "command_line",     # sensors/switches that execute shell commands
    "python_script",    # sandboxed but can escalate via hass.services.call()
    "pyscript",         # scripting integration with broader access
    "hassio",           # addon control, host shutdown/reboot, stdin to containers
    "rest_command",     # HTTP requests from HA server (SSRF vector)
})


def _get_headers(token: str = "") -> Dict[str, str]:
    """Return authorization headers for HA REST API."""
    if not token:
        _, token = _get_config()
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


async def _api_json(method: str, path: str, timeout: float, payload: Any = None) -> Any:
    """One HA REST call (GET or POST JSON) that raises on HTTP errors and returns the JSON body."""
    import aiohttp
    hass_url, hass_token = _get_config()
    kwargs: Dict[str, Any] = {"headers": _get_headers(hass_token), "timeout": aiohttp.ClientTimeout(total=timeout)}
    if method == "POST":
        kwargs["json"] = payload
    async with aiohttp.ClientSession() as session:
        async with session.request(method, f"{hass_url}{path}", **kwargs) as resp:
            if resp.status >= 400:
                # Capture the HA error body into the exception: HA's 400/404 messages
                # (e.g. "Service call requires responses but caller did not ask for
                # responses. Add ?return_response to query parameters.") are what the
                # caller needs to react, and aiohttp's default str() drops them.
                try:
                    text = (await resp.text())[:500]
                except Exception:
                    text = ""
                raise aiohttp.ClientResponseError(
                    resp.request_info, resp.history, status=resp.status,
                    message=text or (resp.reason or ""))
            return await resp.json()


# ── async helpers (called from sync handlers via _run_async) ─────────────────
def _filter_and_summarize(
    states: list,
    domain: Optional[str] = None,
    area: Optional[str] = None,
    entity_ids: Optional[Any] = None,
    name: Optional[str] = None,
    max_entities: Optional[int] = None,
    entity_area: Optional[Dict[str, str]] = None,
    entity_device: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Filter raw HA states, then build the compact payload.

    Every filter is applied to the raw state list BEFORE the payload is
    built, so tokens are never spent on entities that would be dropped:

    1. operator allow/deny lists (HASS_ENTITY_ALLOWLIST / HASS_ENTITY_DENYLIST)
    2. caller filters: domain, area, name (substring), explicit entity_ids
    3. the optional entity cap: the per-call ``max_entities`` parameter
       (always wins when set), else the operator's HASS_MAX_ENTITIES value,
       else no cap (the upstream bare-call behaviour is preserved).

    When a cap is in effect and more entities matched than were returned,
    the result carries ``truncated: true`` and a note telling the model how
    to get the rest.
    """
    entity_area = entity_area or {}
    entity_device = entity_device or {}

    allow, deny = _get_entity_filter_config()
    states = _apply_operator_filters(states, allow, deny)

    id_filter = _normalize_id_list(entity_ids)
    if id_filter:
        # An explicit entity list is the caller's precise intent — it supersedes
        # the broader domain/area/name filters.
        states = [s for s in states if s.get("entity_id", "") in id_filter]
    else:
        if domain:
            states = [s for s in states if s.get("entity_id", "").startswith(f"{domain}.")]
        if area:
            area_lower = area.lower()
            def _in_area(s: Dict[str, Any]) -> bool:
                registered = entity_area.get(s.get("entity_id", ""))
                if registered is not None:
                    # Registry-backed: authoritative. An entity belongs to an area even
                    # when its name says nothing about the room.
                    return area_lower in registered.lower()
                # No registry area (registry unreachable, entity not registered, or no
                # area assigned): keep the name/attribute match so nothing main found
                # disappears.
                attrs = s.get("attributes", {})
                return (area_lower in (attrs.get("friendly_name", "") or "").lower()
                        or area_lower in (attrs.get("area", "") or "").lower())

            states = [s for s in states if _in_area(s)]
        if name:
            name_lower = name.lower()
            states = [
                s for s in states
                if name_lower in (s.get("attributes", {}).get("friendly_name", "") or "").lower()
                or name_lower in (s.get("entity_id", "") or "").lower()]

    # Cap resolution: an explicit per-call ``max`` always wins; otherwise the
    # operator's HASS_MAX_ENTITIES value (if set); otherwise no cap at all.
    explicit = (
        max_entities
        if (isinstance(max_entities, int) and not isinstance(max_entities, bool) and max_entities > 0)
        else None)
    cap = explicit if explicit is not None else _get_max_entities_config()
    total = len(states)
    truncated = cap is not None and total > cap
    if truncated:
        states = states[:cap]

    entities = []
    for s in states:
        eid = s["entity_id"]
        item = {
            "entity_id": eid, "state": s["state"],
            "friendly_name": s.get("attributes", {}).get("friendly_name", "")}
        if eid in entity_area:
            item["area"] = entity_area[eid]
        if eid in entity_device:
            item["device"] = entity_device[eid]
        entities.append(item)

    result: Dict[str, Any] = {"count": len(entities), "entities": entities}
    if truncated:
        result["truncated"] = True
        result["truncated_note"] = (
            f"Showing first {cap} of {total} matching entities. "
            "Refine with domain/area/name filters or an explicit entity_ids "
            "list, or pass a larger max.")
    return result


# ── [ha-area-registry] Area/device resolution via the WebSocket registry ─────
# /api/states does NOT expose area or device. The registries are WebSocket-only.
# Without this, `area` filtering degrades to a friendly_name substring match,
# which silently misses every entity whose name doesn't repeat its room.
_REGISTRY_CACHE: Dict[str, Any] = {"ts": 0.0, "entity_area": {}, "entity_device": {}}
_REGISTRY_TTL = 300.0
_REGISTRY_FAILURE_TTL = 60.0  # a failed pull is not retried on every call for this long
_REGISTRY_TIMEOUT_S = 20.0
_REGISTRY_MAX_MSG = 64 * 1024 * 1024  # entity_registry/list on a big install exceeds aiohttp's 4 MiB default


async def _registry_area_map():
    """Return ({entity_id: area_name}, {entity_id: device_name}), cached."""
    import time as _time

    # ``ts`` is 0.0 until the first successful fetch. A successful fetch is cached
    # even when it maps nothing (an install with no areas assigned) — otherwise
    # every list call would re-download the full registry.
    hass_url, hass_token = _get_config()
    # Keyed by instance: profiles pointing at different Home Assistants must not
    # see each other's area names.
    if _REGISTRY_CACHE.get("url") == hass_url:
        age = _time.time() - _REGISTRY_CACHE.get("failed_at", 0.0)
        if age < _REGISTRY_FAILURE_TTL:
            return {}, {}
        if _REGISTRY_CACHE["ts"] and _time.time() - _REGISTRY_CACHE["ts"] < _REGISTRY_TTL:
            return _REGISTRY_CACHE["entity_area"], _REGISTRY_CACHE["entity_device"]

    import aiohttp

    ws_url = hass_url.replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"

    async def pull() -> Optional[tuple]:
        """Auth + the three registry lists. None = auth refused (logged, not cached)."""
        ent_area: Dict[str, str] = {}
        ent_dev: Dict[str, str] = {}
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(ws_url, max_msg_size=_REGISTRY_MAX_MSG) as ws:
                await ws.receive_json()                      # auth_required
                await ws.send_json({"type": "auth", "access_token": hass_token})
                auth_reply = await ws.receive_json()
                if auth_reply.get("type") != "auth_ok":
                    logger.warning(
                        "HA registry websocket auth failed (%s); area filter degraded",
                        auth_reply.get("message") or auth_reply.get("type"))
                    return None

                async def fetch(mid, mtype):
                    await ws.send_json({"id": mid, "type": mtype})
                    while True:
                        m = await ws.receive_json()
                        if m.get("id") == mid and m.get("type") == "result":
                            if not m.get("success", True):
                                raise RuntimeError(f"{mtype} refused: {m.get('error')}")
                            return m.get("result") or []

                areas = {a["area_id"]: a["name"] for a in await fetch(1, "config/area_registry/list")}
                devices = {d["id"]: d for d in await fetch(2, "config/device_registry/list")}
                for e in await fetch(3, "config/entity_registry/list"):
                    dev = devices.get(e.get("device_id")) or {}
                    aid = e.get("area_id") or dev.get("area_id")
                    if aid in areas:
                        ent_area[e["entity_id"]] = areas[aid]
                    dname = dev.get("name_by_user") or dev.get("name")
                    if dname:
                        ent_dev[e["entity_id"]] = dname
        return ent_area, ent_dev

    try:
        # One deadline for the whole pull (connect, auth, three lists): a stalled
        # HA must not hold ha_list_entities hostage.
        pulled = await asyncio.wait_for(pull(), timeout=_REGISTRY_TIMEOUT_S)
    except asyncio.TimeoutError:
        logger.warning(
            "HA registry lookup timed out after %ss; area filter degraded", _REGISTRY_TIMEOUT_S)
        pulled = None
    except Exception as exc:                                  # noqa: BLE001
        logger.warning("HA registry lookup failed (%s: %s); area filter degraded", type(exc).__name__, exc)
        pulled = None
    if pulled is None:
        # Remember the failure briefly so a stalled or refusing HA costs one
        # timeout per window, not one per call.
        _REGISTRY_CACHE.update({"url": hass_url, "ts": 0.0, "failed_at": _time.time(),
                                "entity_area": {}, "entity_device": {}})
        return {}, {}

    ent_area, ent_dev = pulled
    _REGISTRY_CACHE.update({"url": hass_url, "ts": _time.time(), "failed_at": 0.0,
                            "entity_area": ent_area, "entity_device": ent_dev})
    return ent_area, ent_dev


def _warm_registry_maps():
    """Area/device maps from a fresh cache for this instance, without a network call."""
    import time as _time

    try:
        hass_url, _ = _get_config()
    except ValueError:
        return {}, {}
    if (_REGISTRY_CACHE.get("url") == hass_url and _REGISTRY_CACHE["ts"]
            and _time.time() - _REGISTRY_CACHE["ts"] < _REGISTRY_TTL):
        return _REGISTRY_CACHE["entity_area"], _REGISTRY_CACHE["entity_device"]
    return {}, {}


async def _async_list_entities(
    domain: Optional[str] = None,
    area: Optional[str] = None,
    entity_ids: Optional[Any] = None,
    name: Optional[str] = None,
    max_entities: Optional[int] = None,
) -> Dict[str, Any]:
    """Fetch entity states from HA; filters are applied before the payload is built."""
    states = await _api_json("GET", "/api/states", 15)
    # The registry is only needed to resolve rooms; a plain list stays one REST call.
    if area:
        entity_area, entity_device = await _registry_area_map()
    else:
        entity_area, entity_device = _warm_registry_maps()
    return _filter_and_summarize(
        states, domain=domain, area=area, entity_ids=entity_ids, name=name,
        max_entities=max_entities, entity_area=entity_area, entity_device=entity_device)


def _ha_timestamp_to_local(value: Any) -> Any:
    """Convert a Home Assistant UTC ISO timestamp to the host's local time."""
    if not isinstance(value, str) or not value.strip():
        return value
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().isoformat()
    except (ValueError, TypeError):
        return value


async def _async_get_state(entity_id: str) -> Dict[str, Any]:
    data = await _api_json("GET", f"/api/states/{entity_id}", 10)
    return {
        "entity_id": data["entity_id"], "state": data["state"], "attributes": data.get("attributes", {}),
        "last_changed": _ha_timestamp_to_local(data.get("last_changed")),
        "last_updated": _ha_timestamp_to_local(data.get("last_updated"))}


def _build_service_payload(entity_id: Optional[str] = None, data: Optional[Dict[str, Any]] = None) -> Dict:
    """JSON payload for a HA service call; ``entity_id`` overrides data["entity_id"]."""
    payload: Dict[str, Any] = dict(data or {})
    if entity_id:
        payload["entity_id"] = entity_id
    return payload


def _parse_service_response(domain: str, service: str, result: Any, entity_id: Optional[str] = None) -> Dict[str, Any]:
    """Compact a HA service response. Handles both shapes: a bare list of changed states
    (default), and ``{"changed_states": [...], "service_response": {...}}`` (calls made with
    ``?return_response``, required by response services like weather.get_forecasts)."""
    affected = []
    service_response = None
    if isinstance(result, list):
        affected = [{"entity_id": s.get("entity_id", ""), "state": s.get("state", "")} for s in result if isinstance(s, dict)]
    elif isinstance(result, dict):
        for s in result.get("changed_states") or []:
            if isinstance(s, dict):
                affected.append({"entity_id": s.get("entity_id", ""), "state": s.get("state", "")})
        if result.get("service_response") is not None:
            service_response = result["service_response"]
    out: Dict[str, Any] = {"success": True, "service": f"{domain}.{service}", "affected_entities": affected}
    if service_response is not None:
        out["service_response"] = service_response
    if entity_id and not affected and service_response is None:
        # HA returns 200 with an empty list when nothing changed — _async_call_service
        # verifies why (missing / wrong domain / already in the requested state) and
        # refines this message before it reaches the agent.
        out["success"] = False
        out["error"] = f"{domain}.{service} affected no entities for {entity_id}"
    return out


async def _async_call_service(
    domain: str, service: str, entity_id: Optional[str] = None, data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    payload = _build_service_payload(entity_id, data)
    try:
        result = await _api_json("POST", f"/api/services/{domain}/{service}", 15, payload)
    except Exception as e:
        # Response services (e.g. weather.get_forecasts) 400 unless the call asks for the
        # response; plain services 400 when it does — so retry once with the flag only when
        # HA says responses are required.
        if "requires responses" in str(e):
            result = await _api_json("POST", f"/api/services/{domain}/{service}?return_response", 15, payload)
        else:
            raise
    out = _parse_service_response(domain, service, result, entity_id)
    if out.get("success") is False and entity_id:
        # HA returns only states that actually changed: an empty result can mean the target
        # is missing from this domain, OR the entity already had the requested state. Check
        # which, so the agent gets an actionable answer instead of a guess.
        import aiohttp
        try:
            state = await _api_json("GET", f"/api/states/{entity_id}", 10)
        except aiohttp.ClientResponseError as e:
            if e.status == 404:
                out["error"] = (
                    f"{domain}.{service} affected no entities: {entity_id} does not exist in "
                    f"Home Assistant. Check the exact entity_id with ha_list_entities."
                )
            else:
                out["error"] += f" (could not verify {entity_id}: HTTP {e.status})"
            return out
        except Exception as e:
            out["error"] += f" (could not verify {entity_id}: {e})"
            return out
        actual_domain = state["entity_id"].split(".", 1)[0]
        # The homeassistant.* services (turn_on, toggle, update_entity, ...) act on entities of
        # any domain, so a domain mismatch there is not a mistake.
        if actual_domain != domain and domain != "homeassistant":
            out["error"] = (
                f"{domain}.{service} affected no entities: {entity_id} exists but is in the "
                f"'{actual_domain}' domain, not '{domain}'. Service calls only affect entities "
                f"of their own domain — call {actual_domain}.{service} instead."
            )
            return out
        # HA returns an empty list for radio/physical devices whose state flips only after
        # the device confirms (150-300 ms later): the service call itself succeeded, so do
        # not claim "already in state" from a pre-call reading. Only report a true no-op
        # when the observed state matches the state the service was asked to set.
        requested_state = {"turn_on": "on", "turn_off": "off"}.get(service)
        observed = state.get("state")
        out["success"] = True
        del out["error"]
        if requested_state is not None and observed == requested_state:
            out["note"] = f"{entity_id} was already in state '{observed}' — nothing to change."
        elif requested_state is not None:
            out["note"] = (
                f"{entity_id} accepted {domain}.{service}; device state is still transitioning "
                f"(currently '{observed}')."
            )
        else:
            out["note"] = f"{entity_id} accepted {domain}.{service} (no changed-state reported)."
    return out


async def _async_list_services(domain: Optional[str] = None) -> Dict[str, Any]:
    """Available services, optionally filtered by domain, compacted for context."""
    services = await _api_json("GET", "/api/services", 15)
    if domain:
        services = [s for s in services if s.get("domain") == domain]
    result = []
    for svc_domain in services:
        domain_services = {}
        for svc_name, svc_info in svc_domain.get("services", {}).items():
            svc_entry: Dict[str, Any] = {"description": svc_info.get("description", "")}
            fields = svc_info.get("fields", {})
            if fields:
                svc_entry["fields"] = {
                    k: v.get("description", "") for k, v in fields.items() if isinstance(v, dict)}
            domain_services[svc_name] = svc_entry
        result.append({"domain": svc_domain.get("domain", ""), "services": domain_services})
    return {"count": len(result), "domains": result}


# ── sync wrappers (handler signature: (args, **kw) -> str) ───────────────────
def _run_async(coro):
    """Run a coroutine from a sync handler; hops to a thread if a loop is already running."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():  # already inside a loop: asyncio.run() needs its own thread
        import concurrent.futures
        import contextvars
        # Carry the caller's Context: the coroutine reads HASS_* via get_secret, which is profile-scoped.
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(contextvars.copy_context().run, asyncio.run, coro).result(timeout=30)
    return asyncio.run(coro)


def _dispatch(coro, log_name: str, fail_msg: str) -> str:
    """Run ``coro`` and wrap as ``{"result": ...}``; on error log and return tool_error."""
    try:
        return json.dumps({"result": _run_async(coro)})
    except Exception as e:
        logger.error("%s error: %s", log_name, e)
        return tool_error(f"{fail_msg}: {e}")


def _handle_list_entities(args: dict, **kw) -> str:
    max_entities = args.get("max")
    if isinstance(max_entities, str):
        try:
            max_entities = int(max_entities)
        except ValueError:
            return tool_error(f"Invalid 'max' value: {max_entities!r} (expected a positive integer)")
    return _dispatch(
        _async_list_entities(
            domain=args.get("domain"), area=args.get("area"), entity_ids=args.get("entity_ids"),
            name=args.get("name"), max_entities=max_entities),
        "ha_list_entities", "Failed to list entities")


def _handle_get_state(args: dict, **kw) -> str:
    entity_id = args.get("entity_id", "")
    if not entity_id:
        return tool_error("Missing required parameter: entity_id")
    if not _ENTITY_ID_RE.match(entity_id):
        return tool_error(f"Invalid entity_id format: {entity_id}")
    violation = _operator_filter_violation(entity_id)
    if violation:
        return tool_error(
            f"{entity_id} is excluded by operator config ({violation}). "
            "Use ha_list_entities with a filter to discover an alternative "
            "entity, or ask the operator to adjust the allow/deny lists.")
    return _dispatch(_async_get_state(entity_id), "ha_get_state", f"Failed to get state for {entity_id}")


def _handle_call_service(args: dict, **kw) -> str:
    domain = args.get("domain", "")
    service = args.get("service", "")
    if not domain or not service:
        return tool_error("Missing required parameters: domain and service")
    # Format check BEFORE the blocklist: rejects "shell_command/../light" style bypasses.
    if not _SERVICE_NAME_RE.match(domain):
        return tool_error(f"Invalid domain format: {domain!r}")
    if not _SERVICE_NAME_RE.match(service):
        return tool_error(f"Invalid service format: {service!r}")
    if domain in _BLOCKED_DOMAINS:
        return tool_error(
            f"Service domain '{domain}' is blocked for security. "
            f"Blocked domains: {', '.join(sorted(_BLOCKED_DOMAINS))}")
    entity_id = args.get("entity_id")
    if entity_id and not _ENTITY_ID_RE.match(entity_id):
        return tool_error(f"Invalid entity_id format: {entity_id}")
    data = args.get("data")
    if isinstance(data, str):  # XML tool-calling mode delivers data as a JSON string
        try:
            data = json.loads(data) if data.strip() else None
        except json.JSONDecodeError as e:
            return tool_error(f"Invalid JSON string in 'data' parameter: {e}")
    # Operator allow/deny lists also govern control: refuse before any network call.
    # Checked on the payload actually sent (entity_id overrides data["entity_id"]).
    if any(_get_entity_filter_config()):
        if data is not None and not isinstance(data, dict):
            return tool_error(
                f"Invalid 'data' parameter: expected a JSON object, got {type(data).__name__}")
        violation = _service_call_violation(domain, service, _build_service_payload(entity_id, data))
    else:
        violation = None
    if violation:
        return tool_error(f"Refused by operator config: {violation}")
    return _dispatch(
        _async_call_service(domain, service, entity_id, data),
        "ha_call_service", f"Failed to call {domain}.{service}")


def _check_ha_available() -> bool:
    """Tool is only available when HASS_TOKEN is set."""
    return bool(get_secret("HASS_TOKEN"))


# ── tool schemas ─────────────────────────────────────────────────────────────
HA_LIST_ENTITIES_SCHEMA: Dict[str, Any] = {
    "name": "ha_list_entities",
    "description": (
        "List Home Assistant entities. On a large install a bare call can return "
        "thousands of entities, so use the filters (domain, area, name, "
        "entity_ids) to keep the response small and targeted. Each entity "
        "includes its area and device when known (always after an area query).\n"
        "For a request about a room (\"the kitchen lights\"), filter by `area`, "
        "not `name`: a device belongs to a room by its Home Assistant area, and "
        "its name often does not mention the room. Take everything the area "
        "filter returns as part of that room, and judge what an entity is by "
        "the domain prefix of its entity_id (every light.* is a light), not by "
        "its name.\n"
        "An operator may configure a global cap (HASS_MAX_ENTITIES); when that "
        "or a 'max' parameter limits a result, the response is marked "
        "'truncated' with a note on how to narrow the query or get more."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "domain": {
                "type": "string",
                "description": (
                    "Entity domain to filter by (e.g. 'light', 'switch', 'climate', "
                    "'sensor', 'binary_sensor', 'cover', 'fan', 'media_player'). "
                    "The cheapest filter; combine with area or name for a "
                    "targeted query. Omit to list all entities."
                ),
            },
            "area": {
                "type": "string",
                "description": (
                    "Area/room name to filter by (e.g. 'living room', 'kitchen'). "
                    "Use this, not name, for any request about a room. "
                    "Resolved against the Home Assistant area registry, so an "
                    "entity is matched even when its name does not mention the "
                    "room; falls back to matching friendly names when the "
                    "registry is unreachable. Omit to list all."
                ),
            },
            "name": {
                "type": "string",
                "description": (
                    "Substring to match against entity friendly names and "
                    "entity_ids (e.g. 'thermostat', 'temperature'). "
                    "Useful when you don't know the domain. Not for rooms: "
                    "use area, because names rarely mention the room."
                ),
            },
            "entity_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Exact list of entity_ids to fetch (e.g. "
                    "['climate.office', 'sensor.office_temp']). Returns just "
                    "these entities, ignoring the domain/area/name filters."
                ),
            },
            "max": {
                "type": "integer",
                "description": (
                    "Maximum number of entities to return for this call. "
                    "Overrides any operator-configured cap (HASS_MAX_ENTITIES) "
                    "for this one call; set higher if you genuinely need a "
                    "larger batch and the query is already well filtered."
                ),
            },
        },
        "required": [],
    },
}

HA_GET_STATE_SCHEMA: Dict[str, Any] = {
    "name": "ha_get_state",
    "description": (
        "Get the detailed state of a single Home Assistant entity, including all "
        "attributes (brightness, color, temperature setpoint, sensor readings, etc.)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "entity_id": {
                "type": "string",
                "description": (
                    "The entity ID to query (e.g. 'light.living_room', "
                    "'climate.thermostat', 'sensor.temperature')."
                ),
            },
        },
        "required": ["entity_id"],
    },
}

HA_LIST_SERVICES_SCHEMA: Dict[str, Any] = {
    "name": "ha_list_services",
    "description": (
        "List available Home Assistant services (actions) for device control. "
        "Shows what actions can be performed on each device type and what "
        "parameters they accept. Use this to discover how to control devices "
        "found via ha_list_entities."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "domain": {
                "type": "string",
                "description": (
                    "Filter by domain (e.g. 'light', 'climate', 'switch'). "
                    "Omit to list services for all domains."
                ),
            },
        },
        "required": [],
    },
}

HA_CALL_SERVICE_SCHEMA: Dict[str, Any] = {
    "name": "ha_call_service",
    "description": (
        "Call a Home Assistant service to control a device. Use ha_list_services "
        "to discover available services and their parameters for each domain. "
        "Never guess an entity_id: call ha_list_entities first (optionally with a "
        "domain filter) and use the exact ids it returns. The service domain MUST "
        "match the entity's own domain: a light.* entity needs light.turn_on, a "
        "switch.* entity needs switch.turn_on, an input_boolean.* toggle needs "
        "input_boolean.turn_on — calling the wrong domain affects nothing. The "
        "error field explains exactly what went wrong (missing entity, wrong "
        "domain, or already in that state) — follow its suggestion and retry. "
        "If the operator restricts entities, target explicit entity_ids: calls "
        "on restricted entities, or by area/device/label or with no target, are refused."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "domain": {
                "type": "string",
                "description": (
                    "Service domain (e.g. 'light', 'switch', 'climate', "
                    "'cover', 'media_player', 'fan', 'scene', 'script')."
                ),
            },
            "service": {
                "type": "string",
                "description": (
                    "Service name (e.g. 'turn_on', 'turn_off', 'toggle', "
                    "'set_temperature', 'set_hvac_mode', 'open_cover', "
                    "'close_cover', 'set_volume_level')."
                ),
            },
            "entity_id": {
                "type": "string",
                "description": (
                    "Target entity ID (e.g. 'light.living_room'). "
                    "Some services (like scene.turn_on) may not need this."
                ),
            },
            "data": {
                "type": "string",
                "description": (
                    "Additional service data as a JSON string. Examples: "
                    '{"brightness": 255, "color_name": "blue"} for lights, '
                    '{"temperature": 22, "hvac_mode": "heat"} for climate, '
                    '{"volume_level": 0.5} for media players.'
                ),
            },
        },
        "required": ["domain", "service"],
    },
}


def _handle_list_services(args: dict, **kw) -> str:
    return _dispatch(
        _async_list_services(domain=args.get("domain")), "ha_list_services", "Failed to list services")


TOOLS = (
    (HA_LIST_ENTITIES_SCHEMA, _handle_list_entities),
    (HA_GET_STATE_SCHEMA, _handle_get_state),
    (HA_LIST_SERVICES_SCHEMA, _handle_list_services),
    (HA_CALL_SERVICE_SCHEMA, _handle_call_service),
)


def register_tools(ctx) -> None:
    """Register the four Home Assistant tools in the ``homeassistant`` toolset via the plugin API."""
    for schema, handler in TOOLS:
        ctx.register_tool(
            name=schema["name"], toolset=TOOLSET, schema=schema, handler=handler,
            check_fn=_check_ha_available, emoji="🏠")
