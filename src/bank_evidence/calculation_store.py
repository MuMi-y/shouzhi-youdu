"""Append-only local calculation evidence. Never contains model-supplied results."""
import hashlib
import json
import sqlite3
from contextlib import closing

from src.bank_v1.common import canonical
from src.competition_web.money_calculate import require


def verify_envelope(envelope):
    calculation = envelope['data']['calculation']
    require(calculation.get('contract_version') in (2, 3), '只能引用有来源计算。')
    expected = 'calculation:' + hashlib.sha256(canonical(calculation).encode('utf-8')).hexdigest()
    require(envelope['evidence_id'] == expected, '计算记录完整性核验失败。', 'CALCULATION_INTEGRITY_ERROR')
    require(len(canonical(envelope).encode('utf-8')) <= 65536, '计算来源链超过大小上限。', 'CALCULATION_CHAIN_LIMIT')
    return calculation


class CalculationStore:
    def __init__(self, path):
        self.path = path
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS immutable_calculations (evidence_id TEXT PRIMARY KEY, body TEXT NOT NULL)')

    def save(self, envelope):
        verify_envelope(envelope)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('INSERT OR IGNORE INTO immutable_calculations VALUES (?, ?)',
                       (envelope['evidence_id'], canonical(envelope)))

    def read(self, identifier):
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute('SELECT body FROM immutable_calculations WHERE evidence_id=?', (identifier,)).fetchone()
        require(row is not None, '未找到本实例的受信计算，请重新计算。', 'UNKNOWN_CALCULATION')
        try:
            envelope = json.loads(row[0])
            verify_envelope(envelope)
            require(envelope['evidence_id'] == identifier, '计算编号不一致。', 'CALCULATION_INTEGRITY_ERROR')
            return envelope
        except (KeyError, TypeError, ValueError) as error:
            from src.competition_web.money_calculate import CalculationError
            if isinstance(error, CalculationError):
                raise
            raise CalculationError('CALCULATION_INTEGRITY_ERROR', '计算记录无法解析。') from None
