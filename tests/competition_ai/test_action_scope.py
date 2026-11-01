"""Keep bank receipt claims, concepts and typed read-only node completion apart."""
import copy
import unittest
from src.bank_v1_web.bank_answer import validate_bank_answer


def operation(kind='mandate_cancel', status='SUCCEEDED', receipt=True):
    return {'operation_id': 'op_example', 'kind': kind, 'status': status,
            'receipt': {'receipt_id': 'receipt_example'} if receipt else None}


def evaluate(text, data=None):
    trace = [] if data is None else [{'kind': 'response', 'ok': True,
        'tool': 'task_status', 'response': {'scope': 'bank', 'data': data}}]
    return validate_bank_answer({'ok': True, 'text': text, 'trace': trace}, '解释当前结果')


def task_data(states=('SUCCEEDED', 'SUCCEEDED'), tool='forecast'):
    nodes = [{'id': str(i), 'tool': tool, 'state': state, 'operation_id': None,
              'result': {'data': {'as_of_date': '2026-10-12'}}}
             for i, state in enumerate(states)]
    nodes.append({'id': 'prepare', 'tool': 'prepare_transfer',
                  'state': 'WAITING_CONFIRMATION', 'operation_id': 'op_example',
                  'result': {'data': {'operation': operation('transfer', 'PENDING', False)}}})
    return {'task': {'task_id': 'a' * 32, 'state': 'WAITING_CONFIRMATION', 'nodes': nodes}}


class CancellationScopeTests(unittest.TestCase):
    def test_passive_authorization_cancel_with_receipt(self):
        self.assertTrue(evaluate('代扣授权已成功取消。', {'operation': operation()})['ok'])

    def test_passive_with_be_requires_receipt(self):
        self.assertFalse(evaluate('扣款授权已被正式取消。')['ok'])
        self.assertTrue(evaluate('扣款授权已被正式取消。', {'operation': operation()})['ok'])

    def test_active_cancel_with_receipt(self):
        self.assertTrue(evaluate('已成功取消银行代扣授权。', {'operation': operation()})['ok'])

    def test_query_receipt_cannot_prove_cancel(self):
        self.assertFalse(evaluate('代扣授权已成功取消。', {'operation': operation('mandate_query')})['ok'])

    def test_pending_cannot_prove_cancel(self):
        self.assertFalse(evaluate('代扣授权已成功取消。', {'operation': operation(status='PENDING', receipt=False)})['ok'])

    def test_success_without_receipt_cannot_prove_cancel(self):
        self.assertFalse(evaluate('代扣授权已成功取消。', {'operation': operation(receipt=False)})['ok'])

    def test_cancel_receipt_cannot_prove_merchant_service(self):
        self.assertFalse(evaluate('商户服务已成功取消。', {'operation': operation()})['ok'])

    def test_contract_termination_not_bank_authorization(self):
        self.assertFalse(evaluate('会员合同已终止。', {'operation': operation()})['ok'])

    def test_noun_phrase_is_not_a_cancel_success(self):
        self.assertTrue(evaluate('代扣授权取消属于银行授权管理功能。')['ok'])

    def test_service_cancel_noun_phrase_is_not_actual_success(self):
        self.assertTrue(evaluate('商户服务取消规则由商户提供。')['ok'])

    def test_amount_still_requires_fresh_numeric_evidence(self):
        result = evaluate('代扣授权（72.00元）已成功取消。', {'operation': operation()})
        self.assertFalse(result['ok'])
        self.assertIn('UNSUPPORTED_DISPLAYED_AMOUNT', [r['code'] for r in result['answer_validation']['reasons']])


class ReceiptConceptTests(unittest.TestCase):
    concept = '成功回执也只能证明代扣授权状态，不证明商户服务已取消'

    def test_complete_concept_without_tools(self):
        self.assertTrue(evaluate(self.concept)['ok'])

    def test_other_concept_spelling(self):
        self.assertTrue(evaluate('银行操作回执仅证明扣款授权状态，不能证明会员合同已终止')['ok'])

    def test_previous_actual_success_is_not_hidden(self):
        self.assertFalse(evaluate('代扣授权已成功取消，' + self.concept)['ok'])

    def test_following_actual_success_is_not_hidden(self):
        self.assertFalse(evaluate(self.concept + '，转账已成功')['ok'])

    def test_embedded_actual_success_not_concept(self):
        self.assertFalse(evaluate('已取得成功回执也只能证明代扣授权状态，不证明商户服务已取消')['ok'])

    def test_other_sentence_still_needs_receipt(self):
        self.assertFalse(evaluate(self.concept + '；代扣授权已成功取消')['ok'])


