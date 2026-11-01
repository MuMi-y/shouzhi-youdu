"""Goal state and bank state compose without claiming preview means payment."""
import json
import unittest
from datetime import timedelta
from tests.g2_goals import test_preview as preview_fixture
from tests.g2_goals import test_service_health as health_fixture
from tests.competition_ai.test_request_decision import item, response
from src.bank_requests.context import build
from src.bank_requests.response import validate
from src.bank_v1_web.display_check import check


class GoalDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.f = preview_fixture.PreviewTests(); self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def envelope(self, items, components, trace):
        context = build('m', 'm', [{'message_id': 'm', 'text': '比较目标方案，并查询银行代扣；看看目标跟进进度'}], [], read_only=False)
        raw = response(context, items, {'format': 'bank_outcomes_v1', 'items': components})
        raw['trace'] = trace
        return validate(raw, context)

    def pair(self, name, args, body, identity):
        return [{'kind': 'request', 'name': name, 'arguments': args, 'call_id': identity},
                {'kind': 'response', 'tool': name, 'args': args, 'call_id': identity, 'ok': True, 'status': 200, 'response': body}]

    def test_goal_preview_and_bank_query_pending_are_separate(self):
        preview = self.f.raw()
        args = {'idempotency_key': 'query'}
        prepared = self.f.service.call_tool('prepare_mandate_query', args)
        op = prepared['data']['operation']
        oa = {'operation_id': op['operation_id']}
        read = self.f.service.call_tool('operation', oa)
        trace = preview['trace'] + self.pair('prepare_mandate_query', args, prepared, 'prepare') + self.pair('operation', oa, read, 'read')
        result = self.envelope([item(identifier='goal', intent='goal_compare'), item(identifier='query', intent='mandate_query')],
            [{'item_id': 'goal', 'components': [json.loads(preview['text'])]},
             {'item_id': 'query', 'components': [{'format': 'bank_operation_v1', **oa, 'evidence_id': read['evidence_id']}]}], trace)
        self.assertTrue(result['ok'], result)
        self.assertEqual('PREVIEW', result['business_outcomes'][0]['goal_state'])
        self.assertEqual('NOT_REQUESTED', result['business_outcomes'][0]['execution'])
        self.assertEqual('WAITING_CONFIRMATION', result['business_outcomes'][1]['execution'])
        self.assertTrue(check(result, self.f.service)['ok'])

    def test_goal_source_must_be_latest_even_with_valid_old_claims(self):
        first = self.f.raw()
        profile = {**self.f.profile, 'preferred_amount_minor': 400000}
        second = self.f.raw(self.f.call(profile))
        for row in second['trace']:
            row['call_id'] = 'preview-2'
        result = self.envelope([item(identifier='goal', intent='goal_compare')],
            [{'item_id': 'goal', 'components': [json.loads(first['text'])]}], first['trace'] + second['trace'])
        self.assertEqual('NOT_LATEST_GOAL_SOURCE', result['answer_validation']['reason'])

    def test_failed_analysis_and_quiet_check_preserve_recovery_need(self):
        health = health_fixture.ServiceHealthTests(); health.setUp()
        self.addCleanup(health.tearDown)
        self.f.service = health.service
        follow = health.follow
        pending = follow.observe()
        follow.start(pending['check_id'])
        follow.finish(pending['check_id'], {'ok': False, 'error': 'OFFLINE_SYNTHETIC_FAILURE'})
        health.expense()
        quiet = follow.observe(periodic_due=False)
        self.assertEqual('QUIET', quiet['state'])
        body = self.f.service.call_tool('goal_context', {'goal_id': 'focus'})
        part = {'format': 'goal_answer_v1', 'source_id': body['source_id'],
                'claims': [{k: c[k] for k in ('path', 'value')} for c in body['claim_catalog']],
                'next_step': body['allowed_next_steps'][0]}
        result = self.envelope([item(identifier='follow', intent='goal_followup')],
            [{'item_id': 'follow', 'components': [part]}], self.pair('goal_context', {'goal_id': 'focus'}, body, 'goal'))
        self.assertTrue(result['ok'], result)
        shown = check(result, self.f.service)
        self.assertTrue(shown['ok'], shown)
        self.assertEqual('NEEDS_RECOVERY', shown['business_outcomes'][0]['service_health']['state'])
        self.assertEqual('QUIET', shown['business_outcomes'][0]['service_health']['notice_decision'])
        self.assertIn('最近分析尚未完成', shown['text'])
