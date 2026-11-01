"""Render model-selected facts without letting prose change their meaning.

This finite contract covers money/arrangement/forecast reads. It is not a
semantic verifier for unrestricted Chinese, nor proof that all needed queries
were chosen. Task, billing and goal flows retain their own contracts for now.
"""
import json

from src.bank_v1.common import canonical
from .facts import SUPPORTED_TOOLS, CORE_TOOLS, catalog
from .calculation import assumption_matches
from .calculation_tree import leaves


FORMAT = 'bank_facts_v1'
READS = CORE_TOOLS | {'money_calculate'}


def requested(result):
    try:
        return json.loads(result.get('text', '')).get('format') == FORMAT
    except (ValueError, AttributeError):
        return False


def required(result):
    rows = [r for r in result.get('trace', []) if isinstance(r, dict) and r.get('kind') == 'response']
    bound_calculation = any('input_provenance' in r.get('response', {}).get('data', {}).get('calculation', {}) for r in rows)
    supported = rows and (all(r.get('tool') in READS for r in rows) or
                          bound_calculation and all(r.get('tool') in SUPPORTED_TOOLS | {'money_calculate'} for r in rows))
    return bool(supported and any('fact_catalog' in r.get('response', {}) or
        'input_provenance' in r.get('response', {}).get('data', {}).get('calculation', {}) for r in rows))


def paired_records(trace, *, include_failures=False, allowed_scopes=frozenset({'bank'})):
    requests, completed, records = {}, set(), []
    for row in trace:
        if not isinstance(row, dict):
            continue
        if row.get('kind') == 'request':
            identity = row.get('call_id')
            if not identity or identity in requests:
                raise ValueError('DUPLICATE_OR_MISSING_REQUEST_ID')
            requests[identity] = row
        elif row.get('kind') == 'response':
            identity = row.get('call_id')
            request = requests.get(identity)
            if (not request or identity in completed or request.get('name') != row.get('tool')
                    or request.get('arguments') != row.get('args')):
                raise ValueError('UNPAIRED_RESPONSE')
            completed.add(identity)
            if row.get('ok') is True and row.get('status') == 200:
                if row['response'].get('scope', 'bank') not in allowed_scopes:
                    raise ValueError('WRONG_SCOPE')
                records.append(row)
            elif include_failures:
                if (row.get('response') or {}).get('scope', 'bank') not in allowed_scopes:
                    raise ValueError('WRONG_SCOPE')
                records.append(row)
    if set(requests) != completed:
        raise ValueError('INCOMPLETE_TOOL_TRACE')
    return records


def query_key(tool, args):
    return canonical({'tool': tool, 'arguments': args})


def display_value(fact):
    if fact['unit'] == 'minor':
        # Integer display avoids dependence on a process Decimal context.
        amount = fact['value']
        return f"{'-' if amount < 0 else ''}{abs(amount) // 100}.{abs(amount) % 100:02d}元"
    if fact['unit'] == 'status':
        return {'active': '有效安排（未确认履行）', 'completed': '已记录履行',
                'cancelled': '已取消安排'}[fact['value']]
    return str(fact['value'])


def render_fact(fact):
    line = f"{fact['label']}：{display_value(fact)}。"
    if fact['scenario'] == 'USER_ARRANGEMENT_NOT_POSTED':
        line += f"原定{fact['period']['due_date']}；这是安排记录，不证明已经收付款。"
    elif fact['scenario'] in {'PRIMARY', 'WITHOUT_FUTURE_INCOME'}:
        amount = fact['period']['proposed_expense_minor']
        scenario = ('不新增消费；' if amount == 0 else
                    f"假设{fact['period']['proposed_date']}新增消费{display_value({'unit': 'minor', 'value': amount})}；")
        line = scenario + line
        line += (f"窗口{fact['period']['start']}至{fact['period']['end']}，"
                 f"最低点日期{fact['period']['minimum_date']}；为合计账户预测。")
    elif fact['scenario'] == 'RECORDED_PERIOD_FLOW':
        line += (f"期间{fact['period']['start']}至{fact['period']['end']}（含首尾）。"
                 '仅按已有流水统计，不证明现实资料完整，也不代表当前余额或未来存钱能力。')
    elif fact['scenario'] == 'POSTED_TRANSACTION':
        line += '正数为入账、负数为出账；这是已记账流水。该查询可能有限制，不能当作全部历史。'
    else:
        line += f"查询业务日{fact['business_date']}。"
    return line