class LocalNonproofTests(unittest.TestCase):
    def test_proven_bank_cancel_with_merchant_nonproof(self):
        self.assertTrue(evaluate('银行代扣授权已撤销，不证明商户服务已取消。', {'operation': operation()})['ok'])

    def test_proven_bank_cancel_with_but_limitation(self):
        self.assertTrue(evaluate('撤销已成功，但不证明商户服务已取消。', {'operation': operation()})['ok'])

    def test_bank_success_still_needs_receipt(self):
        self.assertFalse(evaluate('银行代扣授权已撤销，不证明商户服务已取消。')['ok'])

    def test_cannot_prove_cannot_swallow_bank_success(self):
        self.assertFalse(evaluate('银行代扣授权已撤销，不能证明商户服务已取消。')['ok'])

    def test_cannot_prove_with_bank_receipt(self):
        self.assertTrue(evaluate('银行代扣授权已撤销，不能证明商户服务已取消。', {'operation': operation()})['ok'])

    def test_second_merchant_assertion_in_same_clause_rejected(self):
        self.assertFalse(evaluate('银行代扣授权已撤销，不证明商户服务已取消，商户服务已取消。', {'operation': operation()})['ok'])

    def test_positive_merchant_before_nonproof_rejected(self):
        self.assertFalse(evaluate('商户服务已取消，不证明会员合同已终止。', {'operation': operation()})['ok'])

    def test_extra_transfer_still_needs_its_receipt(self):
        self.assertFalse(evaluate('银行代扣授权已撤销，不证明商户服务已取消，转账已成功。', {'operation': operation()})['ok'])

    def test_nonproof_alone_no_action_claim(self):
        self.assertTrue(evaluate('不证明商户服务已取消。')['ok'])

    def test_local_negative_does_not_authorize_second_bank_action(self):
        self.assertFalse(evaluate('不能证明商户服务已取消，银行代扣授权已撤销。')['ok'])


class ReadonlyNodeCompletionTests(unittest.TestCase):
    claim = '1. 评估已完成（两个预测节点均已成功）'

    def test_exact_successful_nodes_with_pending_transfer(self):
        self.assertTrue(evaluate(self.claim, task_data())['ok'])

    def test_no_task_evidence_rejected(self):
        self.assertFalse(evaluate(self.claim)['ok'])

    def test_one_successful_node_not_two(self):
        self.assertFalse(evaluate(self.claim, task_data(('SUCCEEDED',)))['ok'])

    def test_failed_node_not_completed(self):
        self.assertFalse(evaluate(self.claim, task_data(('SUCCEEDED', 'FAILED')))['ok'])

    def test_pending_node_not_completed(self):
        self.assertFalse(evaluate(self.claim, task_data(('SUCCEEDED', 'PENDING')))['ok'])

    def test_tool_type_not_inferred_from_title_or_id(self):
        self.assertFalse(evaluate(self.claim, task_data(tool='accounts'))['ok'])

    def test_no_structured_result_is_not_success_evidence(self):
        data = task_data()
        data['task']['nodes'][0]['result'] = None
        self.assertFalse(evaluate(self.claim, data)['ok'])

    def test_operation_node_cannot_be_readonly(self):
        data = task_data()
        data['task']['nodes'][0]['operation_id'] = 'op_other'
        self.assertFalse(evaluate(self.claim, data)['ok'])

    def test_must_not_combine_different_tasks(self):
        one = task_data(('SUCCEEDED',))['task']
        other = copy.deepcopy(one)
        other['task_id'] = 'b' * 32
        self.assertFalse(evaluate(self.claim, {'tasks': [one, other]})['ok'])

    def test_analysis_does_not_prove_entire_task(self):
        self.assertFalse(evaluate(self.claim + '；任务全部完成', task_data())['ok'])

    def test_analysis_does_not_prove_transfer(self):
        self.assertFalse(evaluate(self.claim + '，转账已成功', task_data())['ok'])

    def test_bare_analysis_completion_not_exempt_with_pending_operation(self):
        self.assertFalse(evaluate('分析已成功', task_data())['ok'])

    def test_successful_transfer_not_proof_of_forecast(self):
        self.assertFalse(evaluate(self.claim, {'operation': operation('transfer')})['ok'])

    def test_named_other_readonly_family(self):
        self.assertTrue(evaluate('分析已完成（一个周期识别节点已成功）'.replace('节点已', '节点均已'), task_data(('SUCCEEDED',), 'recurring_candidates'))['ok'])


if __name__ == '__main__':
    unittest.main()
