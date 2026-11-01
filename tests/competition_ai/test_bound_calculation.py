"""Real facade reads on disposable synthetic banks; no model or private data."""
from copy import deepcopy
from decimal import localcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import tempfile
import unittest

from scripts.g1_goal_demo import seed_new_fixture
from src.bank_v1 import DomainError
from src.bank_v1_web.bank_answer import validate_bank_answer
from src.competition_web.app import create_app
from src.bank_evidence.calculation import calculate_bound, assumption_matches
from src.competition_web.money_calculate import CalculationError


class BoundCalculationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bound-bank-')
        self.directory = Path(self.temp.name)
        self.now = datetime(2026, 11, 2, 2, tzinfo=timezone.utc)
        seed_new_fixture(self.directory / 'bank.sqlite3', self.now)
        self.app = create_app(self.directory, clock=lambda: self.now)
        self.service = self.app.extensions['competition']
        self.client = self.app.test_client()
        self.headers = {'Authorization': 'Bearer ' + self.app.extensions['bank_v1']['tool_token']}

    def tearDown(self):
        self.temp.cleanup()

    def reference(self, entity='COMBINED_ACCOUNTS'):
        envelope = self.service.call_tool('accounts', {})
        fact = next(f for f in envelope['fact_catalog']['facts'] if f['entity'] == entity)
        return fact['value'], {'kind': 'bank_fact', 'tool': 'accounts',
                               'arguments': {}, 'fact_id': fact['fact_id']}

    def args(self, amount=350000, entity='COMBINED_ACCOUNTS'):
        value, source = self.reference(entity)
        return {'operation': 'subtract', 'left_minor': value, 'right_minor': amount,
                'currency': 'CNY', 'left_source': source,
                'right_source': {'kind': 'user_assumption', 'quote': f'拟消费{amount / 100:.2f}元'}}

    def checked(self, response, args, prompt, text='拟消费后的计算结果为6500元。'):
        trace = [{'kind': 'request', 'call_id': 'calc', 'name': 'money_calculate', 'arguments': args},
                 {'kind': 'response', 'call_id': 'calc', 'tool': 'money_calculate',
                  'args': args, 'ok': True, 'status': 200, 'response': response}]
        payload = {'format': 'bank_facts_v1', 'fact_ids': [], 'calculation_ids': [response['evidence_id']]}
        return validate_bank_answer({'ok': True, 'text': json.dumps(payload), 'trace': trace}, prompt)

    def test_http_total_minus_proposal_and_changed_amount_without_bank_mutation(self):
        before = self.service.bank.state()
        for amount, expected in [(350000, 650000), (347500, 652500)]:
            with self.subTest(amount=amount):
                args = self.args(amount)
                response = self.client.post('/api/tools/money_calculate', json=args, headers=self.headers)
                self.assertEqual(200, response.status_code, response.json)
                calc = response.json['data']['calculation']
                self.assertEqual(expected, calc['result']['amount_minor'])
                self.assertFalse(calc['financial_inputs_verified'])
                fact = calc['input_provenance']['left']['fact']
                self.assertEqual('COMBINED_ACCOUNTS', fact['entity'])
                self.assertEqual('total_balance_minor', fact['field'])
                checked = self.checked(response.json, args, args['right_source']['quote'],
                                       f"拟消费后的计算结果为{calc['result']['amount']}元。")
                self.assertTrue(checked['ok'], checked)
        self.assertEqual(before, self.service.bank.state())

    def test_same_reference_cannot_be_used_with_other_account_value(self):
        args = self.args(entity='A')
        args['left_minor'] = self.reference('B')[0]
        with self.assertRaises(DomainError) as error:
            self.service.call_tool('money_calculate', args)
        self.assertEqual('FACT_VALUE_MISMATCH', error.exception.code)

    def test_source_identity_includes_entity_even_for_equal_balances(self):
        envelope = self.service.call_tool('accounts', {})
        from src.bank_evidence.facts import catalog
        envelope = deepcopy(envelope)
        for account in envelope['data']['accounts']:
            account['balance_minor'] = 500000
        facts = catalog('accounts', {}, envelope)['facts']
        self.assertNotEqual(facts[0]['fact_id'], facts[1]['fact_id'])
        self.assertEqual(facts[0]['value'], facts[1]['value'])

    def test_posted_expense_invalidates_old_reference_and_refresh_changes_calculation(self):
        args = self.args()
        self.service.bank.user_action('inject_transaction', {
            'kind': 'expense', 'amount_minor': 10000, 'idempotency_key': 'bound-new-expense'})
        with self.assertRaises(DomainError) as error:
            self.service.call_tool('money_calculate', args)
        self.assertEqual('STALE_OR_UNKNOWN_FACT', error.exception.code)
        refreshed = self.service.call_tool('money_calculate', self.args())
        self.assertEqual(640000, refreshed['data']['calculation']['result']['amount_minor'])

    def test_new_business_day_invalidates_reference_even_when_balance_unchanged(self):
        args = self.args()
        self.now += timedelta(days=1)
        with self.assertRaises(DomainError) as error:
            self.service.call_tool('money_calculate', args)
        self.assertEqual('STALE_OR_UNKNOWN_FACT', error.exception.code)

    def test_untrusted_assumption_is_not_accepted_from_tool_echo(self):
        args = self.args()
        response = self.service.call_tool('money_calculate', args)
        for prompt in ['我拟消费3475.00元', '请查我的余额', '我拟消费35000.00元']:
            with self.subTest(prompt=prompt):
                checked = self.checked(response, args, prompt)
                self.assertFalse(checked['ok'])
                self.assertEqual('UNBOUND_USER_CALCULATION_ASSUMPTION',
                                 checked['answer_validation']['reasons'][0]['code'])

    def test_original_quote_with_wrong_numeric_operand_rejected(self):
        args = self.args()
        args['right_source']['quote'] = '拟消费3475元'
        response = self.service.call_tool('money_calculate', args)
        self.assertFalse(self.checked(response, args, '拟消费3475元')['ok'])
        self.assertFalse(assumption_matches({'quote': '3500元或3475元', 'value_minor': 350000}, '3500元或3475元'))

    def test_assumption_matching_does_not_inherit_decimal_rounding(self):
        with localcontext() as context:
            context.prec = 1
            self.assertTrue(assumption_matches({'quote': '3475元', 'value_minor': 347500}, '3475元'))
            self.assertFalse(assumption_matches({'quote': '3501元', 'value_minor': 400000}, '3501元'))

    def test_new_sources_are_both_required_and_never_call_write_tools(self):
        args = self.args()
        del args['right_source']
        with self.assertRaises(DomainError):
            self.service.call_tool('money_calculate', args)
        args = self.args()
        args['left_source']['tool'] = 'prepare_transfer'
        with self.assertRaises(DomainError):
            self.service.call_tool('money_calculate', args)
        self.assertEqual([], self.service.tasks.list())

    def test_wrong_currency_and_date_as_money_rejected(self):
        args = self.args()
        args['currency'] = None
        with self.assertRaises(DomainError) as error:
            self.service.call_tool('money_calculate', args)
        self.assertEqual('FACT_UNIT_MISMATCH', error.exception.code)
        facts = self.service.call_tool('arrangements', {})['fact_catalog']['facts']
        due = next(f for f in facts if f['unit'] == 'date')
        args = self.args()
        args['left_source'] = {'kind': 'bank_fact', 'tool': 'arrangements',
                               'arguments': {}, 'fact_id': due['fact_id']}
        with self.assertRaises(DomainError) as error:
            self.service.call_tool('money_calculate', args)
        self.assertEqual('FACT_UNIT_MISMATCH', error.exception.code)

    def test_two_bank_operands_preserve_meaning_and_verify_sources(self):
        left, ls = self.reference('A'); right, rs = self.reference('B')
        result = self.service.call_tool('money_calculate', {'operation': 'add',
            'left_minor': left, 'right_minor': right, 'currency': 'CNY',
            'left_source': ls, 'right_source': rs})['data']['calculation']
        self.assertEqual(1000000, result['result']['amount_minor'])
        self.assertTrue(result['financial_inputs_verified'])
        self.assertEqual('A', result['input_provenance']['left']['fact']['entity'])
        self.assertEqual('B', result['input_provenance']['right']['fact']['entity'])

    def test_change_during_multi_query_read_is_explicit_failure(self):
        args = self.args()
        calls = []
        def reader(tool, query):
            calls.append(tool)
            if len(calls) == 2:
                self.service.bank.user_action('inject_transaction', {
                    'kind': 'expense', 'amount_minor': 17, 'idempotency_key': 'during-read'})
            return self.service.bank.call_tool(tool, query)
        with self.assertRaises(CalculationError) as error:
            calculate_bound(args, reader)
        self.assertEqual('FACT_CHANGED_DURING_CALCULATION', error.exception.code)

    def test_forecast_catalog_keeps_scenarios_and_dates_together(self):
        response = self.service.call_tool('forecast', {'through_date': '2026-11-30'})
        minima = [f for f in response['fact_catalog']['facts'] if f['field'] == 'minimum_balance_minor']
        self.assertEqual({'PRIMARY', 'WITHOUT_FUTURE_INCOME'}, {f['scenario'] for f in minima})
        for fact in minima:
            source = response['data']['scenario_minima'][fact['scenario']]
            self.assertEqual(source['minimum_balance_minor'], fact['value'])
            self.assertEqual(source['minimum_balance_date'], fact['period']['minimum_date'])
            self.assertEqual('COMBINED_ACCOUNTS', fact['entity'])


if __name__ == '__main__':
    unittest.main()
