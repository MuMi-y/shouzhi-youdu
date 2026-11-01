"""Typed goal evidence; selectable claims retain scope, scenario and dates together."""
from decimal import Decimal
import hashlib
import json
from jsonschema import Draft202012Validator
from src.bank_v1.common import canonical
from . import date_review

READ_TOOLS = frozenset({'goal_context', 'compare_goal_options'})
GOAL_TOOLS = READ_TOOLS | {'propose_goal'}


def obj(properties, required=None):
    return {'type': 'object', 'properties': properties,
            'required': list(properties) if required is None else required, 'additionalProperties': False}


TEXT = {'type': 'string', 'minLength': 1, 'maxLength': 500}
INTEGER = {'type': 'integer'}
DATE = {'type': 'string', 'pattern': r'^\d{4}-\d{2}-\d{2}$'}
PROFILE = obj({'title': {**TEXT, 'maxLength': 100}, 'user_reason': TEXT, 'priority_reason': TEXT,
    'preferred_amount_minor': {'type': 'integer', 'minimum': 1, 'maximum': 100_000_000_000},
    'min_amount_minor': {'type': 'integer', 'minimum': 1, 'maximum': 100_000_000_000},
    'reserve_floor_minor': {'type': 'integer', 'minimum': 0, 'maximum': 100_000_000_000},
    'preferred_date': DATE, 'latest_date': DATE})
SCENARIO = obj({'minimum_minor': INTEGER, 'date': DATE,
                'reserve_shortfall_minor': {'type': 'integer', 'minimum': 0}})
OPTION = obj({'option_id': {'enum': ['original', 'reduced', 'delayed']},
    'amount_minor': {'type': 'integer', 'minimum': 1}, 'due_date': DATE,
    'status': {'enum': ['NEEDS_TRADEOFF', 'CONDITIONAL', 'WITHIN_KNOWN_CONSTRAINTS']},
    'primary': SCENARIO, 'without_income': SCENARIO})
DATA = obj({'goal_id': {'const': 'focus'}, 'revision': {'type': 'integer', 'minimum': 1},
    'basis_hash': {'type': 'string', 'pattern': '^[a-f0-9]{64}$'},
    'facts_hash': {'type': 'string', 'pattern': '^[a-f0-9]{64}$'}, 'business_date': DATE,
    'profile': PROFILE, 'status': {'enum': ['READY', 'LINK_PENDING', 'GOAL_NOT_ACTIVE', 'DATE_REVIEW_REQUIRED']},
    'options': {'type': 'array', 'items': OPTION, 'maxItems': 3},
    'income_conditions': {'type': 'array', 'items': obj({'arrangement_id': TEXT, 'title': TEXT,
        'due_date': DATE, 'amount_minor': {'type': 'integer', 'minimum': 1}})},
    'choice_state': TEXT, 'current_total_minor': INTEGER})

CURRENT_PLAN = obj({'choice_id': TEXT, 'option_id': {'enum': ['original', 'reduced', 'delayed']},
    'choice_state': TEXT, 'selected_amount_minor': {'type': 'integer', 'minimum': 1}, 'selected_date': DATE,
    'operation_id': {'anyOf': [TEXT, {'type': 'null'}]},
    'receipt_id': {'anyOf': [TEXT, {'type': 'null'}]},
    'notice_state': {'enum': ['SELECTED', 'WAITING_CONFIRMATION', 'SAVED', 'REVIEW_REQUIRED', 'INACTIVE']},
    'arrangement': {'anyOf': [{'type': 'null'}, obj({'arrangement_id': TEXT,
        'version': {'type': 'integer', 'minimum': 1}, 'amount_minor': {'type': 'integer', 'minimum': 1},
        'due_date': DATE, 'status': {'enum': ['active', 'completed', 'cancelled']}})]},
    'projection': {'anyOf': [{'type': 'null'}, obj({'primary': SCENARIO, 'without_income': SCENARIO})]}})
COVERAGE = obj({'window_end': DATE, 'outside_window': {'type': 'array', 'items': obj({
    'arrangement_id': TEXT, 'title': TEXT, 'due_date': DATE})}})
DATA_V2 = obj({**DATA['properties'], 'current_plan': {'anyOf': [{'type': 'null'}, CURRENT_PLAN]},
               'coverage': COVERAGE}, [*DATA['properties'], 'current_plan'])
# Optional in both versions; historical envelopes without coverage remain valid.
DATA['properties']['coverage'] = COVERAGE


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def money(value):
    return f'{Decimal(value)/100:.2f}元'


