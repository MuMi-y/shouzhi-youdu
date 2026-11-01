"""Read-only task progress snapshots; bank receipts and task states stay distinct."""
from datetime import datetime
import hashlib

from src.bank_v1.common import canonical


LABELS = {
    'accounts': '查看账户', 'transactions': '查看流水', 'arrangements': '查看安排',
    'forecast': '预测资金', 'spending_summary': '汇总收支', 'billing_report': '分析账单',
    'recurring_candidates': '查周期扣费候选', 'insight_status': '查看关注事项',
    'money_calculate': '计算金额', 'prepare_transfer': '准备转账',
    'prepare_mandate_query': '查询银行代扣登记', 'match_mandate_from_query': '匹配目标授权',
    'prepare_mandate_cancel': '撤销银行代扣授权', 'operation': '核对银行操作回执',
    'propose_arrangement': '保存用钱安排', 'event_context': '核对事件依据',
}
STATES = {'READY', 'WAITING_CONFIRMATION', 'WAITING_VERIFICATION', 'SUCCEEDED',
          'FAILED', 'PAUSED', 'CANCELLED', 'NEEDS_HUMAN'}


def build(task, operations, observed_at):
    """Operations are fresh reads keyed by node operation ID, never model args."""
    if task.get('subject_id') != 'U1' or task.get('scope') != 'bank' or task.get('state') not in STATES:
        raise ValueError('INVALID_TASK_PROGRESS_SCOPE')
    datetime.fromisoformat(observed_at)
    nodes = []
    for node in task['nodes']:
        identifier = node.get('operation_id')
        operation = operations.get(identifier) if identifier else None
        if identifier and (not isinstance(operation, dict) or operation.get('operation_id') != identifier):
            raise ValueError('TASK_OPERATION_NOT_VERIFIED')
        if operation and operation.get('subject_id', 'U1') != 'U1':
            raise ValueError('TASK_OPERATION_WRONG_SUBJECT')
        nodes.append({key: node.get(key) for key in ('id', 'tool', 'state', 'attempt', 'operation_id')}
                     | {'error_code': (node.get('error') or {}).get('code')})
    used = {n['operation_id'] for n in nodes if n['operation_id']}
    if set(operations) != used:
        raise ValueError('UNRELATED_TASK_OPERATION')
    data = {'task_id': task['task_id'], 'plan_version': task['plan_version'],
            'state': task['state'], 'observed_at': observed_at, 'nodes': nodes,
            'operations': operations}
    return {'contract': 'task-progress-1', 'source_id': hashlib.sha256(canonical(data).encode()).hexdigest(),
            'data': data}


def actionable(operation, observed_at):
    if operation.get('status') != 'PENDING' or not operation.get('expires_at'):
        return False
    return datetime.fromisoformat(operation['expires_at']) > datetime.fromisoformat(observed_at)


def success_receipt(operation):
    receipt = operation.get('receipt') or {}
    return (operation.get('status') == 'SUCCEEDED' and receipt.get('status') == 'SUCCEEDED'
            and receipt.get('operation_id') == operation.get('operation_id') and receipt.get('receipt_id'))


def operation_details(operation):
    """Display bank-owned parameters; never derive money or infer a new target."""
    params = operation['summary']['params']
    kind = operation['kind']
    if kind == 'transfer':
        amount = params['amount_minor']
        text = f"账户 {params['source_account']} → {params['target_account']}，金额 {amount // 100}.{amount % 100:02d}元；"
        if 'reserve_minor' in params:
            floor = params['reserve_minor']
            text += f"转出账户至少保留 {floor // 100}.{floor % 100:02d}元，确认时重新检查；"
        return text
    if kind == 'mandate_cancel':
        snapshot = operation['summary'].get('mandate_snapshot', {})
        name = snapshot.get('name')
        return ('商户“' + name + '”，' if name else '') + '银行授权 ' + params['mandate_id'] + '；'
    if kind == 'arrangement':
        amount = params['amount_minor']
        return f"安排“{params['title']}”，金额 {amount // 100}.{amount % 100:02d}元，原定 {params['due_date']}；"
    return ''


def render(progress, *, confirmation_allowed=True):
    data = progress['data']
    lines = ['任务进度（本次读取时间：' + data['observed_at'] + '）。']
    pending, unsynced = [], False
    for index, node in enumerate(data['nodes'], 1):
        label = LABELS.get(node['tool'], '业务核对')
        operation = data['operations'].get(node['operation_id'])
        if operation:
            receipt = success_receipt(operation)
            if receipt:
                status = '银行操作已成功，原回执 ' + receipt
                if node['state'] != 'SUCCEEDED':
                    status += '；任务节点尚待同步核对，不要重复办理'
                    unsynced = True
            elif actionable(operation, data['observed_at']):
                status = '已有待确认操作 ' + operation['operation_id'] + '，尚未办理'
                pending.append(operation)
            elif operation.get('status') == 'PENDING':
                status = '原确认已过期，不能继续使用；需核对后重新规划'
            elif operation.get('status') == 'REJECTED':
                status = '原操作已拒绝，未作为成功办理'
            else:
                status = '原操作未取得可核实的成功回执，请先核对原操作'
        elif node['state'] == 'SUCCEEDED':
            status = '节点已完成；具体业务结论见该节点工具依据'
        elif node['state'] == 'FAILED':
            status = '节点未完成，请核对失败原因并恢复或接管'
        elif node['state'] == 'CANCELLED':
            status = '未执行部分已取消'
        else:
            status = '尚未完成' + ('，尚未生成银行确认卡' if node['tool'].startswith('prepare_') or node['tool'] == 'propose_arrangement' else '')
        detail = operation_details(operation) if operation else ''
        lines.append(f'{index}. {label}：{detail}{status}。')
    state = data['state']
    if (state == 'SUCCEEDED' and all(n['state'] == 'SUCCEEDED' for n in data['nodes'])
            and all(success_receipt(op) for op in data['operations'].values())):
        next_step = '任务节点均已完成，已有银行操作请保留原回执，不要重复办理。'
    elif state == 'SUCCEEDED':
        next_step = '任务记录已收尾，但当前银行回执未能核实，请先核对原操作，不要重复办理。'
    elif state == 'CANCELLED':
        next_step = '任务已取消；此前成功的银行操作不会因此撤回。'
    elif state in {'PAUSED', 'NEEDS_HUMAN'}:
        next_step = '任务已暂停自动推进，请在任务区核对并继续或接管；暂停任务不等于撤销已有银行待办。'
    elif state == 'FAILED':
        next_step = '任务尚未完成，请在任务区核对失败原因和原操作，再恢复或调整未完成步骤；已成功的银行操作不要重做。'
    elif pending:
        next_step = ('请在银行待确认区核对上述操作的完整参数，再由本人确认或拒绝；需要强验证时按页面办理。后续出现新的银行待确认操作时，仍须本人分别确认；只读核对步骤由任务自动推进。'
                     if confirmation_allowed else '本轮请求条件未通过核对，暂不要确认待办；先核对条件，必要时暂停任务或拒绝原操作。')
    elif unsynced:
        next_step = '银行已返回成功回执，等待任务区同步后核对剩余步骤；不要重复提交。'
    else:
        next_step = '当前没有可供确认的银行操作。请等待任务区进度更新；出现待确认卡后再核对，若持续不推进可在任务区接管。'
    lines.append('下一步：' + next_step)
    if any(n['tool'] == 'prepare_mandate_cancel' for n in data['nodes']):
        lines.append('撤销银行代扣授权不等于解除商户合同、取消会员或免除已有债务。')
    return '\n'.join(lines)
