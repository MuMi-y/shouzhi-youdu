"""Pure integer arithmetic; descriptions are data, never instructions."""
from collections import defaultdict
from calendar import monthrange
from datetime import date, timedelta
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
import hashlib
import json
import re
import unicodedata
from .policy import default_policy, validate_policy
from .ratio_display import metric_label, ratio_display

ALGORITHM = 'recurring-v1-month25-35-week6-8-min3'


def analysis_policy(policy=None):
    """Return a copy of the validated rule values used by this calculation."""
    return default_policy() if policy is None else validate_policy(policy)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def display(value):
    return format(Decimal(value) / 100, '.2f')


def merchant_key(text):
    # Deliberately conservative: do not merge varying reference numbers/merchant names.
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', text).casefold()).strip()


def totals(rows):
    categories = defaultdict(lambda: {'count': 0, 'debit_minor': 0, 'credit_minor': 0, 'evidence_refs': []})
    income = expense = refund = transfers = opening = unknown_debit = unknown_credit = 0
    for row in rows:
        value, kind = row['amount_minor'], row['kind']
        group = categories[row.get('category') or '未分类']
        group['count'] += 1
        group['debit_minor'] += max(0, -value)
        group['credit_minor'] += max(0, value)
        group['evidence_refs'].append(row['id'])
        if kind == 'income': income += value
        elif kind == 'expense': expense += -value
        elif kind == 'refund': refund += value
        elif kind in ('TRANSFER_IN', 'TRANSFER_OUT', 'transfer'): transfers += value
        elif kind == 'OPENING': opening += value
        else:
            unknown_debit += max(0, -value)
            unknown_credit += max(0, value)
    debit = sum(max(0, -r['amount_minor']) for r in rows)
    credit = sum(max(0, r['amount_minor']) for r in rows)
    result = {'count': len(rows), 'debit_minor': debit, 'credit_minor': credit,
              'net_change_minor': credit-debit, 'income_minor': income,
              'expense_minor': expense, 'refund_minor': refund,
              'net_expense_minor': expense-refund, 'internal_transfer_net_minor': transfers,
              'opening_entries_minor': opening, 'unclassified_debit_minor': unknown_debit,
              'unclassified_credit_minor': unknown_credit}
    result['amounts'] = {k.removesuffix('_minor'): display(v) for k, v in result.items() if k.endswith('_minor')}
    result['categories'] = [{'category': key, **v, 'amounts': {'debit': display(v['debit_minor']),
        'credit': display(v['credit_minor'])}} for key, v in sorted(categories.items())]
    result['evidence_refs'] = [r['id'] for r in rows]
    return result


def anomalies(rows):
    groups = defaultdict(list)
    for row in rows:
        if row['kind'] in ('expense', 'outflow') and row['amount_minor'] < 0:
            groups[(row['date'], row['account_id'], merchant_key(row['description']), row['amount_minor'])].append(row)
    return [{'kind': 'POSSIBLE_DUPLICATE', 'date': k[0], 'merchant_key': k[2],
             'amount_minor': -k[3], 'amount': display(-k[3]), 'evidence_refs': [r['id'] for r in v],
             'explanation': '同日同账户同描述同金额；可能是多次真实消费，需核对，不能称欺诈或自动退款。'}
            for k, v in groups.items() if len(v) >= 2]


