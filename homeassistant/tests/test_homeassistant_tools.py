"""Tests for the Home Assistant tool module.

Tests real logic: entity filtering, payload building, response parsing,
handler validation, and availability gating.

Moved from hermes-agent core ``tests/tools/test_homeassistant_tool.py``.
"""

import asyncio
import json
import logging
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest

import homeassistant_plugin.tools as ha_tool
from homeassistant_plugin.tools import (
    _check_ha_available,
    _filter_and_summarize,
    _build_service_payload,
    _parse_service_response,
    _async_call_service,
    _get_headers,
    _get_entity_filter_config,
    _parse_entity_filter_patterns,
    _entity_matches,
    _apply_operator_filters,
    _operator_filter_violation,
    _handle_get_state,
    _handle_call_service,
    _handle_list_entities,
    _BLOCKED_DOMAINS,
    _ENTITY_ID_RE,
    _SERVICE_NAME_RE,
)


# ---------------------------------------------------------------------------
# Sample HA state data (matches real HA /api/states response shape)
# ---------------------------------------------------------------------------

SAMPLE_STATES = [
    {"entity_id": "light.bedroom", "state": "on", "attributes": {"friendly_name": "Bedroom Light", "brightness": 200}},
    {"entity_id": "light.kitchen", "state": "off", "attributes": {"friendly_name": "Kitchen Light"}},
    {"entity_id": "switch.fan", "state": "on", "attributes": {"friendly_name": "Living Room Fan"}},
    {"entity_id": "sensor.temperature", "state": "22.5", "attributes": {"friendly_name": "Kitchen Temperature", "unit_of_measurement": "C"}},
    {"entity_id": "climate.thermostat", "state": "heat", "attributes": {"friendly_name": "Main Thermostat", "current_temperature": 21}},
    {"entity_id": "binary_sensor.motion", "state": "off", "attributes": {"friendly_name": "Hallway Motion"}},
    {"entity_id": "sensor.humidity", "state": "55", "attributes": {"friendly_name": "Bedroom Humidity", "area": "bedroom"}},
]


# ---------------------------------------------------------------------------
# Entity filtering and summarization
# ---------------------------------------------------------------------------


class TestFilterAndSummarize:
    def test_no_filters_returns_all(self):
        result = _filter_and_summarize(SAMPLE_STATES)
        assert result["count"] == 7
        ids = {e["entity_id"] for e in result["entities"]}
        assert "light.bedroom" in ids
        assert "climate.thermostat" in ids

    def test_domain_filter_lights(self):
        result = _filter_and_summarize(SAMPLE_STATES, domain="light")
        assert result["count"] == 2
        for e in result["entities"]:
            assert e["entity_id"].startswith("light.")


    def test_missing_attributes_handled(self):
        states = [{"entity_id": "light.x", "state": "on"}]
        result = _filter_and_summarize(states)
        assert result["count"] == 1
        assert result["entities"][0]["friendly_name"] == ""


# ---------------------------------------------------------------------------
# Service payload building
# ---------------------------------------------------------------------------


class TestBuildServicePayload:
    def test_entity_id_only(self):
        payload = _build_service_payload(entity_id="light.bedroom")
        assert payload == {"entity_id": "light.bedroom"}


    def test_entity_id_param_takes_precedence_over_data(self):
        payload = _build_service_payload(
            entity_id="light.a",
            data={"entity_id": "light.b"},
        )
        # explicit entity_id parameter wins over data["entity_id"]
        assert payload["entity_id"] == "light.a"


# ---------------------------------------------------------------------------
# Service response parsing
# ---------------------------------------------------------------------------


class TestParseServiceResponse:
    def test_list_response_extracts_entities(self):
        ha_response = [
            {"entity_id": "light.bedroom", "state": "on", "attributes": {}},
            {"entity_id": "light.kitchen", "state": "on", "attributes": {}},
        ]
        result = _parse_service_response("light", "turn_on", ha_response)
        assert result["success"] is True
        assert result["service"] == "light.turn_on"
        assert len(result["affected_entities"]) == 2
        assert result["affected_entities"][0]["entity_id"] == "light.bedroom"

    def test_dict_response_with_service_response(self):
        # Response services (weather.get_forecasts) called with ?return_response
        # return {"changed_states": [...], "service_response": {...}}.
        ha_response = {
            "changed_states": [{"entity_id": "weather.home", "state": "recording", "attributes": {}}],
            "service_response": {"forecast": [{"condition": "clear"}]},
        }
        result = _parse_service_response("weather", "get_forecasts", ha_response, "weather.home")
        assert result["success"] is True
        assert result["service_response"] == {"forecast": [{"condition": "clear"}]}
        assert result["affected_entities"][0]["entity_id"] == "weather.home"

    def test_empty_response_with_entity_id_flags_noop(self):
        # HA returns 200 + [] when nothing changed; with an entity_id that must be
        # surfaced as a no-op so _async_call_service can verify why.
        result = _parse_service_response("light", "turn_on", [], "light.bedroom")
        assert result["success"] is False
        assert "affected no entities" in result["error"]

    def test_empty_response_without_entity_id_is_success(self):
        # No target entity (e.g. turn_on all lights): empty list is a valid success.
        result = _parse_service_response("light", "turn_on", [])
        assert result["success"] is True
        assert "error" not in result


