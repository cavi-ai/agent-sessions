"""Pinned native launch configuration; not an attestation of host tool isolation."""

import hashlib
import json
import re
import stat
from pathlib import Path
from urllib.parse import urlsplit

try:
    import tomllib
except ImportError:  # Python 3.9 and 3.10
    import tomli as tomllib

LIMIT = 2 * 1024 * 1024
REQUIRED = {'codex': ('.codex/config.toml',),
            'claude': ('.claude/settings.local.json', '.mcp.json'),
            'agy': ('.agents/settings.json', '.agents/mcp_config.json'),
            'opencode': ('opencode.json',)}
HOST_ALIASES = {'antigravity': 'agy'}
AGENT_FILE_RE = re.compile(r'^\.agents/agents/([A-Za-z0-9][A-Za-z0-9_-]{0,63})(?:/agent)?\.md$')
SESSION_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,199}$')


def normalize_host(host):
    return HOST_ALIASES.get(host, host)


def _agent_files(paths):
    found = []
    for rel in paths:
        if isinstance(rel, str) and (rel == '.agents/agents' or rel.startswith('.agents/agents/')):
            match = AGENT_FILE_RE.fullmatch(rel)
            if not match:
                raise ProfileError('invalid custom agent file path under .agents/agents/')
            found.append((rel, match.group(1)))
    return found


class ProfileError(ValueError):
    """A bound worker must not continue with a different launch configuration."""


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value):
        raise ProfileError('invalid profile id; use the full id returned by profile create')
    return value


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _canonical_relative(relative):
    if not isinstance(relative, str) or not relative or '\\' in relative:
        raise ProfileError('profile files must be relative paths inside the worker directory')
    rel = Path(relative)
    if rel.is_absolute() or '..' in rel.parts:
        raise ProfileError('profile files must be relative paths inside the worker directory')
    canonical = rel.as_posix()
    if canonical in ('', '.'):
        raise ProfileError('profile files must be relative paths inside the worker directory')
    return canonical


def file_record(directory, relative):
    if _canonical_relative(relative) != relative:
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
    host = normalize_host(host)
    if host not in REQUIRED or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', name):
        raise ProfileError('profile needs a supported host and a simple name')
    root = Path(directory).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ProfileError('profile directory must exist')
    extra_paths = {_canonical_relative(p) for p in extras}
    agents = _agent_files(extra_paths)
    if host == 'agy' and not agents:
        dir_agent = f'.agents/agents/{name}/agent.md'
        file_agent = f'.agents/agents/{name}.md'
        has_dir = (root / dir_agent).exists() or (root / dir_agent).is_symlink()
        has_file = (root / file_agent).exists() or (root / file_agent).is_symlink()
        if has_dir and has_file:
            raise ProfileError('ambiguous custom agent definition; keep only one agent file')
        if has_dir:
            extra_paths.add(dir_agent)
        elif has_file:
            extra_paths.add(file_agent)
    paths = sorted(set(REQUIRED[host]) | extra_paths)
    if len(_agent_files(paths)) > 1:
        raise ProfileError('profile supports at most one custom agent file')
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
        if len(_agent_files(paths)) > 1:
            raise ProfileError('profile supports at most one custom agent file')
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
    if not isinstance(session_id, str) or not SESSION_ID_RE.fullmatch(session_id):
        raise ProfileError('invalid session id')
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
    host = normalize_host(host)
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


def pinned_codex_config(profile):
    """Read the exact pinned native configuration."""
    raw = _read_pinned(profile, '.codex/config.toml')
    try:
        return tomllib.loads(raw.decode('utf-8'))
    except (UnicodeError, ValueError) as exc:
        raise ProfileError(f'cannot load pinned capability TOML: {exc}') from exc


