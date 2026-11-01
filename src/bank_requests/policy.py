"""A sourced request policy shared by direct tools and durable task execution."""
from copy import deepcopy

from src.bank_v1.common import DomainError, business_date
from src.bank_v1.mandate_match import normalize as merchant_name
from .decision import validate as validate_decision
from .constraints import date_value, parameters_match, resolved
from .schema import GOAL_BINDINGS, GOAL_TEXT_BINDINGS, DAY_FIELDS
from .normalization import goal_text_matches


PREPARES = {'prepare_transfer': 'transfer', 'propose_arrangement': 'arrangement',
            'prepare_mandate_query': 'mandate_query', 'prepare_mandate_cancel': 'mandate_cancel',
            'propose_goal': 'goal_compare'}
BINDING_TOOLS = frozenset(PREPARES) | {'submit_task_plan', 'replan_task'}
SUPPORTED = {'transfer': {'source_account', 'target_account', 'amount_minor', 'reserve_minor', 'date_text'},
    'arrangement': {'amount_minor', 'income_minor', 'expense_minor', 'date_text', 'purpose'},
    'mandate_query': set(), 'mandate_cancel': {'merchant_text', 'mandate_id'},
    'goal_compare': set(GOAL_BINDINGS) | {'purpose', 'priority'}}


def reject_unless(condition, code, message):
    if not condition:
        raise DomainError(code, message)


def has_reference(value):
    if isinstance(value, dict):
        return '$ref' in value or any(has_reference(v) for v in value.values())
    if isinstance(value, list):
        return any(has_reference(v) for v in value)
    return False


