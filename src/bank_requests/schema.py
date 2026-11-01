"""One field contract shared by validation and the model-facing instructions."""
import json
from dataclasses import dataclass
from functools import lru_cache
from jsonschema import Draft202012Validator
from .protocol import MINIMAL, LEGACY


AMOUNTS = frozenset({'amount_minor', 'reserve_minor', 'minimum_minor', 'maximum_minor',
                     'income_minor', 'expense_minor'})
ACCOUNTS = frozenset({'source_account', 'target_account'})
DAY_FIELDS = frozenset({'date_text', 'latest_date_text'})
DATE_FIELDS = DAY_FIELDS | {'through_date_text'}
FIELDS = {
    'account_overview': ACCOUNTS,
    'billing': frozenset({'period_text', 'start_date', 'end_date', 'source_account'}),
    'transfer': ACCOUNTS | AMOUNTS | {'date_text'},
    'transfer_advice': ACCOUNTS | AMOUNTS | {'date_text'},
    'cashflow_compare': ACCOUNTS | AMOUNTS | {'date_text', 'through_date_text', 'purpose'},
    'cashflow_forecast': frozenset({'through_date_text'}),
    'subscription_review': frozenset({'merchant_text', 'period_text'}),
    'mandate_query': frozenset(),
    'mandate_cancel': frozenset({'merchant_text', 'mandate_id'}),
    'goal_compare': AMOUNTS | DAY_FIELDS | {'goal_title', 'purpose', 'priority'},
    'goal_followup': frozenset({'goal_text'}),
    'task_status': frozenset({'task_id'}),
    'arrangement': AMOUNTS | {'date_text', 'purpose'},
    'general': frozenset({'request_text'}),
}
REQUIRED = {'transfer': frozenset({'source_account', 'target_account', 'amount_minor'}),
            'cashflow_forecast': frozenset({'through_date_text'})}
OBJECT_KINDS = {
    'transfer': ('operation', 'task'), 'transfer_advice': ('operation', 'task'),
    'mandate_query': ('operation', 'task'), 'mandate_cancel': ('operation', 'task'),
    'arrangement': ('operation', 'task'), 'task_status': ('task',),
    'goal_compare': ('goal', 'goal_draft'), 'goal_followup': ('goal', 'goal_draft'),
}


@dataclass(frozen=True)
class FieldSpec:
    value_type: str
    normalizations: tuple = ()


# Field names, value kinds and accepted source transforms share one definition.
FIELD_SPECS = {field: FieldSpec('text') for fields in FIELDS.values() for field in fields}
FIELD_SPECS.update({field: FieldSpec('integer_minor') for field in AMOUNTS})
FIELD_SPECS.update({field: FieldSpec('account', ('account_name',)) for field in ACCOUNTS})
FIELD_SPECS.update({field: FieldSpec('date', ('calendar_period', 'calendar_day')) for field in ('start_date', 'end_date')})
FIELD_SPECS['period_text'] = FieldSpec('period_text', ('calendar_period',))
FIELD_SPECS.update({field: FieldSpec('date_text', ('calendar_day',)) for field in DATE_FIELDS})

# Only exact business correspondences belong here. A reserve is a condition,
# not the transfer amount; an expected income is not an available bank balance.
PARAMETER_BINDINGS = {
    'transfer': {'source_account': 'source_account', 'target_account': 'target_account', 'amount_minor': 'amount_minor'},
    'mandate_cancel': {'mandate_id': 'mandate_id'},
    'arrangement': {'amount_minor': 'amount_minor', 'date_text': 'due_date',
                    'income_minor': 'amount_minor', 'expense_minor': 'amount_minor'},
}
GOAL_BINDINGS = {'amount_minor': 'preferred_amount_minor', 'minimum_minor': 'min_amount_minor',
                 'reserve_minor': 'reserve_floor_minor', 'date_text': 'preferred_date',
                 'latest_date_text': 'latest_date'}
GOAL_TEXT_BINDINGS = {'purpose': ('title', 'user_reason'), 'priority': ('priority_reason',)}


def required_fields(intent, action):
    if action.get('effective_kind') == 'CHECK_STATUS':
        return frozenset()
    return REQUIRED.get(intent, frozenset())


def needs_parameter_input(item):
    status_only = (item.get('object_contract') == 1 and item['intent'] in OBJECT_KINDS
                   and item.get('action', {}).get('effective_kind') == 'CHECK_STATUS')
    return not status_only and bool(item['missing_fields'] or item['conflicts'])


