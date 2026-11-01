"""Independent reviewed counterexamples become regressions, not holdouts."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import json
import tempfile
import unittest

from scripts.g1_goal_demo import seed_new_fixture
from src.competition_web.app import create_app
from src.bank_v1_web.bank_answer import validate_bank_answer


class FactAnswerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='fact-answer-')
        directory = Path(self.temp.name)
        self.now = datetime(2026, 11, 2, 2, tzinfo=timezone.utc)
        seed_new_fixture(directory / 'bank.sqlite3', self.now)
        self.service = create_app(directory, clock=lambda: self.now).extensions['competition']
        self.trace = []

    def tearDown(self):
        self.temp.cleanup()

    def tool(self, name, args):
        body = self.service.call_tool(name, args)
        identity = str(len(self.trace))
        self.trace += [{'kind': 'request', 'call_id': identity, 'name': name, 'arguments': args},
                       {'kind': 'response', 'call_id': identity, 'tool': name, 'args': args,
                        'ok': True, 'status': 200, 'response': body}]
        return body

    def check(self, text):
        return validate_bank_answer({'ok': True, 'text': text, 'trace': self.trace}, '请核对账户情况')

    def answer(self, facts):
        return json.dumps({'format': 'bank_facts_v1', 'fact_ids': [f['fact_id'] for f in facts],
                           'calculation_ids': []})

    def test_review_three_false_claims_no_longer_accepted_in_migrated_flow(self):
        self.tool('accounts', {})
        for text in ['A账户余额为2000元，B账户余额为8000元。',
                     '您昨天收到工资8000元。', '账户状态正常，没有任何需要关注的变化。']:
            with self.subTest(text=text):
                checked = self.check(text)
                self.assertFalse(checked['ok'])
                self.assertEqual('bank_facts', checked['answer_validation']['scope'])

    def test_model_can_select_order_but_cannot_relabel_account_or_event(self):
        facts = self.tool('accounts', {})['fact_catalog']['facts']
        checked = self.check(self.answer(list(reversed(facts))))
        self.assertTrue(checked['ok'], checked)
        self.assertIn('A账户已到账余额：8000.00元', checked['text'])
        self.assertIn('B账户已到账余额：2000.00元', checked['text'])
        payload = json.loads(self.answer(facts))
        payload['conclusion'] = '昨天收到工资8000元'
        self.assertFalse(self.check(json.dumps(payload))['ok'])

    def test_reference_from_before_newer_accounts_read_is_rejected(self):
        old = self.tool('accounts', {})['fact_catalog']['facts']
        self.service.bank.user_action('inject_transaction', {
            'kind': 'expense', 'amount_minor': 17, 'idempotency_key': 'facts-changed'})
        new = self.tool('accounts', {})['fact_catalog']['facts']
        self.assertFalse(self.check(self.answer(old))['ok'])
        self.assertTrue(self.check(self.answer(new))['ok'])

    def test_orphaned_response_and_catalog_relabel_are_rejected(self):
        facts = self.tool('accounts', {})['fact_catalog']['facts']
        original = deepcopy(self.trace)
        self.trace = self.trace[1:]
        self.assertFalse(self.check(self.answer(facts))['ok'])
        self.trace = original
        self.trace[-1]['response']['fact_catalog']['facts'][0]['label'] = '昨天工资'
        self.assertFalse(self.check(self.answer(facts))['ok'])

    def test_unknown_fact_and_incomplete_trace_are_rejected(self):
        facts = self.tool('accounts', {})['fact_catalog']['facts']
        invalid = deepcopy(facts); invalid[0]['fact_id'] = 'fact:' + '0' * 64
        self.assertFalse(self.check(self.answer(invalid))['ok'])
        self.trace.append({'kind': 'request', 'call_id': 'unfinished', 'name': 'accounts', 'arguments': {}})
        self.assertFalse(self.check(self.answer(facts))['ok'])

    def test_arrangement_is_not_rendered_as_actual_income(self):
        facts = self.tool('arrangements', {})['fact_catalog']['facts']
        checked = self.check(self.answer(facts[:3]))
        self.assertTrue(checked['ok'], checked)
        self.assertIn('这是安排记录，不证明已经收付款', checked['text'])


if __name__ == '__main__':
    unittest.main()
