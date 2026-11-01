"""Small shared value helpers; no process-global business state."""
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from uuid import uuid4


class DomainError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def identifier(prefix):
    return prefix + "_" + uuid4().hex


def business_date(now):
    return now.astimezone(timezone(timedelta(hours=8))).date().isoformat()


def require_date(value):
    try:
        if type(value) is not str or date.fromisoformat(value).isoformat() != value:
            raise ValueError()
    except (ValueError, TypeError):
        raise DomainError("INVALID_ARGUMENTS", "日期必须是 YYYY-MM-DD。")
    return value


def require_amount(value, zero=False):
    if type(value) is not int or value < (0 if zero else 1) or value > 100_000_000_000:
        raise DomainError("INVALID_AMOUNT", "金额必须为有效范围内的整数分。")
    return value


def row_account(db, account_id):
    row = db.execute("SELECT * FROM accounts WHERE account_id=?", (account_id,)).fetchone()
    if not row:
        raise DomainError("NOT_FOUND", "账户不存在。")
    if row["subject_id"] != "U1":
        raise DomainError("FORBIDDEN", "无权访问该账户。")
    return dict(row)


def evidence(data, now):
    return {"evidence_id": "sha256:" + hashlib.sha256(canonical(data).encode()).hexdigest(),
            "observed_at": now.isoformat(), "business_date": business_date(now),
            "currency": "CNY", "amount_unit": "minor", "data": data}
