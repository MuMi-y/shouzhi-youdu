"""A bounded parameter request permits future preparation, never past success."""
import unittest
from src.bank_v1_web.bank_answer import validate_bank_answer


def evaluate(text):
    return validate_bank_answer({'ok': True, 'text': text, 'trace': []}, '请帮我办理转账。')


class FuturePreparationTests(unittest.TestCase):
    def test_exact_parameter_request_with_parenthetical_limit(self):
        self.assertTrue(evaluate('明确一个新的转账金额（不超过A账户可用余额），我据此重新准备待确认转账。')['ok'])

    def test_request_account_then_prepare(self):
        self.assertTrue(evaluate('请指定转入账户，我按此准备待确认转账。')['ok'])

    def test_request_amount_then_prepare(self):
        self.assertTrue(evaluate('请您先提供新的数额，我再准备转账确认卡。')['ok'])

    def test_request_date_then_prepare(self):
        self.assertTrue(evaluate('请明确安排日期，我会据此准备待确认安排。')['ok'])

    def test_original_tell_me_wording_remains(self):
        self.assertTrue(evaluate('告诉我金额，我立即准备转账。')['ok'])

    def test_past_prepared_claim_is_not_hidden(self):
        self.assertFalse(evaluate('请明确转账金额，我据此准备，转账已准备好了。')['ok'])

    def test_past_success_claim_is_not_hidden(self):
        self.assertFalse(evaluate('请指定账户，我按此准备，转账已经成功。')['ok'])

    def test_past_completion_claim_is_not_hidden(self):
        self.assertFalse(evaluate('请提供金额，我再准备，银行操作已完成。')['ok'])

    def test_pending_card_without_request_still_needs_evidence(self):
        self.assertFalse(evaluate('转账待确认，请到银行页面确认。')['ok'])

    def test_past_preparation_in_same_requested_sentence_rejected(self):
        self.assertFalse(evaluate('请提供金额，我已据此准备好待确认转账。')['ok'])

    def test_future_clause_cannot_hide_separate_pending_card(self):
        self.assertFalse(evaluate('请提供金额，我再准备转账，当前转账待确认，请到银行页面确认。')['ok'])


if __name__ == '__main__':
    unittest.main()
