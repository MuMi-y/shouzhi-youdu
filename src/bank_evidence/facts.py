"""Project explicitly supported bank fields into stable semantic references.

Do not recursively collect numbers: a balance, an income and a future estimate
are different facts even when they share a value.
"""
import hashlib

from src.bank_v1.common import canonical


CORE_TOOLS = frozenset({'accounts', 'arrangements', 'forecast'})
SUPPORTED_TOOLS = CORE_TOOLS | {'transactions', 'spending_summary', 'billing_report'}


def snapshot_day(envelope):
    return envelope['business_date'] if 'business_date' in envelope else envelope['as_of']


def catalog(tool, arguments, envelope):
    """Pure projection. The caller supplies an actual successful bank response."""
    if tool not in SUPPORTED_TOOLS:
        return {'version': 1, 'facts': []}
    data = envelope['data']
    base = {'source_tool': tool, 'source_arguments': arguments,
            'evidence_id': envelope['evidence_id'],
            'business_date': snapshot_day(envelope), 'subject_id': 'U1',
            'currency': envelope['currency']}
    facts = []

    def add(entity, field, value, *, unit, label, version=None,
            scenario='RECORDED', period=None):
        fact = {**base, 'entity': entity, 'field': field, 'value': value,
                'unit': unit, 'label': label, 'version': version,
                'scenario': scenario, 'period': period}
        identifier = hashlib.sha256(canonical(fact).encode('utf-8')).hexdigest()
        facts.append({'fact_id': 'fact:' + identifier, **fact})

    if tool == 'accounts':
        for account in data['accounts']:
            add(account['account_id'], 'balance_minor', account['balance_minor'],
                unit='minor', label=f"{account['account_id']}账户已到账余额",
                version=account['version'])
        add('COMBINED_ACCOUNTS', 'total_balance_minor', data['total_balance_minor'],
            unit='minor', label='本人账户合计已到账余额',
            version={a['account_id']: a['version'] for a in data['accounts']})
    elif tool == 'arrangements':
        for item in data['arrangements']:
            period = {'due_date': item['due_date'], 'status': item['status'],
                      'kind': item['kind']}
            for field, unit, meaning in [('amount_minor', 'minor', '安排金额'),
                                         ('due_date', 'date', '原定日期'),
                                         ('status', 'status', '记录状态')]:
                add(item['arrangement_id'], field, item[field], unit=unit,
                    label=f"{item['arrangement_id']} {item['title']}：{meaning}",
                    version=item['version'], period=period,
                    scenario='USER_ARRANGEMENT_NOT_POSTED')
    elif tool == 'forecast':
        period = {'start': data['as_of_date'], 'end': data['through_date'],
                  'proposed_expense_minor': arguments.get('proposed_expense_minor', 0),
                  'proposed_date': arguments.get('proposed_date') or data['as_of_date']}
        add('COMBINED_ACCOUNTS', 'current_balance_minor', data['current_balance_minor'],
            unit='minor', label='预测起点的合计已到账余额', period=period)
        for scenario, prefix in [('PRIMARY', '按有效安排计入预计收入'),
                                 ('WITHOUT_FUTURE_INCOME', '不计未来收入')]:
            point = data['scenario_minima'][scenario]
            bound_period = {**period, 'minimum_date': point['minimum_balance_date']}
            for field, unit, suffix in [('minimum_balance_minor', 'minor', '最低合计余额'),
                                        ('minimum_balance_date', 'date', '最低点日期')]:
                add('COMBINED_ACCOUNTS', field, point[field], unit=unit,
                    label=f'{prefix}情景的{suffix}', period=bound_period,
                    scenario=scenario)
            key = 'gap_minor' if scenario == 'PRIMARY' else 'without_future_income_gap_minor'
            add('COMBINED_ACCOUNTS', key, data[key], unit='minor',
                label=f'{prefix}情景的合计缺口', period=bound_period, scenario=scenario)
    else:
        from .ledger_facts import project
        project(tool, arguments, data, add)
    return {'version': 1, 'facts': facts}


def attach(tool, arguments, envelope):
    """Add metadata without changing the bank data or its existing evidence ID."""
    if tool not in SUPPORTED_TOOLS:
        return envelope
    return {**envelope, 'fact_catalog': catalog(tool, arguments, envelope)}
