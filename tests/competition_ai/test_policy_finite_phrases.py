"""Typed policies support only local policy references, not actual changes."""
import unittest
from src.bank_v1_web import bank_answer as gate


def policy(percent=12.5, amount=7300):
    return {'analysis_policy': {'category_spending_increase': {
        'minimum_increase_percent': percent, 'minimum_absolute_increase_minor': amount}}}


def check(text, data=None, ok=True):
    records = [{'kind': 'response', 'tool': 'billing_report', 'ok': ok,
                'response': {'scope': 'bank', 'data': policy() if data is None else data}}]
    return gate.validate_bank_answer({'ok': True, 'text': text, 'trace': records}, '')


class PolicyFinitePhrasesTests(unittest.TestCase):
    def test_reaching_short_form_explicit_policy(self):
        for value in (7, 12.5, 23):
            for prefix in ('规则阈值为涨幅达 ', '规则标准是增幅达', '规则阈值：升幅达'):
                self.assertTrue(check(f'{prefix}{value}%才标记', policy(value))['ok'])

    def test_parenthetical_minimum(self):
        for opening, closing in (('（', '）'), ('(', ')')):
            text = f'显著上涨规则阈值{opening}至少12.5%且绝对增加至少73.00元{closing}'
            self.assertTrue(check(text)['ok'])

    def test_markdown_and_whitespace(self):
        self.assertTrue(check('规则阈值（ **至少 12.5%** 且绝对增加至少73.00元）')['ok'])

    def test_missing_or_untyped_policy(self):
        for text in ('规则阈值为涨幅达12.5%', '规则阈值（至少12.5%）'):
            for data in ({}, {'description': text}, {'args': policy()}, {'inputs': policy()},
                         {'analysis_policy': {'minimum_increase_percent': True}},
                         {'analysis_policy': {'text': '12.5%'}}):
                self.assertFalse(check(text, data)['ok'])
            self.assertFalse(check(text, policy(), ok=False)['ok'])

    def test_existing_numeric_string_policy_contract_preserved(self):
        self.assertTrue(check('规则阈值（至少12.5%）',
                              {'analysis_policy': {'minimum_increase_percent': '12.5'}})['ok'])

    def test_wrong_value(self):
        for text in ('规则阈值为涨幅达13%', '规则阈值（至少13%）'):
            self.assertFalse(check(text)['ok'])

    def test_no_cross_unit_borrow(self):
        for text in ('规则阈值为涨幅达12.5元', '规则阈值（至少12.5元）', '规则阈值（至少73%）'):
            self.assertFalse(check(text)['ok'])

    def test_actual_observation_cannot_use_policy(self):
        for text in ('实际涨幅达12.5%', '本期涨幅达12.5%，符合规则',
                     '规则阈值为实际涨幅达12.5%', '规则阈值（本次涨幅至少12.5%）',
                     '规则阈值（真实增幅至少12.5%）', '规则阈值（余额至少73.00元）'):
            self.assertFalse(check(text)['ok'])

    def test_second_observed_number_is_independent(self):
        for text in ('规则阈值为涨幅达12.5%，实际涨幅12.5%',
                     '规则阈值（至少12.5%），本次涨幅12.5%',
                     '规则阈值（至少12.5%且本期涨幅12.5%）',
                     '规则阈值（至少12.5%），余额73.00元'):
            self.assertFalse(check(text)['ok'])

    def test_remote_policy_and_nonpolicy_parenthesis_not_allowed(self):
        for text in ('规则另述。涨幅达12.5%', '规则：涨幅达12.5%',
                     '上涨情况（至少12.5%），规则另述', '规则阈值（观测金额至少73.00元）'):
            self.assertFalse(check(text)['ok'])

    def test_both_policy_and_observed_values_with_own_source(self):
        data = policy(); data['change_percent'] = 31
        self.assertTrue(check('本期涨幅31%，规则阈值（至少12.5%）', data)['ok'])

    def test_action_claim_remains_checked(self):
        self.assertFalse(check('规则阈值（至少12.5%），转账已成功')['ok'])
        self.assertFalse(check('规则阈值为涨幅达12.5%，已建立转账确认卡')['ok'])

    def test_each_policy_occurrence_needs_its_own_value(self):
        self.assertFalse(check('规则阈值（至少12.5%）；规则阈值（至少13%）')['ok'])


if __name__ == '__main__':
    unittest.main()
