"""Explicit fictional snapshots; no file, service or model dependencies."""
from copy import deepcopy
import unittest
from src.bank_v1_web.forecast_minimum import forecast_minimum_checks, valid_minima
from src.bank_v1_web.bank_answer import validate_bank_answer


def projection(primary=278900, conservative=87900):
    value = {'scope': 'COMBINED_ACCOUNTS', 'as_of_date': '2026-10-13', 'through_date': '2026-11-12',
             'lowest_balance_minor': primary, 'lowest_balance_date': '2026-10-15'}
    value['scenario_minima'] = {'contract_version': 1, 'scope': 'COMBINED_ACCOUNTS', 'currency': 'CNY',
        'window_start': '2026-10-13', 'window_end': '2026-11-12', 'date_basis': 'BUSINESS_DATE_UTC_PLUS_08',
        'selection_rule': 'FIRST_MATCHING_DAILY_DATE_ELSE_CURRENT_SNAPSHOT',
        'PRIMARY': {'minimum_balance_minor': primary, 'minimum_balance_date': '2026-10-15',
                    'minimum_point': 'DAILY_PROJECTION', 'future_income': 'ASSUMED_FROM_ACTIVE_NON_OVERDUE_ARRANGEMENTS'},
        'WITHOUT_FUTURE_INCOME': {'minimum_balance_minor': conservative, 'minimum_balance_date': '2026-11-01',
                    'minimum_point': 'DAILY_PROJECTION', 'future_income': 'EXCLUDED'}}
    return value


def record(value=None, tool='forecast'):
    value=projection() if value is None else value
    return {'kind':'response','ok':True,'tool':tool,'response':{'scope':'bank','currency':'CNY','amount_unit':'minor',
        'evidence_id':'sha256:fictional-fixture','data':{'forecast':value} if tool=='event_context' else value}}


def checks(text, trace=None):
    return forecast_minimum_checks(text, [record()] if trace is None else trace)


def task_record(tool='task_status', version=1):
    identifier='a' * 32
    task={'task_id':identifier,'scope':'bank','subject_id':'U1','plan_version':version,'state':'FAILED',
          'nodes':[{'id':'prediction','tool':'forecast','state':'SUCCEEDED','attempt':1,'operation_id':None,
                    'error':None,'execution_key':'task:'+identifier+':v1:prediction','result':record()['response']}]}
    return {'kind':'response','ok':True,'tool':tool,'args':{'task_id':identifier},
            'response':{'scope':'bank','data':{'task':task}}}


