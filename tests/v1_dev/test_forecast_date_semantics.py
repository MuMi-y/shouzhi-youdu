"""Synthetic SQLite fixtures: projection dates do not assert real-world delinquency."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from src.bank_v1 import finance, events
from src.bank_v1.storage import initialize, connection
from src.bank_v1.common import evidence
from src.bank_v1_web.worker import event_prompt


class ForecastDateSemanticsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'date-fixture.sqlite3'
        self.origin = datetime(2026, 10, 12, 4, tzinfo=timezone.utc)
        initialize(self.path, self.origin)

    def projection(self, offset=0, through='2026-11-02', **kwargs):
        with connection(self.path) as db:
            return finance.forecast(db, self.origin+timedelta(days=offset), through, **kwargs)

    def timing(self, projection, identifier='rent'):
        return next(x for x in projection['arrangement_timing']['items'] if x['arrangement_id'] == identifier)

    def update_fixture(self, sql, params=()):
        with connection(self.path, True) as db:
            db.execute(sql, params)

    def test_future_original_date_retained(self):
        p = self.projection()
        item = self.timing(p)
        self.assertEqual(item['original_due_date'], '2026-10-15')
        self.assertEqual(item['forecast_effective_date'], '2026-10-15')
        self.assertEqual(item['inclusion_reason'], 'ORIGINAL_DATE_IN_WINDOW')
        self.assertEqual(p['daily'][3]['planned_outflow_minor'], 300000)

    def test_today_is_original_date_not_carried(self):
        p = self.projection(3)
        item = self.timing(p)
        self.assertEqual(item['original_due_date'], p['as_of_date'])
        self.assertEqual(item['inclusion_reason'], 'ORIGINAL_DATE_IN_WINDOW')
        self.assertNotIn('原定日期已过', ''.join(events.facts(p)['next_steps']))

    def test_past_unconfirmed_outflow_has_two_dates(self):
        p = self.projection(7)
        item = self.timing(p)
        self.assertEqual((item['original_due_date'], item['forecast_effective_date']), ('2026-10-15','2026-10-19'))
        self.assertEqual(item['inclusion_reason'], 'PAST_OUTFLOW_UNCONFIRMED_CARRIED_FORWARD')
        self.assertEqual(item['fulfillment_confirmation'], 'NOT_CONFIRMED')
        self.assertEqual(p['daily'][0]['planned_outflow_minor'], 300000)
        self.assertEqual(p['daily'][0]['arrangement_refs'], ['rent:v1','salary:v1'])
        self.assertEqual(p['arrangement_timing']['legal_overdue_status'], 'NOT_DETERMINED')

    def test_completed_excluded_is_record_not_transaction_proof(self):
        self.update_fixture("UPDATE arrangements SET status='completed' WHERE arrangement_id='rent'")
        p = self.projection(7)
        item = self.timing(p)
        self.assertEqual(item['inclusion_reason'], 'COMPLETED_EXCLUDED')
        self.assertEqual(item['fulfillment_confirmation'], 'RECORDED_COMPLETED')
        self.assertIsNone(item['forecast_effective_date'])
        self.assertFalse(item['included_in_forecast'])
        self.assertEqual(p['daily'][0]['planned_outflow_minor'], 0)
        self.assertEqual(p['current_balance_minor'], 600000)

    def test_cancelled_not_asserted_fulfilled(self):
        self.update_fixture("UPDATE arrangements SET status='cancelled' WHERE arrangement_id='rent'")
        item = self.timing(self.projection(7))
        self.assertEqual(item['inclusion_reason'], 'CANCELLED_EXCLUDED')
        self.assertEqual(item['fulfillment_confirmation'], 'NOT_ASSERTED')
        self.assertIsNone(item['forecast_effective_date'])

    def test_beyond_window_has_no_projection_date(self):
        item = self.timing(self.projection(0, '2026-10-14'))
        self.assertEqual(item['original_due_date'], '2026-10-15')
        self.assertEqual(item['inclusion_reason'], 'AFTER_WINDOW_EXCLUDED')
        self.assertIsNone(item['forecast_effective_date'])

    def test_past_income_not_moved_to_today(self):
        p = self.projection(8)
        item = self.timing(p, 'salary')
        self.assertEqual(item['inclusion_reason'], 'PAST_INCOME_UNCONFIRMED_EXCLUDED')
        self.assertIsNone(item['forecast_effective_date'])
        self.assertEqual(sum(x['expected_income_minor'] for x in p['daily']), 0)
        self.assertEqual(p['income_conditions'], [])
        self.assertEqual(p['overdue_income_excluded'][0]['due_date'], '2026-10-19')

    def test_future_income_is_assumption_not_posted(self):
        p = self.projection()
        item = self.timing(p, 'salary')
        self.assertEqual(item['forecast_effective_date'], '2026-10-19')
        self.assertEqual(p['arrangement_timing']['inclusion_scope'], 'PRIMARY_SCENARIO')
        self.assertEqual(p['arrangement_timing']['conservative_future_income'], 'EXCLUDED')
        self.assertEqual(item['fulfillment_confirmation'], 'NOT_CONFIRMED')
        self.assertEqual(p['current_balance_minor'], 600000)
        self.assertEqual(p['daily'][-1]['projected_balance_minor'], 350000)
        self.assertEqual(p['daily'][-1]['without_future_income_minor'], 100000)
        self.assertFalse(p['arrangement_timing']['date_passage_posts_transactions'])

    def test_goal_is_carried_without_legal_status(self):
        p = self.projection(21)
        item = self.timing(p, 'laptop')
        self.assertEqual(item['kind'], 'goal')
        self.assertEqual(item['original_due_date'], '2026-11-01')
        self.assertEqual(item['forecast_effective_date'], '2026-11-02')
        self.assertEqual(item['inclusion_reason'], 'PAST_OUTFLOW_UNCONFIRMED_CARRIED_FORWARD')
        self.assertEqual(p['arrangement_timing']['legal_overdue_status'], 'NOT_DETERMINED')

    def test_latest_version_only(self):
        self.update_fixture("INSERT INTO arrangements SELECT arrangement_id,2,title,kind,amount_minor,'2026-10-22',created_at,source,status FROM arrangements WHERE arrangement_id='rent'")
        p = self.projection(7)
        item = self.timing(p)
        self.assertEqual(item['version'], 2)
        self.assertEqual(item['evidence_ref'], 'rent:v2')
        self.assertEqual(item['forecast_effective_date'], '2026-10-22')
        self.assertEqual(len(p['arrangement_timing']['items']), 3)

    def test_beijing_day_boundary(self):
        with connection(self.path) as db:
            before = finance.forecast(db, datetime(2026,10,15,15,59,tzinfo=timezone.utc), '2026-11-02')
            after = finance.forecast(db, datetime(2026,10,15,16,0,tzinfo=timezone.utc), '2026-11-02')
        self.assertEqual(self.timing(before)['inclusion_reason'], 'ORIGINAL_DATE_IN_WINDOW')
        self.assertEqual(self.timing(after)['forecast_effective_date'], '2026-10-16')
        self.assertEqual(self.timing(after)['original_due_date'], '2026-10-15')

    def test_facts_use_original_and_effective_dates(self):
        p = self.projection(7)
        f = events.facts(p)
        self.assertEqual(f['arrangement_timing'], p['arrangement_timing'])
        self.assertIn('原定 2026-10-15', f['summary'])
        self.assertIn('保守计入 2026-10-19', f['summary'])
        self.assertIn('履行尚未确认', f['summary'])
        self.assertIn('不能据此断言现实已逾期或违约', f['summary'])
        self.assertIn('不要仅因预测再次计入而重复付款', ''.join(f['next_steps']))

    def test_legacy_projection_without_timing_does_not_invent_dates(self):
        p = self.projection(7)
        del p['arrangement_timing']
        f = events.facts(p)
        self.assertNotIn('arrangement_timing', f)
        self.assertNotIn('原定 2026-10-15', f['summary'])
        self.assertEqual(f['gap_minor'], p['gap_minor'])

    def test_event_context_real_shape_propagates_without_db_mutation(self):
        self.update_fixture("INSERT INTO events VALUES('test_weekly','test_weekly','due_check','{}','2026-10-19T04:00:00+00:00','PENDING',0,NULL,NULL)")
        before = self.path.read_bytes()
        with connection(self.path) as db:
            c = events.context(db, 'test_weekly', self.origin+timedelta(days=7))
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(c['facts']['arrangement_timing'], c['forecast']['arrangement_timing'])
        self.assertEqual(next(x for x in c['arrangements'] if x['arrangement_id']=='rent')['due_date'], '2026-10-15')
        self.assertEqual(c['analysis_status'], 'PENDING')
        self.assertEqual(c['bank_transaction_status'], 'NOT_APPLICABLE')

    def test_evidence_hash_binds_new_metadata(self):
        p = self.projection(7)
        original = dict(p)
        del original['arrangement_timing']
        self.assertNotEqual(evidence(p, self.origin)['evidence_id'], evidence(original, self.origin)['evidence_id'])

    def test_worker_prompt_preserves_readonly_scope_and_date_limit(self):
        prompt = event_prompt('event_fixture')
        for text in ('event_context', 'facts', '原定日期与预测计入日期', '不表示今日到期', '不发起转账或变更'):
            self.assertIn(text, prompt)


if __name__ == '__main__':
    unittest.main()
