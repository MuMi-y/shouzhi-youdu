"""Self-contained finite future offer checks; no bank/model/history dependencies."""
import unittest
from src.bank_v1_web.bank_answer import requests_details_before_future_preparation as offer
from src.bank_v1_web.bank_answer import validate_bank_answer

BASE = '告知新的转账金额，我重规划任务并重新准备待确认操作'

def answer(text):
    return validate_bank_answer({'ok': True, 'text': text, 'trace': []}, '请调整转账金额')

class FutureReplanOfferTests(unittest.TestCase):
    def test_real_phrase_shape(self):
        self.assertTrue(offer('1. ' + BASE))
        self.assertTrue(answer('1. ' + BASE)['ok'])

    def test_finite_variations(self):
        for request in ('告知', '提供', '明确'):
            for modal in ('', '会', '将', '再'):
                for target in ('待确认操作', '转账'):
                    text = request + '新的转账金额，我' + modal + '重规划任务并重新准备' + target
                    with self.subTest(text=text): self.assertTrue(offer(text))

    def test_list_and_polite_prefix(self):
        for prefix in ('', '1. ', '2、', '99)\t'):
            for polite in ('', '请', '请您', '请先', '请您先'):
                with self.subTest(prefix=prefix, polite=polite): self.assertTrue(offer(prefix + polite + BASE))

    def test_no_new_input_not_future_offer(self):
        for text in ('我重规划任务并重新准备待确认操作', BASE.replace('新的', ''), BASE.replace('转账金额', '账户'), BASE.replace('新的转账金额', '500元')):
            with self.subTest(text=text): self.assertFalse(offer(text))

    def test_no_free_insertions(self):
        for text in ('当前' + BASE, BASE.replace('我重规划', '我现在重规划'), BASE.replace('我重规划', '我本次重规划'), BASE + '请确认', BASE.replace('金额，', '金额然后，')):
            with self.subTest(text=text): self.assertFalse(offer(text))

    def test_actual_state_not_future(self):
        for status in ('已', '已经', '成功', '完成', '好了'):
            text=BASE.replace('我重规划', '我' + status + '重规划')
            with self.subTest(text=text): self.assertFalse(offer(text))

    def test_no_cross_line_or_clause(self):
        for text in (BASE.replace('，', '，\n'), BASE + '；转账已成功', '转账已成功；' + BASE, BASE + '，请确认转账'):
            with self.subTest(text=text): self.assertFalse(offer(text))

    def test_unknown_card_never_created(self):
        for text in (BASE + '；请确认转账操作op_unknown123', BASE + '，转账已准备好，请确认。'):
            with self.subTest(text=text): self.assertFalse(answer(text)['ok'])

    def test_adjacent_actual_success_needs_evidence(self):
        for separator in ('；', '。', '\n', '，'):
            for text in (BASE + separator + '转账已成功', '转账已成功' + separator + BASE):
                with self.subTest(text=text): self.assertFalse(answer(text)['ok'])

    def test_future_offer_does_not_supply_numeric_evidence(self):
        result=answer(BASE + '。当前余额是999.99元。')
        self.assertFalse(result['ok'])
        self.assertTrue(result['answer_validation']['numeric_diagnostics'])

    def test_legacy_request_branch_preserved(self):
        self.assertTrue(offer('请提供转账金额，我会重新准备转账'))
        self.assertFalse(offer('请提供转账金额，我已准备转账'))

    def test_source_free_preparation_still_rejected(self):
        self.assertFalse(answer('我重规划任务并重新准备待确认操作')['ok'])
        self.assertFalse(answer('已重新准备好转账操作，请确认。')['ok'])

if __name__ == '__main__': unittest.main()
