"""A complete spending comparison bound to model-selected bank/tool results."""
import json

from .answer import paired_records, display_value
from .facts import catalog
from .calculation import assumption_matches


FORMAT = 'bank_spending_comparison_v1'
FIELDS = {'format', 'balance_fact_id', 'calculation_id', 'baseline_forecast_id',
          'proposed_forecast_id', 'arrangements_evidence_id'}


def requested(result):
    try:
        return json.loads(result.get('text', '')).get('format') == FORMAT
    except (ValueError, AttributeError):
        return False


def require(condition, code):
    if not condition:
        raise ValueError(code)


def money(value):
    return display_value({'unit': 'minor', 'value': value})


def forecast_line(label, data):
    primary = data['scenario_minima']['PRIMARY']
    conservative = data['scenario_minima']['WITHOUT_FUTURE_INCOME']
    return (f"{label}：按有效安排计入预计收入，窗口最低合计余额为{money(primary['minimum_balance_minor'])}"
            f"（{primary['minimum_balance_date']}）；不计未来收入时为{money(conservative['minimum_balance_minor'])}"
            f"（{conservative['minimum_balance_date']}）。")


def render(balance, calc, baseline, proposed, arrangements, proposed_args):
    amount = calc['inputs']['right_minor']
    spending_date = proposed_args.get('proposed_date') or proposed['as_of_date']
    lines = [f"目前合计已到账{money(balance['value'])}。不新增这笔消费，当前仍是{money(balance['value'])}；"
             f"假设{spending_date}消费{money(amount)}，仅扣除这笔消费的工具试算余额为{calc['result']['amount']}元。"]
    active = [a for a in arrangements if a['status'] == 'active']
    obligations = [a for a in active if a['kind'] in {'expense', 'goal'}]
    incomes = [a for a in active if a['kind'] == 'income']
    if obligations:
        lines.append('已记录的支出安排：' + '；'.join(
            f"{a['title']}，原定{a['due_date']}，{money(a['amount_minor'])}" for a in obligations) + '。')
    else:
        lines.append('本轮未查到有效支出安排，无法据此确定还款义务。')
    if incomes:
        lines.append('收入条件：' + '；'.join(
            f"{a['title']}，原定{a['due_date']}，{money(a['amount_minor'])}，仍是安排、未证明到账" for a in incomes) + '。')
    else:
        lines.append('本轮未查到有效未来收入安排，不额外假定收入。')
    lines.append(f"比较窗口为{proposed['as_of_date']}至{proposed['through_date']}：\n"
                 + forecast_line('不新增消费', baseline) + '\n' + forecast_line('新增这笔消费', proposed))
    omitted = [a for a in active if a['due_date'] > proposed['through_date']]
    if omitted:
        lines.append('尚未覆盖的计划：' + '；'.join(f"{a['title']}（{a['due_date']}）" for a in omitted)
                     + '。下面结论仅限当前窗口，不能据此判断全部计划是否受影响。')
    if proposed['without_future_income_gap_minor'] == 0:
        lines.append('按本次已记录的安排，在这个窗口内即使不计未来收入，合计资金预测没有跌破0的现金缺口。')
    elif proposed['gap_minor'] == 0:
        lines.append('新增消费后的主预测没有现金缺口（跌破0），但依赖预计收入；若收入未到账，'
                     f"合计现金缺口为{money(proposed['without_future_income_gap_minor'])}。")
    else:
        lines.append(f"新增消费后，即使按有效安排计入预计收入，合计预测仍有{money(proposed['gap_minor'])}缺口，"
                     '需要调整消费金额或日期后重新比较。')
    lines.append('上述现金缺口以0为界，不等于满足某个目标的保留余额要求；本组工具未评估目标底线。未登记生活开销仍未知。下一步可核对重点目标与其保留要求。')
    lines.append('这是合计账户试算；具体付款账户能否支付还需核对。未记录支出不在预测内，安排经过日期不代表已履行。本轮没有办理付款。')
    return '\n\n'.join(lines)


