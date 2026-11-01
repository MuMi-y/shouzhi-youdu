"""A locally named rule ratio still needs typed policy, including task results."""
import unittest
from src.bank_v1_web.bank_answer import validate_bank_answer


def check(text, policy=None, nested=False):
    data = {'analysis_policy': policy or {}}
    if nested:
        data = {'task': {'task_id': 'a'*32, 'state': 'WAITING_CONFIRMATION',
            'nodes': [{'tool': 'recurring_candidates', 'state': 'SUCCEEDED',
                       'result': {'data': data}}]}}
    return validate_bank_answer({'ok': True, 'text': text, 'trace': [
        {'kind': 'response', 'tool': 'task_status' if nested else 'recurring_candidates',
         'ok': True, 'response': {'data': data}}]}, '')


class NamedPolicyRatioTests(unittest.TestCase):
    def test_direct_rule_ratio(self):
        self.assertTrue(check('规则阈值为涨幅 13%', {'rise_percent': 13})['ok'])

    def test_nested_rule_ratio(self):
        self.assertTrue(check('规则阈值为涨幅 13%', {'rise_percent': 13}, True)['ok'])

    def test_other_local_rule_spelling(self):
        self.assertTrue(check('规则标准是百分比13%', {'rise_percent': 13})['ok'])

    def test_missing_typed_value(self):
        self.assertFalse(check('规则阈值为涨幅13%')['ok'])

    def test_wrong_value(self):
        self.assertFalse(check('规则阈值为涨幅13%', {'rise_percent': 11})['ok'])

    def test_money_cannot_prove_percentage(self):
        self.assertFalse(check('规则阈值为涨幅13%', {'rise_minor': 1300})['ok'])

    def test_percentage_cannot_prove_money(self):
        self.assertFalse(check('规则阈值为涨幅13元', {'rise_percent': 13})['ok'])

    def test_actual_ratio_after_policy_context_not_supported(self):
        self.assertFalse(check('规则阈值为涨幅13%，本期实际涨幅13%', {'rise_percent': 13})['ok'])

    def test_actual_ratio_before_policy_not_supported(self):
        self.assertFalse(check('本次实际涨幅13%，规则阈值为涨幅13%', {'rise_percent': 13})['ok'])

    def test_remote_rule_word_not_enough(self):
        self.assertFalse(check('规则已说明；涨幅13%', {'rise_percent': 13})['ok'])


if __name__ == '__main__':
    unittest.main()
