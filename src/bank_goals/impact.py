"""Read-only impact calculations over one fixed target and a verified ledger range."""
from copy import deepcopy
from src.bank_v1 import finance, plan_projection, projection_journal as journal
from src.bank_v1.common import DomainError
from src.bank_v1.storage import connection
from . import impact_basis
from .impact_contract import CONTRACT


def token(basis):
    return {key: basis[key] for key in ('fingerprint', 'bank_instance', 'epoch', 'bank_sequence', 'tail_hash')}


def capture(goals):
    with connection(goals.db_path) as db:
        with connection(goals.bank.db_path) as bank_db:
            return impact_basis.capture(goals, db, bank_db, goals._now())


def stage(snapshot, end, floor, label, *, amount=0, day=None, event=None):
    result = finance.forecast_from_snapshot(snapshot, end, amount, day)
    value = {'label': label, 'balance_minor': snapshot['accounts']['total_balance_minor'],
             'hypothetical_balance_minor': snapshot['accounts']['total_balance_minor'] - amount if amount else None,
             'event': event, 'primary': {}, 'without_income': {}}
    for branch, minimum, date in [('primary', 'lowest_balance_minor', 'lowest_balance_date'),
        ('without_income', 'without_future_income_lowest_balance_minor', 'without_future_income_lowest_balance_date')]:
        value[branch] = {'minimum_minor': result[minimum], 'date': result[date],
                        'cash_shortfall_minor': max(0, -result[minimum]),
                        'reserve_shortfall_minor': max(0, floor - result[minimum])}
    return value, result['income_conditions']


def build(goals, name, args, *, impact_context=None):
    mode = next(mode for mode, cfg in CONTRACT['modes'].items() if cfg['tool'] == name)
    with connection(goals.db_path) as db:
        with connection(goals.bank.db_path) as bank_db:
            basis = impact_basis.capture(goals, db, bank_db, goals._now())
        target, snapshot = basis['target'], basis['snapshot']
        if impact_context and impact_context['basis'] != token(basis):
            raise DomainError('STALE_GOAL_IMPACT', '请求后目标或银行依据发生变化，请重新查询。')
        if target['revision'] != args['expected_revision']:
            raise DomainError('VERSION_CONFLICT', '目标版本已变化，请重新读取。')
        if not target['arrangement'] or target['arrangement']['status'] != 'active':
            raise DomainError('GOAL_PLAN_UNAVAILABLE', '当前目标尚无已关联的有效银行安排，请先核对目标状态。')
        floor = target['profile']['reserve_floor_minor']
        requested = max(snapshot['business_date'], target['profile']['latest_date'], args.get('due_date', ''))
        coverage = plan_projection.window(snapshot, requested)
        end = coverage['through_date']
        history_range = None
        if mode == 'WHAT_IF':
            if impact_context:
                expected = impact_context['what_if_inputs']
                if (expected['amount_minor'] is None or expected['due_date'] is None or
                        args['amount_minor'] != expected['amount_minor'] or args['due_date'] != expected['due_date']):
                    raise DomainError('IMPACT_INPUT_NOT_BOUND', '拟消费金额或日期无法与本轮原话唯一对应，请明确后只读试算。')
            before, incomes = stage(snapshot, end, floor, 'BEFORE')
            after, _ = stage(snapshot, end, floor, 'PROPOSED', amount=args['amount_minor'], day=args['due_date'])
            stages = [before, after]
        else:
            bound_range = (impact_context or {}).get('history_range')
            if not bound_range:
                raise DomainError('HISTORY_RANGE_CONTEXT_REQUIRED', '当前交流中没有完整的事件前后引用，请明确要比较哪次目标检查后的消费和收入。')
            anchor, baseline = impact_basis.latest(db, basis['fingerprint'])
            start, finish = baseline['bank_sequence'], basis['bank_sequence']
            if (bound_range['anchor_id'] != anchor['baseline_check_id'] or
                    bound_range['start_seq'] != start or bound_range['end_seq'] != finish):
                raise DomainError('HISTORY_RANGE_CONTEXT_MISMATCH', '当前交流引用与历史起点不一致。')
            chain = basis['chain']
            if (start >= finish or start >= len(chain) or chain[start]['record_hash'] != baseline['tail_hash']
                    or baseline['epoch'] != basis['epoch'] or baseline['bank_instance'] != basis['bank_instance']):
                raise DomainError('HISTORY_UNAVAILABLE', '没有可核对的事件前后历史范围。')
            changes = chain[start + 1:finish + 1]
            if len(changes) != 2 or [r['body']['references']['kind'] for r in changes] != ['expense', 'income']:
                raise DomainError('HISTORY_RANGE_AMBIGUOUS', '本轮不止唯一的一笔消费及随后一笔收入，需明确要比较的范围。')
            if any(r['body']['before']['business_date'] != snapshot['business_date'] or
                   r['body']['after']['business_date'] != snapshot['business_date'] for r in changes):
                raise DomainError('HISTORY_UNAVAILABLE', '跨日变化不能全部归因为这两笔收支。')
            before_snapshot = deepcopy(baseline['snapshot'])
            if journal.facts_hash(before_snapshot) != journal.facts_hash(changes[0]['body']['before']):
                raise DomainError('HISTORY_UNAVAILABLE', '历史起点与银行变更不连续。')
            stages = [stage(before_snapshot, end, floor, 'BEFORE')[0]]
            for label, row in zip(CONTRACT['modes'][mode]['stages'][1:], changes):
                value, incomes = stage(row['body']['after'], end, floor, label, event=row['body']['references'])
                stages.append(value)
            history_range = {'episode_id': anchor['episode_id'], 'anchor_id': anchor['baseline_check_id'],
                'start_seq': start, 'end_seq': finish, 'record_hashes': [r['record_hash'] for r in chain[start:finish+1]]}
            history_range['request_reference'] = deepcopy(impact_context)
    for index, value in enumerate(stages):
        value['change_from_previous'] = None if index == 0 else {
            branch: {key: value[branch][key] - stages[index-1][branch][key]
                     for key in ('minimum_minor', 'reserve_shortfall_minor', 'cash_shortfall_minor')}
            for branch in ('primary', 'without_income')}
    data = {'target': target, 'business_date': snapshot['business_date'], 'reserve_floor_minor': floor,
            'stages': stages, 'income_conditions': incomes, 'coverage': coverage,
            'history_range': history_range, 'basis': token(basis), 'calculation_version': 1,
            'date_basis': 'COMMON_BUSINESS_DAY_RECALCULATION'}
    body = {'scope': 'goal_impact', 'contract': 'goal-impact-1', 'mode': mode, 'tool': name,
            'arguments': deepcopy(args), 'data': data}
    body['source_id'] = journal.digest(body)
    # A new transaction/view is essential: the previous read view cannot see concurrent writes.
    if token(capture(goals)) != token(basis):
        raise DomainError('STALE_GOAL_IMPACT', '计算期间目标、安排或银行记录变化，请重新只读查询。')
    return body


