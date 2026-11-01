"""Task/event, interrupted commit, redaction and strict coverage integration."""
from copy import deepcopy
from unittest.mock import patch
import unittest

from tests.competition_ai import test_handoff_protocol as fixtures
from src.bank_requests.attempt_projection import collect
from src.bank_v1_web.job_display import project
from src.bank_v1_web.handoff_contracts import parameter_view


class HandoffAttemptsTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.HandoffProtocolTests();self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def task(self):
        f=self.f
        job=f.jobs.add('准备从A转500元到B')
        f.jobs.claim(job_id=job['id']);handle=f.store.for_job(job['id'])
        context=f.jobs.request_context(job['id']);item=f.item(job['id'])
        token=f.gateway.open(context,readonly=False,handle=handle)
        args={'prompt':'准备转账','title':'转账任务','idempotency_key':'task',
              'nodes':[{'id':'transfer','tool':'prepare_transfer','args':{'source_account':'A','target_account':'B','amount_minor':50000},'depends_on':[]}],
              'request_binding':{'items':[item],'node_items':{'transfer':'transfer'}}}
        reply=f.tool(token,'submit_task_plan',args)
        self.assertEqual(200,reply.status_code,reply.json)
        f.gateway.close(token)
        f.jobs.finish(job['id'],{'ok':False,'error':'synthetic stopped reply','trace':f.trace})
        return reply.json['data']['task']['task_id'],job['id']

    def test_task_retains_origin_and_bank_commit_survives_task_rollback(self):
        f=self.f;task_id,job_id=self.task()
        with patch.object(f.service.tasks,'_save',side_effect=RuntimeError('synthetic state commit failure')):
            with self.assertRaises(RuntimeError):
                f.service.tasks.tick(task_id)
        self.assertEqual(1,len(f.service.bank.state()['operations']))
        shown=project(f.jobs.get(job_id),f.service,f.store)
        entries=shown['result']['host_disclosure']['entries']
        self.assertTrue(any('不表示银行操作已撤回' in e['text'] for e in entries),entries)
        original=f.service.bank.state()['operations'][0]['operation_id']
        with patch.object(f.service.tasks,'call_tool',wraps=f.service.tasks.call_tool) as calls:
            retried=f.service.tasks.tick(task_id)
        self.assertEqual('WAITING_CONFIRMATION',retried['state'])
        self.assertEqual(1,len(f.service.bank.state()['operations']))
        self.assertEqual(original,retried['nodes'][0]['operation_id'])
        self.assertFalse(any(call.args[0]=='prepare_transfer' for call in calls.call_args_list))

    def test_event_scoped_lease_is_readonly_and_attempts_denial(self):
        f=self.f;job=f.jobs.add('银行事件说明',kind='event',event_key='event:synthetic:0:0')
        f.jobs.claim();handle=f.store.for_job(job['id'])
        token=f.gateway.open(None,readonly=True,handle=handle)
        self.assertEqual(200,f.tool(token,'accounts',{}).status_code)
        self.assertEqual(409,f.tool(token,'prepare_transfer',{}).status_code)
        snapshot=f.store.snapshot(handle.run_id)
        self.assertEqual('EVENT_TYPED',snapshot['run']['response_mode'])
        self.assertIn('REJECTED_BEFORE_DISPATCH',[e['phase'] for e in snapshot['events']])
        self.assertEqual([],f.service.bank.state()['operations'])

    def test_unpaired_trace_is_retained_as_unknown_not_fabricated_dispatch(self):
        f=self.f;job=f.jobs.add('准备从A转500元到B');f.jobs.claim(job_id=job['id'])
        handle=f.store.for_job(job['id'])
        result=collect(f.store,handle,[{'kind':'request','name':'prepare_transfer','call_id':'lost','arguments':{'amount_minor':50000}}])
        self.assertFalse(result['complete'])
        snap=f.store.snapshot(handle.run_id)
        self.assertEqual('MODEL_TRACE',snap['attempts'][0]['actor'])
        self.assertEqual({'BEGIN','TRACE_UNPAIRED'},{e['phase'] for e in snap['events']})

    def test_identical_calls_with_ambiguous_trace_keep_original_attempts_incomplete(self):
        f=self.f;job=f.jobs.add('查询两次余额');f.jobs.claim(job_id=job['id'])
        handle=f.store.for_job(job['id'])
        token=f.gateway.open(f.jobs.request_context(job['id']),readonly=False,handle=handle)
        for _ in range(2):
            self.assertEqual(200,f.tool(token,'accounts',{}).status_code)
        f.gateway.close(token)
        before=f.store.snapshot(handle.run_id)
        for trace in (f.trace, [row for row in f.trace if row['kind']=='request']):
            with self.subTest(response_records=any(row['kind']=='response' for row in trace)):
                result=collect(f.store,handle,trace)
                self.assertFalse(result['complete'])
        after=f.store.snapshot(handle.run_id)
        self.assertEqual(before['attempts'],after['attempts'])
        self.assertEqual(before['events'],after['events'])
        self.assertEqual('INCOMPLETE',after['run']['completeness'])
        f.jobs.finish(job['id'],{'ok':False,'error':'ambiguous trace','trace':f.trace})
        self.assertFalse(project(f.jobs.get(job['id']),f.service,f.store)['result']['host_disclosure']['complete'])

    def test_bank_commit_missing_return_reconciles_exact_original_key_only(self):
        f=self.f;job=f.jobs.add('准备从A转500元到B');f.jobs.claim(job_id=job['id']);handle=f.store.for_job(job['id'])
        args={'source_account':'A','target_account':'B','amount_minor':50000,'idempotency_key':'lost-response'}
        attempt=f.store.begin_attempt(handle,'prepare_transfer',args,'MODEL_GATEWAY',effect_class='BANK_PREPARE')
        f.store.validated(attempt,args);f.store.append(attempt,'DISPATCHED',{})
        op=f.service.bank.call_tool('prepare_transfer',args)['data']['operation']
        f.jobs.finish(job['id'],{'ok':False,'error':'lost'})
        shown=project(f.jobs.get(job['id']),f.service,f.store)
        self.assertTrue(any(r['id']==op['operation_id'] for e in shown['result']['host_disclosure']['entries'] for r in e['objects']))
        self.assertEqual(1,len(f.service.bank.state()['operations']))

    def test_sensitive_values_are_not_stored_in_safe_views(self):
        text,integrity=parameter_view({'password':'private','otp':'654321','verification_factor':'hidden',
            'challenge_answer':'222222','nested':{'Authorization':'Bearer abc'},'text':'ark-synthetic-key'})
        for secret in ('private','654321','hidden','222222','Bearer abc','ark-synthetic-key'):
            self.assertNotIn(secret,text)

    def test_project_never_reports_incomplete_empty_history_as_complete(self):
        f=self.f;job=f.jobs.add('查询余额');f.jobs.claim(job_id=job['id'])
        handle=f.store.for_job(job['id'])
        f.store.mark_incomplete(handle.run_id)
        f.jobs.finish(job['id'],{'ok':False,'error':'synthetic missing trace'})
        shown=project(f.jobs.get(job['id']),f.service,f.store)
        self.assertFalse(shown['result']['host_disclosure']['complete'])
        self.assertEqual([],shown['result']['host_disclosure']['entries'])
        self.assertFalse(shown['result']['host_disclosure']['runs'][0]['complete'])

    def test_original_key_wrong_parameters_subject_or_missing_evidence_remains_unknown(self):
        from src.bank_v1.storage import connection
        f=self.f
        for mismatch in ('parameters','subject','missing'):
            with self.subTest(mismatch=mismatch):
                job=f.jobs.add('准备从A转500元到B');f.jobs.claim(job_id=job['id'])
                handle=f.store.for_job(job['id'])
                args={'source_account':'A','target_account':'B','amount_minor':50000,'idempotency_key':mismatch}
                a=f.store.begin_attempt(handle,'prepare_transfer',args,'MODEL_GATEWAY',effect_class='BANK_PREPARE')
                f.store.validated(a,args);f.store.append(a,'DISPATCHED',{})
                if mismatch!='missing':
                    op=f.service.bank.call_tool('prepare_transfer',{**args,'amount_minor':40000 if mismatch=='parameters' else 50000})['data']['operation']
                    if mismatch=='subject':
                        with connection(f.service.db_path,True) as db:
                            db.execute('UPDATE operations SET subject_id=? WHERE operation_id=?',('U2',op['operation_id']))
                f.jobs.finish(job['id'],{'ok':False,'error':'lost response'})
                shown=project(f.jobs.get(job['id']),f.service,f.store)
                self.assertEqual('UNKNOWN',shown['result']['host_disclosure']['entries'][0]['status'])
                self.assertFalse(shown['result']['host_disclosure']['complete'])
                self.assertNotIn('RECOVERED',[e['phase'] for e in f.store.snapshot(handle.run_id)['events']])

    def test_task_confirmation_refresh_and_restart_keep_same_operation(self):
        from src.competition_web.app import create_app
        f=self.f;task_id,job_id=self.task()
        waiting=f.service.tasks.tick(task_id)
        self.assertEqual('WAITING_CONFIRMATION',waiting['state'],waiting)
        op_id=waiting['nodes'][0]['operation_id']
        f.store.release_owner()
        app=create_app(f.root,clock=lambda:f.now)
        store=app.extensions['bank_v1']['jobs'].handoff;self.addCleanup(store.release_owner)
        service=app.extensions['competition']
        still=service.tasks.tick(task_id)
        self.assertEqual(op_id,still['nodes'][0]['operation_id'])
        op=service.bank.call_tool('operation',{'operation_id':op_id})['data']['operation']
        service.user_action('confirm',{'operation_id':op_id,'expected_summary':op['summary']})
        done=service.tasks.tick(task_id)
        self.assertEqual('SUCCEEDED',done['state'],done)
        self.assertEqual(1,len(service.bank.state()['operations']))
        shown=project(app.extensions['bank_v1']['jobs'].get(job_id),service,store)
        self.assertTrue(any(o['id']==op_id and o['state']=='SUCCEEDED' for e in shown['result']['host_disclosure']['entries'] for o in e['objects']))
        self.assertTrue(any(o['kind']=='task' and o['id']==task_id and o['state']=='SUCCEEDED'
                            for e in shown['result']['host_disclosure']['entries'] for o in e['objects']))
        with patch.object(service.tasks,'get',side_effect=ValueError('original task unavailable')):
            unavailable=project(app.extensions['bank_v1']['jobs'].get(job_id),service,store)
        self.assertTrue(any(o['kind']=='task' and o['id']==task_id and o['state']=='UNKNOWN'
                            for e in unavailable['result']['host_disclosure']['entries'] for o in e['objects']))

    def test_root_task_compatibility_and_model_cannot_forge_root_contract(self):
        f=self.f
        args={'prompt':'准备转账','title':'可信入口','idempotency_key':'root',
              'nodes':[{'id':'transfer','tool':'prepare_transfer','args':{'source_account':'A','target_account':'B','amount_minor':10000},'depends_on':[]}]}
        task=f.service.call_tool('submit_task_plan',args)['data']['task']
        self.assertEqual('WAITING_CONFIRMATION',f.service.tasks.tick(task['task_id'])['state'])
        job=f.jobs.add('准备从A转500元到B');f.jobs.claim(job_id=job['id'])
        token=f.gateway.open(f.jobs.request_context(job['id']),readonly=False,handle=f.store.for_job(job['id']))
        bad={**args,'request_contract':{'origin_kind':'TRUSTED_ROOT'}}
        self.assertEqual(409,f.tool(token,'submit_task_plan',bad).status_code)
        self.assertEqual(1,len(f.service.tasks.list()))

    def test_mandate_task_requires_confirmed_query_before_matching_and_cancel(self):
        from tests.competition_tasks.test_mandate_match import plan
        f=self.f;merchant=f.service.bank.state()['mandates'][0]['name']
        job=f.jobs.add('先查询，再准备撤销'+merchant);f.jobs.claim(job_id=job['id'])
        context=f.jobs.request_context(job['id']);handle=f.store.for_job(job['id'])
        token=f.gateway.open(context,readonly=False,handle=handle)
        request={'id':'cancel','intent':'mandate_cancel','continuation_of':None,'missing_fields':[],'conflicts':[],
            'action':{'kind':'PREPARE','source':{'message_id':job['id'],'quote':'准备撤销'}},
            'slots':{'merchant_text':{'value':merchant,'source':{'message_id':job['id'],'quote':merchant}}}}
        reply=f.tool(token,'submit_task_plan',{'prompt':job['prompt'],'title':'取消授权','idempotency_key':'mandate-task',
            'nodes':plan(merchant),'request_binding':{'items':[request],'node_items':{'query':'cancel','match':'cancel','cancel':'cancel'}}})
        self.assertEqual(200,reply.status_code,reply.json)
        task_id=reply.json['data']['task']['task_id'];waiting=f.service.tasks.tick(task_id)
        self.assertEqual('WAITING_CONFIRMATION',waiting['state'],waiting)
        for _ in range(2):f.service.tasks.tick(task_id)
        self.assertEqual(1,len(f.service.bank.state()['operations']))
        query=f.service.bank.call_tool('operation',{'operation_id':waiting['nodes'][0]['operation_id']})['data']['operation']
        self.assertEqual('mandate_query',query['kind'])
        f.service.user_action('confirm',{'operation_id':query['operation_id'],'expected_summary':query['summary']})
        for _ in range(3):waiting=f.service.tasks.tick(task_id)
        cancel=f.service.bank.call_tool('operation',{'operation_id':waiting['nodes'][2]['operation_id']})['data']['operation']
        self.assertEqual('mandate_cancel',cancel['kind'])
        self.assertEqual(merchant,cancel['summary']['mandate_snapshot']['name'])
        f.service.user_action('confirm',{'operation_id':cancel['operation_id'],'expected_summary':cancel['summary']})
        f.service.tasks.tick(task_id)  # Reconcile confirmation before final receipt node.
        self.assertEqual('SUCCEEDED',f.service.tasks.tick(task_id)['state'])
        self.assertEqual(2,len(f.service.bank.state()['operations']))


if __name__=='__main__':
    unittest.main()
