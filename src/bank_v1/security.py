"""Trusted authentication adapter and persistent, bank-enforced security state.

No authenticator secret or grant token is exposed in a model tool or audit.
The host operator and credential file permissions remain a trust boundary.
"""
import base64
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import hmac
from pathlib import Path
import secrets
import sqlite3
import struct
from .common import DomainError, canonical, identifier
from .storage import audit, connection


@dataclass(frozen=True)
class SecurityPolicy:
    mode: str = "simulation"
    verifier: object | None = None


def totp(secret, counter, digits=6):
    """RFC6238/HOTP truncation; public utility for interoperable test vectors."""
    key = base64.b32decode(secret.upper(), casefold=True)
    digest = hmac.new(key, struct.pack('>Q', counter), hashlib.sha1).digest()
    offset = digest[-1] & 15
    number = struct.unpack('>I', digest[offset:offset+4])[0] & 0x7fffffff
    return str(number % (10 ** digits)).zfill(digits)


def enroll(path, password, base32_secret, *, provenance="USER_ENROLLED"):
    """Offline trusted enrollment only. Never overwrite an existing identity."""
    if provenance not in {"USER_ENROLLED", "TEST_FIXTURE"}:
        raise ValueError("Explicit credential provenance required")
    if type(password) is not str or not 12 <= len(password) <= 300:
        raise ValueError("Password must contain 12 to 300 characters")
    secret = base32_secret.strip().upper()
    if len(base64.b32decode(secret, casefold=True)) < 20:
        raise ValueError("TOTP secret requires at least 160 bits")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic exclusive creation prevents accidental rebinding/downgrade.
    with path.open('xb'):
        pass
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE authenticator(subject TEXT PRIMARY KEY,salt BLOB,password_hash BLOB,totp_secret TEXT,last_counter INTEGER,provenance TEXT)")
        db.execute("INSERT INTO authenticator VALUES('U1',?,?,?,?,?)", (salt, digest, secret, -1, provenance))


