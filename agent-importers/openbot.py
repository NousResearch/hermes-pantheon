"""``hermes import-agent openbot``: bring OpenBot desktop agents (nightly-labs/openbot) into Hermes.

OpenBot keeps its agents in a local SQLite database (``openbot.db`` in the Electron user data folder)
and each agent's skills in its workspace (``~/OpenBot/Agents/<id>/.agents/skills``). This reads both,
read-only, turns every agent into the normalized bot shape :mod:`.grok_bot` imports, and lets that
importer do the writing:

- routines → Hermes cron jobs from their schedule (paused, like Grok Bot's: OpenBot keeps running its
  own copy until the user turns it off), keeping the routine's time zone;
- the agent's description (its role) → ``SOUL.md`` / memory entries; its memories → ``MEMORY.md``;
- workspace skills → ``skills/openbot-imports/<name>/`` (OpenBot's bundled skills are left out);
- MCP servers → ``mcp_servers`` (secret-looking env vars and headers stripped, as for every importer).

Chat history, provider accounts and the browser profile are not read.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Optional, Sequence

from .grok_bot import GrokBotImporter

AGENT = "openbot"
# Electron's userData for productName "OpenBot", per OS.
USER_DATA_DIRS = ("Library/Application Support/OpenBot", ".config/OpenBot", "AppData/Roaming/OpenBot")
DATABASE = "openbot.db"
_SKILL_DIRS = (".agents/skills", ".claude/skills")
_WEEKDAYS = "1-5"


def find_user_data(source: Optional[Path] = None) -> Optional[Path]:
    """The OpenBot user data folder (the one holding ``openbot.db``): ``source`` (that folder or the
    database file itself), else the OS default."""
    candidates = [source] if source is not None else [Path.home() / d for d in USER_DATA_DIRS]
    for candidate in candidates:
        if candidate.is_file() and candidate.name == DATABASE:
            return candidate.parent
        if (candidate / DATABASE).is_file():
            return candidate
    return None


def schedule_to_cron(schedule: dict[str, Any]) -> Optional[str]:
    """An OpenBot routine schedule (``RoutineSchedule`` in its contracts) as a schedule Hermes cron
    accepts; None for a shape with no equivalent."""
    kind = schedule.get("kind")

    def clock(time: str) -> str:
        hour, minute = str(time).split(":")
        return f"{int(minute)} {int(hour)}"

    simple = {
        "hourly": lambda s: f"{int(s['minute'])} * * * *",
        "daily": lambda s: f"{clock(s['time'])} * * *",
        "weekdays": lambda s: f"{clock(s['time'])} * * {_WEEKDAYS}",
        "weekly": lambda s: f"{clock(s['time'])} * * {int(s['weekday'])}",
        "monthly": lambda s: f"{clock(s['time'])} {int(s['day'])} * *",
        "interval": lambda s: f"every {int(s['amount'])}{str(s['unit'])[0]}",
        "custom": lambda s: str(s["expression"]).strip(),
        "once": lambda s: f"{s['date']}T{s['time']}",
        "advanced": _advanced_cron,
    }
    try:
        return simple[kind](schedule) if kind in simple else None
    except (KeyError, ValueError, TypeError):
        return None


def _advanced_cron(schedule: dict[str, Any]) -> Optional[str]:
    months = ",".join(str(int(m)) for m in schedule["months"])
    months = "*" if months == ",".join(str(m) for m in range(1, 13)) else months
    days, time = schedule["days"], schedule["time"]
    listed = ",".join(str(int(d)) for d in days.get("days", []))
    dom, dow = {"every-day": ("*", "*"), "days-of-week": ("*", listed),
                "days-of-month": (listed, "*")}[days["kind"]]
    if time["kind"] == "at-time":
        hour, minute = str(time["time"]).split(":")
        clock = f"{int(minute)} {int(hour)}"
    elif time["unit"] == "minutes":
        clock = f"*/{int(time['amount'])} *"
    else:
        clock = f"0 */{int(time['amount'])}"
    return f"{clock} {dom} {months} {dow}"


def _open_snapshot(database: Path, scratch: Path) -> sqlite3.Connection:
    """A private in-memory copy of the live database (OpenBot may be running, in WAL mode). A
    read-only open first; if SQLite cannot read the WAL that way, copy the files and read the copy."""
    snapshot = sqlite3.connect(":memory:")
    try:
        live = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
        try:
            live.backup(snapshot)
        finally:
            live.close()
    except sqlite3.Error:
        for suffix in ("", "-wal", "-shm"):
            part = database.with_name(database.name + suffix)
            if part.is_file():
                shutil.copy2(part, scratch / part.name)
        copy = sqlite3.connect(scratch / database.name)
        try:
            copy.backup(snapshot)
        finally:
            copy.close()
    snapshot.row_factory = sqlite3.Row
    return snapshot


def _rows(db: sqlite3.Connection, sql: str, *args) -> list[sqlite3.Row]:
    try:
        return db.execute(sql, args).fetchall()
    except sqlite3.OperationalError:  # a table this OpenBot version does not have yet
        return []


def _skills(workspace: Path) -> list[dict[str, Any]]:
    """The agent's own skill folders; OpenBot writes each to both folders, so one per name, and its
    bundled skills (``.openbot-managed.json``) stay behind."""
    found: dict[str, dict[str, Any]] = {}
    for rel in _SKILL_DIRS:
        root = workspace / rel
        if not root.is_dir():
            continue
        for folder in sorted(root.iterdir()):
            if (folder / "SKILL.md").is_file() and not (folder / ".openbot-managed.json").exists():
                found.setdefault(folder.name, {"name": folder.name, "path": str(folder)})
    return list(found.values())


def _mcp_servers(db: sqlite3.Connection) -> dict[str, Any]:
    servers: dict[str, Any] = {}
    for row in _rows(db, "SELECT * FROM projection_mcp_servers WHERE enabled = 1 ORDER BY position"):
        entry: dict[str, Any] = ({"url": row["url"], "headers": json.loads(row["headers_json"] or "{}")}
                                 if row["transport"] == "http" else
                                 {"command": row["command"], "args": json.loads(row["args_json"] or "[]"),
                                  "env": json.loads(row["env_json"] or "{}")})
        if row["transport"] != "http" and row["working_directory"]:
            entry["cwd"] = row["working_directory"]
        servers[row["name"]] = entry
    return servers


def read_agents(user_data: Path, scratch_parent: Path) -> list[dict[str, Any]]:
    """Every OpenBot agent as a normalized bot dict (see :class:`.grok_bot.GrokBotImporter`). The MCP
    servers are machine-wide in OpenBot, so they ride on the first agent. ``scratch_parent`` holds the
    database copy when a read-only open cannot be used, and only for the duration of the read."""
    scratch_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="openbot-import-", dir=scratch_parent) as scratch:
        db = _open_snapshot(user_data / DATABASE, Path(scratch))
    try:
        bots = []
        for row in _rows(db, "SELECT agent_id, agent_json FROM projection_agents ORDER BY sort_order"):
            agent = json.loads(row["agent_json"])
            agent_id = row["agent_id"]
            workspace = Path(agent.get("workspacePath") or Path.home() / "OpenBot" / "Agents" / agent_id)
            routines = []
            for r in _rows(db, "SELECT r.routine_id, r.name, r.instruction, r.active, r.timezone, t.schedule_json "
                               "FROM projection_agent_routines r LEFT JOIN projection_routine_triggers t "
                               "ON t.routine_id = r.routine_id WHERE r.agent_id = ? ORDER BY r.created_at",
                           agent_id):
                schedule = json.loads(r["schedule_json"]) if r["schedule_json"] else None
                cron = schedule_to_cron(schedule) if isinstance(schedule, dict) else None
                routines.append({
                    "name": r["name"], "instruction": r["instruction"], "timezone": r["timezone"],
                    "paused": not r["active"], "trigger": "schedule" if cron else "other",
                    "schedule_cron": cron or "",
                    "trigger_details": "" if cron else f"OpenBot schedule {json.dumps(schedule)}" if schedule
                    else "No schedule: run by hand, a script or a webhook in OpenBot"})
            memories = [m["text"] for m in _rows(
                db, "SELECT text FROM projection_agent_memories WHERE agent_id = ? ORDER BY created_at", agent_id)]
            bots.append({"name": agent.get("name") or agent_id, "title": agent.get("title") or "",
                         "instructions": agent.get("description") or "", "memories": memories,
                         "skills": _skills(workspace), "routines": routines})
        if bots:
            bots[0]["mcp_servers"] = _mcp_servers(db)
        return bots
    finally:
        db.close()


class OpenBotImporter(GrokBotImporter):
    """OpenBot's local agents through the shared bot importer."""

    AGENT = AGENT
    PRODUCT = "OpenBot"
    SKILL_CATEGORY = "openbot-imports"

    def __init__(self, user_data: Path, target_root: Path, execute: bool = False, overwrite: bool = False,
                 activate_routines: bool = False, bot_names: Sequence[str] = ()) -> None:
        super().__init__([user_data / DATABASE], target_root, execute=execute, overwrite=overwrite,
                         activate_routines=activate_routines, bot_names=bot_names)
        self.user_data = user_data

    def load_bots(self) -> list[tuple[Path, dict[str, Any]]]:
        database = self.user_data / DATABASE
        try:
            agents = read_agents(self.user_data, self.target_root / "cache")
        except (sqlite3.Error, OSError, ValueError) as exc:
            self.record("database", database, None, "error", f"Could not read OpenBot's database: {exc}")
            return []
        if not agents:
            self.record("agents", database, None, "skipped", "No agents in OpenBot")
        return [(database, agent) for agent in agents]
