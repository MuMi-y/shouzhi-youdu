"""Read-only original-key checks for existing task/goal domain records."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from .handoff_contracts import digest, canonical, parameter_view
from src.bank_requests.protocol import RecoveryHandle


def file_identity(path):
    path=Path(path).resolve();stat=path.stat()
    return {'path':str(path),'file_id':[stat.st_dev,stat.st_ino]}


def bind(store,service):
    paths={'tasks':service.tasks.path}
    if service.goals:paths['goals']=service.goals.db_path
    current={key:file_identity(value) for key,value in paths.items()}
    with store.jobs.connection() as db:
        previous=store._meta(db,'domain_files') or {}
        if any(key in previous and previous[key]!=value for key,value in current.items()):
            raise ValueError('DOMAIN_INSTANCE_CHANGED')
        store._set_meta(db,'domain_files',{**previous,**current})


def read(path,sql,args):
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row
        row=db.execute(sql,args).fetchone()
        return dict(row) if row else None


class RecoveryMismatch(ValueError):
    pass


def _prove(condition, outcome='UNPROVEN'):
    if not condition:
        raise RecoveryMismatch(outcome)


def _task_prepare_proof(store, service, task, node, expected_args):
    """Prove three parameter layers and original provenance; no business writes."""
    _prove(task.get('subject_id')=='U1' and task.get('scope')=='bank')
    contract=store.task_origin(task,node)
    origin=task['node_origins'][node['id']]
    version=origin['plan_version']
    _prove(type(version) is int and 1<=version<=task['plan_version'])
    _prove(node['execution_key']==f"task:{task['task_id']}:v{version}:{node['id']}")
    _prove(expected_args.get('idempotency_key')==node['execution_key'])
    with store.jobs.connection() as db:
        identities=store._meta(db,'domain_files') or {}
        bound_bank=store._meta(db,'bank_identity')
    _prove(identities.get('tasks')==file_identity(service.tasks.path),'CONFLICT')
    bank_identity=store.bank_identity(service.db_path)
    _prove(bank_identity==bound_bank,'CONFLICT')
    candidates=[]
    for snapshot in store.task_dispatches(task,node):
        for attempt in snapshot['attempts']:
            events=[e for e in snapshot['events'] if e['attempt_id']==attempt['attempt_id']]
            if not any(e['phase']=='DISPATCHED' for e in events):
                continue
            if any(e['phase']=='RAISED' and e['data'].get('bank_no_operation') is True for e in events):
                continue
            candidates.append((snapshot,attempt,events))
    _prove(bool(candidates))
    _prove(len(candidates)==1,'AMBIGUOUS')
    snapshot,attempt,events=candidates[0];run=snapshot['run']
    identity=json.loads(run['identity_json'])
    _prove(run['identity_hash']==digest(identity))
    _prove(run['instance_id']==store.instance and run['subject_id']=='U1' and run['origin']=='TASK_NODE')
    _prove(run['parent_run_id']==contract['run_id'] and attempt['actor']=='TASK_EXECUTOR')
    _prove(identity.get('task_id')==task['task_id'] and identity.get('node_id')==node['id']
           and identity.get('execution_key')==node['execution_key'],'CONFLICT')
    _prove(run['state'] in {'CLOSED','INTERRUPTED'} and run['producers_stopped']==1 and run['trace_finalized']==1)
    dispatch_version=identity.get('plan_version')
    _prove(type(dispatch_version) is int and version<=dispatch_version<=task['plan_version'])
    documents=[task] if dispatch_version==task['plan_version'] else [h for h in task['plan_history'] if h['version']==dispatch_version]
    _prove(len(documents)==1)
    originals=[n for n in documents[0]['nodes'] if n['id']==node['id']]
    _prove(len(originals)==1)
    fields=('id','tool','args','depends_on')
    _prove(canonical({k:originals[0][k] for k in fields})==canonical({k:node[k] for k in fields}),'CONFLICT')
    raw=json.loads(attempt['raw_safe_json'])
    _prove(attempt['entry_name']==node['tool'] and attempt['logical_key']==node['execution_key'],'CONFLICT')
    _prove(attempt['raw_integrity']=='EXACT_SAFE' and attempt['raw_safe_hash']==digest(raw))
    _prove(canonical(raw)==canonical(node['args']),'CONFLICT')
    # Reconstruct only the existing historical reserve injection, never current policy.
    expected=json.loads(canonical(expected_args))
    if contract.get('origin_kind')!='TRUSTED_ROOT' and node['tool']=='prepare_transfer':
        _prove(contract.get('version')==1)
        item_id=contract['node_items'].get(node['id'])
        items=[item for item in contract['items'] if item['id']==item_id]
        _prove(len(items)==1)
        slot=items[0]['slots'].get('reserve_minor')
        if slot is not None:
            floor=slot['value'];source=slot['source']
            _prove(type(floor) is int and floor>=0 and isinstance(source,dict)
                   and isinstance(source.get('message_id'),str) and isinstance(source.get('quote'),str)
                   and bool(source['message_id']) and bool(source['quote']))
            _prove('reserve_minor' not in expected or canonical(expected['reserve_minor'])==canonical(floor),'CONFLICT')
            expected['reserve_minor']=floor
    _prove(parameter_view(expected)[1]=='EXACT_SAFE')
    business=json.loads(attempt['business_safe_json'])
    validated=[e for e in events if e['phase']=='VALIDATED']
    dispatched=[e for e in events if e['phase']=='DISPATCHED']
    _prove(len(validated)==1 and len(dispatched)==1 and validated[0]['sequence']<dispatched[0]['sequence'])
    _prove(all(e['payload_hash']==digest(e['data']) for e in events))
    _prove(attempt['business_safe_hash']==digest(business)==validated[0]['data'].get('business_hash'))
    _prove(canonical(business)==canonical(expected),'CONFLICT')
    kinds={'prepare_transfer':'transfer','prepare_mandate_query':'mandate_query',
           'prepare_mandate_cancel':'mandate_cancel','propose_arrangement':'arrangement'}
    _prove(node['tool'] in kinds)
    params={k:v for k,v in business.items() if k!='idempotency_key'}
    with closing(sqlite3.connect(Path(service.db_path).resolve().as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row
        rows=db.execute('SELECT operation_id,kind,subject_id,params_json FROM operations WHERE idempotency_key=? LIMIT 2',
                        (node['execution_key'],)).fetchall()
    _prove(bool(rows),'NOT_FOUND');_prove(len(rows)==1,'AMBIGUOUS')
    row=rows[0]
    _prove(row['subject_id']=='U1' and row['kind']==kinds[node['tool']]
           and canonical(json.loads(row['params_json']))==canonical(params),'CONFLICT')
    result=service.tasks.call_tool('operation',{'operation_id':row['operation_id']})
    op=result['data']['operation']
    _prove(op['operation_id']==row['operation_id'] and op['kind']==row['kind']
           and canonical(op['summary']['params'])==canonical(params),'CONFLICT')
    _prove(store.bank_identity(service.db_path)==bank_identity,'CONFLICT')
    # This is the existing UI recovery payload, including its no-key params hash.
    recovered={'certain':True,'refs':[{'kind':'operation','id':row['operation_id']}],
               'parameters_hash':digest(params),'bank_identity':bank_identity}
    previous=[e for e in events if e['phase']=='RECOVERED']
    returned=[e for e in events if e['phase'] in {'RETURNED','LATE_RETURN'}]
    if previous:
        _prove(len(previous)==1 and canonical(previous[0]['data'])==canonical(recovered),'CONFLICT')
    elif returned:
        # A task save may fail after the bank's reply was already recorded and
        # its audit reservation released. Reuse that result, never reopen it.
        _prove(len(returned)==1 and returned[0]['data'].get('refs')==recovered['refs'],'CONFLICT')
    else:
        store.append(RecoveryHandle(store.issuer,run['run_id'],attempt['attempt_id']),'RECOVERED',recovered)
    store.release_recovered(run['run_id'])
    return dict(version=1,outcome='FOUND',task_id=task['task_id'],node_id=node['id'],
        execution_key=node['execution_key'],tool=node['tool'],origin_contract_hash=digest(contract),
        origin_plan_version=version,original_run_id=run['run_id'],original_attempt_id=attempt['attempt_id'],
        business_args_hash=digest(business),bank_identity=bank_identity,operation_id=row['operation_id'],
        operation_result=result,observed_at=service.clock().isoformat())


def recover_task_prepare(store, service, task, node, expected_args):
    """Only an exact FOUND permits attaching a node; no match never permits replay."""
    try:
        return _task_prepare_proof(store,service,task,node,expected_args)
    except RecoveryMismatch as error:
        return {'version':1,'outcome':str(error)}
    except (ValueError,KeyError,TypeError,AttributeError,OSError,sqlite3.Error):
        return {'version':1,'outcome':'UNAVAILABLE'}


def reconcile(store,snapshot,service):
    with store.jobs.connection() as db:
        identities=store._meta(db,'domain_files') or {}
    for attempt in snapshot['attempts']:
        events=[e for e in snapshot['events'] if e['attempt_id']==attempt['attempt_id']]
        phases={e['phase'] for e in events}
        if 'DISPATCHED' not in phases or phases & {'RETURNED','LATE_RETURN','RECOVERED'}:continue
        args=json.loads(attempt['business_safe_json'] or 'null')
        if not isinstance(args,dict):continue
        name=attempt['entry_name']; refs=[];certain=False;contract_hash=None
        if name=='submit_task_plan' and identities.get('tasks')==file_identity(service.tasks.path):
            row=read(service.tasks.path,'SELECT request_hash,document FROM tasks WHERE request_key=?',(args.get('idempotency_key'),))
            if row:
                task=json.loads(row['document']);contract=task.get('request_contract')
                bound=next((e['data'].get('binding_hash') for e in events if e['phase']=='VALIDATED'),None)
                identity=[args.get('prompt'),{'title':args.get('title'),'nodes':args.get('nodes')},contract]
                if (task.get('subject_id')=='U1' and contract and bound==digest(contract)
                        and contract.get('run_id')==snapshot['run']['run_id'] and row['request_hash']==digest(identity)):
                    refs=[{'kind':'task','id':task['task_id']}];certain=True;contract_hash=bound
        elif name in {'propose_goal','goals.prepare'} and service.goals and identities.get('goals')==file_identity(service.goals.db_path):
            if name=='propose_goal':
                row=read(service.goals.db_path,'SELECT * FROM goal_drafts WHERE idempotency_key=?',(args.get('idempotency_key'),))
                if row and row['expected_revision']==args.get('expected_revision') and json.loads(row['profile_json'])==args.get('profile'):
                    refs=[{'kind':'goal_draft','id':row['draft_id']}];certain=True
            else:
                choice=read(service.goals.db_path,'SELECT * FROM choices WHERE choice_id=?',(args.get('choice_id'),))
                if not choice:continue
                assessment=read(service.goals.db_path,'SELECT * FROM assessments WHERE assessment_id=?',(choice['assessment_id'],))
                goal=read(service.goals.db_path,"SELECT * FROM goals WHERE goal_id='focus' AND subject_id='U1'",())
                if not assessment or not goal or goal['revision']!=assessment['goal_revision'] or choice['wait_for_income']:continue
                comparison=json.loads(assessment['result_json'])
                option=next((o for o in comparison['options'] if o['option_id']==choice['option_id']),None)
                if not option:continue
                expected={'title':json.loads(goal['profile_json'])['title'],'kind':'expense','amount_minor':option['amount_minor'],
                    'due_date':option['due_date'],'status':'active'}
                if comparison['replaced_arrangement']:
                    old=comparison['replaced_arrangement'];expected.update(arrangement_id=old['arrangement_id'],expected_version=old['version'])
                op=read(service.db_path,"SELECT * FROM operations WHERE idempotency_key=? AND subject_id='U1'",('goal-choice:'+choice['choice_id'],))
                if op and op['kind']=='arrangement' and json.loads(op['params_json'])==expected and choice['operation_id'] in (None,op['operation_id']):
                    refs=[{'kind':'operation','id':op['operation_id']},{'kind':'choice','id':choice['choice_id']}]
                    certain=choice['operation_id']==op['operation_id']
        if refs:
            data={'certain':certain,'refs':refs,'parameters_hash':attempt['business_safe_hash']}
            if contract_hash:data['contract_hash']=contract_hash
            if not certain:data['compound_incomplete']=True
            store.append(RecoveryHandle(store.issuer,snapshot['run']['run_id'],attempt['attempt_id']),'RECOVERED',data)
    store.release_recovered(snapshot['run']['run_id'])
