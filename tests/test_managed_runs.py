"""Managed-run lifecycle through a real subprocess with a synthetic native peer."""

import hashlib
import json
import os
import subprocess
import sys
import time

from test_agent_sessions import ROOT, TOOL, Machine


class ManagedRunTests(Machine):
    def setUp(self):
        super().setUp()
        config = self.worktree / '.codex/config.toml'
        config.parent.mkdir()
        config.write_text('''[features]
hooks = false
apps = false
plugins = false
skill_search = false
[skills]
include_instructions = false
[mcp_servers.fixture]
command = "fixture"
enabled_tools = ["inspect"]
''')
        self.config = config
        self.profile = json.loads(self.run_tool('profile', 'create', 'codex',
                                               str(self.worktree), 'managed'))
        self.pair = json.loads(self.run_tool('partner', 'codex', str(self.worktree),
            'Inspect the selected artifact', '--profile', self.profile['id'],
            '--session', 'codex-main-0001'))
        self.rpc_state = self.root / 'rpc.json'
        self.rpc_state.write_text(json.dumps({'cwd': str(self.worktree)}))
        binary = self.root / 'bin'
        binary.mkdir()
        fake = binary / 'codex'
        fixture = ROOT / 'tests/fixtures/codex_app_server.py'
        fake.write_text('#!'+sys.executable+'\nimport runpy\nrunpy.run_path('
                        +repr(str(fixture))+', run_name="__main__")\n')
        fake.chmod(0o700)
        self.env.update(PATH=str(binary)+os.pathsep+os.environ['PATH'],
                        TEST_RPC_STATE=str(self.rpc_state))

    def native(self, **changes):
        state = json.loads(self.rpc_state.read_text())
        state.update(changes)
        self.rpc_state.write_text(json.dumps(state))
        return state

    def run_pair(self, timeout='5', prompt_file=None):
        extra = ['--prompt-file', str(prompt_file)] if prompt_file is not None else []
        result = subprocess.run([sys.executable, str(TOOL), 'partner', 'run', self.pair['id'],
                                 '--timeout', timeout, *extra], env=self.env, capture_output=True,
                                text=True, timeout=15, check=False)
        self.assertTrue(result.stdout.strip(), result.stderr)
        return json.loads(result.stdout)

    def methods(self):
        return [r['method'] for r in self.native().get('requests', [])]

    def repin(self, text):
        self.config.write_text(text)
        self.profile = json.loads(self.run_tool('profile', 'create', 'codex',
                                               str(self.worktree), 'managed'))
        self.pair = json.loads(self.run_tool('partner', 'codex', str(self.worktree),
            'Inspect the selected artifact', '--profile', self.profile['id'],
            '--session', 'codex-main-0001'))

    def test_completed_run_reuses_identity_and_records_receipt_without_hooks(self):
        first = self.run_pair()
        self.assertEqual(first, {'pair_id': self.pair['id'],
            'session_id': '11111111-1111-4111-8111-111111111111',
            'profile_id': self.profile['id'], 'turn_id': 'turn-1', 'status': 'completed',
            'output_text': 'result',
            'prompt_sha256': hashlib.sha256(self.pair['brief'].encode()).hexdigest(),
            'capabilities': {'mcp_tools': {'fixture': ['inspect']},
                             'automatic_skill_context': False}})
        saved = json.loads(self.run_tool('partner', 'show', self.pair['id']))
        self.assertEqual(saved['last_run']['turn_id'], first['turn_id'])
        binding = json.loads((self.data/'bindings'/(first['session_id']+'.json')).read_text())
        self.assertEqual(binding['state'], 'configured')
        second = self.run_pair()
        self.assertEqual(second['session_id'], first['session_id'])
        self.assertEqual(second['turn_id'], 'turn-2')
        self.assertEqual(self.methods().count('thread/start'), 1)
        self.assertEqual(self.methods().count('thread/resume'), 1)
        requests = self.native()['requests']
        for request in requests:
            if request['method'] in ('thread/start', 'thread/resume'):
                self.assertFalse(request['params']['config']['features']['hooks'])
                self.assertNotIn('model', request['params'])
                self.assertNotIn('sandbox', request['params'])
            if request['method'] == 'turn/start':
                self.assertEqual(request['params']['input'],
                    [{'type': 'text', 'text': self.pair['brief']}])

    def test_inherited_raw_server_blocks_before_thread_creation(self):
        self.native(unexpected_raw=True)
        self.assertEqual(self.run_pair()['status'], 'attention')
        self.assertNotIn('thread/start', self.methods())

    def test_unexpected_tool_retains_native_identity_without_assigning_work(self):
        self.native(unexpected_tool=True)
        receipt = self.run_pair()
        self.assertEqual(receipt['status'], 'attention')
        self.assertIsNotNone(receipt['session_id'])
        self.assertNotIn('turn/start', self.methods())

    def test_unknown_launch_is_not_retried_or_replaced(self):
        self.native(lose_launch=True)
        self.assertEqual(self.run_pair()['status'], 'unknown')
        self.assertEqual(self.run_pair()['status'], 'unknown')
        self.assertEqual(self.methods().count('thread/start'), 1)

    def test_timeout_interrupts_owned_turn_and_never_replays(self):
        self.native(timeout=True)
        receipt = self.run_pair('1')
        self.assertEqual(receipt['status'], 'unknown')
        self.assertEqual(self.native()['interrupted'],
                         {'threadId': receipt['session_id'], 'turnId': receipt['turn_id']})
        self.assertEqual(self.run_pair()['status'], 'unknown')
        self.assertEqual(self.methods().count('turn/start'), 1)

    def test_external_steering_blocks_the_next_assignment(self):
        self.run_pair()
        self.native(turns=[{'id': 'external-turn', 'status': 'completed'}])
        self.assertEqual(self.run_pair()['status'], 'attention')
        self.assertEqual(self.methods().count('turn/start'), 1)

    def test_server_approval_request_is_not_accepted(self):
        self.native(approval=True)
        receipt = self.run_pair()
        self.assertEqual(receipt['status'], 'unknown')
        self.assertIn('turn/interrupt', self.methods())
        self.assertTrue(all('method' in r for r in self.native()['requests']))

    def test_profile_drift_blocks_without_loading_another_thread(self):
        self.run_pair()
        self.config.write_text(self.config.read_text()+'\n')
        self.assertEqual(self.run_pair()['status'], 'attention')
        self.assertEqual(self.methods().count('thread/start'), 1)
        self.assertNotIn('thread/resume', self.methods())

    def test_prompt_file_supplies_exact_later_work_to_the_same_pair(self):
        first = self.run_pair()
        prompt = self.root / 'prompt.txt'
        raw = 'Inspect candidate B.\r\nKeep 雪.\n'.encode()
        prompt.write_bytes(raw)
        second = self.run_pair(prompt_file=prompt)
        self.assertEqual(second['session_id'], first['session_id'])
        self.assertEqual(second['prompt_sha256'], hashlib.sha256(raw).hexdigest())
        turn = [r for r in self.native()['requests'] if r['method'] == 'turn/start'][-1]
        self.assertEqual(turn['params']['input'], [{'type': 'text', 'text': raw.decode()}])
        self.assertEqual(json.loads(self.run_tool('partner', 'show', self.pair['id']))['brief'],
                         self.pair['brief'])

    def test_invalid_prompt_files_cannot_load_a_native_thread(self):
        target = self.root / 'target.txt'
        target.write_text('prompt')
        link = self.root / 'link.txt'
        link.symlink_to(target)
        self.assertEqual(self.run_pair(prompt_file=link)['status'], 'attention')
        target.write_bytes(b'x' * 65537)
        self.assertEqual(self.run_pair(prompt_file=target)['status'], 'attention')
        target.write_bytes(b'\xff')
        self.assertEqual(self.run_pair(prompt_file=target)['status'], 'attention')
        self.assertEqual(self.methods(), [])

    def test_active_native_thread_cannot_receive_a_new_assignment(self):
        self.native(active=True)
        self.assertEqual(self.run_pair()['status'], 'attention')
        self.assertNotIn('turn/start', self.methods())

    def test_timeout_before_identity_retains_launch_intent(self):
        self.native(lose_launch=True)
        receipt = self.run_pair()
        self.assertEqual(receipt['status'], 'unknown')
        pair = json.loads(self.run_tool('partner', 'show', self.pair['id']))
        self.assertEqual(pair['status'], 'profile_launch_pending')
        self.assertIsNone(pair['partner']['id'])

    def test_empty_provider_profile_runs_with_no_exposed_mcp_tools(self):
        self.repin(self.config.read_text().split('[mcp_servers.fixture]')[0])
        receipt = self.run_pair()
        self.assertEqual(receipt['status'], 'completed')
        self.assertEqual(receipt['capabilities']['mcp_tools'], {})

    def test_enabled_hooks_are_rejected_in_pinned_and_effective_settings(self):
        self.repin(self.config.read_text().replace('hooks = false', 'hooks = true'))
        self.assertEqual(self.run_pair()['status'], 'attention')
        self.assertEqual(self.methods(), [])
        self.repin(self.config.read_text().replace('hooks = true', 'hooks = false'))
        self.native(effective_hooks=True)
        self.assertEqual(self.run_pair()['status'], 'attention')
        self.assertNotIn('thread/start', self.methods())

    def test_terminal_native_failure_is_distinct_from_unknown_and_not_replayed(self):
        self.native(failed_turn=True)
        receipt = self.run_pair()
        self.assertEqual(receipt['status'], 'failed')
        self.assertEqual(self.run_pair(), receipt)
        self.assertEqual(self.methods().count('turn/start'), 1)

    def test_concurrent_pair_run_is_rejected_without_starting_a_process(self):
        import fcntl
        lock = self.data / 'pairs' / (self.pair['id']+'.json.lock')
        with lock.open('a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            self.assertEqual(self.run_pair()['status'], 'attention')
        self.assertEqual(self.methods(), [])

    def test_unresponsive_transport_cannot_block_a_large_prompt_write(self):
        self.native(stall_before_turn=True)
        prompt = self.root / 'large.txt'
        prompt.write_text('雪' * 21845)
        started = time.monotonic()
        receipt = self.run_pair('1', prompt_file=prompt)
        self.assertLess(time.monotonic()-started, 4)
        self.assertEqual(receipt['status'], 'unknown')
        self.assertIsNotNone(receipt['session_id'])

    def test_unknown_later_turn_does_not_report_the_previous_turn_as_its_identity(self):
        first = self.run_pair()
        self.native(lose_turn_response=True)
        second = self.run_pair()
        self.assertEqual(second['status'], 'unknown')
        self.assertEqual(second['session_id'], first['session_id'])
        self.assertIsNone(second['turn_id'])
        self.assertEqual(self.run_pair(), second)
        self.assertEqual(self.methods().count('turn/start'), 2)
