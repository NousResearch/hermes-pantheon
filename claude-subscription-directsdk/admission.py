"""Request-scoped native HTTP admission; credentials are forwarded, never persisted."""
import base64
import codecs
import copy
import gzip
import http.client
from http.server import BaseHTTPRequestHandler, HTTPServer
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import ssl
import sys
import threading
from urllib.parse import unquote, urlsplit
import urllib.request


UNCACHEABLE = ('thinking', 'redacted_thinking')
# Anthropic's cache lookup checks at most this many positions back from a breakpoint, the breakpoint
# itself included, and allows this many breakpoints across tools, system and messages.
LOOKBACK = 20
MAX_BREAKPOINTS = 4
# Longest byte silence after the headers before the stream counts as dead. Anthropic documents no ping
# cadence ("any number of ping events"); this is native's own byte-level watchdog against api.anthropic.com
# (180 s, native 2.1.263-2.1.286). Behind this relay native widens its watchdog to 300 s, the same as Hermes'
# compression floor and stall detector, so a silent upstream surfaced as a stall instead of this error.
UPSTREAM_IDLE_SECONDS = 180
# Probe a quiet upstream socket so a dead path fails in ~30 s instead of waiting on TCP retransmits.
KEEPALIVE = (('TCP_KEEPIDLE', 15), ('TCP_KEEPALIVE', 15), ('TCP_KEEPINTVL', 5), ('TCP_KEEPCNT', 3))
GZIP_HOSTS = ('api.anthropic.com',)
_gzip_latched = None
_GZIP_CA_ENV = ('SSL_CERT_FILE', 'SSL_CERT_DIR', 'REQUESTS_CA_BUNDLE', 'CURL_CA_BUNDLE', 'NODE_EXTRA_CA_CERTS')
# Claude Code 2.1.302's origin-400 test. JavaScript's `$` never matches before a trailing newline, hence `\Z`;
# _JS_TRIM is what String.prototype.trimEnd removes (str.rstrip() keeps U+FEFF, strips U+001C-001F and U+0085).
_GO_BAD_JSON = re.compile(r'^bad json: (?:invalid character |unexpected EOF\Z|unexpected end of JSON input\Z|EOF\Z)')
_JS_TRIM = ' \t\n\v\f\r\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff'


def _gzip_eligible(gate, payload, headers):
    target = gate.upstream
    return (target.hostname in GZIP_HOSTS and target.port in (None, 443) and not gate.proxy
            and not any(name in os.environ for name in _GZIP_CA_ENV)
            and os.environ.get('CLAUDE_CODE_GZIP_REQUEST_BODIES', '').lower() not in ('0', 'false', 'no', 'off')
            and len(payload) >= 4096 and _gzip_latched is None and 'Content-Encoding' not in headers)


def _rejection_kind(response, body):
    if response.status not in (400, 403, 415):
        return None
    if not (response.getheader('request-id') or '').startswith('req_') and response.getheader('cf-ray') is None:
        return 'non_anthropic_rejection'
    if response.status == 415:
        return 'unsupported_encoding'
    if response.status != 400:
        return None
    text = body[:8192].decode('utf-8', errors='replace')
    try:
        value = json.loads(text)
    except ValueError:
        return 'invalid_json_at_origin' if _GO_BAD_JSON.match(text.rstrip(_JS_TRIM)) else None
    if not isinstance(value, dict):
        return None
    error = value.get('error')
    if isinstance(error, dict):
        details, message = error.get('details'), error.get('message')
        if (isinstance(details, dict) and details.get('error_code') == 'request_body_encoding_unsupported') \
                or (isinstance(message, str) and message.startswith('Unsupported Content-Encoding')):
            return 'unsupported_encoding'
        # The message prefix counts only in a full Anthropic error envelope; the bad_json string is matched untrimmed.
        if value.get('type') == 'error' and isinstance(error.get('type'), str) and isinstance(message, str) \
                and message.startswith(('The request body is not valid JSON',
                                        'Failed to parse request: could not parse request body as JSON')):
            return 'invalid_json_at_origin'
    elif value.get('reason') == 'bad_json' and isinstance(error, str) and _GO_BAD_JSON.match(error):
        return 'invalid_json_at_origin'
    return None


class UpstreamIdle(OSError):
    """The upstream sent headers, then nothing for UPSTREAM_IDLE_SECONDS."""


def keepalive(sock):
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except OSError:
        return
    for name, value in KEEPALIVE:
        if hasattr(socket, name):
            try:
                sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, name), value)
            except OSError:
                pass  # Not every platform accepts every knob (older Windows, macOS spellings).


class QueriedTurnMismatch(ValueError):
    """The native request could not be reconciled with Hermes' tool results."""


