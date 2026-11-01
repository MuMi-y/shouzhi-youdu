"""Zero-model goal/transaction impact tests; all stores are temporary."""
import json
import unittest
from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch
from tests.g2_goals import test_current_plan as fixture
from src.bank_v1.storage import connection
from src.bank_v1.common import DomainError
from src.bank_v1.plans import PlanService
from src.bank_goals import impact, impact_basis, impact_answer
from src.bank_goals.impact_contract import requirement, identity_matches, model_contract
from src.bank_v1_web.display_check import check
from src.bank_v1_web.bank_answer import validate_bank_answer


class ImpactTests(unittest.TestCase):
    def setUp(self):
        fixture.CurrentPlanTests.setUp(self)
        self.service.goals.action('save', {'expected_revision': 1,
            'profile': {**self.profile, 'reserve_floor_minor': 170000}})
        compared = self.service.goals.action('compare', {'expected_revision': 2})
        choice = self.service.goals.action('select', {'expected_revision': 2,
            'assessment_id': compared['assessment_id'], 'option_id': 'reduced',
            'wait_for_income': False, 'idempotency_key': 'choice'})
        choice = self.service.goals.action('prepare', {'choice_id': choice['choice_id']})
        op = next(o for o in self.service.bank.state()['operations'] if o['operation_id'] == choice['operation_id'])
        self.service.bank.user_action('confirm', {'operation_id': op['operation_id'], 'expected_summary': op['summary']})
        self.service.goals.action('reconcile', {'choice_id': choice['choice_id']})
        data = self.service.goal_tools.call('goal_context', {'goal_id': 'focus'})['data']
        arr = next(a for a in self.service.bank.state()['arrangements']
                   if a['arrangement_id'] == data['current_plan']['arrangement']['arrangement_id'])
        plans = PlanService(self.service.bank, goal_scope=self.service.goals.reserve_scope)
        draft = {k: arr[k] for k in ('arrangement_id', 'title', 'kind', 'due_date')}
        draft.update(amount_minor=310000, expected_version=1)
        preview = plans.preview(draft)
        op = plans.prepare({'draft': draft, 'preview_token': preview['preview_token'], 'idempotency_key': 'edit-v2'})
        self.service.bank.user_action('confirm', {'operation_id': op['operation_id'], 'expected_summary': op['summary']})
        self.jobs.add('我想跟进学习电脑计划')
        self.anchor = self.service.goal_followup.observe()
        self.jobs.add('目标检查',kind='goal',event_key='goal:'+self.anchor['check_id'],read_only=True)

    def event(self, kind, amount, key):
        return self.service.bank.user_action('inject_transaction', {'kind': kind, 'amount_minor': amount, 'idempotency_key': key})

    def two(self):
        self.event('expense', 45000, 'spend'); self.event('income', 50000, 'received')

    def call(self, mode='POSTED_HISTORY'):
        context = None
        if mode == 'POSTED_HISTORY':
            observed = self.service.goal_followup.observe()
            self.jobs.add('目标检查',kind='goal',event_key='goal:'+observed['check_id'],read_only=True)
            job = self.jobs.add('刚才消费和入账对学习电脑计划的保留底线有何影响')
            from src.bank_goals.impact_context import for_request
            context = for_request(self.service.goals,self.jobs.request_context(job['id']))
        return self.service.goal_tools.call('explain_goal_changes' if mode == 'POSTED_HISTORY' else 'compare_goal_impact',
            {'goal_id': 'focus', 'expected_revision': 2, **({'amount_minor': 35000, 'due_date': '2026-11-02'} if mode == 'WHAT_IF' else {})},
            impact_context=context)

    def raw(self, body):
        name, args = body['tool'], body['arguments']
        return {'ok': True, 'text': json.dumps({'format': 'goal_impact_v1', 'mode': body['mode'], 'source_id': body['source_id']}),
                'trace': [{'kind': 'request', 'call_id': 'c1', 'name': name, 'arguments': args},
                          {'kind': 'response', 'call_id': 'c1', 'name': name, 'tool': name, 'args': args,
                           'ok': True, 'status': 200, 'response': body}]}

    def test_two_events_before_observe_keep_every_stage_and_fixed_anchor(self):
        self.two()
        first = self.call()
        self.assertEqual([s['without_income']['reserve_shortfall_minor'] for s in first['data']['stages']], [0,25000,0])
        self.assertEqual(first['data']['target']['arrangement']['version'], 2)
        self.assertEqual(first['data']['target']['receipt_relation'], 'PLAN_CHANGED')
        self.service.goal_followup.observe()
        second = self.call()
        self.assertEqual(first['data']['stages'], second['data']['stages'])
        self.assertEqual(first['data']['history_range']['start_seq'], second['data']['history_range']['start_seq'])
        self.assertEqual(first['data']['history_range']['anchor_id'], self.anchor['check_id'])

    def test_what_if_has_two_scenarios_without_mutation(self):
        before = self.service.bank.state()
        result = self.call('WHAT_IF')
        self.assertEqual([s['without_income']['minimum_minor'] for s in result['data']['stages']], [190000,155000])
        self.assertEqual(result['data']['stages'][1]['without_income']['reserve_shortfall_minor'],15000)
        self.assertEqual(before,self.service.bank.state())

    def test_reference_only_answer_cannot_omit_stages_or_cross_mode(self):
        self.two(); raw = self.raw(self.call())
        result = validate_bank_answer(raw,'学习电脑计划，刚才消费和入账分别对保留底线有什么影响？')
        self.assertTrue(result['ok'],result)
        self.assertIn('0.00 → 250.00 → 0.00',result['text'])
        self.assertIn('不是实际到账',result['text'])
        wrong=deepcopy(raw); wrong['text']=json.dumps({**json.loads(raw['text']),'mode':'WHAT_IF'})
        self.assertFalse(impact_answer.validate(wrong)['ok'])
        omit=deepcopy(raw); omit['text']=json.dumps({**json.loads(raw['text']),'claims':['net']})
        self.assertFalse(impact_answer.validate(omit)['ok'])

    def test_third_event_preserves_history_but_rejects_current_direct_and_nested(self):
        self.two(); result=impact_answer.validate(self.raw(self.call()))
        self.assertTrue(check(result,self.service)['ok'])
        self.event('expense',100,'third')
        current=check(result,self.service)
        self.assertFalse(current['ok']); self.assertTrue(current['impact_freshness']['history_valid'])
        from src.bank_requests.delivery import recheck
        nested={'ok':True,'request_delivery':{'version':1,'decision':{'items':[]},
            'groups':{'x':[{'validated':result}]}}}
        self.assertFalse(recheck(nested,self.service)['ok'])
        with self.assertRaisesRegex(DomainError,'明确'):
            self.call()

    def test_revision_same_profile_association_and_midnight_fences(self):
        old=self.call('WHAT_IF')
        with connection(self.service.goals.db_path,True) as db:
            db.execute("UPDATE goals SET linked_version=99")
        self.assertFalse(check(impact_answer.validate(self.raw(old)),self.service)['ok'])
        self.now += timedelta(days=1)
        with self.assertRaises(DomainError):self.call()

    def test_missing_legacy_metadata_never_fabricates_history(self):
        with connection(self.service.goals.db_path,True) as db:
            db.execute('DELETE FROM goal_check_bases')
            self.assertEqual(len(db.execute('PRAGMA table_info(goal_checks)').fetchall()),9)
        self.two()
        with self.assertRaises(DomainError):self.call()
        self.assertTrue(self.service.goal_followup.state()['check'])

    def test_new_read_view_detects_changes_during_calculation(self):
        original=impact.capture
        def changed(goals):
            self.event('expense',1,'during-read')
            return original(goals)
        with patch.object(impact,'capture',side_effect=changed),self.assertRaises(DomainError):
            self.call('WHAT_IF')

    def test_epoch_restore_cannot_attach_same_sequence(self):
        self.two()
        with connection(self.service.bank.db_path,True) as db:
            db.execute('DROP TABLE projection_journal')
            from src.bank_v1.projection_journal import initialize
            initialize(db,self.now)
        with self.assertRaises(DomainError):self.call()

    def test_exact_identity_and_host_requirement_are_independent_of_model_intent(self):
        target=self.call('WHAT_IF')['data']['target']
        self.assertTrue(identity_matches('学习电脑计划',target))
        self.assertFalse(identity_matches('电脑',target))
        self.assertIsNone(requirement('我要花350元，余额会怎样',None))
        self.assertIsNone(requirement('学习电脑计划现在怎么样',target))
        self.assertEqual(requirement('如果消费350元，对学习电脑计划有什么影响',target),'WHAT_IF')
        from src.bank_goals.impact_contract import enforce
        with self.assertRaises(ValueError):enforce('POSTED_HISTORY',[])
        self.assertIn('POSTED_HISTORY',model_contract())

    def test_anchor_insert_failure_rolls_back_goal_check(self):
        self.event('expense',1,'small')
        with connection(self.service.goals.db_path) as db:
            before=db.execute('SELECT count(*) FROM goal_checks').fetchone()[0]
        with patch.object(impact_basis,'save',side_effect=RuntimeError('simulated')):
            with self.assertRaises(RuntimeError):self.service.goal_followup.observe()
        with connection(self.service.goals.db_path) as db:
            self.assertEqual(before,db.execute('SELECT count(*) FROM goal_checks').fetchone()[0])

    def test_retry_preserves_episode_baseline(self):
        follow=self.service.goal_followup;follow.ai_enabled=True
        self.event('expense',45000,'failed-analysis')
        pending=follow.observe();follow.start(pending['check_id'])
        follow.finish(pending['check_id'],{'ok':False,'error':'offline fixture'})
        retried=follow.retry(pending['check_id'])
        with connection(self.service.goals.db_path) as db:
            one=json.loads(db.execute('SELECT metadata_json FROM goal_check_bases WHERE check_id=?',(pending['check_id'],)).fetchone()[0])
            two=json.loads(db.execute('SELECT metadata_json FROM goal_check_bases WHERE check_id=?',(retried['check_id'],)).fetchone()[0])
        self.assertEqual(one['baseline_check_id'],two['baseline_check_id'])
        self.assertEqual(self.anchor['check_id'],two['baseline_check_id'])

    def test_history_without_current_conversation_references_is_refused(self):
        self.two();self.service.goal_followup.observe()
        with self.assertRaises(DomainError):
            self.service.goal_tools.call('explain_goal_changes',{'goal_id':'focus','expected_revision':2})
        from src.bank_goals.impact_context import for_request
        context={'request_id':'fresh','current_message_id':'fresh',
                 'messages':[{'message_id':'fresh','text':'刚才两笔对目标影响'}]}
        bound=for_request(self.service.goals,context)
        self.assertIsNone(bound['history_range'])

    def test_plan_prepare_and_goal_save_share_lock_order(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        plans=PlanService(self.service.bank,goal_scope=self.service.goals.reserve_scope)
        draft={'title':'另一安排','kind':'expense','amount_minor':1000,'due_date':'2026-11-05'}
        preview=plans.preview(draft);barrier=Barrier(2)
        def prepare():
            barrier.wait(timeout=2)
            try:
                return plans.prepare({'draft':draft,'preview_token':preview['preview_token'],'idempotency_key':'concurrent'})
            except DomainError as error:
                return error.code
        def save():
            barrier.wait(timeout=2)
            return self.service.goals.action('save',{'expected_revision':2,
                'profile':{**self.profile,'reserve_floor_minor':180000}})
        with ThreadPoolExecutor(max_workers=2) as pool:
            a=pool.submit(prepare);b=pool.submit(save)
            self.assertIsNotNone(a.result(timeout=8));self.assertIsNotNone(b.result(timeout=8))

    def test_shared_contract_reaches_request_runner_and_cannot_be_dropped(self):
        from src.bank_v1_web.worker import DshRunner
        from tests.competition_ai.test_request_decision import response,item,slot
        from src.bank_goals.impact_contract import CONTRACT
        from src.bank_v1.projection_journal import digest
        prompt='如果今天消费350元，对学习电脑计划有什么影响'
        job=self.jobs.add(prompt);self.jobs.claim(job_id=job['id']);context=self.jobs.request_context(job['id'])
        runner=DshRunner('.',self.root/'offline','http://127.0.0.1:1','unused',display_service=self.service)
        seen={}
        def model(text,session,**options):
            seen['text']=text
            host=json.loads(text.split('宿主上下文：\n',1)[1]);seen['host']=host
            body=self.service.request_gateway.call(options['tool_token'],'compare_goal_impact',
                {'goal_id':'focus','expected_revision':2,'amount_minor':35000,'due_date':'2026-11-02'})
            raw=self.raw(body)
            req=item({'amount_minor':slot(job['id'],'350元',35000),
                      'purpose':slot(job['id'],'学习电脑计划','学习电脑计划'),
                      'date_text':slot(job['id'],'今天','今天')},identifier='impact',intent='cashflow_compare')
            req['action']={'kind':'ANALYZE','source':{'message_id':job['id'],'quote':prompt}}
            return {**response(host,[req],json.loads(raw['text'])),'trace':raw['trace']}
        with patch.object(runner,'_run_model',side_effect=model):
            result=runner.run_request(prompt,None,request_context=context)
        self.assertTrue(result['ok'],result)
        self.assertEqual(result['business_outcomes'][0]['assessment'],'ADVICE_READY')
        self.assertIn(digest(CONTRACT),seen['text'])
        self.assertEqual(seen['host']['goal_impact_requirement'],'WHAT_IF')
        from src.bank_requests.delivery import validate
        with self.assertRaises(ValueError):
            validate({'format':'bank_facts_v1','fact_ids':[],'calculation_ids':[]},
                {'items':[{'id':'x','intent':'general','slots':{},'missing_fields':[],'conflicts':[]}]},seen['host'],[])

    def test_host_amount_binding_and_pre_event_anchor_after_edit(self):
        from src.bank_goals.impact_context import for_request
        job=self.jobs.add('如果今天再花350元，对学习电脑计划有什么影响')
        host=for_request(self.service.goals,self.jobs.request_context(job['id']))
        with self.assertRaises(DomainError):
            self.service.call_tool('compare_goal_impact',{'goal_id':'focus','expected_revision':2,
                'amount_minor':999,'due_date':'2026-11-02'},impact_context=host)
        # Product facade establishes a changed target's pre-event snapshot without a pump/model.
        self.service.goals.action('save',{'expected_revision':2,'profile':{**self.profile,'reserve_floor_minor':160000}})
        self.service.user_action('inject_transaction',{'kind':'expense','amount_minor':45000,'idempotency_key':'new-target'})
        with connection(self.service.goals.db_path) as db:
            value=json.loads(db.execute('SELECT metadata_json FROM goal_check_bases ORDER BY rowid DESC LIMIT 1').fetchone()[0])
        self.assertEqual(value['target']['revision'],3)
        self.assertEqual(value['snapshot']['accounts']['total_balance_minor'],1000000)

    def test_incomplete_window_is_not_complete_business_outcome(self):
        body=self.call('WHAT_IF');body=deepcopy(body)
        body['data']['coverage']['complete_for_active_dates']=False
        # Isolated projection test; do not call this constructed envelope trusted tool evidence.
        from src.bank_requests.outcomes import project
        from tests.competition_ai.test_request_decision import item
        component={'metadata':{'format':'goal_impact_v1','body':body},
                   'part':{'format':'goal_impact_v1'},'validated':{'ok':True,'text':'fixture'},
                   'covered':{'operations':[],'tasks':[],'failures':[],'drafts':[],'insights':[]}}
        result=project(item(identifier='x',intent='goal_followup'),[component])
        self.assertEqual(result['assessment'],'NEEDS_EVIDENCE')
        self.assertEqual(result['reason'],'IMPACT_COVERAGE_INCOMPLETE')
