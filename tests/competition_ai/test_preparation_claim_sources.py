"""Finite prepared bank objects require their own current evidence."""
import unittest
from src.bank_v1_web import bank_answer as gate


def operation(kind='transfer', status='PENDING', ident='op_old'):
    op = {'operation_id': ident, 'kind': kind, 'status': status}
    if status == 'SUCCEEDED':
        op['receipt'] = {'receipt_id': 'receipt_old', 'kind': kind}
    return op


def check(text, *ops):
    trace = [{'kind': 'response', 'tool': 'operation', 'ok': True,
              'response': {'scope': 'bank', 'data': {'operation': op}}} for op in ops]
    return gate.validate_bank_answer({'ok': True, 'text': text, 'trace': trace}, '说明处理结果')


class PreparationClaimSourceTests(unittest.TestCase):
    def test_five_original_double_denials_require_source(self):
        for prefix in ('并不是', '没有', '尚未', '不', '未能'):
            self.assertFalse(check(prefix + '未建立任何转账确认卡')['ok'])

    def test_positive_bounded_objects_require_source(self):
        for action in ('建立', '生成', '准备'):
            for obj in ('转账确认卡', '代扣查询确认卡', '撤销操作', '银行操作'):
                self.assertFalse(check('已' + action + obj)['ok'])

    def test_actual_pending_and_completed_preparation(self):
        for status in ('PENDING', 'SUCCEEDED'):
            self.assertTrue(check('已建立转账确认卡', operation(status=status))['ok'])
            self.assertTrue(check('并不是未建立任何转账确认卡', operation(status=status))['ok'])

    def test_bad_state_or_kind_cannot_prove_preparation(self):
        for status in ('EXPIRED', 'REJECTED', 'FAILED', 'UNKNOWN'):
            self.assertFalse(check('已建立转账确认卡', operation(status=status))['ok'])
        self.assertFalse(check('已建立转账确认卡', operation(kind='mandate_query'))['ok'])

    def test_id_prefix_and_suffix_are_checked(self):
        for text in ('op_old 已建立转账确认卡', '已建立转账确认卡 op_old'):
            self.assertTrue(check(text, operation())['ok'])
        for text in ('op_unknown 已建立转账确认卡', '已建立转账确认卡 op_unknown'):
            self.assertFalse(check(text, operation())['ok'])

    def test_each_adjacent_claim_needs_its_own_kind(self):
        for sep in ('，', '；', '。', '但'):
            self.assertFalse(check('已建立转账确认卡' + sep + '已建立代扣查询确认卡', operation())['ok'])
            self.assertTrue(check('已建立转账确认卡' + sep + '已建立代扣查询确认卡',
                                  operation(), operation(kind='mandate_query', ident='op_query'))['ok'])

    def test_negative_clause_cannot_hide_positive(self):
        for text in ('没有生成待确认操作，已建立转账确认卡',
                     '未生成任何待确认操作；已建立转账确认卡',
                     '不能证明未生成待确认操作，但已建立转账确认卡'):
            self.assertFalse(check(text)['ok'])

    def test_prior_refusal_prevents_borrowing_old_same_kind_card(self):
        self.assertFalse(check('未生成任何转账待确认操作；已建立转账确认卡', operation())['ok'])
        self.assertTrue(check('未生成任何转账待确认操作；另一笔 op_old 已建立转账确认卡', operation())['ok'])

    def test_question_and_quoted_labels_are_not_claims(self):
        self.assertTrue(check('是否已建立转账确认卡？')['ok'])
        self.assertTrue(check('标签“已建立转账确认卡”仅为示例。')['ok'])

    def test_local_future_or_denial_does_not_hide_next_claim(self):
        self.assertTrue(check('如果已建立转账确认卡，请查询原任务。')['ok'])
        self.assertFalse(check('如果已建立转账确认卡；代扣授权已撤销')['ok'])
        self.assertFalse(check('没有已建立转账确认卡，但已建立代扣查询确认卡')['ok'])

    def test_completed_card_not_pending_for_confirmation(self):
        self.assertFalse(check('已建立转账确认卡，请确认', operation(status='SUCCEEDED'))['ok'])

    def test_task_creation_does_not_become_bank_card(self):
        result = check('任务已建立，但银行在建立待确认操作前已拒绝，未生成任何待确认操作。')
        codes = [item['code'] for item in result['answer_validation']['reasons']]
        self.assertIn('UNBACKED_TASK_SAVE', codes)
        self.assertNotIn('UNBACKED_OPERATION_PREPARATION', codes)

    def test_description_and_args_cannot_prove_preparation(self):
        trace = [{'kind': 'response', 'tool': 'accounts', 'ok': True,
                  'args': {'operation': operation()}, 'response': {'scope': 'bank',
                  'data': {'description': '已建立转账确认卡', 'args': {'operation': operation()}}}}]
        result = gate.validate_bank_answer({'ok': True, 'text': '已建立转账确认卡', 'trace': trace}, '')
        self.assertFalse(result['ok'])

    def test_preparation_does_not_prove_execution(self):
        self.assertFalse(check('已建立转账确认卡，转账已成功', operation())['ok'])

    def test_amount_validation_independent(self):
        result = check('已建立转账确认卡，余额888.88元', operation())
        self.assertIn('UNSUPPORTED_DISPLAYED_AMOUNT', [r['code'] for r in result['answer_validation']['reasons']])

    def test_conjunction_does_not_inherit_other_explicit_id(self):
        self.assertFalse(check('op_old 已建立转账确认卡但 op_bad 已建立转账确认卡',
                               operation(), operation(status='EXPIRED', ident='op_bad'))['ok'])

    def test_future_target_cannot_borrow_old_same_kind_card(self):
        from test_future_plan_evidence import task, response, text, CANCEL
        old = operation(kind='mandate_cancel')
        trace = [response(task()), {'kind': 'response', 'tool': 'operation', 'ok': True,
                 'response': {'scope': 'bank', 'data': {'operation': old}}}]
        answer = text(CANCEL + '，已建立撤销确认卡')
        result = gate.validate_bank_answer({'ok': True, 'text': answer, 'trace': trace}, '说明任务进度')
        self.assertFalse(result['ok'])


if __name__ == '__main__':
    unittest.main()
