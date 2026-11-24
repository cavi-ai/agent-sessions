"""Bounded, explicitly owned Codex turns using existing profiles and pair receipts."""

import hashlib
import json
import math
import os
import queue
import re
import select
import signal
import stat
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from urllib.parse import urlsplit

from . import partners, paths, profiles

TEXT_LIMIT = 65536
FRAME_LIMIT = 2 * 1024 * 1024
FIELDS = ('pair_id', 'session_id', 'profile_id', 'turn_id', 'status', 'output_text',
          'prompt_sha256', 'capabilities')


class Attention(ValueError):
    """A capability or ownership check prevents assigning work."""


class NativeError(Attention):
    """Bounded native error details, without unrelated server data."""

    def __init__(self, method, error):
        self.method = method
        details = error if isinstance(error, dict) else {'message': error}
        self.code = details.get('code')
        self.message = str(details.get('message', ''))[:800]
        super().__init__(f'native RPC rejected: {method} [{self.code}]: {self.message}')


def local_child_env(local, config):
    """Forward only explicitly scoped local provider and enabled MCP environment names."""
    names = {'PATH', 'HOME', 'CODEX_HOME', 'TMPDIR', 'TMP', 'TEMP', 'LANG', 'LC_ALL',
             'LC_CTYPE', 'USER', 'LOGNAME', 'SHELL'}
    names.add(local['provider']['env_key'])
    for server in config.get('mcp_servers', {}).values():
        if server.get('enabled', True) is False:
            continue
        variables = server.get('env_vars', []) or []
        headers = server.get('env_http_headers', {}) or {}
        bearer = server.get('bearer_token_env_var')
        if (not isinstance(variables, list) or not isinstance(headers, dict)
                or any(not isinstance(name, str) for name in variables)
                or any(not isinstance(name, str) for name in headers.values())
                or bearer is not None and not isinstance(bearer, str)):
            raise Attention('invalid MCP environment references')
        names.update(variables)
        names.update(headers.values())
        if bearer:
            names.add(bearer)
    names.difference_update((*paths.FOREIGN_SESSION_ENVS, 'AGENT_SESSIONS_PROFILE_ID'))
    return {name: os.environ[name] for name in names if name in os.environ}


class TurnMetrics:
    """Retain bounded measurements for candidate turns, never native text."""

    def __init__(self, thread_id, started):
        self.thread_id, self.started = thread_id, started
        self.turns = {}

    def observe(self, message, observed):
        method = message.get('method')
        if method not in ('item/agentMessage/delta', 'thread/tokenUsage/updated'):
            return
        params = message.get('params')
        if not isinstance(params, dict) or params.get('threadId') != self.thread_id:
            return
        turn_id = params.get('turnId')
        if not isinstance(turn_id, str) or not 1 <= len(turn_id) <= 200:
            return
        if turn_id not in self.turns:
            if len(self.turns) >= 16:
                raise Attention('too many native turn metric candidates')
            self.turns[turn_id] = {}
        value = self.turns[turn_id]
        if method == 'item/agentMessage/delta':
            delta = params.get('delta')
            if isinstance(delta, str) and delta and 'time_to_first_text_ms' not in value:
                value['time_to_first_text_ms'] = max(0, round((observed-self.started)*1000))
        else:
            usage = params.get('tokenUsage')
            last = usage.get('last') if isinstance(usage, dict) else None
            if isinstance(last, dict):
                fields = {'inputTokens': 'input_tokens', 'cachedInputTokens': 'cached_input_tokens',
                          'outputTokens': 'output_tokens'}
                result = {target: last[source] for source, target in fields.items()
                          if type(last.get(source)) is int and 0 <= last[source] <= 2**53-1}
                if result:
                    value['latest_request_usage'] = result

    def result(self, turn_id):
        return dict(self.turns.get(turn_id, {}))


