"""Real competition facade/HTTP/task integration, with independent new fake ledgers."""
from contextlib import closing
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from src.bank_insights.fixture import create_demo
from src.bank_tasks.validation import PREPARES
from src.bank_v1 import DomainError
from src.bank_v1_web.app import create_app as old_app
from src.competition_web.app import create_app
from src.competition_web.pump import CompetitionPump

ROOT=Path(__file__).resolve().parents[2]


class HistoryScopeStub:
    """Only scope dispatch is tested; this is explicitly not a MoneyData data test."""
    def __init__(self):self.calls=[]
    def tool_specs(self):
        return [{'name':'moneydata_overview','description':'Synthetic scope boundary stub',
                 'parameters':{'type':'object','properties':{},'additionalProperties':False}}]
    def call_tool(self,name,args):
        self.calls.append((name,args));return {'scope':'moneydata','data':{'synthetic':True}}


class CalculatorIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='competition-calculate-')
        self.directory=Path(self.temp.name)
        self.clock=lambda:datetime(2026,10,12,12,tzinfo=timezone(timedelta(hours=8)))
        self.bank=create_demo(self.directory/'bank.sqlite3',clock=self.clock)
        self.history=HistoryScopeStub()
        self.app=create_app(self.directory,clock=self.clock,bank=self.bank,history=self.history)
        self.client=self.app.test_client();self.facade=self.app.extensions['competition']
        config=self.app.extensions['bank_v1']
        self.bank_headers={'Authorization':'Bearer '+config['tool_token']}
        self.history_headers={'Authorization':'Bearer '+config['history_token']}
    def tearDown(self):self.temp.cleanup()
    def bank_snapshot(self):
        with closing(sqlite3.connect(self.bank.db_path)) as db:
            names=[r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            return {name:db.execute('SELECT * FROM "'+name.replace('"','""')+'" ORDER BY rowid').fetchall() for name in names}
    def intent_count(self):
        with closing(sqlite3.connect(self.facade.tasks.intent_path)) as db:
            return db.execute('SELECT count(*) FROM invocation_intents').fetchone()[0]
    def post(self,args,headers=None):
        return self.client.post('/api/tools/money_calculate',json=args,headers=headers or self.bank_headers)
    def test_http_result_and_bank_unchanged(self):
        before=self.bank_snapshot()
        response=self.post(dict(operation='add',left_minor=10,right_minor=20,currency='CNY'))
        self.assertEqual(200,response.status_code)
        calculation=response.json['data']['calculation']
        self.assertEqual('0.30',calculation['result']['amount'])
        self.assertFalse(calculation['financial_inputs_verified'])
        self.assertEqual(before,self.bank_snapshot());self.assertEqual(0,self.intent_count())
    def test_zero_denominator_and_invalid_type_have_domain_errors_without_writes(self):
        before=self.bank_snapshot()
        for args,code in [(dict(operation='percentage',left_minor=100,right_minor=0),'ZERO_DENOMINATOR'),
                          (dict(operation='add',left_minor=True,right_minor=1),'INVALID_ARGUMENTS'),
                          (dict(operation='add',left_minor=1,right_minor=1,expression='1+1'),'INVALID_ARGUMENTS')]:
            with self.subTest(code=code,args=args):
                response=self.post(args);self.assertEqual(409,response.status_code)
                self.assertEqual(code,response.json['error']['code'])
        self.assertEqual(before,self.bank_snapshot());self.assertEqual(0,self.intent_count())
    def test_two_tokens_keep_scope_isolation(self):
        bank_catalog=self.client.get('/api/tool-schema',headers=self.bank_headers).json
        history_catalog=self.client.get('/api/tool-schema',headers=self.history_headers).json
        self.assertIn('money_calculate',[s['name'] for s in bank_catalog['tools']])
        self.assertNotIn('money_calculate',[s['name'] for s in history_catalog['tools']])
        response=self.post(dict(operation='add',left_minor=1,right_minor=2),self.history_headers)
        self.assertEqual(403,response.status_code);self.assertEqual('WRONG_DATA_SCOPE',response.json['error']['code'])
        response=self.client.post('/api/tools/moneydata_overview',json={},headers=self.bank_headers)
        self.assertEqual(403,response.status_code);self.assertEqual([],self.history.calls)
        response=self.client.post('/api/tools/moneydata_overview',json={},headers=self.history_headers)
        self.assertEqual(200,response.status_code);self.assertEqual([('moneydata_overview',{})],self.history.calls)
    def test_old_bank_mode_does_not_publish_calculator(self):
        app=old_app(self.directory/'old-mode',clock=self.clock,service=self.bank)
        headers={'Authorization':'Bearer '+app.extensions['bank_v1']['tool_token']}
        catalog=app.test_client().get('/api/tool-schema',headers=headers).json
        self.assertNotIn('money_calculate',[s['name'] for s in catalog['tools']])
        self.assertNotIn('money_calculate',[s['name'] for s in self.bank.tool_specs()])
    def test_task_two_step_growth_uses_actual_scalar_reference_without_intents(self):
        before=self.bank_snapshot()
        nodes=[{'id':'delta','tool':'money_calculate','args':{'operation':'subtract','left_minor':12500,'right_minor':10000},'depends_on':[]},
               {'id':'ratio','tool':'money_calculate','args':{'operation':'percentage',
                 'left_minor':{'$ref':'delta.data.calculation.result.amount_minor'},'right_minor':10000,
                 'left_evidence_id':{'$ref':'delta.evidence_id'}},'depends_on':['delta']}]
        saved=self.facade.call_tool('submit_task_plan',{'prompt':'明确虚构数字：125与100的增长率','title':'两步增长率',
            'nodes':nodes,'idempotency_key':'calculator-task'})['data']['task']
        first=self.facade.tasks.tick(saved['task_id'])
        self.assertEqual('SUCCEEDED',first['nodes'][0]['state']);self.assertEqual('PENDING',first['nodes'][1]['state'])
        final=self.facade.tasks.tick(saved['task_id']);self.assertEqual('SUCCEEDED',final['state'])
        calculation=final['nodes'][1]['result']['data']['calculation']
        self.assertEqual(2500,calculation['inputs']['left_minor']);self.assertEqual('25.00',calculation['result']['percent'])
        self.assertEqual(final['nodes'][0]['result']['evidence_id'],calculation['inputs']['left_evidence_id'])
        self.assertTrue(all(n['operation_id'] is None for n in final['nodes']))
        self.assertNotIn('money_calculate',PREPARES)
        self.assertEqual(0,self.intent_count());self.assertEqual(before,self.bank_snapshot())
    def test_task_zero_denominator_fails_without_unknown_bank_intent(self):
        before=self.bank_snapshot()
        task=self.facade.tasks.submit('除数为0必须失败',{'title':'除数为0','nodes':[{'id':'ratio','tool':'money_calculate',
             'args':{'operation':'percentage','left_minor':1,'right_minor':0},'depends_on':[]}]},'zero-test')
        task=self.facade.tasks.tick(task['task_id'])
        self.assertEqual('FAILED',task['state']);self.assertEqual('ZERO_DENOMINATOR',task['nodes'][0]['error']['code'])
        self.assertEqual(0,self.intent_count());self.assertEqual(before,self.bank_snapshot())
    def test_real_dsh_schema_function_keeps_calculator_in_readonly_catalog(self):
        # Execute the actual schema function in a Node VM with a fixed local mock fetch.
        # No main(), model, credentials, profile, child process, or network is executed.
        script=r'''
const fs=require('node:fs'),vm=require('node:vm');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
const source=fs.readFileSync(input.source,'utf8');
const prefix=source.slice(0,source.indexOf('function prepareProfile'));
(async()=>{const results=[];for(const item of input.cases){
const context={require:require('node:module').createRequire(input.source),process:{env:{BANK_V1_DATA_SCOPE:item.catalog.scope,BANK_V1_READONLY:'1'}},AbortSignal,
fetch:async()=>({ok:true,text:async()=>JSON.stringify(item.catalog)})};
const schema=vm.runInNewContext(prefix+'\nschema',context);
results.push(await schema('http://127.0.0.1:1','synthetic-token-unused'));}
process.stdout.write(JSON.stringify(results));})().catch(e=>{process.stderr.write(String(e));process.exitCode=1});
'''
        catalogs=[self.client.get('/api/tool-schema',headers=headers).json for headers in (self.bank_headers,self.history_headers)]
        old={'scope':'bank','business_date':'2026-10-12','tools':self.bank.tool_specs()}
        run=subprocess.run(['node','-e',script],input=json.dumps({'source':str(ROOT/'scripts/dsh_v1.cjs'),
            'cases':[{'catalog':c} for c in catalogs+[old]]}),text=True,encoding='utf-8',capture_output=True,timeout=15,check=True)
        results=json.loads(run.stdout);bank_names={s['name'] for s in results[0]['tools']}
        self.assertIn('money_calculate',bank_names)
        self.assertFalse(any(n.startswith('prepare_') for n in bank_names))
        self.assertTrue(bank_names.isdisjoint({'submit_task_plan','replan_task','check_insights','propose_arrangement'}))
        self.assertNotIn('money_calculate',{s['name'] for s in results[1]['tools']})
        self.assertNotIn('money_calculate',{s['name'] for s in results[2]['tools']})
    def test_pump_closes_every_connection_without_gc(self):
        real_connect=sqlite3.connect;opened=[]
        class TrackedConnection(sqlite3.Connection):
            closed=False
            def close(self):
                super().close();self.closed=True
        def connect(*args,**kwargs):
            kwargs['factory']=TrackedConnection
            value=real_connect(*args,**kwargs);opened.append(value);return value
        pump=CompetitionPump(self.facade,self.app.extensions['bank_v1']['jobs'],clock=self.clock)
        with patch('src.competition_web.pump.sqlite3.connect',side_effect=connect):pump.step()
        self.assertTrue(opened)
        self.assertTrue(all(item.closed for item in opened),'A connection escaped explicit closure')
        for connection in opened:
            with self.assertRaises(sqlite3.ProgrammingError):connection.execute('SELECT 1')


if __name__=='__main__':unittest.main(verbosity=2)