def report_metrics(total, start_date, end_date, *, totals_path):
    """Two independent ratios from the same period; neither is a savings rate."""
    denominator = total['income_minor']
    definitions = (
        ('expense_to_income', 'expense_minor', '本期消费支出占本期收入；不扣除退款，不是全部出账占比。'),
        ('net_flow_to_income', 'net_change_minor', '本期净流入占本期收入；含该期所有已纳入口径流量，不等于可支配储蓄或账户余额。'),
    )
    metrics = []
    for name, numerator_key, meaning in definitions:
        value = None
        if denominator > 0:
            with localcontext(Context(prec=40, rounding=ROUND_HALF_EVEN)):
                ratio = Decimal(total[numerator_key]) * 100 / Decimal(denominator)
                rounded = ratio.quantize(Decimal('0.01'))
                value = format(Decimal(0) if rounded == 0 else rounded, '.2f')
        metrics.append({'name': name, 'numerator_path': totals_path+'.'+numerator_key,
            'denominator_path': totals_path+'.income_minor',
            'period': {'start_date': start_date, 'end_date': end_date},
            'formula': 'numerator / denominator * 100', 'value_percent': value,
            'status': 'DEFINED' if denominator > 0 else 'NON_POSITIVE_INCOME',
            'rounding': 'ROUND_HALF_EVEN_2DP', 'unit': 'percent', 'meaning': meaning,
            'display': ratio_display(total[numerator_key], denominator,
                                     label=metric_label(name, totals_path))})
    return metrics


def description_groups(rows, start_date, end_date, *, totals_path):
    """Group original descriptions and transaction kinds, without inferring merchants."""
    group_limit, reference_limit = 20, 50
    groups = {}
    for row in rows:
        key = (row['description'], row['kind'])
        if key not in groups:
            groups[key] = {'description': key[0], 'transaction_kind': key[1],
                'transaction_count': 0, 'debit_minor': 0, 'credit_minor': 0, 'evidence_refs': []}
        group = groups[key]
        group['transaction_count'] += 1
        group['debit_minor'] += max(0, -row['amount_minor'])
        group['credit_minor'] += max(0, row['amount_minor'])
        group['evidence_refs'].append(row['id'])
    ordered = sorted(groups.values(), key=lambda group: (
        -(group['debit_minor']+group['credit_minor']), group['description'], group['transaction_kind']))
    visible, hidden = ordered[:group_limit], ordered[group_limit:]
    kind_summaries = []
    for kind in sorted({group['transaction_kind'] for group in ordered}):
        kind_groups = [group for group in ordered if group['transaction_kind'] == kind]
        returned = sum(group['transaction_kind'] == kind for group in visible)
        kind_summaries.append({'transaction_kind': kind, 'total_groups': len(kind_groups),
            'returned_groups': returned, 'truncated': returned < len(kind_groups),
            'transaction_count': sum(group['transaction_count'] for group in kind_groups),
            'debit_minor': sum(group['debit_minor'] for group in kind_groups),
            'credit_minor': sum(group['credit_minor'] for group in kind_groups)})
    for group in visible:
        group['net_change_minor'] = group['credit_minor']-group['debit_minor']
        group['evidence_ref_count'] = len(group['evidence_refs'])
        group['evidence_refs_truncated'] = group['evidence_ref_count'] > reference_limit
        group['evidence_refs'] = sorted(group['evidence_refs'])[:reference_limit]
    omitted_debit = sum(group['debit_minor'] for group in hidden)
    omitted_credit = sum(group['credit_minor'] for group in hidden)
    return {'group_by': ['description', 'transaction_kind'],
        'semantics': 'RAW_DESCRIPTION_NOT_MERCHANT_OR_INDUSTRY',
        'period': {'start_date': start_date, 'end_date': end_date},
        'limit': group_limit, 'evidence_ref_limit': reference_limit,
        'all_evidence_refs_path': totals_path+'.evidence_refs',
        'sort': 'GROSS_FLOW_DESC_DESCRIPTION_KIND_ASC',
        'total_groups': len(ordered), 'returned_groups': len(visible), 'kind_summaries': kind_summaries,
        'truncated': len(ordered) > group_limit, 'items': visible,
        'omitted': {'group_count': len(hidden),
            'transaction_count': sum(group['transaction_count'] for group in hidden),
            'debit_minor': omitted_debit, 'credit_minor': omitted_credit,
            'net_change_minor': omitted_credit-omitted_debit}}


