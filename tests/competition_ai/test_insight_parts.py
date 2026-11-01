"""Periodic observations, feedback, and notice history retain distinct meanings."""
from copy import deepcopy
from datetime import timedelta
import unittest

from tests.competition_ai import test_service_answer as fixture
from src.bank_evidence.service_answer import recheck


class InsightPartsTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.ServiceAnswerTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def part(self, tool='recurring_candidates', sections=None):
        body = self.f.tool(tool, {})
        sections = sections or ['overview', 'candidates']
        return {'format': 'bank_insights_v1', 'source_tool': tool, 'evidence_id': body['evidence_id'],
                'sections': sections,
                'candidate_ids': [item['candidate_id'] for item in body['data']['candidates']] if 'candidates' in sections else [],
                'notification_ids': [item['notification_id'] for item in body['data'].get('notifications', [])] if 'notifications' in sections else []}

    def feedback(self, decision, **changes):
        item = self.f.service.insights.state()['candidates'][0]
        return self.f.service.insights.user_action('candidate_feedback',
            {'candidate_id': item['candidate_id'], 'expected_version': item['version'], 'decision': decision, **changes})

    def test_exact_entity_observations_and_scope(self):
        self.f.service.insights.check()
        part = self.part()
        result = self.f.answer([part])
        self.assertTrue(result['ok'], result)
        self.assertIn('本次读取缓存本身没有重新扫描流水', result['text'])
        self.assertIn('尚未确认关注', result['text'])
        for item in self.f.service.insights.state()['candidates']:
            self.assertIn(item['label'], result['text'])
            self.assertIn(item['next_due_date'], result['text'])
        self.assertTrue(recheck(result, self.f.service.call_tool)['ok'])

    def test_foreign_id_amount_and_changed_source_are_rejected(self):
        self.f.service.insights.check()
        original = self.part()
        for key, value in [('candidate_ids', ['foreign']), ('amount_minor', 555),
                           ('source_tool', 'check_insights'), ('evidence_id', 'invented')]:
            part = deepcopy(original); part[key] = value
            self.assertFalse(self.f.answer([part])['ok'])
        self.f.trace[-1]['response']['data']['candidates'][0]['amount_minor'] += 1
        self.assertFalse(self.f.answer([original])['ok'])

    def test_feedback_invalidates_old_answer_and_changes_meaning(self):
        self.f.service.insights.check()
        old = self.f.answer([self.part()])
        self.feedback('correct', amount_minor=12777, next_due_date='2026-10-28', label='用户更正名称')
        self.assertFalse(recheck(old, self.f.service.call_tool)['ok'])
        new = self.f.answer([self.part()])
        self.assertTrue(new['ok'], new)
        self.assertIn('用户更正的关注金额 127.77元', new['text'])
        self.assertIn('用户更正的关注日期：2026-10-28', new['text'])
        self.assertIn('不等于订阅合同', new['text'])
        self.feedback('ignore')
        self.assertFalse(recheck(new, self.f.service.call_tool)['ok'])
        ignored = self.f.answer([self.part()])
        self.assertTrue(ignored['ok'], ignored)
        self.assertIn('已忽略', ignored['text'])

    def test_check_display_uses_readonly_status_never_rescans(self):
        self.f.service.insights.check()
        self.feedback('correct', next_due_date='2026-10-24')
        part = self.part('check_insights', ['overview', 'candidates', 'new_notifications'])
        result = self.f.answer([part])
        self.assertTrue(result['ok'], result)
        self.assertIn('该次检查新增的提醒', result['text'])
        before = self.f.service.insights.state()
        calls = []
        def read(name, args):
            calls.append(name)
            self.assertEqual('insight_status', name)
            return self.f.service.call_tool(name, args)
        self.assertTrue(recheck(result, read)['ok'])
        self.assertEqual(['insight_status'], calls)
        self.assertEqual(before, self.f.service.insights.state())

    def test_notice_history_and_no_new_are_not_no_risk(self):
        self.f.service.insights.check(); self.feedback('correct', next_due_date='2026-10-24'); self.f.service.insights.check()
        part = self.part('check_insights', ['overview', 'notifications', 'new_notifications'])
        result = self.f.answer([part])
        self.assertTrue(result['ok'], result)
        self.assertIn('该次检查没有新增提醒', result['text'])
        self.assertIn('缓存中有效', result['text'])
        self.feedback('reject')
        self.assertFalse(recheck(result, self.f.service.call_tool)['ok'])
        # Fresh turn: old successful check need not be represented in new input.
        self.f.trace = []
        history = self.f.answer([self.part('insight_status', ['notifications'])])
        self.assertTrue(history['ok'], history)
        self.assertIn('已失效的历史提醒', history['text'])

    def test_check_and_bank_action_cannot_disappear(self):
        check = self.part('check_insights')
        cached = self.part()
        self.assertFalse(self.f.answer([cached])['ok'])
        op = self.f.transfer()
        self.assertFalse(self.f.answer([check])['ok'])
        self.assertTrue(self.f.answer([check, self.f.part(op)])['ok'])

    def test_unqueried_notifications_and_empty_selection_are_rejected(self):
        self.f.service.insights.check()
        part = self.part()
        for section in ['notifications', 'new_notifications']:
            changed = deepcopy(part); changed['sections'].append(section)
            self.assertFalse(self.f.answer([changed])['ok'])
        part['candidate_ids'] = []
        self.assertFalse(self.f.answer([part])['ok'])

    def test_business_day_change_invalidates_cached_explanation(self):
        self.f.service.insights.check()
        result = self.f.answer([self.part()])
        self.assertTrue(result['ok'], result)
        self.f.now += timedelta(days=1)
        self.assertFalse(recheck(result, self.f.service.call_tool)['ok'])


if __name__ == '__main__':
    unittest.main()