def _codex_provider_overrides(config, local_provider):
    if 'model' not in config or 'model_provider' not in config:
        return {}
    name = config['model_provider']
    if not isinstance(name, str) or not name.strip():
        raise ProfileError('pinned model_provider must be a nonempty string')
    overrides = {'model_provider': name}
    if local_provider is not None:
        overrides['model_providers'] = {name: local_provider['provider']}
        return overrides

    providers = config.get('model_providers', {})
    if not isinstance(providers, dict):
        raise ProfileError('model providers must contain provider tables')
    if name not in providers:
        return overrides
    definition = providers[name]
    if not isinstance(definition, dict):
        raise ProfileError('selected model provider must be a table')
    if any(definition.get(key) for key in ('experimental_bearer_token', 'http_headers',
                                          'query_params', 'auth', 'gateway_oauth', 'aws')):
        raise ProfileError('model provider credentials cannot be exposed in command arguments; '
                           'use env_key or env_http_headers, or define the provider in native user config')
    for key in ('base_url', 'model_catalog_url'):
        if key not in definition:
            continue
        if not isinstance(definition[key], str):
            raise ProfileError('model provider URLs must be strings')
        try:
            url = urlsplit(definition[key])
        except ValueError:
            raise ProfileError('invalid model provider URL') from None
        if url.username is not None or url.password is not None or url.query or url.fragment:
            raise ProfileError('model provider URLs cannot contain credentials, queries or fragments')
    overrides['model_providers'] = {name: definition}
    return overrides


def codex_overrides(profile, *, local_provider=None):
    """Repeat the pinned model, reasoning effort and capability settings as native overrides."""
    try:
        config = pinned_codex_config(profile)
        if any(key in config and (not isinstance(config[key], str) or not config[key].strip())
               for key in ('model', 'model_reasoning_effort')):
            raise ProfileError('pinned model and model_reasoning_effort must be nonempty strings')
        servers = config.get('mcp_servers', {})
        if not isinstance(servers, dict) or any(not isinstance(v, dict) for v in servers.values()):
            raise ProfileError('MCP settings must contain server tables')
        if any(server.get(key) for server in servers.values()
               for key in ('env', 'http_headers', 'bearer_token')):
            raise ProfileError('MCP inline values cannot be exposed in command arguments; use '
                               'environment references: env_vars, env_http_headers or '
                               'bearer_token_env_var')
        overrides = {key: config[key]
                     for key in ('model', 'model_reasoning_effort', 'apps', 'features',
                                 'mcp_servers', 'plugins', 'skills') if key in config}
        overrides.update(_codex_provider_overrides(config, local_provider))
        result = [key+'='+_toml(value) for key, value in overrides.items()]
        if sum(len(v.encode()) for v in result) > 65536:
            raise ProfileError('capability launch overrides exceed 64 KiB')
        return [arg for value in result for arg in ('-c', value)]
    except (OSError, UnicodeError, ValueError) as exc:
        raise ProfileError(f'cannot load pinned capability TOML: {exc}') from exc


def _read_pinned(profile, relative):
    root = Path(profile['cwd'])
    expected = next((f for f in profile['files'] if f['path'] == relative), None)
    if expected is None:
        raise ProfileError(f'profile configuration is missing: {relative}')
    try:
        path = root / relative
        if path.resolve(strict=True) != path or not stat.S_ISREG(path.lstat().st_mode):
            raise ProfileError('profile configuration must be a regular file without symlinks')
        with path.open('rb') as stream:
            raw = stream.read(LIMIT + 1)
    except OSError as exc:
        raise ProfileError(f'profile configuration changed or is unavailable: {relative}') from exc
    if len(raw) > LIMIT or hashlib.sha256(raw).hexdigest() != expected['sha256']:
        raise ProfileError(f'profile configuration changed before launch: {relative}')
    return raw


def _parse_frontmatter(text):
    lines = text.splitlines()
    if not lines or lines[0].strip() != '---':
        raise ProfileError('pinned custom agent must start with YAML frontmatter')
    end = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == '---':
            end = idx
            break
    if end is None:
        raise ProfileError('pinned custom agent frontmatter is not terminated')
    meta = {}
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        match = re.match(r'^([A-Za-z0-9_-]+)\s*:\s*(.*)$', line)
        if match:
            key, val = match.group(1), match.group(2).strip()
            quoted = re.match(r'^("([^"]*)"|\'([^\']*)\')(?:\s+#.*)?$', val)
            if quoted:
                val = quoted.group(2) if quoted.group(2) is not None else quoted.group(3)
            else:
                val = re.sub(r'(?:^|\s+)#.*$', '', val).strip()
            meta[key] = val
    return meta


