"""Deterministic parameter normalization from actual current bank observations."""
from datetime import date, datetime, timedelta
import calendar
import re

from src.bank_v1.common import evidence, canonical
from .write_prohibitions import mask as mask_write_prohibitions


PERIOD_WORDS = {'CURRENT_MONTH': ('本月', '这个月'), 'PREVIOUS_MONTH': ('上月', '上个月'),
                'PAST_30_DAYS': ('最近30天', '近30天')}
DAY_OFFSETS = {'今天': 0, '今日': 0, '明天': 1, '明日': 1, '后天': 2}
# This is a bounded phrase grammar, not extraction from arbitrary prose. The
# full match rejects alternatives, negation, approximate ranges and two dates.
DATE_ATOM = r'(?:\d{4}-\d{2}-\d{2}|(?:\d{4}年)?\d{1,2}月\d{1,2}[日号]|今天|今日|明天|明日|后天)'
DATE_PHRASE = re.compile(
    r'(?:最晚|最迟|不晚于|截至|截止到?|(?:希望|计划|打算|预计)?(?:在|于|到)?)?'
    r'\s*(?P<date>' + DATE_ATOM + r')\s*(?:买|购买|买到|完成购买|完成)?[。！!]?$')


def date_atom(expression):
    if not isinstance(expression, str):
        return None
    match = DATE_PHRASE.fullmatch(expression.strip())
    return match['date'] if match else None


def phrase_day(expression, business_day=None):
    atom = date_atom(expression)
    return calendar_day(atom, business_day) if atom else None


def date_source_is_explicit(quote, message):
    """Do not turn a date inside a negated/alternative clause into a choice."""
    if date_atom(quote) is None:
        return False
    clauses = re.split(r'[，,。；;！!？?\n]', mask_write_prohibitions(message))
    matching = [clause for clause in clauses if quote in clause]
    return bool(matching) and all(
        not re.search(r'不是|不要|不在|别在|取消|不用|或', clause)
        and len(re.findall(DATE_ATOM, clause)) == 1 for clause in matching)


def goal_text_matches(field, value, profile, keys):
    """Match whole literal phrases within one goal, never words or semantics."""
    values = [profile.get(key, '') for key in keys]
    if any(value in text for text in values):
        return True
    if field != 'purpose':
        return False
    parts = [part.strip() for part in re.split(r'[，,；;、]', value)]
    # Keep the boundary and order of the two identity fields. Matching each
    # fragment anywhere independently could splice unrelated word fragments.
    return (len(parts) == len(values) == 2 and all(len(part) >= 2 for part in parts)
            and '，'.join(parts) in '，'.join(text.strip() for text in values))


def period_range(expression, business_day):
    explicit = re.fullmatch(r'(\d{4})年(?:(\d{1,2})月)?', expression)
    if explicit:
        year, month = explicit.groups()
        try:
            year = int(year)
            start_month, end_month = (int(month), int(month)) if month else (1, 12)
            return {'start_date': date(year, start_month, 1).isoformat(),
                    'end_date': date(year, end_month, calendar.monthrange(year, end_month)[1]).isoformat()}
        except (ValueError, calendar.IllegalMonthError):
            return None
    period = next((key for key, words in PERIOD_WORDS.items() if expression in words), None)
    if period is None:
        return None
    today = date.fromisoformat(business_day)
    if period == 'CURRENT_MONTH':
        start, end = today.replace(day=1), today
    elif period == 'PREVIOUS_MONTH':
        end = today.replace(day=1) - timedelta(days=1)
        start = end.replace(day=1)
    else:
        start, end = today - timedelta(days=29), today
    return {'start_date': start.isoformat(), 'end_date': end.isoformat()}


def calendar_day(expression, business_day=None):
    if not isinstance(expression, str):
        return None
    if expression in DAY_OFFSETS and business_day:
        return (date.fromisoformat(business_day) + timedelta(days=DAY_OFFSETS[expression])).isoformat()
    chinese = re.fullmatch(r'(?:(\d{4})年)?(\d{1,2})月(\d{1,2})[日号]', expression)
    if chinese:
        year, month, day = chinese.groups()
        if year is None and not business_day:
            return None
        try:
            parsed = date(int(year) if year else date.fromisoformat(business_day).year, int(month), int(day))
        except ValueError:
            return None
        # A missing year is not permission to roll a past date into next year.
        if year is None and parsed < date.fromisoformat(business_day):
            return None
        return parsed.isoformat()
    try:
        parsed = date.fromisoformat(expression)
        return expression if parsed.isoformat() == expression else None
    except (TypeError, ValueError):
        return None


