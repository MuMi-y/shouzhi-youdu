"""Failure-driven tests for bounded repair and evidence/session isolation."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from src.bank_v1_web.bank_repair import repair_prompt
from src.bank_v1_web.worker import DshRunner


def response(text, amount=None, tool='accounts'):
    trace = [] if amount is None else [{'kind':'response', 'tool':tool, 'ok':True,
        'response':{'data':{'balance_minor':amount}}}]
    return {'ok':True,'text':text,'trace':trace,'session_id':'raw-session'}


class RepairTests(unittest.TestCase):
    def runner(self, results):
        runner = DshRunner('.', '.', 'http://127.0.0.1:1', 'unused', timeout=210)
        runner._run_model = Mock(side_effect=results)
        return runner

    def test_numeric_repair_is_new_readonly_session_with_original_retained(self):
        first = response('账户还剩1345.00元。',484500)
        second = response('工具计算剩余1345.00元。',134500)
        runner = self.runner([first,second])
        with patch('src.bank_v1_web.worker.time.monotonic',side_effect=[10,60]):
            result = runner('如果从A花3500元呢？','old')
        self.assertTrue(result['ok'])
        repair = result['answer_repair']
        self.assertFalse(repair['initial_result']['ok'])
        self.assertEqual(first['text'],repair['initial_result']['text'])
        args, kwargs = runner._run_model.call_args_list[1]
        self.assertIsNone(args[1])
        self.assertEqual({'readonly':True,'data_scope':'bank','timeout':160},kwargs)
        self.assertEqual(second['trace'],result['trace'])

    def test_rejected_draft_does_not_launder_number_as_user_hypothesis(self):
        runner = self.runner([response('剩1345.00元。',484500),response('剩1345.00元。')])
        result = runner('如果从A花3500元呢？',None)
        self.assertFalse(result['ok'])
        self.assertIsNone(result['session_id'])
        self.assertEqual(2,runner._run_model.call_count)

    def test_past_response_is_not_merged_into_fresh_evidence(self):
        runner = self.runner([response('余额4845.00元，花后1345.00元。',484500),response('余额4845.00元。')])
        result = runner('支出3500元的影响？',None)
        self.assertFalse(result['ok'])
        self.assertEqual([],result['trace'])

    def test_action_hallucination_never_triggers_repair(self):
        runner = self.runner([response('已准备好转账。剩1345.00元。')])
        result = runner('支出影响？',None)
        self.assertFalse(result['ok'])
        self.assertFalse(result['answer_repair']['attempted'])
        self.assertEqual(1,runner._run_model.call_count)

    def test_mutation_request_even_failed_blocks_repair(self):
        for tool in ['prepare_transfer','submit_task_plan','replan_task','check_insights','future_tool']:
            with self.subTest(tool=tool):
                first = response('余额1345.00元。')
                first['trace'] = [{'kind':'request','name':tool}]
                runner = self.runner([first])
                result = runner('请检查',None)
                self.assertFalse(result['ok'])
                self.assertEqual(1,runner._run_model.call_count)
                self.assertEqual('FIRST_ATTEMPT_HAS_MUTATION_OR_UNKNOWN_TOOL',result['answer_repair']['reason'])

    def test_no_budget_does_not_start_second_call(self):
        runner = self.runner([response('余额1345.00元。')])
        with patch('src.bank_v1_web.worker.time.monotonic',side_effect=[10,195]):
            result = runner('查余额',None)
        self.assertEqual('INSUFFICIENT_TIME_BUDGET',result['answer_repair']['reason'])
        self.assertEqual(1,runner._run_model.call_count)

    def test_quota_failure_on_repair_keeps_first_failure_no_third_call(self):
        runner = self.runner([response('余额1345.00元。'),{'ok':False,'error':{'code':'AccountQuotaExceeded'}}])
        result = runner('查余额',None)
        self.assertFalse(result['ok'])
        self.assertEqual('AccountQuotaExceeded',result['error']['code'])
        self.assertIn('initial_result',result['answer_repair'])
        self.assertEqual(2,runner._run_model.call_count)

    def test_oversized_prompt_skips_without_truncating_user_request(self):
        rejected = {'text':'X'*24000,'trace':[], 'answer_validation':{
            'status':'REJECTED','reasons':[{'code':'UNSUPPORTED_DISPLAYED_AMOUNT','values':['1345']}]}}
        prompt, reason = repair_prompt(rejected,'查余额',100)
        self.assertIsNone(prompt)
        self.assertEqual('REPAIR_PROMPT_TOO_LARGE',reason)

    def test_timeout_budget_reaches_node_and_python(self):
        runner = DshRunner('.', '.', 'http://127.0.0.1:1', 'unused', timeout=210)
        process = Mock(returncode=0)
        process.communicate.return_value = (json.dumps({'ok':False,'error':'fixture'}).encode(),b'')
        with patch('src.bank_v1_web.worker.subprocess.Popen',return_value=process) as popen:
            runner._run_model('只读核对',None,readonly=True,timeout=80)
        env = popen.call_args.kwargs['env']
        self.assertEqual('1',env['BANK_V1_READONLY'])
        self.assertEqual('77000',env['BANK_V1_TIMEOUT_MS'])
        self.assertEqual(80,process.communicate.call_args.kwargs['timeout'])

    def test_second_process_exception_preserves_initial_without_private_error_text(self):
        runner = self.runner([response('余额1345.00元。'),OSError('private-environment-marker')])
        result = runner('查余额',None)
        self.assertFalse(result['ok'])
        self.assertIsNone(result['session_id'])
        self.assertEqual('MODEL_FAILED',result['answer_validation']['status'])
        self.assertIn('余额1345',result['answer_repair']['initial_result']['text'])
        self.assertNotIn('private-environment-marker',json.dumps(result))
        self.assertEqual(2,runner._run_model.call_count)

    def test_repair_has_existing_operation_locator_but_still_needs_fresh_evidence(self):
        rejected={'text':'查询过的金额30元。','trace':[], 'answer_validation':{
            'status':'REJECTED','reasons':[{'code':'UNSUPPORTED_DISPLAYED_AMOUNT','values':['30']}],
            'operation_ids':['op_existing'],'task_ids':['task_existing']}}
        prompt, reason = repair_prompt(rejected,'请核对回执',100)
        self.assertIn('op_existing',prompt)
        self.assertIn('task_existing',prompt)
        self.assertIn('重新查询',prompt)

    def test_timeout_cleanup_is_bounded_and_retains_cancel_signal_when_unconfirmed(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = DshRunner('.',directory,'http://127.0.0.1:1','unused',timeout=2)
            process = Mock(pid=987654)
            process.communicate.side_effect = subprocess.TimeoutExpired('fixture',2)
            process.wait.side_effect = subprocess.TimeoutExpired('fixture',3)
            with patch('src.bank_v1_web.worker.subprocess.Popen',return_value=process), \
                 patch('src.bank_v1_web.worker.subprocess.run',side_effect=subprocess.TimeoutExpired('taskkill',10)):
                result = runner._run_model('查询',None)
            self.assertFalse(result['ok'])
            self.assertTrue(result['process_cleanup_pending'])
            self.assertEqual(3,process.communicate.call_args.kwargs['timeout'])
            self.assertTrue(list(Path(directory).glob('cancel-*')))


if __name__ == '__main__':
    unittest.main(verbosity=2)
