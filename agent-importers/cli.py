"""``hermes import-agent`` and ``hermes claw``: argparse trees and the preview → confirm → apply
flow. OpenClaw runs through :mod:`.openclaw`, imported only when an OpenClaw command runs."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Any, Optional

from .importer import AGENT_DEFAULT_DIRS, SUPPORTED_AGENTS, AgentImporter, detect_agents

logger = logging.getLogger(__name__)

HELP = "Import an OpenClaw, Claude Code, Codex CLI, Cursor or Grok Bot setup into Hermes"
DESCRIPTION = (
    "One-command import of another agent's setup into Hermes. Maps CLAUDE.md/AGENTS.md "
    "instructions, Cursor rules, permission allowlists, MCP servers, skills, and memories into their Hermes "
    "equivalents; Grok Bot routines become cron jobs. Always shows a preview before making changes. "
    "API keys and credentials are "
    "never imported — run 'hermes setup' for those. OpenClaw (the former 'hermes claw migrate') "
    "imports allowlisted secrets only with --migrate-secrets.")
CLAW_HELP = "OpenClaw migration tools"
CLAW_DESCRIPTION = ("Migrate settings, memories, skills, and API keys from OpenClaw to Hermes "
                    "('hermes claw migrate' is the same as 'hermes import-agent openclaw')")

OPENCLAW = "openclaw"
GROK_BOT = "grok-bot"
# Flags only `import-agent grok-bot` understands, as (dest, value meaning "not passed").
_GROK_ONLY_FLAGS = (("activate_routines", False), ("bot", None))
# Flags only `import-agent openclaw` understands, as (dest, value meaning "not passed").
_OPENCLAW_ONLY_FLAGS = (("preset", None), ("migrate_secrets", False), ("no_backup", False),
                        ("workspace_target", None), ("skill_conflict", None))

_ANSI = {"green": "\033[32m", "yellow": "\033[33m", "red": "\033[31m", "dim": "\033[2m",
         "magenta": "\033[35m"}


def _color(text: str, name: str) -> str:
    return f"{_ANSI[name]}{text}\033[0m" if sys.stdout.isatty() and not os.environ.get("NO_COLOR") else text


def _dirs_hint() -> str:
    dirs = ["~/.openclaw"] + [f"~/{AGENT_DEFAULT_DIRS[a]}" for a in SUPPORTED_AGENTS]
    return ", ".join(dirs[:-1]) + f" or {dirs[-1]}"


def add_openclaw_migrate_flags(parser, *, defaults: bool) -> None:
    """The ``claw migrate`` flags. ``defaults=False`` (import-agent) leaves the OpenClaw-only flags
    at None/False so a use with another agent can be refused; openclaw.py fills the defaults."""
    parser.add_argument(
        "--preset", choices=["user-data", "full"], default="full" if defaults else None,
        help="OpenClaw migration preset (default: full). Neither preset imports secrets — "
             "pass --migrate-secrets to include API keys.")
    parser.add_argument(
        "--migrate-secrets", action="store_true",
        help="OpenClaw: include allowlisted secrets (TELEGRAM_BOT_TOKEN, API keys, etc.). "
             "Required even under --preset full.")
    parser.add_argument(
        "--no-backup", action="store_true",
        help="OpenClaw: skip the pre-migration zip snapshot of the Hermes home (by default one "
             "restore-point archive is written to its backups/ dir before apply; restorable with "
             "'hermes import').")
    parser.add_argument(
        "--workspace-target", help="OpenClaw: absolute path to copy workspace instructions into")
    parser.add_argument(
        "--skill-conflict", choices=["skip", "overwrite", "rename"],
        default="skip" if defaults else None,
        help="OpenClaw: how to handle skill name conflicts (default: skip)")


def setup_cli(parser) -> None:
    """Attach the ``import-agent`` arguments (same spelling core shipped)."""
    parser.add_argument(
        "agent", nargs="?", choices=[OPENCLAW, *SUPPORTED_AGENTS, GROK_BOT],
        help=f"Which agent to import from (default: auto-detect {_dirs_hint()})")
    parser.add_argument(
        "--source", help=f"Path to the agent's config directory (default: {_dirs_hint()}); for "
                         "grok-bot, the export file the Bot wrote (or a folder of them)")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview only — stop after showing what would be imported")
    parser.add_argument(
        "--overwrite", action="store_true",
        help="Overwrite existing Hermes items on name conflicts (default: skip)")
    parser.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompts")
    parser.add_argument(
        "--sync", action="store_true",
        help="Re-import every previously imported source whose files changed since the last "
             "import (registered in HERMES_HOME/import-sync.json). Runs without prompts; "
             "combine with --dry-run to preview.")
    # `hermes setup` runs the import itself: no pointers back to setup or to flags it already chose.
    parser.add_argument("--from-setup", action="store_true", help=argparse.SUPPRESS)
    add_openclaw_migrate_flags(parser, defaults=False)
    parser.add_argument(
        "--activate-routines", action="store_true",
        help="Grok Bot: create the imported cron jobs active (default: paused, so they don't run "
             "alongside the Grok copies until you turn those off)")
    parser.add_argument(
        "--bot", action="append", metavar="NAME",
        help="Grok Bot: import only this Bot from an export holding several (repeatable)")


def handle_cli(args) -> None:
    import_agent_command(args)


def setup_claw_cli(parser) -> None:
    """Attach ``claw migrate`` / ``claw cleanup`` (the spelling core shipped before the move)."""
    sub = parser.add_subparsers(dest="claw_action")
    migrate = sub.add_parser(
        "migrate", help="Migrate from OpenClaw to Hermes (same as 'hermes import-agent openclaw')",
        description="Import settings, memories, skills, and API keys from an OpenClaw installation. "
                    "Always shows a preview before making changes.")
    migrate.add_argument("--source", help="Path to OpenClaw directory (default: ~/.openclaw)")
    migrate.add_argument("--dry-run", action="store_true",
                         help="Preview only — stop after showing what would be migrated")
    migrate.add_argument(
        "--overwrite", action="store_true",
        help="Overwrite existing files (default: refuse to apply when the plan has conflicts)")
    add_openclaw_migrate_flags(migrate, defaults=True)
    migrate.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompts")
    cleanup = sub.add_parser(
        "cleanup", aliases=["clean"], help="Archive leftover OpenClaw directories after migration",
        description="Scan for and archive leftover OpenClaw directories to prevent state fragmentation")
    cleanup.add_argument("--source", help="Path to a specific OpenClaw directory to clean up")
    cleanup.add_argument("--dry-run", action="store_true",
                         help="Preview what would be archived without making changes")
    cleanup.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompts")


def handle_claw_cli(args) -> None:
    from .openclaw import claw_command
    claw_command(args)


def _resolve_agent(args) -> Optional[str]:
    """The agent named on the command line, else the one detected setup. None (reason printed)
    when nothing is found, or several are and no --source picks one."""
    from hermes_cli.cli_output import print_error, print_info

    if args.agent:
        return args.agent
    from .grok_bot import detect_grok_bot
    from .openclaw import _find_openclaw_dirs
    detected = (([OPENCLAW] if _find_openclaw_dirs() else []) + detect_agents()
                + ([GROK_BOT] if detect_grok_bot() else []))
    if not detected:
        print()
        print_error(f"No supported agent setup found ({_dirs_hint()}).")
        print_info("Specify one explicitly: hermes import-agent claude-code --source /path")
        return None
    if len(detected) > 1 and args.source is None:
        print()
        print_info("Multiple agent setups detected: " + ", ".join(detected))
        print_info("Pick one: " + "   or   ".join(f"hermes import-agent {a}" for a in detected))
        return None
    return detected[0]


def _flags_passed(args, flags) -> list[str]:
    return ["--" + dest.replace("_", "-") for dest, unset in flags
            if getattr(args, dest, unset) != unset]


def import_agent_command(args) -> None:
    """Handle ``hermes import-agent``."""
    from hermes_cli.cli_output import (print_error, print_header, print_info, print_success,
                                       prompt_yes_no)
    from hermes_cli.config import get_config_path, load_config, save_config
    from hermes_constants import display_hermes_home, get_hermes_home

    if getattr(args, "sync", False):
        from .sync import sync_imported_agents
        sync_imported_agents(args)
        return

    agent = _resolve_agent(args)
    if agent is None:
        return
    if agent == OPENCLAW:
        from .openclaw import migrate_command
        migrate_command(args)
        return
    for owner, label, flags in ((OPENCLAW, "OpenClaw", _OPENCLAW_ONLY_FLAGS),
                                (GROK_BOT, "Grok Bot", _GROK_ONLY_FLAGS)):
        if agent != owner and (passed := _flags_passed(args, flags)):
            print()
            print_error(f"{', '.join(passed)} only apply to {label} imports; nothing was imported.")
            print_info(f"Use them with: hermes import-agent {owner} {' '.join(passed)} ...")
            return
    explicit_source, overwrite = args.source, args.overwrite
    from_setup = getattr(args, "from_setup", False)
    hermes_home = get_hermes_home()
    if agent == GROK_BOT:
        from .grok_bot import GrokBotImporter, resolve_export_files
        _print_banner()
        export_files = resolve_export_files(args)
        if not export_files:
            return
        source_label = ", ".join(str(p) for p in export_files)

        def make_importer(execute: bool):
            return GrokBotImporter(export_files, hermes_home.resolve(), execute=execute,
                                   overwrite=overwrite, activate_routines=args.activate_routines,
                                   bot_names=args.bot or ())
    else:
        source_dir = Path(explicit_source or Path.home() / AGENT_DEFAULT_DIRS[agent])
        _print_banner()
        if not source_dir.is_dir():
            print()
            print_error(f"Agent directory not found: {source_dir}")
            print_info(f"Specify a custom path: hermes import-agent {agent} --source /path/to/{AGENT_DEFAULT_DIRS[agent]}")
            return
        source_label = str(source_dir)

        def make_importer(execute: bool):
            return AgentImporter(agent, source_dir.resolve(), hermes_home.resolve(),
                                 execute=execute, overwrite=overwrite)

    print()
    print_header("Import Settings")
    print_info(f"Agent:       {agent}")
    print_info(f"Source:      {source_label}")
    print_info(f"Target:      {hermes_home}")
    print_info(f"Overwrite:   {'yes' if overwrite else 'no (skip conflicts)'}")
    print_info("Secrets:     never imported" + ("" if from_setup else " — run 'hermes setup' for credentials"))
    # Ensure config.yaml exists before the import tries to merge into it
    if not get_config_path().exists():
        save_config(load_config())

    def run_import(execute: bool, phase: str) -> Optional[dict[str, Any]]:
        """Run the importer; on failure print the error and return None."""
        try:
            return make_importer(execute).run()
        except Exception as e:
            print()
            print_error(f"Import{phase} failed: {e}")
            logger.debug(f"import-agent{phase} error", exc_info=True)
            return None

    # Phase 1: preview (always)
    preview = run_import(False, " preview")
    if preview is None:
        return
    summary = preview.get("summary", {})
    if summary.get("imported", 0) == 0 and summary.get("conflict", 0) == 0:
        print()
        print_info(f"Nothing to import from {agent}.")
        print_import_report(preview, dry_run=True, from_setup=from_setup)
        return
    print()
    print_header(f"Import Preview — {summary.get('imported', 0)} item(s) would be imported")
    print_info("No changes have been made yet. Review the list below:")
    print_import_report(preview, dry_run=True, from_setup=from_setup)
    if args.dry_run:
        return

    # Phase 2: confirm and execute
    print()
    if not args.yes:
        if not sys.stdin.isatty():
            print_info("Non-interactive session — preview only.")
            print_info(f"To execute, re-run with: hermes import-agent {agent} --yes")
            return
        if not prompt_yes_no("Proceed with import?", default=True):
            print_info("Import cancelled.")
            return
    report = run_import(True, "")
    if report is None:
        return
    print_import_report(report, dry_run=False, from_setup=from_setup)
    if agent == GROK_BOT:
        _print_grok_next_steps(report)
    else:
        _register_for_sync(agent, source_dir, hermes_home, overwrite, report)
    print()
    print_success("Import complete.")
    if from_setup:
        print_info("API keys and credentials were NOT imported.")
    else:
        print_info("API keys and credentials were NOT imported — run 'hermes setup' "
                   f"to configure providers, or add them to {display_hermes_home()}/.env.")


def _print_banner() -> None:
    print()
    print(_color("┌─────────────────────────────────────────────────────────┐", "magenta"))
    print(_color("│          ☤ Hermes — Import From Another Agent          │", "magenta"))
    print(_color("└─────────────────────────────────────────────────────────┘", "magenta"))


def _print_grok_next_steps(report: dict[str, Any]) -> None:
    """Grok exports are one-off files, so they are not sync sources; say how to switch over."""
    from hermes_cli.cli_output import print_info
    jobs = [i for i in report.get("items", []) if i.get("kind") == "routine" and i.get("status") == "imported"]
    paused = [i for i in jobs if i.get("paused")]
    if paused:
        print()
        print_info(f"{len(paused)} routine(s) were created PAUSED so they don't run twice. Once you turn "
                   "the routine off in Grok Bot, resume its copy here:")
        for item in paused:
            print_info(f"  hermes cron resume {item.get('job_id')}    # {item.get('destination')}")
    if jobs:
        print_info("Results land in Hermes' cron output; to have one sent to Telegram, Discord, Slack, "
                   "...: hermes cron edit <id> --deliver <platform>")


def _register_for_sync(agent: str, source_dir: Path, hermes_home: Path, overwrite: bool,
                       report: dict[str, Any]) -> None:
    from hermes_cli.cli_output import print_info

    from .sync import update_sync_manifest
    try:
        update_sync_manifest(agent, source_dir.resolve(), hermes_home.resolve(), overwrite, report)
        print_info("Source registered for sync — re-run 'hermes import-agent --sync' "
                   "any time to pull in changes.")
    except OSError as exc:
        logger.warning("Could not update import sync manifest: %s", exc)


def print_import_report(report: dict[str, Any], dry_run: bool, from_setup: bool = False) -> None:
    """Print a formatted per-item import report."""
    from hermes_cli.cli_output import print_header, print_info
    from hermes_constants import display_hermes_home

    print()
    print_header("Dry Run Results" if dry_run else "Import Results")
    if dry_run:
        print_info("No files were modified. This is a preview of what would happen.")
    print()
    # (status, colour, group heading, summary label)
    groups = (
        ("imported", "green", "✓ Would import" if dry_run else "✓ Imported",
         "would import" if dry_run else "imported"),
        ("conflict", "yellow", "⚠ Conflicts (skipped — use --overwrite to force)", "conflict(s)"),
        ("skipped", "dim", "─ Skipped", "skipped"),
        ("error", "red", "✗ Errors", "error(s)"))
    for status, col, label, _ in groups:
        group_items = [i for i in report.get("items", []) if i.get("status") == status]
        if not group_items:
            continue
        print(_color(f"  {label}:", col))
        for item in group_items:
            tail = ("→ " + str(item.get("destination") or "").replace(str(Path.home()), "~")
                    if status == "imported" else f" {item.get('reason', '')}")
            print(f"      {item.get('kind', 'unknown'):<22s} {tail}")
        print()
    if stripped := report.get("stripped_secrets"):
        print(_color("  ⚷ Secrets stripped (never imported):", "yellow"))
        for name in stripped:
            print(f"      {name}")
        print_info(f"Re-add them deliberately in {display_hermes_home()}/.env." if from_setup
                   else "Re-add credentials deliberately via 'hermes setup' or the profile's .env.")
        print()
    summary = report.get("summary", {})
    parts = [f"{summary[k]} {label}" for k, _, _, label in groups if summary.get(k)]
    if parts:
        print_info(f"Summary: {', '.join(parts)}")
