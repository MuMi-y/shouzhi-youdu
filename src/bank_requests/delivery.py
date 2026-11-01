"""Whole-request composition and freshness, separate from bank execution."""
from copy import deepcopy

from src.bank_evidence.calculation import provenance_reasons
from .decision import require, assumptions
from .components import validate as validate_component, check_coverage, records
from .outcomes import project, render, OPERATION_KINDS, parameters_match
from .actions import check_effects
from .objects import check_status_targets


def expand(answer):
    if answer.get('format') != 'bank_service_v1':
        return [answer]
    require(set(answer) == {'format', 'parts'} and type(answer['parts']) is list
            and 1 <= len(answer['parts']) <= 6, 'INVALID_LEGACY_SERVICE_COMPONENTS')
    require(all(type(part) is dict and part.get('format') != 'bank_service_v1' for part in answer['parts']),
            'NESTED_SERVICE_COMPONENTS')
    return answer['parts']


def assignments(answer, decision):
    identifiers = [item['id'] for item in decision['items']]
    if answer.get('format') == 'bank_outcomes_v1':
        require(set(answer) == {'format', 'items'} and type(answer['items']) is list, 'INVALID_OUTCOME_ASSIGNMENTS')
        result = {}
        for entry in answer['items']:
            require(type(entry) is dict and set(entry) == {'item_id', 'components'}, 'INVALID_OUTCOME_ASSIGNMENT')
            identity, parts = entry['item_id'], entry['components']
            require(type(identity) is str and identity in identifiers and identity not in result, 'UNKNOWN_OR_DUPLICATE_ITEM_DELIVERY')
            require(type(parts) is list and 1 <= len(parts) <= 8 and all(type(p) is dict for p in parts), 'INVALID_ITEM_COMPONENTS')
            result[identity] = [leaf for part in parts for leaf in expand(part)]
        require(set(result) == set(identifiers), 'OMITTED_REQUEST_ITEM')
    elif answer.get('format') == 'request_clarification_v1':
        require(set(answer) == {'format', 'item_ids'} and type(answer['item_ids']) is list and
                len(answer['item_ids']) == len(set(answer['item_ids'])) and set(answer['item_ids']) == set(identifiers),
                'CLARIFICATION_MUST_COVER_REQUEST_ITEMS')
        result = {identity: [{'format': 'request_clarification_v1', 'item_ids': [identity]}] for identity in identifiers}
    else:
        require(len(identifiers) == 1, 'MULTIPLE_ITEMS_REQUIRE_EXPLICIT_DELIVERY')
        result = {identifiers[0]: expand(answer)}
    require(sum(len(parts) for parts in result.values()) <= 30, 'TOO_MANY_COMPONENTS')
    return result


def compatible(item, component):
    kind = component['metadata']['format']
    if kind == 'goal_impact_v1':
        from src.bank_goals.impact_contract import CONTRACT
        require(item['intent'] in CONTRACT['modes'][component['metadata']['body']['mode']]['intents'],
                'COMPONENT_DOES_NOT_ADDRESS_ITEM')
    specific = {'bank_billing_v1': {'billing'}, 'bank_spending_comparison_v1': {'cashflow_compare', 'transfer_advice', 'goal_compare'},
        'bank_insights_v1': {'subscription_review'}, 'bank_transfer_shortfall_v1': {'transfer', 'transfer_advice'},
        'goal_answer_v1': {'goal_compare', 'goal_followup'}, 'goal_preview_answer_v1': {'goal_compare'},
        'goal_draft_v1': {'goal_compare'}}
    if kind in specific:
        require(item['intent'] in specific[kind], 'COMPONENT_DOES_NOT_ADDRESS_ITEM')
    if kind == 'bank_operation_v1':
        actual = component['metadata']['body']['data']['operation']['kind']
        expected = OPERATION_KINDS.get(item['intent'])
        require(actual == expected or item['intent'] == 'task_status' or
                item['intent'] in {'goal_compare', 'goal_followup'} and actual == 'arrangement', 'UNRELATED_OPERATION_FOR_ITEM')
    if kind == 'bank_tool_outcome_v1':
        tool = component['metadata']['attempt']['tool']
        expected = {'transfer': 'prepare_transfer', 'mandate_query': 'prepare_mandate_query',
                    'mandate_cancel': 'prepare_mandate_cancel', 'arrangement': 'propose_arrangement'}.get(item['intent'])
        if expected and tool in {'prepare_transfer', 'prepare_mandate_query', 'prepare_mandate_cancel', 'propose_arrangement'}:
            require(tool == expected or item['intent'] == 'mandate_cancel' and tool == 'prepare_mandate_query',
                    'UNRELATED_FAILURE_FOR_ITEM')
            if tool == expected:
                require(parameters_match(component['metadata']['attempt']['args'], item),
                        'FAILURE_PARAMETERS_DO_NOT_MATCH_REQUEST')