def claim_catalog(data):
    """Explicit paths only. No arbitrary *_minor traversal or account inference."""
    claims = []
    def add(path, value, display):
        claims.append({'path': path, 'value': value, 'display': display})
    add('current_total_minor', data['current_total_minor'],
        f"当前账户合计余额为{money(data['current_total_minor'])}；合计不保证某个付款账户够用。")
    if data.get('coverage'):
        coverage = data['coverage']
        display = f"本次共同计算至{coverage['window_end']}，只包含已登记的一次性安排，不推断未来每月收入或生活费。"
        if coverage['outside_window']:
            display += '未覆盖计划：' + '；'.join(f"{a['title']}（{a['due_date']}）" for a in coverage['outside_window']) + '；其资金影响尚不能判断。'
        add('coverage', coverage, display)
    add('status', data['status'], {'READY': '已核对目标与账本，下面是候选方案预测，保存安排不等于付款。',
        'LINK_PENDING': '原银行操作已有结果，需核对原回执关联目标；当前不重复增加支出。',
        'GOAL_NOT_ACTIVE': '原目标安排已不再有效，请核对原安排；不自动重建。',
        'DATE_REVIEW_REQUIRED': '目标日期已需重新核对，请修改目标日期后再比较。'}[data['status']])
    labels = {'original': '按原计划', 'reduced': '降低金额', 'delayed': '延期'}
    plan = data.get('current_plan')
    if plan:
        if data['status'] == 'READY':
            next(c for c in claims if c['path'] == 'status')['display'] = (
                '已核对已选方案和当前账本；候选试算不代表已经更换安排，保存安排不等于付款。')
        labels = {'original': '按最初期望金额试算', 'reduced': '按最低金额试算', 'delayed': '延期试算'}
        stage = plan['notice_state']
        chosen = f"已选择{plan['selected_date']}支出{money(plan['selected_amount_minor'])}的方案"
        if stage == 'SAVED':
            arrangement = plan['arrangement']
            display = (f"当前已保存安排：{arrangement['due_date']}支出{money(arrangement['amount_minor'])}；"
                       f"保存回执{plan['receipt_id']}。这是保存安排，尚无付款完成依据。")
        elif stage == 'SELECTED':
            display = chosen + '；尚未准备银行确认，也未保存银行安排或付款。'
        elif stage == 'WAITING_CONFIRMATION':
            display = chosen + '；银行操作正在等待你独立确认，尚未完成保存或付款。'
        elif stage == 'INACTIVE':
            state_label = '已标记完成' if plan['arrangement']['status'] == 'completed' else '已取消'
            display = f'此前关联的安排{state_label}，不再作为待执行支出；不能把原保存回执当付款凭证。'
        elif stage == 'REVIEW_REQUIRED' and plan['projection']:
            a = plan['arrangement']
            display = (f"当前账本关联安排v{a['version']}：{a['due_date']}支出{money(a['amount_minor'])}。"
                       '原选择及旧回执未对应此版本；下列按最新账本计算，不把旧回执当作本次修改或付款凭证。')
        else:
            display = chosen + '；当前操作、回执或安排关联需要核对，不能断言已按此方案保存或付款。'
        add('current_plan', {k: v for k, v in plan.items() if k != 'projection'}, display)
        if plan['projection']:
            for branch, label in [('primary', '预计收入按计划计入'), ('without_income', '不计未来收入')]:
                value = plan['projection'][branch]
                add('current_plan.'+branch, value,
                    f"当前账本安排，{label}时：最低合计余额{money(value['minimum_minor'])}（{value['date']}）；"
                    f"距用户要求的保留余额差{money(value['reserve_shortfall_minor'])}。")
    review = date_review.context(data)
    if review:
        next(c for c in claims if c['path'] == 'status')['display'] = '日期变化需要核对；当前计算与原保存回执分别保留。'
        add('date_review', review, date_review.display(review))
    for option in data['options']:
        prefix = 'options.' + option['option_id']
        add(prefix + '.plan', {'amount_minor': option['amount_minor'], 'due_date': option['due_date']},
            f"{labels[option['option_id']]}：计划{option['due_date']}支出{money(option['amount_minor'])}，尚未办理付款。")
        for branch, label in [('primary', '预计收入按计划计入'), ('without_income', '不计未来收入')]:
            value = option[branch]
            add(prefix + '.' + branch, value,
                f"{labels[option['option_id']]}，{label}时：最低合计余额{money(value['minimum_minor'])}"
                f"（{value['date']}）；距用户要求的保留余额差{money(value['reserve_shortfall_minor'])}。")
    for i, income in enumerate(data['income_conditions']):
        add(f'income_conditions.{i}', income,
            f"预计收入：{income['title']}，{income['due_date']}，{money(income['amount_minor'])}；这是未到账条件，不是已收款。")
    return claims


