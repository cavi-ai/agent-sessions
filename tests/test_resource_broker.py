"""Resource admission is durable control state, independent of peer messages."""

import concurrent.futures
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from resource_broker import Broker, current_actor

ROOT = Path(__file__).resolve().parents[1]
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


class BrokerProcessTests(unittest.TestCase):
    def test_parallel_claims_and_cli_do_not_read_or_write_chat(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            broker = Broker(root)
            actor = current_actor('synthetic-owner', pid=os.getpid())
            tickets = [broker.request('fixture.unity', str(n), actor, 30) for n in range(3)]
            code = ('from resource_broker import Broker; import json,sys; from pathlib import Path; '
                    'b=Broker(Path(sys.argv[1])); print(json.dumps(b.claim(sys.argv[2],json.loads(sys.argv[3]))))')
            def claim(ticket):
                result = subprocess.run([sys.executable, '-c', code, str(root), ticket['id'], json.dumps(actor)],
                    cwd=ROOT, text=True, capture_output=True, timeout=10, check=True)
                return json.loads(result.stdout)
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                results = list(pool.map(claim, reversed(tickets)))
            self.assertEqual([x['id'] for x in results if x['state']=='granted'], [tickets[0]['id']])
            env = {**os.environ, 'AGENT_SESSIONS_DATA': str(root), 'CODEX_THREAD_ID': 'synthetic-owner'}
            result = subprocess.run([sys.executable, str(ROOT/'agent_sessions.py'), 'resource', 'events'],
                                    env=env, text=True, capture_output=True, timeout=10, check=True)
            self.assertEqual(json.loads(result.stdout), [])
            self.assertFalse((root/'queue').exists())
            self.assertFalse((root/'cache.sqlite3').exists())


    def test_non_resource_commands_do_not_import_the_broker(self):
        code = "import sys,agent_sessions;agent_sessions.main(['protocol']);assert 'resource_broker' not in sys.modules"
        result = subprocess.run([sys.executable,'-c',code],cwd=ROOT,text=True,capture_output=True,timeout=10,check=False)
        self.assertEqual(result.returncode,0,result.stderr)
