"""Tests for the Home Assistant gateway adapter.

Tests real logic: state change formatting, event filtering pipeline,
cooldown behavior, env seeding, and adapter initialization.

Moved from hermes-agent core ``tests/gateway/test_homeassistant.py``.
"""

import errno
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import (
    Platform,
    PlatformConfig,
)
from homeassistant_plugin.adapter import (
    NOTIFICATION_TITLE,
    HomeAssistantAdapter,
    check_ha_requirements,
    validate_ha_config,
)

HA = Platform("homeassistant")


# ---------------------------------------------------------------------------
# check_ha_requirements
# ---------------------------------------------------------------------------


class TestCheckRequirements:


    @patch("homeassistant_plugin.adapter.AIOHTTP_AVAILABLE", False)
    def test_returns_false_without_aiohttp(self, monkeypatch):
        monkeypatch.setenv("HASS_TOKEN", "test-token")
        assert check_ha_requirements() is False

    def test_validate_config_accepts_platform_token(self, monkeypatch):
        monkeypatch.delenv("HASS_TOKEN", raising=False)
        config = PlatformConfig(enabled=True, token="config-token")
        assert validate_ha_config(config) is True


class TestValidateConfig:
    def test_returns_false_without_token_in_config_or_env(self, monkeypatch):
        monkeypatch.delenv("HASS_TOKEN", raising=False)
        assert validate_ha_config(PlatformConfig(enabled=True)) is False


# ---------------------------------------------------------------------------
# _format_state_change - pure function, all domain branches
# ---------------------------------------------------------------------------


class TestFormatStateChange:
    @staticmethod
    def fmt(entity_id, old_state, new_state):
        return HomeAssistantAdapter._format_state_change(entity_id, old_state, new_state)

    def test_climate_includes_temperatures(self):
        msg = self.fmt(
            "climate.thermostat",
            {"state": "off"},
            {"state": "heat", "attributes": {
                "friendly_name": "Main Thermostat",
                "current_temperature": 21.5,
                "temperature": 23,
            }},
        )
        assert "Main Thermostat" in msg
        assert "'off'" in msg and "'heat'" in msg
        assert "21.5" in msg and "23" in msg

    def test_sensor_includes_unit(self):
        msg = self.fmt(
            "sensor.temperature",
            {"state": "22.5"},
            {"state": "25.1", "attributes": {
                "friendly_name": "Living Room Temp",
                "unit_of_measurement": "C",
            }},
        )
        assert "22.5C" in msg and "25.1C" in msg
        assert "Living Room Temp" in msg







# ---------------------------------------------------------------------------
# Adapter initialization from config
# ---------------------------------------------------------------------------


class TestAdapterInit:
    def test_url_and_token_from_config_extra(self, monkeypatch):
        monkeypatch.delenv("HASS_URL", raising=False)
        monkeypatch.delenv("HASS_TOKEN", raising=False)

        config = PlatformConfig(
            enabled=True,
            token="config-token",
            extra={"url": "http://192.168.1.50:8123"},
        )
        adapter = HomeAssistantAdapter(config)
        assert adapter._hass_token == "config-token"
        assert adapter._hass_url == "http://192.168.1.50:8123"


    def test_watch_filters_parsed(self):
        config = PlatformConfig(
            enabled=True, token="***",
            extra={
                "watch_domains": ["climate", "binary_sensor"],
                "watch_entities": ["sensor.special"],
                "ignore_entities": ["sensor.uptime", "sensor.cpu"],
                "cooldown_seconds": 120,
            },
        )
        adapter = HomeAssistantAdapter(config)
        assert adapter._watch_domains == {"climate", "binary_sensor"}
        assert adapter._watch_entities == {"sensor.special"}
        assert adapter._ignore_entities == {"sensor.uptime", "sensor.cpu"}
        assert adapter._watch_all is False
        assert adapter._cooldown_seconds == 120


# ---------------------------------------------------------------------------
# Event filtering pipeline (_handle_ha_event)
#
# We mock handle_message (not our code, it's the base class pipeline) to
# capture the MessageEvent that _handle_ha_event produces.
# ---------------------------------------------------------------------------


