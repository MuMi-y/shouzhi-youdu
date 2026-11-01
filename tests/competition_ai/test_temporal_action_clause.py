"""Classify the action occurrence, not its list formatting or following objects."""
import itertools
import copy
import unittest
from src.bank_v1_web.bank_answer import execution_claim_spans, validate_bank_answer


def task_trace(action='撤销'):
    tool = {'转账': 'prepare_transfer', '查询': 'prepare_mandate_query', '撤销': 'prepare_mandate_cancel',
            '取消': 'prepare_mandate_cancel', '操作': 'prepare_mandate_cancel'}[action]
    plan = {'task_id': 'd' * 32, 'scope': 'bank', 'subject_id': 'U1', 'state': 'WAITING_CONFIRMATION', 'nodes': [
        {'id': 'c', 'tool': tool, 'state': 'WAITING_CONFIRMATION', 'operation_id': 'op_currentcancel', 'depends_on': [], 'args': {}},
        {'id': 'r', 'tool': 'operation', 'state': 'PENDING', 'depends_on': ['c'], 'args': {'operation_id': {'$ref': 'c.data.operation.operation_id'}}}]}
    return [{'kind': 'response', 'tool': 'task_status', 'ok': True, 'response': {'scope': 'bank', 'data': {'task': plan}}}]


def check(text, trace=None):
    spans = execution_claim_spans(text)
    action = next((x['action'] for x in spans if x['classification'] == 'TEMPORAL_CONDITION'), '撤销')
    return validate_bank_answer({'ok': True, 'text': text, 'trace': task_trace(action) if trace is None else trace}, '说明流程')


def operation(identifier, kind='mandate_cancel', state='SUCCEEDED'):
    return {'kind': 'response', 'tool': 'operation', 'ok': True, 'response': {'data': {'operation': {
        'operation_id': identifier, 'kind': kind, 'status': state,
        'receipt': {'receipt_id': 'r_' + identifier} if state == 'SUCCEEDED' else None}}}}


class TemporalActionClassificationTests(unittest.TestCase):
    def test_bare_action_time_word_matrix(self):
        for action, status, time_word in itertools.product(
                ['转账', '查询', '撤销', '取消', '操作'], ['成功', '完成', '办妥'], ['后', '之后', '以后']):
            text = action + status + time_word + '核对真实回执与授权状态'
            with self.subTest(text=text):
                spans = execution_claim_spans(text)
                self.assertEqual(1, len(spans))
                self.assertEqual('TEMPORAL_CONDITION', spans[0]['classification'])
                self.assertEqual(action + status, text[spans[0]['start']:spans[0]['end']])
                self.assertEqual(len(action + status + time_word), spans[0]['condition_end'])

    def test_titles_numbers_and_dividers_do_not_change_classification(self):
        for prefix, suffix in itertools.product(
                ['', '4. 回执核对：', '1. 回执核对 —— ', '回执核对  ', '**回执核对** — '],
                ['自动核对真实回执与授权状态', '核实银行回执、状态和结果', '读取回执']):
            spans = execution_claim_spans(prefix + '撤销完成后' + suffix)
            self.assertEqual('TEMPORAL_CONDITION', spans[0]['classification'])

    def test_optional_execute_and_space_are_local(self):
        for text in ['撤销执行完成 后核对回执', '转账完成 之后核对回执', '操作执行办妥以后核对回执']:
            self.assertEqual('TEMPORAL_CONDITION', execution_claim_spans(text)[0]['classification'])

    def test_temporal_word_must_be_immediately_after_match(self):
        for text in ['撤销完成，后核对回执', '转账完成的后果', '操作完成后续处理', '查询完成后台处理', '取消成功后方记录']:
            with self.subTest(text=text): self.assertEqual('ASSERTED', execution_claim_spans(text)[0]['classification'])

    def test_present_markers_are_not_erased(self):
        for text in ['已撤销后核对回执', '撤销已完成后核对回执', '已将转账完成后核对回执',
                     '已经将这笔转账完成后核对回执', '现已把该项操作完成后核对回执', '代扣授权已被撤销后核对回执']:
            with self.subTest(text=text):
                self.assertTrue(execution_claim_spans(text))
                self.assertTrue(all(x['classification'] == 'ASSERTED' for x in execution_claim_spans(text)))

    def test_each_match_keeps_independent_classification(self):
        text = '撤销完成后核对回执，已撤销，已读取真实回执'
        self.assertEqual(['TEMPORAL_CONDITION', 'ASSERTED', 'ASSERTED'],
                         [x['classification'] for x in execution_claim_spans(text)])

    def test_later_success_still_rejected_after_success_time_condition(self):
        for text in ['撤销成功后核对回执，撤销已完成', '转账成功后核对回执：转账已成功',
                     '撤销完成后核对回执，已撤销', '查询办妥后读回执；撤销已成功']:
            with self.subTest(text=text): self.assertFalse(check(text)['ok'])

    def test_nonassertion_elsewhere_does_not_erase_later_positive(self):
        for text in ['撤销完成后不会自动转账：撤销已成功', '撤销成功后，若需要会核对回执：撤销已完成',
                     '如果撤销完成后核对回执：代扣授权已撤销']:
            with self.subTest(text=text): self.assertFalse(check(text)['ok'])

    def test_local_denial_or_question_not_success(self):
        for text in ['撤销完成后不能说已成功', '撤销成功后，不会说已成功', '撤销完成后核对是否已成功']:
            with self.subTest(text=text): self.assertTrue(check(text)['ok'])

    def test_numeric_check_does_not_use_temporal_exemption(self):
        for text in ['撤销完成后核对回执，余额999.99元', '转账成功后核对回执，涨幅91%']:
            result = check(text)
            self.assertFalse(result['ok'])
            self.assertTrue(result['answer_validation']['numeric_diagnostics'])


