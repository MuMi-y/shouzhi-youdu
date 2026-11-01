"""Host-owned goal/ledger references; kept outside the public goal envelope."""
import json
from src.bank_v1 import projection_journal as journal
from src.bank_v1.common import DomainError, canonical, identifier
from . import storage


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS goal_check_bases(
        check_id TEXT PRIMARY KEY REFERENCES goal_checks(check_id), metadata_json TEXT NOT NULL)''')


def capture(goals, goal_db, bank_db, now):
    goal = goals._goal(goal_db)
    choice = storage.latest_choice(goal_db)
    operation = goals._operation(bank_db, choice, now) if choice else None
    bank = journal.verified(bank_db, now)
    arrangement = next((a for a in bank['snapshot']['arrangements']
                        if a['arrangement_id'] == goal['linked_arrangement_id']), None)
    receipt = (operation or {}).get('receipt') or {}
    linked = bool(choice and choice['state'] == 'LINKED' and operation
                  and operation['status'] == 'SUCCEEDED' and arrangement
                  and receipt.get('arrangement_id') == goal['linked_arrangement_id'])
    target = {'goal_id': 'focus', 'revision': goal['revision'], 'profile': goal['profile'],
              'arrangement': arrangement if linked else None,
              'association_token': journal.digest({'goal': goal, 'choice': choice,
                                                   'operation': operation, 'arrangement': arrangement}),
              'receipt_id': receipt.get('receipt_id') if linked else None,
              'receipt_relation': ('CURRENT' if receipt.get('version') == arrangement['version']
                                   else 'PLAN_CHANGED') if linked else 'UNLINKED'}
    fingerprint = journal.digest({'target': target, 'business_date': bank['snapshot']['business_date'],
        'accounts': [a['account_id'] for a in bank['snapshot']['accounts']['accounts']],
        'bank_instance': bank['bank_instance'], 'epoch': bank['epoch'], 'algorithm': 1})
    return {'metadata_version': 1, 'target': target, 'fingerprint': fingerprint,
            'bank_instance': bank['bank_instance'], 'epoch': bank['epoch'],
            'bank_sequence': bank['sequence'], 'tail_hash': bank['tail_hash'],
            'snapshot': bank['snapshot'], 'chain': bank['chain']}


def save(db, check_id, basis):
    previous = db.execute('SELECT metadata_json FROM goal_check_bases ORDER BY rowid DESC LIMIT 1').fetchone()
    previous = json.loads(previous[0]) if previous else None
    value = {key: val for key, val in basis.items() if key != 'chain'}
    if previous and previous['fingerprint'] == basis['fingerprint']:
        value.update({key: previous[key] for key in ('episode_id', 'baseline_check_id', 'baseline_sequence')})
    else:
        value.update(episode_id=identifier('episode'), baseline_check_id=check_id,
                     baseline_sequence=basis['bank_sequence'])
    db.execute('INSERT INTO goal_check_bases VALUES(?,?)', (check_id, canonical(value)))


def latest(db, fingerprint):
    row = db.execute('SELECT metadata_json FROM goal_check_bases ORDER BY rowid DESC LIMIT 1').fetchone()
    value = json.loads(row[0]) if row else None
    if not value or value['fingerprint'] != fingerprint:
        raise DomainError('HISTORY_UNAVAILABLE', '此目标版本没有已记录的事件前起点，不能推测历史影响。')
    row = db.execute('SELECT metadata_json FROM goal_check_bases WHERE check_id=?',
                     (value['baseline_check_id'],)).fetchone()
    baseline = json.loads(row[0]) if row else None
    if (not baseline or baseline['fingerprint'] != fingerprint
            or baseline['bank_sequence'] != value['baseline_sequence']):
        raise DomainError('HISTORY_UNAVAILABLE', '历史起点引用不完整。')
    return value, baseline
