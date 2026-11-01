"""Host display check: don't release model-selected facts after they changed."""
from src.bank_v1.common import canonical
from .facts import catalog, SUPPORTED_TOOLS, snapshot_day
from .calculation_tree import leaves


def recheck(result, read_tool):
    """Read only, bounded by the already selected factual sources.

    This is a freshness rejection, never silent replacement with new figures.
    A new model judgment requires a new explicitly scheduled analysis.
    """
    if not result.get('ok') or not result.get('bank_facts_basis'):
        return result
    try:
        basis = result['bank_facts_basis']
        facts = list(basis.get('facts', []))
        for calculation in basis.get('calculations', []):
            facts.extend(p['fact'] for p in leaves(calculation)
                         if p['kind'] == 'bank_fact')
        snapshots = {}
        for source in basis.get('snapshots', []):
            tool, args = source['tool'], source['arguments']
            if tool not in SUPPORTED_TOOLS:
                raise ValueError('UNSUPPORTED_FACT_SOURCE')
            fresh = read_tool(tool, args)
            if fresh['evidence_id'] != source['evidence_id'] or snapshot_day(fresh) != source['business_date']:
                raise ValueError('SNAPSHOT_CHANGED_BEFORE_DISPLAY')
            key = canonical({'tool': tool, 'args': args})
            snapshots[key] = catalog(tool, args, fresh)['facts']
        for fact in facts:
            tool, args = fact['source_tool'], fact['source_arguments']
            if tool not in SUPPORTED_TOOLS:
                raise ValueError('UNSUPPORTED_FACT_SOURCE')
            key = canonical({'tool': tool, 'args': args})
            if key not in snapshots:
                snapshots[key] = catalog(tool, args, read_tool(tool, args))['facts']
            if fact not in snapshots[key]:
                raise ValueError('FACT_CHANGED_BEFORE_DISPLAY')
        return {**result, 'fact_freshness': {'status': 'CURRENT_AT_DISPLAY_CHECK',
            'actor': 'SYSTEM_READ_ONLY', 'queries': len(snapshots),
            'limitation': '只表示显示前重读一致，不预留资金，也不替代付款时的银行检查。'}}
    except Exception as error:
        return {**result, 'ok': False, 'session_id': None,
                'error': '账本或安排在分析期间发生变化，旧判断未采用，请按最新资料重新分析。',
                'fact_freshness': {'status': 'STALE_OR_UNAVAILABLE', 'exception_type': type(error).__name__},
                'answer_validation': {**result.get('answer_validation', {}), 'status': 'REJECTED',
                                      'freshness': 'STALE_OR_UNAVAILABLE'}}
