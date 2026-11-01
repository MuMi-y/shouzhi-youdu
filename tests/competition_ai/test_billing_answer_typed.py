"""Billing meaning, periods, truncation and staleness, without model calls."""
from datetime import datetime, timezone
from pathlib import Path
import json
import tempfile
import unittest

from scripts.g1_goal_demo import seed_new_fixture
from src.bank_v1_web.bank_answer import validate_bank_answer
from src.bank_evidence.freshness import recheck
from src.bank_evidence.billing_render import render
from src.bank_insights.rules import build_report
from src.competition_web.app import create_app


class TypedBillingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='typed-billing-')
        self.directory = Path(self.temp.name)
        self.now = datetime(2026, 11, 2, 2, tzinfo=timezone.utc)
        seed_new_fixture(self.directory / 'bank.sqlite3', self.now)
        self.app = create_app(self.directory, clock=lambda: self.now)
        self.service = self.app.extensions['competition']
        self.args = {'start_date': '2026-09-01', 'end_date': '2026-09-30'}

    def tearDown(self):
        self.temp.cleanup()

    def trace(self, args=None, identity='query'):
        args = args or self.args
        body = self.service.call_tool('billing_report', args)
        return body, [{'kind': 'request', 'call_id': identity, 'name': 'billing_report', 'arguments': args},
                      {'kind': 'response', 'call_id': identity, 'tool': 'billing_report', 'args': args,
                       'ok': True, 'status': 200, 'response': body}]

    def answer(self, body, trace, sections=None, **extra):
        payload = {'format': 'bank_billing_v1', 'report_evidence_id': body['evidence_id'],
                   'sections': sections or ['overview', 'comparison'], 'fact_ids': [], 'calculation_ids': [], **extra}
        return validate_bank_answer({'ok': True, 'text': json.dumps(payload), 'trace': trace}, '请分析账单')

    def test_empty_period_and_equal_length_not_calendar_month(self):
        body, trace = self.trace()
        result = self.answer(body, trace)
        self.assertTrue(result['ok'], result)
        self.assertIn('完整自然月2026-09', result['text'])
        self.assertIn('2026-08-02至2026-08-31', result['text'])
        self.assertIn('观察到0笔', result['text'])
        self.assertIn('不证明现实账户没有活动', result['text'])
        self.assertTrue(recheck(result, self.service.call_tool)['ok'])

    def test_free_text_wrong_semantics_unknown_id_and_extra_claim_rejected(self):
        body, trace = self.trace()
        result = validate_bank_answer({'ok': True, 'text': '本月收入0元，所以没有消费风险。', 'trace': trace}, '分析账单')
        self.assertFalse(result['ok'])
        self.assertEqual('bank_billing', result['answer_validation']['scope'])
        self.assertFalse(self.answer(body, trace, report_evidence_id='insight_unknown')['ok'])
        self.assertFalse(self.answer(body, trace, claim='每月都能存1000元')['ok'])

    def test_already_rendered_report_facts_are_not_repeated(self):
        body, trace = self.trace()
        income = next(f for f in body['fact_catalog']['facts'] if f['field'] == 'income_minor' and f['period']['relation'] == '本期')
        result = self.answer(body, trace, ['overview'], fact_ids=[income['fact_id']])
        self.assertTrue(result['ok'], result)
        self.assertNotIn('本期收入：', result['text'])
        self.assertEqual(1, len(result['bank_facts_basis']['facts']))

    def test_refund_and_transfer_are_not_income_or_extra_consumption(self):
        rows = [self.row('expense', -10000), self.row('refund', 3000, identity='refund'),
                self.row('TRANSFER_OUT', -50000, identity='out'), self.row('TRANSFER_IN', 50000, identity='in'),
                self.row('OPENING', 999900, identity='opening')]
        report = build_report(rows, '2026-09-01', '2026-09-30', currency='CNY')
        text = render(report, ['overview'])
        self.assertIn('消费支出：100.00元', text)
        self.assertIn('退款：30.00元', text)
        self.assertIn('消费减退款：70.00元', text)
        self.assertIn('本人转账净流入：0.00元', text)
        self.assertIn('收入：0.00元', text)
        self.assertIn('期初记账也不计作收入', text)

    def test_snapshot_change_rejected_before_display(self):
        args = {'start_date': '2026-11-02', 'end_date': '2026-11-02'}
        body, trace = self.trace(args)
        old = self.answer(body, trace)
        self.service.bank.user_action('inject_transaction', {'kind': 'expense', 'amount_minor': 19,
                                                            'idempotency_key': 'typed-billing-change'})
        self.assertFalse(recheck(old, self.service.call_tool)['ok'])
        _, later = self.trace(args, 'later')
        self.assertFalse(self.answer(body, trace + later)['ok'])

    @staticmethod
    def row(kind, amount, identity='transaction', description='原始描述', day='2026-09-12'):
        return {'id': identity, 'kind': kind, 'amount_minor': amount, 'description': description,
                'date': day, 'account_id': 'A', 'category': '未分类'}

    def test_duplicate_is_only_candidate_and_nonpositive_ratio_undefined(self):
        rows = [self.row('expense', -10000, identity=str(i)) for i in range(2)]
        text = render(build_report(rows, '2026-09-01', '2026-09-30', currency='CNY'), ['duplicates', 'ratios'])
        self.assertIn('2笔流水构成疑似重复候选', text)
        self.assertIn('不能据此确认重复、欺诈或自动退款', text)
        self.assertIn('未定义（收入非正数）', text)

    def test_omitted_groups_and_changes_are_explicit(self):
        rows = [self.row('expense', -(1000+i), str(i), f'原描述{i}') for i in range(23)]
        text = render(build_report(rows, '2026-09-01', '2026-09-30', currency='CNY'),
                      ['descriptions', 'description_comparison'])
        self.assertIn('共23组，显示20组', text)
        self.assertIn('未显示3组，其中3组有变化', text)
        self.assertIn('原描述不证明商户身份', text)

    def test_unrequested_comparison_not_zero_change(self):
        report = build_report([], '2026-09-01', '2026-09-30', currency='CNY', compare_previous=False)
        text = render(report, ['comparison', 'description_comparison'])
        self.assertIn('未查询前期', text)
        self.assertIn('未计算原描述分组比较', text)


if __name__ == '__main__':
    unittest.main()