def compose(decision, groups):
    outcomes, texts = [], []
    for item in decision['items']:
        outcome = project(item, groups[item['id']])
        outcomes.append(outcome)
        texts.append(render(outcome, groups[item['id']]))
    return outcomes, '\n\n'.join(texts)


def validate(answer, decision, context, trace):
    all_records = records(trace)
    require(not provenance_reasons(all_records, assumptions(decision, context)), 'UNBOUND_USER_CALCULATION_ASSUMPTION')
    assigned = assignments(answer, decision)
    groups = {}
    for item in decision['items']:
        scope = assumptions({'items': [item]}, context)
        components = [validate_component(part, trace, scope, item) for part in assigned[item['id']]]
        for component in components:
            compatible(item, component)
        check_effects(item, components, all_records)
        check_status_targets(item, components)
        groups[item['id']] = components
    host = context.get('_host_attempt_coverage')
    if context.get('source_contract') == 'minimal_source_v2':
        require(host is not None and host['complete'], 'ATTEMPT_HISTORY_INCOMPLETE')
    check_coverage(trace, [component for group in groups.values() for component in group],
                   host_failure_coverage=host['failure_coverage'] if host and host['complete'] else ())
    from src.bank_goals.impact_contract import enforce
    enforce(context.get('goal_impact_requirement'), [c for group in groups.values() for c in group])
    if context.get('goal_impact_requirement'):
        for group in groups.values():
            for c in group:
                if c['metadata']['format'] == 'goal_impact_v1':
                    target = c['metadata']['body']['data']['target']
                    require(target['profile'] == context['goal_impact_target']['profile'], 'IMPACT_TARGET_CHANGED')
                    body = c['metadata']['body']
                    if body['mode'] == 'WHAT_IF':
                        expected = context.get('goal_impact_context', {}).get('what_if_inputs', {})
                        require(all(expected.get(k) == body['arguments'][k] for k in ('amount_minor','due_date')),
                                'IMPACT_INPUT_NOT_BOUND')
    outcomes, text = compose(decision, groups)
    return {'ok': True, 'text': text, 'business_outcomes': outcomes,
            'request_delivery': {'version': 1, 'decision': decision, 'groups': groups}}


def recheck(result, service):
    """Read authoritative sources and recompute projections; never replay writes."""
    from src.bank_v1_web.display_check import check
    delivery = deepcopy(result['request_delivery'])
    try:
        for components in delivery['groups'].values():
            for component in components:
                child = check(component['validated'], service)
                require(child.get('ok') is True, 'STALE_OR_UNAVAILABLE_COMPONENT')
                component['validated'] = child
        outcomes, text = compose(delivery['decision'], delivery['groups'])
        for outcome in outcomes:
            if outcome['intent'] == 'goal_followup' and getattr(service, 'goal_followup', None) is not None:
                health = service.goal_followup.state()['service_health']
                outcome['service_health'] = health
                state = health['state']
                if state in {'NEEDS_RECOVERY', 'RECOVERY_EXHAUSTED', 'PENDING', 'RUNNING', 'NO_MODEL'}:
                    text += '\n\n持续服务：最近分析尚未完成或服务尚未就绪；当前是否安静与分析是否完成分别记录。'
                outcome['health_observer'] = 'SYSTEM_READ_ONLY'
        return {**result, 'text': text, 'business_outcomes': outcomes, 'request_delivery': delivery,
                'delivery_freshness': {'status': 'CURRENT_AT_DISPLAY_CHECK', 'actor': 'SYSTEM_READ_ONLY'}}
    except Exception as error:
        return {**result, 'ok': False, 'session_id': None,
                'error': '业务依据在展示前发生变化或无法核对；旧结论未采用，请查看原操作与回执。',
                'delivery_freshness': {'status': 'STALE_OR_UNAVAILABLE', 'reason': type(error).__name__},
                'answer_validation': {**result.get('answer_validation', {}), 'status': 'REJECTED'}}
