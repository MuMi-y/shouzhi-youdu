"""Durable, bounded host audit; never calls a domain service inside a DB lock."""
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from src.bank_requests.protocol import RunHandle, AttemptHandle, RecoveryHandle, MINIMAL, LEGACY
from .handoff_contracts import canonical, digest, parameter_view, safe, object_refs
from .handoff_read_failure import known as known_read_failure

# DDL is embedded below; no dependency on management evidence at runtime.
DDL = '''


CREATE TABLE IF NOT EXISTS handoff_meta (
  key TEXT PRIMARY KEY,
  value_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS handoff_runs (
  run_id TEXT PRIMARY KEY,
  job_id TEXT UNIQUE REFERENCES jobs(id),
  instance_id TEXT NOT NULL,
  epoch_id TEXT NOT NULL,
  created_epoch_id TEXT NOT NULL, 
  request_id TEXT NOT NULL,
  subject_id TEXT NOT NULL CHECK(subject_id='U1'),
  origin TEXT NOT NULL CHECK(origin IN
    ('CHAT','EVENT','GOAL_AI','TASK_NODE','USER_ACTION','SYSTEM','LEGACY_IMPORT')),
  source_contract TEXT NOT NULL CHECK(source_contract IN
    ('minimal_source_v2','legacy_source_v1','not_applicable')),
  response_mode TEXT NOT NULL CHECK(response_mode IN
    ('REQUEST','EVENT_TYPED','GOAL_TYPED','HOST_ACTION','MONEYDATA','HISTORY')),
  read_only INTEGER NOT NULL CHECK(read_only IN (0,1)),
  identity_json TEXT NOT NULL,
  identity_hash TEXT NOT NULL,
  parent_run_id TEXT REFERENCES handoff_runs(run_id),
  state TEXT NOT NULL CHECK(state IN
    ('QUEUED','RUNNING','CLOSING','CLOSED','INTERRUPTED','HISTORY','AUDIT_BLOCKED')),
  completeness TEXT NOT NULL CHECK(completeness IN
    ('OPEN','COMPLETE','UNKNOWN','INCOMPLETE','LEGACY_UNPROVEN')),
  next_attempt_ordinal INTEGER NOT NULL DEFAULT 1,
  reserved_bytes INTEGER NOT NULL DEFAULT 0 CHECK(reserved_bytes>=0), 
  reserved_event_slots INTEGER NOT NULL DEFAULT 0 CHECK(reserved_event_slots>=0),
  used_bytes INTEGER NOT NULL DEFAULT 0 CHECK(used_bytes>=0),
  used_event_slots INTEGER NOT NULL DEFAULT 0 CHECK(used_event_slots BETWEEN 0 AND 32),
  dispatch_started INTEGER NOT NULL DEFAULT 0 CHECK(dispatch_started IN (0,1)), 
  producers_stopped INTEGER NOT NULL DEFAULT 0 CHECK(producers_stopped IN (0,1)),
  trace_finalized INTEGER NOT NULL DEFAULT 0 CHECK(trace_finalized IN (0,1)),
  created_at TEXT NOT NULL,
  closed_at TEXT
);
CREATE TABLE IF NOT EXISTS handoff_attempts (
  attempt_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES handoff_runs(run_id),
  ordinal INTEGER NOT NULL,
  parent_attempt_id TEXT REFERENCES handoff_attempts(attempt_id),
  actor TEXT NOT NULL CHECK(actor IN ('MODEL_GATEWAY','MODEL_TRACE','TASK_EXECUTOR','TRUSTED_UI','SYSTEM')),
  entry_name TEXT NOT NULL,
  effect_class TEXT NOT NULL CHECK(effect_class IN
    ('BANK_PREPARE','TASK_WRITE','GOAL_DRAFT','GOAL_ACTION','INSIGHT_WRITE','USER_BANK_ACTION','SERVICE_STATE','READ_ONLY','UNKNOWN_TOOL')),
  raw_safe_json TEXT NOT NULL,
  raw_safe_hash TEXT NOT NULL,
  raw_integrity TEXT NOT NULL CHECK(raw_integrity IN ('EXACT_SAFE','REDACTED','METADATA_ONLY')),
  raw_ref_json TEXT,
  business_safe_json TEXT, 
  business_safe_hash TEXT,
  logical_key TEXT,
  reserved_bytes INTEGER NOT NULL DEFAULT 0 CHECK(reserved_bytes>=0),
  reserved_event_slots INTEGER NOT NULL DEFAULT 0 CHECK(reserved_event_slots>=0),
  used_bytes INTEGER NOT NULL DEFAULT 0 CHECK(used_bytes>=0),
  used_event_slots INTEGER NOT NULL DEFAULT 0 CHECK(used_event_slots BETWEEN 0 AND 16),
  reservation_closed INTEGER NOT NULL DEFAULT 0 CHECK(reservation_closed IN (0,1)),
  created_at TEXT NOT NULL,
  UNIQUE(run_id,ordinal)
);
CREATE TABLE IF NOT EXISTS handoff_events (
  sequence INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id TEXT NOT NULL UNIQUE,
  run_id TEXT NOT NULL REFERENCES handoff_runs(run_id),
  attempt_id TEXT REFERENCES handoff_attempts(attempt_id),
  producer_key TEXT NOT NULL UNIQUE,
  phase TEXT NOT NULL CHECK(phase IN
    ('BEGIN','VALIDATED','REJECTED_BEFORE_DISPATCH','DISPATCHED','RETURNED','RAISED',
     'UNKNOWN','LATE_RETURN','TRACE_LINK','DOMAIN_COMMITTED','DOMAIN_ROLLED_BACK',
     'MODEL_FAILED','SOURCE_REJECTED','RESULT_REJECTED','RESULT_ACCEPTED',
     'TRACE_UNPAIRED','TOOL_GUARD_REJECTED','RUN_LIMIT','RUN_CLOSED','RECOVERED','QUEUED_REBOUND')),
  data_json TEXT NOT NULL,
  payload_hash TEXT NOT NULL, 
  observed_at TEXT NOT NULL,
  schema_version INTEGER NOT NULL DEFAULT 1 CHECK(schema_version=1)
);
CREATE INDEX IF NOT EXISTS handoff_event_run ON handoff_events(run_id,sequence);
CREATE INDEX IF NOT EXISTS handoff_event_attempt ON handoff_events(attempt_id,sequence);
CREATE INDEX IF NOT EXISTS handoff_logical_key ON handoff_attempts(logical_key);




'''
TAIL = frozenset({'UNKNOWN', 'LATE_RETURN', 'RECOVERED', 'DOMAIN_COMMITTED', 'DOMAIN_ROLLED_BACK'})
RUN_TAIL = frozenset({'RUN_CLOSED', 'RUN_LIMIT', 'RECOVERED', 'UNKNOWN', 'MODEL_FAILED'})
IDENTITY_FIELDS = ('id', 'kind', 'event_key', 'prompt', 'scope', 'read_only', 'retry_of')


