"""Profile configuration and reopen guards using synthetic native host records."""

import io
import json
import os
import subprocess
import sys
from unittest.mock import patch

from test_agent_sessions import TOOL_CMD, Machine

import agent_sessions
from agent_sessions import profiles as session_profiles


class ProfileTests(Machine):
    def test_inline_mcp_credentials_are_rejected_without_echoing_their_values(self):
        _, config = self.profile()
        secret = 'synthetic-private-value-for-test'
        for setting in ('env = { API_KEY = "'+secret+'" }',
                        'http_headers = { Authorization = "'+secret+'" }',
                        'bearer_token = "'+secret+'"'):
            with self.subTest(setting=setting.split('=')[0]):
                config.write_text('[mcp_servers.fixture]\ncommand = "fixture"\n'+setting+'\n')
                profile = json.loads(self.run_tool('profile', 'create', 'codex',
                                                   str(self.worktree), 'private'))
                result = subprocess.run([*TOOL_CMD, 'profile', 'launch',
                                         profile['id'], '--dry-run'], env=self.env, text=True,
                                        capture_output=True, check=False, timeout=30)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, '')
                self.assertNotIn(secret, result.stderr)
                self.assertIn('environment', result.stderr.lower())

    def test_mcp_environment_references_preserve_names_without_expanding_secrets(self):
        _, config = self.profile()
        config.write_text('[mcp_servers.fixture]\ncommand = "fixture"\n'
                          'env_vars = ["TEST_NATIVE_TOKEN"]\n'
                          'bearer_token_env_var = "TEST_NATIVE_TOKEN"\n'
                          'env_http_headers = { "X-Auth" = "TEST_NATIVE_TOKEN" }\n')
        profile = json.loads(self.run_tool('profile', 'create', 'codex',
                                           str(self.worktree), 'environment'))
        secret = 'synthetic-private-value-for-test'
        output = self.run_tool('profile', 'launch', profile['id'], '--dry-run',
                               env={**self.env, 'TEST_NATIVE_TOKEN': secret})
        self.assertIn('TEST_NATIVE_TOKEN', output)
        self.assertNotIn(secret, output)

    def test_pinned_model_provider_credentials_are_not_exposed_by_launch(self):
        _, config = self.profile()
        secret = 'synthetic-private-value-for-test'
        for setting in ('experimental_bearer_token = "'+secret+'"',
                        'http_headers = { Authorization = "'+secret+'" }',
                        'query_params = { key = "'+secret+'" }',
                        'base_url = "https://example.invalid/v1?key='+secret+'"'):
            with self.subTest(setting=setting.split('=')[0]):
                config.write_text('model = "fixture-model"\nmodel_provider = "fixture"\n'
                                  '[model_providers.fixture]\n'+setting+'\n')
                profile = json.loads(self.run_tool('profile', 'create', 'codex',
                                                   str(self.worktree), 'private-provider'))
                result = subprocess.run([*TOOL_CMD, 'profile', 'launch',
                                         profile['id'], '--dry-run'], env=self.env, text=True,
                                        capture_output=True, check=False, timeout=30)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, '')
                self.assertNotIn(secret, result.stderr)
                self.assertIn('model provider', result.stderr.lower())

    def test_existing_worker_can_be_attached_without_creating_another_session(self):
        pair=json.loads(self.run_tool('partner','attach','codex-sub-0002',
                                      '--session','codex-main-0001'))
        self.assertEqual(pair['status'],'connected')
        self.assertEqual(pair['partner']['id'],'codex-sub-0002')
        self.assertEqual(pair['partner']['cwd'],str(self.worktree))
        self.assertNotIn('request',pair)
        self.assertEqual(json.loads(self.run_tool('partner','attach','codex-sub-0002',
            '--session','codex-main-0001')),pair)

    def profile(self, host='codex', directory=None, name='reviewer',
                settings=None, mcp=None, agent_md=None, agent_layout='file'):
        directory = directory or self.worktree
        if host == 'codex':
            config = directory / '.codex/config.toml'
            config.parent.mkdir(exist_ok=True)
            config.write_text('[features]\napps = false\nplugins = false\n')
        elif host in ('agy', 'antigravity'):
            config = directory / '.agents/settings.json'
            config.parent.mkdir(parents=True, exist_ok=True)
            config.write_text(
                json.dumps(settings if settings is not None else {'disableSlashCommands': True})
                + '\n'
            )
            (directory / '.agents/mcp_config.json').write_text(
                json.dumps(mcp if mcp is not None else {'mcpServers': {}}) + '\n'
            )
            if agent_md is not None:
                if agent_layout == 'dir':
                    apath = directory / '.agents/agents' / name / 'agent.md'
                else:
                    apath = directory / '.agents/agents' / f'{name}.md'
                apath.parent.mkdir(parents=True, exist_ok=True)
                apath.write_text(agent_md)
        elif host == 'opencode':
            config = directory / 'opencode.json'
            config.write_text('{"$schema": "https://opencode.ai/config.json"}\n')
        else:
            config = directory / '.claude/settings.local.json'
            config.parent.mkdir(exist_ok=True)
            config.write_text('{"enabledPlugins": {}}\n')
            (directory / '.mcp.json').write_text('{"mcpServers": {}}\n')
        result = json.loads(self.run_tool('profile', 'create', host, str(directory), name))
        return result, config

    def hook_for(self, profile, session='codex-sub-0002', host='codex', **changes):
        env = {**self.env, 'AGENT_SESSIONS_PROFILE_ID': profile['id']}
        env.update(changes)
        payload = {'session_id': session, 'cwd': profile['cwd'],
                   'hook_event_name': 'UserPromptSubmit'}
        return subprocess.run([*TOOL_CMD, 'hook', '--host', host],
                              input=json.dumps(payload), env=env, capture_output=True,
                              text=True, timeout=30, check=False)

    def test_profiles_pin_native_files_without_copying_them(self):
        profile, config = self.profile()
        self.assertEqual(len(profile['id']), 64)
        self.assertEqual(profile['host'], 'codex')
        self.assertEqual(profile['cwd'], str(self.worktree))
        self.assertEqual(profile['files'][0]['path'], '.codex/config.toml')
        self.assertEqual(profile['files'][0]['bytes'], config.stat().st_size)
        self.assertEqual(json.loads(self.run_tool('profile', 'show', profile['id'])), profile)
        self.assertEqual(os.stat(self.data / 'profiles' / (profile['id']+'.json')).st_mode & 0o777,
                         0o600)

    def test_changed_configuration_blocks_profile_launch(self):
        profile, config = self.profile()
        config.write_text('changed')
        result = subprocess.run([*TOOL_CMD, 'profile', 'launch', profile['id'],
                                 '--dry-run'], env=self.env, capture_output=True, text=True, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('changed', result.stderr.lower())

    def test_native_launch_arguments_preserve_host_configuration(self):
        for host in ('codex', 'claude'):
            with self.subTest(host=host):
                profile, _ = self.profile(host)
                result = json.loads(self.run_tool('profile', 'launch', profile['id'], '--dry-run'))
                self.assertEqual(result['argv'][0], host)
                self.assertEqual(result['cwd'], profile['cwd'])
                self.assertEqual(result['environment']['AGENT_SESSIONS_PROFILE_ID'], profile['id'])
                self.assertNotIn('--model', result['argv'])
                if host == 'claude':
                    self.assertIn('--strict-mcp-config', result['argv'])
                    self.assertIn('--settings', result['argv'])

    def test_opencode_profile_launches_a_private_server_and_blocks_on_drift(self):
        profile, config = self.profile('opencode')
        self.assertEqual([f['path'] for f in profile['files']], ['opencode.json'])
        launch = json.loads(self.run_tool('profile', 'launch', profile['id'], '--dry-run'))
        self.assertEqual(launch['argv'], ['opencode', '--standalone'])
        bound = self.hook_for(profile, session='ses_worker0001', host='opencode')
        self.assertEqual(bound.returncode, 0, bound.stderr)
        resume = json.loads(self.run_tool('profile', 'launch', profile['id'], '--resume',
                                          'ses_worker0001', '--dry-run'))
        self.assertEqual(resume['argv'], ['opencode', '--standalone', '--session',
                                          'ses_worker0001'])
        config.write_text('{"model": "other/model"}\n')
        drifted = self.hook_for(profile, session='ses_worker0001', host='opencode')
        self.assertEqual(drifted.returncode, 2)
        self.assertIn('agent-sessions profile:', drifted.stderr)

    def test_opencode_profile_launch_removes_configuration_overrides(self):
        profile, _ = self.profile('opencode')
        binary = self.root / 'bin'
        binary.mkdir()
        program = binary / 'opencode'
        overrides = {'OPENCODE_CONFIG_PROJECT_DISABLE': '1', 'OPENCODE_DISABLE_PROJECT_CONFIG': '1',
                     'OPENCODE_CONFIG': '/other/opencode.json', 'OPENCODE_CONFIG_CONTENT': '{}'}
        variables = [f'"${{{name}+set}}"' for name in overrides]
        variables += ['"$OPENCODE_CONFIG_DIR"', '"$OPENCODE_DB"', '"$PWD"',
                      '"$AGENT_SESSIONS_PROFILE_ID"']
        program.write_text('#!/bin/sh\nprintf "%s\\n" ' + ' '.join(variables) + '\n')
        program.chmod(0o755)
        env = {**self.env, **overrides, 'OPENCODE_DB': 'custom.db',
               'PATH': str(binary) + os.pathsep + self.env['PATH']}
        output = self.run_tool('profile', 'launch', profile['id'], env=env)
        self.assertEqual(output.splitlines(), [
            '', '', '', '', str(self.opencode_config), 'custom.db', profile['cwd'], profile['id'],
        ])

    def test_symlinked_configuration_is_not_accepted(self):
        target = self.worktree / '.codex'
        target.mkdir()
        (target / 'config.toml').symlink_to(self.repo / 'a.txt')
        result = subprocess.run([*TOOL_CMD, 'profile', 'create', 'codex',
                                 str(self.worktree), 'bad'], env=self.env,
                                capture_output=True, text=True, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('regular', result.stderr.lower())

    def test_codex_launch_repeats_pinned_capability_settings_as_native_overrides(self):
        _, config = self.profile()
        config.write_text('[features]\napps = false\nplugins = false\n'
                          '[skills]\nbundled = {enabled = false}\n'
                          'config = [{name = "painter", enabled = false}]\n'
                          '[mcp_servers.fixture]\ncommand = "fixture"\n'
                          'enabled_tools = ["inspect"]\n')
        from agent_sessions import profiles as session_profiles
        profile = session_profiles.capture('codex', self.worktree, 'scoped')
        for session in (None, 'codex-sub-0002'):
            command = session_profiles.argv(profile, session)
            overrides = [command[i+1] for i, value in enumerate(command) if value == '-c']
            self.assertEqual({value.split('=', 1)[0] for value in overrides},
                             {'features', 'skills', 'mcp_servers'})
            self.assertIn('"inspect"', next(v for v in overrides if v.startswith('mcp_servers=')))
            self.assertNotIn('--model', command)
            if session:
                self.assertEqual(command[-2:], ['resume', session])

    def test_invalid_codex_toml_is_rejected_before_launch(self):
        _, config = self.profile()
        config.write_text('[broken')
        from agent_sessions import profiles as session_profiles
        profile = session_profiles.capture('codex', self.worktree, 'invalid')
        with self.assertRaisesRegex(session_profiles.ProfileError, 'TOML'):
            session_profiles.argv(profile)

    def test_codex_overrides_round_trip_model_and_capability_values(self):
        _, config = self.profile()
        config.write_text('''model = "configured-model"
model_provider = "gateway.with.dots"
model_reasoning_effort = "low"
approval_policy = "on-request"
sandbox_mode = "read-only"
[model_providers."gateway.with.dots"]
name = "Worker gateway"
base_url = "http://127.0.0.1:8095/v1"
env_key = "WORKER_GATEWAY_KEY"
wire_api = "responses"
[model_providers.unused]
name = "Unused gateway"
[apps._default]
enabled = false
[features]
apps = false
plugins = false
[mcp_servers."fixture.with.dots"]
command = "fixture"
args = ["quoted \\"value\\"", "line\\nnext", 'C:\\tools\\fixture', "雪"]
enabled_tools = ["inspect"]
startup_timeout_sec = 12.5
[plugins."fixture@catalog"]
enabled = true
[skills]
bundled = {enabled = false}
config = [{name = "painter", enabled = false}]
''')
        from agent_sessions import profiles as session_profiles
        expected = session_profiles.tomllib.loads(config.read_text())
        pinned = {key: expected[key] for key in
                  ('model', 'model_provider', 'model_reasoning_effort', 'apps', 'features', 'mcp_servers',
                   'plugins', 'skills')}
        pinned['model_providers'] = {'gateway.with.dots': expected['model_providers']['gateway.with.dots']}
        profile = session_profiles.capture('codex', self.worktree, 'roundtrip')
        for session in (None, 'codex-sub-0002'):
            with self.subTest(session=session):
                command = session_profiles.argv(profile, session)
                overrides = [command[i+1] for i, value in enumerate(command) if value == '-c']
                self.assertEqual(session_profiles.tomllib.loads('\n'.join(overrides)), pinned)
                self.assertNotIn('--model', command)
                self.assertNotIn('--ask-for-approval', command)
                self.assertNotIn('--sandbox', command)

    def test_codex_overrides_refuse_a_pinned_model_that_is_not_a_string(self):
        _, config = self.profile()
        config.write_text('model = ["configured-model"]\n')
        from agent_sessions import profiles as session_profiles
        profile = session_profiles.capture('codex', self.worktree, 'model-shape')
        with self.assertRaisesRegex(session_profiles.ProfileError, 'model and model_reasoning_effort'):
            session_profiles.argv(profile)

    def test_profiled_partner_requires_guarded_launch_instead_of_broad_native_creation(self):
        profile, _ = self.profile()
        pair = json.loads(self.run_tool('partner', 'codex', str(self.worktree), 'Check a.txt',
                                       '--profile', profile['id'], '--session', 'codex-main-0001'))
        self.assertEqual(pair['profile'], profile['id'])
        self.assertEqual(pair['status'], 'needs_profile_launch')
        self.assertNotIn('request', pair)
        self.assertIn(profile['id'], pair['launch']['argv'])

    def test_profile_bootstrap_connects_before_receiving_the_requested_work(self):
        profile, _ = self.profile()
        brief='Author a distinctive terrain mesh'
        pair=json.loads(self.run_tool('partner','codex',str(self.worktree),brief,
            '--profile',profile['id'],'--session','codex-main-0001'))
        self.assertEqual(pair['brief'],brief)
        self.assertNotIn(brief,pair['invitation'])
        self.assertIn('connect',pair['invitation'])

    def test_connect_requires_the_matching_profile_start_receipt(self):
        profile, _ = self.profile()
        pair = json.loads(self.run_tool('partner', 'codex', str(self.worktree), 'Check a.txt',
                                       '--profile', profile['id'], '--session', 'codex-main-0001'))
        command = [*TOOL_CMD, 'partner', 'connect', pair['id'],
                   'codex-sub-0002', '--session', 'codex-main-0001']
        rejected = subprocess.run(command, env=self.env, capture_output=True, text=True, check=False)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn('profile', rejected.stderr.lower())
        self.assertEqual(self.hook_for(profile).returncode, 0)
        accepted = subprocess.run(command, env=self.env, capture_output=True, text=True, check=False)
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertEqual(json.loads(accepted.stdout)['status'], 'connected')

    def test_bare_resume_and_configuration_drift_block_before_prompt_or_tool(self):
        for host, session in (('codex', 'codex-sub-0002'), ('claude', 'claude-0006')):
            with self.subTest(host=host):
                profile, config = self.profile(host)
                self.assertEqual(self.hook_for(profile, session, host).returncode, 0)
                for event in ('UserPromptSubmit', 'PreToolUse'):
                    payload = json.dumps({'session_id':session, 'cwd':profile['cwd'],
                                          'hook_event_name':event})
                    result = subprocess.run([*TOOL_CMD, 'hook', '--host', host],
                        input=payload, env=self.env, capture_output=True, text=True, check=False)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn('profile launch', result.stderr)
                config.write_text(config.read_text()+'\n')
                self.assertEqual(self.hook_for(profile, session, host).returncode, 2)

    def test_corrupt_profile_binding_does_not_turn_guard_into_an_unprofiled_hook(self):
        profile, _ = self.profile()
        self.assertEqual(self.hook_for(profile).returncode, 0)
        (self.data / 'bindings/codex-sub-0002.json').write_text('broken')
        self.assertEqual(self.hook_for(profile).returncode, 2)

    def test_claude_subagent_preserves_messages_and_profile_guard(self):
        profile, config = self.profile('claude')
        self.run_tool('send', 'claude-0006', 'for the main thread')
        payload = json.dumps({'session_id':'claude-0006', 'cwd':profile['cwd'],
                             'hook_event_name':'UserPromptSubmit', 'agent_id':'worker'})
        env = {**self.env, 'AGENT_SESSIONS_PROFILE_ID':profile['id']}
        command = [*TOOL_CMD, 'hook', '--host', 'claude']
        result = subprocess.run(command, input=payload, env=env, capture_output=True,
                                text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertIn('for the main thread', self.hook_for(
            profile, 'claude-0006', 'claude').stdout)
        for changed_env in (self.env, env):
            config.write_text(config.read_text()+'\n')
            result = subprocess.run(command, input=payload, env=changed_env,
                                    capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 2, result.stderr)

    def test_missing_binding_of_a_profiled_partner_does_not_allow_bare_reopen(self):
        profile, _ = self.profile()
        pair=json.loads(self.run_tool('partner','codex',str(self.worktree),'Check a.txt',
            '--profile',profile['id'],'--session','codex-main-0001'))
        self.assertEqual(self.hook_for(profile).returncode,0)
        self.run_tool('partner','connect',pair['id'],'codex-sub-0002','--session','codex-main-0001')
        (self.data/'bindings/codex-sub-0002.json').unlink()
        payload=json.dumps({'session_id':'codex-sub-0002','cwd':str(self.worktree),
                            'hook_event_name':'PreToolUse'})
        result=subprocess.run([*TOOL_CMD,'hook','--host','codex'],
            input=payload,env=self.env,capture_output=True,text=True,check=False)
        self.assertEqual(result.returncode,2,result.stderr)

    def test_guarded_partner_resume_reuses_identity_and_profile(self):
        profile, _ = self.profile()
        pair = json.loads(self.run_tool('partner', 'codex', str(self.worktree), 'Check a.txt',
                                       '--profile', profile['id'], '--session', 'codex-main-0001'))
        self.assertEqual(self.hook_for(profile).returncode, 0)
        self.run_tool('partner', 'connect', pair['id'], 'codex-sub-0002', '--session', 'codex-main-0001')
        launch = json.loads(self.run_tool('partner', 'resume', pair['id'], '--dry-run',
                                         '--session', 'codex-main-0001'))
        self.assertEqual(launch['argv'], ['codex', '-c',
            'features={"apps" = false, "plugins" = false}', 'resume', 'codex-sub-0002'])
        self.assertEqual(launch['environment']['AGENT_SESSIONS_PROFILE_ID'], profile['id'])

    def test_binding_write_failure_cannot_make_the_profile_hook_fail_open(self):
        profile, _ = self.profile()
        payload = json.dumps({'session_id':'codex-sub-0002', 'cwd':profile['cwd'],
                              'hook_event_name':'UserPromptSubmit'})
        env = {**self.env, 'AGENT_SESSIONS_PROFILE_ID':profile['id']}
        with patch.dict(os.environ, env, clear=True), patch('sys.stdin', io.StringIO(payload)), \
                patch('agent_sessions.paths.write_json', side_effect=OSError('test storage unavailable')):
            self.assertEqual(agent_sessions.main(['hook','--host','codex']), 2)

    def test_profile_context_errors_block_while_legacy_hook_errors_stay_nonblocking(self):
        profile,_=self.profile()
        marked={**self.env,'AGENT_SESSIONS_PROFILE_ID':profile['id']}
        invalids=('broken json','[]','{}','',json.dumps({'session_id':'../bad',
            'cwd':profile['cwd'],'hook_event_name':'UserPromptSubmit'}))
        for payload in invalids:
            with self.subTest(payload=payload):
                for env,expected in ((marked,2),(self.env,0)):
                    result=subprocess.run([*TOOL_CMD,'hook','--host','codex'],
                        input=payload,env=env,capture_output=True,text=True,check=False)
                    self.assertEqual(result.returncode,expected,result.stderr)

    def test_bound_native_session_cannot_fail_open_on_a_broken_bare_resume_payload(self):
        profile,_=self.profile()
        self.assertEqual(self.hook_for(profile).returncode,0)
        env={**self.env,'CODEX_THREAD_ID':'codex-sub-0002'}
        for payload in ('broken json','{}','[]',''):
            with self.subTest(payload=payload):
                result=subprocess.run([*TOOL_CMD,'hook','--host','codex'],
                    input=payload,env=env,capture_output=True,text=True,check=False)
                self.assertEqual(result.returncode,2,result.stderr)

    def test_unknown_host_does_not_poison_a_later_valid_profile_start(self):
        profile, _ = self.profile()
        env = {**self.env, 'AGENT_SESSIONS_PROFILE_ID':profile['id']}
        payload = json.dumps({'session_id':'codex-sub-0002', 'cwd':profile['cwd'],
                              'hook_event_name':'SessionStart'})
        rejected = subprocess.run([*TOOL_CMD,'hook'],input=payload,env=env,
                                  capture_output=True,text=True,check=False)
        self.assertEqual(rejected.returncode, 2)
        self.assertEqual(self.hook_for(profile).returncode, 0)

    def test_failed_working_directory_change_restores_pair_and_cwd(self):
        profile, _ = self.profile()
        pair = json.loads(self.run_tool('partner', 'codex', str(self.worktree), 'brief',
                                        '--profile', profile['id'], '--session', 'codex-main-0001'))
        old_cwd = os.getcwd()
        with patch.dict(os.environ, self.env, clear=True), \
                patch('agent_sessions.partners.os.chdir', side_effect=OSError('directory unavailable')):
            with self.assertRaises(OSError):
                agent_sessions.launch_profile(profile['id'], pair_id=pair['id'])
            self.assertEqual(agent_sessions.read_pair(pair['id'])['status'], 'needs_profile_launch')
        self.assertEqual(os.getcwd(), old_cwd)

    def test_missing_native_executable_restores_launchable_pair(self):
        profile, _ = self.profile()
        pair = json.loads(self.run_tool('partner', 'codex', str(self.worktree), 'brief',
                                        '--profile', profile['id'], '--session', 'codex-main-0001'))
        empty = self.root / 'empty-path'
        empty.mkdir()
        result = subprocess.run([*TOOL_CMD, 'profile', 'launch', profile['id'],
                                 '--pair', pair['id']], env={**self.env, 'PATH': str(empty)},
                                capture_output=True, text=True, timeout=30, check=False)
        self.assertNotEqual(result.returncode, 0)
        restored = json.loads(self.run_tool('partner', 'show', pair['id']))
        self.assertEqual(restored['status'], 'needs_profile_launch')

    def test_real_launcher_executes_argv_and_records_pending_creation(self):
        profile, _ = self.profile()
        pair = json.loads(self.run_tool('partner','codex',str(self.worktree),
            '--dangerously-skip-permissions is prompt text', '--profile',profile['id'],
            '--session','codex-main-0001'))
        binary = self.root/'bin';binary.mkdir()
        fake = binary/'codex'
        fake.write_text('#!'+sys.executable+'\nimport json,os,sys\n'
            'from pathlib import Path\n'
            'Path(os.environ["TEST_LAUNCH_OUTPUT"]).write_text(json.dumps({'
            '"argv":sys.argv[1:],"cwd":os.getcwd(),'
            '"profile":os.environ.get("AGENT_SESSIONS_PROFILE_ID"),'
            '"foreign":os.environ.get("CLAUDE_CODE_SESSION_ID")}))\n')
        fake.chmod(0o700)
        output=self.root/'launch.json'
        env={**self.env,'PATH':str(binary)+os.pathsep+os.environ['PATH'],
             'TEST_LAUNCH_OUTPUT':str(output),'CLAUDE_CODE_SESSION_ID':'foreign-parent'}
        self.run_tool('profile','launch',profile['id'],'--pair',pair['id'],env=env)
        observed=json.loads(output.read_text())
        self.assertEqual(observed['cwd'],str(self.worktree))
        self.assertEqual(observed['profile'],profile['id'])
        self.assertIsNone(observed['foreign'])
        self.assertEqual(observed['argv'][-2],'--')
        self.assertNotIn('--dangerously-skip-permissions',observed['argv'][-1])
        pending=json.loads(self.run_tool('partner','show',pair['id']))
        self.assertEqual(pending['status'],'profile_launch_pending')

    def test_agy_profile_capture_normalization_and_custom_agent_pinning(self):
        agent_md = '---\nname: auditor\ndescription: Read-only auditor\n---\nAudit files.\n'
        profile, _ = self.profile('antigravity', name='auditor', agent_md=agent_md,
                                  agent_layout='dir')
        self.assertEqual(profile['host'], 'agy')
        paths = [f['path'] for f in profile['files']]
        self.assertEqual(paths, [
            '.agents/agents/auditor/agent.md',
            '.agents/mcp_config.json',
            '.agents/settings.json',
        ])
        dry = json.loads(self.run_tool('profile', 'launch', profile['id'], '--dry-run',
                                       '--print', 'Audit auth.py', '--output-format', 'json'))
        self.assertEqual(dry['argv'], [
            'agy', '--settings', str(self.worktree / '.agents/settings.json'),
            '--agent', 'auditor', '--disable-slash-commands',
            '--output-format', 'json', '--print=Audit auth.py',
        ])
        self.assertNotIn('--model', dry['argv'])
        self.assertNotIn('--dangerously-skip-permissions', dry['argv'])

        # Ambiguous agent definitions (both dir and file) must fail
        (self.worktree / '.agents/agents/auditor.md').write_text(agent_md)
        both = subprocess.run([*TOOL_CMD, 'profile', 'create', 'agy',
                               str(self.worktree), 'auditor'], env=self.env,
                              capture_output=True, text=True, check=False)
        self.assertNotEqual(both.returncode, 0)

        # Invalid path under .agents/agents/ must fail
        bad_agent = self.worktree / '.agents/agents/nested/extra/agent.md'
        bad_agent.parent.mkdir(parents=True)
        bad_agent.write_text(agent_md)
        bad = subprocess.run([*TOOL_CMD, 'profile', 'create', 'agy',
                              str(self.worktree), 'other', '--file',
                              '.agents/agents/nested/extra/agent.md'], env=self.env,
                             capture_output=True, text=True, check=False)
        self.assertNotEqual(bad.returncode, 0)

    def test_agy_profile_validation_rejects_bad_settings_mcp_and_frontmatter(self):
        sentinel = 'synthetic-redacted-marker-for-test'
        bad_cases = [
            ({'hooks': {'PreToolUse': sentinel}}, {'mcpServers': {}}, None),
            ({'disableSlashCommands': sentinel}, {'mcpServers': {}}, None),
            ({'disableSlashCommands': False}, {'mcpServers': {'bad': {'env': sentinel}}}, None),
            ({'disableSlashCommands': False}, {'mcpServers': {}},
             '---\nname: wrong\n---\nbody\n'),
            ({'disableSlashCommands': False}, {'mcpServers': {}},
             '---\nname: validator\nmainAgent: false\n---\nbody\n'),
        ]
        for idx, (settings, mcp, agent_md) in enumerate(bad_cases):
            with self.subTest(case=idx):
                d = self.root / f'agy-bad-{idx}'
                d.mkdir()
                profile, _ = self.profile('agy', directory=d, name='validator',
                                          settings=settings, mcp=mcp, agent_md=agent_md)
                res = subprocess.run([*TOOL_CMD, 'profile', 'launch',
                                      profile['id'], '--dry-run'], env=self.env,
                                     capture_output=True, text=True, check=False)
                self.assertNotEqual(res.returncode, 0)
                self.assertEqual(res.stdout, '')
                self.assertNotIn(sentinel, res.stderr)

    def test_agy_headless_and_pair_bootstrap_constraints(self):
        codex_profile, _ = self.profile('codex')
        res = subprocess.run([*TOOL_CMD, 'profile', 'launch',
                              codex_profile['id'], '--dry-run', '--print', 'hi'],
                             env=self.env, capture_output=True, text=True, check=False)
        self.assertNotEqual(res.returncode, 0)

        agy_profile, _ = self.profile('agy')
        no_prompt = subprocess.run([*TOOL_CMD, 'profile', 'launch',
                                    agy_profile['id'], '--dry-run', '--headless'],
                                   env=self.env, capture_output=True, text=True, check=False)
        self.assertNotEqual(no_prompt.returncode, 0)

        pair = json.loads(self.run_tool('partner', 'agy', str(self.worktree), 'Work',
                                        '--profile', agy_profile['id'],
                                        '--session', 'codex-main-0001'))
        combo = subprocess.run([*TOOL_CMD, 'profile', 'launch',
                                agy_profile['id'], '--pair', pair['id'], '--print', 'hi',
                                '--dry-run'], env=self.env, capture_output=True, text=True,
                               check=False)
        self.assertNotEqual(combo.returncode, 0)

        interactive = json.loads(self.run_tool('profile', 'launch', agy_profile['id'],
                                               '--pair', pair['id'], '--dry-run'))
        self.assertTrue(interactive['argv'][-1].startswith('--prompt-interactive='))

    def test_agy_profile_hook_fails_closed_with_exit_0_and_structured_block(self):
        profile, config = self.profile('agy')
        env = {**self.env, 'AGENT_SESSIONS_PROFILE_ID': profile['id']}
        start_payload = json.dumps({
            'conversationId': 'agy-bound-0001',
            'hookEventName': 'SessionStart',
            'workspacePaths': [profile['cwd']],
        })
        ok = subprocess.run([*TOOL_CMD, 'hook', '--host', 'agy'],
                            input=start_payload, env=env, capture_output=True,
                            text=True, check=False)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        ok_body = json.loads(ok.stdout)
        self.assertNotIn('terminationBehavior', ok_body)

        # Bare resume without marker on PreInvocation must emit terminate JSON and exit 0
        bare_pre = subprocess.run([*TOOL_CMD, 'hook', '--host', 'agy'],
                                  input=json.dumps({
                                      'conversationId': 'agy-bound-0001',
                                      'hookEventName': 'PreInvocation',
                                      'workspacePaths': [profile['cwd']],
                                  }), env=self.env, capture_output=True, text=True, check=False)
        self.assertEqual(bare_pre.returncode, 0)
        body = json.loads(bare_pre.stdout)
        self.assertEqual(body['terminationBehavior'], 'terminate')
        self.assertIn('userMessage', body['injectSteps'][0]['errorMessage'])
        self.assertIn('agent-sessions profile:', bare_pre.stderr)

        # Config drift on PreToolUse must emit deny JSON and exit 0
        config.write_text('{"disableSlashCommands": false}\n')
        drift_tool = subprocess.run([*TOOL_CMD, 'hook', '--host', 'agy'],
                                    input=json.dumps({
                                        'conversationId': 'agy-bound-0001',
                                        'hookEventName': 'PreToolUse',
                                        'workspacePaths': [profile['cwd']],
                                    }), env=env, capture_output=True, text=True, check=False)
        self.assertEqual(drift_tool.returncode, 0)
        deny = json.loads(drift_tool.stdout)
        self.assertEqual(deny['decision'], 'deny')
        self.assertIn('agent-sessions profile:', deny['reason'])

    def test_agy_real_launcher_strips_foreign_envs_and_supports_headless_resume(self):
        profile, _ = self.profile('agy')
        binary = self.root / 'agy-bin'
        binary.mkdir()
        fake = binary / 'agy'
        fake.write_text(
            '#!'+sys.executable+'\nimport json,os,sys\nfrom pathlib import Path\n'
            'Path(os.environ["TEST_AGY_OUT"]).write_text(json.dumps({'
            '"argv":sys.argv[1:],"cwd":os.getcwd(),'
            '"profile":os.environ.get("AGENT_SESSIONS_PROFILE_ID"),'
            '"stripped":[os.environ.get(k) for k in ('
            '"CODEX_THREAD_ID","CLAUDE_CODE_SESSION_ID","CLAUDE_PID",'
            '"ANTIGRAVITY_CONVERSATION_ID","ANTIGRAVITY_AGENT")]}))\n'
        )
        fake.chmod(0o700)
        # Bind session first
        bind_env = {**self.env, 'AGENT_SESSIONS_PROFILE_ID': profile['id']}
        self.run_tool('hook', '--host', 'agy', env=bind_env, stdin=json.dumps({
            'conversationId': 'agy-res-0001', 'hookEventName': 'SessionStart',
            'workspacePaths': [profile['cwd']],
        }))
        out_path = self.root / 'agy-launch.json'
        env = {
            **self.env,
            'PATH': str(binary) + os.pathsep + os.environ['PATH'],
            'TEST_AGY_OUT': str(out_path),
            'CODEX_THREAD_ID': 'c1',
            'CLAUDE_CODE_SESSION_ID': 'cl1',
            'CLAUDE_PID': '12345',
            'ANTIGRAVITY_CONVERSATION_ID': 'a1',
            'ANTIGRAVITY_AGENT': '1',
        }
        self.run_tool('profile', 'launch', profile['id'], '--resume', 'agy-res-0001',
                      '--print', 'Follow up', '--output-format', 'stream-json', env=env)
        observed = json.loads(out_path.read_text())
        self.assertEqual(observed['cwd'], profile['cwd'])
        self.assertEqual(observed['profile'], profile['id'])
        self.assertEqual(observed['stripped'], [None, None, None, None, None])
        self.assertIn('--conversation', observed['argv'])
        self.assertIn('agy-res-0001', observed['argv'])
        self.assertIn('--output-format', observed['argv'])
        self.assertIn('stream-json', observed['argv'])
        self.assertIn('--print=Follow up', observed['argv'])

    def test_agy_frontmatter_strips_inline_yaml_comments(self):
        worker = self.root / 'agy-comment-worker'
        (worker / '.agents' / 'agents' / 'auditor').mkdir(parents=True)
        (worker / '.agents' / 'settings.json').write_text('{}\n')
        (worker / '.agents' / 'mcp_config.json').write_text('{"mcpServers": {}}\n')
        (worker / '.agents' / 'agents' / 'auditor' / 'agent.md').write_text(
            '---\nname: auditor # inline comment\nmainAgent: false # subagent\n---\nPrompt\n'
        )
        created = json.loads(self.run_tool('profile', 'create', 'agy', str(worker), 'auditor'))
        proc = subprocess.run(
            [*TOOL_CMD, 'profile', 'launch', created['id'], '--dry-run'],
            env=self.env, capture_output=True, text=True, check=False,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('mainAgent', proc.stderr)

    def test_relative_path_normalization_prevents_agent_bypass_and_duplicates(self):
        self.profile('agy', name='validator')
        bad_agent = self.worktree / '.agents/agents/nested/evil.md'
        bad_agent.parent.mkdir(parents=True)
        bad_agent.write_text('---\nname: evil\n---\nbody\n')
        res = subprocess.run(
            [*TOOL_CMD, 'profile', 'create', 'agy', str(self.worktree),
             'validator', '--file', './.agents/agents/nested/evil.md'],
            env=self.env, capture_output=True, text=True, check=False,
        )
        self.assertNotEqual(res.returncode, 0)
        self.assertIn('agent', res.stderr.lower())

        codex_profile, _ = self.profile('codex')
        dedup = json.loads(self.run_tool(
            'profile', 'create', 'codex', str(self.worktree), 'reviewer',
            '--file', './.codex/config.toml',
        ))
        self.assertEqual([f['path'] for f in dedup['files']], ['.codex/config.toml'])
        self.assertEqual(dedup['id'], codex_profile['id'])
        with self.assertRaises(session_profiles.ProfileError):
            session_profiles.binding(self.data, '../escape')

    def test_opencode_profile_validates_json_and_escapes_dash_prompt(self):
        from agent_sessions import profiles as session_profiles
        profile, config = self.profile('opencode')
        argv = session_profiles.argv(profile, prompt='--dangerously-skip-permissions')
        self.assertEqual(argv, ['opencode', '--standalone', '--prompt=--dangerously-skip-permissions'])
        config.write_text('{broken json')
        bad = session_profiles.capture('opencode', self.worktree, 'broken')
        with self.assertRaisesRegex(session_profiles.ProfileError, 'opencode.json'):
            session_profiles.argv(bad)
