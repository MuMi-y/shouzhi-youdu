"""Synthetic product-entry tests; never start DSH or call a provider."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.competition_web.app import create_app
from src.bank_requests.execution import run_chat
from src.bank_v1_web.worker import DshRunner
from src.bank_v1_web.job_display import project
from src.bank_requests.schema import binding_schema


class HandoffProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='handoff-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = datetime(2026,10,30,2,tzinfo=timezone.utc)
        self.app = create_app(self.root,clock=lambda:self.now)
        self.jobs = self.app.extensions['bank_v1']['jobs']
        self.store = self.jobs.handoff
        self.addCleanup(self.store.release_owner)
        self.service = self.app.extensions['competition']
        self.gateway = self.service.request_gateway
        self.runner = DshRunner('.',self.root/'dsh','http://127.0.0.1:1','never-pass-root',display_service=self.service)
        self.trace = []

    def tool(self, token, name, args):
        call_id = str(len(self.trace))
        self.trace.append(dict(kind='request',call_id=call_id,name=name,arguments=deepcopy(args)))
        client = self.app.test_client()
        response = client.post('/api/tools/'+name,json=args,headers={'Authorization':'Bearer '+token})
        self.trace.append(dict(kind='response',call_id=call_id,tool=name,args=deepcopy(args),ok=response.status_code==200,status=response.status_code,response=response.json))
        return response

    def current(self):
        job = self.jobs.recent()[-1]
        return job['id'], self.jobs.request_context(job['id'])

    def item(self, mid, *, missing=False):
        values = [('source_account','A','A'),('target_account','B','B')]
        if not missing:
            values.append(('amount_minor','500元',50000))
        return dict(id='transfer',intent='transfer',continuation_of=None,
                    action={'kind':'PREPARE','source':{'message_id':mid,'quote':'准备'}},
                    slots={k:{'value':v,'source':{'message_id':mid,'quote':q}} for k,q,v in values},
                    missing_fields=['amount_minor'] if missing else [],conflicts=[])

    def answer(self, context, items, answer):
        return dict(ok=True,session_id='synthetic',trace=self.trace,text=json.dumps(dict(
            format='bank_response_v2',request_id=context['request_id'],items=items,answer=answer),ensure_ascii=False))

    def test_missing_amount_uses_actual_runner_and_persisted_chat(self):
        def model(prompt,session,**options):
            mid,context=self.current()
            return self.answer(context,[self.item(mid,missing=True)],{'format':'request_clarification_v1','item_ids':['transfer']})
        with patch.object(self.runner,'_run_model',side_effect=model):
            result=run_chat(self.runner,self.jobs,'准备从A转到B',None)
        self.assertTrue(result['ok'],result)
        self.assertIn('金额',result['text'])
        self.assertEqual([],self.service.bank.state()['operations'])
        shown=project(self.jobs.recent()[-1],self.service,self.store)
        self.assertTrue(shown['result']['ok'])

    def test_rejection_then_pending_is_disclosed_without_model_failure_component(self):
        def model(prompt,session,**options):
            token=options['tool_token'];mid,context=self.current();item=self.item(mid)
            args=dict(source_account='A',target_account='B',amount_minor=50000,idempotency_key='synthetic-transfer',request_binding={'items':[item]})
            bad=deepcopy(args);bad['request_binding']['items'][0]['slots']['source_account']['source']['normalization']={'kind':'account_name','evidence_id':'invented'}
            self.assertEqual(409,self.tool(token,'prepare_transfer',bad).status_code)
            response=self.tool(token,'prepare_transfer',args)
            self.assertEqual(200,response.status_code,response.json)
            operation=response.json['data']['operation']
            body=self.tool(token,'operation',{'operation_id':operation['operation_id']}).json
            return self.answer(context,[item],{'format':'bank_operation_v1','operation_id':operation['operation_id'],'evidence_id':body['evidence_id']})
        before=self.service.bank.state()['accounts']
        with patch.object(self.runner,'_run_model',side_effect=model):
            result=run_chat(self.runner,self.jobs,'准备从A转500元到B',None)
        self.assertTrue(result['ok'],result)
        shown=project(self.jobs.recent()[-1],self.service,self.store)
        entries=shown['result']['host_disclosure']['entries']
        self.assertEqual('REJECTED_BEFORE_DISPATCH',entries[0]['status'])
        self.assertTrue(any(r['state']=='PENDING' for e in entries for r in e['objects']))
        self.assertEqual(before,self.service.bank.state()['accounts'])
        self.assertEqual(1,len(self.service.bank.state()['operations']))

    def test_model_failure_keeps_original_pending_on_refresh(self):
        def model(prompt,session,**options):
            mid,_=self.current();item=self.item(mid)
            response=self.tool(options['tool_token'],'prepare_transfer',dict(source_account='A',target_account='B',amount_minor=50000,idempotency_key='pending-failure',request_binding={'items':[item]}))
            self.assertEqual(200,response.status_code,response.json)
            return {'ok':False,'error':'synthetic failure','trace':self.trace}
        with patch.object(self.runner,'_run_model',side_effect=model):
            result=run_chat(self.runner,self.jobs,'准备从A转500元到B',None)
        self.assertFalse(result['ok'])
        for _ in range(2):
            shown=project(self.jobs.recent()[-1],self.service,self.store)
            self.assertTrue(shown['result']['host_disclosure']['entries'])
        self.assertEqual(1,len(self.service.bank.state()['operations']))

    def test_v2_schema_has_no_legacy_branch_and_direct_model_cannot_use_root(self):
        self.assertNotIn('normalization',json.dumps(binding_schema('minimal_source_v2')))
        self.assertIn('normalization',json.dumps(binding_schema('legacy_source_v1')))
        with patch('subprocess.Popen') as process:
            with self.assertRaisesRegex(ValueError,'SCOPED_REQUEST_REQUIRED'):
                self.runner._run_model('anything',None)
            process.assert_not_called()

    def test_ordinary_explicit_month_delivers_billing(self):
        def model(prompt,session,**options):
            mid,context=self.current()
            body=self.tool(options['tool_token'],'billing_report',{'start_date':'2026-09-01','end_date':'2026-09-30'}).json
            item=dict(id='bill',intent='billing',continuation_of=None,action={'kind':'ANALYZE','source':{'message_id':mid,'quote':'查看'}},
                      slots={'period_text':{'value':'2026年9月','source':{'message_id':mid,'quote':'2026年9月'}}},missing_fields=[],conflicts=[])
            return self.answer(context,[item],{'format':'bank_billing_v1','report_evidence_id':body['evidence_id'],'sections':['overview'],'fact_ids':[],'calculation_ids':[]})
        with patch.object(self.runner,'_run_model',side_effect=model):
            result=run_chat(self.runner,self.jobs,'查看2026年9月的账单',None)
        self.assertTrue(result['ok'],result)


if __name__ == '__main__':
    unittest.main()
