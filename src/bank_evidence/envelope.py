"""Decode only a complete known JSON answer envelope, without repairing content."""
import json
import math
import re

FORMATS = frozenset({'bank_response_v2', 'bank_service_v1', 'bank_facts_v1', 'bank_billing_v1',
                     'bank_spending_comparison_v1', 'task_progress_v1',
                     'goal_answer_v1', 'goal_preview_answer_v1', 'goal_impact_v1'})
FENCE = re.compile(r'\A```(?:json)?[ \t]*\r?\n(?P<body>[\s\S]*?)\r?\n```[ \t]*\Z', re.IGNORECASE)


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('DUPLICATE_JSON_KEY')
        value[key] = item
    return value


def reject_constant(_value):
    raise ValueError('NON_FINITE_JSON_NUMBER')


def finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError('NON_FINITE_JSON_NUMBER')
    return number


def normalize(result):
    """Return a copy only for valid envelopes. Evidence validation is still required."""
    text = result['text']
    if not isinstance(text, str):
        raise ValueError('INVALID_ANSWER_TEXT')
    if '```' not in text:
        return result
    # Literal backticks inside a valid bare JSON string are data, not an envelope.
    if text.lstrip().startswith('{'):
        try:
            json.loads(text)
            return result
        except ValueError:
            pass
    match = FENCE.fullmatch(text.strip())
    if not match:
        raise ValueError('INVALID_JSON_ENVELOPE')
    body = match['body']
    parsed = json.loads(body, object_pairs_hook=unique_object, parse_constant=reject_constant,
                        parse_float=finite_float)
    if type(parsed) is not dict or parsed.get('format') not in FORMATS:
        raise ValueError('UNKNOWN_ANSWER_ENVELOPE')
    return {**result, 'text': body, 'model_text': text,
            'answer_transport': {'version': 1, 'envelope': 'markdown_json'}}
