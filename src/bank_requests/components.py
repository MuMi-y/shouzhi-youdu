"""Explicit adapters for existing evidence contracts, plus whole-turn coverage."""
import json

from src.bank_evidence.answer import paired_records
from src.bank_evidence.facts import SUPPORTED_TOOLS
from src.bank_evidence.service_answer import subset_trace, object_part
from src.bank_evidence.dialogue_parts import outcome_part, outcome_key, failed_write, error_code, guidance_part, BANK_WRITES
from src.bank_evidence.insight_parts import insight_part, INSIGHT_TOOLS
from src.bank_v1.common import canonical
from .decision import require, validate_questions


SCOPES = frozenset({'bank', 'goal_plan', 'goal_preview', 'goal_draft', 'goal_impact'})
FACT_TOOLS = SUPPORTED_TOOLS | {'money_calculate'}
MUTATIONS = BANK_WRITES | {'submit_task_plan', 'replan_task', 'propose_goal'}


def records(trace):
    return paired_records(trace, include_failures=True, allowed_scopes=SCOPES)


def selected_trace(trace, predicate):
    chosen = {row['call_id'] for row in records(trace) if predicate(row)}
    return [row for row in trace if isinstance(row, dict) and row.get('call_id') in chosen
            and row.get('kind') in {'request', 'response'}]


def child_input(part, trace):
    return {'ok': True, 'text': json.dumps(part, ensure_ascii=False), 'trace': trace}


def view_child(view):
    return {'ok': True, 'text': view['text'], 'bank_service_basis': {
        'children': [], 'snapshots': view.get('snapshots', [])}}


def forecast_record(row):
    from .normalization import verified_business_day
    require(verified_business_day(row) == row['response']['data']['as_of_date'], 'FORECAST_BUSINESS_DAY_MISMATCH')
    return {'call_id': row['call_id'], 'arguments': row['args'],
            'data': row['response']['data'], 'evidence_id': row['response']['evidence_id']}