# Winsock: shutdown() does not wake a recv blocked in another thread; closesocket() cancels pending calls.
# Without it close() waits for a hung upstream (the single-threaded relay is stuck in getresponse()).
# socket.close() is not enough: http.client holds makefile() refs, so it never reaches closesocket().
_CANCEL_BY_CLOSE = os.name == 'nt'


def _plain(block):
    return {k: v for k, v in block.items() if k != 'cache_control'} if isinstance(block, dict) else block


def _breakpoints(value):
    """Every ``cache_control`` in ``value``: nested ones (tool_result content) and a top-level one included."""
    count, pending = 0, [value]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            count += 'cache_control' in node
            pending += (v for k, v in node.items() if k != 'cache_control')
        elif isinstance(node, list):
            pending += node
    return count


def _frame_first(messages, queried):
    """Send Hermes' first user turn without the context native prepends to it (#77, #195).

    On the opening request native puts its per-request context (the account-email reminder,
    and any CLAUDE.md or auto-memory contents it loaded) *before* the frame Hermes queried; on
    every later request that turn is replayed without it, and later turns' prepended context
    is already dropped by ``stable_request_context``. Kept, that text sat in the user's turn,
    where Claude read Claude Code's own notes as an injection attempt by the user (#195), and
    nothing of the first turn recurred, so call #2 re-wrote all of it, which is most of a cron
    run whose first turn carries a source pack. Dropping the prepended text blocks sends the
    first turn exactly as every later request replays it. A cache marker on a dropped block
    moves onto the frame with setdefault, never multiplying.

    Only the conversation's first turn is touched (no assistant message yet), only when the
    queried frame occurs exactly once, and only when everything before it is plain text.
    Returns whether the turn changed."""
    if any(m.get('role') == 'assistant' for m in messages):
        return False
    first = next((m for m in messages if m.get('role') == 'user'), {})
    content = first.get('content')
    if not isinstance(content, list) or not queried or len(content) <= len(queried):
        return False
    want = [_plain(b) for b in queried]
    starts = [k for k in range(len(content) - len(queried) + 1)
              if [_plain(b) for b in content[k:k + len(queried)]] == want]
    if len(starts) != 1 or starts[0] == 0:
        return False
    k = starts[0]
    if not all(isinstance(b, dict) and b.get('type') == 'text' and set(b) <= {'type', 'text', 'cache_control'}
               for b in content[:k]):
        return False
    mark = next((b['cache_control'] for b in content[:k] if 'cache_control' in b), None)
    del content[:k]
    if mark is not None:
        content[0].setdefault('cache_control', mark)
    return True


# The only trailing system text the relay moves: native 2.1.299's date line, exactly as its opening request ends the
# environment message. Any other trailing text stays put, since moving text that varies per request into the
# environment message would change the prefix from message 1 on every request.
_DATE_LINE = re.compile(r"Today's date is \d{4}-\d{2}-\d{2}\.")


def stable_request_context(payload, queried):
    """Keep native's per-request context in the prefix later per-call requests replay.

    Native splits today's date out of the environment into a trailing system message
    and prepends reminders to the newest user frame. Neither position recurs when
    signed thinking is replayed (#40), and new user turns lose their cache prefix (#36).
    Append that date line (no other trailing text) after two newlines to the first system
    message, keeping a string a string; drop only plain text before the whole queried frame,
    found exactly once, on later user turns, which native already omits from tool-result steps. Removed cache markers move with setdefault, never multiply.
    Warm sessions skip this transform at the relay. Assistant text/thinking, tool-result-only
    turns, opening-turn reminders and top-level system/tools are never touched.
    An already-stable or unexpectedly shaped payload forwards unchanged."""
    try:
        body = json.loads(payload)
        messages = body['messages']
        if not isinstance(messages, list) or not all(isinstance(m, dict) for m in messages):
            return payload
        for message in messages:
            content = message['content']
            if message.get('role') not in ('system', 'user', 'assistant') or not isinstance(content, (str, list)):
                return payload
            if isinstance(content, list) and any(not isinstance(b, dict) or not isinstance(b.get('type'), str)
                    or (b['type'] == 'text' and not isinstance(b.get('text'), str)) for b in content):
                return payload
        if queried is not None and (not isinstance(queried, list) or any(not isinstance(b, dict) for b in queried)):
            return payload
        changed = False
        last = messages[-1] if messages else {}
        env = next((m for m in messages[:-1] if m.get('role') == 'system'), None)
        content = last.get('content')
        tail = {'type': 'text', 'text': content} if isinstance(content, str) else (
            content[0] if isinstance(content, list) and len(content) == 1 else {})
        if last.get('role') == 'system' and env is not None and tail.get('type') == 'text' \
                and _DATE_LINE.fullmatch(tail.get('text', '')):
            original, line = env['content'], '\n\n' + tail['text']
            target = {'type': 'text', 'text': original} if isinstance(original, str) else (original[-1] if original else {})
            if target.get('type') == 'text' and not target['text'].endswith(tail['text']):
                if isinstance(original, str):
                    env['content'] = original + line
                else:
                    target['text'] += line
                mark = tail.get('cache_control', last.get('cache_control'))
                messages.pop()
                previous = messages[-1]['content']
                if mark is not None and isinstance(previous, list) and previous:
                    previous[-1].setdefault('cache_control', mark)
                changed = True
        newest = next((m for m in reversed(messages) if m.get('role') == 'user'), {})
        blocks = newest.get('content')
        if queried and any(b.get('type') != 'tool_result' for b in queried) and isinstance(blocks, list) \
                and any(m.get('role') == 'assistant' for m in messages):
            frame = [_plain(b) for b in queried]
            hits = [i for i in range(len(blocks) - len(frame) + 1) if [_plain(b) for b in blocks[i:i + len(frame)]] == frame]
            k = hits[0] if len(hits) == 1 else None
            if k and all(b.get('type') == 'text' and set(b) <= {'type', 'text', 'cache_control'} for b in blocks[:k]):
                mark = next((b['cache_control'] for b in blocks[:k] if 'cache_control' in b), None)
                del blocks[:k]
                if mark is not None:
                    blocks[0].setdefault('cache_control', mark)
                changed = True
        return json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode('utf-8') if changed else payload
    except (ValueError, TypeError, KeyError, AttributeError, IndexError):
        return payload


