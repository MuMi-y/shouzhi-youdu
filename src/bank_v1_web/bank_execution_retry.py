"""Admit one original-request retry only when a fresh bank turn attempted no tools."""
import json
import math


PREPARATION_REASONS = frozenset({'UNBACKED_OPERATION_PREPARATION', 'UNBACKED_TASK_SAVE'})
# Mirrors the existing DSH bank ceiling, without adding permissions there.
BANK_TOOLS = frozenset({'accounts', 'transactions', 'spending_summary', 'arrangements',
    'forecast', 'propose_arrangement', 'prepare_transfer', 'prepare_mandate_query',
    'prepare_mandate_cancel', 'match_mandate_from_query', 'operation', 'event_context', 'billing_report',
    'recurring_candidates', 'insight_status', 'check_insights', 'submit_task_plan',
    'task_status', 'replan_task', 'money_calculate'})
PREPARE_TOOLS = frozenset({'propose_arrangement', 'prepare_transfer',
                         'prepare_mandate_query', 'prepare_mandate_cancel'})


def empty_call_surface(result):
    """Trust only the exact normal adapter sequence, never absence in partial traces."""
    trace = result.get('trace')
    if (not isinstance(trace, list) or len(trace) != 2
            or any(not isinstance(record, dict) for record in trace)
            or [record.get('kind') for record in trace] != ['registered', 'surface']):
        return None
    registered, surface = trace
    names = registered.get('allowed')
    if (not isinstance(names, list) or not names
            or any(type(name) is not str for name in names)
            or len(set(names)) != len(names) or not set(names) <= BANK_TOOLS
            or surface.get('tools') != names or surface.get('preset') != 'bank-v1'):
        return None
    for field in ('provider', 'model'):
        value = result.get(field)
        if type(value) is not str or not value.strip() or surface.get(field) != value:
            return None
    return frozenset(names)


def execution_retry_prompt(result, validation, original_prompt, *, session_id,
                           readonly, data_scope, remaining_seconds):
    """Return (prompt, reason); this route retains tool execution, not read-only repair."""
    if data_scope != 'bank' or readonly is not False or session_id not in (None, ''):
        return None, 'NOT_FRESH_WRITABLE_BANK_TURN'
    if (result.get('ok') is not True or type(result.get('exit_code')) is not int
            or result['exit_code'] != 0 or result.get('preset') != 'bank-v1'
            or result.get('error') or result.get('process_cleanup_pending')):
        return None, 'MODEL_NOT_CLEANLY_COMPLETED'
    if (not isinstance(validation, dict) or validation.get('gate_applied') is not True
            or validation.get('scope') != 'bank' or validation.get('status') != 'REJECTED'):
        return None, 'NOT_PREPARATION_ONLY_REJECTION'
    reasons = validation.get('reasons')
    if (not isinstance(reasons, list) or not reasons
            or any(not isinstance(reason, dict) or reason.get('code') not in PREPARATION_REASONS
                   for reason in reasons)):
        return None, 'NOT_PREPARATION_ONLY_REJECTION'
    if (type(validation.get('response_count')) is not int or validation['response_count'] != 0
            or validation.get('operation_ids') != [] or validation.get('task_ids') != []
            or validation.get('numeric_diagnostics') != []):
        return None, 'VALIDATION_HAS_EVIDENCE_OR_UNCERTAINTY'
    if (type(remaining_seconds) not in (int, float) or not math.isfinite(remaining_seconds)
            or remaining_seconds < 60):
        return None, 'INSUFFICIENT_EXECUTION_BUDGET'
    names = empty_call_surface(result)
    if names is None:
        return None, 'NO_VERIFIED_EMPTY_TOOL_PATH'
    codes = {reason['code'] for reason in reasons}
    if (('UNBACKED_TASK_SAVE' in codes and 'submit_task_plan' not in names)
            or ('UNBACKED_OPERATION_PREPARATION' in codes and not names & PREPARE_TOOLS)):
        return None, 'REQUIRED_EXECUTION_TOOL_UNAVAILABLE'
    if type(original_prompt) is not str or not original_prompt.strip():
        return None, 'INVALID_ORIGINAL_REQUEST'
    prompt = (
        '这是一次原请求执行纠正。上一轮没有调用任何银行工具，因此没有准备操作或保存任务；'
        '不能声称已准备、已保存或已办理。只依据下面的original_user_request处理，'
        '不沿用上一轮草稿中的金额、对象、状态或授权。'
        '原请求明确要求准备或建立任务时，实际调用银行工具并核对返回结果；'
        '原请求仅有模糊意向时仍只分析，必要信息缺失时澄清。'
        '不要为了满足纠正指令擅自新增办理或猜测参数。'
        '沿用当前银行工具权限；准备操作仍需本人在可信页面确认，不能自行确认或伪造回执。'
        '先用工具取得事实，按真实结果说明已准备、等待确认、失败或尚未建立。'
        '这轮结束后直接报告可验证结果，不声称不存在的确认卡或任务。\n'
        + json.dumps({'original_user_request': original_prompt}, ensure_ascii=False))
    if len(prompt) > 24000:
        return None, 'EXECUTION_RETRY_PROMPT_TOO_LARGE'
    return prompt, 'NO_TOOL_EXECUTION_RETRY'
