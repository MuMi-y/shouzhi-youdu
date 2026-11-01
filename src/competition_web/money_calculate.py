"""Deterministic arithmetic over declared inputs; no bank facts are fetched."""
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
import hashlib
import json

MAX_INPUT_MINOR = 1_000_000_000_000
OPERATIONS = ('add', 'subtract', 'absolute_difference', 'percentage')
REQUIRED = frozenset({'operation', 'left_minor', 'right_minor'})
OPTIONAL = frozenset({'currency', 'left_evidence_id', 'right_evidence_id'})


class CalculationError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def require(condition, message, code='INVALID_ARGUMENTS'):
    if not condition:
        raise CalculationError(code, message)


def validated_inputs(args):
    require(type(args) is dict, '参数须为 JSON 对象。')
    require(REQUIRED <= args.keys() and not args.keys() - REQUIRED - OPTIONAL,
            '必须提供 operation/left_minor/right_minor，且不得含额外字段。')
    require(type(args['operation']) is str and args['operation'] in OPERATIONS,
            '不支持此计算类型。')
    for name in ('left_minor', 'right_minor'):
        require(type(args[name]) is int and abs(args[name]) <= MAX_INPUT_MINOR,
                '操作数须为绝对值不超过 10^12 的整数分；不接受小数、字符串或布尔值。')
    currency = args.get('currency')
    require(currency is None or (type(currency) is str and currency == 'CNY'),
            'currency 只接受 CNY 或 null；不提供时保持未知。')
    references = {}
    for name in ('left_evidence_id', 'right_evidence_id'):
        value = args.get(name)
        require(value is None or (type(value) is str and 1 <= len(value) <= 200
                and all(ord(character) >= 32 and ord(character) != 127 for character in value)),
                '来源引用只接受 1 至 200 字符的单行声明或 null。')
        references[name] = value
    return {'operation': args['operation'], 'left_minor': args['left_minor'],
            'right_minor': args['right_minor'], 'currency': currency, **references}


def decimal_text(value):
    # A new Context avoids inheriting callers' rounding/traps/global precision.
    with localcontext(Context(prec=40, rounding=ROUND_HALF_EVEN)):
        rounded = value.quantize(Decimal('0.01'))
        return format(Decimal(0) if rounded == 0 else rounded, '.2f')


def calculate(args):
    """Pure function. Hashes identify a calculation, not independently verified facts."""
    inputs = validated_inputs(args)
    operation = inputs['operation']
    left, right = inputs['left_minor'], inputs['right_minor']
    if operation == 'percentage':
        require(right != 0, '百分比的分母不能为零；结果未定义。', 'ZERO_DENOMINATOR')
        with localcontext(Context(prec=40, rounding=ROUND_HALF_EVEN)):
            percent = decimal_text(Decimal(left) * Decimal(100) / Decimal(right))
        result = {'kind': 'percentage', 'amount_minor': None, 'amount': None,
                  'percent': percent, 'unit': 'percent', 'formula': 'left_minor / right_minor * 100'}
    else:
        amount = {'add': lambda: left + right, 'subtract': lambda: left - right,
                  'absolute_difference': lambda: abs(left - right)}[operation]()
        with localcontext(Context(prec=40, rounding=ROUND_HALF_EVEN)):
            display = decimal_text(Decimal(amount) / Decimal(100))
        formula = {'add': 'left_minor + right_minor', 'subtract': 'left_minor - right_minor',
                   'absolute_difference': 'abs(left_minor - right_minor)'}[operation]
        result = {'kind': 'amount', 'amount_minor': amount, 'amount': display,
                  'percent': None, 'unit': 'minor', 'formula': formula}
    calculation = {'contract_version': 1, 'inputs': inputs, 'result': result,
        'calculation_verified': True, 'financial_inputs_verified': False,
        'source_references_status': 'DECLARED_NOT_RESOLVED',
        'rounding': {'percentage_decimal_places': 2, 'mode': 'ROUND_HALF_EVEN',
                     'money_result': 'EXACT_INTEGER_MINOR', 'scale': 100},
        'limitations': [
            '只证明所给数字按声明公式计算；未查询银行、核验来源引用或确认币种。',
            '须先核实两个输入属于同一币种、单位、期间和统计口径；本工具不换汇。',
            'percentage 是左值除以右值再乘100，不是增长率；增长率须先计算新值减旧值，再除以旧值。',
            '计算不新建任务、不办理转账、不更改银行记录。']}
    canonical = json.dumps(calculation, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return {'evidence_id': 'calculation:' + hashlib.sha256(canonical.encode('utf-8')).hexdigest(),
            'scope': 'bank', 'currency': inputs['currency'], 'amount_unit': 'minor', 'scale': 100,
            'data': {'calculation': calculation}}


def tool_spec():
    integer = {'type': 'integer', 'minimum': -MAX_INPUT_MINOR, 'maximum': MAX_INPUT_MINOR}
    reference = {'type': ['string', 'null'], 'minLength': 1, 'maxLength': 200,
                 'pattern': '^[^\\x00-\\x1f\\x7f]+$'}
    return {'name': 'money_calculate',
        'description': '只读确定性计算：整数分 add(左+右)、subtract(左-右)、absolute_difference(绝对差)、percentage(左/右×100，保留两位且五成双)。增长率先subtract再percentage。来源编号仅声明不验证；须先用银行工具取得事实，不把计算输入当已核实账本。不支持执行转账。',
        'parameters': {'type': 'object', 'properties': {
            'operation': {'type': 'string', 'enum': list(OPERATIONS)},
            'left_minor': integer.copy(), 'right_minor': integer.copy(),
            'currency': {'type': ['string', 'null'], 'enum': ['CNY', None]},
            'left_evidence_id': reference.copy(), 'right_evidence_id': reference.copy()},
            'required': sorted(REQUIRED), 'additionalProperties': False}}