def _opens_turn(message):
    """A user message carrying anything but tool results: a new turn, or a /steer row after them."""
    content = message.get('content')
    return message.get('role') == 'user' and (isinstance(content, str) or any(
        not isinstance(b, dict) or b.get('type') != 'tool_result' for b in content or []))


def _keep_turn_mark(body, messages, last, target):
    """Keep a breakpoint where the open turn's thinking starts, so the next turn still reads it (#122).

    Once a user message carries more than tool results (the next turn, or a /steer row), the
    entries written after the open turn's first thinking block stop matching; #122's captures
    read back only the newest entry written before it, by the turn's first call or by the call
    whose answer first thought. Anthropic's lookup walks back at most LOOKBACK positions from a
    breakpoint, so after a longer loop the recurring breakpoint cannot reach that entry and the
    read falls back to tools and system: the whole conversation is written again. Out of that
    reach, the block carries a mark of its own, which reads the entry an earlier request of the
    turn wrote there (as its recurring breakpoint) and keeps it alive through the loop.

    The open turn starts at the latest user message with more than tool results before the last
    assistant message; the mark goes on the last cacheable block of the user message right
    before the turn's first assistant message with thinking. Distance is counted per block,
    without the API's merging of tool_use and tool_result runs, so an error only marks early.
    The mark takes the TTL of the next mark after it, so native's order of TTLs holds. It is
    added only while the request stays within MAX_BREAKPOINTS, counting every marker in the body
    (a host marker restoration carried into tool_result content takes a slot too); at the limit
    (2.1.287+ marks two message blocks) a native mark between it and the marked recurring
    breakpoint moves there instead, and otherwise nothing changes. Returns whether a mark was placed."""
    opening = max((i for i in range(last) if _opens_turn(messages[i])), default=None)
    if opening is None:
        return False
    thought = next((i for i in range(opening + 1, last + 1) if messages[i].get('role') == 'assistant'
                    and isinstance(messages[i].get('content'), list)
                    and any(isinstance(b, dict) and b.get('type') in UNCACHEABLE for b in messages[i]['content'])), None)
    if thought is None:
        return False
    frame = next((i for i in range(thought - 1, opening, -1) if messages[i].get('role') == 'user'), opening)
    content = messages[frame].get('content')
    if not isinstance(content, list):
        return False
    j = next((k for k in reversed(range(len(content)))
              if isinstance(content[k], dict) and content[k].get('type') not in UNCACHEABLE), None)
    if j is None or 'cache_control' in content[j] or (frame, j) >= target[:2]:
        return False
    span = sum(len(m['content']) if isinstance(m.get('content'), list) else 1 for m in messages[frame:target[0]])
    if span - j + target[1] + 1 <= LOOKBACK:
        return False  # The recurring breakpoint's own lookback still reaches it.
    marks = [(i, k, b) for i, m in enumerate(messages) if isinstance(m.get('content'), list)
             for k, b in enumerate(m['content']) if isinstance(b, dict) and 'cache_control' in b]
    later = [(i, k, b) for i, k, b in marks if (frame, j) < (i, k)]
    if not later:
        return False
    if _breakpoints(body) < MAX_BREAKPOINTS:
        content[j]['cache_control'] = copy.deepcopy(later[0][2]['cache_control'])
        return True
    spare = next((b for i, k, b in later if (i, k) < target[:2]), None)
    if spare is None or 'cache_control' not in target[2]:
        return False
    content[j]['cache_control'] = spare.pop('cache_control')
    return True


