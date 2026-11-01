"""Portable finite Chinese ratio checks. Synthetic responses; no model calls."""
import unittest
from unittest.mock import Mock
from src.bank_v1_web.bank_answer import validate_bank_answer
from src.bank_v1_web.worker import DshRunner


def answer(text, data=None, *, tool='billing_report', ok=True):
    return {'ok': True, 'text': text, 'session_id': 'synthetic', 'trace': [] if data is None else [
        {'kind': 'request', 'name': tool, 'arguments': {}},
        {'kind': 'response', 'tool': tool, 'ok': ok,
         'response': {'scope': 'bank', 'data': data}}]}


def calculation(percent):
    return {'calculation': {'inputs': {}, 'result': {'percent': percent}}}


class ChineseRatioTests(unittest.TestCase):
    def validate(self, text, data=None, prompt='请分析本期财务。', **kwargs):
        return validate_bank_answer(answer(text, data, **kwargs), prompt)

    def codes(self, result):
        return [r['code'] for r in result['answer_validation']['reasons']]

    def test_unbacked_one_cheng_rejected_and_raw_text_retained(self):
        result = self.validate('收入约有一成结余。')
        self.assertFalse(result['ok'])
        self.assertEqual(result['text'], '收入约有一成结余。')
        self.assertIn('UNSUPPORTED_DISPLAYED_PERCENT', self.codes(result))
        self.assertIsNone(result['session_id'])

    def test_exact_ten_percent_supports_one_cheng(self):
        self.assertTrue(self.validate('结余占收入一成。', calculation('10.00'), tool='money_calculate')['ok'])

    def test_expense_ratio_does_not_calculate_complement(self):
        result = self.validate('支出占88.19%，收入约有一成结余。', calculation('88.19'), tool='money_calculate')
        self.assertFalse(result['ok'])
        self.assertEqual(result['answer_validation']['reasons'][0]['values'], ['一成'])

    def test_no_approximate_rounding_from_nearby_actual_ratio(self):
        self.assertFalse(self.validate('结余约一成。', calculation('11.81'), tool='money_calculate')['ok'])

    def test_policy_ten_cannot_support_actual_ratio(self):
        data = {'analysis_policy': {'recurring_price_increase': {'minimum_increase_percent': 10}}}
        self.assertFalse(self.validate('结余一成。', data)['ok'])
        self.assertFalse(self.validate('规则阈值为一成。', data)['ok'])

    def test_fifteen_and_half_cheng_need_exact_tool_values(self):
        for phrase, value in [('一成五', '15'), ('半成', '5'), ('两成', '20')]:
            with self.subTest(phrase=phrase):
                self.assertFalse(self.validate('占收入' + phrase + '。')['ok'])
                self.assertTrue(self.validate('占收入' + phrase + '。', calculation(value), tool='money_calculate')['ok'])

    def test_one_and_half_cheng_requires_fifteen_not_ten(self):
        self.assertTrue(self.validate('结余一成半。', calculation('15'), tool='money_calculate')['ok'])
        self.assertFalse(self.validate('结余一成半。', calculation('10'), tool='money_calculate')['ok'])

    def test_negative_cheng_does_not_match_positive_value(self):
        for token in ['负一成', '−一成', '-一成']:
            with self.subTest(token=token):
                self.assertFalse(self.validate('变化为' + token + '。', calculation('10'), tool='money_calculate')['ok'])
                self.assertTrue(self.validate('变化为' + token + '。', calculation('-10'), tool='money_calculate')['ok'])

    def test_negative_half_and_one_and_half_have_exact_values(self):
        for token, value in [('负半成', '-5'), ('负一成半', '-15')]:
            with self.subTest(token=token):
                self.assertTrue(self.validate('变化为' + token + '。', calculation(value), tool='money_calculate')['ok'])
                self.assertFalse(self.validate('变化为' + token + '。', calculation('10'), tool='money_calculate')['ok'])

    def test_non_numeric_words_and_common_cheng_words_are_not_ratios(self):
        for text in ['成本仍需核对，任务尚未完成。', '半成品成本待核对。', '不要一成不变地使用假设。', '单一成本项目需单独分析。']:
            with self.subTest(text=text):
                self.assertTrue(self.validate(text)['ok'])

    def test_user_chinese_assumption_can_be_repeated_not_proved(self):
        result = self.validate('按你假设的一成结余进一步分析。', prompt='假设结余一成，请分析。')
        self.assertTrue(result['ok'])
        self.assertIn('用户假设已发生', result['answer_validation']['limitation'])

    def test_user_arabic_assumption_supports_equivalent_chinese(self):
        self.assertTrue(self.validate('按假设的一成结余分析。', prompt='假设结余10%。')['ok'])

    def test_chinese_assumption_does_not_allow_unrelated_new_ratio(self):
        self.assertFalse(self.validate('结余两成。', prompt='假设结余一成。')['ok'])

    def test_echoed_calculation_inputs_do_not_support_ratio(self):
        data = {'calculation': {'inputs': {'percent': 10}, 'result': {'percent': 88}}}
        self.assertFalse(self.validate('结余一成。', data, tool='money_calculate')['ok'])

    def test_failed_tool_and_free_text_do_not_supply_ratio(self):
        self.assertFalse(self.validate('结余一成。', {'percent': 10}, ok=False)['ok'])
        self.assertFalse(self.validate('结余一成。', {'message': '实际结余一成，即10%'})['ok'])

    def test_mixed_amount_check_is_preserved(self):
        result = self.validate('结余一成，余额1234.00元。', calculation('10'), tool='money_calculate')
        self.assertFalse(result['ok'])
        self.assertEqual(self.codes(result), ['UNSUPPORTED_DISPLAYED_AMOUNT'])

    def test_no_number_action_validation_is_preserved(self):
        self.assertTrue(self.validate('请告诉我要分析的期间。')['ok'])
        self.assertFalse(self.validate('转账已成功。')['ok'])

    def test_one_readonly_repair_with_exact_ratio_then_stop(self):
        first = answer('结余一成。', {'net_change_minor': 10000})
        second = answer('结余一成。', calculation('10'), tool='money_calculate')
        runner = DshRunner('.', '.', 'http://127.0.0.1:1', 'unused-test', timeout=210)
        runner._run_model = Mock(side_effect=[first, second])
        result = runner('结余占收入多少？', None)
        self.assertTrue(result['ok'])
        self.assertEqual(runner._run_model.call_count, 2)
        self.assertTrue(runner._run_model.call_args_list[1].kwargs['readonly'])
        self.assertFalse(result['answer_repair']['initial_result']['ok'])
        self.assertEqual(result['answer_repair']['initial_result']['text'], first['text'])

    def test_second_unbacked_ratio_fails_without_third_call(self):
        runner = DshRunner('.', '.', 'http://127.0.0.1:1', 'unused-test', timeout=210)
        runner._run_model = Mock(side_effect=[answer('结余一成。'), answer('结余一成。')])
        result = runner('结余占比多少？', None)
        self.assertFalse(result['ok'])
        self.assertEqual(runner._run_model.call_count, 2)

    def test_mutation_still_disallows_repair(self):
        runner = DshRunner('.', '.', 'http://127.0.0.1:1', 'unused-test', timeout=210)
        runner._run_model = Mock(return_value=answer('结余一成。', {}, tool='check_insights'))
        result = runner('重新检查。', None)
        self.assertFalse(result['ok'])
        self.assertFalse(result['answer_repair']['attempted'])
        self.assertEqual(runner._run_model.call_count, 1)


if __name__ == '__main__':
    unittest.main()
