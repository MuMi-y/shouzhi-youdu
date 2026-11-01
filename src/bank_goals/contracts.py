"""Strict browser contracts and user intent validation."""
from datetime import date
from jsonschema import Draft202012Validator
from src.bank_v1.common import DomainError, require_date


def object_schema(properties):
    return {'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}


def validate(action, body):
    text = {'type': 'string', 'minLength': 1, 'maxLength': 160}
    amount = {'type': 'integer', 'minimum': 1, 'maximum': 100_000_000_000}
    revision = {'type': 'integer', 'minimum': 0}
    profile = object_schema({
        'title': {**text, 'maxLength': 100}, 'user_reason': {**text, 'maxLength': 500},
        'priority_reason': {**text, 'maxLength': 500},
        'preferred_amount_minor': amount, 'min_amount_minor': amount,
        'preferred_date': text, 'latest_date': text,
        'reserve_floor_minor': {**amount, 'minimum': 0}})
    contracts = {
        'save': object_schema({'expected_revision': revision, 'profile': profile}),
        'compare': object_schema({'expected_revision': revision}),
        'select': object_schema({'expected_revision': revision, 'assessment_id': text,
                                 'option_id': text, 'wait_for_income': {'type': 'boolean'}, 'idempotency_key': text}),
        'prepare': object_schema({'choice_id': text}),
        'reconcile': object_schema({'choice_id': text})}
    if action not in contracts or list(Draft202012Validator(contracts[action]).iter_errors(body)):
        raise DomainError('INVALID_ARGUMENTS', '目标参数缺失、类型错误或包含禁止字段。')
    # JSON Schema numbers such as 1.0 must not silently become revisions or minor units.
    if 'expected_revision' in body and type(body['expected_revision']) is not int:
        raise DomainError('INVALID_ARGUMENTS', '版本必须是整数。')
    if action == 'save':
        for key, value in body['profile'].items():
            if key.endswith('_minor') and type(value) is not int:
                raise DomainError('INVALID_ARGUMENTS', '金额必须是整数分。')
            if isinstance(value, str) and not value.strip():
                raise DomainError('INVALID_ARGUMENTS', '请填写目标及取舍理由。')


def validate_dates(profile, today):
    preferred = require_date(profile['preferred_date'])
    latest = require_date(profile['latest_date'])
    if not today <= preferred <= latest or (date.fromisoformat(latest)-date.fromisoformat(today)).days > 366:
        raise DomainError('INVALID_ARGUMENTS', '期望日期须从今天起，最晚日期不早于期望日期且在一年内。')
    if profile['min_amount_minor'] > profile['preferred_amount_minor']:
        raise DomainError('INVALID_ARGUMENTS', '可接受的最低金额不能高于期望金额。')