def verified_business_day(row):
    """Use a paired host tool observation, never a model-supplied clock."""
    if row.get('ok') is not True or row.get('status') != 200:
        return None
    body, tool = row.get('response', {}), row.get('tool')
    if tool in {'accounts', 'arrangements', 'forecast'} and body.get('scope', 'bank') == 'bank':
        rebuilt = evidence(body['data'], datetime.fromisoformat(body['observed_at']))
        if any(rebuilt[key] != body[key] for key in ('evidence_id', 'business_date')):
            raise ValueError('INVALID_NORMALIZATION_SOURCE_HASH')
        return body['business_date']
    if tool == 'billing_report' and body.get('scope') == 'bank':
        from src.bank_insights.rules import digest
        if (body.get('currency') != 'CNY' or
                body.get('evidence_id') != 'insight_' + digest([body['as_of'], body['data']])[:24] or
                calendar_day(body['as_of']) is None):
            raise ValueError('INVALID_NORMALIZATION_SOURCE_HASH')
        return body['as_of']
    if tool in {'compare_goal_impact', 'explain_goal_changes'} and body.get('scope') == 'goal_impact':
        from src.bank_v1.projection_journal import digest
        if body['source_id'] != digest({k: v for k, v in body.items() if k != 'source_id'}):
            raise ValueError('INVALID_NORMALIZATION_SOURCE_HASH')
        return body['data']['business_date']
    if tool == 'preview_goal_options' and body.get('scope') == 'goal_preview':
        from src.bank_goals.preview import envelope
        rebuilt = envelope(body['data'])
    elif tool in {'goal_context', 'compare_goal_options'} and body.get('scope') == 'goal_plan':
        from src.bank_goals.evidence import envelope
        rebuilt = envelope(body['data'], tool)
    else:
        return None
    if canonical(rebuilt) != canonical(body):
        raise ValueError('INVALID_NORMALIZATION_SOURCE_HASH')
    return body['data']['business_date']


def current_business_day(rows):
    for row in reversed(rows):
        day = verified_business_day(row)
        if day is not None:
            return day
    return None


def sourced_day(slot, rows):
    """A date may retain its literal quote or use an exactly equivalent ISO day."""
    expression, value = slot['source']['quote'], slot['value']
    atom = date_atom(expression)
    day = calendar_day(atom, current_business_day(rows)) if atom else None
    return day if day is not None and value in {expression, atom, day} else None


def source_record(normalization, rows, *, tool=None, allow_billing=False):
    success = [row for row in rows if row.get('ok') is True and row.get('status') == 200
               and row.get('response', {}).get('scope', 'bank') == 'bank' and
               (tool is None or row['tool'] == tool)]
    matches = [row for row in success if row['response'].get('evidence_id') == normalization['evidence_id']]
    if not matches:
        raise ValueError('NORMALIZATION_SOURCE_NOT_FOUND')
    selected = matches[-1]
    current = [row for row in success if row['tool'] == selected['tool'] and row['args'] == selected['args']][-1]
    if current['response']['evidence_id'] != normalization['evidence_id']:
        raise ValueError('NORMALIZATION_SOURCE_STALE')
    body = selected['response']
    if allow_billing and selected['tool'] == 'billing_report':
        day = verified_business_day(selected)
        if day != current_business_day(rows):
            raise ValueError('NORMALIZATION_SOURCE_STALE')
        return {**body, 'business_date': day}
    # Use bank envelopes, not reports with a different hash scheme, for calendar dates.
    if selected['tool'] not in {'accounts', 'arrangements', 'forecast'}:
        raise ValueError('UNSUPPORTED_NORMALIZATION_SOURCE')
    rebuilt = evidence(body['data'], datetime.fromisoformat(body['observed_at']))
    if rebuilt['evidence_id'] != body['evidence_id'] or rebuilt['business_date'] != body['business_date']:
        raise ValueError('INVALID_NORMALIZATION_SOURCE_HASH')
    return body


