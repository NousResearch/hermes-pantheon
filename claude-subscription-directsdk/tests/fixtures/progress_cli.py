"""Bounded synthetic native CLI for progress/liveness contracts. No Claude import, credentials, or networking."""
import json
import os
import sys
import time

with open(os.environ['PROGRESS_PIDS'], 'a') as f:
    f.write(json.dumps({'pid': os.getpid()}) + '\n')
if '--version' in sys.argv:
    print('2.1.263 (synthetic offline CLI)')
    sys.exit(0)

mode = os.environ['PROGRESS_MODE']
deltas = int(os.environ.get('PROGRESS_DELTAS', '5'))
gap = float(os.environ.get('PROGRESS_GAP', '0.01'))


def emit(obj):
    print(json.dumps(obj), flush=True)


def delta(body):
    emit({'type': 'stream_event', 'event': {'type': 'content_block_delta', 'delta': body}})


for line in sys.stdin:
    if json.loads(line).get('shouldQuery') is False:
        emit({'type': 'result', 'num_turns': 0, 'is_error': False})

if mode == 'silent':  # accepted the request, then never produces anything
    time.sleep(30)
    sys.exit(0)

usage = {'input_tokens': 1, 'output_tokens': 1}
if mode == 'tool_args':  # the model is generating a tool call: native streams only argument JSON
    for _ in range(deltas):
        delta({'type': 'input_json_delta', 'partial_json': '{'})
        time.sleep(gap)
    content, stop = [{'type': 'tool_use', 'id': 'toolu_1', 'name': 'lookup', 'input': {'q': 'x'}}], 'tool_use'
elif mode == 'pings':  # connection open, model silent: a ping proves the wire, not the work
    for _ in range(deltas):
        emit({'type': 'stream_event', 'event': {'type': 'ping'}})
        time.sleep(gap)
    content, stop = [{'type': 'text', 'text': 'x'}], 'end_turn'
else:  # text
    for _ in range(deltas):
        delta({'type': 'text_delta', 'text': 'x'})
        time.sleep(gap)
    content, stop = [{'type': 'text', 'text': 'x' * deltas}], 'end_turn'
emit({'type': 'assistant', 'message': {'role': 'assistant', 'content': content, 'stop_reason': stop, 'usage': usage}})
emit({'type': 'stream_event', 'event': {'type': 'message_stop'}})
emit({'type': 'result', 'subtype': 'success', 'is_error': False, 'num_turns': 1, 'usage': usage})
