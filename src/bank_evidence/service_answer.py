"""Compose existing evidence contracts with typed bank operation/event views."""
import json

from datetime import datetime
from src.bank_v1.common import canonical, evidence
from .answer import paired_records
from .facts import SUPPORTED_TOOLS
from .service_render import operation_view, event_view, snapshot_state
from .dialogue_parts import failed_write, outcome_key, outcome_part, guidance_part
from .insight_parts import INSIGHT_TOOLS, insight_part


FORMAT = 'bank_service_v1'
TRIGGERS = {'operation', 'event_context', 'prepare_transfer', 'prepare_mandate_query',
            'prepare_mandate_cancel', 'propose_arrangement', 'submit_task_plan', 'replan_task'}
FACT_TOOLS = SUPPORTED_TOOLS | {'money_calculate'}


def requested(result):
    try:
        return json.loads(result.get('text', '')).get('format') == FORMAT
    except (ValueError, AttributeError):
        return False


def required(result):
    return requires_outcomes(result) or requires_insights(result) or any(r.get('kind') == 'response' and r.get('ok') is True and r.get('tool') in TRIGGERS
               for r in result.get('trace', []) if isinstance(r, dict))


def requires_outcomes(result):
    return any(failed_write(row) for row in result.get('trace', []) if isinstance(row, dict))


def requires_insights(result):
    return any(row.get('kind') == 'response' and row.get('ok') is True and row.get('tool') in INSIGHT_TOOLS
               for row in result.get('trace', []) if isinstance(row, dict))


def subset_trace(trace, names):
    return [r for r in trace if isinstance(r, dict) and
            (r.get('kind') == 'request' and r.get('name') in names or
             r.get('kind') == 'response' and r.get('tool') in names)]


def rejection(result, reason):
    return {**result, 'ok': False, 'session_id': None,
            'error': '业务回答未能对应本轮最新银行依据；已有操作请核对银行待办、任务区和原回执，不要重复办理。',
            'answer_validation': {'version': FORMAT, 'scope': 'bank_service', 'status': 'REJECTED',
                                  'gate_applied': True, 'reason': reason}}


def object_part(part, records):
    tool, key, render = {'bank_operation_v1': ('operation', 'operation_id', operation_view),
                         'bank_event_v1': ('event_context', 'event_id', event_view)}[part['format']]
    if set(part) != {'format', key, 'evidence_id'}:
        raise ValueError('INVALID_SERVICE_PART')
    matches = [r for r in records if r['tool'] == tool and r['args'] == {key: part[key]}]
    if not matches or matches[-1]['response']['evidence_id'] != part['evidence_id']:
        raise ValueError('NOT_LATEST_OBJECT_READ')
    row = matches[-1]
    body = row['response']
    if body['evidence_id'] != evidence(body['data'], datetime.fromisoformat(body['observed_at']))['evidence_id']:
        raise ValueError('INVALID_SOURCE_HASH')
    if tool == 'operation' and body['data']['operation']['operation_id'] != part[key]:
        raise ValueError('WRONG_OPERATION')
    if tool == 'event_context' and body['data']['event']['event_id'] != part[key]:
        raise ValueError('WRONG_EVENT')
    return {'text': render(body), 'snapshots': [{'tool': tool, 'arguments': row['args'],
              'state': snapshot_state(tool, body)}], 'operation_ids': [part[key]] if tool == 'operation' else []}


