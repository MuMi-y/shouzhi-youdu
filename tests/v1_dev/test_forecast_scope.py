"""Aggregate projections do not silently become individual account capacity."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from src.bank_v1 import Service


class ForecastScopeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.now = datetime(2026, 10, 12, 4, tzinfo=timezone.utc)
        self.service = Service(Path(tmp.name) / 'scope.sqlite3', clock=lambda: self.now)

    def call(self, name, **args):
        return self.service.call_tool(name, args)['data']

    def forecast(self, **extra):
        return self.call('forecast', through_date='2026-11-02', **extra)

    def test_current_snapshot_reconciles_and_excludes_other_person(self):
        f = self.forecast()
        accounts = self.call('accounts')['accounts']
        included = f['account_allocation']['included_accounts']
        self.assertEqual({a['account_id'] for a in included}, {'A', 'B'})
        self.assertEqual(sum(a['current_balance_minor'] for a in included), f['current_balance_minor'])
        self.assertEqual(included, [{'account_id': a['account_id'],
            'current_balance_minor': a['balance_minor'], 'version': a['version']} for a in accounts])
        for a in included:
            self.assertIn(f"account:{a['account_id']}:v{a['version']}", f['evidence_refs'])

    def test_no_account_projection_or_capacity_is_invented(self):
        f = self.forecast(proposed_expense_minor=12345)
        self.assertEqual(f['scope'], 'COMBINED_ACCOUNTS')
        self.assertEqual(f['account_allocation']['status'], 'NOT_MODELED')
        self.assertEqual(f['account_allocation']['arrangement_account_assignment'], 'NOT_RECORDED')
        for key in ('proposed_expense_account_id', 'per_account_projection', 'per_account_transfer_capacity_minor'):
            self.assertIsNone(f['account_allocation'][key])
        self.assertEqual(f['daily'][0]['proposed_expense_minor'], 12345)

    def test_main_and_conservative_retain_different_income_assumptions(self):
        f = self.forecast()
        self.assertEqual(f['current_balance_minor'], 600000)
        self.assertEqual(f['daily'][-1]['projected_balance_minor'], 350000)
        self.assertEqual(f['daily'][-1]['without_future_income_minor'], 100000)
        self.assertEqual(sum(d['expected_income_minor'] for d in f['daily']), 250000)
        self.assertEqual(f['scenario_basis']['conservative_future_income'], 'EXCLUDED')

    def test_date_passage_does_not_post_or_fulfill(self):
        accounts = self.call('accounts')
        transactions = self.call('transactions')
        self.now += timedelta(days=4)
        f = self.forecast()
        self.assertEqual(self.call('accounts'), accounts)
        self.assertEqual(self.call('transactions'), transactions)
        self.assertEqual(f['daily'][0]['planned_outflow_minor'], 300000)
        self.assertFalse(f['scenario_basis']['date_passage_posts_transactions'])

    def test_actual_payment_then_completion_does_not_create_funds(self):
        self.service.user_action('inject_transaction', {'kind': 'expense', 'account_id': 'A',
            'amount_minor': 300000, 'idempotency_key': 'rent-payment'})
        op = self.call('propose_arrangement', arrangement_id='rent', title='固定还款',
            kind='expense', amount_minor=300000, due_date='2026-10-15',
            expected_version=1, status='completed', idempotency_key='rent-complete')['operation']
        self.service.user_action('confirm', {'operation_id': op['operation_id'], 'expected_summary': op['summary']})
        self.now += timedelta(days=4)
        f = self.forecast()
        self.assertEqual(f['current_balance_minor'], 300000)
        self.assertEqual(f['account_allocation']['included_accounts'][0]['current_balance_minor'], 200000)
        self.assertEqual(sum(d['planned_outflow_minor'] for d in f['daily']), 200000)

    def test_income_posted_to_b_does_not_increase_a(self):
        before = self.forecast()['account_allocation']['included_accounts']
        self.service.user_action('inject_transaction', {'kind': 'income', 'account_id': 'B',
            'amount_minor': 250000, 'idempotency_key': 'income-to-b'})
        f = self.forecast()
        after = f['account_allocation']['included_accounts']
        self.assertEqual(after[0], before[0])
        self.assertEqual(after[1]['current_balance_minor'] - before[1]['current_balance_minor'], 250000)
        self.assertIsNone(f['account_allocation']['per_account_transfer_capacity_minor'])
        self.assertEqual(f['current_balance_minor'], 850000)

    def test_overdue_income_not_assumed_posted_in_primary(self):
        self.now += timedelta(days=8)
        f = self.forecast()
        self.assertEqual(f['current_balance_minor'], 600000)
        self.assertEqual(sum(d['expected_income_minor'] for d in f['daily']), 0)
        self.assertEqual(f['overdue_income_excluded'][0]['arrangement_id'], 'salary')


if __name__ == '__main__':
    unittest.main()
