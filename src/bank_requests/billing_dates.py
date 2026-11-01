"""Bounded billing boundaries; never infer a day from arbitrary prose."""
import re
import calendar
from copy import deepcopy
from datetime import date

from .normalization import calendar_day, period_range, verified_business_day, PERIOD_WORDS


ATOM = r'(?:\d{4}-\d{2}-\d{2}|\d{4}年\d{1,2}月\d{1,2}[日号]|(?:\d{4}年)?\d{1,2}月|\d{4}年|今天|今日|本月|这个月|上个月|上月|最近30天|近30天)'
PERIOD = re.compile(r'(?:(\d{4})年)?(\d{1,2})月|(\d{4})年')
RELATIVE = {word for words in PERIOD_WORDS.values() for word in words}
# Consume the entire numeric calendar-shaped expression before accepting its
# grammar. In particular, an invalid day must not fall back to its month.
NUMBER_CHARACTERS = '0-9零〇一二三四五六七八九十百两'
CALENDAR_NUMBER = rf'(?>[{NUMBER_CHARACTERS}]+)'
# Bounded quantity grammar: a number immediately followed by a classifier is
# not an incomplete day. Atomic numerals prevent backtracking into a prefix.
QUANTITY_UNIT = r'(?:笔|项|次|元|块|个|张|条|份|件|家|人|种|期)'
DAY_SUFFIX = rf'(?:{CALENDAR_NUMBER}(?:日|号)|{CALENDAR_NUMBER}(?!{QUANTITY_UNIT}))'
CALENDAR_TOKEN = re.compile(
    rf'{CALENDAR_NUMBER}年(?:{CALENDAR_NUMBER}月(?:{DAY_SUFFIX})?)?|'
    rf'{CALENDAR_NUMBER}月(?:{DAY_SUFFIX})?|'
    r'\d{4,}(?:[-/]\d+){1,2}|今天|今日|本月|这个月|上个月|上月|最近30天|近30天')
UNRESOLVED = re.compile(r'不|没|别|未|无|并非|而非|取消|放弃|或|大概|左右|也许')
# These whole lexical compounds contain 别 but are not negative imperatives.
# All unrecognized uses stay unresolved, including negated unknown actions.
NON_NEGATING_WORD = re.compile(r'分别|类别|特别|区别|差别|性别|个别|识别|辨别')
POLAR_QUESTION = re.compile(r'([\u4e00-\u9fff]{1,2})(?:不|没)\1')
QUERY_WORD = re.compile(r'查|核对|分析|统计|看|读')
NEGATED_WRITE = re.compile(r'(?:不要|不用|不必|不想|不需要|无需|别)(?:再|自动)?'
                           r'(?:转账|付款|扣款|支付|办理)(?=[，,。；;！!？?\s]|$)')
COMPLETE = re.compile(r'完整|整月|全年|整年')
CONNECTOR = re.compile(r'\s*(?:到|至|—|~|～)\s*')


def require(condition, code):
    if not condition:
        raise ValueError(code)


def is_period(expression):
    return isinstance(expression, str) and (PERIOD.fullmatch(expression) is not None or expression in RELATIVE)


def explicit_source(field, quote, message):
    try:
        return locate_source(field, {'quote': quote}, message) is not None
    except ValueError:
        return False


def period_is_affirmed(message, matches):
    """A limited scope grammar, not general language intent classification.

    A post-period repeated-predicate question is not date negation. A complete
    negated write clause remains a write prohibition but does not cancel reads.
    All other negation/uncertainty stays unresolved; no chosen period is guessed.
    """
    masked = list(message)
    for match in NON_NEGATING_WORD.finditer(message):
        masked[match.start():match.end()] = ' ' * (match.end() - match.start())
    for match in POLAR_QUESTION.finditer(message):
        if match.start() >= matches[-1].end() and not QUERY_WORD.search(match[1]):
            masked[match.start():match.end()] = ' ' * (match.end() - match.start())
    for match in NEGATED_WRITE.finditer(message):
        masked[match.start():match.end()] = ' ' * (match.end() - match.start())
    return UNRESOLVED.search(''.join(masked)) is None


