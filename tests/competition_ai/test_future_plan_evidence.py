"""Finite future steps require the latest actual task and exact dependency refs."""
import copy
import unittest
from src.bank_v1_web.bank_answer import validate_bank_answer, remove_identified_future_steps

ID = 'a' * 32
RECEIPT = '4. 回执核对：撤销完成后自动读取真实回执，核实授权状态后向您报告'
MATCH = '2. 授权匹配：查询确认后自动匹配唯一生效代扣授权'
CANCEL = '3. 撤销该授权：匹配成功后准备撤销该授权，仍需您本人在页面确认'


def task():
    return {'task_id': ID, 'scope': 'bank', 'subject_id': 'U1', 'state': 'WAITING_CONFIRMATION', 'nodes': [
        {'id': 'q', 'tool': 'prepare_mandate_query', 'state': 'WAITING_CONFIRMATION', 'depends_on': [], 'args': {}},
        {'id': 'm', 'tool': 'match_mandate_from_query', 'state': 'PENDING', 'depends_on': ['q'],
         'args': {'query_operation_id': {'$ref': 'q.data.operation.operation_id'}}},
        {'id': 'c', 'tool': 'prepare_mandate_cancel', 'state': 'PENDING', 'depends_on': ['m'],
         'args': {'mandate_id': {'$ref': 'm.data.match.selected.mandate_id'}}},
        {'id': 'r', 'tool': 'operation', 'state': 'PENDING', 'depends_on': ['c'],
         'args': {'operation_id': {'$ref': 'c.data.operation.operation_id'}}}]}


def response(value=None, tool='task_status'):
    return {'kind': 'response', 'tool': tool, 'ok': True, 'response': {'scope': 'bank', 'data': {'task': value or task()}}}


def text(line=RECEIPT, header='当前进度：', identifier=ID):
    return '任务编号：' + identifier + '\n' + header + '\n' + line


def check(answer, trace=None):
    return validate_bank_answer({'ok': True, 'text': answer, 'trace': [response()] if trace is None else trace}, '说明任务进度')


