"""Project authoritative evidence to user outcomes; never execute an action."""
import hashlib

from src.bank_v1.common import canonical
from src.bank_v1.balance_conditions import assess_balance
from src.bank_tasks.progress import actionable, success_receipt
from src.bank_evidence.dialogue_parts import PREPARATION_REFUSALS, error_code
from .decision import require
from .actions import project as project_action
from .schema import needs_parameter_input
from .constraints import parameters_match, evaluate as evaluate_constraints, apply_to_outcome


OPERATION_KINDS = {'transfer': 'transfer', 'mandate_query': 'mandate_query',
                   'mandate_cancel': 'mandate_cancel', 'arrangement': 'arrangement'}
INTENT_LABELS = {'account_overview': '账户查询', 'billing': '账单分析', 'transfer': '账户调拨',
    'transfer_advice': '调拨评估', 'cashflow_compare': '消费影响比较', 'cashflow_forecast': '窗口收支查询', 'subscription_review': '周期扣费核对',
    'mandate_query': '代扣登记查询', 'mandate_cancel': '撤销银行代扣', 'goal_compare': '目标方案',
    'goal_followup': '目标跟进', 'task_status': '任务进度', 'arrangement': '资金安排', 'general': '需求核对'}
NEXT = {'CLARIFY': '补充或核对上述条件。', 'READ_CURRENT': '继续查询所需的当前资料。',
    'REVISE_REQUEST': '核对不满足的条件后，再明确是否调整需求。', 'REVIEW_ADVICE': '核对分析依据和适用条件。',
    'CONFIRM_EXISTING': '在银行待确认区核对原操作，由本人确认或拒绝。',
    'VERIFY_EXISTING': '在原操作确认区完成所需独立验证。',
    'RECONCILE_ORIGINAL': '核对原操作和回执，结果明确前不要重复办理。',
    'REPLAN_UNFINISHED': '只恢复或调整尚未完成的步骤，已成功操作不要重做。',
    'WAIT_TASK': '查看任务区后续进度；出现确认卡后再独立核对。',
    'PREPARE_REQUEST': '尚未准备银行操作，需要继续办理当前请求。',
    'DONE': '保留上述结果和依据。', 'REVIEW_GOAL': '在目标区核对方案与当前保存阶段。'}


def operation_state(operation, observed):
    if operation['status'] == 'SUCCEEDED':
        require(bool(success_receipt(operation)), 'SUCCESS_WITHOUT_BANK_RECEIPT')
        return 'SUCCEEDED', 'BANK_RECEIPT', 'DONE'
    if operation['status'] == 'REJECTED':
        return 'REJECTED', 'USER_REJECTED', 'REVISE_REQUEST'
    if operation['status'] == 'PENDING':
        if not actionable(operation, observed):
            return 'EXPIRED', 'CONFIRMATION_EXPIRED', 'RECONCILE_ORIGINAL'
        if operation['required_verification'] == 'STRONG':
            return 'WAITING_VERIFICATION', 'BANK_PENDING', 'VERIFY_EXISTING'
        return 'WAITING_CONFIRMATION', 'BANK_PENDING', 'CONFIRM_EXISTING'
    return 'UNKNOWN', 'UNVERIFIED_BANK_RESULT', 'RECONCILE_ORIGINAL'


def matches_request(operation, item):
    if item.get('object_contract') == 1 and item.get('action', {}).get('effective_kind') == 'CHECK_STATUS':
        # Identity was bound in delivery. Old operations retain their own amount.
        return operation['kind'] == OPERATION_KINDS.get(item['intent'])
    return (operation['kind'] == OPERATION_KINDS.get(item['intent']) and
            parameters_match(operation['summary']['params'], item))


def fact_basis(components):
    facts, calculations = [], []
    for component in components:
        basis = component['validated'].get('bank_facts_basis', {})
        facts.extend(basis.get('facts', []))
        calculations.extend(basis.get('calculations', []))
    return facts, calculations


