"""Host-owned object identities, independent of user slots and current status.

References locate records. Only this turn's validated tool reads prove status.
Failed deliveries contribute unassociated recovery candidates, never authority.
"""
from copy import deepcopy
import hashlib
import re

from src.bank_v1.common import canonical
from .schema import OBJECT_KINDS, DATE_FIELDS


def identity(value):
    return value['kind'], value['object_id']


def reference(kind, identifier, origin, tool, source, **extra):
    return {'kind': kind, 'object_id': identifier, 'origin_request_ref': origin,
            'provenance': {'tool': tool, 'source_id': source}, **extra}


def component_objects(component, origin):
    """Called only for components that passed their own evidence validator."""
    meta, child, part = component['metadata'], component['validated'], component['part']
    kind = meta['format']
    if kind == 'bank_operation_v1':
        body = meta['body']
        return [reference('operation', body['data']['operation']['operation_id'], origin,
                          'operation', body['evidence_id'])]
    if kind == 'task_progress_v1':
        basis = child.get('task_progress_refresh', {}).get('display_source', child['task_progress_basis'])
        data = basis['data']
        values = [reference('task', data['task_id'], origin, 'task_status', basis['source_id'])]
        values.extend(reference('operation', identifier, origin, 'task_status', basis['source_id'],
                                via_task_id=data['task_id']) for identifier in data['operations'])
        return values
    if kind == 'goal_answer_v1':
        body = meta['body']
        return [reference('goal', body['data']['goal_id'], origin, 'goal_context', body['source_id'])]
    if kind == 'goal_impact_v1':
        body = meta['body']
        return [reference('goal', 'focus', origin, body['tool'], body['source_id'])]
    if kind == 'goal_draft_v1':
        body = meta.get('body')
        if not body:
            return []  # Old deliveries did not retain draft source metadata.
        return [reference('goal_draft', part['draft_id'], origin, 'propose_goal',
                          hashlib.sha256(canonical(body).encode()).hexdigest())]
    return []


def unique(values):
    return list({identity(value): value for value in values}.values())


def historical_items(result, job_id):
    """Validated parameters can survive failed delivery; new object links cannot."""
    decision = result.get('request_decision', {})
    validation = result.get('request_decision_validation', {})
    if (validation.get('version') != 1 or validation.get('status') != 'SOURCES_VALIDATED'
            or decision.get('request_id') != job_id):
        return []
    delivery = result.get('request_delivery', {})
    accepted = (result.get('child_validation', {}).get('status') == 'ACCEPTED'
                and delivery.get('version') == 1 and delivery.get('decision') == decision)
    output = []
    for item in decision['items']:
        ref = job_id + ':' + item['id']
        objects = []
        if accepted:
            for component in delivery['groups'].get(item['id'], []):
                objects.extend(component_objects(component, ref))
        # These inherited identities were supplied by the host, not model slots.
        linked = unique(objects) or deepcopy(item.get('related_objects', []))
        anchors = deepcopy(item.get('constraint_anchors', {}))
        if accepted:
            for outcome in result.get('business_outcomes', []):
                if outcome['item_id'] != item['id']:
                    continue
                for check in outcome.get('constraint_checks', []):
                    if (check['field'] in DATE_FIELDS | {'period_text'} and check['status'] == 'BOUND'
                            and 'canonical_value' in check):
                        # Result proof must not replace the source role and its old clock.
                        anchors[check['field']] = ({**anchors.get(check['field'], {}), **deepcopy(check)}
                            if item.get('cashflow_contract') else deepcopy(check))
        historical = {**item, 'reference': ref, 'related_objects': linked, 'constraint_anchors': anchors}
        historical.pop('goal_preview_origin', None)
        if (accepted and result.get('ok') is True
                and result.get('answer_validation', {}).get('status') == 'ACCEPTED'):
            from .goal_continuation import accepted_origin
            origin = accepted_origin(item, delivery['groups'].get(item['id'], []), ref, anchors)
            if origin:
                historical['goal_preview_origin'] = origin
        output.append(historical)
    return output


def recovery_candidates(result, job_id):
    """Only paired host tool records; never parse IDs from model prose."""
    if result.get('child_validation', {}).get('status') == 'ACCEPTED':
        return [], None
    from .components import records
    try:
        rows = records(result.get('trace', []))
    except (ValueError, KeyError, TypeError):
        return [], {'request_id': job_id, 'reason': 'INCOMPLETE_TOOL_TRACE_CHECK_BANK_RECORDS'}
    candidates = []
    for row in rows:
        if row['ok'] is not True or row['status'] != 200:
            continue
        body, tool = row['response'], row['tool']
        data = body.get('data', {})
        source = body.get('source_id') or body.get('evidence_id') or hashlib.sha256(canonical(body).encode()).hexdigest()
        values = []
        if tool in {'operation', 'prepare_transfer', 'prepare_mandate_query', 'prepare_mandate_cancel', 'propose_arrangement'}:
            values = [('operation', data.get('operation', {}).get('operation_id'))]
        elif tool in {'submit_task_plan', 'replan_task', 'task_status'}:
            task = data.get('task', {})
            values = [('task', task.get('task_id'))]
        elif tool == 'propose_goal':
            values = [('goal_draft', data.get('draft_id'))]
        for kind, identifier in values:
            if isinstance(identifier, str) and identifier:
                candidates.append(reference(kind, identifier, job_id, tool, source,
                                            confidence='TOOL_TRACE_CANDIDATE', requires_current_read=True))
    notice = {'request_id': job_id, 'reason': 'FAILED_DELIVERY_CHECK_ORIGINAL_RECORDS'} if not result.get('ok') else None
    return unique(candidates), notice


