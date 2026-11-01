"""Evidence boundaries for an explicit policy comparison, with no bank writes."""
import unittest
from src.bank_v1_web import bank_answer as gate


def evaluate(text, data, *, successful=True):
    result = {'ok': True, 'text': text, 'trace': [{
        'kind': 'response', 'ok': successful, 'tool': 'recurring_candidates',
        'response': {'scope': 'bank', 'data': data}}]}
    return gate.validate_bank_answer(result, '')


def policy(value=10):
    return {'analysis_policy': {'recurring_price_increase': {
        'minimum_increase_percent': value}}}


class PolicyReachingTests(unittest.TestCase):
    def test_explicit_policy_increase_synonym(self):
        for value in (8, 10, 13.75):
            for comparator in ('达到', '至少'):
                with self.subTest(value=value, comparator=comparator):
                    text = f'规则阈值为升幅{comparator} {value}% 且前期观测金额固定。'
                    self.assertTrue(evaluate(text, policy(value))['ok'])

    def test_actual_increase_synonym_stays_separate(self):
        for text in ('实际升幅至少10%。', '本次升幅达到10%，达到规则阈值。',
                     '规则阈值为升幅至少10%；本期升幅10%。',
                     '规则阈值为升幅至少10%，实际升幅10%。',
                     '规则阈值为升幅至少10%，已准备好代扣查询，请确认。',
                     '规则在前面。升幅至少10%。'):
            with self.subTest(text=text):
                self.assertFalse(evaluate(text, policy())['ok'])

    def test_synonym_requires_correct_typed_unit_and_value(self):
        self.assertFalse(evaluate('规则阈值为升幅至少11%。', policy())['ok'])
        self.assertFalse(evaluate('规则阈值为升幅至少10元。', policy())['ok'])
        self.assertFalse(evaluate('规则阈值为升幅至少10%。', {'analysis_policy': {'minimum_absolute_increase_minor': 1000}})['ok'])

    def test_explicit_policy_with_typed_non_fixture_values(self):
        for value in (7, 10, 12.5, 23):
            for prefix in ('规则阈值为涨幅达到', '规则标准是增幅达到', '规则阈值：百分比达到'):
                with self.subTest(value=value, prefix=prefix):
                    self.assertTrue(evaluate(f'{prefix}{value}%即标记。', policy(value))['ok'])

    def test_wrong_value_is_not_supported(self):
        self.assertFalse(evaluate('规则阈值为涨幅达到11%即标记。', policy())['ok'])

    def test_actual_value_cannot_borrow_policy(self):
        texts = ('实际涨幅达到10%。', '规则阈值为涨幅达到10%，实际涨幅10%。',
                 '本期涨幅达到10%，符合规则。', '采用规则，实际涨幅达到10%。',
                 '规则阈值为实际涨幅达到10%。', '规则阈值为涨幅达到10%；余额10元。')
        for text in texts:
            with self.subTest(text=text):
                self.assertFalse(evaluate(text, policy())['ok'])

    def test_remote_policy_word_does_not_license_reaching(self):
        for text in ('规则如下。涨幅达到10%。', '规则：涨幅达到10%。',
                     '按规则，消费占收入达到10%。'):
            with self.subTest(text=text):
                self.assertFalse(evaluate(text, policy())['ok'])

    def test_failed_missing_or_untyped_evidence_is_rejected(self):
        text = '规则阈值为涨幅达到10%即标记。'
        for data in ({}, {'description': '规则阈值为涨幅达到10%'},
                     {'inputs': policy()}, {'args': policy()},
                     {'analysis_policy': {'text': '10%'}},
                     {'analysis_policy': {'minimum_increase_percent': True}}):
            with self.subTest(data=data):
                self.assertFalse(evaluate(text, data)['ok'])
        self.assertFalse(evaluate(text, policy(), successful=False)['ok'])

    def test_multiple_occurrences_are_checked_separately(self):
        self.assertFalse(evaluate('规则阈值为涨幅达到10%；规则阈值为涨幅达到11%。', policy())['ok'])
        self.assertTrue(evaluate('规则阈值为涨幅达到10%；再次说明，规则阈值为涨幅达到10%。', policy())['ok'])

    def test_markdown_and_parentheses_preserve_local_policy(self):
        text = '上涨标记存在（规则阈值为**涨幅达到10%**即标记）。'
        self.assertTrue(evaluate(text, policy())['ok'])


if __name__ == '__main__':
    unittest.main()
