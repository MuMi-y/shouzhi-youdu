"""Offline evaluator uses the product request context and one controlled round."""
import json
import unittest
from tests.competition_ai import test_request_runner as fixtures
from tests.competition_ai.test_request_decision import item, response
from scripts.g2b_control import Control, ControlledRunner
from scripts.g2b_story import versions
from src.bank_requests.execution import run_chat


class RequestExecutionTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.RequestRunnerTests(); self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def model(self, prompt, session, **options):
        context = json.loads(prompt.split('宿主上下文：\n', 1)[1])
        current = next(m for m in context['messages'] if m['message_id'] == context['current_message_id'])
        request = item(missing=['source_account', 'target_account', 'amount_minor'])
        request['action'] = {'kind': 'PREPARE', 'source': {'message_id': current['message_id'], 'quote': current['text']}}
        return response(context, [request],
                        {'format': 'request_clarification_v1', 'item_ids': ['transfer']})

    def test_controlled_evaluator_keeps_context_and_reserves_exactly_once(self):
        control = Control.create(self.f.jobs.path.parent / 'control', source_hashes=versions())
        control.add_branch('MICRO', '2026-12-03T09:00:00+08:00', {'fixture': 'OFFLINE_ONLY'})
        step = control.begin_step('MICRO', 'request', {})
        controlled = ControlledRunner(control, step, self.f.runner, offline_test=True)
        self.f.runner._run_model.side_effect = self.model
        queued = self.f.jobs.add('另一个尚未执行的请求')
        result = run_chat(controlled, self.f.jobs, '帮我转账', None)
        self.assertTrue(result['ok'], result)
        self.assertEqual('NEEDS_INPUT', result['business_outcomes'][0]['assessment'])
        self.assertEqual(1, self.f.runner._run_model.call_count)
        self.assertEqual(1, control.status()['rounds_used'])
        self.assertEqual('QUEUED', self.f.jobs.get(queued['id'])['status'])
        job = self.f.jobs.get(result['request_context_basis']['request_id'])
        self.assertEqual('DONE', job['status'])

    def test_failure_is_persisted_without_automatic_replay(self):
        self.f.runner._run_model.return_value = {'ok': False, 'error': 'OFFLINE_PROVIDER_FAILURE'}
        result = run_chat(self.f.runner, self.f.jobs, '帮我转账', None)
        self.assertFalse(result['ok'])
        self.assertEqual(1, self.f.runner._run_model.call_count)
        with self.f.jobs.connection() as db:
            rows = db.execute('SELECT status FROM jobs').fetchall()
        self.assertEqual(['FAILED'], [r['status'] for r in rows])
