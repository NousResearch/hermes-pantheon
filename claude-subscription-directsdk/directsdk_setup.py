"""Setup-time probes of the user's Claude CLI: login state and its own model picker.

Neither sends a Messages request: ``auth status`` reads the local credential store, and the
``initialize`` handshake enumerates the picker without one (the admission relay proves it by
counting upstream calls). With the ``claude_code_telemetry`` setting on (the default) the CLI
fetches its feature flags for the handshake, which is what makes its picker list every model. Anything unexpected returns ``None``/``False`` so callers
fall back to the pinned catalog rather than failing setup.
"""
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Mapping

try:
    from .admission import Admission
    from .model_catalog import MODEL_METADATA, native_model
except ImportError:
    from admission import Admission
    from model_catalog import MODEL_METADATA, native_model

INSTALL_HINT = ("Claude Code is not installed (no `claude` on PATH or in the usual install directories). Install it with "
                "`npm install -g @anthropic-ai/claude-code` or set CLAUDE_SUBSCRIPTION_DIRECTSDK_COMMAND to the binary.")
LOGIN_HINT = "Claude Code is installed but not logged in. Run `claude auth login`, then select this provider again."
LOGGED_OUT_HINT = ("Claude Code is installed but has no usable login in the environment Hermes runs it in. Run `claude auth login` "
                   "as the user Hermes runs as, set CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`) in Hermes' environment, "
                   "or point the claude_config_dir plugin setting (or CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR) "
                   "at a logged-in config directory, then try again.")


# Install prefixes probed after PATH, as core's anthropic_adapter does: a service or GUI launch (macOS
# LaunchAgent, Desktop-spawned backend) inherits a bare PATH that carries none of them.
_HOME_PREFIXES = (".local/bin", ".claude/local", "bin", ".npm-global/bin", ".bun/bin", ".volta/bin")
_SYSTEM_PREFIXES = ("/opt/homebrew/bin", "/usr/local/bin")


def _install_prefixes(env):
    home = env.get("USERPROFILE" if os.name == "nt" else "HOME")
    prefixes = [os.path.join(home, prefix) for prefix in _HOME_PREFIXES] if home else []
    return prefixes + ([] if os.name == "nt" else list(_SYSTEM_PREFIXES))


def _resolve(command, env):
    command = list(command) if command else [env.get("CLAUDE_SUBSCRIPTION_DIRECTSDK_COMMAND") or "claude"]
    head = command[0]
    exe = head if os.path.isabs(head) and os.access(head, os.X_OK) else shutil.which(head, path=env.get("PATH") or os.defpath)
    if not exe and head == "claude":
        # Only the default bare name: an explicit command or override is the user's choice, never second-guessed.
        exe = shutil.which(head, path=os.pathsep.join(_install_prefixes(env)))
    return ([exe] + command[1:]) if exe else None


