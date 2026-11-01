"""User outcomes and failure boundaries against temporary real bank state."""
from copy import deepcopy
from datetime import timedelta
import json
import unittest

from tests.competition_ai import test_service_answer as fixture
from src.bank_evidence.answer import paired_records
from src.bank_evidence.service_answer import recheck
from src.bank_v1.common import DomainError
from src.bank_v1_web.bank_answer import validate_bank_answer


class DialoguePartsTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.ServiceAnswerTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def facts(self):
        data = self.f.tool('accounts', {})
        return {'format': 'bank_facts_v1', 'fact_ids': [data['fact_catalog']['facts'][0]['fact_id']],
                'calculation_ids': []}

    def guidance(self):
        return {'format': 'bank_guidance_v1', 'kind': 'request_parameters',
                'tool': 'prepare_transfer', 'fields': ['amount_minor']}

    def fail(self, args=None):
        args = args or {'source_account': 'A', 'target_account': 'B',
                        'amount_minor': 900000, 'idempotency_key': 'failure'}
        with self.assertRaises(DomainError) as caught:
            self.f.service.call_tool('prepare_transfer', args)
        return self.failure_record(args, caught.exception.code, 409)

    def failure_record(self, args, code, status):
        identity = str(len(self.f.trace))
        self.f.trace += [{'kind': 'request', 'name': 'prepare_transfer', 'arguments': args, 'call_id': identity},
                        {'kind': 'response', 'tool': 'prepare_transfer', 'args': args, 'call_id': identity,
                         'ok': False, 'status': status, 'response': {'error': {'code': code, 'message': 'untrusted'}}}]
        return {'format': 'bank_tool_outcome_v1', 'tool': 'prepare_transfer',
                'arguments': deepcopy(args), 'error_code': code}

    def test_query_then_clarify_without_preparation(self):
        before = self.f.service.bank.state()
        result = self.f.answer([self.facts(), self.guidance()])
        self.assertTrue(result['ok'], result)
        self.assertTrue(result['text'].startswith('您希望转多少元'))
        self.assertIn('单独确认', result['text'])
        self.assertTrue(recheck(result, self.f.service.call_tool)['ok'])
        self.assertEqual(before, self.f.service.bank.state())

    def test_pure_question_is_allowed_but_free_claim_is_not(self):
        self.assertTrue(self.f.answer([self.guidance()])['ok'])
        part = self.guidance(); part['amount_minor'] = 900000
        self.assertFalse(self.f.answer([part])['ok'])
        part = self.guidance(); part['fields'] = ['available_transfer_minor']
        self.assertFalse(self.f.answer([part])['ok'])
        part = self.guidance(); part['fields'] = ['amount_minor', 'amount_minor']
        self.assertFalse(self.f.answer([part])['ok'])

    def test_period_questions_and_account_choices_are_generic(self):
        for tool, fields in [('billing_report', ['start_date', 'end_date']),
                             ('prepare_transfer', ['target_account', 'source_account'])]:
            with self.subTest(tool=tool):
                part = {'format': 'bank_guidance_v1', 'kind': 'request_parameters', 'tool': tool, 'fields': fields}
                self.assertTrue(self.f.answer([part])['ok'])

    def test_assessment_requires_actual_forecast_and_never_prepares(self):
        before = self.f.service.bank.state()
        body = self.f.tool('forecast', {'through_date': '2026-11-17'})
        part = {'format': 'bank_guidance_v1', 'kind': 'transfer_assessment',
                'forecast_evidence_id': body['evidence_id']}
        result = self.f.answer([part, self.facts()])
        self.assertTrue(result['ok'], result)
        self.assertIn('不能仅凭合计预测', result['text'])
        self.assertIn('从哪个账户还款', result['text'])
        self.assertTrue(recheck(result, self.f.service.call_tool)['ok'])
        self.assertEqual(before, self.f.service.bank.state())
        part['forecast_evidence_id'] = 'invented'
        self.assertFalse(self.f.answer([part])['ok'])

    def test_changed_forecast_is_rejected_at_display(self):
        body = self.f.tool('forecast', {'through_date': '2026-11-17'})
        part = {'format': 'bank_guidance_v1', 'kind': 'transfer_assessment', 'forecast_evidence_id': body['evidence_id']}
        old = self.f.answer([part])
        self.f.service.bank.user_action('inject_transaction', {'kind': 'expense', 'amount_minor': 137,
            'account_id': 'A', 'description': '虚构消费', 'idempotency_key': 'stale'})
        before = self.f.service.bank.state()
        self.assertFalse(recheck(old, self.f.service.call_tool)['ok'])
        self.assertEqual(before, self.f.service.bank.state())
        self.f.tool('forecast', {'through_date': '2026-11-17'})
        self.assertFalse(self.f.answer([part])['ok'])

    def test_explicit_refusal_is_visible_and_not_replayed(self):
        before = self.f.service.bank.state()
        facts, failure = self.facts(), self.fail()
        result = self.f.answer([facts, failure])
        self.assertTrue(result['ok'], result)
        self.assertIn('余额不足', result['text'])
        self.assertIn('未由这次尝试生成新的待确认操作', result['text'])
        read_calls = []
        def read(name, args):
            read_calls.append(name)
            self.assertNotIn(name, ('prepare_transfer', 'prepare_mandate_cancel'))
            return self.f.service.call_tool(name, args)
        self.assertTrue(recheck(result, read)['ok'])
        self.assertIn('accounts', read_calls)
        self.assertEqual(before, self.f.service.bank.state())

    def test_failure_cannot_disappear_or_be_relabelled(self):
        facts, failure = self.facts(), self.fail()
        self.assertFalse(self.f.answer([facts])['ok'])
        for text in [facts, {'format': 'task_progress_v1', 'task_id': 'fake', 'source_id': 'fake'},
                     {'format': 'goal_answer_v1', 'source_id': 'fake'}]:
            result = validate_bank_answer({'ok': True, 'trace': self.f.trace, 'text': json.dumps(text)}, '')
            self.assertFalse(result['ok'])
            self.assertEqual('bank_service_v1', result['answer_validation']['version'])
        for field, value in [('error_code', 'ALREADY_CANCELLED'), ('arguments', {}), ('tool', 'confirm')]:
            changed = deepcopy(failure); changed[field] = value
            self.assertFalse(self.f.answer([facts, changed])['ok'])

    def test_transport_error_never_means_no_operation(self):
        part = self.failure_record({'idempotency_key': 'unknown'}, 'INTERNAL_ERROR', 500)
        result = self.f.answer([part])
        self.assertTrue(result['ok'], result)
        self.assertIn('不能认定未产生操作', result['text'])
        self.assertNotIn('未由这次尝试生成', result['text'])
        self.assertNotIn('untrusted', result['text'])
        # Even a business-looking code on a server error cannot certify refusal.
        self.f.trace[-1]['response']['error']['code'] = 'INSUFFICIENT_FUNDS'
        part['error_code'] = 'INSUFFICIENT_FUNDS'
        self.assertIn('不能认定未产生操作', self.f.answer([part])['text'])

    def test_failed_attempt_and_later_preparation_both_required(self):
        failure = self.fail()
        op = self.f.transfer()
        current = self.f.part(op)
        self.assertFalse(self.f.answer([failure])['ok'])
        self.assertFalse(self.f.answer([current])['ok'])
        result = self.f.answer([failure, current])
        self.assertTrue(result['ok'], result)
        self.assertIn('一次失败尝试', result['text'])
        self.assertIn('已准备待确认', result['text'])

    def test_idempotency_conflict_does_not_deny_original_operation(self):
        op = self.f.transfer()
        args = dict(op['summary']['params'], idempotency_key='sample-transfer')
        args['amount_minor'] += 100
        failure = self.fail(args)
        result = self.f.answer([failure, self.f.part(op)])
        self.assertTrue(result['ok'], result)
        self.assertIn('原请求可能已有操作', result['text'])
        self.assertNotIn('未由这次尝试生成', result['text'])

    def test_all_distinct_failures_and_pairing_required(self):
        first = self.fail()
        second = self.fail({'source_account': 'A', 'target_account': 'A',
                            'amount_minor': 12500, 'idempotency_key': 'other'})
        self.assertFalse(self.f.answer([first])['ok'])
        self.assertTrue(self.f.answer([first, second])['ok'])
        self.assertEqual([], paired_records(self.f.trace))
        self.assertEqual(2, len(paired_records(self.f.trace, include_failures=True)))
        self.f.trace[-1]['call_id'] = 'not-paired'
        self.assertFalse(self.f.answer([first, second])['ok'])


if __name__ == '__main__':
    unittest.main()