class TestCallServiceVerification:
    """_async_call_service must verify an empty response instead of reporting a bare success."""

    @staticmethod
    def _err(status, message):
        # str() of ClientResponseError reads request_info.real_url, so the tests must
        # supply a real RequestInfo (production always does, via resp.request_info).
        info = aiohttp.RequestInfo("GET", "http://ha/api", ())
        return aiohttp.ClientResponseError(info, (), status=status, message=message)

    def _call(self, post_result, get_state_result, domain="light", service="turn_on", entity="light.bedroom"):
        async def fake_api_json(method, path, timeout, payload=None):
            if method == "POST":
                return post_result
            if method == "GET" and path.startswith("/api/states/"):
                return get_state_result
            raise AssertionError(f"unexpected request {method} {path}")

        with patch.object(ha_tool, "_api_json", side_effect=fake_api_json):
            return asyncio.run(_async_call_service(domain, service, entity, None))

    def test_missing_entity_explains(self):
        # POST succeeded with an empty list; the verification GET 404s.
        async def fake_api_json(method, path, timeout, payload=None):
            if method == "POST":
                return []
            raise self._err(404, "message='Not Found'")
        with patch.object(ha_tool, "_api_json", side_effect=fake_api_json):
            result = asyncio.run(_async_call_service("light", "turn_on", "light.bedroom", None))
        assert result["success"] is False
        assert "does not exist" in result["error"]
        assert "ha_list_entities" in result["error"]

    def test_wrong_domain_suggests_correct_one(self):
        # Entity exists but is in the switch domain, not light.
        result = self._call(
            post_result=[],
            get_state_result={"entity_id": "switch.bedroom", "state": "off"},
            domain="light", service="turn_on", entity="switch.bedroom",
        )
        assert result["success"] is False
        assert "'switch'" in result["error"]
        assert "switch.turn_on" in result["error"]

    def test_already_in_requested_state_is_success_with_note(self):
        # Entity exists in the right domain and nothing changed: it was already on.
        result = self._call(
            post_result=[],
            get_state_result={"entity_id": "light.bedroom", "state": "on"},
            domain="light", service="turn_on", entity="light.bedroom",
        )
        assert result["success"] is True
        assert "already in state" in result["note"]
        assert "error" not in result

    # -- devices that confirm after the call returns (#213) ------------------------------------
    # A Zigbee/Wi-Fi plug or bulb flips its state only when the device confirms, ~150-300 ms
    # after POST /api/services returns [], so the first state read still shows the old state.

    def _call_with_states(self, states, domain, service, entity):
        """POST returns []; successive GET /api/states reads return *states* in order (the last repeats)."""
        reads = []

        async def fake_api_json(method, path, timeout, payload=None):
            if method == "POST":
                return []
            if method == "GET" and path == f"/api/states/{entity}":
                reads.append(path)
                return {"entity_id": entity, "state": states[min(len(reads), len(states)) - 1]}
            raise AssertionError(f"unexpected request {method} {path}")

        sleeps = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        with patch.object(ha_tool, "_api_json", side_effect=fake_api_json), \
                patch.object(ha_tool.asyncio, "sleep", side_effect=fake_sleep):
            result = asyncio.run(_async_call_service(domain, service, entity, None))
        return result, len(reads), sleeps

    def test_turn_off_on_a_device_that_confirms_late_reports_the_new_state(self):
        result, reads, _ = self._call_with_states(["on", "on", "off"], "switch", "turn_off", "switch.plug")
        assert result["success"] is True and "error" not in result
        assert "already" not in result["note"]
        assert "now 'off'" in result["note"]
        assert result["affected_entities"] == [{"entity_id": "switch.plug", "state": "off"}]
        assert reads == 3

    def test_turn_on_that_never_confirms_is_an_actionable_error_not_a_noop(self):
        result, reads, sleeps = self._call_with_states(["off"], "light", "turn_on", "light.bulb")
        assert result["success"] is False
        assert "already" not in result["error"]
        assert "still 'off'" in result["error"] and "ha_get_state" in result["error"]
        assert reads == 1 + len(ha_tool._STATE_CONFIRM_DELAYS)
        assert sum(sleeps) <= 2.0  # the wait for a confirmation stays short

    def test_a_true_noop_answers_from_one_read_without_waiting(self):
        result, reads, sleeps = self._call_with_states(["on"], "switch", "turn_on", "switch.plug")
        assert result["success"] is True and "already in state 'on'" in result["note"]
        assert reads == 1 and sleeps == []

    def test_turn_on_is_reached_by_any_on_like_state(self):
        # media_player / climate report playing, idle, heat ... once on, never the literal "on".
        result, reads, _ = self._call_with_states(["idle"], "media_player", "turn_on", "media_player.tv")
        assert result["success"] is True and "already in state 'idle'" in result["note"]
        assert reads == 1

    def test_toggle_is_never_already_satisfied(self):
        result, _, _ = self._call_with_states(["on", "off"], "light", "toggle", "light.bulb")
        assert result["success"] is True and "already" not in result["note"]
        assert result["affected_entities"] == [{"entity_id": "light.bulb", "state": "off"}]

    def test_a_lock_moving_through_locking_counts_as_confirmed(self):
        result, _, _ = self._call_with_states(["unlocked", "locking"], "lock", "lock", "lock.door")
        assert result["success"] is True and "now 'locking'" in result["note"]

    def test_a_service_without_a_target_state_does_not_claim_it_was_already_satisfied(self):
        # set_cover_position names no state; an unchanged state proves nothing either way.
        result, reads, sleeps = self._call_with_states(["open"], "cover", "set_cover_position", "cover.blind")
        assert result["success"] is True and "error" not in result
        assert "already" not in result["note"] and "'open'" in result["note"]
        assert reads == 1 and sleeps == []

    def test_a_device_dropping_to_unavailable_is_not_reported_as_confirmed(self):
        result, _, _ = self._call_with_states(["on", "unavailable"], "switch", "turn_off", "switch.plug")
        assert result["success"] is False
        assert "'unavailable'" in result["error"] and "still" not in result["error"]

    def test_a_failed_recheck_while_waiting_is_reported_not_raised(self):
        reads = []

        async def fake_api_json(method, path, timeout, payload=None):
            if method == "POST":
                return []
            reads.append(path)
            if len(reads) == 1:
                return {"entity_id": "switch.plug", "state": "on"}
            raise asyncio.TimeoutError()

        async def no_sleep(_seconds):
            return None

        with patch.object(ha_tool, "_api_json", side_effect=fake_api_json), \
                patch.object(ha_tool.asyncio, "sleep", side_effect=no_sleep):
            result = asyncio.run(_async_call_service("switch", "turn_off", "switch.plug", None))
        assert result["success"] is False
        assert "still 'on'" in result["error"] and "already" not in result["error"]

    def test_verification_failure_other_than_404_is_not_reported_as_missing(self):
        # A 401/500/timeout on the verification GET says nothing about whether the entity exists.
        async def fake_api_json(method, path, timeout, payload=None):
            if method == "POST":
                return []
            raise self._err(500, "boom")
        with patch.object(ha_tool, "_api_json", side_effect=fake_api_json):
            result = asyncio.run(_async_call_service("light", "turn_on", "light.bedroom", None))
        assert result["success"] is False
        assert "does not exist" not in result["error"]
        assert "could not verify light.bedroom: HTTP 500" in result["error"]

    def test_homeassistant_domain_services_accept_any_entity_domain(self):
        # homeassistant.turn_on on an already-on light is a no-op, not a wrong-domain call.
        result = self._call(
            post_result=[],
            get_state_result={"entity_id": "light.bedroom", "state": "on"},
            domain="homeassistant", service="turn_on", entity="light.bedroom",
        )
        assert result["success"] is True
        assert "already in state" in result["note"]

    def test_requires_responses_retries_with_return_response(self):
        calls = []
        async def fake_api_json(method, path, timeout, payload=None):
            calls.append(path)
            if "return_response" not in path:
                raise self._err(400, "message='Service call requires responses but caller did not ask for responses. Add ?return_response to query parameters.'")
            return {"changed_states": [], "service_response": {"forecast": []}}
        with patch.object(ha_tool, "_api_json", side_effect=fake_api_json):
            result = asyncio.run(_async_call_service("weather", "get_forecasts", None, None))
        assert result["success"] is True
        assert result["service_response"] == {"forecast": []}
        assert len(calls) == 2 and "return_response" in calls[1]




# ---------------------------------------------------------------------------
# Handler validation (no mocks - these paths don't reach the network)
# ---------------------------------------------------------------------------


class TestHandlerValidation:
    def test_get_state_missing_entity_id(self):
        result = json.loads(_handle_get_state({}))
        assert "error" in result
        assert "entity_id" in result["error"]


    def test_call_service_empty_strings(self):
        result = json.loads(_handle_call_service({"domain": "", "service": ""}))
        assert "error" in result


