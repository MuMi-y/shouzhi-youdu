"""Durable, serial acceptance control. This module never starts a model or worker."""
from contextlib import contextmanager, closing
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

from scripts.acceptance_budget import BudgetError, require_budget, first_attempt_failed


class ControlError(RuntimeError):
    pass


def wall_ms():
    return time.time_ns() // 1_000_000


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def save_json(path, value):
    """Publish complete evidence atomically; never use this to overwrite a bank file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.writing')
    temporary.write_text(encoded(value), encoding='utf-8')
    temporary.replace(path)


def validate_clock(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ControlError('BUSINESS_CLOCK_REQUIRES_TIMEZONE')
    return parsed.isoformat()


class BusinessClock:
    def __init__(self, value):
        self.value = datetime.fromisoformat(validate_clock(value))

    def __call__(self):
        return self.value


def budget_identity(row):
    return {key: row[key] for key in ('path', 'id', 'provider_limit', 'dsh_limit', 'deadline_ms')}


def canonical_path(path):
    return os.path.normcase(str(Path(path).resolve()))


def text_hash(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest() if value is not None else None


class BudgetAuthority:
    """One budget owns one controller and every round. Local evidence cannot refund it."""
    def __init__(self, budget_path, run_id, directory, identity_json, *, now_ms=wall_ms):
        self.path = Path(budget_path).resolve()
        self.run_id, self.control_path = run_id, canonical_path(directory)
        self.identity_json, self.now_ms = identity_json, now_ms

    @contextmanager
    def db(self):
        # rw never recreates a missing authority file.
        with closing(sqlite3.connect(self.path.as_uri() + '?mode=rw', uri=True, timeout=10)) as db:
            db.row_factory = sqlite3.Row
            db.execute('BEGIN IMMEDIATE')
            with db:
                yield db

    def claim(self):
        with self.db() as db:
            budget = require_budget(self.path, max_provider=120, max_dsh=32,
                                    max_seconds=4200, now_ms=self.now_ms())
            if encoded(budget_identity(budget)) != self.identity_json:
                raise ControlError('BUDGET_IDENTITY_CHANGED')
            db.execute('''CREATE TABLE IF NOT EXISTS g2b_control_owner(
                singleton INTEGER PRIMARY KEY CHECK(singleton=1), run_id TEXT NOT NULL,
                control_path TEXT NOT NULL, identity_json TEXT NOT NULL, state TEXT NOT NULL,
                rounds_used INTEGER NOT NULL CHECK(rounds_used BETWEEN 0 AND 16),
                provider_seen INTEGER NOT NULL, dsh_seen INTEGER NOT NULL, stop_reason TEXT,
                created_ms INTEGER NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS g2b_control_rounds(
                id INTEGER PRIMARY KEY CHECK(id BETWEEN 1 AND 16), step_id TEXT NOT NULL,
                branch_id TEXT NOT NULL, input_hash TEXT NOT NULL, state TEXT NOT NULL,
                result_hash TEXT, reserved_ms INTEGER NOT NULL)''')
            if db.execute('SELECT 1 FROM g2b_control_owner').fetchone():
                raise ControlError('BUDGET_ALREADY_BOUND')
            if db.execute('SELECT 1 FROM g2b_control_rounds').fetchone():
                raise ControlError('BUDGET_AUTHORITY_INVALID')
            db.execute('INSERT INTO g2b_control_owner VALUES(1,?,?,?,?,0,?,?,NULL,?)',
                       (self.run_id, self.control_path, self.identity_json, 'INITIALIZING',
                        budget['provider_used'], budget['dsh_used'], self.now_ms()))

    def _check(self, db, local_rounds, *, state='ACTIVE'):
        owner = db.execute('SELECT * FROM g2b_control_owner WHERE singleton=1').fetchone()
        if owner is None:
            return 'BUDGET_BINDING_MISSING'
        if owner['run_id'] != self.run_id or owner['control_path'] != self.control_path:
            return 'BUDGET_OWNER_MISMATCH'
        if owner['identity_json'] != self.identity_json:
            return 'BUDGET_IDENTITY_CHANGED'
        if owner['state'] != state:
            return owner['stop_reason'] or 'BUDGET_OWNER_' + owner['state']
        try:
            budget = require_budget(self.path, max_provider=120, max_dsh=32, now_ms=self.now_ms())
        except BudgetError as error:
            return error.code
        if encoded(budget_identity(budget)) != self.identity_json:
            return 'BUDGET_IDENTITY_CHANGED'
        if budget['provider_used'] < owner['provider_seen'] or budget['dsh_used'] < owner['dsh_seen']:
            return 'BUDGET_COUNTER_REWOUND'
        authoritative = db.execute('SELECT * FROM g2b_control_rounds ORDER BY id').fetchall()
        if owner['rounds_used'] != len(authoritative) or len(authoritative) != len(local_rounds):
            return 'LOCAL_AUTHORITY_DIVERGED'
        for remote, local in zip(authoritative, sorted(local_rounds, key=lambda r: r['id'])):
            expected = (local['id'], local['step_id'], local['branch_id'], text_hash(local['input_json']),
                        local['result_state'], text_hash(local['result_json']))
            actual = tuple(remote[k] for k in ('id', 'step_id', 'branch_id', 'input_hash', 'state', 'result_hash'))
            if actual != expected:
                return 'LOCAL_AUTHORITY_DIVERGED'
        db.execute('UPDATE g2b_control_owner SET provider_seen=?,dsh_seen=? WHERE singleton=1',
                   (budget['provider_used'], budget['dsh_used']))
        return None

    def activate(self, local_rounds):
        with self.db() as db:
            reason = self._check(db, local_rounds, state='INITIALIZING')
            if reason: raise ControlError(reason)
            db.execute("UPDATE g2b_control_owner SET state='ACTIVE' WHERE singleton=1")

    def check(self, local_rounds):
        try:
            with self.db() as db:
                return self._check(db, local_rounds)
        except sqlite3.Error:
            return 'BUDGET_BINDING_MISSING_OR_INVALID'

    def reserve(self, local_rounds, step, payload_json):
        with self.db() as db:
            reason = self._check(db, local_rounds)
            owner = db.execute('SELECT * FROM g2b_control_owner WHERE singleton=1').fetchone()
            if not reason and owner['rounds_used'] >= 16: reason = 'ROUND_LIMIT'
            if not reason and db.execute("SELECT 1 FROM g2b_control_rounds WHERE state='RESERVED'").fetchone():
                reason = 'ROUND_IN_FLIGHT'
            if reason: raise ControlError(reason)
            number = owner['rounds_used'] + 1
            db.execute("INSERT INTO g2b_control_rounds VALUES(?,?,?,?,'RESERVED',NULL,?)",
                       (number, step['id'], step['branch_id'], text_hash(payload_json), self.now_ms()))
            db.execute('UPDATE g2b_control_owner SET rounds_used=? WHERE singleton=1', (number,))
        return number

    def finish(self, local_rounds, round_id, state, result_json):
        with self.db() as db:
            reason = self._check(db, local_rounds)
            if reason: raise ControlError(reason)
            cursor = db.execute("UPDATE g2b_control_rounds SET state=?,result_hash=? WHERE id=? AND state='RESERVED'",
                                (state, text_hash(result_json), round_id))
            if cursor.rowcount != 1: raise ControlError('AUTHORITY_ROUND_ALREADY_FINISHED')

    def stop(self, reason):
        """A copied/moved non-owner may stop itself, never the real owner's batch."""
        try:
            with self.db() as db:
                db.execute("UPDATE g2b_control_owner SET state='STOPPED',stop_reason=COALESCE(stop_reason,?) "
                           'WHERE singleton=1 AND run_id=? AND control_path=? AND identity_json=?',
                           (reason, self.run_id, self.control_path, self.identity_json))
        except sqlite3.Error:
            # Missing authority cannot be recreated from local evidence. Local guard stays fail-closed.
            return False
        return True

    def snapshot(self):
        try:
            with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=10)) as db:
                db.row_factory = sqlite3.Row
                row = db.execute('SELECT * FROM g2b_control_owner WHERE singleton=1').fetchone()
            if row is None: return {'error': 'BUDGET_BINDING_MISSING'}
            result = dict(row)
            result['location_matches'] = result['control_path'] == self.control_path and result['run_id'] == self.run_id
            return result
        except sqlite3.Error:
            return {'error': 'BUDGET_BINDING_MISSING_OR_INVALID'}


