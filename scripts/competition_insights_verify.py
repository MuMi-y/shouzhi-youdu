"""Recompute demo accounting from raw SQLite, inspect outputs, and save hashes."""
from contextlib import closing
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from src.bank_insights.fixture import create_demo
from src.bank_insights import InsightService


def main():
    output=ROOT/'management/evidence/COMPETITION_V1/insights'
    now=datetime.fromisoformat('2026-10-12T10:00:00+08:00')
    with tempfile.TemporaryDirectory() as folder:
        root=Path(folder)
        bank=create_demo(root/'bank.db',clock=lambda:now)
        before=(root/'bank.db').read_bytes()
        insights=InsightService(root/'insights.db',bank=bank,clock=lambda:now)
        report=insights.call_tool('billing_report',{'start_date':'2026-07-01','end_date':'2026-10-12'})
        checked=insights.check()
        with closing(sqlite3.connect(bank.db_path)) as db:
            raw=db.execute("SELECT COUNT(*),SUM(amount_minor) FROM transactions WHERE account_id IN ('A','B')").fetchone()
            expenses=-db.execute("SELECT SUM(amount_minor) FROM transactions WHERE kind='expense'").fetchone()[0]
            balance=db.execute("SELECT SUM(balance_minor) FROM accounts WHERE subject_id='U1'").fetchone()[0]
            mandates=db.execute("SELECT COUNT(*) FROM mandates WHERE status='ACTIVE'").fetchone()[0]
        total=report['data']['report']['totals']
        checks={'full_count':raw[0]==total['count']==17,
                'ledger_reconciles':raw[1]==balance==total['net_change_minor']==584500,
                'expense_arithmetic':expenses==total['expense_minor']==2115500,
                'no_unconfirmed_notice':checked['data']['new_notifications']==[],
                'bank_unchanged':before==(root/'bank.db').read_bytes(),
                'mandate_not_changed':mandates==1}
        assert all(checks.values()),checks
        data={'checks':checks,'report':report,'initial_scan':checked}
        (output/'demo-output-review.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    files=list((ROOT/'src/bank_insights').glob('*.py'))+list((ROOT/'tests/competition_insights').glob('*.py'))
    hashes={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    (output/'source-hashes.json').write_text(json.dumps(hashes,indent=2),encoding='utf-8')
    print(json.dumps(checks))


if __name__=='__main__':main()
