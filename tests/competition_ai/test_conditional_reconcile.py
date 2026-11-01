"""Bounded future reconciliation advice must not hide actual bank assertions."""
import unittest
import copy
from src.bank_v1_web.bank_answer import remove_conditional_reconcile_advice,validate_bank_answer

FIRST='如项目收入到账或还款实际扣款，请在核对页面确认对应安排，以保持预测准确'
SECOND='项目收入实际到账后请及时确认对应安排，避免重复预测'

def evaluate(text,trace=None):
 return validate_bank_answer({'ok':True,'text':text,'trace':trace or []},'解释后续如何避免重复预测')
def codes(result):return [r['code'] for r in result['answer_validation']['reasons']]

class ConditionalReconcileTests(unittest.TestCase):
 def test_both_observed_patterns_are_bounded_future_advice(self):
  for value in [FIRST,SECOND]:
   with self.subTest(value=value):
    self.assertEqual('',remove_conditional_reconcile_advice(value))
    self.assertTrue(evaluate(value)['ok'])
 def test_named_condition_variants_and_candidate_precondition(self):
  for value in ['如收入到账，请确认对应安排','若还款实际扣款，请在核对区域确认相关安排',
   '如果收入到账且出现待核对候选后，请核对后确认对应安排','还款实际扣款后，请及时在核对区确认对应安排',
   '支出实际扣款后请确认对应安排','收入到账后，请确认相关安排，以避免重复预测']:
   with self.subTest(value=value):
    self.assertEqual('',remove_conditional_reconcile_advice(value));self.assertTrue(evaluate(value)['ok'])
 def test_markdown_and_sentence_boundaries(self):
  self.assertTrue(evaluate('下一步：**'+FIRST+'**。')['ok'])
  self.assertEqual('前文；。后文',remove_conditional_reconcile_advice('前文；'+FIRST+'。后文'))
 def test_adjacent_actual_success_is_never_removed(self):
  for advice in [FIRST,SECOND]:
   for suffix in ['，代扣授权已撤销','，商户服务已取消','，操作 op_unknown 已成功','；转账已成功']:
    with self.subTest(advice=advice,suffix=suffix):
     result=evaluate(advice+suffix)
     self.assertFalse(result['ok']);self.assertIn('UNBACKED_OPERATION_SUCCESS',codes(result))
 def test_adjacent_actual_preparation_is_never_removed(self):
  for value in [FIRST+'，查询卡已准备，等待你确认',SECOND+'，现在已准备转账，请确认',
    '银行代扣查询卡已准备，'+FIRST]:
   with self.subTest(value=value):
    self.assertFalse(evaluate(value)['ok']);self.assertIn('UNBACKED_OPERATION_PREPARATION',codes(evaluate(value)))
 def test_runi_suojian_is_not_a_future_condition(self):
  for value in ['如你所见，查询卡已准备，请确认','如你所见，代扣授权已撤销',
                '如现在所示，银行代扣查询卡已准备']:
   with self.subTest(value=value):
    self.assertEqual(value,remove_conditional_reconcile_advice(value));self.assertFalse(evaluate(value)['ok'])
 def test_unconditional_confirmation_or_preparation_not_exempt(self):
  for value in ['请在核对页面确认对应安排','现在已准备修改安排，请确认',
                '项目收入实际到账，请及时确认对应安排','还款已实际扣款，请确认对应安排']:
   with self.subTest(value=value):
    self.assertEqual(value,remove_conditional_reconcile_advice(value));self.assertFalse(evaluate(value)['ok'])
 def test_present_assertion_inside_condition_not_removed(self):
  for value in ['如项目收入已到账，请在核对页面确认对应安排',
                '如项目收入到账，查询卡已准备，请确认对应安排',
                '如项目收入到账，请确认对应安排已完成']:
   with self.subTest(value=value):
    self.assertEqual(value,remove_conditional_reconcile_advice(value));self.assertFalse(evaluate(value)['ok'])
 def test_no_partial_substring_from_label_or_other_word(self):
  for value in ['比如收入到账，请确认对应安排','示例项目收入到账后请确认对应安排',
                '项目收入到账后请确认对应安排版本']:
   with self.subTest(value=value):self.assertEqual(value,remove_conditional_reconcile_advice(value))
 def test_numeric_gate_sees_original_text(self):
  result=evaluate(FIRST+'，余额999.99元。')
  self.assertFalse(result['ok']);self.assertIn('UNSUPPORTED_DISPLAYED_AMOUNT',codes(result))
 def test_untrusted_candidate_text_does_not_create_preparation_evidence(self):
  trace=[{'kind':'response','tool':'event_context','ok':True,'response':{'scope':'bank','data':{'unconfirmed_matches':[],'note':FIRST}}}]
  self.assertFalse(evaluate('查询卡已准备。',trace)['ok'])
 def test_unknown_wide_condition_not_granted_this_new_exemption(self):
  for value in ['如你需要，我已经准备好代扣查询卡，请确认','收入到账后请直接撤销全部授权',
                '如任意操作成功，请在核对页面确认对应安排']:
   with self.subTest(value=value):self.assertEqual(value,remove_conditional_reconcile_advice(value))

TASK_ID='a'*32

def current_plan():
 return {'task_id':TASK_ID,'scope':'bank','subject_id':'U1','state':'WAITING_CONFIRMATION','nodes':[
  {'id':'q','tool':'prepare_mandate_query','state':'WAITING_CONFIRMATION','depends_on':[]},
  {'id':'m','tool':'match_mandate_from_query','state':'PENDING','depends_on':['q']},
  {'id':'c','tool':'prepare_mandate_cancel','state':'PENDING','depends_on':['m']},
  {'id':'r','tool':'operation','state':'PENDING','depends_on':['c']}]}