# ---------------------------------------------------------------------------
# Security: domain blocklist
# ---------------------------------------------------------------------------


class TestDomainBlocklist:
    """Verify dangerous HA service domains are blocked."""

    @pytest.mark.parametrize("domain", sorted(_BLOCKED_DOMAINS))
    def test_blocked_domain_rejected(self, domain):
        result = json.loads(_handle_call_service({
            "domain": domain, "service": "any_service"
        }))
        assert "error" in result
        assert "blocked" in result["error"].lower()

    @patch("homeassistant_plugin.tools._async_call_service", new_callable=AsyncMock)
    def test_safe_domain_not_blocked(self, mock_call_service):
        """Safe domains like ``light`` reach the service-call layer."""
        mock_call_service.return_value = {"success": True}
        result = json.loads(_handle_call_service({
            "domain": "light", "service": "turn_on", "entity_id": "light.test"
        }))
        assert result["result"]["success"] is True
        mock_call_service.assert_awaited_once_with(
            "light",
            "turn_on",
            "light.test",
            None,
        )

    def test_blocked_domains_include_shell_command(self):
        assert "shell_command" in _BLOCKED_DOMAINS

    def test_blocked_domains_include_hassio(self):
        assert "hassio" in _BLOCKED_DOMAINS


# ---------------------------------------------------------------------------
# Security: entity_id validation
# ---------------------------------------------------------------------------


class TestEntityIdValidation:
    """Verify entity_id format validation prevents path traversal."""

    def test_valid_entity_id_accepted(self):
        assert _ENTITY_ID_RE.match("light.bedroom")
        assert _ENTITY_ID_RE.match("sensor.temperature_1")
        assert _ENTITY_ID_RE.match("binary_sensor.motion")
        assert _ENTITY_ID_RE.match("climate.main_thermostat")

    def test_path_traversal_rejected(self):
        assert _ENTITY_ID_RE.match("../../config") is None
        assert _ENTITY_ID_RE.match("light/../../../etc/passwd") is None
        assert _ENTITY_ID_RE.match("../api/config") is None


    @patch("homeassistant_plugin.tools._async_call_service", new_callable=AsyncMock)
    def test_call_service_allows_no_entity_id(self, mock_call_service):
        """Some services (like scene.turn_on) don't need entity_id."""
        mock_call_service.return_value = {"success": True}
        result = json.loads(_handle_call_service({
            "domain": "scene", "service": "turn_on"
        }))
        assert result["result"]["success"] is True
        mock_call_service.assert_awaited_once_with(
            "scene",
            "turn_on",
            None,
            None,
        )


# ---------------------------------------------------------------------------
# String-data deserialization (XML tool calling workaround)
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Security: domain/service name format validation
# ---------------------------------------------------------------------------


class TestServiceNameValidation:
    """Verify domain/service format validation prevents path traversal in URL.

    The domain and service parameters are interpolated into
    /api/services/{domain}/{service}, so allowing arbitrary strings would
    enable SSRF via path traversal or blocked-domain bypass.
    """

    def test_valid_domain_names(self):
        assert _SERVICE_NAME_RE.match("light")
        assert _SERVICE_NAME_RE.match("switch")
        assert _SERVICE_NAME_RE.match("climate")
        assert _SERVICE_NAME_RE.match("shell_command")
        assert _SERVICE_NAME_RE.match("media_player")


    def test_path_traversal_in_domain_rejected(self):
        assert _SERVICE_NAME_RE.match("../../api/config") is None
        assert _SERVICE_NAME_RE.match("light/../../../etc") is None
        assert _SERVICE_NAME_RE.match("../config") is None

    def test_path_traversal_in_service_rejected(self):
        assert _SERVICE_NAME_RE.match("../../api/config") is None
        assert _SERVICE_NAME_RE.match("turn_on/../../config") is None

    def test_blocked_domain_bypass_via_traversal_rejected(self):
        """Ensure shell_command/../light is rejected, not just checked against blocklist."""
        assert _SERVICE_NAME_RE.match("shell_command/../light") is None
        assert _SERVICE_NAME_RE.match("python_script/../scene") is None
        assert _SERVICE_NAME_RE.match("hassio/../automation") is None


    def test_special_chars_rejected(self):
        assert _SERVICE_NAME_RE.match("light;rm") is None
        assert _SERVICE_NAME_RE.match("light&cmd") is None
        assert _SERVICE_NAME_RE.match("light cmd") is None

    def test_handler_rejects_traversal_domain(self):
        """_handle_call_service must reject domain with path traversal."""
        result = json.loads(_handle_call_service({
            "domain": "../../api/config",
            "service": "turn_on",
        }))
        assert "error" in result
        assert "Invalid domain" in result["error"]

    def test_handler_rejects_traversal_service(self):
        """_handle_call_service must reject service with path traversal."""
        result = json.loads(_handle_call_service({
            "domain": "light",
            "service": "../../api/config",
        }))
        assert "error" in result
        assert "Invalid service" in result["error"]


# ---------------------------------------------------------------------------
# Availability check
# ---------------------------------------------------------------------------


class TestCheckAvailable:
    def test_unavailable_without_token(self, monkeypatch):
        monkeypatch.delenv("HASS_TOKEN", raising=False)
        assert _check_ha_available() is False


    def test_empty_token_is_unavailable(self, monkeypatch):
        monkeypatch.setenv("HASS_TOKEN", "")
        assert _check_ha_available() is False

    def test_multiplex_scope_does_not_fall_back_to_another_profile(self, monkeypatch):
        from agent import secret_scope

        monkeypatch.setenv("HASS_TOKEN", "default-profile-token")
        secret_scope.set_multiplex_active(True)
        token = secret_scope.set_secret_scope({})
        try:
            assert _check_ha_available() is False
        finally:
            secret_scope.reset_secret_scope(token)
            secret_scope.set_multiplex_active(False)

    def test_multiplex_scope_supplies_profile_url_and_token(self, monkeypatch):
        from agent import secret_scope
        from homeassistant_plugin.tools import _get_config

        monkeypatch.setenv("HASS_URL", "http://default-profile:8123")
        monkeypatch.setenv("HASS_TOKEN", "default-profile-token")
        secret_scope.set_multiplex_active(True)
        token = secret_scope.set_secret_scope({
            "HASS_URL": "http://secondary-profile:8123/",
            "HASS_TOKEN": "secondary-profile-token",
        })
        try:
            assert _get_config() == (
                "http://secondary-profile:8123",
                "secondary-profile-token",
            )
        finally:
            secret_scope.reset_secret_scope(token)
            secret_scope.set_multiplex_active(False)


# ---------------------------------------------------------------------------
# Auth headers
# ---------------------------------------------------------------------------


class TestGetHeaders:
    def test_bearer_token_format(self, monkeypatch):
        monkeypatch.setattr("homeassistant_plugin.tools._get_config",
                            lambda: ("http://ha.local:8123", "my-secret-token"))
        headers = _get_headers()
        assert headers["Authorization"] == "Bearer my-secret-token"
        assert headers["Content-Type"] == "application/json"


