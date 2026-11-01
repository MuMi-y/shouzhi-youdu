"""Opt-in fixture for a NEW bank database, never resets an existing bank."""
from datetime import timedelta, timezone
from pathlib import Path
from src.bank_v1 import Service
from src.bank_v1.storage import connection
from .rules import canonical


def create_demo(bank_path, *, clock):
    path = Path(bank_path)
    if path.exists(): raise FileExistsError('演示夹具只允许新数据库，禁止重置既有账本')
    service = Service(path, clock=clock)
    now = clock().astimezone(timezone(timedelta(hours=8)))
    entries = [('cloud', '虚构云存储代扣', 3000, 87), ('cloud', '虚构云存储代扣', 3000, 56),
               ('cloud', '虚构云存储代扣', 3000, 25), ('video', '虚构视频会员', 2000, 92),
               ('video', '虚构视频会员', 2000, 61), ('video', '虚构视频会员', 2500, 30)]
    with connection(path, True) as db:
        for index, (key, label, amount, days) in enumerate(entries):
            moment = now-timedelta(days=days)
            db.execute('INSERT INTO transactions VALUES(?,?,?,?,?,?,?,?,?,?)',
                (f'competition_{key}_{index}', 'A', 'expense', -amount, label,
                 moment.date().isoformat(), moment.isoformat(), None, None, 'SYNTHETIC_COMPETITION_V1'))
            db.execute("UPDATE accounts SET balance_minor=balance_minor-?,version=version+1 WHERE account_id='A'", (amount,))
        metadata = {'scenario_id': 'synthetic-competition-v1', 'source': 'SYNTHETIC_FIXTURE',
            'description': '全量虚构银行连续场景；V1期初及三个月收支+明确虚构周期扣费，非MoneyData真人账户',
            'initialized_at': now.isoformat(), 'added_debit_minor': 15500, 'initial_total_balance_minor': 584500}
        db.execute("UPDATE metadata SET value=? WHERE key='scenario'", (canonical(metadata),))
    return service
