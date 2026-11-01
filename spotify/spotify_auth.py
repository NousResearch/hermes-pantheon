"""Spotify OAuth (loopback PKCE): login, refresh, runtime credentials, status and logout.

Formerly ``hermes_cli/auth_spotify.py`` in hermes-agent core. Tokens stay where core kept them
(``providers.spotify`` in the profile's ``auth.json``, written under the auth-store lock), so an
install that logged in while Spotify was built into core keeps its login, and other plugins that
read that state (``spotify-desktop``) keep working.

Hermes internals are imported lazily, inside the functions, from the modules that define them.
"""

from __future__ import annotations

import logging
import os
import ssl
import uuid
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode, urlparse

logger = logging.getLogger(__name__)

PROVIDER = "spotify"
DEFAULT_ACCOUNTS_BASE_URL = "https://accounts.spotify.com"
DEFAULT_API_BASE_URL = "https://api.spotify.com/v1"
# Spotify's redirect-URI rules allow plain http on a loopback IP literal
# (https://developer.spotify.com/documentation/web-api/concepts/redirect_uri): "Use HTTPS for
# your redirect URI, unless you are using a loopback address, when HTTP is permitted." So the
# default stays http, which is also what existing app registrations use. An https URI is
# supported when the user configures one — the listener then serves TLS with a self-signed
# certificate — for dashboards that only accept the https form.
DEFAULT_REDIRECT_URI = "http://127.0.0.1:43827/spotify/callback"
DOCS_URL = "https://hermes-agent.nousresearch.com/docs/user-guide/features/spotify"
DASHBOARD_URL = "https://developer.spotify.com/dashboard"
ACCESS_TOKEN_REFRESH_SKEW_SECONDS = 120
DEFAULT_SCOPE = " ".join((
    "user-modify-playback-state", "user-read-playback-state", "user-read-currently-playing",
    "user-read-recently-played", "playlist-read-private", "playlist-read-collaborative",
    "playlist-modify-public", "playlist-modify-private", "user-library-read", "user-library-modify",
))


def _err(message: str, code: Optional[str] = None, *, relogin: bool = False):
    from hermes_cli.auth_constants import AuthError
    return AuthError(message, provider=PROVIDER, code=code, relogin_required=relogin)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _scope_string(raw_scope: Optional[str] = None) -> str:
    """Requested scope, whitespace-normalized and de-duplicated (order kept)."""
    return " ".join(dict.fromkeys((raw_scope or DEFAULT_SCOPE).split()))


def _setting(
    state: Optional[dict[str, Any]], state_key: str, env_vars: tuple[str, ...], default: str, *,
    explicit: Optional[str] = None, strip_slash: bool = False,
) -> str:
    """First non-empty of explicit arg, env vars (``.env`` aware), stored state, then *default*."""
    from hermes_cli.config import get_env_value
    candidates = (
        explicit, *(get_env_value(var) for var in env_vars),
        state.get(state_key) if isinstance(state, dict) else None, default,
    )
    for candidate in candidates:
        cleaned = _clean(candidate)
        if strip_slash:
            cleaned = cleaned.rstrip("/")
        if cleaned:
            return cleaned
    return default


def _client_id(explicit: Optional[str] = None, state: Optional[dict[str, Any]] = None) -> str:
    client_id = _setting(state, "client_id", ("HERMES_SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_ID"), "", explicit=explicit)
    if client_id:
        return client_id
    raise _err("Spotify client_id is required. Set HERMES_SPOTIFY_CLIENT_ID or pass --client-id.",
               "spotify_client_id_missing")


def _redirect_uri(explicit: Optional[str] = None, state: Optional[dict[str, Any]] = None) -> str:
    return _setting(state, "redirect_uri", ("HERMES_SPOTIFY_REDIRECT_URI", "SPOTIFY_REDIRECT_URI"),
                    DEFAULT_REDIRECT_URI, explicit=explicit)


def _api_base_url(state: Optional[dict[str, Any]] = None) -> str:
    return _setting(state, "api_base_url", ("HERMES_SPOTIFY_API_BASE_URL",), DEFAULT_API_BASE_URL, strip_slash=True)


def _accounts_base_url(state: Optional[dict[str, Any]] = None) -> str:
    return _setting(state, "accounts_base_url", ("HERMES_SPOTIFY_ACCOUNTS_BASE_URL",), DEFAULT_ACCOUNTS_BASE_URL,
                    strip_slash=True)


