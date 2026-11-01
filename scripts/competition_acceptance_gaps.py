"""Independent frozen-case gap checks. Only new temporary banks and fixture factors."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.request import Request, build_opener, HTTPCookieProcessor
from urllib.error import HTTPError
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.bank_v1 import Service, DomainError
from src.bank_v1.security import SecurityPolicy, TotpPasswordVerifier, enroll, totp
from src.bank_insights import InsightService
from src.bank_insights.fixture import create_demo

EVIDENCE = ROOT/'management/evidence/COMPETITION_V1/independent'
RESULTS = {}
PASSWORD = 'Only-Synthetic-Acceptance-2026'
SECRET = 'GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ'  # RFC6238 public test vector


class Gaps(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='competition-gaps-')
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.now = datetime.fromisoformat('2026-10-12T12:00:00+08:00')
        enroll(self.path/'identity.sqlite3', PASSWORD, SECRET, provenance='TEST_FIXTURE')
        self.policy = SecurityPolicy('independent', TotpPasswordVerifier(self.path/'identity.sqlite3'))
        self.bank = Service(self.path/'bank.sqlite3', clock=lambda:self.now, security_policy=self.policy)

    def tool(self, name, **args):
        return self.bank.call_tool(name, args)['data']

    def transfer(self, amount, key):
        return self.tool('prepare_transfer', source_account='A', target_account='B', amount_minor=amount, idempotency_key=key)['operation']

    def confirm(self, operation, **extra):
        return self.bank.user_action('confirm', dict(operation_id=operation['operation_id'], expected_summary=operation['summary'], **extra))

    def ledger(self):
        value = self.bank.state()
        return {key:value[key] for key in ('accounts','transactions','arrangements','mandates')}

    def test_SEC06_exact_600_plus_500(self):
        first = self.confirm(self.transfer(60000,'first'))
        second = self.transfer(50000,'second')
        before = self.ledger()
        with self.assertRaises(DomainError) as error: self.confirm(second)
        self.assertEqual('STRONG_VERIFICATION_REQUIRED', error.exception.code)
        self.assertEqual(before, self.ledger())
        grant = self.bank.user_action('verify_operation', dict(operation_id=second['operation_id'], expected_summary=second['summary'], password=PASSWORD, otp=totp(SECRET,int(self.now.timestamp())//30)))
        result = self.confirm(second, verification_token=grant['verification_token'])
        self.assertEqual('YELLOW',first['receipt']['verification'])
        self.assertEqual('STRONG',result['receipt']['verification'])
        self.assertEqual([390000,210000],[a['balance_minor'] for a in self.bank.state()['accounts']])
        RESULTS['SEC06'] = {'amounts_minor':[60000,50000], 'without_factor':error.exception.code,
            'without_factor_ledger_unchanged':True, 'verification_levels':['YELLOW','STRONG'],
            'final_balances_minor':[390000,210000], 'total_minor':600000}

    def test_REC08_daily_limit_resets_at_business_midnight(self):
        self.confirm(self.transfer(60000,'day-one'))
        same = self.transfer(50000,'same-day')
        self.assertEqual('STRONG',same['required_verification'])
        self.bank.user_action('reject', {'operation_id':same['operation_id']})
        self.now = datetime.fromisoformat('2026-10-13T00:00:01+08:00')
        self.bank = Service(self.path/'bank.sqlite3', clock=lambda:self.now, security_policy=self.policy)
        second = self.transfer(50000,'day-two')
        self.assertEqual('YELLOW',second['required_verification'])
        done = self.confirm(second)
        self.assertEqual('YELLOW', done['receipt']['verification'])
        event = self.bank.user_action('inject_transaction',dict(kind='income',amount_minor=100,idempotency_key='stale'))
        refs = self.tool('event_context',event_id=event['event_id'])['forecast']['evidence_refs']
        self.now += timedelta(days=1)
        before = self.ledger()
        with self.assertRaises(DomainError) as error:
            self.bank.finish_event(event['event_id'],'yesterday',expected_evidence_refs=refs)
        self.assertEqual('STALE_ANALYSIS',error.exception.code)
        self.assertEqual(before,self.ledger())
        RESULTS['REC08'] = {'day_one_minor':60000,'same_day_second_required':'STRONG',
            'day_two_minor':50000,'day_two_required':'YELLOW','day_two_receipt':'YELLOW',
            'late_analysis_code':error.exception.code,'late_analysis_ledger_unchanged':True}

    def test_ACT01_continuous_expense_refund_delay_income(self):
        def projection(): return self.bank.state()['forecast']['data']
        def snapshot(label):
            state=self.bank.state(); forecast=state['forecast']['data']
            return {'step':label,'balance_minor':sum(a['balance_minor'] for a in state['accounts']),
                'gap_minor':forecast['gap_minor'],'conservative_gap_minor':forecast['without_future_income_gap_minor'],
                'future_income_minor':sum(day['expected_income_minor'] for day in forecast['daily']),
                'notifications':[{key:n.get(key) for key in ('kind','message','gap_minor','without_future_income_gap_minor')} for n in state['notifications']]}
        steps=[snapshot('initial')]
        spend=self.bank.user_action('inject_transaction',dict(kind='expense',amount_minor=350000,idempotency_key='purchase'))
        self.bank.finish_event(spend['event_id'],'synthetic review')
        steps.append(snapshot('expense'))
        self.assertEqual((50000,250000),(projection()['gap_minor'],projection()['without_future_income_gap_minor']))
        refund=self.bank.user_action('inject_transaction',dict(kind='refund',amount_minor=50000,idempotency_key='refund',original_transaction_id=spend['transaction_id']))
        self.bank.finish_event(refund['event_id'],'synthetic review')
        steps.append(snapshot('partial_refund'))
        self.assertEqual((0,200000),(projection()['gap_minor'],projection()['without_future_income_gap_minor']))
        self.assertNotEqual('RISK_RESOLVED',self.bank.state()['notifications'][0]['kind'])
        change=self.tool('propose_arrangement',arrangement_id='salary',expected_version=1,title='预计收入延迟（虚构）',kind='income',amount_minor=250000,due_date='2026-11-20',idempotency_key='delay')['operation']
        self.confirm(change)
        for pending in self.bank.pending_events(): self.bank.finish_event(pending['event_id'],'synthetic event completion, no model')
        steps.append(snapshot('income_delayed'))
        self.assertEqual((200000,200000),(projection()['gap_minor'],projection()['without_future_income_gap_minor']))
        self.now=datetime.fromisoformat('2026-11-20T12:00:00+08:00')
        income=self.bank.user_action('inject_transaction',dict(kind='income',amount_minor=250000,idempotency_key='actual-income'))
        self.bank.finish_event(income['event_id'],'synthetic review')
        before=self.ledger()
        match=self.bank.user_action('prepare_match',dict(transaction_id=income['transaction_id'],arrangement_id='salary',expected_version=2,idempotency_key='match-income'))
        self.assertEqual(before,self.ledger())
        done=self.confirm(match)
        for pending in self.bank.pending_events(): self.bank.finish_event(pending['event_id'],'synthetic event completion, no model')
        steps.append(snapshot('actual_income_matched'))
        self.assertEqual(550000,steps[-1]['balance_minor'])
        self.assertEqual(0,steps[-1]['future_income_minor'])
        self.assertEqual(0,steps[-1]['gap_minor'])
        self.assertEqual('completed',next(a for a in self.bank.state()['arrangements'] if a['arrangement_id']=='salary')['status'])
        after=self.ledger()
        self.assertEqual(done,self.confirm(match))
        self.assertEqual(after,self.ledger())
        self.assertEqual(1,len(self.bank.state()['arrangement_matches']))
        RESULTS['ACT01']={'steps':steps,'match_receipt_status':done['status'],'matching_did_not_post_money':True,'repeat_confirm_did_not_post_money':True,'match_count':1}

    def test_ACT02_corrected_date_consumed_and_next_period_advanced(self):
        # A second clearly synthetic bank contains three existing monthly observations.
        path=self.path/'periodic.sqlite3'
        create_demo(path,clock=lambda:self.now)
        bank=Service(path,clock=lambda:self.now,security_policy=self.policy)
        insights=InsightService(self.path/'insights.sqlite3',bank=bank,clock=lambda:self.now)
        item=next(c for c in insights.check()['data']['candidates'] if '云存储' in c['label'])
        corrected=insights.user_action('candidate_feedback',dict(candidate_id=item['candidate_id'],expected_version=item['version'],decision='correct',next_due_date='2026-10-17'))['data']['candidate']
        self.now=datetime.fromisoformat('2026-10-17T12:00:00+08:00')
        tx=bank.user_action('inject_transaction',dict(kind='expense',amount_minor=3000,description='虚构云存储代扣',idempotency_key='new-month'))
        self.now=datetime.fromisoformat('2026-10-26T12:00:00+08:00')
        state=insights.check()['data']
        item=next(c for c in state['candidates'] if c['candidate_id']==corrected['candidate_id'])
        self.assertEqual('2026-11-17',item['next_due_date'])
        self.assertNotIn('next_due_date',item['corrections'])
        history=item['correction_history'][-1]
        self.assertEqual('2026-10-17',history['next_due_date'])
        self.assertEqual(tx['transaction_id'],history['observed_transaction_id'])
        self.assertFalse(any(n['kind']=='MISSED_EXPECTED_CHARGE' and n['status']=='active' for n in state['notifications']))
        self.assertEqual(item, next(c for c in InsightService(self.path/'insights.sqlite3',bank=bank,clock=lambda:self.now).state()['candidates'] if c['candidate_id']==item['candidate_id']))
        RESULTS['ACT02']={'next_due_date':item['next_due_date'],'date_override_removed':True,
            'correction_history':history,'actual_transaction_id':tx['transaction_id'],'false_missing_active':False,'restart_preserved':True}

    def test_REC07_whole_server_waiting_candidate_and_lock_restart(self):
        runtime=self.path/'server'; runtime.mkdir()
        now=lambda:datetime.now(timezone(timedelta(hours=8)))
        create_demo(runtime/'bank.sqlite3',clock=now)
        enroll(runtime/'identity.sqlite3',PASSWORD,SECRET,provenance='TEST_FIXTURE')
        with closing(socket.socket()) as sock: sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
        base=f'http://127.0.0.1:{port}'
        env=dict(os.environ,PYTHONPATH='',PYTHONDONTWRITEBYTECODE='1')
        for key in ('ARK_AGENT_PLAN_API_KEY','ARK_CODING_PLAN_API_KEY','DSH_PACKAGE_PATH','DSH_CONFIG_HOME','BANK_V1_TOOL_TOKEN'): env.pop(key,None)
        log=(self.path/'service.log').open('w',encoding='utf-8'); process=None
        client=build_opener(HTTPCookieProcessor(CookieJar()))
        def request(path,body=None,headers=None):
            req=Request(base+path,data=None if body is None else json.dumps(body).encode(),headers={'Content-Type':'application/json',**(headers or {})})
            try:
                with client.open(req,timeout=5) as response: return response.status,json.load(response)
            except HTTPError as error: return error.code,json.load(error)
        def start():
            child=subprocess.Popen([sys.executable,'-X','utf8',str(ROOT/'scripts/competition_web.py'),'--no-model','--without-history','--data-dir',str(runtime),'--port',str(port)],cwd=ROOT,env=env,stdout=log,stderr=log,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            for _ in range(80):
                if child.poll() is not None: raise RuntimeError('child exited')
                try:
                    if request('/api/health')[0]==200:return child
                except OSError: pass
                time.sleep(.1)
            child.terminate();child.wait(10);raise RuntimeError('health timeout')
        def capture(state,tid,cid):
            return {'accounts':state['accounts'],'transactions':state['transactions'],
                'operation':next(o for o in state['operations'] if o['operation_id']==operation['operation_id']),
                'candidate':next(c for c in state['competition']['insights']['data']['candidates'] if c['candidate_id']==cid),
                'task_state':request('/api/competition/tasks/'+tid)[1]['state'],
                'security':state['security'],'jobs':state['jobs']}
        try:
            process=start(); first_pid=process.pid
            state=request('/api/state')[1]; trusted={'Origin':base,'X-CSRF-Token':state['csrf_token']}
            token=(runtime/'.tool-token').read_text(encoding='ascii'); auth={'Authorization':'Bearer '+token}
            items=request('/api/competition/insights-check',{},trusted)[1]['data']['candidates']; candidate=items[0]
            request('/api/competition/candidate-feedback',dict(candidate_id=candidate['candidate_id'],expected_version=candidate['version'],decision='confirm'),trusted)
            submit=request('/api/tools/submit_task_plan',dict(prompt='虚构等待重启验收',title='重启保持待确认',nodes=[dict(id='pay',tool='prepare_transfer',args=dict(source_account='A',target_account='B',amount_minor=50000),depends_on=[])],idempotency_key='waiting'),auth)[1]['data']['task']
            tid=submit['task_id']
            for _ in range(50):
                task=request('/api/competition/tasks/'+tid)[1]
                if task['state']=='WAITING_CONFIRMATION':break
                time.sleep(.1)
            self.assertEqual('WAITING_CONFIRMATION',task['state'])
            operation=task['nodes'][0]['result']['data']['operation']
            for _ in range(3):
                code,response=request('/api/competition/security/verify',dict(operation_id=operation['operation_id'],expected_summary=operation['summary'],password='synthetic-wrong',otp='000000'),trusted)
                self.assertEqual('VERIFICATION_FAILED',response['error']['code'])
            before=capture(request('/api/state')[1],tid,candidate['candidate_id'])
            process.terminate();process.wait(10);process=None;time.sleep(.5)
            process=start(); second_pid=process.pid
            state=request('/api/state')[1]; after=capture(state,tid,candidate['candidate_id'])
            self.assertTrue(after['security']['locked'])
            self.assertEqual('independent',after['security']['mode'])
            self.assertEqual('WAITING_CONFIRMATION',after['task_state'])
            self.assertEqual('PENDING',after['operation']['status'])
            self.assertTrue(after['candidate']['confirmed'])
            equality={k:before[k]==after[k] for k in before}
            self.assertTrue(all(equality.values()),equality)
            trusted={'Origin':base,'X-CSRF-Token':state['csrf_token']}
            code,response=request('/api/actions/confirm',dict(operation_id=operation['operation_id'],expected_summary=operation['summary']),trusted)
            self.assertEqual('SECURITY_LOCKED',response['error']['code'])
            self.assertEqual(before['transactions'],request('/api/state')[1]['transactions'])
            RESULTS['REC07']={'launcher':'scripts/competition_web.py --no-model --without-history',
                'before_pid':first_pid,'after_pid':second_pid,'stopped_between':True,'snapshot_equal':equality,
                'task_id':tid,'operation_id':operation['operation_id'],'task_state':'WAITING_CONFIRMATION','operation_state':'PENDING',
                'locked':True,'candidate_confirmed':True,'post_restart_confirm':response['error']['code'],'model_calls':0}
        finally:
            if process and process.poll() is None:process.terminate();process.wait(10);time.sleep(.5)
            log.close()


if __name__=='__main__':
    EVIDENCE.mkdir(parents=True,exist_ok=True)
    with (EVIDENCE/'frozen-gap-tests.txt').open('w',encoding='utf-8') as stream:
        result=unittest.TextTestRunner(stream=stream,verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Gaps))
    report={'run':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),
        'credential_provenance':'TEST_FIXTURE','model_calls':0,'cases':RESULTS,
        'source_hashes':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for folder in ('src/bank_v1','src/bank_insights','src/competition_web') for p in (ROOT/folder).glob('*.py')}}
    (EVIDENCE/'frozen-gap-result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'run':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'recorded_cases':list(RESULTS)}))
    sys.exit(not result.wasSuccessful())
