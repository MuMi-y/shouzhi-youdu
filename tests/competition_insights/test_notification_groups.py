from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from src.bank_insights import InsightService
from src.bank_insights.fixture import create_demo
from src.bank_insights.notifications import group_notifications
from src.bank_insights.rules import canonical
from src.bank_insights.service import connect


class NotificationGroupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.now = datetime(2026, 10, 14, 10, tzinfo=timezone(timedelta(hours=8)))
        self.bank = create_demo(self.root / 'bank.db', clock=lambda: self.now)
        self.service = InsightService(self.root / 'insights.db', bank=self.bank, clock=lambda: self.now)
        state = self.service.check()['data']
        candidate = next(c for c in state['candidates'] if c['label'] == '虚构视频会员')
        self.candidate = self.service.user_action('candidate_feedback', {
            'candidate_id': candidate['candidate_id'], 'expected_version': candidate['version'],
            'decision': 'confirm'})['data']['candidate']

    def tearDown(self):
        self.temp.cleanup()

    def feedback(self, decision, **changes):
        self.candidate = self.service.user_action('candidate_feedback', {
            'candidate_id': self.candidate['candidate_id'], 'expected_version': self.candidate['version'],
            'decision': decision, **changes})['data']['candidate']

    def test_one_group_keeps_both_reasons_without_summing(self):
        before = self.bank.db_path.read_bytes()
        data = self.service.check()['data']
        self.assertEqual(2, len(data['new_notifications']))
        self.assertEqual(1, len(data['new_notification_groups']))
        group = data['notification_groups'][0]
        self.assertEqual({'PRICE_INCREASE', 'RENEWAL_DUE'}, {r['kind'] for r in group['reasons']})
        self.assertEqual((2500, '25.00'), (group['amount_minor'], group['amount']))
        self.assertEqual(set(self.candidate['evidence_refs']), set(group['evidence_refs']))
        self.assertEqual({n['notification_id'] for n in data['new_notifications']},
                         {r['notification_id'] for r in group['reasons']})
        self.assertEqual(before, self.bank.db_path.read_bytes())

    def test_repeat_and_restart_preserve_group_without_new_push(self):
        first = self.service.check()['data']['notification_groups']
        self.now += timedelta(minutes=1)
        self.service = InsightService(self.root / 'insights.db', bank=self.bank, clock=lambda: self.now)
        data = self.service.check()['data']
        self.assertEqual(first, data['notification_groups'])
        self.assertEqual([], data['new_notifications'])
        self.assertEqual([], data['new_notification_groups'])

    def test_ignore_and_correct_do_not_leave_old_groups(self):
        old = self.service.check()['data']['notification_groups'][0]
        self.feedback('ignore')
        self.assertEqual([], self.service.state()['notification_groups'])
        self.assertEqual([], self.service.check()['data']['new_notification_groups'])
        self.feedback('correct', amount_minor=2800)
        data = self.service.check()['data']
        self.assertEqual(1, len(data['notification_groups']))
        group = data['notification_groups'][0]
        self.assertNotEqual(old['group_id'], group['group_id'])
        self.assertEqual(2800, group['amount_minor'])
        self.assertEqual(['RENEWAL_DUE'], [r['kind'] for r in group['reasons']])

    def test_expired_renewal_disappears_but_price_reason_remains(self):
        old = self.service.check()['data']['notification_groups'][0]
        self.now += timedelta(days=2)
        data = self.service.check()['data']
        self.assertEqual(old['group_id'], data['notification_groups'][0]['group_id'])
        self.assertEqual(['PRICE_INCREASE'], [r['kind'] for r in data['notification_groups'][0]['reasons']])
        self.assertEqual([], data['new_notification_groups'])

    def test_history_limit_does_not_remove_active_reasons(self):
        expected = self.service.check()['data']['notification_groups']
        base = self.service.state()['notifications'][0]
        with connect(self.service.db_path, True) as db:
            for i in range(105):
                notice = {**base, 'notification_id': f'old_{i}', 'status': 'superseded'}
                db.execute('INSERT INTO notices VALUES(?,?)', (notice['notification_id'], canonical(notice)))
        state = self.service.state()
        self.assertEqual(100, len(state['notifications']))
        self.assertEqual(expected, state['notification_groups'])

    def test_distinct_candidate_date_amount_or_currency_do_not_merge(self):
        data = self.service.check()['data']
        notice = data['notifications'][0]
        notices = [notice]
        for i, changes in enumerate(({'candidate_id': 'different'}, {'due_date': '2026-11-15'},
                                      {'amount_minor': 9000, 'amount': '90.00'}, {'currency': 'USD'})):
            notices.append({**notice, **changes, 'notification_id': f'different_{i}'})
        untouched = deepcopy(notices)
        groups = group_notifications(notices, data['candidates'])
        self.assertEqual(5, len(groups))
        self.assertEqual(groups, group_notifications(list(reversed(notices)), data['candidates']))
        self.assertEqual(untouched, notices)


if __name__ == '__main__':
    unittest.main()
