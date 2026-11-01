"""A bounded negative task-state statement needs direct, complete task evidence."""
import copy
import unittest
from src.bank_v1_web.bank_answer import validate_bank_answer


TASK_ID='a'*32
OTHER_ID='b'*32
CLAIM='任务 5 个节点（账户核对、安排核对、基准预测、拟消费预测、转账办理）均已成功，无失败或待确认节点'


def task(identifier=TASK_ID):
    operation={'operation_id':'op_transfer','kind':'transfer','status':'SUCCEEDED',
        'receipt':{'receipt_id':'receipt_done','operation_id':'op_transfer','kind':'transfer','status':'SUCCEEDED'}}
    nodes=[{'id':str(i),'tool':tool,'state':'SUCCEEDED','operation_id':None,
            'result':{'data':{'observed':True}}} for i,tool in enumerate(['accounts','arrangements','forecast','forecast'])]
    nodes.append({'id':'transfer','tool':'prepare_transfer','state':'SUCCEEDED','operation_id':'op_transfer',
                  'result':{'data':{'operation':operation}}})
    return {'task_id':identifier,'scope':'bank','subject_id':'U1','state':'SUCCEEDED','nodes':nodes}


def response(value,tool='task_status',ok=True,scope='bank'):
    return {'kind':'response','tool':tool,'ok':ok,'response':{'scope':scope,'data':{'task':value}}}


def evaluate(text=CLAIM, tasks=None, trace=None):
    if trace is None:trace=[response(v) for v in (tasks if tasks is not None else [task()])]
    return validate_bank_answer({'ok':True,'text':text,'trace':trace},'核对原任务完成状态')


def codes(result):return [reason['code'] for reason in result['answer_validation']['reasons']]