def _make_adapter(**extra) -> HomeAssistantAdapter:
    config = PlatformConfig(enabled=True, token="tok", extra=extra)
    adapter = HomeAssistantAdapter(config)
    adapter.handle_message = AsyncMock()
    return adapter


def _make_event(entity_id, old_state, new_state, old_attrs=None, new_attrs=None):
    return {
        "data": {
            "entity_id": entity_id,
            "old_state": {"state": old_state, "attributes": old_attrs or {}},
            "new_state": {"state": new_state, "attributes": new_attrs or {"friendly_name": entity_id}},
        }
    }


class TestEventFilteringPipeline:
    @pytest.mark.asyncio
    async def test_ignored_entity_not_forwarded(self):
        adapter = _make_adapter(watch_all=True, ignore_entities=["sensor.uptime"])
        await adapter._handle_ha_event(_make_event("sensor.uptime", "100", "101"))
        adapter.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_unwatched_domain_not_forwarded(self):
        adapter = _make_adapter(watch_domains=["climate"])
        await adapter._handle_ha_event(_make_event("light.bedroom", "off", "on"))
        adapter.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_watched_domain_forwarded(self):
        adapter = _make_adapter(watch_domains=["climate"], cooldown_seconds=0)
        await adapter._handle_ha_event(
            _make_event("climate.thermostat", "off", "heat",
                        new_attrs={"friendly_name": "Thermostat", "current_temperature": 20, "temperature": 22})
        )
        adapter.handle_message.assert_called_once()

        # Verify the actual MessageEvent text content
        msg_event = adapter.handle_message.call_args[0][0]
        assert "Thermostat" in msg_event.text
        assert "heat" in msg_event.text
        assert msg_event.source.platform == HA
        assert msg_event.source.platform.value == "homeassistant"
        assert msg_event.source.chat_id == "ha_events"


# ---------------------------------------------------------------------------
# Cooldown behavior
# ---------------------------------------------------------------------------


class TestCooldown:

    @pytest.mark.asyncio
    async def test_cooldown_expires(self):
        adapter = _make_adapter(watch_all=True, cooldown_seconds=1)

        event = _make_event("sensor.temp", "20", "21",
                            new_attrs={"friendly_name": "Temp"})
        await adapter._handle_ha_event(event)
        assert adapter.handle_message.call_count == 1

        # Simulate time passing beyond cooldown
        adapter._last_event_time["sensor.temp"] = time.time() - 2

        event2 = _make_event("sensor.temp", "21", "22",
                             new_attrs={"friendly_name": "Temp"})
        await adapter._handle_ha_event(event2)
        assert adapter.handle_message.call_count == 2


# ---------------------------------------------------------------------------
# Env enablement (replaces core's HASS_TOKEN/HASS_URL env row)
# ---------------------------------------------------------------------------


class TestEnvEnablement:
    def test_hass_url_and_home_channel_seed_extra(self, monkeypatch):
        from homeassistant_plugin import _env_enablement

        monkeypatch.setenv("HASS_URL", "http://10.0.0.5:8123")
        monkeypatch.setenv("HASS_HOME_CHANNEL", "mobile_app_phone")
        monkeypatch.delenv("HASS_HOME_CHANNEL_NAME", raising=False)
        assert _env_enablement() == {
            "url": "http://10.0.0.5:8123",
            "home_channel": {"chat_id": "mobile_app_phone", "name": "Home"},
        }

    def test_nothing_seeded_without_env(self, monkeypatch):
        from homeassistant_plugin import _env_enablement

        monkeypatch.delenv("HASS_URL", raising=False)
        monkeypatch.delenv("HASS_HOME_CHANNEL", raising=False)
        assert _env_enablement() == {}

    def test_is_connected_tracks_hass_token(self, monkeypatch):
        from homeassistant_plugin import _platform_kwargs

        is_connected = _platform_kwargs()["is_connected"]
        monkeypatch.setenv("HASS_TOKEN", "env-token")
        assert is_connected(PlatformConfig(enabled=True)) is True
        monkeypatch.setenv("HASS_TOKEN", "   ")
        assert is_connected(PlatformConfig(enabled=True)) is False

    def test_adapter_uses_seeded_url_and_env_token(self, monkeypatch):
        from homeassistant_plugin import _env_enablement

        monkeypatch.setenv("HASS_TOKEN", "env-token")
        monkeypatch.setenv("HASS_URL", "http://10.0.0.5:8123/")
        adapter = HomeAssistantAdapter(PlatformConfig(enabled=True, extra=_env_enablement()))
        assert adapter._hass_token == "env-token"
        assert adapter._hass_url == "http://10.0.0.5:8123"


