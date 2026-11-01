"""Tests for the Home Assistant gateway adapter.

Tests real logic: state change formatting, event filtering pipeline,
cooldown behavior, config integration, and adapter initialization.
"""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import (
    GatewayConfig,
    Platform,
    PlatformConfig,
)
from homeassistant_plugin.adapter import (
    HomeAssistantAdapter,
    check_ha_requirements,
    validate_ha_config,
)
from gateway.platforms.base import SendResult
from gateway.session import SessionSource


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


    def test_binary_sensor_on(self):
        msg = self.fmt(
            "binary_sensor.motion",
            {"state": "off"},
            {"state": "on", "attributes": {"friendly_name": "Hallway Motion"}},
        )
        assert "triggered" in msg
        assert "Hallway Motion" in msg


    def test_light_turned_on(self):
        msg = self.fmt(
            "light.bedroom",
            {"state": "off"},
            {"state": "on", "attributes": {"friendly_name": "Bedroom Light"}},
        )
        assert "turned on" in msg

    def test_switch_turned_off(self):
        msg = self.fmt(
            "switch.heater",
            {"state": "on"},
            {"state": "off", "attributes": {"friendly_name": "Heater"}},
        )
        assert "turned off" in msg


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
        assert msg_event.source.platform == Platform("homeassistant")
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
# Config integration (env overrides, round-trip)
# ---------------------------------------------------------------------------


class TestConfigIntegration:
    def test_env_override_creates_ha_platform(self, monkeypatch):
        monkeypatch.setenv("HASS_TOKEN", "env-token")
        monkeypatch.setenv("HASS_URL", "http://10.0.0.5:8123")
        # Clear other platform tokens
        for v in ["TELEGRAM_BOT_TOKEN", "DISCORD_BOT_TOKEN", "SLACK_BOT_TOKEN"]:
            monkeypatch.delenv(v, raising=False)

        from gateway.config import load_gateway_config
        config = load_gateway_config()

        assert Platform("homeassistant") in config.platforms
        ha = config.platforms[Platform("homeassistant")]
        assert ha.enabled is True
        assert ha.token == "env-token"
        assert ha.extra["url"] == "http://10.0.0.5:8123"


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
        assert call_args[1]["json"]["title"] == "Hermes Agent"
        assert call_args[1]["json"]["message"] == "Test notification"
        assert "Bearer tok" in call_args[1]["headers"]["Authorization"]


# ---------------------------------------------------------------------------
# Toolset integration
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# WebSocket URL construction
# ---------------------------------------------------------------------------


class TestWsUrlConstruction:
    def test_http_to_ws(self):
        config = PlatformConfig(enabled=True, token="t", extra={"url": "http://ha:8123"})
        adapter = HomeAssistantAdapter(config)
        ws_url = adapter._hass_url.replace("http://", "ws://").replace("https://", "wss://")
        assert ws_url == "ws://ha:8123"


# ---------------------------------------------------------------------------
# Deliver target config parsing (issue #35060)
# ---------------------------------------------------------------------------


def _make_deliver_adapter(**extra) -> HomeAssistantAdapter:
    """Helper: instantiate adapter with no network, for deliver tests."""
    config = PlatformConfig(enabled=True, token="tok", extra=extra)
    return HomeAssistantAdapter(config)


def _fake_home_channel(chat_id: str):
    """Minimal stand-in for a GatewayConfig home-channel entry."""
    home = MagicMock()
    home.chat_id = chat_id
    return home


