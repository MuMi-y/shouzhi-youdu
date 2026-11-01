"""Negative-result display equivalence, never new positive bank funds."""
import unittest
from src.bank_v1_web.bank_answer import validate_bank_answer


def evaluate(text, amount=-3725, *, success=True, scope='bank', inputs=None, nested_state=None):
    calculation = {'result': {'amount_minor': amount} if amount is not None else {}, 'inputs': inputs or {}}
    data = {'calculation': calculation}
    tool = 'money_calculate'
    if nested_state:
        tool = 'task_status'
        data = {'task': {'nodes': [{'id': 'calc', 'tool': 'money_calculate', 'state': nested_state,
                                  'result': {'scope': 'bank', 'data': data}}]}}
    raw = {'ok': True, 'text': text, 'trace': [{'kind': 'response', 'tool': tool, 'ok': success,
                                             'response': {'scope': scope, 'data': data}}]}
    return validate_bank_answer(raw, '请分析资金状况')['ok']


class AccountShortfallTests(unittest.TestCase):
    def test_account_subjects(self):
        for subject in ['账户', '该账户', '付款账户', '日常账户', '储备账户']:
            with self.subTest(subject=subject): self.assertTrue(evaluate(subject + '差37.25元'))

    def test_different_amounts_and_formats(self):
        for amount, display in [(-165500, '1,655.00'), (-9500, '95'), (-10, '0.10')]:
            with self.subTest(amount=amount): self.assertTrue(evaluate('该账户差' + display + '元', amount))

    def test_display_connectors_and_markdown(self):
        for text in ['该账户差约37.25元', '该账户差：37.25元', '该账户差 **37.25** 元']:
            with self.subTest(text=text): self.assertTrue(evaluate(text))

    def test_other_difference_nouns_not_shortfall(self):
        for prefix in ['预算差', '价差', '利差', '汇差', '误差', '两账户相差', '账户之间差', '两账户差', '不同账户差']:
            with self.subTest(prefix=prefix): self.assertFalse(evaluate(prefix + '37.25元'))

    def test_explicit_not_short(self):
        self.assertFalse(evaluate('该账户不差37.25元'))

    def test_local_explicit_denial(self):
        for text in ['不是该账户差37.25元', '并非付款账户差37.25元', '并不是这个账户差37.25元']:
            with self.subTest(text=text): self.assertFalse(evaluate(text))

    def test_multiple_subjects_not_account_shortfall(self):
        for subject in ['两个储蓄账户', '两个人的账户', '多个外币账户', '3个付款账户', '不同用途的账户']:
            with self.subTest(subject=subject): self.assertFalse(evaluate(subject + '差37.25元'))

    def test_previous_clause_denial_not_contaminating_subject(self):
        for separator in ['，', '。', '；', '\n']:
            with self.subTest(separator=separator): self.assertTrue(evaluate('无需转账' + separator + '该账户差37.25元'))

    def test_account_type_does_not_require_catalog(self):
        for subject in ['储蓄账户', '生活账户', '付款专用账户']:
            with self.subTest(subject=subject): self.assertTrue(evaluate(subject + '差37.25元'))

    def test_nonassertion_cannot_supply_positive_shortfall(self):
        for text in ['不代表该账户差287.40元', '不能说该账户差287.40元', '并不意味着该账户差287.40元']:
            with self.subTest(text=text): self.assertFalse(evaluate(text, -28740))

    def test_demonstrative_plural_and_comparison_not_shortfall(self):
        for subject in ['比较这对账户', '这些账户', '那些账户', '这对储蓄账户']:
            with self.subTest(subject=subject): self.assertFalse(evaluate(subject + '差287.40元', -28740))

    def test_negative_result_not_positive_funds(self):
        for prefix in ['账户余额', '收入', '可用资金']:
            with self.subTest(prefix=prefix): self.assertFalse(evaluate(prefix + '37.25元'))

    def test_each_occurrence_checked(self):
        self.assertFalse(evaluate('该账户差37.25元，但可用余额37.25元'))
        self.assertFalse(evaluate('该账户差37.25元。另一账户余额37.25元'))

    def test_wrong_magnitude(self):
        self.assertFalse(evaluate('该账户差37.26元'))

    def test_no_typed_negative_result(self):
        self.assertFalse(evaluate('该账户差37.25元', None))
        self.assertFalse(evaluate('该账户差37.25元', '-3725'))

    def test_inputs_are_not_results(self):
        self.assertFalse(evaluate('该账户差37.25元', None, inputs={'amount_minor': -3725}))

    def test_unsuccessful_and_foreign_tools(self):
        self.assertFalse(evaluate('该账户差37.25元', success=False))
        self.assertFalse(evaluate('该账户差37.25元', scope='moneydata'))

    def test_successful_nested_matches_direct(self):
        self.assertTrue(evaluate('该账户差37.25元', nested_state='SUCCEEDED'))

    def test_unsuccessful_nested_not_evidence(self):
        for state in ['PENDING', 'RUNNING', 'FAILED']:
            with self.subTest(state=state): self.assertFalse(evaluate('该账户差37.25元', nested_state=state))

    def test_original_signed_and_deficit_displays(self):
        for text in ['计算结果-37.25元', '该账户还差37.25元', '缺口37.25元']:
            with self.subTest(text=text): self.assertTrue(evaluate(text))


if __name__ == '__main__': unittest.main(verbosity=2)