def pin_message_breakpoint(payload, queried):
    """Keep the recurring message ``cache_control`` on content the next request replays unchanged.

    Native attaches per-request context (today's date, the account-email reminder, whatever a
    later CLI adds) to the turn it answers and puts the message breakpoint on or after it. The
    next request replays that turn without it, so the cached prefix never recurs and every
    tool round re-writes the whole history (issue #14, second cause).

    What does recur is known without reading native's text: everything through the last
    assistant message, plus the leading blocks of the newest turn that equal the frame Hermes
    queried. The first block native added or changed ends that span. When that block is a
    tool_result (parallel calls, native's reminder on the last result), a breakpoint on the
    unchanged results before it measured no cache hit even though they replay byte-identical
    (#33, cause unknown), so the span ends at the preceding assistant message.
    Claude Code 2.1.287+ marks two message blocks from the second request on: the last
    tool_use of the turn and its trailing per-request ``role: system`` message (#33). That
    message never recurs, so its entry is never read and each round is written twice. Marks
    after the span are therefore folded into one on its last block; a native mark inside the
    span recurs and stays. After a tool loop longer than the cache lookback, one more mark
    keeps the open turn's start readable for the next turn (``_keep_turn_mark``, #122).
    The count grows by at most that one mark and only within Anthropic's four across tools,
    system and messages; system and tools marks are never touched, and a moved or added
    marker keeps native's own order of TTLs.
    The breakpoint never moves later, content changes only where ``_frame_first`` drops native's
    context from the opening request, and any payload that does not parse forwards as is."""
    if not queried:
        return payload
    try:
        body = json.loads(payload)
        messages = body['messages']
        reordered = _frame_first(messages, queried)
        blocks = [(i, j, b) for i, m in enumerate(messages) if isinstance(m.get('content'), list)
                  for j, b in enumerate(m['content'])]
        marked = [(i, j, b) for i, j, b in blocks if isinstance(b, dict) and 'cache_control' in b]
        if not marked and not reordered:
            return payload
        last = max((i for i, m in enumerate(messages) if m.get('role') == 'assistant'), default=-1)
        stable = [(i, j, b) for i, j, b in blocks if i <= last]
        newest = messages[last + 1] if last + 1 < len(messages) else {}
        if newest.get('role') == 'user' and isinstance(newest.get('content'), list):
            prefix = []
            for j, (sent, host) in enumerate(zip(newest['content'], queried)):
                if _plain(sent) != _plain(host):
                    break
                prefix.append((last + 1, j, sent))
            rest = newest['content'][len(prefix):]
            if not (rest and isinstance(rest[0], dict) and rest[0].get('type') == 'tool_result'):
                stable += prefix
        target = next(((i, j, b) for i, j, b in reversed(stable)
                       if isinstance(b, dict) and b.get('type') not in UNCACHEABLE), None)
        after = [b for i, j, b in marked if target is not None and (target[0], target[1]) < (i, j)]
        folded = target is not None and bool(after)
        if folded:
            moved = [b.pop('cache_control') for b in after][0]
            target[2].setdefault('cache_control', moved)
        turn = target is not None and _keep_turn_mark(body, messages, last, target)
        if not (reordered or folded or turn):
            return payload
        return json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    except (ValueError, TypeError, KeyError, AttributeError, IndexError):
        return payload


def drop_message_breakpoints(payload):
    """Remove native's message ``cache_control`` marks from a tool-less one-shot.

    Hermes' auxiliary calls (compression summaries, titles) send one user turn and no tools, and
    nothing replays that turn: each summary carries a different transcript and, for LCM-style
    summarizers, a per-call nonce. Native still marks the turn for its 1-hour cache, so every such
    call paid a cache write (2x input) that no later request read. Message marks go; system and
    tools marks stay, so a stable system prompt is still cached across calls. Content never
    changes, and a payload that does not parse forwards as is."""
    try:
        body = json.loads(payload)
        dropped = False
        for message in body['messages']:
            content = message.get('content')
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.pop('cache_control', None) is not None:
                        dropped = True
        if not dropped:
            return payload
        return json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    except (ValueError, TypeError, KeyError, AttributeError):
        return payload


