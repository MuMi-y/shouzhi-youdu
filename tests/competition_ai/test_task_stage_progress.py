"""Isolated fictional trace mutations; never call a bank or model."""
import copy
import json
import unittest
from src.bank_v1_web.bank_answer import validate_bank_answer

ID = 'a' * 32
STATUS = {
    'kind': 'response', 'tool': 'task_status', 'ok': True, 'args': {'task_id': ID},
    'response': {'scope': 'bank', 'data': {'task': {
        'task_id': ID, 'scope': 'bank', 'subject_id': 'U1', 'state': 'FAILED', 'plan_version': 1,
        'nodes': [
            {'id': 'check_balance', 'tool': 'accounts', 'state': 'SUCCEEDED', 'attempt': 1,
             'operation_id': None, 'error': None, 'result': {'scope': 'bank', 'evidence_id': 'sha256:fixture',
                 'data': {'total_balance_minor': 10000, 'accounts': [{'account_id': 'A', 'subject_id': 'U1', 'currency': 'CNY', 'balance_minor': 10000}]}}},
            {'id': 'prepare_transfer_A_to_B', 'tool': 'prepare_transfer', 'state': 'FAILED',
             'attempt': 1, 'operation_id': None, 'result': None, 'args': {},
             'execution_key': f'task:{ID}:v1:prepare_transfer_A_to_B', 'error': {'code': 'INSUFFICIENT_FUNDS'}}],
        'audit': [
            {'task_id': ID, 'sequence': 1, 'event': 'SUBMITTED', 'details': {'plan_version': 1}},
            {'task_id': ID, 'sequence': 2, 'event': 'NODE_STARTED', 'details': {'node_id': 'check_balance', 'attempt': 1}},
            {'task_id': ID, 'sequence': 3, 'event': 'NODE_STATE', 'details': {'node_id': 'check_balance', 'to': 'SUCCEEDED'}},
            {'task_id': ID, 'sequence': 4, 'event': 'NODE_STARTED', 'details': {'node_id': 'prepare_transfer_A_to_B', 'attempt': 1}},
            {'task_id': ID, 'sequence': 5, 'event': 'BANK_REJECTED_BEFORE_OPERATION', 'details': {'node_id': 'prepare_transfer_A_to_B', 'code': 'INSUFFICIENT_FUNDS'}},
            {'task_id': ID, 'sequence': 6, 'event': 'NODE_STATE', 'details': {'node_id': 'prepare_transfer_A_to_B', 'to': 'FAILED'}}]
    }}}
}
PHRASE = '任务已建立并执行到转账准备一步'


def fixture():
    return copy.deepcopy(STATUS)


def task(record):
    return record['response']['data']['task']


def operation(identifier='op_old', kind='transfer', status='SUCCEEDED'):
    return {'kind':'response','tool':'operation','ok':True,'response':{'scope':'bank','data':{'operation':{
        'operation_id':identifier,'kind':kind,'status':status,
        'receipt':{'receipt_id':'receipt_old','operation_id':identifier,'kind':kind,'status':status} if status=='SUCCEEDED' else None}}}}