def _comparison_groups(rows):
    """Aggregate full raw keys; never join the already truncated display lists."""
    groups = {}
    for row in rows:
        key = (row['description'], row['kind'])
        group = groups.setdefault(key, {'transaction_count': 0, 'debit_minor': 0,
                                       'credit_minor': 0, 'refs': []})
        group['transaction_count'] += 1
        group['debit_minor'] += max(0, -row['amount_minor'])
        group['credit_minor'] += max(0, row['amount_minor'])
        group['refs'].append(row['id'])
    return groups


def _comparison_period(group):
    observed = group is not None
    group = group or {'transaction_count': 0, 'debit_minor': 0, 'credit_minor': 0}
    values = {key: group[key] for key in ('transaction_count', 'debit_minor', 'credit_minor')}
    values['net_change_minor'] = values['credit_minor']-values['debit_minor']
    return {'observed_in_period': observed, **values}


def _comparison_delta(current, previous):
    values = {key: current[key]-previous[key] for key in
              ('transaction_count', 'debit_minor', 'credit_minor', 'net_change_minor')}
    return values


def _comparison_rate(previous, change):
    value = None
    if previous > 0:
        precision = max(40, len(str(abs(change)))+len(str(previous))+10)
        with localcontext(Context(prec=precision, rounding=ROUND_HALF_EVEN)):
            rounded = (Decimal(change)*100/Decimal(previous)).quantize(Decimal('0.01'))
            value = format(Decimal(0) if rounded == 0 else rounded, '.2f')
    return {'change_percent': value, 'status': 'DEFINED' if previous > 0 else 'NON_POSITIVE_BASE'}


def _comparison_evidence(group, reference_limit):
    refs = sorted(group['refs']) if group is not None else []
    return {'refs': refs[:reference_limit], 'total': len(refs), 'truncated': len(refs) > reference_limit}


def _comparison_summary(items, period):
    values = {key: sum(item[period][key] for item in items) for key in
              ('transaction_count', 'debit_minor', 'credit_minor', 'net_change_minor')}
    return values


def _comparison_metric_definitions():
    """Rate paths are relative to each item, never to whole-period totals."""
    definitions = {'scope': 'EACH_ITEM', 'path_base': 'description_comparison.items[*]'}
    for direction in ('debit', 'credit'):
        definitions[direction+'_change_rate'] = {
            'numerator_path': 'change.'+direction+'_minor',
            'denominator_path': 'previous.'+direction+'_minor',
            'formula': '(current - previous) / previous * 100',
            'rounding': 'ROUND_HALF_EVEN_2DP', 'unit': 'percent'}
    return definitions


