"""Comparison dependency checks on actual queries, not memorized answers."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import json
import tempfile
import unittest

from scripts.g1_goal_demo import seed_new_fixture
from src.competition_web.app import create_app
from src.bank_v1_web.bank_answer import validate_bank_answer


class SpendingAnswerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='spending-answer-')
        directory = Path(self.temp.name)
        self.now = datetime(2026, 11, 2, 2, tzinfo=timezone.utc)
        seed_new_fixture(directory / 'bank.sqlite3', self.now)
        self.service = create_app(directory, clock=lambda: self.now).extensions['competition']
        self.trace = []
        self.prompt = '我拟消费3500元，会不会影响还款？'
        accounts = self.tool('accounts', {})
        self.fact = next(f for f in accounts['fact_catalog']['facts'] if f['entity'] == 'COMBINED_ACCOUNTS')
        arrangements = self.tool('arrangements', {})
        calc = self.tool('money_calculate', {'operation': 'subtract', 'left_minor': self.fact['value'],
            'right_minor': 350000, 'currency': 'CNY',
            'left_source': {'kind': 'bank_fact', 'tool': 'accounts', 'arguments': {}, 'fact_id': self.fact['fact_id']},
            'right_source': {'kind': 'user_assumption', 'quote': '拟消费3500元'}})
        baseline = self.tool('forecast', {'through_date': '2026-11-30'})
        proposed = self.tool('forecast', {'through_date': '2026-11-30', 'proposed_expense_minor': 350000})
        self.payload = {'format': 'bank_spending_comparison_v1', 'balance_fact_id': self.fact['fact_id'],
                        'calculation_id': calc['evidence_id'], 'baseline_forecast_id': baseline['evidence_id'],
                        'proposed_forecast_id': proposed['evidence_id'], 'arrangements_evidence_id': arrangements['evidence_id']}

    def tearDown(self):
        self.service.handoff_store.release_owner()
        self.temp.cleanup()

    def tool(self, name, args):
        body = self.service.call_tool(name, args)
        key = str(len(self.trace))
        self.trace += [{'kind': 'request', 'call_id': key, 'name': name, 'arguments': args},
                       {'kind': 'response', 'call_id': key, 'tool': name, 'args': args,
                        'ok': True, 'status': 200, 'response': body}]
        return body

    def check(self):
        return validate_bank_answer({'ok': True, 'text': json.dumps(self.payload), 'trace': self.trace}, self.prompt)

    def test_complete_comparison_has_explicit_scenarios_calculation_and_dates(self):
        result = self.check()
        self.assertTrue(result['ok'], result)
        self.assertIn('6500.00元', result['text'])
        self.assertIn('不新增消费：', result['text'])
        self.assertIn('新增这笔消费：', result['text'])
        self.assertIn('仍是安排、未证明到账', result['text'])
        self.assertIn('本轮没有办理付款', result['text'])
        self.assertEqual(1, len(result['bank_facts_basis']['calculations']))

    def test_a_forecast_cannot_replace_the_required_calculation(self):
        self.payload['calculation_id'] = self.payload['proposed_forecast_id']
        self.assertFalse(self.check()['ok'])

    def test_swapped_forecast_roles_fail(self):
        self.payload['baseline_forecast_id'], self.payload['proposed_forecast_id'] = (
            self.payload['proposed_forecast_id'], self.payload['baseline_forecast_id'])
        self.assertFalse(self.check()['ok'])

    def test_different_proposal_or_window_fails(self):
        for args in [{'through_date': '2026-11-30', 'proposed_expense_minor': 347500},
                     {'through_date': '2026-11-29', 'proposed_expense_minor': 350000}]:
            with self.subTest(args=args):
                self.payload['proposed_forecast_id'] = self.tool('forecast', args)['evidence_id']
                self.assertFalse(self.check()['ok'])

    def test_bank_update_invalidates_old_comparison_even_after_fresh_accounts(self):
        self.service.bank.user_action('inject_transaction', {'kind': 'expense', 'amount_minor': 10000,
                                                           'idempotency_key': 'spending-change'})
        self.tool('accounts', {})
        self.assertFalse(self.check()['ok'])

    def test_host_display_recheck_rejects_changed_bank_without_rewriting_answer(self):
        from src.bank_evidence.freshness import recheck
        original = self.check()
        self.assertTrue(recheck(original, self.service.call_tool)['ok'])
        self.service.bank.user_action('inject_transaction', {'kind': 'expense', 'amount_minor': 10000,
                                                           'idempotency_key': 'before-display'})
        checked = recheck(original, self.service.call_tool)
        self.assertFalse(checked['ok'])
        self.assertEqual('STALE_OR_UNAVAILABLE', checked['fact_freshness']['status'])
        self.assertEqual(original['text'], checked['text'])

    def test_extra_free_conclusion_and_other_user_amount_fail(self):
        self.payload['conclusion'] = '绝对不影响还款'
        self.assertFalse(self.check()['ok'])
        del self.payload['conclusion']
        self.prompt = '我拟消费3475元'
        self.assertFalse(self.check()['ok'])


if __name__ == '__main__':
    unittest.main()
