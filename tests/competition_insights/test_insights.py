from datetime import datetime, timedelta, timezone
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from src.bank_insights import InsightService, InsightError
from src.bank_insights.fixture import create_demo
from src.bank_insights.rules import build_report, detect_recurring


def rows(amounts=(3000, 3000, 3000), days=('2026-07-17','2026-08-17','2026-09-17'), kind='expense'):
    return [{'id': str(i), 'date': day, 'account_id': 'A', 'kind': kind, 'amount_minor': -amount,
             'description': '云存储', 'category': kind, 'source': 'SYNTHETIC'}
            for i, (day, amount) in enumerate(zip(days, amounts))]


class RuleTests(unittest.TestCase):
    def test_three_required_and_no_future(self):
        self.assertEqual([], detect_recurring(rows()[:2], as_of='2026-10-12', currency='CNY'))
        self.assertEqual([], detect_recurring(rows(), as_of='2026-09-16', currency='CNY'))
        self.assertEqual(1, len(detect_recurring(rows(), as_of='2026-09-17', currency='CNY')))

    def test_irregular_and_same_day_not_cycles(self):
        for days in [('2026-07-17','2026-07-17','2026-09-17'), ('2026-07-17','2026-07-21','2026-09-17')]:
            self.assertEqual([], detect_recurring(rows(days=days), as_of='2026-10-12', currency=None))

    def test_weekly_and_price(self):
        candidate = detect_recurring(rows((1000,1000,1200), ('2026-10-01','2026-10-08','2026-10-15')), as_of='2026-10-15', currency=None)[0]
        self.assertIsNone(candidate['currency'])
        self.assertEqual('weekly', candidate['period_kind'])
        self.assertTrue(candidate['price_increase'])
        self.assertEqual('2026-10-22', candidate['next_due_date'])

    def test_transfers_not_candidates(self):
        for kind in ['TRANSFER_OUT', 'refund', 'OPENING']:
            self.assertEqual([], detect_recurring(rows(kind=kind), as_of='2026-10-12', currency='CNY'))

    def test_report_distinguishes_refund_transfer_opening(self):
        data = rows()
        data += [{**data[0], 'id': 'r', 'kind': 'refund', 'amount_minor': 500},
                 {**data[0], 'id': 't', 'kind': 'TRANSFER_OUT', 'amount_minor': -8000},
                 {**data[0], 'id': 'o', 'kind': 'OPENING', 'amount_minor': 100000}]
        total = build_report(data,'2026-07-01','2026-09-30',currency='CNY')['totals']
        self.assertEqual((9000,500,8500,-8000,100000), tuple(total[k] for k in ['expense_minor','refund_minor','net_expense_minor','internal_transfer_net_minor','opening_entries_minor']))

    def test_empty_report_no_invented_balance(self):
        report = build_report([], '2026-09-01','2026-09-30',currency=None)
        self.assertEqual(0, report['totals']['count'])
        self.assertIsNone(report['changes'][0]['change_percent'])
        self.assertNotIn('balance', report)

    def test_comparison_equal_length_and_anomaly(self):
        data = rows(days=('2026-09-01','2026-09-01','2026-09-02'))
        report = build_report(data,'2026-09-01','2026-09-30',currency='CNY')
        self.assertEqual('2026-08-02',report['previous_period']['start_date'])
        self.assertEqual(1,len(report['anomaly_candidates']))


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.now = datetime(2026,10,12,10,tzinfo=timezone(timedelta(hours=8)))
        self.bank = create_demo(self.root/'bank.db', clock=lambda: self.now)
        self.insight = InsightService(self.root/'insights.db', bank=self.bank, clock=lambda: self.now)

    def tearDown(self): self.temp.cleanup()

    def candidate(self):
        self.insight.check()
        return next(c for c in self.insight.state()['candidates'] if c['label']=='虚构云存储代扣')

    def feedback(self,item,decision,**changes):
        return self.insight.user_action('candidate_feedback', {'candidate_id':item['candidate_id'], 'expected_version':item['version'],'decision':decision,**changes})['data']['candidate']

    def test_fixture_reconciles_and_no_reset(self):
        self.assertEqual(584500,self.bank.call_tool('accounts',{})['data']['total_balance_minor'])
        with self.assertRaises(FileExistsError): create_demo(self.root/'bank.db', clock=lambda: self.now)

    def test_initial_readonly_status_does_not_scan(self):
        self.assertEqual([],self.insight.state()['candidates'])
        self.insight._rows = lambda: self.fail('status scanned source')
        self.assertEqual([],self.insight.call_tool('recurring_candidates',{})['data']['candidates'])
        self.assertEqual([],self.insight.call_tool('insight_status',{})['data']['candidates'])

    def test_readonly_source_and_unconfirmed_silent(self):
        before = (self.root/'bank.db').read_bytes()
        result = self.insight.check()['data']
        self.assertGreaterEqual(len(result['candidates']),2)
        self.assertEqual([],result['new_notifications'])
        self.assertEqual(before,(self.root/'bank.db').read_bytes())

    def test_confirm_dedupe_and_persist(self):
        candidate = self.candidate()
        self.feedback(candidate,'confirm')
        notices = self.insight.check()['data']['new_notifications']
        self.assertEqual(['RENEWAL_DUE'],[n['kind'] for n in notices])
        self.assertEqual([], self.insight.check()['data']['new_notifications'])
        reopened = InsightService(self.root/'insights.db',bank=self.bank,clock=lambda:self.now)
        self.assertEqual(self.insight.state(), reopened.state())

    def test_ignore_persists_and_supersedes(self):
        item = self.feedback(self.candidate(),'confirm')
        self.insight.check()
        self.feedback(item,'ignore')
        self.assertEqual([], self.insight.check()['data']['new_notifications'])
        self.assertTrue(all(n['status']=='superseded' for n in self.insight.state()['notifications']))

    def test_reconfirm_restores_same_notice_without_push_and_survives_restart(self):
        item = self.feedback(self.candidate(),'confirm')
        original = self.insight.check()['data']['new_notifications'][0]
        bank_before = self.bank.db_path.read_bytes()
        for decision in ('ignore','reject'):
            with self.subTest(decision=decision):
                item = self.feedback(item,decision)
                self.insight.check()
                item = self.feedback(item,'confirm')
                self.now += timedelta(minutes=1)
                state = self.insight.check()['data']
                self.assertEqual([],state['new_notifications'])
                self.assertEqual(1,len(state['notifications']))
                restored = state['notifications'][0]
                self.assertEqual('active',restored['status'])
                self.assertEqual(original['notification_id'],restored['notification_id'])
                self.assertEqual(original['created_at'],restored['created_at'])
                self.assertEqual(item['version'],restored['candidate_version'])
                self.assertEqual(self.now.isoformat(),restored['reactivated_at'])
                reopened = InsightService(self.root/'insights.db',bank=self.bank,clock=lambda:self.now)
                self.assertEqual(restored,reopened.check()['data']['notifications'][0])
                self.assertEqual([],reopened.check()['data']['new_notifications'])
        self.assertEqual(bank_before,self.bank.db_path.read_bytes())

    def test_reconfirm_expired_renewal_stays_superseded(self):
        item = self.feedback(self.candidate(),'confirm')
        original = self.insight.check()['data']['new_notifications'][0]
        item = self.feedback(item,'ignore')
        self.now += timedelta(days=8)
        self.feedback(item,'confirm')
        state = self.insight.check()['data']
        self.assertEqual([],state['new_notifications'])
        self.assertEqual('superseded',next(n for n in state['notifications'] if n['notification_id']==original['notification_id'])['status'])

    def test_reconfirm_new_charge_does_not_restore_previous_cycle(self):
        item = self.feedback(self.candidate(),'confirm')
        original = self.insight.check()['data']['new_notifications'][0]
        item = self.feedback(item,'ignore')
        self.now += timedelta(days=8)
        self.bank.user_action('inject_transaction',dict(kind='expense',amount_minor=3000,
            description='虚构云存储代扣',idempotency_key='next-cycle'))
        state = self.insight.check()['data']
        item = next(c for c in state['candidates'] if c['candidate_id']==item['candidate_id'])
        self.feedback(item,'confirm')
        state = self.insight.check()['data']
        self.assertEqual([],state['new_notifications'])
        self.assertTrue(all(n['status']=='superseded' for n in state['notifications']))
        self.assertEqual(original['notification_id'],state['notifications'][0]['notification_id'])

    def test_changed_amount_gets_new_notice_and_never_revives_old(self):
        item = self.feedback(self.candidate(),'confirm')
        original = self.insight.check()['data']['new_notifications'][0]
        item = self.feedback(item,'ignore')
        self.feedback(item,'correct',amount_minor=4500)
        state = self.insight.check()['data']
        self.assertEqual(1,len(state['new_notifications']))
        fresh = state['new_notifications'][0]
        self.assertNotEqual(original['notification_id'],fresh['notification_id'])
        self.assertEqual(4500,fresh['amount_minor'])
        self.assertEqual('superseded',next(n for n in state['notifications'] if n['notification_id']==original['notification_id'])['status'])
        self.assertEqual([],self.insight.check()['data']['new_notifications'])

    def test_reconfirm_without_recent_observations_does_not_revive_notice(self):
        item = self.feedback(self.candidate(),'confirm')
        self.insight.check()
        item = self.feedback(item,'ignore')
        self.now += timedelta(days=80)
        self.feedback(item,'confirm')
        state = self.insight.check()['data']
        self.assertEqual([],state['new_notifications'])
        self.assertTrue(all(n['status']=='superseded' for n in state['notifications']))

    def test_correction_is_not_overwritten(self):
        item = self.feedback(self.candidate(),'correct',amount_minor=4500,next_due_date='2026-10-30')
        self.insight.check()
        found=next(c for c in self.insight.state()['candidates'] if c['candidate_id']==item['candidate_id'])
        self.assertEqual(4500,found['amount_minor'])
        self.assertEqual('2026-10-30',found['next_due_date'])
        self.assertEqual([],self.insight.state()['notifications'])

    def test_stale_feedback_and_model_cannot_confirm(self):
        item=self.candidate(); self.feedback(item,'confirm')
        with self.assertRaises(InsightError) as error: self.feedback(item,'ignore')
        self.assertEqual('STALE_VERSION',error.exception.code)
        with self.assertRaises(InsightError): self.insight.call_tool('candidate_feedback',{})

    def test_strict_types_and_dates(self):
        for args in [{'start_date':'2026-01-01','end_date':'2027-01-01'},
            {'start_date':'0001-01-01','end_date':'0001-01-31'},
            {'start_date':'2026-01-01','end_date':'2026-01-31','compare_previous':1},
            {'start_date':'2026-01-01','end_date':'2026-01-31','subject_id':'U2'}]:
            with self.assertRaises(InsightError): self.insight.call_tool('billing_report',args)
        item=self.candidate()
        for change in [{'amount_minor': True}, {'next_due_date':'2020-01-01'}, {'label':' '}]:
            with self.assertRaises(InsightError): self.feedback(item,'correct',**change)

    def test_source_full_count_no_500_truncation_and_identity(self):
        with closing(sqlite3.connect(self.bank.db_path)) as db:
            for i in range(510):
                db.execute('INSERT INTO transactions VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (f'extra{i}','A','expense',-1,'small','2026-10-01','2026-10-01T00:00:00+08:00',None,None,'SYNTHETIC'))
            for tid,account,day,created in [('foreign','X','2026-10-01','2026-10-01T00:00:00+08:00'),
                ('future','A','2026-10-13','2026-10-13T00:00:00+08:00'),
                ('future_created','A','2026-10-01','2026-10-13T00:00:00+08:00')]:
                db.execute('INSERT INTO transactions VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (tid,account,'expense',-999999,'secret',day,created,None,None,'SYNTHETIC'))
            db.commit()
        report=self.insight.call_tool('billing_report',{'start_date':'2026-10-01','end_date':'2026-10-12'})['data']['report']
        self.assertEqual(510,report['totals']['count'])
        self.assertEqual(510,report['totals']['expense_minor'])

    def test_charge_late_only_observation(self):
        self.feedback(self.candidate(),'confirm')
        self.now += timedelta(days=20)
        notices=self.insight.check()['data']['new_notifications']
        self.assertEqual(['MISSED_EXPECTED_CHARGE'],[n['kind'] for n in notices])

    def test_old_unobserved_candidate_is_not_repeated_forever(self):
        item=self.feedback(self.candidate(),'confirm')
        self.insight.check()
        self.now += timedelta(days=80)
        result=self.insight.check()['data']
        candidate=next(c for c in result['candidates'] if c['candidate_id']==item['candidate_id'])
        self.assertFalse(candidate['currently_observed'])
        self.assertEqual([],result['new_notifications'])
        self.assertTrue(all(n['status']=='superseded' for n in result['notifications']))


if __name__ == '__main__': unittest.main()
