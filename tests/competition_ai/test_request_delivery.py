"""Constructed AI decisions, real isolated tools: request/result boundary tests."""
from copy import deepcopy
from datetime import timedelta
import unittest

from tests.competition_ai import test_service_answer as fixtures
from tests.competition_ai.test_request_decision import item, slot, response
from src.bank_requests.context import build
from src.bank_requests.response import validate
from src.bank_v1_web.display_check import check
from src.bank_v1 import DomainError


class RequestDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.ServiceAnswerTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def request(self, amount=17300, source='A', target='B', identifier='transfer'):
        quote = f'{amount / 100:.2f}元'
        return item({'source_account': slot('m', source, source), 'target_account': slot('m', target, target),
                     'amount_minor': slot('m', quote, amount)}, identifier=identifier)

    def answer(self, items, parts, prompt='A B 173.00元 975.25元 1280.50元 3000.00元 上月'):
        context = build('m', 'm', [{'message_id': 'm', 'text': prompt}], [], read_only=False)
        answer = {'format': 'bank_outcomes_v1', 'items': [
            {'item_id': identity, 'components': components} for identity, components in parts.items()]}
        raw = response(context, items, answer)
        raw['trace'] = deepcopy(self.f.trace)
        return validate(raw, context)

    def billing(self):
        body = self.f.tool('billing_report', {'start_date': '2026-09-01', 'end_date': '2026-09-30', 'compare_previous': False})
        return {'format': 'bank_billing_v1', 'report_evidence_id': body['evidence_id'],
                'sections': ['overview'], 'fact_ids': [], 'calculation_ids': []}

    def calculation(self, amount, source='B'):
        accounts = self.f.tool('accounts', {})
        fact = next(f for f in accounts['fact_catalog']['facts'] if f['entity'] == source)
        calc = self.f.tool('money_calculate', {'operation': 'subtract', 'currency': 'CNY',
            'left_minor': amount, 'left_source': {'kind': 'user_assumption', 'quote': f'{amount / 100:.2f}元'},
            'right_minor': fact['value'], 'right_source': {'kind': 'bank_fact', 'tool': 'accounts', 'arguments': {}, 'fact_id': fact['fact_id']}})
        return {'format': 'bank_facts_v1', 'fact_ids': [fact['fact_id']], 'calculation_ids': [calc['evidence_id']]}

    def failure(self, args, code=None, status=503):
        if code is None:
            try:
                self.f.service.call_tool('prepare_transfer', args)
            except DomainError as error:
                code, status = error.code, 409  # Bank HTTP boundary maps domain refusal to 409.
            else:
                self.fail('Expected a real bank refusal')
        identity = str(len(self.f.trace))
        self.f.trace += [{'kind': 'request', 'name': 'prepare_transfer', 'arguments': args, 'call_id': identity},
            {'kind': 'response', 'tool': 'prepare_transfer', 'args': args, 'call_id': identity,
             'ok': False, 'status': status, 'response': {'error': {'code': code}}}]
        return {'format': 'bank_tool_outcome_v1', 'tool': 'prepare_transfer', 'arguments': args, 'error_code': code}

    def test_mixed_billing_and_pending_transfer_have_separate_outcomes(self):
        bill = self.billing()
        before = self.f.service.bank.state()['accounts']
        op = self.f.transfer()
        parts = {'billing': [bill], 'transfer': [self.f.part(op)]}
        result = self.answer([item(identifier='billing', intent='billing'), self.request()], parts)
        self.assertTrue(result['ok'], result)
        self.assertEqual(['ADVICE_READY', 'ADVICE_READY'], [o['assessment'] for o in result['business_outcomes']])
        self.assertEqual(['NOT_REQUESTED', 'WAITING_CONFIRMATION'], [o['execution'] for o in result['business_outcomes']])
        self.assertTrue(check(result, self.f.service)['ok'])
        self.assertEqual(before, self.f.service.bank.state()['accounts'])

    def test_cannot_omit_item_or_hide_prepared_operation_behind_billing(self):
        bill = self.billing(); self.f.transfer()
        result = self.answer([item(identifier='billing', intent='billing'), self.request()], {'billing': [bill]})
        self.assertEqual('OMITTED_REQUEST_ITEM', result['answer_validation']['reason'])
        result = self.answer([item(identifier='billing', intent='billing')], {'billing': [bill]})
        self.assertEqual('OMITTED_PREPARED_OPERATION', result['answer_validation']['reason'])

    def test_two_transfers_cannot_swap_amounts_or_accounts(self):
        first = self.f.transfer()
        second = self.f.tool('prepare_transfer', {'source_account': 'B', 'target_account': 'A', 'amount_minor': 97525,
                            'idempotency_key': 'other-transfer'})['data']['operation']
        p1, p2 = self.f.part(first), self.f.part(second)
        items = [self.request(), self.request(97525, 'B', 'A', 'second')]
        good = self.answer(items, {'transfer': [p1], 'second': [p2]})
        self.assertTrue(good['ok'], good)
        bad = self.answer(items, {'transfer': [p2], 'second': [p1]})
        self.assertEqual('OPERATION_PARAMETERS_DO_NOT_MATCH_REQUEST', bad['answer_validation']['reason'])

    def test_plain_tool_facts_produce_insufficient_condition_without_special_answer(self):
        before = self.f.service.bank.state()
        part = self.calculation(128050)
        result = self.answer([self.request(128050, 'B', 'A')], {'transfer': [part]})
        self.assertTrue(result['ok'], result)
        outcome = result['business_outcomes'][0]
        self.assertEqual(('CONDITION_UNMET', 'NOT_PREPARED', 'INSUFFICIENT_FUNDS'),
                         (outcome['assessment'], outcome['execution'], outcome['reason']))
        self.assertIn('280.50', result['text'])
        self.assertNotIn('希望转多少', result['text'])
        self.assertEqual(before, self.f.service.bank.state())

    def test_failed_attempt_cannot_be_omitted_or_assigned_to_other_amount(self):
        bill = self.billing()
        failure = self.failure({'source_account': 'B', 'target_account': 'A', 'amount_minor': 128050, 'idempotency_key': 'refused'})
        hidden = self.answer([item(identifier='billing', intent='billing')], {'billing': [bill]})
        self.assertEqual('OMITTED_WRITE_FAILURE', hidden['answer_validation']['reason'])
        good = self.answer([self.request(128050, 'B', 'A')], {'transfer': [failure]})
        self.assertTrue(good['ok'], good)
        self.assertEqual('CONDITION_UNMET', good['business_outcomes'][0]['assessment'])
        wrong = self.answer([self.request(97525, 'B', 'A')], {'transfer': [failure]})
        self.assertFalse(wrong['ok'], 'A refusal for another amount must not be this request outcome')

    def test_unknown_attempt_does_not_claim_no_operation_or_success(self):
        part = self.failure({'source_account': 'A', 'target_account': 'B', 'amount_minor': 17300,
                             'idempotency_key': 'unknown'}, code='TIMEOUT')
        result = self.answer([self.request()], {'transfer': [part]})
        self.assertTrue(result['ok'], result)
        self.assertEqual('UNKNOWN', result['business_outcomes'][0]['execution'])
        self.assertEqual('RECONCILE_ORIGINAL', result['business_outcomes'][0]['next_step'])

    def test_receipt_rejection_and_expiry_states_use_bank_not_model(self):
        op = self.f.transfer()
        pending = self.answer([self.request()], {'transfer': [self.f.part(op)]})
        self.f.now += timedelta(minutes=6)
        self.assertFalse(check(pending, self.f.service)['ok'])
        expired = self.answer([self.request()], {'transfer': [self.f.part(op)]})
        self.assertEqual('EXPIRED', expired['business_outcomes'][0]['execution'])
        self.f.service.bank.user_action('reject', {'operation_id': op['operation_id']})
        rejected = self.answer([self.request()], {'transfer': [self.f.part(op)]})
        self.assertEqual('REJECTED', rejected['business_outcomes'][0]['execution'])
        self.f.trace = []  # A new user turn has its own trace; old receipts remain in bank storage.
        op2 = self.f.tool('prepare_transfer', {'source_account': 'A', 'target_account': 'B', 'amount_minor': 17300,
                                              'idempotency_key': 'success'})['data']['operation']
        self.f.confirm(op2)
        current = self.answer([self.request()], {'transfer': [self.f.part(op2)]})
        self.assertTrue(current['ok'], current)
        self.assertEqual('SUCCEEDED', current['business_outcomes'][0]['execution'])

    def test_strong_verification_is_not_ordinary_confirmation(self):
        op = self.f.tool('prepare_transfer', {'source_account': 'A', 'target_account': 'B', 'amount_minor': 300000,
                                             'idempotency_key': 'strong'})['data']['operation']
        result = self.answer([self.request(300000)], {'transfer': [self.f.part(op)]})
        self.assertTrue(result['ok'], result)
        self.assertEqual('WAITING_VERIFICATION', result['business_outcomes'][0]['execution'])

    def test_changed_ledger_invalidates_old_analysis_without_replay(self):
        part = self.calculation(128050)
        result = self.answer([self.request(128050, 'B', 'A')], {'transfer': [part]})
        self.f.service.bank.user_action('inject_transaction', {'kind': 'income', 'account_id': 'B', 'amount_minor': 50000,
                'description': '到账', 'idempotency_key': 'incoming'})
        state = self.f.service.bank.state()
        self.assertFalse(check(result, self.f.service)['ok'])
        self.assertEqual(state, self.f.service.bank.state())

    def test_billing_cannot_use_a_different_period(self):
        part = self.billing()
        requested = item({'start_date': slot('m', '2026-08-01', '2026-08-01')}, identifier='billing', intent='billing')
        result = self.answer([requested], {'billing': [part]}, prompt='2026-08-01开始的账单')
        self.assertEqual('BILLING_PERIOD_DOES_NOT_MATCH_REQUEST', result['answer_validation']['reason'])

    def test_task_refresh_does_not_keep_waiting_after_read_only_completion(self):
        task = self.f.tool('submit_task_plan', {'prompt': '核对账户', 'title': '核对', 'idempotency_key': 'read-task',
            'nodes': [{'id': 'accounts', 'tool': 'accounts', 'args': {}, 'depends_on': []}]})['data']['task']
        progress = self.f.tool('task_status', {'task_id': task['task_id']})['data']['task_progress']
        part = {'format': 'task_progress_v1', 'task_id': task['task_id'], 'source_id': progress['source_id']}
        result = self.answer([item(identifier='task', intent='task_status')], {'task': [part]})
        self.assertTrue(result['ok'], result)
        self.f.service.tasks.tick(task['task_id'])
        refreshed = check(result, self.f.service)
        self.assertTrue(refreshed['ok'], refreshed)
        outcome = refreshed['business_outcomes'][0]
        self.assertEqual('SUCCEEDED', outcome['task_states'][0]['state'])
        self.assertEqual('NOT_REQUESTED', outcome['execution'])
        self.assertEqual('DONE', outcome['next_step'])

    def test_task_bank_receipt_refresh_and_remaining_steps_are_distinct(self):
        task = self.f.tool('submit_task_plan', {'prompt': '先转账再核对', 'title': '调拨', 'idempotency_key': 'write-task',
            'nodes': [{'id': 'transfer', 'tool': 'prepare_transfer', 'args': {'source_account': 'A', 'target_account': 'B',
                       'amount_minor': 17300, 'idempotency_key': 'node-transfer'}, 'depends_on': []},
                      {'id': 'accounts', 'tool': 'accounts', 'args': {}, 'depends_on': ['transfer']}]})['data']['task']
        self.f.service.tasks.tick(task['task_id'])
        progress = self.f.tool('task_status', {'task_id': task['task_id']})['data']['task_progress']
        part = {'format': 'task_progress_v1', 'task_id': task['task_id'], 'source_id': progress['source_id']}
        result = self.answer([self.request()], {'transfer': [part]})
        self.assertTrue(result['ok'], result)
        self.assertEqual('WAITING_CONFIRMATION', result['business_outcomes'][0]['execution'])
        op = next(iter(progress['data']['operations'].values()))
        self.f.confirm(op)
        refreshed = check(result, self.f.service)
        self.assertTrue(refreshed['ok'], refreshed)
        outcome = refreshed['business_outcomes'][0]
        self.assertEqual('SUCCEEDED', outcome['execution'])
        self.assertEqual('BANK_DONE_TASK_UNFINISHED', outcome['reason'])
        self.assertEqual('WAIT_TASK', outcome['next_step'])

    def test_cancellation_task_query_confirmation_is_not_cancellation_complete(self):
        from tests.competition_tasks.test_mandate_match import plan
        task = self.f.tool('submit_task_plan', {'prompt': '查清后撤销云存储代扣', 'title': '撤销',
            'idempotency_key': 'mandate-task', 'nodes': plan('虚构云存储代扣')})['data']['task']
        self.f.service.tasks.tick(task['task_id'])
        progress = self.f.tool('task_status', {'task_id': task['task_id']})['data']['task_progress']
        part = {'format': 'task_progress_v1', 'task_id': task['task_id'], 'source_id': progress['source_id']}
        result = self.answer([item(identifier='cancel', intent='mandate_cancel')], {'cancel': [part]})
        self.assertTrue(result['ok'], result)
        self.assertEqual('PREREQUISITE_CONFIRMATION', result['business_outcomes'][0]['reason'])
        self.assertEqual('WAITING_CONFIRMATION', result['business_outcomes'][0]['execution'])
        self.assertIn('目标业务尚未完成', result['text'])

    def test_insight_check_must_be_disclosed_and_display_does_not_rescan(self):
        from unittest.mock import patch
        body = self.f.tool('check_insights', {})
        part = {'format': 'bank_insights_v1', 'source_tool': 'check_insights', 'evidence_id': body['evidence_id'],
                'sections': ['overview'], 'candidate_ids': [], 'notification_ids': []}
        result = self.answer([item(identifier='insights', intent='subscription_review')], {'insights': [part]})
        self.assertTrue(result['ok'], result)
        with patch.object(self.f.service, 'call_tool', wraps=self.f.service.call_tool) as calls:
            shown = check(result, self.f.service)
        self.assertTrue(shown['ok'], shown)
        self.assertIn('insight_status', [c.args[0] for c in calls.call_args_list])
        self.assertNotIn('check_insights', [c.args[0] for c in calls.call_args_list])
