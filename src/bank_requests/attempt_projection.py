"""Trusted attempt disclosure and strict trace coverage, separate from model prose."""
import json
from datetime import datetime
from .protocol import AttemptHandle
from src.bank_v1_web.handoff_contracts import digest, safe
from src.bank_v1_web.handoff_read_failure import known as known_read_failure, matches_response


def collect(store, handle, trace):
    from .components import records, MUTATIONS
    from src.bank_evidence.dialogue_parts import outcome_key
    snapshot = store.snapshot(handle.run_id)
    attempts = snapshot['attempts']
    events = snapshot['events']
    trace_valid = True
    try:
        paired = {r['call_id']:r for r in records(trace)}
    except (ValueError,KeyError,TypeError):
        paired, trace_valid = {}, False
    requests = [r for r in trace if isinstance(r,dict) and r.get('kind') == 'request']
    used, failures, complete = set(), [], trace_valid and snapshot['run']['completeness'] not in {'INCOMPLETE','UNKNOWN'}
    for index, request in enumerate(requests):
        name = request.get('name') or request.get('tool')
        row = paired.get(request.get('call_id'))
        args = request.get('arguments', {})
        candidates = [a for a in attempts if a['attempt_id'] not in used and a['entry_name'] == name
                      and a['raw_safe_hash'] == digest(safe(args))]
        if row and row['ok']:
            candidates = [a for a in candidates if any(e['attempt_id']==a['attempt_id'] and
                e['phase'] in {'RETURNED','LATE_RETURN'} and e['data'].get('response_hash')==digest(safe(row['response'])) for e in events)]
        if len(candidates) != 1:
            complete = False
            # Preserve unpaired evidence as an observation, never fabricate dispatch.
            if not candidates and type(args) is dict and type(name) is str:
                try:
                    a = store.begin_attempt(handle,name,args,'MODEL_TRACE')
                    store.append(a,'TRACE_UNPAIRED',{'offset':index,'call_id':request.get('call_id')})
                except Exception:
                    pass
            continue
        attempt = candidates[0]
        used.add(attempt['attempt_id'])
        if not row:
            complete = False
            continue
        if known_read_failure(attempt, events) and not matches_response(row):
            complete = False
            continue
        store.append(AttemptHandle(handle,attempt['attempt_id']),'TRACE_LINK',
                     {'call_id':row['call_id'],'offset':index}, producer_key=attempt['attempt_id']+':TRACE_LINK')
        if not row['ok'] and name in MUTATIONS:
            known = [e for e in events if e['attempt_id']==attempt['attempt_id'] and e['phase']=='REJECTED_BEFORE_DISPATCH']
            code = row['response'].get('error',{}).get('code')
            if known and known[-1]['data'].get('code') == code:
                failures.append(outcome_key(row))
            else:
                complete = False
    if any(a['actor']=='MODEL_GATEWAY' and a['attempt_id'] not in used for a in attempts):
        complete = False
    if any(r.get('denied') for r in trace if isinstance(r,dict)):
        complete = False
    if not complete:
        store.mark_incomplete(handle.run_id)
    return {'complete':complete,'failure_coverage':failures}


def disclose(snapshot, service=None):
    entries = []
    for attempt in snapshot['attempts']:
        phases = [e for e in snapshot['events'] if e['attempt_id']==attempt['attempt_id']]
        names = {e['phase'] for e in phases}
        refs = []
        for event in phases:
            for ref in event['data'].get('refs',[]):
                if ref not in refs:
                    refs.append(ref)
        status = 'UNKNOWN'
        if 'REJECTED_BEFORE_DISPATCH' in names:
            status = 'REJECTED_BEFORE_DISPATCH'
        elif names & {'RETURNED','LATE_RETURN'} or any(e['phase']=='RECOVERED' and e['data'].get('certain') for e in phases):
            status = 'RETURNED'
        elif known_read_failure(attempt, phases):
            status = 'READ_FAILED'
        current = []
        for ref in refs:
            state = 'UNKNOWN'
            if service and ref['kind'] == 'operation':
                try:
                    operation = service.call_tool('operation',{'operation_id':ref['id']})['data']['operation']
                    state = operation['status']
                    if state=='PENDING' and operation.get('expires_at') and datetime.fromisoformat(operation['expires_at']) <= service.clock():
                        state='EXPIRED'
                except Exception:
                    pass
            elif service and ref['kind']=='task':
                try:state=service.tasks.get(ref['id'])['state']
                except Exception:pass
            elif service and getattr(service,'goals',None) and ref['kind'] in {'goal_draft','choice'}:
                try:
                    from src.bank_v1_web.handoff_reconcile import read
                    if ref['kind']=='goal_draft':
                        row=read(service.goals.db_path,'SELECT draft_id FROM goal_drafts WHERE draft_id=?',(ref['id'],))
                        if row:state='UNCONFIRMED_DRAFT'
                    else:
                        row=read(service.goals.db_path,'SELECT state FROM choices WHERE choice_id=?',(ref['id'],))
                        if row:state=row['state']
                except Exception:pass
            current.append({**ref,'state':state})
        if attempt['effect_class'] == 'READ_ONLY' and status == 'RETURNED' and 'DOMAIN_ROLLED_BACK' not in names:
            continue
        descriptions = {'REJECTED_BEFORE_DISPATCH':'这次请求在业务调用前被拒绝，没有执行该次操作。',
                        'READ_FAILED':'未找到已确认的目标，本次目标查询失败；这次查询没有修改目标或银行安排。',
                        'UNKNOWN':'这次调用结果尚未确认。请核对原记录，不要重复办理。',
                        'RETURNED':'已记录这次工具调用的返回结果。'}
        labels = {'PENDING':'待本人确认','SUCCEEDED':'已完成','EXPIRED':'已过期','REJECTED':'已拒绝','UNKNOWN':'暂无法核对',
                  'READY':'等待后续步骤','WAITING_CONFIRMATION':'待本人确认','WAITING_VERIFICATION':'待本人核验',
                  'FAILED':'未完成，需核对','PAUSED':'已暂停','CANCELLED':'已取消','NEEDS_HUMAN':'需本人处理',
                  'UNCONFIRMED_DRAFT':'未确认草案（尚未修改目标）','SELECTED':'已选择方案（尚未办理）'}
        text = descriptions[status]
        if 'DOMAIN_ROLLED_BACK' in names:
            text += ' 任务状态未能保存；这不表示银行操作已撤回。'
        if current:
            text += ' 原记录：'+'；'.join(labels.get(r['state'],r['state'])+'（'+r['id']+'）' for r in current)
        entries.append(dict(attempt_id=attempt['attempt_id'],tool=attempt['entry_name'],status=status,objects=current,text=text))
    return dict(run_id=snapshot['run']['run_id'],coverage_seq=max((e['sequence'] for e in snapshot['events']),default=0),
                complete=snapshot['run']['completeness'] == 'COMPLETE',entries=entries)
