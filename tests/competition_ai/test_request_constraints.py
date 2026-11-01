"""User constraints against real isolated tools, with wrong and correct results."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import unittest

from tests.competition_ai import test_request_delivery as delivery_fixtures
from tests.competition_ai.test_request_decision import item, slot, response
from src.bank_requests.context import build
from src.bank_requests.response import validate
from src.bank_v1_web.display_check import check
from src.bank_requests.objects import historical_items


class RequestConstraintTests(unittest.TestCase):
    def setUp(self):
        self.d = delivery_fixtures.RequestDeliveryTests(); self.d.setUp()
        self.addCleanup(self.d.doCleanups)

    def respond(self, text, request, parts, action='ANALYZE'):
        context = build('m', 'm', [{'message_id': 'm', 'text': text}], [], read_only=False, object_contract=True)
        request = deepcopy(request)
        request['action'] = {'kind': action, 'source': {'message_id': 'm', 'quote': text}}
        answer = {'format': 'bank_outcomes_v1', 'items': [{'item_id': request['id'], 'components': parts}]}
        raw = response(context, [request], answer); raw['trace'] = deepcopy(self.d.f.trace)
        return validate(raw, context)

    def bill(self, start='2026-09-01', end='2026-09-30'):
        body = self.d.f.tool('billing_report', {'start_date': start, 'end_date': end, 'compare_previous': False})
        return {'format': 'bank_billing_v1', 'report_evidence_id': body['evidence_id'],
                'sections': ['overview'], 'fact_ids': [], 'calculation_ids': []}

    def arrangement(self, amount=17300, day='2026-10-24', kind='expense'):
        op = self.d.f.tool('propose_arrangement', {'title': '测试安排', 'kind': kind, 'amount_minor': amount,
            'due_date': day, 'idempotency_key': f'arr-{amount}-{day}-{kind}'})['data']['operation']
        return self.d.f.part(op)

    def transfer(self):
        op = self.d.f.tool('prepare_transfer', {'source_account': 'B', 'target_account': 'A',
            'amount_minor': 17300, 'idempotency_key': 'reserve-transfer'})['data']['operation']
        return self.d.f.part(op)

    def remainder(self, account='B', amount=17300):
        body = self.d.f.tool('accounts', {})
        fact = next(f for f in body['fact_catalog']['facts'] if f['entity'] == account and f['field'] == 'balance_minor')
        calc = self.d.f.tool('money_calculate', {'operation': 'subtract', 'currency': 'CNY',
            'left_minor': fact['value'], 'left_source': {'kind': 'bank_fact', 'tool': 'accounts', 'arguments': {}, 'fact_id': fact['fact_id']},
            'right_minor': amount, 'right_source': {'kind': 'user_assumption', 'quote': f'{amount / 100:.2f}元'}})
        return {'format': 'bank_facts_v1', 'fact_ids': [fact['fact_id']], 'calculation_ids': [calc['evidence_id']]}

    def reserved_request(self, floor=90000):
        request = self.d.request(17300, 'B', 'A')
        request['slots']['reserve_minor'] = slot('m', f'{floor / 100:.2f}元', floor)
        return request

    def continued(self, first, original_text, text, request, parts, *, action='PREPARE', legacy=False):
        # JSON roundtrip matches the persisted job-result representation.
        parents = historical_items(json.loads(json.dumps(first)), 'm')
        if legacy:
            for parent in parents:
                parent.pop('constraint_anchors', None)
        context = build('n', 'n', [{'message_id': 'm', 'text': original_text}, {'message_id': 'n', 'text': text}],
                        parents, read_only=False, object_contract=True)
        request['action'] = {'kind': action, 'source': {'message_id': 'n', 'quote': text}}
        answer = {'format': 'bank_outcomes_v1', 'items': [{'item_id': request['id'], 'components': parts}]}
        raw = response(context, [request], answer); raw['trace'] = deepcopy(self.d.f.trace)
        return validate(raw, context)

    def test_original_wrong_month_rejected_and_correct_month_accepted(self):
        request = item({'period_text': slot('m', '上个月', '上个月')}, intent='billing')
        before = self.d.f.service.bank.state()['accounts']
        wrong = self.respond('查上个月账单', request, [self.bill('2026-08-01', '2026-08-31')])
        self.assertEqual('BILLING_PERIOD_DOES_NOT_MATCH_REQUEST', wrong['answer_validation']['reason'])
        good = self.respond('查上个月账单', request, [self.bill()])
        self.assertTrue(good['ok'], good)
        self.assertEqual('BOUND', good['business_outcomes'][0]['constraint_checks'][0]['status'])
        self.assertEqual(before, self.d.f.service.bank.state()['accounts'])

    def test_cashflow_arithmetic_binds_account_and_amount_without_future_guarantee(self):
        request = item({'source_account': slot('m', 'B', 'B'),
                        'amount_minor': slot('m', '173.00元', 17300)}, intent='cashflow_compare')
        before = self.d.f.service.bank.state()['accounts']
        part = self.remainder()
        good = self.respond('B花173.00元，只试算', request, [part])
        self.assertTrue(good['ok'], good)
        self.assertEqual('ADVICE_READY', good['business_outcomes'][0]['assessment'])
        self.assertNotIn('尚未核实', good['text'])
        self.assertIn('827.00', good['text'])
        request['slots']['reserve_minor'] = slot('m', '800.00元', 80000)
        request['slots']['date_text'] = slot('m', '2026-11-01', '2026-11-01')
        future = self.respond('B花173.00元，2026-11-01保留800.00元', request, [part])
        self.assertTrue(future['ok'], future)
        unknown = {c['field'] for c in future['business_outcomes'][0]['constraint_checks'] if c['status'] == 'UNVERIFIED'}
        self.assertEqual({'reserve_minor', 'date_text'}, unknown)
        self.assertEqual(before, self.d.f.service.bank.state()['accounts'])

    def test_cashflow_wrong_account_does_not_bind_amount_and_comparison_account_needs_fact(self):
        request = item({'source_account': slot('m', 'A', 'A'),
                        'target_account': slot('m', 'B', 'B'),
                        'amount_minor': slot('m', '173.00元', 17300)}, intent='cashflow_compare')
        result = self.respond('A花173.00元，对照B', request, [self.remainder(account='B')])
        self.assertTrue(result['ok'], result)
        checks = {c['field']: c['status'] for c in result['business_outcomes'][0]['constraint_checks']}
        self.assertEqual('UNVERIFIED', checks['source_account'])
        self.assertEqual('UNVERIFIED', checks['amount_minor'])
        self.assertEqual('BOUND', checks['target_account'])
        self.assertNotIn('转出账户', result['text'])

    def test_normalized_period_text_and_explicit_range_cannot_disagree(self):
        body = self.d.f.tool('accounts', {})
        period = slot('m', '上个月', '上个月')
        period['source']['normalization'] = {'kind': 'calendar_period', 'period': 'PREVIOUS_MONTH', 'evidence_id': body['evidence_id']}
        request = item({'period_text': period}, intent='billing')
        good = self.respond('上个月', request, [self.bill()])
        self.assertTrue(good['ok'], good)
        request['slots']['start_date'] = slot('m', '2026-08-01', '2026-08-01')
        bad = self.respond('上个月 2026-08-01', request, [self.bill()])
        self.assertFalse(bad['ok'])

    def test_unknown_period_is_visible_not_completed(self):
        request = item({'period_text': slot('m', '放假那段时间', '放假那段时间')}, intent='billing')
        result = self.respond('查放假那段时间', request, [self.bill()])
        self.assertTrue(result['ok'], result)
        self.assertEqual('NEEDS_EVIDENCE', result['business_outcomes'][0]['assessment'])
        self.assertIn('统计期间', result['text'])
        self.assertIn('尚未核实', result['text'])

    def test_billing_check_status_label_cannot_skip_period_binding(self):
        request = item({'period_text': slot('m', '上个月', '上个月')}, intent='billing')
        result = self.respond('查询上个月账单', request, [self.bill('2026-08-01', '2026-08-31')], 'CHECK_STATUS')
        self.assertFalse(result['ok'])

    def test_normalized_day_matches_the_prepared_date(self):
        body = self.d.f.tool('accounts', {})
        chosen = slot('m', '明天', '2026-10-20')
        chosen['source']['normalization'] = {'kind': 'calendar_day', 'evidence_id': body['evidence_id']}
        request = item({'amount_minor': slot('m', '173.00元', 17300), 'date_text': chosen}, intent='arrangement')
        result = self.respond('准备明天173.00元', request, [self.arrangement(day='2026-10-20')], 'PREPARE')
        self.assertTrue(result['ok'], result)

    def test_arrangement_amount_and_date_independently_bound(self):
        request = item({'amount_minor': slot('m', '173.00元', 17300),
                        'date_text': slot('m', '2026-10-24', '2026-10-24')}, intent='arrangement')
        text = '准备173.00元，2026-10-24'
        before = self.d.f.service.bank.state()['accounts']
        for amount, day, good in [(19537, '2026-10-24', False), (17300, '2026-10-25', False), (17300, '2026-10-24', True)]:
            self.d.f.trace = []
            with self.subTest(amount=amount, day=day):
                result = self.respond(text, request, [self.arrangement(amount, day)], 'PREPARE')
                self.assertEqual(good, result['ok'], result)
                if good:
                    self.assertEqual('ADVICE_READY', result['business_outcomes'][0]['assessment'])
        self.assertEqual(before, self.d.f.service.bank.state()['accounts'])

    def test_relative_arrangement_date_uses_business_day(self):
        request = item({'amount_minor': slot('m', '173.00元', 17300), 'date_text': slot('m', '明天', '明天')}, intent='arrangement')
        good = self.respond('准备173.00元明天', request, [self.arrangement(day='2026-10-20')], 'PREPARE')
        self.assertTrue(good['ok'], good)
        self.d.f.trace = []
        bad = self.respond('准备173.00元明天', request, [self.arrangement(day='2026-10-21')], 'PREPARE')
        self.assertFalse(bad['ok'])

    def test_income_cannot_become_expense_arrangement(self):
        request = item({'income_minor': slot('m', '173.00元', 17300)}, intent='arrangement')
        bad = self.respond('准备收入173.00元', request, [self.arrangement()], 'PREPARE')
        self.assertFalse(bad['ok'])
        self.d.f.trace = []
        good = self.respond('准备收入173.00元', request, [self.arrangement(kind='income')], 'PREPARE')
        self.assertTrue(good['ok'], good)

    def test_original_reserve_without_calculation_is_not_ready_to_confirm(self):
        before = self.d.f.service.bank.state()['accounts']
        result = self.respond('准备B到A173.00元，保留900.00元', self.reserved_request(), [self.transfer()], 'PREPARE')
        self.assertTrue(result['ok'], result)
        outcome = result['business_outcomes'][0]
        self.assertEqual('NEEDS_EVIDENCE', outcome['assessment'])
        self.assertNotIn(outcome['next_step'], {'CONFIRM_EXISTING', 'VERIFY_EXISTING', 'DONE'})
        self.assertIn('不要确认', result['text'])
        self.assertEqual(before, self.d.f.service.bank.state()['accounts'])

    def test_reserve_comparison_uses_bound_tool_result_and_exact_boundary(self):
        part = self.remainder()
        for floor, status in [(90000, 'CONDITION_UNMET'), (82700, 'ADVICE_READY'), (80000, 'ADVICE_READY')]:
            with self.subTest(floor=floor):
                text = f'只分析B到A173.00元，保留{floor / 100:.2f}元'
                result = self.respond(text, self.reserved_request(floor), [part])
                self.assertTrue(result['ok'], result)
                self.assertEqual(status, result['business_outcomes'][0]['assessment'])
                self.assertIn('827.00', result['text'])

    def test_wrong_account_calculation_does_not_prove_reserve(self):
        result = self.respond('分析B到A173.00元保留900.00元', self.reserved_request(), [self.remainder(account='A')])
        self.assertTrue(result['ok'], result)
        self.assertEqual('NEEDS_EVIDENCE', result['business_outcomes'][0]['assessment'])
        reserve = next(c for c in result['business_outcomes'][0]['constraint_checks'] if c['field'] == 'reserve_minor')
        self.assertEqual('UNVERIFIED', reserve['status'])

    def test_pending_conflicting_reserve_preserves_operation_but_not_confirmation_advice(self):
        result = self.respond('准备B到A173.00元保留900.00元', self.reserved_request(), [self.remainder(), self.transfer()], 'PREPARE')
        self.assertTrue(result['ok'], result)
        outcome = result['business_outcomes'][0]
        self.assertEqual('CONDITION_UNMET', outcome['assessment'])
        self.assertEqual('WAITING_CONFIRMATION', outcome['execution'])
        self.assertEqual('REVISE_REQUEST', outcome['next_step'])
        self.assertIn('未自动撤回', result['text'])
        self.assertNotIn('由本人确认或拒绝', result['text'])

    def test_display_refresh_rejects_changed_reserve_source(self):
        result = self.respond('分析B到A173.00元保留800.00元', self.reserved_request(80000), [self.remainder()])
        self.assertTrue(check(result, self.d.f.service)['ok'])
        self.d.f.service.bank.user_action('inject_transaction', {'kind': 'expense', 'account_id': 'B', 'amount_minor': 5000,
            'description': '虚构消费', 'idempotency_key': 'reserve-change'})
        self.assertFalse(check(result, self.d.f.service)['ok'])

    def test_every_declared_extra_condition_remains_visible(self):
        request = self.d.request(17300, 'B', 'A')
        request['slots'].update(income_minor=slot('m', '300.00元', 30000), maximum_minor=slot('m', '900.00元', 90000))
        result = self.respond('分析B到A173.00元，预计收入300.00元，上限900.00元', request, [self.remainder()])
        self.assertTrue(result['ok'], result)
        checks = {c['field']: c for c in result['business_outcomes'][0]['constraint_checks']}
        self.assertEqual(set(request['slots']), set(checks))
        self.assertEqual('UNVERIFIED', checks['income_minor']['status'])
        self.assertEqual('UNVERIFIED', checks['maximum_minor']['status'])
        self.assertIn('拟议收入金额', result['text'])

    def test_goal_preview_cannot_answer_different_user_amount(self):
        from tests.g2_goals.test_preview import PreviewTests
        fixture = PreviewTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        preview = fixture.raw()
        self.d.f.trace = preview['trace']
        for value, expected in [(450000, True), (500000, False)]:
            request = item({'amount_minor': slot('m', f'{value / 100:.2f}元', value)}, intent='goal_compare')
            result = self.respond(f'比较{value / 100:.2f}元目标', request, [json.loads(preview['text'])])
            self.assertEqual(expected, result['ok'], result)

    def test_calendar_normalization_cannot_bypass_amount_type(self):
        body = self.d.f.tool('accounts', {})
        request = item({'amount_minor': slot('m', '上个月', '上个月')}, intent='arrangement')
        request['slots']['amount_minor']['source']['normalization'] = {
            'kind': 'calendar_period', 'period': 'PREVIOUS_MONTH', 'evidence_id': body['evidence_id']}
        result = self.respond('准备上个月', request, [self.arrangement()], 'PREPARE')
        self.assertEqual('NORMALIZATION_NOT_ALLOWED_FOR_FIELD', result['answer_validation']['reason'])

    def test_cashflow_proposal_date_and_reserve_bind_selected_forecast(self):
        from tests.competition_ai.test_spending_answer import SpendingAnswerTests
        fixture = SpendingAnswerTests(); fixture.setUp(); self.addCleanup(fixture.tearDown)
        self.d.f.trace = fixture.trace
        request = item({'amount_minor': slot('m', '拟消费3500元', 350000),
                        'date_text': slot('m', '2026-11-02', '2026-11-02'),
                        'reserve_minor': slot('m', '2000.00元', 200000)}, intent='cashflow_compare')
        result = self.respond('2026-11-02拟消费3500元，保留2000.00元', request, [fixture.payload])
        self.assertTrue(result['ok'], result)
        self.assertEqual('CONDITION_UNMET', result['business_outcomes'][0]['assessment'])
        request['slots']['date_text'] = slot('m', '2026-11-03', '2026-11-03')
        bad = self.respond('2026-11-03拟消费3500元，保留2000.00元', request, [fixture.payload])
        self.assertEqual('COMPARISON_DATE_DOES_NOT_MATCH_REQUEST', bad['answer_validation']['reason'])

    def test_mandate_prerequisite_can_be_confirmed_without_claiming_merchant_matched(self):
        from tests.competition_tasks.test_mandate_match import plan
        task = self.d.f.tool('submit_task_plan', {'prompt': '查询后撤销云存储', 'title': '撤销',
            'idempotency_key': 'merchant-task', 'nodes': plan('虚构云存储代扣')})['data']['task']
        self.d.f.service.tasks.tick(task['task_id'])
        progress = self.d.f.tool('task_status', {'task_id': task['task_id']})['data']['task_progress']
        part = {'format': 'task_progress_v1', 'task_id': task['task_id'], 'source_id': progress['source_id']}
        request = item({'merchant_text': slot('m', '虚构云存储代扣', '虚构云存储代扣')}, intent='mandate_cancel')
        result = self.respond('查询后撤销虚构云存储代扣', request, [part], 'PREPARE')
        self.assertTrue(result['ok'], result)
        self.assertEqual('NEEDS_EVIDENCE', result['business_outcomes'][0]['assessment'])
        self.assertEqual('PREREQUISITE_ONLY', result['business_outcomes'][0]['constraint_phase'])
        self.assertIn('只能核对并确认前置查询', result['text'])
        self.assertNotIn('核清这些条件前不要确认', result['text'])

    def test_task_cannot_reintroduce_confirmation_advice_for_unverified_reserve(self):
        task = self.d.f.tool('submit_task_plan', {'prompt': '转账保留余额', 'title': '调拨', 'idempotency_key': 'reserve-task',
            'nodes': [{'id': 'transfer', 'tool': 'prepare_transfer', 'args': {'source_account': 'B', 'target_account': 'A',
                       'amount_minor': 17300, 'idempotency_key': 'reserve-task-node'}, 'depends_on': []}]})['data']['task']
        self.d.f.service.tasks.tick(task['task_id'])
        progress = self.d.f.tool('task_status', {'task_id': task['task_id']})['data']['task_progress']
        part = {'format': 'task_progress_v1', 'task_id': task['task_id'], 'source_id': progress['source_id']}
        result = self.respond('准备B到A173.00元保留900.00元', self.reserved_request(), [part], 'PREPARE')
        self.assertTrue(result['ok'], result['answer_validation'])
        self.assertEqual('NEEDS_EVIDENCE', result['business_outcomes'][0]['assessment'])
        self.assertNotIn('由本人确认或拒绝', result['text'])
        self.assertIn('暂不要确认待办', result['text'])

    def test_inherited_tomorrow_does_not_slide_when_amount_changes(self):
        request = item({'amount_minor': slot('m', '173.00元', 17300), 'date_text': slot('m', '明天', '明天')}, intent='arrangement')
        original = '准备明天173.00元'
        first = self.respond(original, request, [self.arrangement(day='2026-10-20')], 'PREPARE')
        self.assertTrue(first['ok'], first['answer_validation'])
        self.d.f.now = datetime(2026, 10, 20, 2, tzinfo=timezone.utc)
        for day, expected in [('2026-10-21', False), ('2026-10-20', True)]:
            self.d.f.trace = []
            revised = item({'amount_minor': slot('n', '180.00元', 18000)}, parent='m:transfer', intent='arrangement')
            result = self.continued(first, original, '金额改成180.00元，原日期准备', revised,
                                    [self.arrangement(18000, day)])
            self.assertEqual(expected, result['ok'], result['answer_validation'])

    def test_new_relative_date_replaces_original_anchor(self):
        original = '准备明天173.00元'
        request = item({'amount_minor': slot('m', '173.00元', 17300), 'date_text': slot('m', '明天', '明天')}, intent='arrangement')
        first = self.respond(original, request, [self.arrangement(day='2026-10-20')], 'PREPARE')
        self.d.f.now = datetime(2026, 10, 20, 2, tzinfo=timezone.utc); self.d.f.trace = []
        revised = item({'date_text': slot('n', '明天', '明天')}, parent='m:transfer', intent='arrangement')
        result = self.continued(first, original, '改成明天再准备', revised, [self.arrangement(day='2026-10-21')])
        self.assertTrue(result['ok'], result['answer_validation'])
        date_check = next(c for c in result['business_outcomes'][0]['constraint_checks'] if c['field'] == 'date_text')
        self.assertEqual('2026-10-21', date_check['canonical_value'])

    def test_unanchored_legacy_relative_date_remains_unverified(self):
        original = '准备明天173.00元'
        request = item({'amount_minor': slot('m', '173.00元', 17300), 'date_text': slot('m', '明天', '明天')}, intent='arrangement')
        first = self.respond(original, request, [self.arrangement(day='2026-10-20')], 'PREPARE')
        self.d.f.now = datetime(2026, 10, 20, 2, tzinfo=timezone.utc); self.d.f.trace = []
        result = self.continued(first, original, '原条件再准备', item(parent='m:transfer', intent='arrangement'),
                                [self.arrangement(day='2026-10-21')], legacy=True)
        self.assertTrue(result['ok'], result['answer_validation'])
        self.assertEqual('NEEDS_EVIDENCE', result['business_outcomes'][0]['assessment'])
        self.assertIn('日期要求', result['text'])

    def test_previous_month_keeps_original_period_until_user_revises(self):
        original = '分析上个月'
        request = item({'period_text': slot('m', '上个月', '上个月')}, intent='billing')
        first = self.respond(original, request, [self.bill()])
        self.d.f.now = datetime(2026, 11, 19, 2, tzinfo=timezone.utc)
        for start, end, expected in [('2026-09-01', '2026-09-30', True), ('2026-10-01', '2026-10-31', False)]:
            self.d.f.trace = []
            result = self.continued(first, original, '继续核对原来的月份', item(parent='m:transfer', intent='billing'),
                                    [self.bill(start, end)], action='ANALYZE')
            self.assertEqual(expected, result['ok'], result['answer_validation'])
        self.d.f.trace = []
        revised = item({'period_text': slot('n', '上个月', '上个月')}, parent='m:transfer', intent='billing')
        result = self.continued(first, original, '改查现在的上个月', revised,
                                [self.bill('2026-10-01', '2026-10-31')], action='ANALYZE')
        self.assertTrue(result['ok'], result['answer_validation'])


if __name__ == '__main__':
    unittest.main()
