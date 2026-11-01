"""Calendar boundaries describe the observed window, not a future savings rate."""
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.bank_insights import InsightService
from src.bank_insights.fixture import create_demo
from src.bank_insights.rules import build_report


def report(start, end, rows=None, compare=True):
    return build_report(rows or [], start, end, currency='CNY', compare_previous=compare)


class BillingPeriodTests(unittest.TestCase):
    def test_complete_month_lengths(self):
        for start, end, days in [('2026-01-01', '2026-01-31', 31), ('2026-04-01', '2026-04-30', 30),
                                 ('2025-02-01', '2025-02-28', 28), ('2024-02-01', '2024-02-29', 29)]:
            with self.subTest(start=start):
                metadata = report(start, end)['period_metadata']
                self.assertEqual('FULL_CALENDAR_MONTH', metadata['period_kind'])
                self.assertEqual(start[:7], metadata['calendar_month'])
                self.assertEqual(days, metadata['day_count'])

    def test_century_leap_rule(self):
        for start, end, days in [('1900-02-01', '1900-02-28', 28), ('2000-02-01', '2000-02-29', 29),
                                 ('2100-02-01', '2100-02-28', 28)]:
            with self.subTest(start=start):
                metadata = report(start, end)['period_metadata']
                self.assertEqual('FULL_CALENDAR_MONTH', metadata['period_kind'])
                self.assertEqual(days, metadata['day_count'])

    def test_leap_february_without_last_day_is_partial(self):
        metadata = report('2024-02-01', '2024-02-28')['period_metadata']
        self.assertEqual(('DATE_WINDOW', 28, None), (metadata['period_kind'], metadata['day_count'], metadata['calendar_month']))

    def test_cross_month_same_duration_is_not_month(self):
        for start, end, days in [('2026-04-02', '2026-05-01', 30), ('2026-01-12', '2026-02-11', 31),
                                 ('2024-02-02', '2024-03-01', 29)]:
            with self.subTest(start=start):
                metadata = report(start, end)['period_metadata']
                self.assertEqual(('DATE_WINDOW', days, None), (metadata['period_kind'], metadata['day_count'], metadata['calendar_month']))

    def test_cross_year_window(self):
        metadata = report('2025-12-20', '2026-01-19')['period_metadata']
        self.assertEqual('DATE_WINDOW', metadata['period_kind'])
        self.assertEqual(31, metadata['day_count'])

    def test_single_day_inclusive(self):
        metadata = report('2024-02-29', '2024-02-29')['period_metadata']
        self.assertEqual(1, metadata['day_count'])
        self.assertEqual('DATE_WINDOW', metadata['period_kind'])

    def test_several_complete_months_not_single_calendar_month(self):
        metadata = report('2024-01-01', '2024-02-29')['period_metadata']
        self.assertEqual(('DATE_WINDOW', 60, None), (metadata['period_kind'], metadata['day_count'], metadata['calendar_month']))

    def test_partial_month(self):
        for start, end in [('2026-04-02', '2026-04-30'), ('2026-04-01', '2026-04-29')]:
            with self.subTest(start=start): self.assertEqual('DATE_WINDOW', report(start, end)['period_metadata']['period_kind'])

    def test_date_max_month_has_no_overflow(self):
        metadata = report('9999-12-01', '9999-12-31', compare=False)['period_metadata']
        self.assertEqual(('FULL_CALENDAR_MONTH', 31, '9999-12'), (metadata['period_kind'], metadata['day_count'], metadata['calendar_month']))

    def test_date_min_without_comparison(self):
        metadata = report('0001-01-01', '0001-01-31', compare=False)['period_metadata']
        self.assertEqual('0001-01', metadata['calendar_month'])
        self.assertEqual(31, metadata['day_count'])

    def test_metadata_matches_actual_requested_dates(self):
        data = report('2026-05-05', '2026-06-02')
        metadata = data['period_metadata']
        self.assertEqual((data['start_date'], data['end_date']), (metadata['start_date'], metadata['end_date']))
        self.assertEqual('INCLUSIVE', metadata['boundary'])
        self.assertEqual('REQUESTED_DATE_RANGE', metadata['basis'])

    def test_full_month_does_not_claim_complete_data(self):
        data = report('2026-04-01', '2026-04-30')
        self.assertEqual('NOT_ESTABLISHED', data['period_metadata']['coverage_status'])
        self.assertEqual(0, data['totals']['count'])
        self.assertTrue(all(metric['value_percent'] is None for metric in data['metrics']))

    def test_previous_is_independently_classified_partial_month(self):
        data = report('2026-09-01', '2026-09-30')
        previous = data['previous_period']
        self.assertEqual(('2026-08-02', '2026-08-31'), (previous['start_date'], previous['end_date']))
        self.assertEqual('FULL_CALENDAR_MONTH', data['period_metadata']['period_kind'])
        self.assertEqual(('DATE_WINDOW', 30, None), tuple(previous['period_metadata'][key] for key in ('period_kind', 'day_count', 'calendar_month')))

    def test_previous_can_be_a_different_full_month(self):
        data = report('2026-01-01', '2026-01-31')
        previous = data['previous_period']['period_metadata']
        self.assertEqual(('FULL_CALENDAR_MONTH', 31, '2025-12'), tuple(previous[key] for key in ('period_kind', 'day_count', 'calendar_month')))

    def test_leap_previous_crosses_two_months(self):
        previous = report('2024-03-01', '2024-03-31')['previous_period']['period_metadata']
        self.assertEqual(('2024-01-30', '2024-02-29', 31, 'DATE_WINDOW'), tuple(previous[key] for key in ('start_date', 'end_date', 'day_count', 'period_kind')))

    def test_two_rolling_windows_remain_windows(self):
        data = report('2026-06-12', '2026-07-12')
        self.assertEqual('DATE_WINDOW', data['period_metadata']['period_kind'])
        self.assertEqual('DATE_WINDOW', data['previous_period']['period_metadata']['period_kind'])
        self.assertEqual(31, data['previous_period']['period_metadata']['day_count'])

    def test_disabled_comparison_stays_absent(self):
        data = report('2026-04-01', '2026-04-30', compare=False)
        self.assertIsNone(data['previous_period'])
        self.assertEqual('NOT_REQUESTED', data['comparison_status'])
        self.assertEqual([], data['changes'])

    def test_existing_nested_period_shapes_unchanged(self):
        data = report('2026-04-01', '2026-04-30')
        for window in [data, data['previous_period']]:
            expected = {key: window[key] for key in ('start_date', 'end_date')}
            self.assertEqual(expected, window['description_groups']['period'])
            self.assertTrue(all(item['period'] == expected for item in window['metrics']))
        self.assertEqual(2, data['report_contract_version'])

    def test_mixed_flows_not_reclassified_as_savings(self):
        rows = [{'id': str(i), 'date': '2026-04-10', 'account_id': 'A', 'kind': kind,
                 'amount_minor': amount, 'description': kind, 'category': kind}
                for i, (kind, amount) in enumerate([('income', 10000), ('expense', -4000), ('refund', 1000),
                                                   ('OPENING', 5000), ('TRANSFER_OUT', -2000)])]
        data = report('2026-04-01', '2026-04-30', rows)
        self.assertEqual(10000, data['totals']['net_change_minor'])
        self.assertEqual(['40.00', '100.00'], [metric['value_percent'] for metric in data['metrics']])
        self.assertNotIn('savings_minor', data['totals'])


class BillingPeriodServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.now = datetime(2026, 10, 15, tzinfo=timezone.utc)
        self.bank = create_demo(root / 'bank.db', clock=lambda: self.now)
        self.service = InsightService(root / 'insights.db', bank=self.bank, clock=lambda: self.now)

    def tearDown(self): self.temp.cleanup()

    def test_registered_tool_returns_metadata_in_same_snapshot(self):
        before = (self.bank.db_path.read_bytes(), self.service.db_path.read_bytes())
        with patch.object(self.service, '_rows', wraps=self.service._rows) as source:
            result = self.service.call_tool('billing_report', {'start_date': '2026-09-01', 'end_date': '2026-09-30'})
        self.assertEqual(1, source.call_count)
        self.assertEqual('FULL_CALENDAR_MONTH', result['data']['report']['period_metadata']['period_kind'])
        self.assertEqual('DATE_WINDOW', result['data']['report']['previous_period']['period_metadata']['period_kind'])
        self.assertEqual(before, (self.bank.db_path.read_bytes(), self.service.db_path.read_bytes()))

    def test_evidence_distinguishes_empty_windows_and_repeats_stably(self):
        first_args = {'start_date': '2024-02-01', 'end_date': '2024-02-29'}
        first = self.service.call_tool('billing_report', first_args)
        second = self.service.call_tool('billing_report', {'start_date': '2024-02-02', 'end_date': '2024-03-01'})
        self.assertEqual(first['data']['report']['totals'], second['data']['report']['totals'])
        self.assertNotEqual(first['evidence_id'], second['evidence_id'])
        self.assertEqual(first, self.service.call_tool('billing_report', first_args))


if __name__ == '__main__': unittest.main(verbosity=2)
