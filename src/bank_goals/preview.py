"""Unconfirmed, read-only goal comparison. No goal or bank persistence."""
from copy import deepcopy
from jsonschema import Draft202012Validator
from . import contracts, projection
from .evidence import DATA, PROFILE, OPTION, DATE, INTEGER, obj, digest, claim_catalog, money, COVERAGE

TOOL = 'preview_goal_options'
DATA_SCHEMA = obj({
    'unconfirmed_profile': PROFILE, 'business_date': DATE,
    'bank_snapshot_hash': {'type': 'string', 'pattern': '^[a-f0-9]{64}$'},
    'current_total_minor': INTEGER,
    'options': {'type': 'array', 'items': OPTION, 'minItems': 1, 'maxItems': 3},
    'income_conditions': DATA['properties']['income_conditions'], 'coverage': COVERAGE},
    ['unconfirmed_profile','business_date','bank_snapshot_hash','current_total_minor','options','income_conditions'])


def build(profile, snapshot):
    contracts.validate('save', {'expected_revision': 0, 'profile': profile})
    result = projection.compare({'profile': profile, 'revision': 0,
                                 'linked_arrangement_id': None}, snapshot)
    options = []
    for option in result['options']:
        forecast = option['forecast']
        options.append({key: option[key] for key in ('option_id', 'amount_minor', 'due_date', 'status')} | {
            'primary': {'minimum_minor': forecast['lowest_balance_minor'],
                'date': forecast['lowest_balance_date'],
                'reserve_shortfall_minor': option['reserve_shortfall_minor']},
            'without_income': {'minimum_minor': forecast['without_future_income_lowest_balance_minor'],
                'date': forecast['without_future_income_lowest_balance_date'],
                'reserve_shortfall_minor': option['without_income_reserve_shortfall_minor']}})
    return envelope({'unconfirmed_profile': deepcopy(profile), 'business_date': snapshot['business_date'],
        'bank_snapshot_hash': result['bank_hash'],
        'current_total_minor': snapshot['accounts']['total_balance_minor'], 'options': options,
        'income_conditions': result['options'][0]['forecast']['income_conditions'],
        'coverage': {'window_end': result['window_end'], 'outside_window': [{k: a[k] for k in ('arrangement_id','title','due_date')}
            for a in snapshot['arrangements'] if a['status'] == 'active' and a['due_date'] > result['window_end']]}})


def envelope(data):
    Draft202012Validator(DATA_SCHEMA).validate(data)
    # Reuse display formatting only; never manufacture a confirmed goal envelope.
    claims = claim_catalog({**data, 'status': 'READY'})
    next(c for c in claims if c['path'] == 'status').update(
        value='UNCONFIRMED_PREVIEW', display='这是未确认需求的试算，未保存目标、草案或银行安排，也未办理付款。')
    profile = data['unconfirmed_profile']
    claims.insert(2, {'path': 'reserve_floor_minor', 'value': profile['reserve_floor_minor'],
        'display': f"本次按至少保留{money(profile['reserve_floor_minor'])}比较；仅包含已记录安排，未来收入仍未到账。"})
    return {'scope': 'goal_preview', 'contract': 'goal-preview-1', 'tool': TOOL, 'subject': 'U1',
        'currency': 'CNY', 'unit': 'minor', 'source_id': digest(data), 'data': data,
        'claim_catalog': claims, 'allowed_next_steps': ['REVIEW_GOAL_FORM']}