def validate(result, prompt):
    from .answer import validate as validate_facts
    from .billing_answer import validate as validate_billing
    from .spending_answer import validate as validate_spending
    from src.bank_v1_web.task_progress_answer import validate as validate_progress
    try:
        answer = json.loads(result['text'])
        if (set(answer) != {'format', 'parts'} or answer['format'] != FORMAT or
                type(answer['parts']) is not list or not 1 <= len(answer['parts']) <= 6):
            raise ValueError('INVALID_SERVICE_ANSWER')
        all_records = paired_records(result.get('trace', []), include_failures=True)
        records = [row for row in all_records if row.get('ok') is True and row.get('status') == 200]
        texts, children, snapshots, represented_tasks, represented_operations = [], [], [], set(), set()
        represented_outcomes, conclusions = set(), []
        represented_insights = set()
        seen = set()
        validators = {'bank_facts_v1': validate_facts, 'bank_billing_v1': validate_billing,
                      'bank_spending_comparison_v1': validate_spending}
        for part in answer['parts']:
            if type(part) is not dict or canonical(part) in seen:
                raise ValueError('INVALID_OR_DUPLICATE_PART')
            seen.add(canonical(part))
            kind = part.get('format')
            if kind in validators:
                child = validators[kind]({'ok': True, 'text': json.dumps(part),
                         'trace': subset_trace(result['trace'], FACT_TOOLS)}, prompt)
                if not child.get('ok'):
                    raise ValueError('INVALID_FACT_COMPONENT')
                children.append(child)
                texts.append(child['text'])
            elif kind == 'bank_transfer_shortfall_v1':
                from .transfer_shortfall import render as render_shortfall
                child = validate_facts({'ok': True, 'text': json.dumps({
                    'format': 'bank_facts_v1', 'fact_ids': [],
                    'calculation_ids': [part['calculation_id']]}),
                    'trace': subset_trace(result['trace'], FACT_TOOLS)}, prompt)
                if not child.get('ok'):
                    raise ValueError('INVALID_SHORTFALL_SOURCE')
                conclusions.append(render_shortfall(part, child))
                children.append(child)
            elif kind == 'task_progress_v1':
                child = validate_progress({'ok': True, 'text': json.dumps(part), 'trace': subset_trace(
                    result['trace'], {'task_status', 'submit_task_plan', 'replan_task'})})
                if not child.get('ok'):
                    raise ValueError('INVALID_TASK_COMPONENT')
                texts.append(child['text'])
                represented_tasks.add(part['task_id'])
                progress = child['task_progress_basis']
                represented_operations.update(progress['data']['operations'])
                snapshots.append({'tool': 'task_status', 'arguments': {'task_id': part['task_id']},
                                  'state': snapshot_state('task_status', {'data': {'task_progress': progress}})})
            elif kind in {'bank_operation_v1', 'bank_event_v1'}:
                view = object_part(part, records)
                texts.append(view['text']); snapshots.extend(view['snapshots'])
                represented_operations.update(view['operation_ids'])
            elif kind == 'bank_guidance_v1':
                view = guidance_part(part, records)
                conclusions.append(view['text'])
                snapshots.extend(view['snapshots'])
            elif kind == 'bank_tool_outcome_v1':
                view = outcome_part(part, all_records)
                conclusions.append(view['text'])
                represented_outcomes.add(view['outcome_key'])
            elif kind == 'bank_insights_v1':
                view = insight_part(part, records)
                texts.append(view['text'])
                snapshots.extend(view['snapshots'])
                represented_insights.add(view['source_tool'])
            else:
                raise ValueError('UNKNOWN_COMPONENT')
        # Failed direct writes are historical attempts, never replayed for freshness.
        for row in all_records:
            if failed_write(row) and outcome_key(row) not in represented_outcomes:
                raise ValueError('OMITTED_BANK_TOOL_FAILURE')
        if requires_insights(result) and not represented_insights:
            raise ValueError('OMITTED_INSIGHT_RESULT')
        if any(row['tool'] == 'check_insights' for row in records) and 'check_insights' not in represented_insights:
            raise ValueError('OMITTED_INSIGHT_CHECK')
        # Successful admissions cannot disappear behind an unrelated valid part.
        for row in records:
            data = row['response'].get('data', {})
            if row['tool'] in {'submit_task_plan', 'replan_task'}:
                if data['task']['task_id'] not in represented_tasks:
                    raise ValueError('OMITTED_CHANGED_TASK')
            if row['tool'] in {'prepare_transfer', 'prepare_mandate_query', 'prepare_mandate_cancel', 'propose_arrangement'}:
                if data['operation']['operation_id'] not in represented_operations:
                    raise ValueError('OMITTED_PREPARED_OPERATION')
        return {**result, 'text': '\n\n'.join(conclusions + texts), 'bank_service_answer': answer,
                'bank_service_basis': {'children': children, 'snapshots': snapshots},
                'answer_validation': {'version': FORMAT, 'scope': 'bank_service', 'status': 'ACCEPTED',
                    'gate_applied': True, 'limitation': '引用的业务事实已绑定；计划是否充分解决问题仍须效果验收。'}}
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        return rejection(result, str(error))


def recheck(result, read_tool):
    from .freshness import recheck as recheck_facts
    try:
        for child in result['bank_service_basis']['children']:
            if not recheck_facts(child, read_tool).get('ok'):
                raise ValueError('CHANGED_FACT_COMPONENT')
        for old in result['bank_service_basis']['snapshots']:
            fresh = read_tool(old['tool'], old['arguments'])
            if snapshot_state(old['tool'], fresh) != old['state']:
                raise ValueError('CHANGED_SERVICE_COMPONENT')
        return {**result, 'service_freshness': {'status': 'CURRENT_AT_DISPLAY_CHECK', 'actor': 'SYSTEM_READ_ONLY'}}
    except Exception as error:
        checked = rejection(result, type(error).__name__)
        return {**checked, 'service_freshness': {'status': 'STALE_OR_UNAVAILABLE'}}
