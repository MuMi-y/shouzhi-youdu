"""Cross-service sources and persisted calculation chains on disposable banks."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest

from scripts.g1_goal_demo import seed_new_fixture
from src.bank_v1 import DomainError
from src.bank_evidence.answer import validate
from src.bank_evidence.answer import render_calculations, required
from src.bank_evidence.freshness import recheck
from src.bank_evidence.calculation_store import CalculationStore
from src.bank_evidence.calculation import provenance_reasons
from src.competition_web.app import create_app
from src.competition_web.money_calculate import CalculationError


class LedgerChainTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ledger-chain-')
        self.directory = Path(self.temp.name)
        self.now = datetime(2026, 11, 2, 2, tzinfo=timezone.utc)
        seed_new_fixture(self.directory / 'bank.sqlite3', self.now)
        self.app = create_app(self.directory, clock=lambda: self.now)
        self.service = self.app.extensions['competition']

    def tearDown(self):
        self.temp.cleanup()

    def fact(self, tool='accounts', args=None, field='total_balance_minor', period=None):
        args = args or {}
        response = self.service.call_tool(tool, args)
        facts = response['fact_catalog']['facts']
        fact = next(f for f in facts if f['field'] == field and (period is None or f['period']['relation'] == period))
        return fact, {'kind': 'bank_fact', 'tool': tool, 'arguments': args, 'fact_id': fact['fact_id']}

    def first(self):
        fact, source = self.fact()
        args = {'operation': 'subtract', 'currency': 'CNY', 'left_minor': fact['value'],
                'right_minor': 347500, 'left_source': source,
                'right_source': {'kind': 'user_assumption', 'quote': '先花3475元'}}
        return args, self.service.call_tool('money_calculate', args)

    def next(self, previous, quote='再花125元', amount=12500):
        return {'operation': 'subtract', 'currency': 'CNY',
                'left_minor': previous['data']['calculation']['result']['amount_minor'],
                'right_minor': amount, 'left_source': {'kind': 'calculation_result', 'evidence_id': previous['evidence_id']},
                'right_source': {'kind': 'user_assumption', 'quote': quote}}

    def answer(self, args, response, prompt):
        trace = [{'kind': 'request', 'call_id': 'second', 'name': 'money_calculate', 'arguments': args},
                 {'kind': 'response', 'call_id': 'second', 'tool': 'money_calculate', 'args': args,
                  'ok': True, 'status': 200, 'response': response}]
        payload = {'format': 'bank_facts_v1', 'fact_ids': [], 'calculation_ids': [response['evidence_id']]}
        return validate({'ok': True, 'text': json.dumps(payload), 'trace': trace}, prompt)

    def test_two_steps_persist_restart_render_and_no_bank_writes(self):
        before = self.service.bank.state()
        _, first = self.first()
        self.service.calculations = CalculationStore(self.directory / 'calculations.sqlite3')
        args = self.next(first)
        second = self.service.call_tool('money_calculate', args)
        calc = second['data']['calculation']
        self.assertEqual(3, calc['contract_version'])
        self.assertEqual(640000, calc['result']['amount_minor'])
        self.assertFalse(calc['financial_inputs_verified'])
        checked = self.answer(args, second, '先花3475元，再花125元，请算两步。')
        self.assertTrue(checked['ok'], checked)
        self.assertIn('6525.00元', checked['text'])
        self.assertIn('6400.00元', checked['text'])
        self.assertTrue(recheck(checked, self.service.call_tool)['ok'])
        self.assertEqual(before, self.service.bank.state())

    def test_ancestor_assumption_must_match_original_prompt(self):
        _, first = self.first()
        args = self.next(first)
        second = self.service.call_tool('money_calculate', args)
        self.assertFalse(self.answer(args, second, '先花3400元，再花125元')['ok'])
        reasons = provenance_reasons([{'tool': 'money_calculate', 'response': second}], '再花125元')
        self.assertTrue(reasons)

    def test_render_selected_ancestor_once(self):
        _, first = self.first()
        second = self.service.call_tool('money_calculate', self.next(first))
        calculations = [first['data']['calculation'], second['data']['calculation']]
        self.assertEqual(2, len(render_calculations(calculations)))
        self.assertEqual(render_calculations(calculations), render_calculations(list(reversed(calculations))))

    def test_ledger_calculation_cannot_fall_back_to_number_set_gate(self):
        self.assertTrue(required({'trace': [
            {'kind': 'response', 'tool': 'billing_report', 'response': {'fact_catalog': {}}},
            {'kind': 'response', 'tool': 'money_calculate', 'response': {'data': {'calculation': {'input_provenance': {}}}}}]}))
        self.assertFalse(required({'trace': [
            {'kind': 'response', 'tool': 'billing_report', 'response': {'fact_catalog': {}}}]}))

    def test_bank_change_invalidates_parent_and_final_display(self):
        _, first = self.first()
        args = self.next(first)
        second = self.service.call_tool('money_calculate', args)
        checked = self.answer(args, second, '先花3475元，再花125元')
        self.service.bank.user_action('inject_transaction', {'kind': 'expense', 'amount_minor': 17,
                                      'idempotency_key': 'chain-new-expense'})
        with self.assertRaises(DomainError) as error:
            self.service.call_tool('money_calculate', args)
        self.assertEqual('STALE_OR_UNKNOWN_FACT', error.exception.code)
        self.assertFalse(recheck(checked, self.service.call_tool)['ok'])

    def test_unknown_tampered_amount_and_percentage_parent_rejected(self):
        args, first = self.first()
        chained = self.next(first)
        unknown = deepcopy(chained)
        unknown['left_source']['evidence_id'] = 'calculation:' + 'f' * 64
        with self.assertRaises(DomainError) as error:
            self.service.call_tool('money_calculate', unknown)
        self.assertEqual('UNKNOWN_CALCULATION', error.exception.code)
        chained['left_minor'] += 1
        with self.assertRaises(DomainError) as error:
            self.service.call_tool('money_calculate', chained)
        self.assertEqual('FACT_VALUE_MISMATCH', error.exception.code)
        args['operation'] = 'percentage'
        ratio = self.service.call_tool('money_calculate', args)
        chained['left_source']['evidence_id'] = ratio['evidence_id']
        with self.assertRaises(DomainError) as error:
            self.service.call_tool('money_calculate', chained)
        self.assertEqual('FACT_UNIT_MISMATCH', error.exception.code)

    def test_depth_limit_and_store_integrity(self):
        _, parent = self.first()
        for _ in range(3):
            parent = self.service.call_tool('money_calculate', self.next(parent))
        with self.assertRaises(DomainError) as error:
            self.service.call_tool('money_calculate', self.next(parent))
        self.assertEqual('CALCULATION_CHAIN_LIMIT', error.exception.code)
        altered = deepcopy(parent)
        altered['data']['calculation']['result']['amount_minor'] += 1
        with self.assertRaises(CalculationError):
            self.service.calculations.save(altered)

    def test_billing_periods_and_semantics_resolve_through_insight_reader(self):
        period = {'start_date': '2026-11-01', 'end_date': '2026-11-02'}
        report = self.service.call_tool('billing_report', period)
        groups = report['fact_catalog']['facts']
        same_zero = [f for f in groups if f['field'] == 'expense_minor']
        self.assertEqual(2, len(same_zero))
        self.assertNotEqual(same_zero[0]['fact_id'], same_zero[1]['fact_id'])
        income, source = self.fact('billing_report', period, 'income_minor', '本期')
        expense, other = self.fact('billing_report', period, 'expense_minor', '本期')
        args = {'operation': 'subtract', 'currency': 'CNY', 'left_minor': income['value'],
                'right_minor': expense['value'], 'left_source': source, 'right_source': other}
        response = self.service.call_tool('money_calculate', args)
        self.assertTrue(response['data']['calculation']['financial_inputs_verified'])
        self.assertTrue(self.answer(args, response, '计算该期间收入减消费')['ok'])
        args['left_source']['arguments'] = {'start_date': '2026-10-31', 'end_date': '2026-11-01'}
        with self.assertRaises(DomainError) as error:
            self.service.call_tool('money_calculate', args)
        self.assertEqual('STALE_OR_UNKNOWN_FACT', error.exception.code)

    def test_transaction_direction_period_and_summary(self):
        self.service.bank.user_action('inject_transaction', {'kind': 'expense', 'amount_minor': 1234,
                                      'idempotency_key': 'ledger-direction'})
        envelope = self.service.call_tool('transactions', {'limit': 1})
        fact = envelope['fact_catalog']['facts'][0]
        self.assertEqual(-1234, fact['value'])
        self.assertEqual('POSTED_TRANSACTION', fact['scenario'])
        self.assertEqual('expense', fact['period']['kind'])
        self.assertEqual(1, fact['period']['query_limit'])
        amount, _ = self.fact('spending_summary', {'start_date': '2026-11-02', 'end_date': '2026-11-02'}, 'expense_minor')
        self.assertEqual(1234, amount['value'])
        with self.assertRaises(DomainError):
            self.service.read_fact_source('prepare_transfer', {})


if __name__ == '__main__':
    unittest.main()
