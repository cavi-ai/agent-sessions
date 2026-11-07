"""Managed-run lifecycle through a real subprocess with a synthetic native peer."""

import hashlib
import json
import os
import signal
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
memories = false
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

    def run_pair(self, timeout='5', prompt_file=None, no_mcp=False):
        extra = ['--prompt-file', str(prompt_file)] if prompt_file is not None else []
        if no_mcp:
            extra.append('--no-mcp')
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
                             'automatic_skill_context': False, 'memory_context': False}})
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

    def test_mcp_startup_waits_for_connected_scope_before_assigning_work(self):
        self.native(mcp_states=['notStarted', 'starting', 'connected'])
        result = self.run_pair()
        self.assertEqual(result['status'], 'completed')
        methods = self.methods()
        self.assertEqual(methods.count('thread/start'), 1)
        self.assertEqual(methods.count('mcpServerStatus/list'), 3)
        self.assertEqual(methods.count('turn/start'), 1)
        self.assertLess(methods.index('mcpServerStatus/list'), methods.index('turn/start'))

    def test_terminal_mcp_states_never_start_work_or_retry_startup(self):
        for status in ('failed', 'cancelled', 'disabled', 'authenticationRequired'):
            with self.subTest(status=status):
                self.native(mcp_states=[status], requests=[], turns=[])
                self.assertEqual(self.run_pair()['status'], 'attention')
                self.assertNotIn('turn/start', self.methods())
                self.assertEqual(self.methods().count('mcpServerStatus/list'), 1)

    def test_mcp_startup_deadline_keeps_identity_without_submitting_a_turn(self):
        self.native(mcp_states=['starting'])
        result = self.run_pair('0.5')
        self.assertEqual(result['status'], 'attention')
        self.assertIsNotNone(result['session_id'])
        self.assertNotIn('turn/start', self.methods())
        self.assertNotIn('turn/interrupt', self.methods())

    def test_external_steering_during_mcp_startup_blocks_fresh_assignment(self):
        self.native(mcp_states=['starting', 'connected'], steer_during_startup=True)
        self.assertEqual(self.run_pair()['status'], 'attention')
        self.assertNotIn('turn/start', self.methods())

    def test_startup_catalog_error_does_not_accept_cached_tools(self):
        self.native(mcp_states=['starting', 'connected'],
                    mcp_tools_errors=['catalog not ready', None])
        self.assertEqual(self.run_pair()['status'], 'completed')
        self.assertEqual(self.methods().count('mcpServerStatus/list'), 2)
        self.assertEqual(self.methods().count('turn/start'), 1)

    def test_connected_discovery_error_is_not_retried(self):
        self.native(mcp_states=['connected'], mcp_tools_error='catalog failed')
        self.assertEqual(self.run_pair()['status'], 'attention')
        self.assertEqual(self.methods().count('mcpServerStatus/list'), 1)
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

    def test_no_mcp_ceiling_rejects_tool_enabled_profile_before_any_host_work(self):
        receipt = self.run_pair(no_mcp=True)
        self.assertEqual(receipt['status'], 'attention')
        self.assertIsNone(receipt['session_id'])
        self.assertIsNone(receipt['turn_id'])
        self.assertEqual(self.methods(), [])
        self.assertNotIn('native_identity', self.native())

    def test_no_mcp_ceiling_allows_an_empty_provider_scope(self):
        self.repin(self.config.read_text().split('[mcp_servers.fixture]')[0])
        receipt = self.run_pair(no_mcp=True)
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

    def test_memory_context_must_be_disabled_in_pinned_and_effective_settings(self):
        self.repin(self.config.read_text().replace('memories = false', 'memories = true'))
        self.assertEqual(self.run_pair()['status'], 'attention')
        self.assertEqual(self.methods(), [])
        self.repin(self.config.read_text().replace('memories = true', 'memories = false'))
        self.repin(self.config.read_text().replace('memories = false\n', ''))
        self.assertEqual(self.run_pair()['status'], 'attention')
        self.assertEqual(self.methods(), [])
        self.repin(self.config.read_text().replace('[features]\n', '[features]\nmemories = false\n'))
        self.native(effective_memories=True)
        self.assertEqual(self.run_pair()['status'], 'attention')
        self.assertNotIn('thread/start', self.methods())

    def test_previously_unchecked_memory_context_cannot_resume(self):
        self.assertEqual(self.run_pair()['status'], 'completed')
        path = self.data / 'pairs' / (self.pair['id'] + '.json')
        pair = json.loads(path.read_text())
        pair['last_run']['capabilities'].pop('memory_context', None)
        path.write_text(json.dumps(pair))
        before = len(self.native().get('requests', []))
        self.assertEqual(self.run_pair()['status'], 'attention')
        self.assertEqual(len(self.native().get('requests', [])), before)

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

    def cancelled_run(self, signum):
        self.native(timeout=True)
        process = subprocess.Popen([sys.executable, str(TOOL), 'partner', 'run',
                                    self.pair['id'], '--timeout', '10'],
                                   env=self.env, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic()+5
            receipt_path = self.data/'pairs'/(self.pair['id']+'.json')
            while time.monotonic() < deadline:
                saved = json.loads(receipt_path.read_text()).get('last_run', {})
                if saved.get('phase') == 'running':
                    break
                if process.poll() is not None:
                    self.fail('managed CLI exited before its owned turn was active')
                time.sleep(0.01)
            else:
                self.fail('managed CLI did not record its owned active turn')
            process.send_signal(signum)
            output, error = process.communicate(timeout=5)
            self.assertTrue(output.strip(), error)
            receipt = json.loads(output)
            self.assertEqual(process.returncode, 1)
            self.assertEqual(receipt['status'], 'unknown')
            self.assertEqual(self.native()['interrupted'],
                {'threadId': receipt['session_id'], 'turnId': receipt['turn_id']})
            self.assertEqual(self.methods().count('turn/interrupt'), 1)
            self.assertTrue(self.native()['transport_closed'])
            self.assertEqual(self.run_pair(), receipt)
            self.assertEqual(self.methods().count('turn/start'), 1)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)

    def test_sigterm_interrupts_only_the_owned_turn_and_closes_the_transport(self):
        self.cancelled_run(signal.SIGTERM)

    def test_sigint_interrupts_only_the_owned_turn_and_closes_the_transport(self):
        self.cancelled_run(signal.SIGINT)

    def test_signal_handlers_are_restored_after_a_rejected_run(self):
        import managed_runs
        previous = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
        with self.assertRaises(ValueError):
            managed_runs.run('invalid-pair')
        self.assertEqual({s: signal.getsignal(s) for s in previous}, previous)

    def test_fresh_idle_thread_does_not_need_materialized_history(self):
        self.native(reject_empty_history=True, unmaterialized_read=True)
        self.assertEqual(self.run_pair()['status'], 'completed')
        self.assertNotIn('thread/turns/list', self.methods())

    def test_fresh_history_backend_error_never_becomes_empty_history(self):
        self.native(read_history_error=True)
        self.assertEqual(self.run_pair()['status'], 'attention')
        self.assertNotIn('turn/start', self.methods())

    def test_unsupported_full_history_requires_positive_empty_latest_turn_read(self):
        self.native(unsupported_full_history=True)
        self.assertEqual(self.run_pair()['status'], 'completed')
        self.assertLess(self.methods().index('thread/turns/list'), self.methods().index('turn/start'))

    def test_unsupported_full_history_still_rejects_intervening_steering(self):
        self.native(unsupported_full_history=True, steer_after_metadata=True)
        self.assertEqual(self.run_pair()['status'], 'attention')
        self.assertNotIn('turn/start', self.methods())

    def test_unsupported_full_history_cannot_hide_latest_turn_failure(self):
        self.native(unsupported_full_history=True, history_error=True)
        self.assertEqual(self.run_pair()['status'], 'attention')
        self.assertNotIn('turn/start', self.methods())

    def test_initial_history_adapter_can_reload_the_same_unsubmitted_root_once(self):
        self.native(unsupported_initial_history=True)
        self.assertEqual(self.run_pair()['status'], 'completed')
        self.assertEqual(self.methods().count('thread/start'), 1)
        self.assertEqual(self.methods().count('thread/resume'), 1)
        self.assertEqual(self.methods().count('initialize'), 2)

    def test_reloaded_history_error_still_stops_before_assignment(self):
        self.native(unsupported_initial_history=True, history_error_after_reload=True)
        self.assertEqual(self.run_pair()['status'], 'attention')
        self.assertNotIn('turn/start', self.methods())
        self.assertEqual(self.methods().count('thread/start'), 1)

    def test_reloaded_metadata_history_cannot_be_hidden_by_an_empty_latest_page(self):
        self.native(unsupported_initial_history=True, reloaded_metadata_turns=True)
        self.assertEqual(self.run_pair()['status'], 'attention')
        self.assertNotIn('turn/start', self.methods())

    def test_latest_turn_requires_a_valid_turn_page(self):
        from types import SimpleNamespace

        import managed_runs
        for data in ({}, '', None, [{}], ['turn']):
            with self.subTest(data=data):
                client = SimpleNamespace(request=lambda *_, value=data: {'data': value, 'nextCursor': None})
                with self.assertRaises(managed_runs.Attention):
                    managed_runs.latest_turn(client, 'native-id', time.monotonic()+1)
        client = SimpleNamespace(request=lambda *_: {'data': [], 'nextCursor': 'unexpected'})
        with self.assertRaises(managed_runs.Attention):
            managed_runs.latest_turn(client, 'native-id', time.monotonic()+1)

    def test_unsubmitted_existing_root_resumes_without_full_history_support(self):
        self.native(unexpected_tool=True)
        first = self.run_pair()
        self.native(unexpected_tool=False, unsupported_full_history=True)
        second = self.run_pair()
        self.assertEqual(second['status'], 'completed')
        self.assertEqual(second['session_id'], first['session_id'])
        self.assertEqual(self.methods().count('thread/start'), 1)

    def test_steering_after_fresh_metadata_is_checked_by_the_history_read(self):
        self.native(unmaterialized_read=True, steer_after_metadata=True)
        self.assertEqual(self.run_pair()['status'], 'attention')
        self.assertNotIn('turn/start', self.methods())
        reads = [r for r in self.native()['requests'] if r['method'] == 'thread/read']
        self.assertEqual([r['params']['includeTurns'] for r in reads], [False, True])

    def test_unsubmitted_attention_reconciles_only_explicit_native_absence(self):
        self.native(unexpected_tool=True)
        first = self.run_pair()
        saved = json.loads(self.run_tool('partner', 'show', self.pair['id']))['last_run']
        self.native(unexpected_tool=False, native_absent=True)
        second = self.run_pair()
        self.assertEqual(second['status'], 'completed')
        self.assertNotEqual(second['session_id'], first['session_id'])
        pair = json.loads(self.run_tool('partner', 'show', self.pair['id']))
        prior = pair['reconciled_preassignment'][0]
        self.assertEqual(prior['session_id'], first['session_id'])
        self.assertEqual(prior['last_run'], saved)
        self.assertEqual([p['method'] for p in prior['absence']],
                         ['thread/read', 'thread/turns/list'])

    def test_history_backend_failure_does_not_authorize_replacement(self):
        self.native(unexpected_tool=True)
        self.run_pair()
        self.native(unexpected_tool=False, native_absent=True, history_error=True)
        self.assertEqual(self.run_pair()['status'], 'attention')
        self.assertEqual(self.methods().count('thread/start'), 1)
        saved = json.loads(self.run_tool('partner', 'show', self.pair['id']))['last_run']
        self.assertIn('-32603', saved['error'])
        self.assertIn('history backend unavailable', saved['error'])

    def test_submitted_receipt_cannot_reconcile_a_missing_native_thread(self):
        self.native(unexpected_tool=True)
        self.run_pair()
        path = self.data/'pairs'/(self.pair['id']+'.json')
        pair = json.loads(path.read_text())
        pair['last_run']['turn_submitted'] = True
        path.write_text(json.dumps(pair))
        self.native(unexpected_tool=False, native_absent=True)
        before = self.methods()
        self.assertEqual(self.run_pair()['status'], 'attention')
        self.assertEqual(self.methods(), before)
        self.assertEqual(self.methods().count('thread/start'), 1)

    def test_missing_completed_thread_is_not_replaced(self):
        self.run_pair()
        self.native(native_absent=True)
        self.assertEqual(self.run_pair()['status'], 'attention')
        self.assertEqual(self.methods().count('thread/start'), 1)

    def test_foreign_native_caller_cannot_assign_through_another_pair(self):
        path = self.data/'pairs'/(self.pair['id']+'.json')
        before = path.read_bytes()
        self.env['CODEX_THREAD_ID'] = 'foreign-coordinator'
        self.assertEqual(self.run_pair()['status'], 'attention')
        self.assertEqual(self.methods(), [])
        self.assertEqual(path.read_bytes(), before)

    def test_host_mcp_call_digests_omit_arguments_and_result_text(self):
        args={'selected':'private source'}
        value={'status':'built','source':'private result'}
        event={'type':'mcpToolCall','id':'call-1','server':'fixture','tool':'inspect',
               'arguments':args,'status':'completed','result':{'content':[], 'structuredContent':value},'error':None}
        self.native(mcp_items=[event,event])
        result=self.run_pair()
        self.assertEqual(result['status'],'completed')
        encode=lambda value:json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
        self.assertEqual(result['tool_calls'],[{'id':'call-1','server':'fixture','tool':'inspect',
            'status':'completed','arguments_sha256':hashlib.sha256(encode(args)).hexdigest(),
            'result_sha256':hashlib.sha256(encode(value)).hexdigest()}])
        self.assertNotIn('private source',json.dumps(result))
        self.assertNotIn('private result',json.dumps(result))

    def test_foreign_turn_mcp_events_cannot_attest_this_assignment(self):
        event={'type':'mcpToolCall','id':'foreign-call','server':'fixture','tool':'inspect',
               'arguments':{},'status':'completed','result':{'content':[],'structuredContent':{}},'error':None}
        self.native(mcp_items=[event],mcp_turn='unowned-turn')
        result=self.run_pair()
        self.assertEqual(result['status'],'completed')
        self.assertNotIn('tool_calls',result)

    def test_protocol_advertises_host_call_receipts_before_launch(self):
        self.assertEqual(json.loads(self.run_tool('protocol')),
        {'version':1,'managed_run':{'mcp_call_receipts':'canonical-json-sha256',
            'maximum_mcp_calls':64,'memory_context':'disabled-before-first-turn'}})
        self.assertEqual(self.methods(),[])
