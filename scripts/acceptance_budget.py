"""Read-only evaluation preconditions. Atomic reservations remain in the host broker."""
from pathlib import Path
from contextlib import closing
import sqlite3
import time


class BudgetError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def require_budget(path, *, max_provider=None, max_dsh=None, max_seconds=None, now_ms=None):
    if path is None:
        raise BudgetError('BATCH_BUDGET_REQUIRED')
    path = Path(path).resolve()
    if not path.is_file():
        raise BudgetError('BATCH_BUDGET_MISSING')
    try:
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute('SELECT * FROM batch').fetchall()
        expected = {'id', 'provider_limit', 'dsh_limit', 'deadline_ms', 'provider_used', 'dsh_used', 'state', 'stop_reason'}
        if len(rows) != 1 or set(rows[0].keys()) != expected:
            raise BudgetError('BATCH_BUDGET_INVALID')
        row = dict(rows[0])
        if (not isinstance(row['id'], str) or not row['id'] or
            any(type(row[k]) is not int for k in ('provider_limit', 'dsh_limit', 'deadline_ms', 'provider_used', 'dsh_used')) or
            not 1 <= row['provider_limit'] <= 400 or not 1 <= row['dsh_limit'] <= 230 or
            row['provider_used'] < 0 or row['dsh_used'] < 0):
            raise BudgetError('BATCH_BUDGET_INVALID')
    except sqlite3.Error:
        raise BudgetError('BATCH_BUDGET_INVALID') from None
    now_ms = int(time.time()*1000) if now_ms is None else now_ms
    if row['state'] != 'ACTIVE':
        raise BudgetError('BATCH_STOPPED')
    if now_ms >= row['deadline_ms']:
        raise BudgetError('BATCH_DEADLINE')
    if row['provider_used'] >= row['provider_limit'] or row['dsh_used'] >= row['dsh_limit']:
        raise BudgetError('BATCH_LIMIT')
    if ((max_provider is not None and row['provider_limit'] > max_provider) or
        (max_dsh is not None and row['dsh_limit'] > max_dsh) or
        (max_seconds is not None and row['deadline_ms'] - now_ms > max_seconds*1000)):
        raise BudgetError('BATCH_LIMIT_EXCEEDS_APPROVED_PLAN')
    return {'path': str(path), **row}


def stop_reason(path):
    if path is None:
        return None  # Non-executing library tests retain their prior optional signature.
    try:
        require_budget(path)
    except BudgetError as error:
        return error.code
    return None


def first_attempt_failed(turns):
    """Do not erase a rejected first answer when internal correction later succeeds."""
    for turn in turns:
        result = turn['result']
        if not result.get('ok'):
            return True
        for key in ('answer_repair', 'answer_retry'):
            initial = result.get(key, {}).get('initial_result')
            if initial is not None and not initial.get('ok'):
                return True
    return False
