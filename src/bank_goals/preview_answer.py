"""Bound preview claims and separate freshness checks; no arbitrary model prose."""
import json
from jsonschema import Draft202012Validator
from src.bank_v1.common import canonical
from . import preview
from .evidence import TEXT, obj

READ_ONLY = frozenset({'accounts', 'transactions', 'spending_summary', 'arrangements', 'forecast',
    'operation', 'event_context', 'billing_report', 'recurring_candidates', 'insight_status',
    'task_status', 'money_calculate', 'goal_context', 'compare_goal_options', preview.TOOL})
ANSWER = obj({'format': {'const': 'goal_preview_answer_v1'}, 'source_id': TEXT,
    'claims': {'type': 'array', 'minItems': 1, 'maxItems': 32,
               'items': obj({'path': TEXT, 'value': {}})},
    'next_step': {'const': 'REVIEW_GOAL_FORM'}})


def has_preview(result):
    return any(isinstance(row, dict) and (row.get('name') == preview.TOOL or
        row.get('tool') == preview.TOOL or (isinstance(row.get('response'), dict) and
        row['response'].get('scope') == 'goal_preview')) for row in result.get('trace', []))


def validate(result):
    validation = {'version': 'goal-preview-1', 'scope': 'goal_preview', 'gate_applied': True}
    try:
        requests, completed, source = {}, set(), None
        for row in result.get('trace', []):
            if not isinstance(row, dict):
                continue
            call_id = row.get('call_id')
            if row.get('kind') == 'request':
                if (not isinstance(call_id, str) or not call_id or call_id in requests or
                        row.get('name') not in READ_ONLY):
                    raise ValueError('PREVIEW_REQUEST_NOT_READONLY_OR_UNIQUE')
                requests[call_id] = row
            elif row.get('kind') == 'response':
                request = requests.get(call_id)
                if (not request or call_id in completed or request['name'] != row.get('tool') or
                        request.get('arguments') != row.get('args')):
                    raise ValueError('UNBOUND_PREVIEW_RESPONSE')
                completed.add(call_id)
                body = row.get('response')
                if row.get('ok') is not True:
                    # Only missing first-goal lookup may precede a valid preview.
                    if not (row.get('tool') == 'goal_context' and row.get('status') == 409 and
                            row.get('args') == {'goal_id': 'focus'} and isinstance(body, dict) and
                            body.get('error', {}).get('code') == 'GOAL_MISSING'):
                        raise ValueError('FAILED_TOOL_IN_PREVIEW')
                    continue
                if not isinstance(body, dict) or row.get('status') != 200:
                    raise ValueError('INVALID_PREVIEW_RESPONSE')
                if row['tool'] == preview.TOOL:
                    rebuilt = preview.envelope(body['data'])
                    if (canonical(body) != canonical(rebuilt) or
                            row['args'] != {'profile': body['data']['unconfirmed_profile']}):
                        raise ValueError('PREVIEW_EVIDENCE_MISMATCH')
                    source = body
                elif body.get('scope', 'bank') != 'bank':
                    raise ValueError('MIXED_GOAL_SCOPE')
        if set(requests) != completed or source is None:
            raise ValueError('INCOMPLETE_PREVIEW_TRACE')
        answer = result.get('goal_preview_answer') or json.loads(result['text'])
        Draft202012Validator(ANSWER).validate(answer)
        if answer['source_id'] != source['source_id']:
            raise ValueError('NOT_LATEST_PREVIEW')
        permitted = {c['path']: c for c in source['claim_catalog']}
        selected, displays = set(), []
        for claim in answer['claims']:
            expected = permitted[claim['path']]
            if claim['path'] in selected or canonical(claim['value']) != canonical(expected['value']):
                raise ValueError('PREVIEW_CLAIM_MISMATCH')
            selected.add(claim['path']); displays.append(expected['display'])
        # Every candidate retains amount/date and BOTH income scenarios; no selective optimism.
        if selected != set(permitted):
            raise ValueError('INCOMPLETE_PREVIEW_COMPARISON')
        return {**result, 'text': '\n'.join(displays + [
            '下一步：如果这些取舍符合你的想法，请到目标表单核对并保存；当前仅试算，尚未选择方案。']),
            'goal_preview_answer': answer,
            'goal_preview_basis': {'source_id': source['source_id'],
                                   'profile': source['data']['unconfirmed_profile']},
            'answer_validation': {**validation, 'status': 'ACCEPTED', 'source_id': source['source_id']}}
    except Exception as error:
        return rejected(result, validation, type(error).__name__)


def rejected(result, validation, reason):
    return {**result, 'ok': False, 'session_id': None,
        'error': '未确认目标试算未能与工具证据对应，未作为成功解释展示。',
        'answer_validation': {**validation, 'status': 'REJECTED', 'reason': reason}}


def recheck(result, goal_tools):
    """Revalidate the trace, then reread live facts before committing a chat answer."""
    checked = validate(result)
    if not checked.get('ok'):
        return checked
    basis = checked['goal_preview_basis']
    try:
        fresh = goal_tools.call(preview.TOOL, {'profile': basis['profile']})
        if fresh['source_id'] == basis['source_id']:
            return checked
    except Exception:
        pass  # Missing service, changed goal, invalid date, or storage failure all fail closed.
    return {**checked, 'ok': False, 'session_id': None,
        'error': '目标或账本已改变，旧试算未采用，请重新比较。',
        'answer_validation': {**checked['answer_validation'], 'status': 'STALE'}}
