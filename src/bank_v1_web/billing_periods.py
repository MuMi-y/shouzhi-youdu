"""Finite checks for explicitly labelled previous-period date pairs.

Dates come from a successful current-turn billing response, not the prompt.
Other date expressions are not certified by this check.
"""
from datetime import date
import re


DATE = r'(?:\d{4}-\d{1,2}-\d{1,2}|\d{4}/\d{1,2}/\d{1,2}|\d{4}年\d{1,2}月\d{1,2}日)'
RANGE = re.compile(r'(?P<start>' + DATE + r')\s*(?:至|到|—|–|~|～|\s-\s)\s*(?P<end>' + DATE + r')')
LABEL = r'(?:前一等长期间|前一等长周期|前一等长时段|前一期间|前一周期|等长前期|等长上期|上一等长期间|上期)'
BEFORE = re.compile(LABEL + r'\s*(?:的?(?:日期|范围|窗口|起止日期))?\s*(?:为|是|：|:)?\s*[（(]?\s*$')
AFTER = re.compile(r'^\s*[）)]?\s*(?:的)?\s*' + LABEL)


def parsed_date(value):
    parts = re.findall(r'\d+', value)
    try:
        return date(*map(int, parts)).isoformat()
    except (ValueError, TypeError):
        return None


def pair(value):
    if not isinstance(value, dict):
        return None
    start, end = value.get('start_date'), value.get('end_date')
    if not isinstance(start, str) or not isinstance(end, str):
        return None
    normalized = parsed_date(start), parsed_date(end)
    if None in normalized or normalized[0] > normalized[1]:
        return None
    return normalized


def previous_evidence(trace):
    """A later failed billing read invalidates the earlier billing proof."""
    allowed = set()
    for record in trace:
        if not isinstance(record, dict) or record.get('kind') != 'response' or record.get('tool') != 'billing_report':
            continue
        allowed = set()
        response = record.get('response')
        if record.get('ok') is not True or not isinstance(response, dict) or response.get('scope', 'bank') != 'bank':
            continue
        data = response.get('data')
        report = data.get('report') if isinstance(data, dict) else None
        if not isinstance(report, dict):
            continue
        previous = pair(report.get('previous_period'))
        if report.get('comparison_status') == 'COMPLETED' and previous:
            allowed.add(previous)
        window = report.get('comparison_window')
        if (isinstance(window, dict) and window.get('relation') == 'PREVIOUS_EQUAL_LENGTH'
                and window.get('boundary') == 'INCLUSIVE'
                and window.get('data_status') in {'NOT_REQUESTED', 'QUERIED'}):
            calculated = pair(window)
            if calculated:
                allowed.add(calculated)
    return allowed


def billing_previous_period_checks(text, trace):
    allowed = previous_evidence(trace)
    checks, reasons = [], []
    plain = text.replace('**', '').replace('`', '')
    for match in RANGE.finditer(plain):
        before = plain[max(0, match.start()-36):match.start()]
        after = plain[match.end():match.end()+24]
        if not (BEFORE.search(before) or AFTER.search(after)):
            continue
        claimed = parsed_date(match['start']), parsed_date(match['end'])
        supported = None not in claimed and claimed in allowed
        checks.append({'claim': match[0], 'dates': list(claimed),
                       'status': 'VERIFIED_PAIR' if supported else 'UNSUPPORTED'})
        if not supported:
            reasons.append({'code': 'UNSUPPORTED_BILLING_PREVIOUS_PERIOD', 'clause': match[0]})
    return {'checks': checks, 'reasons': reasons,
            'coverage': 'EXPLICIT_LABELLED_FULL_DATE_PAIRS_ONLY',
            'limitation': '未覆盖无年份、相对日期、转述和所有同义表达；验证日期对不证明前期资料完整或已经比较。'}