def model_contract():
    return json.dumps({intent: {
        'allowed_user_slots': sorted(fields - {'goal_title'}),
        'required_except_CHECK_STATUS': sorted(REQUIRED.get(intent, ())),
        'status_object_kinds': list(OBJECT_KINDS.get(intent, ())),
        'exact_operation_bindings': PARAMETER_BINDINGS.get(intent, {}),
        **({'readonly_preview_references': {
            'action': 'ANALYZE', 'fields': ['goal_title', 'purpose', 'priority'],
            'format': 'goal_profile_field_v1', 'ordinary_goal_title_allowed': False},
            'prepare_slots': sorted(set(GOAL_BINDINGS) | set(GOAL_TEXT_BINDINGS)),
            'prepare_identity': 'purpose以原value/source对应profile.title/user_reason；priority对应priority_reason；禁止goal_title或reference。'}
           if intent == 'goal_compare' else {}),
        **({'date_roles': {'date_text': 'proposed_date 消费发生日',
                           'through_date_text': 'through_date 预测截止日'}} if intent == 'cashflow_compare' else {}),
        **({'date_roles': {'through_date_text': 'through_date 查询截止日'},
            'action': 'ANALYZE only; no proposed consumption'} if intent == 'cashflow_forecast' else {}),
    } for intent, fields in FIELDS.items()}, ensure_ascii=False, separators=(',', ':'))


def field_contract(source_contract=LEGACY):
    return json.dumps({name: {'value_type': 'readonly_preview_reference' if name == 'goal_title' else spec.value_type,
        'source_normalizations': [] if source_contract == MINIMAL else list(spec.normalizations)} for name, spec in FIELD_SPECS.items()},
        ensure_ascii=False, separators=(',', ':'))


# Structural schema uses the installed DSH supported subset.

def object_schema(properties, required=None):
    return {'type': 'object', 'properties': properties,
            'required': list(properties) if required is None else required, 'additionalProperties': False}

def choice(branches):
    return branches[0] if len(branches) == 1 else {'oneOf': branches}

def source_schema(normalizations=()):
    properties = {'message_id': {'type': 'string'}, 'quote': {'type': 'string'}}
    branches = []
    for kind in normalizations:
        fields = {'kind': {'type': 'string', 'const': kind}, 'evidence_id': {'type': 'string'}}
        if kind == 'calendar_period':
            fields['period'] = {'type': 'string', 'enum': ['CURRENT_MONTH', 'PREVIOUS_MONTH', 'PAST_30_DAYS']}
        branches.append(object_schema(fields))
    if branches:
        properties['normalization'] = choice(branches)
    return object_schema(properties, ['message_id', 'quote'])

def slot_schema(field, *, allow_reference=False, source_contract=LEGACY):
    from .goal_profile_reference import FIELDS as reference_fields, FORMAT as reference_format
    spec = FIELD_SPECS[field]
    value = {'type': 'integer' if spec.value_type == 'integer_minor' else 'string'}
    if field in {'source_account', 'target_account'}:
        value['enum'] = ['A', 'B']
    normal = object_schema({'value': value, 'source': source_schema(() if source_contract == MINIMAL else spec.normalizations)})
    if allow_reference and field in reference_fields:
        reference = object_schema({'reference': object_schema({
            'format': {'type': 'string', 'const': reference_format},
            'call_id': {'type': 'string'}, 'source_id': {'type': 'string'},
            'field': {'type': 'string', 'const': reference_fields[field]}}), 'source': source_schema()})
        return reference if field == 'goal_title' else choice([normal, reference])
    return normal

def item_schema(*, require_action=True, source_contract=LEGACY, for_binding=False):
    from .actions import KINDS
    fields = {
        'id': {'type': 'string'}, 'intent': {'type': 'string', 'enum': sorted(FIELDS)},
        'continuation_of': choice([{'type': 'null'}, {'type': 'string'}]),
        'action': object_schema({'kind': {'type': 'string', 'enum': sorted(KINDS)},
                                 'source': source_schema()}),
        'slots': object_schema({f: slot_schema(f, allow_reference=not for_binding, source_contract=source_contract)
                               for f in sorted(FIELD_SPECS) if not (for_binding and f == 'goal_title')}, []),
        'missing_fields': {'type': 'array', 'items': {'type': 'string', 'enum': sorted(FIELD_SPECS)}},
        'conflicts': {'type': 'array', 'items': {'type': 'string', 'enum': sorted(FIELD_SPECS)}}}
    return object_schema(fields, [k for k in fields if require_action or k != 'action'])