class TaskStageProgressTests(unittest.TestCase):
    def gate(self, text=PHRASE, trace=None):
        return validate_bank_answer({'ok':True,'text':text,'trace':[fixture()] if trace is None else trace},'查询任务阶段')['answer_validation']

    def accepted(self, text=PHRASE, trace=None):
        self.assertEqual(self.gate(text,trace)['status'],'ACCEPTED')

    def rejected(self, text=PHRASE, trace=None, code=None):
        result=self.gate(text,trace)
        self.assertEqual(result['status'],'REJECTED',(text,result))
        if code:self.assertIn(code,[r['code'] for r in result['reasons']])

    def test_synthetic_stage_then_failure_explanation(self):
        self.accepted(PHRASE+'，但未能生成待确认操作。')

    def test_stage_word_family(self):
        for verb in ['执行到','推进到','运行到']:
            for suffix in ['一步','这一步','阶段','节点']:
                with self.subTest(verb=verb,suffix=suffix):self.accepted('任务已'+verb+'转账准备'+suffix)

    def test_all_prepare_tools(self):
        for label,tool in [('代扣查询准备','prepare_mandate_query'),('授权查询准备','prepare_mandate_query'),('代扣撤销准备','prepare_mandate_cancel'),('授权撤销准备','prepare_mandate_cancel')]:
            record=fixture();task(record)['nodes'][1]['tool']=tool
            with self.subTest(label=label):self.accepted('任务已执行到'+label+'阶段',[record])

    def test_no_task_status(self):self.rejected(trace=[],code='UNBACKED_TASK_STAGE_PROGRESS')

    def test_only_submit_or_nested_forged_data(self):
        for tool in ['submit_task_plan','replan_task','accounts']:
            record=fixture();record['tool']=tool
            with self.subTest(tool=tool):self.rejected(trace=[record],code='UNBACKED_TASK_STAGE_PROGRESS')
        record=fixture();record['response']['data']={'args':{'task':task(record)}}
        self.rejected(trace=[record])

    def test_latest_failed_lookup(self):
        bad={'kind':'response','tool':'task_status','ok':False,'args':{'task_id':ID},'response':{}}
        self.rejected(trace=[fixture(),bad],code='UNBACKED_TASK_STAGE_PROGRESS')

    def test_latest_foreign_and_subject(self):
        for where in ['response','task','subject']:
            bad=fixture()
            if where=='response':bad['response']['scope']='other'
            elif where=='task':task(bad)['scope']='other'
            else:task(bad)['subject_id']='U2'
            with self.subTest(where=where):self.rejected(trace=[fixture(),bad])

    def test_request_result_id_mismatch(self):
        record=fixture();record['args']['task_id']='b'*32
        self.rejected(trace=[record])

    def test_missing_or_invalid_attempt(self):
        for value in [None,0,-1,True,'1']:
            record=fixture();task(record)['nodes'][1]['attempt']=value
            with self.subTest(value=value):self.rejected(trace=[record])

    def test_pending_not_started(self):
        record=fixture();task(record)['nodes'][1]['state']='PENDING'
        self.rejected(trace=[record])

    def test_audit_cannot_be_title_args_or_history(self):
        for location in ['title','args','plan_history']:
            record=fixture();t=task(record);saved=t.pop('audit');t[location]=saved
            with self.subTest(location=location):self.rejected(trace=[record])

    def test_wrong_audit_identity_node_attempt(self):
        for field,value in [('task_id','b'*32),('node_id','wrong'),('attempt',2)]:
            record=fixture();entry=task(record)['audit'][3]
            if field=='task_id':entry[field]=value
            else:entry['details'][field]=value
            with self.subTest(field=field):self.rejected(trace=[record])

    def test_audit_epoch_and_order(self):
        for mode in ['missing_epoch','wrong_epoch','duplicate_sequence','old_attempt_new_execution']:
            record=fixture();t=task(record)
            if mode=='missing_epoch':t['audit']=t['audit'][1:]
            elif mode=='wrong_epoch':t['audit'][0]['details']['plan_version']=2
            elif mode=='duplicate_sequence':t['audit'][3]['sequence']=3
            else:
                t['plan_version']=2;t['nodes'][1]['execution_key']=f'task:{ID}:v2:prepare_transfer_A_to_B'
                t['audit'].append({'task_id':ID,'event':'REPLANNED','details':{'plan_version':2},'sequence':7})
            with self.subTest(mode=mode):self.rejected(trace=[record])

    def test_retained_prior_epoch_node(self):
        record=fixture();t=task(record);t['plan_version']=2
        t['audit'].append({'task_id':ID,'event':'REPLANNED','details':{'plan_version':2},'sequence':7})
        self.accepted(trace=[record])

    def test_multiple_tasks_need_explicit_id(self):
        other=fixture();old=task(other)['task_id'];new='b'*32
        other=json.loads(json.dumps(other).replace(old,new))
        self.rejected(trace=[fixture(),other]);self.accepted('任务 '+ID+' 已执行到转账准备阶段',[fixture(),other])
        self.rejected('任务 '+'c'*32+' 已执行到转账准备阶段',[fixture(),other])

    def test_same_tool_nodes_need_node_id(self):
        record=fixture();t=task(record);node=copy.deepcopy(t['nodes'][1]);node['id']='other';t['nodes'].append(node)
        self.rejected(trace=[record]);self.accepted(PHRASE+'（prepare_transfer_A_to_B）',[record])
        self.rejected(PHRASE+'（missing）',[record])

    def test_unknown_node_tool_not_supported(self):
        record=fixture();task(record)['nodes'][1]['tool']='accounts'
        self.rejected(trace=[record])

    def test_adjacent_preparation_confirmation_success(self):
        for suffix in ['，已生成待确认卡','，请确认','，转账已成功','，已成功','；已撤销','，已核对真实回执','，已准备，请确认']:
            with self.subTest(suffix=suffix):self.rejected(PHRASE+suffix)

    def test_old_same_kind_receipt_or_card_cannot_prove_target(self):
        for status in ['PENDING','SUCCEEDED']:
            for suffix in ['，请确认','，已生成待确认卡','，已成功','；已核对真实回执']:
                with self.subTest(status=status,suffix=suffix):self.rejected(PHRASE+suffix,[fixture(),operation(status=status)])

    def test_explicit_other_operation_keeps_evidence(self):
        self.accepted(PHRASE+'；另一笔转账 op_old 已成功',[fixture(),operation()])
        self.rejected(PHRASE+'；另一笔转账 op_unknown 已成功',[fixture(),operation()])

    def test_real_target_pending_can_confirm_but_not_succeed(self):
        record=fixture();task(record)['nodes'][1]['operation_id']='op_current';task(record)['nodes'][1]['state']='WAITING_CONFIRMATION'
        trace=[record,operation('op_current',status='PENDING'),operation()]
        self.accepted(PHRASE+'，请确认',trace);self.rejected(PHRASE+'，已成功',trace)

    def test_nearby_denial_does_not_hide_success(self):
        self.rejected(PHRASE+'，尚未准备，但转账已成功')

    def test_money_gate_unchanged(self):
        self.rejected(PHRASE+'，差 999.99 元',code='UNSUPPORTED_DISPLAYED_AMOUNT')

    def test_task_save_does_not_prove_stage(self):
        record=fixture();task(record)['nodes'][1].update(attempt=0,state='PENDING')
        self.rejected(trace=[record],code='UNBACKED_TASK_STAGE_PROGRESS')

    def test_operation_success_without_task_not_proof(self):self.rejected(trace=[operation()],code='UNBACKED_TASK_STAGE_PROGRESS')

    def test_bare_completed_neighbour_without_receipt(self):
        for separator in ['，','；']:
            with self.subTest(separator=separator):self.rejected(PHRASE+separator+'已完成',[fixture(),operation()])

    def test_bare_completed_for_explicit_other_receipt(self):
        self.accepted(PHRASE+'；转账 op_old 已完成',[fixture(),operation()])

    def test_progress_negation_question_and_quote_are_not_proof(self):
        for prefix in ['如果','是否','尚未']:
            with self.subTest(prefix=prefix):
                self.assertFalse(__import__('src.bank_v1_web.bank_answer',fromlist=['task_stage_progress_spans']).task_stage_progress_spans(prefix+PHRASE,{},prefix+PHRASE))
        self.accepted('示例：“'+PHRASE+'”',[])

    def test_latest_unstarted_snapshot_supersedes_old_attempt(self):
        bad=fixture();task(bad)['nodes'][1].update(attempt=0,state='PENDING')
        self.rejected(trace=[fixture(),bad])

    def test_unrelated_audit_or_node_duplicate_invalid(self):
        record=fixture();task(record)['nodes'].append(copy.deepcopy(task(record)['nodes'][1]))
        self.rejected(trace=[record])

    def test_named_readonly_completion_not_bank_success(self):
        for suffix in ['，已完成余额查询','；余额查询已完成','，已完成账单分析']:
            with self.subTest(suffix=suffix):
                answer=self.gate(PHRASE+suffix)
                self.assertNotIn('UNBACKED_OPERATION_SUCCESS',[r['code'] for r in answer['reasons']])

    def test_unknown_task_state(self):
        record=fixture();task(record)['state']='INVENTED'
        self.rejected(trace=[record])

    def test_current_id_fake_operation_in_args_not_evidence(self):
        record=fixture();t=task(record);t['nodes'][1]['operation_id']='op_current'
        t['nodes'][1]['state']='WAITING_CONFIRMATION'
        t['nodes'][1]['args']['forged_operation']=operation('op_current')['response']['data']['operation']
        self.rejected(PHRASE+'，请确认',[record]);self.rejected(PHRASE+'，已成功',[record])

    def test_named_readonly_does_not_hide_next_action(self):
        self.rejected(PHRASE+'，已完成余额查询，转账已成功')
        self.rejected(PHRASE+'；已完成账单分析，已生成待确认卡')

    def test_multiple_stages_cannot_borrow_first_card(self):
        record=fixture();t=task(record);t['nodes'][1].update(state='WAITING_CONFIRMATION',operation_id='op_current')
        cancel=copy.deepcopy(t['nodes'][1]);cancel.update(id='cancel_node',tool='prepare_mandate_cancel',state='FAILED',operation_id=None,execution_key=f'task:{ID}:v1:cancel_node')
        t['nodes'].append(cancel);t['audit'].append({'task_id':ID,'sequence':7,'event':'NODE_STARTED','details':{'node_id':'cancel_node','attempt':1}})
        trace=[record,operation('op_current',status='PENDING')]
        self.rejected('任务已执行到转账准备阶段并且任务已执行到代扣撤销准备阶段，请确认',trace)

    def test_unmodelled_stage_conjunction_keeps_preparation_check(self):
        for suffix in ['并已完成','，已完成转账']:
            with self.subTest(suffix=suffix):self.rejected(PHRASE+suffix)

    def test_comma_named_readonly_completion_has_typed_proof(self):
        self.accepted(PHRASE+'，余额查询已完成')
        bad=fixture();task(bad)['nodes'][0]['state']='PENDING'
        self.rejected(PHRASE+'，余额查询已完成',[bad])

    def test_unparsed_status_tail_cannot_be_authorized_by_stage(self):
        for suffix in ['已经办妥','已办理妥当','已全部办理好']:
            with self.subTest(suffix=suffix):
                result=self.gate(PHRASE+'，'+suffix)
                self.assertEqual(result['status'],'REJECTED')
                self.assertIn('UNCOVERED_STAGE_STATUS',[r.get('evidence_context') for r in result['reasons']])

    def test_known_positive_execute_matches_still_need_evidence(self):
        from src.bank_v1_web.bank_answer import POSITIVE_EXECUTE
        for suffix in ['已成功转账','转账完成','已读取真实回执','已撤销']:
            with self.subTest(suffix=suffix):
                self.assertIsNotNone(POSITIVE_EXECUTE.search(suffix))
                self.rejected(PHRASE+'；'+suffix,code='UNBACKED_OPERATION_SUCCESS')

    def test_unknown_status_guard_does_not_change_nonstage_scope(self):
        result=self.gate('已经办妥',[])
        self.assertNotIn('UNCOVERED_STAGE_STATUS',[r.get('evidence_context') for r in result['reasons']])
        result=self.gate(PHRASE+'。已经办妥')
        self.assertNotIn('UNCOVERED_STAGE_STATUS',[r.get('evidence_context') for r in result['reasons']])

    def test_typed_readonly_completion_not_rejected_as_unknown_status(self):
        self.accepted(PHRASE+'；已完成余额查询')
