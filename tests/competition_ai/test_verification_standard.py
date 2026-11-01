"""Receipt verification criteria are not assertions that an operation succeeded."""
import unittest
from src.bank_v1_web.bank_answer import validate_bank_answer


def evaluate(text):
    return validate_bank_answer({'ok': True, 'text': text, 'trace': []}, '只解释，不办理。')


class VerificationStandardTests(unittest.TestCase):
    def test_cancellation_effect_is_a_receipt_criterion(self):
        self.assertTrue(evaluate('撤销是否生效以操作成功回执中的授权状态为准。')['ok'])

    def test_transfer_completion_with_comma(self):
        self.assertTrue(evaluate('转账是否完成，以银行操作成功回执为准。')['ok'])

    def test_authorization_effect_with_modal_word(self):
        self.assertTrue(evaluate('授权是否生效须以回执中的授权状态为准。')['ok'])

    def test_operation_success_criterion(self):
        self.assertTrue(evaluate('操作是否成功应以操作回执为准。')['ok'])

    def test_task_completion_criterion(self):
        self.assertTrue(evaluate('任务是否完成以任务状态为准。')['ok'])

    def test_past_success_before_criterion_is_not_hidden(self):
        self.assertFalse(evaluate('撤销已成功，撤销是否生效以操作成功回执中的授权状态为准。')['ok'])

    def test_past_success_after_criterion_is_not_hidden(self):
        self.assertFalse(evaluate('撤销是否生效以操作成功回执中的授权状态为准，撤销已成功。')['ok'])

    def test_same_clause_completion_cannot_hide_as_standard(self):
        self.assertFalse(evaluate('撤销已完成，以成功回执为准。')['ok'])

    def test_other_clause_success_still_needs_evidence(self):
        self.assertFalse(evaluate('撤销是否生效以回执为准；转账已成功。')['ok'])

    def test_unconditional_success_still_rejected(self):
        self.assertFalse(evaluate('撤销操作成功。')['ok'])

    def test_explicit_completion_inside_criterion_not_exempt(self):
        self.assertFalse(evaluate('撤销是否生效以已成功的操作回执为准。')['ok'])

    def test_success_with_following_receipt_reference_still_rejected(self):
        self.assertFalse(evaluate('操作成功，具体以回执为准。')['ok'])


if __name__ == '__main__':
    unittest.main()
