"""Bind 'just now' to actual host job references, never model prose or last-N money."""
import json
from src.bank_v1.common import DomainError
from src.bank_v1.storage import connection
from src.bank_v1.projection_journal import digest
from . import impact, impact_basis


def for_request(goals, context):
    basis = impact.capture(goals)
    value = {'basis': impact.token(basis), 'history_range': None,
             'request_id': context['request_id'], 'message_id': context['current_message_id']}
    current = next(m['text'] for m in context['messages'] if m['message_id'] == context['current_message_id'])
    value['original_text_hash'] = digest(current)
    from .impact_contract import what_if_inputs
    value['what_if_inputs'] = what_if_inputs(current, basis['snapshot']['business_date'])
    refs = context.get('goal_check_refs', [])
    try:
        with connection(goals.db_path) as db:
            anchor, baseline = impact_basis.latest(db, basis['fingerprint'])
            visible = []
            for ref in refs:
                row = db.execute('SELECT metadata_json FROM goal_check_bases WHERE check_id=?', (ref['check_id'],)).fetchone()
                if row:
                    item = json.loads(row[0])
                    if item['fingerprint'] == basis['fingerprint']:
                        visible.append((ref['check_id'], item['bank_sequence']))
        # Baseline and final check are host-generated jobs in the retained conversation.
        # A failed analysis still locates its check; it does not claim that the user read it.
        if (anchor['baseline_check_id'] in {i for i, _ in visible}
                and any(seq == basis['bank_sequence'] for _, seq in visible)):
            value['history_range'] = {'anchor_id': anchor['baseline_check_id'],
                'start_seq': baseline['bank_sequence'], 'end_seq': basis['bank_sequence'],
                'host_check_refs': refs}
    except DomainError:
        pass
    value['range_ref'] = digest(value)
    return value
