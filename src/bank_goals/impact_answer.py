"""Reference-only answer plus separate historical integrity/currentness checks."""
import json
from copy import deepcopy
from src.bank_v1.projection_journal import digest
from src.bank_v1.common import DomainError
from . import impact
from .impact_contract import FORMAT, CONTRACT, requirement, enforce


def validate(result, prompt='', *, component=False):
    from src.bank_requests.components import records
    try:
        if not component:
            from src.bank_requests.components import MUTATIONS
            if any(r.get('kind') == 'request' and r.get('name') in MUTATIONS for r in result.get('trace', [])):
                raise ValueError('IMPACT_CANNOT_HIDE_WRITE_ATTEMPT')
        answer = json.loads(result['text'])
        if set(answer) != {'format', 'source_id', 'mode'} or answer['format'] != FORMAT:
            raise ValueError('INVALID_IMPACT_REFERENCE')
        cfg = CONTRACT['modes'][answer['mode']]
        rows = [r for r in records(result.get('trace', [])) if r['ok'] and r['status'] == 200 and r['tool'] == cfg['tool']]
        if not rows or rows[-1]['response']['source_id'] != answer['source_id']:
            raise ValueError('NOT_LATEST_IMPACT_SOURCE')
        row, body = rows[-1], rows[-1]['response']
        if (body['scope'] != 'goal_impact' or body['contract'] != 'goal-impact-1'
                or body['tool'] != cfg['tool'] or body['mode'] != answer['mode']
                or row['args'] != body['arguments']
                or digest({k: v for k, v in body.items() if k != 'source_id'}) != body['source_id']
                or not set(CONTRACT['required']) <= body['data'].keys()
                or [s['label'] for s in body['data']['stages']] != cfg['stages']):
            raise ValueError('INVALID_IMPACT_SOURCE')
        enforce(requirement(prompt, body['data']['target']), [{'metadata': {'format': FORMAT, 'body': body}}])
        if requirement(prompt, body['data']['target']) == 'WHAT_IF':
            from .impact_contract import what_if_inputs
            expected = what_if_inputs(prompt, body['data']['business_date'])
            if any(expected[k] != body['arguments'][k] for k in ('amount_minor','due_date')):
                raise ValueError('IMPACT_INPUT_NOT_BOUND')
        return {**result, 'ok': True, 'text': impact.render(body), 'goal_impact_basis': deepcopy(body),
                'answer_validation': {'version': 'goal-impact-1', 'status': 'ACCEPTED', 'gate_applied': True,
                    'source_id': body['source_id'], 'coverage': 'ALL_REQUIRED_STAGES_HOST_RENDERED'}}
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        return {**result, 'ok': False, 'error': '目标影响的工具依据不完整，请重新只读查询。',
                'answer_validation': {'version': 'goal-impact-1', 'status': 'REJECTED', 'reason': str(error)}}


def recheck(result, tools):
    body = result['goal_impact_basis']; data = body['data']
    history_valid = False
    try:
        if tools is None:
            raise ValueError('GOAL_TOOLS_UNAVAILABLE')
        current = impact.capture(tools.goals)
        if body['mode'] == 'POSTED_HISTORY':
            history = data['history_range']; chain = current['chain']
            history_valid = (current['bank_instance'] == data['basis']['bank_instance']
                and current['epoch'] == data['basis']['epoch']
                and [r['record_hash'] for r in chain[history['start_seq']:history['end_seq']+1]] == history['record_hashes'])
            if not history_valid:
                raise ValueError('HISTORY_UNAVAILABLE')
        if impact.token(current) != data['basis']:
            raise ValueError('CURRENT_SECTION_STALE')
        return {**result, 'impact_freshness': {'history_valid': history_valid if body['mode'] == 'POSTED_HISTORY' else None,
                  'current_section_fresh': True, 'status': 'CURRENT_AT_CHECK'}}
    except (DomainError, ValueError, KeyError, TypeError) as error:
        # Do not rewrite the historical range or source_id to appear current.
        return {**result, 'ok': False, 'session_id': None,
            'error': '历史范围仍有依据，但当前部分已变化，请重新查询。' if history_valid else '目标影响依据已变化或无法核对，请重新查询。',
            'impact_freshness': {'history_valid': history_valid, 'current_section_fresh': False,
                                 'status': 'STALE_OR_UNAVAILABLE', 'reason': getattr(error, 'code', str(error))}}