def allowed_steps(data):
    if date_review.context(data):
        return ['CHECK_PLAN_DATE']
    plan = data.get('current_plan')
    if plan:
        if plan['notice_state'] == 'SAVED':
            if any(value['reserve_shortfall_minor'] for value in plan['projection'].values()):
                return ['REVIEW_ADJUSTMENT']
            return ['MONITOR_SAVED_PLAN', 'REVIEW_ADJUSTMENT']
        if plan['notice_state'] in {'SELECTED', 'WAITING_CONFIRMATION'}:
            return ['REVIEW_PENDING_CHOICE']
        return ['CHECK_RECEIPT', 'UPDATE_GOAL']
    if data['status'] == 'LINK_PENDING':
        return ['CHECK_RECEIPT']
    if data['status'] != 'READY':
        return ['UPDATE_GOAL']
    return ['REVIEW_OPTIONS', 'COMPARE_AGAIN'] + (['WAIT_INCOME'] if data['income_conditions'] else [])


def envelope(data, tool):
    version = 2 if 'current_plan' in data else 1
    Draft202012Validator(DATA_V2 if version == 2 else DATA).validate(data)
    return {'scope': 'goal_plan', 'contract': f'goal-evidence-{version}', 'tool': tool, 'subject': 'U1',
            'currency': 'CNY', 'unit': 'minor', 'source_id': digest(data), 'data': data,
            'claim_catalog': claim_catalog(data), 'allowed_next_steps': allowed_steps(data)}


def has_goal_evidence(result):
    return any(isinstance(r, dict) and r.get('kind') == 'response' and
        (r.get('tool') in GOAL_TOOLS or (isinstance(r.get('response'), dict) and
         str(r['response'].get('scope', '')).startswith('goal'))) for r in result.get('trace', []))


def claim_value_matches(claim, permitted):
    """Legacy current_plan may repeat both exact same-source projection claims."""
    expected = permitted[claim['path']]['value']
    value = claim['value']
    if canonical(value) == canonical(expected):
        return True
    if (claim['path'] != 'current_plan' or type(value) is not dict or
            set(value) != set(expected) | {'projection'}):
        return False
    if canonical({k: v for k, v in value.items() if k != 'projection'}) != canonical(expected):
        return False
    branches = ('primary', 'without_income')
    if not all('current_plan.' + branch in permitted for branch in branches):
        return False
    projection = {branch: permitted['current_plan.' + branch]['value'] for branch in branches}
    return canonical(value['projection']) == canonical(projection)


