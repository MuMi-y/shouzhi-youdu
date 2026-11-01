"""Bounded cashflow date roles. No tool execution or general intent inference.

User spans establish roles; current paired observations establish the clock.
Source anchors survive continuation, but never stand in for result evidence.
"""
from copy import deepcopy
from datetime import date
import re

from .normalization import DATE_ATOM, calendar_day, date_atom, verified_business_day, source_record

INTENTS = frozenset({'cashflow_compare', 'cashflow_forecast'})
ROLES = {'date_text': 'SPENDING_DATE', 'through_date_text': 'FORECAST_CUTOFF'}
DAY_TOKEN = r'(?<![\d年])' + DATE_ATOM + r'(?![\d年月日号])'
CUTOFF = re.compile(r'(?:截至|截止到?|预测到)\s*(?P<date>' + DAY_TOKEN + ')')
SPENDING = re.compile(r'(?P<date>' + DAY_TOKEN + r')\s*(?:拟|将|要)?(?:消费|花|支出)\s*'
                      r'\d+(?:\.\d{1,2})?\s*元')
EXPENSE = re.compile(r'(?:消费|花|支出)\s*\d+(?:\.\d{1,2})?\s*元|消费与不消费|和不花相比')
BLOCK = re.compile(r'[^；;。\n！？!?]+')
UNCERTAIN = re.compile(r'不是|不在|别在|取消|或|大约|左右|差不多|不要(?=(?:在)?(?:'
                       + DATE_ATOM + r'|截至|截止|预测到))')


def require(value, code):
    if not value:
        raise ValueError(code)


def atom(quote):
    parsed = date_atom(quote)
    if parsed:
        return parsed
    match = re.fullmatch(r'预测到\s*(' + DATE_ATOM + ')', quote)
    return match[1] if match else None


def clock(rows):
    observations = [(row, verified_business_day(row)) for row in rows]
    observations = [(row, day) for row, day in observations if day is not None]
    require(len({day for _, day in observations}) <= 1, 'CASHFLOW_CLOCK_CONFLICT')
    if not observations:
        return None, []
    return observations[-1][1], [
        {'call_id': row['call_id'], 'evidence_id': row['response'].get('evidence_id', row['response'].get('source_id'))}
        for row, _ in observations]


def validate_slot(slot, rows):
    """Called after structural and literal message/source validation."""
    quote, value = slot['source']['quote'], slot['value']
    token = atom(quote)
    require(token is not None, 'UNRESOLVED_DATE_SOURCE')
    day, _ = clock(rows)
    canonical = calendar_day(token, day)
    if 'normalization' in slot['source']:
        normal = slot['source']['normalization']
        require(set(normal) == {'kind', 'evidence_id'} and normal['kind'] == 'calendar_day',
                'INVALID_DAY_NORMALIZATION')
        body = source_record(normal, rows)
        require(body['business_date'] == day and value == canonical and canonical is not None,
                'INCORRECT_NORMALIZED_VALUE')
    require(type(value) is str and value in {quote, token, canonical}, 'UNBOUND_SLOT_TEXT')
    # Unknown/missing clock is disclosed by bind(), not guessed using the OS.
    return deepcopy(slot)


def regions(message, quote):
    if not quote or message.count(quote) != 1:
        return []
    start = message.index(quote); end = start + len(quote)
    return [(b.start(), b.end()) for b in BLOCK.finditer(message)
            if b.start() < end and start < b.end()]


