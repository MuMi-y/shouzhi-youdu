"""An unrequested comparison is different from a completed empty comparison."""
import unittest
from src.bank_insights.rules import build_report


def sample_rows():
    return [{'id': 'old', 'date': '2026-08-15', 'account_id': 'A', 'kind': 'expense',
             'amount_minor': -10000, 'description': '虚构支出', 'category': 'expense'},
            {'id': 'current', 'date': '2026-09-15', 'account_id': 'A', 'kind': 'expense',
             'amount_minor': -25000, 'description': '虚构支出', 'category': 'expense'}]


class ComparisonStatusTests(unittest.TestCase):
    def report(self, rows, compare_previous):
        return build_report(rows, '2026-09-01', '2026-09-30', currency='CNY', compare_previous=compare_previous)

    def test_no_comparison_is_explicit_even_when_real_increase_exists(self):
        report = self.report(sample_rows(), False)
        self.assertEqual('NOT_REQUESTED', report['comparison_status'])
        self.assertIsNone(report['previous_period'])
        self.assertEqual([], report['changes'])
        self.assertEqual([], report['category_changes'])
        self.assertTrue(any('不代表无上涨或无变化' in item for item in report['limitations']))
        self.assertEqual(25000, report['totals']['expense_minor'])

    def test_completed_comparison_retains_exact_values_and_amounts(self):
        report = self.report(sample_rows(), True)
        self.assertEqual('COMPLETED', report['comparison_status'])
        self.assertEqual(self.report(sample_rows(), False)['totals'], report['totals'])
        change = next(x for x in report['changes'] if x['metric'] == 'expense_minor')
        self.assertEqual((10000, 25000, 15000, '150.00'),
                         (change['previous_minor'], change['current_minor'], change['change_minor'], change['change_percent']))
        self.assertTrue(report['category_changes'][0]['significant_increase_candidate'])

    def test_completed_does_not_promise_prior_data_completeness(self):
        report = self.report(sample_rows()[1:], True)
        self.assertEqual('COMPLETED', report['comparison_status'])
        self.assertEqual(0, report['previous_period']['totals']['count'])
        self.assertIsNone(report['changes'][0]['change_percent'])
        self.assertTrue(any('不证明前期资料齐全' in item for item in report['limitations']))

    def test_empty_reports_keep_distinct_execution_status(self):
        self.assertEqual('NOT_REQUESTED', self.report([], False)['comparison_status'])
        self.assertEqual('COMPLETED', self.report([], True)['comparison_status'])
        self.assertEqual(self.report([], False)['totals'], self.report([], True)['totals'])


if __name__ == '__main__':
    unittest.main()
