"""Request-aware entry around existing typed evidence and display boundaries."""
import json

from src.bank_evidence.envelope import normalize, unique_object, reject_constant, finite_float, FORMATS
from .context import FORMAT
from .decision import validate as validate_decision, assumptions, validate_questions, require
from .schema import needs_parameter_input


LABELS = {'source_account': '转出账户', 'target_account': '转入账户', 'amount_minor': '金额',
          'reserve_minor': '希望保留的金额', 'minimum_minor': '最低金额', 'maximum_minor': '最高金额',
          'income_minor': '拟议收入金额', 'expense_minor': '拟议支出金额', 'period_text': '统计期间',
          'start_date': '开始日期', 'end_date': '结束日期', 'date_text': '日期要求', 'latest_date_text': '最晚日期', 'purpose': '用途',
          'through_date_text': '预测截止日',
          'goal_title': '目标名称', 'priority': '优先事项', 'merchant_text': '商户条件', 'mandate_id': '授权编号',
          'goal_text': '目标', 'task_id': '任务编号', 'request_text': '具体需求'}
WRITE_TOOLS = frozenset({'prepare_transfer', 'prepare_mandate_query', 'prepare_mandate_cancel',
                          'propose_arrangement', 'submit_task_plan', 'replan_task', 'propose_goal'})
GOAL_RECOVERY = {'kind': 'REVIEW_GOAL_FORM', 'automatic_fill': False, 'automatic_save': False}
GOAL_RECOVERY_MESSAGE = ('这次目标的条件或原话还没核对清楚，分析暂未作为可靠建议展示。'
                         '你可以打开目标表单，核对日期、金额和用途后直接比较方案；不会自动保存或办理。')


def can_recover_goal(result):
    """A manual form is a next step, never acceptance of a rejected answer."""
    trace = result.get('trace', [])
    reads = {'preview_goal_options', 'compare_goal_options', 'goal_context',
             'accounts', 'arrangements', 'forecast'}
    requests = [row for row in trace if row.get('kind') == 'request']
    return (bool(requests) and all(row.get('name') in reads for row in requests)
            and any(row.get('kind') == 'response' and row.get('ok') is True
                    and row.get('name') in {'preview_goal_options', 'compare_goal_options', 'goal_context'}
                    for row in trace))


def recovery_view(result):
    """Offer the same recovery for persisted failures, without rewriting them."""
    if (not result.get('ok') and result.get('answer_validation', {}).get('status') == 'REJECTED'
            and can_recover_goal(result)):
        return {**result, 'recovery': dict(GOAL_RECOVERY), 'error': GOAL_RECOVERY_MESSAGE}
    return result


def rejection(result, reason):
    message = ('本轮动作要求与实际工具调用不一致，未作为合格结果展示。已生成的待办不会自动撤回，'
               '请核对银行待办与原回执，不要重复办理。' if reason == 'REQUEST_ACTION_WRITE_CONTRADICTION' else
               '需求参数或原话来源尚未核实，本次未作为已解决展示；已有银行操作请核对原待办和回执。')
    recovery = can_recover_goal(result)
    if recovery:
        message = GOAL_RECOVERY_MESSAGE
    return {**result, **({'recovery': dict(GOAL_RECOVERY)} if recovery else {}),
            'ok': False, 'model_ok': result.get('ok', False), 'session_id': None,
            'error': message,
            'answer_validation': {'version': FORMAT, 'scope': 'request_decision', 'status': 'REJECTED',
                                  'gate_applied': True, 'reason': reason}}


def clarification(answer, decision, trace):
    require(set(answer) == {'format', 'item_ids'}, 'INVALID_CLARIFICATION')
    identifiers = answer['item_ids']
    require(type(identifiers) is list and identifiers and all(type(x) is str for x in identifiers)
            and len(identifiers) == len(set(identifiers)), 'INVALID_CLARIFICATION_ITEMS')
    require(not any(row.get('kind') == 'request' and row.get('name') in WRITE_TOOLS
                    for row in trace if isinstance(row, dict)), 'CLARIFICATION_CANNOT_HIDE_WRITE_ATTEMPT')
    items = {item['id']: item for item in decision['items']}
    require(set(identifiers) == set(items), 'CLARIFICATION_MUST_COVER_REQUEST_ITEMS')
    lines = []
    for identifier in identifiers:
        item = items[identifier]
        from .objects import clarification_text
        object_question = clarification_text(item)
        require(needs_parameter_input(item) or object_question, 'NO_CLARIFICATION_NEEDED')
        if object_question:
            lines.append(object_question)
        if needs_parameter_input(item) and item['missing_fields']:
            lines.append('请补充：' + '、'.join(LABELS[field] for field in item['missing_fields']) + '。')
        if needs_parameter_input(item) and item['conflicts']:
            lines.append('以下条件存在冲突，请明确本次采用哪一个：' +
                         '、'.join(LABELS[field] for field in item['conflicts']) + '。')
    return {'ok': True, 'text': '\n'.join(lines), 'answer_validation': {
        'version': 'request_clarification_v1', 'status': 'ACCEPTED', 'gate_applied': True}}


def validate(result, context):
    if not result.get('ok') or not result.get('text'):
        return {**result, 'answer_validation': {'version': FORMAT, 'scope': 'request_decision',
                                                'status': 'MODEL_FAILED', 'gate_applied': True}}
    decision_metadata = {}
    try:
        normalized = normalize(result)
        document = json.loads(normalized['text'], object_pairs_hook=unique_object,
                              parse_constant=reject_constant, parse_float=finite_float)
        require(type(document) is dict and set(document) == {'format', 'request_id', 'items', 'answer'}
                and document['format'] == FORMAT, 'REQUEST_ENVELOPE_REQUIRED')
        require(document['request_id'] == context['request_id'], 'WRONG_REQUEST_ID')
        from .components import records
        decision = validate_decision(document['items'], context, records(result.get('trace', [])))
        decision_metadata = {'request_decision': decision, 'request_decision_validation': {
            'version': 1, 'status': 'SOURCES_VALIDATED', 'limitation': '不表示银行业务已完成或用户已授权'}}
        answer = document['answer']
        require(type(answer) is dict, 'INVALID_TYPED_ANSWER')
        from .delivery import validate as validate_delivery
        child = validate_delivery(answer, decision, context, result.get('trace', []))
        if (answer.get('format') == 'request_clarification_v1'
                and any(item['intent'] == 'goal_compare' for item in decision['items'])):
            child['recovery'] = dict(GOAL_RECOVERY)
        return {**result, **child, **decision_metadata, 'model_text': result['text'],
                'request_context_basis': {'request_id': context['request_id'],
                    'current_message_id': context['current_message_id'],
                    'message_ids': [row['message_id'] for row in context['messages']],
                    'truncated': context['truncated'], 'read_only': context['read_only']},
                'child_validation': {'version': 'business-outcomes-1', 'status': 'ACCEPTED'},
                'answer_validation': {'version': FORMAT, 'scope': 'request_decision', 'status': 'ACCEPTED',
                    'gate_applied': True, 'limitation': '参数来源与所选证据已检查；不证明业务目标已完整解决。'}}
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        return {**rejection(result, str(error)), **decision_metadata}
