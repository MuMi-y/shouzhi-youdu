"""Portable numeric-policy tests: production imports only; no evidence files."""
import unittest
import tempfile
from pathlib import Path
from src.bank_v1 import Service
from datetime import datetime, timezone
from src.bank_v1_web import bank_answer as gate
from src.bank_insights import rules, service

def answer(text, data=None, prompt='', tool='billing_report'):
    r = {'ok': True, 'text': text, 'trace': [] if data is None else [{'kind': 'response', 'ok': True, 'tool': tool, 'response': {'scope': 'bank', 'data': data}}]}
    return gate.validate_bank_answer(r, prompt)

def row(id, date, amount):
    return {'id': id, 'date': date, 'amount_minor': -amount, 'kind': 'expense', 'account_id': 'A', 'description': 'synthetic monthly', 'category': 'expense'}

class NumericPolicyTests(unittest.TestCase):

    def test_unknown_percent_hard_reject(self):
        r = answer('出账占入账88%。', {'expense_minor': 705500, 'income_minor': 800000})
        self.assertFalse(r['ok'])
        self.assertEqual('UNSUPPORTED_DISPLAYED_PERCENT', r['answer_validation']['reasons'][0]['code'])

    def test_typed_percent_is_usable(self):
        self.assertTrue(answer('变化0.07%。', {'change_percent': '0.07'})['ok'])

    def test_negative_change_positive_decline_only_local(self):
        for text in ['下降98.57%。', '降幅为98.57%。', '减少了98.57%。', '变化-98.57%。']:
            with self.subTest(text=text):
                self.assertTrue(answer(text, {'change_percent': '-98.57'})['ok'])

    def test_negative_change_does_not_allow_growth(self):
        self.assertFalse(answer('上涨98.57%。', {'change_percent': '-98.57'})['ok'])
        self.assertFalse(answer('下降98.57%；上涨98.57%。', {'change_percent': '-98.57'})['ok'])

    def test_positive_change_not_negative_proof(self):
        self.assertFalse(answer('变化-20%。', {'change_percent': '20'})['ok'])

    def test_negative_calculator_percentage_not_global_abs(self):
        data = {'calculation': {'inputs': {'left_minor': -20, 'right_minor': 100}, 'result': {'percent': '-20.00'}}}
        self.assertFalse(answer('下降20%。', data, tool='money_calculate')['ok'])
        self.assertTrue(answer('结果-20%。', data, tool='money_calculate')['ok'])

    def test_user_percentage_assumption_repeat_only(self):
        self.assertTrue(answer('按您假设的增长20%计算。', prompt='假如增加20%呢？')['ok'])
        self.assertFalse(answer('将增加25%。', prompt='假如增加20%呢？')['ok'])

    def test_tool_free_description_does_not_prove_percentage(self):
        self.assertFalse(answer('规则阈值20%。', {'message': '规则阈值20%', 'description': '20%'})['ok'])

    def test_nested_calculation_inputs_do_not_prove_amount_or_percent(self):
        data = {'task': {'nodes': [{'args': {'growth_percent': 88, 'amount_minor': 10000}, 'result': {'data': {'calculation': {'inputs': {'growth_percent': 88, 'amount_minor': 10000}, 'result': {'percent': '25'}}}}}]}}
        r = answer('金额100元、增长88%。', data, tool='task_status')
        self.assertFalse(r['ok'])
        self.assertEqual({'UNSUPPORTED_DISPLAYED_AMOUNT', 'UNSUPPORTED_DISPLAYED_PERCENT'}, {x['code'] for x in r['answer_validation']['reasons']})

    def test_typed_policy_supports_policy_amount_and_percentage(self):
        self.assertTrue(answer('工具规则：增幅至少20%且绝对增加至少100元。', {'analysis_policy': rules.analysis_policy()})['ok'])
        self.assertTrue(answer('周期上涨规则阈值为10%。', {'analysis_policy': rules.analysis_policy()})['ok'])

    def test_typed_policy_is_not_observed_financial_values(self):
        data = {'analysis_policy': rules.analysis_policy()}
        self.assertFalse(answer('本期消费100元，实际涨幅20%。', data)['ok'])
        self.assertFalse(answer('实际涨幅20%，符合规则。', data)['ok'])

    def test_rule_keywords_cannot_launder_balance_or_ratio(self):
        data = {'analysis_policy': rules.analysis_policy()}
        for text in ['按规则，账户余额100元。', '采用标准，支出占收入20%。', '规则阈值为100元，账户余额100元。', '规则阈值为20%，实际涨幅20%。', '标准增幅至少20%，本期实际增长率20%。']:
            with self.subTest(text=text):
                self.assertFalse(answer(text, data)['ok'])

    def test_policy_needs_comparison_phrase_and_rule_context(self):
        data = {'analysis_policy': rules.analysis_policy()}
        self.assertTrue(answer('阈值（增幅≥20%且绝对增加≥100元）。', data)['ok'])
        self.assertTrue(answer('工具增幅至少20%且绝对增加至少100元的显著上涨标准。', data)['ok'])
        self.assertFalse(answer('增幅至少20%。', data)['ok'])

    def test_t06_arbitrary_suggestion_not_whitelisted(self):
        self.assertFalse(answer('如转4000元。', {'balance_minor': 484500})['ok'])

    def test_clarification_no_numbers_still_passes(self):
        self.assertTrue(answer('请问您想查看哪个月份？')['ok'])

    def test_service_wrap_adds_same_policy_without_input_mutation(self):
        clock = lambda: datetime(2026, 10, 12, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bank = Service(root / 'bank.sqlite3', clock=clock)
            obj = service.InsightService(root / 'insights.sqlite3', bank=bank, clock=clock)
            data = {'candidates': []}
            r = obj._wrap(data)
        self.assertEqual(rules.analysis_policy(), r['data']['analysis_policy'])
        self.assertEqual({'candidates': []}, data)

    def test_report_threshold_boundaries_and_same_policy(self):
        rows = [row('old', '2026-08-15', 50000), row('new', '2026-09-15', 60000)]
        r = rules.build_report(rows, '2026-09-01', '2026-09-30', currency='CNY')
        self.assertTrue(r['category_changes'][0]['significant_increase_candidate'])
        self.assertEqual(rules.analysis_policy(), r['analysis_policy'])
        rows[-1]['amount_minor'] = -59999
        self.assertFalse(rules.build_report(rows, '2026-09-01', '2026-09-30', currency='CNY')['category_changes'][0]['significant_increase_candidate'])

    def test_income_and_net_change_comparisons_are_tool_calculated(self):
        rows = [row('oldexpense', '2026-08-15', 705000), row('newexpense', '2026-09-15', 705500)]
        for ident, day in [('oldincome', '2026-08-15'), ('newincome', '2026-09-15')]:
            rows.append({**row(ident, day, 0), 'kind': 'income', 'category': 'income', 'amount_minor': 800000})
        report = rules.build_report(rows, '2026-09-01', '2026-09-30', currency='CNY')
        self.assertEqual(['expense_minor', 'refund_minor', 'debit_minor', 'credit_minor', 'income_minor', 'net_change_minor'], [c['metric'] for c in report['changes']])
        income, net = report['changes'][4:]
        self.assertEqual((800000, 800000, 0, '0.00'), (income['previous_minor'], income['current_minor'], income['change_minor'], income['change_percent']))
        self.assertEqual((95000, 94500, -500, '-5.00', '-0.53'), (net['previous_minor'], net['current_minor'], net['change_minor'], net['change'], net['change_percent']))

    def test_net_change_zero_denominator_is_not_a_percentage(self):
        rows = [row('e', '2026-08-15', 10000), {**row('i', '2026-08-15', 0), 'kind': 'income', 'amount_minor': 10000}, {**row('current', '2026-09-15', 0), 'kind': 'income', 'amount_minor': 1000}]
        net = rules.build_report(rows, '2026-09-01', '2026-09-30', currency='CNY')['changes'][-1]
        self.assertEqual('net_change_minor', net['metric'])
        self.assertEqual(0, net['previous_minor'])
        self.assertEqual(1000, net['change_minor'])
        self.assertIsNone(net['change_percent'])

    def test_user_details_before_future_preparation_is_not_a_card(self):
        self.assertTrue(answer('1. 告诉我一个不超过 4,845.00 元的金额，我立即准备 A→B 的待确认转账。', {'balance_minor': 484500})['ok'])
        self.assertTrue(answer('告诉我转入账户，我再准备转账。')['ok'])

    def test_detail_request_cannot_hide_already_prepared_or_executed(self):
        for text in ['告诉我金额，我已准备好转账，请确认。', '告诉我金额，我已经成功转账。', '告诉我金额，我会准备转账，转账已执行成功。', '告诉我金额，我立即准备转账，转账已准备好了。']:
            with self.subTest(text=text):
                self.assertFalse(answer(text)['ok'])

    def test_pending_then_check_whether_posted_is_not_success_claim(self):
        data = {'operation': {'operation_id': 'op_pending', 'kind': 'transfer', 'status': 'PENDING', 'receipt': None}}
        self.assertTrue(answer('确认完成后我可以凭操作编号查询真实回执，核实转账是否已入账。', data, tool='operation')['ok'])

    def test_whether_question_does_not_hide_other_success_assertion(self):
        self.assertFalse(answer('转账已成功，随后核实是否已入账。')['ok'])
        self.assertFalse(answer('核实转账是否已入账，转账已成功。')['ok'])

    def test_success_receipt_still_supports_assertion_with_question(self):
        data = {'operation': {'operation_id': 'op_done', 'kind': 'transfer', 'status': 'SUCCEEDED', 'receipt': {'receipt_id': 'receipt_done'}}}
        self.assertTrue(answer('转账已成功，随后核实是否已入账。', data, tool='operation')['ok'])