def query_window(child, rows):
    """Only selected facts bind the forecast; empty arrangements need a snapshot.

    Timing classifications come from the existing bank contract. The snapshots
    are in the child result so display_check rechecks even empty/excluded rows.
    """
    from .normalization import verified_business_day
    from src.bank_v1.finance import arrangement_timing
    basis = child['bank_facts_basis']; facts = basis['facts']
    selected = [f for f in facts if f['source_tool'] == 'forecast']
    ids = {(f['evidence_id'], canonical(f['source_arguments'])) for f in selected}
    if not ids:
        return {'complete': False, 'reason': 'SELECTED_WINDOW_REQUIRED'}
    require(len(ids) == 1, 'QUERY_MIXED_FORECAST_WINDOWS')
    identity, arguments = next(iter(ids))
    candidates = [r for r in rows if r['tool'] == 'forecast' and canonical(r['args']) == arguments]
    require(bool(candidates) and candidates[-1]['response']['evidence_id'] == identity, 'QUERY_FORECAST_NOT_CURRENT')
    row = candidates[-1]; forecast = forecast_record(row)
    require(row['args'].get('proposed_expense_minor', 0) == 0, 'QUERY_HAS_PROPOSED_CONSUMPTION')
    require(row['args'].get('plan_scope', 'window') == 'window', 'FORECAST_CUTOFF_REQUIRES_WINDOW')
    data = forecast['data']
    require(all(f['period']['start'] == data['as_of_date'] and f['period']['end'] == data['through_date']
                and f['business_date'] == data['as_of_date'] for f in selected), 'QUERY_FACT_WINDOW_MISMATCH')
    arrangements = [r for r in rows if r['tool'] == 'arrangements' and r['args'] == {}]
    if not arrangements:
        return {'complete': False, 'reason': 'CURRENT_ARRANGEMENTS_REQUIRED', 'forecast': forecast}
    arrangement = arrangements[-1]; body = arrangement['response']
    require(verified_business_day(arrangement) == data['as_of_date'], 'QUERY_ARRANGEMENTS_DAY_MISMATCH')
    records = body['data']['arrangements']
    timing = data['arrangement_timing']
    require(timing == arrangement_timing(records, data['as_of_date'], data['through_date']), 'QUERY_ARRANGEMENT_TIMING_MISMATCH')
    selected_arrangements = [f for f in facts if f['source_tool'] == 'arrangements']
    require(all(f['evidence_id'] == body['evidence_id'] and f['source_arguments'] == {}
                for f in selected_arrangements), 'QUERY_ARRANGEMENT_VERSION_MISMATCH')
    required = {(t['arrangement_id'], field) for t in timing['items'] if t['included_in_forecast']
                for field in ('amount_minor', 'due_date')}
    present = {(f['entity'], f['field']) for f in selected_arrangements}
    excluded = {t['arrangement_id'] for t in timing['items'] if not t['included_in_forecast']}
    # An explicitly listed excluded row must carry its actual state as well.
    excluded_shown = {f['entity'] for f in selected_arrangements} & excluded
    required.update((identifier, 'status') for identifier in excluded_shown)
    scenarios = {(f['scenario'], f['field']) for f in selected}
    needed_scenarios = {(s, field) for s in ('PRIMARY', 'WITHOUT_FUTURE_INCOME')
                        for field in ('minimum_balance_minor', 'minimum_balance_date')}
    complete = required <= present and needed_scenarios <= scenarios
    snapshots = [{'tool': r['tool'], 'arguments': r['args'], 'evidence_id': r['response']['evidence_id'],
                  'business_date': r['response']['business_date'], 'call_id': r['call_id']}
                 for r in (row, arrangement)]
    basis['snapshots'] = snapshots
    lines = [f"查询窗口：{data['as_of_date']}至{data['through_date']}；未新增假设消费。"]
    included = [t for t in timing['items'] if t['included_in_forecast']]
    if not records:
        lines.append('未查到已记录安排；这不证明现实中没有还款义务或收入。')
    elif not included:
        lines.append('已有安排记录，但本窗口没有计入项；不等于没有已记录安排。')
    explanations = {
        'PAST_OUTFLOW_UNCONFIRMED_CARRIED_FORWARD': '原定日期已过，未确认履行，预测滚入业务今天；未证明已付款',
        'PAST_INCOME_UNCONFIRMED_EXCLUDED': '原定日期已过且未确认到账，已从未来收入情景排除',
        'AFTER_WINDOW_EXCLUDED': '在本窗口之外，未计入本窗口待收付',
        'CANCELLED_EXCLUDED': '已取消，未计入本窗口待收付',
        'COMPLETED_EXCLUDED': '已记录完成，未计入本窗口待收付'}
    for t in timing['items']:
        if t['inclusion_reason'] in explanations:
            lines.append(f"{t['title']}（原定{t['original_due_date']}）：{explanations[t['inclusion_reason']]}。")
    lines.append('收入安排不代表已到账；日期经过不等于履行。这里只核对已登记资料，本轮没有办理付款。')
    child['text'] += '\n\n' + '\n'.join(lines)
    return {'complete': complete, 'reason': 'WINDOW_QUERY_COVERED' if complete else 'WINDOW_QUERY_FACTS_MISSING',
            'forecast': forecast, 'snapshots': snapshots,
            'missing_fields': sorted(required - present), 'missing_scenarios': sorted(needed_scenarios - scenarios)}


