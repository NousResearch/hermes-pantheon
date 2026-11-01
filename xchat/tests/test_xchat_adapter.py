"""Unit tests for the X Chat platform plugin.

All tests run offline: the X API layer is replaced with fakes and the Chat
XDK crypto core is replaced with a stub — no chatxdk native module, no
network, no gateway process.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from xchat_plugin import adapter as xchat_adapter
from xchat_plugin.adapter import (
    XChatAdapter,
    _compile_mention_patterns,
    _env_enablement,
    check_requirements,
)
from xchat_plugin.crypto import XChatCrypto, message_text


# ---------------------------------------------------------------------------
# Helpers / fakes


class FakeCrypto:
    """Stands in for XChatCrypto — records calls, no native SDK."""

    def __init__(self) -> None:
        self.encrypted: List[tuple] = []
        self.batch_calls: List[List[str]] = []
        self.signing_keys: List[Dict[str, str]] = []
        self.decrypt_map: Dict[str, Dict[str, Any]] = {}
        self.fail_encrypt: Optional[Exception] = None
        self.replies: List[tuple] = []
        self.media_encrypted: List[bytes] = []
        self.media_decrypted: List[bytes] = []

    def decrypt_one(self, event_b64, conversation_keys=None):
        return self.decrypt_map[event_b64]

    def decrypt_batch(self, events_b64):
        self.batch_calls.append(list(events_b64))
        return {
            "messages": [],
            "conversation_keys": {"keys": {"1": b"k"}},
            "latest_key_version": "1",
            "errors": {},
        }

    def encrypt_text(self, conversation_id, text, *, attachments=None, **kw):
        if self.fail_encrypt is not None:
            raise self.fail_encrypt
        self.encrypted.append((conversation_id, text, attachments))
        return {
            "message_id": "mid-1",
            "encoded_message_create_event": "ZW5j",
            "encoded_message_event_signature": "c2ln",
        }

    def encrypt_reply(self, conversation_id, text, reply_to_event, *, attachments=None):
        if self.fail_encrypt is not None:
            raise self.fail_encrypt
        self.replies.append((conversation_id, text, reply_to_event, attachments))
        return {
            "message_id": "mid-r",
            "encoded_message_create_event": "cmVw",
            "encoded_message_event_signature": "c2ln",
        }

    def encrypt_media(self, plaintext, conversation_key):
        self.media_encrypted.append(bytes(plaintext))
        return b"ENC" + bytes(plaintext)

    def decrypt_media(self, ciphertext, conversation_key):
        self.media_decrypted.append(bytes(ciphertext))
        assert bytes(ciphertext).startswith(b"ENC")
        return bytes(ciphertext)[3:]

    def set_signing_keys(self, keys):
        self.signing_keys = list(keys)


class FakeApi:
    """Stands in for XChatApi — canned responses, records sends."""

    def __init__(self) -> None:
        self.sent: List[tuple] = []
        self.typing: List[str] = []
        self.reads: List[tuple] = []
        self.uploads: List[tuple] = []
        self.key_changes: List[tuple] = []
        self.media_blobs: Dict[str, bytes] = {}
        self.public_keys: Dict[str, List[Dict[str, Any]]] = {}
        self.events_pages: Dict[str, Dict[str, Any]] = {}
        # Optional multi-page feed: {conv_id: {pagination_token_or_None: page}}
        self.paged_events: Dict[str, Dict[Optional[str], Dict[str, Any]]] = {}
        self.conversations: List[str] = []

    async def get_my_user(self):
        return {"id": "999"}

    async def get_public_keys(self, user_id):
        return self.public_keys.get(user_id, [])

    async def get_conversations(self, *, max_results=100, pagination_token=None):
        return {
            "data": [{"conversation_id": c} for c in self.conversations],
            "meta": {},
        }

    async def get_events(self, conversation_id, *, max_results=50, pagination_token=None):
        if conversation_id in self.paged_events:
            return self.paged_events[conversation_id].get(pagination_token, {"data": []})
        if pagination_token is not None:
            return {"data": []}
        return self.events_pages.get(conversation_id, {"data": []})

    async def send_message(self, conversation_id, body):
        self.sent.append((conversation_id, body))
        return {"data": {"message_id": body.get("message_id", ""), "event_id": "evt-echo"}}

    async def send_typing(self, conversation_id):
        self.typing.append(conversation_id)

    async def mark_read(self, conversation_id, seen_until_sequence_id):
        self.reads.append((conversation_id, seen_until_sequence_id))

    async def media_upload(self, conversation_id, encrypted_blob, *, chunk_size=1024 * 1024):
        self.uploads.append((conversation_id, bytes(encrypted_blob)))
        return f"mhk-{len(self.uploads)}"

    async def media_download(self, conversation_id, media_hash_key):
        return self.media_blobs[media_hash_key]

    async def add_conversation_keys(self, conversation_id, body):
        self.key_changes.append((conversation_id, body))
        return {"data": {"conversation_id": "111:999", "sequence_id": "sq1"}}

    async def aclose(self):
        pass


def _make_adapter(monkeypatch: pytest.MonkeyPatch, **extra) -> XChatAdapter:
    monkeypatch.setenv("XCHAT_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("XCHAT_USER_ID", "999")
    cfg = PlatformConfig(enabled=True, token="", extra=dict(extra))
    return XChatAdapter(cfg)


def _wire(adapter: XChatAdapter) -> tuple[FakeApi, FakeCrypto]:
    api, crypto = FakeApi(), FakeCrypto()
    adapter._api = api
    adapter._crypto = crypto
    adapter._bot_user_id = "999"
    return api, crypto


def _capture(adapter: XChatAdapter, monkeypatch: pytest.MonkeyPatch) -> List[MessageEvent]:
    captured: List[MessageEvent] = []

    async def fake_handle(event: MessageEvent) -> None:
        captured.append(event)

    monkeypatch.setattr(adapter, "handle_message", fake_handle)
    return captured


# ---------------------------------------------------------------------------
# check_fn / config


def test_check_requirements_needs_token_and_blob(monkeypatch, tmp_path):
    monkeypatch.delenv("XCHAT_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("XCHAT_PRIVATE_KEYS_B64", raising=False)
    assert check_requirements() is False

    monkeypatch.setenv("XCHAT_ACCESS_TOKEN", "tok")
    assert check_requirements() is False  # no key blob

    monkeypatch.setenv("XCHAT_PRIVATE_KEYS_B64", "YmxvYg==")
    assert check_requirements() is True


def test_env_enablement_seeds_extra(monkeypatch):
    monkeypatch.delenv("XCHAT_ACCESS_TOKEN", raising=False)
    assert _env_enablement() is None

    monkeypatch.setenv("XCHAT_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("XCHAT_USER_ID", "42")
    monkeypatch.setenv("XCHAT_HOME_CHANNEL", "123-456")
    seed = _env_enablement()
    assert seed is not None
    assert seed["access_token"] == "tok"
    assert seed["user_id"] == "42"
    assert seed["home_channel"]["chat_id"] == "123-456"


def test_adapter_reads_config_extra_over_defaults(monkeypatch):
    adapter = _make_adapter(
        monkeypatch,
        poll_interval="30",
        conversation_ids="111-222, g333",
    )
    assert adapter._poll_interval == 30.0
    assert adapter._pinned_conversations == ["111-222", "g333"]
    assert adapter.platform == Platform("xchat")


def test_poll_interval_floor(monkeypatch):
    adapter = _make_adapter(monkeypatch, poll_interval="0.1")
    assert adapter._poll_interval == 2.0


# ---------------------------------------------------------------------------
# Mention gating


def test_mention_patterns_json_and_csv():
    pats = _compile_mention_patterns('["^bot\\\\b"]')
    assert pats[0].pattern == "^bot\\b"
    pats = _compile_mention_patterns("alpha, beta")
    assert len(pats) == 2
    # None → defaults
    assert _compile_mention_patterns(None)


@pytest.mark.asyncio
async def test_group_mention_gate(monkeypatch):
    monkeypatch.setenv("XCHAT_REQUIRE_MENTION", "true")
    adapter = _make_adapter(monkeypatch)
    _wire(adapter)
    captured = _capture(adapter, monkeypatch)

    # Group message without wake word → dropped
    await adapter._dispatch_inbound(
        conv_id="g123", sender_id="5", text="just chatting", message_id="e1", raw={}
    )
    assert captured == []

    # Group message with wake word → dispatched, wake word stripped
    await adapter._dispatch_inbound(
        conv_id="g123", sender_id="5", text="hermes what time is it", message_id="e2", raw={}
    )
    assert len(captured) == 1
    assert captured[0].text == "what time is it"

    # DMs are never gated
    await adapter._dispatch_inbound(
        conv_id="111-222", sender_id="5", text="no wake word", message_id="e3", raw={}
    )
    assert len(captured) == 2


@pytest.mark.asyncio
async def test_dispatch_sets_chat_type(monkeypatch):
    adapter = _make_adapter(monkeypatch)
    _wire(adapter)
    captured = _capture(adapter, monkeypatch)

    await adapter._dispatch_inbound(
        conv_id="g42", sender_id="7", text="hi", message_id="e1", raw={}
    )
    await adapter._dispatch_inbound(
        conv_id="111-999", sender_id="7", text="hi", message_id="e2", raw={}
    )
    assert captured[0].source.chat_type == "group"
    assert captured[1].source.chat_type == "dm"
    assert captured[0].message_type == MessageType.TEXT
    assert captured[1].source.user_id == "7"


# ---------------------------------------------------------------------------
# Poll loop mechanics


@pytest.mark.asyncio
async def test_backlog_seeds_keys_without_replying(monkeypatch):
    adapter = _make_adapter(monkeypatch)
    api, crypto = _wire(adapter)
    captured = _capture(adapter, monkeypatch)

    api.events_pages["111-999"] = {
        "data": [
            {"id": "e1", "encoded_event": "AAA", "sender_id": "111"},
            {"id": "e2", "encoded_event": "BBB", "sender_id": "111"},
        ]
    }
    await adapter._poll_conversation("111-999")

    # Backlog: batch-decrypted for keys, nothing dispatched, cursor advanced.
    assert crypto.batch_calls == [["BBB", "AAA"]]  # newest-first reversed
    assert captured == []
    assert adapter._cursors["111-999"] == "e1"  # newest wire event


@pytest.mark.asyncio
async def test_new_message_dispatched_after_backlog(monkeypatch):
    adapter = _make_adapter(monkeypatch)
    api, crypto = _wire(adapter)
    captured = _capture(adapter, monkeypatch)
    adapter._cursors["111-999"] = "e1"

    crypto.decrypt_map["CCC"] = {
        "type": "Message",
        "id": "e3",
        "sender_id": "111",
        "conversation_id": "111:999",
        "content": {"text": "hello agent"},
    }
    api.events_pages["111-999"] = {
        "data": [{"id": "e3", "encoded_event": "CCC", "sender_id": "111"}]
    }
    await adapter._poll_conversation("111-999")

    assert len(captured) == 1
    ev = captured[0]
    assert ev.text == "hello agent"
    # Reply target uses the canonical id embedded in the signed event.
    assert ev.source.chat_id == "111:999"
    assert adapter._cursors["111-999"] == "e3"

    # Second poll with the same event id → cursor stops it, no double dispatch.
    await adapter._poll_conversation("111-999")
    assert len(captured) == 1


@pytest.mark.asyncio
async def test_cursor_survives_restart_and_prune(monkeypatch):
    """The persisted cursor — not the in-memory dedup set — is the duplicate
    guard: a fresh adapter (restart) with a pruned/empty dedup set must not
    re-reply to already-processed events, and must still process newer ones."""
    adapter = _make_adapter(monkeypatch)
    api, crypto = _wire(adapter)
    captured = _capture(adapter, monkeypatch)
    adapter._cursors["111-999"] = "0"

    crypto.decrypt_map["CCC"] = {
        "type": "Message",
        "id": "5",
        "sender_id": "111",
        "conversation_id": "111:999",
        "content": {"text": "first"},
    }
    api.events_pages["111-999"] = {
        "data": [{"id": "5", "encoded_event": "CCC", "sender_id": "111"}]
    }
    await adapter._poll_conversation("111-999")
    assert len(captured) == 1

    # Simulate a restart: brand-new adapter, empty dedup set, cursor from disk.
    adapter2 = _make_adapter(monkeypatch)
    api2, crypto2 = _wire(adapter2)
    captured2 = _capture(adapter2, monkeypatch)
    assert adapter2._cursors.get("111-999") == "5"  # loaded from disk

    # Old event 5 again + a newer event 7 that arrived while we were down.
    crypto2.decrypt_map["CCC"] = crypto.decrypt_map["CCC"]
    crypto2.decrypt_map["DDD"] = {
        "type": "Message",
        "id": "7",
        "sender_id": "111",
        "conversation_id": "111:999",
        "content": {"text": "while you were down"},
    }
    api2.events_pages["111-999"] = {
        "data": [
            {"id": "7", "encoded_event": "DDD", "sender_id": "111"},
            {"id": "5", "encoded_event": "CCC", "sender_id": "111"},
        ]
    }
    await adapter2._poll_conversation("111-999")

    # Only the new event dispatched — no re-reply to 5, no backlog swallow of 7.
    assert [ev.text for ev in captured2] == ["while you were down"]
    assert adapter2._cursors["111-999"] == "7"


@pytest.mark.asyncio
async def test_burst_larger_than_one_page_is_paginated(monkeypatch):
    """A burst of more than max_results events between polls must not drop
    the overflow — the poll pages back until it reaches the cursor."""
    adapter = _make_adapter(monkeypatch)
    api, crypto = _wire(adapter)
    captured = _capture(adapter, monkeypatch)
    adapter._cursors["111-999"] = "10"

    for n in (11, 12, 13, 14):
        crypto.decrypt_map[f"E{n}"] = {
            "type": "Message",
            "id": str(n),
            "sender_id": "111",
            "conversation_id": "111:999",
            "content": {"text": f"msg {n}"},
        }
    # Newest-first across two pages: page1 = 14,13 (with next_token), page2 = 12,11,10.
    api.paged_events["111-999"] = {
        None: {
            "data": [
                {"id": "14", "encoded_event": "E14", "sender_id": "111"},
                {"id": "13", "encoded_event": "E13", "sender_id": "111"},
            ],
            "meta": {"next_token": "p2"},
        },
        "p2": {
            "data": [
                {"id": "12", "encoded_event": "E12", "sender_id": "111"},
                {"id": "11", "encoded_event": "E11", "sender_id": "111"},
                {"id": "10", "encoded_event": "E10", "sender_id": "111"},
            ],
            "meta": {},
        },
    }
    await adapter._poll_conversation("111-999")

    # All four new events dispatched oldest-first; the pre-cursor event 10 skipped.
    assert [ev.text for ev in captured] == ["msg 11", "msg 12", "msg 13", "msg 14"]
    assert adapter._cursors["111-999"] == "14"


@pytest.mark.asyncio
async def test_message_edit_dispatched(monkeypatch):
    """The feed sometimes returns an edited message ONLY as the edit event —
    skipping edits would make the message invisible to the bot forever."""
    adapter = _make_adapter(monkeypatch)
    api, crypto = _wire(adapter)
    captured = _capture(adapter, monkeypatch)
    adapter._cursors["111-999"] = "e1"

    crypto.decrypt_map["EDIT"] = {
        "type": "MessageEdit",
        "id": "e6",
        "sender_id": "111",
        "conversation_id": "111:999",
        "content": {"text": "edited text"},
    }
    api.events_pages["111-999"] = {
        "data": [{"id": "e6", "encoded_event": "EDIT", "sender_id": "111"}]
    }
    await adapter._poll_conversation("111-999")

    assert len(captured) == 1
    assert captured[0].text == "edited text"


@pytest.mark.asyncio
async def test_own_messages_filtered(monkeypatch):
    adapter = _make_adapter(monkeypatch)
    api, crypto = _wire(adapter)
    captured = _capture(adapter, monkeypatch)
    adapter._cursors["111-999"] = "e1"

    crypto.decrypt_map["DDD"] = {
        "type": "Message",
        "id": "e4",
        "sender_id": "999",  # the bot itself
        "content": {"text": "echo of our own reply"},
    }
    api.events_pages["111-999"] = {
        "data": [{"id": "e4", "encoded_event": "DDD", "sender_id": "999"}]
    }
    await adapter._poll_conversation("111-999")
    assert captured == []


@pytest.mark.asyncio
async def test_self_chat_answers_owner_never_own_replies(monkeypatch):
    """The account's self-chat reaches the agent; Hermes' own sends (this process or cron) never do."""
    adapter = _make_adapter(monkeypatch)
    api, crypto = _wire(adapter)
    captured = _capture(adapter, monkeypatch)
    adapter._cursors["999-999"] = "e1"
    xchat_adapter._remember_sent("uuid-cron")  # sent by another process (cron / hermes send)
    sent = await adapter.send("999-999", "hermes reply")
    assert sent.success

    events = [("OWN", "uuid-owner", "hello hermes"), ("ECHO", "mid-1", "hermes reply"), ("CRON", "uuid-cron", "digest")]
    for blob, msg_id, text in events:
        crypto.decrypt_map[blob] = {"type": "Message", "id": msg_id, "sender_id": "999",
                                    "conversation_id": "999:999", "content": {"text": text}}
    api.events_pages["999-999"] = {"data": [
        {"id": f"e{n}", "encoded_event": blob, "sender_id": "999"} for n, (blob, _, _) in enumerate(events, 2)
    ][::-1]}
    await adapter._poll_conversation("999-999")
    assert [e.text for e in captured] == ["hello hermes"]
    assert captured[0].source.user_id == "999"
    assert adapter.resolved_allowlist_user_ids() == {"999"}

    monkeypatch.setenv("XCHAT_ALLOW_SELF_CHAT", "false")
    off = _make_adapter(monkeypatch)
    assert off.resolved_allowlist_user_ids() == set()


@pytest.mark.asyncio
async def test_keychange_routes_through_batch(monkeypatch):
    adapter = _make_adapter(monkeypatch)
    api, crypto = _wire(adapter)
    captured = _capture(adapter, monkeypatch)
    adapter._cursors["111-999"] = "e1"

    crypto.decrypt_map["KEY"] = {"type": "KeyChange", "id": "e5"}
    api.events_pages["111-999"] = {
        "data": [{"id": "e5", "encoded_event": "KEY", "sender_id": "111"}]
    }
    await adapter._poll_conversation("111-999")

    assert crypto.batch_calls == [["KEY"]]
    assert captured == []
    assert adapter._conversation_keys["111-999"] == {"1": b"k"}


@pytest.mark.asyncio
async def test_signing_keys_fetched_once_per_sender(monkeypatch):
    adapter = _make_adapter(monkeypatch)
    api, crypto = _wire(adapter)
    api.public_keys["111"] = [
        {
            "public_key_version": "3",
            "signing_public_key": "SPK",
            "public_key": "IPK",
            "identity_public_key_signature": "SIG",
        }
    ]
    events = [{"id": "e1", "sender_id": "111"}]
    await adapter._register_signing_keys(events)
    await adapter._register_signing_keys(events)  # second call — cached

    assert len(crypto.signing_keys) == 1
    entry = crypto.signing_keys[0]
    assert entry["user_id"] == "111"
    assert entry["public_key"] == "SPK"
    assert entry["identity_public_key"] == "IPK"


@pytest.mark.asyncio
async def test_discovery_adds_conversations(monkeypatch):
    adapter = _make_adapter(monkeypatch)
    api, _ = _wire(adapter)
    api.conversations = ["111-222", "g333"]
    await adapter._discover_conversations()
    assert adapter._conversations == {"111-222", "g333"}


def test_dedup_prune_bounds_memory(monkeypatch):
    adapter = _make_adapter(monkeypatch)
    now = time.time()
    for i in range(xchat_adapter.DEDUP_MAX_SIZE + 100):
        adapter._seen_event_ids[f"e{i}"] = now + i
    adapter._prune_dedup()
    assert len(adapter._seen_event_ids) <= xchat_adapter.DEDUP_MAX_SIZE
    # Newest entries survive the prune.
    assert f"e{xchat_adapter.DEDUP_MAX_SIZE + 99}" in adapter._seen_event_ids


# ---------------------------------------------------------------------------
# Outbound


@pytest.mark.asyncio
async def test_send_encrypts_and_posts(monkeypatch):
    adapter = _make_adapter(monkeypatch)
    api, crypto = _wire(adapter)

    result = await adapter.send("111:999", "hi there")
    assert result.success
    assert result.message_id == "mid-1"
    assert crypto.encrypted == [("111:999", "hi there", None)]
    conv, body = api.sent[0]
    assert conv == "111:999"
    assert body["encoded_message_create_event"] == "ZW5j"
    # Echo suppression: returned event id marked as seen.
    assert "evt-echo" in adapter._seen_event_ids


@pytest.mark.asyncio
async def test_send_without_conversation_key(monkeypatch):
    adapter = _make_adapter(monkeypatch)
    _, crypto = _wire(adapter)
    crypto.fail_encrypt = ValueError("no key")

    result = await adapter.send("111:999", "hi")
    assert not result.success
    assert "conversation key" in (result.error or "")


@pytest.mark.asyncio
async def test_send_disconnected(monkeypatch):
    adapter = _make_adapter(monkeypatch)
    result = await adapter.send("111:999", "hi")
    assert not result.success


@pytest.mark.asyncio
async def test_get_chat_info_types(monkeypatch):
    adapter = _make_adapter(monkeypatch)
    assert (await adapter.get_chat_info("g123"))["type"] == "group"
    assert (await adapter.get_chat_info("111-222"))["type"] == "dm"


# ---------------------------------------------------------------------------
# Crypto wrapper (fake Chat object — no native SDK)


class _FakePayload:
    message_id = "m1"
    encrypted_content = "ENC"
    encoded_event_signature = "SIG"


class _FakeChat:
    def __init__(self) -> None:
        self.identity = None
        self.cache = None
        self.imported = None

    def import_keys(self, blob, version=None):
        self.imported = (blob, version)

    def set_identity(self, user_id, version):
        self.identity = (user_id, version)

    def set_cache_keys(self, enabled):
        self.cache = enabled

    def set_signing_keys(self, keys):
        self.signing = keys

    def encrypt_message(self, conversation_id, text):
        return _FakePayload()

    def decrypt_event(self, event_b64, conversation_keys, signing_keys):
        return {"type": "Message", "content": {"text": "plain"}}

    def decrypt_events(self, events, signing_keys):
        return {"messages": [{"event": {"type": "Message"}}], "conversation_keys": {}, "errors": {}}


def test_crypto_load_keys_and_identity():
    crypto = XChatCrypto(chat=_FakeChat())
    crypto.load_keys("YmxvYg==", "7")  # b64("blob")
    assert crypto.chat.imported == (b"blob", "7")
    assert crypto.signing_key_version == "7"
    crypto.set_identity("42")
    assert crypto.chat.identity == ("42", "7")


def test_crypto_encrypt_shapes_send_body():
    crypto = XChatCrypto(chat=_FakeChat())
    body = crypto.encrypt_text("1:2", "hello")
    assert body == {
        "message_id": "m1",
        "encoded_message_create_event": "ENC",
        "encoded_message_event_signature": "SIG",
    }


def test_message_text_extraction():
    assert message_text({"type": "Message", "content": {"text": "x"}}) == "x"
    assert message_text({"type": "KeyChange"}) is None
    assert message_text({"type": "Message", "content": {}}) is None


# ---------------------------------------------------------------------------
# Registry integration


def test_platform_registry_entry_parity():
    """Every parity knob must be populated on the registered entry."""
    from gateway.platform_registry import PlatformEntry

    captured: Dict[str, Any] = {}

    class Ctx:
        class manifest:
            name = "xchat"

        def register_platform(self, **kwargs):
            captured.update(kwargs)

        def register_cli_command(self, **kwargs):
            captured["cli"] = kwargs

    xchat_adapter.register(Ctx())

    assert captured["name"] == "xchat"
    assert captured["allowed_users_env"] == "XCHAT_ALLOWED_USERS"
    assert captured["allow_all_env"] == "XCHAT_ALLOW_ALL_USERS"
    assert captured["cron_deliver_env_var"] == "XCHAT_HOME_CHANNEL"
    assert callable(captured["standalone_sender_fn"])
    assert callable(captured["setup_fn"])
    assert callable(captured["env_enablement_fn"])
    assert captured["platform_hint"]
    assert captured["max_message_length"] > 0
    assert captured["cli"]["name"] == "xchat"
    # The kwargs must construct a valid PlatformEntry.
    entry_kwargs = {k: v for k, v in captured.items() if k != "cli"}
    entry = PlatformEntry(**entry_kwargs)
    assert entry.name == "xchat"


# ---------------------------------------------------------------------------
# Standalone send (config-error paths — no network)


@pytest.mark.asyncio
async def test_standalone_send_requires_token(monkeypatch):
    monkeypatch.delenv("XCHAT_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("XCHAT_PRIVATE_KEYS_B64", raising=False)
    cfg = PlatformConfig(enabled=True, extra={})
    out = await xchat_adapter._standalone_send(cfg, "111", "msg")
    assert "XCHAT_ACCESS_TOKEN" in out["error"]


@pytest.mark.asyncio
async def test_standalone_send_requires_key_blob(monkeypatch):
    monkeypatch.setenv("XCHAT_ACCESS_TOKEN", "tok")
    monkeypatch.delenv("XCHAT_PRIVATE_KEYS_B64", raising=False)
    cfg = PlatformConfig(enabled=True, extra={})
    out = await xchat_adapter._standalone_send(cfg, "111", "msg")
    assert "private-key blob" in out["error"]


class StrictCrypto:
    """Refuses to seed / use conversation keys until set_signing_keys is
    called — mirrors the real SDK, where KeyChange verification (and thus
    key seeding) is impossible without the participants' signing keys."""

    def __init__(self) -> None:
        self.signing_keys: List[Dict[str, str]] = []
        self.keys_seeded = False
        self.encrypted: List[tuple] = []

    def load_keys(self, blob, version="1"):
        pass

    def set_identity(self, user_id):
        pass

    def set_cache_keys(self, enabled=True):
        pass

    def set_signing_keys(self, keys):
        self.signing_keys = list(keys)

    def decrypt_batch(self, events_b64):
        if not self.signing_keys:
            # Real SDK: KeyChange verification fails, no keys extracted.
            return {"messages": [], "conversation_keys": {}, "errors": {"all": "unverified"}}
        self.keys_seeded = True
        return {
            "messages": [{"event": {"conversation_id": "111:999"}}],
            "conversation_keys": {"keys": {"1": b"k"}},
            "errors": {},
        }

    def encrypt_text(self, conversation_id, text, *, attachments=None, **kw):
        if not self.keys_seeded:
            raise ValueError("no verified conversation key")
        self.encrypted.append((conversation_id, text))
        return {
            "message_id": "mid-9",
            "encoded_message_create_event": "ZW5j",
            "encoded_message_event_signature": "c2ln",
        }


