"""``hermes import-agent openbot`` end to end: a real OpenBot database (its own table layout, WAL mode,
as the app leaves it) and agent workspace, through the plugin's parser into a real Hermes home and cron
store (no mocks)."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from agent_importers_plugin.cli import handle_cli, setup_cli
from agent_importers_plugin.openbot import schedule_to_cron

# The tables the importer reads, as OpenBot's openbot-database-schema.ts / mcp-schema.ts create them.
_SCHEMA = """
CREATE TABLE projection_agents (agent_id TEXT PRIMARY KEY, thread_id TEXT, model TEXT NOT NULL,
  updated_at TEXT, sort_order INTEGER NOT NULL, agent_json TEXT NOT NULL CHECK(json_valid(agent_json)),
  last_event_sequence INTEGER NOT NULL);
CREATE TABLE projection_agent_memories (memory_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, text TEXT NOT NULL,
  normalized_text TEXT NOT NULL, origin TEXT NOT NULL, source_turn_id TEXT, created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL, last_event_sequence INTEGER NOT NULL);
CREATE TABLE projection_agent_routines (routine_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, name TEXT NOT NULL,
  instruction TEXT NOT NULL, active INTEGER NOT NULL, timezone TEXT NOT NULL, created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL, last_event_sequence INTEGER NOT NULL);
CREATE TABLE projection_routine_triggers (trigger_id TEXT PRIMARY KEY, routine_id TEXT NOT NULL,
  schedule_json TEXT NOT NULL, next_run_at TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  last_event_sequence INTEGER NOT NULL, UNIQUE(routine_id));