def render(body):
    data, mode = body['data'], body['mode']
    target = data['target']; plan = target['arrangement']
    floor = data['reserve_floor_minor']
    lines = [f'目标「{target["profile"]["title"]}」：当前已保存安排为{plan["due_date"]}支出{plan["amount_minor"]/100:.2f}元（v{plan["version"]}）。保存安排不等于付款。',
             f'按{data["business_date"]}的共同口径测算至{data["coverage"]["through_date"]}；目标希望保留{floor/100:.2f}元。保留要求不足与账户跌破零元的现金缺口不同。']
    if target['receipt_relation'] == 'PLAN_CHANGED':
        lines.append('关联回执证明原安排曾保存；目前采用修改后的银行最新版本计算。')
    labels = {'BEFORE': '消费前' if mode == 'POSTED_HISTORY' else '不花这笔钱',
              'PROPOSED': '假设支出后', 'AFTER_EXPENSE': '消费已入账后', 'AFTER_INCOME': '收入已入账后'}
    for s in data['stages']:
        event = s['event']; amount = body['arguments'].get('amount_minor', 0)
        detail = (f'本笔已入账{event["signed_minor"]/100:+.2f}元；' if event else
                  f'拟支出{amount/100:.2f}元，尚未记账；' if s['label'] == 'PROPOSED' else '')
        lines.append(f'{labels[s["label"]]}：{detail}该阶段实际余额{s["balance_minor"]/100:.2f}元。')
        if s['hypothetical_balance_minor'] is not None:
            lines.append(f'  仅扣除此笔拟支出后的假设余额为{s["hypothetical_balance_minor"]/100:.2f}元；实际账户未扣款。')
        for branch, label in [('primary', '计入预计收入'), ('without_income', '不计未来收入')]:
            value = s[branch]
            lines.append(f'  {label}：最低{value["minimum_minor"]/100:.2f}元（{value["date"]}），距保留要求差{value["reserve_shortfall_minor"]/100:.2f}元，零余额现金缺口{value["cash_shortfall_minor"]/100:.2f}元。')
    if mode == 'POSTED_HISTORY':
        for branch, label in [('primary', '计入预计收入'), ('without_income', '不计未来收入')]:
            amounts = ' → '.join(f'{s[branch]["reserve_shortfall_minor"]/100:.2f}' for s in data['stages'])
            lines.append(f'{label}时，保留不足依次为：{amounts}元；分别反映消费与入账的影响。')
    lines.append('预计收入只是条件，不是实际到账；本次已到账收入不会替代尚未确认的未来收入。')
    if data['income_conditions']:
        lines.append('尚待核对的收入条件：' + '；'.join(
            f'{i["title"]}，预计{i["due_date"]}收入{i["amount_minor"]/100:.2f}元' for i in data['income_conditions']))
    if data['coverage']['outside_window']:
        lines.append('以下安排超出一年计算窗口，不能据此认定不受影响：' + '、'.join(a['title'] for a in data['coverage']['outside_window']))
    lines.append('仅查询和计算；没有更改安排、自动付款或保证未来资金充足。')
    return '\n'.join(lines)
