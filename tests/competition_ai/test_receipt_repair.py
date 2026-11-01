"""Only actual successful receipts admit one fresh read-only action-evidence pass."""
import copy
import json
import unittest
from unittest.mock import Mock, patch

from src.bank_v1_web.bank_repair import repair_prompt
from src.bank_v1_web.worker import DshRunner


def operation(kind='mandate_cancel', identifier='op_cancel'):
    return {'operation_id': identifier, 'kind': kind, 'status': 'SUCCEEDED',
            'receipt': {'operation_id': identifier, 'kind': kind, 'status': 'SUCCEEDED',
                        'receipt_id': 'receipt_' + identifier, 'mandate_status': 'CANCELLED'}}


def record(op=None, tool='operation'):
    return {'kind': 'response', 'tool': tool, 'ok': True,
            'response': {'data': {'operation': operation() if op is None else op}}}


def task_record(op=None, node_tool='operation', node_state='SUCCEEDED'):
    node = {'id': 'verify', 'tool': node_tool, 'state': node_state,
            'result': {'data': {'operation': operation() if op is None else op}}}
    return {'kind': 'response', 'tool': 'task_status', 'ok': True,
            'response': {'data': {'task': {'task_id': 'task_verified',
                'state': 'WAITING_CONFIRMATION', 'scope': 'bank', 'nodes': [node]}}}}


def rejected(trace=None, codes=('UNBACKED_OPERATION_SUCCESS',)):
    return {'ok': False, 'text': '查询已成功，旧金额72.00元。',
            'trace': [record()] if trace is None else trace,
            'answer_validation': {'status': 'REJECTED', 'reasons': [{'code': c} for c in codes],
                'operation_ids': ['op_untrusted_validation'], 'task_ids': ['task_untrusted_validation']}}


def payload(result, seconds=100):
    prompt, reason = repair_prompt(result, '核对当前办理结果', seconds)
    return (json.loads(prompt.rsplit('\n', 1)[1]) if prompt else None), reason