# ---------------------------------------------------------------------------
# send() via REST API
# ---------------------------------------------------------------------------


class TestSendViaRestApi:
    """send() uses REST API (not WebSocket) to avoid race conditions."""

    @staticmethod
    def _mock_aiohttp_session(response_status=200, response_text="OK"):
        """Build a mock aiohttp session + response for async-with patterns.

        aiohttp.ClientSession() is a sync constructor whose return value
        is used as ``async with session:``.  ``session.post(...)`` returns a
        context-manager (not a coroutine), so both layers use MagicMock for
        the call and AsyncMock only for ``__aenter__`` / ``__aexit__``.
        """
        mock_response = MagicMock()
        mock_response.status = response_status
        mock_response.text = AsyncMock(return_value=response_text)
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        return mock_session

    @pytest.mark.asyncio
    async def test_send_success(self):
        adapter = _make_adapter()
        mock_session = self._mock_aiohttp_session(200)

        with patch("homeassistant_plugin.adapter.aiohttp") as mock_aiohttp:
            mock_aiohttp.ClientSession = MagicMock(return_value=mock_session)
            mock_aiohttp.ClientTimeout = lambda total: total

            result = await adapter.send("ha_events", "Test notification")

        assert result.success is True
        # Verify the REST API was called with correct payload
        call_args = mock_session.post.call_args
        assert "/api/services/persistent_notification/create" in call_args[0][0]
        assert call_args[1]["json"]["title"] == NOTIFICATION_TITLE == "Hermes Agent"
        assert call_args[1]["json"]["message"] == "Test notification"
        assert call_args[1]["json"]["notification_id"] == "hermes_agent"
        assert "Bearer tok" in call_args[1]["headers"]["Authorization"]

    @pytest.mark.asyncio
    async def test_send_uses_configured_notification_id(self):
        adapter = _make_adapter(notification_id="hermes_agent_homeassistant_events")
        mock_session = self._mock_aiohttp_session(200)

        with patch("homeassistant_plugin.adapter.aiohttp") as mock_aiohttp:
            mock_aiohttp.ClientSession = MagicMock(return_value=mock_session)
            mock_aiohttp.ClientTimeout = lambda total: total

            result = await adapter.send("ha_events", "Test notification")

        assert result.success is True
        assert mock_session.post.call_args[1]["json"]["notification_id"] == "hermes_agent_homeassistant_events"

    @pytest.mark.asyncio
    async def test_send_omits_empty_notification_id(self):
        adapter = _make_adapter(notification_id="")
        mock_session = self._mock_aiohttp_session(200)

        with patch("homeassistant_plugin.adapter.aiohttp") as mock_aiohttp:
            mock_aiohttp.ClientSession = MagicMock(return_value=mock_session)
            mock_aiohttp.ClientTimeout = lambda total: total

            result = await adapter.send("ha_events", "Test notification")

        assert result.success is True
        assert "notification_id" not in mock_session.post.call_args[1]["json"]


# ---------------------------------------------------------------------------
# Toolset integration
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# WebSocket URL construction
# ---------------------------------------------------------------------------