class BoundTemporalAnswerTests(unittest.TestCase):
    def test_actual_task_accepts_title_and_object_variations(self):
        for prefix, suffix in itertools.product(
                ['', '4. 回执核对：', '1. 回执核对 —— ', '回执核对  ', '**回执核对** — '],
                ['自动核对真实回执与授权状态', '核实银行回执、状态和结果', '读取回执']):
            with self.subTest(prefix=prefix, suffix=suffix): self.assertTrue(check(prefix + '撤销完成后' + suffix)['ok'])

    def test_each_action_time_family_with_matching_task(self):
        for action, status, time_word in itertools.product(
                ['转账', '查询', '撤销', '取消', '操作'], ['成功', '完成', '办妥'], ['后', '之后', '以后']):
            with self.subTest(action=action, status=status, time=time_word):
                self.assertTrue(check(action + status + time_word + '自动核对真实回执与状态')['ok'])

    def test_syntax_alone_does_not_expand_answer_permission(self):
        text = '回执核对 —— 撤销完成后自动核对真实回执与授权状态'
        self.assertEqual('TEMPORAL_CONDITION', execution_claim_spans(text)[0]['classification'])
        self.assertFalse(check(text, [])['ok'])

    def test_paused_or_terminal_task_not_automatic_progress(self):
        for state in ['PAUSED', 'NEEDS_HUMAN', 'FAILED', 'CANCELLED', 'SUCCEEDED']:
            trace = task_trace(); trace[0]['response']['data']['task']['state'] = state
            self.assertFalse(check('撤销完成后自动核对回执与状态', trace)['ok'])

    def test_wrong_ref_or_dependency_kind_not_bound(self):
        for mutation in ['ref', 'kind', 'node_state', 'dependency_state']:
            trace = task_trace(); nodes = trace[0]['response']['data']['task']['nodes']
            if mutation == 'ref': nodes[1]['args']['operation_id'] = {'$ref': 'other.data.operation.operation_id'}
            if mutation == 'kind': nodes[0]['tool'] = 'prepare_transfer'
            if mutation == 'node_state': nodes[1]['state'] = 'SUCCEEDED'
            if mutation == 'dependency_state': nodes[0]['state'] = 'SUCCEEDED'
            self.assertFalse(check('撤销完成后自动核对回执与状态', trace)['ok'])

    def test_latest_failure_invalidates_current_task(self):
        trace = task_trace(); trace.append({'kind': 'response', 'tool': 'task_status', 'ok': False,
                                           'args': {'task_id': 'd' * 32}, 'response': {}})
        self.assertFalse(check('撤销完成后核对回执与状态', trace)['ok'])

    def test_ambiguous_nodes_or_tasks_not_bound(self):
        trace = task_trace(); extra = copy.deepcopy(trace[0]['response']['data']['task']['nodes'][1]); extra['id'] = 'r2'
        trace[0]['response']['data']['task']['nodes'].append(extra)
        self.assertFalse(check('撤销完成后核对回执与状态', trace)['ok'])
        trace = task_trace(); extra = copy.deepcopy(trace[0]); extra['response']['data']['task']['task_id'] = 'e' * 32
        self.assertFalse(check('撤销完成后核对回执与状态', trace + [extra])['ok'])

    def test_readonly_operation_result_alone_cannot_prove_workflow(self):
        self.assertFalse(check('撤销完成后核对回执与状态', [operation('op_oldquery', 'mandate_query')])['ok'])

    def test_same_kind_old_receipt_cannot_back_anonymous_tail(self):
        trace = task_trace() + [operation('op_oldcancel')]
        for suffix in ['，已成功', '：已撤销', '；已读取真实回执', '，已核对真实回执']:
            with self.subTest(suffix=suffix):
                self.assertFalse(check('回执核对 —— 撤销完成后自动核对回执与状态' + suffix, trace)['ok'])

    def test_uncreated_target_cannot_borrow_old_card(self):
        trace = task_trace(); trace[0]['response']['data']['task']['nodes'][0].pop('operation_id')
        trace.append(operation('op_oldcancel', state='PENDING'))
        self.assertFalse(check('撤销完成后核对回执与状态，已准备，请确认', trace)['ok'])

    def test_current_receipt_backs_anonymous_tail(self):
        trace = task_trace() + [operation('op_currentcancel')]
        self.assertTrue(check('撤销完成后核对回执与状态，已读取真实回执', trace)['ok'])

    def test_explicit_old_operation_can_be_reported_separately(self):
        trace = task_trace() + [operation('op_oldcancel')]
        self.assertTrue(check('撤销完成后核对回执与状态；另一个操作 op_oldcancel 已成功', trace)['ok'])
        self.assertFalse(check('撤销完成后核对回执与状态；另一个操作 op_oldcancel 已成功；已成功', trace)['ok'])

    def test_local_deny_then_actual_tail_not_swallowed(self):
        for value in ['撤销成功后不会说已成功：代扣授权已撤销', '撤销完成后若需要可以核对回执：撤销已完成']:
            self.assertFalse(check(value)['ok'])

    def test_current_markers_need_receipt_despite_word_after(self):
        for value in ['已将转账完成后核对回执', '撤销已完成后核对回执', '代扣授权已被撤销后核对回执']:
            self.assertFalse(check(value)['ok'])

    def test_each_actual_match_uses_its_own_kind(self):
        trace = task_trace() + [operation('op_currentcancel')]
        self.assertFalse(check('撤销完成后核对回执：转账已成功', trace)['ok'])
        trace.append(operation('op_transfer', 'transfer'))
        self.assertTrue(check('撤销完成后核对回执：转账已成功', trace)['ok'])

    def test_multiple_actual_matches_must_all_have_receipts(self):
        trace = task_trace() + [operation('op_transfer', 'transfer')]
        self.assertFalse(check('撤销完成后核对回执：转账已成功，代扣授权已撤销', trace)['ok'])


if __name__ == '__main__': unittest.main(verbosity=2)
