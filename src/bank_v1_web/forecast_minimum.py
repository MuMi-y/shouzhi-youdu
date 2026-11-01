"""Finite forecast-minimum claims bound to one typed scenario snapshot.

This is not a general factuality checker. Unparsed scopes and multiple snapshots
are explicit coverage gaps, never evidence that a matching number is correct.
"""
from datetime import date
from decimal import Decimal
import json
import re

SCENARIOS = {
    'PRIMARY': 'ASSUMED_FROM_ACTIVE_NON_OVERDUE_ARRANGEMENTS',
    'WITHOUT_FUTURE_INCOME': 'EXCLUDED',
}
MINIMUM = re.compile(r'最低(?:合计余额|资金余额|余额|点|值)')
SCOPE = re.compile(r'(?P<primary>主预测|主口径|主情景)|'
                   r'(?P<conservative>保守(?:口径|预测|情景)|(?:不计|不含|排除)未来收入|'
                   r'(?:未来|预计|该)?收入(?:未|不)到账)')
AMOUNT = re.compile(r'(?<![\d.])[-+−]?(?:\d{1,3}(?:,\d{3})+|\d+)'
                    r'(?:\.\d{2}(?![\d.]|[ \t]*[%％])(?:[ \t]*元)?|(?:\.\d+)?[ \t]*元)')
DATE = re.compile(r'(?<![\d-])(?:\d{4}-\d{2}-\d{2}|\d{1,2}[-/]\d{1,2}|\d{1,2}月\d{1,2}日)(?!\d)')


def iso(value):
    try:
        return type(value) is str and date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def valid_minima(value, projection):
    """Validate the explicit v1 producer contract, not arbitrary numeric trees."""
    if not isinstance(value, dict) or type(value.get('contract_version')) is not int or value['contract_version'] != 1:
        return False
    required = {'scope': 'COMBINED_ACCOUNTS', 'currency': 'CNY',
                'date_basis': 'BUSINESS_DATE_UTC_PLUS_08',
                'selection_rule': 'FIRST_MATCHING_DAILY_DATE_ELSE_CURRENT_SNAPSHOT'}
    if any(value.get(key) != expected for key, expected in required.items()):
        return False
    start, end = value.get('window_start'), value.get('window_end')
    if (not iso(start) or not iso(end) or start > end
            or start != projection.get('as_of_date') or end != projection.get('through_date')):
        return False
    for scenario, income in SCENARIOS.items():
        point = value.get(scenario)
        if (not isinstance(point, dict) or type(point.get('minimum_balance_minor')) is not int
                or not iso(point.get('minimum_balance_date'))
                or not start <= point['minimum_balance_date'] <= end
                or point.get('minimum_point') not in {'DAILY_PROJECTION', 'CURRENT_SNAPSHOT'}
                or point.get('future_income') != income):
            return False
    return True


def forecast_sources(trace):
    """Select direct results and latest typed task snapshots; never plan history."""
    sources, tasks = [], {}
    task_id = lambda value: isinstance(value, str) and bool(re.fullmatch(r'[a-f0-9]{32}', value))
    for record in trace:
        if not isinstance(record, dict) or record.get('kind') != 'response':
            continue
        response = record.get('response')
        response = response if isinstance(response, dict) else {}
        data = response.get('data')
        if record.get('tool') in {'forecast', 'event_context'} and record.get('ok') is True:
            projection = data.get('forecast') if record['tool'] == 'event_context' and isinstance(data, dict) else data
            path = 'data.forecast' if record['tool'] == 'event_context' else 'data'
            sources.append((response, projection, path))
        elif record.get('tool') in {'task_status', 'submit_task_plan', 'replan_task'}:
            task = data.get('task') if isinstance(data, dict) else None
            identifier = task.get('task_id') if isinstance(task, dict) else None
            arguments = record.get('args')
            requested = arguments.get('task_id') if isinstance(arguments, dict) else None
            identities = {value for value in (identifier, requested) if task_id(value)}
            usable = (record.get('ok') is True and response.get('scope', 'bank') == 'bank'
                      and task_id(identifier) and (requested is None or requested == identifier))
            for key in identities:
                tasks[key] = task if usable and key == identifier else None
    for identifier, task in tasks.items():
        if (not isinstance(task, dict) or task.get('scope') != 'bank' or task.get('subject_id') != 'U1'
                or type(task.get('plan_version')) is not int or task['plan_version'] < 1):
            continue
        nodes = task.get('nodes')
        if not isinstance(nodes, list) or not nodes or any(not isinstance(node, dict) for node in nodes):
            continue
        ids = [node.get('id') for node in nodes]
        if any(not isinstance(value, str) or not value for value in ids) or len(set(ids)) != len(ids):
            continue
        for node in nodes:
            if (node.get('tool') != 'forecast' or node.get('state') != 'SUCCEEDED'
                    or type(node.get('attempt')) is not int or node['attempt'] < 1
                    or node.get('operation_id') or node.get('error') is not None):
                continue
            key = node.get('execution_key')
            execution = re.fullmatch(r'task:' + identifier + r':v([1-9][0-9]*):' + re.escape(node['id']), key) if isinstance(key, str) else None
            if not execution or int(execution[1]) > task['plan_version']:
                continue
            result = node.get('result')
            if isinstance(result, dict):
                sources.append((result, result.get('data'), 'data.task.nodes[' + node['id'] + '].result.data'))
    return sources


