"""Failed read-only analysis remains a failure, with a useful and truthful next step."""
from copy import deepcopy
import unittest
from src.bank_requests.response import rejection, recovery_view


def trace(tool='forecast', args=None):
    args = args if args is not None else {'proposed_expense_minor': 150000}
    return [{'kind':'request','call_id':'c1','name':tool,'arguments':args},
            {'kind':'response','call_id':'c1','tool':tool,'args':args,'ok':True,
             'status':200,'response':{'scope':'bank','data':{}}}]


class FailureMessages(unittest.TestCase):
    def test_complete_readonly_analysis_explains_failure_without_claiming_bank_refusal(self):
        result = rejection({'ok':True,'trace':trace()}, 'UNBOUND_SLOT_AMOUNT')
        self.assertFalse(result['ok'])
        self.assertEqual('REJECTED', result['answer_validation']['status'])
        self.assertEqual('UNBOUND_SLOT_AMOUNT', result['answer_validation']['reason'])
        self.assertNotIn('没有发起付款', result['error'])
        self.assertIn('不是银行的拒付回执', result['error'])
        self.assertIn('试算一笔日常支出', result['error'])
        self.assertNotIn('可以购买', result['error'])

    def test_persisted_failure_projection_does_not_rewrite_raw_record(self):
        raw = {'ok':False,'error':'old internal message','trace':trace(),
               'answer_validation':{'status':'REJECTED','reason':'UNBOUND_SLOT_AMOUNT'}}
        before=deepcopy(raw)
        view=recovery_view(raw)
        self.assertEqual(raw,before)
        self.assertNotEqual(view['error'],raw['error'])
        self.assertFalse(view['ok'])
        self.assertEqual(view['answer_validation'],raw['answer_validation'])

    def test_missing_incomplete_unknown_or_write_trace_never_claims_no_operations(self):
        for rows in [[],trace()[:1],trace('prepare_transfer'),trace('unknown'),
                     trace()+[{'kind':'request','name':'prepare_transfer','call_id':'c2','arguments':{}}]]:
            with self.subTest(rows=rows):
                result=rejection({'ok':True,'trace':rows},'UNBOUND_SLOT_AMOUNT')
                self.assertNotIn('没有发起付款',result['error'])
                self.assertNotIn('不是银行拒绝',result['error'])

    def test_billing_failure_does_not_suggest_purchasing_or_quote_unchecked_amounts(self):
        result=rejection({'ok':True,'trace':trace('billing_report',{})},'UNRESOLVED_BILLING_DATE_SOURCE')
        self.assertIn('查询分析',result['error'])
        self.assertNotIn('适合买',result['error'])
        self.assertNotIn('1500',result['error'])

    def test_other_host_activity_is_not_denied_by_read_trace_message(self):
        for disclosure in [{'complete':False,'entries':[]},
                           {'complete':True,'entries':[{'status':'UNKNOWN'}]}]:
            result=rejection({'ok':True,'trace':trace(), 'host_disclosure':disclosure},'UNBOUND_SLOT_AMOUNT')
            self.assertNotIn('没有发起付款',result['error'])
            self.assertEqual(disclosure,result['host_disclosure'])


if __name__ == '__main__':
    unittest.main()
