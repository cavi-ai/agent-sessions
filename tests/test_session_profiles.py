"""Profile configuration and reopen guards using synthetic native host records."""

import io
import json
import os
import subprocess
import sys
from unittest.mock import patch

from test_agent_sessions import TOOL, Machine

import agent_sessions


class ProfileTests(Machine):
    def test_existing_worker_can_be_attached_without_creating_another_session(self):
        pair=json.loads(self.run_tool('partner','attach','codex-sub-0002',
                                      '--session','codex-main-0001'))
        self.assertEqual(pair['status'],'connected')
        self.assertEqual(pair['partner']['id'],'codex-sub-0002')
        self.assertEqual(pair['partner']['cwd'],str(self.worktree))
        self.assertNotIn('request',pair)
        self.assertEqual(json.loads(self.run_tool('partner','attach','codex-sub-0002',
            '--session','codex-main-0001')),pair)

    def profile(self, host='codex', directory=None):
        directory = directory or self.worktree
        if host == 'codex':
            config = directory / '.codex/config.toml'
            config.parent.mkdir(exist_ok=True)
            config.write_text('[features]\napps = false\nplugins = false\n')
        else:
            config = directory / '.claude/settings.local.json'
            config.parent.mkdir(exist_ok=True)
            config.write_text('{"enabledPlugins": {}}\n')
            (directory / '.mcp.json').write_text('{"mcpServers": {}}\n')
        result = json.loads(self.run_tool('profile', 'create', host, str(directory), 'reviewer'))
        return result, config

    def hook_for(self, profile, session='codex-sub-0002', host='codex', **changes):
        env = {**self.env, 'AGENT_SESSIONS_PROFILE_ID': profile['id']}
        env.update(changes)
        payload = {'session_id': session, 'cwd': profile['cwd'],
                   'hook_event_name': 'UserPromptSubmit'}
        return subprocess.run([sys.executable, str(TOOL), 'hook', '--host', host],
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
        result = subprocess.run([sys.executable, str(TOOL), 'profile', 'launch', profile['id'],
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

    def test_symlinked_configuration_is_not_accepted(self):
        target = self.worktree / '.codex'
        target.mkdir()
        (target / 'config.toml').symlink_to(self.repo / 'a.txt')
        result = subprocess.run([sys.executable, str(TOOL), 'profile', 'create', 'codex',
                                 str(self.worktree), 'bad'], env=self.env,
                                capture_output=True, text=True, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('regular', result.stderr.lower())

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
        command = [sys.executable, str(TOOL), 'partner', 'connect', pair['id'],
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
                    result = subprocess.run([sys.executable, str(TOOL), 'hook', '--host', host],
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
        command = [sys.executable, str(TOOL), 'hook', '--host', 'claude']
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
        result=subprocess.run([sys.executable,str(TOOL),'hook','--host','codex'],
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
        self.assertEqual(launch['argv'], ['codex', 'resume', 'codex-sub-0002'])
        self.assertEqual(launch['environment']['AGENT_SESSIONS_PROFILE_ID'], profile['id'])

    def test_binding_write_failure_cannot_make_the_profile_hook_fail_open(self):
        profile, _ = self.profile()
        payload = json.dumps({'session_id':'codex-sub-0002', 'cwd':profile['cwd'],
                              'hook_event_name':'UserPromptSubmit'})
        env = {**self.env, 'AGENT_SESSIONS_PROFILE_ID':profile['id']}
        with patch.dict(os.environ, env, clear=True), patch('sys.stdin', io.StringIO(payload)), \
                patch('agent_sessions.write_json', side_effect=OSError('test storage unavailable')):
            self.assertEqual(agent_sessions.main(['hook','--host','codex']), 2)

    def test_profile_context_errors_block_while_legacy_hook_errors_stay_nonblocking(self):
        profile,_=self.profile()
        marked={**self.env,'AGENT_SESSIONS_PROFILE_ID':profile['id']}
        invalids=('broken json','[]','{}','',json.dumps({'session_id':'../bad',
            'cwd':profile['cwd'],'hook_event_name':'UserPromptSubmit'}))
        for payload in invalids:
            with self.subTest(payload=payload):
                for env,expected in ((marked,2),(self.env,0)):
                    result=subprocess.run([sys.executable,str(TOOL),'hook','--host','codex'],
                        input=payload,env=env,capture_output=True,text=True,check=False)
                    self.assertEqual(result.returncode,expected,result.stderr)

    def test_bound_native_session_cannot_fail_open_on_a_broken_bare_resume_payload(self):
        profile,_=self.profile()
        self.assertEqual(self.hook_for(profile).returncode,0)
        env={**self.env,'CODEX_THREAD_ID':'codex-sub-0002'}
        for payload in ('broken json','{}','[]',''):
            with self.subTest(payload=payload):
                result=subprocess.run([sys.executable,str(TOOL),'hook','--host','codex'],
                    input=payload,env=env,capture_output=True,text=True,check=False)
                self.assertEqual(result.returncode,2,result.stderr)

    def test_unknown_host_does_not_poison_a_later_valid_profile_start(self):
        profile, _ = self.profile()
        env = {**self.env, 'AGENT_SESSIONS_PROFILE_ID':profile['id']}
        payload = json.dumps({'session_id':'codex-sub-0002', 'cwd':profile['cwd'],
                              'hook_event_name':'SessionStart'})
        rejected = subprocess.run([sys.executable,str(TOOL),'hook'],input=payload,env=env,
                                  capture_output=True,text=True,check=False)
        self.assertEqual(rejected.returncode, 2)
        self.assertEqual(self.hook_for(profile).returncode, 0)

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