@pytest.mark.asyncio
async def test_standalone_send_registers_signing_keys_before_decrypt(monkeypatch):
    """Full standalone-send path against a strict crypto fake: without the
    sender's signing keys pushed FIRST, key seeding fails and the send would
    error — the regression the original FakeCrypto (keys unconditionally
    handed out) let slip through."""
    monkeypatch.setenv("XCHAT_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("XCHAT_PRIVATE_KEYS_B64", "YmxvYg==")
    monkeypatch.setenv("XCHAT_USER_ID", "999")

    api = FakeApi()
    api.public_keys["111"] = [
        {
            "public_key_version": "3",
            "signing_public_key": "SPK",
            "public_key": "IPK",
            "identity_public_key_signature": "SIG",
        }
    ]
    api.events_pages["111-999"] = {
        "data": [{"id": "e1", "encoded_event": "KEYCHG", "sender_id": "111"}]
    }
    crypto = StrictCrypto()
    monkeypatch.setattr(xchat_adapter, "XChatApi", lambda *a, **kw: api)
    monkeypatch.setattr(xchat_adapter, "XChatCrypto", lambda: crypto)

    cfg = PlatformConfig(enabled=True, extra={})
    out = await xchat_adapter._standalone_send(cfg, "111-999", "hello")

    assert out.get("success") is True, out
    # Signing keys were pushed before decrypt_batch could seed the key.
    assert crypto.signing_keys and crypto.signing_keys[0]["user_id"] == "111"
    assert crypto.keys_seeded
    # Canonical conversation id (from the decrypted backlog) used for the send.
    assert out["chat_id"] == "111:999"
    assert api.sent and api.sent[0][0] == "111:999"


@pytest.mark.asyncio
async def test_standalone_send_no_key_without_signing_roster(monkeypatch):
    """If the sender's public keys can't be fetched, the key never seeds and
    the send reports the no-verified-key error instead of crashing."""
    monkeypatch.setenv("XCHAT_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("XCHAT_PRIVATE_KEYS_B64", "YmxvYg==")
    monkeypatch.setenv("XCHAT_USER_ID", "999")

    api = FakeApi()  # no public_keys registered → empty roster
    api.events_pages["111-999"] = {
        "data": [{"id": "e1", "encoded_event": "KEYCHG", "sender_id": "111"}]
    }
    crypto = StrictCrypto()
    monkeypatch.setattr(xchat_adapter, "XChatApi", lambda *a, **kw: api)
    monkeypatch.setattr(xchat_adapter, "XChatCrypto", lambda: crypto)

    cfg = PlatformConfig(enabled=True, extra={})
    out = await xchat_adapter._standalone_send(cfg, "111-999", "hello")
    assert "no verified conversation key" in out.get("error", "")


# ---------------------------------------------------------------------------
# Media, replies, read receipts, key-events meta


@pytest.mark.asyncio
async def test_inbound_attachment_downloaded_and_decrypted(monkeypatch, tmp_path):
    """Encrypted inbound media is downloaded, decrypted with the event's
    key version, cached locally, and surfaced on the MessageEvent."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = _make_adapter(monkeypatch)
    api, crypto = _wire(adapter)
    captured = _capture(adapter, monkeypatch)
    adapter._cursors["111-999"] = "e1"
    adapter._conversation_keys["111-999"] = {"1": b"k"}
    adapter._latest_key_version["111-999"] = "1"

    # PNG magic so mime sniffing (real chatxdk helper unavailable → fallback)
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 32
    api.media_blobs["mhk-img"] = b"ENC" + png
    crypto.decrypt_map["MED"] = {
        "type": "Message",
        "id": "e9",
        "sender_id": "111",
        "conversation_id": "111:999",
        "key_version": "1",
        "content": {
            "text": "look at this",
            "attachments": [{"media_hash_key": "mhk-img", "filename": "photo.png"}],
        },
    }
    api.events_pages["111-999"] = {
        "data": [{"id": "e9", "encoded_event": "MED", "sender_id": "111"}]
    }
    # detect_mime_type needs the native SDK — stub it.
    monkeypatch.setattr(xchat_adapter, "detect_mime_type", lambda b: "image/png")

    await adapter._poll_conversation("111-999")

    assert len(captured) == 1
    ev = captured[0]
    assert ev.text == "look at this"
    assert len(ev.media_urls) == 1
    assert ev.media_types == ["image/png"]
    from pathlib import Path as _P

    assert _P(ev.media_urls[0]).read_bytes() == png
    assert crypto.media_decrypted == [b"ENC" + png]


@pytest.mark.asyncio
async def test_outbound_media_encrypt_upload_attach(monkeypatch, tmp_path):
    adapter = _make_adapter(monkeypatch)
    api, crypto = _wire(adapter)
    adapter._conversation_keys["111-999"] = {"2": b"k2"}
    adapter._latest_key_version["111-999"] = "2"

    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF-fake")
    monkeypatch.setattr(xchat_adapter, "detect_image_dimensions", lambda b: None)

    result = await adapter.send_document("111:999", str(f))
    assert result.success, result.error
    # Encrypted before upload...
    assert crypto.media_encrypted == [b"%PDF-fake"]
    assert api.uploads and api.uploads[0][1] == b"ENC%PDF-fake"
    # ...and the send body carried the attachment descriptor.
    conv, text, attachments = crypto.encrypted[0]
    assert attachments and attachments[0]["media_hash_key"] == "mhk-1"
    assert attachments[0]["filename"] == "doc.pdf"


@pytest.mark.asyncio
async def test_outbound_media_without_key_fails_cleanly(monkeypatch, tmp_path):
    adapter = _make_adapter(monkeypatch)
    _wire(adapter)
    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    result = await adapter.send_image("111:999", str(f))
    assert not result.success
    assert "conversation key" in (result.error or "")


@pytest.mark.asyncio
async def test_native_threaded_reply_uses_cached_event(monkeypatch):
    adapter = _make_adapter(monkeypatch)
    api, crypto = _wire(adapter)
    target = "RU5DT0RFRA=="  # the encoded event as served; the SDK rejects a decrypted dict
    adapter._cache_event("111:999", "e5", target)

    result = await adapter.send("111:999", "threaded answer", reply_to="e5")
    assert result.success
    assert crypto.replies == [("111:999", "threaded answer", target, None)]
    assert crypto.encrypted == []  # took the reply path, not plain encrypt

    # Unknown reply target falls back to a plain send.
    result = await adapter.send("111:999", "plain", reply_to="nope")
    assert result.success
    assert crypto.encrypted == [("111:999", "plain", None)]


@pytest.mark.asyncio
async def test_meta_key_events_absorbed_before_messages(monkeypatch):
    """KeyChange events arrive in meta.conversation_key_events — they must
    seed the key cache before this poll's messages are decrypted."""
    adapter = _make_adapter(monkeypatch)
    api, crypto = _wire(adapter)
    captured = _capture(adapter, monkeypatch)
    adapter._cursors["111-999"] = "e1"

    crypto.decrypt_map["MSG"] = {
        "type": "Message",
        "id": "e2",
        "sender_id": "111",
        "conversation_id": "111:999",
        "content": {"text": "post-rotation"},
    }
    api.events_pages["111-999"] = {
        "data": [{"id": "e2", "encoded_event": "MSG", "sender_id": "111"}],
        "meta": {"conversation_key_events": ["KEYCHG"]},
    }
    await adapter._poll_conversation("111-999")

    assert crypto.batch_calls == [["KEYCHG"]]
    assert adapter._conversation_keys["111-999"] == {"1": b"k"}
    assert adapter._latest_key_version["111-999"] == "1"
    assert [ev.text for ev in captured] == ["post-rotation"]


@pytest.mark.asyncio
async def test_read_receipts_opt_in(monkeypatch):
    adapter = _make_adapter(monkeypatch)
    api, crypto = _wire(adapter)
    _capture(adapter, monkeypatch)
    adapter._cursors["111-999"] = "e1"
    crypto.decrypt_map["CCC"] = {
        "type": "Message",
        "id": "e3",
        "sender_id": "111",
        "conversation_id": "111:999",
        "content": {"text": "hi"},
    }
    api.events_pages["111-999"] = {
        "data": [{"id": "e3", "encoded_event": "CCC", "sender_id": "111", "sequence_id": "sq3"}]
    }

    # Default: off.
    await adapter._poll_conversation("111-999")
    assert api.reads == []

    # Opt in.
    adapter2 = _make_adapter(monkeypatch, send_read_receipts=True)
    api2, crypto2 = _wire(adapter2)
    _capture(adapter2, monkeypatch)
    adapter2._cursors["111-888"] = "e1"
    crypto2.decrypt_map["CCC"] = crypto.decrypt_map["CCC"]
    api2.events_pages["111-888"] = api.events_pages["111-999"]
    await adapter2._poll_conversation("111-888")
    assert api2.reads == [("111-888", "sq3")]


@pytest.mark.asyncio
async def test_inbound_reply_context_propagates(monkeypatch):
    adapter = _make_adapter(monkeypatch)
    api, crypto = _wire(adapter)
    captured = _capture(adapter, monkeypatch)
    adapter._cursors["111-999"] = "e1"
    crypto.decrypt_map["RPL"] = {
        "type": "Message",
        "id": "e7",
        "sender_id": "111",
        "conversation_id": "111:999",
        # Shape the real Chat XDK returns for a threaded reply.
        "content": {
            "text": "and this one?",
            "replying_to_preview": {
                "sender_id": "999",
                "message_text": "earlier bot answer",
                "replying_to_message_sequence_id": "e4",
            },
        },
    }
    api.events_pages["111-999"] = {
        "data": [{"id": "e7", "encoded_event": "RPL", "sender_id": "111"}]
    }
    await adapter._poll_conversation("111-999")

    ev = captured[0]
    assert ev.reply_to_message_id == "e4"
    assert ev.reply_to_text == "earlier bot answer"
    assert ev.reply_to_is_own_message is True


@pytest.mark.asyncio
async def test_standalone_new_conversation_handshake(monkeypatch):
    """Standalone send to a bare user id with no existing conversation
    performs the verified key handshake, then sends under the fresh key."""
    monkeypatch.setenv("XCHAT_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("XCHAT_PRIVATE_KEYS_B64", "YmxvYg==")
    monkeypatch.setenv("XCHAT_USER_ID", "999")

    api = FakeApi()
    api.public_keys["999"] = [
        {"public_key_version": "1", "public_key": "BOT-IPK",
         "signing_public_key": "BOT-SPK", "identity_public_key_signature": "BOT-SIG"}
    ]
    api.public_keys["111"] = [
        {"public_key_version": "2", "public_key": "USR-IPK",
         "signing_public_key": "USR-SPK", "identity_public_key_signature": "USR-SIG"}
    ]

    class HandshakeCrypto(FakeCrypto):
        def __init__(self):
            super().__init__()
            self.bindings: List[tuple] = []
            self.prepared = False
            self.explicit_key_used = None

        def load_keys(self, blob, version="1"):
            pass

        def set_identity(self, user_id):
            pass

        def set_cache_keys(self, enabled=True):
            pass

        def verify_key_binding(self, ipk, spk, sig):
            self.bindings.append((ipk, spk, sig))
            return True

        def prepare_conversation_key_change(self, public_keys, *, conversation_id=None):
            self.prepared = True
            return {
                "body": {"conversation_key_version": "1",
                         "conversation_participant_keys": [], "action_signatures": []},
                "conversation_key": b"fresh-key",
                "conversation_key_version": "1",
            }

        def encrypt_text(self, conversation_id, text, *, attachments=None,
                         conversation_key=None, conversation_key_version=None):
            if conversation_key is None:
                raise ValueError("no verified conversation key")
            self.explicit_key_used = conversation_key
            return {
                "message_id": "mid-new",
                "encoded_message_create_event": "bmV3",
                "encoded_message_event_signature": "c2ln",
            }

    crypto = HandshakeCrypto()
    monkeypatch.setattr(xchat_adapter, "XChatApi", lambda *a, **kw: api)
    monkeypatch.setattr(xchat_adapter, "XChatCrypto", lambda: crypto)

    cfg = PlatformConfig(enabled=True, extra={})
    out = await xchat_adapter._standalone_send(cfg, "111", "hello new friend")

    assert out.get("success") is True, out
    # Both parties' bindings verified, key change POSTed, canonical id adopted.
    assert len(crypto.bindings) == 2
    assert crypto.prepared
    assert api.key_changes and api.key_changes[0][0] == "111"
    assert crypto.explicit_key_used == b"fresh-key"
    assert out["chat_id"] == "111:999"


@pytest.mark.asyncio
async def test_conversation_started_while_running_is_not_backlog(monkeypatch):
    """A DM that opens a new conversation after startup gets a reply; only conversations
    present at startup have a backlog to skip."""
    adapter = _make_adapter(monkeypatch)
    api, crypto = _wire(adapter)
    captured = _capture(adapter, monkeypatch)
    await adapter._discover_conversations()  # startup pass: nothing yet

    api.conversations = ["111-999"]
    crypto.decrypt_map["NEW"] = {
        "type": "Message", "id": "e1", "sender_id": "111",
        "conversation_id": "111:999", "content": {"text": "first ever message"},
    }
    api.events_pages["111-999"] = {"data": [{"id": "e1", "encoded_event": "NEW", "sender_id": "111"}]}
    await adapter._discover_conversations()
    await adapter._poll_conversation("111-999")
    assert [e.text for e in captured] == ["first ever message"]


@pytest.mark.asyncio
async def test_idle_conversation_not_reread(monkeypatch):
    """Events are billed per resource returned: a conversation whose newest event is at the
    cursor must not get an events read."""
    adapter = _make_adapter(monkeypatch)
    api, _ = _wire(adapter)
    adapter._running = True
    adapter._poll_interval = 0
    adapter._conversations = {"111-999"}
    adapter._cursors["111-999"] = "e5"
    reads: list = []

    async def get_conversations(**_kw):
        return {"data": [{"id": "111-999", "newest_event_id": "e5"}], "meta": {}}

    async def poll(conv_id):
        reads.append(conv_id)
        adapter._running = False

    monkeypatch.setattr(api, "get_conversations", get_conversations, raising=False)
    monkeypatch.setattr(adapter, "_poll_conversation", poll)

    async def stop_after_one_cycle(_s):
        adapter._running = False

    monkeypatch.setattr(xchat_adapter.asyncio, "sleep", stop_after_one_cycle)
    await adapter._run_poll_loop()
    assert reads == []


def test_send_target_forms():
    """`hermes send xchat:<id>` accepts the ids the API and events use; anything else is refused."""
    parse = xchat_adapter._parse_target_ref
    assert parse("111-999") == ("111-999", None)
    assert parse("111:999") == ("111-999", None)
    assert parse("999") == ("999", None)
    assert parse("g12345") == ("g12345", None)
    assert parse("#general") is None


@pytest.mark.asyncio
async def test_degraded_discovery_polls_known_conversations(monkeypatch, tmp_path):
    monkeypatch.setattr(xchat_adapter, "_state_dir", lambda: tmp_path)
    adapter = _make_adapter(monkeypatch)
    api, _crypto = _wire(adapter)
    adapter._conversations = {"111:999"}

    calls = {"n": 0}

    async def throttled(**_kw):
        calls["n"] += 1
        raise xchat_adapter.XChatRateLimited("rate limited", int(time.time()) + 600)

    api.get_conversations = throttled

    # The throttled list degrades to the known set instead of ending the cycle.
    assert await adapter._discover_or_degrade() == {}
    assert calls["n"] == 1
    assert adapter._discovery_blocked_until > 0

    # While the block is live the list is not called again.
    assert await adapter._discover_or_degrade() == {}
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_degraded_discovery_without_known_conversations_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(xchat_adapter, "_state_dir", lambda: tmp_path)
    adapter = _make_adapter(monkeypatch)
    api, _crypto = _wire(adapter)
    adapter._conversations = set()

    async def throttled(**_kw):
        raise xchat_adapter.XChatRateLimited("rate limited", int(time.time()) + 600)

    api.get_conversations = throttled

    with pytest.raises(RuntimeError):
        await adapter._discover_or_degrade()


def test_known_conversations_persist_across_restart(monkeypatch, tmp_path):
    monkeypatch.setattr(xchat_adapter, "_state_dir", lambda: tmp_path)
    adapter = _make_adapter(monkeypatch)
    adapter._conversations = {"111:999"}
    adapter._persist_conversations()

    path = tmp_path / "conversations.json"
    assert path.exists()
    assert oct(path.stat().st_mode)[-3:] == "600"

    second = _make_adapter(monkeypatch)
    second._load_conversations()
    assert "111:999" in second._conversations


def test_known_conversations_recover_from_cursor_state(monkeypatch, tmp_path):
    monkeypatch.setattr(xchat_adapter, "_state_dir", lambda: tmp_path)
    adapter = _make_adapter(monkeypatch)
    adapter._conversations = set()
    adapter._cursors = {"111-999": "e1"}
    adapter._load_conversations()
    assert "111-999" in adapter._conversations
