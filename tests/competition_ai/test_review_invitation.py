"""Information review is not a claim that a banking card awaits confirmation."""
import unittest
from src.bank_v1_web import bank_answer as gate


def operation(kind='arrangement',status='PENDING',ident='op_current'):
    op={'operation_id':ident,'kind':kind,'status':status}
    if status=='SUCCEEDED':op['receipt']={'receipt_id':'receipt_current','kind':kind}
    return op


def check(text,operations=()):
    trace=[{'kind':'response','tool':'operation','ok':True,
        'response':{'scope':'bank','data':{'operation':op}}} for op in operations]
    return gate.validate_bank_answer({'ok':True,'text':text,'trace':trace},'请说明资金变化')


class ReviewInvitationTests(unittest.TestCase):
    def test_actual_information_invitation(self):
        self.assertTrue(check('下一步：请在11月1日前按缺口日期核对安排与到账条件')['ok'])

    def test_information_fields(self):
        for obj in ['安排与到账条件','安排日期','收入到账条件','余额和流水','账单信息','还款日期','转账金额']:
            with self.subTest(obj=obj):
                self.assertTrue(check('请核对'+obj)['ok'])
                self.assertEqual(gate.confirmation_request_spans('请核对'+obj)[0]['classification'],'INFORMATION_REVIEW')

    def test_span_structure(self):
        text='下一步：请按日期核对安排与到账条件'
        span=gate.confirmation_request_spans(text)[0]
        self.assertEqual(text[span['start']:span['end']],span['text'])
        self.assertEqual(span['verb'],'核对')

    def test_unknown_review_object_keeps_legacy_verification(self):
        span=gate.confirmation_request_spans('请核对神秘安排物件')[0]
        self.assertEqual(span['classification'],'UNCLASSIFIED_CONFIRMATION')
        self.assertFalse(check('请核对神秘安排物件')['ok'])

    def test_explicit_confirmation_still_requires_operation(self):
        for text in ['请确认对应安排','请确认这笔转账','等待您确认安排','安排待确认']:
            with self.subTest(text=text):self.assertFalse(check(text)['ok'])

    def test_card_and_ui_review_require_pending(self):
        for text in ['请核对安排确认卡','请在银行面板核对安排日期','请在核对页面核对安排','请核对操作弹窗中的日期']:
            with self.subTest(text=text):
                self.assertFalse(check(text)['ok'])
                self.assertTrue(check(text,[operation()])['ok'])
                self.assertFalse(check(text,[operation(status='SUCCEEDED')])['ok'])

    def test_prepared_context_keeps_pending_check(self):
        text='安排已准备，请核对安排日期'
        self.assertFalse(check(text)['ok'])
        self.assertTrue(check(text,[operation()])['ok'])
        self.assertFalse(check(text,[operation(status='SUCCEEDED')])['ok'])

    def test_explicit_current_id_is_not_information_exemption(self):
        text='请核对安排 op_current 的日期'
        self.assertFalse(check(text)['ok'])
        self.assertTrue(check(text,[operation()])['ok'])
        self.assertFalse(check(text,[operation(status='SUCCEEDED')])['ok'])

    def test_unknown_id_cannot_borrow_old_pending(self):
        self.assertFalse(check('请核对安排 op_unknown 的日期',[operation()])['ok'])

    def test_abbreviated_unknown_id_still_checked(self):
        self.assertFalse(check('请核对安排 op_deadbeef… 的日期',[operation()])['ok'])

    def test_pending_wrong_kind_does_not_support_card(self):
        self.assertFalse(check('请核对转账确认卡',[operation()])['ok'])

    def test_second_waiting_request_not_hidden(self):
        for text in ['请核对安排日期，等待你确认安排','请核对安排日期；请确认安排',
                     '请核对安排日期并确认操作']:
            with self.subTest(text=text):self.assertFalse(check(text)['ok'])

    def test_following_positive_claim_remains_checked(self):
        for tail in ['，转账已成功','；安排已准备，请确认','：代扣授权已撤销','且转账已成功']:
            with self.subTest(tail=tail):self.assertFalse(check('请核对安排与到账条件'+tail)['ok'])

    def test_preceding_positive_claim_remains_checked(self):
        for prefix in ['转账已成功，','安排已准备，','代扣授权已撤销；']:
            self.assertFalse(check(prefix+'请核对安排日期')['ok'])

    def test_information_with_real_other_operation_success(self):
        self.assertTrue(check('请核对安排日期；转账 op_current 已成功',[operation('transfer','SUCCEEDED')])['ok'])

    def test_unsupported_success_does_not_borrow_other_kind(self):
        self.assertFalse(check('请核对安排日期；转账已成功',[operation('mandate_query','SUCCEEDED')])['ok'])

    def test_information_text_does_not_hide_unknown_claim_id(self):
        self.assertFalse(check('请核对安排日期；转账 op_unknown 已成功',[operation('transfer','SUCCEEDED')])['ok'])

    def test_information_is_not_pending_even_with_old_success(self):
        self.assertTrue(check('请核对安排日期',[operation(status='SUCCEEDED')])['ok'])

    def test_information_numeric_gate_is_unchanged(self):
        result=check('请核对安排日期，余额1234.56元')
        self.assertFalse(result['ok'])
        self.assertIn('UNSUPPORTED_DISPLAYED_AMOUNT',[r['code'] for r in result['answer_validation']['reasons']])

    def test_bank_action_object_is_not_information(self):
        for text in ['请核对转账','请核对撤销代扣操作','请核对修改安排操作']:
            self.assertFalse(check(text)['ok'])

    def test_question_clarification_keeps_existing_behavior(self):
        self.assertTrue(check('请确认转账日期是哪一天？')['ok'])

    def test_first_confirmation_not_overwritten_by_later_review(self):
        text='请确认转账，请核对日期'
        self.assertFalse(check(text)['ok'])
        spans=gate.confirmation_request_spans(text)
        self.assertEqual([s['classification'] for s in spans],['OPERATION_CONFIRMATION','INFORMATION_REVIEW'])

    def test_first_unknown_review_not_overwritten_by_later_information(self):
        self.assertFalse(check('请核对转账，请核对日期')['ok'])

    def test_two_information_reviews_remain_information(self):
        text='请核对安排日期，请核对到账条件'
        self.assertTrue(check(text)['ok'])
        for span in gate.confirmation_request_spans(text):
            self.assertEqual(span['classification'],'INFORMATION_REVIEW')
            self.assertEqual(span['text'],text[span['start']:span['end']])

    def test_no_comma_confirm_before_review_not_hidden(self):
        for text in ['请确认转账并核对日期','请确认这笔转账再核对日期']:
            with self.subTest(text=text):self.assertFalse(check(text)['ok'])

    def test_no_comma_confirmation_state_matrix(self):
        for connector in ['并','再','然后','并且','随后','以及','、',' ']:
            text='请确认转账'+connector+'核对日期'
            with self.subTest(connector=connector):
                self.assertFalse(check(text)['ok'])
                self.assertTrue(check(text,[operation('transfer')])['ok'])
                self.assertFalse(check(text,[operation('transfer','SUCCEEDED')])['ok'])

    def test_no_comma_review_then_confirm_stays_protected(self):
        for connector in ['并','再','然后','、',' ']:
            text='请核对日期'+connector+'确认转账'
            self.assertFalse(check(text)['ok'])
            self.assertTrue(check(text,[operation('transfer')])['ok'])
            self.assertFalse(check(text,[operation('transfer','SUCCEEDED')])['ok'])

    def test_multiple_predicate_classification_offsets(self):
        text='请确认转账并核对日期'
        spans=gate.confirmation_request_spans(text)
        self.assertEqual([s['verb'] for s in spans],['确认','核对'])
        self.assertEqual([s['classification'] for s in spans],['OPERATION_CONFIRMATION','INFORMATION_REVIEW'])
        for span in spans:self.assertEqual(span['text'],text[span['start']:span['end']])

    def test_repeated_please_without_comma_not_hidden(self):
        self.assertFalse(check('请确认转账请核对日期')['ok'])

    def test_waiting_then_information_still_requires_pending(self):
        for text in ['等待你确认转账并核对日期','请核对日期并等待你确认转账']:
            self.assertFalse(check(text)['ok'])
            self.assertTrue(check(text,[operation('transfer')])['ok'])
            self.assertFalse(check(text,[operation('transfer','SUCCEEDED')])['ok'])

    def test_same_kind_old_success_cannot_replace_pending(self):
        self.assertFalse(check('请确认转账并核对日期',[operation('transfer','SUCCEEDED','op_old')])['ok'])

    def test_no_comma_unknown_id_not_hidden(self):
        for text in ['请确认转账并核对日期 op_unknown','请确认 op_unknown 转账再核对日期']:
            self.assertFalse(check(text,[operation('transfer')])['ok'])

    def test_no_comma_numeric_gate_remains(self):
        self.assertFalse(check('请确认转账并核对金额1234.56元',[operation('transfer')])['ok'])



