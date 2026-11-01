import json
from pathlib import Path
import unittest

from src.bank_insights.rules import build_report
from src.bank_v1_web.bank_answer import validate_bank_answer
from src.bank_v1_web.billing_periods import billing_previous_period_checks


def report(start='2026-01-01', end='2026-10-12', compare=False):
    return build_report([], start, end, currency='CNY', compare_previous=compare)


def trace(data, *, ok=True, scope='bank'):
    return [{'kind': 'response', 'tool': 'billing_report', 'ok': ok,
             'response': {'scope': scope, 'data': {'report': data}}}]


class PreviousPeriodEvidenceTests(unittest.TestCase):
    def test_optional_dates_do_not_claim_data_queried(self):
        data = report()
        self.assertEqual(('2025-03-22', '2025-12-31', 285), tuple(data['comparison_window'][k] for k in ('start_date', 'end_date', 'day_count')))
        self.assertEqual('NOT_REQUESTED', data['comparison_window']['data_status'])
        self.assertIsNone(data['previous_period'])
        self.assertEqual([], data['changes'])

    def test_actual_comparison_reuses_same_window(self):
        data = report(compare=True)
        self.assertEqual('QUERIED', data['comparison_window']['data_status'])
        for key in ('start_date', 'end_date'):
            self.assertEqual(data['previous_period'][key], data['comparison_window'][key])

    def test_calendar_edges(self):
        for start, end, expected in [
            ('2024-03-01', '2024-03-31', ('2024-01-30', '2024-02-29', 31)),
            ('2024-02-29', '2024-02-29', ('2024-02-28', '2024-02-28', 1)),
            ('2026-01-01', '2026-01-02', ('2025-12-30', '2025-12-31', 2))]:
            with self.subTest(start=start):
                window = report(start, end)['comparison_window']
                self.assertEqual(expected, tuple(window[k] for k in ('start_date', 'end_date', 'day_count')))

    def test_minimum_supported_date_without_comparison(self):
        self.assertIsNone(report('0001-01-01', '0001-01-31')['comparison_window'])

    def test_original_real_failure_is_rejected_without_mutating_evidence(self):
        path = Path(__file__).resolve().parent / 'fixtures/b02_date_failure_20261019.json'
        original = path.read_bytes()
        turn = json.loads(original)['turn']
        result = validate_bank_answer(turn['result'], turn['question'])
        self.assertFalse(result['ok'])
        self.assertIn('UNSUPPORTED_BILLING_PREVIOUS_PERIOD', [r['code'] for r in result['answer_validation']['reasons']])
        self.assertEqual(original, path.read_bytes())

    def test_correct_pairs_in_multiple_formats(self):
        for text in ['前一等长期间为2025-03-22至2025-12-31。',
                     '如需与2025年3月22日到2025年12月31日的等长前期比较，可以告诉我。',
                     '**上期：2025/03/22 ～ 2025/12/31**，尚未查询资料。']:
            with self.subTest(text=text):
                result = billing_previous_period_checks(text, trace(report()))
                self.assertEqual([], result['reasons'])
                self.assertEqual('VERIFIED_PAIR', result['checks'][0]['status'])

    def test_conditional_wrong_pair_not_exempt(self):
        result = billing_previous_period_checks('如需与2025-03-26至2026-01-01的等长前期对比，可以告诉我。', trace(report()))
        self.assertEqual('UNSUPPORTED_BILLING_PREVIOUS_PERIOD', result['reasons'][0]['code'])

    def test_parenthesized_period_not_left_unchecked(self):
        for text in ['前一等长期间（2025-03-22至2025-12-31，共285天）',
                     '（2025-03-22至2025-12-31）的等长前期']:
            checked = billing_previous_period_checks(text, trace(report()))
            self.assertEqual('VERIFIED_PAIR', checked['checks'][0]['status'])
        bad = billing_previous_period_checks('前一等长期间（2025-03-26至2026-01-01）', trace(report()))
        self.assertTrue(bad['reasons'])

    def test_pairs_not_bag_of_known_dates(self):
        self.assertTrue(billing_previous_period_checks('上期为2025-03-22至2026-01-01。', trace(report()))['reasons'])

    def test_failed_foreign_or_later_failed_reads_not_evidence(self):
        data = report()
        for records in [trace(data, ok=False), trace(data, scope='history'), trace(data) + trace(data, ok=False)]:
            self.assertTrue(billing_previous_period_checks('上期为2025-03-22至2025-12-31。', records)['reasons'])

    def test_existing_completed_report_without_new_field_compatible(self):
        data = report(compare=True)
        del data['comparison_window']
        self.assertEqual([], billing_previous_period_checks('上期为2025-03-22至2025-12-31。', trace(data))['reasons'])

    def test_no_date_invitation_and_current_period_still_pass(self):
        for text in ['如需比较前一等长期间，可以继续查询。', '本期为2026-01-01至2026-10-12。']:
            result = validate_bank_answer({'ok': True, 'text': text, 'trace': trace(report())}, '')
            self.assertTrue(result['ok'])


if __name__ == '__main__':
    unittest.main()
