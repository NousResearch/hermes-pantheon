"""Home Assistant adapter: WS ``state_changed`` events -> MessageEvents; outbound = persistent notifications.

Requires aiohttp, HASS_TOKEN (Long-Lived Access Token) and HASS_URL (default http://homeassistant.local:8123).

Moved out of hermes-agent core (``plugins/platforms/homeassistant/adapter.py``) into the standalone
``hermes-homeassistant`` plugin; behaviour is unchanged. Registration lives in the package ``__init__``.
"""

import asyncio
import errno
import json
import logging
import re
import sys
import time
import uuid
from datetime import datetime
from typing import Any, Dict, Optional, Set

try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    aiohttp = None  # type: ignore[assignment]

from gateway.restart import is_supervised_gateway_launch
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import gateway_trust_env, BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType
from gateway.platforms._shared import get_scoped_secret as _get_scoped_secret, send_error

from .tools import url_scheme_problem

logger = logging.getLogger(__name__)

# Title of the persistent notification outbound replies are posted as.
NOTIFICATION_TITLE = "Hermes Agent"
# A notify service name (``notify.<name>``): the form cron delivery targets take.
_NOTIFY_SERVICE = re.compile(r"[a-z0-9_]+")


def check_ha_requirements() -> bool:
    """Check if Home Assistant runtime dependencies are available."""
    return AIOHTTP_AVAILABLE


def validate_ha_config(config: PlatformConfig) -> bool:
    """True when Home Assistant has enough credential config to connect."""
    return bool((getattr(config, "token", None) or _get_scoped_secret("HASS_TOKEN", "")).strip())


def _domain_of(entity_id: str) -> str:
    return entity_id.split(".")[0] if "." in entity_id else ""


