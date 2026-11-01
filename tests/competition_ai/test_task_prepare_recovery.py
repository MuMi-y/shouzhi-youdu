"""Production recorder recovery using fictional banks and explicit fault injection."""
from copy import deepcopy
from contextlib import closing
from datetime import timedelta
import json
import sqlite3
import unittest
from unittest.mock import patch

from tests.competition_ai import test_handoff_protocol as protocol_fixtures
from src.competition_web.app import create_app
from src.bank_tasks.validation import TaskError
from src.bank_requests.attempt_projection import disclose
from src.bank_v1_web.handoff_contracts import canonical, digest
from src.bank_v1_web.job_display import project

OBSERVATIONS=[]


class TaskPrepareRecoveryTests(unittest.TestCase):
    def fixture(self):
        f=protocol_fixtures.HandoffProtocolTests();f.setUp();self.addCleanup(f.doCleanups)
        return f

    def node(self, amount=50000):
        return {'id':'pay','tool':'prepare_transfer','args':{'source_account':'A','target_account':'B','amount_minor':amount},'depends_on':[]}

    def submit(self,f, *, scoped=False, reference=False, amount=50000):
        node=self.node(amount);nodes=[node]
        if reference:
            node['args']['target_account']={'$ref':'read.data.accounts.1.account_id'}
            node['depends_on']=['read'];nodes.insert(0,{'id':'read','tool':'accounts','args':{},'depends_on':[]})
        args={'prompt':'准备转账','title':'恢复测试','nodes':nodes,'idempotency_key':'test-task'}
        if scoped:
            job=f.jobs.add('准备从A转500元到B，保留1000元');f.jobs.claim(job_id=job['id'])
            handle=f.store.for_job(job['id']);token=f.gateway.open(f.jobs.request_context(job['id']),readonly=False,handle=handle)
            item=f.item(job['id']);item['slots']['reserve_minor']={'value':100000,'source':{'message_id':job['id'],'quote':'1000元'}}
            args['request_binding']={'items':[item],'node_items':{'pay':'transfer'}}
            reply=f.tool(token,'submit_task_plan',args);self.assertEqual(200,reply.status_code,reply.json)
            task=reply.json['data']['task'];f.gateway.close(token);f.jobs.finish(job['id'],{'ok':False,'error':'fixture stopped','trace':f.trace})
        else:
            task=f.service.call_tool('submit_task_plan',args)['data']['task']
        if reference:f.service.tasks.tick(task['task_id'])
        return task['task_id']

    def lost(self,f,tid):
        original=f.service.tasks.call_tool
        def call(name,args):
            value=original(name,args)
            self.assertEqual('prepare_transfer',name)
            raise TimeoutError('fixture: bank committed before reply loss')
        with patch.object(f.service.tasks,'call_tool',side_effect=call):
            failed=f.service.tasks.tick(tid)
        self.assertEqual('FAILED',failed['state']);return failed

    def recover(self,f,tid):
        task=f.service.tasks.get(tid)
        if task['state']=='FAILED':f.service.tasks.retry(tid,'pay')
        with patch.object(f.service.tasks,'call_tool',wraps=f.service.tasks.call_tool) as calls:
            recovered=f.service.tasks.tick(tid)
        self.assertFalse(any(c.args[0].startswith('prepare_') for c in calls.call_args_list),calls.call_args_list)
        return recovered,[{'name':c.args[0],'args':c.args[1]} for c in calls.call_args_list]

    def mutate_task(self,f,tid,change):
        with f.service.tasks._db(True) as db:
            task=f.service.tasks._load(db,tid);change(task);f.service.tasks._save(db,task)

    def test_rec02_real_callback_loss_reopen_same_key_waits_without_prepare(self):
        f=self.fixture();tid=self.submit(f);before=f.service.bank.state()
        failed=self.lost(f,tid);op=f.service.bank.state()['operations'][0]
        with self.assertRaises(TaskError):f.service.tasks.cancel(tid)
        with self.assertRaises(TaskError):f.service.tasks.replan(tid,1,[self.node(40000)],'unknown')
        f.store.release_owner();f.app=create_app(f.root,clock=lambda:f.now)
        f.service=f.app.extensions['competition'];f.jobs=f.app.extensions['bank_v1']['jobs'];f.store=f.jobs.handoff;f.addCleanup(f.store.release_owner)
        recovered,calls=self.recover(f,tid);after=f.service.bank.state()
        self.assertEqual('WAITING_CONFIRMATION',recovered['state'])
        self.assertEqual(op['operation_id'],recovered['nodes'][0]['operation_id'])
        self.assertEqual(failed['nodes'][0]['execution_key'],recovered['nodes'][0]['execution_key'])
        self.assertEqual(1,len(after['operations']))
        for key in ('accounts','transactions','arrangements','mandates'):self.assertEqual(before[key],after[key])
        OBSERVATIONS.append({'case':'REC02_CURRENT_FIXED','before':before,'first_failure':failed,'recovered':recovered,'after':after,'recovery_calls':calls})

    def test_production_ref_and_original_reserve_injection(self):
        f=self.fixture();tid=self.submit(f,scoped=True,reference=True);self.lost(f,tid)
        task=f.service.tasks.get(tid);node=task['nodes'][-1]
        self.assertNotIn('reserve_minor',node['args']);self.assertIsInstance(node['args']['target_account'],dict)
        self.assertEqual(100000,f.service.bank.state()['operations'][0]['summary']['params']['reserve_minor'])
        recovered,_=self.recover(f,tid)
        self.assertEqual('WAITING_CONFIRMATION',recovered['state'])
        self.assertEqual(100000,recovered['nodes'][-1]['result']['data']['operation']['summary']['params']['reserve_minor'])

    def test_ref_reserve_or_origin_tamper_remains_unknown(self):
        for change in ('reserve','reference','origin'):
            with self.subTest(change=change):
                f=self.fixture();tid=self.submit(f,scoped=True,reference=True);self.lost(f,tid)
                def mutate(t):
                    if change=='reserve':t['nodes'][-1]['args']['reserve_minor']=50000
                    elif change=='reference':t['nodes'][0]['result']['data']['accounts'][1]['account_id']='A'
                    else:t['node_origins']['pay']['contract']['items'][0]['slots']['reserve_minor']['value']=50000
                self.mutate_task(f,tid,mutate);recovered,_=self.recover(f,tid)
                self.assertEqual('FAILED',recovered['state']);self.assertIsNone(recovered['nodes'][-1]['operation_id'])

    def test_ui_recovered_released_reservation_reused_unchanged(self):
        f=self.fixture();tid=self.submit(f);self.lost(f,tid);node=f.service.tasks.get(tid)['nodes'][0]
        old=f.store.task_dispatches(f.service.tasks.get(tid),node)[0]
        f.store.reconcile_bank(old,f.service.db_path);before=f.store.snapshot(old['run']['run_id'])
        self.assertEqual(0,before['attempts'][0]['reserved_bytes'])
        recovered,_=self.recover(f,tid);self.assertEqual('WAITING_CONFIRMATION',recovered['state'])
        self.assertEqual(before['events'],f.store.snapshot(old['run']['run_id'])['events'])

    def test_recovery_save_rollback_is_visible_and_later_recovers(self):
        f=self.fixture();tid=self.submit(f);self.lost(f,tid);f.service.tasks.retry(tid,'pay')
        before=f.service.tasks.get(tid)
        with patch.object(f.service.tasks,'_save',side_effect=RuntimeError('fixture save loss')):
            with self.assertRaises(RuntimeError):f.service.tasks.tick(tid)
        self.assertEqual(before,f.service.tasks.get(tid))
        with f.jobs.connection() as db:
            run=db.execute("SELECT run_id FROM handoff_attempts WHERE entry_name='task.reconcile_prepare' ORDER BY rowid DESC").fetchone()[0]
        shown=disclose(f.store.snapshot(run),f.service)
        self.assertTrue(any('任务状态未能保存' in e['text'] for e in shown['entries']),shown)
        self.assertEqual('WAITING_CONFIRMATION',self.recover(f,tid)[0]['state'])
        self.assertEqual(1,len(f.service.bank.state()['operations']))
        self.assertEqual('WAITING_CONFIRMATION',f.service.tasks.tick(tid)['state'])

    def test_retained_node_origin_survives_dispatch_in_later_plan(self):
        f=self.fixture();tid=self.submit(f)
        extra={'id':'extra','tool':'accounts','args':{},'depends_on':['pay']}
        t=f.service.tasks.replan(tid,1,[self.node(),extra],'keep original pay')
        self.assertEqual(2,t['plan_version']);self.assertEqual(1,t['node_origins']['pay']['plan_version'])
        self.lost(f,tid);recovered,_=self.recover(f,tid)
        self.assertEqual('WAITING_CONFIRMATION',recovered['state']);self.assertIn(':v1:',recovered['nodes'][0]['execution_key'])

    def test_missing_intent_does_not_replay_unknown_dispatch(self):
        f=self.fixture();tid=self.submit(f);self.lost(f,tid)
        with closing(sqlite3.connect(f.service.tasks.intent_path)) as db,db:db.execute('DELETE FROM invocation_intents')
        recovered,_=self.recover(f,tid);self.assertEqual('WAITING_CONFIRMATION',recovered['state'])

    def test_missing_or_multiple_evidence_and_live_producer_stop(self):
        for mode in ('missing','multiple','live','unreadable','no_callback'):
            with self.subTest(mode=mode):
                f=self.fixture();tid=self.submit(f);self.lost(f,tid)
                task=f.service.tasks.get(tid);snap=f.store.task_dispatches(task,task['nodes'][0])[0]
                if mode=='live':
                    with f.jobs.connection() as db:db.execute('UPDATE handoff_runs SET producers_stopped=0 WHERE run_id=?',(snap['run']['run_id'],))
                if mode=='no_callback':f.service.tasks.recover_prepare=None
                context=(patch.object(f.store,'task_dispatches',return_value=[] if mode=='missing' else [snap,snap])
                         if mode in {'missing','multiple'} else patch.object(f.store,'bank_identity',side_effect=OSError('fixture'))
                         if mode=='unreadable' else patch.object(f.store,'epoch',f.store.epoch))
                with context:recovered,_=self.recover(f,tid)
                self.assertEqual('FAILED',recovered['state']);self.assertIsNone(recovered['nodes'][0]['operation_id'])
                with self.assertRaises(TaskError):f.service.tasks.cancel(tid)

    def test_bank_key_params_subject_kind_and_identity_conflicts(self):
        for mode in ('key','amount','direction','reserve','extra_expected','subject','kind','identity'):
            with self.subTest(mode=mode):
                f=self.fixture();tid=self.submit(f);self.lost(f,tid)
                with closing(sqlite3.connect(f.service.db_path)) as db,db:
                    if mode=='identity':db.execute("UPDATE metadata SET value='{}' WHERE key='scenario'")
                    elif mode in {'key','subject','kind'}:
                        column={'key':'idempotency_key','subject':'subject_id','kind':'kind'}[mode]
                        db.execute('UPDATE operations SET '+column+'=?',('other',))
                    else:
                        params=json.loads(db.execute('SELECT params_json FROM operations').fetchone()[0])
                        if mode=='amount':params['amount_minor']=40000
                        elif mode=='direction':params.update(source_account='B',target_account='A')
                        elif mode=='reserve':params['reserve_minor']=1
                        else:params['expected_version']=2
                        db.execute('UPDATE operations SET params_json=?',(canonical(params),))
                recovered,_=self.recover(f,tid);self.assertEqual('FAILED',recovered['state'])
                self.assertIsNone(recovered['nodes'][0]['operation_id'])

    def test_current_expiry_rejection_strong_and_success_receipt(self):
        for mode in ('expired','rejected','strong','succeeded','missing_receipt'):
            with self.subTest(mode=mode):
                f=self.fixture();tid=self.submit(f,amount=150000 if mode=='strong' else 50000);self.lost(f,tid)
                op=f.service.bank.state()['operations'][0]
                if mode=='expired':f.now+=timedelta(minutes=6)
                elif mode=='rejected':f.service.bank.user_action('reject',{'operation_id':op['operation_id']})
                elif mode in {'succeeded','missing_receipt'}:
                    f.service.bank.user_action('confirm',{'operation_id':op['operation_id'],'expected_summary':op['summary']})
                original=f.service.tasks.call_tool
                def read(name,args):
                    reply=original(name,args)
                    if mode=='missing_receipt':reply['data']['operation']['receipt']=None
                    return reply
                with patch.object(f.service.tasks,'call_tool',side_effect=read):recovered,_=self.recover(f,tid)
                expected={'expired':'FAILED','rejected':'FAILED','strong':'WAITING_VERIFICATION','succeeded':'SUCCEEDED','missing_receipt':'FAILED'}[mode]
                self.assertEqual(expected,recovered['state']);self.assertEqual(1,len(f.service.bank.state()['operations']))
                self.assertEqual(op['operation_id'],recovered['nodes'][0]['operation_id'])

    def test_trusted_bank_precondition_clear_allows_original_retry_and_replan(self):
        for legacy in (False,True):
            with self.subTest(legacy=legacy):
                f=self.fixture();tid=self.submit(f,amount=99999999);failed=f.service.tasks.tick(tid)
                self.assertEqual('INSUFFICIENT_FUNDS',failed['nodes'][0]['error']['code'])
                self.assertFalse(f.service.tasks._was_invoked(failed['nodes'][0]['execution_key']))
                if legacy:
                    with f.jobs.connection() as db:
                        for row in db.execute("SELECT event_id,data_json FROM handoff_events WHERE phase='RAISED'").fetchall():
                            data=json.loads(row['data_json']);data.pop('bank_no_operation',None)
                            db.execute('UPDATE handoff_events SET data_json=?,payload_hash=? WHERE event_id=?',(canonical(data),digest(data),row['event_id']))
                f.service.tasks.retry(tid,'pay');retried=f.service.tasks.tick(tid)
                self.assertEqual('INSUFFICIENT_FUNDS',retried['nodes'][0]['error']['code'])
                f.service.call_tool('replan_task',{'task_id':tid,'expected_version':1,
                    'nodes':[self.node(40000)],'reason':'actual balance'})
                self.assertEqual('WAITING_CONFIRMATION',f.service.tasks.tick(tid)['state'])

    def test_conflicting_existing_recovery_is_not_rewritten(self):
        f=self.fixture();tid=self.submit(f);self.lost(f,tid);task=f.service.tasks.get(tid)
        old=f.store.task_dispatches(task,task['nodes'][0])[0]
        f.store.reconcile_bank(old,f.service.db_path)
        with f.jobs.connection() as db:
            row=db.execute("SELECT event_id,data_json FROM handoff_events WHERE run_id=? AND phase='RECOVERED'",(old['run']['run_id'],)).fetchone()
            data=json.loads(row['data_json']);data['parameters_hash']='conflicting-proof'
            db.execute('UPDATE handoff_events SET data_json=?,payload_hash=? WHERE event_id=?',(canonical(data),digest(data),row['event_id']))
        before=f.store.snapshot(old['run']['run_id'])
        self.assertEqual('FAILED',self.recover(f,tid)[0]['state'])
        self.assertEqual(before,f.store.snapshot(old['run']['run_id']))

    def test_recovery_audit_failure_does_not_return_complete_success(self):
        f=self.fixture();tid=self.submit(f);self.lost(f,tid);f.service.tasks.retry(tid,'pay')
        append=f.store.append
        def fail(handle,phase,data,**kw):
            if phase=='DOMAIN_COMMITTED':raise RuntimeError('fixture audit unavailable')
            return append(handle,phase,data,**kw)
        with patch.object(f.store,'append',side_effect=fail):
            with self.assertRaises(RuntimeError):f.service.tasks.tick(tid)
        with f.jobs.connection() as db:
            rid=db.execute("SELECT run_id FROM handoff_attempts WHERE entry_name='task.reconcile_prepare'").fetchone()[0]
        self.assertFalse(disclose(f.store.snapshot(rid),f.service)['complete'])
        self.assertEqual(1,len(f.service.bank.state()['operations']))


if __name__=='__main__':unittest.main(verbosity=2)
