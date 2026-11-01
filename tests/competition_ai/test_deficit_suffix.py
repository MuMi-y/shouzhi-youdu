"""Bounded deficit suffix display; self-contained synthetic responses."""
import unittest
from src.bank_v1_web.bank_answer import validate_bank_answer


def answer(text, *, value=-3725, success=True, data=None, tool='forecast'):
    payload = {'daily': [{'date': '2026-11-01', 'projected_balance_minor': value}]} if data is None else data
    return validate_bank_answer({'ok': True, 'text': text, 'trace': [
        {'kind': 'response', 'tool': tool, 'ok': success,
         'response': {'scope': 'bank', 'data': payload}}]}, '仅分析当前工具证据。')


class DeficitSuffixTests(unittest.TestCase):
    def test_suffix_spaces_and_optional_particle(self):
        for text in ['出现37.25元缺口。', '再次出现 37.25 元缺口',
                     '有37.25元的缺口。', '37.25\t元\t的\t缺口。']:
            with self.subTest(text=text): self.assertTrue(answer(text)['ok'])

    def test_other_magnitudes_not_fixed_example(self):
        for value, text in [(-7300, '出现73.00元缺口。'), (-105125, '出现1,051.25元的缺口。')]:
            with self.subTest(value=value): self.assertTrue(answer(text, value=value)['ok'])

    def test_existing_prefix_and_signed_balance(self):
        for text in ['缺口37.25元。', '还差37.25元。', '预测余额为-37.25元。']:
            with self.subTest(text=text): self.assertTrue(answer(text)['ok'])

    def test_signed_calculation_result_uses_same_contract(self):
        self.assertTrue(answer('出现37.25元缺口。', tool='money_calculate',
            data={'calculation': {'result': {'amount_minor': -3725}}})['ok'])

    def test_suffix_does_not_relabel_positive_financial_subject(self):
        for subject in ['余额为', '可用资金为', '收入', '已到账', '入账', '还款金额为', '已支付', '到账金额：', '收入金额:', '余额正']:
            with self.subTest(subject=subject): self.assertFalse(answer(subject+'37.25元缺口。')['ok'])

    def test_plain_positive_balance_income_payment_remain_unsupported(self):
        for text in ['余额37.25元。', '收入37.25元。', '到账37.25元。', '还款37.25元。']:
            with self.subTest(text=text): self.assertFalse(answer(text)['ok'])

    def test_later_deficit_is_not_same_occurrence(self):
        for text in ['余额37.25元，另有37.25元缺口。', '出现37.25元缺口，到账37.25元。']:
            with self.subTest(text=text): self.assertFalse(answer(text)['ok'])

    def test_wrong_or_adjacent_amount_still_rejected(self):
        for text in ['出现37.26元缺口。', '出现37.25元缺口，还有999.99元缺口。']:
            with self.subTest(text=text): self.assertFalse(answer(text)['ok'])

    def test_no_punctuation_or_newline_bridging(self):
        for text in ['37.25元，缺口。', '37.25元。缺口。', '37.25\n元缺口。', '37.25元\n缺口。']:
            with self.subTest(text=text): self.assertFalse(answer(text)['ok'])

    def test_suffix_boundary_is_not_an_unrelated_noun(self):
        for text in ['37.25元缺口率。', '37.25元缺口编号。']:
            with self.subTest(text=text): self.assertFalse(answer(text)['ok'])

    def test_no_source_or_failed_tool(self):
        self.assertFalse(answer('出现37.25元缺口。', data={})['ok'])
        self.assertFalse(answer('出现37.25元缺口。', success=False)['ok'])

    def test_string_description_and_arguments_are_not_balance(self):
        for data in [{'projected_balance_minor': '-3725'}, {'description': '-37.25'},
                     {'args': {'projected_balance_minor': -3725}}]:
            with self.subTest(data=data): self.assertFalse(answer('出现37.25元缺口。', data=data)['ok'])

    def test_positive_zero_other_amount_do_not_create_this_deficit(self):
        for value in [0, 7300, -7300]:
            with self.subTest(value=value): self.assertFalse(answer('出现37.25元缺口。', value=value)['ok'])

    def test_same_sentence_false_operation_not_masked(self):
        self.assertFalse(answer('出现37.25元缺口，转账已成功。')['ok'])

    def test_repayment_precedes_valid_gap_without_being_amount_subject(self):
        self.assertTrue(answer('还款后出现37.25元缺口。')['ok'])


    def test_independent_first_draft_counterexamples(self):
        for text in ['余额达到61.89元缺口。', '收入增加了61.89元缺口。',
                     '已入账合计61.89元缺口。', '没有61.89元缺口。']:
            with self.subTest(text=text): self.assertFalse(answer(text,value=-6189)['ok'])

    def test_negated_or_questioned_background_does_not_grant_suffix(self):
        for text in ['没有还款后出现37.25元缺口。', '并非预留后再出现37.25元缺口。',
                     '不会在预留后出现37.25元缺口。', '无需还款后再次出现37.25元缺口。',
                     '是否预留后出现37.25元缺口。', '不是预留后出现37.25元缺口。']:
            with self.subTest(text=text): self.assertFalse(answer(text)['ok'])

    def test_complete_positive_introduction_with_temporal_background(self):
        for text in ['但11-01目标预留后再出现37.25元缺口。', '安排执行后再次出现37.25元的缺口。',
                     '37.25元缺口。', '有37.25元缺口。']:
            with self.subTest(text=text): self.assertTrue(answer(text)['ok'])

    def test_unmodelled_introduction_does_not_expand_permission(self):
        for text in ['余额达到37.25元缺口。', '收入形成37.25元缺口。', '不存在37.25元缺口。',
                     '预计留有37.25元缺口。']:
            with self.subTest(text=text): self.assertFalse(answer(text)['ok'])


if __name__ == '__main__': unittest.main()
