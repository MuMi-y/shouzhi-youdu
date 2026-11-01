"""Verify finite, complete tool-generated ratio phrases with exact anchors.

This is a display check, not proof of all surrounding natural-language claims.
"""
import re
from src.bank_insights.ratio_display import metric_label, ratio_display


def latest_report(trace):
    report = None
    for record in trace:
        if not isinstance(record, dict) or record.get('kind') != 'response' or record.get('tool') != 'billing_report':
            continue
        report = None
        envelope = record.get('response')
        if record.get('ok') is not True or not isinstance(envelope, dict) or envelope.get('scope', 'bank') != 'bank':
            continue
        data = envelope.get('data')
        candidate = data.get('report') if isinstance(data, dict) else None
        if isinstance(candidate, dict):
            report = candidate
    return report


def display_evidence(report):
    """Require the actual additive field and consistency with its typed totals."""
    if not isinstance(report, dict):
        return []
    periods = [(report, 'totals')]
    if report.get('comparison_status') == 'COMPLETED' and isinstance(report.get('previous_period'), dict):
        periods.append((report['previous_period'], 'previous_period.totals'))
    proofs = []
    for period, path in periods:
        totals, metrics = period.get('totals'), period.get('metrics')
        if not isinstance(totals, dict) or not isinstance(metrics, list):
            continue
        for metric in metrics:
            if not isinstance(metric, dict) or metric.get('status') != 'DEFINED':
                continue
            name = metric.get('name')
            numerator_key = {'expense_to_income': 'expense_minor', 'net_flow_to_income': 'net_change_minor'}.get(name)
            expected_period = {key: period.get(key) for key in ('start_date', 'end_date')}
            if (not numerator_key or not all(isinstance(x, str) for x in expected_period.values())
                    or metric.get('period') != expected_period
                    or metric.get('numerator_path') != path + '.' + numerator_key
                    or metric.get('denominator_path') != path + '.income_minor'):
                continue
            display = ratio_display(totals.get(numerator_key), totals.get('income_minor'),
                                    label=metric_label(name, path))
            if (display is None or metric.get('display') != display
                    or display['exact_text'] != str(metric.get('value_percent')) + '%'):
                continue
            proofs.append({'metric': name, 'period': expected_period, 'display': display})
    return proofs


def phrase_pattern(phrase):
    """Allow layout whitespace between tokens, never within a numeric token."""
    label, values = phrase.split('约', 1)
    approximate, exact = values[:-1].split('（')
    space = r'[ \t\u3000]*'

    def token_pattern(value):
        if value.endswith('%'):
            return re.escape(value[:-1]) + space + '[%％]'
        return re.escape(value)

    return (re.escape(label) + space + '约' + space
            + '(?P<approx>' + token_pattern(approximate) + ')' + space
            + '（' + space + token_pattern(exact) + space + '）')


def billing_ratio_display_checks(text, trace):
    # Markdown emphasis and ASCII parentheses don't change a numeric claim.
    plain = text.replace('**', '').replace('`', '').replace('(', '（').replace(')', '）')
    masked = list(plain)
    checks = []
    for proof in display_evidence(latest_report(trace)):
        for phrase in proof['display']['phrases']:
            for match in re.finditer(phrase_pattern(phrase), plain):
                # Mask only the rounded portion; keep the exact anchor for the
                # existing strict checker. Every unrecognized number stays visible.
                start, end = match.span('approx')
                masked[start:end] = ' ' * (end-start)
                checks.append({'status': 'VERIFIED_DISPLAY', 'claim': match.group(),
                               'metric': proof['metric'], 'period': proof['period'],
                               'exact_text': proof['display']['exact_text'],
                               'rounding': proof['display']['rounding'], 'quantum': '1'})
    return {'numeric_text': ''.join(masked), 'checks': checks,
            'coverage': 'COMPLETE_TOOL_PHRASE_WITH_PERIOD_METRIC_AND_EXACT_ANCHOR',
            'limitation': '仅核验完整工具显示片段；不证明周围自由文本、跨句时间指代或任意同义表达。'}
