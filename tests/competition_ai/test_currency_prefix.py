"""Finite explicit-RMB amounts; only local synthetic tool responses."""
from copy import deepcopy
import unittest
from src.bank_v1_web import bank_answer as gate


def response(data=None, *, currency='CNY', unit='minor', tool='accounts', ok=True):
    return {'kind':'response','tool':tool,'ok':ok,'response':{'scope':'bank','currency':currency,
            'amount_unit':unit,'evidence_id':'synthetic:currency', 'data': data or {}}}


def evaluate(text, records=(), prompt=''):
    return gate.validate_bank_answer({'ok':True,'text':text,'trace':list(records)},prompt)


def balance(value=6189, **options):
    return response({'balance_minor':value},**options)


def calculation(value=-6189, currency='CNY'):
    return response({'calculation':{'inputs':{'left_minor':9000},
        'result':{'kind':'amount','amount_minor':value,'unit':'minor'}}},currency=currency,tool='money_calculate')


def task_record(*, state='SUCCEEDED', currency='CNY', tool='money_calculate'):
    identifier='a'*32
    result=calculation(currency=currency)['response'] if tool=='money_calculate' else balance()['response']
    node={'id':'read','tool':tool,'state':state,'attempt':1,'error':None,
          'execution_key':'task:'+identifier+':v1:read','result':result,'args':{}}
    task={'task_id':identifier,'scope':'bank','subject_id':'U1','plan_version':1,'nodes':[node]}
    record=response({'task':task},tool='task_status');record['args']={'task_id':identifier}
    return record


