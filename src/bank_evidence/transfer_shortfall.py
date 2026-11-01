"""A narrow preflight conclusion backed by a verified single-account calculation."""
import hashlib

from src.bank_v1.common import canonical
from src.bank_v1.balance_conditions import assess_balance
from .answer import display_value


def render(part, child):
    if set(part) != {'format', 'calculation_id'} or part['format'] != 'bank_transfer_shortfall_v1':
        raise ValueError('INVALID_TRANSFER_SHORTFALL')
    calculations = child['bank_facts_basis']['calculations']
    if len(calculations) != 1:
        raise ValueError('ONE_SHORTFALL_CALCULATION_REQUIRED')
    calc = calculations[0]
    identity = 'calculation:' + hashlib.sha256(canonical(calc).encode('utf-8')).hexdigest()
    if part['calculation_id'] != identity:
        raise ValueError('INVALID_SHORTFALL_CALCULATION_HASH')
    sources, inputs = calc['input_provenance'], calc['inputs']
    bank_sides = [side for side in ('left','right') if sources[side]['kind'] == 'bank_fact']
    assumed_sides = [side for side in ('left','right') if sources[side]['kind'] == 'user_assumption']
    if len(bank_sides) != 1 or len(assumed_sides) != 1:
        raise ValueError('SINGLE_ACCOUNT_AND_PROPOSED_AMOUNT_REQUIRED')
    bank_side, assumed_side = bank_sides[0], assumed_sides[0]
    fact = sources[bank_side]['fact']
    if (fact['source_tool'] != 'accounts' or fact['source_arguments'] != {} or
            fact['field'] != 'balance_minor' or fact['entity'] not in {'A','B'} or
            fact['subject_id'] != 'U1' or fact['currency'] != 'CNY' or fact['unit'] != 'minor' or
            fact['scenario'] != 'RECORDED' or inputs['currency'] != 'CNY'):
        raise ValueError('CURRENT_SINGLE_ACCOUNT_BALANCE_REQUIRED')
    balance, proposed = fact['value'], sources[assumed_side]['value_minor']
    if (type(balance) is not int or type(proposed) is not int or balance < 0 or
            proposed <= 0 or inputs[bank_side+'_minor'] != balance or
            inputs[assumed_side+'_minor'] != proposed):
        raise ValueError('TRANSFER_SHORTFALL_NOT_PROVEN')
    if assess_balance(balance, proposed).status != 'INSUFFICIENT_FUNDS':
        raise ValueError('TRANSFER_SHORTFALL_NOT_PROVEN')
    difference = inputs['left_minor'] - inputs['right_minor']
    operation = inputs['operation']
    if operation not in {'subtract','absolute_difference'}:
        raise ValueError('DIFFERENCE_CALCULATION_REQUIRED')
    expected = abs(difference) if operation == 'absolute_difference' else difference
    outcome = calc['result']
    if (outcome['kind'] != 'amount' or outcome['unit'] != 'minor' or
            type(outcome['amount_minor']) is not int or outcome['amount_minor'] != expected):
        raise ValueError('INCORRECT_SHORTFALL_CALCULATION')
    money = lambda value: display_value({'unit':'minor','value':value})
    return (f"按本轮查询，若从{fact['entity']}账户转出{money(proposed)}，"
            f"该账户现有余额为{money(balance)}，不足{money(abs(expected))}，当前余额不能支持这笔调拨。"
            '请调整拟转金额，或补足该账户资金后重新查询。\n'
            '这是该账户与拟议金额的余额条件检查，不是银行拒绝回执，也不表示已准备或办理转账；'
            '其他付款条件和独立确认仍需核对。')