def validate(part, trace, scope, item):
    """No heuristic dispatch; the selected contract owns the factual validation."""
    require(type(part) is dict, 'INVALID_COMPONENT')
    kind = part.get('format')
    all_records = records(trace)
    successful = [row for row in all_records if row['ok'] is True and row['status'] == 200]
    covered = {'operations': [], 'tasks': [], 'failures': [], 'drafts': [], 'insights': []}
    metadata = {'format': kind}
    if kind in {'bank_facts_v1', 'bank_billing_v1', 'bank_spending_comparison_v1'}:
        from src.bank_evidence.answer import validate as facts
        from src.bank_evidence.billing_answer import validate as billing
        from src.bank_evidence.spending_answer import validate as spending
        validator = {'bank_facts_v1': facts, 'bank_billing_v1': billing,
                     'bank_spending_comparison_v1': spending}[kind]
        child = validator(child_input(part, subset_trace(trace, FACT_TOOLS)), scope)
        if kind == 'bank_spending_comparison_v1' and child.get('ok'):
            for name in ('baseline', 'proposed'):
                chosen = next(row for row in reversed(successful) if row['tool'] == 'forecast'
                              and row['response']['evidence_id'] == part[name + '_forecast_id'])
                metadata[name + '_forecast'] = forecast_record(chosen)
        if kind == 'bank_facts_v1' and child.get('ok') and item['intent'] == 'cashflow_forecast':
            metadata['cashflow_window'] = query_window(child, successful)
    elif kind in {'bank_operation_v1', 'bank_event_v1'}:
        view = object_part(part, successful)
        child = view_child(view)
        covered['operations'] = view['operation_ids']
        tool, key = ('operation', 'operation_id') if kind == 'bank_operation_v1' else ('event_context', 'event_id')
        matched = [row for row in successful if row['tool'] == tool and row['args'] == {key: part[key]}][-1]
        metadata.update(body=matched['response'], order=all_records.index(matched))
    elif kind == 'task_progress_v1':
        from src.bank_v1_web.task_progress_answer import validate as progress
        def relevant(row):
            return row['tool'] in {'task_status', 'submit_task_plan', 'replan_task'} and (
                row['args'].get('task_id') == part['task_id'] or
                (row.get('response') or {}).get('data', {}).get('task', {}).get('task_id') == part['task_id'])
        child = progress(child_input(part, selected_trace(trace, relevant)))
        if child.get('ok'):
            covered['tasks'] = [part['task_id']]
            covered['operations'] = list(child['task_progress_basis']['data']['operations'])
    elif kind == 'bank_tool_outcome_v1':
        if part.get('tool') in BANK_WRITES:
            view = outcome_part(part, all_records)
        else:
            require(set(part) == {'format', 'tool', 'arguments', 'error_code'}, 'INVALID_TOOL_OUTCOME')
            identity = canonical({key: part[key] for key in ('tool', 'arguments', 'error_code')})
            matches = [row for row in all_records if not row['ok'] and outcome_key(row) == identity]
            require(bool(matches), 'UNBACKED_TOOL_OUTCOME')
            view = {'text': '本次工具请求未取得成功结果。需核对已有记录和失败原因，不能据此认定业务已完成。',
                    'outcome_key': identity}
            if part['tool'] in {'compare_goal_impact', 'explain_goal_changes'}:
                view['text'] = ('本次目标影响尚未完成核对。当前交流缺少唯一的事件前后范围、历史依据不足或记录已变化；'
                                '请明确要比较哪次计划检查后的消费与收入，再重新只读查询。银行已入账记录仍有效，不要重复记账。')
        matched = [row for row in all_records if not (row['ok'] is True and row['status'] == 200)
                   and outcome_key(row) == view['outcome_key']][-1]
        child = view_child(view)
        covered['failures'] = [view['outcome_key']]
        metadata['attempt'] = matched
    elif kind == 'bank_guidance_v1':
        validate_questions({'format': 'bank_service_v1', 'parts': [part]}, {'items': [item]})
        view = guidance_part(part, successful)
        if part['kind'] == 'transfer_assessment':
            view['text'] = ('合计预测没有提供单账户未来余额或可转额度。'
                            '需要结合实际付款账户、保留资金要求和真实到账情况继续核对；本人账户调拨不增加合计资金。')
        child = view_child(view)
    elif kind == 'bank_insights_v1':
        view = insight_part(part, successful)
        # Display reads cache state; never run the analysis again for freshness.
        for snapshot in view['snapshots']:
            if snapshot['tool'] == 'check_insights':
                snapshot['tool'] = 'insight_status'
        child = view_child(view)
        covered['insights'] = [view['source_tool']]
    elif kind == 'bank_transfer_shortfall_v1':
        from src.bank_evidence.answer import validate as facts
        from src.bank_evidence.transfer_shortfall import render
        child = facts(child_input({'format': 'bank_facts_v1', 'fact_ids': [],
            'calculation_ids': [part['calculation_id']]}, subset_trace(trace, FACT_TOOLS)), scope)
        if child.get('ok'):
            child['text'] = render(part, child)
    elif kind in {'goal_answer_v1', 'goal_preview_answer_v1'}:
        from src.bank_goals.evidence import validate_answer
        from src.bank_goals.preview_answer import validate as preview
        names = {'goal_context', 'compare_goal_options'} if kind == 'goal_answer_v1' else {'preview_goal_options'}
        rows = [row for row in successful if row['tool'] in names]
        require(bool(rows) and part['source_id'] == rows[-1]['response']['source_id'], 'NOT_LATEST_GOAL_SOURCE')
        validator = validate_answer if kind == 'goal_answer_v1' else preview
        child = validator(child_input(part, subset_trace(trace, names)))
        metadata['body'] = rows[-1]['response']
        metadata['call_id'] = rows[-1]['call_id']
        if item.get('goal_profile_binding'):
            from .goal_profile_reference import verify_component, display
            verify_component(item, {'metadata': metadata})
            if child.get('ok'):
                child['text'] = display(item) + '\n' + child['text']
    elif kind == 'goal_impact_v1':
        from src.bank_goals.impact_answer import validate as impact_answer
        child = impact_answer(child_input(part, trace), component=True)
        if child.get('ok'):
            metadata['body'] = child['goal_impact_basis']
    elif kind == 'goal_draft_v1':
        require(set(part) == {'format', 'draft_id'}, 'INVALID_DRAFT_COMPONENT')
        rows = [row for row in successful if row['tool'] == 'propose_goal']
        require(bool(rows) and rows[-1]['response']['scope'] == 'goal_draft' and
                rows[-1]['response']['data']['draft_id'] == part['draft_id'] and
                rows[-1]['response']['data']['status'] == 'UNCONFIRMED', 'UNBACKED_GOAL_DRAFT')
        covered['drafts'] = [part['draft_id']]
        metadata['body'] = rows[-1]['response']
        child = {'ok': True, 'text': '已保存未确认的目标草案，请在目标区核对；目标与银行安排尚未因此确认。'}
    elif kind == 'request_clarification_v1':
        from .response import clarification
        # All write coverage is checked across the entire composed answer below.
        child = clarification(part, {'items': [item]}, [])
    else:
        raise ValueError('UNKNOWN_COMPONENT_FORMAT')
    require(child.get('ok') is True, 'COMPONENT_REJECTED:' + str(child.get('answer_validation', {}).get('reason', kind)))
    return {'part': part, 'validated': child, 'covered': covered, 'metadata': metadata}