class TestDeliverTargetParsing:
    """Configurable deliver-target parsing for HA watch config.

    watch_entities and watch_domains entries accept either a plain string
    (unchanged behavior) or a single-entry dict {entity_or_domain: {"deliver": "<platform>"}}.
    Also reads optional top-level "deliver" / "default_deliver" as the default.
    Precedence: per-entry deliver > top-level deliver > "homeassistant".
    """

    # -- plain-string entries -------------------------------------------------

    def test_plain_string_entities_default_to_homeassistant(self):
        """Plain-string watch_entities entries resolve to 'homeassistant'."""
        adapter = _make_deliver_adapter(
            watch_entities=["sensor.temp", "light.bedroom"],
        )
        assert adapter.resolve_deliver_target("sensor.temp") == "homeassistant"
        assert adapter.resolve_deliver_target("light.bedroom") == "homeassistant"

    def test_plain_string_domains_default_to_homeassistant(self):
        """Plain-string watch_domains entries resolve to 'homeassistant'."""
        adapter = _make_deliver_adapter(
            watch_domains=["climate", "binary_sensor"],
        )
        assert adapter.resolve_deliver_target("climate.thermostat") == "homeassistant"
        assert adapter.resolve_deliver_target("binary_sensor.motion") == "homeassistant"

    # -- dict-form entries ----------------------------------------------------

    def test_dict_form_entity_resolves_per_entity(self):
        """Dict-form watch_entities entry sets deliver target per entity."""
        adapter = _make_deliver_adapter(
            watch_entities=[{"sensor.temp": {"deliver": "whatsapp"}}],
            watch_domains=["climate"],
        )
        assert adapter.resolve_deliver_target("sensor.temp") == "whatsapp"
        # Other entities still default to "homeassistant"
        assert adapter.resolve_deliver_target("climate.thermostat") == "homeassistant"

    def test_dict_form_domain_resolves_by_domain(self):
        """Dict-form watch_domains entry sets deliver target for any entity in that domain."""
        adapter = _make_deliver_adapter(
            watch_domains=[{"climate": {"deliver": "telegram"}}],
        )
        assert adapter.resolve_deliver_target("climate.thermostat") == "telegram"
        assert adapter.resolve_deliver_target("climate.ac") == "telegram"
        # Unknown domain defaults
        assert adapter.resolve_deliver_target("light.bedroom") == "homeassistant"

    # -- top-level deliver / default_deliver ----------------------------------

    def test_top_level_deliver_sets_default(self):
        """Top-level 'deliver' key sets the default deliver target for all watch items."""
        adapter = _make_deliver_adapter(
            deliver="telegram",
            watch_entities=["sensor.temp", "light.bedroom"],
        )
        assert adapter.resolve_deliver_target("sensor.temp") == "telegram"
        assert adapter.resolve_deliver_target("light.bedroom") == "telegram"

    def test_default_deliver_alias_sets_default(self):
        """Top-level 'default_deliver' alias sets the default deliver target."""
        adapter = _make_deliver_adapter(
            default_deliver="slack",
            watch_entities=["sensor.temp"],
        )
        assert adapter.resolve_deliver_target("sensor.temp") == "slack"

    # -- precedence -----------------------------------------------------------

    def test_per_entry_overrides_top_level_deliver(self):
        """Per-entry deliver target overrides the top-level default."""
        adapter = _make_deliver_adapter(
            deliver="slack",
            watch_entities=[{"sensor.temp": {"deliver": "whatsapp"}}],
            watch_domains=["climate"],
        )
        assert adapter.resolve_deliver_target("sensor.temp") == "whatsapp"
        assert adapter.resolve_deliver_target("climate.thermostat") == "slack"

    def test_top_level_deliver_overrides_implicit_default(self):
        """Top-level deliver overrides the implicit 'homeassistant' default."""
        adapter = _make_deliver_adapter(
            deliver="discord",
            watch_entities=["sensor.temp"],
            watch_domains=["climate"],
        )
        assert adapter.resolve_deliver_target("sensor.temp") == "discord"
        assert adapter.resolve_deliver_target("climate.thermostat") == "discord"

    # -- malformed entries ----------------------------------------------------

    def test_malformed_entry_dict_with_multiple_keys_skipped(self, caplog):
        """Dict with >1 key is skipped with a warning."""
        adapter = _make_deliver_adapter(
            watch_entities=[{"sensor.temp": {"deliver": "whatsapp"}, "extra": "bad"}],
            watch_domains=["climate"],
        )
        assert adapter.resolve_deliver_target("sensor.temp") == "homeassistant"
        assert adapter.resolve_deliver_target("climate.thermostat") == "homeassistant"
        assert "Malformed" in caplog.text or "skipping" in caplog.text

    def test_malformed_entry_deliver_not_str_skipped(self, caplog):
        """Dict-form entry with non-string deliver value is skipped with a warning."""
        adapter = _make_deliver_adapter(
            watch_entities=[{"sensor.temp": {"deliver": 123}}],
            watch_domains=["climate"],
        )
        assert adapter.resolve_deliver_target("sensor.temp") == "homeassistant"
        assert adapter.resolve_deliver_target("climate.thermostat") == "homeassistant"
        assert "Malformed" in caplog.text or "skipping" in caplog.text

    def test_malformed_entry_non_str_non_dict_skipped(self, caplog):
        """Entry that is neither str nor dict is skipped with a warning."""
        adapter = _make_deliver_adapter(
            watch_entities=[42, "sensor.valid"],
            watch_domains=["climate"],
        )
        assert adapter.resolve_deliver_target("sensor.valid") == "homeassistant"
        assert adapter.resolve_deliver_target("climate.thermostat") == "homeassistant"
        assert "Malformed" in caplog.text or "skipping" in caplog.text

    @pytest.mark.parametrize("bad_entry", [
        {"climate": {"deliver": "whatsapp"}, "extra": "bad"},  # dict with >1 key
        {"climate": {"deliver": 123}},                          # non-string deliver
        42,                                                     # neither str nor dict
    ])
    def test_malformed_domain_entries_skipped(self, caplog, bad_entry):
        """watch_domains malformed entries are skipped with a warning, same as watch_entities."""
        adapter = _make_deliver_adapter(watch_domains=[bad_entry, "cover"])
        assert adapter.resolve_deliver_target("climate.thermostat") == "homeassistant"
        assert adapter.resolve_deliver_target("cover.garage") == "homeassistant"
        assert "Malformed" in caplog.text or "skipping" in caplog.text

    def test_malformed_entries_dont_break_startup(self):
        """Multiple malformed entries don't raise at startup."""
        adapter = _make_deliver_adapter(
            watch_entities=[
                {"sensor.a": {"deliver": "whatsapp", "extra": "bad"}},
                {"sensor.b": {"deliver": 123}},
                42,
                "sensor.valid",
            ],
            watch_domains=[{"climate.ac": {"deliver": "telegram"}}],
        )
        # The adapter should be constructable and queryable
        assert adapter.resolve_deliver_target("sensor.valid") == "homeassistant"

    # -- resolve_deliver_target interface -------------------------------------

    def test_resolve_deliver_target_unknown_entity_uses_default(self):
        """Entity not in any watch list still resolves to the default deliver target."""
        adapter = _make_deliver_adapter(
            deliver="telegram",
            watch_entities=["sensor.temp"],
            watch_domains=["climate"],
        )
        # Unknown entity - not watched, but resolve_deliver_target still returns default
        assert adapter.resolve_deliver_target("light.unknown") == "telegram"

    # -- M2: null-safe parsing and dotless id --------------------------------

    def test_watch_config_none_does_not_crash(self):
        """Extra with watch_entities=None and watch_domains=None does not crash at construction."""
        adapter = _make_deliver_adapter(watch_entities=None, watch_domains=None)
        assert adapter._watch_entities == set()
        assert adapter._watch_domains == set()

    def test_resolve_deliver_target_dotless_id_falls_through(self):
        """resolve_deliver_target with a dotless id falls through to domain lookup / default."""
        adapter = _make_deliver_adapter()
        # "climate" has no dot, no overrides exist — falls through to default
        assert adapter.resolve_deliver_target("climate") == "homeassistant"

    def test_resolve_deliver_target_entity_overrides_domain(self):
        """When both an entity key and its domain key have overrides, the entity (most specific) wins."""
        adapter = _make_deliver_adapter(
            watch_entities=[{"sensor.camera": {"deliver": "signal"}}],
            watch_domains=[{"sensor": {"deliver": "whatsapp"}}],
        )
        # Exact entity match beats the domain match
        assert adapter.resolve_deliver_target("sensor.camera") == "signal"
        # Sibling entities on the watched domain still use the domain override
        assert adapter.resolve_deliver_target("sensor.motion") == "whatsapp"


# ---------------------------------------------------------------------------
# Cross-platform delivery routing in send() (issue #35060)
# ---------------------------------------------------------------------------


