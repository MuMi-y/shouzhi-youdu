"""Goal metadata only. Bank balances and receipts remain bank-owned."""
import json
from pathlib import Path
from src.bank_v1.storage import connection


def initialize(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with connection(path, True) as db:
        db.execute('''CREATE TABLE IF NOT EXISTS goals(
            goal_id TEXT PRIMARY KEY CHECK(goal_id='focus'), subject_id TEXT NOT NULL CHECK(subject_id='U1'),
            revision INTEGER NOT NULL, profile_json TEXT NOT NULL,
            linked_arrangement_id TEXT, linked_version INTEGER, updated_at TEXT NOT NULL)''')
        db.execute('''CREATE TABLE IF NOT EXISTS assessments(
            assessment_id TEXT PRIMARY KEY, goal_revision INTEGER NOT NULL,
            bank_hash TEXT NOT NULL, snapshot_json TEXT NOT NULL, result_json TEXT NOT NULL)''')
        db.execute('''CREATE TABLE IF NOT EXISTS choices(
            choice_id TEXT PRIMARY KEY, assessment_id TEXT UNIQUE NOT NULL,
            option_id TEXT NOT NULL, wait_for_income INTEGER NOT NULL,
            idempotency_key TEXT UNIQUE NOT NULL, operation_id TEXT,
            state TEXT NOT NULL, receipt_json TEXT, created_at TEXT NOT NULL)''')


def goal(db):
    row = db.execute("SELECT * FROM goals WHERE goal_id='focus' AND subject_id='U1'").fetchone()
    if not row:
        return None
    value = dict(row)
    value['profile'] = json.loads(value.pop('profile_json'))
    return value


def latest_choice(db):
    row = db.execute('SELECT * FROM choices ORDER BY rowid DESC LIMIT 1').fetchone()
    return dict(row) if row else None
