"""G2A offline contracts on new fictional runtimes. No provider or bank finish_event calls."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from scripts.g1_goal_demo import seed_new_fixture
from src.bank_v1 import Service, DomainError
from src.bank_v1.storage import connection
from src.bank_v1_web.worker import Worker, DshRunner
from src.bank_v1_web.bank_answer import validate_bank_answer
from src.competition_web.app import create_app
from src.competition_web.pump import CompetitionPump
from src.bank_goals.evidence import validate_answer

EVIDENCE = Path(__file__).resolve().parents[2]/'management/evidence/G2A_20261017'


def trace_for(response, args=None, name='goal_context'):
    args = args or {'goal_id': 'focus'}
    return [{'kind': 'request', 'call_id': 'offline-1', 'name': name, 'arguments': args},
            {'kind': 'response', 'call_id': 'offline-1', 'tool': name, 'name': name,
             'args': args, 'status': 200, 'ok': True, 'response': response}]


def stub_result(response):
    required = {'status', 'options.original.primary', 'options.original.without_income'}
    answer = {'format': 'goal_answer_v1', 'source_id': response['source_id'],
        'claims': [{k: c[k] for k in ('path', 'value')} for c in response['claim_catalog'] if c['path'] in required],
        'next_step': response['allowed_next_steps'][0]}
    return {'ok': True, 'text': json.dumps(answer, ensure_ascii=False), 'trace': trace_for(response),
            'execution_mode': 'OFFLINE_STUB'}


class G2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = datetime(2026, 11, 2, 2, tzinfo=timezone.utc)
        seed_new_fixture(self.root/'bank.sqlite3', self.now)
        self.app = create_app(self.root, clock=lambda:self.now, goals_enabled=True, goal_followup=True)
        self.service = self.app.extensions['competition']; self.bank = self.service.bank
        self.jobs = self.app.extensions['bank_v1']['jobs']
        self.addCleanup(self.jobs.handoff.release_owner)
        self.profile = {'title':'学习电脑','user_reason':'学习','priority_reason':'优先还款',
            'preferred_amount_minor':450000,'min_amount_minor':300000,'preferred_date':'2026-11-22',
            'latest_date':'2026-11-30','reserve_floor_minor':200000}
        self.service.goals.action('save', {'expected_revision':0, 'profile':self.profile})
        self.follow = self.service.goal_followup

    def fresh(self):
        return self.service.call_tool('goal_context', {'goal_id':'focus'})

    def inject(self, amount=10000, key='g2-expense'):
        return self.bank.user_action('inject_transaction', {'kind':'expense','amount_minor':amount,'idempotency_key':key})

    def test_real_http_schema_draft_boundary_and_registration_fixture(self):
        token = self.app.extensions['bank_v1']['tool_token']
        headers = {'Authorization':'Bearer '+token}
        client = self.app.test_client()
        catalog = client.get('/api/tool-schema', headers=headers).get_json()
        self.assertEqual(len(catalog['tools']),24)
        self.assertTrue({'goal_context','compare_goal_options','propose_goal'} <= {x['name'] for x in catalog['tools']})
        response = client.post('/api/tools/goal_context',json={'goal_id':'focus'},headers=headers)
        self.assertEqual(response.status_code,200)
        original = self.service.goals.state()['goal']
        proposed = client.post('/api/tools/propose_goal',json={'expected_revision':1,
            'profile':{**self.profile,'title':'未确认新电脑'},'idempotency_key':'draft-1'},headers=headers)
        self.assertEqual(proposed.status_code,200)
        self.assertEqual(self.service.goals.state()['goal'],original)
        self.assertNotEqual(client.post('/api/competition/goals/save',json={'expected_revision':1,
            'profile':self.profile},headers=headers).status_code,200)
        for name,args in [('goal_context',{'goal_id':'focus','confirm':True}),
                          ('compare_goal_options',{'goal_id':'focus','expected_revision':1.0}),
                          ('propose_goal',{'expected_revision':1,'profile':self.profile,'idempotency_key':'d','confirm':True})]:
            self.assertNotEqual(client.post('/api/tools/'+name,json=args,headers=headers).status_code,200)
        EVIDENCE.mkdir(parents=True,exist_ok=True)
        (EVIDENCE/'tool-fixture.json').write_text(json.dumps({'catalog':catalog,'response':response.get_json()},ensure_ascii=False,indent=2),encoding='utf8')

    def test_context_is_readonly_and_preserves_two_scenarios(self):
        before = self.bank.state()
        a = self.fresh()
        b = self.service.call_tool('compare_goal_options', {'goal_id':'focus','expected_revision':1})
        self.assertEqual(a['data'],b['data'])
        self.assertEqual([o['primary']['minimum_minor'] for o in a['data']['options']],[50000,200000,300000])
        self.assertEqual([o['without_income']['minimum_minor'] for o in a['data']['options']],[50000,200000,50000])
        self.assertEqual(self.bank.state(),before)
        with connection(self.service.goals.db_path) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM assessments').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM choices').fetchone()[0],0)

    def test_typed_goal_answers_and_wrong_scope_or_semantics(self):
        raw = stub_result(self.fresh())
        valid = validate_bank_answer(raw,'目标如何安排？')
        self.assertTrue(valid['ok']); self.assertIn('不计未来收入',valid['text'])
        self.assertIn('不等于付款',valid['text'])
        # The same accepted format is safely validated twice by runner and worker.
        self.assertTrue(validate_answer(valid)['ok'])
        variants=[]
        for scope in ['bank','moneydata','unknown_goal']:
            x=deepcopy(raw);x['trace'][1]['response']['scope']=scope;variants.append(x)
        x=deepcopy(raw);x['trace'][1]['tool']='fake_goal';variants.append(x)
        x=deepcopy(raw);x['trace']=x['trace'][1:];variants.append(x)
        for path in ['income_posted','payment_completed','accounts.A.balance_minor']:
            x=deepcopy(raw);a=json.loads(x['text']);a['claims'].append({'path':path,'value':250000});x['text']=json.dumps(a);variants.append(x)
        x=deepcopy(raw);a=json.loads(x['text']);a['claims'][1]['value']['date']='2027-01-01';x['text']=json.dumps(a);variants.append(x)
        x=deepcopy(raw);a=json.loads(x['text']);a['comment']='收入已经到账';x['text']=json.dumps(a);variants.append(x)
        x=deepcopy(raw);a=json.loads(x['text']);a['claims']=a['claims'][:2];x['text']=json.dumps(a);variants.append(x)
        for index,x in enumerate(variants):
            with self.subTest(index=index):self.assertFalse(validate_answer(x)['ok'])

    def test_draft_idempotent_and_cannot_assert_confirmation(self):
        args={'expected_revision':1,'profile':self.profile,'idempotency_key':'draft'}
        a=self.service.call_tool('propose_goal',args)
        self.assertEqual(a,self.service.call_tool('propose_goal',args))
        result=validate_bank_answer({'ok':True,'text':'目标已确认并付款4500元',
            'trace':trace_for(a,args,'propose_goal')},'帮我拟目标')
        self.assertTrue(result['ok']);self.assertNotIn('已确认并付款',result['text'])
        self.assertIn('尚未改变',result['text'])
        with self.assertRaises(DomainError):
            self.service.call_tool('propose_goal',{**args,'profile':{**self.profile,'title':'other'}})

    def test_pump_observes_goal_and_arrangement_without_ledger_change(self):
        pump=CompetitionPump(self.service,self.jobs,clock=lambda:self.now)
        pump.step(); first=self.follow.state()['check']
        self.assertEqual(first['state'],'NO_MODEL');self.assertEqual(self.jobs.recent(),[])
        pump.step();self.assertEqual(self.follow.state()['check']['check_id'],first['check_id'])
        self.service.goals.action('save',{'expected_revision':1,'profile':{**self.profile,'min_amount_minor':290000}})
        pump.step();second=self.follow.state()['check']
        self.assertIn('revision',second['reasons'])
        with connection(self.bank.db_path,True) as db:
            db.execute("UPDATE arrangements SET amount_minor=400000 WHERE arrangement_id='income'")
        pump.step();third=self.follow.state()['check']
        self.assertNotEqual(second['check_id'],third['check_id']);self.assertIn('income_conditions',third['reasons'])
        self.assertEqual(self.jobs.recent(),[])

    def test_quiet_change_updates_facts_without_new_explanation(self):
        self.service.goals.action('save',{'expected_revision':1,'profile':{**self.profile,
            'preferred_amount_minor':100000,'min_amount_minor':100000,'reserve_floor_minor':0}})
        self.follow.ai_enabled=True;self.follow.enqueue(self.jobs)
        first=self.follow.state()['check'];self.assertEqual(len(self.jobs.recent()),1)
        self.follow.finish(first['check_id'],stub_result(self.fresh()))
        self.inject(1);self.follow.enqueue(self.jobs)
        latest=self.follow.state()['check'];self.assertEqual(latest['state'],'QUIET')
        self.assertNotEqual(first['basis_hash'],latest['basis_hash']);self.assertEqual(len(self.jobs.recent()),1)
        self.follow.enqueue(self.jobs);self.assertEqual(len(self.jobs.recent()),1)

    def test_unfinished_important_explanation_is_not_lost_by_quiet_change(self):
        self.service.goals.action('save',{'expected_revision':1,'profile':{**self.profile,
            'preferred_amount_minor':100000,'min_amount_minor':100000,'reserve_floor_minor':0}})
        self.follow.ai_enabled=True;self.follow.enqueue(self.jobs)
        first=self.follow.state()['check'];self.inject(1);self.follow.enqueue(self.jobs)
        latest=self.follow.state()['check']
        self.assertNotEqual(first['check_id'],latest['check_id'])
        self.assertEqual(latest['state'],'PENDING')

    def test_weekly_date_tick_respects_cadence_but_real_changes_do_not_wait(self):
        self.jobs.set_setting('schedule',{'enabled':True,'cadence':'weekly'})
        pump=CompetitionPump(self.service,self.jobs,clock=lambda:self.now)
        pump.step();first=self.follow.state()['check']['check_id']
        self.now+=timedelta(days=1);pump.step()
        self.assertEqual(self.follow.state()['check']['check_id'],first)
        self.inject(1);pump.step()
        changed=self.follow.state()['check']['check_id'];self.assertNotEqual(changed,first)
        self.now+=timedelta(days=6);pump.step()
        self.assertNotEqual(self.follow.state()['check']['check_id'],changed)

    def test_actual_worker_stub_is_never_real_ai_or_bank_completion(self):
        self.follow.ai_enabled=True
        self.jobs.set_setting('schedule',{'enabled':False,'cadence':'daily'})
        self.inject(10000);before=self.bank.pending_events()
        CompetitionPump(self.service,self.jobs,clock=lambda:self.now).step()
        calls=[]
        def runner(prompt,session_id,**options):
            calls.append(options);return stub_result(self.fresh())
        worker=Worker(self.service,self.jobs,runner,clock=lambda:self.now)
        with patch.object(self.service,'finish_event',side_effect=AssertionError('Forbidden fake completion')):
            self.assertTrue(worker.step());self.assertFalse(worker.step())
        self.assertEqual(calls,[{'readonly':True}])
        self.assertEqual(self.follow.state()['check']['state'],'OFFLINE_CONTRACT_ONLY')
        self.assertEqual(self.bank.pending_events(),before)
        self.assertEqual(self.jobs.recent()[0]['status'],'FAILED')

    def test_async_old_result_rejected_even_without_pump_observation(self):
        self.follow.ai_enabled=True;check=self.follow.observe();self.follow.start(check['check_id'])
        result=stub_result(self.fresh());self.inject(1)
        accepted=self.follow.finish(check['check_id'],result)
        self.assertFalse(accepted['ok']);self.assertEqual(self.follow.state()['check']['state'],'STALE')
        self.assertTrue(self.follow.state()['stale'])
        self.follow.observe()
        with self.assertRaises(DomainError):
            self.service.call_tool('goal_context',{'goal_id':'focus','check_id':check['check_id']})

    def test_deadline_and_failed_explanation_do_not_retry_forever(self):
        self.follow.ai_enabled=True;first=self.follow.observe()
        self.follow.finish(first['check_id'],{'ok':False,'error':'offline unavailable'})
        self.follow.enqueue(self.jobs);self.assertEqual(self.jobs.recent(),[])
        self.now += timedelta(days=17)
        check=self.follow.observe();self.assertIn('deadline',check['reasons'])
        self.now += timedelta(days=4)
        check=self.follow.observe();self.assertEqual(check['context']['data']['status'],'DATE_REVIEW_REQUIRED')

    def test_full_runtime_copy_preserves_drafts_checks_and_stops_running(self):
        self.service.call_tool('propose_goal',{'expected_revision':1,'profile':self.profile,'idempotency_key':'restore'})
        self.follow.ai_enabled=True;self.follow.enqueue(self.jobs)
        job=self.jobs.claim();self.follow.start(job['event_key'].split(':',1)[1])
        destination=self.root/'restored';destination.mkdir()
        for p in self.root.glob('*.sqlite3'):shutil.copy2(p,destination/p.name)
        restored=create_app(destination,clock=lambda:self.now,goals_enabled=True,goal_followup=True,goal_ai=True)
        service=restored.extensions['competition'];jobs=restored.extensions['bank_v1']['jobs']
        jobs.recover();service.goal_followup.recover(jobs)
        self.assertEqual(service.goal_followup.state()['check']['state'],'INTERRUPTED')
        self.assertEqual(len(service.goal_tools.drafts()),1)
        self.assertEqual(service.bank.state()['accounts'],self.bank.state()['accounts'])
        service.goal_followup.enqueue(jobs);self.assertEqual(len(jobs.recent()),1)
        self.assertEqual(jobs.recent()[0]['status'],'INTERRUPTED')

    def test_g1_confirm_and_link_recovery_still_bound_to_original(self):
        goals=self.service.goals
        assessment=goals.action('compare',{'expected_revision':1})
        choice=goals.action('select',{'expected_revision':1,'assessment_id':assessment['assessment_id'],
            'option_id':'reduced','wait_for_income':False,'idempotency_key':'g2-choice'})
        prepared=goals.action('prepare',{'choice_id':choice['choice_id']})
        op=self.bank.call_tool('operation',{'operation_id':prepared['operation_id']})['data']['operation']
        self.bank.user_action('confirm',{'operation_id':op['operation_id'],'expected_summary':op['summary']})
        self.assertEqual(self.fresh()['data']['status'],'LINK_PENDING')
        goals.action('reconcile',{'choice_id':choice['choice_id']})
        self.assertEqual(self.fresh()['data']['status'],'READY')
        goals.action('reconcile',{'choice_id':choice['choice_id']})
        self.assertEqual(len(self.bank.state()['transactions']),2)

    def test_budget_forwarded_on_each_runner_attempt_without_invocation(self):
        runner=DshRunner(self.root,self.root,'http://127.0.0.1:1','test-only-token-12345',sandbox=True,
                         batch_budget=self.root/'budget.sqlite3')
        class Process:
            returncode=0
            def communicate(self,*args,**kwargs):return (b'{"ok":true,"text":"offline"}',b'')
        with patch('src.bank_v1_web.worker.subprocess.Popen',return_value=Process()) as start:
            runner._run_model('one',None);runner._run_model('repair',None,readonly=True)
        self.assertEqual(len(start.call_args_list),2)
        for call in start.call_args_list:
            self.assertEqual(call.kwargs['env']['BANK_V1_BATCH_BUDGET'],str(self.root/'budget.sqlite3'))


if __name__=='__main__':unittest.main()
