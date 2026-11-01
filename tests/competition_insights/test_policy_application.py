"""Report-level policy object scope, without models or real banking."""
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from src.bank_insights.rules import build_report
from src.bank_insights.policy import default_policy
from src.bank_insights import InsightService
from src.bank_insights.fixture import create_demo


def row(identifier, amount, day, description='虚构项目', kind='expense', category=None):
    return {'id': identifier, 'amount_minor': amount, 'date': day, 'description': description,
            'kind': kind, 'category': category or kind, 'account_id': 'A', 'source': 'SYNTHETIC'}


def report(rows, **options):
    return build_report(rows, '2026-09-01', '2026-09-30', currency='CNY', **options)


class PolicyApplicationTests(unittest.TestCase):
    def test_shared_scope_and_paths(self):
        data=report([])
        scope=data['policy_application']['category_increase']
        self.assertEqual(1,data['policy_application']['contract_version'])
        self.assertEqual('CATEGORY_AGGREGATED_DEBIT',scope['scope'])
        self.assertEqual(['category'],scope['group_by'])
        self.assertEqual('SUM_OF_OUTGOING_MINOR',scope['amount_basis'])
        self.assertEqual('analysis_policy.category_increase',scope['policy_path'])
        self.assertEqual('category_changes[*].significant_increase_candidate',scope['result_path'])
        self.assertEqual(['DESCRIPTION_GROUP','SINGLE_TRANSACTION','RECURRING_SEQUENCE'],scope['not_applicable_to'])
        self.assertEqual('EVALUATED',scope['status'])
        self.assertEqual('NOT_PERFORMED',data['policy_application']['description_comparison']['threshold_assessment'])
        self.assertEqual(2,data['report_contract_version'])

    def test_not_requested_is_not_evaluated_false(self):
        data=report([],compare_previous=False)
        self.assertEqual('NOT_REQUESTED',data['policy_application']['category_increase']['status'])
        self.assertEqual([],data['category_changes'])
        self.assertEqual('NOT_PERFORMED',data['policy_application']['description_comparison']['threshold_assessment'])

    def test_description_change_cannot_inherit_category_threshold(self):
        data=report([row('old-other',-700000,'2026-08-10','其他'),row('old-small',-2000,'2026-08-10','会员'),
                     row('new-other',-700000,'2026-09-10','其他'),row('new-small',-2500,'2026-09-10','会员')])
        self.assertFalse(data['category_changes'][0]['significant_increase_candidate'])
        item=next(x for x in data['description_comparison']['items'] if x['description']=='会员')
        self.assertNotIn('significant_increase_candidate',item)
        self.assertEqual('NOT_PERFORMED',data['policy_application']['description_comparison']['threshold_assessment'])
        self.assertNotIn('candidates',data)

    def test_single_group_coincidence_does_not_change_scope(self):
        data=report([row('old',-10000,'2026-08-10'),row('new',-20000,'2026-09-10')])
        self.assertTrue(data['category_changes'][0]['significant_increase_candidate'])
        self.assertEqual('CATEGORY_AGGREGATED_DEBIT',data['policy_application']['category_increase']['scope'])
        self.assertNotIn('significant_increase_candidate',data['description_comparison']['items'][0])

    def test_rule_explicit_scope_and_and_threshold_preserved(self):
        for current,expected in [(15000,False),(20000,True)]:
            with self.subTest(current=current):
                data=report([row('old',-10000,'2026-08-10'),row('new',-current,'2026-09-10')])
                change=data['category_changes'][0]
                self.assertEqual(expected,change['significant_increase_candidate'])
                self.assertTrue(change['rule'].startswith('仅针对该类别汇总出账；'))
                self.assertIn('不是用户预算或欺诈判断',change['rule'])

    def test_configurable_values_remain_single_policy_source(self):
        policy=default_policy(); policy['category_increase']['minimum_absolute_increase_minor']=5000
        policy['category_increase']['minimum_increase_percent']=30
        data=report([row('old',-10000,'2026-08-10'),row('new',-15000,'2026-09-10')],policy=policy)
        self.assertTrue(data['category_changes'][0]['significant_increase_candidate'])
        self.assertEqual(policy,data['analysis_policy'])
        self.assertNotIn('minimum_increase_percent',data['policy_application']['category_increase'])

    def test_zero_previous_and_credit_only_are_not_increases(self):
        data=report([row('refund',50000,'2026-09-10',kind='refund')])
        self.assertFalse(data['category_changes'][0]['significant_increase_candidate'])
        self.assertEqual(0,data['category_changes'][0]['current_debit_minor'])

    def test_transfer_is_not_relabelled_as_consumption(self):
        data=report([row('old',-10000,'2026-08-10',kind='TRANSFER_OUT'),row('new',-20000,'2026-09-10',kind='TRANSFER_OUT')])
        self.assertEqual(0,data['totals']['expense_minor'])
        self.assertEqual('TRANSFER_OUT',data['category_changes'][0]['category'])
        self.assertTrue(data['category_changes'][0]['significant_increase_candidate'])

    def test_separate_calls_do_not_share_mutable_metadata(self):
        first=report([]); first['policy_application']['category_increase']['group_by'].append('wrong')
        self.assertEqual(['category'],report([])['policy_application']['category_increase']['group_by'])

    def test_actual_readonly_service_returns_scope(self):
        now=datetime(2026,10,12,tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); bank=create_demo(root/'bank.db',clock=lambda:now)
            service=InsightService(root/'insights.db',bank=bank,clock=lambda:now)
            before=(root/'bank.db').read_bytes()
            response=service.call_tool('billing_report',{'start_date':'2026-09-01','end_date':'2026-09-30'})
            self.assertEqual('EVALUATED',response['data']['report']['policy_application']['category_increase']['status'])
            self.assertEqual(before,(root/'bank.db').read_bytes())
            self.assertTrue(response['evidence_id'])


if __name__ == '__main__': unittest.main()