def _build_authorize_url(
    *, client_id: str, redirect_uri: str, scope: str, state: str, code_challenge: str, accounts_base_url: str,
) -> str:
    query = urlencode({
        "client_id": client_id, "response_type": "code", "redirect_uri": redirect_uri,
        "scope": scope, "state": state, "code_challenge_method": "S256", "code_challenge": code_challenge,
    })
    return f"{accounts_base_url}/authorize?{query}"


def _validate_redirect_uri(redirect_uri: str) -> tuple[str, int, str]:
    parsed = urlparse(redirect_uri)
    host = parsed.hostname or ""
    problem = (
        # Both schemes are valid on a loopback IP literal: http is what Spotify documents and
        # what the default uses, https is honoured when the app was registered that way.
        "must use http:// or https:// on 127.0.0.1/localhost." if parsed.scheme not in {"http", "https"}
        else "must point to localhost or 127.0.0.1." if host not in {"127.0.0.1", "localhost"}
        else "must include an explicit localhost port." if not parsed.port
        else None
    )
    if problem:
        raise _err(f"Spotify PKCE redirect_uri {problem}", "spotify_redirect_invalid")
    return host, parsed.port, parsed.path or "/"


def _server_ssl_context_class():
    """The stdlib ``SSLContext``, even when a TLS-verifying shim replaced ``ssl.SSLContext``.

    Hermes calls ``truststore.inject_into_ssl()`` at startup, which swaps ``ssl.SSLContext``
    for a subclass that verifies the *peer* certificate. That is right for client sockets and
    wrong for a server one: there is no peer certificate yet, and the handshake dies with
    ``AttributeError: 'NoneType' object has no attribute 'get_unverified_chain'``. Serving TLS
    needs no trust decisions, so use the original class the shim keeps around.
    """
    cls = ssl.SSLContext
    if getattr(cls, "__module__", "").startswith("truststore"):
        try:
            from truststore._ssl_constants import _original_SSLContext as cls
        except Exception:  # pragma: no cover - defensive: shim without its saved class
            cls = cls.__mro__[1]
    return cls


def _loopback_tls_context() -> ssl.SSLContext:
    """TLS context for the loopback listener, with a self-signed certificate for 127.0.0.1.

    Used when the configured redirect URI is https (the default stays http, which Spotify permits
    on a loopback IP literal). The loopback flow still requires an IP literal, so the CLI
    terminates TLS itself. The certificate is generated once and cached
    under ``$HERMES_HOME/cache/spotify-loopback/`` (key mode 0600); it carries
    ``SAN IP:127.0.0.1, DNS:localhost`` so the browser offers the usual "proceed" warning
    instead of refusing outright.
    """
    from hermes_constants import get_hermes_home

    cache_dir = Path(get_hermes_home()) / "cache" / "spotify-loopback"
    cert_path, key_path = cache_dir / "cert.pem", cache_dir / "key.pem"

    def _load_cached() -> ssl.SSLContext:
        context = _server_ssl_context_class()(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
        return context

    # Reuse a cached pair only if it still loads; a truncated/corrupt pair is
    # regenerated once instead of wedging every subsequent login.
    if cert_path.exists() and key_path.exists():
        try:
            return _load_cached()
        except (ssl.SSLError, OSError, ValueError):
            for path in (cert_path, key_path):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass

    cache_dir.mkdir(parents=True, exist_ok=True)
    try:
        import datetime as dt
        import ipaddress
        import os as _os
        import tempfile

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
        now = dt.datetime.now(dt.timezone.utc)
        certificate = (
            x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=397))
            .add_extension(x509.SubjectAlternativeName([
                x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                x509.DNSName("localhost"),
            ]), critical=False)
            .sign(key, hashes.SHA256())
        )
        key_bytes = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption())
        cert_bytes = certificate.public_bytes(serialization.Encoding.PEM)

        def _atomic_write(path: Path, data: bytes, mode: int) -> None:
            fd, tmp = tempfile.mkstemp(dir=str(cache_dir), prefix=f".{path.name}.")
            try:
                try:
                    if hasattr(_os, "fchmod"):
                        _os.fchmod(fd, mode)
                except (OSError, AttributeError):
                    pass
                with _os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    _os.fsync(stream.fileno())
                _os.replace(tmp, path)
                try:
                    _os.chmod(path, mode)
                except OSError:
                    pass
            except BaseException:
                try:
                    _os.unlink(tmp)
                except FileNotFoundError:
                    pass
                raise

        # Key is created owner-only (0600) and both files appear atomically.
        _atomic_write(key_path, key_bytes, 0o600)
        _atomic_write(cert_path, cert_bytes, 0o644)
    except ImportError as exc:  # pragma: no cover - cryptography ships with Hermes
        raise _err(
            "Serving the Spotify https callback needs the 'cryptography' package. "
            "Install it, or register the http loopback URI if your app still allows it.",
            "spotify_tls_unavailable") from exc
    return _load_cached()



