"""Preparation-before-refusal cards share the existing finite object family."""
import unittest
from src.bank_v1_web import bank_answer as gate


def check(text, status=None, kind='transfer', ident='op_old'):
    records = []
    if status:
        op = {'operation_id': ident, 'kind': kind, 'status': status}
        if status == 'SUCCEEDED':
            op['receipt_id'] = 'receipt_old'
        records = [{'kind': 'response', 'tool': 'operation', 'ok': True,
                    'response': {'scope': 'bank', 'data': {'operation': op}}}]
    return gate.validate_bank_answer({'ok': True, 'text': text, 'trace': records}, '说明现状')


class PreparationRefusalCardTests(unittest.TestCase):
    def test_card_and_card_sheet_have_compatible_spans(self):
        for action in ('生成', '建立', '创建', '准备'):
            for obj in ('待确认卡', '待确认卡片', '转账待确认卡', '代扣查询待确认卡片', '待确认操作'):
                with self.subTest(action=action, obj=obj):
                    text = '在' + action + obj + '之前被银行拒绝'
                    span = gate.preparation_refusal_spans(text)[0]
                    self.assertEqual(span['object'], obj)
                    self.assertEqual(text[span['start']:span['end']], text)
                    self.assertEqual(set(span), {'start','end','text','action','object','before','kind'})
                    self.assertTrue(check(text)['ok'])

    def test_real_word_order_and_whitespace(self):
        for text in ('被银行在生成待确认卡之前拒绝，原因是余额不足。',
                     '银行在 建立 转账待 确认 卡片 前已拒绝。'):
            self.assertTrue(check(text)['ok'])

    def test_refusal_mask_only_removes_local_span(self):
        text = '在生成转账待确认卡之前拒绝，转账已成功'
        masked = gate.mask_preparation_refusals(text)
        self.assertIn('转账已成功', masked)
        self.assertNotIn('待确认卡', masked)

    def test_adjacent_actual_execution_still_needs_source(self):
        for sep in ('，', '；', '。', '\n'):
            for action in ('转账已成功', '代扣授权已撤销', '已核对真实回执'):
                self.assertFalse(check('在生成待确认卡之前拒绝' + sep + action)['ok'])
                self.assertFalse(check(action + sep + '在生成待确认卡之前拒绝')['ok'])

    def test_adjacent_prepared_or_pending_still_needs_source(self):
        for suffix in ('已建立转账确认卡', '转账已准备，请确认', '转账待确认'):
            self.assertFalse(check('在生成转账待确认卡之前拒绝；' + suffix)['ok'])

    def test_old_same_kind_cannot_back_uncreated_anonymous_action(self):
        for status, suffix in (('PENDING', '已准备，请确认'), ('SUCCEEDED', '已成功')):
            self.assertFalse(check('在生成转账待确认卡之前拒绝；' + suffix, status)['ok'])

    def test_unknown_kind_refusal_does_not_borrow_old_receipt(self):
        for kind in ('transfer', 'mandate_query', 'mandate_cancel'):
            self.assertFalse(check('在生成待确认卡之前拒绝；已成功', 'SUCCEEDED', kind)['ok'])

    def test_explicit_separate_id_can_use_its_actual_state(self):
        self.assertTrue(check('在生成转账待确认卡之前拒绝；另一笔转账 op_old 已准备，请确认', 'PENDING')['ok'])
        self.assertTrue(check('在生成转账待确认卡之前拒绝；另一笔转账 op_old 已成功', 'SUCCEEDED')['ok'])
        self.assertFalse(check('在生成转账待确认卡之前拒绝；另一笔转账 op_unknown 已成功', 'SUCCEEDED')['ok'])

    def test_completed_card_is_not_pending(self):
        self.assertFalse(check('在生成待确认卡之前拒绝；转账 op_old 请确认', 'SUCCEEDED')['ok'])

    def test_negated_refusal_has_no_exemption(self):
        for prefix in ('不', '未', '没有', '并非', '不是'):
            text = prefix + '在生成转账待确认卡之前被银行拒绝'
            self.assertEqual(gate.preparation_refusal_spans(text), [])
            self.assertFalse(check(text + '；转账已成功')['ok'])

    def test_double_negation_remains_source_checked(self):
        for prefix in ('不是', '并非', '没有', '尚未', '不', '未能'):
            self.assertFalse(check(prefix + '未生成任何转账待确认卡')['ok'])

    def test_unknown_objects_and_embedded_ids_not_masked(self):
        for obj in ('贷款待确认卡', '报告待确认卡片', '转账 op_unknown 待确认卡', '待确认卡交易'):
            text = '在生成' + obj + '之前被银行拒绝'
            self.assertEqual(gate.preparation_refusal_spans(text), [])
            self.assertFalse(check(text + '；转账已成功')['ok'])

    def test_no_cross_clause_object_match(self):
        text = '在生成，待确认卡之前被银行拒绝'
        self.assertEqual(gate.preparation_refusal_spans(text), [])
        self.assertFalse(check(text + '；转账已成功')['ok'])

    def test_numeric_gate_not_changed(self):
        result = check('在生成待确认卡之前被银行拒绝；余额812.34元')
        self.assertIn('UNSUPPORTED_DISPLAYED_AMOUNT', [item['code'] for item in result['answer_validation']['reasons']])


if __name__ == '__main__':
    unittest.main()