class TaskNegativeStateTests(unittest.TestCase):
    def test_complete_counted_task_not_positive_pending(self):
        result=evaluate();self.assertTrue(result['ok']);self.assertNotIn('OPERATION_NOT_PENDING',codes(result))

    def test_no_parenthesis_is_supported(self):
        self.assertTrue(evaluate('任务5个节点都已完成，没有失败和待确认节点')['ok'])

    def test_markdown_explicit_id(self):
        claim=CLAIM.replace('任务 5',f'任务 `{TASK_ID}` **5**')
        self.assertTrue(evaluate(claim)['ok'])

    def test_no_tools_rejected(self):
        self.assertIn('UNBACKED_TASK_COMPLETION',codes(evaluate(tasks=[])))

    def test_completed_transfer_alone_not_task(self):
        op=task()['nodes'][-1]['result']
        result=evaluate(trace=[{'kind':'response','tool':'operation','ok':True,'response':op}])
        self.assertFalse(result['ok'])

    def test_task_envelope_in_other_tool_not_fresh_status(self):
        self.assertFalse(evaluate(trace=[response(task(),tool='accounts')])['ok'])

    def test_nested_task_args_do_not_count(self):
        trace=[{'kind':'response','tool':'task_status','ok':True,'response':{'data':{'args':{'task':task()}}}}]
        self.assertFalse(evaluate(trace=trace)['ok'])

    def test_failed_response_not_evidence(self):
        self.assertFalse(evaluate(trace=[response(task(),ok=False)])['ok'])

    def test_foreign_scope_response_rejected(self):
        self.assertFalse(evaluate(trace=[response(task(),scope='moneydata')])['ok'])

    def test_foreign_task_scope_or_subject(self):
        for key,value in [('scope','moneydata'),('subject_id','U2')]:
            t=task();t[key]=value
            with self.subTest(key=key):self.assertFalse(evaluate(tasks=[t])['ok'])

    def test_task_failed_even_if_nodes_succeeded(self):
        t=task();t['state']='FAILED';self.assertFalse(evaluate(tasks=[t])['ok'])

    def test_any_waiting_failed_or_pending_node_rejected(self):
        for state in ['FAILED','WAITING_CONFIRMATION','WAITING_VERIFICATION','PENDING','RUNNING']:
            t=task();t['nodes'][2]['state']=state
            with self.subTest(state=state):self.assertFalse(evaluate(tasks=[t])['ok'])

    def test_wrong_count_and_out_of_bounds(self):
        for count in ['4','6','0','21']:
            with self.subTest(count=count):self.assertFalse(evaluate(CLAIM.replace('5',count))['ok'])

    def test_duplicate_node_ids_rejected(self):
        t=task();t['nodes'][1]['id']='0';self.assertFalse(evaluate(tasks=[t])['ok'])

    def test_missing_node_result_rejected(self):
        t=task();t['nodes'][2]['result']=None;self.assertFalse(evaluate(tasks=[t])['ok'])

    def test_foreign_node_result_rejected(self):
        t=task();t['nodes'][2]['result']['scope']='moneydata';self.assertFalse(evaluate(tasks=[t])['ok'])

    def test_misnamed_tool_in_parenthesis_rejected(self):
        t=task();t['nodes'][2]['tool']='accounts';self.assertFalse(evaluate(tasks=[t])['ok'])

    def test_embedded_success_in_labels_not_hidden(self):
        self.assertFalse(evaluate(CLAIM.replace('账户核对','已成功取消商户服务'))['ok'])

    def test_missing_receipt_not_proof_of_successful_node(self):
        t=task();t['nodes'][-1]['result']['data']['operation']['receipt']=None
        self.assertFalse(evaluate(tasks=[t])['ok'])

    def test_receipt_operation_or_kind_or_status_mismatch(self):
        for key,value in [('operation_id','op_other'),('kind','mandate_cancel'),('status','PENDING')]:
            t=task();t['nodes'][-1]['result']['data']['operation']['receipt'][key]=value
            with self.subTest(key=key):self.assertFalse(evaluate(tasks=[t])['ok'])

    def test_multiple_tasks_ambiguous_even_if_both_succeeded(self):
        self.assertFalse(evaluate(tasks=[task(),task(OTHER_ID)])['ok'])

    def test_explicit_id_selects_one_of_multiple(self):
        t=task(OTHER_ID);t['state']='FAILED'
        self.assertTrue(evaluate(CLAIM.replace('任务 ',f'任务 {TASK_ID} '),[task(),t])['ok'])

    def test_explicit_id_not_found_rejected(self):
        self.assertFalse(evaluate(CLAIM.replace('任务 ',f'任务 {OTHER_ID} '))['ok'])

    def test_explicit_failed_task_not_other_success(self):
        t=task(OTHER_ID);t['state']='FAILED'
        self.assertFalse(evaluate(CLAIM.replace('任务 ',f'任务 {OTHER_ID} '),[task(),t])['ok'])

    def test_latest_status_overrides_old_success(self):
        latest=task();latest['state']='WAITING_CONFIRMATION'
        self.assertFalse(evaluate(tasks=[task(),latest])['ok'])

    def test_latest_invalid_scope_does_not_reuse_old_success(self):
        latest=task();latest['subject_id']='U2'
        self.assertFalse(evaluate(tasks=[task(),latest])['ok'])

    def test_append_pending_contradiction(self):
        for tail in ['，但仍有待确认节点','，但仍有待确认','，且还有2个失败节点']:
            with self.subTest(tail=tail):
                self.assertIn('CONTRADICTORY_TASK_STATE',codes(evaluate(CLAIM+tail)))

    def test_other_unbacked_preparation_after_summary(self):
        result=evaluate(CLAIM+'，银行代扣查询卡已准备，等待你确认')
        self.assertIn('UNBACKED_OPERATION_PREPARATION',codes(result))

    def test_other_unbacked_success_before_summary(self):
        result=evaluate('代扣授权已撤销，'+CLAIM)
        self.assertIn('UNBACKED_OPERATION_SUCCESS',codes(result))

    def test_other_unbacked_success_after_summary(self):
        result=evaluate(CLAIM+'，商户服务已取消')
        self.assertIn('UNBACKED_OPERATION_SUCCESS',codes(result))

    def test_local_meiyou_does_not_swallow_other_success(self):
        result=evaluate(CLAIM.replace('无失败','没有失败')+'，代扣授权已撤销')
        self.assertIn('UNBACKED_OPERATION_SUCCESS',codes(result))

    def test_unknown_operation_id_after_summary(self):
        self.assertFalse(evaluate(CLAIM+'，操作 op_unknown 已成功')['ok'])

    def test_invalid_amount_after_summary_still_rejected(self):
        result=evaluate(CLAIM+'；余额为999.99元')
        self.assertIn('UNSUPPORTED_DISPLAYED_AMOUNT',codes(result))

    def test_conditional_summary_does_not_claim_current_completion(self):
        self.assertTrue(evaluate('如果 '+CLAIM+'，就可以结束任务',tasks=[])['ok'])

    def test_empty_tool_not_completed_node(self):
        t=task();t['nodes'][0]['tool']=''
        self.assertFalse(evaluate('任务5个节点均已成功，无失败或待确认节点',tasks=[t])['ok'])

    def test_condition_does_not_cover_following_query_card_assertion(self):
        result=evaluate('如果'+CLAIM.replace('无失败','没有失败')+'，银行代扣查询卡已准备，等待你确认',tasks=[])
        self.assertIn('UNBACKED_OPERATION_PREPARATION',codes(result))

    def test_unrelated_negative_phrase_does_not_cover_cancel(self):
        result=evaluate(CLAIM+'，没有问题，代扣授权已撤销')
        self.assertIn('UNBACKED_OPERATION_SUCCESS',codes(result))

    def test_latest_failed_response_invalidates_only_answer_evidence(self):
        result=evaluate(trace=[response(task()),response(task(),ok=False)])
        self.assertIn('UNBACKED_TASK_COMPLETION',codes(result))

    def test_latest_foreign_response_invalidates_old_bank_summary(self):
        self.assertFalse(evaluate(trace=[response(task()),response(task(),scope='moneydata')])['ok'])

    def test_failed_response_request_locator_is_not_completion_fact(self):
        failure={'kind':'response','tool':'task_status','ok':False,'args':{'task_id':TASK_ID},'response':{'error':'unavailable'}}
        self.assertFalse(evaluate(trace=[response(task()),failure])['ok'])
        self.assertFalse(evaluate(trace=[failure])['ok'])

    def test_unknown_and_meta_tools_are_not_task_business_nodes(self):
        for tool in ['fabricated_tool','submit_task_plan','replan_task','task_status',{},None]:
            t=task();t['nodes'][0]['tool']=tool
            with self.subTest(tool=tool):
                self.assertFalse(evaluate('任务5个节点均已成功，无失败或待确认节点',tasks=[t])['ok'])

    def test_actual_matcher_is_known_business_tool(self):
        t=task();t['nodes'][0]['tool']='match_mandate_from_query'
        self.assertTrue(evaluate('任务5个节点均已成功，无失败或待确认节点',tasks=[t])['ok'])

    def test_short_query_card_without_tools_rejected(self):
        self.assertIn('UNBACKED_OPERATION_PREPARATION',codes(evaluate('查询卡已准备。',tasks=[])))

    def test_short_query_card_failed_tool_rejected(self):
        self.assertFalse(evaluate('查询卡已准备。',trace=[{'kind':'response','tool':'prepare_mandate_query','ok':False,'response':{}}])['ok'])

    def test_short_query_card_transfer_receipt_cannot_prove_query(self):
        self.assertFalse(evaluate('查询卡已准备。')['ok'])

    def test_short_query_card_backed_pending_query(self):
        op={'operation_id':'op_query','kind':'mandate_query','status':'PENDING','receipt':None}
        trace=[{'kind':'response','tool':'prepare_mandate_query','ok':True,'response':{'data':{'operation':op}}}]
        self.assertTrue(evaluate('查询卡已准备。',trace=trace)['ok'])

    def test_future_query_card_explanation_not_actual_preparation(self):
        self.assertTrue(evaluate('如果需要查询卡，我再准备并等待你确认。',tasks=[])['ok'])

    def test_no_tools_prepared_mandate_still_rejected(self):
        result=evaluate('当前状态：银行代扣查询卡已准备，正在等待你确认。',tasks=[])
        self.assertIn('UNBACKED_OPERATION_PREPARATION',codes(result))


if __name__=='__main__':unittest.main()