def _wait_for_callback(redirect_uri: str, *, timeout_seconds: float = 180.0) -> dict[str, Any]:
    from hermes_cli.auth_device_flow import (
        _bind_loopback_callback_server, _make_loopback_callback_handler, _serve_loopback_callback)
    host, port, path = _validate_redirect_uri(redirect_uri)
    handler_cls, result = _make_loopback_callback_handler(path, display_name="Spotify")
    server = _bind_loopback_callback_server(
        host, port, handler_cls, err=_err, bind_failed_code="spotify_callback_bind_failed")
    if urlparse(redirect_uri).scheme == "https":
        # Wrap the already-bound socket: the handler and the core's serving loop stay as they
        # are. Handshake noise from browser probes is dropped instead of printed as tracebacks.
        server.socket = _loopback_tls_context().wrap_socket(server.socket, server_side=True)
        server.handle_error = lambda request, client_address: None  # noqa: ARG005
    return _serve_loopback_callback(
        server, result, timeout_seconds=timeout_seconds, err=_err, timeout_code="spotify_callback_timeout")


def _token_payload_to_state(
    token_payload: dict[str, Any], *, client_id: str, redirect_uri: str, requested_scope: str,
    accounts_base_url: str, api_base_url: str, previous_state: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    from hermes_cli.auth import _coerce_ttl_seconds
    now = datetime.now(timezone.utc)
    expires_in = _coerce_ttl_seconds(token_payload.get("expires_in", 0))
    expires_at = datetime.fromtimestamp(now.timestamp() + expires_in, tz=timezone.utc)
    state = dict(previous_state or {})
    state.update({
        "client_id": client_id, "redirect_uri": redirect_uri,
        "accounts_base_url": accounts_base_url, "api_base_url": api_base_url,
        "scope": requested_scope,
        "granted_scope": str(token_payload.get("scope") or requested_scope).strip(),
        "token_type": _clean(token_payload.get("token_type", "Bearer") or "Bearer") or "Bearer",
        "access_token": _clean(token_payload.get("access_token")),
        "refresh_token": _clean(token_payload.get("refresh_token") or state.get("refresh_token")),
        "obtained_at": now.isoformat(), "expires_at": expires_at.isoformat(),
        "expires_in": expires_in, "auth_type": "oauth_pkce",
    })
    return state


def _token_post(
    accounts_base_url: str, data: dict[str, str], *, timeout_seconds: float, what: str,
    failed_code: str, invalid_code: str, invalid_message: str, failed_suffix: str = "",
    relogin_required: bool = False,
) -> dict[str, Any]:
    """POST to Spotify's ``/api/token`` and return the JSON payload, or raise a shaped AuthError."""
    import httpx
    try:
        response = httpx.post(
            f"{accounts_base_url}/api/token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=data, timeout=timeout_seconds,
        )
    except Exception as exc:
        raise _err(f"Spotify {what} failed: {exc}", failed_code) from exc

    if response.status_code >= 400:
        detail = response.text.strip()
        raise _err(f"Spotify {what} failed.{failed_suffix}" + (f" Response: {detail}" if detail else ""),
                   failed_code, relogin=relogin_required)
    payload = response.json()
    if not isinstance(payload, dict) or not _clean(payload.get("access_token")):
        raise _err(invalid_message, invalid_code, relogin=relogin_required)
    return payload


def refresh_oauth_state(state: dict[str, Any], *, timeout_seconds: float = 20.0) -> dict[str, Any]:
    refresh_token = _clean(state.get("refresh_token"))
    if not refresh_token:
        raise _err("Spotify refresh token missing. Run `hermes spotify login` again.",
                   "spotify_refresh_token_missing", relogin=True)

    client_id = _client_id(state=state)
    accounts_base_url = _accounts_base_url(state)
    payload = _token_post(
        accounts_base_url,
        {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": client_id},
        timeout_seconds=timeout_seconds, what="token refresh", failed_code="spotify_refresh_failed",
        invalid_code="spotify_refresh_invalid",
        invalid_message="Spotify refresh response did not include an access_token.",
        failed_suffix=" Run `hermes spotify login` again.", relogin_required=True,
    )
    return _token_payload_to_state(
        payload, client_id=client_id, redirect_uri=_redirect_uri(state=state),
        requested_scope=str(state.get("scope") or DEFAULT_SCOPE),
        accounts_base_url=accounts_base_url, api_base_url=_api_base_url(state),
        previous_state=state,
    )


def resolve_runtime_credentials(
    *, force_refresh: bool = False, refresh_if_expiring: bool = True,
    refresh_skew_seconds: int = ACCESS_TOKEN_REFRESH_SKEW_SECONDS,
) -> dict[str, Any]:
    from hermes_cli.auth import (
        _auth_store_lock, _is_expiring, _load_auth_store, _load_provider_state, _quarantine_flat_oauth_state,
        _save_auth_store, _store_provider_state)
    from hermes_cli.auth_constants import AuthError
    with _auth_store_lock():
        auth_store = _load_auth_store()
        state = _load_provider_state(auth_store, PROVIDER)
        if not state:
            raise _err("Spotify is not authenticated. Run `hermes spotify login` first.",
                       "spotify_auth_missing", relogin=True)

        should_refresh = bool(force_refresh)
        if not should_refresh and refresh_if_expiring:
            should_refresh = _is_expiring(state.get("expires_at"), refresh_skew_seconds)
        if should_refresh:
            try:
                state = refresh_oauth_state(state)
                _store_provider_state(auth_store, PROVIDER, state, set_active=False)
                _save_auth_store(auth_store)
            except AuthError as exc:
                if exc.relogin_required and state.get("refresh_token"):
                    _quarantine_flat_oauth_state(state, PROVIDER, exc)
                    try:
                        _store_provider_state(auth_store, PROVIDER, state, set_active=False)
                        _save_auth_store(auth_store)
                    except Exception as save_exc:
                        logger.debug("Spotify OAuth: failed to persist quarantined state: %s", save_exc)
                raise

    access_token = _clean(state.get("access_token"))
    if not access_token:
        raise _err("Spotify access token missing. Run `hermes spotify login` again.",
                   "spotify_access_token_missing", relogin=True)

    return {
        "provider": PROVIDER, "access_token": access_token, "api_key": access_token,
        "token_type": str(state.get("token_type", "Bearer") or "Bearer"),
        "base_url": _api_base_url(state),
        "scope": _clean(state.get("granted_scope") or state.get("scope")),
        "client_id": _client_id(state=state),
        "redirect_uri": _redirect_uri(state=state), "expires_at": state.get("expires_at"),
        "refresh_token": _clean(state.get("refresh_token")),
    }


def auth_status() -> dict[str, Any]:
    from hermes_cli.auth import _is_expiring, get_provider_auth_state
    state = get_provider_auth_state(PROVIDER)
    if not state:
        return {"logged_in": False}
    expires_at = state.get("expires_at")
    refresh_token = _clean(state.get("refresh_token"))
    return {
        "logged_in": bool(refresh_token or not _is_expiring(expires_at, 0)),
        "auth_type": state.get("auth_type", "oauth_pkce"), "client_id": state.get("client_id"),
        "redirect_uri": state.get("redirect_uri"),
        "scope": state.get("granted_scope") or state.get("scope"), "expires_at": expires_at,
        "api_base_url": state.get("api_base_url"), "has_refresh_token": bool(refresh_token),
    }


def logout() -> bool:
    """Drop the stored Spotify login. True when something was removed."""
    from hermes_cli.auth import clear_provider_auth
    return clear_provider_auth(PROVIDER)


def _interactive_setup(redirect_uri_hint: str) -> str:
    """Walk the user through creating a Spotify developer app; persist the client_id to the profile's .env."""
    from hermes_cli.auth_device_flow import _is_remote_session
    from hermes_cli.cli_output import line_input
    from hermes_cli.config import save_env_value
    print(
        f"\n{'=' * 70}\nSpotify first-time setup\n{'=' * 70}\n\n"
        "Spotify requires every user to register their own lightweight\n"
        "developer app. This takes about two minutes and only has to be\n"
        "done once per machine.\n\n"
        f"Full guide: {DOCS_URL}\n\n"
        "Steps:\n"
        f"  1. Opening {DASHBOARD_URL} in your browser...\n"
        "  2. Click 'Create app' and fill in:\n"
        "       App name:     anything (e.g. hermes-agent)\n"
        "       Description:  anything\n"
        f"       Redirect URI: {redirect_uri_hint}\n"
        "       API/SDK:      Web API\n"
        "  3. Agree to the terms, click Save.\n"
        "  4. Open the app's Settings page and copy the Client ID.\n"
        "  5. Paste it below.\n"
    )
    if not _is_remote_session():
        try:
            webbrowser.open(DASHBOARD_URL)
        except Exception:
            pass
    try:
        raw = line_input("Spotify Client ID: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise SystemExit("Spotify setup cancelled.")
    if not raw:
        print(f"\nNo Client ID entered. See {DOCS_URL} for the full guide.")
        raise SystemExit("Spotify setup cancelled: empty Client ID.")

    # Persist so later runs skip the wizard; only pin a NON-default redirect URI.
    save_env_value("HERMES_SPOTIFY_CLIENT_ID", raw)
    if redirect_uri_hint and redirect_uri_hint != DEFAULT_REDIRECT_URI:
        save_env_value("HERMES_SPOTIFY_REDIRECT_URI", redirect_uri_hint)
    print("\nSaved HERMES_SPOTIFY_CLIENT_ID to the profile's .env\n")
    return raw


def login(args) -> None:
    from hermes_cli.auth import (
        _auth_store_lock, _load_auth_store, _save_auth_store, _store_provider_state, get_provider_auth_state)
    from hermes_cli.auth_constants import AuthError
    from hermes_cli.auth_device_flow import (
        _can_open_graphical_browser, _is_remote_session, _pkce_code_challenge, _pkce_code_verifier,
        _print_loopback_ssh_hint)
    existing_state = get_provider_auth_state(PROVIDER) or {}

    # No client_id anywhere -> wizard instead of "HERMES_SPOTIFY_CLIENT_ID is required".
    try:
        client_id = _client_id(getattr(args, "client_id", None), existing_state)
    except AuthError as exc:
        if getattr(exc, "code", "") != "spotify_client_id_missing":
            raise
        client_id = _interactive_setup(redirect_uri_hint=getattr(args, "redirect_uri", None) or DEFAULT_REDIRECT_URI)

    redirect_uri = _redirect_uri(getattr(args, "redirect_uri", None), existing_state)
    scope = _scope_string(getattr(args, "scope", None) or existing_state.get("scope"))
    accounts_base_url = _accounts_base_url(existing_state)
    api_base_url = _api_base_url(existing_state)
    open_browser = not getattr(args, "no_browser", False)

    code_verifier = _pkce_code_verifier()
    state_nonce = uuid.uuid4().hex
    authorize_url = _build_authorize_url(
        client_id=client_id, redirect_uri=redirect_uri, scope=scope, state=state_nonce,
        code_challenge=_pkce_code_challenge(code_verifier), accounts_base_url=accounts_base_url,
    )
    print(
        f"Starting Spotify PKCE login...\nClient ID: {client_id}\nRedirect URI: {redirect_uri}\n"
        "Make sure this redirect URI is allow-listed in your Spotify app settings.\n\n"
        f"Open this URL to authorize Hermes:\n{authorize_url}\n\nFull setup guide: {DOCS_URL}\n"
    )
    _print_loopback_ssh_hint(redirect_uri, docs_url=DOCS_URL)
    if open_browser and not _is_remote_session() and _can_open_graphical_browser():
        try:
            opened = webbrowser.open(authorize_url)
        except Exception:
            opened = False
        print("Browser opened for Spotify authorization." if opened
              else "Could not open the browser automatically; use the URL above.")

    callback = _wait_for_callback(redirect_uri, timeout_seconds=float(getattr(args, "timeout", None) or 180.0))
    if callback.get("error"):
        raise SystemExit(f"Spotify authorization failed: {callback.get('error_description') or callback['error']}")
    if callback.get("state") != state_nonce:
        raise SystemExit("Spotify authorization failed: state mismatch.")

    token_payload = _token_post(
        accounts_base_url,
        {
            "client_id": client_id, "grant_type": "authorization_code",
            "code": str(callback.get("code") or ""), "redirect_uri": redirect_uri, "code_verifier": code_verifier,
        },
        timeout_seconds=float(getattr(args, "timeout", None) or 20.0),
        what="token exchange", failed_code="spotify_token_exchange_failed",
        invalid_code="spotify_token_exchange_invalid",
        invalid_message="Spotify token response did not include an access_token.",
    )
    spotify_state = _token_payload_to_state(
        token_payload, client_id=client_id, redirect_uri=redirect_uri, requested_scope=scope,
        accounts_base_url=accounts_base_url, api_base_url=api_base_url,
    )
    with _auth_store_lock():
        auth_store = _load_auth_store()
        _store_provider_state(auth_store, PROVIDER, spotify_state, set_active=False)
        saved_to = _save_auth_store(auth_store)
    print(f"Spotify login successful!\n  Auth state: {saved_to}\n"
          f"  Provider state saved under providers.spotify\n  Docs: {DOCS_URL}")
