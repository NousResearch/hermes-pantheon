"""X Chat platform adapter (Hermes plugin).

Connects the Hermes gateway to X's end-to-end encrypted direct messages
via the official X Chat API. All plaintext stays local: inbound
``encoded_event`` blobs are decrypted with the Chat XDK (``chatxdk``) and
outbound replies are encrypted + signed before they ever reach X.

Transport model
---------------
Inbound is a polling loop over ``GET /2/chat/conversations/{id}/events``
(the same shape as X's own bot example). Conversations are auto-discovered
via ``GET /2/chat/conversations`` (or pinned with
``XCHAT_CONVERSATION_IDS``); each is polled every ``XCHAT_POLL_INTERVAL``
seconds with exponential backoff on errors. Outbound goes through
``POST /2/chat/conversations/{id}/messages``.

Identity / key state (written by ``hermes xchat setup``):

* ``XCHAT_ACCESS_TOKEN``   OAuth2 user token (dm.read, dm.write, users.read, tweet.read)
* ``XCHAT_USER_ID``        numeric id of the X account Hermes runs as
* ``XCHAT_SIGNING_KEY_VERSION``  registered public-key version
* private-key blob at ``~/.hermes/xchat/private_keys.b64`` (mode 600), or
  ``XCHAT_PRIVATE_KEYS_B64`` env override

The E2EE session is one ``chat_xdk.Chat`` instance with ``set_identity`` +
``set_cache_keys(True)``: KeyChange events route through the batch decrypt
path to feed the verified-key cache, so encrypt calls need no explicit
conversation key.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)

from .api import HTTPX_AVAILABLE, XChatApi, XChatApiError, XChatRateLimited
from .crypto import (
    XChatCrypto,
    detect_image_dimensions,
    detect_mime_type,
    message_attachments,
    message_text,
)

logger = logging.getLogger(__name__)

# X DMs cap out around 10k chars; stay under it so chunking kicks in first.
MAX_MESSAGE_LENGTH = 9500

DEFAULT_POLL_INTERVAL = 10.0
ERROR_BACKOFF = [5, 15, 30, 60, 120]
DEDUP_MAX_SIZE = 5000
# Bounded cache of recent decrypted events per conversation — used to build
# native threaded replies (encrypt_reply needs the decrypted target event).
REPLY_CACHE_MAX = 200
# Cap inbound attachments processed per message.
MAX_INBOUND_ATTACHMENTS = 5
# Message ids Hermes sent, kept so self-chat echoes are never answered.
SENT_IDS_MAX = 500

# Group-chat mention wake words — same defaults as the other Hermes channels
# so group gating behaves identically everywhere.
_DEFAULT_MENTION_PATTERNS = [
    r"(?<![\w@])@?hermes\s+agent\b[,:\-]?",
    r"(?<![\w@])@?hermes\b[,:\-]?",
]


def _state_dir() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "xchat"


def _cursor_path() -> Path:
    return _state_dir() / "cursors.json"


def _conversations_path() -> Path:
    """Conversation ids seen so far — the fallback when the list endpoint is dark."""
    return _state_dir() / "conversations.json"


def _load_cursors() -> Dict[str, str]:
    """Per-conversation cursors (last processed event id), persisted to disk.

    The cursor is the source of truth for "what have we already handled":
    it survives restarts (so messages received while the gateway was down
    are processed, not swallowed) and never evicts (so a pruned dedup
    entry can't cause a re-reply to an old message).
    """
    try:
        data = json.loads(_cursor_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if str(v)}


def _conv_key(conversation_id: str) -> str:
    """One cache key per conversation: events embed ``A:B``, URL paths and listings use ``A-B``."""
    return str(conversation_id).replace(":", "-")


def _is_self_chat(conversation_id: str, account_id: str) -> bool:
    """True for the account's conversation with itself (``A-A`` / ``A:A``), never a group."""
    parts = _conv_key(conversation_id).split("-")
    return bool(account_id) and len(parts) == 2 and parts[0] == parts[1] == account_id


def _sent_path() -> Path:
    return _state_dir() / "sent_message_ids.json"


def _load_sent_ids() -> List[str]:
    try:
        data = json.loads(_sent_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [str(m) for m in data] if isinstance(data, list) else []


def _remember_sent(message_id: str) -> None:
    """Record a message id this account sent through Hermes.

    The gateway and out-of-process senders (cron, ``hermes send``) share this file, so in the
    self-chat the poll loop can tell Hermes' own messages from ones the owner typed. Ids are the
    SDK-generated message UUIDs, which the decrypted event carries back as ``id``.
    """
    if not message_id:
        return
    ids = [m for m in _load_sent_ids() if m != message_id] + [message_id]
    try:
        path = _sent_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(ids[-SENT_IDS_MAX:]) + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError:
        logger.debug("[xchat] sent-id persist failed", exc_info=True)


def _event_id_newer(a: str, b: str) -> bool:
    """True when event id ``a`` is newer than ``b`` (ids are numeric,
    time-ordered; fall back to string comparison just in case)."""
    try:
        return int(a) > int(b)
    except (TypeError, ValueError):
        return str(a) > str(b)


def _read_key_blob() -> str:
    """Private-key blob: env override first, then the setup-written file."""
    env_blob = os.getenv("XCHAT_PRIVATE_KEYS_B64", "").strip()
    if env_blob:
        return env_blob
    blob_path = _state_dir() / "private_keys.b64"
    try:
        return blob_path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def check_requirements() -> bool:
    """True when the adapter is minimally configured (token + key material).

    Deliberately does NOT import chatxdk (a native module): a pre-flight
    check must stay cheap. A missing SDK surfaces at connect time.
    """
    if not HTTPX_AVAILABLE:
        return False
    if not os.getenv("XCHAT_ACCESS_TOKEN", "").strip():
        return False
    return bool(_read_key_blob())


def validate_config(config) -> bool:
    extra = getattr(config, "extra", {}) or {}
    token = extra.get("access_token") or os.getenv("XCHAT_ACCESS_TOKEN", "")
    return bool(token)


def is_connected(config) -> bool:
    extra = getattr(config, "extra", {}) or {}
    token = os.getenv("XCHAT_ACCESS_TOKEN") or extra.get("access_token", "")
    return bool(token)


def _compile_mention_patterns(raw: Any) -> List[re.Pattern]:
    """Accept list / JSON string / comma- or newline-separated string / None."""
    patterns: List[str]
    if raw is None or raw == "":
        patterns = _DEFAULT_MENTION_PATTERNS
    elif isinstance(raw, list):
        patterns = [str(p) for p in raw if str(p).strip()]
    else:
        text = str(raw).strip()
        if text.startswith("["):
            try:
                patterns = [str(p) for p in json.loads(text)]
            except (ValueError, TypeError):
                patterns = [text]
        else:
            parts = re.split(r"[\n,]+", text)
            patterns = [p.strip() for p in parts if p.strip()]
        if not patterns:
            patterns = _DEFAULT_MENTION_PATTERNS
    compiled = []
    for p in patterns:
        try:
            compiled.append(re.compile(p, re.IGNORECASE))
        except re.error:
            logger.warning("[xchat] invalid mention pattern skipped: %r", p)
    return compiled or [re.compile(p, re.IGNORECASE) for p in _DEFAULT_MENTION_PATTERNS]


class XChatAdapter(BasePlatformAdapter):
    """X Chat (encrypted X DMs) adapter."""

    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH

    def __init__(self, config: PlatformConfig):
        platform = Platform("xchat")
        super().__init__(config=config, platform=platform)

        extra = config.extra or {}
        self._access_token: str = (
            extra.get("access_token") or os.getenv("XCHAT_ACCESS_TOKEN", "")
        ).strip()
        self._refresh_token: str = (
            extra.get("refresh_token") or os.getenv("XCHAT_REFRESH_TOKEN", "")
        ).strip()
        self._client_id: str = (
            extra.get("client_id") or os.getenv("XCHAT_CLIENT_ID", "")
        ).strip()
        self._client_secret: str = (
            extra.get("client_secret") or os.getenv("XCHAT_CLIENT_SECRET", "")
        ).strip()
        self._bot_user_id: str = str(
            extra.get("user_id") or os.getenv("XCHAT_USER_ID", "")
        ).strip()
        self._signing_key_version: str = str(
            extra.get("signing_key_version")
            or os.getenv("XCHAT_SIGNING_KEY_VERSION", "1")
        ).strip() or "1"

        try:
            self._poll_interval = float(
                extra.get("poll_interval") or os.getenv("XCHAT_POLL_INTERVAL", "") or DEFAULT_POLL_INTERVAL
            )
        except (TypeError, ValueError):
            self._poll_interval = DEFAULT_POLL_INTERVAL
        self._poll_interval = max(2.0, self._poll_interval)

        # Pinned conversations (skip discovery when set).
        conv_raw = extra.get("conversation_ids") or os.getenv("XCHAT_CONVERSATION_IDS", "")
        if isinstance(conv_raw, list):
            self._pinned_conversations = [str(c).strip() for c in conv_raw if str(c).strip()]
        else:
            self._pinned_conversations = [
                c.strip() for c in str(conv_raw).split(",") if c.strip()
            ]

        # Group mention gating.
        env_require = os.getenv("XCHAT_REQUIRE_MENTION")
        if env_require is not None:
            self.require_mention = env_require.strip().lower() in {"1", "true", "yes"}
        else:
            self.require_mention = bool(extra.get("require_mention", False))
        self._mention_patterns = _compile_mention_patterns(
            extra.get("mention_patterns") or os.getenv("XCHAT_MENTION_PATTERNS")
        )

        # Runtime state
        self._api: Optional[XChatApi] = None
        self._crypto: Optional[XChatCrypto] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._running = False
        self._lock_acquired = False

        # Per-conversation cursors + dedup. Cursors persist across restarts
        # (~/.hermes/xchat/cursors.json) — they are the primary duplicate /
        # backlog guard; the in-memory dedup set is only a same-session
        # safety net (echo suppression of our own sends).
        self._conversations: Set[str] = set(self._pinned_conversations)
        # Monotonic deadline before which the conversation list is not re-tried.
        self._discovery_blocked_until = 0.0
        # Conversations that appeared after the first discovery pass: no backlog to skip.
        self._initial_discovery_done = False
        self._live_conversations: Set[str] = set()
        self._cursors: Dict[str, str] = _load_cursors()
        self._seen_event_ids: Dict[str, float] = {}
        self._conversation_keys: Dict[str, Dict[str, bytes]] = {}
        # Latest verified key version per conversation — media decrypt must
        # use the key for the EVENT's version, media encrypt the latest.
        self._latest_key_version: Dict[str, str] = {}
        # Recent decrypted events per conversation, keyed by event id — lets
        # send(reply_to=...) build a native threaded reply.
        self._event_cache: Dict[str, Dict[str, str]] = {}
        # The account's own messages in its self-chat ("message yourself") reach the agent.
        env_self = os.getenv("XCHAT_ALLOW_SELF_CHAT")
        if env_self is not None:
            self._allow_self_chat = env_self.strip().lower() not in {"0", "false", "no", "off"}
        else:
            self._allow_self_chat = bool(extra.get("allow_self_chat", True))
        self._sent_ids: Set[str] = set(_load_sent_ids())
        if self._allow_self_chat and self._bot_user_id and not os.getenv("XCHAT_ALLOWED_USERS", "").strip():
            # With no allowlist configured the owner still reaches their own agent from the self-chat;
            # strangers keep the gateway's normal unknown-sender handling.
            if config.extra is None:
                config.extra = {}
            config.extra.setdefault("allowed_users", self._bot_user_id)
        # Read receipts (privacy default: off).
        env_read = os.getenv("XCHAT_SEND_READ_RECEIPTS")
        if env_read is not None:
            self._send_read_receipts = env_read.strip().lower() in {"1", "true", "yes"}
        else:
            self._send_read_receipts = bool(extra.get("send_read_receipts", False))

        # Signing-key roster (accumulated; the SDK store is replaced wholesale)
        self._signing_keys: List[Dict[str, str]] = []
        self._known_senders: Set[str] = set()

        logger.info(
            "[xchat] adapter initialized: user_id=%s poll=%.0fs pinned=%d refresh=%s",
            self._bot_user_id or "?",
            self._poll_interval,
            len(self._pinned_conversations),
            "yes" if (self._refresh_token and self._client_id) else "no",
        )

    # -- Connection lifecycle -------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not HTTPX_AVAILABLE:
            logger.warning("[xchat] httpx not installed")
            return False
        if not self._access_token:
            logger.warning("[xchat] XCHAT_ACCESS_TOKEN not configured — run `hermes xchat setup`")
            return False

        key_blob = _read_key_blob()
        if not key_blob:
            logger.warning(
                "[xchat] no private-key blob found (~/.hermes/xchat/private_keys.b64 "
                "or XCHAT_PRIVATE_KEYS_B64) — run `hermes xchat setup`"
            )
            return False

        # One credential = one gateway. Prevents two profiles polling (and
        # double-replying) on the same bot account.
        try:
            from gateway.status import acquire_scoped_lock

            ok, holder = acquire_scoped_lock("xchat", self._access_token[:16])
            if not ok:
                logger.error("[xchat] credential already in use by another gateway: %s", holder)
                return False
            self._lock_acquired = True
        except Exception:
            logger.debug("[xchat] scoped lock unavailable; continuing", exc_info=True)

        self._api = XChatApi(
            self._access_token,
            refresh_token=self._refresh_token,
            client_id=self._client_id,
            client_secret=self._client_secret,
            on_token_refresh=self._persist_rotated_tokens,
        )

        # Derive the bot's own user id when not configured.
        if not self._bot_user_id:
            try:
                me = await self._api.get_my_user()
                self._bot_user_id = str(me.get("id") or "")
            except XChatApiError as e:
                logger.error("[xchat] failed to resolve bot user id: %s", e)
                await self._teardown()
                return False
        if not self._bot_user_id:
            logger.error("[xchat] could not determine bot user id")
            await self._teardown()
            return False

        # Unlock the E2EE session (first chatxdk import).
        try:
            crypto = XChatCrypto()
            crypto.load_keys(key_blob, self._signing_key_version)
            crypto.set_identity(self._bot_user_id)
            crypto.set_cache_keys(True)
            self._crypto = crypto
        except Exception as e:
            logger.error("[xchat] failed to initialize Chat XDK session: %s", e)
            await self._teardown()
            return False

        self._load_conversations()
        self._running = True
        self._poll_task = asyncio.create_task(self._run_poll_loop())
        self._mark_connected()
        logger.info("[xchat] connected as user %s", self._bot_user_id)
        return True

    async def disconnect(self) -> None:
        self._running = False
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except (asyncio.CancelledError, Exception):
                pass
            self._poll_task = None
        await self._teardown()
        logger.info("[xchat] disconnected")

    async def _teardown(self) -> None:
        if self._api is not None:
            await self._api.aclose()
            self._api = None
        if self._lock_acquired:
            try:
                from gateway.status import release_scoped_lock

                release_scoped_lock("xchat", self._access_token[:16])
            except Exception:
                pass
            self._lock_acquired = False

    async def _persist_rotated_tokens(self, access_token: str, refresh_token: str) -> None:
        """X rotates the refresh token on every renewal — persist both to .env."""
        self._access_token = access_token
        self._refresh_token = refresh_token
        try:
            from hermes_cli.config import save_env_value

            save_env_value("XCHAT_ACCESS_TOKEN", access_token)
            if refresh_token:
                save_env_value("XCHAT_REFRESH_TOKEN", refresh_token)
        except Exception:
            logger.warning("[xchat] failed to persist rotated OAuth tokens", exc_info=True)

    # -- Polling loop -----------------------------------------------------------

    async def _run_poll_loop(self) -> None:
        backoff_idx = 0
        while self._running:
            try:
                # One conversation-list read per cycle says which conversations have
                # anything newer than our cursor; only those get an events read. Events
                # are billed per resource returned, so re-reading idle conversations
                # every cycle is the dominant cost.
                newest = await self._discover_or_degrade()
                for conv_id in list(self._conversations):
                    if not self._running:
                        return
                    head = newest.get(_conv_key(conv_id))
                    cursor = self._cursors.get(conv_id, "")
                    if head and cursor and not _event_id_newer(head, cursor):
                        continue
                    await self._poll_conversation(conv_id)

                backoff_idx = 0
                await asyncio.sleep(self._poll_interval)
            except asyncio.CancelledError:
                return
            except XChatRateLimited as e:
                wait = max(30.0, (e.reset_epoch - time.time()) if e.reset_epoch else 60.0)
                wait = min(wait, 900.0)
                logger.warning("[xchat] rate limited — sleeping %.0fs", wait)
                await asyncio.sleep(wait)
            except Exception as e:
                delay = ERROR_BACKOFF[min(backoff_idx, len(ERROR_BACKOFF) - 1)]
                backoff_idx += 1
                logger.warning("[xchat] poll error (retry in %ds): %s", delay, e)
                await asyncio.sleep(delay)

    async def _discover_conversations(self) -> Dict[str, str]:
        """List conversations; returns ``{conversation: newest_event_id}`` where reported."""
        assert self._api is not None
        token: Optional[str] = None
        found: Set[str] = set()
        newest: Dict[str, str] = {}
        for _ in range(10):  # hard page cap
            page = await self._api.get_conversations(max_results=100, pagination_token=token)
            for conv in page.get("data") or []:
                cid = str(conv.get("conversation_id") or conv.get("id") or "").strip()
                if cid:
                    found.add(cid)
                    if conv.get("newest_event_id"):
                        newest[_conv_key(cid)] = str(conv["newest_event_id"])
            token = (page.get("meta") or {}).get("next_token")
            if not token:
                break
        new = found - self._conversations
        if new:
            logger.info("[xchat] discovered %d new conversation(s)", len(new))
            if self._initial_discovery_done:
                # Started after we were already watching: every event is a live message.
                self._live_conversations |= new
        self._initial_discovery_done = True
        if self._pinned_conversations:
            return newest  # pinned: poll exactly those, never widen the set
        self._conversations |= found
        if new:
            self._persist_conversations()
        return newest

    def _persist_conversations(self) -> None:
        try:
            path = _conversations_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(sorted(self._conversations)))
            os.chmod(tmp, 0o600)
            tmp.replace(path)
        except Exception as e:  # noqa: BLE001 - cache only
            logger.warning("[xchat] could not persist conversation list: %s", e)

    def _load_conversations(self) -> None:
        path = _conversations_path()
        before = len(self._conversations)
        try:
            if path.exists():
                known = json.loads(path.read_text() or "[]")
                if isinstance(known, list):
                    self._conversations |= {str(c) for c in known if c}
        except Exception as e:  # noqa: BLE001
            logger.warning("[xchat] could not load known conversations: %s", e)
        # Cursors are keyed by conversation, so a conversation we have polled
        # before is recoverable even if the id list was never written.
        self._conversations |= set(self._cursors)
        if len(self._conversations) > before:
            logger.info(
                "[xchat] loaded %d known conversation(s)",
                len(self._conversations) - before,
            )

    async def _discover_or_degrade(self) -> Dict[str, str]:
        """List conversations, or fall back to known ones when the list is refused.

        The list endpoint and the per-conversation events endpoint are metered
        separately (1,000/day against 10,000/day on current limits), so a
        throttled list does not mean inbound has to stop: known conversation ids
        — persisted, so a restart mid-throttle still has them — can be polled
        directly. Discovery is re-tried when the throttle is due to lift rather
        than every cycle, which would spend a refused request per cycle.
        """
        now = time.monotonic()
        if now >= self._discovery_blocked_until:
            try:
                return await self._discover_conversations()
            except XChatRateLimited as e:
                wait = 300.0
                if e.reset_epoch:
                    wait = min(max(60.0, e.reset_epoch - time.time()), 1800.0)
                self._discovery_blocked_until = now + wait
                logger.warning(
                    "[xchat] conversation list throttled — polling %d known conversation(s) "
                    "directly for the next %.0fs",
                    len(self._conversations),
                    wait,
                )
        if not self._conversations:
            raise RuntimeError(
                "conversation list throttled and no known conversations to fall back on"
            )
        # No head ids without the list: every known conversation is polled, and
        # the cursor check inside _poll_conversation keeps that to one page each.
        return {}

    async def _poll_conversation(self, conv_id: str) -> None:
        assert self._api is not None and self._crypto is not None
        cursor = self._cursors.get(conv_id, "")

        # Page until we reach the cursor (or the feed ends) so a burst of
        # more than one page between polls is never dropped. Newest-first
        # on the wire; hard page cap keeps a pathological feed bounded.
        # KeyChange events arrive SEPARATELY in meta.conversation_key_events
        # — they must be decrypted (batch path) before the messages that
        # were encrypted under them, or no conversation key is available.
        collected: List[Dict[str, Any]] = []
        key_events_b64: List[str] = []
        token: Optional[str] = None
        reached_cursor = False
        for _ in range(10):
            page = await self._api.get_events(
                conv_id, max_results=50, pagination_token=token
            )
            raw = page.get("data") or []
            meta = page.get("meta") or {}
            for kev in meta.get("conversation_key_events") or []:
                if kev and kev not in key_events_b64:
                    key_events_b64.append(kev)
            if not raw:
                break
            for item in raw:
                eid = str(item.get("id") or "")
                if cursor and eid and not _event_id_newer(eid, cursor):
                    reached_cursor = True
                    break
                collected.append(item)
            token = meta.get("next_token")
            if reached_cursor or not token:
                break
        if not collected and not key_events_b64:
            return

        # Process oldest-first.
        collected.reverse()
        await self._register_signing_keys(collected)

        # Verify + cache any conversation-key changes FIRST (after signing
        # keys are registered — an unverifiable KeyChange is dropped by the
        # SDK), so this poll's messages can decrypt under rotated keys.
        if key_events_b64:
            self._absorb_key_batch(conv_id, key_events_b64)
        if not collected:
            return

        if not cursor and conv_id not in self._live_conversations:
            # First sight of this conversation EVER (no persisted cursor):
            # batch-decrypt to seed the SDK's verified-key cache, but do NOT
            # reply to the historical backlog. The cursor persists, so a
            # gateway restart does not re-enter this branch — messages that
            # arrived while we were down are processed normally above.
            events_b64 = [e["encoded_event"] for e in collected if e.get("encoded_event")]
            if events_b64:
                self._absorb_key_batch(conv_id, events_b64, label="backlog")
            newest = str(collected[-1].get("id") or "")
            if newest:
                self._set_cursor(conv_id, newest)
            return

        for item in collected:
            event_id = str(item.get("id") or "")
            if not event_id:
                continue
            if event_id in self._seen_event_ids:
                # Same-session echo suppression (our own sends).
                self._set_cursor(conv_id, event_id)
                continue
            self._seen_event_ids[event_id] = time.time()
            self._prune_dedup()

            event_b64 = item.get("encoded_event")
            if not event_b64:
                self._set_cursor(conv_id, event_id)
                continue
            try:
                event = self._crypto.decrypt_one(
                    event_b64, self._conversation_keys.get(_conv_key(conv_id)) or None
                )
            except Exception as e:
                logger.warning("[xchat] decrypt failed conv=%s event=%s: %s", conv_id, event_id, e)
                self._set_cursor(conv_id, event_id)
                continue

            etype = event.get("type")
            if etype == "KeyChange":
                # Key rotation: route through the batch path — it verifies the
                # change and feeds the SDK's verified-key cache.
                self._absorb_key_batch(conv_id, [event_b64], label="key-change")
                self._set_cursor(conv_id, event_id)
                continue
            if etype not in ("Message", "MessageEdit"):
                # Reactions, read receipts, etc.
                self._set_cursor(conv_id, event_id)
                continue

            sender_id = str(event.get("sender_id") or item.get("sender_id") or "")
            # The signature covers the canonical conversation id embedded in
            # the event — prefer it for replies.
            canonical_conv = str(event.get("conversation_id") or conv_id)
            if sender_id == self._bot_user_id and not self._is_owner_self_chat_message(canonical_conv, event):
                # Our own replies, and the account's messages in its other conversations.
                self._set_cursor(conv_id, event_id)
                continue
            self._cache_event(canonical_conv, event_id, event_b64)

            # Encrypted media attachments: download + decrypt + cache locally
            # so vision / the agent can read them.
            media_urls, media_types = await self._fetch_attachments(
                conv_id, event, item
            )

            text = message_text(event) or ""
            if not text and not media_urls:
                self._set_cursor(conv_id, event_id)
                continue

            await self._dispatch_inbound(
                conv_id=canonical_conv,
                sender_id=sender_id,
                text=text,
                message_id=event_id,
                raw=item,
                media_urls=media_urls,
                media_types=media_types,
                reply_to=self._reply_context(canonical_conv, event),
            )
            self._set_cursor(conv_id, event_id)
            await self._maybe_mark_read(conv_id, item)

    def _is_owner_self_chat_message(self, conv_id: str, event: Dict[str, Any]) -> bool:
        """A message the account owner typed in their self-chat (not one Hermes sent)."""
        if not (self._allow_self_chat and _is_self_chat(conv_id, self._bot_user_id)):
            return False
        message_id = str(event.get("id") or "")
        if message_id in self._sent_ids:
            return False
        # Cron / `hermes send` deliveries come from other processes: re-read their record.
        self._sent_ids = set(_load_sent_ids())
        return bool(message_id) and message_id not in self._sent_ids

    def resolved_allowlist_user_ids(self) -> Set[str]:
        """Gateway allowlist hook: the account itself is allowed, so its self-chat works for installs
        whose XCHAT_ALLOWED_USERS predates self-chat. Its messages only ever reach the gateway from the
        self-chat (every other own-account event is dropped at intake)."""
        return {self._bot_user_id} if self._allow_self_chat and self._bot_user_id else set()

    def _record_sent(self, message_id: str) -> None:
        if message_id:
            self._sent_ids.add(message_id)
            _remember_sent(message_id)

    def _absorb_key_batch(
        self, conv_id: str, events_b64: List[str], *, label: str = "key-events"
    ) -> None:
        """Batch-decrypt events to extract + cache verified conversation keys."""
        assert self._crypto is not None
        try:
            batch = self._crypto.decrypt_batch(events_b64)
        except Exception as e:
            logger.warning("[xchat] %s decrypt failed conv=%s: %s", label, conv_id, e)
            return
        keys = (batch.get("conversation_keys") or {}).get("keys") or {}
        if keys:
            self._conversation_keys.setdefault(_conv_key(conv_id), {}).update(keys)
        latest = batch.get("latest_key_version")
        if latest:
            self._latest_key_version[_conv_key(conv_id)] = str(latest)

    def _cache_event(self, conv_id: str, event_id: str, event_b64: str) -> None:
        """Keep a bounded cache of encoded events: the SDK builds a threaded reply from the raw event."""
        cache = self._event_cache.setdefault(_conv_key(conv_id), {})
        cache[event_id] = event_b64
        while len(cache) > REPLY_CACHE_MAX:
            cache.pop(next(iter(cache)))

    def _reply_context(
        self, conv_id: str, event: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Reply metadata for the inbound event (id/text/author), if present."""
        content = event.get("content") if isinstance(event.get("content"), dict) else {}
        reply = content.get("replying_to_preview")
        if not isinstance(reply, dict):
            return None
        return {
            "message_id": str(reply.get("replying_to_message_sequence_id") or "") or None,
            "text": reply.get("message_text"),
            "author_id": str(reply.get("sender_id") or "") or None,
        }

    async def _fetch_attachments(
        self, conv_id: str, event: Dict[str, Any], item: Dict[str, Any]
    ) -> tuple[List[str], List[str]]:
        """Download + decrypt inbound media attachments to the local cache.

        Uses the conversation key for the EVENT's key version — after a
        rotation the latest key cannot decrypt older media.
        """
        atts = message_attachments(event)
        if not atts:
            return [], []
        assert self._api is not None and self._crypto is not None
        from gateway.platforms.base import (
            cache_audio_from_bytes,
            cache_document_from_bytes,
            cache_image_from_bytes,
            cache_video_from_bytes,
            validate_inbound_media_size,
        )

        keys = self._conversation_keys.get(_conv_key(conv_id)) or {}
        event_key_version = str(event.get("key_version") or "")
        conv_key = keys.get(event_key_version) or (
            keys.get(self._latest_key_version.get(_conv_key(conv_id), "")) if keys else None
        )
        if conv_key is None and keys:
            # Last resort: any cached key (single-key conversations).
            conv_key = next(iter(keys.values()))
        if conv_key is None:
            logger.warning(
                "[xchat] attachment skipped conv=%s: no conversation key cached", conv_id
            )
            return [], []

        media_urls: List[str] = []
        media_types: List[str] = []
        for att in atts[:MAX_INBOUND_ATTACHMENTS]:
            hash_key = str(att.get("media_hash_key") or "")
            if not hash_key:
                continue
            try:
                blob = await self._api.media_download(conv_id, hash_key)
                # Raises ValueError when over the inbound media cap.
                validate_inbound_media_size(len(blob), media_type="attachment")
                plaintext = self._crypto.decrypt_media(blob, conv_key)
            except Exception as e:
                logger.warning(
                    "[xchat] attachment fetch/decrypt failed conv=%s key=%s: %s",
                    conv_id, hash_key[:12], e,
                )
                continue
            mime = detect_mime_type(plaintext) or "application/octet-stream"
            try:
                if mime.startswith("image/"):
                    ext = "." + (mime.split("/", 1)[1] or "jpg").replace("jpeg", "jpg")
                    path = cache_image_from_bytes(plaintext, ext=ext)
                elif mime.startswith("audio/"):
                    path = cache_audio_from_bytes(plaintext)
                elif mime.startswith("video/"):
                    path = cache_video_from_bytes(plaintext)
                else:
                    filename = str(att.get("filename") or f"xchat-{hash_key[:10]}.bin")
                    path = cache_document_from_bytes(plaintext, filename)
            except Exception as e:
                logger.warning("[xchat] attachment cache failed conv=%s: %s", conv_id, e)
                continue
            media_urls.append(path)
            media_types.append(mime)
        return media_urls, media_types

    async def _maybe_mark_read(self, conv_id: str, item: Dict[str, Any]) -> None:
        """Best-effort read receipt (opt-in via XCHAT_SEND_READ_RECEIPTS)."""
        if not self._send_read_receipts or self._api is None:
            return
        seq = str(item.get("sequence_id") or item.get("id") or "")
        if not seq:
            return
        try:
            await self._api.mark_read(conv_id, seq)
        except Exception:
            logger.debug("[xchat] mark-read failed conv=%s", conv_id, exc_info=True)

    def _set_cursor(self, conv_id: str, event_id: str) -> None:
        """Advance + persist the per-conversation cursor (monotonic)."""
        current = self._cursors.get(conv_id, "")
        if current and not _event_id_newer(event_id, current):
            return
        self._cursors[conv_id] = event_id
        try:
            path = _cursor_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._cursors, indent=0) + "\n", encoding="utf-8")
            tmp.replace(path)
        except OSError:
            logger.debug("[xchat] cursor persist failed", exc_info=True)

    def _prune_dedup(self) -> None:
        if len(self._seen_event_ids) <= DEDUP_MAX_SIZE:
            return
        # Drop the oldest half.
        items = sorted(self._seen_event_ids.items(), key=lambda kv: kv[1])
        for eid, _ in items[: len(items) // 2]:
            self._seen_event_ids.pop(eid, None)

    async def _register_signing_keys(self, events: List[Dict[str, Any]]) -> None:
        """Fetch new senders' public keys into the SDK's signing-key store."""
        assert self._api is not None and self._crypto is not None
        senders = {
            str(e.get("sender_id")) for e in events if e.get("sender_id")
        } | {self._bot_user_id}
        senders -= self._known_senders
        for sender_id in senders:
            try:
                for pk in await self._api.get_public_keys(sender_id):
                    self._signing_keys.append(
                        {
                            "user_id": sender_id,
                            "public_key_version": str(pk.get("public_key_version") or ""),
                            "public_key": pk.get("signing_public_key") or "",
                            "identity_public_key": pk.get("public_key") or "",
                            "identity_public_key_signature": pk.get("identity_public_key_signature") or "",
                        }
                    )
                self._known_senders.add(sender_id)
            except Exception:
                logger.warning("[xchat] public-key fetch failed sender=%s", sender_id)
        if senders and self._signing_keys:
            # The SDK store is replaced wholesale — push the full roster.
            self._crypto.set_signing_keys(self._signing_keys)

    # -- Inbound dispatch --------------------------------------------------------

    def _message_matches_mention_patterns(self, text: str) -> bool:
        return any(p.search(text) for p in self._mention_patterns)

    def _clean_mention_text(self, text: str) -> str:
        """Strip ONLY a leading wake-word match — never mid-prompt words."""
        stripped = text.lstrip()
        for p in self._mention_patterns:
            m = p.match(stripped)
            if m:
                return stripped[m.end():].lstrip()
        return text

    async def _dispatch_inbound(
        self,
        *,
        conv_id: str,
        sender_id: str,
        text: str,
        message_id: str,
        raw: Dict[str, Any],
        media_urls: Optional[List[str]] = None,
        media_types: Optional[List[str]] = None,
        reply_to: Optional[Dict[str, Any]] = None,
    ) -> None:
        is_group = conv_id.startswith("g")
        chat_type = "group" if is_group else "dm"

        if is_group and self.require_mention:
            if not self._message_matches_mention_patterns(text):
                return
            text = self._clean_mention_text(text)
            if not text and not media_urls:
                return

        source = self.build_source(
            chat_id=conv_id,
            chat_name=None,
            chat_type=chat_type,
            user_id=sender_id,
            user_name=None,
            message_id=message_id,
        )
        mtype = MessageType.TEXT
        if media_urls:
            first = (media_types or [""])[0]
            if first.startswith("image/"):
                mtype = MessageType.PHOTO
            elif first.startswith("audio/"):
                mtype = MessageType.VOICE
            elif first.startswith("video/"):
                mtype = MessageType.VIDEO
            else:
                mtype = MessageType.DOCUMENT
        reply_to = reply_to or {}
        event = MessageEvent(
            text=text,
            message_type=mtype,
            source=source,
            raw_message=raw,
            message_id=message_id,
            user_id=sender_id,
            media_urls=list(media_urls or []),
            media_types=list(media_types or []),
            reply_to_message_id=reply_to.get("message_id"),
            reply_to_text=reply_to.get("text"),
            reply_to_author_id=reply_to.get("author_id"),
            reply_to_is_own_message=(
                str(reply_to.get("author_id") or "") == self._bot_user_id
            ),
        )
        await self.handle_message(event)

    # -- Outbound ------------------------------------------------------------------

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        if self._api is None or self._crypto is None:
            return SendResult(success=False, error="xchat adapter not connected")
        if len(content) > MAX_MESSAGE_LENGTH:
            content = content[:MAX_MESSAGE_LENGTH]
        try:
            body = self._encrypt_outbound(chat_id, content, reply_to=reply_to)
        except ValueError:
            # No verified conversation key cached yet. For a 1:1, the key
            # cache seeds from the conversation backlog; a brand-new
            # conversation the bot initiates needs a key handshake — see
            # initiate_conversation() (used by the standalone sender).
            return SendResult(
                success=False,
                error=(
                    "No verified conversation key for this conversation yet. "
                    "The key cache seeds from inbound events — reply flows "
                    "always have it. To message a brand-new user, use "
                    "`hermes send xchat:<user-id>` (it performs the key "
                    "handshake automatically)."
                ),
            )
        except Exception as e:
            return SendResult(success=False, error=f"encrypt failed: {e}")
        self._record_sent(str(body.get("message_id") or ""))
        try:
            out = await self._api.send_message(chat_id, body)
        except XChatApiError as e:
            logger.warning("[xchat] send failed conv=%s: %s", chat_id, e)
            return SendResult(success=False, error=str(e))
        data = out.get("data") or {}
        msg_id = str(data.get("message_id") or body.get("message_id") or "")
        # Suppress the echo when it comes back around the poll loop.
        for eid_key in ("event_id", "id"):
            eid = data.get(eid_key)
            if eid:
                self._seen_event_ids[str(eid)] = time.time()
        return SendResult(success=True, message_id=msg_id)

    def _encrypt_outbound(
        self,
        chat_id: str,
        content: str,
        *,
        reply_to: Optional[str] = None,
        attachments: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, str]:
        """Encrypt text (or media caption) — native threaded reply when the
        replied-to event is in the decrypted-event cache."""
        assert self._crypto is not None
        if reply_to:
            target = (self._event_cache.get(_conv_key(chat_id)) or {}).get(str(reply_to))
            if target is not None:
                try:
                    return self._crypto.encrypt_reply(
                        chat_id, content, target, attachments=attachments
                    )
                except Exception:
                    logger.debug(
                        "[xchat] encrypt_reply failed conv=%s — plain send", chat_id,
                        exc_info=True,
                    )
        return self._crypto.encrypt_text(chat_id, content, attachments=attachments)

    def _conversation_key_for_send(self, chat_id: str) -> Optional[bytes]:
        """Latest cached raw conversation key (for media stream encryption)."""
        keys = self._conversation_keys.get(_conv_key(chat_id)) or {}
        if not keys:
            return None
        latest = self._latest_key_version.get(_conv_key(chat_id))
        if latest and latest in keys:
            return keys[latest]
        # Highest numeric version wins when latest is unknown.
        try:
            return keys[max(keys, key=lambda v: int(v))]
        except (ValueError, TypeError):
            return next(iter(keys.values()))

    async def _send_media_file(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str],
        reply_to: Optional[str] = None,
    ) -> SendResult:
        """Encrypt + upload a file, then send a message carrying the attachment."""
        if self._api is None or self._crypto is None:
            return SendResult(success=False, error="xchat adapter not connected")
        conv_key = self._conversation_key_for_send(chat_id)
        if conv_key is None:
            return SendResult(
                success=False,
                error="No conversation key cached — cannot encrypt media yet.",
            )
        try:
            plaintext = Path(file_path).read_bytes()
        except OSError as e:
            return SendResult(success=False, error=f"cannot read media file: {e}")
        try:
            blob = self._crypto.encrypt_media(plaintext, conv_key)
            media_hash_key = await self._api.media_upload(chat_id, blob)
        except (XChatApiError, Exception) as e:
            logger.warning("[xchat] media upload failed conv=%s: %s", chat_id, e)
            return SendResult(success=False, error=f"media upload failed: {e}")

        att: Dict[str, Any] = {
            "attachment_type": "media",
            "media_hash_key": media_hash_key,
            "filesize_bytes": len(plaintext),
            "filename": Path(file_path).name,
        }
        dims = detect_image_dimensions(plaintext)
        att["width"], att["height"] = dims if dims else (0, 0)
        try:
            body = self._encrypt_outbound(
                chat_id, caption or "", reply_to=reply_to, attachments=[att]
            )
            self._record_sent(str(body.get("message_id") or ""))
            out = await self._api.send_message(chat_id, body)
        except ValueError:
            return SendResult(success=False, error="No verified conversation key.")
        except XChatApiError as e:
            return SendResult(success=False, error=str(e))
        data = out.get("data") or {}
        for eid_key in ("event_id", "id"):
            eid = data.get(eid_key)
            if eid:
                self._seen_event_ids[str(eid)] = time.time()
        return SendResult(
            success=True,
            message_id=str(data.get("message_id") or body.get("message_id") or ""),
        )

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        path = image_url
        if path.startswith("file://"):
            from urllib.parse import unquote, urlparse

            path = unquote(urlparse(path).path)
        if not os.path.isfile(path):
            # Remote URL — fall back to the base implementation (sends URL text).
            return await super().send_image(chat_id, image_url, caption, reply_to, metadata)
        return await self._send_media_file(chat_id, path, caption, reply_to)

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        return await self._send_media_file(chat_id, image_path, caption, reply_to)

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        return await self._send_media_file(chat_id, audio_path, caption, reply_to)

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        return await self._send_media_file(chat_id, video_path, caption, reply_to)

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        return await self._send_media_file(chat_id, file_path, caption, reply_to)

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        if self._api is None:
            return
        try:
            await self._api.send_typing(chat_id)
        except Exception:
            pass  # best-effort

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        chat_type = "group" if str(chat_id).startswith("g") else "dm"
        return {"name": str(chat_id), "type": chat_type, "chat_id": str(chat_id)}


# ---------------------------------------------------------------------------
# Plugin registration


def _env_enablement() -> Optional[dict]:
    """Seed ``PlatformConfig.extra`` from env vars during gateway config load."""
    token = os.getenv("XCHAT_ACCESS_TOKEN", "").strip()
    if not token:
        return None
    seed: dict = {"access_token": token}
    for env, key in (
        ("XCHAT_REFRESH_TOKEN", "refresh_token"),
        ("XCHAT_CLIENT_ID", "client_id"),
        ("XCHAT_CLIENT_SECRET", "client_secret"),
        ("XCHAT_USER_ID", "user_id"),
        ("XCHAT_SIGNING_KEY_VERSION", "signing_key_version"),
        ("XCHAT_CONVERSATION_IDS", "conversation_ids"),
        ("XCHAT_POLL_INTERVAL", "poll_interval"),
        ("XCHAT_SEND_READ_RECEIPTS", "send_read_receipts"),
    ):
        val = os.getenv(env, "").strip()
        if val:
            seed[key] = val
    home = os.getenv("XCHAT_HOME_CHANNEL", "").strip()
    if home:
        seed["home_channel"] = {
            "chat_id": home,
            "name": os.getenv("XCHAT_HOME_CHANNEL_NAME", home),
        }
    return seed


async def _standalone_send(
    pconfig,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Optional[List[Any]] = None,
    force_document: bool = False,
) -> Dict[str, Any]:
    """Out-of-process encrypted send for cron / send_message_tool.

    Opens an ephemeral API client + Chat XDK session, seeds the
    conversation key from the conversation's event backlog (or performs the
    conversation-key handshake for a brand-new 1:1 given a bare user id),
    encrypts, sends, and closes. ``media_files`` are encrypted with the
    conversation key, uploaded via the 3-step chat-media flow, and attached
    to the message. ``thread_id`` is accepted for signature parity — X Chat
    has no thread primitive.
    """
    if not HTTPX_AVAILABLE:
        return {"error": "xchat standalone send: httpx not installed"}

    extra = getattr(pconfig, "extra", {}) or {}
    access_token = (extra.get("access_token") or os.getenv("XCHAT_ACCESS_TOKEN", "")).strip()
    if not access_token:
        return {"error": "xchat standalone send: XCHAT_ACCESS_TOKEN not configured"}
    key_blob = _read_key_blob()
    if not key_blob:
        return {"error": "xchat standalone send: private-key blob missing — run `hermes xchat setup`"}
    user_id = str(extra.get("user_id") or os.getenv("XCHAT_USER_ID", "")).strip()
    key_version = str(
        extra.get("signing_key_version") or os.getenv("XCHAT_SIGNING_KEY_VERSION", "1")
    ).strip() or "1"

    api = XChatApi(
        access_token,
        refresh_token=(extra.get("refresh_token") or os.getenv("XCHAT_REFRESH_TOKEN", "")).strip(),
        client_id=(extra.get("client_id") or os.getenv("XCHAT_CLIENT_ID", "")).strip(),
        client_secret=(extra.get("client_secret") or os.getenv("XCHAT_CLIENT_SECRET", "")).strip(),
    )
    try:
        if not user_id:
            me = await api.get_my_user()
            user_id = str(me.get("id") or "")
        if not user_id:
            return {"error": "xchat standalone send: could not resolve bot user id"}

        crypto = XChatCrypto()
        crypto.load_keys(key_blob, key_version)
        crypto.set_identity(user_id)
        crypto.set_cache_keys(True)

        # Seed the conversation key from the backlog (KeyChange events).
        # KeyChange verification needs the participants' signing keys in the
        # SDK store FIRST — decrypt_batch can't verify (and therefore can't
        # seed the conversation key) without them. KeyChange events arrive
        # separately in meta.conversation_key_events.
        page = await api.get_events(chat_id, max_results=50)
        raw_events = page.get("data") or []
        meta = page.get("meta") or {}
        key_events = [k for k in (meta.get("conversation_key_events") or []) if k]
        events_b64 = key_events + [
            e["encoded_event"] for e in raw_events if e.get("encoded_event")
        ]
        canonical = chat_id
        raw_key: Optional[bytes] = None
        raw_key_version: Optional[str] = None
        if events_b64:
            sender_ids = {str(e.get("sender_id")) for e in raw_events if e.get("sender_id")} | {user_id}
            signing_keys: List[Dict[str, str]] = []
            for sender_id in sender_ids:
                try:
                    for pk in await api.get_public_keys(sender_id):
                        signing_keys.append(
                            {
                                "user_id": sender_id,
                                "public_key_version": str(pk.get("public_key_version") or ""),
                                "public_key": pk.get("signing_public_key") or "",
                                "identity_public_key": pk.get("public_key") or "",
                                "identity_public_key_signature": pk.get("identity_public_key_signature") or "",
                            }
                        )
                except Exception:
                    logger.debug("[xchat] standalone public-key fetch failed sender=%s", sender_id)
            if signing_keys:
                crypto.set_signing_keys(signing_keys)
            try:
                batch = crypto.decrypt_batch(events_b64)
                for m in batch.get("messages") or []:
                    conv = (m.get("event") or {}).get("conversation_id")
                    if conv:
                        canonical = str(conv)
                        break
                conv_keys = (batch.get("conversation_keys") or {}).get("keys") or {}
                latest = batch.get("latest_key_version")
                if conv_keys:
                    if latest and str(latest) in conv_keys:
                        raw_key_version = str(latest)
                    else:
                        try:
                            raw_key_version = max(conv_keys, key=lambda v: int(v))
                        except (ValueError, TypeError):
                            raw_key_version = next(iter(conv_keys))
                    raw_key = conv_keys[raw_key_version]
            except Exception as e:
                logger.debug("[xchat] standalone backlog decrypt: %s", e)

        explicit_key: Optional[bytes] = None
        explicit_key_version: Optional[str] = None

        # Encrypt + upload media attachments (needs the RAW conversation key).
        attachments: List[Dict[str, Any]] = []
        media_errors: List[str] = []
        for mf in media_files or []:
            # send_message_tool passes (path, is_voice) tuples; accept bare
            # strings/paths too for direct callers.
            if isinstance(mf, (tuple, list)) and mf:
                mpath = str(mf[0])
            else:
                mpath = str(getattr(mf, "path", None) or mf)
            if raw_key is None:
                media_errors.append(f"{Path(mpath).name}: no raw conversation key")
                continue
            try:
                plaintext = Path(mpath).read_bytes()
                blob = crypto.encrypt_media(plaintext, raw_key)
                media_hash_key = await api.media_upload(canonical, blob)
            except (OSError, XChatApiError, Exception) as e:
                media_errors.append(f"{Path(mpath).name}: {e}")
                continue
            att: Dict[str, Any] = {
                "attachment_type": "media",
                "media_hash_key": media_hash_key,
                "filesize_bytes": len(plaintext),
                "filename": Path(mpath).name,
            }
            dims = detect_image_dimensions(plaintext)
            att["width"], att["height"] = dims if dims else (0, 0)
            attachments.append(att)
        if media_errors:
            logger.warning("[xchat] standalone media skipped: %s", "; ".join(media_errors))

        try:
            body = crypto.encrypt_text(
                canonical, message, attachments=attachments or None
            )
        except ValueError:
            # No verified conversation key. For a bare recipient user id
            # (brand-new 1:1), perform the conversation-key handshake:
            # verify both parties' key bindings, wrap a fresh key for each,
            # and POST it — then encrypt under the raw key directly.
            if not _is_bare_user_id(chat_id):
                return {
                    "error": (
                        "xchat: no verified conversation key — the target must have "
                        "an existing conversation with the bot, or pass the "
                        "recipient's bare user id to start a new one"
                    )
                }
            try:
                init = await _initiate_conversation(api, crypto, user_id, chat_id)
            except (XChatApiError, ValueError) as e:
                return {"error": f"xchat: conversation-key handshake failed: {e}"}
            canonical = init["conversation_id"] or chat_id
            explicit_key = init["conversation_key"]
            explicit_key_version = init["conversation_key_version"]
            body = crypto.encrypt_text(
                canonical,
                message,
                conversation_key=explicit_key,
                conversation_key_version=explicit_key_version,
            )
        _remember_sent(str(body.get("message_id") or ""))
        out = await api.send_message(canonical, body)
        data = out.get("data") or {}
        return {
            "success": True,
            "platform": "xchat",
            "chat_id": canonical,
            "message_id": str(data.get("message_id") or body.get("message_id") or ""),
        }
    except XChatApiError as e:
        return {"error": f"xchat standalone send failed: {e}"}
    except Exception as e:
        return {"error": f"xchat standalone send failed: {e}"}
    finally:
        await api.aclose()


_TARGET_RE = re.compile(r"^(?:\d{1,19}|\d{1,19}[-:]\d{1,19}|g\d{1,19})$")


def _parse_target_ref(target_ref: str):
    """``hermes send xchat:<id>``: a user id, a 1:1 conversation (``A-B`` / ``A:B``) or a group (``g…``)."""
    ref = str(target_ref).strip()
    return (ref.replace(":", "-"), None) if _TARGET_RE.match(ref) else None


def _is_bare_user_id(chat_id: str) -> bool:
    """True for a bare numeric X user id (a 1:1 target with no conversation yet)."""
    return str(chat_id).isdigit()


async def _initiate_conversation(
    api: "XChatApi", crypto: "XChatCrypto", bot_user_id: str, recipient_id: str
) -> Dict[str, Any]:
    """Conversation-key handshake for a brand-new 1:1 conversation.

    Fetches both parties' public keys, verifies each record's
    identity↔signing binding (a substituted identity key must never receive
    the conversation key), wraps a fresh conversation key for every
    participant, and POSTs it to the add-conversation-keys endpoint.

    Returns ``{"conversation_id", "conversation_key", "conversation_key_version"}``.
    """
    participants: List[Dict[str, str]] = []
    for uid in dict.fromkeys((bot_user_id, recipient_id)):  # one entry for a self-chat
        records = await api.get_public_keys(uid)
        if not records:
            raise ValueError(f"user {uid} has no registered X Chat public keys")
        rec = records[0]
        if not crypto.verify_key_binding(
            str(rec.get("public_key") or ""),
            str(rec.get("signing_public_key") or ""),
            str(rec.get("identity_public_key_signature") or ""),
        ):
            raise ValueError(f"public-key binding verification failed for user {uid}")
        participants.append(
            {
                "user_id": uid,
                "public_key": str(rec.get("public_key") or ""),
                "key_version": str(rec.get("public_key_version") or "1"),
            }
        )

    prepared = crypto.prepare_conversation_key_change(participants)
    resp = await api.add_conversation_keys(recipient_id, prepared["body"])
    data = resp.get("data") or {}
    return {
        "conversation_id": str(data.get("conversation_id") or ""),
        "conversation_key": prepared["conversation_key"],
        "conversation_key_version": prepared["conversation_key_version"],
    }


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin loader at startup."""
    import dataclasses

    from gateway.platform_registry import PlatformEntry

    from . import cli as _cli

    # Newer cores parse `hermes send xchat:<conversation id>` through the plugin; older ones lack the field.
    optional = {"parse_target_ref_fn": _parse_target_ref}
    known = {f.name for f in dataclasses.fields(PlatformEntry)}
    ctx.register_platform(
        **{k: v for k, v in optional.items() if k in known},
        name="xchat",
        label="X Chat (encrypted DMs)",
        adapter_factory=lambda cfg: XChatAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["XCHAT_ACCESS_TOKEN"],
        install_hint=(
            "Run: hermes xchat setup  (stores the OAuth2 user token, registers "
            "the bot's E2EE keys, saves the private-key blob)."
        ),
        setup_fn=_cli.gateway_setup,
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="XCHAT_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        allowed_users_env="XCHAT_ALLOWED_USERS",
        allow_all_env="XCHAT_ALLOW_ALL_USERS",
        max_message_length=MAX_MESSAGE_LENGTH,
        emoji="𝕏",
        pii_safe=False,
        allow_update_command=True,
        platform_hint=(
            "You are communicating via X Chat — X's end-to-end encrypted "
            "direct messages, sending as the owner's X account (in their "
            "self-chat, the owner is talking to you directly). Treat replies like regular chat messages: "
            "short and conversational. Markdown is NOT rendered — use plain "
            "text. User identifiers are numeric X user ids; conversation ids "
            "starting with 'g' are group chats. You can send files, images, "
            "voice notes, and videos as encrypted attachments via MEDIA:<path> "
            "tags; inbound attachments are decrypted locally and available "
            "to your vision/file tools."
        ),
    )

    ctx.register_cli_command(
        name="xchat",
        help="Set up and manage the X Chat (encrypted X DMs) integration",
        setup_fn=_cli.register_cli,
        handler_fn=_cli.dispatch,
    )