def validate_answer(result):
    """Validate host tool traces and exact claim references; never render arbitrary model prose."""
    validation = {'version': 'goal-evidence-1', 'scope': 'goal_plan', 'gate_applied': True}
    try:
        sources, requests, draft = {}, {}, None
        for row in result.get('trace', []):
            if not isinstance(row, dict):
                continue
            if row.get('kind') == 'request':
                requests[row.get('call_id')] = row
                if row.get('name') in {'propose_arrangement', 'prepare_transfer', 'prepare_mandate_query',
                        'match_mandate_from_query', 'prepare_mandate_cancel', 'submit_task_plan', 'replan_task'}:
                    raise ValueError('GOAL_WRITE_MIXED')
            if row.get('kind') != 'response' or not row.get('ok'):
                continue
            response = row.get('response', {})
            scope = response.get('scope', 'bank') if isinstance(response, dict) else 'invalid'
            if scope not in {'bank', 'goal_plan', 'goal_draft'}:
                raise ValueError('UNKNOWN_EVIDENCE_SCOPE')
            if scope == 'goal_draft':
                request = requests.get(row.get('call_id'))
                if (row.get('tool') != 'propose_goal' or not request or request.get('name') != 'propose_goal'
                        or request.get('arguments') != row.get('args') or row.get('status') != 200
                        or set(response) != {'scope', 'data'}
                        or set(response['data']) != {'draft_id', 'status', 'notice'}
                        or response['data']['status'] != 'UNCONFIRMED'
                        or not isinstance(response['data']['draft_id'], str)):
                    raise ValueError('FAKE_GOAL_TOOL')
                draft = response['data']['draft_id']
                continue  # Drafts never support confirmed financial claims.
            if scope == 'bank' and row.get('tool') not in READ_TOOLS:
                continue
            request = requests.get(row.get('call_id'))
            if (not request or row.get('tool') not in READ_TOOLS or request.get('name') != row.get('tool')
                    or request.get('arguments') != row.get('args') or row.get('status') != 200):
                raise ValueError('UNBOUND_GOAL_RESPONSE')
            rebuilt = envelope(response['data'], row['tool'])
            if canonical(response) != canonical(rebuilt):
                raise ValueError('INVALID_GOAL_ENVELOPE')
            sources[response['source_id']] = response
        if not sources and draft:
            return {**result, 'text': '已保存一份未确认的目标草案。请到目标区采用到表单，核对后再保存；目标和银行安排尚未改变。',
                    'answer_validation': {**validation, 'status': 'ACCEPTED_DRAFT_ONLY'}, 'goal_draft_id': draft}
        answer = result.get('goal_answer') or json.loads(result['text'])
        schema = obj({'format': {'const': 'goal_answer_v1'}, 'source_id': TEXT,
            'claims': {'type': 'array', 'minItems': 1, 'maxItems': 20,
                       'items': {'oneOf': [obj({'path': TEXT, 'value': {}}),
                                          obj({'path': TEXT, 'reference': {'const': True}})]}},
            'next_step': {'enum': ['REVIEW_OPTIONS', 'COMPARE_AGAIN', 'WAIT_INCOME', 'CHECK_RECEIPT', 'UPDATE_GOAL',
                                   'MONITOR_SAVED_PLAN', 'REVIEW_ADJUSTMENT', 'REVIEW_PENDING_CHOICE', 'CHECK_PLAN_DATE']}})
        Draft202012Validator(schema).validate(answer)
        source = sources[answer['source_id']]
        permitted = {c['path']: c for c in source['claim_catalog']}
        paths, displays = set(), []
        for claim in answer['claims']:
            expected = permitted[claim['path']]
            # Render the catalog only; copied values must match every trusted field.
            if claim['path'] in paths or ('value' in claim and
                    not claim_value_matches(claim, permitted)):
                raise ValueError('CLAIM_VALUE_MISMATCH')
            paths.add(claim['path']); displays.append(expected['display'])
        required = {'status'} | ({'options.original.primary', 'options.original.without_income'}
                                 if source['data']['options'] else set())
        plan = source['data'].get('current_plan')
        if plan:
            required = {'status', 'current_plan'}
            if plan['projection']:
                required |= {'current_plan.primary', 'current_plan.without_income'}
        review = date_review.context(source['data'])
        if review:
            required.add('date_review')
            required |= {c['path'] for c in source['claim_catalog'] if c['path'].startswith('income_conditions.')}
        if not required.issubset(paths) or answer['next_step'] not in source['allowed_next_steps']:
            raise ValueError('MISSING_CONDITION_OR_NEXT_STEP')
        steps = {'REVIEW_OPTIONS': '下一步：核对候选方案，选择仍需你确认。',
            'COMPARE_AGAIN': '下一步：如需选择，点击重新比较取得当前方案。',
            'WAIT_INCOME': '下一步：可选择等待收入；实际到账后仍须核对并重新比较，不自动办理。',
            'CHECK_RECEIPT': '下一步：核对原回执并更新目标，不重复提交银行操作。',
            'UPDATE_GOAL': '下一步：核对目标资料和关联安排，由你确认修改。',
            'MONITOR_SAVED_PLAN': '下一步：继续关注已保存安排；新的收支会重新核算，保存安排不会自动付款。',
            'REVIEW_ADJUSTMENT': '下一步：核对当前已保存安排的缺口；如需调整，请重新比较并独立确认，不会自动替换。',
            'REVIEW_PENDING_CHOICE': '下一步：核对已选方案和待办；尚未办理时先准备，已有待办时确认或拒绝原操作，不重复提交。'}
        if review:
            steps['CHECK_PLAN_DATE'] = date_review.next_step(review)
        if 'coverage' in permitted and 'coverage' not in paths:
            displays.insert(0, permitted['coverage']['display'])
        return {**result, 'text': '\n'.join(displays + [steps[answer['next_step']]]),
                'goal_answer': answer, 'goal_basis_hash': source['data']['basis_hash'],
                'answer_validation': {**validation, 'version': source['contract'], 'status': 'ACCEPTED', 'source_id': source['source_id']}}
    except Exception as error:
        # ValidationError contains schema/data; do not expose it as user-facing prose.
        return {**result, 'ok': False, 'session_id': None,
                'error': '目标解释未能与具体工具字段及条件对应，未作为成功解释展示。',
                'answer_validation': {**validation, 'status': 'REJECTED', 'reason': type(error).__name__}}