def render_calculation(calc, *, include_parents=True):
    operands = []
    for side in ('left', 'right'):
        item = calc['input_provenance'][side]
        if item['kind'] == 'bank_fact':
            fact = item['fact']
            period = fact['period']
            window = f" [{period['start']}至{period['end']}]" if period and period.get('start') else ''
            operands.append(f"{fact['label']}{window}（{display_value(fact)}）")
        elif item['kind'] == 'calculation_result':
            operands.append('前项试算结果（' + display_value({'unit': 'minor', 'value': item['value_minor']}) + '）')
        else:
            value = calc['inputs'][side + '_minor']
            operands.append('原话中的拟议金额（' + display_value({'unit': 'minor', 'value': value}) + '）')
    operation = calc['inputs']['operation']
    expression = {'add': ' 加 ', 'subtract': ' 减 ', 'absolute_difference': ' 与 ', 'percentage': ' 除以 '}[operation]
    suffix = '的绝对差' if operation == 'absolute_difference' else '乘100%' if operation == 'percentage' else ''
    result = calc['result']
    value = result['percent'] + '%' if result['kind'] == 'percentage' else result['amount'] + '元'
    parents = ([render_calculation(item['envelope']['data']['calculation'])
                for item in calc['input_provenance'].values() if item['kind'] == 'calculation_result']
               if include_parents else [])
    return '\n'.join(parents + [f"工具计算：{operands[0]}{expression}{operands[1]}{suffix}＝{value}。这是试算，不会改动实际余额。"])


def render_calculations(calculations):
    seen, lines = set(), []

    def visit(calculation):
        key = canonical(calculation)
        if key in seen:
            return
        for source in calculation['input_provenance'].values():
            if source['kind'] == 'calculation_result':
                visit(source['envelope']['data']['calculation'])
        seen.add(key)
        lines.append(render_calculation(calculation, include_parents=False))

    for calculation in calculations:
        visit(calculation)
    return lines


def validate(result, prompt):
    try:
        answer = json.loads(result['text'])
        if (set(answer) != {'format', 'fact_ids', 'calculation_ids'} or answer['format'] != FORMAT):
            raise ValueError('INVALID_ANSWER_FIELDS')
        for key in ('fact_ids', 'calculation_ids'):
            ids = answer[key]
            if (type(ids) is not list or len(ids) > 20 or not all(type(i) is str for i in ids)
                    or len(ids) != len(set(ids))):
                raise ValueError('INVALID_REFERENCE_LIST')
        if not answer['fact_ids'] and not answer['calculation_ids']:
            raise ValueError('EMPTY_ANSWER')
        records = paired_records(result.get('trace', []))
        latest, calculations = {}, {}
        for row in records:
            tool, args, body = row['tool'], row['args'], row['response']
            if tool in SUPPORTED_TOOLS:
                rebuilt = catalog(tool, args, body)
                if body.get('fact_catalog') != rebuilt:
                    raise ValueError('INVALID_FACT_CATALOG')
                latest[query_key(tool, args)] = rebuilt
            elif tool == 'money_calculate':
                calculations[body['evidence_id']] = body['data']['calculation']
            else:
                raise ValueError('UNSUPPORTED_MIXED_FLOW')
        facts = {fact['fact_id']: fact for group in latest.values() for fact in group['facts']}
        selected = [facts[identity] for identity in answer['fact_ids']]
        calc_selected = [calculations[identity] for identity in answer['calculation_ids']]
        for calc in calc_selected:
            if calc.get('contract_version') not in (2, 3) or set(calc.get('input_provenance', {})) != {'left', 'right'}:
                raise ValueError('UNRESOLVED_CALCULATION')
            for source in leaves(calc):
                if source['kind'] == 'user_assumption':
                    if not assumption_matches(source, prompt):
                        raise ValueError('UNBOUND_USER_ASSUMPTION')
                elif source['kind'] == 'bank_fact':
                    fact = source['fact']
                    key = query_key(fact['source_tool'], fact['source_arguments'])
                    if key in latest and fact not in latest[key]['facts']:
                        raise ValueError('CALCULATION_USES_OLD_FACT')
                else:
                    raise ValueError('UNKNOWN_SOURCE_KIND')
        lines = [render_fact(fact) for fact in selected]
        lines += render_calculations(calc_selected)
        lines.append('以上只说明本轮已查询的事实及试算；未查询的收支或计划仍需补充，不能据此判断所有风险已排除。')
        return {**result, 'text': '\n\n'.join(lines), 'bank_facts_answer': answer,
                'bank_facts_basis': {'facts': selected, 'calculations': calc_selected},
                'answer_validation': {'version': FORMAT, 'scope': 'bank_facts', 'status': 'ACCEPTED',
                    'gate_applied': True, 'limitation': '所选事实渲染已绑定；问题是否完整解决需另做效果验收。'}}
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        return {**result, 'ok': False, 'model_ok': True, 'session_id': None,
                'error': '回答未能绑定本轮账户、安排或计算依据，未作为核验结果展示。',
                'answer_validation': {'version': FORMAT, 'scope': 'bank_facts',
                    'status': 'REJECTED', 'gate_applied': True, 'reason': str(error)}}
