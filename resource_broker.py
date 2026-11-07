"""Host-local FIFO admission. Control state never enters the peer-message queue."""

from __future__ import annotations

import ctypes
import errno
import functools
import hashlib
import math
import os
import re
import secrets
import sqlite3
import statistics
import struct
import subprocess
import sys
import time
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path

NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}')
DIGEST = re.compile(r'[0-9a-f]{64}')
TERMINAL = ('completed', 'failed', 'cancelled', 'expired')
ACTIVE = ('granted', 'running', 'unresolved')


@functools.lru_cache(maxsize=1)
def _darwin_processes():
    library = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
    library.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
    library.proc_pidinfo.restype = ctypes.c_int
    return library


@functools.lru_cache(maxsize=1)
def _boot_identity():
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def process_identity(pid):
    if sys.platform == 'darwin':
        # Public proc_info.h: PROC_PIDTBSDINFO=3, proc_bsdinfo=136 bytes on LP64.
        data = (ctypes.c_ubyte * 136)()
        ctypes.set_errno(0)
        count = _darwin_processes().proc_pidinfo(pid, 3, 0, ctypes.byref(data), 136)
        error = ctypes.get_errno()
        if count == 0 and error == errno.ESRCH:
            return None
        if count != 136 or struct.unpack_from('=I', bytes(data), 12)[0] != pid:
            raise OSError(error or errno.EIO, 'Cannot establish broker owner identity')
        seconds, micros = struct.unpack_from('=QQ', bytes(data), 120)
        return 'darwin:'+str(seconds)+':'+str(micros)
    if sys.platform.startswith('linux'):
        try:
            text = Path('/proc/'+str(pid)+'/stat').read_text()
        except FileNotFoundError:
            return None
        fields = text.rpartition(')')[2].split()
        if len(fields) < 20:
            raise OSError('Incomplete broker owner identity')
        return 'linux:'+_boot_identity()+':'+fields[19]
    try:
        result = subprocess.run(['ps', '-p', str(pid), '-o', 'lstart='], capture_output=True,
                                text=True, timeout=2, check=False,
                                env={**os.environ, 'LC_ALL': 'C', 'TZ': 'UTC'})
    except subprocess.SubprocessError as exc:
        raise OSError('Cannot establish broker owner identity') from exc
    if result.returncode not in (0, 1) or result.stderr.strip():
        raise OSError('Cannot establish broker owner identity')
    return result.stdout.strip() or None


def current_actor(session=None, *, pid=None):
    pid = os.getppid() if pid is None else pid
    birth = process_identity(pid)
    if birth is None:
        raise ValueError('The resource owner process has exited')
    owner = session or os.environ.get('CODEX_THREAD_ID') or os.environ.get('CLAUDE_CODE_SESSION_ID')
    return {'id': owner or 'process-'+hashlib.sha256((str(pid)+birth).encode()).hexdigest(),
            'pid': pid, 'birth': birth}


def bounded_seconds(value, maximum=3600):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not .001 <= value <= maximum:
        raise ValueError('Wait must be between 0.001 and '+str(maximum)+' seconds')
    return float(value)


def timestamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value is not None else None


def actor_fields(actor):
    if (not isinstance(actor, dict) or set(actor) != {'id', 'pid', 'birth'}
            or not isinstance(actor['id'], str) or not NAME.fullmatch(actor['id'])
            or type(actor['pid']) is not int or actor['pid'] <= 0
            or not isinstance(actor['birth'], str) or not 1 <= len(actor['birth']) <= 128):
        raise ValueError('A resource owner requires a session and process birth identity')
    return actor


