"""Bounded, explicitly owned Codex turns using existing profiles and pair receipts."""

import hashlib
import json
import math
import os
import queue
import select
import stat
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

import agent_sessions as storage
import session_profiles as profiles

TEXT_LIMIT = 65536
FRAME_LIMIT = 2 * 1024 * 1024
FIELDS = ('pair_id', 'session_id', 'profile_id', 'turn_id', 'status', 'output_text',
          'prompt_sha256', 'capabilities')


class Attention(ValueError):
    """A capability or ownership check prevents assigning work."""


class Rpc:
    def __init__(self, profile, overrides):
        env = dict(os.environ)
        for name in ('CODEX_THREAD_ID', 'CLAUDE_CODE_SESSION_ID', 'AGENT_SESSIONS_PROFILE_ID'):
            env.pop(name, None)
        self.process = subprocess.Popen(['codex', *overrides, 'app-server', '--stdio'],
            cwd=profile['cwd'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=env)
        self.incoming = queue.Queue(maxsize=16)
        self.events = deque()
        self.counter = 0
        os.set_blocking(self.process.stdin.fileno(), False)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            while True:
                line = self.process.stdout.readline(FRAME_LIMIT + 1)
                if not line:
                    raise ValueError('owned App Server closed its transport')
                if len(line) > FRAME_LIMIT:
                    raise ValueError('native response exceeds 2 MiB')
                self.incoming.put(json.loads(line))
        except (OSError, ValueError) as exc:
            self.incoming.put(exc)

    def send(self, message, deadline):
        data = memoryview((json.dumps(message)+'\n').encode())
        descriptor = self.process.stdin.fileno()
        while data:
            remaining = deadline-time.monotonic()
            if remaining <= 0 or not select.select([], [descriptor], [], remaining)[1]:
                raise TimeoutError('managed transport write deadline reached')
            try:
                data = data[os.write(descriptor, data):]
            except BlockingIOError:
                continue

    def next(self, deadline):
        try:
            message = self.incoming.get(timeout=max(0, deadline-time.monotonic()))
        except queue.Empty as exc:
            raise TimeoutError('managed run deadline reached') from exc
        if isinstance(message, Exception):
            raise message
        if not isinstance(message, dict):
            raise TypeError('invalid native protocol message')
        if 'method' in message and 'id' in message:
            raise Attention('native request requires attention: '+str(message['method'])[:160])
        return message

    def request(self, method, params, deadline):
        self.counter += 1
        request_id = self.counter
        self.send({'id': request_id, 'method': method, 'params': params}, deadline)
        while True:
            message = self.next(deadline)
            if message.get('id') == request_id:
                if 'error' in message:
                    raise Attention('native RPC rejected: '+method)
                return message['result']
            if message.get('method') in ('turn/completed', 'item/completed'):
                if len(self.events) >= 32:
                    raise ValueError('too many pending native completion events')
                self.events.append(message)

    def close(self):
        try:
            self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=1)
        self.process.stdout.close()


def prompt_text(pair, filename):
    if filename is None:
        raw = pair['brief'].encode('utf-8')
    else:
        path = Path(filename).expanduser()
        if not path.is_absolute() or path.resolve(strict=True) != path:
            raise Attention('prompt file must have a canonical absolute path')
        if not stat.S_ISREG(path.lstat().st_mode):
            raise Attention('prompt file must be regular')
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(descriptor, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise Attention('prompt file must be regular')
            raw = stream.read(TEXT_LIMIT + 1)
            after = os.fstat(stream.fileno())
        signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)
        if (signature(before) != signature(after) or signature(after) != signature(path.stat())
                or path.resolve(strict=True) != path):
            raise Attention('prompt file changed while reading')
    if len(raw) > TEXT_LIMIT:
        raise Attention('prompt exceeds 64 KiB')
    text = raw.decode('utf-8')
    if not text.strip():
        raise Attention('prompt must not be empty')
    return text, hashlib.sha256(raw).hexdigest()


