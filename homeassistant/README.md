<p align="center"><img src="../assets/homeassistant.jpg" alt="Home Assistant: official Hermes Agent plugin" width="100%"></p>

# Home Assistant for Hermes Agent

> Part of the [Hermes Official Plugins](../README.md), Nous Research's official plugins for Hermes Agent. Formerly [`NousResearch/hermes-homeassistant`](https://github.com/NousResearch/hermes-homeassistant), which is archived; this directory carries its full history.

Official [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin for
[Home Assistant](https://www.home-assistant.io/). It provides two things:

1. **Gateway platform `homeassistant`**: subscribes to Home Assistant's WebSocket event bus and
   forwards `state_changed` events to the agent, with domain/entity filters and per-entity cooldowns.
   The agent's replies show up as Home Assistant persistent notifications titled "Hermes Agent".
   Cron jobs can deliver to `deliver=homeassistant[:target]` through the target's own notify service
   (`notify.mobile_app_my_phone` for `target=mobile_app_my_phone`), without a running gateway.
2. **Toolset `homeassistant`**: four tools the model can call to query and control devices over the
   REST API: `ha_list_entities`, `ha_get_state`, `ha_list_services`, `ha_call_service`.

This used to be built into Hermes core. It now ships as a standalone plugin with the same platform
name, toolset, tool names and schemas, env vars and config keys. **If you already used Home
Assistant with Hermes, you don't need to change anything:** `hermes update` (or the first start
after updating) installs and enables this plugin for you.

## Install

From the Hermes plugin catalog:

```bash
hermes plugins install homeassistant
```

The plugin's only Python dependency is `aiohttp` (`>=3.9,<4`, resolved under Hermes's own pins). It
needs a Hermes Agent release of `0.21.5` or newer.

## Configuration

### 1. Create a Long-Lived Access Token

1. Open your Home Assistant instance.
2. Go to your **Profile** (click your name in the sidebar).
3. Scroll to **Long-Lived Access Tokens**.
4. Click **Create Token**, name it something like "Hermes Agent", and copy it.

### 2. Environment variables

```bash
# ~/.hermes/.env

# Required: your Long-Lived Access Token
HASS_TOKEN=your-long-lived-access-token

# Optional: HA URL (default: http://homeassistant.local:8123)
HASS_URL=http://192.168.1.100:8123

# Optional: default notify target for cron deliver=homeassistant
# (a notify service target, e.g. mobile_app_my_phone)
HASS_HOME_CHANNEL=mobile_app_my_phone
```

| Variable | Required | Purpose |
|---|---|---|
| `HASS_TOKEN` | yes | Long-Lived Access Token. Enables both the tools and the gateway platform. |
| `HASS_URL` | no | Base URL of your instance. Default `http://homeassistant.local:8123`. Seeds `platforms.homeassistant.extra.url` when the platform is enabled from env. |
| `HASS_HOME_CHANNEL` | no | Default target for cron `deliver=homeassistant`. `HASS_HOME_CHANNEL_NAME` sets a display name. |
| `HASS_ENTITY_DENYLIST` | no | Comma-separated entity ID prefixes or globs that the tools always exclude (final veto): `ha_list_entities` hides them, `ha_get_state` refuses to read them and `ha_call_service` refuses to control them. |
| `HASS_ENTITY_ALLOWLIST` | no | Comma-separated entity ID prefixes or globs the tools are limited to (whitelist; unset = unrestricted), for listing, reading and `ha_call_service` control alike. |
| `HASS_MAX_ENTITIES` | no | Optional cap on `ha_list_entities` results (unset = no cap; a per-call `max` overrides it). |

### 3. Tools

The four tools only appear when `HASS_TOKEN` is set. Turn the `homeassistant` toolset on or off per
platform with `hermes tools`.

`ha_list_entities` keeps responses small on large installs. Besides `domain`, it takes `area`
(resolved against the Home Assistant area registry, so entities match even when their names do not
mention the room; falls back to friendly-name matching if the registry is unreachable), `name` (a
substring of friendly names and entity IDs), `entity_ids` (an exact list that supersedes the other
filters) and `max` (a per-call cap). Each entity includes its area and device when known. A result
cut by a cap is marked `truncated` with a note on how to narrow the query or get the rest.

#### Operator entity filters (optional)

Three environment variables constrain what the tools list, read and control without a code change. All are
opt-in: unset means the tools behave exactly as before.

| Variable | Effect |
|---|---|
| `HASS_ENTITY_DENYLIST` | Entity ID prefixes or globs (e.g. `office_thermostat_*,lock.*`) that `ha_list_entities` always excludes. A final veto: it wins even against the allowlist, and `ha_get_state` and `ha_call_service` refuse a matching entity before any network call. |
| `HASS_ENTITY_ALLOWLIST` | Same format; when non-empty, listing, reading and control are restricted to matching entities. |
| `HASS_MAX_ENTITIES` | Optional cap on how many entities `ha_list_entities` returns. Unset means no cap; a per-call `max` always overrides it. |