def validate(result, prompt):
    try:
        answer = json.loads(result['text'])
        require(set(answer) == FIELDS and answer['format'] == FORMAT
                and all(type(answer[k]) is str for k in FIELDS), 'INVALID_COMPARISON_FIELDS')
        rows = paired_records(result.get('trace', []))
        require(all(r['tool'] in {'accounts', 'arrangements', 'forecast', 'money_calculate'} for r in rows),
                'UNSUPPORTED_MIXED_FLOW')
        accounts = [r for r in rows if r['tool'] == 'accounts'][-1]
        arrangements = [r for r in rows if r['tool'] == 'arrangements'][-1]
        require(arrangements['response']['evidence_id'] == answer['arrangements_evidence_id'], 'OLD_ARRANGEMENTS')
        current = catalog('accounts', accounts['args'], accounts['response'])['facts']
        balance = next(f for f in current if f['fact_id'] == answer['balance_fact_id'])
        require(balance['entity'] == 'COMBINED_ACCOUNTS' and balance['field'] == 'total_balance_minor', 'NOT_COMBINED_BALANCE')
        calculation = next(r['response']['data']['calculation'] for r in reversed(rows)
                           if r['tool'] == 'money_calculate' and r['response']['evidence_id'] == answer['calculation_id'])
        require(calculation['contract_version'] == 2 and calculation['inputs']['operation'] == 'subtract', 'MISSING_BOUND_SUBTRACTION')
        left, right = (calculation['input_provenance'][key] for key in ('left', 'right'))
        require(left['kind'] == 'bank_fact' and left['fact'] == balance, 'WRONG_CALCULATION_BALANCE')
        require(right['kind'] == 'user_assumption' and assumption_matches(right, prompt), 'UNBOUND_SPENDING_AMOUNT')
        require(calculation['inputs']['right_minor'] > 0, 'NOT_POSITIVE_PROPOSAL')
        forecasts = {r['response']['evidence_id']: r for r in rows if r['tool'] == 'forecast'}
        baseline_row = forecasts[answer['baseline_forecast_id']]
        proposed_row = forecasts[answer['proposed_forecast_id']]
        baseline, proposed = (row['response']['data'] for row in (baseline_row, proposed_row))
        require(baseline_row['args'].get('proposed_expense_minor', 0) == 0, 'BASELINE_HAS_EXTRA_SPENDING')
        require(proposed_row['args'].get('proposed_expense_minor') == calculation['inputs']['right_minor'], 'PROPOSED_AMOUNT_MISMATCH')
        require(baseline['through_date'] == proposed['through_date'] and
                baseline['as_of_date'] == proposed['as_of_date'] == balance['business_date'] and
                baseline['current_balance_minor'] == proposed['current_balance_minor'] == balance['value'], 'FORECAST_WINDOW_OR_BALANCE_MISMATCH')
        require(baseline['evidence_refs'] == proposed['evidence_refs'], 'FORECAST_SOURCE_VERSION_MISMATCH')
        expected_accounts = balance['version']
        require(all(a['version'] == expected_accounts[a['account_id']] for a in
                    proposed['account_allocation']['included_accounts']), 'ACCOUNT_VERSION_MISMATCH')
        records = arrangements['response']['data']['arrangements']
        # Full timing rows include cancelled/completed/out-of-window arrangements,
        # so compare every record, not only active projected cash movements.
        timing = proposed['arrangement_timing']['items']
        require(len(timing) == len(records) and all(any(
            p['arrangement_id'] == a['arrangement_id'] and p['version'] == a['version']
            and p['original_due_date'] == a['due_date'] and p['amount_minor'] == a['amount_minor']
            and p['arrangement_status'] == a['status'] for p in timing) for a in records), 'ARRANGEMENT_VERSION_MISMATCH')
        selected_facts = [balance] + [f for row in (baseline_row, proposed_row)
            for f in catalog('forecast', row['args'], row['response'])['facts']
            if f['field'] == 'minimum_balance_minor']
        return {**result, 'text': render(balance, calculation, baseline, proposed, records, proposed_row['args']),
                'bank_spending_answer': answer,
                'bank_facts_basis': {'facts': selected_facts, 'calculations': [calculation]},
                'answer_validation': {'version': FORMAT, 'scope': 'bank_spending_comparison',
                    'status': 'ACCEPTED', 'gate_applied': True,
                    'limitation': '同窗口合计预测与拟议消费比较，不保证未记录义务或指定付款账户。'}}
    except (ValueError, TypeError, KeyError, IndexError, StopIteration, AttributeError) as error:
        return {**result, 'ok': False, 'model_ok': True, 'session_id': None,
                'error': '消费比较尚未取得相互对应的余额、工具计算与两种预测，未作为完成结果展示。',
                'answer_validation': {'version': FORMAT, 'scope': 'bank_spending_comparison',
                    'status': 'REJECTED', 'gate_applied': True, 'reason': str(error)}}