def forecast_snapshots(trace):
    """Validate selected producer snapshots before any numeric comparison."""
    snapshots, legacy, invalid = {}, False, False
    for response, projection, path in forecast_sources(trace):
        if (response.get('scope', 'bank') != 'bank' or response.get('currency') != 'CNY'
                or response.get('amount_unit') != 'minor'
                or not isinstance(response.get('evidence_id'), str) or not response['evidence_id'].strip()):
            continue
        if not isinstance(projection, dict) or projection.get('scope') != 'COMBINED_ACCOUNTS':
            continue
        if 'scenario_minima' not in projection:
            legacy = legacy or (iso(projection.get('as_of_date')) and iso(projection.get('through_date'))
                                and type(projection.get('lowest_balance_minor')) is int
                                and iso(projection.get('lowest_balance_date')))
            continue
        value = projection['scenario_minima']
        if not valid_minima(value, projection):
            invalid = True
            continue
        key = json.dumps(value, sort_keys=True, ensure_ascii=False)
        snapshots[key] = {'minima': value, 'evidence_id': response['evidence_id'], 'path': path + '.scenario_minima'}
    return list(snapshots.values()), legacy, invalid


def displayed_date(token, start, end):
    """A short date is usable only when its year is unique within the window."""
    if iso(token):
        return token
    parts = re.fullmatch(r'(\d{1,2})[-/月](\d{1,2})日?', token)
    if not parts:
        return None
    candidates = []
    for year in range(date.fromisoformat(start).year, date.fromisoformat(end).year + 1):
        try:
            value = date(year, int(parts[1]), int(parts[2])).isoformat()
            if start <= value <= end:
                candidates.append(value)
        except ValueError:
            pass
    return candidates[0] if len(candidates) == 1 else None


def claim_scope(paragraph, position):
    markers = list(SCOPE.finditer(paragraph[:position]))
    if not markers:
        return None
    if len(markers) > 1:
        bridge = paragraph[markers[-2].end():markers[-1].start()].strip(' *：:')
        if re.fullmatch(r'(?:和|与|及|、|/|或)', bridge):
            return None
    return 'PRIMARY' if markers[-1].group('primary') else 'WITHOUT_FUTURE_INCOME'