def locate_source(field, source, message):
    """Derive roles from the whole message, before looking at a slot's value."""
    quote = source['quote']
    code = 'UNRESOLVED_BILLING_DATE_SOURCE'
    require(bool(quote) and message.count(quote) == 1, code)
    matches = list(CALENDAR_TOKEN.finditer(message))
    if not matches:
        return None  # Unknown period prose remains unverified, not a date.
    require(len(matches) <= 2 and period_is_affirmed(message, matches), code)
    for match in matches:
        token = match.group()
        require(re.fullmatch(ATOM, token) is not None, code)
        before, after = message[max(0, match.start()-1):match.start()], message[match.end():match.end()+1]
        quantity = re.match(CALENDAR_NUMBER + QUANTITY_UNIT, message[match.end():])
        require(not re.search(r'[A-Za-z0-9_/\-]', before) and
                (quantity is not None or not re.search(rf'[A-Za-z{NUMBER_CHARACTERS}_年月日号/\-]', after)), code)
        if is_period(token):
            calendar_match = PERIOD.fullmatch(token)
            if calendar_match:
                year, month, full_year = calendar_match.groups()
                try:
                    date(int(full_year or year or 2000), int(month or 1), 1)
                except ValueError:
                    raise ValueError('INVALID_BILLING_PERIOD') from None
        elif token not in {'今天', '今日'}:
            require(calendar_day(token) is not None, code)
    kind = 'single' if len(matches) == 1 else 'range'
    if kind == 'range':
        require(CONNECTOR.fullmatch(message[matches[0].end():matches[1].start()]) is not None, code)
    offset = message.index(quote)
    quoted = [offset, offset + len(quote)]
    selected = matches if field == 'period_text' or kind == 'single' else [matches[0 if field == 'start_date' else 1]]
    require(all(quoted[0] <= m.start() and m.end() <= quoted[1] for m in selected), code)
    endpoints = []
    for index, match in enumerate(matches):
        span = [match.start(), match.end()]
        inside = quoted[0] <= span[0] and span[1] <= quoted[1]
        local = [span[0]-offset, span[1]-offset] if inside else None
        require(message[slice(*span)] == match.group() and
                (local is None or quote[slice(*local)] == match.group()), code)
        endpoints.append({'text': match.group(), 'role': 'period' if kind == 'single' else ('start' if index == 0 else 'end'),
                          'message_span': span, 'quote_span': local})
    expression = message[matches[0].start():matches[-1].end()]
    return {'message_id': source.get('message_id'), 'original_quote': quote,
            'quote_span_in_message': quoted, 'endpoints': endpoints,
            'expression': expression, 'expression_kind': kind, 'field_role': field,
            'requires_complete': bool(COMPLETE.search(message))}


def source_resolution(field, source, message, business_day):
    span = locate_source(field, source, message)
    if span is None:
        return None
    tokens = [endpoint['text'] for endpoint in span['endpoints']]
    if span['expression_kind'] == 'single':
        info = resolve_period(tokens[0], business_day, message)
        if info is None:
            return None  # A literal single day still binds only its own field.
    else:
        requested, observed = {}, {}
        for key, token in zip(('start_date', 'end_date'), tokens):
            period = resolve_period(token, business_day, message)
            day = boundary(key, token, business_day)
            require(day is not None, 'UNRESOLVED_BILLING_DATE_SOURCE')
            requested[key] = period['requested_range'][key] if period else day
            observed[key] = day
        require(requested['start_date'] <= requested['end_date'] and observed['start_date'] <= observed['end_date'],
                'INVALID_BILLING_PERIOD')
        info = {'business_date': business_day, 'requested_range': requested, 'observable_range': observed,
                'coverage': 'FULL_REQUESTED_RANGE' if requested == observed else 'AS_OF_CUTOFF'}
    return {**info, 'literal_quote': source['quote'], 'source_span': span}