def status_context(item, parent, context):
    related = deepcopy(parent.get('related_objects', [])) if parent else []
    recovery = [value for value in context.get('recovery_objects', []) if not parent or
                value['origin_request_ref'] == parent['reference'].split(':', 1)[0]]
    available = related or deepcopy(recovery)
    current = next(row['text'] for row in context['messages'] if row['message_id'] == context['current_message_id'])
    # Literal IDs are supported for compatibility, never required of the user.
    pattern = r'(?<![A-Za-z0-9_-])(?:op_|task_|draft_)[A-Za-z0-9_-]+(?![A-Za-z0-9_-])'
    explicit = re.findall(pattern, (item['action'].get('source') or {}).get('quote', '')) or re.findall(pattern, current)
    expected = OBJECT_KINDS.get(item['intent'], ())
    candidates = [value for value in available if value['kind'] in expected and not value.get('via_task_id')]
    issue = None
    if item['action']['effective_kind'] == 'CHECK_STATUS' and expected:
        if candidates and all(value['kind'] == 'goal_draft' for value in candidates):
            issue = 'DRAFT_STATUS_TOOL_UNAVAILABLE'
        elif not candidates and not explicit and (parent or item['intent'] not in {'goal_compare', 'goal_followup'}):
            issue = 'OBJECT_NOT_IDENTIFIED'
        elif len(set(explicit)) > 1 or (any(sum(value['kind'] == kind for value in candidates) > 1
                                          for kind in expected) and not explicit):
            issue = 'AMBIGUOUS_OBJECT'
    return {'object_contract': 1, 'related_objects': related, 'recovery_objects': deepcopy(recovery),
            'explicit_object_ids': explicit, 'object_issue': issue}


def check_status_targets(item, components):
    """Bind current reads to the selected request; equal parameters are not identity."""
    if item.get('object_contract') != 1 or item['action']['effective_kind'] != 'CHECK_STATUS':
        return
    expected = OBJECT_KINDS.get(item['intent'], ())
    if not expected:
        return
    if any(c['metadata']['format'] == 'request_clarification_v1' for c in components):
        if any(component_objects(c, '') for c in components):
            raise ValueError('AMBIGUOUS_OBJECT_CANNOT_BE_PRESENTED_AS_SELECTED')
        return
    if item.get('object_issue'):
        raise ValueError(item['object_issue'])
    available = item['related_objects'] or item['recovery_objects']
    known = {identity(value) for value in available}
    selected = [value for c in components for value in component_objects(c, '') if not value.get('via_task_id')]
    if not selected:
        # A failed read is legitimate evidence of an unknown result, not success.
        for c in components:
            attempt = c['metadata'].get('attempt', {})
            mapping = {'operation': ('operation', 'operation_id'), 'task_status': ('task', 'task_id'),
                       'goal_context': ('goal', 'goal_id')}
            if attempt.get('tool') in mapping:
                kind, field = mapping[attempt['tool']]
                selected.append({'kind': kind, 'object_id': attempt['args'].get(field)})
    if not selected:
        raise ValueError('STATUS_OBJECT_EVIDENCE_REQUIRED')
    for value in selected:
        if value['kind'] not in expected:
            raise ValueError('WRONG_STATUS_OBJECT_TYPE')
        if item['explicit_object_ids'] and value['kind'] != 'goal' and value['object_id'] not in item['explicit_object_ids']:
            raise ValueError('STATUS_OBJECT_DIFFERS_FROM_EXPLICIT_SELECTION')
        implicit_focus = value['kind'] == 'goal' and value['object_id'] == 'focus' and not available
        if (identity(value) not in known and value['object_id'] not in item['explicit_object_ids'] and not implicit_focus):
            raise ValueError('STATUS_OBJECT_NOT_ASSOCIATED_WITH_REQUEST')


def clarification_text(item):
    return {
        'OBJECT_NOT_IDENTIFIED': '尚不能确定要核对哪次办理。请说明是哪项业务或哪次请求；不需要提供内部编号。',
        'AMBIGUOUS_OBJECT': '这项请求关联了多笔记录。请说明要核对哪一次办理，当前没有替你选定其中一笔。',
        'DRAFT_STATUS_TOOL_UNAVAILABLE': '原记录是未确认的目标草案。当前工具不能查询该草案的采用状态，请在目标区核对；不能用已确认目标的状态代替草案结果。',
    }.get(item.get('object_issue'))
