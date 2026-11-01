"""Native progress the plugin does not translate into text is still proof of life for the host.

Hermes' stale detector only sees chunks. While the model generates a tool call, native streams
``input_json_delta`` events and no text, so a plugin that yields nothing for them looks stalled
and the host kills a healthy request. Synthetic CLI only.
"""
import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

import directsdk as ds


def living(pid):
    try:
        return psutil.Process(pid).is_running() and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def is_liveness(chunk):
    """A chunk that carries no content, tool call, reasoning or finish: only 'still working'."""
    choice, delta = chunk.choices[0], chunk.choices[0].delta
    return choice.finish_reason is None and not any(
        getattr(delta, name, None) for name in ('content', 'tool_calls', 'reasoning_content', 'reasoning_details'))


@pytest.fixture
def native(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, 'apply_traffic_policy', lambda env: env)
    pids = tmp_path / 'pids.jsonl'
    command = [sys.executable, '-I', str(Path(__file__).parent / 'fixtures/progress_cli.py')]
    clients, initial = [], set(threading.enumerate())

    def make(mode, timeout=5, **knobs):
        env = {'HOME': str(tmp_path), 'PATH': '', 'ANTHROPIC_BASE_URL': 'http://127.0.0.1:9',
               'PROGRESS_PIDS': str(pids), 'PROGRESS_MODE': mode,
               **{'PROGRESS_' + key.upper(): str(value) for key, value in knobs.items()}}
        client = ds.Client(command=command, env=env, timeout=timeout)
        clients.append(client)
        return client

    def spawned():
        return [json.loads(line)['pid'] for line in pids.read_text().splitlines()] if pids.exists() else []

    yield SimpleNamespace(make=make, spawned=spawned)
    for client in clients:
        client.close()
        assert not client._requests
    leftovers = [t for t in threading.enumerate() if t not in initial
                 and t.name.startswith('claude-directsdk-') and t.is_alive()]
    for t in leftovers:
        t.join(2)
    assert not any(t.is_alive() for t in leftovers), 'transport thread leaked'


def query():
    return dict(model='sonnet', messages=[{'role': 'user', 'content': 'go'}])


def test_text_progress_streams_as_content(native):
    """Positive control: the harness can observe native progress at all."""
    chunks = list(native.make('text', deltas=6)._create(stream=True, **query()))
    assert ''.join(c.choices[0].delta.content or '' for c in chunks[:-1]) == 'x' * 6


def test_tool_argument_progress_is_visible_before_the_final_chunk(native):
    chunks = list(native.make('tool_args', deltas=40)._create(stream=True, **query()))
    *progress, final = chunks
    assert hasattr(final, '_response') and final.choices[0].finish_reason == 'tool_calls'
    heartbeats = [c for c in progress if is_liveness(c)]
    assert len(heartbeats) >= 40, 'tool-call argument generation produced no chunk the host can see'
    assert len(heartbeats) == len(progress), 'progress chunks must carry no content'


def test_pings_are_not_progress(native):
    """A ping proves the connection is open, not that the model is working.

    If pings fed the host's stale detector, a stall that still pings would never be cancelled.
    """
    *progress, final = list(native.make('pings', deltas=10)._create(stream=True, **query()))
    assert final.choices[0].finish_reason == 'stop'
    assert not [c for c in progress if is_liveness(c)], 'a ping-only stretch must stay silent to the host'


def test_progress_chunks_do_not_change_the_final_response(native):
    streamed = list(native.make('tool_args', deltas=8)._create(stream=True, **query()))[-1]._response
    direct = native.make('tool_args', deltas=8)._create(**query())
    for response in (streamed, direct):
        message = response.choices[0].message
        assert response.choices[0].finish_reason == 'tool_calls' and message.content is None
        assert [c.function.name for c in message.tool_calls] == ['lookup']
    assert direct.choices[0].message.tool_calls == streamed.choices[0].message.tool_calls


def test_internal_progress_outlives_the_idle_timeout(native):
    """Progress resets the plugin's own idle deadline; only the host needed to see it."""
    started = time.monotonic()
    response = native.make('tool_args', timeout=1.5, deltas=25, gap=.1)._create(**query())
    assert time.monotonic() - started > 1.5
    assert response.choices[0].finish_reason == 'tool_calls'


def test_true_stall_still_times_out_finitely_and_is_reaped(native):
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        native.make('silent', timeout=1.5)._create(**query())
    assert time.monotonic() - started < 10  # finite; the bound only guards a hang and must tolerate a loaded runner
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and any(living(pid) for pid in native.spawned()):
        time.sleep(.02)
    assert native.spawned() and not any(living(pid) for pid in native.spawned()), 'stalled child leaked'