def shortfall(item, components):
    """Derive the condition from slots + verified tool results, not a prose tag."""
    slots = item['slots']
    if not {'source_account', 'amount_minor'} <= slots.keys():
        return None
    account, amount = slots['source_account']['value'], slots['amount_minor']['value']
    facts, calculations = fact_basis(components)
    for calc in calculations:
        for source in calc.get('input_provenance', {}).values():
            if source.get('kind') == 'bank_fact':
                facts.append(source['fact'])
    balances = [fact for fact in facts if fact['source_tool'] == 'accounts' and
                fact['entity'] == account and fact['field'] == 'balance_minor']
    if not balances or amount <= 0:
        return None
    condition = assess_balance(balances[-1]['value'], amount)
    if condition.status != 'INSUFFICIENT_FUNDS':
        return None
    from src.bank_evidence.transfer_shortfall import render
    for calc in calculations:
        sources = list(calc.get('input_provenance', {}).values())
        if not (any(source.get('kind') == 'bank_fact' and source['fact']['entity'] == account for source in sources)
                and any(source.get('kind') == 'user_assumption' and source['value_minor'] == amount for source in sources)):
            continue
        identifier = 'calculation:' + hashlib.sha256(canonical(calc).encode('utf-8')).hexdigest()
        try:
            text = render({'format': 'bank_transfer_shortfall_v1', 'calculation_id': identifier},
                          {'bank_facts_basis': {'calculations': [calc]}})
            return {'text': text, 'evidence_ref': identifier}
        except (ValueError, KeyError, TypeError):
            continue
    raise ValueError('SHORTFALL_REQUIRES_BOUND_TOOL_CALCULATION')


