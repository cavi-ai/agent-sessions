#!/usr/bin/env python3
"""Example adapter: one fixed Python workload, durable admission, no peer messages.

Replace the fixed workload and its cleanup proof with a project's trusted native
supervisor. This example intentionally accepts no shell or application command.
"""

import argparse
import hashlib
import json
import math
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from resource_client import ResourceClient, read_private, supervisor_lock, write_private


def submit(client, handle, key, seconds):
    if not math.isfinite(seconds) or not .001 <= seconds <= 60:
        raise ValueError('The example workload must last 0.001 to 60 seconds')
    payload = {'kind':'fixed-python-sleep', 'seconds':seconds}
    state = client.request('example.heavy', key, payload, estimate_seconds=seconds)
    client.checkpoint(handle, state, payload)
    return {key:value for key,value in state.items() if key!='lease'}


def run(client, handle):
    with supervisor_lock(handle):
        return supervised_run(client,handle)


def supervised_run(client, handle):
    state, payload = client.resume(handle)
    receipt_path = Path(handle).with_suffix('.receipt.json')
    if receipt_path.exists():
        proof = read_private(receipt_path)
        if (proof.get('ticket') != state['id'] or proof.get('fence') != state['fence']
                or proof.get('payload_sha256') != state['payload_sha256']
                or proof.get('cleanup_error') is not None
                or not (type(proof.get('exit_code')) is int or proof.get('launch')=='not-started')):
            raise ValueError('Existing proof does not establish this fixed workload exit')
        outcome = 'completed' if proof['exit_code']==0 and not proof.get('error') else 'failed'
        return client.finish(state, outcome, hashlib.sha256(receipt_path.read_bytes()).hexdigest())
    if state['state'] not in ('queued','granted'):
        raise ValueError('Existing attempt cannot be replayed; reconcile its supervisor proof')
    state = client.wait(state['id'], seconds=60)
    if state['state'] != 'granted':
        return state
    if payload.get('kind')!='fixed-python-sleep' or not .001 <= payload.get('seconds', 0) <= 60:
        raise ValueError('Invalid fixed example workload')
    signal=client.start(state)  # A replay acknowledges intent but cannot authorize a second launch.
    if signal.get('launch_permitted') is not True:
        raise RuntimeError('This ticket already has a launch owner; reconcile instead of replaying')
    child = None
    error = None
    try:
        child = subprocess.Popen([sys.executable, '-I', '-c', 'import time,sys;time.sleep(float(sys.argv[1]))', str(payload['seconds'])])
        child.wait(timeout=65)
    except (OSError, subprocess.SubprocessError, KeyboardInterrupt) as failure:
        error = failure
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.wait(timeout=5)
    proof = {'ticket':state['id'], 'fence':state['fence'], 'payload_sha256':state['payload_sha256'],
             'exit_code':child.returncode if child is not None else None, 'cleanup_error':None}
    if child is None:
        proof['launch']='not-started'
    if error is not None:
        proof['error']=str(error)
    evidence = write_private(receipt_path, proof)
    result=client.finish(state, 'completed' if proof['exit_code']==0 and error is None else 'failed', evidence)
    if error is not None:
        raise error
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('submit','status','run'))
    parser.add_argument('--handle', required=True, type=Path)
    parser.add_argument('--key')
    parser.add_argument('--work-seconds', type=float, default=1)
    args = parser.parse_args()
    client = ResourceClient()
    if args.action == 'submit':
        if not args.key:
            parser.error('submit requires a stable --key for this attempt')
        result = submit(client, args.handle, args.key, args.work_seconds)
    elif args.action == 'status':
        result, _payload = client.resume(args.handle)
    else:
        result = run(client, args.handle)
    print(json.dumps({key:value for key,value in result.items() if key!='lease'}))


if __name__ == '__main__':
    main()