class TotpPasswordVerifier:
    def __init__(self, path):
        self.path = Path(path).resolve()

    def status(self):
        if not self.path.is_file():
            return {"enrolled": False, "provenance": None}
        try:
            with closing(sqlite3.connect(self.path.as_uri()+'?mode=ro', uri=True)) as db:
                row = db.execute("SELECT provenance FROM authenticator WHERE subject='U1'").fetchone()
            return {"enrolled": bool(row), "provenance": row[0] if row else None}
        except sqlite3.Error:
            return {"enrolled": False, "provenance": None}

    def verify(self, password, otp, now):
        if not self.status()["enrolled"]:
            raise DomainError("VERIFIER_UNAVAILABLE", "尚未绑定独立验证因素；不能执行红色操作。")
        with closing(sqlite3.connect(self.path, timeout=20)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT salt,password_hash,totp_secret,last_counter FROM authenticator WHERE subject='U1'").fetchone()
            actual = hashlib.scrypt(password.encode(), salt=row[0], n=16384, r=8, p=1)
            counter = int(now.timestamp()) // 30
            accepted = next((step for step in (counter, counter-1, counter+1)
                             if step > row[3] and hmac.compare_digest(totp(row[2], step), otp)), None)
            if not hmac.compare_digest(actual, row[1]) or accepted is None:
                raise DomainError("VERIFICATION_FAILED", "独立验证失败或验证码已使用。")
            db.execute("UPDATE authenticator SET last_counter=? WHERE subject='U1'", (accepted,))
        return True


def initialize_security(path, now, policy):
    if policy is not None and policy.mode not in {"simulation", "independent"}:
        raise ValueError("Unknown security mode")
    with connection(path, True) as db:
        db.execute("CREATE TABLE IF NOT EXISTS security_state(subject TEXT PRIMARY KEY,mode TEXT NOT NULL,failures INTEGER NOT NULL,window_started TEXT,locked_at TEXT,reason TEXT)")
        db.execute("CREATE TABLE IF NOT EXISTS verification_grants(token_hash TEXT PRIMARY KEY,operation_id TEXT NOT NULL,summary_hash TEXT NOT NULL,expires_at TEXT NOT NULL,consumed_at TEXT)")
        db.execute("CREATE TABLE IF NOT EXISTS security_audit(audit_id TEXT PRIMARY KEY,action TEXT,reference_id TEXT,created_at TEXT,detail_json TEXT)")
        row = db.execute("SELECT mode FROM security_state WHERE subject='U1'").fetchone()
        if not row:
            db.execute("INSERT INTO security_state VALUES('U1',?,0,NULL,NULL,NULL)", (policy.mode if policy else 'simulation',))
        elif policy and row[0] != policy.mode:
            if row[0] == 'independent':
                raise DomainError("SECURITY_DOWNGRADE_DENIED", "已启用独立验证，禁止静默降级为模拟。")
            db.execute("UPDATE security_state SET mode='independent' WHERE subject='U1'")
            security_audit(db, "SECURITY_UPGRADED", "U1", now, {})


def security_audit(db, action, reference, now, detail):
    db.execute("INSERT INTO security_audit VALUES(?,?,?,?,?)", (identifier('security'), action, reference, now.isoformat(), canonical(detail)))
    audit(db, action, reference, now, detail)


def status(db, verifier=None):
    row = db.execute("SELECT * FROM security_state WHERE subject='U1'").fetchone()
    if row is None:  # Compatibility for callers using the old low-level fixture.
        return {"mode": "simulation", "locked": False, "failures": 0}
    binding = verifier.status() if verifier else {"enrolled": False, "provenance": None}
    return {"mode": row['mode'], "locked": bool(row['locked_at']), "failures": row['failures'],
            "locked_at": row['locked_at'], "reason": row['reason'], "threshold": 3, **binding}


def assert_unlocked(db):
    if status(db)['locked']:
        raise DomainError("SECURITY_LOCKED", "安全锁定中；可以查询或拒绝，须独立验证后解除。")


def record_failure(path, code, reference, now):
    counted = code in {"FORBIDDEN", "UNKNOWN_TOOL", "SUMMARY_MISMATCH", "VERIFICATION_FAILED", "INVALID_VERIFICATION_GRANT"}
    with connection(path, True) as db:
        if counted:
            row = db.execute("SELECT * FROM security_state WHERE subject='U1'").fetchone()
            fresh = not row['window_started'] or now-datetime.fromisoformat(row['window_started']) >= timedelta(minutes=15)
            count = (0 if fresh and not row['locked_at'] else row['failures'])+1
            start = now.isoformat() if fresh else row['window_started']
            locked = row['locked_at'] or (now.isoformat() if count >= 3 else None)
            db.execute("UPDATE security_state SET failures=?,window_started=?,locked_at=?,reason=? WHERE subject='U1'", (count,start,locked,code))
            if locked and not row['locked_at']:
                db.execute("UPDATE verification_grants SET consumed_at=? WHERE consumed_at IS NULL", (now.isoformat(),))
                security_audit(db,'SECURITY_LOCKED','U1',now,{'reason':code,'failures':count})
        security_audit(db,'SECURITY_DENIED',reference,now,{'code':code,'counted':counted})


def summary_hash(summary):
    return hashlib.sha256(canonical(summary).encode()).hexdigest()


def verify_operation(db, op, args, now, verifier):
    assert_unlocked(db)
    if canonical(args['expected_summary']) != canonical(op['summary']):
        raise DomainError('SUMMARY_MISMATCH','验证内容与待办理操作不完全一致。')
    if not verifier:
        raise DomainError('VERIFIER_UNAVAILABLE','尚未配置独立验证器；不能执行红色操作。')
    verifier.verify(args['password'], args['otp'], now)
    token = secrets.token_urlsafe(32)
    expiry = min(datetime.fromisoformat(op['expires_at']), now+timedelta(minutes=2)).isoformat()
    db.execute("INSERT INTO verification_grants VALUES(?,?,?,?,NULL)", (hashlib.sha256(token.encode()).hexdigest(),op['operation_id'],summary_hash(op['summary']),expiry))
    security_audit(db,'INDEPENDENT_VERIFIED',op['operation_id'],now,{'summary_hash':summary_hash(op['summary']),'expires_at':expiry})
    return {'verification_token':token,'expires_at':expiry,'simulation_only':False,
            'credential_provenance':verifier.status()['provenance']}


def consume_grant(db, op, token, now):
    row = db.execute("SELECT * FROM verification_grants WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
    if not row or row['operation_id'] != op['operation_id'] or row['summary_hash'] != summary_hash(op['summary']) or row['consumed_at'] or now >= datetime.fromisoformat(row['expires_at']):
        raise DomainError('INVALID_VERIFICATION_GRANT','验证凭证缺失、失效、已使用或与本次操作不符。')
    db.execute("UPDATE verification_grants SET consumed_at=? WHERE token_hash=?", (now.isoformat(),row['token_hash']))
    security_audit(db,'VERIFICATION_CONSUMED',op['operation_id'],now,{'summary_hash':row['summary_hash']})


def unlock(db, args, now, verifier):
    if not verifier:
        raise DomainError('VERIFIER_UNAVAILABLE','解除锁定需要已绑定的独立验证器。')
    if not args['reason'].strip():
        raise DomainError('INVALID_ARGUMENTS','请填写解除锁定原因。')
    verifier.verify(args['password'],args['otp'],now)
    db.execute("UPDATE verification_grants SET consumed_at=? WHERE consumed_at IS NULL", (now.isoformat(),))
    db.execute("UPDATE security_state SET failures=0,window_started=NULL,locked_at=NULL,reason=NULL WHERE subject='U1'")
    # Reason is trusted UI text, not an authenticator field. Limit to schema length.
    security_audit(db,'SECURITY_UNLOCKED','U1',now,{'reason':args['reason']})
    return status(db,verifier)