def has_period_source(field, source, message):
    span = locate_source(field, source, message)
    return span is not None and (span['expression_kind'] == 'range' or is_period(span['endpoints'][0]['text']))


def validate_source_value(field, slot, message, business_day):
    source, value = slot['source'], slot['value']
    span = locate_source(field, source, message)
    if span is None:
        require(field == 'period_text', 'UNRESOLVED_BILLING_DATE_SOURCE')
        return None
    info = source_resolution(field, source, message, business_day)
    if field == 'period_text':
        require(info is not None and value in {source['quote'], span['expression']}, 'UNBOUND_BILLING_PERIOD')
    else:
        token = span['endpoints'][0 if span['expression_kind'] == 'single' or field == 'start_date' else 1]['text']
        expected = info['observable_range'][field] if info else boundary(field, token, business_day)
        require(type(value) is str and value == expected,
                'INCORRECT_NORMALIZED_VALUE' if 'normalization' in source else 'UNBOUND_BILLING_DATE')
    return span


def period_signature(info, source_span=None):
    """Equal observation windows alone do not establish the same request."""
    span = source_span if source_span is not None else info.get('source_span', {})
    return (tuple(sorted(info['requested_range'].items())), tuple(sorted(info['observable_range'].items())),
            span.get('expression_kind', 'single'), tuple(e['role'] for e in span.get('endpoints', [{'role': 'period'}])),
            span.get('requires_complete', False))


def boundary(field, expression, business_day=None):
    """Only the observable boundary is accepted; a day is never clipped."""
    if is_period(expression):
        period = resolve_period(expression, business_day)
        return period['observable_range'][field]
    day = calendar_day(expression, business_day)
    require(day is None or business_day is None or day <= business_day, 'BILLING_FUTURE_DAY')
    return day


def resolve_period(expression, business_day, message=''):
    """A calendar request and its observation window are distinct host facts."""
    if not is_period(expression):
        return None
    require(calendar_day(business_day) is not None, 'BILLING_CLOCK_REQUIRED')
    today = date.fromisoformat(business_day)
    match = PERIOD.fullmatch(expression)
    try:
        if match:
            year, month, full_year = match.groups()
            year = int(full_year or year or today.year)
            first, last = (int(month), int(month)) if month else (1, 12)
            requested = {'start_date': date(year, first, 1).isoformat(),
                         'end_date': date(year, last, calendar.monthrange(year, last)[1]).isoformat()}
        elif expression in PERIOD_WORDS['CURRENT_MONTH']:
            requested = {'start_date': today.replace(day=1).isoformat(),
                         'end_date': today.replace(day=calendar.monthrange(today.year, today.month)[1]).isoformat()}
        else:
            requested = period_range(expression, business_day)
    except (ValueError, calendar.IllegalMonthError):
        raise ValueError('INVALID_BILLING_PERIOD') from None
    require(requested['start_date'] <= business_day, 'BILLING_FUTURE_PERIOD')
    observed = {**requested, 'end_date': min(requested['end_date'], business_day)}
    clipped = observed != requested
    require(not clipped or not re.search(r'完整|整月|全年|整年', message), 'BILLING_COMPLETE_PERIOD_NOT_AVAILABLE')
    return {'literal_quote': expression, 'business_date': business_day, 'requested_range': requested,
            'observable_range': observed, 'coverage': 'AS_OF_CUTOFF' if clipped else 'FULL_REQUESTED_RANGE'}


def clock_and_reports(rows):
    """Rows must already be paired by the host. Never trust an error's clock."""
    reports, days = [], set()
    for row in rows:
        if row.get('ok') is not True or row.get('status') != 200:
            continue
        if row['tool'] not in {'billing_report', 'accounts', 'arrangements', 'forecast'}:
            continue
        day = verified_business_day(row)
        if day is not None:
            days.add(day)
        if row['tool'] == 'billing_report':
            body, args = row['response'], row['args']
            require(day is not None, 'BILLING_CLOCK_REQUIRED')
            report = body['data']['report']
            require(all(report.get(k) == args.get(k) for k in ('start_date', 'end_date')) and
                    args['start_date'] <= args['end_date'] <= day, 'BILLING_REPORT_PERIOD_INCONSISTENT')
            reports.append({'call_id': row['call_id'], 'evidence_id': body['evidence_id'],
                            'business_date': day, 'arguments': deepcopy(args)})
    require(len(days) <= 1, 'BILLING_CLOCK_CONFLICT')
    return next(iter(days), None), reports