CREATE TABLE projection_mcp_servers (mcp_server_id TEXT PRIMARY KEY, name TEXT NOT NULL, transport TEXT NOT NULL,
  enabled INTEGER NOT NULL, command TEXT NOT NULL, args_json TEXT NOT NULL, env_json TEXT NOT NULL,
  env_passthrough_json TEXT NOT NULL, working_directory TEXT NOT NULL, url TEXT NOT NULL,
  headers_json TEXT NOT NULL, position INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
"""


def _run(argv: list[str]) -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command").add_parser("import-agent")
    setup_cli(sub)
    sub.set_defaults(func=handle_cli)
    args = parser.parse_args(["import-agent", *argv])
    args.func(args)


def _fixture_openbot(home: Path) -> tuple[Path, sqlite3.Connection]:
    user_data = home / ".config" / "OpenBot"
    user_data.mkdir(parents=True)
    workspace = home / "OpenBot" / "Agents" / "agent-1"
    for skills, name, managed in ((".agents/skills", "triage", False), (".claude/skills", "triage", False),
                                  (".agents/skills", "openbot-bundled", True)):
        folder = workspace / skills / name
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\n\nSort the inbox.\n")
        if managed:
            (folder / ".openbot-managed.json").write_text("{}")
    db = sqlite3.connect(user_data / "openbot.db")
    db.execute("PRAGMA journal_mode = WAL")
    db.executescript(_SCHEMA)
    agent = {"name": "Inbox Keeper", "title": "Email", "description": "Keep the inbox at zero. Ask before sending.",
             "workspacePath": str(workspace)}
    db.execute("INSERT INTO projection_agents VALUES ('agent-1', NULL, 'm', NULL, 0, ?, 1)", (json.dumps(agent),))
    db.execute("INSERT INTO projection_agent_memories VALUES ('m1', 'agent-1', 'The user files receipts under Finance',"
               " 'x', 'manual', NULL, '2026-10-01', '2026-10-01', 1)")
    for rid, name, active, schedule in (
            ("r1", "Morning sweep", 1, {"kind": "weekdays", "time": "09:00"}),
            ("r2", "Every two hours", 0, {"kind": "interval", "amount": 2, "unit": "hours", "anchorAt": "2026-10-01T00:00:00Z"})):
        db.execute("INSERT INTO projection_agent_routines VALUES (?, 'agent-1', ?, 'Sweep the inbox.', ?, "
                   "'Europe/Berlin', ?, '2026-10-01', 1)", (rid, name, active, f"2026-10-0{rid[1]}"))
        db.execute("INSERT INTO projection_routine_triggers VALUES (?, ?, ?, '2026-11-02', '2026-10-01', '2026-10-01', 1)",
                   (f"t-{rid}", rid, json.dumps(schedule)))
    db.execute("INSERT INTO projection_mcp_servers VALUES ('s1', 'notes', 'stdio', 1, 'npx', '[\"notes-mcp\"]', "
               "'{\"NOTES_API_KEY\": \"sk-live-123\", \"NOTES_DIR\": \"/n\"}', '[]', '', '', '{}', 0, 'a', 'b')")
    db.commit()  # the connection stays open below, so the rows are still in the WAL as in a running app
    return user_data, db


def test_openbot_agents_become_paused_cron_jobs_skills_memories_and_mcp(tmp_path, capsys):
    """Read while OpenBot holds the database open: weekday and interval routines become paused cron
    jobs in the routine's zone, the agent's skill imports once (not the bundled one), the secret MCP env
    var is stripped, and the role lands in SOUL.md."""
    from hermes_cli.default_soul import DEFAULT_SOUL_MD
    home = tmp_path / ".hermes"
    (home / "SOUL.md").write_text(DEFAULT_SOUL_MD, encoding="utf-8")
    _user_data, live = _fixture_openbot(tmp_path)

    _run(["openbot", "-y"])  # found at ~/.config/OpenBot without --source
    live.close()

    jobs = {j["name"]: j for j in json.loads((home / "cron" / "jobs.json").read_text())["jobs"]}
    sweep, two_hourly = jobs["Inbox Keeper: Morning sweep"], jobs["Inbox Keeper: Every two hours"]
    assert sweep["schedule"]["expr"] == "0 9 * * 1-5" and sweep["enabled"] is False
    assert two_hourly["schedule"]["minutes"] == 120 and two_hourly["enabled"] is False
    skills = home / "skills" / "openbot-imports"
    assert sorted(p.name for p in skills.iterdir()) == ["triage"]
    assert "Finance" in (home / "memories" / "MEMORY.md").read_text(encoding="utf-8")
    assert "Ask before sending" in (home / "SOUL.md").read_text(encoding="utf-8")
    config = (home / "config.yaml").read_text(encoding="utf-8")
    assert "notes-mcp" in config and "NOTES_DIR" in config and "sk-live-123" not in config
    assert "mcp_servers.notes.env.NOTES_API_KEY" in capsys.readouterr().out


def test_every_openbot_schedule_shape_maps_to_a_hermes_schedule():
    cases = {
        json.dumps({"kind": "hourly", "minute": 15}): "15 * * * *",
        json.dumps({"kind": "daily", "time": "07:05"}): "5 7 * * *",
        json.dumps({"kind": "weekly", "weekday": 0, "time": "18:30"}): "30 18 * * 0",
        json.dumps({"kind": "monthly", "day": 1, "time": "09:00"}): "0 9 1 * *",
        json.dumps({"kind": "interval", "amount": 30, "unit": "minutes", "anchorAt": "x"}): "every 30m",
        json.dumps({"kind": "custom", "expression": "0 6 * * 1"}): "0 6 * * 1",
        json.dumps({"kind": "once", "date": "2027-01-05", "time": "09:30"}): "2027-01-05T09:30",
        json.dumps({"kind": "advanced", "months": [3, 6], "days": {"kind": "days-of-month", "days": [1, 15]},
                    "time": {"kind": "every", "amount": 15, "unit": "minutes"}}): "*/15 * 1,15 3,6 *",
        json.dumps({"kind": "advanced", "months": list(range(1, 13)), "days": {"kind": "days-of-week", "days": [1, 3]},
                    "time": {"kind": "at-time", "time": "08:00"}}): "0 8 * * 1,3",
    }
    assert {k: schedule_to_cron(json.loads(k)) for k in cases} == cases
    assert schedule_to_cron({"kind": "lunar"}) is None
