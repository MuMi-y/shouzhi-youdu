"""Model selects report scope/sections; host binds the complete source report."""
import json
from .answer import paired_records, query_key, validate as validate_facts, render_fact, render_calculations
from .billing_render import SECTIONS, render, covered_fact
from .facts import SUPPORTED_TOOLS, snapshot_day

FORMAT = 'bank_billing_v1'


def requested(result):
    try:
        return json.loads(result.get('text', '')).get('format') == FORMAT
    except (ValueError, AttributeError):
        return False


def required(result):
    rows = [r for r in result.get('trace', []) if isinstance(r, dict) and r.get('kind') == 'response']
    if not rows or any(r.get('tool') not in SUPPORTED_TOOLS | {'money_calculate'} for r in rows):
        return False
    bound = any('input_provenance' in r.get('response', {}).get('data', {}).get('calculation', {}) for r in rows)
    return not bound and any(r.get('tool') == 'billing_report' and r.get('ok') is True for r in rows)


def validate(result, prompt):
    try:
        answer = json.loads(result['text'])
        if set(answer) != {'format', 'report_evidence_id', 'sections', 'fact_ids', 'calculation_ids'} or answer['format'] != FORMAT:
            raise ValueError('INVALID_BILLING_FIELDS')
        sections = answer['sections']
        if (type(sections) is not list or not 1 <= len(sections) <= len(SECTIONS)
                or not all(type(s) is str and s in SECTIONS for s in sections) or len(sections) != len(set(sections))):
            raise ValueError('INVALID_BILLING_SECTIONS')
        records = paired_records(result.get('trace', []))
        if any(r['tool'] not in SUPPORTED_TOOLS | {'money_calculate'} for r in records):
            raise ValueError('UNSUPPORTED_MIXED_FLOW')
        latest = {}
        for row in records:
            if row['tool'] == 'billing_report':
                latest[query_key(row['tool'], row['args'])] = row
        chosen = [r for r in latest.values() if r['response']['evidence_id'] == answer['report_evidence_id']]
        if len(chosen) != 1:
            raise ValueError('STALE_OR_UNKNOWN_REPORT')
        row = chosen[0]
        basis = {'facts': [], 'calculations': []}
        extra = ''
        for key in ('fact_ids', 'calculation_ids'):
            if type(answer[key]) is not list:
                raise ValueError('INVALID_REFERENCE_LIST')
        if answer['fact_ids'] or answer['calculation_ids']:
            selected = validate_facts({**result, 'text': json.dumps({'format': 'bank_facts_v1',
                'fact_ids': answer['fact_ids'], 'calculation_ids': answer['calculation_ids']})}, prompt)
            if not selected.get('ok'):
                raise ValueError('INVALID_ADDITIONAL_FACTS')
            basis = selected['bank_facts_basis']
            report = row['response']['data']['report']
            extra_lines = [render_fact(fact) for fact in basis['facts']
                           if not covered_fact(fact, report, row['response']['evidence_id'], sections)]
            extra_lines += render_calculations(basis['calculations'])
            extra = '\n\n' + '\n\n'.join(extra_lines) if extra_lines else ''
        body = row['response']
        basis['snapshots'] = [{'tool': 'billing_report', 'arguments': row['args'],
                               'evidence_id': body['evidence_id'], 'business_date': snapshot_day(body)}]
        return {**result, 'text': render(body['data']['report'], sections) + extra,
                'bank_billing_answer': answer, 'bank_facts_basis': basis,
                'answer_validation': {'version': FORMAT, 'scope': 'bank_billing', 'status': 'ACCEPTED',
                    'gate_applied': True, 'limitation': '报告字段与期间绑定；AI选择是否充分解决问题仍须产品验收。'}}
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        return {**result, 'ok': False, 'model_ok': True, 'session_id': None,
                'error': '账单回答未能绑定本轮报告或期间，未作为核验结果展示。',
                'answer_validation': {'version': FORMAT, 'scope': 'bank_billing', 'status': 'REJECTED',
                                      'gate_applied': True, 'reason': str(error)}}
