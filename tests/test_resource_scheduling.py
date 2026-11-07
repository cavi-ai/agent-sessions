"""Queue advice survives compaction without weakening active process ownership."""

import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from resource_broker import Broker

DIGEST = hashlib.sha256(b'fixed workload').hexdigest()


class SchedulingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.births = {1: 'a', 2: 'b', 3: 'c', 4: 'd'}
        self.now = 1000.0
        self.broker = Broker(self.root, identity=self.births.get, clock=lambda: self.now)

    def actor(self, pid, owner=None):
        return {'id': owner or 'session-'+str(pid), 'pid': pid, 'birth': self.births[pid]}

    def submit(self, pid, key, **args):
        return self.broker.request('shared.render', key, self.actor(pid), 600,
                                   estimate_seconds=60, **args)

    def start(self, ticket, actor):
        grant = self.broker.claim(ticket['id'], actor)
        return self.broker.running(ticket['id'], actor, grant['lease'], grant['fence'])

    def test_timestamp_position_distinct_sessions_and_eta_include_the_holder(self):
        first = self.submit(1, 'first')
        self.start(first, self.actor(1))
        self.now += 10
        self.submit(2, 'second')
        third = self.submit(3, 'third')
        self.assertEqual(third['created_at'], '1970-01-01T00:16:50+00:00')
        self.assertEqual((third['jobs_ahead'], third['sessions_ahead']), (2, 2))
        self.assertEqual(third['estimated_wait_seconds'], 110)
        self.assertEqual(third['estimated_start_at'], '1970-01-01T00:18:40+00:00')
        self.assertLessEqual(third['retry_after_seconds'], third['estimated_wait_seconds'])

    def test_compacted_queue_owner_can_exit_and_new_process_acquires_the_same_ticket(self):
        queued = self.submit(1, 'resume', resumable=True, payload_digest=DIGEST)
        self.births.pop(1)
        resumed = self.actor(4, 'session-1')
        same = self.broker.request('shared.render', 'resume', resumed, 600,
            estimate_seconds=60, resumable=True, payload_digest=DIGEST)
        self.assertEqual(same['id'], queued['id'])
        running = self.start(same, resumed)
        self.assertEqual(running['state'], 'running')
        with self.assertRaises(ValueError):
            self.broker.running(same['id'], self.actor(2, 'session-1'), running['lease'], running['fence'])
        self.births.pop(4)
        self.assertEqual(self.broker.snapshot(same['id'], self.actor(3))['state'], 'unresolved')

    def test_changed_payload_cannot_reuse_a_request_key(self):
        self.submit(1, 'same', resumable=True, payload_digest=DIGEST)
        with self.assertRaisesRegex(ValueError, 'different'):
            self.broker.request('shared.render', 'same', self.actor(1), 600,
                estimate_seconds=60, resumable=True, payload_digest='a'*64)

    def test_bounded_wait_can_preserve_a_queued_placeholder(self):
        self.start(self.submit(1, 'first'), self.actor(1))
        other = self.submit(2, 'later', resumable=True)
        state = self.broker.wait(other['id'], self.actor(2), .001, keep=True)
        self.assertEqual(state['state'], 'queued')
        self.assertEqual(self.broker.snapshot(other['id'], self.actor(2))['id'], other['id'])

    def test_sessions_ahead_are_distinct_and_missing_duration_is_not_fabricated(self):
        self.start(self.submit(1,'first'),self.actor(1))
        self.submit(1,'another-from-same-session')
        last=self.submit(3,'third')
        self.assertEqual((last['jobs_ahead'],last['sessions_ahead']),(2,1))
        unknown=self.broker.request('another.resource','unknown',self.actor(1),600)
        self.start(unknown,self.actor(1))
        next_ticket=self.broker.request('another.resource','after-unknown',self.actor(2),600)
        self.assertIsNone(next_ticket['estimated_wait_seconds'])
        self.assertEqual(next_ticket['estimate_reason'],'duration_unknown')

    def test_start_and_end_signals_are_atomic_idempotent_and_owner_scoped(self):
        first = self.submit(1, 'signals')
        running = self.start(first, self.actor(1))
        self.broker.running(first['id'], self.actor(1), running['lease'], running['fence'])
        self.now += 20
        for _ in range(2):
            self.broker.finish(first['id'], self.actor(1), running['lease'], running['fence'], 'completed', DIGEST)
        events = self.broker.events(self.actor(1), lifecycle=True)
        self.assertEqual([e['state'] for e in events], ['running', 'completed'])
        self.assertEqual(len(self.broker.events(self.actor(1))), 1)
        self.assertEqual(self.broker.events(self.actor(2), lifecycle=True), [])
        self.assertEqual(events[0]['fence'], events[1]['fence'])
        self.assertEqual(events[0]['created_at'], '1970-01-01T00:16:40+00:00')

    def test_history_is_used_but_overrun_and_unknown_holder_have_no_false_eta(self):
        first = self.submit(1, 'history')
        running = self.start(first, self.actor(1))
        self.now += 20
        self.broker.finish(first['id'], self.actor(1), running['lease'], running['fence'], 'completed', DIGEST)
        second = self.broker.request('shared.render', 'observed', self.actor(2), 600)
        second = self.start(second, self.actor(2))
        third = self.submit(3, 'next')
        self.assertEqual(third['estimated_wait_seconds'], 20)
        self.now += 21
        third = self.broker.snapshot(third['id'], self.actor(3))
        self.assertIsNone(third['estimated_wait_seconds'])
        self.assertEqual(third['estimate_reason'], 'holder_overrun')
        self.births.pop(2)
        self.assertEqual(self.broker.snapshot(third['id'], self.actor(3))['estimate_reason'], 'unresolved_holder')

    def test_migration_preserves_existing_running_lease_without_invented_timestamps(self):
        legacy = self.root/'legacy'
        legacy.mkdir()
        with sqlite3.connect(legacy/'resources.sqlite3') as db:
            db.executescript('''CREATE TABLE tickets (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
                resource TEXT NOT NULL, key TEXT NOT NULL, owner TEXT NOT NULL,
                pid INTEGER NOT NULL, birth TEXT NOT NULL, seconds REAL NOT NULL,
                deadline REAL NOT NULL, state TEXT NOT NULL, lease TEXT, fence INTEGER,
                reason TEXT, evidence TEXT, UNIQUE(owner,key));''')
            db.execute('INSERT INTO tickets(id,resource,key,owner,pid,birth,seconds,deadline,state,lease,fence) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                ('legacy', 'shared.render', 'existing', 'session-1', 1, 'a', 600, 1600, 'running', 'b'*64, 9))
        again = Broker(legacy, identity=self.births.get, clock=lambda: self.now)
        state = again.snapshot('legacy', self.actor(1))
        self.assertEqual((state['lease'], state['fence'], state['state']), ('b'*64,9,'running'))
        self.assertIsNone(state['created_at'])
        self.assertIsNone(state['started_at'])
        again.finish('legacy',self.actor(1),'b'*64,9,'completed',DIGEST)
        next_ticket=again.request('shared.render','next',self.actor(2),600)
        self.assertGreater(again.claim(next_ticket['id'],self.actor(2))['fence'],9)
        with sqlite3.connect(legacy/'resources.sqlite3') as db:
            self.assertEqual(db.execute("SELECT count(*) FROM tickets WHERE id='legacy'").fetchone()[0], 1)
