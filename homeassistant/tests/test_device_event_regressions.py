"""Synthetic regression coverage for device event correctness."""

from unittest.mock import AsyncMock, patch

import pytest

from gateway.config import PlatformConfig
from homeassistant_plugin.adapter import HomeAssistantAdapter


@pytest.mark.asyncio
async def test_noop_does_not_consume_cooldown():
    adapter = HomeAssistantAdapter(PlatformConfig(enabled=True, token="synthetic-token", extra={
        "watch_all": True, "cooldown_seconds": 30,
    }))
    adapter.handle_message = AsyncMock()
    entity = "switch.example"
    with patch("homeassistant_plugin.adapter.time.time", return_value=1000):
        await adapter._handle_ha_event({"data": {
            "entity_id": entity, "old_state": {"state": "off"},
            "new_state": {"state": "off", "attributes": {"friendly_name": "Example Device"}},
        }})
        assert entity not in adapter._last_event_time
        await adapter._handle_ha_event({"data": {
            "entity_id": entity, "old_state": {"state": "off"}, "new_state": {"state": "on"},
        }})
        await adapter._handle_ha_event({"data": {
            "entity_id": entity, "old_state": {"state": "on"}, "new_state": {"state": "off"},
        }})
    adapter.handle_message.assert_awaited_once()
    assert adapter._last_event_time[entity] == 1000


@pytest.mark.parametrize("domain", ["switch", "light", "fan", "binary_sensor"])
@pytest.mark.parametrize("old,new", [("on", "unavailable"), ("unknown", "off"), ("unavailable", "on")])
def test_nonbinary_transitions_preserve_actual_states(domain, old, new):
    message = HomeAssistantAdapter._format_state_change(
        f"{domain}.example", {"state": old}, {"state": new})
    assert f"changed from '{old}' to '{new}'" in message


@pytest.mark.parametrize("field", ["current_temperature", "temperature", "target_temp_low", "target_temp_high"])
def test_climate_temperature_attribute_changes(field):
    message = HomeAssistantAdapter._format_state_change(
        "climate.example", {"state": "heat", "attributes": {field: 20}},
        {"state": "heat", "attributes": {field: 21, "friendly_name": "Example Thermostat"}})
    assert f"{field}: 20 -> 21" in message
    assert "HVAC mode: 'heat'" in message


@pytest.mark.parametrize("state", ["heat", "unknown", "unavailable"])
def test_unrelated_climate_attribute_changes_are_ignored(state):
    assert HomeAssistantAdapter._format_state_change(
        "climate.example", {"state": state, "attributes": {"friendly_name": "Example A"}},
        {"state": state, "attributes": {"friendly_name": "Example B"}}) is None


@pytest.mark.parametrize("state", ["unknown", "unavailable"])
def test_unavailable_climate_temperature_changes_are_ignored(state):
    assert HomeAssistantAdapter._format_state_change(
        "climate.example", {"state": state, "attributes": {"temperature": 20}},
        {"state": state, "attributes": {"temperature": 21}}) is None
