"""Explicit display references resolve identity; they never grant authority."""
import unittest
from src.bank_v1_web.bank_answer import validate_bank_answer

A = 'op_a1b2c3d4111122223333444455556666'
B = 'op_a1b2c3d47777888899990000aaaabbbb'
C = 'op_ffeeddcc111122223333444455556666'


def record(identifier=A, kind='transfer', status='PENDING', receipt=True, **extra):
    operation = {'operation_id': identifier, 'kind': kind, 'status': status}
    if status == 'SUCCEEDED' and receipt:
        operation['receipt'] = {'receipt_id': 'receipt_test', 'status': 'SUCCEEDED'}
    return {'kind': 'response', 'tool': 'operation', 'ok': True,
            'response': {'scope': 'bank', 'data': {'operation': operation}}, **extra}


def check(text, trace=None):
    return validate_bank_answer({'ok': True, 'text': text,
                                 'trace': [record()] if trace is None else trace}, '说明当前操作状态')


def future_trace(*records):
    task = {'task_id': 'a' * 32, 'scope': 'bank', 'subject_id': 'U1', 'state': 'WAITING_CONFIRMATION',
            'nodes': [{'id': 'c', 'tool': 'prepare_mandate_cancel', 'state': 'WAITING_CONFIRMATION',
                       'depends_on': [], 'args': {}, 'operation_id': C},
                      {'id': 'r', 'tool': 'operation', 'state': 'PENDING', 'depends_on': ['c'],
                       'args': {'operation_id': {'$ref': 'c.data.operation.operation_id'}}}]}
    return [{'kind': 'response', 'tool': 'task_status', 'ok': True,
             'response': {'scope': 'bank', 'data': {'task': task}}}, *records]