def inherited_anchor(field, slot, parent, messages):
    anchor = (parent or {}).get('constraint_anchors', {}).get(field)
    require(anchor is not None and anchor.get('value') == slot['value'] and anchor.get('source') == slot['source']
            and anchor.get('status') == 'BOUND', 'BILLING_HISTORY_REQUIRES_CURRENT_PERIOD')
    info = anchor.get('billing_resolution')
    require(isinstance(info, dict), 'BILLING_HISTORY_REQUIRES_CURRENT_PERIOD')
    source = slot['source']
    require(explicit_source(field, source['quote'], messages[source['message_id']]), 'UNRESOLVED_BILLING_DATE_SOURCE')
    message = messages[source['message_id']]
    if 'source_span' in info:
        validate_source_value(field, slot, message, info['business_date'])
        rebuilt = source_resolution(field, source, message, info['business_date'])
    else:
        # R5 anchors were accepted only for bare period tokens. Never backfill
        # a new span onto an old rejected long quote or use today's clock.
        require(is_period(source['quote']), 'INVALID_BILLING_HISTORY_ANCHOR')
        rebuilt = resolve_period(source['quote'], info['business_date'], message)
    require(rebuilt == info, 'INVALID_BILLING_HISTORY_ANCHOR')
    if 'source_span' not in info:
        expected = source['quote'] if field == 'period_text' else info['observable_range'][field]
        require(slot['value'] == expected, 'INVALID_BILLING_HISTORY_ANCHOR')
    return deepcopy(anchor)


def bind_item(item, parent, context, rows):
    """Keep verified calendar resolution separate from this round's report clock."""
    if item['intent'] != 'billing':
        return
    day, reports = clock_and_reports(rows)
    messages = {m['message_id']: m['text'] for m in context['messages']}
    resolutions, signatures = {}, set()
    for field, slot in item['slots'].items():
        source = slot['source']
        span = locate_source(field, source, messages[source['message_id']])
        if span is None or span['expression_kind'] == 'single' and not is_period(span['endpoints'][0]['text']):
            continue  # Literal days retain strict per-field binding; unknown text stays unverified.
        if source['message_id'] != context['current_message_id']:
            anchor = inherited_anchor(field, slot, parent, messages)
            info = anchor['billing_resolution']
        else:
            info = source_resolution(field, source, messages[source['message_id']], day)
            anchor = {'field': field, 'value': slot['value'], 'source': deepcopy(source), 'status': 'BOUND',
                      'rule': 'BILLING_OBSERVABLE_PERIOD', 'billing_resolution': info,
                      'canonical_value': info['observable_range'] if field == 'period_text' else info['observable_range'][field]}
        resolutions[field] = deepcopy(info)
        # Legacy anchors retain their original representation. Derive their
        # comparison flags from the same original message, without migration.
        signatures.add(period_signature(info, span))
        item['constraint_anchors'][field] = anchor
    require(len(signatures) <= 1, 'CONFLICTING_BILLING_PERIODS')
    item['billing_period_binding'] = {'business_date': day, 'reports': reports, 'resolutions': resolutions}


def verify_report(item, snapshot):
    binding = item.get('billing_period_binding')
    if binding is None:
        return {}  # Old persisted deliveries retain their old recheck contract.
    require(snapshot['business_date'] == binding['business_date'] and any(
        all(snapshot[k] == candidate[k] for k in ('evidence_id', 'business_date', 'arguments'))
        for candidate in binding['reports']), 'BILLING_SELECTED_REPORT_MISMATCH')
    return binding['resolutions']
