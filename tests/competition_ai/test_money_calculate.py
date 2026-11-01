"""Independent draft checks; literal expected amounts, no model or bank writes."""
from copy import deepcopy
from decimal import getcontext, localcontext, Inexact, ROUND_UP
import json
from pathlib import Path
import sys
import unittest

from src.competition_web.money_calculate import calculate, CalculationError, tool_spec, MAX_INPUT_MINOR


def result(operation, left, right, **extra):
    return calculate(dict(operation=operation, left_minor=left, right_minor=right, **extra))['data']['calculation']['result']


class CalculationTests(unittest.TestCase):
    def test_exact_addition_does_not_use_binary_float(self):
        self.assertEqual({'kind':'amount','amount_minor':30,'amount':'0.30','percent':None,
                          'unit':'minor','formula':'left_minor + right_minor'}, result('add',10,20))

    def test_signed_subtraction_and_absolute_difference_have_different_meaning(self):
        self.assertEqual(-250, result('subtract',100,350)['amount_minor'])
        self.assertEqual('-2.50', result('subtract',100,350)['amount'])
        self.assertEqual(250, result('absolute_difference',100,350)['amount_minor'])

    def test_refund_or_credit_can_be_negative_without_silent_abs(self):
        self.assertEqual(-725, result('add',-1000,275)['amount_minor'])
        self.assertEqual('-7.25', result('add',-1000,275)['amount'])

    def test_percentage_is_ratio_not_percentage_change(self):
        self.assertEqual('125.00', result('percentage',12500,10000)['percent'])
        difference = result('subtract',12500,10000)['amount_minor']
        self.assertEqual('25.00', result('percentage',difference,10000)['percent'])

    def test_repeating_fraction_rounding(self):
        self.assertEqual('33.33', result('percentage',1,3)['percent'])
        self.assertEqual('66.67', result('percentage',2,3)['percent'])

    def test_half_even_ties_both_directions(self):
        self.assertEqual('3.12', result('percentage',1,32)['percent'])
        self.assertEqual('9.38', result('percentage',3,32)['percent'])

    def test_negative_percentage_is_not_clamped(self):
        self.assertEqual('-25.00', result('percentage',-1,4)['percent'])
        self.assertEqual('-25.00', result('percentage',1,-4)['percent'])
        self.assertEqual('25.00', result('percentage',-1,-4)['percent'])

    def test_zero_numerator_and_negative_rounded_zero(self):
        self.assertEqual('0.00', result('percentage',0,7)['percent'])
        self.assertEqual('0.00', result('percentage',-1,100000)['percent'])

    def test_zero_denominator_is_undefined_not_zero_percent(self):
        for left in (0,1,-1):
            with self.subTest(left=left), self.assertRaises(CalculationError) as caught:
                result('percentage',left,0)
            self.assertEqual('ZERO_DENOMINATOR',caught.exception.code)
        self.assertEqual(5,result('add',5,0)['amount_minor'])

    def test_global_decimal_precision_rounding_and_traps_do_not_leak(self):
        with localcontext() as context:
            context.prec=2;context.rounding=ROUND_UP;context.traps[Inexact]=True
            self.assertEqual('33.33', result('percentage',1,3)['percent'])
            self.assertEqual('3.12', result('percentage',1,32)['percent'])
            self.assertEqual('20000000000.00',result('add',MAX_INPUT_MINOR,MAX_INPUT_MINOR)['amount'])
            self.assertEqual(2,getcontext().prec)
            self.assertTrue(getcontext().traps[Inexact])

    def test_boundaries_and_large_percent_remain_json_safe(self):
        self.assertEqual(2000000000000,result('add',MAX_INPUT_MINOR,MAX_INPUT_MINOR)['amount_minor'])
        self.assertEqual(2000000000000,result('absolute_difference',MAX_INPUT_MINOR,-MAX_INPUT_MINOR)['amount_minor'])
        self.assertEqual('100000000000000.00',result('percentage',MAX_INPUT_MINOR,1)['percent'])
        for number in (MAX_INPUT_MINOR+1,-MAX_INPUT_MINOR-1):
            with self.assertRaises(CalculationError):result('add',number,0)

    def test_rejects_booleans_floats_numeric_strings_and_nonfinite(self):
        for value in (True,False,1.0,'100',None,float('nan'),float('inf'),[],{}):
            with self.subTest(value=repr(value)), self.assertRaises(CalculationError):result('add',value,1)
            with self.subTest(right=repr(value)), self.assertRaises(CalculationError):result('add',1,value)

    def test_rejects_eval_and_extra_claims(self):
        for extra in ({'expression':'1+2'},{'financial_inputs_verified':True},{'scope':'moneydata'}, {'rounding':'UP'}):
            with self.assertRaises(CalculationError):result('add',1,2,**extra)
        for operation in ('eval','__import__("os")','multiply','percentage_change','ADD',None):
            with self.assertRaises(CalculationError):result(operation,1,2)

    def test_shape_and_missing_parameters(self):
        for args in ([],None,'{}',{}, {'operation':'add','left_minor':1}):
            with self.assertRaises(CalculationError):calculate(args)

    def test_unknown_currency_stays_unknown(self):
        response=calculate({'operation':'add','left_minor':100,'right_minor':50})
        self.assertIsNone(response['currency'])
        self.assertFalse(response['data']['calculation']['financial_inputs_verified'])
        for value in ('USD','cny',100,False):
            with self.assertRaises(CalculationError):result('add',1,2,currency=value)

    def test_declared_bank_looking_reference_never_verifies_financial_fact(self):
        response=calculate({'operation':'subtract','left_minor':900000,'right_minor':1,
                            'currency':'CNY','left_evidence_id':'sha256:pretend-bank-evidence'})
        calculation=response['data']['calculation']
        self.assertTrue(calculation['calculation_verified'])
        self.assertFalse(calculation['financial_inputs_verified'])
        self.assertEqual('DECLARED_NOT_RESOLVED',calculation['source_references_status'])
        self.assertEqual('sha256:pretend-bank-evidence',calculation['inputs']['left_evidence_id'])
        self.assertTrue(response['evidence_id'].startswith('calculation:'))

    def test_source_reference_is_bounded_and_not_interpreted(self):
        for value in ('', 'x'*201, 'bad\nlabel', '\x00', '\x7f', [], True):
            with self.assertRaises(CalculationError):result('add',1,2,left_evidence_id=value)
        value='__import__("os")'
        calculation=calculate({'operation':'add','left_minor':1,'right_minor':2,'left_evidence_id':value})
        self.assertEqual(value,calculation['data']['calculation']['inputs']['left_evidence_id'])

    def test_inputs_not_mutated_results_are_fresh_and_hash_is_reproducible(self):
        args={'operation':'add','left_minor':100,'right_minor':200}
        snapshot=deepcopy(args);a=calculate(args);b=calculate(dict(reversed(list(args.items()))))
        self.assertEqual(snapshot,args);self.assertEqual(a,b)
        a['data']['calculation']['result']['amount_minor']=999
        self.assertEqual(300,calculate(args)['data']['calculation']['result']['amount_minor'])
        self.assertNotEqual(b['evidence_id'],calculate({**args,'right_evidence_id':'declared'})['evidence_id'])
        json.dumps(b,allow_nan=False)

    def test_spec_calls_out_ratio_and_unverified_sources(self):
        schema=tool_spec();self.assertEqual('money_calculate',schema['name'])
        self.assertEqual(['left_minor','operation','right_minor'],schema['parameters']['required'])
        self.assertFalse(schema['parameters']['additionalProperties'])
        self.assertIn('来源编号仅声明不验证',schema['description'])
        schema['parameters']['properties']['left_minor']['maximum']=0
        self.assertEqual(MAX_INPUT_MINOR,tool_spec()['parameters']['properties']['left_minor']['maximum'])


if __name__=='__main__':
    unittest.main(verbosity=2)