class HandoffError(ValueError):
    pass


def service_ownership(directory, inherited=None):
    """Same lock as the previous production launcher, before ANY migration."""
    path = (Path(directory)/'.service-lock').resolve()
    if inherited is not None:
        if not hasattr(inherited,'fileno') or inherited.closed or Path(inherited.name).resolve() != path:
            raise HandoffError('INVALID_SERVICE_LOCK')
        return inherited,False
    stream = path.open('a+b')
    try:
        stream.seek(0)
        if not stream.read(1):
            stream.write(b'1'); stream.flush()
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
        return stream,True
    except BaseException:
        stream.close()
        raise HandoffError('SERVICE_INSTANCE_ALREADY_ACTIVE') from None


def backup_before_migration(directory):
    directory=Path(directory)
    path=directory/'jobs.sqlite3'
    if not path.exists():
        return
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='handoff_meta'").fetchone():
            return
    target=directory/('handoff-before-v1-'+uuid4().hex)
    target.mkdir()
    manifest={}
    for source in directory.glob('*.sqlite3'):
        destination=target/source.name
        with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as original, closing(sqlite3.connect(destination)) as copied:
            original.backup(copied)
            if copied.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise HandoffError('MIGRATION_BACKUP_INVALID')
        import hashlib
        manifest[source.name]=hashlib.sha256(destination.read_bytes()).hexdigest()
    (target/'manifest.json').write_text(canonical(manifest),encoding='utf-8')


def insert(db, table, values):
    keys = list(values)
    db.execute(f"INSERT INTO {table} ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})", tuple(values[k] for k in keys))


