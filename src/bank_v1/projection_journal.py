"""Bank-owned projection history. Helpers use the caller's transaction only."""
import hashlib
import json
from uuid import uuid4

from . import finance
from .common import DomainError, canonical

VERSION = 1


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf8')).hexdigest()


def facts_hash(snapshot):
    # A midnight clock tick is not an unrecorded financial mutation.
    return digest({key: value for key, value in snapshot.items() if key != 'business_date'})


def _fail():
    raise DomainError('HISTORY_UNAVAILABLE', '银行历史依据不连续，请核对当前记录；不能补造过去的变化。')


def exists(db):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='projection_journal'").fetchone() is not None


def rows(db):
    if not exists(db):
        return []
    result = []
    for row in db.execute('SELECT * FROM projection_journal ORDER BY seq'):
        value = dict(row)
        value['body'] = json.loads(value.pop('body_json'))
        result.append(value)
    return result


def _insert(db, seq, body):
    db.execute('INSERT INTO projection_journal(seq,mutation_key,body_json,record_hash) VALUES(?,?,?,?)',
               (seq, body['mutation_key'], canonical(body), digest({'seq': seq, 'body': body})))


def initialize(db, now):
    """Atomic, idempotent BASELINE; no executescript or separate commit."""
    db.execute('''CREATE TABLE IF NOT EXISTS projection_journal(
        seq INTEGER PRIMARY KEY, mutation_key TEXT UNIQUE NOT NULL,
        body_json TEXT NOT NULL, record_hash TEXT NOT NULL)''')
    if db.execute('SELECT 1 FROM projection_journal LIMIT 1').fetchone():
        return
    snapshot = finance.forecast_snapshot(db, now)
    body = {'schema_version': VERSION, 'bank_instance': str(uuid4()), 'epoch': str(uuid4()),
            'mutation_key': 'BASELINE', 'previous_hash': None, 'recorded_at': now.isoformat(),
            'before': snapshot, 'after': snapshot, 'references': {'kind': 'BASELINE'}}
    _insert(db, 0, body)


def verified(db, now):
    """Read and verify the chain plus the live tail; never silently rebase it."""
    chain = rows(db)
    if not chain:
        _fail()
    first = chain[0]['body']
    previous = None
    for index, row in enumerate(chain):
        body = row['body']
        if (row['seq'] != index or row['mutation_key'] != body['mutation_key']
                or body['schema_version'] != VERSION
                or body['bank_instance'] != first['bank_instance'] or body['epoch'] != first['epoch']
                or row['record_hash'] != digest({'seq': index, 'body': body})
                or body['previous_hash'] != (previous['record_hash'] if previous else None)):
            _fail()
        if previous and facts_hash(previous['body']['after']) != facts_hash(body['before']):
            _fail()
        if not previous and (body['mutation_key'] != 'BASELINE' or body['before'] != body['after']):
            _fail()
        previous = row
    actual = finance.forecast_snapshot(db, now)
    if facts_hash(actual) != facts_hash(chain[-1]['body']['after']):
        _fail()
    return {'bank_instance': first['bank_instance'], 'epoch': first['epoch'],
            'sequence': chain[-1]['seq'], 'tail_hash': chain[-1]['record_hash'],
            'snapshot': actual, 'chain': chain}


def capture(db, now):
    initialize(db, now)
    return verified(db, now)


def append(db, before, mutation_key, references, now):
    """A failed insert propagates and rolls back the original bank mutation."""
    head = db.execute('SELECT seq,record_hash FROM projection_journal ORDER BY seq DESC LIMIT 1').fetchone()
    if not head or head['seq'] != before['sequence'] or head['record_hash'] != before['tail_hash']:
        _fail()
    after = finance.forecast_snapshot(db, now)
    if facts_hash(before['snapshot']) == facts_hash(after):
        return before['sequence']
    body = {'schema_version': VERSION, 'bank_instance': before['bank_instance'], 'epoch': before['epoch'],
            'mutation_key': mutation_key, 'previous_hash': before['tail_hash'],
            'recorded_at': now.isoformat(), 'before': before['snapshot'], 'after': after,
            'references': references}
    _insert(db, before['sequence'] + 1, body)
    return before['sequence'] + 1