# ---------------------------------------------------------------------------
# String-data deserialization reaches the service layer as a dict
# ---------------------------------------------------------------------------


class TestStringData:
    @patch("homeassistant_plugin.tools._async_call_service", new_callable=AsyncMock)
    def test_json_string_data_is_parsed(self, mock_call_service):
        mock_call_service.return_value = {"success": True}
        json.loads(_handle_call_service({
            "domain": "light", "service": "turn_on", "entity_id": "light.a",
            "data": '{"brightness": 255}',
        }))
        mock_call_service.assert_awaited_once_with("light", "turn_on", "light.a", {"brightness": 255})

    def test_invalid_json_string_data_is_an_error(self):
        result = json.loads(_handle_call_service({
            "domain": "light", "service": "turn_on", "data": "{not json"}))
        assert "Invalid JSON string" in result["error"]


class TestUrlWithoutScheme:
    def test_scheme_is_required(self):
        from homeassistant_plugin.tools import url_scheme_problem

        assert url_scheme_problem("https://ha.example.com") is None
        assert url_scheme_problem("http://homeassistant.local:8123") is None
        assert "HASS_URL" in url_scheme_problem("ha.example.com")

    def test_tool_call_reports_the_cause(self, monkeypatch):
        monkeypatch.setenv("HASS_URL", "ha.example.com")
        monkeypatch.setenv("HASS_TOKEN", "tok")
        result = json.loads(_handle_get_state({"entity_id": "light.kitchen"}))
        assert "no http:// or https:// scheme" in result["error"]


# ---------------------------------------------------------------------------
# Tool schema: the description must steer the model toward targeted queries
# ---------------------------------------------------------------------------


class TestListEntitiesSchema:
    def test_schema_advertises_filters(self):
        schema = ha_tool.HA_LIST_ENTITIES_SCHEMA
        props = schema["parameters"]["properties"]
        for key in ("domain", "area", "name", "entity_ids", "max"):
            assert key in props, f"schema missing {key!r}"

    def test_description_steers_to_filters_without_asserting_a_fixed_cap(self):
        """The old 'Omit to list all entities' framing was the footgun; but the
        cap is operator-configurable, so the description must not present a
        hardcoded number as a guarantee either."""
        desc = ha_tool.HA_LIST_ENTITIES_SCHEMA["description"].lower()
        assert "filter" in desc
        assert "truncated" in desc
        assert "omit to list all" not in desc
        assert "returns only the first 100" not in desc  # 100 is no longer a fact
        assert "hass_max_entities" in desc  # the cap is mentioned as operator config

    def test_entity_ids_schema_is_a_string_array(self):
        assert ha_tool.HA_LIST_ENTITIES_SCHEMA["parameters"]["properties"]["entity_ids"]["type"] == "array"
        assert ha_tool.HA_LIST_ENTITIES_SCHEMA["parameters"]["properties"]["entity_ids"]["items"] == {"type": "string"}


# ---------------------------------------------------------------------------
# Targeted queries: entity-id list, name/area match, cap, operator lists
# ---------------------------------------------------------------------------

def _reset_filter_mirrors(monkeypatch):
    """Clear the module-level allow/deny/cap mirrors + env so each test is isolated."""
    monkeypatch.setattr(ha_tool, "_HASS_ENTITY_ALLOWLIST", "")
    monkeypatch.setattr(ha_tool, "_HASS_ENTITY_DENYLIST", "")
    monkeypatch.setattr(ha_tool, "_HASS_MAX_ENTITIES", "")
    monkeypatch.delenv("HASS_ENTITY_ALLOWLIST", raising=False)
    monkeypatch.delenv("HASS_ENTITY_DENYLIST", raising=False)
    monkeypatch.delenv("HASS_MAX_ENTITIES", raising=False)


# A realistic-ish payload with a zombie aux-entity cluster left behind by a
# device migration, plus a live duplicate of the same physical room.
ZOMBY_STATES = [
    {"entity_id": "climate.office", "state": "heat",
     "attributes": {"friendly_name": "Office Thermostat", "current_temperature": 28.0, "temperature": 28.0}},
    {"entity_id": "office_thermostat.0_external_temperature", "state": "0.0",
     "attributes": {"friendly_name": "Office Thermostat External Temperature"}},
    {"entity_id": "office_thermostat.0_valve_position", "state": "0",
     "attributes": {"friendly_name": "Office Thermostat Valve Position"}},
    {"entity_id": "climate.office_zombie", "state": "off",
     "attributes": {"friendly_name": "Office Thermostat (duplicate)", "current_temperature": 22.0}},
    {"entity_id": "sensor.kitchen_temp", "state": "24.0",
     "attributes": {"friendly_name": "Kitchen Temperature"}},
]


class TestTargetedEntityIdFilter:
    def test_entity_ids_returns_only_requested(self, monkeypatch):
        _reset_filter_mirrors(monkeypatch)
        result = _filter_and_summarize(ZOMBY_STATES, entity_ids=["climate.office", "sensor.kitchen_temp"])
        assert result["count"] == 2
        ids = {e["entity_id"] for e in result["entities"]}
        assert ids == {"climate.office", "sensor.kitchen_temp"}

    def test_entity_ids_accepts_comma_string(self, monkeypatch):
        """The XML/LLM tool-calling path may deliver the list as a string."""
        _reset_filter_mirrors(monkeypatch)
        result = _filter_and_summarize(ZOMBY_STATES, entity_ids="climate.office, sensor.kitchen_temp")
        assert result["count"] == 2
        assert {e["entity_id"] for e in result["entities"]} == {"climate.office", "sensor.kitchen_temp"}

    def test_entity_ids_supersedes_domain(self, monkeypatch):
        """An explicit id list wins over a broader co-passed domain filter."""
        _reset_filter_mirrors(monkeypatch)
        result = _filter_and_summarize(ZOMBY_STATES, domain="light", entity_ids=["sensor.kitchen_temp"])
        assert [e["entity_id"] for e in result["entities"]] == ["sensor.kitchen_temp"]

    def test_handler_passes_entity_ids_through(self, monkeypatch):
        _reset_filter_mirrors(monkeypatch)
        with patch.object(ha_tool, "_run_async", return_value={"count": 1, "entities": []}) as run:
            _handle_list_entities({"entity_ids": ["climate.office"]})
            coro = run.call_args[0][0]
            try:
                assert coro.cr_frame.f_locals["entity_ids"] == ["climate.office"]
            finally:
                coro.close()


