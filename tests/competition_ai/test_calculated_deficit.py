"""A signed calculation may be worded as a deficit, never as available money."""
import unittest
from src.bank_v1_web.bank_answer import validate_bank_answer


def evaluate(text, result=None, *, inputs=None, success=True, tool='money_calculate', data=None):
    data = data if data is not None else {'calculation': {'result': result or {}, 'inputs': inputs or {}}}
    return validate_bank_answer({'ok': True, 'text': text, 'trace': [
        {'kind': 'response', 'tool': tool, 'ok': success, 'response': {'scope': 'bank', 'data': data}}
    ]}, '请核对资金状况。')


class CalculatedDeficitTests(unittest.TestCase):
    def test_signed_result_positive_deficit(self):
        self.assertTrue(evaluate('资金缺口为37.25元。', {'amount_minor': -3725})['ok'])

    def test_actual_x04_thousands_display(self):
        self.assertTrue(evaluate('单账户还差1,655.00元。', {'amount_minor': -165500})['ok'])

    def test_explicit_shortfall_magnitude(self):
        self.assertTrue(evaluate('仍差 37.25 元。', {'amount_minor': -3725})['ok'])

    def test_signed_result_cannot_be_positive_balance(self):
        self.assertFalse(evaluate('余额为37.25元。', {'amount_minor': -3725})['ok'])

    def test_signed_result_cannot_be_income(self):
        self.assertFalse(evaluate('收入37.25元。', {'amount_minor': -3725})['ok'])

    def test_signed_result_cannot_be_available_money(self):
        self.assertFalse(evaluate('可用资金37.25元。', {'amount_minor': -3725})['ok'])

    def test_deficit_does_not_whitelist_other_occurrence(self):
        self.assertFalse(evaluate('还差37.25元，余额37.25元。', {'amount_minor': -3725})['ok'])

    def test_no_result_cannot_support_deficit(self):
        self.assertFalse(evaluate('缺口37.25元。')['ok'])

    def test_inputs_are_not_results(self):
        self.assertFalse(evaluate('缺口37.25元。', inputs={'amount_minor': -3725})['ok'])

    def test_string_result_is_not_typed_integer(self):
        self.assertFalse(evaluate('缺口37.25元。', {'amount_minor': '-3725'})['ok'])

    def test_description_is_not_result(self):
        self.assertFalse(evaluate('缺口37.25元。', {'description': '-37.25元'})['ok'])

    def test_failed_tool_does_not_support_result(self):
        self.assertFalse(evaluate('缺口37.25元。', {'amount_minor': -3725}, success=False)['ok'])

    def test_wrong_magnitude_rejected(self):
        self.assertFalse(evaluate('缺口37.26元。', {'amount_minor': -3725})['ok'])

    def test_original_signed_display_still_supported(self):
        self.assertTrue(evaluate('计算结果为-37.25元。', {'amount_minor': -3725})['ok'])

    def test_forecast_negative_balance_display_preserved(self):
        self.assertTrue(evaluate('仍差37.25元。', tool='forecast', data={'balance_minor': -3725})['ok'])

    def test_negative_calculated_percentage_not_globally_absolute(self):
        self.assertFalse(evaluate('实际涨幅10%。', {'percent': '-10'})['ok'])


    def nested(self, calculation, *, state='SUCCEEDED'):
        return {'task': {'task_id': 'synthetic-task', 'state': 'RUNNING', 'nodes': [
            {'id': 'calc', 'tool': 'money_calculate', 'state': state,
             'result': {'scope': 'bank', 'data': {'calculation': calculation}}}]}}

    def test_direct_and_successful_nested_calculation_equivalent(self):
        result = {'amount_minor': -3725}
        direct = evaluate('还差37.25元。', result)
        nested = evaluate('还差37.25元。', tool='task_status', data=self.nested({'result': result}))
        self.assertTrue(direct['ok'])
        self.assertEqual(direct['ok'], nested['ok'])

    def test_nested_inputs_do_not_support_deficit(self):
        data = self.nested({'inputs': {'amount_minor': -3725}, 'result': {}})
        self.assertFalse(evaluate('还差37.25元。', tool='task_status', data=data)['ok'])

    def test_nested_string_result_does_not_support_deficit(self):
        data = self.nested({'result': {'amount_minor': '-3725'}})
        self.assertFalse(evaluate('还差37.25元。', tool='task_status', data=data)['ok'])

    def test_nested_wrong_magnitude_rejected(self):
        data = self.nested({'inputs': {'amount_minor': -9900}, 'result': {'amount_minor': -3725}})
        self.assertFalse(evaluate('还差99.00元。', tool='task_status', data=data)['ok'])

    def test_nested_positive_balance_cannot_borrow_deficit(self):
        data = self.nested({'result': {'amount_minor': -3725}})
        self.assertFalse(evaluate('余额37.25元。', tool='task_status', data=data)['ok'])

    def test_unsuccessful_task_node_cannot_support_deficit(self):
        for state in ['PENDING', 'FAILED', 'RUNNING']:
            with self.subTest(state=state):
                data = self.nested({'result': {'amount_minor': -3725}}, state=state)
                self.assertFalse(evaluate('还差37.25元。', tool='task_status', data=data)['ok'])


if __name__ == '__main__':
    unittest.main()
