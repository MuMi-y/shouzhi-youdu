import unittest

from src.bank_evidence.assumption_scope import AssumptionScope
from src.bank_evidence.calculation import assumption_matches
from src.bank_requests.cashflow_dates import discoveries
from src.bank_requests.decision import validate_slot


class SpendingAmountTests(unittest.TestCase):
    def test_original_slot_and_broad_calculation_quote(self):
        message = '我今天想要花费1500买手机，可以吗/'
        for quote in ('花费1500', '1500', '我今天想要花费1500买手机'):
            with self.subTest(quote=quote):
                p = {'quote': quote, 'value_minor': 150000}
                self.assertTrue(assumption_matches(p, message, read_only_spending=True))
                scope = AssumptionScope(((message, 150000),), ((message, 150000),))
                self.assertTrue(assumption_matches(p, scope))

    def test_read_only_capability_is_required(self):
        message = '今天花费1500买手机'
        p = {'quote': '花费1500', 'value_minor': 150000}
        self.assertFalse(assumption_matches(p, message))
        self.assertFalse(assumption_matches(p, AssumptionScope(((message, 150000),))))
        slot = {'value': 150000, 'source': {'message_id': 'u', 'quote': '花费1500'}}
        with self.assertRaisesRegex(ValueError, 'UNBOUND_SLOT_AMOUNT'):
            validate_slot('amount_minor', slot, {'u': message})

    def test_implicit_amount_and_date_variants(self):
        for message, quote, minor in [
            ('明天打算消费2380买桌子', '消费2380', 238000),
            ('后天计划支出45.50买午餐', '支出45.50', 4550),
            ('今天要花870买书，不转账', '花870', 87000),
            ('今天想花1500但不创建计划或转账', '花1500', 150000),
        ]:
            with self.subTest(message=message):
                self.assertTrue(assumption_matches({'quote': quote, 'value_minor': minor}, message, read_only_spending=True))
                roles = discoveries(message, [(0, len(message))])
                self.assertEqual(len(roles['date_text']), 1)
                self.assertFalse(roles['date_text'][0]['uncertain'])

    def test_unbound_or_ambiguous_amounts_stay_rejected(self):
        for message, quote in [
            ('今天买1500型号手机', '1500'),
            ('1500可以吗', '1500'),
            ('今天花1500美元买手机', '花1500'),
            ('今天花1500港币买手机', '花1500'),
            ('今天花1500或2000买手机', '花1500'),
            ('今天花1500到2000买手机', '花1500'),
            ('今天花1500和500买东西', '花1500'),
            ('今天大约花1500买手机', '花1500'),
            ('今天不想花1500买手机', '花1500'),
            ('请别花费1500买手机', '花费1500'),
            ('今天花费1500，或者2000买手机', '花费1500'),
            ('我的预算用美元，今天花费1500买手机', '花费1500'),
            ('今天花15000买手机', '花1500'),
            ('今天花1500k买手机', '花1500'),
            ('今天花1500买手机，明天花1500买电脑', '花1500'),
        ]:
            with self.subTest(message=message):
                self.assertFalse(assumption_matches({'quote': quote, 'value_minor': 150000}, message, read_only_spending=True))

    def test_value_must_equal_literal_amount(self):
        self.assertFalse(assumption_matches({'quote': '花费1500', 'value_minor': 1500}, '今天花费1500买手机', read_only_spending=True))

    def test_explicit_yuan_legacy_still_works(self):
        for text, minor in [('1500元', 150000), ('1.5万元', 1500000), ('1,500.25元', 150025)]:
            with self.subTest(text=text):
                self.assertTrue(assumption_matches({'quote': text, 'value_minor': minor}, text))

    def test_no_inherited_implicit_permission_for_other_amount(self):
        message = '今天花1500买手机'
        scope = AssumptionScope(((message, 150000),), ((message, 50000),))
        self.assertFalse(assumption_matches({'quote': '花1500', 'value_minor': 150000}, scope))


if __name__ == '__main__':
    unittest.main()
