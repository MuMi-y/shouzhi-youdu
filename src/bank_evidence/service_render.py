"""Finite operation/event views. Model prose cannot relabel these bank facts."""
from src.bank_tasks.progress import actionable, success_receipt
from src.bank_v1.events import facts
from .answer import display_value


def money(value):
    if type(value) is not int:
        raise ValueError('INVALID_BANK_AMOUNT')
    return display_value({'unit': 'minor', 'value': value})


def operation_view(body, *, confirmation_allowed=True):
    op = body['data']['operation']
    identifier, kind = op['operation_id'], op['kind']
    params = op['summary']['params']
    labels = {'transfer': '本人账户转账', 'arrangement': '用钱安排',
              'arrangement_match': '安排履行关联', 'mandate_query': '银行代扣登记查询',
              'mandate_cancel': '银行代扣授权撤销'}
    lines = [labels[kind] + '（操作 ' + identifier + '）。']
    if kind == 'transfer':
        lines.append(f"转出账户 {params['source_account']} → 转入账户 {params['target_account']}，金额 {money(params['amount_minor'])}。")
        if 'reserve_minor' in params:
            lines.append(f"本次条件：转账后转出账户至少保留 {money(params['reserve_minor'])}；确认时银行会重新检查。")
    elif kind == 'arrangement':
        flow = {'income': '预计收入', 'expense': '预计支出', 'goal': '目标用钱'}[params['kind']]
        lines.append(f"安排“{params['title']}”：{flow} {money(params['amount_minor'])}，原定 {params['due_date']}。保存安排不等于实际收付款。")
    elif kind == 'mandate_cancel':
        lines.append('目标银行授权：' + params['mandate_id'] + '。')
        snapshot = op['summary'].get('mandate_snapshot', {})
        if snapshot.get('name'):
            lines.append('商户登记名称：' + snapshot['name'] + '。')
    receipt = op.get('receipt') or {}
    if op['status'] == 'SUCCEEDED':
        if not success_receipt(op) or receipt.get('kind') != kind:
            raise ValueError('INVALID_OPERATION_RECEIPT')
        lines.append('银行操作已成功。原回执 ' + receipt['receipt_id'] + '，完成时间 ' + receipt['committed_at'] + '。')
        if kind == 'transfer':
            if any(receipt.get(key) != params[key] for key in ('source_account', 'target_account', 'amount_minor')):
                raise ValueError('RECEIPT_PARAMETERS_MISMATCH')
            lines.append('这是本人账户之间调拨，不会增加合计资金。不要重复办理。')
        elif kind == 'mandate_query':
            lines.append('以下为该查询回执当时的登记清单，不保证授权现在仍有效：')
            for mandate in receipt['mandates']:
                status = {'ACTIVE': '当时有效', 'CANCELLED': '当时已撤销'}[mandate['status']]
                lines.append(f"- {mandate['name']}：{mandate['mandate_id']}，{status}，登记金额 {money(mandate['amount_minor'])}。")
            if not receipt['mandates']:
                lines.append('该次查询清单为空。')
        elif kind == 'mandate_cancel':
            if receipt.get('mandate_id') != params['mandate_id'] or receipt.get('mandate_status') != 'CANCELLED':
                raise ValueError('CANCEL_RECEIPT_MISMATCH')
            lines.append('该笔银行代扣授权已撤销。')
    elif op['status'] == 'REJECTED':
        lines.append('该操作已被拒绝，未办理。' + ('拒绝时间 ' + op['rejected_at'] + '。' if op.get('rejected_at') else '没有可核实的拒绝时间。'))
    elif op['status'] == 'PENDING':
        if actionable(op, body['observed_at']):
            guidance = ('请在银行待确认区核对完整参数，由本人确认或拒绝；' if confirmation_allowed else
                        '本轮请求条件未通过核对，暂不要确认；请核对或拒绝原待办。有效期至 ')
            if confirmation_allowed:
                guidance += '有效期至 '
            lines.append('已准备待确认，尚未办理。' + guidance + op['expires_at'] + '。')
            lines.append('需独立口令和认证器强验证。' if op['required_verification'] == 'STRONG' else '此操作须本人明确确认；这一步不等于双因素验证。')
        else:
            lines.append('原确认已过期，尚未办理；先核对原操作，再决定是否重新准备。')
    else:
        lines.append('未取得可核实的成功状态，请先核对原操作，不要重复提交。')
    if kind == 'mandate_cancel':
        lines.append('撤销银行代扣授权不等于解除商户合同、取消会员或免除已有债务。')
    return '\n'.join(lines)


def event_view(body):
    data = body['data']
    current = facts(data['forecast'])
    if data['facts'] != current:
        raise ValueError('EVENT_FACTS_MISMATCH')
    transaction = data.get('transaction')
    prefix = []
    if transaction:
        kind = {'expense': '消费', 'income': '收入', 'refund': '退款'}.get(transaction['kind'], '交易')
        prefix.append(f"本次{kind}已记账：账户 {transaction['account_id']}，金额方向 {money(transaction['amount_minor'])}，记账日 {transaction['business_date']}。分析进度不影响已经完成的记账，不要重复办理。")
    return '\n'.join(prefix + [current['summary'], '下一步：' + ' '.join(current['next_steps']),
                      '仅覆盖当前虚构银行账户和已确认安排；未知收支或生活计划仍未包含。'])


def snapshot_state(tool, body):
    """Ignore read time, but a confirmation becoming expired is a real change."""
    from .insight_parts import INSIGHT_TOOLS, snapshot_state as insight_state
    if tool in INSIGHT_TOOLS:
        return insight_state(tool, body)
    if tool == 'forecast':
        return {'data': body['data'], 'business_date': body['business_date']}
    if tool == 'operation':
        op = body['data']['operation']
        return {'data': body['data'], 'actionable': actionable(op, body['observed_at'])}
    if tool == 'task_status':
        data = dict(body['data']['task_progress']['data'])
        observed = data.pop('observed_at')
        return {'data': data, 'actionable': {k: actionable(v, observed) for k, v in data['operations'].items()}}
    if tool == 'event_context':
        # Event delivery metadata changes independently; retain the complete
        # forecast and transaction/arrangement meaning used by the answer.
        data = body['data']
        return {key: data.get(key) for key in ('transaction', 'forecast', 'facts', 'arrangements')}
    raise ValueError('UNSUPPORTED_SERVICE_SNAPSHOT')
