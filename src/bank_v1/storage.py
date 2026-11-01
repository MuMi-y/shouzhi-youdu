"""SQLite transactions and an explicitly synthetic, reconcilable CNY fixture."""
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
import sqlite3
from .common import business_date, canonical, identifier


@contextmanager
def connection(path, write=False):
    db = sqlite3.connect(path, timeout=20, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    try:
        db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def initialize(path, now):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    try:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS accounts(account_id TEXT PRIMARY KEY,subject_id TEXT NOT NULL,
          name TEXT NOT NULL,currency TEXT NOT NULL,balance_minor INTEGER NOT NULL CHECK(balance_minor>=0),version INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS transactions(transaction_id TEXT PRIMARY KEY,account_id TEXT NOT NULL REFERENCES accounts,
          kind TEXT NOT NULL,amount_minor INTEGER NOT NULL,description TEXT NOT NULL,business_date TEXT NOT NULL,
          created_at TEXT NOT NULL,operation_id TEXT,original_transaction_id TEXT,source TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS operations(operation_id TEXT PRIMARY KEY,kind TEXT NOT NULL,subject_id TEXT NOT NULL,
          idempotency_key TEXT UNIQUE NOT NULL,params_json TEXT NOT NULL,summary_json TEXT NOT NULL,status TEXT NOT NULL,
          created_at TEXT NOT NULL,expires_at TEXT NOT NULL,receipt_json TEXT);
        CREATE TABLE IF NOT EXISTS challenges(operation_id TEXT PRIMARY KEY REFERENCES operations,answer TEXT NOT NULL,
          expires_at TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS arrangements(arrangement_id TEXT NOT NULL,version INTEGER NOT NULL,title TEXT NOT NULL,
          kind TEXT NOT NULL,amount_minor INTEGER NOT NULL,due_date TEXT NOT NULL,created_at TEXT NOT NULL,source TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'active', PRIMARY KEY(arrangement_id,version));
        CREATE TABLE IF NOT EXISTS mandates(mandate_id TEXT PRIMARY KEY,subject_id TEXT NOT NULL,name TEXT NOT NULL,
          amount_minor INTEGER NOT NULL,status TEXT NOT NULL,version INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS injections(idempotency_key TEXT PRIMARY KEY,params_json TEXT NOT NULL,result_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events(event_id TEXT PRIMARY KEY,event_key TEXT UNIQUE NOT NULL,kind TEXT NOT NULL,
          payload_json TEXT NOT NULL,created_at TEXT NOT NULL,state TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,
          analysis_text TEXT,error TEXT);
        CREATE TABLE IF NOT EXISTS notifications(notification_id TEXT PRIMARY KEY,event_id TEXT UNIQUE NOT NULL REFERENCES events,
          kind TEXT NOT NULL,message TEXT NOT NULL,created_at TEXT NOT NULL,gap_minor INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS audit(audit_id TEXT PRIMARY KEY,action TEXT NOT NULL,reference_id TEXT NOT NULL,
          created_at TEXT NOT NULL,detail_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS arrangement_matches(transaction_id TEXT PRIMARY KEY REFERENCES transactions,
          arrangement_id TEXT UNIQUE NOT NULL,arrangement_version INTEGER NOT NULL,completed_version INTEGER NOT NULL,
          operation_id TEXT NOT NULL REFERENCES operations,confirmed_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS match_rejections(transaction_id TEXT NOT NULL REFERENCES transactions,
          arrangement_id TEXT NOT NULL,expected_version INTEGER NOT NULL,rejected_at TEXT NOT NULL,
          PRIMARY KEY(transaction_id,arrangement_id,expected_version));
        ''')
        columns = {row[1] for row in db.execute("PRAGMA table_info(arrangements)")}
        if "status" not in columns:
            db.execute("ALTER TABLE arrangements ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
        notification_columns = {row[1] for row in db.execute("PRAGMA table_info(notifications)")}
        for name, kind in [("without_future_income_gap_minor", "INTEGER"),
                           ("first_gap_date", "TEXT"), ("first_without_income_gap_date", "TEXT")]:
            if name not in notification_columns:
                db.execute(f"ALTER TABLE notifications ADD COLUMN {name} {kind}")
        db.commit()
    finally:
        db.close()
    with connection(path, True) as db:
        if db.execute("SELECT 1 FROM metadata WHERE key='scenario'").fetchone():
            return
        scenario = {"scenario_id": "synthetic-cny-v1", "source": "SYNTHETIC_FIXTURE",
                    "description": "完全虚构人民币个人场景，非 MoneyData 真人账户", "initialized_at": now.isoformat()}
        db.execute("INSERT INTO metadata VALUES('scenario',?)", (canonical(scenario),))
        for account in [("A", "U1", "日常账户", "CNY", 0, 1), ("B", "U1", "储备账户", "CNY", 0, 1),
                        ("X", "U2", "隔离验证账户", "CNY", 0, 1)]:
            db.execute("INSERT INTO accounts VALUES(?,?,?,?,?,?)", account)
        # Three historical months reconcile exactly: A=500000, B=100000.
        entries = [("A", "OPENING", 200000, 92, "期初余额"), ("B", "OPENING", 100000, 92, "期初余额")]
        for days in [80, 50, 20]:
            entries += [("A", "income", 800000, days, "虚构工资到账"),
                        ("A", "expense", -500000, days-1, "虚构住房与生活支出"),
                        ("A", "expense", -200000, days-2, "虚构餐饮交通与日常支出")]
        for index, (account, kind, amount, days, desc) in enumerate(entries):
            moment = now - timedelta(days=days)
            db.execute("INSERT INTO transactions VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (f"fixture_tx_{index:02}", account, kind, amount, desc, business_date(moment), moment.isoformat(), None, None, "SYNTHETIC_FIXTURE"))
            db.execute("UPDATE accounts SET balance_minor=balance_minor+? WHERE account_id=?", (amount, account))
        for aid, title, kind, amount, days in [("rent", "固定还款", "expense", 300000, 3),
              ("salary", "预计项目收入（未到账）", "income", 250000, 7), ("laptop", "购机储蓄目标", "goal", 200000, 20)]:
            db.execute("INSERT INTO arrangements(arrangement_id,version,title,kind,amount_minor,due_date,created_at,source) VALUES(?,?,?,?,?,?,?,?)",
                       (aid, 1, title, kind, amount, business_date(now+timedelta(days=days)), now.isoformat(), "SYNTHETIC_USER_CONFIRMED"))
        db.execute("INSERT INTO mandates VALUES('M1','U1','虚构云存储代扣',3000,'ACTIVE',1)")


def audit(db, action, reference, now, detail):
    db.execute("INSERT INTO audit VALUES(?,?,?,?,?)", (identifier("audit"), action, reference, now.isoformat(), canonical(detail)))


def emit(db, key, kind, payload, now):
    existing = db.execute("SELECT event_id FROM events WHERE event_key=?", (key,)).fetchone()
    if existing:
        return existing[0]
    eid = identifier("event")
    db.execute("INSERT INTO events VALUES(?,?,?,?,?,'PENDING',0,NULL,NULL)", (eid, key, kind, canonical(payload), now.isoformat()))
    return eid
