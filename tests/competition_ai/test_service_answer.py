"""Composed business answers against actual isolated ledgers; no paid models."""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from pathlib import Path
import json
import tempfile
import unittest

from src.competition_web.app import create_app
from src.bank_v1_web.bank_answer import validate_bank_answer
from src.bank_evidence.service_answer import recheck


class ServiceAnswerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='service-answer-')
        self.addCleanup(self.temp.cleanup)
        self.now = datetime(2026, 10, 19, 2, tzinfo=timezone.utc)
        # Legacy typed-component/domain fixture; it does not impersonate a v2
        # scoped model run. Product v2 entry coverage lives in test_handoff_*.
        from src.competition_web.service import CompetitionService
        from src.bank_v1 import Service
        root=Path(self.temp.name)
        self.service=CompetitionService(root,bank=Service(root/'bank.sqlite3',clock=lambda:self.now),clock=lambda:self.now)
        self.trace = []

    def tool(self, name, args):
        body = self.service.call_tool(name, args)
        identity = str(len(self.trace))
        self.trace += [{'kind': 'request', 'name': name, 'arguments': args, 'call_id': identity},
                       {'kind': 'response', 'tool': name, 'args': args, 'call_id': identity,
                        'ok': True, 'status': 200, 'response': body}]
        return body

    def answer(self, parts):
        return validate_bank_answer({'ok': True, 'text': json.dumps({'format': 'bank_service_v1', 'parts': parts}),
                                     'trace': self.trace}, '请按要求办理并核对状态')

    def transfer(self):
        return self.tool('prepare_transfer', {'source_account': 'A', 'target_account': 'B',
            'amount_minor': 17300, 'idempotency_key': 'sample-transfer'})['data']['operation']

    def part(self, op):
        body = self.tool('operation', {'operation_id': op['operation_id']})
        return {'format': 'bank_operation_v1', 'operation_id': op['operation_id'], 'evidence_id': body['evidence_id']}

    def confirm(self, op):
        return self.service.bank.user_action('confirm', {'operation_id': op['operation_id'], 'expected_summary': op['summary']})

    def test_prepare_direction_amount_and_separate_confirmation(self):
        before = self.service.bank.state()['accounts']
        op = self.transfer()
        result = self.answer([self.part(op)])
        self.assertTrue(result['ok'], result)
        self.assertIn('A → 转入账户 B，金额 173.00元', result['text'])
        self.assertIn('尚未办理', result['text'])
        self.assertEqual(before, self.service.bank.state()['accounts'])
        self.assertTrue(recheck(result, self.service.call_tool)['ok'])

    def test_prepare_source_cannot_replace_required_operation_read(self):
        body = self.tool('prepare_mandate_query', {'idempotency_key': 'q'})
        part = {'format': 'bank_operation_v1', 'operation_id': body['data']['operation']['operation_id'], 'evidence_id': body['evidence_id']}
        self.assertFalse(self.answer([part])['ok'])

    def test_billing_scope_does_not_deny_an_actual_pending_transfer(self):
        report = self.tool('billing_report', {'start_date': '2026-09-01', 'end_date': '2026-09-30', 'compare_previous': False})
        op = self.transfer()
        part = {'format': 'bank_billing_v1', 'report_evidence_id': report['evidence_id'],
                'sections': ['overview'], 'fact_ids': [], 'calculation_ids': []}
        result = self.answer([part, self.part(op)])
        self.assertTrue(result['ok'], result)
        self.assertNotIn('未发起银行操作', result['text'])
        self.assertIn('本段账单查询不办理银行操作', result['text'])
        self.assertIn('已准备待确认', result['text'])

    def test_fact_and_operation_cannot_hide_or_relabel_each_other(self):
        accounts = self.tool('accounts', {})
        op = self.transfer()
        part = self.part(op)
        facts = {'format': 'bank_facts_v1', 'fact_ids': [accounts['fact_catalog']['facts'][0]['fact_id']], 'calculation_ids': []}
        result = self.answer([facts, part])
        self.assertTrue(result['ok'], result)
        self.assertFalse(self.answer([facts])['ok'])
        altered = deepcopy(part); altered['amount_minor'] = 173000
        self.assertFalse(self.answer([facts, altered])['ok'])
        altered = deepcopy(part); altered['operation_id'] = 'op_unknown'
        self.assertFalse(self.answer([facts, altered])['ok'])

    def test_rejection_and_expiration_invalidate_old_display_without_writes(self):
        op = self.transfer()
        old = self.answer([self.part(op)])
        self.now += timedelta(minutes=6)
        self.assertFalse(recheck(old, self.service.call_tool)['ok'])
        current = self.answer([self.part(op)])
        self.assertIn('已过期', current['text'])
        self.service.bank.user_action('reject', {'operation_id': op['operation_id']})
        self.assertFalse(recheck(current, self.service.call_tool)['ok'])
        rejected = self.answer([self.part(op)])
        self.assertTrue(rejected['ok'], rejected)
        self.assertIn('已被拒绝', rejected['text'])

    def test_actual_receipt_and_bank_state_no_repeat(self):
        op = self.transfer(); self.confirm(op)
        before = self.service.bank.state()
        result = self.answer([self.part(op)])
        self.assertTrue(result['ok'], result)
        self.assertIn('银行操作已成功', result['text'])
        self.assertIn('不会增加合计资金', result['text'])
        self.assertTrue(recheck(result, self.service.call_tool)['ok'])
        self.assertEqual(before, self.service.bank.state())

    def test_query_then_cancellation_show_receipt_time_and_contract_limit(self):
        query = self.tool('prepare_mandate_query', {'idempotency_key': 'q'})['data']['operation']
        confirmed = self.confirm(query)
        query_part = self.part(query)
        shown = self.answer([query_part])
        self.assertTrue(shown['ok'], shown)
        self.assertIn('当时的登记清单', shown['text'])
        self.assertIn('虚构云存储代扣', shown['text'])
        mid = confirmed['receipt']['mandates'][0]['mandate_id']
        cancel = self.tool('prepare_mandate_cancel', {'mandate_id': mid, 'idempotency_key': 'cancel'})['data']['operation']
        self.confirm(cancel)
        result = self.answer([query_part, self.part(cancel)])
        self.assertTrue(result['ok'], result)
        self.assertIn('该笔银行代扣授权已撤销', result['text'])
        self.assertIn('不等于解除商户合同', result['text'])

    def test_event_posting_and_latest_projection(self):
        action = self.service.bank.user_action('inject_transaction', {'kind': 'expense', 'amount_minor': 237,
                     'account_id': 'A', 'description': '虚构消费', 'idempotency_key': 'event'})
        body = self.tool('event_context', {'event_id': action['event_id']})
        part = {'format': 'bank_event_v1', 'event_id': action['event_id'], 'evidence_id': body['evidence_id']}
        result = self.answer([part])
        self.assertTrue(result['ok'], result)
        self.assertIn('消费已记账', result['text'])
        self.assertIn('-2.37元', result['text'])
        self.assertTrue(recheck(result, self.service.call_tool)['ok'])
        self.service.bank.user_action('inject_transaction', {'kind': 'income', 'amount_minor': 100,
                    'idempotency_key': 'new-income'})
        self.assertFalse(recheck(result, self.service.call_tool)['ok'])

    def test_task_admission_must_be_represented_and_progress_can_compose(self):
        args = {'prompt': '先核对账户再查安排', 'title': '核对任务', 'idempotency_key': 'task', 'nodes': [
            {'id': 'accounts', 'tool': 'accounts', 'args': {}, 'depends_on': []},
            {'id': 'arrangements', 'tool': 'arrangements', 'args': {}, 'depends_on': ['accounts']}]}
        task = self.tool('submit_task_plan', args)['data']['task']
        accounts = self.tool('accounts', {})
        facts = {'format': 'bank_facts_v1', 'fact_ids': [accounts['fact_catalog']['facts'][0]['fact_id']], 'calculation_ids': []}
        self.assertFalse(self.answer([facts])['ok'])
        progress = self.tool('task_status', {'task_id': task['task_id']})['data']['task_progress']
        part = {'format': 'task_progress_v1', 'task_id': task['task_id'], 'source_id': progress['source_id']}
        result = self.answer([facts, part])
        self.assertTrue(result['ok'], result)
        self.now += timedelta(seconds=5)
        self.assertTrue(recheck(result, self.service.call_tool)['ok'])
        self.service.tasks.pause(task['task_id'])
        self.assertFalse(recheck(result, self.service.call_tool)['ok'])


if __name__ == '__main__':
    unittest.main()