def project(item, components):
    formats = {component['metadata']['format'] for component in components}
    outcome = {'item_id': item['id'], 'intent': item['intent'], 'assessment': 'NEEDS_EVIDENCE',
        'execution': 'NOT_REQUESTED', 'reason': 'MORE_EVIDENCE_NEEDED', 'next_step': 'READ_CURRENT',
        'evidence_refs': [], 'conclusion': ''}
    if needs_parameter_input(item) or item.get('object_issue'):
        require('request_clarification_v1' in formats or 'bank_guidance_v1' in formats, 'MISSING_CLARIFICATION_DELIVERY')
        outcome.update(assessment='NEEDS_INPUT', reason='PARAMETERS_NEED_REVIEW', next_step='CLARIFY')
    operations, tasks, failures = [], [], []
    for component in components:
        child, meta = component['validated'], component['metadata']
        if meta['format'] == 'bank_operation_v1':
            operations.append((meta['body']['data']['operation'], meta['body']['observed_at'], meta['order']))
        if 'task_progress_basis' in child:
            progress = child.get('task_progress_refresh', {}).get('display_source', child['task_progress_basis'])['data']
            tasks.append(progress)
            operations.extend((op, progress['observed_at'], i) for i, op in enumerate(progress['operations'].values()))
        if 'attempt' in meta:
            failures.append(meta['attempt'])
        outcome['evidence_refs'].extend(component['covered']['operations'] + component['covered']['tasks'])
    expected_kind = OPERATION_KINDS.get(item['intent'])
    if expected_kind and not item.get('object_issue'):
        matching = [row for row in operations if matches_request(row[0], item)]
        if matching:
            operation, observed, _ = sorted(matching, key=lambda row: row[2])[-1]
            execution, reason, next_step = operation_state(operation, observed)
            outcome.update(assessment='ADVICE_READY', execution=execution, reason=reason, next_step=next_step)
        elif any(op['kind'] == expected_kind for op, _, _ in operations):
            raise ValueError('OPERATION_PARAMETERS_DO_NOT_MATCH_REQUEST')
        elif tasks:
            pending = [(op, observed) for op, observed, _ in operations if actionable(op, observed)]
            if pending:
                execution, _, next_step = operation_state(*pending[-1])
                outcome.update(execution=execution, reason='PREREQUISITE_CONFIRMATION', next_step=next_step)
            else:
                outcome.update(execution='NOT_PREPARED', reason='TASK_IN_PROGRESS', next_step='WAIT_TASK')
        elif operations:
            raise ValueError('UNRELATED_OPERATION_FOR_REQUEST')
        elif failures:
            attempt = failures[-1]
            code = error_code(attempt)
            if attempt['status'] == 409 and (attempt['tool'], code) in PREPARATION_REFUSALS:
                outcome.update(assessment='CONDITION_UNMET', execution='NOT_PREPARED', reason=code, next_step='REVISE_REQUEST')
            else:
                outcome.update(execution='UNKNOWN', reason='UNVERIFIED_ATTEMPT', next_step='RECONCILE_ORIGINAL')
        elif item['intent'] == 'transfer' and not (item['missing_fields'] or item['conflicts']):
            check = shortfall(item, components)
            if check:
                outcome.update(assessment='CONDITION_UNMET', execution='NOT_PREPARED', reason='INSUFFICIENT_FUNDS',
                               next_step='REVISE_REQUEST', conclusion=check['text'])
                outcome['evidence_refs'].append(check['evidence_ref'])
            else:
                outcome.update(execution='NOT_PREPARED', reason='REQUEST_NOT_PREPARED', next_step='PREPARE_REQUEST')
    elif item['intent'] == 'task_status' and not item.get('object_issue'):
        require(bool(tasks or failures), 'TASK_STATUS_EVIDENCE_REQUIRED')
        if tasks:
            outcome.update(assessment='ADVICE_READY', reason='TASK_STATUS_OBSERVED', next_step='WAIT_TASK')
        else:
            outcome.update(execution='UNKNOWN', reason='UNVERIFIED_ATTEMPT', next_step='RECONCILE_ORIGINAL')
    elif item['intent'] in {'goal_compare', 'goal_followup'}:
        for component in components:
            kind = component['metadata']['format']
            if kind == 'goal_preview_answer_v1':
                outcome.update(assessment='ADVICE_READY', reason='UNCONFIRMED_PREVIEW', next_step='REVIEW_GOAL', goal_state='PREVIEW')
            elif kind == 'goal_draft_v1':
                outcome.update(assessment='NEEDS_INPUT', reason='UNCONFIRMED_DRAFT', next_step='REVIEW_GOAL', goal_state='DRAFT')
            elif kind == 'goal_answer_v1':
                data = component['metadata']['body']['data']
                plan = data.get('current_plan')
                outcome.update(assessment='ADVICE_READY', reason='GOAL_EVIDENCE_OBSERVED', next_step='REVIEW_GOAL',
                               goal_state=plan['notice_state'] if plan else data['status'])
            elif kind == 'goal_impact_v1':
                outcome.update(assessment='ADVICE_READY', reason='GOAL_IMPACT_OBSERVED', next_step='REVIEW_GOAL')
    elif not (item['missing_fields'] or item['conflicts']):
        required = {'account_overview': {'bank_facts_v1'}, 'billing': {'bank_billing_v1'},
            'subscription_review': {'bank_insights_v1'}, 'cashflow_compare': {'bank_spending_comparison_v1', 'goal_impact_v1'},
            'transfer_advice': {'bank_facts_v1', 'bank_spending_comparison_v1', 'bank_transfer_shortfall_v1'}}
        if formats & required.get(item['intent'], set()):
            outcome.update(assessment='ADVICE_READY', reason='ANALYSIS_PROVIDED', next_step='REVIEW_ADVICE')
        if item['intent'] == 'transfer_advice':
            check = shortfall(item, components)
            if check:
                outcome.update(assessment='CONDITION_UNMET', reason='INSUFFICIENT_FUNDS', next_step='REVISE_REQUEST', conclusion=check['text'])
                outcome['evidence_refs'].append(check['evidence_ref'])
    if tasks:
        outcome['task_states'] = [{'task_id': t['task_id'], 'state': t['state']} for t in tasks]
        if any(t['state'] in {'FAILED', 'PAUSED', 'NEEDS_HUMAN'} for t in tasks):
            outcome['next_step'] = 'REPLAN_UNFINISHED'
        elif item['intent'] == 'task_status' and all(t['state'] == 'SUCCEEDED' for t in tasks):
            outcome['next_step'] = 'DONE'
        elif any(t['state'] == 'CANCELLED' for t in tasks):
            outcome['next_step'] = 'REVIEW_ADVICE'
        elif outcome['execution'] == 'SUCCEEDED' and any(t['state'] != 'SUCCEEDED' for t in tasks):
            outcome.update(reason='BANK_DONE_TASK_UNFINISHED', next_step='WAIT_TASK')
    analysis_formats = {'bank_facts_v1', 'bank_billing_v1', 'bank_spending_comparison_v1',
                        'bank_insights_v1', 'goal_answer_v1', 'goal_preview_answer_v1', 'goal_impact_v1',
                        'bank_transfer_shortfall_v1', 'bank_guidance_v1'}
    outcome = project_action(outcome, item, has_operations=bool(operations), has_tasks=bool(tasks),
                             has_failures=bool(failures), has_analysis=bool(formats & analysis_formats))
    if any(c['metadata']['format'] == 'goal_impact_v1' and
           not c['metadata']['body']['data']['coverage']['complete_for_active_dates'] for c in components):
        outcome.update(assessment='NEEDS_EVIDENCE', reason='IMPACT_COVERAGE_INCOMPLETE', next_step='READ_CURRENT')
    if any(c['metadata']['format'] == 'bank_tool_outcome_v1' and c['metadata']['attempt']['tool']
           in {'compare_goal_impact','explain_goal_changes'} for c in components):
        outcome.update(assessment='NEEDS_EVIDENCE', reason='IMPACT_NOT_ESTABLISHED', next_step='READ_CURRENT')
    outcome = apply_to_outcome(outcome, evaluate_constraints(item, components, operations, failures))
    if item['intent'] == 'cashflow_forecast' and outcome['assessment'] != 'NEEDS_INPUT':
        windows = [c['metadata']['cashflow_window'] for c in components if 'cashflow_window' in c['metadata']]
        covered = len(windows) == 1 and windows[0]['complete']
        outcome['window_coverage'] = {'complete': covered,
            'reason': windows[0]['reason'] if len(windows) == 1 else 'SELECTED_WINDOW_REQUIRED'}
        if not covered:
            outcome.update(assessment='NEEDS_EVIDENCE', reason='WINDOW_QUERY_INCOMPLETE', next_step='READ_CURRENT')
    contract = item.get('cashflow_contract', {})
    if (item['intent'] == 'cashflow_compare' and contract.get('comparison_requested')
            and ('through_date_text' in item['slots'] or contract.get('date_roles'))
            and not formats & {'bank_spending_comparison_v1', 'goal_impact_v1'}
            and outcome['assessment'] != 'NEEDS_INPUT'):
        outcome.update(assessment='NEEDS_EVIDENCE', reason='CASHFLOW_COMPARISON_REQUIRED', next_step='READ_CURRENT')
    return outcome


