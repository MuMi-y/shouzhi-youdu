"""Selected intent, saved arrangement and hypothetical alternatives must stay distinct."""
from copy import deepcopy
import json
import unittest
from tests.g2_goals.test_preview import PreviewTests
from tests.g2_goals.test_service import trace_for
from src.bank_goals.evidence import envelope
from src.bank_v1.storage import connection
from src.bank_v1_web.bank_answer import validate_bank_answer


class CurrentPlanTests(unittest.TestCase):
    def setUp(self):
        PreviewTests.setUp(self)
        self.service.goals.action('save', {'expected_revision': 0, 'profile': self.profile})

    def fresh(self):
        return self.service.call_tool('goal_context', {'goal_id': 'focus'})

    def answer(self, response=None):
        response = response or self.fresh()
        answer = {'format': 'goal_answer_v1', 'source_id': response['source_id'],
            'claims': [{k: c[k] for k in ('path', 'value')} for c in response['claim_catalog']],
            'next_step': response['allowed_next_steps'][0]}
        return {'ok': True, 'text': json.dumps(answer, ensure_ascii=False), 'trace': trace_for(response),
                'execution_mode': 'OFFLINE_STUB'}

    def select(self):
        comparison = self.service.goals.action('compare', {'expected_revision': 1})
        return self.service.goals.action('select', {'expected_revision': 1,
            'assessment_id': comparison['assessment_id'], 'option_id': 'reduced',
            'wait_for_income': False, 'idempotency_key': 'selected-current-test'})

    def prepare(self, choice):
        choice = self.service.goals.action('prepare', {'choice_id': choice['choice_id']})
        operation = next(o for o in self.service.bank.state()['operations'] if o['operation_id'] == choice['operation_id'])
        return choice, operation

    def save_arrangement(self):
        choice, operation = self.prepare(self.select())
        self.service.bank.user_action('confirm', {'operation_id': operation['operation_id'], 'expected_summary': operation['summary']})
        return self.service.goals.action('reconcile', {'choice_id': choice['choice_id']})

    def test_lifecycle_displays_each_actual_stage_without_changing_bank(self):
        self.assertIsNone(self.fresh()['data']['current_plan'])
        choice = self.select()
        self.assertEqual(self.fresh()['data']['current_plan']['notice_state'], 'SELECTED')
        text = validate_bank_answer(self.answer(), '进展如何')['text']
        self.assertIn('尚未准备银行确认', text)
        choice, op = self.prepare(choice)
        self.assertEqual(self.fresh()['data']['current_plan']['notice_state'], 'WAITING_CONFIRMATION')
        self.assertIn('等待你独立确认', validate_bank_answer(self.answer(), '进展如何')['text'])
        self.service.bank.user_action('confirm', {'operation_id': op['operation_id'], 'expected_summary': op['summary']})
        self.assertEqual(self.fresh()['data']['current_plan']['notice_state'], 'REVIEW_REQUIRED')
        self.service.goals.action('reconcile', {'choice_id': choice['choice_id']})
        before = self.service.bank.state()
        response = self.fresh(); plan = response['data']['current_plan']
        self.assertEqual(plan['notice_state'], 'SAVED')
        self.assertEqual(plan['arrangement']['amount_minor'], 300000)
        self.assertEqual(plan['projection']['primary']['minimum_minor'], 1000000-300000-200000-300000)
        self.assertEqual(plan['projection']['without_income']['minimum_minor'], 200000)
        self.assertEqual(response['data']['options'][0]['primary']['minimum_minor'], 50000)
        checked = validate_bank_answer(self.answer(response), '进展如何')
        self.assertTrue(checked['ok']); self.assertIn('当前已保存安排：2026-11-22支出3000.00元', checked['text'])
        self.assertIn('尚无付款完成依据', checked['text']); self.assertIn(plan['receipt_id'], checked['text'])
        self.assertNotIn('选择仍需你确认', checked['text'])
        self.assertEqual(before, self.service.bank.state())

    def test_spending_changes_current_plan_and_small_followup_is_quiet(self):
        self.save_arrangement()
        self.service.goal_followup.observe()
        self.service.bank.user_action('inject_transaction', {'kind': 'expense', 'amount_minor': 60000, 'idempotency_key': 'expense600'})
        check = self.service.goal_followup.observe()
        self.assertIn('options', check['reasons'])
        response = self.fresh(); plan = response['data']['current_plan']
        for value in plan['projection'].values():
            self.assertEqual(value, {'minimum_minor': 140000, 'date': '2026-11-22', 'reserve_shortfall_minor': 60000})
        self.assertEqual(response['allowed_next_steps'], ['REVIEW_ADJUSTMENT'])
        self.assertEqual(response['data']['options'][0]['primary']['minimum_minor'], -10000)
        checked = validate_bank_answer(self.answer(response), '消费影响')
        self.assertTrue(checked['ok']); self.assertIn('当前已保存安排', checked['text'])
        self.assertIn('差600.00元', checked['text'])
        self.service.bank.user_action('inject_transaction', {'kind': 'expense', 'amount_minor': 17, 'idempotency_key': 'expense017'})
        quiet = self.service.goal_followup.observe()
        self.assertEqual(quiet['state'], 'QUIET')
        self.assertEqual(self.fresh()['data']['current_plan']['projection']['primary']['minimum_minor'], 139983)

    def test_old_goal_v1_envelope_remains_readable(self):
        data = deepcopy(self.fresh()['data']); data.pop('current_plan')
        old = envelope(data, 'goal_context')
        self.assertEqual(old['contract'], 'goal-evidence-1')
        checked = validate_bank_answer(self.answer(old), '旧报告')
        self.assertTrue(checked['ok']); self.assertEqual(checked['answer_validation']['version'], 'goal-evidence-1')

    def test_omission_wrong_current_amount_and_wrong_next_step_are_rejected(self):
        self.save_arrangement(); raw = self.answer()
        for changes in [
            {'claims': [c for c in json.loads(raw['text'])['claims'] if not c['path'].startswith('current_plan')]},
            {'next_step': 'REVIEW_OPTIONS'}]:
            value = deepcopy(raw); value['text'] = json.dumps({**json.loads(raw['text']), **changes})
            self.assertFalse(validate_bank_answer(value, '进展如何')['ok'])
        answer = json.loads(raw['text'])
        next(c for c in answer['claims'] if c['path'] == 'current_plan.primary')['value']['minimum_minor'] = 50000
        self.assertFalse(validate_bank_answer({**raw, 'text': json.dumps(answer)}, '进展如何')['ok'])

    def test_changed_goal_does_not_relabel_saved_bank_amount(self):
        self.save_arrangement()
        self.service.goals.action('save', {'expected_revision': 1, 'profile': {**self.profile,
            'preferred_amount_minor': 400000, 'min_amount_minor': 250000}})
        data = self.fresh()['data']
        self.assertEqual(data['current_plan']['selected_amount_minor'], 300000)
        self.assertEqual(data['current_plan']['arrangement']['amount_minor'], 300000)
        self.assertEqual(data['current_plan']['projection']['primary']['minimum_minor'], 200000)
        self.assertEqual(data['options'][1]['amount_minor'], 250000)

    def test_current_plan_threshold_change_triggers_when_candidates_do_not_change(self):
        self.save_arrangement()
        self.service.goals.action('save', {'expected_revision': 1, 'profile': {**self.profile,
            'preferred_amount_minor': 400000, 'min_amount_minor': 250000}})
        before = self.service.goal_followup.observe()
        self.service.bank.user_action('inject_transaction', {'kind': 'expense', 'amount_minor': 10000, 'idempotency_key': 'current-only'})
        after = self.service.goal_followup.observe()
        self.assertEqual(before['signature']['options'], after['signature']['options'])
        self.assertIn('current_plan', after['reasons'])
        self.assertTrue(after['signature']['current_plan']['reserve_breach']['primary'])

    def test_inactive_or_changed_bank_arrangement_is_not_claimed_active(self):
        self.save_arrangement()
        original = self.fresh()['data']['current_plan']['arrangement']
        for status, version in [('cancelled', 2), ('completed', 2), ('active', 2)]:
            # Explicit adversarial mutation of this temporary fixture only.
            with connection(self.service.bank.db_path, True) as db:
                db.execute('UPDATE arrangements SET status=?,version=? WHERE arrangement_id=?',
                           (status, version, original['arrangement_id']))
            response = self.fresh(); plan = response['data']['current_plan']
            self.assertEqual(plan['notice_state'], 'REVIEW_REQUIRED' if status == 'active' else 'INACTIVE')
            if status == 'active': self.assertIsNotNone(plan['projection'])
            else: self.assertIsNone(plan['projection'])
            checked = validate_bank_answer(self.answer(response), '状态如何')
            self.assertTrue(checked['ok']); self.assertNotIn('当前已保存安排：', checked['text'])


if __name__ == '__main__': unittest.main()