class CurrencyPrefixTests(unittest.TestCase):
    def test_no_source_rejects_explicit_rmb(self):
        for text in ['余额CNY61.89。','余额RMB61.89元。','余额人民币61.89元。','余额CNY0。']:
            with self.subTest(text=text):self.assertFalse(evaluate(text)['ok'])

    def test_same_value_and_currency_support(self):
        for text in ['余额CNY61.89。','余额RMB61.89元。','余额人民币61.89元。','余额CNY 61.89。']:
            with self.subTest(text=text):self.assertTrue(evaluate(text,[balance()])['ok'])

    def test_integer_decimal_and_thousands(self):
        for token,value in [('CNY1234',123400),('RMB1,234.50',123450),('人民币0.5元',50),('CNY+61.89',6189)]:
            with self.subTest(token=token):self.assertTrue(evaluate('余额'+token+'。',[balance(value)])['ok'])

    def test_negative_sign_is_preserved(self):
        for token in ['CNY-61.89','RMB−61.89']:
            with self.subTest(token=token):
                self.assertTrue(evaluate('余额'+token+'。',[balance(-6189)])['ok'])
                self.assertFalse(evaluate('余额'+token+'。',[balance(6189)])['ok'])

    def test_negative_evidence_cannot_support_positive_balance(self):
        self.assertFalse(evaluate('余额CNY61.89元。',[balance(-6189)])['ok'])

    def test_labelled_currency_symbol_supported(self):
        for token in ['CNY¥61.89','人民币￥61.89','RMB ￥ -61.89']:
            with self.subTest(token=token):
                self.assertTrue(evaluate('余额'+token+'。',[balance(-6189 if '-' in token else 6189)])['ok'])

    def test_pre_label_sign_and_foreign_symbol_are_not_stripped(self):
        for token in ['-CNY61.89','+RMB61.89','−人民币61.89','CNY$61.89','CNY€61.89','CNY¥￥61.89']:
            with self.subTest(token=token):self.assertFalse(evaluate(token,[balance(6189),balance(-6189)])['ok'])

    def test_invalid_numeric_format_cannot_fallback(self):
        for token in ['CNY1,23.45','CNY61.890','CNY--61.89','CNY61..89','CNY1e3','CNY61.89x',
                      'CNY.50','CNY1, 234.50','CNY61.89%','CNY61.89美元']:
            with self.subTest(token=token):self.assertFalse(evaluate(token,[balance(6189),balance(100),balance(123450)])['ok'])

    def test_iso_dates_are_not_year_amounts(self):
        for text in ['标签CNY2026-10-16','标签CNY2026/10/16']:
            with self.subTest(text=text):self.assertTrue(evaluate(text)['ok'])

    def test_embedded_currency_codes_and_identifiers_not_new_money(self):
        for text in ['ABCNY6189','op_CNY6189','RMB_model_6189','CNY_6189','CNY61_89']:
            with self.subTest(text=text):self.assertTrue(evaluate(text)['ok'])

    def test_foreign_unknown_or_wrong_unit_cannot_prove_cny(self):
        for currency,unit in [('USD','minor'),(None,'minor'),('CNY','yuan')]:
            with self.subTest(currency=currency,unit=unit):self.assertFalse(evaluate('CNY61.89',[balance(currency=currency,unit=unit)])['ok'])

    def test_failed_and_wrong_tool_are_not_sources(self):
        self.assertFalse(evaluate('CNY61.89',[balance(ok=False)])['ok'])
        self.assertFalse(evaluate('CNY61.89',[balance(tool='untrusted_tool')])['ok'])

    def test_nested_foreign_currency_does_not_inherit(self):
        for currency in ['USD',None]:
            with self.subTest(currency=currency):
                r=response({'item':{'currency':currency,'balance_minor':6189}})
                self.assertFalse(evaluate('CNY61.89',[r])['ok'])

    def test_description_args_and_inputs_not_amount_source(self):
        for data in [{'description':'CNY61.89'}, {'args':{'balance_minor':6189}},
                     {'inputs':{'balance_minor':6189}}, {'plan_history':[{'balance_minor':6189}]}]:
            with self.subTest(data=data):self.assertFalse(evaluate('CNY61.89',[response(data)])['ok'])

    def test_explicit_user_assumption_repeat_not_new_result(self):
        self.assertTrue(evaluate('假设支出CNY61.89。',prompt='假设支出RMB61.89')['ok'])
        self.assertFalse(evaluate('假设支出CNY62.89。',prompt='假设支出RMB61.89')['ok'])
        self.assertTrue(evaluate('假设支出61.89元。',prompt='假设支出CNY61.89')['ok'])

    def test_direct_yuan_assumption_compatible(self):
        self.assertTrue(evaluate('假设支出CNY61.89。',prompt='假设支出61.89元')['ok'])
        self.assertTrue(evaluate('假设支出61.89元。',prompt='假设支出61.89元')['ok'])

    def test_foreign_and_unknown_assumptions_not_promoted(self):
        for prompt in ['假设USD61.89','假设USD 61.89元','假设美元61.89元','假设61.89美元',
                       '假设61.89','未知币种61.89元','假设$61.89元']:
            with self.subTest(prompt=prompt):self.assertFalse(evaluate('CNY61.89',prompt=prompt)['ok'])

    def test_malformed_assumption_does_not_seed_valid_value(self):
        self.assertFalse(evaluate('CNY61.89',prompt='假设CNY--61.89')['ok'])

    def test_deficit_prefix_and_suffix_keep_numeric_position(self):
        for text in ['缺口CNY61.89元。','出现RMB61.89元的缺口。','预留后再出现人民币61.89元缺口。']:
            with self.subTest(text=text):self.assertTrue(evaluate(text,[balance(-6189)])['ok'])

    def test_original_independent_cny_wrong_subject(self):
        self.assertFalse(evaluate('还款金额为CNY61.89元的缺口。',[balance(-6189)])['ok'])

    def test_calculation_uses_envelope_currency_not_missing_result_field(self):
        self.assertTrue(evaluate('缺口CNY61.89元。',[calculation()])['ok'])
        self.assertFalse(evaluate('缺口CNY61.89元。',[calculation(currency=None)])['ok'])
        self.assertFalse(evaluate('CNY90.00',[calculation()])['ok'])

    def test_policy_is_not_actual_balance(self):
        r=response({'analysis_policy':{'category_increase':{'minimum_absolute_increase_minor':10000}}},tool='billing_report')
        self.assertTrue(evaluate('规则阈值为CNY100元。',[r])['ok'])
        self.assertFalse(evaluate('实际余额CNY100元。',[r])['ok'])

    def test_multiple_occurrences_and_false_action_remain_checked(self):
        self.assertFalse(evaluate('余额CNY61.89，另有RMB999.99。',[balance()])['ok'])
        self.assertFalse(evaluate('缺口CNY61.89元，转账已经成功。',[balance(-6189)])['ok'])

    def test_task_successful_current_result(self):
        self.assertTrue(evaluate('缺口CNY61.89元。',[task_record()])['ok'])
        self.assertTrue(evaluate('余额CNY61.89元。',[task_record(tool='accounts')])['ok'])

    def test_task_state_and_unknown_currency_rejected(self):
        for state in ['FAILED','PENDING','RUNNING']:
            with self.subTest(state=state):self.assertFalse(evaluate('缺口CNY61.89元。',[task_record(state=state)])['ok'])
        self.assertFalse(evaluate('缺口CNY61.89元。',[task_record(currency=None)])['ok'])

    def test_task_latest_failure_invalidates_old_result(self):
        old=task_record();bad={'kind':'response','tool':'task_status','ok':False,'args':{'task_id':'a'*32}}
        self.assertFalse(evaluate('缺口CNY61.89元。',[old,bad])['ok'])

    def test_task_owner_key_and_history_not_borrowed(self):
        for mutation in ['owner','key','attempt','history']:
            r=task_record();task=r['response']['data']['task']
            if mutation=='owner':task['subject_id']='U2'
            elif mutation=='key':task['nodes'][0]['execution_key']='task:wrong:v1:read'
            elif mutation=='attempt':task['nodes'][0]['attempt']=0
            else:task['plan_history']=[deepcopy(task['nodes'])];task['nodes']=[]
            with self.subTest(mutation=mutation):self.assertFalse(evaluate('缺口CNY61.89元。',[r])['ok'])

    def test_native_numeric_match_offsets_and_overlap(self):
        text='余额CNY-61.89元。'
        tokens=gate.amount_mentions(text)
        self.assertEqual(1,len(tokens))
        item=tokens[0];m=item['match']
        self.assertEqual('-61.89',m.group())
        self.assertEqual('-61.89',text[m.start():m.end()])
        self.assertEqual('RMB_PREFIX',item['origin'])
        self.assertEqual('VALID',item['status'])



