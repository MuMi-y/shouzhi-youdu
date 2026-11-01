"""Bounded, redacted audit values. No tool output or credential dumps."""
import hashlib
import json
import re

SECRET = re.compile(r'token|api.?key|password|secret|challenge.?answer|verification.?factor|authorization|^otp$|^pin$', re.I)
TOKEN = re.compile(r'\b(?:ark-[\w-]+|sk-[\w-]{12,}|Bearer\s+\S+)')


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def safe(value, depth=0):
    if depth > 30:
        return {'omitted': 'depth'}
    if type(value) is dict:
        return {str(k): '[REDACTED]' if SECRET.search(str(k)) else safe(v, depth+1) for k, v in value.items()}
    if type(value) is list:
        return [safe(v, depth+1) for v in value]
    if type(value) is str:
        return TOKEN.sub('[REDACTED]', value)
    if value is None or type(value) in (int, bool, float):
        return value
    return {'type': type(value).__name__}


def parameter_view(value):
    view = safe(value)
    text = canonical(view)
    if len(text.encode('utf-8')) > 65536:
        return canonical({'omitted': 'oversize', 'safe_hash': digest(view)}), 'METADATA_ONLY'
    return text, 'EXACT_SAFE' if view == value else 'REDACTED'


def object_refs(value):
    result = []
    def visit(item):
        if isinstance(item, dict):
            for key, kind in [('operation_id', 'operation'), ('task_id', 'task'), ('draft_id', 'goal_draft'),
                              ('goal_id', 'goal'), ('choice_id', 'choice'), ('check_id', 'check')]:
                if type(item.get(key)) is str:
                    ref = {'kind': kind, 'id': item[key]}
                    if ref not in result:
                        result.append(ref)
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)
    visit(value)
    return result[:20]
