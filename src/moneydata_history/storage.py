"""Atomic import into a separate immutable, query-only SQLite database."""
import json
import os
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
from .source import canonical, reconcile, row_digest


def connect_readonly(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    return db


def build_manifest(rows, sources, reconciliation):
    last, first = rows[0], rows[-1]
    opening = first['balance_minor'] + (first['debit_minor'] or 0) - (first['credit_minor'] or 0)
    debit = sum(x['debit_minor'] or 0 for x in rows)
    credit = sum(x['credit_minor'] or 0 for x in rows)
    return {'schema_version': 1, 'dataset': 'MoneyData / MoneyVis', 'scope': 'moneydata',
        'row_count': len(rows), 'date_min': first['transaction_date'], 'date_max': last['transaction_date'],
        'currency': None, 'amount_unit': 'source_minor', 'scale': 100, 'account_id': None,
        'dataset_id': row_digest({'sources': sources, 'schema_version': 1}), 'sources': sources,
        'normalized_content_sha256': row_digest(rows), 'reconciliation': reconciliation,
        'totals': {'count': len(rows), 'debit_minor': debit, 'credit_minor': credit, 'net_change_minor': credit-debit},
        'balance_anchors': {'opening': {'balance_minor': opening, 'date': first['transaction_date'],
            'kind': 'derived_immediately_before_first_transaction', 'source_transaction_number': first['source_transaction_number'],
            'formula': 'first_observed_balance + first_debit - first_credit'},
            'closing': {'balance_minor': last['balance_minor'], 'date': last['transaction_date'],
            'kind': 'observed_after_last_transaction', 'source_transaction_number': last['source_transaction_number']},
            'equation_matches': opening + credit - debit == last['balance_minor']},
        'quality': {'blank_categories': sum(not x['category_raw'] for x in rows),
                    'blank_transaction_types': sum(not x['transaction_type_raw'] for x in rows),
                    'missing_currency_rows': len(rows), 'missing_account_identifiers': len(rows),
                    'limitations': ['源文件无币种，不能称人民币或英镑', '无账户标识，无法证明覆盖个人全部账户',
                                    '出账不一定是消费，入账不一定是收入；未推断本人转账',
                                    '最后余额为2022年历史证据，不是今天的可用资金', '没有未来安排或提醒真值标签']},
        'attribution': {'author': 'Robert Laramee', 'title': 'Bank Transactions Dataset',
                        'doi': '10.17632/dnxtg6n4rv.1', 'url': 'https://data.mendeley.com/datasets/dnxtg6n4rv/1',
                        'license': 'CC BY 4.0', 'modifications': 'Date repair and fixed-point normalization; provenance added'}}


def prepare_history(project_root, db_path):
    root, target = Path(project_root).resolve(), Path(db_path).resolve()
    for protected in [root/'data/external', root/'data/working']:
        if target.is_relative_to(protected):
            raise ValueError('Cannot create runtime database in protected data directories')
    rows, sources, checks = reconcile(root)
    manifest = build_manifest(rows, sources, checks)
    if target.exists():
        with closing(connect_readonly(target)) as db:
            existing = json.loads(db.execute("SELECT value FROM metadata WHERE key='manifest'").fetchone()[0])
            actual = [json.loads(r[0]) for r in db.execute('SELECT record_json FROM transactions ORDER BY source_order')]
        if existing != manifest or row_digest(actual) != manifest['normalized_content_sha256']:
            raise ValueError('Existing historical database differs; use a new path, never silently reset')
        return existing
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=target.name+'.', suffix='.tmp', dir=target.parent)
    os.close(descriptor)
    temp = Path(temp_name)
    try:
        with closing(sqlite3.connect(temp)) as db:
            db.execute('CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            db.execute('CREATE TABLE transactions (source_order INTEGER PRIMARY KEY, record_json TEXT NOT NULL)')
            db.executemany('INSERT INTO transactions VALUES (?, ?)', [(x['source_order'], canonical(x)) for x in rows])
            db.execute('INSERT INTO metadata VALUES (?, ?)', ('manifest', canonical(manifest)))
            db.commit()
        # Hard-link publication is atomic and refuses to overwrite a concurrent destination.
        os.link(temp, target)
    finally:
        temp.unlink(missing_ok=True)
    return manifest
