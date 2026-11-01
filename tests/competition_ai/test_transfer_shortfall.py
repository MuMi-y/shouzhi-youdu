"""Real temporary tools: preflight refusal without preparing a bank operation."""
from copy import deepcopy
import json
import unittest

from tests.competition_ai import test_dialogue_parts as fixture
from src.bank_v1_web.bank_answer import validate_bank_answer
from src.bank_evidence.service_answer import recheck


class TransferShortfallTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.DialoguePartsTests();self.f.setUp();self.addCleanup(self.f.doCleanups)

    def calculation(self, *, entity='B', delta=13725, reverse=False, operation='subtract'):
        body=self.f.f.tool('accounts',{})
        fact=next(f for f in body['fact_catalog']['facts'] if f['entity']==entity)
        proposed=fact['value']+delta
        self.prompt=f"考虑从{entity}转{proposed//100}.{proposed%100:02d}元，请先判断。"
        bank={'kind':'bank_fact','tool':'accounts','arguments':{},'fact_id':fact['fact_id']}
        assumed={'kind':'user_assumption','quote':self.prompt}
        args={'operation':operation,'currency':'CNY','left_minor':proposed,'left_source':assumed,
              'right_minor':fact['value'],'right_source':bank}
        if reverse:
            args['left_minor'],args['right_minor']=args['right_minor'],args['left_minor']
            args['left_source'],args['right_source']=args['right_source'],args['left_source']
        calc=self.f.f.tool('money_calculate',args)
        return {'format':'bank_transfer_shortfall_v1','calculation_id':calc['evidence_id']}

    def answer(self, part, prompt=None):
        return validate_bank_answer({'ok':True,'text':json.dumps({'format':'bank_service_v1','parts':[part]}),
                                     'trace':self.f.f.trace},prompt or self.prompt)

    def test_both_subtraction_orders_deliver_account_shortfall_without_prepare(self):
        before=self.f.f.service.bank.state()
        for reverse in (False,True):
            part=self.calculation(reverse=reverse)
            actual=self.answer(part)
            self.assertTrue(actual['ok'],actual)
            self.assertIn('不足137.25元',actual['text'])
            self.assertIn('若从B账户',actual['text'])
            self.assertIn('当前余额不能支持这笔调拨',actual['text'])
            self.assertTrue(recheck(actual,self.f.f.service.call_tool)['ok'])
        self.assertEqual(before,self.f.f.service.bank.state())
        self.assertFalse(any(r.get('tool')=='prepare_transfer' for r in self.f.f.trace))

    def test_other_account_and_absolute_difference(self):
        part=self.calculation(entity='A',delta=20001,operation='absolute_difference')
        result=self.answer(part)
        self.assertTrue(result['ok'],result)
        self.assertIn('若从A账户',result['text']);self.assertIn('不足200.01元',result['text'])

    def test_enough_or_equal_balance_does_not_authorize_shortfall(self):
        for delta in (-1,0):self.assertFalse(self.answer(self.calculation(delta=delta))['ok'])

    def test_combined_accounts_cannot_stand_in_for_source_account(self):
        self.assertFalse(self.answer(self.calculation(entity='COMBINED_ACCOUNTS'))['ok'])

    def test_wrong_operation_and_unbound_user_amount_rejected(self):
        self.assertFalse(self.answer(self.calculation(operation='add'))['ok'])
        part=self.calculation()
        self.assertFalse(self.answer(part,'请查询账户，不涉及该金额')['ok'])

    def test_invented_reference_or_extra_amount_rejected(self):
        part=self.calculation();bad=deepcopy(part);bad['calculation_id']='calculation:invented'
        self.assertFalse(self.answer(bad)['ok'])
        bad=deepcopy(part);bad['amount_minor']=100
        self.assertFalse(self.answer(bad)['ok'])

    def test_mutated_result_or_entity_rejected(self):
        part=self.calculation();original=deepcopy(self.f.f.trace)
        for mutate in ('amount','entity'):
            self.f.f.trace=deepcopy(original)
            calc=self.f.f.trace[-1]['response']['data']['calculation']
            if mutate=='amount':calc['result']['amount_minor']+=1
            else:calc['input_provenance']['right']['fact']['entity']='A'
            self.assertFalse(self.answer(part)['ok'])

    def test_balance_change_rejects_old_conclusion_without_replaying_write(self):
        part=self.calculation();result=self.answer(part);self.assertTrue(result['ok'])
        self.f.f.service.bank.user_action('inject_transaction',{'kind':'income','account_id':'B',
            'amount_minor':100,'description':'虚构变动','idempotency_key':'shortfall-change'})
        calls=[]
        def read(name,args):
            calls.append(name);return self.f.f.service.call_tool(name,args)
        self.assertFalse(recheck(result,read)['ok'])
        self.assertEqual(['accounts'],calls)
        self.f.f.tool('accounts',{})
        self.assertFalse(self.answer(part)['ok'])

    def test_preflight_cannot_hide_actual_failed_bank_attempt(self):
        part=self.calculation();self.f.fail()
        result=self.answer(part)
        self.assertFalse(result['ok'])
        self.assertEqual('OMITTED_BANK_TOOL_FAILURE',result['answer_validation']['reason'])
