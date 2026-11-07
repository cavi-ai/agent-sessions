"""Small synchronous resource transport and private compaction checkpoints."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from contextlib import contextmanager
from pathlib import Path

LIMIT = 65536


def encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)+'\n').encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def read_private(path):
    path = Path(path).absolute()
    if path != path.resolve():
        raise ValueError('Use a canonical checkpoint without symbolic links')
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > LIMIT:
            raise ValueError('Use a bounded checkpoint owned by the current user')
        raw = stream.read(LIMIT+1)
    if len(raw) > LIMIT:
        raise ValueError('Checkpoint exceeds 64 KiB')
    return json.loads(raw)


def write_private(path, value):
    path = Path(path).absolute()
    if path != path.resolve():
        raise ValueError('Use a canonical checkpoint without symbolic links')
    raw = encoded(value)
    if len(raw) > LIMIT:
        raise ValueError('Checkpoint exceeds 64 KiB')
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return hashlib.sha256(raw).hexdigest()


@contextmanager
def supervisor_lock(checkpoint):
    """One supervisor per attempt, including concurrent calls in the same process."""
    import fcntl

    path=Path(checkpoint).absolute().with_suffix('.supervisor.lock')
    if path!=path.resolve():
        raise ValueError('Supervisor lock cannot contain symbolic links')
    descriptor=os.open(path,os.O_CREAT|os.O_RDWR|getattr(os,'O_NOFOLLOW',0),0o600)
    try:
        info=os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_size:
            raise ValueError('Use an empty supervisor lock owned by the current user')
        try:
            fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError as failure:
            raise RuntimeError('This attempt already has a resident supervisor') from failure
        yield
    finally:
        os.close(descriptor)


class ResourceClient:
    """The supervising process owns this client; no messages or model callbacks."""

    def __init__(self, *, command=None, owner=None, environment=None):
        self.command = list(command or ['agent-sessions'])
        self.owner = owner
        self.environment = environment

    def call(self, action, *arguments, timeout=10):
        owner = ['--session', self.owner] if self.owner is not None else []
        result = subprocess.run([*self.command, 'resource', *owner, action, *map(str, arguments)],
            env=self.environment, capture_output=True, text=True, timeout=timeout, check=False)
        if result.returncode or len(result.stdout) > LIMIT:
            raise RuntimeError('Resource broker refused '+action+': '+result.stderr.strip()[:512])
        value = json.loads(result.stdout)
        if not isinstance(value, list if action=='events' else dict):
            raise TypeError('Invalid resource broker response')
        return value

    def request(self, resource, key, payload, *, seconds=3600, estimate_seconds=None):
        protocol = self.call('protocol')
        if not {'queue-advice','resumable-tickets','payload-idempotency','lifecycle-events','one-shot-start'} <= set(protocol.get('capabilities', [])):
            raise RuntimeError('Upgrade agent-sessions for resumable resource tickets')
        args = [resource, key, '--seconds', seconds, '--resumable', '--payload-sha256', digest(payload)]
        if estimate_seconds is not None:
            args.extend(['--estimate-seconds', estimate_seconds])
        return self.call('request', *args)

    def status(self, ticket):
        return self.call('status', ticket)

    def wait(self, ticket, *, seconds=60, keep=True):
        return self.call('wait', ticket, '--seconds', seconds, *(['--keep'] if keep else []), timeout=seconds+10)

    def start(self, grant):
        return self.call('start', grant['id'], '--lease', grant['lease'], '--fence', grant['fence'])

    def finish(self, grant, outcome, evidence):
        return self.call('finish', grant['id'], '--lease', grant['lease'], '--fence', grant['fence'],
                         '--outcome', outcome, '--evidence', evidence)

    def events(self, after=0):
        return self.call('events', '--lifecycle', '--after', after)

    @staticmethod
    def checkpoint(path, ticket, payload):
        if ticket.get('payload_sha256') != digest(payload):
            raise ValueError('Ticket payload differs from the checkpoint inputs')
        value = {'kind':'resource-checkpoint', 'version':1, 'ticket':ticket['id'],
                 'owner':ticket['owner'], 'resource':ticket['resource'], 'key':ticket['key'],
                 'payload_sha256':ticket['payload_sha256'], 'payload':payload}
        if Path(path).exists():
            if read_private(path) != value:
                raise ValueError('Checkpoint already belongs to a different request')
        else:
            write_private(path, value)
        return value

    def resume(self, path):
        handle = read_private(path)
        if (not isinstance(handle, dict) or handle.get('kind')!='resource-checkpoint' or handle.get('version')!=1
                or handle.get('payload_sha256') != digest(handle.get('payload'))):
            raise ValueError('Invalid resource checkpoint')
        # Explicit owner IDs retain a same-user recovery namespace, not OS authentication.
        if self.owner is not None and self.owner != handle['owner']:
            raise ValueError('Checkpoint belongs to a different logical owner')
        self.owner = handle['owner']
        state = self.status(handle['ticket'])
        if any(state.get(key)!=handle[key] for key in ('owner','resource','key','payload_sha256')):
            raise ValueError('Checkpoint no longer matches its authoritative ticket')
        return state, handle['payload']