class ForecastMinimumTests(unittest.TestCase):
    def test_correct_scenario_pair(self):
        result=checks('主预测最低余额2789.00元，日期为10-15；保守口径最低余额879.00元，日期为11-01。')
        self.assertEqual([c['status'] for c in result['checks']], ['VERIFIED','VERIFIED'])
        self.assertFalse(result['reasons'])

    def test_mixed_main_value_in_conservative_paragraph(self):
        result=checks('预计收入不到账的保守口径。缺口仍为0元。10-15还款后余额2789.00元，最低点为当日2789.00元。')
        self.assertEqual(result['checks'][0]['status'],'REJECTED_AMOUNT_MISMATCH')
        self.assertEqual(result['checks'][0]['scenario'],'WITHOUT_FUTURE_INCOME')

    def test_same_amount_wrong_scenario_date(self):
        r=record(projection(87900,87900))
        result=checks('保守预测最低余额879.00元，日期为10-15。',[r])
        self.assertEqual(result['checks'][0]['status'],'REJECTED_DATE_MISMATCH')

    def test_amount_correct_date_wrong(self):
        self.assertTrue(checks('主预测最低余额2789元，日期为11/1。')['reasons'])

    def test_short_and_iso_date_variants(self):
        for displayed in ('2026-11-01','11-01','11/1','11月1日'):
            with self.subTest(date=displayed):
                self.assertEqual(checks('保守口径最低余额879元，日期为'+displayed)['checks'][0]['status'],'VERIFIED')

    def test_relative_date_uses_same_paragraph(self):
        r=checks('保守口径：11-01预留目标。最低点为当日879元。')
        self.assertEqual(r['checks'][0]['status'],'VERIFIED')
        r=checks('主预测：10-15。\n\n保守口径最低点为当日879元。')
        self.assertEqual(r['checks'][0]['status'],'NOT_COVERED_DATE_REFERENCE')

    def test_blank_paragraph_resets_scope(self):
        r=checks('保守口径。\n\n最低余额879元。')
        self.assertEqual(r['checks'][0]['status'],'NOT_COVERED_SCENARIO')

    def test_markdown_header_inherits_across_single_newline(self):
        r=checks('**保守口径**\n最低余额879元，日期为11-01。')
        self.assertEqual(r['checks'][0]['status'],'VERIFIED')

    def test_two_scope_marker_not_unique(self):
        self.assertEqual(checks('主预测和保守口径最低余额879元。')['checks'][0]['status'],'NOT_COVERED_SCENARIO')

    def test_different_forecasts_not_any_matching_value(self):
        result=checks('保守口径最低余额879元。',[record(),record(projection(37900,17900))])
        self.assertEqual(result['checks'][0]['status'],'NOT_COVERED_AMBIGUOUS_FORECAST')

    def test_identical_snapshot_deduplicates(self):
        result=checks('保守口径最低余额879元。',[record(),record(tool='event_context')])
        self.assertEqual(result['checks'][0]['status'],'VERIFIED_AMOUNT_ONLY')

    def test_event_context_direct_path(self):
        result=checks('保守口径最低余额879元，日期为11-01。',[record(tool='event_context')])
        self.assertEqual(result['checks'][0]['source_path'],'data.forecast.scenario_minima.WITHOUT_FUTURE_INCOME')

    def test_legacy_is_explicit_uncovered_not_recomputed(self):
        p=projection();p.pop('scenario_minima')
        self.assertEqual(checks('保守口径最低余额2789元。',[record(p)])['checks'][0]['status'],'NOT_COVERED_LEGACY')

    def test_no_evidence_and_echo_do_not_support_minimum(self):
        for trace in ([],[{'kind':'request','tool':'forecast','arguments':projection()}],
                      [record({'args':projection()})],[record({'facts':projection()})],[record(tool='transactions')]):
            with self.subTest(trace=trace): self.assertTrue(checks('保守口径最低余额879元。',trace)['reasons'])

    def test_bad_response_metadata_or_failure(self):
        for key,value in (('scope','foreign'),('currency','USD'),('amount_unit','yuan'),('evidence_id','')):
            r=record();r['response'][key]=value
            with self.subTest(key=key): self.assertTrue(checks('保守口径最低余额879元。',[r])['reasons'])
        r=record();r['ok']=False
        self.assertTrue(checks('保守口径最低余额879元。',[r])['reasons'])

    def test_malformed_typed_summary_rejected(self):
        for key,value in (('contract_version',True),('window_start','2026-10-12'),('selection_rule','ANY'),('currency','USD')):
            p=projection();p['scenario_minima'][key]=value
            with self.subTest(key=key): self.assertTrue(checks('保守口径最低余额879元。',[record(p)])['reasons'])

    def test_bool_or_string_minor_not_money(self):
        for value in (True,'87900',879.0):
            p=projection();p['scenario_minima']['WITHOUT_FUTURE_INCOME']['minimum_balance_minor']=value
            with self.subTest(value=value): self.assertTrue(checks('保守口径最低余额879元。',[record(p)])['reasons'])

    def test_future_income_tag_and_minimum_date_validation(self):
        for key,value in (('future_income','ASSUMED'),('minimum_balance_date','2026-12-01'),('minimum_point','UNKNOWN')):
            p=projection();p['scenario_minima']['WITHOUT_FUTURE_INCOME'][key]=value
            with self.subTest(key=key): self.assertTrue(checks('保守口径最低余额879元。',[record(p)])['reasons'])

    def test_signed_balance_not_absolute_shortfall(self):
        r=record(projection(278900,-87900))
        self.assertFalse(checks('保守口径最低余额-879元。',[r])['reasons'])
        self.assertTrue(checks('保守口径最低余额879元。',[r])['reasons'])

    def test_same_numeric_value_elsewhere_not_support(self):
        r=record();r['response']['data']['current_balance_minor']=278900
        self.assertTrue(checks('保守口径最低余额2789元。',[r])['reasons'])

    def test_no_amount_is_explicitly_uncovered(self):
        self.assertEqual(checks('保守口径最低余额是多少？')['checks'][0]['status'],'NOT_COVERED_NONASSERTION')

    def test_conditional_quoted_and_negative_not_claim(self):
        for text in ('如果最低余额是879元，就需调整。','保守口径最低余额不是879元。','“保守口径最低余额879元”仅为待核说法。'):
            with self.subTest(text=text): self.assertFalse(checks(text,[])['reasons'])

    def test_denial_does_not_skip_next_assertion(self):
        result=checks('保守口径不是最低余额2789元，最低余额是2789元。')
        self.assertTrue(result['reasons'])

    def test_original_gate_numeric_and_action_still_run(self):
        for text in ('保守口径最低余额879元。额外余额999.99元。','保守口径最低余额879元。转账已成功。'):
            with self.subTest(text=text):
                r=validate_bank_answer({'ok':True,'text':text,'trace':[record()]},'')
                self.assertFalse(r['ok'])
                self.assertEqual(r['answer_validation']['forecast_minimum_checks'][0]['status'],'VERIFIED_AMOUNT_ONLY')

    def test_integration_same_number_different_meaning_rejected(self):
        r=validate_bank_answer({'ok':True,'text':'保守口径最低余额2789元，日期为10-15。','trace':[record()]},'')
        self.assertFalse(r['ok'])
        self.assertIn('FORECAST_MINIMUM_EVIDENCE_MISMATCH',[x['code'] for x in r['answer_validation']['reasons']])

    def test_decimal_display_without_yuan(self):
        self.assertEqual(checks('主预测最低余额2,789.00，日期为10-15。')['checks'][0]['status'],'VERIFIED')

    def test_combined_minimum_not_account_minimum(self):
        self.assertEqual(checks('主预测A账户最低余额2789元。')['checks'][0]['status'],'NOT_COVERED_ACCOUNT_SCOPE')

    def test_producer_standard_summary(self):
        text = ('主预测窗口最低合计余额为2789.00元，对应2026-10-15（当日收支计入后的预测）。'
                '不计未来收入的保守预测窗口最低合计余额为879.00元，对应2026-11-01（当日收支计入后的预测）。')
        result=checks(text)
        self.assertEqual([x['status'] for x in result['checks']], ['VERIFIED','VERIFIED'])

    def test_financial_scenario_condition_is_verifiable(self):
        for marker in ('若不计未来收入','如果保守口径','假设不含未来收入'):
            with self.subTest(marker=marker):
                self.assertEqual(checks(marker+'最低余额879元，对应11-01。')['checks'][0]['status'],'VERIFIED')
                self.assertTrue(checks(marker+'最低余额2789元，对应10-15。')['reasons'])

    def test_task_forecast_result_legal(self):
        for tool in ('task_status','submit_task_plan','replan_task'):
            result=checks('保守预测最低余额879元，对应11-01。',[task_record(tool)])
            with self.subTest(tool=tool):
                self.assertEqual(result['checks'][0]['status'],'VERIFIED')
                self.assertIn('nodes[prediction].result.data',result['checks'][0]['source_path'])

    def test_retained_successful_task_node_older_version_legal(self):
        result=checks('主预测最低余额2789元，对应10-15。',[task_record(version=3)])
        self.assertEqual(result['checks'][0]['status'],'VERIFIED')

    def test_task_forecast_latest_failure_or_foreign_invalidates(self):
        original=task_record()
        for alteration in ('failed','foreign','mismatched_id','node_failed'):
            later=deepcopy(original)
            if alteration=='failed': later['ok']=False
            if alteration=='foreign': later['response']['scope']='history'
            if alteration=='mismatched_id': later['response']['data']['task']['task_id']='b'*32
            if alteration=='node_failed': later['response']['data']['task']['nodes'][0]['state']='FAILED'
            with self.subTest(alteration=alteration):
                self.assertTrue(checks('主预测最低余额2789元。',[original,later])['reasons'])

    def test_task_node_strict_identity_state_and_attempt(self):
        for key,value in (('state','PENDING'),('attempt',True),('attempt',0),('execution_key','task:'+'b'*32+':v1:prediction'),
                          ('execution_key','task:'+'a'*32+':v2:prediction'),('operation_id','op_old123'),('error','failed')):
            row=task_record();row['response']['data']['task']['nodes'][0][key]=value
            with self.subTest(key=key,value=value): self.assertTrue(checks('主预测最低余额2789元。',[row])['reasons'])

    def test_task_subject_duplicate_ids_and_history_not_evidence(self):
        for alteration in ('subject','duplicates','history_only','args_only'):
            row=task_record();task=row['response']['data']['task']
            if alteration=='subject': task['subject_id']='U2'
            if alteration=='duplicates': task['nodes'].append(deepcopy(task['nodes'][0]))
            if alteration=='history_only': task['plan_history']=task.pop('nodes')
            if alteration=='args_only': row['args']['task']=row['response']['data'].pop('task')
            with self.subTest(alteration=alteration): self.assertTrue(checks('主预测最低余额2789元。',[row])['reasons'])

    def test_task_multiple_predictions_explicitly_uncovered(self):
        row=task_record();task=row['response']['data']['task'];node=deepcopy(task['nodes'][0]);node.update(id='alternative',execution_key='task:'+'a'*32+':v1:alternative',result=record(projection(12300,10000))['response']);task['nodes'].append(node)
        result=checks('主预测最低余额2789元。',[row])
        self.assertEqual(result['checks'][0]['status'],'NOT_COVERED_AMBIGUOUS_FORECAST')

    def test_question_does_not_hide_later_assertion(self):
        result=checks('保守口径最低余额2789元吗？实际最低余额2789元。')
        self.assertEqual(result['checks'][0]['status'],'NOT_COVERED_NONASSERTION')
        self.assertTrue(result['reasons'])

    def test_two_dates_are_not_silently_selected(self):
        result=checks('保守口径最低余额879元，10-15或11-01。')
        self.assertEqual(result['checks'][0]['status'],'NOT_COVERED_DATE_REFERENCE')