def stream_tool_input(payload):
    """Ask the API to stream every tool's arguments as the model writes them (``eager_input_streaming``).

    Native turns fine-grained tool streaming on only for a first-party base URL, so behind this relay its
    tools go out without the flag and the API holds a large argument (a whole file in one ``write_file``)
    until it is complete. Hermes' stall detector then sees minutes of silence and cancels a healthy call.
    A value native already set wins, and a payload with no tools or that does not parse forwards as is."""
    try:
        body = json.loads(payload)
        tools = [t for t in body.get('tools') or () if isinstance(t, dict) and 'eager_input_streaming' not in t]
        if not tools:
            return payload
        for tool in tools:
            tool['eager_input_streaming'] = True
        return json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    except (ValueError, TypeError, AttributeError):
        return payload


def restore_queried_turn(payload, queried):
    """Restore a uniquely anchored tool-result frame without dropping protected content.

    Only trailing bare text is removable. Split/joined text runs are equivalent
    for matching, but never across media, metadata or cache directives. Restore
    the host's original representation and retain unambiguously mapped markers.
    """
    if not isinstance(queried, list) or not queried:
        return payload
    strict = any(isinstance(block, dict) and block.get('type') == 'tool_result' for block in queried)
    # An ordinary user-only frame is outside restoration scope, byte for byte.
    if not strict:
        return payload

    def unmatched():
        raise QueriedTurnMismatch('Native request does not uniquely match Hermes tool results')

    def bare_text(block):
        return (isinstance(block, dict) and set(block) == {'type', 'text'}
                and block['type'] == 'text' and isinstance(block['text'], str))

    def restore_content(sent, host):
        # Scalar/list equivalence is allowed only for text with no metadata or markers.
        if isinstance(host, str) or isinstance(sent, str):
            def text(value):
                if isinstance(value, str):
                    return value
                if isinstance(value, list) and all(bare_text(b) for b in value):
                    return ''.join(b['text'] for b in value)
                return None
            left, right = text(sent), text(host)
            if left is None or right is None or not left.startswith(right):
                return unmatched()
            return copy.deepcopy(host)
        if isinstance(sent, list) and isinstance(host, list):
            return restore_sequence(sent, host)
        if sent != host:
            return unmatched()
        return copy.deepcopy(host)

    def restore_block(sent, host):
        if not isinstance(sent, dict) or not isinstance(host, dict):
            return unmatched()
        restored = copy.deepcopy(host)
        if host.get('type') == 'tool_result':
            left_error, right_error = sent.get('is_error', False), host.get('is_error', False)
            # Missing means false; numeric 0 is not a protocol boolean.
            if type(left_error) is not bool or type(right_error) is not bool or left_error != right_error:
                return unmatched()
            ignored = {'content', 'is_error', 'cache_control'}
            if {k: v for k, v in sent.items() if k not in ignored} != {k: v for k, v in host.items() if k not in ignored}:
                return unmatched()
            if ('content' in sent) != ('content' in host):
                return unmatched()
            if 'content' in host:
                restored['content'] = restore_content(sent['content'], host['content'])
        elif _plain(sent) != _plain(host):
            return unmatched()
        if 'cache_control' in sent:
            if 'cache_control' in host and sent['cache_control'] != host['cache_control']:
                return unmatched()
            restored['cache_control'] = copy.deepcopy(sent['cache_control'])
        return restored

    def restore_sequence(sent, host):
        restored = []
        i = j = 0
        while i < len(host):
            if j >= len(sent):
                return unmatched()
            if bare_text(host[i]) and bare_text(sent[j]):
                host_end, sent_end = i + 1, j + 1
                while host_end < len(host) and bare_text(host[host_end]):
                    host_end += 1
                while sent_end < len(sent) and bare_text(sent[sent_end]):
                    sent_end += 1
                original = ''.join(b['text'] for b in host[i:host_end])
                actual = ''.join(b['text'] for b in sent[j:sent_end])
                # Extensions before a protected host block are insertions, not suffixes.
                if not (actual.startswith(original) if host_end == len(host) else actual == original):
                    return unmatched()
                restored.extend(copy.deepcopy(host[i:host_end]))
                i, j = host_end, sent_end
            else:
                restored.append(restore_block(sent[j], host[i]))
                i += 1
                j += 1
        if any(not bare_text(b) for b in sent[j:]):
            return unmatched()
        return restored

    try:
        body = json.loads(payload)
        messages = body['messages']
        last_assistant = max((i for i, m in enumerate(messages) if m.get('role') == 'assistant'), default=-1)
        newest = messages[last_assistant + 1] if last_assistant + 1 < len(messages) else {}
        native = newest.get('content') if newest.get('role') == 'user' else None
        if not isinstance(native, list):
            return unmatched()

        result_ids = [block.get('tool_use_id') for block in queried
                      if isinstance(block, dict) and block.get('type') == 'tool_result']
        if any(not isinstance(result_id, str) or not result_id for result_id in result_ids) or len(set(result_ids)) != len(result_ids):
            return unmatched()

        native_ids = [b.get('tool_use_id') for b in native if isinstance(b, dict) and b.get('type') == 'tool_result']
        # Unique ordered result IDs anchor the complete frame; no substring search or
        # reminder-pattern stripping can accidentally choose another host span.
        if result_ids != native_ids:
            return unmatched()
        restored = restore_sequence(native, queried)
        if native == restored:
            return payload
        newest['content'] = restored
        return json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    except QueriedTurnMismatch:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, IndexError):
        return unmatched()