class TestNameAreaFilter:
    def test_name_substring_matches_friendly_name_and_entity_id(self, monkeypatch):
        _reset_filter_mirrors(monkeypatch)
        result = _filter_and_summarize(ZOMBY_STATES, name="kitchen")
        assert [e["entity_id"] for e in result["entities"]] == ["sensor.kitchen_temp"]

    def test_name_matches_entity_id_when_no_friendly_name(self, monkeypatch):
        _reset_filter_mirrors(monkeypatch)
        states = [{"entity_id": "sensor.office_co2", "state": "600", "attributes": {}}]
        result = _filter_and_summarize(states, name="office")
        assert [e["entity_id"] for e in result["entities"]] == ["sensor.office_co2"]

    def test_area_filter_unaffected_by_name_param(self, monkeypatch):
        _reset_filter_mirrors(monkeypatch)
        states = [
            {"entity_id": "switch.a", "state": "on", "attributes": {"area": "kitchen"}},
            {"entity_id": "switch.b", "state": "on", "attributes": {"area": "garage"}},
        ]
        result = _filter_and_summarize(states, area="kitchen")
        assert [e["entity_id"] for e in result["entities"]] == ["switch.a"]


class TestMaxEntitiesConfig:
    def test_unset_env_means_no_cap_returns_everything(self, monkeypatch):
        """Compatibility guarantee: unset = current upstream behaviour."""
        _reset_filter_mirrors(monkeypatch)
        big = [
            {"entity_id": f"sensor.idx_{i}", "state": "0", "attributes": {}}
            for i in range(2500)
        ]
        result = _filter_and_summarize(big)
        assert result["count"] == 2500
        assert "truncated" not in result

    def test_env_set_caps_and_marks_truncated(self, monkeypatch):
        _reset_filter_mirrors(monkeypatch)
        monkeypatch.setenv("HASS_MAX_ENTITIES", "100")
        big = [
            {"entity_id": f"sensor.idx_{i}", "state": "0", "attributes": {}}
            for i in range(2500)
        ]
        result = _filter_and_summarize(big)
        assert result["count"] == 100
        assert result["truncated"] is True
        assert "2500" in result["truncated_note"]

    def test_env_set_below_count_no_truncation_flag(self, monkeypatch):
        _reset_filter_mirrors(monkeypatch)
        monkeypatch.setenv("HASS_MAX_ENTITIES", "10")
        result = _filter_and_summarize(ZOMBY_STATES)
        assert result["count"] == len(ZOMBY_STATES)
        assert "truncated" not in result

    def test_env_set_still_caps_a_filtered_query(self, monkeypatch):
        """The cap applies even when domain/name filters are present."""
        _reset_filter_mirrors(monkeypatch)
        monkeypatch.setenv("HASS_MAX_ENTITIES", "200")
        big = [
            {"entity_id": f"sensor.idx_{i}", "state": "0", "attributes": {}}
            for i in range(250)
        ]
        result = _filter_and_summarize(big, domain="sensor")
        assert result["count"] == 200
        assert result["truncated"] is True
        assert "250" in result["truncated_note"]

    def test_explicit_max_overrides_env(self, monkeypatch):
        _reset_filter_mirrors(monkeypatch)
        monkeypatch.setenv("HASS_MAX_ENTITIES", "100")
        big = [
            {"entity_id": f"sensor.idx_{i}", "state": "0", "attributes": {}}
            for i in range(300)
        ]
        result = _filter_and_summarize(big, max_entities=50)
        assert result["count"] == 50
        assert result["truncated"] is True

    def test_handler_coerces_string_max(self, monkeypatch):
        _reset_filter_mirrors(monkeypatch)
        with patch.object(ha_tool, "_run_async", return_value={"count": 1, "entities": []}) as run:
            _handle_list_entities({"max": "50"})
            coro = run.call_args[0][0]
            try:
                assert coro.cr_frame.f_locals["max_entities"] == 50
            finally:
                coro.close()

    def test_handler_rejects_non_integer_max(self, monkeypatch):
        _reset_filter_mirrors(monkeypatch)
        result = json.loads(_handle_list_entities({"max": "lots"}))
        assert "error" in result
        assert "max" in result["error"].lower()

    def test_get_max_entities_config_reads_env(self, monkeypatch):
        _reset_filter_mirrors(monkeypatch)
        assert ha_tool._get_max_entities_config() is None
        monkeypatch.setenv("HASS_MAX_ENTITIES", "250")
        assert ha_tool._get_max_entities_config() == 250

    def test_get_max_entities_config_rejects_bad_values(self, monkeypatch):
        _reset_filter_mirrors(monkeypatch)
        monkeypatch.setenv("HASS_MAX_ENTITIES", "not-a-number")
        assert ha_tool._get_max_entities_config() is None
        monkeypatch.setenv("HASS_MAX_ENTITIES", "0")
        assert ha_tool._get_max_entities_config() is None
        monkeypatch.setenv("HASS_MAX_ENTITIES", "-5")
        assert ha_tool._get_max_entities_config() is None


class TestOperatorAllowDeny:
    def test_denylist_excludes_zombie_cluster(self, monkeypatch):
        """Zombie entities left behind by a device migration are hidden by config."""
        _reset_filter_mirrors(monkeypatch)
        monkeypatch.setenv("HASS_ENTITY_DENYLIST", "office_thermostat_*")
        result = _filter_and_summarize(ZOMBY_STATES)
        ids = {e["entity_id"] for e in result["entities"]}
        assert "office_thermostat.0_external_temperature" not in ids
        assert "office_thermostat.0_valve_position" not in ids
        assert "climate.office" in ids  # live entity with different id survives

    def test_denylist_vetoes_even_when_allowlisted(self, monkeypatch):
        """Denylist is a final veto: an entity in both lists is excluded."""
        _reset_filter_mirrors(monkeypatch)
        monkeypatch.setenv("HASS_ENTITY_ALLOWLIST", "climate.*")
        monkeypatch.setenv("HASS_ENTITY_DENYLIST", "climate.office_zombie")
        result = _filter_and_summarize(ZOMBY_STATES)
        ids = {e["entity_id"] for e in result["entities"]}
        assert ids == {"climate.office"}  # zombie in both lists dropped; non-allowlisted dropped

    def test_allowlist_only_keeps_matched(self, monkeypatch):
        _reset_filter_mirrors(monkeypatch)
        monkeypatch.setenv("HASS_ENTITY_ALLOWLIST", "sensor.kitchen_temp")
        result = _filter_and_summarize(ZOMBY_STATES)
        assert [e["entity_id"] for e in result["entities"]] == ["sensor.kitchen_temp"]

    def test_patterns_parsed_from_env(self, monkeypatch):
        _reset_filter_mirrors(monkeypatch)
        monkeypatch.setenv("HASS_ENTITY_DENYLIST", "  a.b , c.d  ")
        allow, deny = _get_entity_filter_config()
        assert allow == []
        assert deny == ["a.b", "c.d"]

    def test_apply_operator_filters_noops_when_empty(self):
        out = _apply_operator_filters(ZOMBY_STATES, [], [])
        assert out is ZOMBY_STATES

    def test_entity_matches_prefix_exact_and_glob(self):
        assert _entity_matches("office_thermostat.0_external_temperature", ["office_thermostat_*"])
        assert _entity_matches("office_thermostat.0", ["office_thermostat.0"])
        assert _entity_matches("office_thermostat.0", ["office_thermostat."])
        assert not _entity_matches("sensor.other", ["office_thermostat.*"])


# ---------------------------------------------------------------------------
# Single-entity read path (ha_get_state) honors the operator allow/deny lists
# ---------------------------------------------------------------------------