class FuturePlanEvidenceTests(unittest.TestCase):
    def test_receipt_headers_are_not_proof_or_gate(self):
        for header in ['当前进度：', '后续步骤：', '任务流程：', '', '已保存任务，流程为：']:
            with self.subTest(header=header):
                self.assertTrue(check(text(header=header))['ok'])

    def test_all_new_categories_are_removed_only_by_evidence(self):
        for line in [RECEIPT, MATCH, CANCEL]:
            with self.subTest(line=line):
                answer = text(line)
                self.assertNotEqual(answer, remove_identified_future_steps(answer, {ID: task()}))
                self.assertEqual(answer, remove_identified_future_steps(answer, {}))
                self.assertTrue(check(answer)['ok'])

    def test_matching_wording_variants(self):
        for line in ['2. 待查询确认后自动匹配授权', '2. 匹配授权：查询完成后将自动匹配唯一授权']:
            with self.subTest(line=line):
                self.assertNotIn(line, remove_identified_future_steps(text(line), {ID: task()}))

    def test_cancel_wording_variants(self):
        for line in ['3. 匹配完成后将生成一张撤销确认卡，仍需本人确认',
                     '3. 准备撤销：匹配成功后准备撤销授权并再次等待您确认']:
            with self.subTest(line=line):
                self.assertNotIn(line, remove_identified_future_steps(text(line), {ID: task()}))
                self.assertTrue(check(text(line))['ok'])

    def test_receipt_types_must_match_dependency_prepare(self):
        for label, tool, state in [('查询', 'prepare_mandate_query', '授权'), ('转账', 'prepare_transfer', '转账'),
                                   ('撤销', 'prepare_mandate_cancel', '授权'), ('安排修改', 'propose_arrangement', '安排')]:
            plan = task(); plan['nodes'][2]['tool'] = tool
            line = f'4. 读取回执：{label}完成后将自动核对银行回执，核实{state}状态后向你报告'
            with self.subTest(label=label):
                self.assertTrue(check(text(line), [response(plan)])['ok'])
                self.assertNotIn(line, remove_identified_future_steps(text(line), {ID: plan}))

    def test_generic_operation_receipt_requires_actual_prepare(self):
        line = '4. 操作完成后自动读取真实回执'
        self.assertTrue(check(text(line))['ok'])
        plan = task(); plan['nodes'][2]['tool'] = 'accounts'
        self.assertEqual(text(line), remove_identified_future_steps(text(line), {ID: plan}))

    def test_generic_prepare_categories(self):
        for label, tool in [('查询代扣', 'prepare_mandate_query'), ('转账', 'prepare_transfer'), ('修改安排', 'propose_arrangement')]:
            plan = task(); plan['nodes'][2]['tool'] = tool
            line = f'3. 前序步骤完成后准备{label}并等待本人确认'
            with self.subTest(label=label):
                self.assertTrue(check(text(line), [response(plan)])['ok'])
                self.assertNotIn(line, remove_identified_future_steps(text(line), {ID: plan}))

    def test_no_id_uses_unique_current_task(self):
        answer = '当前进度：\n' + RECEIPT
        self.assertNotEqual(answer, remove_identified_future_steps(answer, {ID: task()}))
        self.assertTrue(check(answer)['ok'])

    def test_no_id_multiple_current_tasks_is_ambiguous(self):
        other = task(); other['task_id'] = 'b' * 32
        self.assertFalse(check('当前进度：\n' + RECEIPT, [response(), response(other)])['ok'])

    def test_renumbered_followup_list_is_not_internal_node_position(self):
        for number in ['1', '3', '9']:
            line = number + '. 撤销完成后核对回执，向您报告结果。'
            self.assertTrue(check('后续步骤：\n' + line)['ok'])

    def test_two_compatible_future_nodes_are_ambiguous(self):
        plan = task(); other = copy.deepcopy(plan['nodes'][3]); other['id'] = 'r2'; plan['nodes'].append(other)
        self.assertFalse(check(text(), [response(plan)])['ok'])

    def test_two_ids_not_one_identified_task(self):
        answer = text() + '\n其他任务：' + 'b' * 32
        self.assertFalse(check(answer)['ok'])

    def test_one_named_task_among_multiple(self):
        other = task(); other['task_id'] = 'b' * 32
        self.assertTrue(check(text(), [response(other), response()])['ok'])

    def test_wrong_id_rejects(self):
        self.assertFalse(check(text(identifier='b' * 32))['ok'])

    def test_tasks_inside_args_or_other_tools_not_evidence(self):
        variants = [response(tool='accounts'), response(tool='submit_task_plan')]
        arg = response(); arg['response']['data'] = {'args': {'task': task()}}; variants.append(arg)
        for item in variants:
            with self.subTest(item=item): self.assertFalse(check(text(), [item])['ok'])

    def test_latest_failed_lookup_invalidates(self):
        failed = {'kind': 'response', 'tool': 'task_status', 'ok': False, 'args': {'task_id': ID}, 'response': {}}
        self.assertFalse(check(text(), [response(), failed])['ok'])

    def test_latest_foreign_response_invalidates(self):
        foreign = response(); foreign['response']['scope'] = 'moneydata'
        self.assertFalse(check(text(), [response(), foreign])['ok'])

    def test_foreign_task_or_subject(self):
        for key, value in [('scope', 'moneydata'), ('subject_id', 'U2')]:
            plan = task(); plan[key] = value
            with self.subTest(key=key): self.assertFalse(check(text(), [response(plan)])['ok'])

    def test_paused_handoff_terminal_or_unknown_state(self):
        for state in ['PAUSED', 'NEEDS_HUMAN', 'FAILED', 'CANCELLED', 'SUCCEEDED', 'UNKNOWN']:
            plan = task(); plan['state'] = state
            with self.subTest(state=state): self.assertFalse(check(text(), [response(plan)])['ok'])

    def test_receipt_node_not_pending(self):
        for state in ['WAITING_CONFIRMATION', 'FAILED', 'RUNNING', 'SUCCEEDED']:
            plan = task(); plan['nodes'][3]['state'] = state
            with self.subTest(state=state): self.assertFalse(check(text(), [response(plan)])['ok'])

    def test_real_task_ready_state_before_first_pump(self):
        plan = task(); plan['state'] = 'READY'; plan['nodes'][0]['state'] = 'PENDING'
        self.assertTrue(check(text(), [response(plan)])['ok'])

    def test_node_states_are_not_task_states(self):
        for state in ['PENDING', 'RUNNING']:
            plan = task(); plan['state'] = state
            self.assertEqual(text(), remove_identified_future_steps(text(), {ID: plan}))

    def test_dependency_not_pending_or_no_dependency(self):
        for state in ['FAILED', 'CANCELLED', 'SUCCEEDED']:
            plan = task(); plan['nodes'][2]['state'] = state
            with self.subTest(state=state): self.assertFalse(check(text(), [response(plan)])['ok'])
        plan = task(); plan['nodes'][3]['depends_on'] = []
        self.assertFalse(check(text(), [response(plan)])['ok'])

    def test_dependency_reference_wrong_or_not_object(self):
        for value in [{'$ref': 'q.data.operation.operation_id'}, {'$ref': 'c.data.operation.receipt_id'},
                      '{$ref:c.data.operation.operation_id}', 'op_unrelated', {'$ref': 'c.data.operation.operation_id', 'extra': True}]:
            plan = task(); plan['nodes'][3]['args']['operation_id'] = value
            with self.subTest(value=value): self.assertFalse(check(text(), [response(plan)])['ok'])

    def test_match_and_cancel_must_reference_correct_dependency(self):
        for ordinal, line in [(2, MATCH), (3, CANCEL)]:
            plan = task(); plan['nodes'][ordinal - 1]['args'] = {}
            self.assertEqual(text(line), remove_identified_future_steps(text(line), {ID: plan}))

    def test_wrong_tool(self):
        plan = task(); plan['nodes'][3]['tool'] = 'accounts'
        self.assertFalse(check(text(), [response(plan)])['ok'])

    def test_wrong_operation_kind_or_reported_state_not_removed(self):
        for line in [RECEIPT.replace('撤销完成', '转账完成'), RECEIPT.replace('授权状态', '转账状态')]:
            self.assertEqual(text(line), remove_identified_future_steps(text(line), {ID: task()}))

    def test_adjacent_current_success_inside_numbered_line(self):
        for suffix in ['，代扣授权已撤销', '：已成功', '；转账已成功', '。代扣查询已完成', '，商户服务已取消']:
            with self.subTest(suffix=suffix): self.assertFalse(check(text(RECEIPT + suffix))['ok'])

    def test_adjacent_current_prepared_inside_numbered_line(self):
        for suffix in ['，查询卡已准备，请确认', '：已准备撤销，请确认', '，现在已准备转账，请确认']:
            with self.subTest(suffix=suffix): self.assertFalse(check(text(RECEIPT + suffix))['ok'])

    def test_other_lines_current_claims_remain(self):
        for line in ['代扣授权已撤销。', '查询卡已准备，请确认。', '所有节点全部完成。']:
            for answer in [line + '\n' + text(), text() + '\n' + line]:
                with self.subTest(answer=answer): self.assertFalse(check(answer)['ok'])

    def test_elliptical_cancel_success_cannot_borrow_successful_query(self):
        operation = {'kind': 'response', 'tool': 'operation', 'ok': True,
                     'response': {'data': {'operation': {'operation_id': 'op_q', 'kind': 'mandate_query',
                         'status': 'SUCCEEDED', 'receipt': {'receipt_id': 'r_q'}}}}}
        for suffix in ['，已成功', '：已撤销', '；操作已完成', '，当前已成功', '。已成功']:
            with self.subTest(suffix=suffix):
                self.assertFalse(check(text(RECEIPT + suffix), [response(), operation])['ok'])

    def test_elliptical_preparation_cannot_borrow_other_pending_operation(self):
        operation = {'kind': 'response', 'tool': 'operation', 'ok': True,
                     'response': {'data': {'operation': {'operation_id': 'op_q', 'kind': 'mandate_query', 'status': 'PENDING'}}}}
        for suffix in ['，已准备，请确认', '：等待您确认', '，现在已备好，请确认']:
            with self.subTest(suffix=suffix):
                self.assertFalse(check(text(RECEIPT + suffix), [response(), operation])['ok'])

    def test_explicit_different_operation_keeps_its_own_context(self):
        operation = {'kind': 'response', 'tool': 'operation', 'ok': True,
                     'response': {'data': {'operation': {'operation_id': 'op_q', 'kind': 'mandate_query', 'status': 'PENDING'}}}}
        self.assertTrue(check(text(RECEIPT + '，查询卡已准备，请确认'), [response(), operation])['ok'])

    def test_current_receipt_read_does_not_borrow_other_kind(self):
        operation = {'kind': 'response', 'tool': 'operation', 'ok': True,
                     'response': {'data': {'operation': {'operation_id': 'op_q', 'kind': 'mandate_query',
                         'status': 'SUCCEEDED', 'receipt': {'receipt_id': 'r_q'}}}}}
        for suffix in ['，已读取回执', '：已核对真实回执', '；已读取银行回执']:
            with self.subTest(suffix=suffix):
                self.assertFalse(check(text(RECEIPT + suffix), [response(), operation])['ok'])

    def test_current_receipt_read_with_correct_receipt_is_backed(self):
        operation = {'kind': 'response', 'tool': 'operation', 'ok': True,
                     'response': {'data': {'operation': {'operation_id': 'op_q', 'kind': 'mandate_query',
                         'status': 'SUCCEEDED', 'receipt': {'receipt_id': 'r_q'}}}}}
        self.assertTrue(check('代扣查询已读取真实回执。', [operation])['ok'])
        self.assertFalse(check('撤销操作已读取真实回执。', [operation])['ok'])

    def test_other_same_kind_success_not_current_target(self):
        plan = task(); plan['nodes'][2].update(state='WAITING_CONFIRMATION', operation_id='op_currentcancel')
        old = {'kind': 'response', 'tool': 'operation', 'ok': True,
               'response': {'data': {'operation': {'operation_id': 'op_oldcancel', 'kind': 'mandate_cancel',
                   'status': 'SUCCEEDED', 'receipt': {'receipt_id': 'r_old'}}}}}
        for suffix in ['，已成功', '：已撤销', '；已读取回执', '，已核对真实回执']:
            with self.subTest(suffix=suffix):
                self.assertFalse(check(text(RECEIPT + suffix), [response(plan), old])['ok'])

    def test_uncreated_target_cannot_borrow_any_same_kind_operation(self):
        old = {'kind': 'response', 'tool': 'operation', 'ok': True,
               'response': {'data': {'operation': {'operation_id': 'op_oldcancel', 'kind': 'mandate_cancel',
                   'status': 'SUCCEEDED', 'receipt': {'receipt_id': 'r_old'}}}}}
        self.assertFalse(check(text(RECEIPT + '，已成功'), [response(), old])['ok'])

    def test_same_target_receipt_can_back_current_tail(self):
        plan = task(); plan['nodes'][2].update(state='WAITING_CONFIRMATION', operation_id='op_currentcancel')
        current = {'kind': 'response', 'tool': 'operation', 'ok': True,
                   'response': {'data': {'operation': {'operation_id': 'op_currentcancel', 'kind': 'mandate_cancel',
                       'status': 'SUCCEEDED', 'receipt': {'receipt_id': 'r_current'}}}}}
        self.assertTrue(check(text(RECEIPT + '，已读取真实回执'), [response(plan), current])['ok'])

    def test_old_same_kind_pending_not_current_preparation(self):
        plan = task(); plan['nodes'][2].update(state='WAITING_CONFIRMATION', operation_id='op_currentcancel')
        old = {'kind': 'response', 'tool': 'operation', 'ok': True,
               'response': {'data': {'operation': {'operation_id': 'op_oldcancel', 'kind': 'mandate_cancel', 'status': 'PENDING'}}}}
        self.assertFalse(check(text(RECEIPT + '，已准备，请确认'), [response(plan), old])['ok'])

    def test_same_target_pending_can_back_current_preparation(self):
        plan = task(); plan['nodes'][2].update(state='WAITING_CONFIRMATION', operation_id='op_currentcancel')
        current = {'kind': 'response', 'tool': 'operation', 'ok': True,
                   'response': {'data': {'operation': {'operation_id': 'op_currentcancel', 'kind': 'mandate_cancel', 'status': 'PENDING'}}}}
        self.assertTrue(check(text(RECEIPT + '，已准备，请确认'), [response(plan), current])['ok'])

    def test_explicit_old_operation_is_a_different_record(self):
        old = {'kind': 'response', 'tool': 'operation', 'ok': True,
               'response': {'data': {'operation': {'operation_id': 'op_oldcancel', 'kind': 'mandate_cancel',
                   'status': 'SUCCEEDED', 'receipt': {'receipt_id': 'r_old'}}}}}
        self.assertTrue(check(text(RECEIPT + '；另一个操作 op_oldcancel 已成功'), [response(), old])['ok'])
        self.assertFalse(check(text(RECEIPT + '；另一个操作 op_oldcancel 已成功；已成功'), [response(), old])['ok'])

    def test_current_assertion_before_future_span_not_removed(self):
        for prefix in ['4. 代扣授权已撤销，', '4. 查询卡已准备，']:
            answer = text(prefix + RECEIPT.split('：', 1)[1])
            self.assertEqual(answer, remove_identified_future_steps(answer, {ID: task()}))
            self.assertFalse(check(answer)['ok'])

    def test_un_numbered_future_receipt_does_not_gain_exemption(self):
        answer = text(RECEIPT[3:])
        self.assertEqual(answer, remove_identified_future_steps(answer, {ID: task()}))

    def test_first_actual_pending_card_still_required(self):
        answer = text() + '\n第一步查询已准备，等待你确认。'
        self.assertFalse(check(answer)['ok'])
        operation = {'kind': 'response', 'tool': 'prepare_mandate_query', 'ok': True,
                     'response': {'data': {'operation': {'operation_id': 'op_q', 'kind': 'mandate_query', 'status': 'PENDING'}}}}
        self.assertTrue(check(answer, [response(), operation])['ok'])

    def test_amount_and_ratio_in_any_future_or_current_tail_still_checked(self):
        for suffix in ['，余额9999.99元', '，涨幅91%']:
            result = check(text(RECEIPT + suffix))
            self.assertFalse(result['ok'])
            self.assertTrue(result['answer_validation']['numeric_diagnostics'])

    def test_arbitrary_title_or_generic_condition_not_recognized(self):
        for line in [RECEIPT.replace('回执核对：', '已撤销：'), RECEIPT.replace('撤销完成后', '如你所见，代扣授权已撤销，'),
                     RECEIPT.replace('自动读取', '已读取')]:
            self.assertEqual(text(line), remove_identified_future_steps(text(line), {ID: task()}))
            self.assertFalse(check(text(line))['ok'])


if __name__ == '__main__': unittest.main(verbosity=2)
