"""Claude Subscription DirectSDK (Experimental) — standalone Hermes model-provider registration."""
from datetime import datetime, timezone
import logging
import math
import os
import shutil

from providers import register_provider
from providers.base import ProviderProfile

# Dual import: the Hermes loader imports this directory as a package; the flat test path does not.
try:
    from .model_catalog import ALIASES, MODEL_METADATA, MODEL_CAPABILITIES, native_model
    from .directsdk_setup import INSTALL_HINT, _resolve
except ImportError:
    from model_catalog import ALIASES, MODEL_METADATA, MODEL_CAPABILITIES, native_model
    from directsdk_setup import INSTALL_HINT, _resolve

logger = logging.getLogger(__name__)

# Core checks `process_command` with a PATH-only `shutil.which` before this plugin is asked, so a CLI found only in
# an install prefix is handed over as its absolute path; a PATH hit keeps the bare name and follows PATH.
_found = _resolve(None, os.environ)
_process_command = 'claude' if _found is None or shutil.which('claude') else _found[0]


# A `limits[]` kind maps to a fixed label; a model-scoped limit reads "<model> week" (Fable's own
# weekly limit); any other kind keeps its raw name so a new limit still shows.
LIMIT_LABELS = {'session': 'Current session', 'weekly_all': 'Current week'}
# The older top-level windows, for a response without `limits` (core's Anthropic fetcher reads these).
LEGACY_WINDOWS = (('five_hour', 'Current session'), ('seven_day', 'Current week'),
                  ('seven_day_opus', 'Opus week'), ('seven_day_sonnet', 'Sonnet week'))