def minimum_claims(text):
    """Paragraph scope survives sentence breaks, but never a blank paragraph."""
    for paragraph in re.split(r'\n[ \t]*\n', text.replace('*', '').replace('`', '')):
        for match in MINIMUM.finditer(paragraph):
            start = max([paragraph.rfind(mark, 0, match.start()) for mark in '。；;！？?\n'] + [-1]) + 1
            ends = [paragraph.find(mark, match.end()) for mark in '。；;！？?\n']
            end = min([value for value in ends if value >= 0] + [len(paragraph)])
            fragment = paragraph[start:end]
            prefix = paragraph[start:match.start()]
            # Questions and explicit denial are not positive minima assertions.
            local_prefix = re.split(r'[，,：:]', prefix)[-1]
            quoted = any(paragraph.rfind(left, start, match.start()) > paragraph.rfind(right, start, match.start())
                         and paragraph.find(right, match.end(), end) >= 0 for left, right in (('“', '”'), ('「', '」')))
            predicate_tail = paragraph[match.end():end]
            scenario = claim_scope(paragraph, match.start())
            hypothetical = not scenario and re.search(r'如果|假设|若', local_prefix)
            if (quoted or hypothetical or paragraph[end:end+1] in {'?', '？'}
                    or re.search(r'是否|多少|尚未确定|不能确定|并非|不是', local_prefix)
                    or re.match(r'[ \t]*(?:并非|不是|是否)', predicate_tail)):
                yield {'status': 'NOT_COVERED_NONASSERTION', 'clause': fragment}
                continue
            tail = paragraph[match.end():end]
            next_scope = SCOPE.search(tail)
            next_min = MINIMUM.search(tail)
            limit = min([m.start() for m in (next_scope, next_min) if m] + [len(tail)])
            amount = AMOUNT.search(tail[:limit])
            if not amount:
                yield {'status': 'NOT_COVERED_AMOUNT_SYNTAX', 'clause': fragment}
                continue
            raw = amount[0].replace('元', '').strip().replace(',', '').replace('−', '-')
            minor = Decimal(raw) * 100
            if minor != minor.to_integral_value():
                yield {'status': 'NOT_COVERED_AMOUNT_PRECISION', 'clause': fragment}
                continue
            # Keep only this predicate's text; other scenario comparisons are separate.
            local = prefix + paragraph[match.start():match.end()] + tail[:limit]
            dates = [m[0] for m in DATE.finditer(local)]
            relative = bool(re.search(r'当日|该日', tail[:limit]))
            if relative and not dates:
                earlier = list(DATE.finditer(paragraph[:match.start()]))
                dates = [earlier[-1][0]] if earlier else []
            yield {'status': 'CLAIM', 'clause': fragment, 'scenario': scenario,
                   'amount_minor': int(minor), 'date_tokens': list(dict.fromkeys(dates)),
                   'relative_date_unresolved': relative and not dates,
                   'account_scoped': bool(re.search(r'(?:A|B|日常|储备)账户', local))}


def forecast_minimum_checks(text, trace):
    """Return separate coverage diagnostics and narrow source-bound violations."""
    snapshots, legacy, invalid = forecast_snapshots(trace)
    checks, reasons = [], []
    for claim in minimum_claims(text):
        check = dict(claim)
        if claim['status'] != 'CLAIM':
            checks.append(check)
            continue
        if len(snapshots) > 1:
            check['status'] = 'NOT_COVERED_AMBIGUOUS_FORECAST'
        elif not snapshots:
            check['status'] = 'NOT_COVERED_LEGACY' if legacy and not invalid else 'REJECTED_NO_TYPED_SOURCE'
        elif claim['account_scoped']:
            check['status'] = 'NOT_COVERED_ACCOUNT_SCOPE'
        elif not claim['scenario']:
            check['status'] = 'NOT_COVERED_SCENARIO'
        else:
            source = snapshots[0]
            minima = source['minima']
            expected = minima[claim['scenario']]
            check.update(evidence_id=source['evidence_id'], source_path=source['path'] + '.' + claim['scenario'],
                         expected_amount_minor=expected['minimum_balance_minor'],
                         expected_date=expected['minimum_balance_date'])
            if claim['amount_minor'] != expected['minimum_balance_minor']:
                check['status'] = 'REJECTED_AMOUNT_MISMATCH'
            elif claim['relative_date_unresolved'] or len(claim['date_tokens']) > 1:
                check['status'] = 'NOT_COVERED_DATE_REFERENCE'
            elif claim['date_tokens']:
                value = displayed_date(claim['date_tokens'][0], minima['window_start'], minima['window_end'])
                check['displayed_date'] = value
                check['status'] = ('VERIFIED' if value == expected['minimum_balance_date'] else 'REJECTED_DATE_MISMATCH')
            else:
                check['status'] = 'VERIFIED_AMOUNT_ONLY'
        checks.append(check)
        if check['status'].startswith('REJECTED'):
            reasons.append({'code': 'FORECAST_MINIMUM_EVIDENCE_MISMATCH', 'clause': check['clause'],
                            'detail': check['status'], 'scenario': check.get('scenario'),
                            'source_path': check.get('source_path')})
    return {'reasons': reasons, 'checks': checks}
