"""The bounded modifier does not suppress adjacent bank assertions."""
import unittest
from src.bank_v1_web import bank_answer as gate


def check(text, kind='transfer', status=None, ident='op_old'):
    trace = []
    if status:
        operation = {'operation_id': ident, 'kind': kind, 'status': status}
        if status == 'SUCCEEDED':
            operation['receipt'] = {'receipt_id': 'receipt_old', 'kind': kind}
        trace = [{'kind': 'response', 'tool': 'operation', 'ok': True,
                  'response': {'scope': 'bank', 'data': {'operation': operation}}}]
    return gate.validate_bank_answer({'ok': True, 'text': text, 'trace': trace}, '说明处理结果')


class NegatedAnyPreparationTests(unittest.TestCase):
    def test_finite_actions_and_objects(self):
        for action in ('生成', '建立', '准备'):
            for obj in ('待确认操作', '确认卡', '确认卡片', '转账待确认操作', '银行操作', '代扣查询请求', '撤销操作'):
                with self.subTest(action=action, obj=obj):
                    text = '未' + action + '任何' + obj
                    candidates = gate.negated_preparation_candidates(text)
                    self.assertEqual(len(candidates), 1)
                    self.assertEqual(candidates[0][1], 'DENIED')
                    self.assertTrue(check(text)['ok'])

    def test_span_contract_and_offsets(self):
        text = '说明：未 建立 任何 转账确认卡；后文'
        span = gate.preparation_refusal_spans(text)[0]
        self.assertEqual(set(span), {'start', 'end', 'text', 'action', 'object', 'before', 'kind'})
        self.assertEqual(text[span['start']:span['end']], '未 建立 任何 转账确认卡')
        self.assertEqual(span['object'], '转账确认卡')
        self.assertEqual(span['kind'], 'transfer')
        self.assertIsNone(span['before'])

    def test_no_modifier_remains_compatible(self):
        self.assertTrue(check('未生成待确认操作')['ok'])
        self.assertEqual(gate.negated_preparation_candidates('未生成待确认操作')[0][0]['object'], '待确认操作')

    def test_double_negation_is_not_exempt(self):
        for prefix in ('不是', '并非', '并不是', '未能'):
            with self.subTest(prefix=prefix):
                text = prefix + '未生成任何待确认操作'
                self.assertEqual(gate.negated_preparation_candidates(text)[0][1], 'DOUBLE_NEGATION')
                self.assertFalse(check(text)['ok'])

    def test_condition_and_cognitive_nonassertions(self):
        for prefix in ('如果', '若', '不能证明', '不代表'):
            with self.subTest(prefix=prefix):
                text = prefix + '未生成任何待确认操作，请查询原任务。'
                self.assertEqual(gate.negated_preparation_candidates(text)[0][1], 'NONASSERTION')
                self.assertTrue(check(text)['ok'])

    def test_adjacent_positive_claims_still_require_evidence(self):
        for sep in ('，', '；', '。', '\n'):
            for claim in ('转账已准备，请确认', '转账已成功', '代扣授权已撤销'):
                with self.subTest(sep=sep, claim=claim):
                    self.assertFalse(check('未生成任何待确认操作' + sep + claim)['ok'])
                    self.assertFalse(check(claim + sep + '未生成任何待确认操作')['ok'])

    def test_condition_cannot_hide_adjacent_success(self):
        for prefix in ('如果', '不能证明'):
            self.assertFalse(check(prefix + '未生成任何待确认操作，转账已成功')['ok'])
            self.assertFalse(check(prefix + '未生成任何待确认操作，已准备，请确认')['ok'])

    def test_old_pending_cannot_prove_anonymous_new_card(self):
        for sep in ('，', '；', '：'):
            self.assertFalse(check('未生成任何转账待确认操作' + sep + '已准备，请确认', status='PENDING')['ok'])

    def test_old_success_cannot_prove_anonymous_new_success(self):
        for sep in ('，', '；', '：'):
            self.assertFalse(check('未生成任何转账待确认操作' + sep + '已成功', status='SUCCEEDED')['ok'])

    def test_unknown_kind_cannot_borrow_old_receipt(self):
        for kind in ('transfer', 'mandate_query', 'mandate_cancel'):
            self.assertFalse(check('未生成任何待确认操作，已成功', kind=kind, status='SUCCEEDED')['ok'])

    def test_explicit_separate_id_can_use_its_actual_state(self):
        self.assertTrue(check('未生成任何转账待确认操作；另一笔转账 op_old 已准备，请确认', status='PENDING')['ok'])
        self.assertTrue(check('未生成任何转账待确认操作；另一笔转账 op_old 已成功', status='SUCCEEDED')['ok'])
        self.assertFalse(check('未生成任何转账待确认操作；另一笔转账 op_old 已准备，请确认', status='SUCCEEDED')['ok'])

    def test_unknown_id_remains_rejected(self):
        self.assertFalse(check('未生成任何转账待确认操作；转账 op_unknown 已成功', status='SUCCEEDED')['ok'])
        self.assertFalse(check('未生成任何转账 op_unknown 待确认操作', status='PENDING')['ok'])

    def test_unknown_object_and_extra_modifiers_not_accepted_as_spans(self):
        for text in ('未生成任何贷款待确认操作', '未生成任何报告待确认操作', '未生成所有待确认操作',
                     '未生成任何一种待确认操作', '未生成任何，待确认操作'):
            with self.subTest(text=text):
                self.assertEqual(gate.preparation_refusal_spans(text), [])
                self.assertFalse(check(text + '；转账已成功')['ok'])

    def test_numeric_validation_remains_independent(self):
        result = check('未生成任何待确认操作，余额999.99元')
        self.assertFalse(result['ok'])
        self.assertIn('UNSUPPORTED_DISPLAYED_AMOUNT', [r['code'] for r in result['answer_validation']['reasons']])

    def test_direct_conjunction_is_not_consumed(self):
        for suffix in ('且转账已成功', '但转账已准备，请确认'):
            self.assertFalse(check('未生成任何待确认操作' + suffix)['ok'])

    def test_adjacent_other_clause_does_not_negate_current_denial(self):
        self.assertTrue(check('不是另一项请求，未生成任何待确认操作')['ok'])


if __name__ == '__main__':
    unittest.main()