class TestGetStateOperatorFilters:
    """A denylisted zombie must not be readable via ha_get_state either."""

    def test_denylisted_entity_is_refused_with_clear_error(self, monkeypatch):
        _reset_filter_mirrors(monkeypatch)
        monkeypatch.setenv("HASS_ENTITY_DENYLIST", "office_thermostat_*")
        # Refused BEFORE any network call — _run_async must not be reached.
        with patch.object(ha_tool, "_run_async") as run:
            result = json.loads(_handle_get_state(
                {"entity_id": "office_thermostat.0_external_temperature"}
            ))
        assert "error" in result
        assert "operator config" in result["error"]
        assert "HASS_ENTITY_DENYLIST" in result["error"]
        run.assert_not_called()

    def test_allowlist_blocks_non_listed_entity(self, monkeypatch):
        _reset_filter_mirrors(monkeypatch)
        monkeypatch.setenv("HASS_ENTITY_ALLOWLIST", "climate.*")
        with patch.object(ha_tool, "_run_async") as run:
            result = json.loads(_handle_get_state({"entity_id": "sensor.kitchen_temp"}))
        assert "error" in result
        assert "HASS_ENTITY_ALLOWLIST" in result["error"]
        run.assert_not_called()

    def test_denylist_vetoes_allowlisted_entity_on_read(self, monkeypatch):
        """Consistent with the list path: deny is a final veto."""
        _reset_filter_mirrors(monkeypatch)
        monkeypatch.setenv("HASS_ENTITY_ALLOWLIST", "climate.*")
        monkeypatch.setenv("HASS_ENTITY_DENYLIST", "climate.office_zombie")
        with patch.object(ha_tool, "_run_async") as run:
            result = json.loads(_handle_get_state({"entity_id": "climate.office_zombie"}))
        assert "error" in result
        assert "HASS_ENTITY_DENYLIST" in result["error"]
        run.assert_not_called()

    def test_normal_entity_still_reads(self, monkeypatch):
        """No operator config: a valid entity reaches the fetch layer."""
        _reset_filter_mirrors(monkeypatch)
        with patch.object(ha_tool, "_async_get_state", new_callable=AsyncMock) as fetch:
            fetch.return_value = {"entity_id": "climate.office", "state": "heat"}
            result = json.loads(_handle_get_state({"entity_id": "climate.office"}))
        assert result["result"]["entity_id"] == "climate.office"
        fetch.assert_awaited_once_with("climate.office")

    def test_operator_configured_but_matched_entity_reads(self, monkeypatch):
        """With a denylist set, an entity that does NOT match it still reads."""
        _reset_filter_mirrors(monkeypatch)
        monkeypatch.setenv("HASS_ENTITY_DENYLIST", "office_thermostat_*")
        with patch.object(ha_tool, "_async_get_state", new_callable=AsyncMock) as fetch:
            fetch.return_value = {"entity_id": "climate.office", "state": "heat",
                                  "current_temperature": 28.0}
            result = json.loads(_handle_get_state({"entity_id": "climate.office"}))
        assert result["result"]["entity_id"] == "climate.office"
        fetch.assert_awaited_once_with("climate.office")

    def test_violation_helper_returns_none_when_unset(self, monkeypatch):
        _reset_filter_mirrors(monkeypatch)
        assert _operator_filter_violation("sensor.anything") is None

    def test_violation_helper_reports_deny_then_allow(self, monkeypatch):
        _reset_filter_mirrors(monkeypatch)
        monkeypatch.setenv("HASS_ENTITY_ALLOWLIST", "climate.*")
        assert _operator_filter_violation("sensor.x") == "not in HASS_ENTITY_ALLOWLIST"
        monkeypatch.setenv("HASS_ENTITY_DENYLIST", "office_thermostat_*")
        assert _operator_filter_violation("office_thermostat.0_valve_position") == (
            "excluded by HASS_ENTITY_DENYLIST"
        )


# ---------------------------------------------------------------------------
# Control path (ha_call_service) honors the operator allow/deny lists
# ---------------------------------------------------------------------------


class TestCallServiceOperatorFilters:
    """The lists block control too: a denied or non-allowlisted entity cannot be
    switched, and a call whose targets cannot be checked is refused. Every refusal
    happens before any network call."""

    def _call(self, args):
        with patch.object(ha_tool, "_run_async") as run:
            result = json.loads(_handle_call_service(args))
        return result, run

    def test_denylisted_entity_cannot_be_controlled(self, monkeypatch):
        _reset_filter_mirrors(monkeypatch)
        monkeypatch.setenv("HASS_ENTITY_DENYLIST", "lock.*,office_thermostat_*")
        for args in (
            {"domain": "lock", "service": "unlock", "entity_id": "lock.front_door"},
            {"domain": "light", "service": "turn_on",
             "data": '{"entity_id": ["light.kitchen", "lock.front_door"]}'},
            {"domain": "climate", "service": "set_temperature",
             "data": {"target": {"entity_id": "office_thermostat.0_valve"}, "temperature": 30}},
        ):
            result, run = self._call(args)
            assert "HASS_ENTITY_DENYLIST" in result.get("error", ""), args
            run.assert_not_called()
        assert "'lock.*'" in self._call(
            {"domain": "lock", "service": "unlock", "entity_id": "lock.front_door"})[0]["error"]

    def test_allowlist_refuses_outside_and_unresolvable_targets(self, monkeypatch):
        _reset_filter_mirrors(monkeypatch)
        monkeypatch.setenv("HASS_ENTITY_ALLOWLIST", "light.*,script.goodnight")
        refused = (
            {"domain": "switch", "service": "turn_on", "entity_id": "switch.heater"},
            {"domain": "light", "service": "turn_off"},                       # no target = every light
            {"domain": "light", "service": "turn_off", "data": {"area_id": "kitchen"}},
            {"domain": "light", "service": "turn_off", "data": {"target": {"device_id": "abc"}}},
            {"domain": "light", "service": "turn_off", "data": {"floor_id": "f1"}},
            {"domain": "light", "service": "turn_off", "data": {"label_id": "l1"}},
            {"domain": "light", "service": "turn_off", "data": {"entity_id": "all"}},
            {"domain": "script", "service": "wake_up"},                       # runs script.wake_up
        )
        for args in refused:
            result, run = self._call(args)
            assert "Refused by operator config" in result.get("error", ""), args
            run.assert_not_called()
        with patch.object(ha_tool, "_async_call_service", new_callable=AsyncMock) as call:
            call.return_value = {"success": True}
            for args in (
                {"domain": "light", "service": "turn_on", "entity_id": "light.kitchen"},
                {"domain": "script", "service": "goodnight"},
                {"domain": "script", "service": "turn_on", "entity_id": "script.goodnight"},
                {"domain": "notify", "service": "mobile_app_phone", "data": {"message": "hi"}},
            ):
                assert json.loads(_handle_call_service(args))["result"]["success"] is True, args
        assert call.await_count == 4

    def test_no_filters_leaves_control_unchanged(self, monkeypatch):
        _reset_filter_mirrors(monkeypatch)
        with patch.object(ha_tool, "_async_call_service", new_callable=AsyncMock) as call:
            call.return_value = {"success": True}
            for args in (
                {"domain": "light", "service": "turn_off"},
                {"domain": "light", "service": "turn_off", "data": {"area_id": "kitchen"}},
                {"domain": "lock", "service": "unlock", "entity_id": "lock.front_door"},
            ):
                assert json.loads(_handle_call_service(args))["result"]["success"] is True, args
        assert call.await_count == 3


