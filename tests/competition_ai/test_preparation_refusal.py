"""A pre-preparation refusal is not a current confirmation card."""
import unittest
from src.bank_v1_web import bank_answer as gate


def validate(text, operations=()):
    trace = [{'kind': 'response', 'ok': True, 'tool': 'operation',
              'response': {'scope': 'bank', 'data': {'operation': item}}} for item in operations]
    return gate.validate_bank_answer({'ok': True, 'text': text, 'trace': trace}, '请说明实际结果')


def operation(kind='transfer', status='PENDING'):
    item = {'operation_id': 'op_evidence', 'kind': kind, 'status': status}
    if status == 'SUCCEEDED': item['receipt'] = {'receipt_id': 'receipt_test', 'kind': kind}
    return item


class PreparationRefusalTests(unittest.TestCase):
    def test_real_clause_without_unsupported_amounts(self):
        self.assertTrue(validate('- 准备转账节点：银行在建立待确认操作前拒绝，原因是付款账户余额不足')['ok'])

    def test_bounded_action_object_time_family(self):
        for action in ['建立', '生成', '创建', '准备']:
            for obj in ['待确认操作', '转账确认卡', '银行操作']:
                for before in ['前', '之前', '以前']:
                    with self.subTest(action=action, obj=obj, before=before):
                        self.assertTrue(validate('银行在' + action + obj + before + '被拒绝')['ok'])

    def test_bank_actor_and_refusal_adverbs(self):
        for tail in ['拒绝', '就拒绝', '即被拒绝', '已被银行拒绝', '已经被银行拒绝了']:
            with self.subTest(tail=tail):
                self.assertTrue(validate('在生成待确认操作之前' + tail)['ok'])

    def test_space_formatting(self):
        self.assertTrue(validate('银行在 生成 转账确认卡 之前 被银行拒绝')['ok'])

    def test_current_preparation_never_exempt(self):
        for text in ['转账已准备，请确认', '在建立待确认操作后拒绝',
                     '在已生成待确认操作之前被拒绝', '在生成待确认操作前已准备好，请确认',
                     '在生成待确认操作前拒绝成功', '在生成待确认操作前拒绝的请求现已生成待确认卡']:
            with self.subTest(text=text): self.assertFalse(validate(text)['ok'])

    def test_neighbouring_preparation_and_success_are_checked(self):
        prefix = '银行在建立待确认操作前拒绝'
        for separator in ['，', '：', '；', '。', '\n']:
            for claim in ['转账已准备，请确认', '转账已成功', '撤销已完成', '已成功', '已准备，请确认']:
                if separator in ['。', '\n'] and claim.startswith('已'):
                    # Unqualified claims in a new sentence are an existing
                    # detector boundary, not this local refusal contract.
                    continue
                with self.subTest(separator=separator, claim=claim):
                    self.assertFalse(validate(prefix + separator + claim)['ok'])

    def test_preceding_claim_is_checked(self):
        self.assertFalse(validate('转账已准备，银行在建立待确认操作前拒绝')['ok'])
        self.assertFalse(validate('转账已成功；银行在建立待确认操作前拒绝')['ok'])

    def test_explicit_kind_context_does_not_borrow_query_success(self):
        text = '银行在生成转账待确认操作前拒绝，已成功'
        self.assertFalse(validate(text, [operation('mandate_query', 'SUCCEEDED')])['ok'])

    def test_evidenced_actual_claim_stays_supported(self):
        text = '银行在建立待确认操作前拒绝；另一笔转账 op_evidence 已准备，请确认'
        self.assertTrue(validate(text, [operation()])['ok'])

    def test_explicit_new_id_after_local_refusal_is_supported(self):
        prefix = '银行在生成转账待确认操作前拒绝；'
        self.assertTrue(validate(prefix + '转账 op_evidence 已准备，请确认', [operation()])['ok'])
        self.assertTrue(validate(prefix + '转账 op_evidence 已成功', [operation(status='SUCCEEDED')])['ok'])
        self.assertFalse(validate(prefix + '转账 op_unknown 已成功', [operation(status='SUCCEEDED')])['ok'])

    def test_anonymous_cannot_borrow_same_kind_old_operation(self):
        prefix = '银行在生成转账待确认操作前拒绝，'
        self.assertFalse(validate(prefix + '已成功', [operation(status='SUCCEEDED')])['ok'])
        self.assertFalse(validate(prefix + '已准备，请确认', [operation()])['ok'])

    def test_generic_span_keeps_explicit_node_title_kind(self):
        prefix = '- 准备转账节点：银行在建立待确认操作前拒绝'
        for separator in ['，', '；', '：']:
            with self.subTest(separator=separator):
                self.assertFalse(validate(prefix + separator + '转账已成功', [operation(status='SUCCEEDED')])['ok'])
                self.assertFalse(validate(prefix + separator + '转账已准备，请确认', [operation()])['ok'])

    def test_generic_span_title_allows_separately_identified_operation(self):
        prefix = '- 准备转账节点：银行在建立待确认操作前拒绝；'
        self.assertTrue(validate(prefix + '转账 op_evidence 已成功', [operation(status='SUCCEEDED')])['ok'])
        self.assertTrue(validate(prefix + '转账 op_evidence 已准备，请确认', [operation()])['ok'])

    def test_unknown_refusal_kind_cannot_borrow_named_old_operation(self):
        prefix = '银行在建立待确认操作前拒绝'
        for separator in ['，', '；', '：']:
            for claim in ['转账已成功', '本次转账已成功', '转账已准备，请确认']:
                with self.subTest(separator=separator, claim=claim):
                    self.assertFalse(validate(prefix + separator + claim,
                        [operation(status='SUCCEEDED')])['ok'])

    def test_known_refusal_kind_does_not_block_different_kind_evidence(self):
        self.assertTrue(validate('银行在生成转账待确认操作前拒绝；代扣查询已成功',
            [operation('mandate_query', 'SUCCEEDED')])['ok'])

    def test_numerical_gate_still_reads_entire_answer(self):
        for tail in ['，余额987.65元', '，上涨37.25%']:
            with self.subTest(tail=tail):
                self.assertFalse(validate('银行在建立待确认操作前拒绝' + tail)['ok'])

    def test_parser_has_precise_offsets_and_no_cross_clause(self):
        text = '前缀：在生成转账确认卡之前被银行拒绝；尾部已准备'
        spans = gate.preparation_refusal_spans(text)
        self.assertEqual(len(spans), 1)
        self.assertEqual(text[spans[0]['start']:spans[0]['end']], spans[0]['text'])
        self.assertNotIn('已准备', spans[0]['text'])
        self.assertEqual(gate.preparation_refusal_spans('在生成待确认操作；前拒绝'), [])

    def test_no_generic_refusal_exemption(self):
        self.assertFalse(validate('拒绝了旧请求，现已准备好转账，请确认')['ok'])
        self.assertFalse(validate('转账已成功，但银行拒绝退款')['ok'])

    def test_denied_or_hypothetical_refusal_not_classified(self):
        for prefix in ['不', '未', '没有', '并非', '不是', '假设', '如果', '若']:
            with self.subTest(prefix=prefix):
                self.assertEqual(gate.preparation_refusal_spans(prefix + '在建立待确认操作前拒绝'), [])


if __name__ == '__main__': unittest.main(verbosity=2)
