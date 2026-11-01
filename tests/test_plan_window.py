"""Plan UI/API boundaries and all-plan integration; only isolated fictional banks."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from src.bank_v1 import Service, DomainError
from src.bank_v1.plans import PlanService
from src.bank_v1.storage import connection
from src.competition_web.app import create_app


class PlanWindowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = datetime(2026,10,28,3,tzinfo=timezone.utc)
        self.bank = Service(self.root/'bank.sqlite3',clock=lambda:self.now)
        self.plans = PlanService(self.bank)
        self.app = create_app(self.root,bank=self.bank,clock=lambda:self.now,goals_enabled=True,goal_followup=True)
        self.app.extensions['runtime_status'] = lambda: {'managed':True,'ready_for_requests':False,'mode':'NO_MODEL','reason':'offline'}
        self.client=self.app.test_client();self.state=self.client.get('/api/state').json
        self.headers={'Origin':'http://localhost','X-CSRF-Token':self.state['csrf_token']}

    def day(self,n):return (self.now+timedelta(days=n)).date().isoformat()

    def draft(self,days=365,amount=900000):
        return {'title':'隔离：远期设备','kind':'goal','amount_minor':amount,'due_date':self.day(days),'status':'active'}

    def save(self,draft):
        p=self.plans.preview(draft)
        op=self.plans.prepare({'draft':draft,'preview_token':p['preview_token'],'idempotency_key':'test-'+str(len(self.bank.state()['operations']))})
        return self.bank.user_action('confirm',{'operation_id':op['operation_id'],'expected_summary':op['summary']})

    def test_offline_routes_and_csrf_channel(self):
        url='/api/competition/plans/preview';body=self.draft()
        self.assertEqual(self.client.get('/api/competition/plans').status_code,200)
        self.assertEqual(self.client.post(url,json=body).status_code,403)
        self.assertEqual(self.client.post(url,json=body,headers={**self.headers,'Origin':'http://evil.test'}).status_code,403)
        token=self.app.extensions['bank_v1']['tool_token']
        self.assertEqual(self.client.post(url,json=body,headers={**self.headers,'Authorization':'Bearer '+token}).status_code,403)
        preview=self.client.post(url,json=body,headers=self.headers)
        self.assertEqual(preview.status_code,200)
        prepared=self.client.post('/api/competition/plans/prepare',json={'draft':body,'preview_token':preview.json['preview_token'],'idempotency_key':'api-new'},headers=self.headers)
        self.assertEqual(prepared.status_code,200)
        op=prepared.json
        before=self.bank.state()
        done=self.client.post('/api/actions/confirm',json={'operation_id':op['operation_id'],'expected_summary':op['summary']},headers=self.headers)
        self.assertEqual(done.status_code,200)
        self.assertEqual(done.json['status'],'SUCCEEDED')
        self.assertEqual(before['accounts'],self.bank.state()['accounts'])
        self.assertEqual(before['transactions'],self.bank.state()['transactions'])

    def test_forecast_exact_default_and_opt_in_same_latest_ledger(self):
        op=self.save(self.draft())
        args={'through_date':self.day(30),'proposed_expense_minor':350000}
        exact=self.bank.call_tool('forecast',args)['data']
        complete=self.bank.call_tool('forecast',{**args,'plan_scope':'all'})['data']
        self.assertEqual(exact['through_date'],self.day(30))
        self.assertTrue(exact['plan_coverage']['outside_window'])
        self.assertEqual(complete['through_date'],self.day(365))
        manual=self.plans.spending({'amount_minor':350000,'due_date':self.day(0)})['after']
        self.assertEqual(complete['daily'],manual['daily'])
        self.assertLess(complete['lowest_balance_minor'],exact['lowest_balance_minor'])
        record=next(a for a in self.bank.state()['arrangements'] if a['arrangement_id']==op['receipt']['arrangement_id'])
        self.save({**{k:record[k] for k in ('title','kind','amount_minor','due_date')},'status':'cancelled','arrangement_id':record['arrangement_id'],'expected_version':record['version']})
        latest=self.bank.call_tool('forecast',{**args,'plan_scope':'all'})['data']
        self.assertEqual(latest['through_date'],self.day(30))

    def test_event_uses_remote_plan_after_daily_spending(self):
        self.save(self.draft())
        self.bank.user_action('inject_transaction',{'kind':'expense','amount_minor':10000,'idempotency_key':'expense'})
        event=next(e for e in self.bank.state()['events'] if e['kind']=='expense')
        ctx=self.bank.call_tool('event_context',{'event_id':event['event_id']})['data']
        self.assertEqual(ctx['forecast']['through_date'],self.day(365))
        self.assertIn(self.day(365),ctx['facts']['summary'])
        self.assertEqual(ctx['forecast']['current_balance_minor'],self.bank.call_tool('accounts',{})['data']['total_balance_minor'])

    def test_beyond_year_visible_with_explicit_incomplete_coverage(self):
        self.save(self.draft(500))
        state=self.plans.state()
        self.assertEqual(state['forecast']['through_date'],self.day(366))
        self.assertFalse(state['forecast']['plan_coverage']['complete_for_active_dates'])
        self.assertEqual(state['forecast']['plan_coverage']['outside_window'][0]['due_date'],self.day(500))

    def test_prepare_rejects_edited_preview_and_recovers_same_request(self):
        original=self.draft();p=self.plans.preview(original)
        req={'draft':original,'preview_token':p['preview_token'],'idempotency_key':'stable'}
        with self.assertRaises(DomainError):self.plans.prepare({**req,'draft':{**original,'amount_minor':1}})
        op=self.plans.prepare(req)
        self.assertEqual(self.plans.prepare(req)['operation_id'],op['operation_id'])
        self.bank.user_action('confirm',{'operation_id':op['operation_id'],'expected_summary':op['summary']})
        self.assertEqual(self.plans.prepare(req)['status'],'SUCCEEDED')

    def test_goal_preferences_distinct_after_linked_plan_edit(self):
        facade=self.app.extensions['competition'];goals=facade.goals
        profile={'title':'相机','user_reason':'摄影','priority_reason':'先还款','preferred_amount_minor':20000,
            'min_amount_minor':10000,'reserve_floor_minor':10000,'preferred_date':self.day(20),'latest_date':self.day(25)}
        goals.action('save',{'expected_revision':0,'profile':profile})
        self.save(self.draft())
        result=goals.action('compare',{'expected_revision':1})
        self.assertEqual(result['window_end'],self.day(365))
        choice=goals.action('select',{'expected_revision':1,'assessment_id':result['assessment_id'],'option_id':'original','wait_for_income':False,'idempotency_key':'focus'})
        prepared=goals.action('prepare',{'choice_id':choice['choice_id']})
        op=self.bank.call_tool('operation',{'operation_id':prepared['operation_id']})['data']['operation']
        saved=self.bank.user_action('confirm',{'operation_id':op['operation_id'],'expected_summary':op['summary']})
        goals.action('reconcile',{'choice_id':choice['choice_id']})
        self.save({'title':'改后的相机','kind':'expense','amount_minor':55000,'due_date':self.day(60),'status':'active',
            'arrangement_id':saved['receipt']['arrangement_id'],'expected_version':1})
        state=goals.state()
        self.assertEqual(state['goal']['profile'],profile)
        self.assertTrue(state['assessment']['stale'])
        self.assertEqual(state['choice']['display_state'],'PLAN_CHANGED')
        self.assertEqual(state['choice']['current_arrangement']['amount_minor'],55000)
        context=facade.goal_tools.call('goal_context',{'goal_id':'focus'})
        self.assertEqual(context['data']['current_plan']['notice_state'],'REVIEW_REQUIRED')
        self.assertIsNotNone(context['data']['current_plan']['projection'])
        self.assertEqual(context['data']['current_plan']['arrangement']['amount_minor'],55000)
        self.assertEqual(context['data']['coverage']['window_end'],self.day(365))

    def test_malformed_inputs_no_ledger_change(self):
        original=self.bank.state()
        for draft in [{**self.draft(),'amount_minor':True},{**self.draft(),'amount_minor':-1},{**self.draft(),'unknown':1},
                      {**self.draft(),'due_date':'2027-02-30'},{**self.draft(),'title':'  '}]:
            self.assertEqual(self.client.post('/api/competition/plans/preview',json=draft,headers=self.headers).status_code,409)
        self.assertEqual(self.bank.state()['arrangements'],original['arrangements'])


if __name__=='__main__': unittest.main()