class ArrangementInformationObjectTests(unittest.TestCase):
    def test_modified_arrangement_names(self):
        for modifier in ['', '固定', '定期', '每月', '每周', '本期', '近期', '未来', '已记录的', '已确认的']:
            for kind in ['还款','收入','支出','收支']:
                with self.subTest(modifier=modifier,kind=kind):
                    self.assertTrue(gate.financial_arrangement_review_object(modifier+kind+'安排'))
                    self.assertTrue(check('请核对'+modifier+kind+'安排')['ok'])

    def test_actual_object_list(self):
        text='下一步：请在10月15日前核对固定还款安排与项目收入的到账条件'
        self.assertTrue(check(text)['ok'])
        self.assertEqual(gate.confirmation_request_spans(text)[0]['classification'],'INFORMATION_REVIEW')

    def test_bounded_lists_and_fields(self):
        for obj in ['固定还款安排的日期','当前固定还款安排以及预计收入的到账时间','定期支出安排、收入金额和日期','已记录收入安排与项目收入的到账条件']:
            with self.subTest(obj=obj):self.assertTrue(gate.financial_arrangement_review_object(obj))
        self.assertFalse(gate.financial_arrangement_review_object('固定还款安排与日期与时间与金额'))

    def test_unmodeled_tail_not_information(self):
        for tail in ['已生成待确认','已准备','已成功','未成功','并确认转账','然后办理转账','并且核对余额','不存在','不是真的']:
            text='请核对固定还款安排'+tail
            with self.subTest(text=text):
                self.assertFalse(gate.financial_arrangement_review_object('固定还款安排'+tail))
                self.assertNotEqual(gate.confirmation_request_spans(text)[0]['classification'],'INFORMATION_REVIEW')

    def test_unknown_modifier_and_newline(self):
        for obj in ['神秘还款安排','所有任意还款安排','固定还款安排\n与日期','固定还款安排\n']:
            self.assertFalse(gate.financial_arrangement_review_object(obj))

    def test_confirm_not_review(self):
        self.assertFalse(check('请确认固定还款安排')['ok'])
        self.assertEqual(gate.confirmation_request_spans('请确认固定还款安排')[0]['classification'],'OPERATION_CONFIRMATION')

    def test_operation_context_wins(self):
        for tail in ['确认卡','卡片','页面','面板','操作','已准备',' op_unknown']:
            with self.subTest(tail=tail):self.assertFalse(check('请核对固定还款安排'+tail)['ok'])

    def test_following_actual_confirmation(self):
        for suffix in ['，然后确认操作','，请确认转账','并确认转账','以及最近的入账情况，然后确认转账']:
            with self.subTest(suffix=suffix):self.assertFalse(check('请核对固定还款安排'+suffix)['ok'])

    def test_preceding_actual_confirmation(self):
        self.assertFalse(check('请确认转账，然后请核对固定还款安排')['ok'])

    def test_adjacent_prepared_and_executed(self):
        for suffix in ['，转账已准备，请确认','，转账已成功','；已撤销代扣','并已成功转账']:
            with self.subTest(suffix=suffix):self.assertFalse(check('请核对固定还款安排'+suffix)['ok'])

    def test_old_success_no_pending(self):
        self.assertFalse(check('请核对固定还款安排，然后请确认转账',[operation('transfer','SUCCEEDED','op_old')])['ok'])

    def test_unknown_reference_stays_blocked(self):
        self.assertFalse(check('请核对固定还款安排，请确认 op_unknown 转账',[operation('transfer')])['ok'])

    def test_numeric_checks_unchanged(self):
        self.assertFalse(check('请核对固定还款安排；当前余额1234.56元')['ok'])

    def test_valid_pending_still_works(self):
        self.assertTrue(check('请核对固定还款安排，然后请确认转账',[operation('transfer')])['ok'])

    def test_confirmed_adjective_is_not_request(self):
        for obj in ['已确认的还款安排','固定还款安排与已确认的收入安排','已确认的还款安排与已确认的收入的到账条件']:
            text='请核对'+obj
            with self.subTest(text=text):
                self.assertTrue(check(text)['ok'])
                spans=gate.confirmation_request_spans(text)
                self.assertEqual(len(spans),1)
                self.assertEqual(spans[0]['verb'],'核对')
                self.assertEqual(spans[0]['classification'],'INFORMATION_REVIEW')

    def test_confirmed_adjective_does_not_hide_requests(self):
        for text in ['请确认已确认的还款安排','请核对已确认的还款安排并确认操作','请核对已确认的还款安排，再确认操作','请确认转账并核对已确认的还款安排','请核对已确认的还款安排，请确认 op_unknown 转账']:
            with self.subTest(text=text):self.assertFalse(check(text)['ok'])

    def test_confirmed_adjective_is_exact_and_no_action_tail(self):
        for obj in ['已确认还款安排','已经确认的还款安排','未确认的还款安排','已确认的还款安排已生成待确认']:
            self.assertFalse(gate.financial_arrangement_review_object(obj))
        self.assertFalse(check('请核对已确认的还款安排，转账已成功')['ok'])

if __name__=='__main__':unittest.main()
