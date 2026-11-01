"""Real isolated application/SQLite paths; no DSH or provider calls."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from src.bank_requests.attempt_projection import collect, disclose
from src.bank_requests.response import validate
from src.bank_v1.common import DomainError
from src.bank_v1_web.handoff_store import HandoffError
from src.bank_v1_web.job_display import project
from src.competition_web.app import create_app
from tests.competition_ai.test_handoff_protocol import HandoffProtocolTests
from tests.competition_ai.test_goal_prepare_contract import MESSAGE, request


class ReadFailureTests(unittest.TestCase):
    def setUp(self):
        self.f = HandoffProtocolTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        # The general handoff fixture deliberately disables optional goal tools.
        f = self.f
        f.store.release_owner()
        f.app = create_app(f.root, clock=lambda: f.now, goals_enabled=True, goal_followup=True)
        f.jobs = f.app.extensions['bank_v1']['jobs']
        f.store = f.jobs.handoff
        f.service = f.app.extensions['competition']
        f.gateway = f.service.request_gateway
        self.addCleanup(f.store.release_owner)

    def start(self, prompt=MESSAGE):
        f = self.f
        job = f.jobs.add(prompt)
        f.jobs.claim(job_id=job['id'])
        handle = f.store.for_job(job['id'])
        context = f.jobs.request_context(job['id'])
        token = f.gateway.open(context, readonly=False, handle=handle)
        self.addCleanup(f.gateway.close, token)
        return job, handle, context, token

    def test_real_missing_read_then_draft_persist_display_restart_and_release(self):
        f = self.f
        job, handle, context, token = self.start()
        before = deepcopy(f.service.bank.state())
        self.assertEqual(409, f.tool(token, 'goal_context', {'goal_id': 'focus'}).status_code)
        item = request()
        item['action']['source']['message_id'] = job['id']
        for slot in item['slots'].values():
            slot['source']['message_id'] = job['id']
        profile = dict(title='学习电脑计划', user_reason='学习电脑', priority_reason='先保证还款',
                       preferred_amount_minor=450000, min_amount_minor=300000, reserve_floor_minor=200000,
                       preferred_date='2026-11-22', latest_date='2026-11-30')
        reply = f.tool(token, 'propose_goal', dict(expected_revision=0, profile=profile,
                       idempotency_key='read-failure-draft', request_binding={'items': [item]}))
        self.assertEqual(200, reply.status_code, reply.json)
        context['_host_attempt_coverage'] = collect(f.store, handle, f.trace)
        self.assertTrue(context['_host_attempt_coverage']['complete'])
        result = validate(f.answer(context, [item], {'format': 'goal_draft_v1',
                          'draft_id': reply.json['data']['draft_id']}), context)
        self.assertTrue(result['ok'], result)
        f.gateway.close(token)
        f.jobs.finish(job['id'], result)
        snap = f.store.snapshot(handle.run_id)
        self.assertEqual('COMPLETE', snap['run']['completeness'])
        self.assertNotIn('UNKNOWN', [e['phase'] for e in snap['events']])
        self.assertEqual(0, snap['attempts'][0]['reservation_closed'])
        self.assertGreater(snap['attempts'][0]['reserved_bytes'], 0)
        shown = project(f.jobs.get(job['id']), f.service, f.store)['result']['host_disclosure']
        self.assertTrue(shown['complete'])
        self.assertEqual(['READ_FAILED', 'RETURNED'], [e['status'] for e in shown['entries']])
        self.assertIn('查询失败', shown['entries'][0]['text'])
        f.store.producers_finished(handle, trace_finalized=False)
        self.assertEqual(0, f.store.snapshot(handle.run_id)['attempts'][0]['reservation_closed'])
        f.store.producers_finished(handle)
        after = f.store.snapshot(handle.run_id)
        self.assertTrue(all(a['reservation_closed'] == 1 and a['reserved_bytes'] == 0 for a in after['attempts']))
        self.assertEqual(0, after['run']['reserved_bytes'])
        self.assertEqual(before, f.service.bank.state())
        self.assertEqual(profile, f.service.goal_tools.drafts()[0]['profile'])
        self.assertEqual([], f.service.tasks.list())
        f.store.release_owner()
        app = create_app(f.root, clock=lambda: f.now, goals_enabled=True, goal_followup=True)
        jobs = app.extensions['bank_v1']['jobs']; service = app.extensions['competition']
        self.addCleanup(jobs.handoff.release_owner)
        again = project(jobs.get(job['id']), service, jobs.handoff)['result']['host_disclosure']
        self.assertEqual(shown, again)
        self.assertEqual(1, len(service.goal_tools.drafts()))

    def test_missing_or_tampered_trace_stays_incomplete(self):
        for variation in ('missing', 'message', 'code', 'status', 'parameters'):
            with self.subTest(variation=variation):
                f = self.f
                job, handle, _, token = self.start('查询目标')
                f.trace = []
                f.tool(token, 'goal_context', {'goal_id': 'focus'})
                trace = deepcopy(f.trace)
                if variation == 'missing':
                    trace.pop()
                elif variation in ('message', 'code'):
                    trace[-1]['response']['error'][variation] = 'invented'
                elif variation == 'status':
                    trace[-1]['status'] = 500
                else:
                    trace[0]['arguments'] = trace[-1]['args'] = {'goal_id': 'other'}
                self.assertFalse(collect(f.store, handle, trace)['complete'])
                f.gateway.close(token); f.jobs.finish(job['id'], {'ok': False})
                self.assertFalse(disclose(f.store.snapshot(handle.run_id))['complete'])

    def test_other_domain_and_runtime_errors_remain_unknown(self):
        for error in (DomainError('NOT_FOUND', 'missing'), RuntimeError('GOAL_MISSING')):
            with self.subTest(error=type(error).__name__):
                f = self.f
                job, handle, _, token = self.start('查询目标')
                with patch.object(f.gateway, 'call_tool', side_effect=error):
                    with self.assertRaises(type(error)):
                        f.gateway.call(token, 'goal_context', {'goal_id': 'focus'})
                f.gateway.close(token); f.jobs.finish(job['id'], {'ok': False})
                view = disclose(f.store.snapshot(handle.run_id))
                self.assertFalse(view['complete'])
                self.assertEqual('UNKNOWN', view['entries'][0]['status'])

    def test_code_only_old_raised_unknown_tool_and_write_never_upgrade(self):
        from src.bank_v1_web.handoff_read_failure import receipt
        for name, effect, typed in [('goal_context', 'READ_ONLY', False),
                                   ('prepare_transfer', 'BANK_PREPARE', True),
                                   ('unknown', 'UNKNOWN_TOOL', True)]:
            with self.subTest(name=name, typed=typed):
                f = self.f
                job, handle, _, token = self.start('查询目标')
                params = {'goal_id': 'focus'}
                a = f.store.begin_attempt(handle, name, params, 'MODEL_GATEWAY', effect_class=effect)
                f.store.validated(a, params); f.store.append(a, 'DISPATCHED', {})
                payload = receipt('goal_context', params, DomainError('GOAL_MISSING', '请先核对并保存一个目标。'))
                f.store.append(a, 'RAISED', payload if typed else {'code': 'GOAL_MISSING'})
                f.gateway.close(token); f.jobs.finish(job['id'], {'ok': False}); f.store.producers_finished(handle)
                snap = f.store.snapshot(handle.run_id)
                self.assertEqual('UNKNOWN', disclose(snap)['entries'][0]['status'])
                self.assertEqual(0, snap['attempts'][0]['reservation_closed'])

    def test_audit_append_failure_does_not_claim_known_failure(self):
        f = self.f
        job, handle, _, token = self.start('查询目标')
        original = f.store.append
        def append(a, phase, payload, **kwargs):
            if phase == 'RAISED':
                raise HandoffError('synthetic audit failure')
            return original(a, phase, payload, **kwargs)
        with patch.object(f.store, 'append', side_effect=append):
            with self.assertRaises(DomainError):
                f.gateway.call(token, 'goal_context', {'goal_id': 'focus'})
        f.gateway.close(token); f.jobs.finish(job['id'], {'ok': False})
        view = disclose(f.store.snapshot(handle.run_id))
        self.assertFalse(view['complete'])
        self.assertEqual('UNKNOWN', view['entries'][0]['status'])

    def test_success_then_audit_error_cannot_become_known_domain_failure(self):
        f = self.f
        job, handle, _, token = self.start('查询目标')
        original = f.store.append
        def append(a, phase, payload, **kwargs):
            if phase == 'RETURNED':
                raise DomainError('GOAL_MISSING', '请先核对并保存一个目标。')
            return original(a, phase, payload, **kwargs)
        with patch.object(f.gateway, 'call_tool', return_value={'data': {}}), patch.object(f.store, 'append', side_effect=append):
            with self.assertRaises(DomainError):
                f.gateway.call(token, 'goal_context', {'goal_id': 'focus'})
        f.gateway.close(token); f.jobs.finish(job['id'], {'ok': False})
        view = disclose(f.store.snapshot(handle.run_id))
        self.assertFalse(view['complete'])
        self.assertEqual('UNKNOWN', view['entries'][0]['status'])


if __name__ == '__main__':
    unittest.main()