class PendingCurrencyTests(unittest.TestCase):
    def pending(self, strong=False):
        r=task_record(state='WAITING_VERIFICATION' if strong else 'WAITING_CONFIRMATION')
        node=r['response']['data']['task']['nodes'][0]
        node.update(tool='prepare_transfer', operation_id='op_pending123')
        node['result']=response({'operation':{'operation_id':'op_pending123','kind':'transfer',
            'status':'PENDING','required_verification':'STRONG' if strong else 'STANDARD',
            'payload':{'amount_minor':6189}}},tool='prepare_transfer')['response']
        return r

    def test_pending_operation_amount_is_not_bank_success(self):
        for strong in (False, True):
            r=self.pending(strong)
            self.assertTrue(evaluate('待确认金额CNY61.89元。',[r])['ok'])
            self.assertFalse(evaluate('转账已成功，金额CNY61.89元。',[r])['ok'])

    def test_pending_node_requires_matching_operation(self):
        for field,value in [('operation_id','op_wrong123'),('kind','mandate_cancel'),('status','FAILED')]:
            r=self.pending();r['response']['data']['task']['nodes'][0]['result']['data']['operation'][field]=value
            with self.subTest(field=field):self.assertFalse(evaluate('金额CNY61.89元。',[r])['ok'])
        r=task_record(state='WAITING_CONFIRMATION',tool='accounts')
        self.assertFalse(evaluate('金额CNY61.89元。',[r])['ok'])

    def test_foreign_task_envelope_cannot_inherit_child_cny(self):
        for currency in ('USD',None):
            r=task_record();r['response']['currency']=currency
            self.assertFalse(evaluate('缺口CNY61.89元。',[r])['ok'])

    def test_ascii_punctuation_does_not_hide_explicit_label(self):
        for separator in (',','.',';'):
            self.assertFalse(evaluate('说明'+separator+'CNY61.89元。')['ok'])
            self.assertTrue(evaluate('说明'+separator+'CNY61.89元。',[balance()])['ok'])



