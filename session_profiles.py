"""Pinned native launch configuration; not an attestation of host tool isolation."""

import hashlib
import json
import re
import stat
from pathlib import Path

try:
    import tomllib
except ImportError:  # Python 3.9 and 3.10
    import tomli as tomllib

LIMIT = 2 * 1024 * 1024
REQUIRED = {'codex': ('.codex/config.toml',),
            'claude': ('.claude/settings.local.json', '.mcp.json')}


class ProfileError(ValueError):
    """A bound worker must not continue with a different launch configuration."""


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value):
        raise ProfileError('invalid profile id; use the full id returned by profile create')
    return value


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def file_record(directory, relative):
    if (not isinstance(relative, str) or not relative or Path(relative).is_absolute()
            or '..' in Path(relative).parts):
        raise ProfileError('profile files must be relative paths inside the worker directory')
    path = directory / relative
    try:
        if path.resolve(strict=True) != path or not stat.S_ISREG(path.lstat().st_mode):
            raise ProfileError('profile configuration must be a regular file without symlinks')
        with path.open('rb') as stream:
            raw = stream.read(LIMIT + 1)
        if len(raw) > LIMIT:
            raise ProfileError('profile configuration exceeds 2 MiB')
    except OSError as exc:
        raise ProfileError(f'profile configuration changed or is unavailable: {relative}') from exc
    return {'path': relative, 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}


def capture(host, directory, name, extras=()):
    if host not in REQUIRED or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', name):
        raise ProfileError('profile needs a supported host and a simple name')
    root = Path(directory).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ProfileError('profile directory must exist')
    paths = sorted(set(REQUIRED[host]) | set(extras))
    if len(paths) > 16:
        raise ProfileError('profile supports at most 16 configuration files')
    value = {'version': 1, 'name': name, 'host': host, 'cwd': str(root),
             'files': [file_record(root, p) for p in paths]}
    return {'id': fingerprint(value), **value}


def validate(profile):
    try:
        if set(profile) != {'id', 'version', 'name', 'host', 'cwd', 'files'}:
            raise ProfileError('invalid profile fields')
        identifier(profile['id'])
        if profile['version'] != 1 or type(profile['version']) is not int:
            raise ProfileError('unsupported profile version')
        if profile['host'] not in REQUIRED or not re.fullmatch(
                r'[A-Za-z][A-Za-z0-9_-]{0,63}', profile['name']):
            raise ProfileError('invalid profile identity')
        root = Path(profile['cwd'])
        if not root.is_absolute() or root.resolve(strict=True) != root or not root.is_dir():
            raise ProfileError('profile directory changed')
        files = profile['files']
        if not isinstance(files, list) or not 1 <= len(files) <= 16:
            raise ProfileError('invalid profile file count')
        paths = [f['path'] for f in files]
        if paths != sorted(set(paths)) or not set(REQUIRED[profile['host']]) <= set(paths):
            raise ProfileError('profile native configuration is missing or duplicated')
        if fingerprint({k:v for k,v in profile.items() if k != 'id'}) != profile['id']:
            raise ProfileError('profile record changed')
        for record in files:
            if (set(record) != {'path','bytes','sha256'} or type(record['bytes']) is not int
                    or file_record(root, record['path']) != record):
                raise ProfileError('profile configuration changed: '+record['path'])
    except (OSError, KeyError, TypeError, RuntimeError) as exc:
        raise ProfileError('profile is invalid or its files changed') from exc
    return profile


def load(data, profile_id):
    path = Path(data) / 'profiles' / (identifier(profile_id)+'.json')
    try:
        with path.open('rb') as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ProfileError('profile record exceeds 64 KiB')
        profile = json.loads(raw)
        if profile['id'] != profile_id:
            raise ProfileError('profile identity changed')
        return validate(profile)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise ProfileError(f'profile configuration changed or is unavailable: {profile_id}') from exc


