"""Resolve calculation operands through an explicitly injected read-only reader."""
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
import hashlib
import json
import re

from src.bank_v1.common import canonical
from src.competition_web.money_calculate import CalculationError, calculate, require
from .facts import SUPPORTED_TOOLS, catalog, snapshot_day
from .calculation_store import verify_envelope
from .calculation_tree import leaves, depth, MAX_DEPTH


SOURCE_KEYS = frozenset({'left_source', 'right_source'})


def source_schema():
    return {'oneOf': [
        {'type': 'object', 'properties': {
            'kind': {'const': 'bank_fact'},
            'tool': {'type': 'string', 'enum': sorted(SUPPORTED_TOOLS)},
            'arguments': {'type': 'object'},
            'fact_id': {'type': 'string', 'pattern': '^fact:[a-f0-9]{64}$'}},
         'required': ['kind', 'tool', 'arguments', 'fact_id'], 'additionalProperties': False},
        {'type': 'object', 'properties': {
            'kind': {'const': 'user_assumption'},
            'quote': {'type': 'string', 'minLength': 1, 'maxLength': 1000}},
         'required': ['kind', 'quote'], 'additionalProperties': False},
        {'type': 'object', 'properties': {
            'kind': {'const': 'calculation_result'},
            'evidence_id': {'type': 'string', 'pattern': '^calculation:[a-f0-9]{64}$'}},
         'required': ['kind', 'evidence_id'], 'additionalProperties': False}]}


def resolve(source, value, read_tool, snapshots, read_calculation=None):
    require(type(source) is dict, '输入来源必须是对象。')
    if source.get('kind') == 'calculation_result':
        require(set(source) == {'kind', 'evidence_id'} and type(source['evidence_id']) is str
                and callable(read_calculation), '须引用本实例保存的受信计算。')
        envelope = read_calculation(source['evidence_id'])
        parent = verify_envelope(envelope)
        require(depth(parent) < MAX_DEPTH, '连续计算最多四层。', 'CALCULATION_CHAIN_LIMIT')
        require(parent['result']['kind'] == 'amount' and parent['inputs']['currency'] == 'CNY',
                '前项必须是人民币金额，百分比不能作为金额。', 'FACT_UNIT_MISMATCH')
        require(parent['result']['amount_minor'] == value, '声明金额与前项计算不一致。', 'FACT_VALUE_MISMATCH')
        for leaf in leaves(parent):
            if leaf['kind'] == 'bank_fact':
                fact = leaf['fact']
                resolve({'kind': 'bank_fact', 'tool': fact['source_tool'],
                         'arguments': fact['source_arguments'], 'fact_id': fact['fact_id']},
                        fact['value'], read_tool, snapshots)
        return {'kind': 'calculation_result', 'evidence_id': envelope['evidence_id'],
                'value_minor': value, 'status': 'RESOLVED_WITH_FRESH_ROOT_FACTS', 'envelope': envelope}
    if source.get('kind') == 'user_assumption':
        require(set(source) == {'kind', 'quote'} and type(source['quote']) is str
                and 1 <= len(source['quote']) <= 1000, '用户假设须提供原话。')
        return {'kind': 'user_assumption', 'value_minor': value,
                'quote': source['quote'], 'status': 'PENDING_ORIGINAL_PROMPT_BINDING',
                'is_posted_bank_fact': False}
    require(set(source) == {'kind', 'tool', 'arguments', 'fact_id'}
            and source.get('kind') == 'bank_fact'
            and type(source.get('tool')) is str and source['tool'] in SUPPORTED_TOOLS
            and type(source.get('arguments')) is dict
            and type(source.get('fact_id')) is str,
            '银行来源只接受受支持的只读事实引用。')
    key = canonical({'tool': source['tool'], 'arguments': source['arguments']})
    if key not in snapshots:
        snapshots[key] = read_tool(source['tool'], source['arguments'])
    facts = catalog(source['tool'], source['arguments'], snapshots[key])['facts']
    matches = [fact for fact in facts if fact['fact_id'] == source['fact_id']]
    require(len(matches) == 1, '来源已过期或不属于该查询，请重新查询。', 'STALE_OR_UNKNOWN_FACT')
    fact = matches[0]
    require(fact['unit'] == 'minor' and fact['currency'] == 'CNY'
            and type(fact['value']) is int, '来源不是人民币整数分金额。', 'FACT_UNIT_MISMATCH')
    require(fact['value'] == value, '声明金额与该事实不一致。', 'FACT_VALUE_MISMATCH')
    return {'kind': 'bank_fact', 'status': 'RESOLVED_BY_FRESH_BANK_READ', 'fact': fact}


