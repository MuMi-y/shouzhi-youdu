"""Host-owned accepted-preview conditions, separated from current write intent."""
from copy import deepcopy
import re

from src.bank_v1.common import canonical
from src.bank_goals.evidence import digest
from .schema import GOAL_BINDINGS
from .normalization import DATE_ATOM

FORMAT = 'accepted_goal_preview_origin_v1'
IDENTITY = {'goal_title': 'title', 'purpose': 'user_reason', 'priority': 'priority_reason'}
MODIFIER = r'\s*(?:(?:改为|改成|改到|改|调整为|调整到|调整|是|为|到)\s*)?'
ROLE_PATTERNS = {
    'amount_minor': r'(?:预算|期望金额|理想金额)',
    'minimum_minor': r'最低(?:金额)?',
    'reserve_minor': r'(?:保留|预留)(?:金额|额)?',
    'date_text': r'(?:期望日期|期望日|计划日期|购买日期|购买日|计划)',
    'latest_date_text': r'(?:最晚|最迟)(?:日期)?',
}
CONDITIONS = {field: re.compile(label + MODIFIER + '(?P<value>' +
    (DATE_ATOM if field.endswith('date_text') or field == 'date_text' else r'\d+(?:\.\d+)?\s*元?') + ')')
    for field, label in ROLE_PATTERNS.items()}
CONDITIONS['priority'] = re.compile(r'(?:优先事项|优先偏好|偏好|优先)\s*(?:改为|改成|调整为|为|是)\s*(?P<value>[^，,。；;\n]+)')
EXPLICIT = re.compile(r'\d+(?:\.\d+)?\s*元|\d{4}-\d{2}-\d{2}|(?:\d{4}年)?\d{1,2}月\d{1,2}[日号]')
CHANGE = re.compile(r'改(?:为|成|到)?|调整(?:为|到)?|换(?:为|成)?')
CONDITIONAL = re.compile(r'如果|假如|假设|要是|万一|倘若|若是')
UNDECIDED_CONDITION = re.compile(r'是否|能否|会不会|可不可以|行不行|吗|不行|不可以|不能|不可|不应|不合适|不对')
REFUSED_CONDITION = re.compile(r'拒绝|禁止|不同意|不接受|不允许|反对|否决|不采纳')


def require(condition, code='INVALID_GOAL_PREVIEW_PARENT'):
    if not condition:
        raise ValueError(code)


def fingerprint(origin):
    return digest({key: value for key, value in origin.items() if key != 'fingerprint'})


def accepted_origin(item, components, reference, anchors):
    """Only historical_items' accepted-delivery branch calls this constructor."""
    binding = item.get('goal_profile_binding')
    if not binding:
        return deepcopy(item.get('goal_prepare_parent'))
    from .goal_profile_reference import verify_component
    previews = [c for c in components if c['metadata']['format'] == 'goal_preview_answer_v1']
    if len(previews) != 1:
        return None
    verify_component(item, previews[0])
    profile = previews[0]['metadata']['body']['data']['unconfirmed_profile']
    fields = set(IDENTITY) | set(GOAL_BINDINGS)
    if not fields <= item['slots'].keys():
        return None  # No inferred/default profile fields become user conditions.
    for field in ('date_text', 'latest_date_text'):
        anchor = anchors.get(field, {})
        if anchor.get('status') != 'BOUND' or anchor.get('canonical_value') != profile[GOAL_BINDINGS[field]]:
            return None
    origin = {'format': FORMAT, 'origin_reference': reference,
              'request_id': binding['request_id'], 'call_id': binding['call_id'], 'source_id': binding['source_id'],
              'identity_slots': {field: deepcopy(item['slots'][field]) for field in IDENTITY},
              'identity': {key: profile[key] for key in IDENTITY.values()}}
    origin['fingerprint'] = fingerprint(origin)
    return origin