class Control:
    def __init__(self, directory, *, now_ms=wall_ms):
        self.directory = Path(directory).resolve()
        self.path = self.directory / 'control.sqlite3'
        self.now_ms = now_ms  # Explicit real-clock injection, independent of business time.
        if not self.path.is_file():
            raise ControlError('CONTROL_NOT_INITIALIZED')

    @classmethod
    def create(cls, directory, *, mode='offline', budget_path=None, source_hashes=None, now_ms=wall_ms):
        if mode not in {'offline', 'live'}:
            raise ControlError('INVALID_MODE')
        directory = Path(directory).resolve()
        budget = None
        if mode == 'live' or budget_path is not None:
            budget = require_budget(budget_path, max_provider=120, max_dsh=32,
                                    max_seconds=4200, now_ms=now_ms())
        if directory.exists(): raise FileExistsError(str(directory))
        run_id = uuid4().hex
        authority = BudgetAuthority(budget['path'], run_id, directory, encoded(budget_identity(budget)),
                                    now_ms=now_ms) if budget else None
        if authority:
            authority.claim()  # Durable first. Any later init failure leaves INITIALIZING occupied.
        # A fresh control directory prevents accidental reuse/reset of existing story evidence.
        directory.mkdir(parents=True, exist_ok=False)
        with closing(sqlite3.connect(directory / 'control.sqlite3')) as db, db:
            db.executescript('''
                CREATE TABLE run(id TEXT PRIMARY KEY, mode TEXT, state TEXT, created_ms INTEGER,
                    deadline_ms INTEGER, round_limit INTEGER, budget_path TEXT, budget_identity_json TEXT,
                    provider_seen INTEGER, dsh_seen INTEGER, stop_reason TEXT, source_hashes_json TEXT);
                CREATE TABLE branches(id TEXT PRIMARY KEY, runtime TEXT UNIQUE, business_clock TEXT, config_json TEXT);
                CREATE TABLE steps(id TEXT PRIMARY KEY, branch_id TEXT, kind TEXT, trigger_json TEXT,
                    state TEXT, started_ms INTEGER, finished_ms INTEGER, evidence_path TEXT, review_json TEXT);
                CREATE TABLE rounds(id INTEGER PRIMARY KEY AUTOINCREMENT, step_id TEXT, branch_id TEXT,
                    reserved_at_ms INTEGER, input_json TEXT, result_state TEXT, result_json TEXT);
                CREATE TABLE audit(id INTEGER PRIMARY KEY AUTOINCREMENT, at_ms INTEGER, action TEXT, detail_json TEXT);
            ''')
            now = now_ms()
            db.execute('INSERT INTO run VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', (
                run_id, mode, 'ACTIVE', now, budget['deadline_ms'] if budget else now + 4200000,
                16, budget['path'] if budget else None, encoded(budget_identity(budget)) if budget else None,
                budget['provider_used'] if budget else 0, budget['dsh_used'] if budget else 0,
                None, encoded(source_hashes or {})))
        if authority: authority.activate([])
        return cls(directory, now_ms=now_ms)

    @contextmanager
    def db(self):
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA busy_timeout=10000')
            yield db

    def _audit(self, db, action, detail):
        db.execute('INSERT INTO audit(at_ms,action,detail_json) VALUES(?,?,?)',
                   (self.now_ms(), action, encoded(detail)))

    def _authority(self, row):
        return BudgetAuthority(row['budget_path'], row['id'], self.directory, row['budget_identity_json'],
                               now_ms=self.now_ms) if row['budget_path'] else None

    @staticmethod
    def _rounds(db):
        return [dict(r) for r in db.execute('SELECT * FROM rounds ORDER BY id')]

    def _halt(self, db, reason):
        row = db.execute('SELECT * FROM run').fetchone()
        authority = self._authority(row)
        if authority: authority.stop(reason)
        db.execute("UPDATE run SET state='STOPPED',stop_reason=COALESCE(stop_reason,?)", (reason,))
        self._audit(db, 'stop', {'reason': reason})

    def _guard(self, db, *, required_state='ACTIVE'):
        """Return a reason instead of raising inside a transaction, so STOPPED commits."""
        row = dict(db.execute('SELECT * FROM run').fetchone())
        if row['state'] != required_state:
            return row['stop_reason'] or ('REVIEW_REQUIRED' if row['state'] == 'REVIEW' else 'RUN_NOT_ACTIVE')
        if self.now_ms() >= row['deadline_ms']:
            return 'CONTROL_DEADLINE'
        if row['budget_path']:
            try:
                budget = require_budget(row['budget_path'], max_provider=120, max_dsh=32,
                                        now_ms=self.now_ms())
            except BudgetError as error:
                return error.code
            if encoded(budget_identity(budget)) != row['budget_identity_json']:
                return 'BUDGET_IDENTITY_CHANGED'
            if budget['provider_used'] < row['provider_seen'] or budget['dsh_used'] < row['dsh_seen']:
                return 'BUDGET_COUNTER_REWOUND'
            reason = self._authority(row).check(self._rounds(db))
            if reason: return reason
            db.execute('UPDATE run SET provider_seen=?,dsh_seen=?',
                       (budget['provider_used'], budget['dsh_used']))
        return None

    def status(self):
        with self.db() as db:
            result = {'run': dict(db.execute('SELECT * FROM run').fetchone())}
            for table in ('branches', 'steps', 'rounds', 'audit'):
                result[table] = [dict(row) for row in db.execute(f'SELECT * FROM {table} ORDER BY rowid')]
        result['local_round_evidence_count'] = len(result['rounds'])
        authority = self._authority(result['run'])
        result['authority'] = authority.snapshot() if authority else None
        result['rounds_used'] = result['authority'].get('rounds_used') if authority else len(result['rounds'])
        result['rounds_remaining'] = 16 - result['rounds_used'] if result['rounds_used'] is not None else None
        result['has_uncertain_step'] = any(s['state'] == 'RUNNING' for s in result['steps'])
        return result

    def add_branch(self, branch_id, business_clock, config, *, runtime_root=None):
        if branch_id not in {'G', 'S03', 'H01', 'H02', 'MICRO'}:
            raise ControlError('UNKNOWN_BRANCH')
        runtime = ((Path(runtime_root).resolve() if runtime_root is not None else
                    self.directory / 'branches') / branch_id)
        if runtime_root is not None and runtime.exists():
            raise ControlError('BRANCH_RUNTIME_ALREADY_EXISTS')
        value = validate_clock(business_clock)
        with self.db() as db, db:
            db.execute('BEGIN IMMEDIATE')
            reason = self._guard(db)
            if reason:
                if reason != 'REVIEW_REQUIRED': self._halt(db, reason)
            elif db.execute("SELECT 1 FROM steps WHERE state='RUNNING'").fetchone():
                reason = 'STEP_IN_FLIGHT'
            else:
                db.execute('INSERT INTO branches VALUES(?,?,?,?)', (branch_id, str(runtime), value, encoded(config)))
                self._audit(db, 'add_branch', {'branch_id': branch_id, 'runtime': str(runtime)})
        if reason: raise ControlError(reason)
        return self.branch(branch_id)

    def branch(self, branch_id):
        with self.db() as db:
            row = db.execute('SELECT * FROM branches WHERE id=?', (branch_id,)).fetchone()
        if row is None: raise ControlError('BRANCH_NOT_INITIALIZED')
        return dict(row)

    def begin_step(self, branch_id, kind, trigger):
        step_id = uuid4().hex
        with self.db() as db, db:
            db.execute('BEGIN IMMEDIATE')
            reason = self._guard(db)
            if reason:
                if reason != 'REVIEW_REQUIRED': self._halt(db, reason)
            elif not db.execute('SELECT 1 FROM branches WHERE id=?', (branch_id,)).fetchone():
                reason = 'BRANCH_NOT_INITIALIZED'
            elif db.execute("SELECT 1 FROM steps WHERE state='RUNNING'").fetchone():
                reason = 'STEP_IN_FLIGHT_REQUIRES_RECOVERY'
            else:
                path = str(self.directory / 'evidence' / (step_id + '.json'))
                db.execute('INSERT INTO steps VALUES(?,?,?,?,?,?,?,?,?)',
                           (step_id, branch_id, kind, encoded(trigger), 'RUNNING', self.now_ms(), None, path, None))
        if reason: raise ControlError(reason)
        return step_id

    def reserve_round(self, step_id, payload):
        with self.db() as db, db:
            db.execute('BEGIN IMMEDIATE')
            reason = self._guard(db)
            step = db.execute('SELECT * FROM steps WHERE id=?', (step_id,)).fetchone()
            count = db.execute('SELECT count(*) FROM rounds').fetchone()[0]
            if not reason and (step is None or step['state'] != 'RUNNING'):
                reason = 'STEP_NOT_RUNNING'
            if not reason and db.execute("SELECT 1 FROM rounds WHERE result_state='RESERVED'").fetchone():
                reason = 'ROUND_IN_FLIGHT'
            if not reason and count >= 16:
                reason = 'ROUND_LIMIT'
            if reason:
                self._halt(db, reason)
                round_id = None
            else:
                authority = self._authority(db.execute('SELECT * FROM run').fetchone())
                try:
                    if authority:
                        round_id = authority.reserve(self._rounds(db), step, encoded(payload))
                        db.execute('INSERT INTO rounds(id,step_id,branch_id,reserved_at_ms,input_json,result_state) VALUES(?,?,?,?,?,?)',
                                   (round_id, step_id, step['branch_id'], self.now_ms(), encoded(payload), 'RESERVED'))
                    else:
                        cursor = db.execute('INSERT INTO rounds(step_id,branch_id,reserved_at_ms,input_json,result_state) VALUES(?,?,?,?,?)',
                                            (step_id, step['branch_id'], self.now_ms(), encoded(payload), 'RESERVED'))
                        round_id = cursor.lastrowid
                except (ControlError, sqlite3.Error) as error:
                    reason = str(error) if isinstance(error, ControlError) else 'LOCAL_ROUND_WRITE_FAILED'
                    self._halt(db, reason)
                    round_id = None
        if reason: raise ControlError(reason)
        return round_id

    def finish_round(self, round_id, result):
        failed = first_attempt_failed([{'result': result}])
        with self.db() as db, db:
            db.execute('BEGIN IMMEDIATE')
            authority = self._authority(db.execute('SELECT * FROM run').fetchone())
            if authority:
                try:
                    authority.finish(self._rounds(db), round_id, 'FAILED' if failed else 'RETURNED', encoded(result))
                except (ControlError, sqlite3.Error) as error:
                    reason = str(error) if isinstance(error, ControlError) else 'AUTHORITY_FINISH_FAILED'
                    self._halt(db, reason)
                    # Preserve the returned raw result even if the authority/host deadline stopped in flight.
                    db.execute("UPDATE rounds SET result_state='FAILED',result_json=? WHERE id=? AND result_state='RESERVED'",
                               (encoded(result), round_id))
                    return
            cursor = db.execute("UPDATE rounds SET result_state=?,result_json=? WHERE id=? AND result_state='RESERVED'",
                                ('FAILED' if failed else 'RETURNED', encoded(result), round_id))
            if cursor.rowcount != 1: raise ControlError('ROUND_ALREADY_FINISHED')
            reason = self._guard(db)
            if failed: reason = 'MODEL_OR_FIRST_ATTEMPT_FAILED'
            if reason: self._halt(db, reason)

    def finish_step(self, step_id, evidence, *, error=None):
        with self.db() as db:
            step = db.execute('SELECT * FROM steps WHERE id=?', (step_id,)).fetchone()
        if step is None or step['state'] != 'RUNNING': raise ControlError('STEP_NOT_RUNNING')
        # Write full artifacts before marking DONE. A crash leaves an uncertain step, never an auto retry.
        save_json(step['evidence_path'], evidence)
        with self.db() as db, db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE steps SET state=?,finished_ms=? WHERE id=?',
                       ('FAILED' if error else 'DONE', self.now_ms(), step_id))
            if error:
                self._halt(db, error)
            else:
                db.execute("UPDATE run SET state='REVIEW' WHERE state='ACTIVE'")

    def review(self, step_id, *, accept, note):
        if not note.strip(): raise ControlError('REVIEW_NOTE_REQUIRED')
        with self.db() as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM steps ORDER BY rowid DESC LIMIT 1').fetchone()
            if row is None or row['id'] != step_id or row['state'] not in {'DONE', 'FAILED'}:
                raise ControlError('REVIEW_LAST_COMPLETED_STEP_ONLY')
            db.execute('UPDATE steps SET review_json=? WHERE id=?',
                       (encoded({'accept_to_continue': accept, 'note': note, 'quality_verdict': 'EXTERNAL_REVIEW'}), step_id))
            self._audit(db, 'review', {'step_id': step_id, 'accept': accept, 'note': note})
            if not accept:
                self._halt(db, 'REVIEW_REJECTED')
            elif db.execute('SELECT state FROM run').fetchone()[0] == 'REVIEW':
                reason = self._guard(db, required_state='REVIEW')
                if reason: self._halt(db, reason)
                else: db.execute("UPDATE run SET state='ACTIVE'")
        # A stopped batch cannot be revived by review, even after a corrected answer.
        return self.status()

    def set_clock(self, step_id, value):
        value = validate_clock(value)
        with self.db() as db, db:
            row = db.execute("SELECT * FROM steps WHERE id=? AND state='RUNNING'", (step_id,)).fetchone()
            if row is None or row['kind'] != 'clock': raise ControlError('EXPLICIT_CLOCK_STEP_REQUIRED')
            old = db.execute('SELECT business_clock FROM branches WHERE id=?', (row['branch_id'],)).fetchone()[0]
            if datetime.fromisoformat(value) < datetime.fromisoformat(old): raise ControlError('CLOCK_CANNOT_REWIND')
            db.execute('UPDATE branches SET business_clock=? WHERE id=?', (value, row['branch_id']))
            self._audit(db, 'business_clock', {'before': old, 'after': value, 'step_id': step_id})

    def stop(self, reason):
        with self.db() as db, db:
            db.execute('BEGIN IMMEDIATE')
            self._halt(db, reason)

    def recover_uncertain(self):
        """Call only under the exclusive process lease. No replay, refund or resume."""
        with self.db() as db, db:
            db.execute('BEGIN IMMEDIATE')
            count = db.execute("SELECT count(*) FROM steps WHERE state='RUNNING'").fetchone()[0]
            if count:
                db.execute("UPDATE steps SET state='INTERRUPTED',finished_ms=? WHERE state='RUNNING'", (self.now_ms(),))
                db.execute("UPDATE rounds SET result_state='UNCERTAIN' WHERE result_state='RESERVED'")
                self._halt(db, 'UNCERTAIN_STEP_NO_REPLAY')
        return count