class Capture:
    def __init__(self):
        self.message = None
        self.complete = False
        self.pending = ''
        self.decoder = codecs.getincrementaldecoder('utf-8')()
        self.arguments = {}
        self.unparsed = []

    def feed(self, chunk):
        self.pending += self.decoder.decode(chunk)
        while match := re.search(r'\r?\n\r?\n', self.pending):
            frame, self.pending = self.pending[:match.start()], self.pending[match.end():]
            data = '\n'.join(line[5:].lstrip(' ') for line in frame.splitlines() if line.startswith('data:'))
            if data:
                self.event(json.loads(data))

    def event(self, event):
        handler = {
            'message_start': self._start,
            'content_block_start': self._block_start,
            'content_block_delta': self._block_delta,
            'content_block_stop': self._block_stop,
            'message_delta': self._delta,
            'message_stop': self._stop,
        }.get(event['type'])
        if handler:
            handler(event)

    def _start(self, event):
        self.message = copy.deepcopy(event['message'])

    def _block_start(self, event):
        self.message['content'].append(copy.deepcopy(event['content_block']))

    def _block_delta(self, event):
        delta = event['delta']
        block = self.message['content'][event['index']]
        field = {'text_delta':'text', 'thinking_delta':'thinking', 'signature_delta':'signature'}.get(delta['type'])
        if field:
            block[field] = block.get(field, '') + delta[field]
        elif delta['type'] == 'input_json_delta':
            index = event['index']
            self.arguments[index] = self.arguments.get(index, '') + delta['partial_json']
        elif delta['type'] == 'citations_delta':
            block.setdefault('citations', []).append(copy.deepcopy(delta['citation']))

    def _block_stop(self, event):
        index = event['index']
        if index in self.arguments:
            # A no-argument tool call streams one input_json_delta with an empty partial_json.
            raw = self.arguments.pop(index)
            try:
                self.message['content'][index]['input'] = json.loads(raw) if raw.strip() else {}
            except ValueError:
                # Streamed tool input is not validated (``eager_input_streaming``): a reply cut off at
                # max_tokens ends mid-argument. The call is dropped at message_stop, never run with guessed
                # arguments, and the stop reason (finish ``length``) tells Hermes the reply was cut short.
                self.unparsed.append(index)

    def _delta(self, event):
        self.message.update(event.get('delta', {}))
        self.message['usage'].update(event.get('usage', {}))

    def _stop(self, event):
        for index in sorted(self.unparsed, reverse=True):
            del self.message['content'][index]
        self.complete = bool(self.message and self.message.get('stop_reason') and not self.arguments)


def upstream_proxy(upstream):
    """``(host, port, tunnel_headers)`` of the HTTP CONNECT proxy for ``upstream``, or ``None``.

    The relay opens the upstream connection itself, so native's own proxy handling never runs;
    honor the same variables with the standard library's rules: ``https_proxy``/``HTTPS_PROXY``
    then ``http_proxy``/``HTTP_PROXY`` (lowercase wins, as for native), ``NO_PROXY`` exceptions.
    A proxy is only a CONNECT tunnel: TLS and certificate checks still end at the upstream host.
    """
    if upstream.scheme != 'https':
        return None
    proxies = urllib.request.getproxies_environment()
    if 'no' in proxies:
        proxies['no'] = proxies['no'].replace(' ', ',')  # native also accepts space-separated entries
    if urllib.request.proxy_bypass_environment(f'{upstream.hostname}:{upstream.port or 443}', proxies):
        return None
    raw = (
        os.environ.get('CLAUDE_SUBSCRIPTION_DIRECTSDK_PROXY')
        or proxies.get('https')
        or proxies.get('http')
    )
    if not raw:
        return None
    proxy = urlsplit(raw if '://' in raw else 'http://' + raw)
    try:
        port = proxy.port or 80
    except ValueError:
        port = None
    # Before proxy support the relay always connected directly; an unusable proxy keeps that rather
    # than breaking a setup that worked. The message never quotes the URL: it may carry credentials.
    if proxy.scheme != 'http' or not proxy.hostname or port is None:
        logging.getLogger(__name__).warning(
            'ignoring HTTPS_PROXY for the relay: only an http://host[:port] CONNECT proxy is supported '
            '(SOCKS and TLS-to-proxy are not); connecting directly')
        return None
    headers = None
    if proxy.username is not None:
        credentials = unquote(proxy.username) + ':' + unquote(proxy.password or '')
        headers = {'Proxy-Authorization': 'Basic ' + base64.b64encode(credentials.encode()).decode()}
    return proxy.hostname, port, headers


