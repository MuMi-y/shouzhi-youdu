"""Independent HTTP integration acceptance; new temp banks and TEST_FIXTURE MFA only."""
from contextlib import closing
from datetime import datetime, timedelta
import base64
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from src.bank_v1 import Service, DomainError
from src.bank_v1.security import SecurityPolicy, TotpPasswordVerifier, enroll, totp
from src.bank_insights.fixture import create_demo
from src.competition_web.app import create_app


class HTTPReview(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)
        self.now=datetime.fromisoformat('2026-10-12T12:00:00+08:00')
        self.password=secrets.token_urlsafe(24)
        self.secret=base64.b32encode(secrets.token_bytes(20)).decode()
        enroll(self.path/'auth.db',self.password,self.secret,provenance='TEST_FIXTURE')
        self.policy=SecurityPolicy(mode='independent',verifier=TotpPasswordVerifier(self.path/'auth.db'))
        create_demo(self.path/'bank.sqlite3',clock=lambda:self.now)
        self.bank=Service(self.path/'bank.sqlite3',clock=lambda:self.now,security_policy=self.policy)
        self.app=create_app(self.path,clock=lambda:self.now,bank=self.bank)
        self.app.config['TESTING']=True
        self.client=self.app.test_client()
        self.csrf=self.client.get('/api/state').get_json()['csrf_token']
        self.token=self.app.extensions['bank_v1']['tool_token']

    def user(self,path,body,**headers):
        return self.client.post(path,json=body,headers={'Origin':'http://localhost','X-CSRF-Token':self.csrf,**headers})

    def tool(self,name,args):
        return self.client.post('/api/tools/'+name,json=args,headers={'Authorization':'Bearer '+self.token})

    def prepare(self,amount=50000,key='pay'):
        response=self.tool('prepare_transfer',{'source_account':'A','target_account':'B','amount_minor':amount,'idempotency_key':key})
        self.assertEqual(200,response.status_code)
        return response.get_json()['data']['operation']

    def confirm(self,op,**extra):
        return self.user('/api/actions/confirm',{'operation_id':op['operation_id'],'expected_summary':op['summary'],**extra})

    def verify(self,op,**extra):
        return self.user('/api/competition/security/verify',{'operation_id':op['operation_id'],
            'expected_summary':op['summary'],'password':self.password,
            'otp':totp(self.secret,int(self.now.timestamp())//30),**extra})

    def assert_code(self,response,status,code):
        self.assertEqual(status,response.status_code)
        self.assertEqual(code,response.get_json()['error']['code'])

    def test_browser_origin_csrf_and_tool_token_cannot_confirm(self):
        op=self.prepare()
        body={'operation_id':op['operation_id'],'expected_summary':op['summary']}
        self.assert_code(self.client.post('/api/actions/confirm',json=body),403,'INVALID_ORIGIN')
        self.assert_code(self.client.post('/api/actions/confirm',json=body,headers={'Origin':'http://localhost'}),403,'INVALID_CSRF')
        self.assert_code(self.user('/api/actions/confirm',body,Authorization='Bearer '+self.token),403,'WRONG_AUTH_CHANNEL')
        self.assert_code(self.tool('confirm',body),409,'UNKNOWN_TOOL')
        self.assertEqual('PENDING',self.bank.call_tool('operation',{'operation_id':op['operation_id']})['data']['operation']['status'])

    def test_competition_post_routes_share_boundary(self):
        for path in ['/api/competition/insights-check','/api/competition/candidate-feedback',
                     '/api/competition/security/verify','/api/competition/security/unlock','/api/competition/tasks/none/pause']:
            with self.subTest(path=path):
                self.assert_code(self.client.post(path,json={}),403,'INVALID_ORIGIN')
                self.assert_code(self.user(path,{},Authorization='Bearer '+self.token),403,'WRONG_AUTH_CHANNEL')

    def test_green_reads_and_mandate_yellow_receipt(self):
        before=self.bank.state()['transactions']
        self.assertEqual(200,self.tool('accounts',{}).status_code)
        state=self.client.get('/api/state').get_json()
        self.assertNotIn('mandates',state)
        operation=self.tool('prepare_mandate_query',{'idempotency_key':'mandate-query'}).get_json()['data']['operation']
        self.assertEqual('PENDING',operation['status'])
        self.assertIsNone(operation['receipt'])
        response=self.confirm(operation)
        self.assertEqual(200,response.status_code)
        op=response.get_json()
        self.assertNotIn('data',op)
        self.assertEqual('SUCCEEDED',op['status'])
        self.assertEqual('M1',op['receipt']['mandates'][0]['mandate_id'])
        self.assertEqual(before,self.bank.state()['transactions'])

    def test_cumulative_boundary_and_real_independent_verification(self):
        first=self.prepare(100000,'first')
        self.assertEqual('YELLOW',first['required_verification'])
        self.assertEqual(200,self.confirm(first).status_code)
        second=self.prepare(1,'second')
        self.assertEqual('STRONG',second['required_verification'])
        before=self.bank.state()['transactions']
        self.assert_code(self.confirm(second),409,'STRONG_VERIFICATION_REQUIRED')
        self.assertEqual(before,self.bank.state()['transactions'])
        proof=self.verify(second)
        self.assertEqual(200,proof.status_code)
        value=proof.get_json()
        self.assertNotIn('data',value)
        self.assertIn('verification_token',value)
        self.assertEqual('TEST_FIXTURE',value['credential_provenance'])
        result=self.confirm(second,verification_token=value['verification_token'])
        self.assertEqual(200,result.status_code)
        self.assertEqual('STRONG',result.get_json()['receipt']['verification'])
        posted=self.bank.state()['transactions']
        self.assertEqual(200,self.confirm(second,verification_token=value['verification_token']).status_code)
        self.assertEqual(posted,self.bank.state()['transactions'])
        third=self.prepare(1,'third')
        self.assert_code(self.confirm(third,verification_token=value['verification_token']),409,'INVALID_VERIFICATION_GRANT')
        snapshot=json.dumps(self.client.get('/api/state').get_json(),ensure_ascii=False)
        self.assertNotIn(value['verification_token'],snapshot)
        self.assertNotIn(self.password,snapshot)
        self.assertNotIn(self.secret,snapshot)

    def test_three_bad_passwords_lock_restart_and_independent_unlock(self):
        op=self.prepare(100001)
        before=self.bank.state()['transactions']
        for i in range(3):self.assert_code(self.verify(op,password='deliberately-wrong'),409,'VERIFICATION_FAILED')
        state=self.client.get('/api/state').get_json()
        self.assertTrue(state['security']['locked'])
        self.assert_code(self.tool('prepare_transfer',{'source_account':'A','target_account':'B','amount_minor':1,'idempotency_key':'locked'}),409,'SECURITY_LOCKED')
        self.assertEqual(200,self.tool('accounts',{}).status_code)
        reopened=Service(self.path/'bank.sqlite3',clock=lambda:self.now)
        self.assertEqual('independent',reopened.state()['security']['mode'])
        self.assertTrue(reopened.state()['security']['locked'])
        with self.assertRaises(DomainError) as error:
            Service(self.path/'bank.sqlite3',clock=lambda:self.now,security_policy=SecurityPolicy(mode='simulation'))
        self.assertEqual('SECURITY_DOWNGRADE_DENIED',error.exception.code)
        response=self.user('/api/competition/security/unlock',{'password':self.password,
            'otp':totp(self.secret,int(self.now.timestamp())//30),'reason':'独立测试身份核对后解除'})
        self.assertEqual(200,response.status_code)
        self.assertFalse(response.get_json()['locked'])
        self.assertEqual(before,self.bank.state()['transactions'])
        self.now+=timedelta(seconds=31)
        proof=self.verify(op)
        self.assertEqual(200,proof.status_code)
        self.assertEqual(200,self.confirm(op,verification_token=proof.get_json()['verification_token']).status_code)

    def test_task_plan_http_wait_confirm_receipt_no_repeat(self):
        body={'prompt':'转500元到我的储备账户，确认后核对回执','title':'真实HTTP任务',
            'idempotency_key':'task-http','nodes':[
                {'id':'pay','tool':'prepare_transfer','args':{'source_account':'A','target_account':'B','amount_minor':50000},'depends_on':[]},
                {'id':'receipt','tool':'operation','args':{'operation_id':{'$ref':'pay.data.operation.operation_id'}},'depends_on':['pay']}]}
        before=self.bank.state()['transactions']
        response=self.tool('submit_task_plan',body)
        self.assertEqual(200,response.status_code)
        task=response.get_json()['data']['task'];facade=self.app.extensions['competition']
        facade.advance_tasks()
        state=self.client.get('/api/competition/tasks/'+task['task_id']).get_json()
        self.assertEqual('WAITING_CONFIRMATION',state['state'])
        self.assertEqual(before,self.bank.state()['transactions'])
        operation=state['nodes'][0]['result']['data']['operation']
        self.assertEqual(200,self.confirm(operation).status_code)
        facade.advance_tasks();facade.advance_tasks()
        state=self.tool('task_status',{'task_id':task['task_id']}).get_json()['data']['task']
        self.assertEqual('SUCCEEDED',state['state'])
        self.assertEqual(operation['operation_id'],state['nodes'][1]['result']['data']['operation']['operation_id'])
        after=self.bank.state()['transactions']
        facade.advance_tasks();self.tool('submit_task_plan',body)
        self.assertEqual(after,self.bank.state()['transactions'])

    def test_candidate_correction_http_and_stale_version(self):
        response=self.user('/api/competition/insights-check',{})
        self.assertEqual(200,response.status_code)
        candidate=next(c for c in response.get_json()['data']['candidates'] if c['label']=='虚构云存储代扣')
        body={'candidate_id':candidate['candidate_id'],'expected_version':candidate['version'],
            'decision':'correct','amount_minor':4500,'next_due_date':'2026-10-30'}
        before=self.bank.state()['transactions']
        response=self.user('/api/competition/candidate-feedback',body)
        self.assertEqual(200,response.status_code)
        self.assertEqual(4500,response.get_json()['data']['candidate']['amount_minor'])
        self.assert_code(self.user('/api/competition/candidate-feedback',body),409,'STALE_VERSION')
        self.assertEqual(before,self.bank.state()['transactions'])

    def test_security_secrets_absent_from_audit(self):
        op=self.prepare(100001)
        grant=self.verify(op).get_json()['verification_token']
        self.confirm(op,verification_token=grant)
        with closing(sqlite3.connect(self.bank.db_path)) as db:
            recorded=' '.join(str(r) for table in ('audit','security_audit') for r in db.execute('SELECT * FROM '+table))
        for secret in (self.password,self.secret,grant):self.assertNotIn(secret,recorded)


if __name__=='__main__':
    output=ROOT/'management/evidence/COMPETITION_V1/independent'
    output.mkdir(parents=True,exist_ok=True)
    with (output/'http-tests.txt').open('w',encoding='utf-8') as stream:
        result=unittest.TextTestRunner(stream=stream,verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(HTTPReview))
    files=[ROOT/'src/competition_web/app.py',ROOT/'src/competition_web/service.py',ROOT/'src/bank_v1/security.py',ROOT/'src/bank_v1/operations.py',ROOT/'src/bank_v1_web/app.py']
    summary={'run':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),
        'credential_provenance':'TEST_FIXTURE','model_calls':0,
        'source_hashes':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}}
    (output/'http-result.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
    print((output/'http-tests.txt').read_text(encoding='utf-8'))
    sys.exit(not result.wasSuccessful())