class OperationReferenceTests(unittest.TestCase):
    def test_unique_prefix_two_markers(self):
        for marker in ['…', '...']:
            with self.subTest(marker=marker):
                self.assertTrue(check('转账 op_a1b2c3d4' + marker + ' 已准备，等待你确认')['ok'])

    def test_unique_prefix_suffix(self):
        self.assertTrue(check('转账 op_a1b2c3d4…6666 已准备，等待你确认', [record(), record(B)])['ok'])

    def test_wrapping_and_punctuation(self):
        for token in ['`op_a1b2c3d4…`', '**op_a1b2c3d4…**', '（op_a1b2c3d4…）', 'op_a1b2c3d4...,']:
            with self.subTest(token=token): self.assertTrue(check('转账 ' + token + ' 已准备')['ok'])

    def test_exact_full_and_legacy_short(self):
        for identifier in [A, 'op_short']:
            with self.subTest(identifier=identifier):
                self.assertTrue(check('转账 ' + identifier + ' 已准备', [record(identifier)])['ok'])

    def test_prefix_without_marker_is_unknown(self):
        self.assertFalse(check('转账 op_a1b2c3d4 已准备')['ok'])

    def test_unknown_prefix(self):
        self.assertFalse(check('转账 op_abcdef01… 已准备')['ok'])

    def test_wrong_suffix(self):
        self.assertFalse(check('转账 op_a1b2c3d4…6667 已准备')['ok'])

    def test_ambiguous_prefix(self):
        self.assertFalse(check('转账 op_a1b2c3d4… 已准备', [record(), record(B)])['ok'])

    def test_uniqueness_precedes_kind_filter(self):
        self.assertFalse(check('转账 op_a1b2c3d4… 已准备', [record(), record(B, 'mandate_query')])['ok'])

    def test_uniqueness_precedes_status_filter(self):
        self.assertFalse(check('转账 op_a1b2c3d4… 已准备', [record(), record(B, status='EXPIRED')])['ok'])

    def test_wrong_kind_not_backed(self):
        self.assertFalse(check('代扣授权 op_a1b2c3d4… 已撤销', [record(status='SUCCEEDED')])['ok'])

    def test_success_needs_receipt(self):
        self.assertTrue(check('转账 op_a1b2c3d4… 已成功', [record(status='SUCCEEDED')])['ok'])
        self.assertFalse(check('转账 op_a1b2c3d4… 已成功', [record(status='SUCCEEDED', receipt=False)])['ok'])
        self.assertFalse(check('转账 op_a1b2c3d4… 已成功')['ok'])

    def test_wrong_pending_status(self):
        for state in ['FAILED', 'EXPIRED', 'REJECTED', 'UNKNOWN']:
            with self.subTest(state=state):
                self.assertFalse(check('转账 op_a1b2c3d4… 已准备，等待你确认', [record(status=state)])['ok'])

    def test_succeeded_cannot_be_pending(self):
        self.assertFalse(check('转账 op_a1b2c3d4… 等待你确认', [record(status='SUCCEEDED')])['ok'])

    def test_missing_or_unsuccessful_trace(self):
        self.assertFalse(check('转账 op_a1b2c3d4… 已准备', [])['ok'])
        self.assertFalse(check('转账 op_a1b2c3d4… 已准备', [record(ok=False)])['ok'])
        foreign = record(); foreign['response']['scope'] = 'moneydata'
        self.assertFalse(check('转账 op_a1b2c3d4… 已准备', [foreign])['ok'])

    def test_request_args_and_prompt_not_evidence(self):
        for item in [{'kind': 'request', 'tool': 'operation', 'args': {'operation_id': A}},
                     {'kind': 'response', 'ok': True, 'tool': 'operation', 'response': {'data': {'args': record()['response']['data']}}}]:
            with self.subTest(item=item): self.assertFalse(check('转账 op_a1b2c3d4… 已准备', [item])['ok'])

    def test_minimum_prefix(self):
        for length in range(1, 8):
            with self.subTest(length=length):
                self.assertFalse(check('转账 ' + A[:3 + length] + '… 已准备')['ok'])

    def test_invalid_markers(self):
        for marker in ['..', '....', '……', '….']:
            with self.subTest(marker=marker):
                self.assertFalse(check('转账 op_a1b2c3d4' + marker + ' 已准备')['ok'])

    def test_invalid_contiguous_tail_after_ellipsis(self):
        self.assertFalse(check('转账 op_a1b2c3d4…_bad 已准备，请确认')['ok'])

    def test_invalid_contiguous_tail_after_suffix(self):
        self.assertFalse(check('转账 op_a1b2c3d4…6666_extra 已准备，请确认')['ok'])

    def test_second_ellipsis_not_silently_ignored(self):
        for reference in ['op_a1b2c3d4…6666…bad', 'op_a1b2c3d4...6666...bad']:
            with self.subTest(reference=reference):
                self.assertFalse(check('转账 ' + reference + ' 已准备')['ok'])

    def test_must_omit_characters(self):
        for ref in [A + '…', A[:-4] + '…6666', A + '…6666']:
            with self.subTest(ref=ref): self.assertFalse(check('转账 ' + ref + ' 已准备')['ok'])

    def test_case_sensitive(self):
        self.assertFalse(check('转账 op_A1B2C3D4… 已准备')['ok'])

    def test_unresolved_reference_not_hidden_by_valid_reference(self):
        self.assertFalse(check('转账 ' + A + ' 及 op_deadbeef… 已准备')['ok'])

    def test_multiple_explicit_claims_have_their_own_evidence(self):
        trace = [record(), record(C, 'mandate_query')]
        self.assertTrue(check('转账 op_a1b2c3d4… 已准备。代扣查询卡 op_ffeeddcc… 已准备', trace)['ok'])
        self.assertFalse(check('转账 op_a1b2c3d4… 已准备。代扣查询卡 op_deadbeef… 已准备', trace)['ok'])

    def test_valid_reference_cannot_mask_ambiguous_reference(self):
        trace = [record(), record(B), record(C)]
        self.assertFalse(check('转账 ' + C + ' 及 op_a1b2c3d4… 已准备', trace)['ok'])

    def test_abbreviation_cannot_mask_unknown_full_id(self):
        self.assertFalse(check('转账 op_a1b2c3d4… 及 ' + C + ' 已准备')['ok'])
        self.assertTrue(check('转账 op_a1b2c3d4… 及 ' + C + ' 已准备', [record(), record(C)])['ok'])

    def test_mixed_full_id_also_requires_trusted_source(self):
        item = record()
        item['response']['data']['args'] = record(C)['response']['data']
        self.assertFalse(check('转账 op_a1b2c3d4… 及 ' + C + ' 已准备', [item])['ok'])

    def test_mixed_full_id_cannot_use_input_polluted_status(self):
        item = record(C)
        item['response']['data']['args'] = record(C, status='SUCCEEDED')['response']['data']
        self.assertFalse(check('转账 op_a1b2c3d4… 及 ' + C + ' 已成功', [record(), item])['ok'])

    def test_all_full_id_legacy_boundary_unchanged(self):
        self.assertTrue(check('转账 ' + A + ' 及 ' + C + ' 已准备')['ok'])

    def test_nested_real_task_operation_visible(self):
        task = {'task_id': 'b' * 32, 'scope': 'bank', 'subject_id': 'U1', 'state': 'WAITING_CONFIRMATION',
                'nodes': [{'tool': 'prepare_transfer', 'state': 'WAITING_CONFIRMATION', 'result': record()['response']}]}
        trace = [{'kind': 'response', 'tool': 'task_status', 'ok': True, 'response': {'data': {'task': task}}}]
        self.assertTrue(check('转账 op_a1b2c3d4… 已准备', trace)['ok'])

    def test_failed_unexecuted_nested_node_not_visible(self):
        for state in ['PENDING', 'RUNNING', 'FAILED', 'SKIPPED', 'UNKNOWN']:
            task = {'task_id': 'b' * 32, 'scope': 'bank', 'subject_id': 'U1',
                    'nodes': [{'tool': 'prepare_transfer', 'state': state, 'result': record()['response']}]}
            trace = [{'kind': 'response', 'tool': 'task_status', 'ok': True, 'response': {'data': {'task': task}}}]
            with self.subTest(state=state): self.assertFalse(check('转账 op_a1b2c3d4… 已准备', trace)['ok'])

    def test_nested_foreign_result_not_visible(self):
        result = record()['response']; result['scope'] = 'moneydata'
        task = {'task_id': 'b' * 32, 'scope': 'bank', 'subject_id': 'U1',
                'nodes': [{'tool': 'prepare_transfer', 'state': 'SUCCEEDED', 'result': result}]}
        trace = [{'kind': 'response', 'tool': 'task_status', 'ok': True, 'response': {'data': {'task': task}}}]
        self.assertFalse(check('转账 op_a1b2c3d4… 已准备', trace)['ok'])

    def test_echoed_inputs_and_plan_history_not_visible(self):
        for key in ['args', 'arguments', 'inputs', 'plan_history']:
            item = {'kind': 'response', 'tool': 'task_status', 'ok': True,
                    'response': {'data': {key: record()['response']['data']}}}
            with self.subTest(key=key): self.assertFalse(check('转账 op_a1b2c3d4… 已准备', [item])['ok'])

    def test_same_id_input_cannot_override_trusted_status(self):
        for key in ['args', 'inputs', 'plan_history']:
            item = record()
            item['response']['data'][key] = record(status='SUCCEEDED')['response']['data']
            with self.subTest(key=key):
                self.assertFalse(check('转账 op_a1b2c3d4… 已成功', [item])['ok'])
                self.assertTrue(check('转账 op_a1b2c3d4… 已准备，等待你确认', [item])['ok'])

    def test_same_id_input_cannot_override_trusted_kind(self):
        item = record()
        item['response']['data']['args'] = record(kind='mandate_cancel', status='SUCCEEDED')['response']['data']
        self.assertFalse(check('代扣授权 op_a1b2c3d4… 已撤销', [item])['ok'])
        self.assertTrue(check('转账 op_a1b2c3d4… 已准备', [item])['ok'])

    def test_same_id_input_cannot_inject_receipt(self):
        item = record(status='SUCCEEDED', receipt=False)
        item['response']['data']['inputs'] = record(status='SUCCEEDED')['response']['data']
        self.assertFalse(check('转账 op_a1b2c3d4… 已成功', [item])['ok'])

    def test_future_target_does_not_borrow_old_same_kind(self):
        trace = future_trace(record(A, 'mandate_cancel', 'SUCCEEDED'))
        self.assertFalse(check('撤销完成后自动核对回执，已撤销', trace)['ok'])
        self.assertTrue(check('撤销完成后自动核对回执，op_a1b2c3d4… 已撤销', trace)['ok'])
        self.assertFalse(check('撤销完成后自动核对回执，op_ffeeddcc… 已撤销', trace)['ok'])

    def test_future_recursion_cannot_disambiguate_by_scoping(self):
        trace = future_trace(record(A, 'mandate_cancel', 'SUCCEEDED'), record(B, 'mandate_query', 'SUCCEEDED'))
        self.assertFalse(check('4. 撤销完成后自动核对回执，op_a1b2c3d4… 已撤销', trace)['ok'])

    def test_explicit_old_id_does_not_prove_later_anonymous_claim(self):
        trace = future_trace(record(A, 'mandate_cancel', 'SUCCEEDED'))
        self.assertFalse(check('撤销完成后自动核对回执，op_a1b2c3d4… 已撤销；已撤销', trace)['ok'])

    def test_numeric_gate_unchanged(self):
        self.assertFalse(check('转账 op_a1b2c3d4… 已准备，金额 999.99 元')['ok'])


if __name__ == '__main__': unittest.main(verbosity=2)
