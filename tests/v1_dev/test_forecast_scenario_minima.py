"""Independent scenario minima, preserving forecast money and date semantics."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from src.bank_v1 import finance, events
from src.bank_v1.storage import initialize, connection
from src.bank_v1.common import evidence


class ForecastScenarioMinimaTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'synthetic.sqlite3'
        self.now = datetime(2026, 10, 13, 4, tzinfo=timezone.utc)
        initialize(self.path, self.now - timedelta(days=1))

    def change(self, sql, args=()):
        with connection(self.path, True) as db:
            db.execute(sql, args)

    def forecast(self, end='2026-11-12', **kwargs):
        with connection(self.path) as db:
            return finance.forecast(db, self.now, end, **kwargs)

    def point(self, projection, scenario):
        value = projection['scenario_minima'][scenario]
        return value['minimum_balance_minor'], value['minimum_balance_date'], value['minimum_point']

    def test_daily_counterexample_two_minima_not_two_gaps(self):
        p = self.forecast()
        self.assertEqual(self.point(p, 'PRIMARY'), (300000, '2026-10-15', 'DAILY_PROJECTION'))
        self.assertEqual(self.point(p, 'WITHOUT_FUTURE_INCOME'), (100000, '2026-11-01', 'DAILY_PROJECTION'))
        self.assertEqual((p['gap_minor'], p['without_future_income_gap_minor']), (0, 0))

    def test_aliases_and_explicit_scope(self):
        p = self.forecast()
        a, b = p['scenario_minima']['PRIMARY'], p['scenario_minima']['WITHOUT_FUTURE_INCOME']
        self.assertEqual((p['lowest_balance_minor'], p['lowest_balance_date']),
                         (a['minimum_balance_minor'], a['minimum_balance_date']))
        self.assertEqual((p['without_future_income_lowest_balance_minor'], p['without_future_income_lowest_balance_date']),
                         (b['minimum_balance_minor'], b['minimum_balance_date']))
        self.assertEqual(p['scenario_minima']['scope'], p['scope'])
        self.assertEqual((p['scenario_minima']['window_start'], p['scenario_minima']['window_end']),
                         (p['as_of_date'], p['through_date']))
        self.assertEqual(b['future_income'], p['scenario_basis']['conservative_future_income'])
        self.assertEqual(a['future_income'], p['scenario_basis']['primary_future_income'])

    def test_no_income_same_amount_and_date(self):
        self.change("DELETE FROM arrangements WHERE kind='income'")
        p = self.forecast()
        self.assertEqual(self.point(p, 'PRIMARY'), self.point(p, 'WITHOUT_FUTURE_INCOME'))
        self.assertEqual(self.point(p, 'PRIMARY')[:2], (100000, '2026-11-01'))

    def test_negative_minimum_is_signed_and_gap_positive(self):
        p = self.forecast(proposed_expense_minor=400000, proposed_date='2026-10-14')
        self.assertEqual(self.point(p, 'PRIMARY')[:2], (-100000, '2026-10-15'))
        self.assertEqual(self.point(p, 'WITHOUT_FUTURE_INCOME')[:2], (-300000, '2026-11-01'))
        self.assertEqual((p['gap_minor'], p['without_future_income_gap_minor']), (100000, 300000))
        self.assertEqual(p['first_without_income_gap_date'], '2026-10-15')

    def test_zero_minimum_is_not_negative_gap(self):
        p = self.forecast(proposed_expense_minor=100000)
        self.assertEqual(self.point(p, 'WITHOUT_FUTURE_INCOME')[:2], (0, '2026-11-01'))
        self.assertEqual(p['without_future_income_gap_minor'], 0)
        self.assertIsNone(p['first_without_income_gap_date'])

    def test_no_arrangements_earliest_daily_tie(self):
        self.change('DELETE FROM arrangements')
        p = self.forecast()
        for key in ('PRIMARY', 'WITHOUT_FUTURE_INCOME'):
            self.assertEqual(self.point(p, key), (600000, '2026-10-13', 'DAILY_PROJECTION'))

    def test_current_snapshot_minimum_when_today_income_raises_daily(self):
        self.change("DELETE FROM arrangements WHERE kind!='income'")
        self.change("UPDATE arrangements SET due_date='2026-10-13'")
        p = self.forecast()
        self.assertEqual(self.point(p, 'PRIMARY'), (600000, '2026-10-13', 'CURRENT_SNAPSHOT'))
        self.assertEqual(self.point(p, 'WITHOUT_FUTURE_INCOME'), (600000, '2026-10-13', 'DAILY_PROJECTION'))
        self.assertEqual(p['daily'][0]['projected_balance_minor'], 850000)

    def test_initial_minimum_equal_later_daily_retains_legacy_date(self):
        self.change("DELETE FROM arrangements WHERE kind='goal'")
        self.change("UPDATE arrangements SET due_date='2026-10-13' WHERE kind='income'")
        self.change("UPDATE arrangements SET amount_minor=250000 WHERE kind='expense'")
        p = self.forecast()
        self.assertEqual(self.point(p, 'PRIMARY'), (600000, '2026-10-15', 'DAILY_PROJECTION'))
        self.assertEqual(p['lowest_balance_date'], '2026-10-15')

    def test_day0_outflow_minimum(self):
        self.change("DELETE FROM arrangements WHERE arrangement_id!='rent'")
        self.change("UPDATE arrangements SET due_date='2026-10-13'")
        self.assertEqual(self.point(self.forecast(), 'PRIMARY'), (300000, '2026-10-13', 'DAILY_PROJECTION'))

    def test_window_excludes_later_minimum(self):
        p = self.forecast('2026-10-18')
        self.assertEqual(self.point(p, 'WITHOUT_FUTURE_INCOME')[:2], (300000, '2026-10-15'))
        self.assertEqual(p['scenario_minima']['window_end'], '2026-10-18')

    def test_today_only_window(self):
        p = self.forecast('2026-10-13', proposed_expense_minor=12345)
        self.assertEqual(self.point(p, 'PRIMARY')[:2], (587655, '2026-10-13'))
        self.assertEqual(len(p['daily']), 1)

    def test_overdue_income_remains_excluded(self):
        self.change("UPDATE arrangements SET due_date='2026-10-12' WHERE kind='income'")
        p = self.forecast()
        self.assertEqual(self.point(p, 'PRIMARY'), (100000, '2026-11-01', 'DAILY_PROJECTION'))
        self.assertEqual(self.point(p, 'PRIMARY'), self.point(p, 'WITHOUT_FUTURE_INCOME'))
        self.assertEqual(len(p['overdue_income_excluded']), 1)

    def test_facts_pass_same_structure_and_separate_dates(self):
        p = self.forecast()
        facts = events.facts(p)
        self.assertIs(facts['scenario_minima'], p['scenario_minima'])
        self.assertIn('主预测窗口最低合计余额为 3000.00 元，对应 2026-10-15', facts['summary'])
        self.assertIn('不计未来收入的保守预测窗口最低合计余额为 1000.00 元，对应 2026-11-01', facts['summary'])
        self.assertEqual(facts['gap_minor'], 0)

    def test_legacy_projection_does_not_invent_minima(self):
        p = self.forecast()
        for key in ('scenario_minima', 'without_future_income_lowest_balance_minor', 'without_future_income_lowest_balance_date'):
            del p[key]
        facts = events.facts(p)
        self.assertNotIn('scenario_minima', facts)
        self.assertNotIn('窗口最低', facts['summary'])

    def test_context_same_snapshot_and_no_database_mutation(self):
        self.change("INSERT INTO events VALUES('daily_test','daily_test','due_check','{}','2026-10-13T04:00:00+00:00','PENDING',0,NULL,NULL)")
        before = self.path.read_bytes()
        with connection(self.path) as db:
            result = events.context(db, 'daily_test', self.now)
        self.assertEqual(before, self.path.read_bytes())
        self.assertIs(result['facts']['scenario_minima'], result['forecast']['scenario_minima'])
        self.assertEqual(result['bank_transaction_status'], 'NOT_APPLICABLE')
        self.assertEqual(result['analysis_status'], 'PENDING')

    def test_full_evidence_hash_binds_minima_not_just_old_refs(self):
        p = self.forecast()
        old = dict(p)
        del old['scenario_minima']
        self.assertEqual(old['evidence_refs'], p['evidence_refs'])
        self.assertNotEqual(evidence(old, self.now)['evidence_id'], evidence(p, self.now)['evidence_id'])


if __name__ == '__main__':
    unittest.main()
