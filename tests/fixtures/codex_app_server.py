"""Deterministic stdio peer for managed-run protocol tests; never calls a model."""

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import session_profiles

path = Path(os.environ['TEST_RPC_STATE'])
state = json.loads(path.read_text())
config = session_profiles.tomllib.loads('\n'.join(
    sys.argv[i+1] for i, value in enumerate(sys.argv) if value == '-c'))
thread_id = '11111111-1111-4111-8111-111111111111'


def save():
    path.write_text(json.dumps(state))


def send(value):
    print(json.dumps(value), flush=True)


for line in sys.stdin:
    request = json.loads(line)
    method, params = request['method'], request.get('params', {})
    state.setdefault('requests', []).append(request)
    save()
    if 'id' not in request:
        continue
    result = {}
    if method == 'config/read':
        effective = json.loads(json.dumps(config))
        if state.get('unexpected_raw'):
            effective.setdefault('mcp_servers', {})['unrelated'] = {'command': 'unrelated'}
        if state.get('effective_hooks'):
            effective['features']['hooks'] = True
        result = {'config': effective}
    elif method == 'thread/turns/list':
        result = {'data': state.get('turns', []), 'nextCursor': None}
    elif method in ('thread/start', 'thread/resume'):
        state['native_identity'] = thread_id
        save()
        if state.get('lose_launch'):
            sys.exit(0)
        result = {'thread': {'id': thread_id, 'sessionId': thread_id,
                            'cwd': params.get('cwd', state['cwd']),
                            'status': {'type': 'active' if state.get('active') else 'idle'}}}
    elif method == 'mcpServerStatus/list':
        servers = []
        for name, server in config.get('mcp_servers', {}).items():
            if server.get('enabled', True) is False:
                continue
            tools = server['enabled_tools'] + (['excluded'] if state.get('unexpected_tool') else [])
            servers.append({'name': name, 'tools': {tool: {} for tool in tools},
                            'runtimeStatus': 'connected', 'toolsError': None})
        result = {'data': servers, 'nextCursor': None}
    elif method == 'turn/start':
        number = state.get('turn_count', 0) + 1
        state['turn_count'] = number
        turn = {'id': 'turn-'+str(number), 'status': 'inProgress', 'items': []}
        state['turns'] = [turn]
        save()
        if state.get('lose_turn_response'):
            sys.exit(0)
        send({'id': request['id'], 'result': {'turn': turn}})
        if state.get('approval'):
            send({'id': 'approval-1', 'method': 'item/commandExecution/requestApproval',
                  'params': {'threadId': thread_id, 'turnId': turn['id']}})
        elif not state.get('timeout'):
            turn.update(status='failed' if state.get('failed_turn') else 'completed',
                        items=[{'type': 'agentMessage', 'text': 'result'}])
            state['turns'] = [turn]
            save()
            send({'method': 'turn/completed', 'params': {'threadId': thread_id, 'turn': turn}})
        continue
    elif method == 'turn/interrupt':
        state['interrupted'] = params
        save()
    send({'id': request['id'], 'result': result})
    if method == 'thread/turns/list' and state.get('stall_before_turn'):
        time.sleep(5)