class TestDeliverRouting:
    """send() routes cross-platform when chat_id uses the ha_events: prefix."""

    @staticmethod
    def _stub_adapter(send_result=None):
        """Build a minimal stub adapter with async send()."""
        stub = MagicMock()
        if send_result is not None:
            stub.send = AsyncMock(return_value=send_result)
        else:
            stub.send = AsyncMock(return_value=SendResult(success=True))
        return stub

    @staticmethod
    def _make_ha_adapter(**extra) -> HomeAssistantAdapter:
        config = PlatformConfig(enabled=True, token="tok", extra=extra)
        adapter = HomeAssistantAdapter(config)
        return adapter

    def _stub_runner(self, target_platform, target_adapter=None, home_chat_id=None):
        """Build a stub gateway runner that returns a target adapter."""
        runner = MagicMock()
        runner.adapters = {}
        if target_adapter is not None:
            runner.adapters[target_platform] = target_adapter
        runner.config = MagicMock()

        if home_chat_id is not None:

            class _FakeHomeChannel:
                chat_id = home_chat_id

            runner.config.get_home_channel = MagicMock(return_value=_FakeHomeChannel())
        else:
            runner.config.get_home_channel = MagicMock(return_value=None)
        runner._profile_adapters = {}

        def _authorization_adapter(platform, profile=None):
            """Mirror GatewayAuthorizationMixin: own profile's map only, fail closed."""
            if profile and profile != "default":
                return (runner._profile_adapters or {}).get(profile, {}).get(platform)
            return runner.adapters.get(platform)

        runner._authorization_adapter = MagicMock(side_effect=_authorization_adapter)
        return runner

    @pytest.mark.asyncio
    async def test_send_routes_to_target_adapter_when_chat_id_has_prefix(self):
        """send('ha_events:telegram', ...) routes to the telegram adapter."""
        adapter = self._make_ha_adapter()
        target_adapter = self._stub_adapter()
        runner = self._stub_runner(
            Platform.TELEGRAM, target_adapter=target_adapter, home_chat_id="chat_42"
        )
        adapter.gateway_runner = runner

        with patch("homeassistant_plugin.adapter.aiohttp") as mock_aiohttp:
            mock_aiohttp.ClientSession = MagicMock()
            mock_aiohttp.ClientTimeout = lambda total: total

            result = await adapter.send("ha_events:telegram", "hello from HA")

        assert result.success is True
        # Target adapter should have been called with the home channel's chat_id
        target_adapter.send.assert_called_once_with("chat_42", "hello from HA", metadata=None)

    @pytest.mark.asyncio
    async def test_send_ha_events_no_prefix_stays_local(self):
        """send('ha_events', ...) without colon suffix stays in HA notification path."""
        adapter = self._make_ha_adapter()
        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.text = AsyncMock(return_value="OK")
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)
        mock_session.post = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("homeassistant_plugin.adapter.aiohttp") as mock_aiohttp:
            mock_aiohttp.ClientSession = MagicMock(return_value=mock_session)
            mock_aiohttp.ClientTimeout = lambda total: total

            result = await adapter.send("ha_events", "direct notification")

        assert result.success is True
        # Verify HA REST API was called
        call_args = mock_session.post.call_args
        assert "/api/services/persistent_notification/create" in call_args[0][0]

    @pytest.mark.asyncio
    async def test_send_falls_back_to_ha_when_no_gateway_runner(self):
        """send('ha_events:telegram', ...) falls back to HA notification when gateway_runner is None."""
        adapter = self._make_ha_adapter()
        # gateway_runner is None by default
        assert adapter.gateway_runner is None

        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.text = AsyncMock(return_value="OK")
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)
        mock_session.post = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("homeassistant_plugin.adapter.aiohttp") as mock_aiohttp:
            mock_aiohttp.ClientSession = MagicMock(return_value=mock_session)
            mock_aiohttp.ClientTimeout = lambda total: total

            result = await adapter.send("ha_events:telegram", "fallback content")

        assert result.success is True
        # HA notification path should have been used
        call_args = mock_session.post.call_args
        assert "/api/services/persistent_notification/create" in call_args[0][0]

    @pytest.mark.asyncio
    async def test_send_falls_back_to_ha_when_target_adapter_missing(self):
        """send('ha_events:telegram', ...) falls back when target adapter is not connected."""
        adapter = self._make_ha_adapter()
        runner = self._stub_runner(Platform.TELEGRAM, target_adapter=None)
        adapter.gateway_runner = runner

        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.text = AsyncMock(return_value="OK")
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)
        mock_session.post = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("homeassistant_plugin.adapter.aiohttp") as mock_aiohttp:
            mock_aiohttp.ClientSession = MagicMock(return_value=mock_session)
            mock_aiohttp.ClientTimeout = lambda total: total

            result = await adapter.send("ha_events:telegram", "fallback content")

        assert result.success is True
        call_args = mock_session.post.call_args
        assert "/api/services/persistent_notification/create" in call_args[0][0]

    @pytest.mark.asyncio
    async def test_send_falls_back_to_ha_when_no_home_channel(self):
        """send('ha_events:telegram', ...) falls back when home channel is missing."""
        adapter = self._make_ha_adapter()
        target_adapter = self._stub_adapter()
        runner = self._stub_runner(
            Platform.TELEGRAM, target_adapter=target_adapter, home_chat_id=None
        )
        adapter.gateway_runner = runner

        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.text = AsyncMock(return_value="OK")
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)
        mock_session.post = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("homeassistant_plugin.adapter.aiohttp") as mock_aiohttp:
            mock_aiohttp.ClientSession = MagicMock(return_value=mock_session)
            mock_aiohttp.ClientTimeout = lambda total: total

            result = await adapter.send("ha_events:telegram", "fallback content")

        assert result.success is True
        call_args = mock_session.post.call_args
        assert "/api/services/persistent_notification/create" in call_args[0][0]

    @pytest.mark.asyncio
    async def test_send_falls_back_to_ha_when_unknown_platform(self):
        """send('ha_events:unknown_platform', ...) falls back to HA notification."""
        adapter = self._make_ha_adapter()
        runner = self._stub_runner(None)  # No target registered at all
        adapter.gateway_runner = runner

        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.text = AsyncMock(return_value="OK")
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)
        mock_session.post = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("homeassistant_plugin.adapter.aiohttp") as mock_aiohttp:
            mock_aiohttp.ClientSession = MagicMock(return_value=mock_session)
            mock_aiohttp.ClientTimeout = lambda total: total

            result = await adapter.send("ha_events:unknown_platform", "fallback content")

        assert result.success is True
        call_args = mock_session.post.call_args
        assert "/api/services/persistent_notification/create" in call_args[0][0]

    @pytest.mark.asyncio
    async def test_handle_ha_event_sets_tagged_chat_id_for_cross_platform(self):
        """_handle_ha_event sets chat_id to ha_events:telegram when deliver target is telegram."""
        adapter = self._make_ha_adapter(
            watch_entities=[{"sensor.temp": {"deliver": "telegram"}}],
            cooldown_seconds=0,
        )
        adapter.handle_message = AsyncMock()
        await adapter._handle_ha_event(
            _make_event("sensor.temp", "22", "25",
                        new_attrs={"friendly_name": "Temp Sensor", "unit_of_measurement": "C"})
        )
        adapter.handle_message.assert_called_once()
        msg_event = adapter.handle_message.call_args[0][0]
        assert msg_event.source.chat_id == "ha_events:telegram"

    @pytest.mark.asyncio
    async def test_handle_ha_event_leaves_default_chat_id_for_ha(self):
        """_handle_ha_event keeps default chat_id when deliver target is homeassistant."""
        adapter = self._make_ha_adapter(
            watch_entities=["sensor.temp"],
            cooldown_seconds=0,
        )
        adapter.handle_message = AsyncMock()
        await adapter._handle_ha_event(
            _make_event("sensor.temp", "22", "25",
                        new_attrs={"friendly_name": "Temp Sensor", "unit_of_measurement": "C"})
        )
        adapter.handle_message.assert_called_once()
        msg_event = adapter.handle_message.call_args[0][0]
        assert msg_event.source.chat_id == "ha_events"

    @pytest.mark.asyncio
    async def test_send_accepts_capitalized_platform_name(self):
        """send('ha_events:Telegram', ...) normalizes case and routes correctly."""
        adapter = self._make_ha_adapter()
        target_adapter = self._stub_adapter()
        runner = self._stub_runner(
            Platform.TELEGRAM, target_adapter=target_adapter, home_chat_id="chat_42"
        )
        adapter.gateway_runner = runner

        result = await adapter.send("ha_events:Telegram", "capitalized alert")

        assert result.success is True
        target_adapter.send.assert_called_once_with("chat_42", "capitalized alert", metadata=None)

    @pytest.mark.asyncio
    async def test_send_falls_back_to_ha_when_target_adapter_raises(self):
        """A raise from the target adapter falls back to HA notification."""
        adapter = self._make_ha_adapter()
        target_adapter = self._stub_adapter()
        target_adapter.send = AsyncMock(side_effect=RuntimeError("network exploded"))
        runner = self._stub_runner(
            Platform.TELEGRAM, target_adapter=target_adapter, home_chat_id="chat_42"
        )
        adapter.gateway_runner = runner

        with patch(
            "homeassistant_plugin.adapter.HomeAssistantAdapter._send_ha_notification",
            new_callable=AsyncMock,
            return_value=SendResult(success=True),
        ) as mock_ha_fallback:
            result = await adapter.send("ha_events:telegram", "fallback alert")

        assert result.success is True
        mock_ha_fallback.assert_awaited_once_with("fallback alert")

    @pytest.mark.asyncio
    async def test_send_uses_own_profile_adapter_and_home_channel(self):
        """A secondary profile delivers through ITS OWN adapter and ITS OWN home channel."""
        adapter = self._make_ha_adapter()
        adapter._owner_profile = "profile_1"
        target_adapter = self._stub_adapter()
        runner = self._stub_runner(Platform.TELEGRAM, target_adapter=None)
        runner._profile_adapters = {"profile_1": {Platform.TELEGRAM: target_adapter}}
        adapter.gateway_runner = runner
        adapter._target_home_channel = MagicMock(return_value=_fake_home_channel("chat_42"))

        result = await adapter.send("ha_events:telegram", "profile alert")

        assert result.success is True
        target_adapter.send.assert_called_once_with("chat_42", "profile alert", metadata=None)
        runner._authorization_adapter.assert_called_once_with(Platform.TELEGRAM, "profile_1")
        adapter._target_home_channel.assert_called_once_with(Platform.TELEGRAM, "profile_1")

    @pytest.mark.asyncio
    async def test_send_does_not_borrow_another_profiles_adapter(self):
        """Regression (#65939): a routed alert never egresses through another profile's bot.

        The target platform connected only on a DIFFERENT profile: this adapter's own
        profile has no adapter for it, so the alert degrades to the HA notification
        instead of being posted as the wrong profile's identity.
        """
        adapter = self._make_ha_adapter()
        adapter._owner_profile = "profile_1"
        foreign_adapter = self._stub_adapter()
        runner = self._stub_runner(Platform.TELEGRAM, target_adapter=None, home_chat_id="chat_42")
        runner._profile_adapters = {"profile_2": {Platform.TELEGRAM: foreign_adapter}}
        adapter.gateway_runner = runner
        # A home channel exists for this profile, so borrowing profile_2's adapter WOULD
        # deliver: only the fail-closed resolution can keep the alert off the wrong bot.
        adapter._target_home_channel = MagicMock(return_value=_fake_home_channel("chat_42"))

        with patch(
            "homeassistant_plugin.adapter.HomeAssistantAdapter._send_ha_notification",
            new_callable=AsyncMock,
            return_value=SendResult(success=True),
        ) as mock_ha_fallback:
            result = await adapter.send("ha_events:telegram", "cross-profile alert")

        assert result.success is True
        foreign_adapter.send.assert_not_called()
        adapter._target_home_channel.assert_not_called()
        mock_ha_fallback.assert_awaited_once_with("cross-profile alert")

    def test_target_home_channel_reads_own_profile_config(self):
        """A secondary profile's home channel comes from ITS config.yaml, not the default's."""
        adapter = self._make_ha_adapter()
        runner = self._stub_runner(Platform.TELEGRAM, home_chat_id="default_chat")
        adapter.gateway_runner = runner
        profile_cfg = MagicMock()
        profile_cfg.get_home_channel = MagicMock(
            return_value=_fake_home_channel("profile_chat"))
        with patch("gateway.config.load_gateway_config", return_value=profile_cfg), patch(
            "gateway.run._profile_runtime_scope"
        ) as mock_scope, patch(
            "hermes_cli.profiles.get_profile_dir", return_value="/tmp/profile_1"
        ):
            home = adapter._target_home_channel(Platform.TELEGRAM, "profile_1")

        assert home.chat_id == "profile_chat"
        profile_cfg.get_home_channel.assert_called_once_with(Platform.TELEGRAM)
        runner.config.get_home_channel.assert_not_called()
        mock_scope.assert_called_once_with("/tmp/profile_1")

    def test_target_home_channel_uses_runner_config_without_profile(self):
        """Primary/default: the runner's own config is the source, no scope switch."""
        adapter = self._make_ha_adapter()
        runner = self._stub_runner(Platform.TELEGRAM, home_chat_id="default_chat")
        adapter.gateway_runner = runner

        home = adapter._target_home_channel(Platform.TELEGRAM, None)

        assert home.chat_id == "default_chat"
        runner.config.get_home_channel.assert_called_once_with(Platform.TELEGRAM)



