"""Evidence-bound questions and historical bank attempts, without write replay."""
from datetime import datetime

from src.bank_v1.common import canonical, evidence
from .service_render import snapshot_state


BANK_WRITES = frozenset({'prepare_transfer', 'prepare_mandate_query',
                         'prepare_mandate_cancel', 'propose_arrangement'})
QUESTIONS = {
    'prepare_transfer': {
        'source_account': '您希望从哪个账户转出？',
        'target_account': '您希望转入哪个账户？',
        'amount_minor': '您希望转多少元？明确金额后才能准备待确认操作，实际办理还需要您单独确认。',
    },
    'billing_report': {
        'start_date': '账单从哪一天开始统计？',
        'end_date': '账单统计到哪一天？',
    },
}
ATTEMPT_LABELS = {
    'prepare_transfer': '准备转账', 'prepare_mandate_query': '准备查询代扣授权',
    'prepare_mandate_cancel': '准备撤销代扣授权', 'propose_arrangement': '准备资金安排',
}
# Audited in operations.prepare: these errors occur before INSERT, and cannot
# be returned for an existing idempotency key's successfully prepared operation.
PREPARATION_REFUSALS = {
    ('prepare_transfer', 'INSUFFICIENT_FUNDS'): '转出账户余额不足',
    ('prepare_transfer', 'RESERVE_SHORTFALL'): '转账后无法满足要求保留的账户余额',
    ('prepare_transfer', 'INVALID_ACCOUNT'): '转出和转入账户相同',
    ('prepare_mandate_cancel', 'ALREADY_CANCELLED'): '该代扣授权已经撤销',
    ('prepare_mandate_cancel', 'VERSION_CONFLICT'): '授权版本已变化',
    ('propose_arrangement', 'VERSION_CONFLICT'): '安排版本已变化',
    ('propose_arrangement', 'ALREADY_MATCHED'): '安排已关联流水，不能重新激活',
}


def failed_write(row):
    return (row.get('kind') == 'response' and row.get('tool') in BANK_WRITES
            and not (row.get('ok') is True and row.get('status') == 200))


def error_code(row):
    value = (row.get('response') or {}).get('error', {})
    return value['code'] if isinstance(value, dict) and type(value.get('code')) is str else 'UNKNOWN'


def outcome_key(row):
    return canonical({'tool': row['tool'], 'arguments': row['args'], 'error_code': error_code(row)})


def outcome_part(part, records):
    if (set(part) != {'format', 'tool', 'arguments', 'error_code'} or
            part['tool'] not in BANK_WRITES or type(part['arguments']) is not dict or
            type(part['error_code']) is not str):
        raise ValueError('INVALID_TOOL_OUTCOME')
    identity = canonical({key: part[key] for key in ('tool', 'arguments', 'error_code')})
    matches = [row for row in records if failed_write(row) and outcome_key(row) == identity]
    if not matches:
        raise ValueError('UNBACKED_TOOL_OUTCOME')
    row = matches[-1]
    reason = PREPARATION_REFUSALS.get((part['tool'], part['error_code']))
    prefix = f"本轮{ATTEMPT_LABELS[part['tool']]}有一次失败尝试："
    if row.get('status') == 409 and reason:
        text = (prefix + reason + '，银行拒绝了这次请求，未由这次尝试生成新的待确认操作。'
                '这是该次尝试的结果；其他已存在或随后准备的操作仍以各自状态与回执为准。'
                '请核对请求条件后再决定是否继续。')
    elif row.get('status') == 409 and part['error_code'] == 'IDEMPOTENCY_CONFLICT':
        text = (prefix + '相同请求编号使用了不同参数，银行拒绝此次变更。'
                '原请求可能已有操作，请先核对原请求和回执，不要换一个请求编号重复办理。')
    else:
        text = (prefix + '未取得可确认的准备结果，不能据此认定已办理，也不能认定未产生操作。'
                '请先核对原请求、银行待办及原操作回执；不要换一个请求编号重复办理。')
    return {'text': text, 'outcome_key': identity}


def guidance_part(part, records):
    if part.get('kind') == 'request_parameters':
        if set(part) != {'format', 'kind', 'tool', 'fields'}:
            raise ValueError('INVALID_GUIDANCE_FIELDS')
        questions, fields = QUESTIONS.get(part['tool'], {}), part['fields']
        if (type(fields) is not list or not fields or len(fields) > len(questions) or
                any(type(field) is not str or field not in questions for field in fields) or
                len(set(fields)) != len(fields)):
            raise ValueError('INVALID_REQUESTED_PARAMETERS')
        return {'text': '\n'.join(questions[field] for field in fields), 'snapshots': []}
    if (part.get('kind') != 'transfer_assessment' or
            set(part) != {'format', 'kind', 'forecast_evidence_id'}):
        raise ValueError('INVALID_GUIDANCE_KIND')
    latest = {}
    for row in records:
        if row['tool'] == 'forecast' and row.get('ok') is True and row.get('status') == 200:
            latest[canonical(row['args'])] = row
    matches = [row for row in latest.values() if row['response']['evidence_id'] == part['forecast_evidence_id']]
    if not matches:
        raise ValueError('NOT_LATEST_GUIDANCE_FORECAST')
    row, body = matches[-1], matches[-1]['response']
    if body['evidence_id'] != evidence(body['data'], datetime.fromisoformat(body['observed_at']))['evidence_id']:
        raise ValueError('INVALID_GUIDANCE_SOURCE_HASH')
    if (body['data']['scope'] != 'COMBINED_ACCOUNTS' or
            body['data']['account_allocation']['status'] != 'NOT_MODELED'):
        raise ValueError('UNSUPPORTED_TRANSFER_ASSESSMENT')
    return {
        'text': ('现在还不能仅凭合计预测判断应该挪多少钱。该预测没有按付款账户分配未来收支，'
                 '也没有给出单账户可转额度；合计有余款不代表转出账户的还款资金充足。'
                 '本人账户间调拨只改变资金分布，不会增加合计资金。\n'
                 '请先明确：近期从哪个账户还款或付款、希望在该账户保留多少资金，以及您考虑调拨的金额。'
                 '本段只是判断边界与询问，不构成办理或授权。'),
        'snapshots': [{'tool': 'forecast', 'arguments': row['args'],
                       'state': snapshot_state('forecast', body)}],
    }