def capabilities(config):
    features, skills = config.get('features', {}), config.get('skills', {})
    if (any(features.get(name) is not False for name in ('hooks', 'apps', 'plugins', 'skill_search'))
            or skills.get('include_instructions') is not False):
        raise Attention('managed runs require hooks, apps, plugins, skill_search and '
                        'automatic skill instructions disabled')
    servers = config.get('mcp_servers', {})
    expected = {}
    for name, server in servers.items():
        if server.get('enabled', True) is False:
            continue
        tools = server.get('enabled_tools')
        if (not isinstance(tools, list) or any(not isinstance(t, str) or not t for t in tools)
                or len(set(tools)) != len(tools)):
            raise Attention('each enabled MCP server needs an explicit enabled_tools allowlist')
        expected[name] = sorted(tools)
    return expected


def latest_turn(client, session_id, deadline):
    result = client.request('thread/turns/list', {'threadId': session_id, 'limit': 1,
                            'sortDirection': 'desc'}, deadline)
    turns = result['data']
    if len(turns) > 1:
        raise Attention('native latest-turn response was not bounded')
    return turns[0] if turns else None


def discovered_tools(client, session_id, expected, deadline):
    actual, seen, cursor = {}, set(), None
    for _ in range(100):
        result = client.request('mcpServerStatus/list', {'threadId': session_id,
            'detail': 'toolsAndAuthOnly', 'limit': 100, 'cursor': cursor}, deadline)
        for server in result['data']:
            name, tools = server['name'], server.get('tools', {})
            if name in seen:
                raise Attention('duplicate native server inventory')
            seen.add(name)
            if tools or name in expected:
                if server.get('toolsError') or server.get('runtimeStatus') != 'connected':
                    raise Attention('MCP server is unavailable: '+name)
                actual[name] = sorted(tools)
        cursor = result.get('nextCursor')
        if cursor is None:
            break
    else:
        raise Attention('MCP inventory pagination exceeds limit')
    if actual != expected:
        raise Attention('native MCP tools differ from pinned allowlists')
    return actual


