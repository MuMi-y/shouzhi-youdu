"""Current pump schedule semantics and bank-commit/task-recovery process boundary."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.request import Request, build_opener, HTTPCookieProcessor
import hashlib
import gc
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.competition_acceptance_gaps import Gaps
from src.bank_insights.fixture import create_demo
from src.bank_v1 import Service
from src.competition_web.service import CompetitionService
from src.competition_web.pump import CompetitionPump
from src.bank_v1_web.jobs import JobStore

OUT=ROOT/'management/evidence/COMPETITION_V1/independent'
RESULTS={}


class Continuity(Gaps):
    def tearDown(self):
        # Test cleanup only: frozen CompetitionPump leaves SQLite connections to GC.
        # Preserve initial WinError32 evidence; this does not fix production resource ownership.
        gc.collect()
        super().tearDown()

    def test_ACT03_pump_daily_weekly_disabled_ignored(self):
        bank=create_demo(self.path/'periodic.db',clock=lambda:self.now)
        facade=CompetitionService(self.path/'facade',bank=bank,clock=lambda:self.now)
        jobs=JobStore(self.path/'jobs.sqlite3',clock=lambda:self.now)
        pump=CompetitionPump(facade,jobs,clock=lambda:self.now)
        snapshots=[]
        def step(label):
            pump.step();state=facade.insights.state()
            with closing(sqlite3.connect(facade.insights.db_path)) as db:
                checked=db.execute("SELECT value FROM metadata WHERE key='last_check'").fetchone()[0]
            result={'step':label,'business_date':self.now.date().isoformat(),'last_check':checked,
                'last_schedule_date':jobs.setting('last_insight_date'),
                'notification_count':len(state['notifications']),
                'active_count':sum(n['status']=='active' for n in state['notifications'])}
            snapshots.append(result);return result
        first=step('first_daily_unconfirmed')
        self.assertEqual(0,first['notification_count'])
        self.now+=timedelta(hours=1)
        same=step('same_day_no_change');self.assertEqual(first['last_check'],same['last_check'])
        self.now+=timedelta(days=1)
        day=step('next_day_daily');self.assertNotEqual(first['last_check'],day['last_check'])
        self.assertEqual(0,day['notification_count'])
        item=next(c for c in facade.insights.state()['candidates'] if '云存储' in c['label'])
        def feedback(candidate,decision):
            return facade.insights.user_action('candidate_feedback',dict(candidate_id=candidate['candidate_id'],expected_version=candidate['version'],decision=decision))['data']['candidate']
        item=feedback(item,'confirm');created=facade.insights.check()['data']
        self.assertEqual(1,len(created['new_notifications']))
        feedback(item,'ignore')
        jobs.set_setting('schedule',{'enabled':True,'cadence':'weekly'})
        self.now+=timedelta(days=6)
        six=step('weekly_before_seven_days');self.assertEqual(day['last_schedule_date'],six['last_schedule_date'])
        self.now+=timedelta(days=1)
        weekly=step('weekly_seventh_day');self.assertEqual(self.now.date().isoformat(),weekly['last_schedule_date'])
        self.assertEqual((1,0),(weekly['notification_count'],weekly['active_count']))
        jobs.set_setting('schedule',{'enabled':False,'cadence':'weekly'})
        self.now+=timedelta(days=8)
        paused=step('disabled_unchanged');self.assertEqual(weekly['last_check'],paused['last_check'])
        bank.user_action('inject_transaction',dict(kind='expense',amount_minor=1,idempotency_key='changed-while-disabled',description='虚构一次性消费'))
        changed=step('disabled_with_new_bank_event');self.assertNotEqual(paused['last_check'],changed['last_check'])
        self.assertEqual((1,0),(changed['notification_count'],changed['active_count']))
        self.assertEqual('ignored',next(c for c in facade.insights.state()['candidates'] if c['candidate_id']==item['candidate_id'])['status'])
        self.assertEqual([],facade.insights.check()['data']['new_notifications'])
        RESULTS['ACT03']={'snapshots':snapshots,'repeat_or_ignored_new_notifications':0,
            'scope':'Actual CompetitionPump.step with controlled clock, not real-time sleeping or a model. Disabled schedule suppresses periodic scans; changed bank facts still trigger a scan by existing design.'}

    def test_REC03_http_bank_commit_process_exit_then_task_resume(self):
        runtime=self.path/'crash';runtime.mkdir()
        with closing(socket.socket()) as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        base=f'http://127.0.0.1:{port}'
        env=dict(os.environ,PYTHONPATH=str(ROOT),PYTHONDONTWRITEBYTECODE='1')
        for key in ('ARK_AGENT_PLAN_API_KEY','ARK_CODING_PLAN_API_KEY','DSH_PACKAGE_PATH','DSH_CONFIG_HOME','BANK_V1_TOOL_TOKEN'):env.pop(key,None)
        # The first process deliberately holds task polling at the bank/task commit gap.
        # Actual competition app and HTTP confirm execute; no production source is patched.
        child_code='''from pathlib import Path
from datetime import datetime,timezone,timedelta
import os,sys
from flask import request
from waitress import create_server
from src.bank_insights.fixture import create_demo
from src.bank_v1.security import SecurityPolicy,TotpPasswordVerifier,enroll
from src.competition_web.app import create_app
p=Path(sys.argv[1]);clock=lambda:datetime.now(timezone(timedelta(hours=8)))
create_demo(p/'bank.sqlite3',clock=clock)
enroll(p/'identity.sqlite3','Only-Synthetic-Acceptance-2026','GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ',provenance='TEST_FIXTURE')
app=create_app(p,clock=clock,security_policy=SecurityPolicy('independent',TotpPasswordVerifier(p/'identity.sqlite3')))
@app.after_request
def crash_after_commit(response):
    if request.path=='/api/actions/confirm' and response.status_code==200:os._exit(41)
    if request.path=='/api/tools/submit_task_plan':app.extensions['competition'].advance_tasks()
    return response
create_server(app,host='127.0.0.1',port=int(sys.argv[2]),threads=4).run()
'''
        client=build_opener(HTTPCookieProcessor(CookieJar()));process=None
        log=(self.path/'crash.log').open('w',encoding='utf-8')
        def request(path,body=None,headers=None):
            req=Request(base+path,data=None if body is None else json.dumps(body).encode(),headers={'Content-Type':'application/json',**(headers or {})})
            with client.open(req,timeout=5) as response:return json.load(response)
        def ready():
            for _ in range(80):
                if process.poll() is not None:raise RuntimeError('server exited early')
                try:
                    if request('/api/health')['ok']:return
                except OSError:pass
                time.sleep(.1)
            raise RuntimeError('health timeout')
        def ledger():
            with closing(sqlite3.connect((runtime/'bank.sqlite3').as_uri()+'?mode=ro',uri=True)) as db:
                return db.execute('SELECT * FROM transactions ORDER BY transaction_id').fetchall()
        try:
            process=subprocess.Popen([sys.executable,'-c',child_code,str(runtime),str(port)],cwd=ROOT,env=env,stdout=log,stderr=log,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0));ready()
            state=request('/api/state');trusted={'Origin':base,'X-CSRF-Token':state['csrf_token']}
            auth={'Authorization':'Bearer '+(runtime/'.tool-token').read_text(encoding='ascii')}
            task=request('/api/tools/submit_task_plan',dict(prompt='确认后进程退出验收',title='唯一转账',nodes=[dict(id='pay',tool='prepare_transfer',args=dict(source_account='A',target_account='B',amount_minor=10000),depends_on=[])],idempotency_key='crash-task'),auth)['data']['task']
            waiting=request('/api/competition/tasks/'+task['task_id']);self.assertEqual('WAITING_CONFIRMATION',waiting['state'])
            op=waiting['nodes'][0]['result']['data']['operation'];before=ledger()
            try:request('/api/actions/confirm',dict(operation_id=op['operation_id'],expected_summary=op['summary']),trusted)
            except (OSError,ValueError):pass
            process.wait(timeout=15);self.assertEqual(41,process.returncode);process=None;time.sleep(.5)
            with closing(sqlite3.connect(runtime/'tasks.sqlite3')) as db:
                persisted=json.loads(db.execute('SELECT document FROM tasks WHERE task_id=?',(task['task_id'],)).fetchone()[0])
            self.assertEqual('WAITING_CONFIRMATION',persisted['state'])
            with closing(sqlite3.connect(runtime/'bank.sqlite3')) as db:
                status,receipt=db.execute('SELECT status,receipt_json FROM operations WHERE operation_id=?',(op['operation_id'],)).fetchone()
            self.assertEqual('SUCCEEDED',status);receipt=json.loads(receipt);posted=ledger();self.assertEqual(len(before)+2,len(posted))
            process=subprocess.Popen([sys.executable,'-X','utf8',str(ROOT/'scripts/competition_web.py'),'--no-model','--without-history','--port',str(port),'--data-dir',str(runtime)],cwd=ROOT,env=env,stdout=log,stderr=log,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0));ready()
            for _ in range(60):
                recovered=request('/api/competition/tasks/'+task['task_id'])
                if recovered['state']=='SUCCEEDED':break
                time.sleep(.1)
            self.assertEqual('SUCCEEDED',recovered['state']);self.assertEqual(op['operation_id'],recovered['nodes'][0]['operation_id'])
            self.assertEqual(receipt,recovered['nodes'][0]['result']['data']['operation']['receipt'])
            self.assertEqual(posted,ledger())
            state=request('/api/state');trusted={'Origin':base,'X-CSRF-Token':state['csrf_token']}
            repeated=request('/api/actions/confirm',dict(operation_id=op['operation_id'],expected_summary=op['summary']),trusted)
            self.assertEqual(receipt,repeated['receipt']);self.assertEqual(posted,ledger())
            RESULTS['REC03']={'first_process_exit':41,'http_reply_lost':True,'persisted_task_at_crash':'WAITING_CONFIRMATION',
                'persisted_bank_at_crash':'SUCCEEDED','operation_id':op['operation_id'],'receipt_id':receipt['receipt_id'],
                'recovered_task':'SUCCEEDED','ledger_rows_before':len(before),'ledger_rows_after_commit':len(posted),
                'restart_and_repeat_no_extra_posting':True,'credential_provenance':'TEST_FIXTURE',
                'scope':'Actual competition HTTP bank confirmation; first test process deliberately omits automatic task polling after preparation and exits in after_request, deterministically exposing cross-database gap. Restart uses unmodified production launcher/pump.'}
        finally:
            if process and process.poll() is None:process.terminate();process.wait(10);time.sleep(.5)
            log.close()


if __name__=='__main__':
    suite=unittest.TestSuite(Continuity(name) for name in ('test_ACT03_pump_daily_weekly_disabled_ignored','test_REC03_http_bank_commit_process_exit_then_task_resume'))
    with (OUT/'continuity-tests.txt').open('w',encoding='utf-8') as stream:result=unittest.TextTestRunner(stream=stream,verbosity=2).run(suite)
    report={'run':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'cases':RESULTS,'model_calls':0,
        'source_hashes':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in ('src/competition_web/pump.py','src/competition_web/app.py','src/bank_tasks/service.py','src/bank_insights/service.py')}}
    (OUT/'continuity-result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'run':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'cases':list(RESULTS)}))
    sys.exit(not result.wasSuccessful())
