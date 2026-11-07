"""Real CLI checkpoints and the fixed subprocess example survive requester exit."""

import concurrent.futures
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from examples.shared_resource import run, submit
from resource_client import ResourceClient, read_private, supervisor_lock

ROOT = Path(__file__).resolve().parents[1]


class ResourceClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.env = {**os.environ, 'AGENT_SESSIONS_DATA':str(self.root/'data')}
        self.command = [sys.executable, str(ROOT/'agent_sessions.py')]
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
from resource_client import ResourceClient
from examples.shared_resource import submit
c=ResourceClient(command=[sys.executable,sys.argv[1]],owner='fixture-owner')
submit(c,sys.argv[2],'compaction-attempt',.01)
'''
        result = subprocess.run([sys.executable,'-c',code,str(ROOT/'agent_sessions.py'),str(self.handle)],
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
