"""Bounded traversal of calculation provenance, shared by all answer boundaries."""
from .calculation_store import verify_envelope
from src.competition_web.money_calculate import require


MAX_DEPTH = 4


def leaves(calculation, depth=1):
    require(depth <= MAX_DEPTH, '连续计算最多四层，请重新组织计算。', 'CALCULATION_CHAIN_LIMIT')
    provenance = calculation.get('input_provenance', {})
    require(set(provenance) == {'left', 'right'}, '计算来源记录不完整。', 'CALCULATION_INTEGRITY_ERROR')
    result = []
    for source in provenance.values():
        if source.get('kind') == 'calculation_result':
            parent = verify_envelope(source['envelope'])
            require(source.get('evidence_id') == source['envelope']['evidence_id'],
                    '连续计算父编号不一致。', 'CALCULATION_INTEGRITY_ERROR')
            require(parent['result']['kind'] == 'amount' and parent['result']['amount_minor'] == source['value_minor'],
                    '连续计算父金额不一致。', 'CALCULATION_INTEGRITY_ERROR')
            result.extend(leaves(parent, depth + 1))
        else:
            require(source.get('kind') in {'bank_fact', 'user_assumption'}, '未知来源类型。')
            result.append(source)
    return result


def depth(calculation):
    children = [source for source in calculation['input_provenance'].values()
                if source['kind'] == 'calculation_result']
    return 1 + max((depth(source['envelope']['data']['calculation']) for source in children), default=0)