def description_comparison(current_rows, previous_rows, current_period, previous_period, *, currency):
    """An additive comparison of observed raw descriptions, not merchant identities."""
    group_limit, reference_limit = 20, 50
    requested = previous_rows is not None
    result = {'contract_version': 1, 'status': 'COMPLETED' if requested else 'NOT_REQUESTED',
        'group_by': ['description', 'transaction_kind'],
        'semantics': 'RAW_DESCRIPTION_NOT_MERCHANT_OR_INDUSTRY', 'currency': currency,
        'unit': 'minor_unit', 'current_period': dict(current_period),
        'previous_period': dict(previous_period) if requested else None,
        'formula': 'current - previous', 'metric_definitions': _comparison_metric_definitions(),
        'coverage_status': 'NOT_ESTABLISHED',
        'limit': group_limit, 'evidence_ref_limit_per_period': reference_limit,
        'sort': 'ABS_FLOW_CHANGE_DESC_GROSS_FLOW_DESC_DESCRIPTION_KIND_ASC',
        'all_evidence_refs_paths': {'current': 'totals.evidence_refs',
            'previous': 'previous_period.totals.evidence_refs' if requested else None},
        'total_groups': None, 'returned_groups': None, 'changed_group_count': None,
        'truncated': False, 'items': [], 'omitted': None, 'kind_summary': []}
    if not requested:
        return result
    current_groups, previous_groups = _comparison_groups(current_rows), _comparison_groups(previous_rows)
    items = []
    for key in sorted(current_groups.keys() | previous_groups.keys()):
        new_group, old_group = current_groups.get(key), previous_groups.get(key)
        current, previous = _comparison_period(new_group), _comparison_period(old_group)
        change = _comparison_delta(current, previous)
        changed = any(change[field] != 0 for field in
                      ('transaction_count', 'debit_minor', 'credit_minor', 'net_change_minor'))
        items.append({'description': key[0], 'transaction_kind': key[1],
            'current': current, 'previous': previous, 'change': change, 'changed': changed,
            'debit_change_rate': _comparison_rate(previous['debit_minor'], change['debit_minor']),
            'credit_change_rate': _comparison_rate(previous['credit_minor'], change['credit_minor']),
            'evidence': {'previous': _comparison_evidence(old_group, reference_limit),
                         'current': _comparison_evidence(new_group, reference_limit)}})
    items.sort(key=lambda item: (
        -abs(item['change']['debit_minor'])-abs(item['change']['credit_minor']),
        -sum(item[period][key] for period in ('previous', 'current') for key in ('debit_minor', 'credit_minor')),
        item['description'], item['transaction_kind']))
    visible, hidden = items[:group_limit], items[group_limit:]
    result.update(total_groups=len(items), returned_groups=len(visible),
                  changed_group_count=sum(item['changed'] for item in items),
                  truncated=len(items) > group_limit, items=visible)
    result['omitted'] = {'group_count': len(hidden), 'changed_group_count': sum(item['changed'] for item in hidden),
        **{period: _comparison_summary(hidden, period) for period in ('previous', 'current', 'change')}}
    for kind in sorted({item['transaction_kind'] for item in items}):
        same_kind = [item for item in items if item['transaction_kind'] == kind]
        hidden_kind = [item for item in hidden if item['transaction_kind'] == kind]
        result['kind_summary'].append({'transaction_kind': kind, 'total_groups': len(same_kind),
            'changed_group_count': sum(item['changed'] for item in same_kind),
            'omitted_groups': len(hidden_kind), 'omitted_changed_group_count': sum(item['changed'] for item in hidden_kind)})
    return result


def period_metadata(start, end):
    """Classify requested inclusive dates, not data completeness or future income."""
    full_month = (start.day == 1 and (start.year, start.month) == (end.year, end.month)
                  and end.day == monthrange(start.year, start.month)[1])
    return {'start_date': start.isoformat(), 'end_date': end.isoformat(),
            'day_count': (end-start).days+1, 'boundary': 'INCLUSIVE',
            'period_kind': 'FULL_CALENDAR_MONTH' if full_month else 'DATE_WINDOW',
            'calendar_month': start.isoformat()[:7] if full_month else None,
            'basis': 'REQUESTED_DATE_RANGE', 'coverage_status': 'NOT_ESTABLISHED'}


def report_policy_application(compare_previous):
    """Describe the existing rule's object; never extend it to description groups."""
    return {'contract_version': 1,
        'category_increase': {
            'scope': 'CATEGORY_AGGREGATED_DEBIT', 'group_by': ['category'],
            'amount_basis': 'SUM_OF_OUTGOING_MINOR',
            'policy_path': 'analysis_policy.category_increase',
            'result_path': 'category_changes[*].significant_increase_candidate',
            'not_applicable_to': ['DESCRIPTION_GROUP', 'SINGLE_TRANSACTION', 'RECURRING_SEQUENCE'],
            'status': 'EVALUATED' if compare_previous else 'NOT_REQUESTED'},
        'description_comparison': {
            'threshold_assessment': 'NOT_PERFORMED',
            'meaning': '只比较原描述与交易类型分组的观察金额和笔数，不应用类别显著上涨规则。'}}


