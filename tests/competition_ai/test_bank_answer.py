"""Behavioral fixtures for the limited gate; no real bank or model calls."""
import unittest
import re
from src.bank_v1_web.bank_answer import validate_bank_answer, quoted_action_label


def answer(text, data=None, *, successful=True, tool='operation'):
    trace = [] if data is None else [{'kind':'response', 'ok':successful, 'tool':tool, 'response':{'data':data}}]
    return validate_bank_answer({'ok':True,'text':text,'session_id':'old-session','trace':trace}, '')


def operation(status='PENDING', kind='transfer'):
    value = {'operation_id':'op_probe1','kind':kind,'status':status}
    if status == 'SUCCEEDED':
        value['receipt'] = {'receipt_id':'receipt_probe','kind':kind,'status':status,'amount_minor':50000}
    return {'operation':value}


def task(state='READY', prepared=None):
    return {'task':{'task_id':'task1','state':state,'nodes':[
        {'id':'prepare','state':'WAITING_CONFIRMATION' if prepared else 'PENDING','result':prepared}]}}


class AnswerGateTests(unittest.TestCase):
    def test_s03_missing_real_operation_rejected(self):
        value = answer('已为您准备好代扣查询操作，请到银行面板确认。')
        self.assertFalse(value['ok'])
        self.assertIsNone(value['session_id'])
        self.assertTrue(value['model_ok'])
        self.assertIn('未作为成功结果',value['error'])

    def test_zero_tool_missing_amount_is_legitimate(self):
        self.assertTrue(answer('您打算转账多少？')['ok'])

    def test_choice_of_operation_is_not_confirmation_card(self):
        self.assertTrue(answer('请确认您要办理的是转账还是取消代扣？')['ok'])

    def test_missing_date_clarification_is_not_confirmation_card(self):
        self.assertTrue(answer('请确认转账日期是哪一天？')['ok'])

    def test_general_explanation_does_not_require_tools(self):
        self.assertTrue(answer('取消银行代扣授权不等于取消商户订阅。')['ok'])

    def test_future_action_not_executed_claim(self):
        self.assertTrue(answer('转账成功后，我才能查询回执。')['ok'])

    def test_negation_does_not_hide_following_claim(self):
        self.assertFalse(answer('我不会替您确认，但已准备转账，请到银行面板确认。')['ok'])

    def test_conditional_does_not_hide_next_sentence(self):
        self.assertFalse(answer('如果您需要帮助，请告诉我；已准备好代扣查询操作。')['ok'])

    def test_prepared_does_not_prove_executed(self):
        self.assertFalse(answer('转账已准备，并且已经执行成功。',operation())['ok'])

    def test_completed_operation_cannot_request_confirmation(self):
        self.assertFalse(answer('请到银行面板确认转账。',operation('SUCCEEDED'))['ok'])

    def test_saved_task_without_operation_is_not_card(self):
        self.assertFalse(answer('转账任务已准备好，请确认。',task())['ok'])

    def test_saved_task_can_honestly_wait_for_executor(self):
        self.assertTrue(answer('转账任务已保存，等待执行器继续准备。',task())['ok'])

    def test_task_executor_tool_result_is_valid_evidence(self):
        data=task('WAITING_CONFIRMATION',{'data':operation()})
        self.assertTrue(answer('转账已准备，请确认。',data)['ok'])

    def test_actual_receipt_allows_success(self):
        self.assertTrue(answer('转账已经成功。',operation('SUCCEEDED'))['ok'])

    def test_done_preparation_is_not_claimed_bank_completion(self):
        self.assertTrue(answer('已办妥，新的确认卡已生成。',operation())['ok'])

    def test_bank_action_done_synonym_still_needs_receipt(self):
        self.assertFalse(answer('转账已办妥，钱到了。')['ok'])

    def test_expired_rejected_failed_are_not_confirmable(self):
        for state in ['EXPIRED','REJECTED','FAILED']:
            with self.subTest(state=state):
                self.assertFalse(answer('转账已准备，请确认。',operation(state))['ok'])

    def test_error_response_is_not_evidence(self):
        self.assertFalse(answer('转账已执行成功。',operation('SUCCEEDED'),successful=False)['ok'])

    def test_wrong_operation_kind_cannot_back_query(self):
        self.assertFalse(answer('代扣查询已准备，请确认。',operation())['ok'])

    def test_wrong_operation_id_cannot_back_claim(self):
        self.assertFalse(answer('转账 op_other 已成功。',operation('SUCCEEDED'))['ok'])

    def test_task_complete_needs_all_nodes(self):
        data=task('FAILED',{'data':operation('SUCCEEDED')})
        self.assertFalse(answer('任务全部完成。',data)['ok'])

    def test_successful_bank_and_failed_task_must_be_distinguished(self):
        data=task('FAILED',{'data':operation('SUCCEEDED')})
        self.assertTrue(answer('转账已成功。任务尚未全部完成。',data)['ok'])

    def test_task_title_is_not_a_new_confirmation_request(self):
        self.assertTrue(answer('任务「查余额并准备转账（待确认）」已收尾。',
            {'task':{'task_id':'task1','state':'SUCCEEDED','nodes':[{'state':'SUCCEEDED'}]}})['ok'])

    def test_no_bank_tool_execution_by_validator(self):
        original=operation()
        answer('转账已准备，请确认。',original)
        self.assertEqual('PENDING',original['operation']['status'])

    def test_unbacked_new_amount_is_rejected_and_original_preserved(self):
        value=answer('账户剩余1345.00元。',{'balance_minor':484500})
        self.assertFalse(value['ok'])
        self.assertEqual('账户剩余1345.00元。',value['text'])
        self.assertEqual(1,len(value['trace']))
        self.assertIsNone(value['session_id'])
        self.assertEqual('UNSUPPORTED_DISPLAYED_AMOUNT',value['answer_validation']['reasons'][0]['code'])
        self.assertEqual(['1345.00'],value['answer_validation']['numeric_diagnostics'][0]['values'])

    def test_calculator_echo_is_not_new_financial_fact(self):
        value=answer('余额10000.00元。',{'calculation':{'inputs':{'left_minor':1000000},'result':{'amount_minor':650000}}},tool='money_calculate')
        self.assertFalse(value['ok'])
        self.assertEqual(['10000.00'],value['answer_validation']['numeric_diagnostics'][0]['values'])

    def test_task_wrapping_does_not_turn_input_into_bank_fact(self):
        data={'task':{'task_id':'task1','state':'READY','nodes':[{
            'args':{'left_minor':1000000},
            'result':{'data':{'calculation':{'inputs':{'left_minor':1000000},'result':{'amount_minor':650000}}}}}]}}
        value=answer('余额10000.00元。',data,tool='task_status')
        self.assertFalse(value['ok'])
        self.assertEqual(['10000.00'],value['answer_validation']['numeric_diagnostics'][0]['values'])

    def test_actual_tool_amounts_and_zero_are_supported(self):
        self.assertTrue(answer('余额4,845.00元，零支出为0元。',{'balance_minor':484500})['ok'])

    def test_user_original_hypothetical_can_be_repeated(self):
        result={'ok':True,'text':'按您提出的3500元消费假设，需要先查还款日。','trace':[]}
        self.assertTrue(validate_bank_answer(result,'假如今天支出3500元，会怎样？')['ok'])

    def test_user_amount_does_not_prove_new_derived_result(self):
        result={'ok':True,'text':'消费后还剩1345元。','trace':[]}
        self.assertFalse(validate_bank_answer(result,'假如今天支出3500元，会怎样？')['ok'])

    def test_negative_balance_can_be_displayed_as_local_deficit(self):
        for text in ['预计余额−155.00元，缺口155元。','资金短缺约 **155.00** 元。','还缺155元。']:
            with self.subTest(text=text):
                self.assertTrue(answer(text,{'projected_balance_minor':-15500})['ok'])

    def test_deficit_word_cannot_whitelist_positive_balance_elsewhere(self):
        value=answer('缺口155元。账户剩余155元。',{'projected_balance_minor':-15500})
        self.assertFalse(value['ok'])

    def test_negative_current_or_total_balance_not_global_absolute_value(self):
        for field in ['balance_minor','current_balance_minor','total_balance_minor','lowest_balance_minor']:
            with self.subTest(field=field):
                self.assertFalse(answer('可用余额155元。',{field:-15500})['ok'])
                self.assertTrue(answer('余额−155元，缺口155元。',{field:-15500})['ok'])

    def test_negative_calculator_result_does_not_prove_positive_amount(self):
        data={'calculation':{'inputs':{'left_minor':10000,'right_minor':25500},'result':{'amount_minor':-15500}}}
        self.assertFalse(answer('剩余155元。',data,tool='money_calculate')['ok'])
        self.assertTrue(answer('计算结果为-155元。',data,tool='money_calculate')['ok'])

    def test_calculator_result_is_usable_but_not_proof_of_input_facts(self):
        data={'calculation':{'inputs':{'left_minor':484500,'right_minor':350000},
              'result':{'amount_minor':134500},'financial_inputs_verified':False}}
        value=answer('按输入数计算得到1345元。',data,tool='money_calculate')
        self.assertTrue(value['ok'])
        self.assertIn('计算输入事实正确',value['answer_validation']['limitation'])

    def test_description_and_declared_reference_are_not_amount_sources(self):
        data={'description':'用户余额1345元','left_evidence_id':'余额1345元','args':{'amount_minor':134500}}
        self.assertFalse(answer('余额1345元。',data)['ok'])

    def test_failed_response_and_wrong_scope_are_not_amount_sources(self):
        self.assertFalse(answer('余额1345元。',{'balance_minor':134500},successful=False)['ok'])
        result={'ok':True,'text':'余额1345元。','trace':[{'kind':'response','ok':True,
                'response':{'scope':'moneydata','data':{'balance_minor':134500}}}]}
        self.assertFalse(validate_bank_answer(result,'')['ok'])

    def test_description_threshold_is_not_numeric_evidence(self):
        value=answer('规则为上涨至少10%，目前不能认定确切涨幅。',{'message':'上涨至少10%'})
        self.assertFalse(value['ok'])
        self.assertEqual('UNSUPPORTED_DISPLAYED_PERCENT',value['answer_validation']['numeric_diagnostics'][0]['code'])
        self.assertIn('UNSUPPORTED_DISPLAYED_PERCENT', [r['code'] for r in value['answer_validation']['reasons']])
        self.assertEqual('AMOUNT_AND_PERCENT_ENFORCED',value['answer_validation']['numeric_policy'])

    def test_decimal_percent_is_not_reinterpreted_as_money(self):
        value=answer('实际涨幅待核对；规则阈值10.00%。')
        self.assertFalse(value['ok'])
        self.assertEqual(['UNSUPPORTED_DISPLAYED_PERCENT'],[d['code'] for d in value['answer_validation']['numeric_diagnostics']])

    def test_action_fabrication_stays_separate_from_amount_failure(self):
        value=answer('转账已成功，余额1345元。')
        self.assertEqual({'UNBACKED_OPERATION_SUCCESS','UNSUPPORTED_DISPLAYED_AMOUNT'},
                         {r['code'] for r in value['answer_validation']['reasons']})

    def test_model_failure_is_not_reclassified_as_numeric_repair(self):
        value=validate_bank_answer({'ok':False,'text':'余额1345元。','error':'timeout'},'')
        self.assertEqual('MODEL_FAILED',value['answer_validation']['status'])
        self.assertFalse(value['ok'])

    def test_shortfall_difference_is_local(self):
        for text in ['仍差155.00元。','还差155元。','尚差约155元。','差155元。','结果：差155元。']:
            with self.subTest(text=text): self.assertTrue(answer(text,{'projected_balance_minor':-15500})['ok'])
    def test_difference_is_not_any_positive_amount(self):
        for text in ['余额155元。','相差155元。','价差155元。','仍差155元。余额155元。']:
            with self.subTest(text=text): self.assertFalse(answer(text,{'projected_balance_minor':-15500})['ok'])
    def test_shortfall_still_needs_negative_source(self):
        self.assertFalse(answer('仍差155元。',{'projected_balance_minor':-20000})['ok'])
    def test_short_label_cancellation_uses_cancellation_pending(self):
        query=operation('SUCCEEDED','mandate_query')['operation']
        cancel=operation('PENDING','mandate_cancel')['operation'];cancel['operation_id']='op_cancel'
        for quote in ['「撤销代扣」','“撤销代扣”','"撤销代扣"','「取消代扣」']:
            with self.subTest(quote=quote):
                self.assertTrue(answer(quote+'待确认卡已生成（对象：云存储代扣）。',{'operations':[query,cancel]})['ok'])
    def test_pending_query_cannot_substitute_cancellation(self):
        self.assertFalse(answer('「撤销代扣」待确认卡已生成（对象：云存储代扣）。',operation('PENDING','mandate_query'))['ok'])
    def test_pending_cancel_cannot_substitute_query(self):
        self.assertFalse(answer('「代扣查询」待确认卡已生成。',operation('PENDING','mandate_cancel'))['ok'])
    def test_cancellation_pending_not_success(self):
        self.assertFalse(answer('「撤销代扣」已经成功。',operation('PENDING','mandate_cancel'))['ok'])
    def test_quote_status_and_task_title_still_stripped(self):
        for quote in ['「转账已成功」','“撤销代扣待确认”','"查余额并准备转账（待确认）"']:
            with self.subTest(quote=quote): self.assertEqual('',quoted_action_label(re.search(r'「[^」]*」|“[^”]*”|"[^"\n]*"',quote)))

if __name__ == '__main__':
    unittest.main(verbosity=2)