# ---------------------------------------------------------------------------
# Session-integrated delivery (deliver_mode, issue #35060 follow-up)
# ---------------------------------------------------------------------------


def _make_session_mode_adapter(**extra) -> HomeAssistantAdapter:
    """Adapter for session-mode tests: no network, deliver:whatsapp by default."""
    config = PlatformConfig(enabled=True, token="tok", extra=extra)
    return HomeAssistantAdapter(config)


class _Entry:
    """Minimal SessionEntry stand-in for store fakes.

    ``user_id`` mirrors what the gateway stamps on the origin at ingress: pass
    None for a chat whose grouping config is not per-user (the derived key then
    carries no participant), or a participant id when it is.
    """

    def __init__(self, session_key, chat_id, updated_at=1, user_id=None):
        self.session_key = session_key
        self.session_id = session_key
        self.updated_at = updated_at
        self.origin = SessionSource(
            platform=Platform.WHATSAPP, chat_id=chat_id, chat_type="group",
            user_id=user_id, user_name="Tester",
        )


class _Store:
    """Session-store stand-in: list-based reads plus the deterministic
    get_or_create_session used by session-mode targeting."""

    def __init__(self, entries, config=None):
        self._entries = entries
        self.created = []
        self.config = config

    def lookup_by_session_key(self, session_key):
        """Return the persisted entry for an exact session key (None if unknown)."""
        return next((e for e in self._entries if e.session_key == session_key), None)

    def list_sessions(self, active_minutes=None):
        return list(self._entries)

    def get_or_create_session(self, source, force_new=False, touch_activity=True):
        """Return the entry whose key matches *source*, creating one if absent.

        Mirrors the real store: the key comes from the source (deterministic),
        never from recency — and touch_activity is recorded so tests can assert
        the injection path does not reset the user-activity clock.
        """
        from gateway.session import build_session_key as _bk
        from gateway.config import load_gateway_config as _lgc
        try:
            _cfg = _lgc()
            per_user = getattr(_cfg, "group_sessions_per_user", True)
            per_thread = getattr(_cfg, "thread_sessions_per_user", False)
        except Exception:
            per_user, per_thread = True, False
        key = _bk(source, group_sessions_per_user=per_user, thread_sessions_per_user=per_thread)
        for e in self._entries:
            if e.session_key == key:
                if touch_activity:
                    e.touched = True
                return e
        fresh = _Entry(key, source.chat_id, updated_at=0, user_id=getattr(source, "user_id", None))
        fresh.created = True
        self._entries.append(fresh)
        self.created.append(fresh)
        return fresh


class _RecordingAdapter:
    """Target adapter fake: records handle_message / send calls."""

    def __init__(self, accept=True):
        self.accept = accept
        self.handled = []
        self.sent = []

    async def handle_message(self, event):
        self.handled.append(event)
        if self.accept:
            event._gateway_accepted = True

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append((chat_id, content))
        return SendResult(success=True, message_id="bc-1")


class _Runner:
    """GatewayRunner stand-in: profile-scoped adapter + home channel + store."""

    def __init__(self, adapter, store, home_chat_id="G", home_chat_type="group", home_profile=None):
        self._adapter = adapter
        self.session_store = store
        self.home_chat_id = home_chat_id
        self.home_chat_type = home_chat_type
        self.home_profile = home_profile

    def _authorization_adapter(self, platform, profile):
        return self._adapter

    def _profile_name_for_source(self, source, profile=None):
        return self.home_profile or "default"

    class _cfg:
        @staticmethod
        def get_home_channel(platform):
            return None

    config = _cfg()

    def home(self):
        class _h:
            chat_id = self.home_chat_id
            chat_type = self.home_chat_type
            thread_id = None
            user_id = None
        return _h()


def _wire_session_mode(adapter, runner, home):
    adapter.gateway_runner = runner
    adapter._owner_profile = None
    runner.config.get_home_channel = staticmethod(lambda p, _h=home: _h)


def _home_store(adapter_platform="whatsapp", chat_id="G", chat_type="group"):
    """Store pre-populated with the home channel's derived session - the never-mint
    contract requires the target session to already exist (a real message built it)."""
    from gateway.session import SessionSource as _SS, build_session_key as _bk
    from gateway.config import Platform as _P
    src = _SS(platform=getattr(_P, adapter_platform.upper()), chat_id=chat_id, chat_type=chat_type)
    key = _bk(src, group_sessions_per_user=True)
    return _Store([_Entry(key, chat_id)])


def test_deliver_mode_defaults_to_broadcast():
    adapter = _make_session_mode_adapter(watch_domains=["zone"], deliver="whatsapp")
    assert adapter.resolve_deliver_mode("zone.x") == "broadcast"


