"""One host-proven read failure; never infer a terminal outcome from model text."""
import json

from src.bank_v1.common import DomainError
from .handoff_contracts import digest, safe

KIND = 'READ_ONLY_DOMAIN_FAILURE_V1'
PARAMS = {'goal_id': 'focus'}
RESPONSE = {'error': {'code': 'GOAL_MISSING', 'message': '请先核对并保存一个目标。'}}


def receipt(name, params, error):
    """Invoke only at the actual domain callback's exception boundary."""
    if (name != 'goal_context' or params != PARAMS or type(error) is not DomainError
            or error.code != RESPONSE['error']['code'] or error.message != RESPONSE['error']['message']):
        return None
    return dict(failure_kind=KIND, status=409, code=error.code,
                response_hash=digest(safe(RESPONSE)), business_hash=digest(PARAMS))


def known(attempt, events):
    """Validate persisted producer identity, exact parameters and ordered receipt."""
    try:
        attempt = dict(attempt)
        if (attempt['actor'] != 'MODEL_GATEWAY' or attempt['effect_class'] != 'READ_ONLY'
                or attempt['entry_name'] != 'goal_context' or attempt['raw_integrity'] != 'EXACT_SAFE'
                or json.loads(attempt['raw_safe_json']) != PARAMS
                or json.loads(attempt['business_safe_json']) != PARAMS
                or attempt['raw_safe_hash'] != digest(PARAMS)
                or attempt['business_safe_hash'] != digest(PARAMS)):
            return False
        rows = [dict(e) for e in events if e['attempt_id'] == attempt['attempt_id']]
        if any(e['run_id'] != attempt['run_id'] for e in rows):
            return False
        if {e['phase'] for e in rows} - {'BEGIN', 'VALIDATED', 'DISPATCHED', 'RAISED', 'TRACE_LINK'}:
            return False
        ordered = []
        for phase in ('BEGIN', 'VALIDATED', 'DISPATCHED', 'RAISED'):
            matches = [e for e in rows if e['phase'] == phase]
            if len(matches) != 1:
                return False
            ordered.append(matches[0])
        if not all(a['sequence'] < b['sequence'] for a, b in zip(ordered, ordered[1:])):
            return False
        def data(event):
            return event['data'] if 'data' in event else json.loads(event['data_json'])
        expected = receipt('goal_context', PARAMS, DomainError(**RESPONSE['error']))
        return (data(ordered[1]).get('business_hash') == digest(PARAMS)
                and data(ordered[-1]) == expected)
    except (KeyError, TypeError, ValueError):
        return False


def matches_response(row):
    return (row.get('ok') is False and row.get('status') == 409
            and digest(safe(row.get('response'))) == digest(safe(RESPONSE)))
