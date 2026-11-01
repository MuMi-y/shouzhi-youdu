"""Real local HTTP and durable bank/task services; constructed decisions, no model."""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.competition_web.app import create_app
from src.bank_requests.context import build
from src.bank_v1.common import DomainError
from src.bank_tasks import TaskService
from src.bank_v1_web.worker import DshRunner
from tests.competition_ai.test_request_decision import item, slot
from tests.competition_ai.test_request_actions import action
from tests.competition_tasks.test_mandate_match import node, plan


class RequestGatewayTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='request-gate-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.now = datetime(2026, 10, 19, 2, tzinfo=timezone.utc)
        self.restart()

    def restart(self):
        if hasattr(self,'app'):
            self.app.extensions['bank_v1']['jobs'].handoff.release_owner()
        self.app = create_app(self.root, clock=lambda: self.now)
        self.addCleanup(self.app.extensions['bank_v1']['jobs'].handoff.release_owner)
        self.service = self.app.extensions['competition']
        self.gateway = self.service.request_gateway
        original_open=self.gateway.open
        self.gateway.open=lambda context,**options:self.host_open(self.app,original_open,context,**options)
        self.client = self.app.test_client()

    @staticmethod
    def host_open(app,original_open,context,**options):
        if 'handle' not in options:
            jobs=app.extensions['bank_v1']['jobs'];store=jobs.handoff
            if context is None:
                handle=store.open_host_run('SYSTEM',{'fixture':'readonly'},readonly=True)
            else:
                job=jobs.add(context['messages'][-1]['text'],read_only=options['readonly'])
                jobs.claim(job_id=job['id']);handle=store.for_job(job['id'])
                context.update(request_id=job['id'],run_id=handle.run_id,source_contract=handle.source_contract)
            options['handle']=handle
        return original_open(context,**options)

    def transfer(self, *, kind='PREPARE', floor=80000, readonly=False):
        phrase = {'PREPARE': '准备转账', 'ANALYZE': '先分析', 'CHECK_STATUS': '查状态'}[kind]
        text = '从B到A转173.00元，至少保留800.00元，' + phrase
        request = item({'source_account': slot('m', 'B', 'B'), 'target_account': slot('m', 'A', 'A'),
                        'amount_minor': slot('m', '173.00元', 17300)})
        if floor is not None:
            # All other floors used below are explicit new user requests.
            quoted = f'{floor // 100}.{floor % 100:02d}元'
            text = text.replace('800.00元', quoted)
            request['slots']['reserve_minor'] = slot('m', quoted, floor)
        request['action'] = action(kind, phrase)
        context = build('m', 'm', [{'message_id': 'm', 'text': text}], [], read_only=readonly)
        token = self.gateway.open(context, readonly=readonly)
        return context, request, token

    def post(self, name, args, token):
        return self.client.post('/api/tools/' + name, json=args, headers={'Authorization': 'Bearer ' + token})

    def args(self, request):
        return {'source_account': 'B', 'target_account': 'A', 'amount_minor': 17300,
                'idempotency_key': 'transfer', 'request_binding': {'items': [request]}}

    def submit(self, request, token, nodes, mapping):
        return self.post('submit_task_plan', {'prompt': '按当前要求办理', 'title': '请求任务', 'nodes': nodes,
            'idempotency_key': 'task', 'request_binding': {'items': [request], 'node_items': mapping}}, token)

    def confirm(self, operation):
        return self.service.bank.user_action('confirm', {'operation_id': operation['operation_id'],
                                                      'expected_summary': operation['summary']})

    def balances(self):
        return self.service.bank.state()['accounts']

    def test_valid_http_prepare_keeps_floor_and_cannot_confirm_with_capability(self):
        _, request, token = self.transfer()
        before = self.balances()
        result = self.post('prepare_transfer', self.args(request), token)
        self.assertEqual(200, result.status_code, result.json)
        operation = result.json['data']['operation']
        self.assertEqual(80000, operation['summary']['params']['reserve_minor'])
        self.assertEqual(before, self.balances())
        rejected = self.client.post('/api/actions/confirm', json={}, headers={'Authorization': 'Bearer ' + token})
        self.assertEqual(403, rejected.status_code)
        self.assertEqual('WRONG_AUTH_CHANNEL', rejected.json['error']['code'])
        first = self.confirm(operation)
        after = self.balances()
        self.assertEqual('SUCCEEDED', first['status'])
        self.assertEqual(first['receipt']['receipt_id'], self.confirm(operation)['receipt']['receipt_id'])
        self.assertEqual(after, self.balances())
        from src.bank_evidence.service_render import operation_view
        from src.bank_tasks.progress import operation_details
        body = self.service.call_tool('operation', {'operation_id': operation['operation_id']})
        self.assertIn('800.00元', operation_view(body))
        self.assertIn('800.00元', operation_details(operation))

    def test_missing_forged_mismatched_and_analyze_requests_create_no_operation(self):
        _, request, token = self.transfer()
        baseline = self.service.bank.state()
        cases = []
        missing = self.args(request); missing.pop('request_binding'); cases.append((missing, 'INVALID_ARGUMENTS'))
        forged = self.args(deepcopy(request)); forged['request_binding']['items'][0]['action']['source']['message_id'] = 'fake'
        cases.append((forged, 'REQUEST_SOURCE_INVALID'))
        for field, value in [('source_account', 'A'), ('amount_minor', 19537), ('reserve_minor', 0)]:
            args = self.args(request); args[field] = value
            cases.append((args, 'REQUEST_PARAMETERS_MISMATCH'))
        for args, code in cases:
            with self.subTest(code=code, fields=list(args)):
                result = self.post('prepare_transfer', args, token)
                self.assertEqual(409, result.status_code, result.json)
                self.assertEqual(code, result.json['error']['code'])
        for kind in ('ANALYZE', 'CHECK_STATUS'):
            _, other, cap = self.transfer(kind=kind)
            result = self.post('prepare_transfer', self.args(other), cap)
            self.assertEqual('REQUEST_ACTION_FORBIDS_WRITE', result.json['error']['code'], result.json)
        self.assertEqual(baseline, self.service.bank.state())

    def test_schema_and_readonly_and_expiry_do_not_change_legacy_specs(self):
        before = deepcopy(self.service.tool_specs())
        _, request, token = self.transfer(readonly=True)
        headers = {'Authorization': 'Bearer ' + token}
        specs = self.client.get('/api/tool-schema', headers=headers).json['tools']
        self.assertNotIn('prepare_transfer', [s['name'] for s in specs])
        self.assertEqual(200, self.post('accounts', {}, token).status_code)
        self.assertEqual('REQUEST_READ_ONLY', self.post('prepare_transfer', self.args(request), token).json['error']['code'])
        self.gateway.close(token)
        self.assertEqual(403, self.post('accounts', {}, token).status_code)
        _, _, writable = self.transfer()
        prepared = next(s for s in self.gateway.specs(writable) if s['name'] == 'prepare_transfer')
        self.assertIn('request_binding', prepared['parameters']['required'])
        self.assertEqual(before, self.service.tool_specs())
        with patch.object(self.gateway, 'monotonic', return_value=10):
            expired = self.gateway.open(None, readonly=True, lifetime=1)
        with patch.object(self.gateway, 'monotonic', return_value=12):
            self.assertEqual(403, self.post('accounts', {}, expired).status_code)

    def test_floor_checked_again_at_confirm_after_real_other_transfer(self):
        _, request, token = self.transfer()
        operation = self.post('prepare_transfer', self.args(request), token).json['data']['operation']
        other = self.service.bank.call_tool('prepare_transfer', {'source_account': 'B', 'target_account': 'A',
                    'amount_minor': 3000, 'idempotency_key': 'other'})['data']['operation']
        self.confirm(other)
        before = self.balances()
        with self.assertRaises(DomainError) as caught:
            self.confirm(operation)
        self.assertEqual('RESERVE_SHORTFALL', caught.exception.code)
        self.assertEqual(before, self.balances())
        pending = self.service.bank.call_tool('operation', {'operation_id': operation['operation_id']})['data']['operation']
        self.assertEqual('PENDING', pending['status'])
        self.assertIsNone(pending['receipt'])

    def test_floor_failure_before_prepare_and_invalid_plan_are_structured(self):
        _, request, token = self.transfer(floor=90000)
        self.assertEqual('RESERVE_SHORTFALL', self.post('prepare_transfer', self.args(request), token).json['error']['code'])
        for nodes in ([{}], None, ['not-a-node']):
            result = self.submit(request, token, nodes, {})
            self.assertEqual(409, result.status_code, result.json)
            self.assertEqual('INVALID_ARGUMENTS', result.json['error']['code'])
        self.assertEqual([], self.service.tasks.list())
        self.assertEqual([], self.service.bank.state()['operations'])

    def test_task_contract_survives_restart_and_replan_cannot_drop_it(self):
        _, request, token = self.transfer()
        args = self.args(request); args.pop('request_binding'); args.pop('idempotency_key')
        nodes = [node('transfer', 'prepare_transfer', args)]
        created = self.submit(request, token, nodes, {'transfer': 'transfer'})
        self.assertEqual(200, created.status_code, created.json)
        task = created.json['data']['task']; identifier = task['task_id']
        self.gateway.close(token)
        self.restart()
        saved = self.service.tasks.get(identifier)
        self.assertEqual(task['request_contract'], saved['request_contract'])
        bad = deepcopy(nodes); bad[0]['args']['amount_minor'] = 19537
        with self.assertRaises(DomainError) as caught:
            self.service.call_tool('replan_task', {'task_id': identifier, 'expected_version': 1,
                                                'nodes': bad, 'reason': 'change'})
        self.assertEqual('REQUEST_PARAMETERS_MISMATCH', caught.exception.code)
        revised = self.service.call_tool('replan_task', {'task_id': identifier, 'expected_version': 1,
                                               'nodes': nodes, 'reason': 'same request'})['data']['task']
        self.assertEqual(task['request_contract'], revised['request_contract'])
        waiting = self.service.tasks.tick(identifier)
        self.assertEqual('WAITING_CONFIRMATION', waiting['state'], waiting)
        operation = self.service.bank.call_tool('operation', {'operation_id': waiting['nodes'][0]['operation_id']})['data']['operation']
        self.assertEqual(80000, operation['summary']['params']['reserve_minor'])
        self.confirm(operation)
        finished = self.service.tasks.tick(identifier)
        self.assertEqual('SUCCEEDED', finished['state'])
        before = self.balances()
        self.service.tasks.tick(identifier)
        self.assertEqual(before, self.balances())

    def test_reference_is_checked_after_resolution_before_bank_intent(self):
        _, request, token = self.transfer()
        nodes = [node('read', 'money_calculate', {'operation': 'subtract', 'currency': 'CNY',
                                               'left_minor': 20000, 'right_minor': 463}),
                 node('transfer', 'prepare_transfer', {'source_account': 'B', 'target_account': 'A',
                     'amount_minor': {'$ref': 'read.data.calculation.result.amount_minor'}}, ['read'])]
        created = self.submit(request, token, nodes, {'transfer': 'transfer'})
        self.assertEqual(200, created.status_code, created.json)
        identifier = created.json['data']['task']['task_id']
        self.service.tasks.tick(identifier)
        failed = self.service.tasks.tick(identifier)
        self.assertEqual('FAILED', failed['state'], failed)
        self.assertEqual('REQUEST_PARAMETERS_MISMATCH', failed['nodes'][1]['error']['code'], failed)
        self.assertFalse(self.service.tasks._was_invoked(failed['nodes'][1]['execution_key']))
        self.assertEqual([], self.service.bank.state()['operations'])

    def test_bound_task_without_policy_fails_closed(self):
        _, request, token = self.transfer()
        args = self.args(request); args.pop('request_binding'); args.pop('idempotency_key')
        created = self.submit(request, token, [node('transfer', 'prepare_transfer', args)], {'transfer': 'transfer'})
        identifier = created.json['data']['task']['task_id']
        old = self.service.tasks
        legacy = TaskService(old.path, clock=lambda: self.now, tool_specs=self.service.business_specs(),
                             call_tool=self.service.business_tool)
        failed = legacy.tick(identifier)
        self.assertEqual('REQUEST_POLICY_UNAVAILABLE', failed['nodes'][0]['error']['code'])
        self.assertFalse(legacy._was_invoked(failed['nodes'][0]['execution_key']))
        self.assertEqual([], self.service.bank.state()['operations'])

    def test_mandate_task_requires_query_confirmation_then_exact_selected_cancel(self):
        merchant = self.service.bank.state()['mandates'][0]['name']
        text = '先查询，再准备撤销' + merchant
        request = item({'merchant_text': slot('m', merchant, merchant)}, identifier='cancel', intent='mandate_cancel')
        request['action'] = action('PREPARE', '准备撤销')
        context = build('m', 'm', [{'message_id': 'm', 'text': text}], [], read_only=False)
        token = self.gateway.open(context, readonly=False)
        created = self.submit(request, token, plan(merchant), {'query': 'cancel', 'match': 'cancel', 'cancel': 'cancel'})
        self.assertEqual(200, created.status_code, created.json)
        identifier = created.json['data']['task']['task_id']
        waiting = self.service.tasks.tick(identifier)
        query = self.service.bank.call_tool('operation', {'operation_id': waiting['nodes'][0]['operation_id']})['data']['operation']
        self.assertEqual('mandate_query', query['kind'])
        self.assertEqual('WAITING_CONFIRMATION', waiting['state'])
        self.confirm(query)
        self.gateway.close(token)
        self.restart()
        for _ in range(3):
            waiting = self.service.tasks.tick(identifier)
        cancel = self.service.bank.call_tool('operation', {'operation_id': waiting['nodes'][2]['operation_id']})['data']['operation']
        self.assertEqual('mandate_cancel', cancel['kind'])
        self.assertEqual(merchant, cancel['summary']['mandate_snapshot']['name'])
        self.assertEqual('WAITING_CONFIRMATION', waiting['state'])
        self.confirm(cancel)
        for _ in range(2):
            finished = self.service.tasks.tick(identifier)
        self.assertEqual('SUCCEEDED', finished['state'], finished)

    def test_runner_revokes_capability_on_exception(self):
        context, _, token = self.transfer()
        self.gateway.close(token)
        runner = DshRunner('.', self.root, 'http://127.0.0.1:1', 'legacy', request_gateway=self.gateway)
        seen = []
        def model(*args, **kwargs):
            seen.append(kwargs['tool_token'])
            self.assertTrue(self.gateway.recognizes(seen[0]))
            raise RuntimeError('test model failure')
        with patch.object(runner, '_run_model', side_effect=model):
            with self.assertRaisesRegex(RuntimeError, 'test model failure'):
                runner.run_request(context['messages'][0]['text'], None, request_context=context)
        self.assertEqual(1, len(seen))
        self.assertFalse(self.gateway.recognizes(seen[0]))

    def test_arrangement_date_and_amount_hold_across_restart_and_day_change(self):
        request = item({'expense_minor': slot('m', '173.00元', 17300),
                        'date_text': slot('m', '明天', '明天'), 'purpose': slot('m', '书费', '书费')}, intent='arrangement')
        request['action'] = action('PREPARE', '准备安排')
        context = build('m', 'm', [{'message_id': 'm', 'text': '准备安排明天书费支出173.00元'}], [], read_only=False)
        token = self.gateway.open(context, readonly=False)
        args = {'title': '书费', 'kind': 'expense', 'amount_minor': 17300, 'due_date': '2026-10-20'}
        for changes in ({'amount_minor': 19537}, {'due_date': '2026-10-21'}, {'kind': 'income'}, {'title': '租金'}):
            result = self.post('propose_arrangement', {**args, **changes, 'idempotency_key': 'bad',
                                                     'request_binding': {'items': [request]}}, token)
            self.assertEqual('REQUEST_PARAMETERS_MISMATCH', result.json['error']['code'], result.json)
        submitted = self.submit(request, token, [node('arrange', 'propose_arrangement', args)], {'arrange': 'transfer'})
        self.assertEqual(200, submitted.status_code, submitted.json)
        identifier = submitted.json['data']['task']['task_id']
        self.now += timedelta(days=1)
        self.restart()
        waiting = self.service.tasks.tick(identifier)
        self.assertEqual('WAITING_CONFIRMATION', waiting['state'], waiting)
        operation = self.service.bank.call_tool('operation', {'operation_id': waiting['nodes'][0]['operation_id']})['data']['operation']
        self.assertEqual('2026-10-20', operation['summary']['params']['due_date'])
        self.confirm(operation)
        self.assertEqual('SUCCEEDED', self.service.tasks.tick(identifier)['state'])

    def test_account_normalization_requires_observation_from_this_run(self):
        accounts = self.service.bank.call_tool('accounts', {})
        source = next(a for a in accounts['data']['accounts'] if a['account_id'] == 'B')
        target = next(a for a in accounts['data']['accounts'] if a['account_id'] == 'A')
        text = f"从{source['name']}到{target['name']}转173.00元，准备转账"
        context = build('m', 'm', [{'message_id': 'm', 'text': text}], [], read_only=False)
        token = self.gateway.open(context, readonly=False)
        request = item({'amount_minor': slot('m', '173.00元', 17300)})
        request['action'] = action('PREPARE', '准备转账')
        for field, account in (('source_account', source), ('target_account', target)):
            request['slots'][field] = slot('m', account['name'], account['account_id'])
            # Current v2 selects its source evidence on the host, without a
            # model-supplied legacy normalization label. The same absence of
            # this run's accounts observation must still fail below.
        rejected = self.post('prepare_transfer', self.args(request), token)
        self.assertEqual('REQUEST_SOURCE_INVALID', rejected.json['error']['code'])
        self.assertEqual(200, self.post('accounts', {}, token).status_code)
        accepted = self.post('prepare_transfer', self.args(request), token)
        self.assertEqual(200, accepted.status_code, accepted.json)

    def test_task_mapping_is_required_before_persistence(self):
        _, request, token = self.transfer()
        args = self.args(request); args.pop('request_binding'); args.pop('idempotency_key')
        nodes = [node('transfer', 'prepare_transfer', args)]
        for mapping in ({}, {'transfer': 'unknown'}, {'transfer': 'transfer', 'extra': 'transfer'}):
            result = self.submit(request, token, nodes, mapping)
            self.assertEqual('REQUEST_NODE_UNBOUND', result.json['error']['code'], result.json)
        self.assertEqual([], self.service.tasks.list())

    def test_goal_draft_matches_request_without_confirming_goal(self):
        from tests.g2_goals import test_preview as fixtures
        fixture = fixtures.PreviewTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        service = fixture.service
        gateway = service.request_gateway
        message = '准备学习电脑草案，4500.00元，最低3000.00元，保留2000.00元，2026-11-22，先保证还款'
        request = item({'amount_minor': slot('m', '4500.00元', 450000),
                        'minimum_minor': slot('m', '3000.00元', 300000),
                        'reserve_minor': slot('m', '2000.00元', 200000),
                        'date_text': slot('m', '2026-11-22', '2026-11-22'),
                        'purpose': slot('m', '学习电脑', '学习电脑'),
                        'priority': slot('m', '先保证还款', '先保证还款')}, intent='goal_compare')
        request['action'] = action('PREPARE', message)
        context = build('m', 'm', [{'message_id': 'm', 'text': message}], [], read_only=False)
        token = self.host_open(fixture.app,gateway.open,context,readonly=False)
        args = {'profile': fixture.profile, 'expected_revision': 0, 'idempotency_key': 'goal-gate',
                'request_binding': {'items': [request]}}
        before = service.goals.state()
        for key, value in [('preferred_amount_minor', 440000), ('reserve_floor_minor', 100000),
                           ('preferred_date', '2026-11-23')]:
            bad = deepcopy(args); bad['profile'][key] = value
            with self.assertRaises(DomainError) as caught:
                gateway.call(token, 'propose_goal', bad)
            self.assertEqual('REQUEST_PARAMETERS_MISMATCH', caught.exception.code)
        result = gateway.call(token, 'propose_goal', args)
        self.assertTrue(result['data']['draft_id'])
        self.assertEqual(before, service.goals.state())

    def test_direct_prepare_trace_delivers_same_bound_operation(self):
        from tests.competition_ai.test_request_decision import response
        from src.bank_requests.response import validate
        context, request, token = self.transfer(floor=None)
        trace = []
        def tool(name, args):
            result = self.post(name, args, token)
            self.assertEqual(200, result.status_code, result.json)
            identity = str(len(trace))
            trace.extend([{'kind': 'request', 'name': name, 'arguments': args, 'call_id': identity},
                          {'kind': 'response', 'tool': name, 'args': args, 'call_id': identity,
                           'ok': True, 'status': 200, 'response': result.json}])
            return result.json
        prepared = tool('prepare_transfer', self.args(request))['data']['operation']
        read = tool('operation', {'operation_id': prepared['operation_id']})
        raw = response(context, [request], {'format': 'bank_operation_v1', 'operation_id': prepared['operation_id'],
                                          'evidence_id': read['evidence_id']})
        raw['trace'] = trace
        from src.bank_requests.attempt_projection import collect
        context['_host_attempt_coverage']=collect(self.gateway.store,self.gateway.store.for_job(context['request_id']),trace)
        delivered = validate(raw, context)
        self.assertTrue(delivered['ok'], delivered)
        self.assertEqual('WAITING_CONFIRMATION', delivered['business_outcomes'][0]['execution'])


if __name__ == '__main__':
    unittest.main()
