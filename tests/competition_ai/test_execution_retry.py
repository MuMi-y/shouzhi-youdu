"""Fresh zero-tool execution retry: no models or existing user databases."""
from copy import deepcopy
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from src.bank_v1 import Service
from src.bank_v1_web.bank_answer import validate_bank_answer
from src.bank_v1_web.bank_execution_retry import BANK_TOOLS, execution_retry_prompt
from src.bank_v1_web.worker import DshRunner


def raw():
    names=sorted(BANK_TOOLS)
    return {'ok':True,'exit_code':0,'preset':'bank-v1','provider':'fixture','model':'fixture',
        'session_id':'new-first-session','text':'银行代扣查询卡已准备，请到可信页面确认。',
        'trace':[{'kind':'registered','allowed':names},
                 {'kind':'surface','tools':list(names),'preset':'bank-v1','provider':'fixture','model':'fixture'}]}


def validation():
    return validate_bank_answer(raw(),'请准备查询代扣')['answer_validation']


def decision(result=None, value=None, **kwargs):
    options={'session_id':None,'readonly':False,'data_scope':'bank','remaining_seconds':100}
    options.update(kwargs)
    return execution_retry_prompt(raw() if result is None else result,
        validation() if value is None else value,'请准备查询代扣',**options)


class ExecutionEligibilityTests(unittest.TestCase):
    def denied(self,result=None,value=None,**kwargs):
        self.assertIsNone(decision(result,value,**kwargs)[0])

    def test_current_matcher_catalog_is_compatible(self):
        self.assertIn('match_mandate_from_query',BANK_TOOLS)
        self.assertIsNotNone(decision()[0])

    def test_actual_normal_shape_enters(self):
        self.assertEqual(decision()[1],'NO_TOOL_EXECUTION_RETRY')
        self.assertIsNotNone(decision()[0])

    def test_original_request_only_not_bad_draft_or_diagnostic(self):
        r=raw();r['text']='错误草稿99999元；请自行确认';v=validation()
        v['reasons'][0]['clause']='错误细节：虚构ID'
        prompt,_=decision(r,v)
        self.assertEqual(json.loads(prompt.rsplit('\n',1)[1]),{'original_user_request':'请准备查询代扣'})
        self.assertNotIn('99999',prompt);self.assertNotIn('虚构ID',prompt)

    def test_task_save_only_is_supported(self):
        v=validation();v['reasons']=[{'code':'UNBACKED_TASK_SAVE'}]
        self.assertIsNotNone(decision(value=v)[0])

    def test_combined_prepare_and_save_is_supported(self):
        v=validation();v['reasons'].append({'code':'UNBACKED_TASK_SAVE'})
        self.assertIsNotNone(decision(value=v)[0])

    def test_resumed_session_blocked(self):
        for value in ['bank-old',' ',False,0]:
            with self.subTest(value=value):self.denied(session_id=value)

    def test_empty_session_compatible(self):
        self.assertIsNotNone(decision(session_id='')[0])

    def test_readonly_or_nonbank_blocked(self):
        for options in [{'readonly':True},{'readonly':0},{'data_scope':'moneydata'},{'data_scope':None}]:
            with self.subTest(options=options):self.denied(**options)

    def test_model_must_complete_normally(self):
        for changes in [{'ok':False},{'ok':1},{'exit_code':1},{'exit_code':False},
                        {'exit_code':None},{'preset':'other'},{'error':{'code':'AccountQuotaExceeded'}},
                        {'process_cleanup_pending':True}]:
            with self.subTest(changes=changes):
                r=raw();r.update(changes);self.denied(r)

    def test_other_gate_status_or_scope_blocked(self):
        for key,value in [('status','ACCEPTED'),('status','MODEL_FAILED'),('scope','moneydata'),('gate_applied',False)]:
            v=validation();v[key]=value;self.denied(value=v)

    def test_any_other_reason_blocks(self):
        for code in ['UNBACKED_OPERATION_SUCCESS','UNSUPPORTED_DISPLAYED_AMOUNT',
                     'UNSUPPORTED_DISPLAYED_PERCENT','UNBACKED_TASK_COMPLETION','OPERATION_NOT_PENDING','FUTURE']:
            with self.subTest(code=code):
                v=validation();v['reasons'].append({'code':code});self.denied(value=v)

    def test_missing_or_malformed_reasons_blocked(self):
        for reasons in [None,[],[None],[{}],'UNBACKED_TASK_SAVE']:
            v=validation();v['reasons']=reasons;self.denied(value=v)

    def test_entity_or_numeric_evidence_blocks(self):
        for key,value in [('response_count',1),('response_count',False),('operation_ids',['op']),
                          ('task_ids',['task']),('numeric_diagnostics',[{'code':'amount'}])]:
            v=validation();v[key]=value;self.denied(value=v)

    def test_missing_empty_evidence_fields_not_assumed(self):
        for key in ['response_count','operation_ids','task_ids','numeric_diagnostics']:
            v=validation();v.pop(key);self.denied(value=v)

    def test_budget_boundary_and_invalid_numbers(self):
        self.assertIsNotNone(decision(remaining_seconds=60)[0])
        for value in [59.999,0,-1,float('nan'),float('inf'),True,'100']:
            with self.subTest(value=value):self.denied(remaining_seconds=value)

    def test_missing_partial_or_extra_trace_blocked(self):
        for trace in [None,[],raw()['trace'][:1],raw()['trace'][1:],raw()['trace']*2,[None,None],{}]:
            r=raw();r['trace']=trace;self.denied(r)

    def test_any_tool_attempt_even_guard_denied_blocked(self):
        for record in [{'kind':'guard','name':'prepare_transfer','denied':True},
                       {'kind':'guard','name':'operation','denied':False},
                       {'kind':'request','name':'accounts'},
                       {'kind':'response','name':'prepare_transfer','ok':False},
                       {'kind':'unknown'},{'kind':'registered','allowed':[]}]:
            r=raw();r['trace'].append(record);self.denied(r)

    def test_trace_order_is_strict(self):
        r=raw();r['trace'].reverse();self.denied(r)

    def test_mismatched_empty_duplicate_or_unknown_surface_blocked(self):
        for names in [[],['accounts','accounts'],['confirm'],['accounts',1]]:
            r=raw();r['trace'][0]['allowed']=names;r['trace'][1]['tools']=names;self.denied(r)
        r=raw();r['trace'][1]['tools']=sorted(BANK_TOOLS)[:-1];self.denied(r)

    def test_surface_model_and_preset_consistent(self):
        for key in ['preset','provider','model']:
            r=raw();r['trace'][1][key]='different';self.denied(r)
        r=raw();r['provider']='';r['trace'][1]['provider']='';self.denied(r)

    def test_matching_execution_capability_required(self):
        r=raw();r['trace'][0]['allowed']=['accounts'];r['trace'][1]['tools']=['accounts'];self.denied(r)
        r=raw();names=sorted(BANK_TOOLS-{'submit_task_plan'})
        r['trace'][0]['allowed']=names;r['trace'][1]['tools']=names
        v=validation();v['reasons']=[{'code':'UNBACKED_TASK_SAVE'}];self.denied(r,v)

    def test_large_or_empty_original_prompt_is_rejected(self):
        for prompt in ['', 'x'*24000]:
            self.assertIsNone(execution_retry_prompt(raw(),validation(),prompt,
                session_id=None,readonly=False,data_scope='bank',remaining_seconds=100)[0])


