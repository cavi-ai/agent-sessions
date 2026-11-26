"""Resource admission is durable control state, independent of peer messages."""

import concurrent.futures
import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / 'src') not in sys.path:
    sys.path.insert(0, str(ROOT / 'src'))

from agent_sessions.resource_broker import Broker, current_actor

EVIDENCE = hashlib.sha256(b'verified supervisor receipt').hexdigest()


class ResourceBrokerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.births = {1: 'first', 2: 'second', 3: 'third'}
        self.now = 1000.0
        self.broker = Broker(self.root, identity=self.births.get, clock=lambda: self.now)
        self.actors = [{'id': 'owner-'+str(n), 'pid': n, 'birth': self.births[n]} for n in (1, 2, 3)]

    def request(self, number, key=None):
        actor = self.actors[number]
        return self.broker.request('native.unity', key or 'job-'+str(number), actor, 60)

    def start(self, ticket, actor):
        granted = self.broker.claim(ticket['id'], actor)
        return self.broker.running(ticket['id'], actor, granted['lease'], granted['fence'])

    def test_fifo_is_atomic_and_later_requests_cannot_overtake(self):
        a, b, c = [self.request(n) for n in range(3)]
        self.assertEqual(self.broker.claim(c['id'], self.actors[2])['state'], 'queued')
        running = self.start(a, self.actors[0])
        self.assertEqual(self.broker.claim(b['id'], self.actors[1])['position'], 1)
        self.broker.finish(a['id'], self.actors[0], running['lease'], running['fence'], 'completed', EVIDENCE)
        second = self.start(b, self.actors[1])
        self.assertGreater(second['fence'], running['fence'])
        self.assertEqual(self.broker.claim(c['id'], self.actors[2])['state'], 'queued')

    def test_retry_is_one_ticket_and_changed_request_is_rejected(self):
        a = self.request(0)
        self.assertEqual(self.request(0)['id'], a['id'])
        with self.assertRaisesRegex(ValueError, 'different'):
            self.broker.request('native.unity', 'job-0', self.actors[0], 61)
        self.assertEqual(self.broker.events(self.actors[0]), [])
        self.assertFalse((self.root / 'queue').exists())

    def test_cancel_and_expiry_never_launch_and_do_not_block_fifo(self):
        a, b = self.request(0), self.request(1)
        self.broker.cancel(a['id'], self.actors[0])
        self.now += 61
        self.assertEqual(self.broker.claim(b['id'], self.actors[1])['state'], 'expired')
        c = self.request(2)
        self.assertEqual(self.broker.claim(c['id'], self.actors[2])['state'], 'granted')

    def test_dead_prelaunch_owner_can_be_removed_but_unknown_native_exit_cannot(self):
        a, b = self.request(0), self.request(1)
        self.broker.claim(a['id'], self.actors[0])
        self.births.pop(1)
        second = self.start(b, self.actors[1])
        self.births[2] = 'reused-pid'
        c = self.request(2)
        self.assertEqual(self.broker.claim(c['id'], self.actors[2])['state'], 'queued')
        state = self.broker.snapshot(b['id'], self.actors[1])
        self.assertEqual(state['state'], 'unresolved')
        self.assertEqual(state['fence'], second['fence'])
        self.now += 100000
        self.assertEqual(self.broker.snapshot(b['id'], self.actors[1])['state'], 'unresolved')

    def test_restart_fencing_owner_and_evidence_are_preserved(self):
        a = self.request(0)
        running = self.start(a, self.actors[0])
        restarted = Broker(self.root, identity=self.births.get, clock=lambda: self.now)
        for actor, lease, fence, evidence in (
            (self.actors[1], running['lease'], running['fence'], EVIDENCE),
            (self.actors[0], 'foreign', running['fence'], EVIDENCE),
            (self.actors[0], running['lease'], running['fence']+1, EVIDENCE),
            (self.actors[0], running['lease'], running['fence'], None),
        ):
            with self.subTest(actor=actor), self.assertRaises(ValueError):
                restarted.finish(a['id'], actor, lease, fence, 'completed', evidence)
        self.assertNotIn('lease', restarted.snapshot(a['id'], self.actors[1]))
        restarted.finish(a['id'], self.actors[0], running['lease'], running['fence'], 'completed', EVIDENCE)
        restarted.finish(a['id'], self.actors[0], running['lease'], running['fence'], 'completed', EVIDENCE)
        events = restarted.events(self.actors[0])
        self.assertEqual(len(events), 1)
        self.assertEqual(restarted.events(self.actors[0], events[0]['cursor']), [])
        self.assertEqual(restarted.events(self.actors[1]), [])

    def test_wait_is_bounded_without_peer_messages(self):
        a = self.request(0)
        running = self.start(a, self.actors[0])
        b = self.request(1)
        self.assertEqual(self.broker.wait(b['id'], self.actors[1], .02)['state'], 'cancelled')
        self.assertEqual(self.broker.snapshot(a['id'], self.actors[0])['fence'], running['fence'])
        self.assertFalse((self.root / 'queue').exists())

    def test_unresolved_finish_rejects_non_digest_evidence(self):
        a = self.request(0)
        running = self.start(a, self.actors[0])
        with self.assertRaisesRegex(ValueError, 'SHA256'):
            self.broker.finish(a['id'], self.actors[0], running['lease'], running['fence'],
                               'unresolved', 'not-a-digest')
        state = self.broker.finish(a['id'], self.actors[0], running['lease'], running['fence'],
                                   'unresolved', None)
        self.assertEqual(state['state'], 'unresolved')

    def test_current_actor_prefers_nearer_host_session_over_codex(self):
        clean_env = {k: v for k, v in os.environ.items() if k not in (
            'CODEX_THREAD_ID', 'CLAUDE_CODE_SESSION_ID', 'CLAUDE_PID',
            'ANTIGRAVITY_CONVERSATION_ID', 'ANTIGRAVITY_AGENT', 'OPENCODE_SESSION_ID',
        )}
        nested_env = {
            **clean_env,
            'CODEX_THREAD_ID': 'codex-outer-session',
            'CLAUDE_CODE_SESSION_ID': 'claude-inner-session',
            'CLAUDE_PID': str(os.getppid()),
        }
        with mock.patch.dict(os.environ, nested_env, clear=True):
            actor = current_actor(pid=os.getpid())
            self.assertEqual(actor['id'], 'claude-inner-session')
            explicit = current_actor('explicit-owner', pid=os.getpid())
            self.assertEqual(explicit['id'], 'explicit-owner')
        with mock.patch.dict(os.environ, clean_env, clear=True):
            fallback = current_actor(pid=os.getpid())
            self.assertTrue(fallback['id'].startswith('process-'))

    def test_broker_rejects_symlinked_and_broken_storage_directory(self):
        target = self.root / 'real-target'
        target.mkdir(mode=0o755)
        target.chmod(0o755)
        symlink_root = self.root / 'broker-link'
        symlink_root.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symbolic link'):
            Broker(symlink_root)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)
        self.assertFalse((target / 'resources.sqlite3').exists())
        missing = self.root / 'missing-target'
        broken_link = self.root / 'broken-link'
        broken_link.symlink_to(missing)
        with self.assertRaisesRegex(ValueError, 'symbolic link'):
            Broker(broken_link)
        self.assertFalse(missing.exists())

    def test_wait_propagates_base_exception_when_cancel_fails_and_respects_keep(self):
        a = self.request(0)
        self.start(a, self.actors[0])

        # keep=False: best-effort cancel runs and original KeyboardInterrupt propagates
        b = self.request(1, key='job-interrupt-cancel')
        interrupt = KeyboardInterrupt('stop waiting')
        with mock.patch('agent_sessions.resource_broker.time.sleep', side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt) as ctx:
                self.broker.wait(b['id'], self.actors[1], 10, keep=False)
        self.assertIs(ctx.exception, interrupt)
        self.assertEqual(self.broker.snapshot(b['id'], self.actors[1])['state'], 'cancelled')

        # keep=False: real cancel() ValueError must not mask KeyboardInterrupt
        c = self.request(1, key='job-interrupt-valueerror')
        interrupt_val = KeyboardInterrupt('stop with dead actor')
        def fail_after_owner_exits(_delay):
            self.births.pop(2)
            raise interrupt_val
        with mock.patch('agent_sessions.resource_broker.time.sleep', side_effect=fail_after_owner_exits):
            with self.assertRaises(KeyboardInterrupt) as ctx:
                self.broker.wait(c['id'], self.actors[1], 10, keep=False)
        self.assertIs(ctx.exception, interrupt_val)
        self.births[2] = 'second'
        self.assertEqual(self.broker.snapshot(c['id'], self.actors[1])['state'], 'queued')

        # keep=False: sqlite3.OperationalError during cancel() must not mask KeyboardInterrupt
        interrupt_db = KeyboardInterrupt('stop with db error')
        with mock.patch('agent_sessions.resource_broker.time.sleep', side_effect=interrupt_db), \
             mock.patch.object(self.broker, 'cancel', side_effect=sqlite3.OperationalError('disk I/O error')) as cancel_mock:
            with self.assertRaises(KeyboardInterrupt) as ctx:
                self.broker.wait(c['id'], self.actors[1], 10, keep=False)
        self.assertIs(ctx.exception, interrupt_db)
        cancel_mock.assert_called_once_with(c['id'], self.actors[1])

        # keep=True: cancel() is not called on exception and ticket stays queued
        interrupt_keep = KeyboardInterrupt('stop but keep')
        with mock.patch('agent_sessions.resource_broker.time.sleep', side_effect=interrupt_keep), \
             mock.patch.object(self.broker, 'cancel', wraps=self.broker.cancel) as keep_cancel_mock:
            with self.assertRaises(KeyboardInterrupt) as ctx:
                self.broker.wait(c['id'], self.actors[1], 10, keep=True)
        self.assertIs(ctx.exception, interrupt_keep)
        keep_cancel_mock.assert_not_called()
        self.assertEqual(self.broker.snapshot(c['id'], self.actors[1])['state'], 'queued')