class Rpc:
    def __init__(self, profile, overrides, child_env=None):
        env = dict(os.environ) if child_env is None else child_env
        for name in (*paths.FOREIGN_SESSION_ENVS, 'AGENT_SESSIONS_PROFILE_ID'):
            env.pop(name, None)
        self.process = subprocess.Popen(['codex', *overrides, 'app-server', '--stdio'],
            cwd=profile['cwd'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=env)
        self.incoming = queue.Queue(maxsize=16)
        self.events = deque()
        self.counter = 0
        self.metrics = None
        self._closed = threading.Event()
        os.set_blocking(self.process.stdin.fileno(), False)
        threading.Thread(target=self._read, daemon=True).start()

    def _enqueue(self, item):
        while not self._closed.is_set():
            try:
                self.incoming.put(item, timeout=0.1)
                return
            except queue.Full:
                continue

    def _read(self):
        try:
            while not self._closed.is_set():
                line = self.process.stdout.readline(FRAME_LIMIT + 1)
                if not line:
                    raise ValueError('owned App Server closed its transport')
                if len(line) > FRAME_LIMIT:
                    raise ValueError('native response exceeds 2 MiB')
                self._enqueue(json.loads(line))
        except (OSError, ValueError) as exc:
            self._enqueue(exc)

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
        if self.metrics is not None:
            self.metrics.observe(message, time.monotonic())
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
                    raise NativeError(method, message['error'])
                return message['result']
            if message.get('method') in ('turn/completed', 'item/completed'):
                if len(self.events) >= 32:
                    raise ValueError('too many pending native completion events')
                self.events.append(message)

    def close(self):
        self._closed.set()
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
        try:
            self.process.stdout.close()
        except (OSError, ValueError):
            pass


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
    if not isinstance(config, dict):
        raise Attention('native configuration must be a mapping')
    features, skills = config.get('features', {}), config.get('skills', {})
    if not isinstance(features, dict) or not isinstance(skills, dict):
        raise Attention('managed runs require hooks, apps, plugins, skill_search and '
                        'automatic skill instructions and memories disabled')
    if (any(features.get(name) is not False for name in
            ('hooks', 'apps', 'plugins', 'skill_search', 'memories'))
            or skills.get('include_instructions') is not False):
        raise Attention('managed runs require hooks, apps, plugins, skill_search and '
                        'automatic skill instructions and memories disabled')
    servers = config.get('mcp_servers', {})
    if not isinstance(servers, dict):
        raise Attention('MCP server configuration must be a mapping')
    expected = {}
    for name, server in servers.items():
        if not isinstance(server, dict):
            raise Attention('each MCP server entry must be a mapping')
        if server.get('enabled', True) is False:
            continue
        tools = server.get('enabled_tools')
        if (not isinstance(tools, list) or any(not isinstance(t, str) or not t for t in tools)
                or len(set(tools)) != len(tools)):
            raise Attention('each enabled MCP server needs an explicit enabled_tools allowlist')
        expected[name] = sorted(tools)
    return expected


def local_provider(profile):
    """Validate the selected pinned provider without resolving or reading its key."""
    config = profiles.pinned_codex_config(profile)
    model, name, providers = config.get('model'), config.get('model_provider'), config.get('model_providers')
    if (not isinstance(model, str) or not model.strip() or len(model) > 512
            or any(ord(char) < 32 or ord(char) == 127 for char in model)):
        raise Attention('local provider requires an explicit model')
    if (not isinstance(name, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', name)
            or name in ('openai', 'ollama', 'lmstudio')):
        raise Attention('local provider requires a simple custom provider name')
    if not isinstance(providers, dict) or set(providers) != {name} or not isinstance(providers[name], dict):
        raise Attention('local provider requires exactly one selected custom provider')
    value = providers[name]
    allowed = {'name', 'base_url', 'env_key', 'wire_api', 'requires_openai_auth', 'supports_websockets',
               'stream_max_retries'}
    if set(value) - allowed:
        raise Attention('local provider contains unsupported settings')
    url = value.get('base_url')
    if not isinstance(url, str) or not re.fullmatch(r'http://(?:127\.0\.0\.1|\[::1\]):[0-9]{1,5}(?:/[A-Za-z0-9._~-]+)*', url):
        raise Attention('local provider requires a literal loopback HTTP URL with port')
    parts = urlsplit(url)
    if (not parts.port or parts.port > 65535 or parts.username or parts.password or parts.query or parts.fragment
            or any(part in ('.', '..') for part in parts.path.split('/'))):
        raise Attention('local provider URL is invalid')
    key = value.get('env_key')
    if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,127}', key):
        raise Attention('local provider requires an environment key reference')
    if not os.environ.get(key):
        raise Attention('local provider environment key is unavailable')
    if value.get('wire_api') != 'responses' or value.get('requires_openai_auth', False) is not False or value.get('supports_websockets', False) is not False:
        raise Attention('local provider requires responses without account auth or websockets')
    if ('stream_max_retries' in value and
            (type(value['stream_max_retries']) is not int or not 0 <= value['stream_max_retries'] <= 5)):
        raise Attention('local provider stream retries must be an integer from 0 to 5')
    if 'name' in value and (not isinstance(value['name'], str) or not value['name'].strip() or len(value['name']) > 128):
        raise Attention('local provider display name is invalid')
    normalized = dict(value, requires_openai_auth=False, supports_websockets=False)
    return {'model': model, 'model_provider': name, 'provider': normalized}


def check_local_effective(config, expected):
    providers = config.get('model_providers')
    actual = providers.get(expected['model_provider']) if isinstance(providers, dict) else None
    inert = {'model_catalog_url', 'env_key_instructions', 'experimental_bearer_token',
             'auth', 'gateway_oauth', 'aws', 'project_id', 'location', 'credential_file',
             'capabilities', 'query_params', 'http_headers', 'env_http_headers',
             'request_max_retries', 'stream_max_retries', 'stream_idle_timeout_ms',
             'websocket_connect_timeout_ms'}
    if (config.get('cli_auth_credentials_store') != 'ephemeral'
            or config.get('model_reasoning_summary') != 'none'
            or config.get('web_search') != 'disabled'
            or config.get('model') != expected['model'] or config.get('model_provider') != expected['model_provider']
            or not isinstance(actual, dict)
            or any(actual.get(k) != v for k, v in expected['provider'].items())
            or any(v is not None for k, v in actual.items() if k in inert and k not in expected['provider'])
            or actual.get('supports_standalone_web_search', False) is not False
            or set(actual) - set(expected['provider']) - inert - {'supports_standalone_web_search'}):
        raise Attention('effective local model or provider differs from pinned configuration')


def check_thread_provider(thread, expected):
    if thread.get('modelProvider') != expected['model_provider']:
        raise Attention('native thread provider differs from pinned configuration')


def latest_turn(client, session_id, deadline):
    result = client.request('thread/turns/list', {'threadId': session_id, 'limit': 1,
                            'sortDirection': 'desc', 'itemsView': 'notLoaded'}, deadline)
    turns = result['data']
    if (not isinstance(turns, list) or len(turns) > 1
            or not turns and result.get('nextCursor') is not None
            or turns and (not isinstance(turns[0], dict)
                          or not isinstance(turns[0].get('id'), str) or not turns[0]['id'])):
        raise Attention('native latest-turn response was malformed or not bounded')
    return turns[0] if turns else None


def configured_client(profile, overrides, expected, deadline, local=None, config=None):
    client = Rpc(profile, overrides, local_child_env(local, config) if local is not None else None)
    try:
        client.request('initialize', {'clientInfo': {'name': 'agent-sessions', 'version': paths.VERSION},
                                      'capabilities': {'experimentalApi': True}}, deadline)
        client.send({'method': 'initialized', 'params': {}}, deadline)
        effective = client.request('config/read', {'cwd': profile['cwd'],
                                  'includeLayers': False}, deadline)['config']
        if local is not None:
            check_local_effective(effective, local)
        if capabilities(effective) != expected:
            raise Attention('effective enabled raw MCP servers differ from profile')
        return client
    except BaseException:
        client.close()
        raise


def preassignment_absence(client, session_id, deadline):
    """Both native reads must report the exact missing persisted/live identity."""
    proof = []
    for method, params in (
            ('thread/read', {'threadId': session_id, 'includeTurns': False}),
            ('thread/turns/list', {'threadId': session_id, 'limit': 1,
                                 'sortDirection': 'desc', 'itemsView': 'notLoaded'})):
        try:
            client.request(method, params, deadline)
        except NativeError as exc:
            if exc.code != -32600 or exc.message != 'thread not loaded: '+session_id:
                raise
            proof.append({'method': method, 'params': params,
                          'error': {'code': exc.code, 'message': exc.message}})
        else:
            return None
    return proof


def discovered_tools(client, session_id, expected, deadline):
    while time.monotonic() < deadline:
        actual, seen, pending, cursor = {}, set(), set(), None
        for _ in range(100):
            try:
                result = client.request('mcpServerStatus/list', {'threadId': session_id,
                    'detail': 'toolsAndAuthOnly', 'limit': 100, 'cursor': cursor}, deadline)
            except TimeoutError:
                raise Attention('MCP startup did not finish before the managed deadline') from None
            for server in result['data']:
                name, tools = server['name'], server.get('tools', {})
                if name in seen:
                    raise Attention('duplicate native server inventory')
                seen.add(name)
                if tools or name in expected:
                    status = server.get('runtimeStatus')
                    if name in expected and status in ('notStarted', 'starting'):
                        pending.add(name)
                    elif server.get('toolsError'):
                        raise Attention('MCP tool discovery failed: '+name)
                    elif status != 'connected':
                        raise Attention('MCP server is unavailable: '+name)
                    else:
                        actual[name] = sorted(tools)
            cursor = result.get('nextCursor')
            if cursor is None:
                break
        else:
            raise Attention('MCP inventory pagination exceeds limit')
        if actual != {name: tools for name, tools in expected.items() if name not in pending}:
            raise Attention('native MCP tools differ from pinned allowlists')
        if not pending:
            return actual
        time.sleep(min(.05, max(0, deadline-time.monotonic())))
    raise Attention('MCP startup did not finish before the managed deadline')


def public_receipt(receipt):
    result={key:receipt[key] for key in FIELDS}
    if 'metrics' in receipt:
        result['metrics'] = receipt['metrics']
    if 'completion' in receipt:
        result['completion'] = receipt['completion']
    if receipt.get('tool_calls'):
        result['tool_calls']=receipt['tool_calls']
    return result


def record_mcp_call(item, receipt, expected):
    if item.get('type') != 'mcpToolCall':
        return
    if (any(not isinstance(item.get(key),str) or not 1 <= len(item[key]) <= 200 for key in ('id','server','tool'))
            or item.get('status') not in ('completed','failed')
            or item['tool'] not in expected.get(item['server'],[])):
        raise Attention('host MCP call differs from the pinned scope')
    encode=lambda value:json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
    result=item.get('result') or {}
    if not isinstance(result,dict):
        raise Attention('invalid host MCP result')
    structured=result.get('structuredContent')
    value={key:item[key] for key in ('id','server','tool','status')}
    value.update(arguments_sha256=hashlib.sha256(encode(item.get('arguments'))).hexdigest(),
                 result_sha256=hashlib.sha256(encode(structured)).hexdigest() if structured is not None and not item.get('error') else None)
    calls=receipt.setdefault('tool_calls',[])
    previous=next((call for call in calls if call['id']==value['id']),None)
    if previous is not None:
        if previous != value:
            raise Attention('host MCP completion changed')
        return
    if len(calls) >= 64:
        raise Attention('managed MCP call receipt exceeds 64 entries')
    calls.append(value)


def _same_cwd(value, expected):
    return isinstance(value, str) and bool(value) and str(Path(value).resolve()) == expected


def execute(pair, receipt, persist, timeout, filename, no_mcp, profile, local, complete=False, archive=False, metrics=False):
    previous = pair.get('last_run', {})
    if profile['host'] != 'codex' or pair['partner']['host'] != 'codex':
        raise Attention('managed runs require a Codex profile and partner')
    if not _same_cwd(pair['partner']['cwd'], profile['cwd']):
        raise Attention('partner directory differs from profile')
    prompt, receipt['prompt_sha256'] = prompt_text(pair, filename)
    overrides = profiles.codex_overrides(profile, local_provider=local)
    config = profiles.tomllib.loads('\n'.join(overrides[1::2]))
    expected = capabilities(config)
    if local is not None:
        config.update(model_reasoning_summary='none', web_search='disabled')
        receipt['local_provider'] = local
        overrides += ['-c', 'cli_auth_credentials_store='+profiles._toml('ephemeral'),
                      '-c', 'model_reasoning_summary='+profiles._toml('none'),
                      '-c', 'web_search='+profiles._toml('disabled')]
    if receipt['session_id'] and previous.get('capabilities', {}).get('memory_context') is not False:
        raise Attention('existing worker memory context was not checked; preserve its identity '
                        'and reconcile the assignment before preparing a fresh worker')
    if no_mcp and expected:
        raise Attention('caller requires no MCP servers, but the pinned profile enables them')
    deadline = time.monotonic() + timeout
    client, owned_turn = None, None

    def guard():
        profiles.guard(paths.data_dir(), receipt['session_id'], profile['cwd'], 'codex',
                       profile['id'], paths.write_json, expected=profile['id'])

    try:
        client = configured_client(profile, overrides, expected, deadline, local, config)
        receipt['capabilities'].update(automatic_skill_context=False, memory_context=False)
        session_id = receipt['session_id']
        if session_id:
            guard()
            eligible = (previous.get('status') == 'attention' and previous.get('phase') == 'bound'
                        and previous.get('turn_id') is None
                        and previous.get('expected_turn_id') is None
                        and previous.get('turn_submitted', False) is False)
            absence = preassignment_absence(client, session_id, deadline) if eligible else None
            if absence:
                retired = pair.setdefault('reconciled_preassignment', [])
                if not isinstance(retired, list) or len(retired) >= 8:
                    raise Attention('preassignment reconciliation history requires attention')
                retired.append({'session_id': session_id, 'last_run': previous, 'absence': absence})
                pair['partner']['id'] = None
                pair['status'] = 'needs_profile_launch'
                receipt['session_id'] = session_id = None
                persist('preflight', 'attention')
        if session_id:
            latest = latest_turn(client, session_id, deadline)
            if ((latest or {}).get('id') != receipt['expected_turn_id'] or
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
        thread = client.request(method, {**params, 'cwd': profile['cwd'], 'config': config, **({'model':local['model']} if local else {})},
                                deadline)['thread']
        native_id = paths.validate_session_id(thread['id'])
        if session_id and native_id != session_id:
            receipt['observed_session_id'] = native_id
            raise Attention('native resume returned a different identity')
        receipt['session_id'] = native_id
        persist('loading', 'unknown')
        if local is not None:
            check_thread_provider(thread, local)
        if not _same_cwd(thread.get('cwd'), profile['cwd']):
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
        if method == 'thread/start':
            if thread.get('turns') != []:
                raise Attention('fresh native thread must return an explicit empty turn history')
            # Read live metadata before history so a completed intervening turn is visible.
            metadata = client.request('thread/read', {'threadId': native_id,
                                       'includeTurns': False}, deadline)['thread']
            if (metadata.get('id') != native_id or not _same_cwd(metadata.get('cwd'), profile['cwd'])
                    or metadata.get('status', {}).get('type') != 'idle'
                    or metadata.get('turns', []) != []):
                raise Attention('fresh native thread changed before assignment')
            history_absent = False
            try:
                current = client.request('thread/read', {'threadId': native_id,
                                          'includeTurns': True}, deadline)['thread']
            except NativeError as exc:
                # Legacy persistent roots materialize their rollout at the first user message.
                absent = ('thread '+native_id+' is not materialized yet; '
                          'includeTurns is unavailable before first user message')
                if exc.code == -32601 and exc.message == 'list_turns is not supported yet':
                    try:
                        newest = latest_turn(client, native_id, deadline)
                    except NativeError as history_error:
                        if (history_error.code != -32601 or
                                history_error.message != 'list_turns is not supported yet'):
                            raise
                        # Some new-root adapters cannot list history until reloaded.
                        # Reopen the same bound identity once; never replace it or submit yet.
                        guard()
                        profiles.validate(profile)
                        client.close()
                        client = None
                        receipt['capabilities'].update(automatic_skill_context=None, memory_context=None)
                        client = configured_client(profile, overrides, expected, deadline, local, config)
                        receipt['capabilities'].update(automatic_skill_context=False, memory_context=False)
                        loaded = client.request('thread/resume', {'threadId': native_id,
                            'excludeTurns': True, 'cwd': profile['cwd'], 'config': config,
                            **({'model': local['model']} if local else {})}, deadline)['thread']
                        if local is not None:
                            check_thread_provider(loaded, local)
                        if (loaded.get('id') != native_id or not _same_cwd(loaded.get('cwd'), profile['cwd'])
                                or loaded.get('sessionId', native_id) != native_id
                                or loaded.get('status', {}).get('type') != 'idle'):
                            raise Attention('reloaded native root identity or state changed')
                        receipt['capabilities']['mcp_tools'] = discovered_tools(client, native_id, expected, deadline)
                        guard()
                        metadata = client.request('thread/read', {'threadId': native_id,
                            'includeTurns': False}, deadline)['thread']
                        if (metadata.get('id') != native_id or not _same_cwd(metadata.get('cwd'), profile['cwd'])
                                or metadata.get('status', {}).get('type') != 'idle'
                                or metadata.get('turns', []) != []):
                            raise Attention('reloaded native root changed before assignment')
                        newest = latest_turn(client, native_id, deadline)
                        receipt['history_check'] = {'method': 'thread/turns/list', 'reloaded_same_root': True}
                    if newest is not None:
                        raise Attention('fresh native thread acquired history before assignment')
                    current = {**metadata, 'turns': []}
                elif exc.code == -32600 and exc.message == absent:
                    current = metadata
                    history_absent = True
                else:
                    raise
            if (current.get('id') != native_id or not _same_cwd(current.get('cwd'), profile['cwd'])
                    or current.get('status', {}).get('type') != 'idle'
                    or not history_absent and current.get('turns') != []):
                raise Attention('fresh native thread changed before assignment')
        else:
            latest = latest_turn(client, native_id, deadline)
            if ((latest or {}).get('id') != receipt['expected_turn_id'] or
                    latest and latest.get('status') not in ('completed', 'failed', 'interrupted')):
                raise Attention('native turn changed or became active before assignment')
        receipt['turn_submitted'] = True
        persist('submitting', 'unknown')
        if metrics:
            client.metrics = TurnMetrics(native_id, time.monotonic())
        turn = client.request('turn/start', {'threadId': native_id,
                              'input': [{'type': 'text', 'text': prompt}],
                              **({'model':local['model'], 'summary':'none'} if local else {})}, deadline)['turn']
        owned_turn = paths.validate_session_id(turn['id'])
        receipt['turn_id'] = owned_turn
        persist('running', 'unknown')
        output = []
        while True:
            event = client.events.popleft() if client.events else client.next(deadline)
            params = event.get('params')
            if not isinstance(params, dict) or params.get('threadId') != native_id:
                continue
            if event.get('method') == 'item/completed' and params.get('turnId') == owned_turn:
                item = params.get('item')
                if not isinstance(item, dict):
                    raise TypeError('invalid native item payload')
                record_mcp_call(item, receipt, expected)
                if item.get('type') == 'mcpToolCall':
                    persist('running', 'unknown')
                if item.get('type') == 'agentMessage':
                    text = item.get('text', '')
                    if not isinstance(text, str):
                        raise TypeError('invalid native agentMessage text')
                    output.append(text)
            if event.get('method') == 'turn/completed':
                ended = params.get('turn')
                if not isinstance(ended, dict):
                    raise TypeError('invalid native turn completion payload')
                if ended.get('id') != owned_turn:
                    continue
                items = ended.get('items', [])
                if not isinstance(items, list) or any(not isinstance(i, dict) for i in items):
                    raise TypeError('invalid native turn items payload')
                for item in items:
                    record_mcp_call(item, receipt, expected)
                if not output:
                    for item in items:
                        if item.get('type') == 'agentMessage':
                            text = item.get('text', '')
                            if not isinstance(text, str):
                                raise TypeError('invalid native agentMessage text')
                            output.append(text)
                joined = '\n'.join(output).encode('utf-8')
                if len(joined) > TEXT_LIMIT:
                    raise Attention('worker output exceeds 64 KiB')
                receipt['output_text'] = joined.decode('utf-8')
                guard()
                if metrics:
                    observed = client.metrics.result(owned_turn)
                    if observed:
                        receipt['metrics'] = observed
                receipt['status'] = 'completed' if ended['status'] == 'completed' else 'failed'
                owned_turn = None
                if complete and receipt['status'] == 'completed':
                    state = 'archive_failed' if archive else 'closed'
                    partners.record_pair_completion(pair, paths.environment_session_id(), state)
                    receipt['completion'] = {'status': 'archive_failed' if archive else 'completed',
                                             'native_state': state}
                    if archive:
                        receipt['completion']['error'] = 'Native archive is unconfirmed.'
                persist('finished', receipt['status'])
                if complete and archive and receipt['status'] == 'completed':
                    try:
                        if partners.worker_has_open_references(pair):
                            raise Attention('worker remains referenced by another open pair')
                        if client.request('thread/archive', {'threadId': native_id}, deadline) != {}:
                            raise Attention('native archive response is invalid')
                        pair['completion']['native_state'] = 'archived'
                        receipt['completion'] = {'status': 'completed', 'native_state': 'archived'}
                        persist('finished', 'completed')
                    except (OSError, ValueError, KeyError, TypeError, TimeoutError):
                        receipt['completion'] = {'status': 'archive_failed', 'native_state': 'archive_failed',
                                                 'error': 'Native archive is unconfirmed.'}
                return
            if sum(len(t.encode()) for t in output) > TEXT_LIMIT:
                raise Attention('worker output exceeds 64 KiB')
    except (OSError, ValueError, KeyError, TypeError) as exc:
        if receipt.get('completion') and receipt.get('status') == 'completed':
            receipt['completion'] = {'status': 'completion_failed', 'error': 'Pair completion could not be recorded.'}
            return
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


def _run(pair_id, timeout, prompt_file, no_mcp, use_local, complete=False, archive=False, metrics=False):
    pair = partners.read_pair(pair_id)
    previous = pair.get('last_run', {})
    receipt = {'pair_id': pair_id, 'session_id': pair['partner']['id'],
        'profile_id': pair.get('profile'), 'turn_id': None,
        'expected_turn_id': previous.get('turn_id') or previous.get('expected_turn_id'),
        'turn_submitted': False,
        'status': 'attention', 'output_text': '', 'prompt_sha256': None,
        'capabilities': {'mcp_tools': {}, 'automatic_skill_context': None, 'memory_context': None}}
    try:
        if archive and not complete:
            raise Attention('archive requires completion')
        if pair.get('status') == 'completed':
            raise Attention('completed pair cannot run again')
        caller = paths.environment_session_id()
        if caller:
            paths.validate_session_id(caller)
            if caller not in (pair['caller']['id'], pair['partner']['id']):
                raise Attention('only a pair endpoint can run the partner')
        pending = (previous.get('status') != 'completed' and
                   (previous.get('turn_submitted') is True or
                    previous.get('phase') in ('submitting', 'running')))
        if previous.get('status') in ('unknown', 'failed') or pending:
            return public_receipt(previous)
        with paths.locked(paths.pair_path(pair_id), blocking=False):
            locked_pair = partners.read_pair(pair_id)
            if locked_pair.get('status') == 'completed':
                raise Attention('completed pair cannot run again')
            if locked_pair != pair:
                raise Attention('pair changed before ownership was acquired')
            if not math.isfinite(timeout) or not 0 < timeout <= 3600:
                raise Attention('timeout must be positive and at most 3600 seconds')
            if not pair.get('profile') or pair['partner']['id'] and not previous:
                raise Attention('managed run requires a profiled pair with managed ownership')
            def persist(phase, status):
                receipt.update(phase=phase, status=status)
                pair['last_run'] = dict(receipt)
                paths.write_json(paths.pair_path(pair_id), pair)

            preflight_done = False
            try:
                profile = profiles.load(paths.data_dir(), pair['profile'])
                local = local_provider(profile) if use_local else None
                if receipt['session_id'] and previous.get('local_provider') != local:
                    raise Attention('saved local provider mode or configuration changed')
                preflight_done = True
                execute(pair, receipt, persist, timeout, prompt_file, no_mcp, profile, local, complete, archive, metrics)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                receipt['error'] = str(exc)[:1024]
                if preflight_done or not receipt['session_id'] or not (use_local or previous.get('local_provider')):
                    persist('preflight', 'attention')
                print('agent-sessions: '+receipt['error'], file=sys.stderr)
    except (OSError, ValueError) as exc:
        print('agent-sessions: '+str(exc)[:1024], file=sys.stderr)
    return public_receipt(receipt)


def run(pair_id, timeout=60, prompt_file=None, no_mcp=False, local_provider=False, complete=False, archive=False, metrics=False):
    if threading.current_thread() is not threading.main_thread():
        return _run(pair_id, timeout, prompt_file, no_mcp, local_provider, complete, archive, metrics)
    previous = {signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGINT)}
    interrupted = False

    def interrupt(signum, frame):
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise InterruptedError('managed run interrupted by signal '+str(signum))

    try:
        for signum in previous:
            signal.signal(signum, interrupt)
        return _run(pair_id, timeout, prompt_file, no_mcp, local_provider, complete, archive, metrics)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