class ReceiptEligibilityTests(unittest.TestCase):
    def assert_denied(self, trace):
        value, reason = payload(rejected(trace))
        self.assertIsNone(value)
        self.assertEqual(reason, 'NO_VERIFIED_SUCCESSFUL_OPERATION')

    def test_scope_omitted_real_shape_and_verified_locators(self):
        data, reason = payload(rejected())
        self.assertEqual(reason, 'READONLY_OPERATION_EVIDENCE_REPAIR')
        self.assertEqual(data['known_locators'], {'operation_ids': ['op_cancel'], 'task_ids': []})

    def test_success_with_numeric_errors_qualifies(self):
        data, reason = payload(rejected(codes=('UNBACKED_OPERATION_SUCCESS',
            'UNSUPPORTED_DISPLAYED_AMOUNT', 'UNSUPPORTED_DISPLAYED_PERCENT')))
        self.assertIsNotNone(data)
        self.assertEqual(reason, 'READONLY_OPERATION_EVIDENCE_REPAIR')

    def test_explicit_bank_scope(self):
        item = record(); item['response']['scope'] = 'bank'
        self.assertIsNotNone(payload(rejected([item]))[0])

    def test_explicit_other_scope_cannot_qualify(self):
        item = record(); item['response']['scope'] = 'moneydata'
        self.assert_denied([item])

    def test_two_real_receipts_supply_two_locators(self):
        data, _ = payload(rejected([record(), record(operation('mandate_query', 'op_query'))]))
        self.assertEqual(data['known_locators']['operation_ids'], ['op_cancel', 'op_query'])

    def test_empty_trace_cannot_trust_validation_ids(self):
        self.assert_denied([])

    def test_request_or_failed_response_cannot_qualify(self):
        request = {'kind': 'request', 'tool': 'operation', 'arguments': {'operation': operation()}}
        failed = record(); failed['ok'] = False
        self.assert_denied([request, failed])

    def test_only_true_ok_qualifies(self):
        for invalid in (1, 'true', None):
            with self.subTest(ok=invalid):
                item = record(); item['ok'] = invalid
                self.assert_denied([item])

    def test_actual_operation_not_arguments_or_arbitrary_wrappers(self):
        for key in ('args', 'arguments', 'inputs', 'description', 'nested'):
            with self.subTest(key=key):
                item = record(); item['response']['data'] = {key: {'operation': operation()}}
                self.assert_denied([item])

    def test_accounts_tool_cannot_smuggle_an_operation(self):
        self.assert_denied([record(tool='accounts')])

    def test_bank_operation_kind_is_required(self):
        self.assert_denied([record(operation('merchant_service_cancel'))])

    def test_operation_non_success_never_qualifies(self):
        for status in ('PENDING', 'FAILED', 'UNKNOWN', 'REJECTED'):
            with self.subTest(status=status):
                op = operation(); op['status'] = status
                self.assert_denied([record(op)])

    def test_receipt_must_be_structured_success(self):
        for receipt in (None, 'receipt_example', {}, {'receipt_id': 'receipt_example'}):
            with self.subTest(receipt=receipt):
                op = operation(); op['receipt'] = receipt
                self.assert_denied([record(op)])

    def test_receipt_fields_must_match_operation(self):
        for key, value in (('operation_id', 'op_wrong'), ('kind', 'transfer'),
                           ('status', 'PENDING'), ('receipt_id', ' ')):
            with self.subTest(key=key):
                op = operation(); op['receipt'][key] = value
                self.assert_denied([record(op)])

    def test_empty_operation_identifier_rejected(self):
        self.assert_denied([record(operation(identifier=' '))])

    def test_successful_task_node_can_supply_real_locators(self):
        data, reason = payload(rejected([task_record()]))
        self.assertEqual(reason, 'READONLY_OPERATION_EVIDENCE_REPAIR')
        self.assertEqual(data['known_locators'], {'operation_ids': ['op_cancel'], 'task_ids': ['task_verified']})

    def test_successful_prepare_node_may_contain_a_completed_operation(self):
        # This is an old task result read via task_status, not a fresh write call.
        item = task_record(node_tool='prepare_mandate_cancel')
        self.assertIsNotNone(payload(rejected([item]))[0])

    def test_wrong_node_tool_cannot_supply_operation(self):
        for tool in ('forecast', 'prepare_transfer', 'future_tool'):
            with self.subTest(tool=tool):
                self.assert_denied([task_record(node_tool=tool)])

    def test_failed_or_pending_task_node_not_success_evidence(self):
        for state in ('FAILED', 'PENDING', 'WAITING_CONFIRMATION'):
            with self.subTest(state=state):
                self.assert_denied([task_record(node_state=state)])

    def test_task_id_alone_not_enough(self):
        item = task_record(); item['response']['data']['task']['nodes'] = []
        self.assert_denied([item])

    def test_task_args_are_not_recursively_scanned(self):
        item = task_record(); node = item['response']['data']['task']['nodes'][0]
        node['args'] = copy.deepcopy(node['result']); node['result'] = {}
        self.assert_denied([item])

    def test_explicit_foreign_task_or_node_scope_rejected(self):
        item = task_record(); item['response']['data']['task']['scope'] = 'moneydata'
        self.assert_denied([item])
        item = task_record(); item['response']['data']['task']['nodes'][0]['result']['scope'] = 'moneydata'
        self.assert_denied([item])

    def test_node_operation_identity_mismatch_rejected(self):
        item = task_record(); item['response']['data']['task']['nodes'][0]['operation_id'] = 'op_other'
        self.assert_denied([item])

    def test_actual_task_id_required_not_validation_task_id(self):
        item = task_record(); item['response']['data']['task'].pop('task_id')
        self.assert_denied([item])

    def test_write_request_even_failed_still_blocks(self):
        for name in ('prepare_transfer', 'submit_task_plan', 'replan_task', 'check_insights', 'confirm', 'future_tool'):
            with self.subTest(name=name):
                data, reason = payload(rejected([record(), {'kind': 'request', 'name': name}]))
                self.assertIsNone(data)
                self.assertEqual(reason, 'FIRST_ATTEMPT_HAS_MUTATION_OR_UNKNOWN_TOOL')

    def test_missing_tool_name_still_blocks(self):
        data, reason = payload(rejected([record(), {'kind': 'request'}]))
        self.assertIsNone(data)
        self.assertEqual(reason, 'FIRST_ATTEMPT_HAS_MUTATION_OR_UNKNOWN_TOOL')

    def test_other_action_or_unknown_reason_not_broadened(self):
        for code in ('UNBACKED_OPERATION_PREPARATION', 'UNBACKED_TASK_COMPLETION',
                     'UNBACKED_ANALYSIS_COMPLETION', 'OPERATION_NOT_PENDING', 'UNBACKED_TASK_SAVE', 'FUTURE_REASON'):
            with self.subTest(code=code):
                data, reason = payload(rejected(codes=('UNBACKED_OPERATION_SUCCESS', code)))
                self.assertIsNone(data)
                self.assertEqual(reason, 'ACTION_OR_OTHER_REJECTION')

    def test_budget_boundary(self):
        self.assertEqual(payload(rejected(), 29)[1], 'INSUFFICIENT_TIME_BUDGET')
        self.assertIsNotNone(payload(rejected(), 30)[0])

    def test_numeric_only_keeps_original_no_receipt_requirement(self):
        data, reason = payload(rejected([], ('UNSUPPORTED_DISPLAYED_AMOUNT',)))
        self.assertEqual(reason, 'NUMERIC_EVIDENCE_REPAIR')
        self.assertIsNotNone(data)