def _percent(value, fraction=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return value * 100.0 if fraction and value <= 1 else float(value)


def _reset_at(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def usage_windows(payload):
    """Plan-limits body (Claude Code's `get_usage` `rate_limits`, the `/api/oauth/usage` shape) -> (windows, detail lines). Untrusted shapes degrade, never raise."""
    from agent.account_usage import AccountUsageWindow
    limits = payload.get('limits')
    limits = [entry for entry in limits if isinstance(entry, dict)] if isinstance(limits, list) else []
    if limits:
        windows = []
        for entry in limits:
            kind = entry.get('kind')
            scope = entry.get('scope') if isinstance(entry.get('scope'), dict) else {}
            model = scope.get('model') if isinstance(scope.get('model'), dict) else {}
            name = model.get('display_name')
            label = (LIMIT_LABELS.get(kind) if isinstance(kind, str) else None) or (
                f'{name} week' if isinstance(name, str) and name else str(kind or 'unknown'))
            windows.append(AccountUsageWindow(label=label, used_percent=_percent(entry.get('percent')),
                                              reset_at=_reset_at(entry.get('resets_at'))))
        return windows, []
    windows = []
    for key, label in LEGACY_WINDOWS:
        window = payload.get(key)
        used = _percent(window.get('utilization'), fraction=True) if isinstance(window, dict) else None
        if used is not None:
            windows.append(AccountUsageWindow(label=label, used_percent=used, reset_at=_reset_at(window.get('resets_at'))))
    extra = payload.get('extra_usage') if isinstance(payload.get('extra_usage'), dict) else {}
    spent, cap = _percent(extra.get('used_credits')), _percent(extra.get('monthly_limit'))
    details = [f"Extra usage: {spent:.2f} / {cap:.2f} {extra.get('currency') or 'USD'}"] if (
        extra.get('is_enabled') and spent is not None and cap is not None) else []
    return windows, details


class ClaudeOAuthDirectSDKProfile(ProviderProfile):
    model_metadata = MODEL_METADATA

    def get_model_context_length(self, model):
        route = native_model(model)
        pinned = self.model_metadata.get(route, {}).get('context_window')
        # Unpinned: behind the relay native Claude Code runs a plain id within its 200K default, and
        # Hermes' own family guess (claude-opus-5-5 -> 1M before it was pinned) would outgrow that.
        # A [1m] id stays unreported: no Hermes guess exceeds the 1M native budget, nor is it promised.
        return pinned or (None if route.endswith('[1m]') else 200_000)

    def get_usage_cost(self, model, usage):
        from decimal import Decimal, InvalidOperation
        from agent.usage_pricing import CostResult

        native = (usage.raw_usage or {}).get('native_cost') or {}
        unknown = CostResult(amount_usd=None, status='unknown', source='none', label='n/a',
                             notes=('native final list-price accounting unavailable; subscription invoice unknown',))
        amount = native.get('total_cost_usd')
        models = native.get('modelUsage') or {}
        if isinstance(amount, bool) or not models or any(row.get('costBasis') != 'list' for row in models.values()):
            return unknown
        try:
            amount = Decimal(str(amount))
        except InvalidOperation:
            return unknown
        if not amount.is_finite() or amount < 0:
            return unknown
        # Subscription-included (Claude Max): out-of-pocket is $0, and `amount` is native's
        # LIST-PRICE equivalent — an allowance gauge, never an invoice. It rides
        # `list_price_usd` so spend consumers summing amount_usd stay truthful.
        included_notes = ('subscription-included (Claude Max); list_price_usd is the native API '
                          'list-price equivalent, not an invoice',)
        try:
            return CostResult(amount_usd=Decimal(0), status='included', source='subscription_included',
                              label='included', list_price_usd=amount, notes=included_notes)
        except TypeError:
            # Older core CostResult lacks list_price_usd: still included/$0, gauge dropped.
            return CostResult(amount_usd=Decimal(0), status='included', source='subscription_included',
                              label='included', notes=included_notes)

    def create_client(self, **client_kwargs):
        try:
            from .directsdk import Client
        except ImportError:
            from directsdk import Client
        return Client(**client_kwargs)

    def fetch_models(self, **kwargs):
        # No HTTP /models endpoint: the account's own picker (CLI `initialize` handshake) is the
        # live list for /model, the Desktop picker and `hermes model`; None degrades to the catalog.
        rows = self.discover_models(**kwargs)
        return [row["id"] for row in rows] if rows else None

    def setup_status(self, **kwargs):
        try:
            from .directsdk_setup import setup_status
        except ImportError:
            from directsdk_setup import setup_status
        return setup_status(**kwargs)

    def discover_models(self, **kwargs):
        try:
            from .directsdk_setup import discover_models
        except ImportError:
            from directsdk_setup import discover_models
        return discover_models(**kwargs)

    def build_api_kwargs_extras(self, *, reasoning_config=None, **context):
        extra = {'reasoning': dict(reasoning_config)} if reasoning_config else {}
        # Hermes' auxiliary calls (call_llm: compression summaries, titles) reach this hook without the
        # conversation context its chat path always passes (session_id). Mark them so the client can skip
        # a message cache write that no later request would read.
        auxiliary = context.get('task') is not None or 'session_id' not in context
        return extra, ({'hermes_auxiliary': True} if auxiliary else {})

    def fetch_account_usage(self, *, base_url=None, api_key=None):
        """`/usage` plan limits, asked of Claude Code itself; every failure is an `Unavailable:` line, never a raise."""
        from agent.account_usage import AccountUsageSnapshot
        try:
            from .directsdk_setup import fetch_usage
        except ImportError:
            from directsdk_setup import fetch_usage

        limits, reason = fetch_usage()
        windows, details = usage_windows(limits) if limits is not None else ((), ())
        return AccountUsageSnapshot(provider=self.name, source='claude_code_get_usage', fetched_at=datetime.now(timezone.utc),
                                    title='Claude plan limits', windows=tuple(windows), details=tuple(details),
                                    unavailable_reason=reason)


def classify_api_error(error, **_):
    # Hermes' stale-call watchdog stops a hung call through Client.cancel(), so its kill comes back as our cancel
    # error, which Hermes would retry as unknown: classify it as the timeout it is. A user interrupt never arrives
    # as this error; Hermes marks its request cancelled first, drops ours and raises InterruptedError.
    if isinstance(error, RuntimeError) and str(error) == 'Claude request cancelled':
        return {'reason': 'timeout'}
    return None


profile = ClaudeOAuthDirectSDKProfile(
    name='claude-subscription-directsdk-experimental',
    display_name='Claude Subscription DirectSDK (Experimental)',
    description='Claude Subscription DirectSDK (Experimental) (Claude Pro/Max subscription via your Claude Code login; Hermes owns tools)',
    api_mode='chat_completions',
    auth_type='external_process',
    supports_health_check=False,
    # Per-model only: the profile-wide supports_vision would also flip computer_use screenshots
    # for unpinned ids to native, where the executor still treats them as text-only.
    model_capabilities=MODEL_CAPABILITIES,
    native_reasoning_details_type='claude-subscription-directsdk-experimental.native_assistant',
    env_vars=(),
    base_url='process://claude-subscription-directsdk-experimental',
    process_command=_process_command,
    process_args=(),
    process_command_env_vars=('CLAUDE_SUBSCRIPTION_DIRECTSDK_COMMAND',),
    default_aux_model='claude-sonnet-5[1m]',
    fallback_models=tuple(MODEL_METADATA),
    model_aliases={alias: native_model(alias) for alias in ALIASES},
    # A dataclass field (Hermes >= 0.21.4), so a method on the subclass would be shadowed by its None default.
    classify_api_error=classify_api_error,
)
# Attribute assignment keeps the plugin loadable on hosts predating this capability.
# Newer hosts use it to avoid buffering a healthy native stream behind a stale-call timer.
profile.supports_streaming = True
register_provider(profile)

# The provider stays registered when Claude Code is missing so `hermes model` can show the
# install hint; the request path (`directsdk.Client`) refuses with the same message.
if _found is None:
    logger.warning("%s: %s", profile.display_name, INSTALL_HINT)