def check_coverage(trace, components, *, host_failure_coverage=()):
    covered = {key: set() for key in ('operations', 'tasks', 'failures', 'drafts', 'insights')}
    for component in components:
        for key, values in component['covered'].items():
            covered[key].update(values)
    covered['failures'].update(host_failure_coverage)
    for row in records(trace):
        tool = row['tool']
        success = row['ok'] is True and row['status'] == 200
        if not success and tool in MUTATIONS:
            require(outcome_key(row) in covered['failures'], 'OMITTED_WRITE_FAILURE')
        if not success:
            continue
        data = row['response'].get('data', {})
        if tool in BANK_WRITES:
            require(data['operation']['operation_id'] in covered['operations'], 'OMITTED_PREPARED_OPERATION')
        elif tool in {'submit_task_plan', 'replan_task'}:
            require(data['task']['task_id'] in covered['tasks'], 'OMITTED_CHANGED_TASK')
        elif tool == 'propose_goal':
            require(data['draft_id'] in covered['drafts'], 'OMITTED_GOAL_DRAFT')
        elif tool == 'check_insights':
            require(tool in covered['insights'], 'OMITTED_INSIGHT_CHECK')
    insight_reads = {row['tool'] for row in records(trace) if row['ok'] and row['tool'] in INSIGHT_TOOLS}
    require(not insight_reads or bool(covered['insights']), 'OMITTED_INSIGHT_RESULT')