def previous_comparison_window(start, end, queried):
    """Compute adjacent inclusive dates, independently of querying prior rows."""
    days = (end-start).days+1
    try:
        previous_start, previous_end = start-timedelta(days=days), start-timedelta(days=1)
    except OverflowError:
        return None
    return {'relation': 'PREVIOUS_EQUAL_LENGTH', 'boundary': 'INCLUSIVE',
            'start_date': previous_start.isoformat(), 'end_date': previous_end.isoformat(),
            'day_count': days, 'data_status': 'QUERIED' if queried else 'NOT_REQUESTED'}


def build_report(rows, start_date, end_date, *, currency, compare_previous=True, policy=None):
    policy = analysis_policy(policy)
    threshold = policy['category_increase']
    start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
    if start > end: raise ValueError('开始日期不能晚于结束日期')
    selected = [r for r in rows if start_date <= r['date'] <= end_date]
    current = totals(selected)
    previous = None
    previous_rows = None
    changes = []
    category_changes = []
    comparison_window = previous_comparison_window(start, end, compare_previous)
    if compare_previous:
        if comparison_window is None:
            raise ValueError('前一等长期间超出支持的日期范围')
        prev_start = date.fromisoformat(comparison_window['start_date'])
        prev_end = date.fromisoformat(comparison_window['end_date'])
        previous_rows = [r for r in rows if prev_start.isoformat() <= r['date'] <= prev_end.isoformat()]
        previous = {'start_date': prev_start.isoformat(), 'end_date': prev_end.isoformat(),
            'totals': totals(previous_rows)}
        previous['period_metadata'] = period_metadata(prev_start, prev_end)
        previous['metrics'] = report_metrics(previous['totals'], previous['start_date'], previous['end_date'], totals_path='previous_period.totals')
        previous['description_groups'] = description_groups(previous_rows, previous['start_date'], previous['end_date'], totals_path='previous_period.totals')
        for key in ('expense_minor', 'refund_minor', 'debit_minor', 'credit_minor', 'income_minor', 'net_change_minor'):
            old, new = previous['totals'][key], current[key]
            changes.append({'metric': key, 'previous_minor': old, 'current_minor': new,
                'change_minor': new-old, 'change': display(new-old),
                'change_percent': format(Decimal(new-old)*100/old, '.2f') if old else None,
                'boundary': '上期为零时不计算百分比；等长期间比较，不自动称同比或环比。'})
        old_groups = {g['category']: g for g in previous['totals']['categories']}
        new_groups = {g['category']: g for g in current['categories']}
        for category in sorted(set(old_groups) | set(new_groups)):
            old = old_groups.get(category, {}).get('debit_minor', 0)
            new = new_groups.get(category, {}).get('debit_minor', 0)
            category_changes.append({'category': category, 'previous_debit_minor': old,
                'current_debit_minor': new, 'change_minor': new-old, 'change': display(new-old),
                'significant_increase_candidate': old > 0 and new*100 >= old*(100+threshold['minimum_increase_percent']) and new-old >= threshold['minimum_absolute_increase_minor'],
                'rule': f"仅针对该类别汇总出账；上期出账非零且增幅至少{threshold['minimum_increase_percent']}%、绝对增加至少{display(threshold['minimum_absolute_increase_minor'])}源单位；不是用户预算或欺诈判断。",
                'evidence_refs': new_groups.get(category, {}).get('evidence_refs', [])})
    limitations = ['账单仅覆盖绑定资料；空期间表示未观察到流水，不代表账户没有活动。',
        '出账不等于消费，退款按本期入账展示，未据此修改原期账单。',
        '类别来自来源或交易类型，不做未经确认的商户语义推断。',
        'comparison_status仅表示是否执行比较，不证明前期资料齐全。',
        'comparison_window只计算相邻等长日期；data_status=NOT_REQUESTED时未查询前期资料，不支持比较金额或变化结论。',
        'period_metadata只分类请求日期，不证明流水完整；本期与前期各自分类，等长前期不自动等于上一个自然月。',
        'net_change_minor是该观察期间已纳入流水的净流入，可能含退款、期初及内部调拨净额；不是可支配储蓄、可用余额或未来月度存钱能力。',
        'DATE_WINDOW只能称所列期间，不能称每月；FULL_CALENDAR_MONTH只能称calendar_month所指的那个月，不能外推稳定的未来月度能力。']
    if not compare_previous:
        limitations.append('本次未请求期间比较；空changes/category_changes不代表无上涨或无变化。')
    comparison = description_comparison(selected, previous_rows,
        {'start_date': start_date, 'end_date': end_date},
        {'start_date': previous['start_date'], 'end_date': previous['end_date']} if previous else None,
        currency=currency)
    return {'report_contract_version': 2, 'description_comparison': comparison,
        'comparison_window': comparison_window,
        'policy_application': report_policy_application(compare_previous),
        'period_metadata': period_metadata(start, end),
        'metrics': report_metrics(current, start_date, end_date, totals_path='totals'),
        'description_groups': description_groups(selected, start_date, end_date, totals_path='totals'),
        'start_date': start_date, 'end_date': end_date, 'currency': currency,
        'comparison_status': 'COMPLETED' if compare_previous else 'NOT_REQUESTED',
        'analysis_policy': policy, 'totals': current, 'previous_period': previous, 'changes': changes, 'category_changes': category_changes,
        'anomaly_candidates': anomalies(selected),
        'limitations': limitations}