# ── Registry-backed area/device resolution ───────────────────────────────────
class _FakeWS:
    """Scripted HA websocket: hands out the queued frames, records what was sent."""

    def __init__(self, frames):
        self.frames = list(frames)
        self.sent = []

    async def receive_json(self):
        return self.frames.pop(0)

    async def send_json(self, msg):
        self.sent.append(msg)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    """Stand-in for aiohttp.ClientSession whose ws_connect yields the scripted socket."""

    ws: "_FakeWS" = _FakeWS([])
    connect_calls = 0
    last_url = ""

    def __init__(self, *args, **kwargs):
        pass

    def ws_connect(self, url, **kwargs):
        _FakeSession.connect_calls += 1
        _FakeSession.last_url = url
        return _FakeSession.ws

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _registry_frames(area_reg, device_reg, entity_reg, auth="auth_ok"):
    return [
        {"type": "auth_required"},
        {"type": auth, "message": "Invalid access token" if auth != "auth_ok" else None},
        {"type": "event", "event": "noise"},          # unrelated frame: must be skipped
        {"id": 1, "type": "result", "result": area_reg},
        {"id": 2, "type": "result", "result": device_reg},
        {"id": 3, "type": "result", "result": entity_reg},
    ]


def _install_fake_ws(monkeypatch, frames, clock=None):
    import aiohttp

    _FakeSession.ws = _FakeWS(frames)
    _FakeSession.connect_calls = 0
    _FakeSession.last_url = ""
    monkeypatch.setattr(aiohttp, "ClientSession", _FakeSession)
    monkeypatch.setattr(ha_tool, "_get_config", lambda: ("http://ha.local:8123", "tok-123"))
    monkeypatch.setattr(ha_tool, "_REGISTRY_CACHE", {"ts": 0.0, "entity_area": {}, "entity_device": {}})
    if clock is not None:
        monkeypatch.setattr("time.time", lambda: clock[0])


class TestRegistryAreaMap:
    AREAS = [{"area_id": "office", "name": "Office"}, {"area_id": "kitchen", "name": "Kitchen"}]
    DEVICES = [{"id": "dev1", "area_id": "kitchen", "name": "Fridge Hub", "name_by_user": "Fridge"}]
    ENTITIES = [
        {"entity_id": "climate.desk", "area_id": "office", "device_id": None},
        {"entity_id": "sensor.fridge_temp", "area_id": None, "device_id": "dev1"},
        {"entity_id": "sensor.orphan", "area_id": None, "device_id": None},
    ]

    def test_maps_entity_area_and_falls_back_to_device_area(self, monkeypatch):
        _install_fake_ws(monkeypatch, _registry_frames(self.AREAS, self.DEVICES, self.ENTITIES))
        ent_area, ent_dev = asyncio.run(ha_tool._registry_area_map())
        assert ent_area == {"climate.desk": "Office", "sensor.fridge_temp": "Kitchen"}
        assert ent_dev == {"sensor.fridge_temp": "Fridge"}          # name_by_user beats name
        assert _FakeSession.last_url == "ws://ha.local:8123/api/websocket"
        assert _FakeSession.ws.sent[0] == {"type": "auth", "access_token": "tok-123"}
        assert [m["type"] for m in _FakeSession.ws.sent[1:]] == [
            "config/area_registry/list", "config/device_registry/list", "config/entity_registry/list"]

    def test_successful_fetch_is_cached_within_ttl(self, monkeypatch):
        clock = [1000.0]
        _install_fake_ws(monkeypatch, _registry_frames(self.AREAS, self.DEVICES, self.ENTITIES), clock)
        first = asyncio.run(ha_tool._registry_area_map())
        clock[0] += ha_tool._REGISTRY_TTL - 1
        assert asyncio.run(ha_tool._registry_area_map()) == first
        assert _FakeSession.connect_calls == 1

    def test_cache_expires_after_ttl(self, monkeypatch):
        clock = [1000.0]
        _install_fake_ws(monkeypatch, _registry_frames(self.AREAS, self.DEVICES, self.ENTITIES), clock)
        asyncio.run(ha_tool._registry_area_map())
        clock[0] += ha_tool._REGISTRY_TTL + 1
        _FakeSession.ws = _FakeWS(_registry_frames(self.AREAS, [], []))
        assert asyncio.run(ha_tool._registry_area_map()) == ({}, {})
        assert _FakeSession.connect_calls == 2

    def test_empty_but_successful_fetch_is_cached_too(self, monkeypatch):
        # An install with no areas assigned must not re-download the registry on every call.
        clock = [1000.0]
        _install_fake_ws(monkeypatch, _registry_frames([], [], self.ENTITIES), clock)
        assert asyncio.run(ha_tool._registry_area_map()) == ({}, {})
        clock[0] += 10
        assert asyncio.run(ha_tool._registry_area_map()) == ({}, {})
        assert _FakeSession.connect_calls == 1

    def test_auth_failure_degrades_with_warning_and_is_not_cached(self, monkeypatch, caplog):
        _install_fake_ws(monkeypatch, _registry_frames(self.AREAS, self.DEVICES, self.ENTITIES, auth="auth_invalid"))
        with caplog.at_level(logging.WARNING, logger="homeassistant_plugin.tools"):
            assert asyncio.run(ha_tool._registry_area_map()) == ({}, {})
        assert "auth failed" in caplog.text and "Invalid access token" in caplog.text
        assert ha_tool._REGISTRY_CACHE["ts"] == 0.0                    # next call retries

    def test_stalled_server_hits_the_deadline_and_degrades(self, monkeypatch, caplog):
        # A HA that accepts the socket but never answers must not hang ha_list_entities.
        class _StalledWS(_FakeWS):
            async def receive_json(self):
                await asyncio.sleep(3600)

        _install_fake_ws(monkeypatch, [])
        _FakeSession.ws = _StalledWS([])
        monkeypatch.setattr(ha_tool, "_REGISTRY_TIMEOUT_S", 0.2)
        with caplog.at_level(logging.WARNING, logger="homeassistant_plugin.tools"):
            assert asyncio.run(ha_tool._registry_area_map()) == ({}, {})
        assert "registry lookup timed out after 0.2s" in caplog.text
        assert ha_tool._REGISTRY_CACHE["ts"] == 0.0

    def test_connection_error_degrades_with_warning_and_is_not_cached(self, monkeypatch, caplog):
        _install_fake_ws(monkeypatch, [])

        def boom(self, url, **kwargs):
            raise ConnectionRefusedError("no route")

        monkeypatch.setattr(_FakeSession, "ws_connect", boom)
        with caplog.at_level(logging.WARNING, logger="homeassistant_plugin.tools"):
            assert asyncio.run(ha_tool._registry_area_map()) == ({}, {})
        assert "registry lookup failed" in caplog.text
        assert ha_tool._REGISTRY_CACHE["ts"] == 0.0