def calculate_bound(args, read_tool, read_calculation=None):
    """No writes. A reference is verified by value and meaning, not by ID alone."""
    require(type(args) is dict and SOURCE_KEYS <= args.keys(), '两项输入都须声明来源。')
    legacy = {key: value for key, value in args.items() if key not in SOURCE_KEYS}
    result = calculate(legacy)
    require(legacy.get('currency') == 'CNY', '有来源金额计算须明确人民币。', 'FACT_UNIT_MISMATCH')
    snapshots = {}
    provenance = {side: resolve(args[side + '_source'], args[side + '_minor'], read_tool, snapshots, read_calculation)
                  for side in ('left', 'right')}
    # A second read detects an update between queries. This is an as-of read,
    # not a reservation or transaction guarantee; bank execution has its own guard.
    for key, snapshot in snapshots.items():
        query = json.loads(key)
        fresh = read_tool(query['tool'], query['arguments'])
        require(fresh['evidence_id'] == snapshot['evidence_id']
                and snapshot_day(fresh) == snapshot_day(snapshot),
                '计算期间事实发生变化，请重新查询。', 'FACT_CHANGED_DURING_CALCULATION')
    calculation = result['data']['calculation']
    has_assumptions = any(p['kind'] == 'user_assumption' for p in leaves({'input_provenance': provenance}))
    version = 3 if any(p['kind'] == 'calculation_result' for p in provenance.values()) else 2
    calculation = {**calculation, 'contract_version': version, 'input_provenance': provenance,
                   'financial_inputs_verified': not has_assumptions,
                   'source_references_status': ('BANK_RESOLVED_ASSUMPTIONS_PENDING' if has_assumptions
                                                else 'BANK_FACTS_RESOLVED'),
                   'limitations': [
                       '事实来源已按查询、主体、字段、日期及版本解析；这不是银行资金预留或付款授权。',
                       '用户假设须在回答边界关联原消息；假设和预测不等于实际入账。',
                       '输入核验不证明所选算式能解决业务问题；须核对情景、期间和操作含义。']}
    result = {**result, 'data': {'calculation': calculation}}
    result['evidence_id'] = 'calculation:' + hashlib.sha256(canonical(calculation).encode('utf-8')).hexdigest()
    return result


def assumption_matches(provenance, prompt, *, read_only_spending=False):
    """Bind a declared numeral to original text, without claiming intent inference.

    Deliberately bounded to a single explicit yuan amount in the quote. Other
    expressions need clarification, not a guessed conversion or silent approval.
    """
    quote = provenance.get('quote')
    from .assumption_scope import AssumptionScope
    if type(quote) is not str or not quote:
        return False
    bound = (prompt.contains(quote, provenance.get('value_minor')) if isinstance(prompt, AssumptionScope)
             else type(prompt) is str and quote in prompt)
    if not bound:
        return False
    amounts = re.findall(r'(?<![\d.,A-Za-z_])(-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?)\s*(万?元)', quote)
    if not amounts:
        if isinstance(prompt, AssumptionScope):
            return prompt.omitted_spending_matches(quote, provenance.get('value_minor'))
        if read_only_spending:
            from .spending_amount import omitted_yuan_matches
            return omitted_yuan_matches(quote, prompt, provenance.get('value_minor'))
    if len(amounts) != 1:
        return False
    amount, unit = amounts[0]
    with localcontext(Context(prec=40, rounding=ROUND_HALF_EVEN)):
        value = Decimal(amount.replace(',', '')) * (1000000 if unit == '万元' else 100)
    return value == provenance.get('value_minor')


def provenance_reasons(records, prompt):
    """Only new bound calculations enter this check; legacy API is unchanged."""
    reasons = []
    for record in records:
        if record.get('tool') != 'money_calculate':
            continue
        calc = record.get('response', {}).get('data', {}).get('calculation', {})
        if not calc.get('input_provenance'):
            continue
        try:
            sources = leaves(calc)
        except (ValueError, KeyError, TypeError):
            reasons.append({'code': 'INVALID_CALCULATION_PROVENANCE'})
            continue
        for source in sources:
            if source.get('kind') == 'user_assumption' and not assumption_matches(source, prompt):
                reasons.append({'code': 'UNBOUND_USER_CALCULATION_ASSUMPTION'})
    return reasons