def normalized_value(field, source, rows):
    from .schema import FIELD_SPECS
    normalization = source['normalization']
    if type(normalization) is not dict:
        raise ValueError('INVALID_NORMALIZATION')
    kind = normalization.get('kind')
    if kind not in FIELD_SPECS[field].normalizations:
        raise ValueError('NORMALIZATION_NOT_ALLOWED_FOR_FIELD')
    if kind == 'account_name':
        if field not in {'source_account', 'target_account'} or set(normalization) != {'kind', 'evidence_id'}:
            raise ValueError('INVALID_ACCOUNT_NORMALIZATION')
        body = source_record(normalization, rows, tool='accounts')
        matches = [account for account in body['data']['accounts'] if account['name'] in source['quote']]
        if len(matches) != 1 or matches[0]['account_id'] not in {'A', 'B'}:
            raise ValueError('ACCOUNT_NAME_NOT_UNIQUE')
        return matches[0]['account_id']
    if kind == 'calendar_period':
        if set(normalization) != {'kind', 'evidence_id', 'period'}:
            raise ValueError('INVALID_PERIOD_NORMALIZATION')
        period = normalization['period']
        if period not in PERIOD_WORDS or source['quote'] not in PERIOD_WORDS[period]:
            raise ValueError('UNSUPPORTED_PERIOD_EXPRESSION')
        resolved = period_range(source['quote'], source_record(normalization, rows,
            allow_billing=field in {'start_date', 'end_date'})['business_date'])
        return source['quote'] if field == 'period_text' else resolved[field]
    if kind == 'calendar_day':
        if set(normalization) != {'kind', 'evidence_id'}:
            raise ValueError('INVALID_DAY_NORMALIZATION')
        day = source_record(normalization, rows, allow_billing=field in {'start_date', 'end_date'})['business_date']
        value = phrase_day(source['quote'], day)
        if value is None:
            raise ValueError('UNSUPPORTED_DAY_EXPRESSION')
        return value
    raise ValueError('UNKNOWN_NORMALIZATION')


def account_source(slot, rows):
    """Resolve a unique name or validate the legacy literal-ID occurrence.

    rows belongs to the current gateway lease or paired current-turn trace. It
    must never be assembled from historical messages or model-supplied records.
    Explicit legacy labels are validated first and never repaired or ignored.
    """
    source, value = slot['source'], slot['value']
    explicit = 'normalization' in source
    if explicit:
        expected = normalized_value('source_account', source, rows)
        if value != expected:
            raise ValueError('INCORRECT_NORMALIZED_VALUE')
    ids = set(re.findall(r'(?<![A-Za-z0-9_])[AB](?![A-Za-z0-9_])', source['quote']))
    observations = [row for row in rows if row.get('tool') == 'accounts'
                    and row.get('ok') is True and row.get('status') == 200]
    selected, matches = None, []
    if observations:
        selected = observations[-1]
        # Existing bank envelopes omit scope; paired_records already treats that
        # exact legacy shape as bank. An explicit different scope is rejected.
        if selected.get('args') != {} or selected.get('response', {}).get('scope', 'bank') != 'bank':
            raise ValueError('INVALID_ACCOUNT_SOURCE_SCOPE')
        body = source_record({'evidence_id': selected['response']['evidence_id']}, rows, tool='accounts')
        matches = [account for account in body['data']['accounts']
                   if account.get('name') and account['name'] in source['quote']]
        if len(matches) > 1:
            raise ValueError('ACCOUNT_NAME_NOT_UNIQUE')
    if matches:
        expected = matches[0]['account_id']
        if expected not in {'A', 'B'} or ids - {expected} or value != expected:
            raise ValueError('UNBOUND_SLOT_ACCOUNT')
    else:
        # Legacy contract proves exact literal occurrence, not transfer-role
        # interpretation. Do not impose a new whole-quote uniqueness rule here.
        if value not in ids or value not in {'A', 'B'}:
            raise ValueError('UNBOUND_SLOT_ACCOUNT')
        expected = value
    # The original slot is deliberately not annotated or rewritten.
    basis = {'version': 1, 'rule': 'EXPLICIT_ACCOUNT_NAME' if explicit else
             ('CURRENT_ACCOUNT_NAME' if matches else 'LITERAL_ACCOUNT_ID'),
             'canonical_value': expected}
    if not matches:
        basis['guarantee'] = 'LITERAL_OCCURRENCE_ONLY'
    if selected is not None:
        basis.update(evidence_id=selected['response']['evidence_id'], tool='accounts', arguments={})
        if selected.get('call_id') is not None:
            basis['call_id'] = selected['call_id']
    return basis