def binding_schema(source_contract=LEGACY):
    result = object_schema({'items': {'type': 'array', 'items': item_schema(source_contract=source_contract, for_binding=True)},
        'node_items': {'type': 'object', 'additionalProperties': True,
                       'description': '节点id到需求item_id字符串的映射；宿主校验每个键值及节点归属。'}}, ['items'])
    result['description'] = ('结构与最终items同源，准备/任务写入绑定仅接受普通value/source，不接受只读预览reference或goal_title；'
                             '目标名称和用途沿用purpose，优先事项用priority。字段适用、数量/长度、来源/动作及业务条件由宿主继续严格验证。')
    return result


@lru_cache(maxsize=None)
def slot_validator(field, source_contract=LEGACY):
    return Draft202012Validator(slot_schema(field, source_contract=source_contract))


def validate_slot_structure(field, slot, source_contract=LEGACY):
    definition = slot_schema(field, source_contract=source_contract)
    if type(slot) is not dict or set(slot) != set(definition['required']):
        raise ValueError('INVALID_SLOT')
    source = slot['source']
    source_definition = definition['properties']['source']
    if (type(source) is not dict or not set(source_definition['required']) <= set(source)
            or not set(source) <= set(source_definition['properties'])):
        raise ValueError('INVALID_SLOT_SOURCE')
    if not slot_validator(field, source_contract).is_valid(slot):
        raise ValueError('INVALID_SLOT_STRUCTURE')


def source_examples():
    """Examples use the same builders/field definitions as the structural gate."""
    examples = {'minimal_account': {'value': 'A', 'source': {'message_id': 'm', 'quote': '日用金'}}}
    for field in ('source_account', 'start_date', 'date_text'):
        spec = FIELD_SPECS[field]
        for kind in spec.normalizations:
            if kind in examples:
                continue
            quote, value = {'account_name': ('日用金', 'A'), 'calendar_day': ('今天', '2026-10-12'),
                            'calendar_period': ('本月', '本月' if field == 'period_text' else '2026-10-01')}[kind]
            normal = {'kind': kind, 'evidence_id': '本轮实际证据ID'}
            if kind == 'calendar_period':
                normal['period'] = 'CURRENT_MONTH'
            example = {'value': value, 'source': {'message_id': 'm', 'quote': quote, 'normalization': normal}}
            validate_slot_structure(field, example)
            examples[kind] = example
    return examples


def failure_example():
    """Illustrative IDs only; real replies must copy actual trace arguments exactly."""
    quote = '请从A转237.18元到B，等待我确认'
    item = {'id': 'transfer', 'intent': 'transfer', 'continuation_of': None,
            'action': {'kind': 'PREPARE', 'source': {'message_id': 'm', 'quote': quote}},
            'slots': {f: {'value': v, 'source': {'message_id': 'm', 'quote': q}} for f, v, q in
                      [('source_account', 'A', 'A'), ('target_account', 'B', 'B'), ('amount_minor', 23718, '237.18元')]},
            'missing_fields': [], 'conflicts': []}
    arguments = {'source_account': 'A', 'target_account': 'B', 'amount_minor': 23718,
                 'idempotency_key': '实际原请求键', 'request_binding': {'items': [item]}}
    return {'format': 'bank_response_v2', 'request_id': '实际宿主request_id', 'items': [item],
            'answer': {'format': 'bank_outcomes_v1', 'items': [{'item_id': 'transfer', 'components': [
                {'format': 'bank_tool_outcome_v1', 'tool': 'prepare_transfer', 'arguments': arguments,
                 'error_code': '实际失败错误码'},
                {'format': 'bank_operation_v1', 'operation_id': '随后原操作的实际ID',
                 'evidence_id': '本轮operation读取的实际evidence_id'}]}]}}


def generated_examples(source_contract=LEGACY):
    if source_contract == MINIMAL:
        return "当前只接受value/source(message_id,quote)，禁止normalization等显式转换标签。宿主根据本轮真实证据核验账户和期间；错误字段不会被自动删除。"

    return ('默认来源与严格兼容示例（normalization只在source内，禁止搬移或删除错误字段；账户quote优先引用一个原账户，'
            '名称映射必须唯一且与字面ID一致，不能用value消歧。字面A/B仅验证精确出现，不证明整句转出转入角色）：\n' +
            json.dumps(source_examples(), ensure_ascii=False, separators=(',', ':')) +
            '\n当前bank_response_v2失败后另有原操作的完整结构示例（不是执行指令，所有示例ID须替换为本轮真实值；'
            'arguments须完整精确复制该次失败实际参数，包含实际存在的request_binding；没有的字段不能补造。'
            '后续成功或PENDING不免除之前失败；没有实际操作则不可填操作组件）：\n' +
            json.dumps(failure_example(), ensure_ascii=False, separators=(',', ':')))
