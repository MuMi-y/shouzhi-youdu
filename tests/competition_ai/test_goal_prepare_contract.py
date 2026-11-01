"""Constructed goal preparation contracts and real isolated storage; no model."""
from copy import deepcopy
from contextlib import closing
import json
import sqlite3
import unittest

from jsonschema import Draft202012Validator
from src.bank_requests.context import build, model_prompt
from src.bank_requests.schema import binding_schema, item_schema, model_contract
from src.bank_requests.response import validate as validate_response
from src.bank_v1.common import DomainError
from tests.g2_goals import test_preview as fixtures
from tests.competition_ai import test_request_gateway as gateways
from tests.competition_ai.test_request_decision import response


MESSAGE = '想准备一个学习电脑计划，预算4500元，最低3000元，保留2000元，计划2026-11-22买，最晚2026-11-30，先保证还款。先给我草案。'


def request(message=MESSAGE):
    pairs = {'purpose': ('学习电脑', '学习电脑'), 'priority': ('先保证还款', '先保证还款'),
             'amount_minor': (450000, '预算4500元'), 'minimum_minor': (300000, '最低3000元'),
             'reserve_minor': (200000, '保留2000元'), 'date_text': ('2026-11-22', '计划2026-11-22'),
             'latest_date_text': ('2026-11-30', '最晚2026-11-30')}
    return {'id': 'goal', 'intent': 'goal_compare', 'continuation_of': None,
            'action': {'kind': 'PREPARE', 'source': {'message_id': 'm', 'quote': message}},
            'slots': {key: {'value': value, 'source': {'message_id': 'm', 'quote': quote}}
                      for key, (value, quote) in pairs.items()}, 'missing_fields': [], 'conflicts': []}


class GoalPrepareContractTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PreviewTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.service = self.fixture.service
        self.gateway = self.service.request_gateway

    def prepare(self, message=MESSAGE, change=None):
        if hasattr(self, 'active_job'):
            self.fixture.jobs.finish(self.active_job, {'ok': False, 'error': 'Offline fixture completed'})
        context = build('m', 'm', [{'message_id': 'm', 'text': message}], [], read_only=False)
        item = request(message)
        profile = {**self.fixture.profile, 'title': '学习电脑计划', 'user_reason': '学习电脑'}
        args = {'expected_revision': 0, 'idempotency_key': 'goal-literal-fixture',
                'profile': profile, 'request_binding': {'items': [item]}}
        if change:
            change(item, args)
        token = gateways.RequestGatewayTests.host_open(self.fixture.app, self.gateway.open, context, readonly=False)
        self.active_job = context['request_id']
        self.addCleanup(self.gateway.close, token)
        return context, item, args, token

    def bank_dump(self):
        with closing(sqlite3.connect((self.fixture.root / 'bank.sqlite3').as_uri() + '?mode=ro', uri=True)) as db:
            return list(db.iterdump())

    def test_schema_exposes_only_supported_shapes(self):
        item = request()
        for protocol in ('legacy_source_v1', 'minimal_source_v2'):
            schema = Draft202012Validator(binding_schema(protocol))
            self.assertTrue(schema.is_valid({'items': [item]}))
            old = deepcopy(item)
            old['slots']['goal_title'] = {'value': '学习电脑计划', 'source': {'message_id': 'm', 'quote': '学习电脑计划'}}
            self.assertFalse(schema.is_valid({'items': [old]}))
            self.assertFalse(Draft202012Validator(item_schema(source_contract=protocol)).is_valid(old))
        contract = json.loads(model_contract())['goal_compare']
        self.assertNotIn('goal_title', contract['allowed_user_slots'])
        self.assertNotIn('goal_title', contract['prepare_slots'])
        context, _, _, _ = self.prepare()
        prompt, _ = model_prompt(context)
        self.assertIn('不能只引用“准备”', prompt)

    def test_gateway_draft_delivery_idempotency_and_no_bank_effect(self):
        context, item, args, token = self.prepare()
        bank_before = self.bank_dump()
        goal_before = deepcopy(self.service.goals.state())
        tasks_before = deepcopy(self.service.tasks.list())
        self.assertEqual([], self.service.goal_tools.drafts())
        body = self.gateway.call(token, 'propose_goal', args)
        self.assertEqual('UNCONFIRMED', body['data']['status'])
        self.assertEqual(1, len(self.service.goal_tools.drafts()))
        self.assertEqual(args['profile'], self.service.goal_tools.drafts()[0]['profile'])
        self.assertEqual(goal_before, self.service.goals.state())
        self.assertEqual(tasks_before, self.service.tasks.list())
        self.assertEqual(bank_before, self.bank_dump())
        raw = response(context, [item], {'format': 'goal_draft_v1', 'draft_id': body['data']['draft_id']})
        raw['trace'] = []
        for call_id in ('draft-1',):
            raw['trace'].extend([
                {'kind': 'request', 'call_id': call_id, 'name': 'propose_goal', 'arguments': args},
                {'kind': 'response', 'call_id': call_id, 'tool': 'propose_goal', 'args': args,
                 'ok': True, 'status': 200, 'response': body}])
        from src.bank_requests.attempt_projection import collect
        context['_host_attempt_coverage'] = collect(self.gateway.store,
            self.gateway.store.for_job(context['request_id']), raw['trace'])
        result = validate_response(raw, context)
        self.assertTrue(result['ok'], result)
        self.assertEqual('DRAFT', result['business_outcomes'][0]['goal_state'])
        self.assertEqual('NOT_REQUESTED', result['business_outcomes'][0]['execution'])
        # A separate request reuses the original idempotency key. Same-run
        # identical attempts have a pre-existing ambiguous trace-link boundary.
        _, _, retry_args, retry_token = self.prepare()
        self.assertEqual(body, self.gateway.call(retry_token, 'propose_goal', retry_args))
        self.assertEqual(1, len(self.service.goal_tools.drafts()))
        self.assertEqual(goal_before, self.service.goals.state())
        self.assertEqual(bank_before, self.bank_dump())

    def test_extended_identity_is_rejected_before_storage(self):
        for field, value in [('title', '学习电脑和旅游计划'), ('user_reason', '学习电脑并兼顾旅游'),
                             ('priority_reason', '先保证还款并投资')]:
            with self.subTest(field=field):
                _, _, args, token = self.prepare(change=lambda item, args: args['profile'].update({field: value}))
                with self.assertRaises(DomainError) as caught:
                    self.gateway.call(token, 'propose_goal', args)
                self.assertEqual('REQUEST_GOAL_IDENTITY_UNBOUND', caught.exception.code)
        self.assertEqual([], self.service.goal_tools.drafts())

    def test_clipped_action_does_not_hide_negation_or_alternatives(self):
        for prefix in ('不要', '相机或', '旅游和'):
            message = prefix + MESSAGE
            _, _, args, token = self.prepare(message,
                change=lambda item, args: item['action']['source'].update(quote=MESSAGE))
            with self.assertRaises(DomainError) as caught:
                self.gateway.call(token, 'propose_goal', args)
            self.assertEqual('REQUEST_GOAL_IDENTITY_UNBOUND', caught.exception.code)
        self.assertEqual([], self.service.goal_tools.drafts())

    def test_original_title_missing_source_wrong_value_and_analyze_still_reject(self):
        changes = [
            lambda item, args: item['slots'].update(goal_title={'value': '学习电脑计划', 'source': {'message_id': 'm', 'quote': '学习电脑计划'}}),
            lambda item, args: item['slots'].pop('purpose'),
            lambda item, args: item['action']['source'].update(quote='准备'),
            lambda item, args: args['profile'].update(preferred_amount_minor=440000),
            lambda item, args: args['profile'].update(latest_date='2026-11-29'),
            lambda item, args: item['action'].update(kind='ANALYZE'),
            lambda item, args: item.update(goal_prepare_identity={'format': 'goal_prepare_identity_v1'}),
        ]
        for index, change in enumerate(changes):
            with self.subTest(index=index):
                _, _, args, token = self.prepare(change=change)
                with self.assertRaises(DomainError):
                    self.gateway.call(token, 'propose_goal', args)
        self.assertEqual([], self.service.goal_tools.drafts())

    def test_historical_literal_identity_keeps_current_preparation_authority(self):
        from src.bank_requests.decision import validate as decide
        first = build('m', 'm', [{'message_id': 'm', 'text': MESSAGE}], [], read_only=False)
        parent = decide([request()], first)['items'][0]
        parent['reference'] = 'm:goal'
        policy = self.service.request_policy
        for text, quote, accepted in [('请准备草案', '请准备草案', True), ('不要准备草案', '准备草案', False)]:
            context = build('m2', 'm2', first['messages'] + [{'message_id': 'm2', 'text': text}], [parent], read_only=False)
            child = {'id': 'continued', 'intent': 'goal_compare', 'continuation_of': parent['reference'],
                     'slots': {}, 'missing_fields': [], 'conflicts': [],
                     'action': {'kind': 'PREPARE', 'source': {'message_id': 'm2', 'quote': quote}}}
            if accepted:
                bound = policy.bind({'items': [child]}, context, [])
                profile = {**self.fixture.profile, 'title': '学习电脑计划', 'user_reason': '学习电脑'}
                policy.check_call('propose_goal', {'profile': profile}, policy.item_for(bound))
            else:
                with self.assertRaises(DomainError) as caught:
                    policy.bind({'items': [child]}, context, [])
                self.assertEqual('REQUEST_GOAL_IDENTITY_UNBOUND', caught.exception.code)


if __name__ == '__main__':
    unittest.main()
