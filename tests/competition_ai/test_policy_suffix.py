"""Policy references stay local; policy values never become actual facts."""
import unittest
from src.bank_v1_web.bank_answer import validate_bank_answer


def evaluate(text, *, policy=True, actual=True, success=True, data=None):
    if data is None:
        data = {}
        if policy:
            data['analysis_policy'] = {'price': {'minimum_increase_percent': 10},
                                       'category': {'minimum_absolute_increase_minor': 10000}}
        if actual:
            data.update({'amount_minor': 2500, 'change_percent': '25.00'})
    result = {'ok': True, 'text': text, 'session_id': 'synthetic', 'trace': [
        {'kind': 'response', 'tool': 'check_insights', 'ok': success,
         'response': {'scope': 'bank', 'data': data}}]}
    return validate_bank_answer(result, '请解释本次观测结果及规则。')


class PolicySuffixTests(unittest.TestCase):
    def test_actual_s08_local_comparison(self):
        self.assertTrue(evaluate('本次25.00元、涨幅25.00%，超过10%阈值，故触发。')['ok'])

    def test_short_actual_s08_percentage_comparison(self):
        self.assertTrue(evaluate('本次25%、涨幅25%，超过10%阈值。')['ok'])

    def test_rule_threshold_suffix(self):
        self.assertTrue(evaluate('参照10%规则阈值。')['ok'])

    def test_standalone_typed_threshold(self):
        self.assertTrue(evaluate('10%阈值。')['ok'])

    def test_amount_threshold(self):
        self.assertTrue(evaluate('采用100元阈值。')['ok'])

    def test_decimal_amount_threshold(self):
        self.assertTrue(evaluate('采用100.00元规则阈值。')['ok'])

    def test_markdown_spaces_fullwidth_percent(self):
        self.assertTrue(evaluate('超过 **10.00 ％** 规则阈值。')['ok'])

    def test_supported_actual_can_equal_policy(self):
        self.assertTrue(evaluate('实际涨幅10%。', data={'change_percent': 10})['ok'])

    def test_actual_ratio_cannot_borrow_policy(self):
        self.assertFalse(evaluate('实际涨幅10%。')['ok'])

    def test_actual_amount_cannot_borrow_policy(self):
        self.assertFalse(evaluate('当前余额100元。')['ok'])

    def test_same_value_two_contexts_still_rejects_actual(self):
        self.assertFalse(evaluate('采用10%阈值；实际涨幅10%。')['ok'])

    def test_same_amount_two_contexts_still_rejects_actual(self):
        self.assertFalse(evaluate('采用100元阈值；账户余额100元。')['ok'])

    def test_suffix_does_not_mask_actual_rate_assignment(self):
        self.assertFalse(evaluate('本次实际涨幅为10%阈值。')['ok'])

    def test_suffix_does_not_mask_balance_assignment(self):
        self.assertFalse(evaluate('当前余额为100元规则阈值。')['ok'])

    def test_threshold_noun_distant_from_actual_does_not_support(self):
        self.assertFalse(evaluate('实际涨幅10%，另有规则阈值待说明。')['ok'])

    def test_missing_typed_policy(self):
        self.assertFalse(evaluate('超过10%阈值。', policy=False)['ok'])

    def test_description_only_is_not_policy(self):
        self.assertFalse(evaluate('超过10%阈值。', data={'description': '10%阈值', 'rule': '10%'})['ok'])

    def test_failed_policy_tool_is_not_evidence(self):
        self.assertFalse(evaluate('超过10%阈值。', success=False)['ok'])

    def test_unbacked_actual_alongside_valid_threshold_is_rejected(self):
        value = evaluate('本次涨幅25%，超过10%阈值。', actual=False)
        self.assertFalse(value['ok'])
        self.assertEqual(['25'], value['answer_validation']['reasons'][0]['values'])

    def test_typed_percentage_does_not_support_amount(self):
        self.assertFalse(evaluate('采用10元阈值。')['ok'])

    def test_typed_amount_does_not_support_percentage(self):
        self.assertFalse(evaluate('采用100%阈值。')['ok'])

    def test_non_policy_suffix_cannot_support(self):
        self.assertFalse(evaluate('本次10%收益。')['ok'])

    def test_other_action_gate_remains(self):
        value = evaluate('采用10%阈值。转账已成功。')
        self.assertFalse(value['ok'])
        self.assertIn('UNBACKED_OPERATION_SUCCESS', [r['code'] for r in value['answer_validation']['reasons']])


    def test_suffix_with_de(self):
        self.assertTrue(evaluate('本次涨幅25%，超过10%的阈值。')['ok'])

    def test_suffix_causal_connector(self):
        self.assertTrue(evaluate('本次涨幅25%，超过10%阈值而触发提醒。')['ok'])

    def test_suffix_de_rule_threshold_causal_connector(self):
        self.assertTrue(evaluate('超过10%的规则阈值而触发提醒。')['ok'])

    def test_suffix_de_cannot_mask_actual_assignment(self):
        self.assertFalse(evaluate('实际涨幅10%的阈值。')['ok'])

    def test_suffix_causal_cannot_mask_actual_assignment(self):
        self.assertFalse(evaluate('本期百分比为10%阈值而触发提醒。')['ok'])

    def test_distant_suffix_stays_rejected(self):
        self.assertFalse(evaluate('本次涨幅10%，采用另一个阈值而触发。')['ok'])

    def test_suffix_causal_still_requires_typed_policy(self):
        self.assertFalse(evaluate('超过10%的阈值而触发。', policy=False)['ok'])

    def test_missing_actual_not_rescued_by_de_suffix(self):
        self.assertFalse(evaluate('本期涨幅25%，超过10%的阈值。', actual=False)['ok'])

    def test_absolute_increase_and_percentage_policy_language(self):
        data = {'analysis_policy': {'minimum_absolute_increase_minor': 10000, 'minimum_increase_percent': 20}}
        self.assertTrue(evaluate('规则阈值为：绝对涨幅至少100元且百分比至少20%。', data=data)['ok'])

    def test_actual_percentage_comparisons_cannot_borrow_rules(self):
        data = {'analysis_policy': {'minimum_absolute_increase_minor': 10000, 'minimum_increase_percent': 20}}
        for text in ['规则阈值为20%，实际百分比至少20%。', '规则阈值为20%，本期增幅至少20%。',
                     '规则阈值为20%，真实增长率至少20%。', '规则阈值为100元，本次金额至少100元。']:
            with self.subTest(text=text):
                self.assertFalse(evaluate(text, data=data)['ok'])

    def test_policy_comparison_words_without_policy_data(self):
        self.assertFalse(evaluate('规则阈值为：绝对涨幅至少100元且百分比至少20%。', policy=False)['ok'])


if __name__ == '__main__':
    unittest.main()