class ReceiptRepairRunnerTests(unittest.TestCase):
    def initial(self):
        return {'ok': True, 'text': '核实代扣：查询成功，金额72.00元。撤销已成功。',
                'trace': [record()], 'session_id': 'old_session'}

    def runner(self, second):
        runner = DshRunner('.', '.', 'http://127.0.0.1:1', 'unused', timeout=210)
        runner._run_model = Mock(side_effect=[self.initial(), second])
        return runner

    def test_one_fresh_readonly_pass_can_omit_unproved_history(self):
        second = {'ok': True, 'text': '撤销已成功。', 'trace': [record()]}
        runner = self.runner(second)
        with patch('src.bank_v1_web.worker.time.monotonic', side_effect=[10, 60]):
            result = runner('请核对当前操作结果', 'original_session')
        self.assertTrue(result['ok'])
        args, kwargs = runner._run_model.call_args_list[1]
        self.assertIsNone(args[1])
        self.assertEqual(kwargs, {'readonly': True, 'data_scope': 'bank', 'timeout': 160})
        self.assertEqual(result['answer_repair']['reason'], 'READONLY_OPERATION_EVIDENCE_REPAIR')
        self.assertFalse(result['answer_repair']['initial_result']['ok'])
        data = json.loads(args[0].rsplit('\n', 1)[1])
        self.assertEqual(data['known_locators']['operation_ids'], ['op_cancel'])
        self.assertNotIn('op_query', data['known_locators']['operation_ids'])
        self.assertEqual(result['trace'], second['trace'])

    def test_repeated_unproven_query_and_amount_still_rejected(self):
        runner = self.runner(self.initial())
        result = runner('请核对当前操作结果', None)
        self.assertFalse(result['ok'])
        self.assertEqual(runner._run_model.call_count, 2)
        codes = {r['code'] for r in result['answer_validation']['reasons']}
        self.assertIn('UNBACKED_OPERATION_SUCCESS', codes)
        self.assertIn('UNSUPPORTED_DISPLAYED_AMOUNT', codes)

    def test_initial_receipt_not_merged_into_new_evidence(self):
        runner = self.runner({'ok': True, 'text': '撤销已成功。', 'trace': []})
        result = runner('核对结果', None)
        self.assertFalse(result['ok'])
        self.assertEqual(result['trace'], [])
        self.assertEqual(runner._run_model.call_count, 2)

    def test_new_wrong_kind_receipt_cannot_prove_cancel(self):
        second = {'ok': True, 'text': '撤销已成功。',
                  'trace': [record(operation('mandate_query', 'op_query'))]}
        self.assertFalse(self.runner(second)('核对结果', None)['ok'])

    def test_new_pending_operation_cannot_prove_success(self):
        op = operation(); op['status'] = 'PENDING'; op['receipt'] = None
        second = {'ok': True, 'text': '撤销已成功。', 'trace': [record(op)]}
        self.assertFalse(self.runner(second)('核对结果', None)['ok'])

    def test_new_real_receipts_can_support_both_steps(self):
        query = operation('mandate_query', 'op_query')
        query['receipt']['mandates'] = [{'amount_minor': 7200, 'status': 'ACTIVE'}]
        second = {'ok': True, 'text': '当时查询已成功，登记金额72.00元。撤销已成功。',
                  'trace': [record(query), record()]}
        self.assertTrue(self.runner(second)('核对结果', None)['ok'])

    def test_quota_failure_preserves_initial_and_no_third_call(self):
        runner = self.runner({'ok': False, 'error': {'code': 'AccountQuotaExceeded'}})
        result = runner('核对结果', None)
        self.assertFalse(result['ok'])
        self.assertIn('initial_result', result['answer_repair'])
        self.assertEqual(runner._run_model.call_count, 2)

    def test_process_failure_preserves_initial(self):
        runner = self.runner(OSError('do-not-leak-fixture-secret'))
        result = runner('核对结果', None)
        self.assertFalse(result['ok'])
        self.assertEqual(result['error']['code'], 'REPAIR_PROCESS_FAILED')
        self.assertNotIn('do-not-leak-fixture-secret', json.dumps(result))
        self.assertEqual(runner._run_model.call_count, 2)


if __name__ == '__main__':
    unittest.main()
