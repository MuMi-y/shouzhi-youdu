"""Isolated reserve semantics; never modifies historical evidence."""
import unittest
from tests.test_plan_window import PlanWindowTests

class ReserveTests(unittest.TestCase):
    setUp=PlanWindowTests.setUp
    day=PlanWindowTests.day
    def test_missing_and_explicit_goal_are_distinct(self):
        before=self.client.get('/api/competition/plans').json
        self.assertIsNone(before['forecast']['reserve_assessment'])
        bank_before=self.bank.state()
        profile={'title':'学习电脑','user_reason':'学习','priority_reason':'先还款','preferred_amount_minor':20000,
          'min_amount_minor':10000,'reserve_floor_minor':10000000,'preferred_date':self.day(20),'latest_date':self.day(25)}
        goals=self.app.extensions['competition'].goals
        goals.action('save',{'expected_revision':0,'profile':profile})
        first=self.client.get('/api/competition/plans').json['forecast'];r=first['reserve_assessment']
        self.assertEqual(r['revision'],1);self.assertEqual(r['title'],'学习电脑')
        self.assertEqual(r['shortfall_minor'],max(0,10000000-first['lowest_balance_minor']))
        self.assertEqual(first['gap_minor'],max(0,-first['lowest_balance_minor']))
        self.assertGreater(r['shortfall_minor'],first['gap_minor'])
        goals.action('save',{'expected_revision':1,'profile':{**profile,'reserve_floor_minor':0}})
        second=self.client.get('/api/competition/plans').json['forecast']['reserve_assessment']
        self.assertEqual(second['revision'],2);self.assertEqual(second['floor_minor'],0)
        self.assertEqual(bank_before['accounts'],self.bank.state()['accounts'])
        self.assertEqual(bank_before['arrangements'],self.bank.state()['arrangements'])
        spend=self.client.post('/api/competition/plans/spending',json={'amount_minor':100,'due_date':self.day(0)},headers=self.headers).json
        self.assertEqual(spend['after']['reserve_assessment']['revision'],2)
        self.assertEqual(spend['after']['reserve_assessment']['without_income_shortfall_minor'],max(0,-spend['after']['without_future_income_lowest_balance_minor']))

if __name__=='__main__':unittest.main()