class TestLocalNetworkConnectHint:
    def test_ehostunreach_outside_launchd_is_a_plain_unreachable_host(self, monkeypatch):
        """The same errno from a Terminal-run gateway (or another OS) must not be blamed on macOS."""
        from homeassistant_plugin.adapter import _connect_error_detail

        monkeypatch.delenv("HERMES_SUPERVISED_CHILD", raising=False)
        monkeypatch.delenv("XPC_SERVICE_NAME", raising=False)
        err = OSError(errno.EHOSTUNREACH, "No route to host")
        assert _connect_error_detail(err) == str(err)
        assert _connect_error_detail(RuntimeError("auth failed")) == "auth failed"

    @pytest.mark.skipif(sys.platform != "darwin", reason="macOS Local Network Privacy only")
    def test_ehostunreach_under_launchd_names_the_remedy(self, monkeypatch):
        """Only the launchd-supervised gateway can be denied by Local Network Privacy (#71206)."""
        from homeassistant_plugin.adapter import _connect_error_detail

        monkeypatch.setenv("HERMES_SUPERVISED_CHILD", "1")
        err = OSError(errno.EHOSTUNREACH, "No route to host")
        detail = _connect_error_detail(err)
        assert detail.startswith(str(err))
        assert len(detail) > len(str(err))  # a remedy hint is appended


# ---------------------------------------------------------------------------
# Multiplex profiles: the URL resolves through the same scope as the token
# ---------------------------------------------------------------------------


class TestMultiplexEndpointScope:
    """Under ``gateway.multiplex_profiles`` os.environ holds the DEFAULT profile; a secondary profile's
    HASS_TOKEN must never be posted to the default profile's HASS_URL.

    Moved from hermes-agent core ``tests/gateway/test_multiplex_endpoint_identity_scope.py``."""

    DEFAULT = {"HASS_URL": "http://default-ha.example:8123", "HASS_TOKEN": "default-ha-token"}
    SECONDARY = {"HASS_URL": "http://bot2-ha.example:8123", "HASS_TOKEN": "bot2-ha-token"}

    @pytest.fixture
    def secondary_scope(self, monkeypatch):
        from agent import secret_scope as ss

        for key, value in self.DEFAULT.items():
            monkeypatch.setenv(key, value)
        ss.set_multiplex_active(True)
        token = ss.set_secret_scope(self.SECONDARY)
        yield
        ss.reset_secret_scope(token)
        ss.set_multiplex_active(False)

    def test_adapter_url_follows_the_scoped_token(self, secondary_scope):
        adapter = HomeAssistantAdapter(PlatformConfig(enabled=True))
        assert (adapter._hass_url, adapter._hass_token) == (
            self.SECONDARY["HASS_URL"], self.SECONDARY["HASS_TOKEN"])

    @pytest.mark.asyncio
    async def test_standalone_send_targets_scoped_url(self, secondary_scope, monkeypatch):
        import aiohttp
        from homeassistant_plugin import adapter as ha

        seen = {}

        class _Resp:
            status = 200
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def text(self): return ""

        class _Sess:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            def post(self, url, **kw):
                seen["url"], seen["auth"] = url, kw["headers"]["Authorization"]
                return _Resp()

        monkeypatch.setattr(aiohttp, "ClientSession", _Sess)
        result = await ha._standalone_send(PlatformConfig(enabled=True), "x", "hi")
        assert result["success"] is True
        assert seen["url"].startswith(self.SECONDARY["HASS_URL"] + "/")
        assert self.SECONDARY["HASS_TOKEN"] in seen["auth"]


# ---------------------------------------------------------------------------
# A URL without a scheme is a config error, not a connection failure
# ---------------------------------------------------------------------------


class TestUrlWithoutScheme:
    @pytest.mark.asyncio
    async def test_connect_fails_once_with_a_named_cause(self, monkeypatch, tmp_path):
        """``ha.example.com`` used to become the WS URL ``ha.example.com/api/websocket`` and retry forever."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        adapter = _make_adapter(url="ha.example.com")
        adapter._ws_connect = AsyncMock()

        assert await adapter.connect() is False
        adapter._ws_connect.assert_not_awaited()
        assert adapter.has_fatal_error and not adapter.fatal_error_retryable
        assert "no http:// or https:// scheme" in adapter.fatal_error_message
        assert "HASS_URL" in adapter.fatal_error_message

    @pytest.mark.asyncio
    async def test_standalone_send_names_the_cause(self):
        import homeassistant_plugin.adapter as ha

        result = await ha._standalone_send(
            PlatformConfig(enabled=True, token="tok", extra={"url": "ha.example.com"}), "x", "hi")
        assert "no http:// or https:// scheme" in str(result)