def detect_recurring(rows, *, as_of, currency, policy=None):
    price_threshold = analysis_policy(policy)['recurring_price_increase']['minimum_increase_percent']
    today = date.fromisoformat(as_of)
    groups = defaultdict(list)
    for row in rows:
        if row['date'] <= as_of and row['kind'] in ('expense', 'outflow') and row['amount_minor'] < 0:
            key = (row['account_id'], merchant_key(row['description']))
            if key[1]: groups[key].append(row)
    candidates = []
    for (account, merchant), all_rows in sorted(groups.items(), key=lambda x: str(x[0])):
        ordered = sorted(all_rows, key=lambda r: (r['date'], r['id']))[-6:]
        if len(ordered) < 3: continue
        dates = [date.fromisoformat(r['date']) for r in ordered]
        intervals = [(b-a).days for a, b in zip(dates, dates[1:])]
        monthly = all(25 <= n <= 35 for n in intervals)
        weekly = all(6 <= n <= 8 for n in intervals)
        if not (monthly or weekly): continue
        if (today-dates[-1]).days > 70: continue
        period = round(sum(intervals)/len(intervals))
        amounts = [-r['amount_minor'] for r in ordered]
        previous = amounts[-2]
        increase = all(v == previous for v in amounts[:-1]) and amounts[-1]*100 >= previous*(100+price_threshold)
        item = {'candidate_id': 'rec_'+digest([account, merchant, currency])[:24],
            'merchant_key': merchant, 'label': ordered[-1]['description'], 'account_id': account,
            'currency': currency, 'period_days': period, 'period_kind': 'monthly' if monthly else 'weekly',
            'evidence_refs': [r['id'] for r in ordered], 'observed_dates': [r['date'] for r in ordered],
            'amount_minor': amounts[-1], 'amount': display(amounts[-1]),
            'previous_amount_minor': previous, 'price_increase': increase,
            'next_due_date': (dates[-1]+timedelta(days=period)).isoformat(),
            'algorithm_version': ALGORITHM,
            'boundary': '至少三次周期性出账候选；不证明订阅合同或银行代扣，未来日期是观测推算。'}
        item['evidence_hash'] = digest(item)
        candidates.append(item)
    return candidates