def owned_regions(items, context):
    """A short action quote cannot hide another date within its request block.

    Two cashflow items sharing a block are ambiguous, never assigned by value.
    A semicolon/sentence boundary supports separate requests in one message.
    """
    message = next(m['text'] for m in context['messages'] if m['message_id'] == context['current_message_id'])
    candidates = [i for i in items if i.get('intent') in INTENTS]
    scopes = {}
    for item in candidates:
        quote = item.get('action', {}).get('source', {}).get('quote')
        if quote is None and len(candidates) == 1:  # Legacy source, not execution authority.
            quote = message
        scopes[item['id']] = regions(message, quote)
    # Full-message discovery detects omitted conditions without assigning
    # another item's dates to this item or choosing a role from its value.
    all_spans = [span for spans in scopes.values() for span in spans]
    for item in items:
        if item.get('intent') in INTENTS:
            continue
        quote = (item.get('action', {}).get('source') or {}).get('quote', '')
        if quote and message.count(quote) == 1:
            start = message.index(quote)
            all_spans.append((start, start + len(quote)))
    discovered = discoveries(message, [(b.start(), b.end()) for b in BLOCK.finditer(message)])
    unassigned = any(not any(start <= found['span'][0] and found['span'][1] <= end
                            for start, end in all_spans)
                     for values in discovered.values() for found in values)
    result = {}
    for item in candidates:
        spans = scopes[item['id']]
        overlapping = any(set(spans) & set(other) for key, other in scopes.items() if key != item['id'])
        result[item['id']] = {'spans': spans, 'ambiguous': not spans or overlapping or unassigned}
    return result


def discoveries(message, spans):
    values = {field: [] for field in ROLES}
    for start, end in spans:
        block = message[start:end]
        for field, pattern in (('date_text', SPENDING), ('through_date_text', CUTOFF)):
            for match in pattern.finditer(block):
                # Inspect the date's comma-delimited clause, not the cropped quote.
                left = max(block.rfind(c, 0, match.start()) for c in ('，', ',')) + 1
                rights = [block.find(c, match.end()) for c in ('，', ',')]
                right = min([p for p in rights if p >= 0] or [len(block)])
                clause = block[left:right]
                values[field].append({'span': [start + match.start('date'), start + match.end('date')],
                    'atom': match['date'], 'uncertain': bool(UNCERTAIN.search(clause)),
                    'scope': [start, end]})
    return values


def source_role(field, slot, message, spans):
    quote = slot['source']['quote']
    locations = [match.span() for match in re.finditer(re.escape(quote), message)
                 if any(start <= match.start() and match.end() <= end for start, end in spans)]
    if len(locations) != 1:
        return None
    begin, end = locations[0]
    candidates = discoveries(message, spans)[field]
    if len(candidates) != 1 or candidates[0]['uncertain']:
        return None
    found = candidates[0]
    if not begin <= found['span'][0] < found['span'][1] <= end:
        return None
    return found


