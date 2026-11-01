"""Explicit report sections; no model-authored financial facts or labels."""
from .answer import display_value
from .ledger_facts import MONEY_FIELDS

SECTIONS = frozenset({'overview', 'comparison', 'descriptions', 'description_comparison', 'ratios', 'duplicates'})


def money(value):
    return display_value({'unit': 'minor', 'value': value})


def period(section):
    meta = section['period_metadata']
    kind = f"完整自然月{meta['calendar_month']}" if meta['period_kind'] == 'FULL_CALENDAR_MONTH' else '指定日期窗口'
    return f"{section['start_date']}至{section['end_date']}，含首尾{meta['day_count']}天，{kind}"


def overview(report):
    total = report['totals']
    lines = [f"本期资料观察到{total['count']}笔流水。"]
    for fields in [('income_minor', 'expense_minor', 'refund_minor', 'net_expense_minor'),
                   ('debit_minor', 'credit_minor', 'net_change_minor'),
                   ('internal_transfer_net_minor', 'opening_entries_minor', 'unclassified_debit_minor', 'unclassified_credit_minor')]:
        lines.append(' '.join(f"{MONEY_FIELDS[field]}：{money(total[field])}。" for field in fields))
    lines.append('本人账户之间转账不计作消费或收入；期初记账也不计作收入。')
    if total['count'] == 0:
        lines.append('零笔仅表示这份资料未观察到，不证明现实账户没有活动。')
    return lines


def covered_fact(fact, report, evidence_id, sections):
    """Deduplicate only fields actually rendered from this identical report."""
    if fact['source_tool'] != 'billing_report' or fact['evidence_id'] != evidence_id:
        return False
    period = fact.get('period') or {}
    if (period.get('start'), period.get('end')) == (report['start_date'], report['end_date']):
        return 'overview' in sections and fact['field'] in MONEY_FIELDS
    previous = report['previous_period']
    return bool(previous and 'comparison' in sections and
                (period.get('start'), period.get('end')) == (previous['start_date'], previous['end_date']) and
                fact['field'] in {item['metric'] for item in report['changes']})


def comparison(report):
    previous = report['previous_period']
    if previous is None:
        return ['本轮未查询前期，不能判断期间变化。']
    lines = ['比较对象是相邻的前一等长期间：' + period(previous) + '。不能自动称为两个自然月环比。']
    for item in report['changes']:
        lines.append(f"{MONEY_FIELDS[item['metric']]}：前期{money(item['previous_minor'])}，本期{money(item['current_minor'])}，"
                     f"本期减前期为{money(item['change_minor'])}。")
    return lines


def descriptions(report):
    groups = report['description_groups']
    lines = [f"按原描述与交易类型分组，共{groups['total_groups']}组，显示{groups['returned_groups']}组；原描述不证明商户身份或行业。"]
    for item in groups['items']:
        lines.append(f"原描述「{item['description']}」/类型{item['transaction_kind']}：{item['transaction_count']}笔，"
                     f"出账{money(item['debit_minor'])}，入账{money(item['credit_minor'])}。")
    if groups['truncated']:
        omitted = groups['omitted']
        lines.append(f"未显示{omitted['group_count']}组、{omitted['transaction_count']}笔，"
                     f"出账{money(omitted['debit_minor'])}，入账{money(omitted['credit_minor'])}；未显示不等于不存在。")
    return lines


def description_comparison(report):
    groups = report['description_comparison']
    if groups['status'] != 'COMPLETED':
        return ['本轮未计算原描述分组比较，不能据此说各组没有变化。']
    lines = ['原描述分组比较的前期：' + period(report['previous_period']) + '。',
             f"完整并集{groups['total_groups']}组，其中{groups['changed_group_count']}组有变化；"
             f"显示{groups['returned_groups']}组。原描述不证明商户身份，以下不是类别上涨阈值判断。"]
    for item in groups['items']:
        lines.append(f"原描述「{item['description']}」/类型{item['transaction_kind']}："
                     f"出账由{money(item['previous']['debit_minor'])}变为{money(item['current']['debit_minor'])}，"
                     f"差额{money(item['change']['debit_minor'])}；"
                     f"入账由{money(item['previous']['credit_minor'])}变为{money(item['current']['credit_minor'])}，"
                     f"差额{money(item['change']['credit_minor'])}。")
    if groups['truncated']:
        omitted = groups['omitted']
        lines.append(f"未显示{omitted['group_count']}组，其中{omitted['changed_group_count']}组有变化。"
                     '隐藏组净差相抵不代表没有变化。')
    return lines


def ratios(report):
    labels = {'expense_to_income': '消费支出/收入', 'net_flow_to_income': '净流入/收入'}
    lines = []
    for section in [report] + ([report['previous_period']] if report['previous_period'] else []):
        for metric in section['metrics']:
            value = metric['value_percent'] + '%' if metric['status'] == 'DEFINED' else '未定义（收入非正数）'
            lines.append(f"{section['start_date']}至{section['end_date']} {labels[metric['name']]}：{value}。{metric['meaning']}")
    return lines


def duplicates(report):
    items = report['anomaly_candidates']
    if not items:
        return ['这份报告未发现同日同账户同描述同金额的疑似重复候选；不代表已排除其他异常。']
    return [f"{item['date']}，归一化描述「{item['merchant_key']}」，同金额{money(item['amount_minor'])}的"
            f"{len(item['evidence_refs'])}笔流水构成疑似重复候选。可能是多次真实消费，需核对订单或收据；"
            '不能据此确认重复、欺诈或自动退款。' for item in items]


def render(report, sections):
    renderers = {'overview': overview, 'comparison': comparison, 'descriptions': descriptions,
                 'description_comparison': description_comparison, 'ratios': ratios, 'duplicates': duplicates}
    scope = report.get('account_scope', {})
    label = (f"{scope['account_name']}（{scope['account_id']}）" if scope.get('kind') == 'ACCOUNT'
             else '全部已授权账户合计')
    lines = [label + '｜本期账单：' + period(report) + '。']
    for name in sections:
        lines.extend(renderers[name](report))
    lines.append('统计仅覆盖已提供的银行流水，完整日期窗口不证明资料完整；期间净流入不是当前余额、可支配储蓄或未来每月存钱能力。本段账单查询不办理银行操作；同轮其他办理的状态见对应任务或银行操作。')
    return '\n\n'.join(lines)
