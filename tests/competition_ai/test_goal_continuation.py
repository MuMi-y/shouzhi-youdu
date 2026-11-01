"""Persisted two-turn preview-to-draft journey with real isolated tools, no AI."""
from copy import deepcopy
import json
import unittest

from src.bank_requests.response import validate
from src.bank_requests.attempt_projection import collect
from src.bank_requests.goal_profile_reference import FIELDS, FORMAT
from src.bank_requests.objects import historical_items
from src.bank_v1.common import DomainError
from tests.g2_goals import test_preview as fixtures
from tests.competition_ai.test_request_decision import response


class GoalContinuationTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.PreviewTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.service = self.f.service; self.jobs = self.f.jobs; self.gateway = self.service.request_gateway
        self.profile = {**self.f.profile, 'title': '职业证书培训', 'user_reason': '提升工作技能', 'priority_reason': '房租优先'}
        self.message = '想安排职业证书培训，用途是提升工作技能，预算4500元，最低3000元，保留2000元，计划2026-11-22，最晚2026-11-30，房租优先。先比较，不要保存。'
        self.preview()

    def open(self, text, readonly=False):
        job = self.jobs.add(text, read_only=readonly); self.jobs.claim(job_id=job['id'])
        context = self.jobs.request_context(job['id'])
        token = self.gateway.open(context, readonly=readonly, handle=self.jobs.handoff.for_job(job['id']))
        return job['id'], context, token

    def finish_result(self, job_id, context, token, item, name, args, body, part):
        raw = response(context, [item], part)
        raw['trace'] = [{'kind': 'request', 'call_id': 'actual-call', 'name': name, 'arguments': args},
                        {'kind': 'response', 'call_id': 'actual-call', 'tool': name, 'args': args,
                         'ok': True, 'status': 200, 'response': body}]
        context['_host_attempt_coverage'] = collect(self.jobs.handoff, self.jobs.handoff.for_job(job_id), raw['trace'])
        result = validate(raw, context)
        self.assertTrue(result['ok'], result.get('answer_validation'))
        self.jobs.finish(job_id, result); self.gateway.close(token)
        return result

    def preview(self):
        job_id, context, token = self.open(self.message, readonly=True)
        args = {'profile': self.profile}
        body = self.gateway.call(token, 'preview_goal_options', args)
        item = {'id': 'g', 'intent': 'goal_compare', 'continuation_of': None,
                'action': {'kind': 'ANALYZE', 'source': {'message_id': job_id, 'quote': self.message}},
                'slots': {}, 'missing_fields': [], 'conflicts': []}
        for field, value, quote in [('amount_minor', 450000, '4500元'), ('minimum_minor', 300000, '3000元'),
                                     ('reserve_minor', 200000, '2000元'),
                                     ('date_text', *getattr(self, 'preferred_source', ('2026-11-22', '2026-11-22'))),
                                     ('latest_date_text', '2026-11-30', '2026-11-30')]:
            item['slots'][field] = {'value': value, 'source': {'message_id': job_id, 'quote': quote}}
        for field, key in FIELDS.items():
            item['slots'][field] = {'reference': {'format': FORMAT, 'call_id': 'actual-call',
                'source_id': body['source_id'], 'field': key},
                'source': {'message_id': job_id, 'quote': self.profile[key]}}
        answer = json.loads(self.f.raw(body)['text'])
        self.preview_result = self.finish_result(job_id, context, token, item, 'preview_goal_options', args, body, answer)
        self.parent_ref = job_id + ':g'

    def draft(self, text='请按刚才的条件给我草案。', slots=None, profile=None, mutate_context=None):
        job_id, context, token = self.open(text)
        self.assertIn('goal_preview_origin', next(p for p in context['previous_items'] if p['reference'] == self.parent_ref))
        if mutate_context:
            self.gateway.close(token)
            mutate_context(context)
            token = self.gateway.open(context, readonly=False, handle=self.jobs.handoff.for_job(job_id))
        item = {'id': 'draft', 'intent': 'goal_compare', 'continuation_of': self.parent_ref,
                'action': {'kind': 'PREPARE', 'source': {'message_id': job_id, 'quote': text}},
                'slots': {field: {'value': value, 'source': {'message_id': job_id, 'quote': quote}}
                          for field, (value, quote) in (slots or {}).items()}, 'missing_fields': [], 'conflicts': []}
        args = {'expected_revision': 0, 'idempotency_key': 'draft-' + job_id,
                'profile': profile or self.profile, 'request_binding': {'items': [item]}}
        try:
            body = self.gateway.call(token, 'propose_goal', args)
            result = self.finish_result(job_id, context, token, item, 'propose_goal', args, body,
                                        {'format': 'goal_draft_v1', 'draft_id': body['data']['draft_id']})
            return result
        except Exception:
            self.gateway.close(token)
            self.jobs.finish(job_id, {'ok': False, 'error': 'Isolated rejected request'})
            raise

    def test_persisted_preview_then_prepare_without_retyping(self):
        before = deepcopy(self.service.goals.state())
        bank_before = self.service.bank.call_tool('accounts', {})
        result = self.draft()
        self.assertEqual('DRAFT', result['business_outcomes'][0]['goal_state'])
        self.assertEqual('NOT_REQUESTED', result['business_outcomes'][0]['execution'])
        self.assertEqual(self.profile, self.service.goal_tools.drafts()[0]['profile'])
        self.assertEqual(before, self.service.goals.state())
        self.assertEqual(bank_before, self.service.bank.call_tool('accounts', {}))
        self.assertEqual([], self.service.tasks.list())

    def test_current_reserve_and_latest_revision_must_both_be_explicit(self):
        text = '按刚才给草案，不过保留额改为1900元、最晚改为2026-12-08'
        reserve = {'reserve_minor': (190000, '1900元')}
        latest = {'latest_date_text': ('2026-12-08', '2026-12-08')}
        for slots in ({}, reserve, latest):
            with self.assertRaises(DomainError):
                self.draft(text, slots)
        self.assertEqual([], self.service.goal_tools.drafts())
        updated = {**self.profile, 'reserve_floor_minor': 190000, 'latest_date': '2026-12-08'}
        result = self.draft(text, {**reserve, **latest}, updated)
        self.assertTrue(result['ok'])
        self.assertEqual(updated, self.service.goal_tools.drafts()[0]['profile'])

    def test_budget_and_preferred_date_revision_preserves_other_conditions(self):
        text = '按刚才准备草案，预算改为4000元，期望日期改为2026-11-25'
        updated = {**self.profile, 'preferred_amount_minor': 400000, 'preferred_date': '2026-11-25'}
        self.draft(text, {'amount_minor': (400000, '4000元'), 'date_text': ('2026-11-25', '2026-11-25')}, updated)
        self.assertEqual(updated, self.service.goal_tools.drafts()[0]['profile'])

    def test_failed_delivery_and_tampered_parent_do_not_gain_origin(self):
        rejected = deepcopy(self.preview_result); rejected['child_validation']['status'] = 'REJECTED'
        history = historical_items(rejected, rejected['request_decision']['request_id'])
        self.assertNotIn('goal_preview_origin', history[0])
        failed = deepcopy(self.preview_result); failed['ok'] = False
        self.assertNotIn('goal_preview_origin', historical_items(failed, failed['request_decision']['request_id'])[0])
        for mutate in (
            lambda c: c['previous_items'][0].pop('goal_preview_origin'),
            lambda c: c['previous_items'][0]['goal_preview_origin']['identity'].update(title='旅游'),
            lambda c: c['messages'].pop(0),
        ):
            with self.assertRaises((DomainError, ValueError)):
                self.draft(mutate_context=mutate)
        self.assertEqual([], self.service.goal_tools.drafts())

    def test_negated_or_undecided_current_request_and_changed_identity_reject(self):
        for text in ('不要按刚才准备草案', '还没选定目标，按刚才给草案', '按刚才给草案，或者改成旅游'):
            with self.assertRaises(DomainError):
                self.draft(text)
        with self.assertRaises(DomainError):
            self.draft(profile={**self.profile, 'title': '提升工作技能'})
        self.assertEqual([], self.service.goal_tools.drafts())

    def test_expired_dates_and_stale_same_identity_parent_reject(self):
        original_ref = self.parent_ref
        self.preview()  # A second accepted same-identity preview is newer.
        newest_ref = self.parent_ref
        self.parent_ref = original_ref
        with self.assertRaises(DomainError):
            self.draft()
        self.parent_ref = newest_ref
        from datetime import timedelta
        self.f.now += timedelta(days=40)
        with self.assertRaises(DomainError):
            self.draft()
        self.assertEqual([], self.service.goal_tools.drafts())

    def test_failed_converted_parent_cannot_fall_back_to_literal_history(self):
        result = self.draft()
        previous_id = result['request_decision']['request_id']
        failed = deepcopy(result); failed['ok'] = False
        failed['answer_validation']['status'] = 'REJECTED'
        self.jobs.finish(previous_id, failed)  # Isolated fixture failure state.
        job_id, context, token = self.open('按刚才继续准备草案')
        parent_ref = previous_id + ':draft'
        parent = next(p for p in context['previous_items'] if p['reference'] == parent_ref)
        self.assertIn('goal_prepare_parent', parent)
        self.assertNotIn('goal_preview_origin', parent)
        item = {'id': 'retry', 'intent': 'goal_compare', 'continuation_of': parent_ref,
                'slots': {}, 'missing_fields': [], 'conflicts': [],
                'action': {'kind': 'PREPARE', 'source': {'message_id': job_id, 'quote': '按刚才继续准备草案'}}}
        count = len(self.service.goal_tools.drafts())
        with self.assertRaises(DomainError):
            self.gateway.call(token, 'propose_goal', {'expected_revision': 0, 'profile': self.profile,
                'idempotency_key': 'failed-parent', 'request_binding': {'items': [item]}})
        self.assertEqual(count, len(self.service.goal_tools.drafts()))
        self.gateway.close(token)

    def test_relative_parent_date_keeps_accepted_anchor_after_day_changes(self):
        from datetime import timedelta
        self.profile['preferred_date'] = '2026-11-03'
        self.message = self.message.replace('2026-11-22', '明天')
        self.preferred_source = ('明天', '明天')
        self.preview()
        self.f.now += timedelta(days=1)
        self.draft()
        self.assertEqual('2026-11-03', self.service.goal_tools.drafts()[0]['profile']['preferred_date'])

    def test_negated_conditional_or_questioned_revisions_cannot_write(self):
        cases = [
            ('不要把保留额改为1900元', 'reserve_minor', 190000, '保留额改为1900元', 'reserve_floor_minor'),
            ('不要把预算改为4000元', 'amount_minor', 400000, '预算改为4000元', 'preferred_amount_minor'),
            ('别把最低金额改为2800元', 'minimum_minor', 280000, '最低金额改为2800元', 'min_amount_minor'),
            ('不要把期望日期改为2026-11-25', 'date_text', '2026-11-25', '期望日期改为2026-11-25', 'preferred_date'),
            ('不要把最晚改为2026-12-08', 'latest_date_text', '2026-12-08', '最晚改为2026-12-08', 'latest_date'),
            ('不要把优先事项改为旅游优先', 'priority', '旅游优先', '旅游优先', 'priority_reason'),
            ('保留额改为1900元不行', 'reserve_minor', 190000, '1900元', 'reserve_floor_minor'),
            ('保留额改为1900元可以吗', 'reserve_minor', 190000, '1900元', 'reserve_floor_minor'),
            ('如果收入到账，保留额改为1900元', 'reserve_minor', 190000, '1900元', 'reserve_floor_minor'),
        ]
        for index, (text, field, value, quote, key) in enumerate(cases):
            if index and index % 4 == 0:
                self.preview()  # Preserve the real eight-message history bound.
            with self.subTest(text=text):
                with self.assertRaises(DomainError):
                    self.draft('请按刚才的条件准备草案，' + text + '。', {field: (value, quote)}, {**self.profile, key: value})
        self.assertEqual([], self.service.goal_tools.drafts())

    def test_explicit_refusal_set_applies_to_every_revision_role(self):
        for text, field, value, quote, key in [
            ('拒绝把预算改为4300元', 'amount_minor', 430000, '4300元', 'preferred_amount_minor'),
            ('禁止把最晚改为2026-12-08', 'latest_date_text', '2026-12-08', '2026-12-08', 'latest_date'),
            ('不同意把保留额改为1900元', 'reserve_minor', 190000, '1900元', 'reserve_floor_minor'),
        ]:
            with self.subTest(text=text), self.assertRaises(DomainError):
                self.draft('请按刚才的条件准备草案，' + text + '。', {field: (value, quote)}, {**self.profile, key: value})
        self.assertEqual([], self.service.goal_tools.drafts())


if __name__ == '__main__':
    unittest.main()