def bind(item, parent, context, rows, scope, *, supplied_fields):
    if item['intent'] not in INTENTS:
        return
    messages = {m['message_id']: m['text'] for m in context['messages']}
    current_id = context['current_message_id']; current = messages[current_id]
    issue = []
    def unknown(field, reason):
        issue.append({'field': field, 'rule': reason})
    if scope['ambiguous']:
        unknown('through_date_text', 'CASHFLOW_ITEM_SCOPE_UNRESOLVED')
    found = discoveries(current, scope['spans'])
    owned_text = '\n'.join(current[a:b] for a, b in scope['spans'])
    if len(re.findall(r'截至|截止到?|预测到', owned_text)) != len(found['through_date_text']):
        unknown('through_date_text', 'CASHFLOW_CUTOFF_EXPRESSION_UNRESOLVED')
    comparing = bool(EXPENSE.search(owned_text)) or bool(parent and
        parent.get('cashflow_contract', {}).get('comparison_requested') and
        {'amount_minor', 'expense_minor'} & item['slots'].keys())
    if item['intent'] == 'cashflow_forecast' and comparing:
        unknown('through_date_text', 'CONSUMPTION_CANNOT_BECOME_QUERY')
    require(item['intent'] != 'cashflow_forecast' or item['action']['effective_kind'] == 'ANALYZE',
            'CASHFLOW_QUERY_READ_ONLY')
    current_source_fields = {field for field in supplied_fields
        if field in item['slots'] and item['slots'][field]['source']['message_id'] == current_id}
    blocked_fields = set()
    for field in ROLES:
        candidates = found[field]
        # Merged parent slots cannot prove a condition expressed this turn.
        if candidates and field not in current_source_fields:
            unknown(field, 'CURRENT_CASHFLOW_DATE_NOT_DECLARED')
            blocked_fields.add(field)
        if len(candidates) > 1 or any(c['uncertain'] for c in candidates):
            unknown(field, 'CASHFLOW_DATE_ROLE_AMBIGUOUS')
    business_day, observations = clock(rows)
    bindings = {}
    for field in ROLES.keys() & item['slots'].keys():
        slot = item['slots'][field]
        old = item['constraint_anchors'].pop(field, None)
        if field in blocked_fields:
            continue
        source_id = slot['source']['message_id']; historical = source_id != current_id
        spans = scope['spans']
        if historical:
            valid_parent = parent and parent['intent'] == item['intent'] and parent['slots'].get(field) == slot
            if not valid_parent:
                unknown(field, 'CASHFLOW_HISTORY_ROLE_UNPROVEN'); continue
            old_scope = parent.get('cashflow_contract', {}).get('date_roles', {}).get(field, {}).get('source_scope')
            spans = [old_scope] if old_scope else regions(messages[source_id],
                (parent.get('action', {}).get('source') or {}).get('quote', ''))
        role = source_role(field, slot, messages[source_id], spans)
        if role is None or (not historical and scope['ambiguous']):
            unknown(field, 'CASHFLOW_DATE_ROLE_UNPROVEN'); continue
        if historical:
            if (not old or old.get('source') != slot['source'] or old.get('value') != slot['value']
                    or calendar_day(old.get('canonical_value')) is None
                    or old.get('role', ROLES[field]) != ROLES[field]):
                unknown(field, 'CASHFLOW_HISTORY_ANCHOR_UNPROVEN'); continue
            resolved = old['canonical_value']
            # A prior canonical anchor may lack clock metadata; do not re-date it.
            anchor_day = old.get('business_date')
            refs = old.get('clock_sources', [])
        else:
            resolved = calendar_day(role['atom'], business_day)
            if resolved is None or business_day is None:
                unknown(field, 'CASHFLOW_DATE_CLOCK_UNRESOLVED'); continue
            anchor_day, refs = business_day, observations
        anchor = {'field': field, 'value': slot['value'], 'source': deepcopy(slot['source']),
            'canonical_value': resolved, 'status': 'BOUND', 'rule': 'SOURCED_CASHFLOW_DATE_ROLE',
            'role': ROLES[field], 'source_span': role['span'], 'source_scope': role['scope'],
            'business_date': anchor_day, 'clock_sources': deepcopy(refs)}
        item['constraint_anchors'][field] = anchor
        bindings[field] = deepcopy(anchor)
    item['cashflow_contract'] = {'version': 1, 'date_roles': bindings, 'issues': issue,
        'comparison_requested': comparing, 'source_scope': deepcopy(scope),
        'supplied_fields': sorted(supplied_fields), 'current_source_fields': sorted(current_source_fields)}


def cutoff(row, expected):
    args, data = row['arguments'], row['data']
    require(args.get('plan_scope', 'window') == 'window', 'FORECAST_CUTOFF_REQUIRES_WINDOW')
    require(args.get('through_date') == data['through_date'] == data['scenario_minima']['window_end'] == expected,
            'FORECAST_CUTOFF_DOES_NOT_MATCH_REQUEST')
    require(data['as_of_date'] == data['scenario_minima']['window_start'], 'FORECAST_WINDOW_START_MISMATCH')
    require(0 <= (date.fromisoformat(expected) - date.fromisoformat(data['as_of_date'])).days <= 366,
            'FORECAST_WINDOW_OUT_OF_RANGE')


def apply_issues(item, checks):
    """No omission/uncertain role is allowed to fall back to generic success."""
    for issue in item.get('cashflow_contract', {}).get('issues', []):
        field = issue['field']; slot = item['slots'].get(field, {})
        checks[field] = {'field': field, 'value': slot.get('value'), 'source': deepcopy(slot.get('source')),
                         'status': 'UNVERIFIED', 'rule': issue['rule'], 'evidence_refs': []}
