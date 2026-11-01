"""Time before confirmation is not a current confirmation request."""
import unittest
from src.bank_v1_web import bank_answer as gate


def operation(status='PENDING', kind='transfer', identifier='op_current'):
    op={'operation_id':identifier,'kind':kind,'status':status}
    if status=='SUCCEEDED': op['receipt']={'receipt_id':'receipt_current','operation_id':identifier,'kind':kind,'status':status}
    return {'kind':'response','tool':'operation','ok':True,'response':{'scope':'bank','data':{'operation':op}}}


def check(text, trace=()):
    return gate.validate_bank_answer({'ok':True,'text':text,'trace':list(trace)},'请说明安排和账户情况')


class ConfirmationTimeReferenceTests(unittest.TestCase):
    def test_finite_time_forms(self):
        for text in ['请在确认前自行留意A的留存资金','请在确认之前检查余额',
                     '请在您确认前仔细检查余额','请在转账确认前先检查余额',
                     '请在操作确认前务必自行留意余额','请 在 确认 前 自行 检查余额']:
            with self.subTest(text=text):
                spans=gate.confirmation_request_spans(text)
                self.assertEqual(len(spans),1)
                self.assertEqual(spans[0]['classification'],'TEMPORAL_REFERENCE')
                self.assertEqual(spans[0]['text'],text[spans[0]['start']:spans[0]['end']])
                self.assertTrue(check(text)['ok'])

    def test_time_reminder_with_real_arrangement_not_operation(self):
        trace=[{'kind':'response','tool':'arrangements','ok':True,'response':{'scope':'bank','data':{'arrangements':[
            {'title':'固定还款','kind':'expense','amount_minor':300000,'due_date':'2026-10-15','status':'active'}]}}}]
        self.assertTrue(check('固定还款安排金额3,000.00元，请在确认前自行留意A的留存资金',trace)['ok'])

    def test_non_temporal_confirmations(self):
        for text in ['请确认前一笔转账','等待确认转账','请确认该转账',
                     '请在确认前一笔转账','请在确认前生成转账操作']:
            with self.subTest(text=text):
                self.assertFalse(check(text)['ok'])
                self.assertNotIn('TEMPORAL_REFERENCE',[s['classification'] for s in gate.confirmation_request_spans(text)])

    def test_no_cross_punctuation(self):
        for text in ['转账操作：请在确认，前检查转账','转账操作：请在确认；前检查转账','转账操作：请在确认。前检查转账','转账操作：请在确认\n前检查转账']:
            with self.subTest(text=text):
                self.assertNotIn('TEMPORAL_REFERENCE',[s['classification'] for s in gate.confirmation_request_spans(text)])
                self.assertFalse(check(text)['ok'])

    def test_subjectless_fragments_do_not_gain_temporal_classification(self):
        # Whole-answer legacy action-context behavior is recorded separately;
        # this change must not interpret across punctuation as a time span.
        for text in ['请在确认；前检查转账','请在确认。前检查转账','请在确认\n前检查转账']:
            with self.subTest(text=text):
                self.assertNotIn('TEMPORAL_REFERENCE',[s['classification'] for s in gate.confirmation_request_spans(text)])

    def test_actual_confirmation_after_temporal_predicate(self):
        for tail in ['并确认转账','，请确认转账','；等待确认转账','请确认转账']:
            text='请在确认前检查余额'+tail
            with self.subTest(text=text):
                self.assertFalse(check(text)['ok'])
                self.assertTrue(check(text,[operation()])['ok'])
                self.assertFalse(check(text,[operation('SUCCEEDED')])['ok'])

    def test_first_actual_confirmation_cannot_be_reclassified(self):
        text='请确认转账并请在确认前检查余额'
        spans=gate.confirmation_request_spans(text)
        self.assertIn('OPERATION_CONFIRMATION',[x['classification'] for x in spans])
        self.assertFalse(check(text)['ok'])

    def test_adjacent_preparation_remains_checked(self):
        for separator in ['，','；','并','：','\n']:
            text='请在确认前检查余额'+separator+'已生成转账确认卡'
            with self.subTest(separator=separator):self.assertFalse(check(text)['ok'])
        self.assertFalse(check('转账已准备，请在确认前检查余额')['ok'])

    def test_adjacent_success_remains_checked(self):
        for separator in ['，','；','并','：','\n']:
            text='请在确认前检查余额'+separator+'转账已成功'
            with self.subTest(separator=separator):
                self.assertFalse(check(text)['ok'])
                self.assertFalse(check(text,[operation('SUCCEEDED','mandate_query')])['ok'])
                self.assertTrue(check(text,[operation('SUCCEEDED')])['ok'])

    def test_unknown_id_does_not_borrow_existing_pending(self):
        self.assertFalse(check('请在确认前检查余额，请确认转账 op_unknown',[operation()])['ok'])
        self.assertFalse(check('请在确认前检查余额；转账 op_unknown 已成功',[operation('SUCCEEDED')])['ok'])

    def test_time_reminder_does_not_require_current_pending(self):
        for status in ['PENDING','SUCCEEDED','REJECTED','EXPIRED']:
            with self.subTest(status=status):self.assertTrue(check('请在确认前自行留意余额',[operation(status)])['ok'])

    def test_number_and_percent_checks_unchanged(self):
        for tail,code in [('余额3,000.00元','UNSUPPORTED_DISPLAYED_AMOUNT'),('支出占收入35%','UNSUPPORTED_DISPLAYED_PERCENT')]:
            with self.subTest(tail=tail):
                result=check('请在确认前检查'+tail)
                self.assertFalse(result['ok'])
                self.assertIn(code,[r['code'] for r in result['answer_validation']['reasons']])

    def test_polite_prefix_time_reference(self):
        for prefix in ['请您在','请你在','请 您 在','请\t你\t在']:
            text=prefix+'确认前检查账户余额'
            with self.subTest(text=text):
                self.assertEqual(gate.confirmation_request_spans(text)[0]['classification'],'TEMPORAL_REFERENCE')
                self.assertTrue(check(text)['ok'])

    def test_polite_prefix_not_previous_transfer_object(self):
        for text in ['请您确认前一笔转账','请你确认前一笔转账','请您在确认前一笔转账']:
            with self.subTest(text=text):
                self.assertNotIn('TEMPORAL_REFERENCE',[x['classification'] for x in gate.confirmation_request_spans(text)])
                self.assertFalse(check(text)['ok'])

    def test_polite_prefix_neighbours_still_require_evidence(self):
        for prefix in ['请您在','请你在']:
            for tail in ['，转账已成功','；已生成转账确认卡','并已核对真实回执','，请确认转账 op_unknown']:
                text=prefix+'确认前检查余额'+tail
                with self.subTest(text=text):self.assertFalse(check(text)['ok'])

    def test_independent_long_tail_confirmation_regressions(self):
        for text in ['请在确认前检查账户余额和近期支出并确认转账',
                     '请在确认前自行留意账户余额以及最近的入账情况，然后确认转账',
                     '请您在确认前检查账户余额和近期支出并确认转账',
                     '请你在确认前自行留意账户余额以及最近的入账情况，然后确认转账']:
            with self.subTest(text=text):
                self.assertNotIn('TEMPORAL_REFERENCE',[x['classification'] for x in gate.confirmation_request_spans(text)])
                self.assertFalse(check(text)['ok'])
                self.assertTrue(check(text,[operation()])['ok'])

    def test_multiple_predicates_keep_legacy_classification(self):
        for text in ['请在确认前检查账户余额和近期支出并核对转账',
                     '请您在确认前检查余额，请核对转账',
                     '请在确认前检查余额并确认转账',
                     '请在确认前检查余额和确认卡']:
            with self.subTest(text=text):
                self.assertNotIn('TEMPORAL_REFERENCE',[x['classification'] for x in gate.confirmation_request_spans(text)])
                self.assertFalse(check(text)['ok'])

    def test_not_global_nonassertion(self):
        text='请在确认前检查余额，已核对真实回执'
        result=check(text)
        self.assertFalse(result['ok'])
        self.assertIn('UNBACKED_OPERATION_SUCCESS',[r['code'] for r in result['answer_validation']['reasons']])


if __name__=='__main__':unittest.main()