class ControlledRunner:
    def __init__(self, control, step_id, runner, *, offline_test=False):
        self.control, self.step_id, self.runner = control, step_id, runner
        self.offline_test = offline_test

    def __call__(self, prompt, session_id, **options):
        return self._invoke(prompt, session_id, options)

    def run_request(self, prompt, session_id, *, request_context, readonly=False):
        return self._invoke(prompt, session_id, {'readonly': readonly}, request_context=request_context)

    def _invoke(self, prompt, session_id, options, *, request_context=None):
        state = self.control.status()['run']
        if state['mode'] != 'live' and not self.offline_test:
            raise ControlError('OFFLINE_MODEL_DISABLED')
        if state['mode'] == 'live' and str(Path(self.runner.batch_budget).resolve()) != state['budget_path']:
            self.control.stop('RUNNER_BUDGET_MISMATCH')
            raise ControlError('RUNNER_BUDGET_MISMATCH')
        round_id = self.control.reserve_round(self.step_id, {'prompt': prompt, 'session_id': session_id, 'options': options})
        try:
            if request_context is None:
                result = self.runner(prompt, session_id, **options)
            elif callable(getattr(type(self.runner), 'run_request', None)):
                result = self.runner.run_request(prompt, session_id, request_context=request_context, **options)
            elif self.offline_test:
                result = self.runner(prompt, session_id, **options)
            else:
                raise ControlError('REQUEST_CONTEXT_RUNNER_REQUIRED')
            if not isinstance(result, dict):
                result = {'ok': False, 'error': 'INVALID_RUNNER_RESULT'}
        except Exception as error:
            self.control.finish_round(round_id, {'ok': False, 'error': {'type': type(error).__name__, 'message': str(error)}})
            raise
        self.control.finish_round(round_id, result)
        return result


@contextmanager
def exclusive_lease(directory):
    """OS releases the lease on crash; durable RUNNING steps remain for explicit recovery."""
    path = Path(directory) / '.execution-lock'
    with path.open('a+b') as handle:
        try:
            # Reading an already locked byte itself raises PermissionError on Windows.
            # Inspect size instead; keep initialization and acquisition in the same error boundary.
            if path.stat().st_size == 0:
                handle.write(b'1')
                handle.flush()
            handle.seek(0)
            if __import__('sys').platform == 'win32':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ControlError('OTHER_EXECUTOR_ACTIVE') from None
        try:
            yield
        finally:
            handle.seek(0)
            if __import__('sys').platform == 'win32':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