def render(outcome, components):
    labels = {'WAITING_CONFIRMATION': '等待本人确认', 'WAITING_VERIFICATION': '等待独立验证',
              'SUCCEEDED': '已有银行成功回执', 'REJECTED': '原操作已拒绝', 'EXPIRED': '原确认已过期',
              'UNKNOWN': '执行结果尚未核实', 'NOT_PREPARED': '尚未准备银行操作'}
    status = labels.get(outcome['execution'], {'NEEDS_INPUT': '需要核对条件', 'NEEDS_EVIDENCE': '还需核对依据',
        'CONDITION_UNMET': '当前条件不满足', 'ADVICE_READY': '已提供本轮分析'}[outcome['assessment']])
    if outcome['reason'] == 'PREREQUISITE_CONFIRMATION' or outcome.get('constraint_phase') == 'PREREQUISITE_ONLY':
        status = '前置步骤' + status + '，目标业务尚未完成'
    if outcome['reason'] == 'BANK_DONE_TASK_UNFINISHED':
        status += '，任务仍有未完成步骤'
    lines = [INTENT_LABELS[outcome['intent']] + '：' + status + '。']
    from .response import LABELS
    field_labels = dict(LABELS)
    if outcome['intent'] in {'account_overview', 'cashflow_compare'}:
        field_labels.update(source_account='支出或查询账户', target_account='对照账户')
    if outcome['intent'] == 'cashflow_compare':
        field_labels['date_text'] = '消费发生日'
    checks = outcome.get('constraint_checks', [])
    if outcome.get('window_coverage', {}).get('complete') is False:
        lines.append('本窗口资料尚未核对完整，不能将部分事实当作全部还款和收入安排。')
    if outcome['reason'] == 'CASHFLOW_COMPARISON_REQUIRED':
        lines.append('尚缺同窗口消费与不消费的完整工具比较，本轮未完成消费影响评估。')
    if (outcome['intent'] == 'cashflow_compare'
            and any(c['metadata']['format'] == 'bank_spending_comparison_v1' for c in components)
            and not any(c['field'] == 'date_text' for c in checks)):
        lines.append('未指定消费发生日，本次按工具的业务今天作假设，未把这个假设当作你的日期选择。')
    shown_periods = set()
    for check in checks:
        period = check.get('billing_resolution')
        if not period or period['coverage'] != 'AS_OF_CUTOFF':
            continue
        key = canonical({name: period[name] for name in ('business_date', 'requested_range', 'observable_range', 'coverage')})
        if key in shown_periods:
            continue
        shown_periods.add(key)
        requested, observed = period['requested_range'], period['observable_range']
        span = period.get('source_span', {})
        expression = span['expression'] if span.get('expression_kind') == 'range' else period['literal_quote']
        lines.append(f"你询问的期间「{expression}」对应{requested['start_date']}至{requested['end_date']}；"
                     f"该请求按业务日{period['business_date']}锁定，目前只统计{observed['start_date']}至{observed['end_date']}。"
                     '后续日期尚未纳入，不能作为整月或整年的完整账单；重查原期间不会自动延长这个范围。')
    if any(c['status'] == 'PREFERENCE_RECORDED' for c in checks):
        lines.append('优先事项已纳入这次目标资料；这不表示还款已履行或未来一定有足够资金，方案仅按已登记收支评估。')
    unmet = [field_labels[c['field']] for c in checks if c['status'] == 'UNMET']
    unknown = [field_labels[c['field']] for c in checks if c['status'] == 'UNVERIFIED'
               and outcome['assessment'] != 'NEEDS_INPUT']
    if unmet or unknown:
        prefix = []
        if unmet:
            prefix.append('以下要求不满足：' + '、'.join(unmet) + '。')
        if unknown:
            prefix.append('以下要求尚未核实：' + '、'.join(unknown) + '。')
        prefix.append('本轮还没有完成全部要求。')
        if outcome.get('constraint_phase') == 'PREREQUISITE_ONLY':
            prefix.append('当前只能核对并确认前置查询；后续办理条件仍需查询结果核实。')
        elif outcome['execution'] in {'WAITING_CONFIRMATION', 'WAITING_VERIFICATION'}:
            prefix.append('已有待确认操作未自动撤回；核清这些条件前不要确认。')
        lines = prefix + lines
    if outcome['conclusion'] and not any(c['metadata']['format'] == 'bank_transfer_shortfall_v1' for c in components):
        lines.append(outcome['conclusion'])
    confirmation_allowed = not (unmet or unknown) or outcome.get('constraint_phase') == 'PREREQUISITE_ONLY'
    for component in components:
        child, meta = component['validated'], component['metadata']
        if not confirmation_allowed and meta['format'] == 'bank_operation_v1':
            from src.bank_evidence.service_render import operation_view
            lines.append(operation_view(meta['body'], confirmation_allowed=False))
        elif not confirmation_allowed and meta['format'] == 'task_progress_v1':
            from src.bank_tasks.progress import render as render_task
            progress = child.get('task_progress_refresh', {}).get('display_source', child['task_progress_basis'])
            lines.append(render_task(progress, confirmation_allowed=False))
        else:
            lines.append(child['text'])
    lines.append('下一步：' + NEXT[outcome['next_step']])
    return '\n\n'.join(lines)