def test_deliver_mode_top_level_session():
    adapter = _make_session_mode_adapter(deliver="whatsapp", deliver_mode="session")
    assert adapter.resolve_deliver_mode("sensor.any") == "session"


def test_deliver_mode_per_entry_overrides_domain_and_top():
    adapter = _make_session_mode_adapter(
        deliver="whatsapp", deliver_mode="session",
        watch_entities=[{"alarm_control_panel.x": {"deliver_mode": "broadcast"}}],
    )
    assert adapter.resolve_deliver_mode("alarm_control_panel.x") == "broadcast"
    assert adapter.resolve_deliver_mode("sensor.other") == "session"


def test_deliver_mode_invalid_falls_back_to_broadcast():
    adapter = _make_session_mode_adapter(deliver="whatsapp", deliver_mode="banana")
    assert adapter._default_deliver_mode == "broadcast"


@pytest.mark.asyncio
async def test_dm_home_channel_derives_matching_session_key():
    """A DM home channel must produce the same session key a real inbound DM builds.

    Regression for the derived-target mismatch: with no chat_type recorded, the
    adapter used to substitute "group" and mint agent:<ns>:whatsapp:group:<id> —
    a key no real DM message ever enters. The resolved key must byte-match
    build_session_key for a real DM source on the same chat.
    """
    adapter = _make_session_mode_adapter(watch_entities=["sensor.s"], deliver="whatsapp", deliver_mode="session")
    wa = _RecordingAdapter()
    entry = _Entry("agent:main:whatsapp:dm:447700900000", "447700900000")
    runner = _Runner(wa, _Store([entry]), home_chat_id="447700900000", home_chat_type="dm")
    _wire_session_mode(adapter, runner, runner.home())
    adapter.handle_message = AsyncMock()

    await adapter._handle_ha_event(_make_event("sensor.s", "0", "1"))

    assert wa.handled, "injection was attempted"
    injected_key = wa.handled[0].metadata["gateway_session_key"]
    # The same key a real inbound DM message from that chat builds:
    from gateway.session import SessionSource, build_session_key
    real_key = build_session_key(SessionSource(
        platform=Platform.WHATSAPP, chat_id="447700900000", chat_type="dm"))
    assert injected_key == real_key == "agent:main:whatsapp:dm:447700900000"


@pytest.mark.asyncio
async def test_unresolvable_chat_type_fails_closed_to_broadcast():
    """An unresolvable chat shape must degrade to broadcast, never mint a session."""
    adapter = _make_session_mode_adapter(watch_entities=["sensor.s"], deliver="whatsapp", deliver_mode="session")
    wa = _RecordingAdapter()
    entry = _Entry("agent:main:telegram:unknown:weird", "weird")
    runner = _Runner(wa, _Store([entry]), home_chat_id="weird", home_chat_type=None)
    _wire_session_mode(adapter, runner, runner.home())
    adapter.handle_message = AsyncMock()

    await adapter._handle_ha_event(_make_event("sensor.s", "0", "1"))

    assert not wa.handled, "fail-closed: unknown chat shape must not inject"
    assert adapter.handle_message.await_count == 1, "event must reach the source session"


@pytest.mark.asyncio
async def test_whatsapp_group_jid_derives_chat_type_without_recorded_field():
    """Env-seeded homes have chat_type=None; the WhatsApp JID format resolves it."""
    adapter = _make_session_mode_adapter(watch_entities=["sensor.s"], deliver="whatsapp", deliver_mode="session")
    wa = _RecordingAdapter()
    entry = _Entry("agent:main:whatsapp:group:1203@g.us", "1203@g.us")
    runner = _Runner(wa, _Store([entry]), home_chat_id="1203@g.us", home_chat_type=None)
    home = runner.home()
    home.platform = Platform.WHATSAPP  # env-seeded homes carry the platform
    _wire_session_mode(adapter, runner, home)
    adapter.handle_message = AsyncMock()

    await adapter._handle_ha_event(_make_event("sensor.s", "0", "1"))

    assert wa.handled, "JID format resolves the chat type"
    injected_key = wa.handled[0].metadata["gateway_session_key"]
    assert injected_key == "agent:main:whatsapp:group:1203@g.us"


@pytest.mark.asyncio
async def test_lid_home_without_recorded_field_degrades_to_broadcast():
    """A WhatsApp ``@lid`` home is a person alias, not a group: with no recorded
    ``chat_type`` the shape is unresolvable, so the adapter must broadcast rather
    than mint ``...:group:<lid>`` (a key no real message on that chat enters)."""
    adapter = _make_session_mode_adapter(watch_entities=["sensor.s"], deliver="whatsapp", deliver_mode="session")
    wa = _RecordingAdapter()
    # Pre-populate with the key the old @lid->group mutation would derive, so the
    # resolver (not the empty-store lookup) is what the test pins:
    store = _Store([_Entry("agent:main:whatsapp:group:999888777@lid", "999888777@lid")])
    runner = _Runner(wa, store, home_chat_id="999888777@lid", home_chat_type=None)
    home = runner.home()
    home.platform = Platform.WHATSAPP
    _wire_session_mode(adapter, runner, home)
    adapter.handle_message = AsyncMock()

    await adapter._handle_ha_event(_make_event("sensor.s", "0", "1"))

    assert not wa.handled, "@lid without a recorded chat_type is not derivable"
    assert adapter.handle_message.await_count == 1, "event must reach the source session"
    assert store.created == [], "never mint: no session may be created for an unresolvable home"


@pytest.mark.asyncio
async def test_telegram_negative_id_home_without_recorded_field_degrades_to_broadcast():
    """A Telegram negative id may build group, forum, or channel: the id does not
    encode forum-ness, so with no recorded ``chat_type`` the adapter must not
    guess ``group`` - it broadcasts."""
    adapter = _make_session_mode_adapter(watch_entities=["sensor.s"], deliver="telegram", deliver_mode="session")
    tg = _RecordingAdapter()
    # Pre-populate with the key the old negative-id->group guess would derive:
    store = _Store([_Entry("agent:main:telegram:group:-1001234", "-1001234")])
    runner = _Runner(tg, store, home_chat_id="-1001234", home_chat_type=None)
    home = runner.home()
    home.platform = Platform.TELEGRAM
    _wire_session_mode(adapter, runner, home)
    adapter.handle_message = AsyncMock()

    await adapter._handle_ha_event(_make_event("sensor.s", "0", "1"))

    assert not tg.handled, "negative telegram id alone cannot prove ``group``"
    assert store.created == [], "never mint on an ambiguous negative id"


@pytest.mark.asyncio
async def test_per_participant_group_home_broadcasts_instead_of_minting():
    """With group_sessions_per_user, real group sessions carry a participant id in
    the key. The home source has no participant, so its derived key cannot match
    any member's session - lookup must fail and degrade to broadcast, never mint."""
    adapter = _make_session_mode_adapter(watch_entities=["sensor.s"], deliver="whatsapp", deliver_mode="session")
    wa = _RecordingAdapter()
    # The store holds ONLY the member's real key (with participant), not the home's:
    from gateway.session import SessionSource as _SS, build_session_key as _bk
    member_key = _bk(_SS(platform=Platform.WHATSAPP, chat_id="1203@g.us",
                         chat_type="group", user_id="member1", user_name="M"))
    store = _Store([_Entry(member_key, "1203@g.us")])
    runner = _Runner(wa, store, home_chat_id="1203@g.us", home_chat_type="group")
    home = runner.home()
    home.platform = Platform.WHATSAPP
    _wire_session_mode(adapter, runner, home)
    adapter.handle_message = AsyncMock()

    await adapter._handle_ha_event(_make_event("sensor.s", "0", "1"))

    assert not wa.handled, "the derived participant-less key matches no member session"
    assert store.created == [], "never mint: no orphaned session for the home source"
    assert adapter.handle_message.await_count == 1, "event reaches the source session"


