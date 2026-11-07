"""Deterministic stdio peer for managed-run protocol tests; never calls a model."""

import json
import os
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import session_profiles

path = Path(os.environ['TEST_RPC_STATE'])
state = json.loads(path.read_text())
config = session_profiles.tomllib.loads('\n'.join(
    sys.argv[i+1] for i, value in enumerate(sys.argv) if value == '-c'))
thread_id = state.get('native_identity', '11111111-1111-4111-8111-111111111111')


def save():
    path.write_text(json.dumps(state))


def send(value):
    print(json.dumps(value), flush=True)


def reject(request, code, message):
    send({'id': request['id'], 'error': {'code': code, 'message': message}})


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
        if state.get('history_error'):
            reject(request, -32603, 'history backend unavailable')
            continue
        if state.get('native_absent'):
            reject(request, -32600, 'thread not loaded: '+params['threadId'])
            continue
        if state.get('reject_empty_history') and not state.get('turns'):
            reject(request, -32600, 'thread history has not been materialized')
            continue
        result = {'data': state.get('turns', []), 'nextCursor': None}
    elif method == 'thread/read':
        if state.get('native_absent'):
            reject(request, -32600, 'thread not loaded: '+params['threadId'])
            continue
        if params.get('includeTurns') and state.get('read_history_error'):
            reject(request, -32603, 'history backend unavailable')
            continue
        if (params.get('includeTurns') and state.get('unmaterialized_read')
                and not state.get('turns')):
            reject(request, -32600, 'thread '+params['threadId']+
                   ' is not materialized yet; includeTurns is unavailable before first user message')
            continue
        result = {'thread': {'id': thread_id, 'cwd': state['cwd'],
                            'turns': state.get('turns', []) if params.get('includeTurns') else [],
                            'status': {'type': 'active' if state.get('active') else 'idle'}}}
        if not params.get('includeTurns') and state.get('steer_after_metadata'):
            state['turns'] = [{'id': 'external-turn', 'status': 'completed'}]
            save()
    elif method in ('thread/start', 'thread/resume'):
        if method == 'thread/start':
            generation = state.get('generation', 0)
            thread_id = str(uuid.UUID(int=uuid.UUID('11111111-1111-4111-8111-111111111111').int
                                     + generation))
            state.update(generation=generation+1, native_absent=False)
        state['native_identity'] = thread_id
        save()
        if state.get('lose_launch'):
            sys.exit(0)
        result = {'thread': {'id': thread_id, 'sessionId': thread_id,
                            'cwd': params.get('cwd', state['cwd']),
                            'turns': state.get('turns', []),
                            'status': {'type': 'active' if state.get('active') else 'idle'}}}
    elif method == 'mcpServerStatus/list':
        servers = []
        states = state.get('mcp_states', ['connected'])
        connection = states[0]
        if len(states) > 1:
            state['mcp_states'] = states[1:]
        errors = state.get('mcp_tools_errors', [state.get('mcp_tools_error')])
        tools_error = errors[0]
        if len(errors) > 1:
            state['mcp_tools_errors'] = errors[1:]
        if connection != 'connected' and state.get('steer_during_startup'):
            state['turns'] = [{'id': 'external-turn', 'status': 'completed'}]
        save()
        for name, server in config.get('mcp_servers', {}).items():
            if server.get('enabled', True) is False:
                continue
            tools = server['enabled_tools'] + (['excluded'] if state.get('unexpected_tool') else [])
            servers.append({'name': name, 'tools': {tool: {} for tool in tools},
                            'runtimeStatus': connection, 'toolsError': tools_error})
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
        for item in state.get('mcp_items',[]):
            send({'method':'item/completed','params':{'threadId':thread_id,
                'turnId':state.get('mcp_turn',turn['id']),'item':item}})
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
    if method == 'thread/read' and params.get('includeTurns') and state.get('stall_before_turn'):
        time.sleep(5)

state['transport_closed'] = True
save()