class RequestPolicy:
    def __init__(self, *, read_tool, clock):
        self.read_tool, self.clock = read_tool, clock

    def bind(self, binding, context, records):
        reject_unless(type(binding) is dict and set(binding) in ({'items'}, {'items', 'node_items'}),
                      'REQUEST_BINDING_REQUIRED', '准备操作须绑定本轮用户请求。')
        try:
            decision = validate_decision(binding['items'], {**context, 'require_current_action': True}, records)
        except (ValueError, KeyError, TypeError, AttributeError):
            raise DomainError('REQUEST_SOURCE_INVALID', '请求原话、条件或本轮动作未通过核对；尚未调用业务工具。') from None
        day = business_date(self.clock())
        for item in decision['items']:
            if item['intent'] == 'goal_compare' and item['action']['effective_kind'] == 'PREPARE':
                from .goal_prepare_identity import bind as bind_goal_identity
                item['goal_prepare_identity'] = bind_goal_identity(item, context)
            for field in DAY_FIELDS & item['slots'].keys():
                value = date_value(item, field, day)
                if value:
                    anchor = resolved(field, item['slots'][field], 'BOUND', 'HOST_REQUEST_BUSINESS_DAY')
                    anchor['canonical_value'] = value
                    item['constraint_anchors'][field] = anchor
        mapping = binding.get('node_items', {})
        reject_unless(type(mapping) is dict and all(type(k) is str and type(v) is str for k, v in mapping.items()),
                      'REQUEST_BINDING_INVALID', '节点与请求的对应关系无效。')
        return {'version': 1, 'request_id': decision['request_id'], 'items': decision['items'], 'node_items': deepcopy(mapping), **({k:context[k] for k in ('source_contract','run_id')} if 'run_id' in context else {})}

    def item_for(self, contract, node_id=None):
        reject_unless(type(contract) is dict and contract.get('version') == 1,
                      'REQUEST_BINDING_INVALID', '缺少已核验的请求契约。')
        items = contract['items']
        if node_id is None:
            reject_unless(len(items) == 1, 'REQUEST_BINDING_AMBIGUOUS', '直接办理须对应一项明确请求。')
            return items[0]
        identity = contract['node_items'].get(node_id)
        matches = [item for item in items if item['id'] == identity]
        reject_unless(len(matches) == 1, 'REQUEST_NODE_UNBOUND', '该业务节点尚未对应明确的用户请求。')
        return matches[0]

    def check_action(self, name, item):
        expected = PREPARES.get(name)
        prerequisite = name == 'prepare_mandate_query' and item['intent'] == 'mandate_cancel'
        reject_unless(item['action']['effective_kind'] == 'PREPARE' and not item['action']['host_read_only'],
                      'REQUEST_ACTION_FORBIDS_WRITE', '本轮仅核对或分析，不能准备银行操作。')
        reject_unless(item['intent'] == expected or prerequisite, 'REQUEST_TOOL_MISMATCH', '工具与本项业务请求不对应。')
        reject_unless(not item['missing_fields'] and not item['conflicts'],
                      'REQUEST_CONDITIONS_INCOMPLETE', '请求条件尚有缺失或冲突，不能准备。')
        reject_unless(set(item['slots']) <= SUPPORTED[item['intent']], 'REQUEST_CONSTRAINT_UNSUPPORTED',
                      '本项仍有尚未明确落实的条件，请先核对，不能忽略后办理。')
        return prerequisite

    def check_call(self, name, args, item):
        params = deepcopy(args)
        if name == 'match_mandate_from_query':
            reject_unless(item['intent'] == 'mandate_cancel', 'REQUEST_TOOL_MISMATCH', '商户匹配未对应撤销请求。')
            merchant = item['slots'].get('merchant_text')
            reject_unless(merchant and merchant_name(params.get('merchant_text', '')) == merchant_name(merchant['value']),
                          'REQUEST_PARAMETERS_MISMATCH', '商户匹配条件与用户请求不一致。')
            return params
        if name not in PREPARES:
            return params
        if self.check_action(name, item):
            return params  # Formal query is a prerequisite, never cancellation success.
        slots, day = item['slots'], business_date(self.clock())
        try:
            matches = parameters_match(params, item, day)
        except ValueError:
            raise DomainError('REQUEST_CONDITIONS_CONFLICT', '请求中的金额或收支条件相互冲突，不能准备。') from None
        reject_unless(matches, 'REQUEST_PARAMETERS_MISMATCH',
                      '工具参数与用户要求的账户、金额或日期不一致。')
        if 'date_text' in slots:
            value = date_value(item, 'date_text', day)
            reject_unless(value is not None, 'REQUEST_DATE_UNRESOLVED', '日期要求尚未解析，不能准备。')
            if name == 'prepare_transfer':
                reject_unless(value == day, 'REQUEST_DATE_UNSUPPORTED', '当前转账工具只准备当日操作，不能冒充预约转账。')
        if name == 'prepare_transfer' and 'reserve_minor' in slots:
            floor = slots['reserve_minor']['value']
            reject_unless('reserve_minor' not in params or params['reserve_minor'] == floor,
                          'REQUEST_PARAMETERS_MISMATCH', '工具保留额与用户要求不一致。')
            params['reserve_minor'] = floor
        if name == 'propose_arrangement' and 'purpose' in slots:
            reject_unless(slots['purpose']['value'] in params.get('title', ''),
                          'REQUEST_PARAMETERS_MISMATCH', '安排用途与用户要求不一致。')
        if name == 'propose_goal':
            profile = params.get('profile', {})
            from .goal_prepare_identity import check as check_goal_identity
            check_goal_identity(profile, item)
            for field, key in GOAL_BINDINGS.items():
                if field in slots:
                    value = date_value(item, field, day) if field in DAY_FIELDS else slots[field]['value']
                    reject_unless(profile.get(key) == value, 'REQUEST_PARAMETERS_MISMATCH', '目标草案未落实用户条件。')
            for field, keys in GOAL_TEXT_BINDINGS.items():
                if field in slots:
                    reject_unless(goal_text_matches(field, slots[field]['value'], profile, keys),
                                  'REQUEST_PARAMETERS_MISMATCH', '目标用途或优先事项未对应用户要求。')
        if name == 'prepare_mandate_cancel' and 'merchant_text' in slots:
            reject_unless('query_operation_id' in params and 'expected_version' in params,
                          'REQUEST_MERCHANT_SOURCE_REQUIRED', '须用本人确认查询的回执唯一核对商户和授权版本。')
            match = self.read_tool('match_mandate_from_query', {'query_operation_id': params['query_operation_id'],
                'merchant_text': slots['merchant_text']['value'], 'match_mode': 'normalized_contains'})
            selected = match['data']['match']['selected']
            reject_unless(selected['mandate_id'] == params.get('mandate_id') and selected['version'] == params['expected_version'],
                          'REQUEST_PARAMETERS_MISMATCH', '待撤销授权与正式唯一商户匹配不一致。')
        return params

    def check_plan(self, nodes, contract):
        required = {node['id'] for node in nodes if node['tool'] in PREPARES or node['tool'] == 'match_mandate_from_query'}
        identifiers = {node['id'] for node in nodes}
        reject_unless(required <= contract['node_items'].keys() and contract['node_items'].keys() <= identifiers,
                      'REQUEST_NODE_UNBOUND', '计划中的准备或商户匹配节点必须逐项绑定用户请求。')
        for node in nodes:
            if node['id'] not in contract['node_items']:
                continue
            item = self.item_for(contract, node['id'])
            if node['tool'] in PREPARES:
                self.check_action(node['tool'], item)
            if not has_reference(node['args']):
                self.check_call(node['tool'], node['args'], item)

    def before_task_call(self, name, args, node_id, contract):
        if name in PREPARES or name == 'match_mandate_from_query' or node_id in contract['node_items']:
            return self.check_call(name, args, self.item_for(contract, node_id))
        return args
