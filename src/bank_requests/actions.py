"""Current-turn intent provenance and post-call consistency, never bank authority."""
from copy import deepcopy

from src.bank_evidence.dialogue_parts import BANK_WRITES, outcome_key
from .schema import needs_parameter_input
from .write_prohibitions import spans as write_prohibitions


KINDS = frozenset({'ANALYZE', 'PREPARE', 'CHECK_STATUS', 'UNDETERMINED'})


def normalize(action, context):
    """Do not inherit a previous turn's willingness to prepare a bank operation."""
    if action is None:
        kind, source, contract = 'UNDETERMINED', None, 'LEGACY_UNSPECIFIED'
    else:
        if type(action) is not dict or set(action) != {'kind', 'source'}:
            raise ValueError('INVALID_REQUEST_ACTION')
        kind, source = action['kind'], action['source']
        if type(kind) is not str or kind not in KINDS or type(source) is not dict or set(source) != {'message_id', 'quote'}:
            raise ValueError('INVALID_REQUEST_ACTION')
        current = next(row for row in context['messages'] if row['message_id'] == context['current_message_id'])
        quote = source['quote']
        if (source['message_id'] != current['message_id'] or type(quote) is not str or
                not 1 <= len(quote) <= 1000 or quote not in current['text']):
            raise ValueError('ACTION_REQUIRES_CURRENT_USER_SOURCE')
        source, contract = deepcopy(source), 'CURRENT_SOURCE_VALIDATED'
        if kind == 'PREPARE' and write_prohibitions(current['text']):
            raise ValueError('EXPLICIT_WRITE_PROHIBITION')
    readonly = bool(context['read_only'])
    effective = 'ANALYZE' if readonly and kind != 'CHECK_STATUS' else kind
    return {'kind': kind, 'source': source, 'effective_kind': effective,
            'host_read_only': readonly, 'contract': contract}


def check_effects(item, components, records):
    """Reject contradictory delivery; this does not undo or prevent earlier calls."""
    action = item.get('action', {})
    effective = action.get('effective_kind', 'UNDETERMINED')
    if effective == 'PREPARE' or effective == 'UNDETERMINED' and action.get('contract') == 'LEGACY_UNSPECIFIED':
        return
    covered = {key: set() for key in ('operations', 'tasks', 'drafts', 'failures')}
    for component in components:
        for key in covered:
            covered[key].update(component['covered'][key])
    for row in records:
        success = row['ok'] is True and row['status'] == 200
        data = row['response'].get('data', {})
        tool = row['tool']
        if tool in BANK_WRITES:
            belongs = (data.get('operation', {}).get('operation_id') in covered['operations'] if success
                       else outcome_key(row) in covered['failures'])
            if belongs:
                raise ValueError('REQUEST_ACTION_WRITE_CONTRADICTION')
        elif tool == 'propose_goal':
            if data.get('draft_id') in covered['drafts'] or not success and outcome_key(row) in covered['failures']:
                raise ValueError('REQUEST_ACTION_WRITE_CONTRADICTION')
        elif tool in {'submit_task_plan', 'replan_task'}:
            task = data.get('task', {})
            if task.get('task_id') in covered['tasks']:
                writes = any(node['tool'] in BANK_WRITES for node in task['nodes'])
                if effective in {'CHECK_STATUS', 'UNDETERMINED'} or writes or action.get('host_read_only'):
                    raise ValueError('REQUEST_ACTION_WRITE_CONTRADICTION')
            elif not success and outcome_key(row) in covered['failures']:
                raise ValueError('REQUEST_ACTION_WRITE_CONTRADICTION')


def project(outcome, item, *, has_operations, has_tasks, has_failures, has_analysis):
    """Choose a next step without treating complete parameters as a request to act."""
    action = item.get('action', {})
    kind = action.get('effective_kind', 'UNDETERMINED')
    outcome.update(requested_action=action.get('kind', 'UNDETERMINED'), effective_action=kind,
                   host_read_only=bool(action.get('host_read_only')),
                   action_contract=action.get('contract', 'LEGACY_UNSPECIFIED'))
    if needs_parameter_input(item) or item.get('object_issue') or has_tasks or has_failures:
        return outcome
    if has_operations:
        # Preserve bank status/receipt even if this turn merely inspects it.
        if kind == 'ANALYZE':
            outcome['next_step'] = 'REVIEW_ADVICE'
        return outcome
    if kind == 'CHECK_STATUS' and item['intent'] in {'transfer', 'mandate_query', 'mandate_cancel', 'arrangement'}:
        outcome.update(assessment='NEEDS_EVIDENCE', execution='NOT_REQUESTED', reason='STATUS_EVIDENCE_REQUIRED',
                       conclusion='', next_step='READ_CURRENT')
    elif kind == 'ANALYZE':
        outcome['execution'] = 'NOT_REQUESTED'
        if has_analysis:
            if outcome['assessment'] == 'NEEDS_EVIDENCE':
                outcome.update(assessment='ADVICE_READY', reason='ANALYSIS_PROVIDED')
            outcome['next_step'] = 'REVIEW_ADVICE'
    elif kind == 'UNDETERMINED' and outcome['next_step'] == 'PREPARE_REQUEST':
        # Old records stay readable but never silently imply a new instruction to prepare.
        outcome['next_step'] = 'REVIEW_ADVICE' if has_analysis else 'READ_CURRENT'
        if has_analysis:
            outcome.update(assessment='ADVICE_READY', reason='ANALYSIS_PROVIDED')
    return outcome