def execute(pair, receipt, persist, timeout, filename):
    profile = profiles.load(storage.data_dir(), pair['profile'])
    if profile['host'] != 'codex' or pair['partner']['host'] != 'codex':
        raise Attention('managed runs require a Codex profile and partner')
    if profile['cwd'] != pair['partner']['cwd']:
        raise Attention('partner directory differs from profile')
    prompt, receipt['prompt_sha256'] = prompt_text(pair, filename)
    overrides = profiles.codex_overrides(profile)
    config = profiles.tomllib.loads('\n'.join(overrides[1::2]))
    expected = capabilities(config)
    deadline = time.monotonic() + timeout
    client, owned_turn = None, None

    def guard():
        profiles.guard(storage.data_dir(), receipt['session_id'], profile['cwd'], 'codex',
                       profile['id'], storage.write_json, expected=profile['id'])

    try:
        client = Rpc(profile, overrides)
        client.request('initialize', {'clientInfo': {'name': 'agent-sessions', 'version': '0.1.0'},
                                      'capabilities': {'experimentalApi': True}}, deadline)
        client.send({'method': 'initialized', 'params': {}}, deadline)
        effective = client.request('config/read', {'cwd': profile['cwd'],
                                  'includeLayers': False}, deadline)['config']
        if capabilities(effective) != expected:
            raise Attention('effective enabled raw MCP servers differ from profile')
        session_id = receipt['session_id']
        if session_id:
            guard()
            latest = latest_turn(client, session_id, deadline)
            if ((latest or {}).get('id') != receipt['turn_id'] or
                    latest and latest.get('status') not in ('completed', 'failed', 'interrupted')):
                raise Attention('latest native turn changed or is active')
            method, params = 'thread/resume', {'threadId': session_id, 'excludeTurns': True}
        else:
            if pair['status'] != 'needs_profile_launch':
                raise Attention('launch was already attempted; reconcile the saved pair')
            method, params = 'thread/start', {'ephemeral': False}
            pair['status'] = 'profile_launch_pending'
        profiles.validate(profile)
        persist('loading', 'unknown')
        thread = client.request(method, {**params, 'cwd': profile['cwd'], 'config': config},
                                deadline)['thread']
        native_id = storage.validate_session_id(thread['id'])
        if session_id and native_id != session_id:
            receipt['observed_session_id'] = native_id
            raise Attention('native resume returned a different identity')
        receipt['session_id'] = native_id
        persist('loading', 'unknown')
        if str(Path(thread['cwd']).resolve()) != profile['cwd']:
            raise Attention('native thread directory differs from profile')
        if thread.get('sessionId', native_id) != native_id:
            raise Attention('managed worker must be a persistent root session')
        pair['partner'].update(id=native_id, host_id='local')
        pair['status'] = 'connected'
        persist('bound', 'attention')
        guard()
        if thread.get('status', {}).get('type') != 'idle':
            raise Attention('native thread has concurrent active work')
        receipt['capabilities']['mcp_tools'] = discovered_tools(client, native_id, expected, deadline)
        guard()
        latest = latest_turn(client, native_id, deadline)
        if ((latest or {}).get('id') != receipt['turn_id'] or
                latest and latest.get('status') not in ('completed', 'failed', 'interrupted')):
            raise Attention('native turn changed or became active before assignment')
        persist('submitting', 'unknown')
        turn = client.request('turn/start', {'threadId': native_id,
                              'input': [{'type': 'text', 'text': prompt}]}, deadline)['turn']
        owned_turn = storage.validate_session_id(turn['id'])
        receipt['turn_id'] = owned_turn
        persist('running', 'unknown')
        output = []
        while True:
            event = client.events.popleft() if client.events else client.next(deadline)
            params = event.get('params', {})
            if params.get('threadId') != native_id:
                continue
            if event.get('method') == 'item/completed' and params.get('turnId') == owned_turn:
                item = params['item']
                if item.get('type') == 'agentMessage':
                    output.append(item.get('text', ''))
            if event.get('method') == 'turn/completed' and params['turn']['id'] == owned_turn:
                ended = params['turn']
                if not output:
                    output = [i.get('text', '') for i in ended.get('items', [])
                              if i.get('type') == 'agentMessage']
                receipt['output_text'] = '\n'.join(output).encode()[:TEXT_LIMIT].decode('utf-8', 'ignore')
                guard()
                receipt['status'] = 'completed' if ended['status'] == 'completed' else 'failed'
                owned_turn = None
                persist('finished', receipt['status'])
                return
            if sum(len(t.encode()) for t in output) > TEXT_LIMIT:
                raise Attention('worker output exceeds 64 KiB')
    except (OSError, ValueError, KeyError, TypeError) as exc:
        uncertain = receipt.get('phase') in ('loading', 'submitting', 'running')
        receipt['error'] = str(exc)[:1024]
        persist(receipt.get('phase', 'preflight'), 'unknown' if uncertain else 'attention')
        if owned_turn and client:
            try:
                client.request('turn/interrupt', {'threadId': receipt['session_id'],
                               'turnId': owned_turn}, time.monotonic()+1)
            except (OSError, ValueError, KeyError, TypeError):
                pass
        print('agent-sessions: '+receipt['error'], file=sys.stderr)
    finally:
        if client:
            client.close()


def run(pair_id, timeout=60, prompt_file=None):
    pair = storage.read_pair(pair_id)
    previous = pair.get('last_run', {})
    receipt = {'pair_id': pair_id, 'session_id': pair['partner']['id'],
        'profile_id': pair.get('profile'), 'turn_id': previous.get('turn_id'),
        'status': 'attention', 'output_text': '', 'prompt_sha256': None,
        'capabilities': {'mcp_tools': {}, 'automatic_skill_context': False}}
    if previous.get('status') in ('unknown', 'failed'):
        return {key: previous[key] for key in FIELDS}
    try:
        with storage.locked(storage.pair_path(pair_id), blocking=False):
            if storage.read_pair(pair_id) != pair:
                raise Attention('pair changed before ownership was acquired')
            if not math.isfinite(timeout) or not 0 < timeout <= 3600:
                raise Attention('timeout must be positive and at most 3600 seconds')
            if not pair.get('profile') or pair['partner']['id'] and not previous:
                raise Attention('managed run requires a profiled pair with managed ownership')

            def persist(phase, status):
                receipt.update(phase=phase, status=status)
                pair['last_run'] = dict(receipt)
                storage.write_json(storage.pair_path(pair_id), pair)

            try:
                execute(pair, receipt, persist, timeout, prompt_file)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                receipt['error'] = str(exc)[:1024]
                persist('preflight', 'attention')
                print('agent-sessions: '+receipt['error'], file=sys.stderr)
    except (OSError, ValueError) as exc:
        print('agent-sessions: '+str(exc)[:1024], file=sys.stderr)
    return {key: receipt[key] for key in FIELDS}