def current_revisions(item, context):
    message = next(row['text'] for row in context['messages'] if row['message_id'] == context['current_message_id'])
    from .goal_prepare_identity import UNDECIDED, DENIED_PREPARATION, CLAUSE, NEGATION
    require(not UNDECIDED.search(message) and not DENIED_PREPARATION.search(message)
            and not CONDITIONAL.search(message), 'UNRESOLVED_GOAL_PREPARATION')
    spans = []
    for field, pattern in CONDITIONS.items():
        matches = list(pattern.finditer(message))
        require(len(matches) <= 1, 'AMBIGUOUS_GOAL_REVISION')
        for match in matches:
            clause = next((row.group() for row in CLAUSE.finditer(message)
                           if row.start() <= match.start() and match.end() <= row.end()), None)
            require(clause is not None and not NEGATION.search(clause)
                    and not UNDECIDED_CONDITION.search(clause) and not REFUSED_CONDITION.search(clause),
                    'UNRESOLVED_GOAL_REVISION_POLARITY')
            slot = item['slots'].get(field)
            require(slot is not None and slot['source']['message_id'] == context['current_message_id'],
                    'GOAL_REVISION_REQUIRES_CURRENT_SLOT')
            quote = slot['source']['quote']
            require(message.count(quote) == 1, 'AMBIGUOUS_GOAL_REVISION')
            start = message.index(quote)
            require(start <= match.start('value') and match.end('value') <= start + len(quote),
                    'GOAL_REVISION_SOURCE_ROLE_MISMATCH')
            spans.append(match.span())
    if 'priority' in item['slots']:
        source = item['slots']['priority']['source']
        require(source['message_id'] == context['current_message_id'] and message.count(source['quote']) == 1,
                'GOAL_REVISION_REQUIRES_CURRENT_SLOT')
        start = message.index(source['quote'])
        spans.append((start, start + len(source['quote'])))
    for atom in list(EXPLICIT.finditer(message)) + list(CHANGE.finditer(message)):
        require(any(start <= atom.start() and atom.end() <= end for start, end in spans),
                'UNRESOLVED_GOAL_REVISION')


def prepare_parent(parent, item, action, context):
    if not parent or item['intent'] != 'goal_compare' or action['effective_kind'] != 'PREPARE':
        return None
    origin = parent.get('goal_preview_origin')
    if not origin and 'goal_title' not in parent['slots'] and not parent.get('goal_prepare_parent'):
        return None  # Ordinary literal-source history keeps its existing route.
    require(isinstance(origin, dict) and origin.get('format') == FORMAT
            and origin.get('fingerprint') == fingerprint(origin))
    require(origin['origin_reference'].startswith(origin['request_id'] + ':'))
    if parent.get('goal_profile_binding'):
        require(origin['origin_reference'] == parent['reference']
                and all(origin[key] == parent['goal_profile_binding'][key]
                        for key in ('request_id', 'call_id', 'source_id')))
    require(not action['host_read_only'] and action['contract'] == 'CURRENT_SOURCE_VALIDATED')
    require('purpose' not in item['slots'] and 'goal_title' not in item['slots'],
            'GOAL_IDENTITY_REVISION_REQUIRES_NEW_REQUEST')
    require(not {'purpose', 'goal_title'}.intersection(item['missing_fields'] + item['conflicts']))
    for field in ('goal_title', 'purpose'):
        if field in parent['slots']:
            require(parent['slots'][field] == origin['identity_slots'][field])
    # The source message remains host history, not stale balance evidence.
    messages = {row['message_id']: row['text'] for row in context['messages']}
    from .goal_profile_reference import unresolved_source
    source_ids = {origin['identity_slots'][f]['source']['message_id'] for f in IDENTITY}
    require(len(source_ids) == 1 and next(iter(source_ids)) in messages)
    message = messages[next(iter(source_ids))]
    for field, key in IDENTITY.items():
        slot = origin['identity_slots'][field]
        require(slot['value'] == origin['identity'][key] and slot['source']['quote'] == slot['value']
                and slot['value'] in message)
    unresolved_source(message, {field: origin['identity'][key] for field, key in IDENTITY.items()})
    from .goal_prepare_identity import check_request_scope
    check_request_scope(message, origin['identity']['user_reason'])
    current_revisions(item, context)
    # If two active, distinct preview identities exist, no 'just now' guessing.
    superseded = {row.get('continuation_of') for row in context['previous_items']}
    active = [row for row in context['previous_items']
              if row.get('goal_preview_origin') and row['reference'] not in superseded]
    identities = {canonical({key: row['goal_preview_origin']['identity'][key] for key in ('title', 'user_reason')})
                  for row in active}
    require(len(identities) == 1, 'AMBIGUOUS_GOAL_PREVIEW_PARENT')
    require(active[-1]['reference'] == parent['reference'], 'STALE_GOAL_PREVIEW_PARENT')
    return deepcopy(origin)