def agy_overrides(profile):
    """Validate pinned agy files and return native CLI override flags."""
    root = Path(profile['cwd'])
    raw_settings = _read_pinned(profile, '.agents/settings.json')
    try:
        settings = json.loads(raw_settings.decode('utf-8'))
    except (UnicodeError, ValueError) as exc:
        raise ProfileError('invalid JSON in .agents/settings.json') from exc
    if not isinstance(settings, dict):
        raise ProfileError('.agents/settings.json must be a JSON object')
    if 'hooks' in settings:
        raise ProfileError('hooks must not be configured in .agents/settings.json')
    disable_slash = settings.get('disableSlashCommands')
    if disable_slash is not None and not isinstance(disable_slash, bool):
        raise ProfileError('disableSlashCommands in .agents/settings.json must be a boolean')

    raw_mcp = _read_pinned(profile, '.agents/mcp_config.json')
    try:
        mcp = json.loads(raw_mcp.decode('utf-8'))
    except (UnicodeError, ValueError) as exc:
        raise ProfileError('invalid JSON in .agents/mcp_config.json') from exc
    if not isinstance(mcp, dict):
        raise ProfileError('.agents/mcp_config.json must be a JSON object')
    if 'mcpServers' in mcp:
        servers = mcp['mcpServers']
        if not isinstance(servers, dict) or any(not isinstance(v, dict) for v in servers.values()):
            raise ProfileError('mcpServers in .agents/mcp_config.json must be a map of objects')
        for server in servers.values():
            if not any(isinstance(server.get(k), str) and server.get(k)
                       for k in ('command', 'url', 'serverUrl')):
                raise ProfileError('each MCP server must specify command, url, or serverUrl')

    args = ['--settings', str(root / '.agents/settings.json')]
    agents = _agent_files([f['path'] for f in profile['files']])
    if agents:
        agent_path, agent_name = agents[0]
        raw_agent = _read_pinned(profile, agent_path)
        try:
            text = raw_agent.decode('utf-8')
        except UnicodeError as exc:
            raise ProfileError('pinned custom agent must be valid UTF-8') from exc
        meta = _parse_frontmatter(text)
        if meta.get('name') != agent_name:
            raise ProfileError('pinned custom agent frontmatter name must match its file path')
        if meta.get('mainAgent', '').lower() == 'false':
            raise ProfileError('pinned custom agent cannot disable mainAgent')
        args += ['--agent', agent_name]
    if disable_slash is True:
        args.append('--disable-slash-commands')
    return args


def argv(profile, session_id=None, prompt=None, headless=False, output_format=None):
    validate(profile)
    if output_format not in (None, 'text', 'json', 'stream-json'):
        raise ProfileError('output_format must be text, json, or stream-json')
    if output_format is not None and not headless:
        raise ProfileError('output_format requires headless mode')
    if headless and profile['host'] != 'agy':
        raise ProfileError('headless mode is only supported for agy profiles')
    root = Path(profile['cwd'])
    if profile['host'] == 'agy':
        command = ['agy', *agy_overrides(profile)]
        if session_id:
            command += ['--conversation', session_id]
        if headless:
            if prompt is None:
                raise ProfileError('headless mode requires a prompt')
            if output_format is not None:
                command += ['--output-format', output_format]
            command += [f'--print={prompt}']
        elif prompt is not None:
            command += [f'--prompt-interactive={prompt}']
        return command
    if profile['host'] == 'opencode':
        raw_opencode = _read_pinned(profile, 'opencode.json')
        try:
            opencode_cfg = json.loads(raw_opencode.decode('utf-8'))
        except (UnicodeError, ValueError) as exc:
            raise ProfileError('invalid JSON in opencode.json') from exc
        if not isinstance(opencode_cfg, dict):
            raise ProfileError('opencode.json must be a JSON object')
        # A private server inherits the launch environment, so its hooks see the profile marker.
        command = ['opencode', '--standalone']
        if session_id:
            command += ['--session', session_id]
        if prompt is not None:
            command += [f'--prompt={prompt}']
        return command
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