def plan_response(task=None,**updates):
 value={'kind':'response','tool':'task_status','ok':True,'response':{'scope':'bank','data':{'task':task or current_plan()}}}
 value.update(updates);return value

PLAN='已保存任务「测试流程」，流程为：\n\n1. 查询银行代扣授权\n2. 匹配唯一授权\n3. 准备撤销并再次等待本人确认\n4. 核对回执\n'

class FuturePlanStepTests(unittest.TestCase):
 def test_evidenced_pending_later_step_not_prepared(self):
  self.assertTrue(evaluate(PLAN,[plan_response()])['ok'])
 def test_no_direct_task_status_not_proof(self):
  for trace in [[],[plan_response(tool='accounts')],[{'kind':'response','tool':'task_status','ok':True,'response':{'data':{'args':{'task':current_plan()}}}}]]:
   with self.subTest(trace=trace):self.assertFalse(evaluate(PLAN,trace)['ok'])
 def test_multiple_tasks_require_explicit_id(self):
  other=current_plan();other['task_id']='b'*32
  trace=[plan_response(),plan_response(other)]
  self.assertFalse(evaluate(PLAN,trace)['ok'])
  self.assertTrue(evaluate(PLAN+'任务编号：'+TASK_ID,trace)['ok'])
 def test_latest_failed_or_foreign_lookup_not_old_success(self):
  bad=plan_response();bad['response']['scope']='moneydata'
  for latest in [plan_response(ok=False),bad]:
   with self.subTest(latest=latest):self.assertFalse(evaluate(PLAN,[plan_response(),latest])['ok'])
 def test_terminal_or_foreign_task_not_future_permission(self):
  for key,value in [('state','FAILED'),('state','SUCCEEDED'),('state','CANCELLED'),('subject_id','U2'),('scope','moneydata')]:
   task=current_plan();task[key]=value
   with self.subTest(key=key,value=value):self.assertFalse(evaluate(PLAN,[plan_response(task)])['ok'])
 def test_actual_waiting_node_without_card_still_needs_evidence(self):
  task=current_plan();task['nodes'][2]['state']='WAITING_CONFIRMATION'
  self.assertFalse(evaluate(PLAN,[plan_response(task)])['ok'])
 def test_wrong_ordinal_tool_or_completed_dependency_not_covered(self):
  self.assertFalse(evaluate(PLAN.replace('3. 准备','4. 准备'),[plan_response()])['ok'])
  self.assertFalse(evaluate(PLAN.replace('准备撤销','准备转账'),[plan_response()])['ok'])
  for state in ['SUCCEEDED','FAILED','CANCELLED']:
   task=current_plan();task['nodes'][1]['state']=state
   with self.subTest(state=state):self.assertFalse(evaluate(PLAN,[plan_response(task)])['ok'])
 def test_current_prepared_or_success_not_a_neutral_plan_step(self):
  for line in ['3. 已准备撤销并再次等待本人确认','3. 准备撤销并再次等待本人确认：代扣授权已撤销',
   '3. 准备撤销并再次等待本人确认，查询卡已准备','3. 代扣授权已撤销']:
   text=PLAN.replace('3. 准备撤销并再次等待本人确认',line)
   with self.subTest(line=line):self.assertFalse(evaluate(text,[plan_response()])['ok'])
 def test_other_un_numbered_actual_statement_not_exempt(self):
  for tail in ['\n代扣授权已撤销。','\n查询卡已准备，请确认。','\n现在已准备修改安排，请确认。']:
   with self.subTest(tail=tail):self.assertFalse(evaluate(PLAN+tail,[plan_response()])['ok'])
 def test_no_header_or_wrong_header_not_a_saved_plan_context(self):
  for header in ['', '流程为：', '当前进度：', '虚构例子，流程为：']:
   text=header+'\n3. 准备撤销并再次等待本人确认'
   with self.subTest(header=header):self.assertFalse(evaluate(text,[plan_response()])['ok'])
 def test_first_actual_card_requires_pending_query_evidence(self):
  text=PLAN+'\n当前进度：第一步查询已准备，等待你确认。'
  self.assertFalse(evaluate(text,[plan_response()])['ok'])
  op={'kind':'response','tool':'prepare_mandate_query','ok':True,'response':{'data':{'operation':{'operation_id':'op_q','kind':'mandate_query','status':'PENDING','summary':{}}}}}
  self.assertTrue(evaluate(text,[plan_response(),op])['ok'])
 def test_future_list_does_not_suppress_numbers(self):
  result=evaluate(PLAN+'\n余额9999.99元。',[plan_response()])
  self.assertFalse(result['ok']);self.assertIn('UNSUPPORTED_DISPLAYED_AMOUNT',codes(result))

 def test_current_prepared_query_with_confirmation_needs_real_query(self):
  for text in ['第一步查询已准备，等待你确认。','查询已备好，请确认。','当前进度：第2步查询已经准备，请您确认。']:
   with self.subTest(text=text):self.assertFalse(evaluate(text)['ok'])
 def test_ordinary_readonly_query_completion_needs_no_yellow_operation(self):
  for text in ['账户查询已完成。','已查询当前账户余额。']:
   with self.subTest(text=text):self.assertTrue(evaluate(text)['ok'])

if __name__=='__main__':unittest.main(verbosity=2)
