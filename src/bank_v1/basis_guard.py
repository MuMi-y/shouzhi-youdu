"""Optional bank-owned projection guards, checked inside confirmation's transaction."""
from .common import DomainError
from .finance import forecast_snapshot, snapshot_hash


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS operation_basis_guards(
        operation_id TEXT PRIMARY KEY REFERENCES operations,
        basis_hash TEXT NOT NULL, choice_id TEXT NOT NULL)''')


def bind(db, operation_id, basis_hash, choice_id):
    db.execute('INSERT INTO operation_basis_guards VALUES(?,?,?)',
               (operation_id, basis_hash, choice_id))


def assert_current(db, operation_id, now):
    # Older databases have no guards. Existing interfaces and operations retain their behavior.
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='operation_basis_guards'").fetchone():
        return
    guard = db.execute('SELECT basis_hash FROM operation_basis_guards WHERE operation_id=?',
                       (operation_id,)).fetchone()
    if guard and snapshot_hash(forecast_snapshot(db, now)) != guard['basis_hash']:
        raise DomainError('STALE_GOAL_BASIS', '资金或安排依据已变化，请拒绝旧卡，重新比较并选择方案。')