class Broker:
    def __init__(self, directory, *, identity=process_identity, clock=time.time):
        self.root = Path(directory)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / 'resources.sqlite3'
        for suffix in ('', '-wal', '-shm'):
            if Path(str(self.path)+suffix).is_symlink():
                raise ValueError('Broker storage cannot be a symbolic link')
        self.identity, self.clock = identity, clock
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            db.execute('PRAGMA synchronous=FULL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS tickets (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
                    resource TEXT NOT NULL, key TEXT NOT NULL, owner TEXT NOT NULL,
                    pid INTEGER NOT NULL, birth TEXT NOT NULL, seconds REAL NOT NULL,
                    deadline REAL NOT NULL, state TEXT NOT NULL, lease TEXT, fence INTEGER,
                    reason TEXT, evidence TEXT, UNIQUE(owner,key));
                CREATE UNIQUE INDEX IF NOT EXISTS one_resource_holder ON tickets(resource)
                    WHERE state IN ('granted','running','unresolved');
                CREATE INDEX IF NOT EXISTS ticket_state ON tickets(state,resource,seq);
                CREATE TABLE IF NOT EXISTS generations (resource TEXT PRIMARY KEY, fence INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    cursor INTEGER PRIMARY KEY AUTOINCREMENT, ticket TEXT NOT NULL,
                    owner TEXT NOT NULL, state TEXT NOT NULL, reason TEXT,
                    UNIQUE(ticket,state));
                CREATE INDEX IF NOT EXISTS owner_events ON events(owner,cursor);
            ''')
            # Serialize additive migration with other constructors and active claims.
            # Existing rows keep NULL times: their original timestamps are unknown.
            db.execute('BEGIN IMMEDIATE')
            migrated = False
            for table, fields in {
                'tickets': {'created': 'REAL', 'granted': 'REAL', 'started': 'REAL',
                            'ended': 'REAL', 'estimate': 'REAL', 'resumable': 'INTEGER DEFAULT 0',
                            'payload': 'TEXT'},
                'events': {'created': 'REAL', 'fence': 'INTEGER'},
            }.items():
                existing = {r[1] for r in db.execute('PRAGMA table_info('+table+')')}
                for name, kind in fields.items():
                    if name not in existing:
                        migrated = True
                        db.execute('ALTER TABLE '+table+' ADD COLUMN '+name+' '+kind)
            db.execute('CREATE INDEX IF NOT EXISTS resource_history ON tickets(resource,seq DESC)')
            if migrated:
                for resource, fence in db.execute('SELECT resource,max(fence) FROM tickets WHERE fence IS NOT NULL GROUP BY resource').fetchall():
                    db.execute('INSERT INTO generations VALUES(?,?) ON CONFLICT(resource) DO UPDATE SET fence=max(fence,excluded.fence)', (resource,fence))
            db.commit()
        self.path.chmod(0o600)

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA synchronous=FULL')
        try:
            db.execute('BEGIN IMMEDIATE')
            self.sweep(db)
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def transition(self, db, row, state, reason=None, evidence=None):
        now = self.clock()
        db.execute('UPDATE tickets SET state=?,reason=?,evidence=? WHERE id=?',
                   (state, reason, evidence, row['id']))
        if state == 'running':
            db.execute('UPDATE tickets SET started=? WHERE id=?', (now, row['id']))
        elif state in TERMINAL:
            db.execute('UPDATE tickets SET ended=? WHERE id=?', (now, row['id']))
        if state in (*TERMINAL, 'unresolved', 'running'):
            db.execute('INSERT OR IGNORE INTO events(ticket,owner,state,reason,created,fence) VALUES(?,?,?,?,?,?)',
                       (row['id'], row['owner'], state, reason, now, row['fence']))

    def sweep(self, db):
        for row in db.execute("SELECT * FROM tickets WHERE state='queued' AND deadline<=?", (self.clock(),)).fetchall():
            self.transition(db, row, 'expired', 'queue_deadline')
        identities = {}
        def alive(row):
            pid = row['pid']
            if pid not in identities:
                identities[pid] = self.identity(pid)
            return identities[pid] == row['birth']
        for row in db.execute("SELECT * FROM tickets WHERE state IN ('granted','running')").fetchall():
            if not alive(row):
                self.transition(db, row, 'unresolved' if row['state']=='running' else 'cancelled', 'owner_exited')
        # Only a free resource's head can be admitted. Do not scan every waiting PID.
        resources = [r[0] for r in db.execute("SELECT DISTINCT resource FROM tickets WHERE state='queued'")]
        for resource in resources:
            if db.execute("SELECT 1 FROM tickets WHERE resource=? AND state IN ('granted','running','unresolved')", (resource,)).fetchone():
                continue
            while True:
                row = db.execute("SELECT * FROM tickets WHERE resource=? AND state='queued' ORDER BY seq LIMIT 1", (resource,)).fetchone()
                if row is None or row['resumable'] or alive(row):
                    break
                self.transition(db, row, 'cancelled', 'owner_exited')

    def row(self, db, ticket):
        row = db.execute('SELECT * FROM tickets WHERE id=?', (ticket,)).fetchone()
        if row is None:
            raise ValueError('Unknown resource ticket')
        return row

    def authorize(self, row, actor, lease=None, fence=None, *, process=False):
        actor_fields(actor)
        if row['owner'] != actor['id']:
            raise ValueError('Resource ticket belongs to another owner')
        if process and (row['pid'] != actor['pid'] or row['birth'] != actor['birth']
                        or self.identity(actor['pid']) != actor['birth']):
            raise ValueError('Resource owner process changed')
        if lease is not None and (not secrets.compare_digest(row['lease'] or '', lease) or row['fence'] != fence):
            raise ValueError('Resource lease or generation changed')

    def view(self, db, row, actor):
        result = {key: row[key] for key in ('id', 'resource', 'key', 'owner', 'state', 'reason', 'fence')}
        result['position'] = db.execute("SELECT count(*) FROM tickets WHERE resource=? AND state='queued' AND seq<=?",
                                        (row['resource'], row['seq'])).fetchone()[0] if row['state']=='queued' else 0
        if row['owner'] == actor['id'] and row['lease']:
            result['lease'] = row['lease']
        result.update({name+'_at': timestamp(row[column]) for name, column in
                       (('created','created'), ('granted','granted'), ('started','started'), ('ended','ended'))})
        result.update(expires_at=timestamp(row['deadline']), queue_sequence=row['seq'],
                      resumable=bool(row['resumable']), payload_sha256=row['payload'])
        result.update(self.advice(db, row))
        return result

    def advice(self, db, row):
        ahead = db.execute("SELECT * FROM tickets WHERE resource=? AND id!=? AND "
            "(state IN ('granted','running','unresolved') OR (state='queued' AND seq<?)) ORDER BY seq",
            (row['resource'], row['id'], row['seq'])).fetchall() if row['state']=='queued' else []
        history = [r[0] for r in db.execute("SELECT ended-started FROM tickets WHERE resource=? "
            "AND state IN ('completed','failed') AND evidence IS NOT NULL AND ended>started "
            "ORDER BY seq DESC LIMIT 20", (row['resource'],))]
        median = statistics.median(history) if history else None
        wait, reason = 0.0, None
        basis = set()
        for item in ahead:
            duration = item['estimate'] if item['estimate'] is not None else median
            if item['state'] == 'unresolved':
                reason = 'unresolved_holder'
                break
            if duration is None or (item['state']=='running' and item['started'] is None):
                reason = 'duration_unknown'
                break
            basis.add('declared' if item['estimate'] is not None else 'recent_median')
            if item['state'] == 'running':
                duration -= max(0, self.clock()-item['started'])
                if duration <= 0:
                    reason = 'holder_overrun'
                    break
            wait += duration
        wait = None if reason else round(wait, 3)
        remaining = max(0, row['deadline']-self.clock())
        retry = min(60 if wait is None else wait, remaining, 300) if row['state']=='queued' else 0
        return {'jobs_ahead': len(ahead), 'sessions_ahead': len({r['owner'] for r in ahead}),
                'estimated_wait_seconds': wait, 'estimated_start_at': timestamp(self.clock()+wait) if wait is not None and row['state']=='queued' else None,
                'estimate_reason': reason, 'estimate_basis': sorted(basis), 'history_samples': len(history),
                'retry_after_seconds': round(max(0, retry), 3), 'observed_at': timestamp(self.clock())}

    def request(self, resource, key, actor, seconds=600, *, estimate_seconds=None, resumable=False, payload_digest=None):
        actor_fields(actor)
        seconds = bounded_seconds(seconds, 86400)
        if estimate_seconds is not None:
            estimate_seconds = bounded_seconds(estimate_seconds, 86400)
        if type(resumable) is not bool or (payload_digest is not None and
                (not isinstance(payload_digest, str) or not DIGEST.fullmatch(payload_digest))):
            raise ValueError('Use a boolean resume flag and a SHA256 payload digest')
        if not isinstance(resource, str) or not NAME.fullmatch(resource) or not isinstance(key, str) or not NAME.fullmatch(key):
            raise ValueError('Resource and request key must be bounded identifiers')
        with self.transaction() as db:
            old = db.execute('SELECT * FROM tickets WHERE owner=? AND key=?', (actor['id'], key)).fetchone()
            if old is not None:
                if ((old['resource'], old['seconds'], old['estimate'], bool(old['resumable']), old['payload']) !=
                        (resource, seconds, estimate_seconds, resumable, payload_digest)
                        or (not resumable and (old['pid'], old['birth']) != (actor['pid'], actor['birth']))):
                    raise ValueError('Request key already has different resource, deadline, payload or process inputs')
                return self.view(db, old, actor)
            if self.identity(actor['pid']) != actor['birth']:
                raise ValueError('Resource owner process changed')
            if db.execute("SELECT count(*) FROM tickets WHERE state='queued'").fetchone()[0] >= 256:
                raise ValueError('Resource queue is full')
            ticket = uuid.uuid4().hex
            now = self.clock()
            db.execute('INSERT INTO tickets(id,resource,key,owner,pid,birth,seconds,deadline,state,created,estimate,resumable,payload) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (ticket, resource, key, actor['id'], actor['pid'], actor['birth'], seconds, now+seconds, 'queued', now, estimate_seconds, int(resumable), payload_digest))
            return self.view(db, self.row(db, ticket), actor)

    def snapshot(self, ticket, actor):
        actor_fields(actor)
        with self.transaction() as db:
            return self.view(db, self.row(db, ticket), actor)

    def claim(self, ticket, actor):
        with self.transaction() as db:
            row = self.row(db, ticket)
            self.authorize(row, actor, process=not bool(row['resumable']))
            if row['state'] != 'queued':
                return self.view(db, row, actor)
            holder = db.execute("SELECT id FROM tickets WHERE resource=? AND state IN ('granted','running','unresolved')", (row['resource'],)).fetchone()
            first = db.execute("SELECT id FROM tickets WHERE resource=? AND state='queued' ORDER BY seq LIMIT 1", (row['resource'],)).fetchone()
            if holder is None and first['id'] == ticket:
                if self.identity(actor['pid']) != actor['birth']:
                    raise ValueError('Resource owner process changed')
                db.execute('INSERT OR IGNORE INTO generations VALUES(?,0)', (row['resource'],))
                db.execute('UPDATE generations SET fence=fence+1 WHERE resource=?', (row['resource'],))
                fence = db.execute('SELECT fence FROM generations WHERE resource=?', (row['resource'],)).fetchone()[0]
                db.execute("UPDATE tickets SET state='granted',lease=?,fence=?,pid=?,birth=?,granted=? WHERE id=?",
                           (secrets.token_hex(32), fence, actor['pid'], actor['birth'], self.clock(), ticket))
            return self.view(db, self.row(db, ticket), actor)

    def check_lease(self, lease, fence):
        if not isinstance(lease, str) or not DIGEST.fullmatch(lease) or type(fence) is not int or fence < 1:
            raise ValueError('A resource lease and generation are required')

    def running(self, ticket, actor, lease, fence):
        self.check_lease(lease, fence)
        with self.transaction() as db:
            row = self.row(db, ticket)
            self.authorize(row, actor, lease, fence, process=True)
            if row['state'] == 'running':
                return {**self.view(db, row, actor), 'launch_permitted':False}
            if row['state'] != 'granted':
                raise ValueError('Launch requires a current resource grant')
            self.transition(db, row, 'running')
            return {**self.view(db, self.row(db, ticket), actor), 'launch_permitted':True}

    def finish(self, ticket, actor, lease, fence, outcome, evidence=None):
        self.check_lease(lease, fence)
        if outcome not in ('completed', 'failed', 'unresolved'):
            raise ValueError('Choose completed, failed or unresolved')
        if outcome != 'unresolved' and (not isinstance(evidence, str) or not DIGEST.fullmatch(evidence)):
            raise ValueError('Release requires a supervisor evidence digest')
        with self.transaction() as db:
            row = self.row(db, ticket)
            self.authorize(row, actor, lease, fence)
            if row['state'] in TERMINAL:
                if row['state'] != outcome or row['evidence'] != evidence:
                    raise ValueError('Ticket already has a different terminal result')
            elif row['state'] not in ACTIVE:
                raise ValueError('Ticket has no resource grant')
            else:
                self.transition(db, row, outcome, 'cleanup_unresolved' if outcome=='unresolved' else None, evidence)
            return self.view(db, self.row(db, ticket), actor)

    def cancel(self, ticket, actor):
        with self.transaction() as db:
            row = self.row(db, ticket)
            self.authorize(row, actor, process=not (row['resumable'] and row['state']=='queued'))
            if row['state'] in ('queued', 'granted'):
                self.transition(db, row, 'cancelled', 'owner_cancelled')
            elif row['state'] not in TERMINAL:
                raise ValueError('A launch intent requires supervised cleanup before release')
            return self.view(db, self.row(db, ticket), actor)

    def wait(self, ticket, actor, seconds=600, *, keep=False):
        deadline = time.monotonic()+bounded_seconds(seconds)
        try:
            while True:
                state = self.claim(ticket, actor)
                if state['state'] != 'queued':
                    return state
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    return state if keep else self.cancel(ticket, actor)
                time.sleep(min(.2, remaining))
        except BaseException:
            if not keep:
                self.cancel(ticket, actor)
            raise

    def events(self, actor, after=0, *, lifecycle=False):
        actor_fields(actor)
        if type(after) is not int or after < 0:
            raise ValueError('Event cursor must be a nonnegative integer')
        with self.transaction() as db:
            terminal = '' if lifecycle else " AND state!='running'"
            result = [dict(row) for row in db.execute('SELECT cursor,ticket,state,reason,created,fence FROM events WHERE owner=? AND cursor>?'+terminal+' ORDER BY cursor LIMIT 64', (actor['id'], after))]
            for row in result:
                row['created_at'] = timestamp(row.pop('created'))
                row['signal'] = 'start' if row['state']=='running' else 'attention' if row['state']=='unresolved' else 'end'
            return result



def dispatch(args, root, session):
    if args.resource_action == 'protocol':
        return {'version': 1, 'admission': 'fifo-exclusive', 'notifications': 'owner-terminal-events',
                'capabilities': ['queue-advice', 'resumable-tickets', 'payload-idempotency', 'lifecycle-events', 'one-shot-start']}
    actor = current_actor(args.session or session)
    broker = Broker(root)
    action = args.resource_action
    if action == 'request':
        return broker.request(args.resource, args.key, actor, args.seconds, estimate_seconds=args.estimate_seconds,
                              resumable=args.resumable, payload_digest=args.payload_sha256)
    if action == 'events':
        return broker.events(actor, args.after, lifecycle=args.lifecycle)
    if action == 'status':
        return broker.snapshot(args.ticket, actor)
    if action == 'wait':
        return broker.wait(args.ticket, actor, args.seconds, keep=args.keep)
    if action == 'cancel':
        return broker.cancel(args.ticket, actor)
    if action == 'start':
        return broker.running(args.ticket, actor, args.lease, args.fence)
    return broker.finish(args.ticket, actor, args.lease, args.fence, args.outcome, args.evidence)