Patterns match exact IDs, family prefixes (`.` and `_` are interchangeable at the boundary, so
`office_thermostat_*` covers `office_thermostat.0_valve_position`) and shell-style globs
(`climate.*`, `*.valve*`). A prefix pattern starts at the start of the entity ID, so it does not
cover `climate.office_thermostat`; use a glob such as `*office_thermostat*` to catch every domain.

When either list is set, `ha_call_service` follows this rule, checked before anything is sent:

- Every entity the call names is checked: `entity_id`, and in `data` the keys `entity_id`, `target.entity_id`,
  `*_entity_id(s)`, `entities` and `snapshot_entities`. One denied entity, or (with an allowlist) one entity
  outside it, refuses the whole call. The error names the entity and the pattern that blocked it.
- Calls that target by `area_id`, `device_id`, `floor_id` or `label_id`, or with `entity_id: all`, are refused:
  Home Assistant resolves those on the server, where the lists cannot be checked. Target explicit entity IDs
  instead (`ha_list_entities` with `area` finds them).
- A call that names no entity is refused, because an entity service with no target can act on every entity of
  its domain (`light.turn_off` with no target turns off every light). Exceptions: `notify.*` and
  `persistent_notification.*` act on no entity and pass; `script.<name>` runs the entity `script.<name>` and is
  checked as that entity. Scenes, scripts and automations are entities (`scene.x`, `script.y`, `automation.z`),
  so `scene.turn_on` / `automation.trigger` need their `entity_id` and are filtered by it.

### 4. Gateway platform

```bash
hermes gateway
```

By default, **no events are forwarded**. Configure at least one of `watch_domains`, `watch_entities`
or `watch_all`, otherwise a warning is logged at startup and every state change is dropped. Settings
go under the platform's `extra` section in `~/.hermes/config.yaml`:

```yaml
platforms:
  homeassistant:
    enabled: true
    extra:
      watch_domains:
        - climate
        - binary_sensor
        - alarm_control_panel
        - light
      watch_entities:
        - sensor.front_door_battery
      ignore_entities:
        - sensor.uptime
        - sensor.cpu_usage
        - sensor.memory_usage
      cooldown_seconds: 30
      # Reuse one persistent notification for routine Hermes replies.
      # Set an empty string to let Home Assistant create a new one per reply.
      notification_id: hermes_agent
```

| Setting | Default | Description |
|---|---|---|
| `url` | `HASS_URL` | Home Assistant base URL |
| `watch_domains` | *(none)* | Only watch these entity domains (e.g. `climate`, `light`, `binary_sensor`) |
| `watch_entities` | *(none)* | Only watch these specific entity IDs |
| `watch_all` | `false` | Receive **all** state changes (not recommended for most setups) |
| `ignore_entities` | *(none)* | Always ignore these entities (applied before domain/entity filters) |
| `cooldown_seconds` | `30` | Minimum seconds between events for the same entity |
| `notification_id` | `hermes_agent` | Persistent-notification ID for agent replies; set to an empty string to create a new notification per reply |

Events come from the instance the adapter authenticated to with `HASS_TOKEN`, so they are not
subject to a user allowlist. The adapter reconnects automatically with 5s → 10s → 30s → 60s backoff.

## Security

`ha_call_service` refuses service domains that allow arbitrary code execution on the Home Assistant
host or requests to your local network. Home Assistant has no service-level access control, so this
blocklist is the only guard:

- `shell_command`: arbitrary shell commands
- `command_line`: sensors/switches that execute commands
- `python_script`: scripted Python execution
- `pyscript`: broader scripting integration
- `hassio`: add-on control, host shutdown/reboot
- `rest_command`: HTTP requests from the HA server (SSRF vector)

When `HASS_ENTITY_DENYLIST` or `HASS_ENTITY_ALLOWLIST` is set, `ha_call_service` also refuses to control
any entity the lists exclude, and any call whose targets it cannot check (area/device/floor/label targets,
`entity_id: all`, or no target); see [Operator entity filters](#operator-entity-filters-optional). For
defense in depth, also give Hermes a Home Assistant user whose token can only reach what it needs: the
lists guard this plugin's tools, not the token itself.

Domain and service names must match `^[a-z][a-z0-9_]*$` and entity IDs
`^[a-z_][a-z0-9_]*\.[a-z0-9_]+$`, which rules out path traversal into other REST endpoints
(e.g. `shell_command/../light`).

What the plugin does on your network: it talks only to the Home Assistant URL you configure (REST and
WebSocket) using your `HASS_TOKEN`. It stores no state and sends no telemetry.

## Development

Tests need a hermes-agent checkout on `PYTHONPATH` plus `pytest` and `pytest-asyncio`:

```bash
PYTHONPATH=/path/to/hermes-agent python -m pytest tests -q
```

## License

MIT, see [LICENSE](LICENSE). Copyright (c) 2026 Nous Research.