class ScopeAndAssumptionTests(unittest.TestCase):
    def projection(self):
        return {'scope':'COMBINED_ACCOUNTS','as_of_date':'2026-10-16','through_date':'2026-10-16',
            'current_balance_minor':28740,'daily':[{'date':'2026-10-16','expected_income_minor':0,
            'planned_outflow_minor':0,'proposed_expense_minor':0,'projected_balance_minor':28740,
            'without_future_income_minor':28740}]}

    def test_explicit_nested_permission_scope_is_not_inherited(self):
        for scope in ('personal',None,'unknown'):
            r=response({'nested':{'scope':scope,'balance_minor':28740}})
            with self.subTest(scope=scope):self.assertFalse(evaluate('余额CNY287.40元。',[r])['ok'])
        self.assertTrue(evaluate('余额CNY287.40元。',[response({'nested':{'scope':'bank','balance_minor':28740}})])['ok'])

    def test_forecast_known_domain_scope_direct_and_task(self):
        r=response(self.projection(),tool='forecast')
        self.assertTrue(evaluate('合计余额CNY287.40元。',[r])['ok'])
        task=task_record(tool='forecast');task['response']['data']['task']['nodes'][0]['result']=r['response']
        self.assertTrue(evaluate('合计余额CNY287.40元。',[task])['ok'])

    def test_event_forecast_path_and_unknown_nested_scope(self):
        projection=self.projection()
        self.assertTrue(evaluate('合计余额CNY287.40元。',[response({'forecast':projection},tool='event_context')])['ok'])
        for tool,data in [('accounts',projection),('forecast',{'nested':projection}),('event_context',{'nested':projection})]:
            with self.subTest(tool=tool,data=data):self.assertFalse(evaluate('余额CNY287.40元。',[response(data,tool=tool)])['ok'])

    def test_forecast_structure_is_required_not_string_allowlist(self):
        for key,value in [('as_of_date','invalid'),('through_date','2026-10-15'),('daily',None),('current_balance_minor',True)]:
            p=self.projection();p[key]=value
            with self.subTest(key=key):self.assertFalse(evaluate('余额CNY287.40元。',[response(p,tool='forecast')])['ok'])
        p=self.projection();p['daily'][0]['date']='invalid'
        self.assertFalse(evaluate('余额CNY287.40元。',[response(p,tool='forecast')])['ok'])

    def test_minima_scope_requires_typed_contract(self):
        p=self.projection();p['scenario_minima']={'scope':'COMBINED_ACCOUNTS','balance_minor':6189}
        self.assertFalse(evaluate('余额CNY61.89元。',[response(p,tool='forecast')])['ok'])
        p['scenario_minima']={'contract_version':1,'scope':'COMBINED_ACCOUNTS','currency':'CNY',
          'date_basis':'BUSINESS_DATE_UTC_PLUS_08','selection_rule':'FIRST_MATCHING_DAILY_DATE_ELSE_CURRENT_SNAPSHOT',
          'window_start':p['as_of_date'],'window_end':p['through_date'],
          'PRIMARY':{'minimum_balance_minor':6189,'minimum_balance_date':p['as_of_date'],
                     'minimum_point':'CURRENT_SNAPSHOT','future_income':'ASSUMED_FROM_ACTIVE_NON_OVERDUE_ARRANGEMENTS'},
          'WITHOUT_FUTURE_INCOME':{'minimum_balance_minor':6189,'minimum_balance_date':p['as_of_date'],
                     'minimum_point':'CURRENT_SNAPSHOT','future_income':'EXCLUDED'}}
        self.assertTrue(evaluate('金额CNY61.89元。',[response(p,tool='forecast')])['ok'])

    def test_foreign_unknown_label_colon_not_cny_assumption(self):
        for prompt in ('假设USD：287.40元','假设USD : 287.40元','假设美元\t：\t287.40元',
                       '假设未知币种：287.40元','假设币种未知:287.40元'):
            with self.subTest(prompt=prompt):self.assertFalse(evaluate('假设CNY287.40元。',prompt=prompt)['ok'])

    def test_currency_exclusion_does_not_cross_clause(self):
        for prompt in ('说明USD。假设287.40元','说明USD\n假设287.40元','假设人民币287.40元',
                       '假设支出：287.40元'):
            with self.subTest(prompt=prompt):self.assertTrue(evaluate('假设CNY287.40元。',prompt=prompt)['ok'])

    def test_nested_scope_exclusion_preserves_sibling(self):
        data={'balance_minor':6189,'nested':{'scope':None,'balance_minor':28740}}
        self.assertTrue(evaluate('余额CNY61.89元。',[response(data)])['ok'])
        self.assertFalse(evaluate('余额CNY287.40元。',[response(data)])['ok'])

if __name__=='__main__':unittest.main()