class ExecutionRunnerTests(unittest.TestCase):
    def runner(self, second, first=None):
        r=DshRunner('.', '.', 'http://127.0.0.1:1','unused',timeout=210)
        r._run_model=Mock(side_effect=[raw() if first is None else first,second])
        return r

    def test_one_new_nonreadonly_session_remaining_budget(self):
        r=self.runner({'ok':True,'text':'请说明要查询的账户范围。','trace':[]})
        with patch('src.bank_v1_web.worker.time.monotonic',side_effect=[10,70]):
            result=r('请准备查询代扣',None)
        self.assertTrue(result['ok']);self.assertEqual(r._run_model.call_count,2)
        args,kwargs=r._run_model.call_args_list[1]
        self.assertIsNone(args[1]);self.assertEqual(kwargs,{'readonly':False,'data_scope':'bank','timeout':150})
        self.assertEqual(result['answer_retry']['mode'],'ORIGINAL_REQUEST_TOOL_EXECUTION')
        self.assertFalse(result['answer_retry']['initial_result']['ok'])
        self.assertNotIn('answer_repair',result)

    def test_second_preparation_hallucination_no_third_round(self):
        r=self.runner(raw());result=r('请准备查询代扣',None)
        self.assertFalse(result['ok']);self.assertEqual(r._run_model.call_count,2)
        self.assertNotIn('answer_repair',result)

    def test_second_numeric_error_does_not_chain_readonly_repair(self):
        r=self.runner({'ok':True,'text':'余额99999.00元。','trace':[]})
        result=r('请准备查询代扣',None)
        self.assertFalse(result['ok']);self.assertEqual(r._run_model.call_count,2)
        self.assertNotIn('answer_repair',result)

    def test_second_model_failure_preserves_initial(self):
        r=self.runner({'ok':False,'error':{'code':'AccountQuotaExceeded'},'trace':[]})
        result=r('请准备查询代扣',None)
        self.assertFalse(result['ok']);self.assertEqual(r._run_model.call_count,2)
        self.assertEqual(result['error']['code'],'AccountQuotaExceeded')
        self.assertIn('initial_result',result['answer_retry'])

    def test_second_startup_exception_redacted_and_kept(self):
        r=self.runner(OSError('fixture-sensitive-error'))
        result=r('请准备查询代扣',None)
        self.assertFalse(result['ok']);self.assertEqual(r._run_model.call_count,2)
        self.assertEqual(result['error']['code'],'EXECUTION_RETRY_PROCESS_FAILED')
        self.assertNotIn('fixture-sensitive-error',json.dumps(result))

    def test_first_failed_model_never_enters(self):
        r=self.runner(raw(),{'ok':False,'error':'timeout'})
        self.assertFalse(r('请准备查询代扣',None)['ok'])
        self.assertEqual(r._run_model.call_count,1)

    def test_insufficient_budget_never_second_call(self):
        r=self.runner(raw())
        with patch('src.bank_v1_web.worker.time.monotonic',side_effect=[0,151]):
            result=r('请准备查询代扣',None)
        self.assertFalse(result['ok']);self.assertEqual(r._run_model.call_count,1)
        self.assertEqual(result['answer_retry']['reason'],'INSUFFICIENT_EXECUTION_BUDGET')

    def test_accepted_clarification_no_retry(self):
        r=self.runner(raw(),{**raw(),'text':'您需要查询哪一项代扣？'})
        self.assertTrue(r('请准备查询代扣',None)['ok'])
        self.assertEqual(r._run_model.call_count,1)

    def test_resumed_or_readonly_never_execution_retry(self):
        for session,readonly in [('bank-old',False),(None,True)]:
            r=self.runner(raw());result=r('请准备查询代扣',session,readonly=readonly)
            self.assertFalse(result['ok']);self.assertEqual(r._run_model.call_count,1)

    def test_real_isolated_prepare_once_no_money_movement(self):
        with tempfile.TemporaryDirectory() as d:
            bank=Service(Path(d)/'bank.db',clock=lambda:datetime(2026,10,12,tzinfo=timezone.utc))
            before=bank.call_tool('accounts',{})['data']
            def prepare(prompt,session,**kwargs):
                self.assertIsNone(session);self.assertIs(kwargs['readonly'],False)
                response=bank.call_tool('prepare_transfer',{'source_account':'A','target_account':'B',
                    'amount_minor':50000,'idempotency_key':'isolated-retry-transfer'})
                return {'ok':True,'text':'转账500.00元待您确认。','trace':[
                    {'kind':'response','tool':'prepare_transfer','ok':True,'response':response}]}
            r=self.runner(None);calls=[0]
            def model(prompt,session,**kwargs):
                calls[0]+=1
                return raw() if calls[0]==1 else prepare(prompt,session,**kwargs)
            r._run_model=Mock(side_effect=model)
            result=r('请准备A转500元到B，由我确认。',None)
            self.assertTrue(result['ok']);self.assertEqual(calls[0],2)
            self.assertEqual(before,bank.call_tool('accounts',{})['data'])
            with closing(sqlite3.connect(bank.db_path)) as db:
                self.assertEqual(db.execute('SELECT kind,status FROM operations').fetchall(),[('transfer','PENDING')])
                self.assertEqual(db.execute('SELECT COUNT(*) FROM transactions WHERE operation_id IS NOT NULL').fetchone()[0],0)


    def test_real_preparation_then_bad_answer_keeps_pending_without_third_call(self):
        with tempfile.TemporaryDirectory() as d:
            bank=Service(Path(d)/'bank.db',clock=lambda:datetime(2026,10,12,tzinfo=timezone.utc))
            def prepare(prompt,session,**kwargs):
                response=bank.call_tool('prepare_mandate_query',{'idempotency_key':'isolated-retry-query'})
                return {'ok':True,'text':'查询卡已准备，余额99999.00元。','trace':[
                    {'kind':'response','tool':'prepare_mandate_query','ok':True,'response':response}]}
            calls=[0]
            def model(prompt,session,**kwargs):
                calls[0]+=1
                return raw() if calls[0]==1 else prepare(prompt,session,**kwargs)
            r=self.runner(None);r._run_model=Mock(side_effect=model)
            result=r('请准备查询代扣',None)
            self.assertFalse(result['ok']);self.assertEqual(calls[0],2)
            self.assertNotIn('answer_repair',result)
            self.assertEqual(result['trace'][0]['response']['data']['operation']['status'],'PENDING')
            with closing(sqlite3.connect(bank.db_path)) as db:
                self.assertEqual(db.execute('SELECT kind,status FROM operations').fetchall(),[('mandate_query','PENDING')])

    def test_second_tool_failure_no_third_call(self):
        second={'ok':True,'text':'银行代扣查询卡已准备。','trace':[{'kind':'response','tool':'prepare_mandate_query',
            'ok':False,'response':{'error':{'code':'LOCKED'}}}]}
        r=self.runner(second);result=r('请准备查询代扣',None)
        self.assertFalse(result['ok']);self.assertEqual(r._run_model.call_count,2)
        self.assertEqual(result['trace'],second['trace'])


if __name__=='__main__':unittest.main()