class BrokerProcessTests(unittest.TestCase):
    def test_parallel_claims_and_cli_do_not_read_or_write_chat(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            broker = Broker(root)
            actor = current_actor('synthetic-owner', pid=os.getpid())
            tickets = [broker.request('fixture.unity', str(n), actor, 30) for n in range(3)]
            code = ('from agent_sessions.resource_broker import Broker; import json,sys; from pathlib import Path; '
                    'b=Broker(Path(sys.argv[1])); print(json.dumps(b.claim(sys.argv[2],json.loads(sys.argv[3]))))')
            py_env = {**os.environ, 'PYTHONPATH': str(ROOT / 'src') + os.pathsep + os.environ.get('PYTHONPATH', '')}
            def claim(ticket):
                result = subprocess.run([sys.executable, '-c', code, str(root), ticket['id'], json.dumps(actor)],
                    cwd=ROOT, env=py_env, text=True, capture_output=True, timeout=10, check=True)
                return json.loads(result.stdout)
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                results = list(pool.map(claim, reversed(tickets)))
            self.assertEqual([x['id'] for x in results if x['state']=='granted'], [tickets[0]['id']])
            env = {**py_env, 'AGENT_SESSIONS_DATA': str(root), 'CODEX_THREAD_ID': 'synthetic-owner'}
            result = subprocess.run([sys.executable, '-m', 'agent_sessions', 'resource', 'events'],
                                    env=env, text=True, capture_output=True, timeout=10, check=True)
            self.assertEqual(json.loads(result.stdout), [])
            self.assertFalse((root/'queue').exists())
            self.assertFalse((root/'cache.sqlite3').exists())


    def test_non_resource_commands_do_not_import_the_broker(self):
        code = ("import sys,agent_sessions;agent_sessions.main(['protocol']);"
                "assert 'agent_sessions.resource_broker' not in sys.modules")
        env = {**os.environ, 'PYTHONPATH': str(ROOT / 'src') + os.pathsep + os.environ.get('PYTHONPATH', '')}
        result = subprocess.run([sys.executable,'-c',code],cwd=ROOT,env=env,text=True,capture_output=True,timeout=10,check=False)
        self.assertEqual(result.returncode,0,result.stderr)