# The plugin's settings live at plugins.entries.<manifest name>.settings in the active profile's
# config.yaml (plugin.yaml `config_schema`; Desktop → Capabilities → Plugins, or `hermes config set`).
PLUGIN_ID = "claude-subscription-directsdk-experimental"
TELEMETRY_SETTING = "claude_code_telemetry"
CONFIG_DIR_SETTING = "claude_config_dir"
# Any one of these turns off Claude Code's feature-flag fetch, and with it every model its picker
# only offers through a flag (#86: 11 models become 5). Off only when the user turns the setting off.
QUIET_TRAFFIC = {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_TELEMETRY": "1", "DISABLE_ERROR_REPORTING": "1"}
# Never wanted from a child the plugin starts, setting or not: a background self-update of the
# user's CLI mid-session, and the feedback path (/feedback, /bug, /share, Claude-drafted feedback)
# that uploads a session to Anthropic. Neither gates feature flags. DISABLE_FEEDBACK_COMMAND is the
# current name of DISABLE_BUG_COMMAND, which the CLI still accepts. CLAUDE_CODE_DISABLE_TERMINAL_TITLE
# also stops native's own session-title request (#116): under an inherited Agent SDK or non-CLI
# entrypoint, or a server flag, it races the turn for the one upstream request the relay admits.
ALWAYS_QUIET = {"DISABLE_AUTOUPDATER": "1", "DISABLE_FEEDBACK_COMMAND": "1", "CLAUDE_CODE_DISABLE_TERMINAL_TITLE": "1"}
# Native names its caller on every request (`cc_entrypoint` in the billing header, the user agent).
# Under `-p` it defaults to `sdk-cli`, and subscriptions draw the 5-hour window about 2x faster per
# token for `sdk-*` callers than for the interactive CLI or a client's own name (anthropics/claude-code#97074).
# An inherited host name (`sdk-py`, `claude-desktop`) would reach the child too, so it is overwritten.
# Off the `sdk-*` names native offers its artifact tool and adds a scratchpad line to every request;
# CLAUDE_CODE_DISABLE_ARTIFACT keeps the request identical to the `sdk-cli` one apart from the name.
CALLER_IDENTITY = {"CLAUDE_CODE_ENTRYPOINT": "hermes-agent", "CLAUDE_CODE_DISABLE_ARTIFACT": "1"}
_OFF = ("false", "0", "no", "off")


def _setting(key, default=None):
    """Read one plugin setting, with unreadable config or missing keys treated as not set.

    Read from the active profile's config on every call (``load_config_readonly`` resolves
    ``get_hermes_home()`` per call and caches on the file signature), so a multi-profile gateway
    honors each profile's choice and a change applies to the next spawn without a restart.
    """
    try:
        from hermes_cli.config import load_config_readonly
        value = load_config_readonly()
    except Exception:
        return default
    for segment in ("plugins", "entries", PLUGIN_ID, "settings", key):
        if not isinstance(value, Mapping) or segment not in value:
            return default
        value = value[segment]
    return value


def telemetry_enabled():
    """The active profile's ``claude_code_telemetry`` plugin setting, default on."""
    value = _setting(TELEMETRY_SETTING, True)
    if isinstance(value, str):
        return value.strip().lower() not in _OFF
    return value is not False and value != 0


def apply_config_dir(env):
    """Select the active profile's Claude config directory on a child env, in place."""
    override = env.pop("CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR", None)
    setting = _setting(CONFIG_DIR_SETTING)
    config = (setting.strip() if isinstance(setting, str) else "") or override or env.get("CLAUDE_CONFIG_DIR")
    if config:
        env["CLAUDE_CONFIG_DIR"] = os.path.expanduser(config)
    return env


def apply_traffic_policy(env, telemetry=None):
    """Set the plugin's Claude Code traffic flags on a child ``env`` (in place) and return it.

    Never removes anything: a flag the user exported in Hermes' own environment
    (``DISABLE_TELEMETRY``, ``DO_NOT_TRACK``, ...) still reaches the child with the setting on.
    The caller name is the one value replaced, whatever the parent set.
    """
    env.update(ALWAYS_QUIET, **CALLER_IDENTITY)
    if not (telemetry_enabled() if telemetry is None else telemetry):
        env.update(QUIET_TRAFFIC)
    return env


def _child_env(env):
    child = dict(env)
    # Drop API-key credentials inherited from Hermes' environment so `claude auth
    # status` reports the subscription login rather than an API-key one.  Endpoint
    # and backend redirects are preserved (they change where requests go).
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_FOUNDRY_API_KEY"):
        child.pop(key, None)
    apply_config_dir(child)
    return apply_traffic_policy(child)


def _plan_label(raw):
    """``"pro"`` (auth status) and ``"Claude Pro"`` (handshake) name the same plan."""
    raw = str(raw or "").strip()
    if not raw:
        return ""
    return raw if raw.lower().startswith("claude") else "Claude " + raw.replace("_", " ").title()


def setup_status(command=None, env=None, timeout=20):
    """``{available, logged_in, plan, detail, login_command}`` from the CLI's own ``auth status``."""
    env = dict(env if env is not None else os.environ)
    resolved = _resolve(command, env)
    if resolved is None:
        return {"available": False, "logged_in": False, "plan": "", "detail": INSTALL_HINT, "login_command": None}
    login_command = resolved + ["auth", "login"]
    try:
        run = subprocess.run(resolved + ["auth", "status"], env=_child_env(env), stdin=subprocess.DEVNULL,
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        auth = json.loads(run.stdout) if run.stdout.strip().startswith("{") else {}
    except (OSError, ValueError, subprocess.SubprocessError):
        auth = {}
    logged_in = auth.get("loggedIn") is True
    plan = str(auth.get("subscriptionType") or "")
    detail = "" if logged_in else LOGIN_HINT
    return {"available": True, "logged_in": logged_in, "plan": _plan_label(plan), "detail": detail,
            "login_command": login_command}


def discover_models(command=None, env=None, timeout=40):
    """The account's live picker as ``[{id, label, note, upstream_requests}]`` in Hermes route ids,
    or ``None`` when the CLI is missing, logged out, or the handshake fails."""
    env = dict(env if env is not None else os.environ)
    resolved = _resolve(command, env)
    # Logged out, the handshake still answers with a generic default list; only a signed-in
    # account's picker reflects its entitlements.
    if resolved is None or not setup_status(command=resolved, env=env, timeout=timeout)["logged_in"]:
        return None
    child = _child_env(env)
    gate = Admission("https://api.anthropic.com", timeout)
    try:
        child["ANTHROPIC_BASE_URL"] = gate.url
        argv = resolved + ["-p", "--model", "sonnet", "--input-format", "stream-json", "--output-format", "stream-json",
                           "--verbose", "--tools", "", "--setting-sources", "", "--strict-mcp-config",
                           "--mcp-config", '{"mcpServers":{}}', "--disable-slash-commands", "--no-session-persistence"]
        handshake = json.dumps({"type": "control_request", "request_id": "hermes-picker",
                                "request": {"subtype": "initialize"}}) + "\n"
        with tempfile.TemporaryDirectory(prefix="claude-directsdk-picker-") as cwd:
            run = subprocess.run(argv, input=handshake, env=child, cwd=cwd, capture_output=True,
                                 text=True, encoding="utf-8", errors="replace", timeout=timeout)
        rows = [json.loads(line) for line in run.stdout.splitlines() if line.startswith("{")]
        response = next(r["response"] for r in rows if r.get("type") == "control_response")
        native = response.get("response", {}).get("models") or []
        upstream = int(bool(gate.used))
    except (OSError, ValueError, StopIteration, KeyError, subprocess.SubprocessError):
        return None
    finally:
        gate.close()
    if upstream or not native:
        return None
    # Pro / Team-standard seats bill Fable to usage credits from the first request; the CLI only
    # says so at request time, so apply the documented plan rule here.
    plan = str((response.get("response", {}).get("account") or {}).get("subscriptionType") or "").lower()
    credit_billed_on_plan = {"claude-fable-5-1"} if plan and "max" not in plan else set()
    # The pinned table adds metadata (1M route, window, aliases) to the models it knows; it never
    # decides visibility, so a model the CLI ships before the table does is listed the same day.
    announced = [str(row.get("resolvedModel") or row.get("value") or "") for row in native]
    # An unpinned model gets [1m] only from the CLI itself; offered both ways it collapses onto [1m]
    # like the pinned 1M models (behind the relay the suffix is the client-side window selection, #8).
    long_context = {model.removesuffix("[1m]") for model in announced if model.endswith("[1m]")}
    routes = {}
    for row, model in zip(native, announced):
        base = model.removesuffix("[1m]")
        if not base:
            continue
        route = native_model(base)
        pinned = route in MODEL_METADATA
        if not pinned:
            # A `value` the CLI did not resolve (`default`, `best`) is an alias row, not a model.
            if not row.get("resolvedModel"):
                continue
            if base in long_context:
                route = base + "[1m]"
        # Model names, not the native "Default (recommended)" alias row.
        label = str(row.get("description") or "").split("·")[0].strip() or route
        entry = routes.setdefault(route, {"id": route, "label": label, "note": "" if pinned else "unpinned",
                                          "upstream_requests": upstream})
        if "usage credit" in str(row.get("description") or "").lower() or base in credit_billed_on_plan:
            # The billing warning reads first; an unpinned row keeps its marker after it.
            entry["note"] = "usage credits" if pinned else "usage credits · unpinned"
    if not routes:
        return None
    # The live picker names only each family's current model, yet the account still runs the older
    # pinned ones (Opus 5, Opus 4.8, ...). Replacing the static catalog with the picker would hide
    # them, so every pinned route the CLI did not announce is appended after the advertised rows.
    for route, meta in MODEL_METADATA.items():
        if route in routes:
            continue
        canonical = meta["canonical_model"]
        routes[route] = {"id": route, "label": _catalog_label(canonical),
                         "note": "usage credits" if canonical in credit_billed_on_plan else "",
                         "upstream_requests": upstream}
    return list(routes.values())


def fetch_usage(command=None, env=None, timeout=8):
    """``(rate_limits, None)`` from Claude Code's own ``get_usage`` control request, else ``(None, reason)``.

    The CLI fetches the plan limits with the login it already holds, so the plugin never reads or
    sends that token (catalog rule 11) and ``auth.adopt_external_logins: false`` keeps meaning what
    it says. It follows the same per-profile config dir as every native spawn, runs behind an unused
    admission gate (zero inference), and fits under core's 10 s ``/usage`` hook deadline.
    """
    env = dict(env if env is not None else os.environ)
    resolved = _resolve(command, env)
    if resolved is None:
        return None, INSTALL_HINT
    child = _child_env(env)
    gate = Admission("https://api.anthropic.com", timeout)
    try:
        child["ANTHROPIC_BASE_URL"] = gate.url
        argv = resolved + ["-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                           "--tools", "", "--setting-sources", "", "--strict-mcp-config", "--mcp-config",
                           '{"mcpServers":{}}', "--disable-slash-commands", "--no-session-persistence"]
        request = json.dumps({"type": "control_request", "request_id": "hermes-usage",
                              "request": {"subtype": "get_usage", "skip_behaviors": True}}) + "\n"
        with tempfile.TemporaryDirectory(prefix="claude-directsdk-usage-") as cwd:
            run = subprocess.run(argv, input=request, env=child, cwd=cwd, capture_output=True,
                                 text=True, encoding="utf-8", errors="replace", timeout=timeout)
        rows = [json.loads(line) for line in run.stdout.splitlines() if line.startswith("{")]
        response = next(r["response"] for r in rows if r.get("type") == "control_response"
                        and r.get("response", {}).get("request_id") == "hermes-usage")
    except subprocess.TimeoutExpired:
        return None, "Claude Code did not answer in time"
    except (OSError, ValueError, StopIteration, KeyError, AttributeError, subprocess.SubprocessError):
        return None, "Claude Code gave no usage answer (needs a version with get_usage)"
    finally:
        gate.close()
    if response.get("subtype") != "success":
        return None, "Claude Code refused the usage request (needs a version with get_usage)"
    body = response.get("response") if isinstance(response.get("response"), dict) else {}
    limits = body.get("rate_limits")
    if isinstance(limits, dict):
        return limits, None
    if not body.get("rate_limits_available"):
        return None, "no Claude subscription login found (run `claude` and log in)"
    if child.get("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"):
        # Measured on 2.1.302: with nonessential traffic off the CLI answers rate_limits: null.
        return None, "Claude Code skips the usage fetch with nonessential traffic off (claude_code_telemetry)"
    return None, "Claude Code returned no plan limits"


def _catalog_label(model):
    """``claude-opus-4-8`` -> ``Opus 4.8``; a trailing snapshot date is dropped."""
    parts = model.removeprefix("claude-").split("-")
    if parts and len(parts[-1]) == 8 and parts[-1].isdigit():
        parts = parts[:-1]
    family, version = parts[0], [p for p in parts[1:] if p.isdigit()]
    return " ".join(filter(None, [family.capitalize(), ".".join(version)]))
