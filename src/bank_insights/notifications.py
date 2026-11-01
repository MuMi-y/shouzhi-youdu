"""One attention item per observed candidate occurrence; retain each reason."""
from .rules import digest


def group_notifications(notifications, candidates):
    """Project active records only, without altering their IDs or audit history."""
    by_id = {item['candidate_id']: item for item in candidates}
    groups = {}
    for notice in notifications:
        if notice['status'] != 'active':
            continue
        key = (notice['candidate_id'], notice['currency'],
               notice['due_date'], notice['amount_minor'])
        if key not in groups:
            candidate = by_id.get(notice['candidate_id'], {})
            groups[key] = {
                'group_id': 'notice_group_' + digest(key)[:24],
                'candidate_id': notice['candidate_id'],
                'label': candidate.get('label') or candidate.get('merchant') or '周期支出',
                'currency': notice['currency'], 'amount_minor': notice['amount_minor'],
                'amount': notice['amount'], 'due_date': notice['due_date'],
                'reasons': [], 'evidence_refs': [],
            }
        group = groups[key]
        group['reasons'].append({
            'notification_id': notice['notification_id'], 'kind': notice['kind'],
            'message': notice['message'], 'evidence_refs': list(notice['evidence_refs']),
            'candidate_version': notice['candidate_version'],
        })
        group['evidence_refs'] = sorted(set(group['evidence_refs']) | set(notice['evidence_refs']))
    for group in groups.values():
        group['reasons'].sort(key=lambda reason: (reason['kind'], reason['notification_id']))
    return sorted(groups.values(), key=lambda group: (group['due_date'], group['group_id']))


def groups_with_new_notices(groups, new_notices):
    """Send one complete group if any of its reasons is newly created."""
    new_ids = {notice['notification_id'] for notice in new_notices}
    return [group for group in groups
            if any(reason['notification_id'] in new_ids for reason in group['reasons'])]