class _Server(HTTPServer):
    """HTTPServer that stays quiet about a client that hung up before reading our answer.

    Native (Bun's own HTTP client) sends a throwaway warm-up probe (observed as a GET/HEAD to
    an `/api/hello`-style path) ahead of the real `/v1/messages` POST on every turn. Handler only
    implements do_POST, so http.server's default dispatch answers the probe with a 501
    ("Unsupported method") via send_error(); native never reads that body and disconnects
    immediately, so the write raises BrokenPipeError/ConnectionResetError while send_error() is
    still writing it. The 501 itself is expected and harmless to the real request (see the
    archived hermes-plugin-claude-subscription-directsdk#17); the only bug worth fixing is
    socketserver's default handle_error() dumping a scary-looking traceback to stderr for this
    exact, harmless, every-turn race. Anything else still gets the real traceback.
    """
    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


class Admission:
    def __init__(self, upstream, timeout, queried=None):
        self.upstream = urlsplit(upstream)
        self.queried = queried
        host = self.upstream.hostname
        try:
            local = ipaddress.ip_address(host).is_loopback
        except ValueError:
            local = host == 'localhost'
        if (self.upstream.scheme != 'https' and not (self.upstream.scheme == 'http' and local)) or not host or self.upstream.username or self.upstream.password or self.upstream.query or self.upstream.fragment:
            raise ValueError('Native upstream must be HTTPS or a loopback HTTP fixture')
        self.timeout = timeout
        self.proxy = upstream_proxy(self.upstream)
        self.lock = threading.Lock()
        self.sockets = set()
        self.cancelled = False
        self.used = False
        self.denied = 0
        self.request_id = None
        self.status = None
        self.failure = None
        self.unrestored = None
        self.capture = Capture()
        self.error_body = b''
        self.prefix = '/admit/' + secrets.token_urlsafe(32)
        self.server = _Server(('127.0.0.1', 0), Handler)
        self.server.admission = self
        self.url = f'http://127.0.0.1:{self.server.server_port}' + self.prefix
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval':.05}, daemon=True)
        self.thread.start()

    def error_text(self):
        """The upstream's own message for a non-200 answer, '' when none was captured."""
        text = self.error_body.decode('utf-8', errors='replace')
        try:
            message = json.loads(text)['error']['message']
        except (ValueError, KeyError, TypeError):
            return text
        return message if isinstance(message, str) else text

    def abort(self):
        with self.lock:
            self.cancelled = True
            for sock in self.sockets:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass  # Peer may have closed between the read and cancellation.
                if _CANCEL_BY_CLOSE:
                    try:
                        # detach() marks the object closed (no later double close) and hands back the OS handle.
                        fd = sock.detach()
                        if fd is not None and fd >= 0:
                            socket.close(fd)
                    except OSError:
                        pass

    def close(self):
        self.abort()
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        gate = self.server.admission
        with gate.lock:
            gate.sockets.add(self.request)
            cancelled = gate.cancelled
        try:
            self.request.settimeout(gate.timeout)
            if cancelled:
                self.request.shutdown(socket.SHUT_RDWR)
            super().setup()
        except BaseException:
            with gate.lock:
                gate.sockets.discard(self.request)
            raise

    def finish(self):
        try:
            super().finish()
        finally:
            with self.server.admission.lock:
                self.server.admission.sockets.discard(self.request)

    def log_message(self, *args):
        pass  # Native authorization and the per-call route must never enter logs.

    def _connect_upstream(self):
        gate, target = self.server.admission, self.server.admission.upstream
        if target.scheme == 'https':
            if gate.proxy:
                proxy_host, proxy_port, tunnel_headers = gate.proxy
                conn = http.client.HTTPSConnection(proxy_host, proxy_port, timeout=gate.timeout, context=ssl.create_default_context())
                conn.set_tunnel(target.hostname, target.port or 443, headers=tunnel_headers)
            else:
                conn = http.client.HTTPSConnection(target.hostname, target.port, timeout=gate.timeout, context=ssl.create_default_context())
        else:
            conn = http.client.HTTPConnection(target.hostname, target.port, timeout=gate.timeout)
        try:
            conn.connect()
            sock = conn.sock
            keepalive(sock)
            with gate.lock:
                if gate.cancelled:
                    conn.close()
                    return None, None
                gate.sockets.add(sock)
            return conn, sock
        except BaseException:
            conn.close()
            raise

    def do_POST(self):
        global _gzip_latched
        gate = self.server.admission
        path = urlsplit(self.path)
        if path.path != gate.prefix + '/v1/messages' or self.headers.get('Origin'):
            self.send_error(404)
            return
        with gate.lock:
            if gate.cancelled or gate.used:
                gate.denied += 1
                body = b'{"type":"error","error":{"type":"invalid_request_error","message":"HERMES_MODEL_ADMISSION_CONSUMED"}}'
                self.send_response(400)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            gate.used = True
        conn = None
        upstream_socket = None
        try:
            self.connection.settimeout(gate.timeout)
            payload = self.rfile.read(int(self.headers['Content-Length']))
            if getattr(gate, 'one_shot', False):
                payload = drop_message_breakpoints(payload)
            try:
                payload = restore_queried_turn(payload, gate.queried)
            except QueriedTurnMismatch as exc:
                # Restoration is hygiene, not a gate: a frame it cannot prove lossless forwards
                # exactly as native built it (the pre-restoration behaviour), so a CLI that
                # reshapes its tool results degrades to an uncached tail, never a failed turn.
                gate.unrestored = str(exc)
            payload = stable_request_context(payload, gate.queried)
            payload = pin_message_breakpoint(payload, gate.queried)
            payload = stream_tool_input(payload)
            target = gate.upstream
            conn, upstream_socket = self._connect_upstream()
            if conn is None:
                return
            # Request identity and payload remain native; only HTTP transfer encoding changes.
            headers = {k:v for k,v in self.headers.items() if k.lower() not in ('host','connection','content-length','transfer-encoding','proxy-authorization','proxy-connection','accept-encoding')}
            headers['Accept-Encoding'] = 'identity'
            route = target.path.rstrip('/') + '/v1/messages' + ('?' + path.query if path.query else '')
            compressed = _gzip_eligible(gate, payload, self.headers)
            wire = payload
            if compressed:
                pad = b'\n' + bytes(secrets.choice(b' \t') for _ in range(secrets.randbelow(257)))
                wire = gzip.compress(payload + pad, compresslevel=3, mtime=0)
            conn.request('POST', route, wire, {**headers, 'Content-Encoding': 'gzip'} if compressed else headers)
            response = conn.getresponse()
            prefix = b''
            if compressed and response.status in (400, 403, 415):
                # Recorded before the body read, so a failed read still reports this answer as an identity send would.
                gate.request_id = response.getheader('request-id') or response.getheader('x-request-id')
                gate.status = response.status
                prefix = response.read(65536)
                reason = _rejection_kind(response, prefix)
                if reason:
                    gate.request_id = gate.status = None  # The resend reports its own answer, or none.
                    if _gzip_latched is None:
                        _gzip_latched = reason
                        logging.getLogger(__name__).warning('request gzip disabled: %s status=%s', reason, response.status)
                    with gate.lock:
                        gate.sockets.discard(upstream_socket)
                    response.close()
                    conn.close()
                    with gate.lock:
                        if gate.cancelled:
                            return
                    conn, upstream_socket = self._connect_upstream()
                    if conn is None:
                        return
                    conn.request('POST', route, payload, headers)
                    response = conn.getresponse()
                    prefix = b''
            del headers, payload, wire
            gate.request_id = response.getheader('request-id') or response.getheader('x-request-id')
            gate.status = response.status
            self.send_response(response.status)
            for key, value in response.getheaders():
                if key.lower() not in ('connection','transfer-encoding','server','date'):
                    self.send_header(key, value)
            self.send_header('Connection', 'close')
            self.end_headers()
            idle = min(gate.timeout, UPSTREAM_IDLE_SECONDS)
            if not response.isclosed():
                upstream_socket.settimeout(idle)
            while True:
                try:
                    chunk = prefix or response.read1(65536)
                    prefix = b''
                except TimeoutError as exc:  # socket.timeout is TimeoutError since 3.10.
                    raise UpstreamIdle(f'no upstream bytes for {idle:g} s') from exc
                if not chunk:
                    break
                if response.status == 200:
                    gate.capture.feed(chunk)
                elif len(gate.error_body) < 65536:
                    gate.error_body += chunk  # The rejection reason ("prompt is too long", "adaptive thinking is not supported"), bounded.
                self.wfile.write(chunk)
                self.wfile.flush()
        except (OSError, http.client.HTTPException, ValueError, KeyError, IndexError, TypeError) as exc:
            gate.failure = type(exc).__name__
        finally:
            with gate.lock:
                gate.sockets.discard(upstream_socket)
            if conn:
                conn.close()
            self.close_connection = True