class TestRegistryBackedAreaFilter:
    STATES = [
        {"entity_id": "climate.desk", "state": "heat", "attributes": {"friendly_name": "Desk climate"}},
        {"entity_id": "light.lamp", "state": "on", "attributes": {"friendly_name": "Office lamp"}},
        {"entity_id": "sensor.fridge_temp", "state": "4", "attributes": {"friendly_name": "Fridge"}},
    ]
    REGISTRY = {"climate.desk": "Office", "sensor.fridge_temp": "Kitchen", "light.lamp": "Hallway"}

    def test_registry_is_authoritative_over_friendly_name(self, monkeypatch):
        _reset_filter_mirrors(monkeypatch)
        result = _filter_and_summarize(self.STATES, area="office", entity_area=self.REGISTRY)
        # Kept: registry says Office although the name never mentions the room.
        # Dropped: "Office lamp" is registered in the Hallway.
        assert [e["entity_id"] for e in result["entities"]] == ["climate.desk"]
        assert result["entities"][0]["area"] == "Office"

    def test_fallback_to_name_match_when_registry_unavailable(self, monkeypatch):
        _reset_filter_mirrors(monkeypatch)
        result = _filter_and_summarize(self.STATES, area="office", entity_area={})
        assert [e["entity_id"] for e in result["entities"]] == ["light.lamp"]
        assert "area" not in result["entities"][0]

    def test_area_and_device_fields_added_when_known(self, monkeypatch):
        _reset_filter_mirrors(monkeypatch)
        result = _filter_and_summarize(
            self.STATES, entity_area=self.REGISTRY, entity_device={"sensor.fridge_temp": "Fridge"})
        by_id = {e["entity_id"]: e for e in result["entities"]}
        assert by_id["sensor.fridge_temp"] == {
            "entity_id": "sensor.fridge_temp", "state": "4", "friendly_name": "Fridge",
            "area": "Kitchen", "device": "Fridge"}
        assert "device" not in by_id["climate.desk"]

    def test_list_handler_feeds_registry_into_filter(self, monkeypatch):
        _reset_filter_mirrors(monkeypatch)
        monkeypatch.setattr(ha_tool, "_api_json", AsyncMock(return_value=self.STATES))
        monkeypatch.setattr(ha_tool, "_registry_area_map", AsyncMock(return_value=(self.REGISTRY, {})))
        result = json.loads(ha_tool._handle_list_entities({"area": "kitchen"}))["result"]
        assert [e["entity_id"] for e in result["entities"]] == ["sensor.fridge_temp"]
        assert result["entities"][0]["area"] == "Kitchen"


class TestRegistryHardening:
    """Review follow-ups: per-instance cache, failure backoff, refused lists, no websocket on plain lists."""

    AREAS = [{"area_id": "kitchen", "name": "Kitchen"}]
    ENTITIES = [{"entity_id": "light.ceiling", "area_id": "kitchen", "device_id": None}]

    def test_unregistered_entity_still_matches_by_name(self, monkeypatch):
        _reset_filter_mirrors(monkeypatch)
        states = [
            {"entity_id": "light.ceiling", "state": "on", "attributes": {"friendly_name": "Ceiling"}},
            {"entity_id": "light.kitchen_strip", "state": "on", "attributes": {"friendly_name": "Kitchen strip"}},
            {"entity_id": "light.kitchen_lamp", "state": "on", "attributes": {"friendly_name": "Kitchen lamp"}},
        ]
        # light.kitchen_strip has no registry entry (YAML entity); light.kitchen_lamp is registered in the hall.
        result = _filter_and_summarize(
            states, area="kitchen", entity_area={"light.ceiling": "Kitchen", "light.kitchen_lamp": "Hall"})
        assert [e["entity_id"] for e in result["entities"]] == ["light.ceiling", "light.kitchen_strip"]

    def test_cache_is_per_instance(self, monkeypatch):
        _install_fake_ws(monkeypatch, _registry_frames(self.AREAS, [], self.ENTITIES))
        assert asyncio.run(ha_tool._registry_area_map())[0] == {"light.ceiling": "Kitchen"}
        monkeypatch.setattr(ha_tool, "_get_config", lambda: ("http://other.local:8123", "tok-b"))
        _FakeSession.ws = _FakeWS(_registry_frames([], [], []))
        assert asyncio.run(ha_tool._registry_area_map()) == ({}, {})
        assert _FakeSession.connect_calls == 2

    def test_failure_is_not_retried_on_every_call(self, monkeypatch):
        _install_fake_ws(monkeypatch, _registry_frames(self.AREAS, [], self.ENTITIES, auth="auth_invalid"))
        assert asyncio.run(ha_tool._registry_area_map()) == ({}, {})
        assert asyncio.run(ha_tool._registry_area_map()) == ({}, {})
        assert _FakeSession.connect_calls == 1

    def test_failure_backoff_expires(self, monkeypatch):
        clock = [1000.0]
        _install_fake_ws(monkeypatch, _registry_frames(self.AREAS, [], self.ENTITIES, auth="auth_invalid"), clock)
        asyncio.run(ha_tool._registry_area_map())
        clock[0] += ha_tool._REGISTRY_FAILURE_TTL + 1
        _FakeSession.ws = _FakeWS(_registry_frames(self.AREAS, [], self.ENTITIES))
        assert asyncio.run(ha_tool._registry_area_map())[0] == {"light.ceiling": "Kitchen"}
        assert _FakeSession.connect_calls == 2

    def test_refused_registry_list_is_a_failure_not_an_empty_success(self, monkeypatch, caplog):
        frames = _registry_frames(self.AREAS, [], self.ENTITIES)
        frames[-1] = {"id": 3, "type": "result", "success": False, "error": {"code": "unauthorized"}}
        _install_fake_ws(monkeypatch, frames)
        with caplog.at_level(logging.WARNING, logger="homeassistant_plugin.tools"):
            assert asyncio.run(ha_tool._registry_area_map()) == ({}, {})
        assert "refused" in caplog.text
        assert ha_tool._REGISTRY_CACHE["ts"] == 0.0

    def test_plain_list_does_not_open_the_websocket(self, monkeypatch):
        _reset_filter_mirrors(monkeypatch)
        monkeypatch.setattr(ha_tool, "_api_json", AsyncMock(return_value=[]))
        pull = AsyncMock(return_value=({}, {}))
        monkeypatch.setattr(ha_tool, "_registry_area_map", pull)
        monkeypatch.setattr(ha_tool, "_get_config", lambda: ("http://ha.local:8123", "tok"))
        json.loads(ha_tool._handle_list_entities({"domain": "light"}))
        assert pull.await_count == 0
        json.loads(ha_tool._handle_list_entities({"area": "kitchen"}))
        assert pull.await_count == 1
