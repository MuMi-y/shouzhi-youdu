"""Finite ledger projections; periods and transaction identities remain explicit."""

MONEY_FIELDS = {
    'income_minor': '收入', 'expense_minor': '消费支出', 'refund_minor': '退款',
    'net_expense_minor': '消费减退款', 'debit_minor': '全部出账',
    'credit_minor': '全部入账', 'net_change_minor': '账单净流入（不是余额或储蓄）',
    'internal_transfer_net_minor': '本人转账净流入',
    'opening_entries_minor': '期初记账', 'unclassified_debit_minor': '未分类出账',
    'unclassified_credit_minor': '未分类入账'}


def project(tool, arguments, data, add):
    if tool == 'transactions':
        for row in data['transactions']:
            period = {'start': arguments.get('start_date'), 'end': arguments.get('end_date'),
                      'transaction_date': row['business_date'], 'account_id': row['account_id'],
                      'kind': row['kind'], 'query_limit': arguments.get('limit', 100)}
            add(row['transaction_id'], 'amount_minor', row['amount_minor'], unit='minor',
                label=f"{row['account_id']}账户 {row['business_date']} {row['kind']}流水 {row['transaction_id']} 的带方向金额",
                scenario='POSTED_TRANSACTION', period=period)
        return
    if tool == 'spending_summary':
        sections = [('指定期间', data, data)]
    else:
        report = data['report']
        if arguments.get('account_id') != report.get('account_scope', {}).get('account_id'):
            raise ValueError('BILLING_ACCOUNT_SCOPE_MISMATCH')
        sections = [('本期', report, report['totals'])]
        if report['previous_period'] is not None:
            prior = report['previous_period']
            sections.append(('前一等长期间', prior, prior['totals']))
    for name, section, total in sections:
        period = {'start': section['start_date'], 'end': section['end_date'],
                  'boundary': 'INCLUSIVE', 'relation': name,
                  'coverage': 'AVAILABLE_BANK_RECORDS_ONLY'}
        account_id = arguments.get('account_id') if tool == 'billing_report' else None
        if account_id is not None:
            period['account_id'] = account_id
        for field, label in MONEY_FIELDS.items():
            if field in total:
                add(account_id or 'COMBINED_ACCOUNT_LEDGER', field, total[field], unit='minor',
                    label=f'{name}{label}', period=period, scenario='RECORDED_PERIOD_FLOW')