@pytest.mark.asyncio
async def test_home_with_pinned_user_id_derives_participant_less_key():
    """Regression: /sethome persists the operator's user_id on the channel. The
    derived key must NOT carry it - the home source must derive the shared chat
    key, or the lookup misses every real session (per-user off) or pins to the
    operator's private session (per-user on)."""
    adapter = _make_session_mode_adapter(watch_entities=["sensor.s"], deliver="whatsapp", deliver_mode="session")
    wa = _RecordingAdapter()
    # The real shared key (per_user=false live config, no participant):
    store = _Store([_Entry("agent:main:whatsapp:group:1203@g.us", "1203@g.us")],
                   config=type("C", (), {"group_sessions_per_user": False})())
    runner = _Runner(wa, store, home_chat_id="1203@g.us", home_chat_type="group")
    home = runner.home()
    home.platform = Platform.WHATSAPP
    home.user_id = "867530900000000@lid"  # what /sethome records
    _wire_session_mode(adapter, runner, home)
    adapter.handle_message = AsyncMock()

    await adapter._handle_ha_event(_make_event("sensor.s", "0", "1"))

    assert wa.handled, "participant-less derived key must match the shared session"
    injected_key = wa.handled[0].metadata["gateway_session_key"]
    assert injected_key == "agent:main:whatsapp:group:1203@g.us", "no participant slot"
    assert store.created == []


def test_derived_key_namespace_follows_profile():
    """build_session_key takes the namespace from the profile argument, not
    source.profile: a named-profile home must derive agent:<profile>:..., matching
    what that profile's sessions really build."""
    from gateway.session import SessionSource as _SS, build_session_key as _bk
    from gateway.config import Platform as _P
    src = _SS(platform=_P.WHATSAPP, chat_id="1203@g.us", chat_type="group", profile="work")
    key = _bk(src, group_sessions_per_user=True, profile="work")
    assert key.startswith("agent:work:whatsapp:group:"), key
    # and the default profile stays agent:main:
    src_d = _SS(platform=_P.WHATSAPP, chat_id="1203@g.us", chat_type="group", profile=None)
    assert _bk(src_d, group_sessions_per_user=True).startswith("agent:main:")


def test_silence_marker_stripped_regardless_of_case_or_whitespace():
    """The gateway's silence matcher canonicalizes (case-fold + whitespace
    collapse); the pre-injection strip must catch the same variants, or an
    entity could echo "no_reply"/"NO_REPLY " and suppress its own alert."""
    import types as _types
    adapter = _make_session_mode_adapter(watch_entities=["sensor.s"], deliver="whatsapp", deliver_mode="session")
    strip = adapter._strip_canonical_marker
    for variant, marker in (
        ("NO_REPLY", "NO_REPLY"), ("no_reply", "NO_REPLY"), ("No_Reply", "NO_REPLY"),
        ("NO  REPLY", "NO REPLY"), ("NO\tREPLY", "NO REPLY"), ("no reply", "NO REPLY"),
    ):
        out = strip(f"sensor says {variant} now", marker)
        assert "[marker stripped]" in out, f"{variant!r} survived: {out!r}"
        assert variant not in out, f"{variant!r} survived: {out!r}"
    # prose mentioning the marker inline is NOT a silence candidate for the
    # matcher (it is not marker-sized), but the strip still removes the bare
    # token - acceptable asymmetry: over-stripping an entity's prose is safe,
    # under-stripping lets the entity silence its own alert.


@pytest.mark.asyncio
async def test_injected_text_framed_as_untrusted_data():
    """The entity-derived payload must be framed as data (untrusted, not an
    instruction) so a malicious entity state cannot pose as an instruction to
    the agent's tool-using turn."""
    adapter = _make_session_mode_adapter(watch_entities=["sensor.s"], deliver="whatsapp", deliver_mode="session")
    wa = _RecordingAdapter()
    store = _home_store()
    runner = _Runner(wa, store, home_chat_id="G", home_chat_type="group")
    _wire_session_mode(adapter, runner, runner.home())
    adapter.handle_message = AsyncMock()

    await adapter._handle_ha_event(_make_event("sensor.s", "0", "1"))

    assert wa.handled, "injection attempted"
    text = wa.handled[0].text
    assert "untrusted" in text.lower(), "payload must carry the untrusted framing"


@pytest.mark.asyncio
async def test_home_with_pinned_user_id_under_per_user_default_broadcasts():
    """Mutation pin (default group_sessions_per_user: true): with the pinned
    user_id passed through, the derived key lands in the pinning user's private
    session - an arbitrary injection target. With the participant-less fix the
    derived key matches NO member key, so the delivery must degrade to broadcast
    even when the store holds the operator's own key."""
    adapter = _make_session_mode_adapter(watch_entities=["sensor.s"], deliver="whatsapp", deliver_mode="session")
    wa = _RecordingAdapter()
    from gateway.session import SessionSource as _SS, build_session_key as _bk
    member_key = _bk(_SS(platform=Platform.WHATSAPP, chat_id="1203@g.us",
                         chat_type="group", user_id="member1", user_name="M"),
                     group_sessions_per_user=True)
    operator_key = _bk(_SS(platform=Platform.WHATSAPP, chat_id="1203@g.us",
                           chat_type="group", user_id="operator", user_name="O"),
                       group_sessions_per_user=True)
    store = _Store([_Entry(member_key, "1203@g.us"), _Entry(operator_key, "1203@g.us")],
                   config=type("C", (), {"group_sessions_per_user": True})())
    runner = _Runner(wa, store, home_chat_id="1203@g.us", home_chat_type="group")
    home = runner.home()
    home.platform = Platform.WHATSAPP
    home.user_id = "operator"
    _wire_session_mode(adapter, runner, home)
    adapter.handle_message = AsyncMock()

    await adapter._handle_ha_event(_make_event("sensor.s", "0", "1"))

    assert not wa.handled, "participant-less home key must match no member key"
    assert store.created == [], "never mint"


@pytest.mark.asyncio
async def test_session_mode_wire_passes_profile_to_derivation():
    """The adapter wiring (profile= argument at the build_session_key call) must
    carry the named profile into the namespace: driving _handle_ha_event with a
    named-profile owner derives agent:<profile>:..., byte-matching what that
    profile's real sessions build."""
    adapter = _make_session_mode_adapter(watch_entities=["sensor.s"], deliver="whatsapp", deliver_mode="session")
    wa = _RecordingAdapter()
    from gateway.session import SessionSource as _SS, build_session_key as _bk
    work_key = _bk(_SS(platform=Platform.WHATSAPP, chat_id="1203@g.us", chat_type="group"),
                   group_sessions_per_user=True, profile="work")
    store = _Store([_Entry(work_key, "1203@g.us")],
                   config=type("C", (), {"group_sessions_per_user": True})())
    runner = _Runner(wa, store, home_chat_id="1203@g.us", home_chat_type="group", home_profile="work")
    home = runner.home()
    home.platform = Platform.WHATSAPP
    _wire_session_mode(adapter, runner, home)
    adapter._owner_profile = "work"  # AFTER the wire (the wire resets it)
    # Profile-scoped home loading goes through the real profile config on disk;
    # the pin here is the DERIVATION wiring, so stub the loader to the home:
    adapter._target_home_channel = lambda platform, profile: home
    adapter.handle_message = AsyncMock()

    await adapter._handle_ha_event(_make_event("sensor.s", "0", "1"))

    assert wa.handled, "profile-correct derivation must match the profile's session"
    injected = wa.handled[0].metadata["gateway_session_key"]
    assert injected == work_key == "agent:work:whatsapp:group:1203@g.us", injected


