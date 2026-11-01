"""Narrow first-goal clarification route; never a financial goal report."""
import re
from .evidence import GOAL_TOOLS


WRITES = frozenset({'propose_arrangement', 'prepare_transfer', 'prepare_mandate_query',
                   'match_mandate_from_query', 'prepare_mandate_cancel', 'submit_task_plan', 'replan_task'})


def missing_goal_clarification(result):
    """Require complete host-bound GOAL_MISSING reads, without automatic checks."""
    if result.get('goal_answer') is not None:
        return None
    requests, responses = {}, {}
    for row in result.get('trace', []):
        if not isinstance(row, dict):
            continue
        if row.get('kind') == 'request':
            name = row.get('name')
            if name in WRITES or name in GOAL_TOOLS - {'goal_context'}:
                return None
            if name == 'goal_context':
                call_id = row.get('call_id')
                if not isinstance(call_id, str) or not call_id or call_id in requests:
                    return None
                if row.get('arguments') != {'goal_id': 'focus'}:
                    return None
                requests[call_id] = row
        if row.get('kind') != 'response':
            continue
        response = row.get('response')
        if isinstance(response, dict) and str(response.get('scope', '')).startswith('goal'):
            return None
        if row.get('tool') not in GOAL_TOOLS:
            continue
        call_id = row.get('call_id')
        request = requests.get(call_id)
        if (row.get('tool') != 'goal_context' or row.get('ok') is not False or row.get('status') != 409
                or request is None or call_id in responses or request['arguments'] != row.get('args')
                or not isinstance(response, dict) or set(response) != {'error'}
                or not isinstance(response['error'], dict) or response['error'].get('code') != 'GOAL_MISSING'):
            return None
        responses[call_id] = row
    if not requests or set(requests) != set(responses):
        return None
    return {'code': 'GOAL_MISSING', 'tool': 'goal_context', 'call_ids': list(responses),
            'coverage': 'BOUND_MISSING_GOAL_INTERACTIVE_READS_ONLY',
            'limitation': '允许需求整理与下一步说明，不证明目标已确认或财务预测成立；仍受普通金额与有限状态检查。'}


def clarification_reasons(text, trace):
    """Reject finite affirmative completion claims; not a general semantic proof."""
    claim = re.compile(
        r'(?:目标|草案|方案|计划)(?:已|已经)(?:保存|确认|建立|生效|采用|选定|完成)'
        r'|已(?:经)?(?:为你|替你)?(?:保存|确认|建立|采用|选定|完成)(?:了)?(?:这[个份项]|该)?(?:目标|草案|方案|计划)')
    projection_available = any(
        isinstance(row, dict) and row.get('kind') == 'response' and row.get('tool') == 'forecast'
        and row.get('ok') is True and isinstance(row.get('response'), dict)
        and row['response'].get('scope', 'bank') == 'bank'
        and isinstance(row['response'].get('data'), dict)
        and isinstance(row['response'].get('data', {}).get('scenario_minima'), dict)
        for row in trace)
    patterns = [claim]
    if not projection_available:
        patterns.append(re.compile(r'(?:比较|预测|测算)结果(?:显示|表明|如下|为|是)'))
    reasons = []
    for clause in re.split(r'[，,。；;！？!?\n]', text):
        for pattern in patterns:
            for match in pattern.finditer(clause):
                prefix = clause[:match.start()]
                if re.search(r'(?:没有|并未|未曾|不能|无法|不要|不代表|不会|尚未|如果|若|假设|确认后|保存后|需要先有|才会成为)', prefix):
                    continue
                reasons.append({'code': 'UNBACKED_GOAL_CLARIFICATION_CLAIM', 'clause': clause})
    return reasons
