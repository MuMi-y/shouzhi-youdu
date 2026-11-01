"""Full-period per-kind statistics must remain valid across display truncation."""
import unittest
from tests.competition_insights.test_billing_evidence import report, row


def kinds(data):
    return {item['transaction_kind']: item for item in data['description_groups']['kind_summaries']}


class KindSummaryTests(unittest.TestCase):
    def test_income_opening_and_expenses_are_distinct(self):
        data = report([row('i', 100000, 'income'), row('o', 50000, 'OPENING'),
                       row('e1', -1000, description='餐饮'), row('e2', -2000, description='交通')])
        self.assertEqual(4, data['description_groups']['total_groups'])
        self.assertEqual({'OPENING': 1, 'expense': 2, 'income': 1},
                         {kind: value['total_groups'] for kind, value in kinds(data).items()})
        self.assertEqual(100000, kinds(data)['income']['credit_minor'])
        self.assertEqual(50000, kinds(data)['OPENING']['credit_minor'])
        self.assertEqual(3000, kinds(data)['expense']['debit_minor'])

    def test_same_description_and_repeated_transactions_not_overcounted(self):
        data = report([row('i', 100000, 'income'), row('e1', -1000), row('e2', -2000), row('r', 500, 'refund')])
        self.assertEqual(3, data['description_groups']['total_groups'])
        expense = kinds(data)['expense']
        self.assertEqual((1, 2, 3000, 0), tuple(expense[key] for key in
                         ['total_groups', 'transaction_count', 'debit_minor', 'credit_minor']))

    def test_truncated_list_does_not_truncate_full_kind_totals(self):
        data = report([row(str(i), -100-i, description=f'描述{i}') for i in range(25)] +
                      [row('income', 100000, 'income')])
        expense = kinds(data)['expense']
        self.assertEqual((25, 19, True, 25, 2800), tuple(expense[key] for key in
                         ['total_groups', 'returned_groups', 'truncated', 'transaction_count', 'debit_minor']))
        self.assertFalse(kinds(data)['income']['truncated'])

    def test_unknown_and_transfer_types_not_promoted_to_consumption(self):
        data = report([row('u', -30, 'OTHER'), row('t', -50, 'TRANSFER_OUT')])
        self.assertEqual({'OTHER', 'TRANSFER_OUT'}, set(kinds(data)))
        self.assertEqual(0, data['totals']['expense_minor'])

    def test_empty_and_previous_period_keep_separate_summaries(self):
        self.assertEqual([], report([])['description_groups']['kind_summaries'])
        data = report([row('old', 10000, 'income', day='2026-08-15'), row('now', -50)])
        self.assertEqual({'income'}, set(kinds(data['previous_period'])))
        self.assertEqual({'expense'}, set(kinds(data)))


if __name__ == '__main__':
    unittest.main()
