"""Durable work queue. Process interruption never becomes a successful answer."""

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from uuid import uuid4
from .errors import failure_message
from .history_answer import validate_history_answer


def original_job(db, row):
    """Follow trusted queue links; system retry instructions are not the user's question."""
    visited = {row['id']}
    while row['retry_of']:
        parent = db.execute('SELECT * FROM jobs WHERE id=?', (row['retry_of'],)).fetchone()
        if parent is None or parent['id'] in visited:
            break
        visited.add(parent['id'])
        row = parent
    return row


class JobStore:
    def __init__(self, path, *, clock, handoff=False, service_lock=None):
        self.path = Path(path)
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, event_key TEXT UNIQUE,
                    prompt TEXT NOT NULL, session_id TEXT NOT NULL,
                    status TEXT NOT NULL, result_json TEXT, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            ''')
            columns = {row[1] for row in db.execute('PRAGMA table_info(jobs)')}
            for name, declaration in [('scope', "TEXT NOT NULL DEFAULT 'bank'"),
                                      ('read_only', 'INTEGER NOT NULL DEFAULT 0'), ('retry_of', 'TEXT')]:
                if name not in columns:
                    db.execute(f'ALTER TABLE jobs ADD COLUMN {name} {declaration}')
            db.execute('CREATE UNIQUE INDEX IF NOT EXISTS one_job_retry ON jobs(retry_of)')
        self.handoff = None
        if handoff:
            self.enable_handoff(service_lock=service_lock)

    def enable_handoff(self, *, service_lock=None):
        if self.handoff is None:
            from .handoff_store import HandoffStore, service_ownership, backup_before_migration
            owner,owned = service_ownership(self.path.parent,service_lock)
            try:
                backup_before_migration(self.path.parent.resolve())
                self.handoff = HandoffStore(self)
                self.handoff.service_lock = owner if owned else None
                self.handoff.recover()
            except BaseException:
                if self.handoff:
                    self.handoff.release_owner()
                if owned:
                    owner.close()
                raise
        return self.handoff

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA synchronous=FULL')
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def record(row):
        if row is None:
            return None
        value = dict(row)
        raw = value.pop('result_json')
        value['result'] = json.loads(raw) if raw else None
        if value['result'] and not value['result'].get('ok'):
            from src.bank_requests.response import recovery_view
            value['result'] = recovery_view(value['result'])
            value['result']['error_message'] = failure_message(value['result'])
        return value

    def recover(self):
        """Never automatically replay an interrupted model run containing prepared actions."""
        now = self.clock().isoformat()
        result = json.dumps({'ok': False, 'error': '上次服务在分析时停止。已完成的银行操作请查看原回执；可重新发起查询。'}, ensure_ascii=False)
        if self.handoff:
            self.handoff.recover()
        with self.connection() as db:
            db.execute("UPDATE jobs SET status='INTERRUPTED', result_json=COALESCE(result_json,?), updated_at=? WHERE status='RUNNING'", (result, now))
            # Preserve old answers and diagnostics, but stop presenting unsupported amounts as success.
            for row in db.execute("SELECT * FROM jobs WHERE scope='moneydata' AND status='DONE'").fetchall():
                checked = validate_history_answer(json.loads(row['result_json']), original_job(db, row)['prompt'])
                if not checked.get('ok'):
                    db.execute("UPDATE jobs SET status='FAILED',result_json=?,updated_at=? WHERE id=?",
                               (json.dumps(checked, ensure_ascii=False), now, row['id']))

    def add(self, prompt, *, kind='chat', event_key=None, session_id='bank-v1', scope='bank', read_only=False):
        if scope not in {'bank', 'moneydata'} or type(read_only) is not bool:
            raise ValueError('任务资料范围或只读参数无效。')
        now = self.clock().isoformat()
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            if event_key:
                existing = db.execute('SELECT * FROM jobs WHERE event_key=?', (event_key,)).fetchone()
                if existing:
                    return self.record(existing)
            pending = db.execute("SELECT count(*) FROM jobs WHERE status IN ('QUEUED','RUNNING') AND kind='chat'").fetchone()[0]
            if kind == 'chat' and pending >= 5:
                raise ValueError('正在处理的请求较多，请等当前分析结束。')
            job_id = uuid4().hex
            db.execute('INSERT INTO jobs(id,kind,event_key,prompt,session_id,status,result_json,created_at,updated_at,scope,read_only) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                       (job_id, kind, event_key, prompt, session_id, 'QUEUED', None, now, now, scope, int(read_only)))
            if self.handoff:
                self.handoff.enroll_in_tx(db, db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone())
            return self.record(db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())

    def retry(self, job_id):
        """An explicit recovery reads current facts; it never replays a write request."""
        now = self.clock().isoformat()
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            if not previous or previous['kind'] != 'chat' or previous['status'] not in {'FAILED', 'INTERRUPTED'}:
                raise ValueError('仅能恢复失败或中断的对话。')
            existing = db.execute('SELECT * FROM jobs WHERE retry_of=?', (job_id,)).fetchone()
            if existing:
                return self.record(existing)
            count = db.execute("SELECT count(*) FROM jobs WHERE kind='chat' AND status IN ('QUEUED','RUNNING')").fetchone()[0]
            if count >= 5:
                raise ValueError('正在处理的请求较多，请稍后恢复。')
            new_id = uuid4().hex
            original = original_job(db, previous)
            prompt = ('上次分析中断。本次只能重新查询现有事实并解释，不能新建或执行任何操作。'
                      '如需办理变更，请提示用户核对已有待确认操作或重新明确提出。原始问题：\n' + original['prompt'])
            previous_result = json.loads(previous['result_json']) if previous['result_json'] else {}
            unsupported = previous_result.get('amount_check', {}).get('unsupported', [])
            if unsupported:
                prompt += ('\n上次回答以下数字没有直接工具依据：' + '、'.join(unsupported)
                           + '。本次只引用工具amounts金额，不输出百分比、近似占比或阈值，不进行自行换算。')
            db.execute('INSERT INTO jobs(id,kind,event_key,prompt,session_id,status,result_json,created_at,updated_at,scope,read_only,retry_of) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                       (new_id, 'chat', None, prompt, '', 'QUEUED', None, now, now, previous['scope'], 1, job_id))
            if self.handoff:
                parent = db.execute('SELECT run_id FROM handoff_runs WHERE job_id=?',(job_id,)).fetchone()
                self.handoff.enroll_in_tx(db,db.execute('SELECT * FROM jobs WHERE id=?',(new_id,)).fetchone(),parent=parent[0] if parent else None)
            return self.record(db.execute('SELECT * FROM jobs WHERE id=?', (new_id,)).fetchone())

    def claim(self, *, job_id=None):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            if job_id is None:
                row = db.execute("SELECT * FROM jobs WHERE status='QUEUED' ORDER BY CASE kind WHEN 'chat' THEN 0 ELSE 1 END,created_at,rowid LIMIT 1").fetchone()
            else:
                row = db.execute("SELECT * FROM jobs WHERE status='QUEUED' AND kind='chat' AND scope='bank' AND id=?", (job_id,)).fetchone()
            if not row:
                return None
            if self.handoff:
                self.handoff.claim_in_tx(db,row)
            db.execute("UPDATE jobs SET status='RUNNING',updated_at=? WHERE id=?", (self.clock().isoformat(), row['id']))
            return self.record(db.execute('SELECT * FROM jobs WHERE id=?', (row['id'],)).fetchone())

    def finish(self, job_id, result):
        status = 'DONE' if result.get('ok') else 'FAILED'
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            if self.handoff:
                run = db.execute('SELECT * FROM handoff_runs WHERE job_id=?',(job_id,)).fetchone()
                if run is None:
                    raise ValueError('PROTOCOL_METADATA_MISSING')
                self.handoff.close_in_tx(db,self.handoff._handle(run),result)
            db.execute('UPDATE jobs SET status=?,result_json=?,updated_at=? WHERE id=?', (status, json.dumps(result, ensure_ascii=False), self.clock().isoformat(), job_id))

    def get(self, job_id):
        with self.connection() as db:
            return self.record(db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())

    def original_prompt(self, job_id):
        with self.connection() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            if row is None:
                raise ValueError('没有此任务。')
            return original_job(db, row)['prompt']

    def request_context(self, job_id):
        """Load actual user rows before this request, excluding all synthetic retries."""
        from src.bank_requests.context import build, MAX_MESSAGES
        from src.bank_requests.objects import historical_items, recovery_candidates, unique
        with self.connection() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            if row is None or row['scope'] != 'bank' or row['kind'] != 'chat':
                raise ValueError('BANK_CHAT_REQUIRED')
            original = original_job(db, row)
            upper = db.execute('SELECT rowid FROM jobs WHERE id=?', (original['id'],)).fetchone()[0]
            rows = db.execute("""SELECT * FROM jobs WHERE kind='chat' AND scope='bank'
                AND retry_of IS NULL AND rowid<=? ORDER BY rowid DESC LIMIT ?""",
                (upper, MAX_MESSAGES + 1)).fetchall()
            rows.reverse()
            messages = [{'message_id': item['id'], 'text': item['prompt']} for item in rows]
            lower = db.execute('SELECT rowid FROM jobs WHERE id=?', (rows[0]['id'],)).fetchone()[0]
            decisions = db.execute("""SELECT * FROM jobs WHERE kind='chat' AND scope='bank'
                AND rowid>=? AND rowid<? ORDER BY rowid""", (lower, upper)).fetchall()
            previous, recovery, notices = [], [], []
            for item in decisions:
                if not item['result_json']:
                    continue
                result = json.loads(item['result_json'])
                old_run = db.execute('SELECT source_contract FROM handoff_runs WHERE job_id=?',(item['id'],)).fetchone() if self.handoff else None
                if not self.handoff or old_run and old_run[0] == 'minimal_source_v2':
                    previous.extend(historical_items(result, item['id']))
                candidates, notice = recovery_candidates(result, item['id'])
                recovery.extend(candidates)
                if notice:
                    notices.append(notice)
            result = build(job_id, original['id'], messages, previous, read_only=bool(row['read_only']),
                           object_contract=True, recovery_objects=unique(recovery), recovery_notices=notices)
            # build may discard rows for either message-count or character limits.
            # History references must follow that actual retained window too.
            retained_lower = db.execute('SELECT rowid FROM jobs WHERE id=?',
                (result['messages'][0]['message_id'],)).fetchone()[0]
            checks = db.execute("""SELECT id,event_key FROM jobs WHERE kind='goal' AND scope='bank'
                AND rowid>=? AND rowid<? ORDER BY rowid""", (retained_lower, upper)).fetchall()
            result['goal_check_refs'] = [{'job_id': r['id'], 'check_id': r['event_key'].split(':',1)[1]}
                for r in checks if r['event_key'] and r['event_key'].startswith('goal:')]
            if self.handoff:
                run = db.execute('SELECT * FROM handoff_runs WHERE job_id=?',(job_id,)).fetchone()
                if run is None:
                    raise ValueError('PROTOCOL_METADATA_MISSING')
                result.update(source_contract=run['source_contract'], run_id=run['run_id'])
            return result

    def take_session(self):
        value = self.setting('chat_session_v2')
        if not value or not self.handoff or value.get('epoch') != self.handoff.epoch:
            return None
        return value['session_id'] if self.clear_setting_if_matches('chat_session_v2',value) else None

    def save_session(self, job_id, result):
        if self.handoff and result.get('ok') and result.get('session_id'):
            handle = self.handoff.for_job(job_id)
            self.set_setting('chat_session_v2',dict(session_id=result['session_id'],epoch=handle.epoch_id,last_run_id=handle.run_id))

    def recent(self, limit=30):
        with self.connection() as db:
            rows = db.execute('SELECT * FROM jobs ORDER BY created_at DESC,rowid DESC LIMIT ?', (limit,)).fetchall()
            return [self.record(row) for row in reversed(rows)]

    def setting(self, key, default=None):
        with self.connection() as db:
            row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
            return json.loads(row['value']) if row else default

    def set_setting(self, key, value):
        with self.connection() as db:
            db.execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, json.dumps(value, ensure_ascii=False)))

    def clear_setting_if_matches(self, key, expected):
        """Discard one failed session without erasing a newer concurrent setting."""
        with self.connection() as db:
            cursor = db.execute('DELETE FROM settings WHERE key=? AND value=?',
                                (key, json.dumps(expected, ensure_ascii=False)))
            return cursor.rowcount == 1
