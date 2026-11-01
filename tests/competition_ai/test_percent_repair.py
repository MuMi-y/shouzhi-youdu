"""Portable bounded percentage repair tests; mocked DSH never starts a process."""
import unittest
from unittest.mock import Mock, patch
from src.bank_v1_web.worker import DshRunner


def result(text, data, *, tool='billing_report'):
    return {'ok': True, 'text': text, 'session_id': 'model-session', 'trace': [
        {'kind': 'request', 'name': tool, 'arguments': {}},
        {'kind': 'response', 'tool': tool, 'ok': True,
         'response': {'scope': 'bank', 'data': data}}]}


class PercentRepairTests(unittest.TestCase):
    def runner(self, responses):
        runner = DshRunner('.', '.', 'http://127.0.0.1:1', 'unused-test', timeout=210)
        runner._run_model = Mock(side_effect=responses)
        return runner

    def test_unbacked_percentage_repairs_once_using_fresh_readonly_session(self):
        first = result('出账占收入88.19%。', {'expense_minor': 705500, 'income_minor': 800000})
        second = result('计算所得支出占收入88.19%。', {'calculation': {
            'inputs': {'left_minor': 705500, 'right_minor': 800000},
            'result': {'percent': '88.19'}}}, tool='money_calculate')
        runner = self.runner([first, second])
        with patch('src.bank_v1_web.worker.time.monotonic', side_effect=[10, 60]):
            actual = runner('支出占收入多少？', 'old-session')
        self.assertTrue(actual['ok'])
        self.assertTrue(actual['answer_repair']['attempted'])
        self.assertFalse(actual['answer_repair']['initial_result']['ok'])
        self.assertEqual(first['text'], actual['answer_repair']['initial_result']['text'])
        self.assertEqual(first['trace'], actual['answer_repair']['initial_result']['trace'])
        self.assertEqual(second['trace'], actual['trace'])
        args, kwargs = runner._run_model.call_args_list[1]
        self.assertIsNone(args[1])
        self.assertEqual({'readonly': True, 'data_scope': 'bank', 'timeout': 160}, kwargs)
        self.assertEqual(2, runner._run_model.call_count)

    def test_check_insights_before_bad_percentage_blocks_automatic_repair(self):
        first = result('实际涨幅88%。', {'candidates': []}, tool='check_insights')
        runner = self.runner([first])
        actual = runner('重新检查关注项变化。', 'old-session')
        self.assertFalse(actual['ok'])
        self.assertFalse(actual['answer_repair']['attempted'])
        self.assertEqual('FIRST_ATTEMPT_HAS_MUTATION_OR_UNKNOWN_TOOL', actual['answer_repair']['reason'])
        self.assertEqual(first['text'], actual['text'])
        self.assertEqual(first['trace'], actual['trace'])
        self.assertEqual(1, runner._run_model.call_count)

    def test_failed_draft_percentage_is_not_a_user_assumption_on_second_pass(self):
        first = result('占收入88%。', {'expense_minor': 705500, 'income_minor': 800000})
        second = {'ok': True, 'text': '占收入88%。', 'trace': [], 'session_id': 'new-session'}
        runner = self.runner([first, second])
        actual = runner('支出占收入多少？', None)
        self.assertFalse(actual['ok'])
        self.assertIsNone(actual['session_id'])
        self.assertEqual(2, runner._run_model.call_count)
        self.assertEqual('UNSUPPORTED_DISPLAYED_PERCENT', actual['answer_validation']['reasons'][0]['code'])