class HandoffStore:
    def __init__(self, jobs):
        self.jobs, self.clock = jobs, jobs.clock
        self.issuer = object()
        self.epoch = uuid4().hex
        # Process-lifetime ownership: an old worker/pump cannot coexist with a
        # new epoch. Old DSH children only have revoked scoped HTTP credentials.
        self._owner = open(str(jobs.path)+'.handoff-lock', 'a+b')
        self._owner.seek(0)
        if os.name == 'nt':
            import msvcrt
            try:
                msvcrt.locking(self._owner.fileno(),msvcrt.LK_NBLCK,1)
            except OSError:
                self._owner.close()
                raise HandoffError('HANDOFF_INSTANCE_ALREADY_ACTIVE') from None
        else:
            import fcntl
            fcntl.flock(self._owner,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:
            self._initialize()
        except BaseException:
            self._owner.close()
            raise

    def _initialize(self):
        with self.jobs.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            for statement in DDL.split(';'):
                if statement.strip():
                    db.execute(statement)
            instance = self._meta(db, 'instance_id')
            if instance is None:
                instance = uuid4().hex
                self._set_meta(db, 'instance_id', instance)
                self._set_meta(db, 'capacity_v1', dict(used_bytes=0, reserved_bytes=0,
                    used_event_slots=0, reserved_event_slots=0, max_bytes=512*1024*1024, max_slots=200000))
                self.instance = instance
                for row in db.execute('SELECT * FROM jobs').fetchall():
                    self.enroll_in_tx(db, row, legacy=True)
                self._set_meta(db, 'migration', 'handoff-1')
            self.instance = instance
            self._set_meta(db, 'active_epoch', self.epoch)

    @staticmethod
    def _meta(db, key):
        row = db.execute('SELECT value_json FROM handoff_meta WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _set_meta(db, key, value):
        db.execute('INSERT INTO handoff_meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json',
                   (key, canonical(value)))

    def _capacity(self, db, *, used_bytes=0, reserved_bytes=0, used_event_slots=0, reserved_event_slots=0):
        value = self._meta(db, 'capacity_v1')
        for key, delta in dict(used_bytes=used_bytes, reserved_bytes=reserved_bytes,
                              used_event_slots=used_event_slots, reserved_event_slots=reserved_event_slots).items():
            value[key] += delta
            if value[key] < 0:
                raise HandoffError('AUDIT_COUNTER_INVALID')
        if (value['used_bytes'] + value['reserved_bytes'] > value['max_bytes'] or
                value['used_event_slots'] + value['reserved_event_slots'] > value['max_slots']):
            raise HandoffError('AUDIT_CAPACITY_EXCEEDED')
        self._set_meta(db, 'capacity_v1', value)

    def _run(self, db, run_id):
        row = db.execute('SELECT * FROM handoff_runs WHERE run_id=?', (run_id,)).fetchone()
        if row is None or row['instance_id'] != self.instance:
            raise HandoffError('PROTOCOL_METADATA_MISSING')
        return row

    def _handle(self, row):
        return RunHandle(self.issuer, row['instance_id'], row['epoch_id'], row['run_id'], row['job_id'],
                         row['source_contract'], row['response_mode'], bool(row['read_only']))

    def _check(self, db, handle, *, active=True):
        if not isinstance(handle, RunHandle) or handle.issuer is not self.issuer:
            raise HandoffError('INVALID_RUN_HANDLE')
        row = self._run(db, handle.run_id)
        if self._handle(row) != handle or row['epoch_id'] != self.epoch or self._meta(db, 'active_epoch') != self.epoch:
            raise HandoffError('STALE_RUN_HANDLE')
        if active and row['state'] != 'RUNNING':
            raise HandoffError('RUN_NOT_EXECUTABLE')
        return row

    def validate_handle(self, handle):
        with self.jobs.connection() as db:
            return dict(self._check(db, handle))

    def _create(self, db, *, identity, origin, mode, readonly, job_id=None, parent=None, legacy=False, queued=False):
        safe_identity = safe(identity)
        if 'prompt' in safe_identity:
            safe_identity['prompt'] = {'sha256':digest(safe_identity['prompt'])}
        identity_text = canonical(safe_identity)
        if len(identity_text.encode('utf-8')) > 7000:
            raise HandoffError('RUN_IDENTITY_TOO_LARGE')
        run_id = uuid4().hex
        n = len(identity_text.encode('utf-8')) + 1024
        self._capacity(db, used_bytes=n, reserved_bytes=136*1024-n, reserved_event_slots=32)
        insert(db, 'handoff_runs', dict(run_id=run_id, job_id=job_id, instance_id=self.instance,
            epoch_id=self.epoch, created_epoch_id=self.epoch, request_id=job_id or run_id, subject_id='U1',
            origin=origin, source_contract=LEGACY if legacy else MINIMAL if job_id else 'not_applicable', response_mode=mode,
            read_only=int(readonly), identity_json=identity_text, identity_hash=digest(identity), parent_run_id=parent,
            state='HISTORY' if legacy else ('QUEUED' if queued else 'RUNNING'),
            completeness='LEGACY_UNPROVEN' if legacy else 'OPEN', dispatch_started=int(not queued and not legacy),
            reserved_bytes=136*1024-n, reserved_event_slots=32, used_bytes=n, created_at=self.clock().isoformat()))
        return self._handle(self._run(db, run_id))

    def enroll_in_tx(self, db, job, *, legacy=False, parent=None):
        identity = {key: job[key] for key in IDENTITY_FIELDS}
        mode = 'MONEYDATA' if job['scope'] == 'moneydata' else {'chat': 'REQUEST', 'event': 'EVENT_TYPED', 'goal': 'GOAL_TYPED'}[job['kind']]
        return self._create(db, identity=identity, origin='LEGACY_IMPORT' if legacy else {'chat':'CHAT','event':'EVENT','goal':'GOAL_AI'}[job['kind']],
                            mode='HISTORY' if legacy else mode, readonly=bool(job['read_only']) or job['kind'] != 'chat',
                            job_id=job['id'], legacy=legacy, queued=True, parent=parent)

    def for_job(self, job_id):
        with self.jobs.connection() as db:
            row = db.execute('SELECT * FROM handoff_runs WHERE job_id=?', (job_id,)).fetchone()
            if row is None:
                raise HandoffError('PROTOCOL_METADATA_MISSING')
            return self._handle(row)

    def claim_in_tx(self, db, job):
        row = db.execute('SELECT * FROM handoff_runs WHERE job_id=?', (job['id'],)).fetchone()
        if row is None or row['source_contract'] != MINIMAL or row['state'] != 'QUEUED':
            raise HandoffError('PROTOCOL_METADATA_MISSING')
        if row['identity_hash'] != digest({key: job[key] for key in IDENTITY_FIELDS}):
            raise HandoffError('JOB_IDENTITY_MISMATCH')
        if row['dispatch_started'] or db.execute('SELECT 1 FROM handoff_attempts WHERE run_id=?', (row['run_id'],)).fetchone():
            raise HandoffError('QUEUED_ALREADY_STARTED')
        if row['epoch_id'] != self.epoch:
            self._event(db, row['run_id'], None, 'QUEUED_REBOUND', {'from':row['epoch_id'], 'to':self.epoch}, 'rebind:'+row['run_id']+':'+self.epoch)
        db.execute("UPDATE handoff_runs SET epoch_id=?,state='RUNNING',dispatch_started=1 WHERE run_id=?", (self.epoch,row['run_id']))
        return self._handle(self._run(db, row['run_id']))

    def open_host_run(self, origin, identity, *, readonly=False, parent=None):
        with self.jobs.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            if self._meta(db, 'active_epoch') != self.epoch:
                raise HandoffError('STALE_RUN_HANDLE')
            return self._create(db, identity=identity, origin=origin, mode='HOST_ACTION', readonly=readonly, parent=parent)

    def begin_attempt(self, handle, entry, args, actor, *, effect_class='UNKNOWN_TOOL', parent_attempt=None):
        try:
            return self._begin_attempt(handle,entry,args,actor,effect_class=effect_class,parent_attempt=parent_attempt)
        except HandoffError as error:
            if str(error) in {'REQUEST_TOOL_LIMIT','AUDIT_CAPACITY_EXCEEDED'}:
                with self.jobs.connection() as db:
                    db.execute('BEGIN IMMEDIATE')
                    self._check(db,handle)
                    self._event(db,handle.run_id,None,'RUN_LIMIT',{'reason':str(error)},handle.run_id+':RUN_LIMIT')
                    db.execute("UPDATE handoff_runs SET state='AUDIT_BLOCKED',completeness='INCOMPLETE' WHERE run_id=?",(handle.run_id,))
            raise

    def _begin_attempt(self, handle, entry, args, actor, *, effect_class, parent_attempt):
        text, integrity = parameter_view(args)
        with self.jobs.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self._check(db, handle)
            if row['next_attempt_ordinal'] > 64:
                raise HandoffError('REQUEST_TOOL_LIMIT')
            if parent_attempt:
                parent = db.execute('SELECT run_id FROM handoff_attempts WHERE attempt_id=?', (parent_attempt,)).fetchone()
                if not parent or self._run(db,parent[0])['subject_id'] != row['subject_id']:
                    raise HandoffError('INVALID_PARENT_ATTEMPT')
            attempt_id = uuid4().hex
            n = len(text.encode('utf-8')) + 1024
            self._capacity(db, used_bytes=n, reserved_bytes=200*1024-n, reserved_event_slots=16)
            insert(db, 'handoff_attempts', dict(attempt_id=attempt_id, run_id=handle.run_id,
                ordinal=row['next_attempt_ordinal'], parent_attempt_id=parent_attempt, actor=actor, entry_name=entry,
                effect_class=effect_class, raw_safe_json=text, raw_safe_hash=digest(json.loads(text)), raw_integrity=integrity,
                reserved_bytes=200*1024-n, reserved_event_slots=16, used_bytes=n, created_at=self.clock().isoformat()))
            db.execute('UPDATE handoff_runs SET next_attempt_ordinal=next_attempt_ordinal+1 WHERE run_id=?', (handle.run_id,))
            self._event(db,handle.run_id,attempt_id,'BEGIN',{},attempt_id+':BEGIN')
            return AttemptHandle(handle,attempt_id)

    def _event(self, db, run_id, attempt_id, phase, payload, key):
        payload = safe(payload)
        data = canonical(payload)
        old = db.execute('SELECT * FROM handoff_events WHERE producer_key=?', (key,)).fetchone()
        if old:
            if (old['run_id'],old['attempt_id'],old['phase'],old['data_json']) != (run_id,attempt_id,phase,data):
                raise HandoffError('EVENT_KEY_CONFLICT')
            return old['sequence']
        table, pk, identifier = ('handoff_attempts','attempt_id',attempt_id) if attempt_id else ('handoff_runs','run_id',run_id)
        row = db.execute(f'SELECT * FROM {table} WHERE {pk}=?',(identifier,)).fetchone()
        if not row or row['run_id'] != run_id:
            raise HandoffError('EVENT_RUN_MISMATCH')
        if attempt_id and row['actor']=='MODEL_TRACE' and phase not in {'BEGIN','TRACE_UNPAIRED','TRACE_LINK'}:
            raise HandoffError('TRACE_OBSERVATION_CANNOT_DISPATCH')
        tail, ordinary_limit = (TAIL,12) if attempt_id else (RUN_TAIL,24)
        events = db.execute('SELECT phase FROM handoff_events WHERE run_id=? AND attempt_id IS ?', (run_id,attempt_id)).fetchall()
        if phase not in tail and sum(e['phase'] not in tail for e in events) >= ordinary_limit:
            raise HandoffError('AUDIT_EVENT_LIMIT')
        if phase in tail and any(e['phase'] == phase for e in events):
            raise HandoffError('AUDIT_TERMINAL_ALREADY_RECORDED')
        event = dict(event_id=uuid4().hex,run_id=run_id,attempt_id=attempt_id,producer_key=key,phase=phase,
                     data_json=data,payload_hash=digest(payload),observed_at=self.clock().isoformat(),schema_version=1)
        n = len(canonical(event).encode('utf-8'))
        if n > 4096 or row['reserved_bytes'] < n or row['reserved_event_slots'] < 1:
            raise HandoffError('AUDIT_EVENT_LIMIT')
        self._capacity(db, used_bytes=n,reserved_bytes=-n,used_event_slots=1,reserved_event_slots=-1)
        insert(db,'handoff_events',event)
        db.execute(f'UPDATE {table} SET used_bytes=used_bytes+?,reserved_bytes=reserved_bytes-?,used_event_slots=used_event_slots+1,reserved_event_slots=reserved_event_slots-1 WHERE {pk}=?', (n,n,identifier))
        return db.execute('SELECT sequence FROM handoff_events WHERE event_id=?',(event['event_id'],)).fetchone()[0]

    def append(self, handle, phase, payload, *, producer_key=None):
        try:
            return self._append(handle,phase,payload,producer_key=producer_key)
        except HandoffError as error:
            if str(error)=='AUDIT_EVENT_LIMIT':
                run_id=handle.run.run_id if isinstance(handle,AttemptHandle) else handle.run_id
                with self.jobs.connection() as db:
                    db.execute("UPDATE handoff_runs SET completeness='INCOMPLETE',state=CASE WHEN state='RUNNING' THEN 'AUDIT_BLOCKED' ELSE state END WHERE run_id=?",(run_id,))
            raise

    def _append(self, handle, phase, payload, *, producer_key=None):
        with self.jobs.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            if isinstance(handle, AttemptHandle):
                row = self._check(db,handle.run,active=False)
                key=producer_key or handle.attempt_id+':'+phase
                old=db.execute('SELECT phase FROM handoff_events WHERE producer_key=?',(key,)).fetchone()
                if old and (old[0]==phase or phase=='RETURNED' and old[0]=='LATE_RETURN'):
                    return self._event(db,handle.run.run_id,handle.attempt_id,old[0],payload,key)
                if row['state'] != 'RUNNING' and phase == 'RETURNED':
                    phase = 'LATE_RETURN'
                    producer_key=key
                if row['state'] != 'RUNNING' and phase not in TAIL:
                    raise HandoffError('RUN_CLOSED')
                run_id, attempt_id = handle.run.run_id,handle.attempt_id
            elif isinstance(handle, RecoveryHandle):
                if handle.issuer is not self.issuer or phase not in TAIL:
                    raise HandoffError('INVALID_RECOVERY_HANDLE')
                run_id,attempt_id = handle.run_id,handle.attempt_id
            else:
                self._check(db,handle,active=False)
                run_id,attempt_id = handle.run_id,None
            return self._event(db,run_id,attempt_id,phase,payload,producer_key or (attempt_id or run_id)+':'+phase)

    def validated(self, attempt, params, *, binding_hash=None):
        text, integrity = parameter_view(params)
        if integrity == 'METADATA_ONLY':
            raise HandoffError('AUDIT_PARAMETERS_TOO_LARGE')
        with self.jobs.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            self._check(db,attempt.run)
            row = db.execute('SELECT * FROM handoff_attempts WHERE attempt_id=?',(attempt.attempt_id,)).fetchone()
            if row['business_safe_json'] is not None or row['raw_integrity'] == 'METADATA_ONLY':
                raise HandoffError('AUDIT_PARAMETERS_INVALID')
            n = len(text.encode('utf-8'))
            self._capacity(db,used_bytes=n,reserved_bytes=-n)
            db.execute('UPDATE handoff_attempts SET business_safe_json=?,business_safe_hash=?,logical_key=?,used_bytes=used_bytes+?,reserved_bytes=reserved_bytes-? WHERE attempt_id=?',
                       (text,digest(json.loads(text)),params.get('idempotency_key') if isinstance(params,dict) else None,n,n,attempt.attempt_id))
            self._event(db,attempt.run.run_id,attempt.attempt_id,'VALIDATED',{'business_hash':digest(json.loads(text)),
                **({'binding_hash':binding_hash} if binding_hash else {})},attempt.attempt_id+':VALIDATED')

    def close_in_tx(self, db, handle, result):
        row = self._check(db,handle,active=False)
        if row['state'] == 'CLOSED':
            return
        self._event(db,handle.run_id,None,'RESULT_ACCEPTED' if result.get('ok') else 'RESULT_REJECTED',
                    {'ok':bool(result.get('ok')), 'result_hash':digest(safe(result))},handle.run_id+':RESULT')
        for attempt in db.execute('SELECT * FROM handoff_attempts WHERE run_id=?',(handle.run_id,)).fetchall():
            events = db.execute('SELECT * FROM handoff_events WHERE attempt_id=?',(attempt['attempt_id'],)).fetchall()
            phases = {r['phase'] for r in events}
            if ('DISPATCHED' in phases and not phases & {'RETURNED','REJECTED_BEFORE_DISPATCH','UNKNOWN'}
                    and not known_read_failure(attempt, events)):
                self._event(db,handle.run_id,attempt['attempt_id'],'UNKNOWN',{'reason':'unclosed_dispatch'},attempt['attempt_id']+':UNKNOWN')
                db.execute("UPDATE handoff_runs SET completeness='UNKNOWN' WHERE run_id=?",(handle.run_id,))
        self._event(db,handle.run_id,None,'RUN_CLOSED',{},handle.run_id+':CLOSED')
        db.execute("UPDATE handoff_runs SET state='CLOSED',closed_at=?,completeness=CASE WHEN completeness='OPEN' THEN 'COMPLETE' ELSE completeness END WHERE run_id=?",(self.clock().isoformat(),handle.run_id))

    def close(self, handle, result):
        with self.jobs.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            self.close_in_tx(db,handle,result)

    def mark_incomplete(self, run_id):
        with self.jobs.connection() as db:
            db.execute("UPDATE handoff_runs SET completeness='INCOMPLETE' WHERE run_id=?",(run_id,))

    def producers_finished(self, handle, *, trace_finalized=True):
        """Called only after actual producers exit, not after the 2s close wait."""
        with self.jobs.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            self._check(db,handle,active=False)
            db.execute('UPDATE handoff_runs SET producers_stopped=?,trace_finalized=? WHERE run_id=?',(int(trace_finalized),int(trace_finalized),handle.run_id))
            self._release(db,handle.run_id)

    def _release(self, db, run_id):
        run = self._run(db,run_id)
        if run['state'] not in {'CLOSED','INTERRUPTED'} or not run['producers_stopped'] or not run['trace_finalized']:
            return
        waiting = False
        for a in db.execute('SELECT * FROM handoff_attempts WHERE run_id=?',(run_id,)).fetchall():
            if a['reservation_closed']:
                continue
            events = db.execute('SELECT * FROM handoff_events WHERE attempt_id=?',(a['attempt_id'],)).fetchall()
            phases = {e['phase'] for e in events}
            certain = bool(phases & {'RETURNED','LATE_RETURN','REJECTED_BEFORE_DISPATCH'}) or any(
                e['phase']=='RECOVERED' and json.loads(e['data_json']).get('certain') is True for e in events)
            certain = certain or known_read_failure(a, events)
            if a['actor'] == 'TASK_EXECUTOR':
                certain = certain and bool(phases & {'DOMAIN_COMMITTED','DOMAIN_ROLLED_BACK'})
            if not certain:
                waiting = True
                continue
            self._capacity(db,reserved_bytes=-a['reserved_bytes'],reserved_event_slots=-a['reserved_event_slots'])
            db.execute('UPDATE handoff_attempts SET reserved_bytes=0,reserved_event_slots=0,reservation_closed=1 WHERE attempt_id=?',(a['attempt_id'],))
        if not waiting:
            self._capacity(db,reserved_bytes=-run['reserved_bytes'],reserved_event_slots=-run['reserved_event_slots'])
            db.execute('UPDATE handoff_runs SET reserved_bytes=0,reserved_event_slots=0 WHERE run_id=?',(run_id,))

    def release_owner(self):
        """Lifecycle shutdown only, after worker/pump have stopped."""
        self._owner.close()
        if getattr(self,'service_lock',None):
            self.service_lock.close()

    def snapshot(self, run_id):
        with self.jobs.connection() as db:
            run = dict(self._run(db,run_id))
            attempts = [dict(r) for r in db.execute('SELECT * FROM handoff_attempts WHERE run_id=? ORDER BY ordinal',(run_id,))]
            events = [dict(r) for r in db.execute('SELECT * FROM handoff_events WHERE run_id=? ORDER BY sequence',(run_id,))]
        for event in events:
            event['data'] = json.loads(event.pop('data_json'))
        return dict(run=run,attempts=attempts,events=events)

    def task_origin(self, task, node):
        contract = task.get('node_origins',{}).get(node['id'],{}).get('contract')
        if not contract or contract.get('source_contract') not in {MINIMAL,'not_applicable'}:
            raise HandoffError('TASK_ORIGIN_REQUIRES_EXPLICIT_REPLAN')
        snapshot = self.snapshot(contract['run_id'])
        if contract.get('source_contract')=='not_applicable' and not (
                contract.get('origin_kind')=='TRUSTED_ROOT' and snapshot['run']['origin']=='SYSTEM' and
                snapshot['run']['response_mode']=='HOST_ACTION'):
            raise HandoffError('TASK_ORIGIN_MISMATCH')
        if snapshot['run']['request_id'] != contract['request_id']:
            raise HandoffError('TASK_ORIGIN_MISMATCH')
        if not any({'kind':'task','id':task['task_id']} in e['data'].get('refs',[]) and e['data'].get('contract_hash')==digest(contract)
                   for e in snapshot['events'] if e['phase'] in {'RETURNED','LATE_RETURN'} or e['phase']=='RECOVERED' and e['data'].get('certain')):
            raise HandoffError('TASK_ORIGIN_UNPROVEN')
        return contract

    def bind_task_attempt(self, attempt, contract):
        with self.jobs.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            self._check(db,attempt.run)
            self._run(db,contract['run_id'])
            db.execute('UPDATE handoff_runs SET parent_run_id=? WHERE run_id=?',(contract['run_id'],attempt.run.run_id))

    def children(self, run_id):
        with self.jobs.connection() as db:
            return [r[0] for r in db.execute('SELECT run_id FROM handoff_runs WHERE parent_run_id=? AND origin=? ORDER BY created_at LIMIT 65',(run_id,'TASK_NODE'))]

    def bind_bank(self, bank_path):
        identity=self.bank_identity(bank_path)
        with self.jobs.connection() as db:
            previous=self._meta(db,'bank_identity')
            if previous is not None and previous != identity:
                raise HandoffError('BANK_INSTANCE_CHANGED')
            self._set_meta(db,'bank_identity',identity)

    @staticmethod
    def bank_identity(bank_path):
        path=Path(bank_path).resolve()
        # The projection journal is created lazily by goal services, so merely
        # reading this identity must not initialize it or mutate the bank.
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
            row=db.execute("SELECT value FROM metadata WHERE key='scenario'").fetchone()
        stat=path.stat()
        return {'path':str(path),'file_id':[stat.st_dev,stat.st_ino],
                'initialization_hash':digest(json.loads(row[0]))}

    def reconcile_bank(self, snapshot, bank_path):
        """Read by original key AND exact parameters. Never prepare or confirm."""
        current=self.bank_identity(bank_path)
        with self.jobs.connection() as db:
            if self._meta(db,'bank_identity') != current:
                return
        names={'prepare_transfer':'transfer','prepare_mandate_query':'mandate_query',
               'prepare_mandate_cancel':'mandate_cancel','propose_arrangement':'arrangement'}
        for attempt in snapshot['attempts']:
            phases={e['phase'] for e in snapshot['events'] if e['attempt_id']==attempt['attempt_id']}
            if attempt['entry_name'] not in names or not attempt['logical_key'] or 'DISPATCHED' not in phases or phases & {'RETURNED','LATE_RETURN','RECOVERED'}:
                continue
            params=json.loads(attempt['business_safe_json'] or 'null')
            if not isinstance(params,dict):
                continue
            expected={k:v for k,v in params.items() if k!='idempotency_key'}
            with closing(sqlite3.connect(Path(bank_path).resolve().as_uri()+'?mode=ro',uri=True)) as db:
                row=db.execute('SELECT operation_id,kind,subject_id,params_json FROM operations WHERE idempotency_key=?',(attempt['logical_key'],)).fetchone()
            if row and row[1]==names[attempt['entry_name']] and row[2]=='U1' and json.loads(row[3])==expected:
                handle=RecoveryHandle(self.issuer,snapshot['run']['run_id'],attempt['attempt_id'])
                self.append(handle,'RECOVERED',{'certain':True,'refs':[{'kind':'operation','id':row[0]}],
                    'parameters_hash':digest(expected),'bank_identity':current})
        self.release_recovered(snapshot['run']['run_id'])

    def release_recovered(self, run_id):
        # Recovery owns no live execution capability. Release only the already
        # proven producer/terminal conditions; UNKNOWN still retains capacity.
        with self.jobs.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            self._run(db,run_id)
            self._release(db,run_id)

    def task_dispatches(self, task, node):
        """Bounded original dispatch evidence; never infer absence from a truncation."""
        with self.jobs.connection() as db:
            rows = db.execute('''SELECT DISTINCT r.run_id FROM handoff_runs r
                JOIN handoff_attempts a ON a.run_id=r.run_id
                JOIN handoff_events e ON e.attempt_id=a.attempt_id AND e.phase='DISPATCHED'
                WHERE a.actor='TASK_EXECUTOR' AND a.entry_name IN
                ('prepare_transfer','prepare_mandate_query','prepare_mandate_cancel','propose_arrangement')
                AND (a.logical_key=? OR json_extract(r.identity_json,'$.execution_key')=?)
                ORDER BY r.created_at,r.run_id LIMIT 33''', (node['execution_key'],node['execution_key'])).fetchall()
        if len(rows)>32:
            raise HandoffError('TASK_RECOVERY_EVIDENCE_LIMIT')
        return [self.snapshot(row[0]) for row in rows]

    def task_begin(self, task, node, *, reconcile=False):
        # Begin before resolving parameters or checking the origin, so rejection
        # is retained too. Parent linkage is added only after it is verified.
        waiting=node['state'].startswith('WAITING_')
        handle = self.open_host_run('TASK_NODE',dict(task_id=task['task_id'],node_id=node['id'],
            plan_version=task['plan_version'],execution_key=node['execution_key']),readonly=waiting or reconcile)
        return self.begin_attempt(handle,'task.reconcile_prepare' if reconcile else 'operation' if waiting else node['tool'],
            {'execution_key':node['execution_key']} if reconcile else
            {'operation_id':node['operation_id']} if waiting else node['args'],'TASK_EXECUTOR',
            effect_class='READ_ONLY' if waiting or reconcile else 'TASK_WRITE')

    def task_end(self, attempt, *, committed):
        self.append(attempt,'DOMAIN_COMMITTED' if committed else 'DOMAIN_ROLLED_BACK',{})
        self.close(attempt.run,{'ok':committed})
        self.producers_finished(attempt.run)

    def recover(self):
        with self.jobs.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            for run in db.execute("SELECT * FROM handoff_runs WHERE epoch_id<>? AND state IN ('RUNNING','CLOSING','AUDIT_BLOCKED')",(self.epoch,)).fetchall():
                for attempt in db.execute('SELECT attempt_id,actor FROM handoff_attempts WHERE run_id=?',(run['run_id'],)).fetchall():
                    phases = {r[0] for r in db.execute('SELECT phase FROM handoff_events WHERE attempt_id=?',(attempt[0],))}
                    if attempt['actor']=='MODEL_TRACE':
                        continue  # Observation is not proof of pre-dispatch rejection.
                    if not phases & {'RETURNED','UNKNOWN','REJECTED_BEFORE_DISPATCH'}:
                        phase = 'UNKNOWN' if 'DISPATCHED' in phases else 'REJECTED_BEFORE_DISPATCH'
                        self._event(db,run['run_id'],attempt[0],phase,{'reason':'restart'},attempt[0]+':'+phase)
                db.execute("UPDATE handoff_runs SET state='INTERRUPTED',completeness='UNKNOWN' WHERE run_id=?",(run['run_id'],))
            # Exclusive .service-lock/.handoff-lock and a new epoch retire the
            # previous host's HTTP capabilities. Old bridge files are not
            # imported into this epoch: trace collection is finalized as
            # incomplete, never claimed complete or replayed.
            for run in db.execute("SELECT * FROM handoff_runs WHERE epoch_id<>? AND state IN ('CLOSED','INTERRUPTED') AND (producers_stopped=0 OR trace_finalized=0)",(self.epoch,)).fetchall():
                self._event(db,run['run_id'],None,'RECOVERED',{'producer_boundary':'EXCLUSIVE_HOST_EPOCH_RETIRED',
                    'trace_status':'FINALIZED_INCOMPLETE'},run['run_id']+':RETIRE')
                db.execute("UPDATE handoff_runs SET producers_stopped=1,trace_finalized=1,completeness=CASE WHEN completeness='COMPLETE' THEN 'INCOMPLETE' ELSE completeness END WHERE run_id=?",(run['run_id'],))
                self._release(db,run['run_id'])
            db.execute("UPDATE jobs SET status='INTERRUPTED' WHERE id IN (SELECT job_id FROM handoff_runs WHERE state IN ('INTERRUPTED','HISTORY')) AND status IN ('QUEUED','RUNNING')")

    def invoke(self, origin, entry, args, callback, *, effect_class='SERVICE_STATE', parent=None):
        handle = self.open_host_run(origin, {'entry':entry}, parent=parent)
        attempt = self.begin_attempt(handle,entry,args,{'USER_ACTION':'TRUSTED_UI','TASK_NODE':'TASK_EXECUTOR'}.get(origin,'SYSTEM'),effect_class=effect_class)
        dispatched = False
        try:
            self.validated(attempt,args)
            self.append(attempt,'DISPATCHED',{})
            dispatched = True
            result = callback()
            self.append(attempt,'RETURNED',{'refs':object_refs(result)})
            self.close(handle,{'ok':True})
            self.producers_finished(handle)
            return result
        except BaseException as error:
            self.append(attempt,'RAISED' if dispatched else 'REJECTED_BEFORE_DISPATCH',{'code':getattr(error,'code',type(error).__name__)})
            self.close(handle,{'ok':False})
            self.producers_finished(handle)
            raise