def test_punctuated_silence_marker_stripped_like_matcher():
    """Mutation pin for the edge-punctuation fold: ".NO_REPLY." survives the
    gateway matcher as intentional silence, so the pre-injection strip must
    remove it exactly like the matcher would - failing the test if the fold is
    removed."""
    from homeassistant_plugin.adapter import HomeAssistantAdapter as _H
    out = _H._strip_canonical_marker("sensor says .NO_REPLY. now", "NO_REPLY")
    assert ".NO_REPLY." not in out, out
    assert "NO_REPLY" not in out.replace("[marker stripped]", ""), out
    # exact form and case/whitespace variants still covered:
    for variant in ("no_reply", "No_Reply", "NO  REPLY", "NO_REPLY"):
        o2 = _H._strip_canonical_marker(f"echo {variant} end", "NO_REPLY")
        assert "NO_REPLY" not in o2.replace("[marker stripped]", ""), (variant, o2)


@pytest.mark.asyncio
async def test_injected_wake_text_carries_exactly_one_source_tag():
    """Mutation pin for the double-tag guard: the upstream templates still carry
    a '[Home Assistant] ' source tag and the envelope adds its own; the guard
    must detect the template tag on the RAW payload (before the untrusted
    framing prefix) and inject exactly one source tag."""
    adapter = _make_session_mode_adapter(watch_entities=["sensor.s"], deliver="whatsapp", deliver_mode="session")
    wa = _RecordingAdapter()
    from gateway.session import SessionSource as _SS, build_session_key as _bk
    shared_key = _bk(_SS(platform=Platform.WHATSAPP, chat_id="1203@g.us", chat_type="group"),
                     group_sessions_per_user=True)
    store = _Store([_Entry(shared_key, "1203@g.us")],
                   config=type("C", (), {"group_sessions_per_user": True})())
    runner = _Runner(wa, store, home_chat_id="1203@g.us", home_chat_type="group")
    home = runner.home()
    home.platform = Platform.WHATSAPP
    _wire_session_mode(adapter, runner, home)
    adapter.handle_message = AsyncMock()

    await adapter._handle_ha_event(_make_event("sensor.s", "0", "1"))

    assert wa.handled, "shared key under per-user=true must match"
    text = wa.handled[0].text
    count = text.count("[Home Assistant]")
    assert count == 1, f"expected exactly 1 source tag, got {count}: {text[:220]}"


def test_injection_budget_slot_spent_only_on_admission(monkeypatch):
    """A budget slot is committed only after admission accepts; a check
    (commit=False) leaves the window untouched."""
    adapter = _make_session_mode_adapter(deliver="whatsapp", deliver_mode="session")
    monkeypatch.setattr(adapter, "_INJECTIONS_PER_CHAT_PER_HOUR", 1)
    key = "whatsapp:G"
    assert adapter._consume_injection_budget(key, commit=False) is True
    # A check alone must not spend the slot:
    assert adapter._consume_injection_budget(key, commit=False) is True
    adapter._commit_injection_budget(key)
    # Now the (patched) cap of 1 is reached:
    assert adapter._consume_injection_budget(key, commit=False) is False


@pytest.mark.asyncio
async def test_cancelled_error_propagates_from_injection():
    """Cancellation must propagate (the broadcast block's contract), not degrade."""
    adapter = _make_session_mode_adapter(watch_entities=["sensor.s"], deliver="whatsapp", deliver_mode="session")
    wa = _RecordingAdapter()
    entry = _Entry("agent:main:whatsapp:group:G", "G")
    runner = _Runner(wa, _Store([entry]))
    _wire_session_mode(adapter, runner, runner.home())
    adapter.handle_message = AsyncMock()

    import asyncio as _aio
    from unittest.mock import patch as _patch
    async def _cancelled(*a, **k):
        raise _aio.CancelledError()
    with _patch("gateway.wake.admit_internal_event", _cancelled):
        with pytest.raises(_aio.CancelledError):
            await adapter._handle_ha_event(_make_event("sensor.s", "0", "1"))
    # and the source-session path must NOT run on a cancelling task:
    adapter.handle_message.assert_not_called()


@pytest.mark.asyncio
async def test_session_mode_omitted_injection_fails_the_assertion():
    """Guard against vacuous tests: broadcast mode must NOT inject.

    Drives _handle_ha_event (the only injection caller) — send() has no injection
    path, so asserting on send() cannot fail even if injection regresses."""
    adapter = _make_session_mode_adapter(watch_entities=["sensor.s"], deliver="whatsapp")
    wa = _RecordingAdapter()
    entry = _Entry("agent:main:whatsapp:group:G:u1", "G")
    runner = _Runner(wa, _Store([entry]))
    _wire_session_mode(adapter, runner, runner.home())
    adapter.handle_message = AsyncMock()

    await adapter._handle_ha_event(_make_event("sensor.s", "0", "1"))

    assert not wa.handled, "broadcast mode must not inject"
    assert adapter.handle_message.await_count == 1, "broadcast must reach the source session"


@pytest.mark.asyncio
async def test_session_mode_with_default_target_logs_and_falls_back(caplog):
    """deliver_mode: session with the default target (homeassistant) has no target
    session to integrate with: log + fall back to the HA notification path.

    Mutation pin: driven through _handle_ha_event (where the warning lives), so
    neutering the logger.warning call fails this test."""
    import logging as _logging

    adapter = _make_session_mode_adapter(watch_entities=["sensor.s"], deliver_mode="session")
    assert adapter.resolve_deliver_target("sensor.s") == "homeassistant"
    monkeypatched = SendResult(success=True, message_id="ha-fb")
    orig = HomeAssistantAdapter._send_ha_notification

    async def fake_ha(self, content):
        return monkeypatched

    HomeAssistantAdapter._send_ha_notification = fake_ha
    try:
        with caplog.at_level(_logging.WARNING, logger="homeassistant_plugin.adapter"):
            await adapter._handle_ha_event(_make_event("sensor.s", "0", "1"))
    finally:
        HomeAssistantAdapter._send_ha_notification = orig
    assert any(
        "has no effect with the default" in rec.message and rec.levelno == _logging.WARNING
        for rec in caplog.records
    ), "default-target warning must fire on the _handle_ha_event path"




def test_deliver_mode_domain_override_precedence():
    """Domain-level override (watch_domains dict form) must apply when no
    per-entity override exists — the middle tier of the precedence chain."""
    adapter = _make_session_mode_adapter(
        deliver="whatsapp",
        watch_domains=[{"zone": {"deliver_mode": "session"}}],
    )
    assert adapter.resolve_deliver_mode("zone.back_yard") == "session"
    assert adapter.resolve_deliver_mode("sensor.other") == "broadcast"


@pytest.mark.asyncio
async def test_handle_event_injects_the_event_not_a_reply(monkeypatch):
    """Session mode must inject the EVENT at ingestion time so the agent reasons
    inside the target session. Injecting the outbound reply instead (the previous
    shape) moved a finished answer across platforms, ran the reasoning in the
    source session, and cost a second agent turn per event."""
    adapter = _make_session_mode_adapter(
        watch_entities=["sensor.s"], deliver="whatsapp", deliver_mode="session",
    )
    wa = _RecordingAdapter()
    store = _home_store()
    runner = _Runner(wa, store)
    _wire_session_mode(adapter, runner, runner.home())
    adapter.gateway_runner = runner
    adapter._owner_profile = None
    adapter._last_event_time.clear()

    # If the source-session path ran, it would call handle_message on the adapter.
    adapter.handle_message = AsyncMock()

    await adapter._handle_ha_event(_make_event("sensor.s", "0", "1"))

    adapter.handle_message.assert_not_called()  # no source-session turn
    assert wa.handled, "the event must be injected into the target session"
    injected = wa.handled[0]
    assert injected.internal is True
    assert injected.allow_gateway_control is False
    assert injected.metadata["hermes_ha_entity_id"] == "sensor.s"
    # The injected text is the EVENT description, not an agent reply.
    assert "sensor.s" in injected.text
    assert "NO_REPLY" in injected.text  # reply contract present


