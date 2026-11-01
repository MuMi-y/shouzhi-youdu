"""Compare bounded alternatives using the same immutable bank projection core."""
from copy import deepcopy
from src.bank_v1 import finance
from src.bank_v1 import plan_projection
from src.bank_v1.common import DomainError
from .contracts import validate_dates


def compare(goal, snapshot):
    profile = goal['profile']
    validate_dates(profile, snapshot['business_date'])
    linked_id = goal['linked_arrangement_id']
    linked = next((a for a in snapshot['arrangements'] if a['arrangement_id'] == linked_id), None)
    if linked_id and (linked is None or linked['kind'] != 'expense' or linked['status'] != 'active'):
        raise DomainError('GOAL_NOT_ACTIVE', '关联安排已完成、取消或改变类型，请核对原安排；不自动重新增加目标支出。')
    # All other arrangements remain untouched, including same-amount and legacy goal rows.
    alternatives = [('original', '按原计划', profile['preferred_amount_minor'], profile['preferred_date']),
                    ('reduced', '降低购置金额', profile['min_amount_minor'], profile['preferred_date']),
                    ('delayed', '推迟购置日期', profile['preferred_amount_minor'], profile['latest_date'])]
    options, seen = [], set()
    for option_id, label, amount, due in alternatives:
        if (amount, due) in seen:
            continue
        seen.add((amount, due))
        trial = deepcopy(snapshot)
        trial['arrangements'] = [a for a in trial['arrangements'] if a['arrangement_id'] != linked_id]
        trial['arrangements'].append({'arrangement_id': linked_id or 'goal_trial:focus',
            'version': linked['version'] if linked else 0, 'title': profile['title'], 'kind': 'expense',
            'amount_minor': amount, 'due_date': due, 'status': 'active'})
        end = plan_projection.window(snapshot, profile['latest_date'])['through_date']
        result = plan_projection.forecast(trial, end)
        reserve = profile['reserve_floor_minor']
        primary = max(0, reserve-result['lowest_balance_minor'])
        conservative = max(0, reserve-result['without_future_income_lowest_balance_minor'])
        status = 'NEEDS_TRADEOFF' if primary else 'CONDITIONAL' if conservative else 'WITHIN_KNOWN_CONSTRAINTS'
        options.append({'option_id': option_id, 'label': label, 'amount_minor': amount, 'due_date': due,
            'status': status, 'reserve_shortfall_minor': primary,
            'without_income_reserve_shortfall_minor': conservative, 'forecast': result})
    return {'mode': 'DETERMINISTIC_NO_MODEL', 'currency': 'CNY', 'goal_revision': goal['revision'],
            'window_start': snapshot['business_date'], 'window_end': end,
            'bank_hash': finance.snapshot_hash(snapshot), 'options': options,
            'replaced_arrangement': linked,
            'protected_arrangements': [a for a in snapshot['arrangements'] if a['arrangement_id'] != linked_id],
            'notice': '仅比较至多三个候选；未来收入未到账，合计预测不保证某个付款账户够用。'}
