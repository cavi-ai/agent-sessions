"""Real CLI checkpoints and the fixed subprocess example survive requester exit."""

import concurrent.futures
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / 'src') not in sys.path:
    sys.path.insert(0, str(ROOT / 'src'))

import examples.shared_resource as shared_resource
from agent_sessions.resource_client import (
    ResourceClient,
    read_private,
    read_private_bytes,
    supervisor_lock,
    write_private,
)
from examples.shared_resource import run, submit, supervised_run


class ResourceClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.env = {
            **os.environ,
            'AGENT_SESSIONS_DATA': str(self.root / 'data'),
            'PYTHONPATH': str(ROOT / 'src') + os.pathsep + os.environ.get('PYTHONPATH', ''),
        }
        self.command = [sys.executable, '-m', 'agent_sessions']
        self.client = ResourceClient(command=self.command, owner='fixture-owner', environment=self.env)
        self.handle = self.root/'run.json'

    def test_checkpoint_is_private_payload_bound_and_completed_work_does_not_relaunch(self):
        ticket = submit(self.client, self.handle, 'fixed-attempt', .01)
        self.assertEqual(ticket['state'], 'queued')
        self.assertNotIn('lease', read_private(self.handle))
        self.assertEqual(self.handle.stat().st_mode & 0o777, 0o600)
        result = run(self.client, self.handle)
        self.assertEqual(result['state'], 'completed')
        original = self.handle.with_suffix('.receipt.json').read_bytes()
        self.assertEqual(run(self.client,self.handle)['state'],'completed')
        self.assertEqual(self.handle.with_suffix('.receipt.json').read_bytes(), original)
        events = self.client.events()
        self.assertEqual([e['state'] for e in events], ['running','completed'])
        self.assertEqual(self.client.events(events[-1]['cursor']), [])
        self.assertFalse((self.root/'data/queue').exists())

    def test_separate_requester_process_exits_then_supervisor_resumes_the_same_place(self):
        code = '''import sys
from agent_sessions.resource_client import ResourceClient
from examples.shared_resource import submit
c=ResourceClient(command=[sys.executable,'-m','agent_sessions'],owner='fixture-owner')
submit(c,sys.argv[1],'compaction-attempt',.01)
'''
        result = subprocess.run([sys.executable,'-c',code,str(self.handle)],
            env=self.env,cwd=ROOT,text=True,capture_output=True,timeout=10,check=False)
        self.assertEqual(result.returncode,0,result.stderr)
        before,_payload = self.client.resume(self.handle)
        self.assertEqual(before['state'],'queued')
        self.assertEqual(run(self.client,self.handle)['id'], before['id'])
        self.assertEqual(self.client.status(before['id'])['state'],'completed')

    def test_checkpoint_substitution_is_rejected(self):
        submit(self.client, self.handle, 'fixed-attempt', .01)
        value=read_private(self.handle)
        value['payload']['seconds']=5
        self.handle.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError,'checkpoint'):
            self.client.resume(self.handle)

    def test_same_process_concurrent_supervisor_cannot_launch_the_same_attempt(self):
        submit(self.client,self.handle,'concurrent-attempt',.01)
        with supervisor_lock(self.handle), concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(run,self.client,self.handle)
            with self.assertRaisesRegex(RuntimeError,'resident supervisor'):
                future.result(timeout=5)
        self.assertEqual(self.client.events(),[])
        self.assertEqual(run(self.client,self.handle)['state'],'completed')

    def test_two_checkpoint_paths_for_one_ticket_still_allow_only_one_launch(self):
        other=self.root/'alias.json'
        first=submit(self.client,self.handle,'aliased-attempt',.01)
        second=submit(self.client,other,'aliased-attempt',.01)
        self.assertEqual(first['id'],second['id'])
        barrier=threading.Barrier(2)
        class Contender(ResourceClient):
            def start(inner,grant):
                barrier.wait(timeout=10)
                return super().start(grant)
        clients=[Contender(command=self.command,owner='fixture-owner',environment=self.env) for _ in range(2)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(run,client,path) for client,path in zip(clients,(self.handle,other))]
            results=[]
            for future in futures:
                try:
                    results.append(future.result(timeout=20)['state'])
                except RuntimeError as error:
                    self.assertIn('launch owner',str(error))
                    results.append('refused')
        self.assertCountEqual(results,['completed','refused'])
        self.assertEqual(sum(p.with_suffix('.receipt.json').exists() for p in (self.handle,other)),1)
        self.assertEqual([event['state'] for event in self.client.events()],['running','completed'])

    def test_finish_unresolved_without_evidence_cancel_and_failed_resume_preserves_owner(self):
        ticket = self.client.request('example.heavy', 'cancel-me', {'step': 1}, seconds=60)
        cancelled = self.client.cancel(ticket['id'])
        self.assertEqual(cancelled['state'], 'cancelled')

        second = self.client.request('example.heavy', 'unresolved-me', {'step': 2}, seconds=60)
        grant = self.client.wait(second['id'], seconds=5)
        self.client.start(grant)
        unresolved = self.client.finish(grant, 'unresolved')
        self.assertEqual(unresolved['state'], 'unresolved')

        bad_handle = self.root / 'missing-ticket.json'
        self.client.checkpoint(bad_handle, {**second, 'id': '0' * 32}, {'step': 2})
        anon = ResourceClient(command=self.command, environment=self.env)
        with self.assertRaises(RuntimeError):
            anon.resume(bad_handle)
        self.assertIsNone(anon.owner)

    def test_receipt_recovery_reads_once_and_rejects_symlinks_and_loops(self):
        ticket = submit(self.client, self.handle, 'receipt-recovery', .01)
        grant = self.client.wait(ticket['id'], seconds=5)
        self.client.start(grant)
        receipt_path = self.handle.with_suffix('.receipt.json')
        proof = {
            'ticket': grant['id'],
            'fence': grant['fence'],
            'payload_sha256': grant['payload_sha256'],
            'exit_code': 0,
            'cleanup_error': None,
        }

        # Live symlink receipt must be rejected.
        real_target = self.root / 'real-target.receipt.json'
        write_private(real_target, proof)
        receipt_path.symlink_to(real_target)
        with self.assertRaisesRegex(ValueError, 'symbolic links'):
            read_private_bytes(receipt_path)
        with self.assertRaisesRegex(ValueError, 'symbolic links'):
            supervised_run(self.client, self.handle)
        receipt_path.unlink()

        # Dangling symlink receipt must fail closed with ValueError, not be treated as absent.
        receipt_path.symlink_to(self.root / 'missing-target.receipt.json')
        with self.assertRaisesRegex(ValueError, 'symbolic links'):
            read_private_bytes(receipt_path)
        with self.assertRaisesRegex(ValueError, 'symbolic links'):
            supervised_run(self.client, self.handle)
        receipt_path.unlink()

        # Self-referencing symlink loop must raise ValueError, not RuntimeError.
        receipt_path.symlink_to(receipt_path)
        with self.assertRaisesRegex(ValueError, 'symbolic links'):
            read_private_bytes(receipt_path)
        with self.assertRaisesRegex(ValueError, 'symbolic links'):
            supervised_run(self.client, self.handle)
        receipt_path.unlink()

        # Valid pre-existing receipt is read once and hashed from those exact bytes without Path.read_bytes.
        expected_digest = write_private(receipt_path, proof)
        decoy = self.root / 'decoy.receipt.json'
        decoy.write_text('{"tampered": true}\n', encoding='utf-8')
        read_calls = []
        recorded_evidence = []
        original_read_bytes = Path.read_bytes

        def guarded_read_bytes(path_obj):
            if path_obj.resolve(strict=False) == receipt_path:
                raise AssertionError('supervised_run must not re-open receipt via Path.read_bytes')
            return original_read_bytes(path_obj)

        def single_read_and_swap(path_obj):
            raw = read_private_bytes(path_obj)
            read_calls.append(Path(path_obj))
            # Swap receipt on disk immediately after the single validated read to prove TOCTOU immunity.
            receipt_path.unlink()
            receipt_path.symlink_to(decoy)
            return raw

        class RecordingClient(ResourceClient):
            def finish(inner, finish_grant, outcome, evidence=None):
                recorded_evidence.append((outcome, evidence))
                return super().finish(finish_grant, outcome, evidence)

        recording_client = RecordingClient(
            command=self.command, owner='fixture-owner', environment=self.env
        )
        with (
            mock.patch.object(Path, 'read_bytes', autospec=True, side_effect=guarded_read_bytes),
            mock.patch.object(shared_resource, 'read_private_bytes', side_effect=single_read_and_swap),
        ):
            result = supervised_run(recording_client, self.handle)

        self.assertEqual(result['state'], 'completed')
        self.assertEqual(read_calls, [receipt_path])
        self.assertEqual(recorded_evidence, [('completed', expected_digest)])
        # Confirm the broker recorded the exact original receipt digest, not the post-read decoy.
        self.assertEqual(self.client.finish(grant, 'completed', expected_digest)['state'], 'completed')
        with self.assertRaisesRegex(RuntimeError, 'different terminal result'):
            self.client.finish(grant, 'completed', hashlib.sha256(decoy.read_bytes()).hexdigest())

    def test_leading_dash_inputs_fail_closed_in_broker_subprocess(self):
        for bad in ('--help', '-bad'):
            bad_owner = ResourceClient(command=self.command, owner=bad, environment=self.env)
            with self.assertRaisesRegex(RuntimeError, r'^Resource broker refused request:') as ctx:
                bad_owner.request('example.heavy', 'valid-key', {'step': 1})
            self.assertNotIn('usage:', str(ctx.exception))

            with self.assertRaisesRegex(RuntimeError, r'^Resource broker refused status:') as ctx:
                bad_owner.status('0' * 32)
            self.assertNotIn('usage:', str(ctx.exception))

            with self.assertRaisesRegex(RuntimeError, r'^Resource broker refused events:') as ctx:
                bad_owner.events()
            self.assertNotIn('usage:', str(ctx.exception))

            with self.assertRaisesRegex(RuntimeError, r'^Resource broker refused request:') as ctx:
                self.client.request(bad, 'valid-key', {'step': 1})
            self.assertNotIn('usage:', str(ctx.exception))

            with self.assertRaisesRegex(RuntimeError, r'^Resource broker refused request:') as ctx:
                self.client.request('example.heavy', bad, {'step': 1})
            self.assertNotIn('usage:', str(ctx.exception))

            with self.assertRaisesRegex(RuntimeError, r'^Resource broker refused status:') as ctx:
                self.client.status(bad)
            self.assertNotIn('usage:', str(ctx.exception))

            with self.assertRaisesRegex(RuntimeError, r'^Resource broker refused cancel:') as ctx:
                self.client.cancel(bad)
            self.assertNotIn('usage:', str(ctx.exception))

            with self.assertRaisesRegex(RuntimeError, r'^Resource broker refused wait:') as ctx:
                self.client.wait(bad, seconds=1)
            self.assertNotIn('usage:', str(ctx.exception))

            bad_grant = {'id': bad, 'lease': '0' * 32, 'fence': 1}
            with self.assertRaisesRegex(RuntimeError, r'^Resource broker refused start:') as ctx:
                self.client.start(bad_grant)
            self.assertNotIn('usage:', str(ctx.exception))

            with self.assertRaisesRegex(RuntimeError, r'^Resource broker refused finish:') as ctx:
                self.client.finish(bad_grant, 'completed', hashlib.sha256(b'ok').hexdigest())
            self.assertNotIn('usage:', str(ctx.exception))

        with self.assertRaisesRegex(RuntimeError, r'^Resource broker refused start:') as ctx:
            self.client.start({'id': '0' * 32, 'lease': '--help', 'fence': 1})
        self.assertNotIn('usage:', str(ctx.exception))

        with self.assertRaisesRegex(RuntimeError, r'^Resource broker refused finish:') as ctx:
            self.client.finish({'id': '0' * 32, 'lease': '0' * 32, 'fence': 1}, 'completed', '--help')
        self.assertNotIn('usage:', str(ctx.exception))
