"""Local denied preparation never hides a separate bank state claim."""
import unittest
from src.bank_v1_web import bank_answer as gate


def operation(kind='transfer', status='PENDING', ident='op_old'):
    result = {'operation_id': ident, 'kind': kind, 'status': status}
    if status == 'SUCCEEDED':
        result['receipt'] = {'receipt_id':'receipt_old','kind':kind}
    return result


def check(text, operations=(), prompt='请说明处理结果'):
    trace = [{'kind':'response','tool':'operation','ok':True,
              'response':{'scope':'bank','data':{'operation':op}}} for op in operations]
    return gate.validate_bank_answer({'ok':True,'text':text,'trace':trace}, prompt)


class NegatedPreparationTests(unittest.TestCase):
    def test_observed_heading(self):
        self.assertTrue(check('节点：准备转账——失败，未生成待确认操作')['ok'])

    def test_local_families(self):
        for action in ['生成','建立','准备']:
            for obj in ['待确认操作','转账待确认操作','银行操作','转账确认卡','代扣查询确认卡片','撤销操作']:
                with self.subTest(action=action,obj=obj):
                    text='未'+action+obj
                    self.assertEqual(len(gate.preparation_refusal_spans(text)),1)
                    self.assertTrue(check(text)['ok'])

    def test_span_offsets_and_compatible_keys(self):
        text='节点：未 生成 转账确认卡；另有信息'
        spans=gate.preparation_refusal_spans(text)
        self.assertEqual(len(spans),1)
        self.assertEqual(text[spans[0]['start']:spans[0]['end']],spans[0]['text'])
        self.assertEqual(set(spans[0]),{'start','end','text','action','object','before','kind'})
        self.assertIsNone(spans[0]['before'])
        self.assertEqual(spans[0]['kind'],'transfer')

    def test_old_time_refusal_stays_compatible(self):
        spans=gate.preparation_refusal_spans('在生成转账确认卡之前被银行拒绝')
        self.assertEqual(len(spans),1)
        self.assertEqual(spans[0]['before'],'之前')

    def test_positive_preceding_claim_without_evidence(self):
        for claim in ['转账已准备，请确认','转账已成功','撤销已成功']:
            self.assertFalse(check(claim+'；未生成待确认操作')['ok'])

    def test_positive_following_claim_without_evidence(self):
        for separator in ['，','；','：','。','\n']:
            for claim in ['转账已准备，请确认','转账已成功','撤销已成功']:
                with self.subTest(separator=separator,claim=claim):
                    self.assertFalse(check('未生成待确认操作'+separator+claim)['ok'])

    def test_old_pending_cannot_support_anonymous_new_card(self):
        for separator in ['，','；','：']:
            self.assertFalse(check('未生成转账待确认操作'+separator+'已准备，请确认',[operation()])['ok'])

    def test_old_success_cannot_support_anonymous_current_success(self):
        for separator in ['，','；','：']:
            self.assertFalse(check('未生成转账待确认操作'+separator+'已成功',[operation(status='SUCCEEDED')])['ok'])

    def test_unknown_kind_does_not_borrow_old_success(self):
        for kind in ['transfer','mandate_query','mandate_cancel']:
            self.assertFalse(check('未生成待确认操作，已成功',[operation(kind,status='SUCCEEDED')])['ok'])

    def test_explicit_separate_pending_id_is_allowed(self):
        self.assertTrue(check('未生成转账待确认操作；另一笔转账 op_old 已准备，请确认',[operation()])['ok'])

    def test_explicit_separate_success_id_is_allowed(self):
        self.assertTrue(check('未生成转账待确认操作；另一笔转账 op_old 已成功',[operation(status='SUCCEEDED')])['ok'])

    def test_explicit_unknown_id_stays_rejected(self):
        self.assertFalse(check('未生成转账待确认操作；转账 op_unknown 已成功',[operation(status='SUCCEEDED')])['ok'])

    def test_other_kind_success_requires_own_evidence(self):
        text='未生成转账待确认操作；代扣查询已成功'
        self.assertTrue(check(text,[operation('mandate_query','SUCCEEDED')])['ok'])
        self.assertFalse(check(text,[operation('transfer','SUCCEEDED')])['ok'])

    def test_success_does_not_make_current_card_pending(self):
        text='未生成待确认操作；转账 op_old 已准备，请确认'
        self.assertFalse(check(text,[operation(status='SUCCEEDED')])['ok'])

    def test_no_negation_of_negation_exemption(self):
        for prefix in ['不是','并非','并不是','并没有','并未','尚未','未能']:
            with self.subTest(prefix=prefix):
                text=prefix+'未生成待确认操作'
                self.assertEqual(gate.preparation_refusal_spans(text),[])
                self.assertFalse(check(text)['ok'])

    def test_adjacent_preceding_negative_is_not_scope(self):
        self.assertTrue(check('不是另一项请求，未生成待确认操作')['ok'])

    def test_unknown_object_not_masked(self):
        for obj in ['贷款待确认操作','商户订单待确认操作','报告待确认操作','神秘确认卡']:
            with self.subTest(obj=obj):
                self.assertEqual(gate.preparation_refusal_spans('未生成'+obj),[])
                self.assertFalse(check('未生成'+obj+'；转账已成功')['ok'])

    def test_object_boundary_cannot_consume_following_claim(self):
        self.assertFalse(check('未生成转账确认卡，已准备，请确认')['ok'])
        self.assertFalse(check('未生成转账确认卡；撤销已成功')['ok'])

    def test_numeric_gate_stays_independent(self):
        result=check('未生成待确认操作，余额999.99元')
        self.assertFalse(result['ok'])
        self.assertIn('UNSUPPORTED_DISPLAYED_AMOUNT',[r['code'] for r in result['answer_validation']['reasons']])

    def test_user_hypothesis_not_new_amount(self):
        self.assertTrue(check('拟转9000.00元的节点失败，未生成待确认操作',prompt='准备转9000元')['ok'])

    def test_directly_joined_positive_predicate_stays_checked(self):
        for suffix in ['且转账已成功','但转账已准备，请确认']:
            self.assertFalse(check('未生成待确认操作'+suffix)['ok'])

    def test_reference_inside_object_is_not_deleted(self):
        text='未生成转账 op_unknown 待确认操作'
        self.assertEqual(gate.preparation_refusal_spans(text),[])
        self.assertFalse(check(text,[operation()])['ok'])

    def test_condition_is_not_preparation(self):
        self.assertTrue(check('如果未生成待确认操作，请稍后再试。')['ok'])

    def test_uncertainty_is_not_preparation(self):
        self.assertTrue(check('不能证明未生成待确认操作，请查询原任务。')['ok'])

    def test_classifier_has_three_explicit_states(self):
        for text,expected in [('未生成待确认操作','DENIED'),('并非未生成待确认操作','DOUBLE_NEGATION'),
                              ('如果未生成待确认操作','NONASSERTION'),('不能证明未生成待确认操作','NONASSERTION')]:
            with self.subTest(text=text):
                self.assertEqual(gate.negated_preparation_candidates(text)[0][1],expected)

    def test_nonassertion_prefix_removed_only_with_predicate(self):
        text='前文：不能证明未生成待确认操作；后文'
        span=gate.preparation_refusal_spans(text)[0]
        self.assertEqual(text[span['start']:span['end']],'不能证明未生成待确认操作')
        self.assertEqual(span['text'],text[span['start']:span['end']])

    def test_condition_or_uncertainty_cannot_hide_later_success(self):
        for prefix in ['如果','若','假设','不能证明','没有证明','不代表','不能说','没有说','不是说','不等于','无法断言','并不意味着']:
            for suffix in ['，转账已成功','；撤销已成功','：已成功','，已准备，请确认']:
                with self.subTest(prefix=prefix,suffix=suffix):
                    self.assertFalse(check(prefix+'未生成待确认操作'+suffix)['ok'])

    def test_nonassertion_old_pending_or_success_not_borrowed(self):
        for prefix in ['如果','不能证明']:
            self.assertFalse(check(prefix+'未生成转账待确认操作，已准备，请确认',[operation()])['ok'])
            self.assertFalse(check(prefix+'未生成转账待确认操作，已成功',[operation(status='SUCCEEDED')])['ok'])

    def test_nonassertion_explicit_other_id_still_checked(self):
        for prefix in ['如果','不能证明']:
            self.assertTrue(check(prefix+'未生成转账待确认操作；另一笔转账 op_old 已成功',[operation(status='SUCCEEDED')])['ok'])
            self.assertFalse(check(prefix+'未生成转账待确认操作；另一笔转账 op_unknown 已成功',[operation(status='SUCCEEDED')])['ok'])

    def test_nonassertion_preceding_positive_is_checked(self):
        self.assertFalse(check('转账已成功，不能证明未生成待确认操作')['ok'])

    def test_nonassertion_numeric_gate_still_checks_original(self):
        self.assertFalse(check('如果未生成待确认操作，余额999.99元')['ok'])


if __name__ == '__main__': unittest.main()
