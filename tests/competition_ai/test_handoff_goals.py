"""Current v2 runner/gateway journeys, separate from legacy interpreter fixtures."""
import json
import unittest
from copy import deepcopy
from unittest.mock import patch
from tests.g2_goals import test_preview as preview_fixture
from tests import test_goal_impact as impact_fixture
from tests.competition_ai.test_request_decision import item,slot,response
from src.bank_requests.execution import run_chat
from src.bank_v1_web.worker import DshRunner
from src.bank_v1_web.job_display import project


class HandoffGoalTests(unittest.TestCase):
    def test_goal_edit_is_not_consumption_but_mixed_request_keeps_impact_gate(self):
        from src.bank_goals.impact_contract import requirement,enforce
        target={'profile':{'title':'学习电脑'},'arrangement':None}
        for text in ('准备学习电脑草案，保留2000元','修改学习电脑目标，把保留额改为2000元','如果调整学习电脑的保留底线呢'):
            self.assertIsNone(requirement(text,target),text)
        for text in ('如果消费350元，对学习电脑计划有何影响','准备支出350元，学习电脑底线会受影响吗',
                     '想花350元，对学习电脑计划有何影响','准备购买耳机，对学习电脑的保留额有什么影响',
                     '准备学习电脑草案，保留2000元；如果消费350元，对学习电脑有何影响'):
            self.assertEqual('WHAT_IF',requirement(text,target),text)
            with self.assertRaisesRegex(ValueError,'GOAL_IMPACT_RELATION_NOT_COVERED'):
                enforce(requirement(text,target),[])
        self.assertEqual('POSTED_HISTORY',requirement('刚才消费和入账对学习电脑的底线有什么影响',target))

    def setup_fixture(self,fixture):
        self.f=fixture;fixture.setUp();self.addCleanup(fixture.doCleanups)
        self.service=fixture.service;self.jobs=fixture.jobs
        self.runner=DshRunner('.',fixture.root/'offline','http://127.0.0.1:1','unused',display_service=self.service)
        self.trace=[]

    def tool(self,token,name,args):
        call_id=str(len(self.trace));body=self.service.request_gateway.call(token,name,args)
        self.trace.extend([{'kind':'request','call_id':call_id,'name':name,'arguments':deepcopy(args)},
            {'kind':'response','call_id':call_id,'tool':name,'args':deepcopy(args),'ok':True,'status':200,'response':body}])
        return body

    def current(self):
        job=next(j for j in reversed(self.jobs.recent()) if j['kind']=='chat' and j['status']=='RUNNING')
        return job,self.jobs.request_context(job['id'])

    def test_draft_and_confirmed_goal_remain_distinct_through_current_runner(self):
        self.setup_fixture(preview_fixture.PreviewTests())
        self.service.goals.action('save',{'expected_revision':0,'profile':self.f.profile})
        before=self.service.goals.state()['goal']
        phrase='准备学习电脑草案，4500元，最低3000元，保留2000元，2026-11-22，最晚2026-11-30，先保证还款'
        def model(prompt,session,**options):
            job,context=self.current();mid=job['id']
            values={'amount_minor':('4500元',450000),'minimum_minor':('3000元',300000),
                'reserve_minor':('2000元',200000),'date_text':('2026-11-22','2026-11-22'),
                'latest_date_text':('2026-11-30','2026-11-30'),'purpose':('学习电脑','学习电脑'),'priority':('先保证还款','先保证还款')}
            request=item({k:slot(mid,q,v) for k,(q,v) in values.items()},identifier='goal',intent='goal_compare')
            request['action']={'kind':'PREPARE','source':{'message_id':mid,'quote':'准备'}}
            body=self.tool(options['tool_token'],'propose_goal',{'expected_revision':1,'profile':self.f.profile,
                'idempotency_key':'v2-draft','request_binding':{'items':[request]}})
            return {**response(context,[request],{'format':'goal_draft_v1','draft_id':body['data']['draft_id']}),'trace':self.trace}
        with patch.object(self.runner,'_run_model',side_effect=model):
            first=run_chat(self.runner,self.jobs,phrase,None)
        self.assertTrue(first['ok'],first)
        first_job=self.jobs.recent()[-1];self.assertEqual(before,self.service.goals.state()['goal'])
        def read_model(prompt,session,**options):
            self.trace=[];job,context=self.current()
            request=item(identifier='goal',intent='goal_followup',parent=first_job['id']+':goal' if job['prompt'].startswith('核对') else None)
            request['action']={'kind':'CHECK_STATUS','source':{'message_id':job['id'],'quote':job['prompt']}}
            body=self.tool(options['tool_token'],'goal_context',{'goal_id':'focus'})
            answer={'format':'goal_answer_v1','source_id':body['source_id'],
                'claims':[{k:c[k] for k in ('path','value')} for c in body['claim_catalog']],
                'next_step':body['allowed_next_steps'][0]}
            return {**response(context,[request],answer),'trace':self.trace}
        with patch.object(self.runner,'_run_model',side_effect=read_model):
            wrong=run_chat(self.runner,self.jobs,'核对刚才草案的状态',None)
            self.assertFalse(wrong['ok'])
            self.assertEqual('DRAFT_STATUS_TOOL_UNAVAILABLE',wrong['answer_validation']['reason'])
            current=run_chat(self.runner,self.jobs,'查询当前已确认的重点目标',None)
        self.assertTrue(current['ok'],current)
        self.assertEqual(before,self.service.goals.state()['goal'])

    def test_current_runner_calculates_full_goal_impact_and_history(self):
        self.setup_fixture(impact_fixture.ImpactTests())
        before=self.service.bank.state()
        def model(prompt,session,**options):
            self.trace=[];job,context=self.current();mid=job['id']
            history='刚才' in job['prompt']
            body=self.tool(options['tool_token'],'explain_goal_changes' if history else 'compare_goal_impact',
                {'goal_id':'focus','expected_revision':2,**({} if history else {'amount_minor':35000,'due_date':'2026-11-02'})})
            request=item({'goal_text' if history else 'purpose':slot(mid,'学习电脑计划','学习电脑计划'),**({} if history else {
                'amount_minor':slot(mid,'350元',35000),'date_text':slot(mid,'今天','今天')})},identifier='impact',intent='goal_followup' if history else 'cashflow_compare')
            request['action']={'kind':'ANALYZE','source':{'message_id':mid,'quote':job['prompt']}}
            return {**response(context,[request],{'format':'goal_impact_v1','mode':body['mode'],'source_id':body['source_id']}),'trace':self.trace}
        with patch.object(self.runner,'_run_model',side_effect=model):
            whatif=run_chat(self.runner,self.jobs,'如果今天消费350元，对学习电脑计划有什么影响',None)
            self.assertTrue(whatif['ok'],whatif)
            self.assertEqual(before,self.service.bank.state())
            self.f.two();check=self.service.goal_followup.observe()
            self.jobs.add('目标检查',kind='goal',event_key='goal:'+check['check_id'],read_only=True)
            history=run_chat(self.runner,self.jobs,'刚才消费和入账对学习电脑计划的保留底线有何影响',None)
        self.assertTrue(history['ok'],history)
        self.assertIn('0.00 → 250.00 → 0.00',history['text'])
        shown=project(self.jobs.recent()[-1],self.service,self.jobs.handoff)
        self.assertTrue(shown['result']['ok'])