class CarryforwardScopeTests(unittest.TestCase):
    def test_processing_terms_do_not_replace_primary(self):
        for treatment in ('保守计入今日','保守估计','保守处理'):
            with self.subTest(treatment=treatment):
                result=checks('主预测，旧还款因履行未确认而'+treatment+'，最低合计余额2789元，日期为10-15。')
                self.assertEqual(result['checks'][0]['status'],'VERIFIED')
                self.assertEqual(result['checks'][0]['scenario'],'PRIMARY')

    def test_processing_terms_do_not_remove_value_checks(self):
        result=checks('主预测，旧还款保守计入今日，最低合计余额2790元，日期为10-15。')
        self.assertEqual(result['checks'][0]['status'],'REJECTED_AMOUNT_MISMATCH')

    def test_explicit_conservative_still_enforced(self):
        for label in ('保守口径','保守预测','保守情景','不计未来收入','若该收入不到账'):
            with self.subTest(label=label):
                self.assertTrue(checks(label+'最低余额2789元，日期为10-15。')['reasons'])
                self.assertEqual(checks(label+'最低余额879元，日期为11-01。')['checks'][0]['status'],'VERIFIED')

    def test_bare_conservative_without_scope_is_uncovered(self):
        for label in ('保守计入今日','保守估计','保守处理','保守'):
            with self.subTest(label=label):
                self.assertEqual(checks(label+'，最低余额879元。')['checks'][0]['status'],'NOT_COVERED_SCENARIO')

    def test_cross_sentence_scope_preserved(self):
        result=checks('主预测。旧安排保守计入今日。最低余额2789元，日期为10-15。')
        self.assertEqual(result['checks'][0]['status'],'VERIFIED')

    def test_blank_paragraph_still_resets(self):
        result=checks('主预测。\n\n旧安排保守计入今日。最低余额2789元。')
        self.assertEqual(result['checks'][0]['status'],'NOT_COVERED_SCENARIO')

    def test_two_scenarios_can_be_compared(self):
        result=checks('主预测，旧安排保守计入今日，最低余额2789元，日期为10-15；保守情景，旧安排仍保守处理，最低余额879元，日期为11-01。')
        self.assertEqual([x['status'] for x in result['checks']],['VERIFIED','VERIFIED'])
        self.assertEqual([x['scenario'] for x in result['checks']],['PRIMARY','WITHOUT_FUTURE_INCOME'])

    def test_mixed_scope_remains_ambiguous(self):
        self.assertEqual(checks('主预测和保守口径最低余额879元。')['checks'][0]['status'],'NOT_COVERED_SCENARIO')

if __name__=='__main__':unittest.main()