@pytest.mark.asyncio
async def test_handle_event_falls_back_to_source_session_when_not_injectable(monkeypatch):
    """When integration is impossible (no home channel), the event must still be
    processed normally — the alert is never dropped."""
    adapter = _make_session_mode_adapter(
        watch_entities=["sensor.s"], deliver="whatsapp", deliver_mode="session",
    )
    runner = _Runner(_RecordingAdapter(), _home_store())
    runner.config = type("C", (), {"get_home_channel": staticmethod(lambda p: None)})()
    adapter.gateway_runner = runner
    adapter._owner_profile = None
    adapter.handle_message = AsyncMock()
    adapter._last_event_time.clear()

    await adapter._handle_ha_event(_make_event("sensor.s", "0", "1"))

    adapter.handle_message.assert_called_once()  # normal source-session path


# ---------------------------------------------------------------------------
# Session-mode injection at the ingestion point (issue #35060 follow-up)
# ---------------------------------------------------------------------------


def _session_mode_ingest(monkeypatch, **extra):
    """Wire an adapter for _handle_ha_event injection tests; returns (adapter, wa, store)."""
    extra.setdefault("watch_entities", ["sensor.s"])
    adapter = _make_session_mode_adapter(deliver="whatsapp", deliver_mode="session", **extra)
    wa = _RecordingAdapter()
    store = _home_store()
    runner = _Runner(wa, store)
    _wire_session_mode(adapter, runner, runner.home())
    adapter.gateway_runner = runner
    adapter._owner_profile = None
    adapter._last_event_time.clear()
    return adapter, wa, store


@pytest.mark.asyncio
async def test_injection_budget_degrades_to_source_session(monkeypatch):
    """Past the per-chat budget the event must still be processed — via the normal
    source-session path — never dropped and never injected."""
    adapter, wa, store = _session_mode_ingest(monkeypatch)
    adapter.handle_message = AsyncMock()
    cap = HomeAssistantAdapter._INJECTIONS_PER_CHAT_PER_HOUR

    for i in range(cap):
        adapter._last_event_time.clear()
        await adapter._handle_ha_event(_make_event("sensor.s", str(i), str(i + 1)))
    assert len(wa.handled) == cap, f"expected {cap} injections, got {len(wa.handled)}"

    adapter._last_event_time.clear()
    await adapter._handle_ha_event(_make_event("sensor.s", "x", "y"))
    assert len(wa.handled) == cap, "no injection past the cap"
    adapter.handle_message.assert_called()  # degraded to the source session


def test_injection_budget_window_expires():
    """Rolling window, not a lifetime quota: entries past the window are pruned."""
    import time as _t

    adapter = _make_session_mode_adapter(deliver="whatsapp", deliver_mode="session")
    key = "whatsapp:G"
    cap = HomeAssistantAdapter._INJECTIONS_PER_CHAT_PER_HOUR
    window = HomeAssistantAdapter._INJECTION_WINDOW_SECONDS
    adapter._injection_times[key] = [_t.time() - window - 1] * cap

    assert adapter._consume_injection_budget(key) is True, "expired entries must not block"


@pytest.mark.asyncio
async def test_injection_budget_is_chat_scoped():
    """The budget key is the CHAT, not a session: group chats key one session per
    participant, so a per-session cap would multiply by participant count."""
    adapter = _make_session_mode_adapter(deliver="whatsapp", deliver_mode="session")
    cap = HomeAssistantAdapter._INJECTIONS_PER_CHAT_PER_HOUR
    for _ in range(cap):
        assert adapter._consume_injection_budget("whatsapp:G") is True
    assert adapter._consume_injection_budget("whatsapp:G") is False
    # A different chat has its own budget.
    assert adapter._consume_injection_budget("whatsapp:OTHER") is True


@pytest.mark.asyncio
async def test_oversized_event_payload_is_truncated(monkeypatch):
    """Entity state values can be arbitrarily large; the injected text is bounded."""
    adapter, wa, store = _session_mode_ingest(monkeypatch)
    huge = "x" * (HomeAssistantAdapter._WAKE_TEXT_MAX_CONTENT + 5000)
    adapter._format_state_change = staticmethod(lambda *a, **k: huge)

    await adapter._handle_ha_event(_make_event("sensor.s", "0", "1"))

    assert wa.handled
    assert len(wa.handled[0].text) < HomeAssistantAdapter._WAKE_TEXT_MAX_CONTENT + 300
    assert "[truncated]" in wa.handled[0].text


@pytest.mark.asyncio
async def test_event_content_with_silence_marker_is_stripped(monkeypatch):
    """Untrusted entity text containing a gateway silence marker must not survive
    into the injected envelope (the agent could echo it and suppress the alert)."""
    adapter, wa, store = _session_mode_ingest(monkeypatch)
    adapter._format_state_change = staticmethod(
        lambda *a, **k: "sensor says NO_REPLY and [SILENT]"
    )

    await adapter._handle_ha_event(_make_event("sensor.s", "0", "1"))

    assert wa.handled
    payload = wa.handled[0].text.split("\n", 1)[0]
    assert "NO_REPLY" not in payload
    assert "[SILENT]" not in payload
    assert "[marker stripped]" in payload


@pytest.mark.asyncio
async def test_injected_envelope_states_silence_contract(monkeypatch):
    """The envelope names the reply contract, and names a marker the gateway honors."""
    adapter, wa, store = _session_mode_ingest(monkeypatch)

    await adapter._handle_ha_event(_make_event("sensor.s", "0", "1"))

    assert wa.handled
    text = wa.handled[0].text
    # Source tag: the injected envelope is internal=True and the gateway does not
    # attribute it, so the agent needs the tag to tell a machine event from the
    # owner's own message in the target session. The untrusted-framing prefix
    # comes first by design; the source tag appears exactly once (the double-tag
    # guard detects the template tag on the RAW payload, before framing).
    assert text.startswith("entity value (untrusted"), (
        "the untrusted framing must come first"
    )
    assert text.count("[Home Assistant]") == 1, (
        "injected events must carry the source tag exactly once: "
        "nothing else attributes them"
    )
    assert "informational unless action is needed" in text
    assert "NO_REPLY" in text
    from gateway.response_filters import LIVE_GATEWAY_SILENT_MARKERS
    assert "NO_REPLY" in LIVE_GATEWAY_SILENT_MARKERS


@pytest.mark.asyncio
async def test_injection_not_accepted_degrades_to_source_session(monkeypatch):
    """A rejected injection must not drop the event: the source-session path runs."""
    adapter = _make_session_mode_adapter(
        deliver="whatsapp", deliver_mode="session", watch_entities=["sensor.s"],
    )
    wa = _RecordingAdapter(accept=False)  # admit_internal_event will raise
    store = _home_store()
    runner = _Runner(wa, store)
    _wire_session_mode(adapter, runner, runner.home())
    adapter.gateway_runner = runner
    adapter._owner_profile = None
    adapter._last_event_time.clear()
    adapter.handle_message = AsyncMock()

    await adapter._handle_ha_event(_make_event("sensor.s", "0", "1"))

    assert wa.handled, "injection was attempted"
    adapter.handle_message.assert_called_once()  # degraded, not dropped