def _auth_headers(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# domain -> description template; see ``_format_state_change`` for the fields.
_TURNED = "[Home Assistant] {name}: turned {on_off}"
_DOMAIN_TEMPLATES = {
    "climate": (
        "[Home Assistant] {name}: HVAC mode changed from "
        "'{old}' to '{new}' (current: {temp}, target: {target})"
    ),
    "sensor": "[Home Assistant] {name}: changed from {old}{unit} to {new}{unit}",
    "binary_sensor": "[Home Assistant] {name}: {new_trig} (was {old_trig})",
    "light": _TURNED,
    "switch": _TURNED,
    "fan": _TURNED,
    "alarm_control_panel": "[Home Assistant] {name}: alarm state changed from '{old}' to '{new}'",
}
_DEFAULT_TEMPLATE = "[Home Assistant] {name} ({entity_id}): changed from '{old}' to '{new}'"
_TRIGGERED = ("cleared", "triggered")  # binary_sensor wording, indexed by ``state == "on"``


def _connect_error_detail(exc: BaseException) -> str:
    """Annotate macOS Local Network Privacy denials so launchd HA failures are actionable (#71206).

    Only a supervised (launchd) gateway on macOS can be denied this way; the same errno from a
    Terminal-run gateway or on another OS is a genuinely unreachable host.
    """
    text = str(exc)
    os_error = getattr(exc, "os_error", None) or exc.__cause__ or exc
    if (sys.platform == "darwin" and is_supervised_gateway_launch()
            and getattr(os_error, "errno", None) == errno.EHOSTUNREACH):
        return (
            f"{text} — macOS Local Network Privacy is blocking this launchd gateway from the LAN. "
            "Run `hermes gateway install` to regenerate the launchd job, then `hermes gateway restart`. "
            "https://github.com/NousResearch/hermes-agent/issues/71206"
        )
    return text


class HomeAssistantAdapter(BasePlatformAdapter):
    """``state_changed`` -> MessageEvents with domain/entity filtering and per-entity cooldowns."""

    MAX_MESSAGE_LENGTH = 4096
    _BACKOFF_STEPS = [5, 10, 30, 60]  # reconnect backoff (seconds)

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform("homeassistant"))
        self._session: Optional["aiohttp.ClientSession"] = None
        self._ws: Optional["aiohttp.ClientWebSocketResponse"] = None
        self._rest_session: Optional["aiohttp.ClientSession"] = None
        self._listen_task: Optional[asyncio.Task] = None
        self._msg_id: int = 0
        extra = config.extra or {}
        # URL is scoped like the token below: a secondary's HASS_TOKEN must never be posted to the
        # DEFAULT profile's HA instance (os.environ under multiplex).
        self._hass_url: str = (extra.get("url") or _get_scoped_secret("HASS_URL", "http://homeassistant.local:8123")).rstrip("/")
        self._hass_token: str = config.token or _get_scoped_secret("HASS_TOKEN", "")
        self._watch_domains: Set[str] = set(extra.get("watch_domains", []))
        self._watch_entities: Set[str] = set(extra.get("watch_entities", []))
        self._ignore_entities: Set[str] = set(extra.get("ignore_entities", []))
        self._watch_all: bool = bool(extra.get("watch_all", False))
        self._cooldown_seconds: int = int(extra.get("cooldown_seconds", 30))
        self._last_event_time: Dict[str, float] = {}  # entity_id -> last event ts

    def _next_id(self) -> int:
        self._msg_id += 1
        return self._msg_id

    @staticmethod
    def _new_session() -> "aiohttp.ClientSession":
        return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30), trust_env=gateway_trust_env())

    # -- Connection lifecycle -----------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Connect to HA WebSocket API and subscribe to events."""
        if not AIOHTTP_AVAILABLE:
            logger.warning("[%s] aiohttp not installed. Run: pip install aiohttp", self.name)
            return False
        if not self._hass_token:
            logger.warning("[%s] No HASS_TOKEN configured", self.name)
            return False
        if problem := url_scheme_problem(self._hass_url):
            logger.error("[%s] %s", self.name, problem)
            # Not retryable: only a config edit fixes it, so reconnecting would fail the same way forever.
            self._set_fatal_error("homeassistant_url_scheme", problem, retryable=False)
            return False
        try:
            if not await self._ws_connect():
                return False
            self._rest_session = self._new_session()  # dedicated REST session for send()
            if not (self._watch_domains or self._watch_entities or self._watch_all):
                logger.warning(
                    "[%s] No watch_domains, watch_entities, or watch_all configured. "
                    "All state_changed events will be dropped. Configure filters in "
                    "your HA platform config to receive events.",
                    self.name)
            self._listen_task = asyncio.create_task(self._listen_loop())
            self._running = True
            logger.info("[%s] Connected to %s", self.name, self._hass_url)
            self._wire_plugin_handlers(None)
            return True
        except Exception as e:
            logger.error("[%s] Failed to connect: %s", self.name, _connect_error_detail(e))
            return False

    async def _ws_connect(self) -> bool:
        """Open the WebSocket, authenticate, and subscribe to ``state_changed``."""
        ws_url = self._hass_url.replace("https://", "wss://").replace("http://", "ws://")
        self._session = self._new_session()
        self._ws = await self._session.ws_connect(f"{ws_url}/api/websocket", heartbeat=30, timeout=30)
        msg = await self._ws.receive_json()
        if msg.get("type") != "auth_required":
            return await self._handshake_failed("Expected auth_required, got: %s", msg.get("type"))
        await self._ws.send_json({"type": "auth", "access_token": self._hass_token})
        msg = await self._ws.receive_json()
        if msg.get("type") != "auth_ok":
            return await self._handshake_failed("Auth failed: %s", msg)
        await self._ws.send_json({"id": self._next_id(), "type": "subscribe_events", "event_type": "state_changed"})
        msg = await self._ws.receive_json()
        if not msg.get("success"):
            return await self._handshake_failed("Failed to subscribe to events: %s", msg)
        return True

    async def _handshake_failed(self, fmt: str, detail: Any) -> bool:
        logger.error(fmt, detail)
        await self._cleanup_ws()
        return False

    @staticmethod
    async def _close(obj) -> None:
        if obj and not obj.closed:
            await obj.close()

    async def _cleanup_ws(self) -> None:
        await self._close(self._ws)
        self._ws = None
        await self._close(self._session)
        self._session = None

    async def disconnect(self) -> None:
        self._running = False
        if self._listen_task:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
            self._listen_task = None
        await self._cleanup_ws()
        await self._close(self._rest_session)
        self._rest_session = None
        logger.info("[%s] Disconnected", self.name)

    # -- Event listener -----------------------------------------------------

    async def _listen_loop(self) -> None:
        """Main event loop with automatic reconnection."""
        backoff_idx = 0
        while self._running:
            try:
                await self._read_events()
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning("[%s] WebSocket error: %s", self.name, _connect_error_detail(e))
            if not self._running:
                return
            delay = self._BACKOFF_STEPS[min(backoff_idx, len(self._BACKOFF_STEPS) - 1)]
            logger.info("[%s] Reconnecting in %ds...", self.name, delay)
            await asyncio.sleep(delay)
            backoff_idx += 1
            try:
                await self._cleanup_ws()
                if await self._ws_connect():
                    backoff_idx = 0
                    logger.info("[%s] Reconnected", self.name)
            except Exception as e:
                logger.warning("[%s] Reconnection failed: %s", self.name, _connect_error_detail(e))

    async def _read_events(self) -> None:
        """Read events from WebSocket until disconnected."""
        if self._ws is None or self._ws.closed:
            return
        async for ws_msg in self._ws:
            if ws_msg.type in {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR}:
                break
            if ws_msg.type != aiohttp.WSMsgType.TEXT:
                continue
            try:
                data = json.loads(ws_msg.data)
            except json.JSONDecodeError:
                logger.debug("Invalid JSON from HA WS: %s", ws_msg.data[:200])
                continue
            if data.get("type") == "event":
                await self._handle_ha_event(data.get("event", {}))

    def _passes_filters(self, entity_id: str) -> bool:
        """Closed by default: requires watch_domains, watch_entities, or watch_all."""
        if entity_id in self._ignore_entities:
            return False
        if self._watch_domains or self._watch_entities:
            return _domain_of(entity_id) in self._watch_domains or entity_id in self._watch_entities
        return self._watch_all

    async def _handle_ha_event(self, event: Dict[str, Any]) -> None:
        """Process a state_changed event from Home Assistant."""
        event_data = event.get("data", {})
        entity_id: str = event_data.get("entity_id", "")
        if not entity_id or not self._passes_filters(entity_id):
            return
        now = time.time()
        if (now - self._last_event_time.get(entity_id, 0)) < self._cooldown_seconds:
            return
        message = self._format_state_change(
            entity_id, event_data.get("old_state", {}), event_data.get("new_state", {}))
        if not message:
            return
        self._last_event_time[entity_id] = now
        source = self.build_source(
            chat_id="ha_events", chat_name="Home Assistant Events", chat_type="channel",
            user_id="homeassistant", user_name="Home Assistant")
        await self.handle_message(MessageEvent(
            text=message, message_type=MessageType.TEXT, source=source,
            message_id=f"ha_{entity_id}_{int(now)}", timestamp=datetime.now()))

    @staticmethod
    def _format_state_change(entity_id: str, old_state: Dict[str, Any], new_state: Dict[str, Any]) -> Optional[str]:
        """Convert a state_changed event into a human-readable description."""
        if not new_state:
            return None
        old_val = old_state.get("state", "unknown") if old_state else "unknown"
        new_val = new_state.get("state", "unknown")
        attrs = new_state.get("attributes", {})
        domain = _domain_of(entity_id)
        if old_val == new_val:
            # Only temperature fields are actionable attribute-only climate events.
            # Ignore unrelated attribute changes.
            if domain == "climate" and new_val not in {"unknown", "unavailable"}:
                old_attrs = (old_state or {}).get("attributes", {})
                changes = [
                    f"{key}: {old_attrs.get(key, '?')} -> {attrs.get(key, '?')}"
                    for key in ("current_temperature", "temperature", "target_temp_low", "target_temp_high")
                    if old_attrs.get(key) != attrs.get(key)
                ]
                if changes:
                    return (f"[Home Assistant] {attrs.get('friendly_name', entity_id)}: "
                            f"temperature changed ({'; '.join(changes)}; HVAC mode: '{new_val}')")
            return None
        template = _DOMAIN_TEMPLATES.get(domain, _DEFAULT_TEMPLATE)
        if domain in {"switch", "light", "fan", "binary_sensor"} and (
                old_val not in {"on", "off"} or new_val not in {"on", "off"}):
            template = _DEFAULT_TEMPLATE
        return template.format(
            name=attrs.get("friendly_name", entity_id), entity_id=entity_id, old=old_val, new=new_val,
            temp=attrs.get("current_temperature", "?"), target=attrs.get("temperature", "?"),
            unit=attrs.get("unit_of_measurement", ""), on_off="on" if new_val == "on" else "off",
            new_trig=_TRIGGERED[new_val == "on"], old_trig=_TRIGGERED[old_val == "on"])

    # -- Outbound messaging -------------------------------------------------

    async def send(
        self, chat_id: str, content: str, reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a notification via HA REST API (persistent_notification.create).

        REST rather than the WebSocket, to avoid racing the listener loop that
        reads from the same WS connection.
        """
        url = f"{self._hass_url}/api/services/persistent_notification/create"
        payload = {"title": NOTIFICATION_TITLE, "message": content[:self.MAX_MESSAGE_LENGTH]}

        async def _post(session) -> SendResult:
            async with session.post(
                url, headers=_auth_headers(self._hass_token), json=payload, timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status < 300:
                    return SendResult(success=True, message_id=uuid.uuid4().hex[:12])
                return SendResult(success=False, error=f"HTTP {resp.status}: {await resp.text()}")

        try:
            if self._rest_session:
                return await _post(self._rest_session)
            async with aiohttp.ClientSession(trust_env=gateway_trust_env()) as session:
                return await _post(session)
        except asyncio.TimeoutError:
            return SendResult(success=False, error="Timeout sending notification to HA")
        except Exception as e:
            return SendResult(success=False, error=str(e))

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": "Home Assistant Events", "type": "channel", "url": self._hass_url}


# -- Standalone (out-of-process) sender — cron deliver=homeassistant ---------


async def _standalone_send(
    pconfig, chat_id: str, message: str, *,
    thread_id: Optional[str] = None, media_files: Optional[list] = None, force_document: bool = False,
) -> Dict[str, Any]:
    """Send through the HA notify service the target names, without a live gateway adapter.

    The target (``HASS_HOME_CHANNEL`` or ``deliver=homeassistant:<target>``) is a notify service name such
    as ``mobile_app_my_phone``, so the call is ``notify.<target>``. Home Assistant documents ``notify.notify``
    as "the first notify action it can find" and answers it with a 500 when given a ``target`` it does not
    own; that grouped call is kept only for a target HA has no service for (400) and for an empty target.

    Token: ``pconfig.token`` then ``HASS_TOKEN``; URL: ``pconfig.extra["url"]`` then ``HASS_URL``.
    ``thread_id``/``media_files``/``force_document`` are signature parity only (HA has no threads/attachments).
    """
    if not AIOHTTP_AVAILABLE:
        return send_error("aiohttp not installed. Run: pip install aiohttp")
    extra = getattr(pconfig, "extra", {}) or {}
    hass_url = (extra.get("url") or _get_scoped_secret("HASS_URL", "")).rstrip("/")
    token = (getattr(pconfig, "token", None) or _get_scoped_secret("HASS_TOKEN", "")).strip()
    if not hass_url or not token:
        return send_error("Home Assistant standalone send: HASS_URL and HASS_TOKEN must both be set")
    if problem := url_scheme_problem(hass_url):
        return send_error(f"Home Assistant standalone send: {problem}")
    url = f"{hass_url}/api/services/notify/notify"
    payload = {"message": message, "target": chat_id}
    calls = [(url, payload)]
    service = (chat_id or "").strip().removeprefix("notify.")
    if _NOTIFY_SERVICE.fullmatch(service):
        calls.insert(0, (f"{hass_url}/api/services/notify/{service}", {"message": message}))
    try:
        async with HomeAssistantAdapter._new_session() as session:
            for i, (url, payload) in enumerate(calls):
                async with session.post(url, headers=_auth_headers(token), json=payload) as resp:
                    if resp.status in {200, 201}:
                        return {"success": True, "platform": "homeassistant", "chat_id": chat_id}
                    error = f"Home Assistant API error ({resp.status}) from {url.removeprefix(hass_url)}: {await resp.text()}"
                    # Only "no such service" falls through to the grouped call; any other failure is final.
                    if resp.status != 400 or i == len(calls) - 1:
                        return send_error(error)
    except asyncio.TimeoutError:
        return send_error("Timeout sending notification to Home Assistant")
    except Exception as e:
        return send_error(f"Home Assistant send failed: {e}")