def binding(data, session_id):
    path = Path(data) / 'bindings' / (session_id+'.json')
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ProfileError('profile binding must be a regular file')
        with path.open('rb') as stream:
            raw = stream.read(65537)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ProfileError('cannot read profile binding') from exc
    try:
        if len(raw) > 65536:
            raise ProfileError('profile binding exceeds 64 KiB')
        value = json.loads(raw)
        if (not isinstance(value, dict) or set(value) != {'profile','host','cwd','state'}
                or value['host'] not in REQUIRED or value['state'] not in ('configured','blocked')
                or not isinstance(value['cwd'], str) or not Path(value['cwd']).is_absolute()):
            raise ProfileError('invalid profile binding')
        identifier(value['profile'])
        return value
    except (ValueError, TypeError) as exc:
        raise ProfileError('profile binding is damaged; refusing an unprofiled continuation') from exc


def guard(data, session_id, cwd, host, marker, write, expected=None):
    existing = binding(data, session_id)
    if existing is None and marker is None and expected is None:
        return False
    profile_id = existing['profile'] if existing else identifier(expected or marker)
    record = existing

    def persist(value):
        if value == existing:
            return
        try:
            write(Path(data)/'bindings'/(session_id+'.json'), value)
        except (OSError, ValueError, TypeError) as exc:
            raise ProfileError('cannot preserve profile binding; refusing to continue') from exc

    try:
        profile = load(data, profile_id)
        record = existing or {'profile':profile_id, 'host':profile['host'], 'cwd':profile['cwd'],
                              'state':'blocked'}
        if (expected not in (None,profile_id) or marker != profile_id or host != profile['host']
                or str(Path(cwd).resolve()) != profile['cwd']
                or record['host'] != profile['host'] or record['cwd'] != profile['cwd']):
            raise ProfileError('profile launch context is missing or changed')
    except (OSError, ValueError, TypeError) as exc:
        if record is not None:
            persist({**record, 'state':'blocked'})
        raise ProfileError(f'{exc}; reopen with: agent-sessions profile launch '
                           f'{profile_id} --resume {session_id}') from exc
    persist({**record, 'state':'configured'})
    return True


def _toml(value):
    if isinstance(value, dict):
        return '{'+', '.join(json.dumps(k, ensure_ascii=False)+' = '+_toml(v)
                            for k, v in value.items())+'}'
    if isinstance(value, list):
        return '['+', '.join(_toml(v) for v in value)+']'
    if value is None or not isinstance(value, (str, bool, int, float)):
        raise ProfileError('unsupported TOML value in capability settings')
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def codex_overrides(profile):
    """Repeat only capability settings at the native process override layer."""
    root = Path(profile['cwd'])
    try:
        with (root/'.codex/config.toml').open('rb') as stream:
            raw = stream.read(LIMIT + 1)
        expected = next(f for f in profile['files'] if f['path'] == '.codex/config.toml')
        if len(raw) > LIMIT or hashlib.sha256(raw).hexdigest() != expected['sha256']:
            raise ProfileError('profile configuration changed before launch')
        config = tomllib.loads(raw.decode('utf-8'))
        servers = config.get('mcp_servers', {})
        if not isinstance(servers, dict) or any(not isinstance(v, dict) for v in servers.values()):
            raise ProfileError('MCP settings must contain server tables')
        if any(server.get(key) for server in servers.values()
               for key in ('env', 'http_headers', 'bearer_token')):
            raise ProfileError('MCP inline values cannot be exposed in command arguments; use '
                               'environment references: env_vars, env_http_headers or '
                               'bearer_token_env_var')
        result = [key+'='+_toml(config[key])
                  for key in ('apps', 'features', 'mcp_servers', 'plugins', 'skills')
                  if key in config]
        if sum(len(v.encode()) for v in result) > 65536:
            raise ProfileError('capability launch overrides exceed 64 KiB')
        return [arg for value in result for arg in ('-c', value)]
    except (OSError, UnicodeError, ValueError) as exc:
        raise ProfileError(f'cannot load pinned capability TOML: {exc}') from exc


def argv(profile, session_id=None, prompt=None):
    validate(profile)
    root = Path(profile['cwd'])
    if profile['host'] == 'codex':
        command = ['codex', *codex_overrides(profile)]
        if session_id:
            command += ['resume', session_id]
    else:
        command = ['claude', '--settings', str(root/'.claude/settings.local.json'),
                   '--strict-mcp-config', '--mcp-config', str(root/'.mcp.json')]
        if session_id:
            command += ['--resume', session_id]
    if prompt is not None:
        command += ['--', prompt]
    return command
