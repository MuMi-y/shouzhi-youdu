"""Future optional service does not prove preparation or relax numeric evidence."""
import unittest
from src.bank_v1_web.bank_answer import validate_bank_answer


def evaluate(text):
    return validate_bank_answer({'ok': True, 'text': text, 'trace': []}, '只说明，不办理。')


class OptionalFutureServiceTests(unittest.TestCase):
    def test_later_optional_service_keeps_confirmation_boundary(self):
        self.assertTrue(evaluate('如后续还想查看正式授权清单或办理其他事项，告诉我即可，仍会逐项等您确认。')['ok'])

    def test_if_later_need_query(self):
        self.assertTrue(evaluate('若以后需要查询代扣，告诉我，我再逐项请您确认。')['ok'])

    def test_future_transfer_service(self):
        self.assertTrue(evaluate('如后续想办理转账，可告诉我，届时会等您确认。')['ok'])

    def test_present_confirmation_claim_still_rejected(self):
        self.assertFalse(evaluate('代扣查询待确认，请您确认。')['ok'])

    def test_future_does_not_hide_prepared_claim(self):
        self.assertFalse(evaluate('若以后需要查询代扣，已准备查询操作，仍会等您确认。')['ok'])

    def test_future_does_not_hide_success_claim(self):
        self.assertFalse(evaluate('如后续想查询代扣，查询操作已成功，仍会等您确认。')['ok'])

    def test_future_does_not_hide_completed_claim(self):
        self.assertFalse(evaluate('如后续想查询代扣，银行操作完成了，仍会等您确认。')['ok'])

    def test_current_pending_card_is_not_optional_future(self):
        self.assertFalse(evaluate('如后续想查询代扣，当前查询待确认，仍会等您确认。')['ok'])

    def test_other_success_clause_is_not_hidden(self):
        self.assertFalse(evaluate('若以后需要查询代扣，我再请您确认；转账已成功。')['ok'])

    def test_optional_future_does_not_whitelist_unbacked_amount(self):
        result = evaluate('金额为37.25元。如后续想查询代扣，仍会等您确认。')
        self.assertFalse(result['ok'])
        self.assertEqual(['UNSUPPORTED_DISPLAYED_AMOUNT'], [x['code'] for x in result['answer_validation']['reasons']])


if __name__ == '__main__':
    unittest.main()
