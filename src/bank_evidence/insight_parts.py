"""Bind periodic candidates and notice states to one actual insight snapshot."""
from src.bank_insights.rules import digest, display


INSIGHT_TOOLS = frozenset({'recurring_candidates', 'insight_status', 'check_insights'})
SECTIONS = frozenset({'overview', 'candidates', 'notifications', 'new_notifications'})
CANDIDATE_STATUS = {'proposed': '尚未确认关注', 'confirmed': '已确认关注',
                    'ignored': '已忽略', 'rejected': '已否认该候选'}
NOTICE_STATUS = {'active': '缓存中有效', 'superseded': '已失效的历史提醒'}
NOTICE_KIND = {'RENEWAL_DUE': '临近推算日期', 'PRICE_INCREASE': '周期出账上涨观察',
               'MISSED_EXPECTED_CHARGE': '推算日期后未观察到预期扣费'}


def snapshot_state(tool, body):
    """check_insights writes the cache; its display check must read status only."""
    if tool not in INSIGHT_TOOLS:
        raise ValueError('NOT_INSIGHT_SOURCE')
    data = {key: value for key, value in body['data'].items()
            if key not in {'new_notifications', 'new_notification_groups'}}
    return {'as_of': body['as_of'], 'data': data}


def candidate_text(item):
    if item['currency'] != 'CNY':
        raise ValueError('UNSUPPORTED_INSIGHT_CURRENCY')
    corrections = item['corrections']
    amount_label = '用户更正的关注金额' if 'amount_minor' in corrections else '最近观测出账'
    date_label = '用户更正的关注日期' if 'next_due_date' in corrections else '下一次推算日期'
    period = {'monthly': '近似月度', 'weekly': '近似周度'}[item['period_kind']]
    lines = [f"{item['label']}（账户 {item['account_id']}）：{CANDIDATE_STATUS[item['status']]}。",
             f"{amount_label} {display(item['amount_minor'])}元；前一次观测出账 {display(item['previous_amount_minor'])}元。",
             f"观察日期：{'、'.join(item['observed_dates'])}；{period}间隔 {item['period_days']}天。",
             f"{date_label}：{item['next_due_date']}。"]
    if item['price_increase']:
        lines.append('原始出账观测触发上涨标记，原因待核对，不能自动认定商户涨价。')
        if 'amount_minor' in corrections:
            lines.append('关注金额已有用户更正，不能把该更正值当作最新实际扣费或据此认定涨价。')
    else:
        lines.append('本次缓存观测未触发周期上涨规则；不证明费用合理或未来不会变化。')
    if not item.get('currently_observed', True):
        lines.append('最近扫描已不再观察到此候选，保留的是历史关注记录。')
    lines.append('确认关注仅是提醒偏好，不等于订阅合同、正式代扣授权或银行付款。')
    return '\n'.join(lines)


def notice_text(item, candidates, *, new=False):
    if item['currency'] != 'CNY' or item['candidate_id'] not in candidates:
        raise ValueError('INVALID_NOTICE_CANDIDATE')
    candidate = candidates[item['candidate_id']]
    prefix = '该次检查新增的提醒' if new else '缓存提醒'
    return (f"{prefix}：{candidate['label']}，{NOTICE_KIND[item['kind']]}，{NOTICE_STATUS[item['status']]}。"
            f"该提醒记录的金额 {display(item['amount_minor'])}元，关注日期 {item['due_date']}；"
            '推算日期不证明将实际扣款，提醒不等于银行办理。')


def selected_ids(part, key, available, enabled):
    ids = part[key]
    if (type(ids) is not list or len(ids) > 50 or
            any(type(identity) is not str or identity not in available for identity in ids) or
            len(ids) != len(set(ids)) or (ids and not enabled)):
        raise ValueError('INVALID_INSIGHT_SELECTION')
    return ids


def insight_part(part, records):
    if (set(part) != {'format', 'source_tool', 'evidence_id', 'sections', 'candidate_ids', 'notification_ids'}
            or part['source_tool'] not in INSIGHT_TOOLS):
        raise ValueError('INVALID_INSIGHT_PART')
    sections = part['sections']
    if (type(sections) is not list or not sections or len(sections) > len(SECTIONS) or
            any(type(section) is not str or section not in SECTIONS for section in sections) or
            len(sections) != len(set(sections))):
        raise ValueError('INVALID_INSIGHT_SECTIONS')
    source_tool = part['source_tool']
    rows = [row for row in records if row['tool'] == source_tool and row['args'] == {}]
    if not rows or rows[-1]['response']['evidence_id'] != part['evidence_id']:
        raise ValueError('NOT_LATEST_INSIGHT_SOURCE')
    body = rows[-1]['response']
    if (body.get('scope') != 'bank' or body.get('currency') != 'CNY' or
            body['evidence_id'] != 'insight_' + digest([body['as_of'], body['data']])[:24]):
        raise ValueError('INVALID_INSIGHT_SOURCE_HASH')
    data = body['data']
    if ('new_notifications' in sections and source_tool != 'check_insights' or
            'notifications' in sections and source_tool == 'recurring_candidates'):
        raise ValueError('INSIGHT_SECTION_NOT_QUERIED')
    candidates = {item['candidate_id']: item for item in data['candidates']}
    notices = {item['notification_id']: item for item in data.get('notifications', [])}
    ids = selected_ids(part, 'candidate_ids', candidates, 'candidates' in sections)
    notice_ids = selected_ids(part, 'notification_ids', notices, 'notifications' in sections)
    if ('candidates' in sections and candidates and not ids or
            'notifications' in sections and notices and not notice_ids):
        raise ValueError('EMPTY_INSIGHT_SELECTION')
    lines = ['本次检查已扫描流水并更新关注缓存。' if source_tool == 'check_insights' else
             '以下来自已扫描的关注缓存；本次读取缓存本身没有重新扫描流水。']
    lines.append(f"缓存读取业务日：{body['as_of']}。")
    for section in sections:
        if section == 'overview':
            lines.append(f"缓存中有 {len(candidates)} 个周期关注候选；候选不证明正式合同，空缓存也不证明现实中没有订阅。")
            if 'last_check' in data:
                lines.append('最近扫描记录：' + (data['last_check'] or '尚无扫描时间') + '。')
        elif section == 'candidates':
            lines.extend(candidate_text(candidates[identity]) for identity in ids)
            if not candidates:
                lines.append('缓存中没有周期候选，不能据此认定现实中没有周期支出。')
            elif len(ids) != len(candidates):
                lines.append('以上仅展示所选候选，不是缓存全部项目。')
        elif section == 'notifications':
            lines.extend(notice_text(notices[identity], candidates) for identity in notice_ids)
            lines.append('所读通知历史最多包含最近一百条；本段仅展示所选缓存提醒，不保证覆盖所有当前风险。')
            if not notices:
                lines.append('所读缓存没有提醒记录；这不等于现在没有风险。')
        else:
            lines.extend(notice_text(item, candidates, new=True) for item in data['new_notifications'])
            if not data['new_notifications']:
                lines.append('该次检查没有新增提醒；既有提醒和现实风险仍需分别核对。')
    read_tool = 'insight_status' if source_tool == 'check_insights' else source_tool
    return {'text': '\n\n'.join(lines), 'source_tool': source_tool,
            'snapshots': [{'tool': read_tool, 'arguments': {}, 'state': snapshot_state(source_tool, body)}]}
