"""Current-turn action provenance and actual tool/result consistency; offline only."""
from copy import deepcopy
import unittest

from tests.competition_ai import test_request_delivery as deliveries
from tests.competition_ai import test_request_runner as runners
from tests.competition_ai.test_request_decision import item, slot, response
from src.bank_requests.context import build
from src.bank_requests.decision import validate
from src.bank_requests.response import validate as validate_response


def action(kind, quote, message='m'):
    return {'kind': kind, 'source': {'message_id': message, 'quote': quote}}


class RequestActionTests(unittest.TestCase):
    def setUp(self):
        self.d = deliveries.RequestDeliveryTests(); self.d.setUp()
        self.addCleanup(self.d.doCleanups)

    def test_full_parameters_do_not_automatically_mean_prepare(self):
        part = self.d.calculation(97525)
        for kind, clause, expected in [('ANALYZE', '先核对金额', 'REVIEW_ADVICE'),
                                      ('PREPARE', '准备这笔转账', 'PREPARE_REQUEST'),
                                      ('CHECK_STATUS', '查原操作状态', 'READ_CURRENT'),
                                      ('UNDETERMINED', '再看看', 'REVIEW_ADVICE')]:
            with self.subTest(kind=kind):
                request = self.d.request(97525, 'B', 'A')
                request['action'] = action(kind, clause)
                result = self.d.answer([request], {'transfer': [part]}, prompt='从B到A，975.25元，' + clause)
                self.assertTrue(result['ok'], result)
                outcome = result['business_outcomes'][0]
                self.assertEqual(expected, outcome['next_step'])
                self.assertEqual(kind, outcome['effective_action'])
                if kind == 'ANALYZE':
                    self.assertIn('24.75', result['text'])
                    self.assertEqual('NOT_REQUESTED', outcome['execution'])
                    self.assertNotIn('需要继续办理', result['text'])
                if kind == 'CHECK_STATUS':
                    self.assertEqual('NEEDS_EVIDENCE', outcome['assessment'])
        legacy = self.d.answer([self.d.request(97525, 'B', 'A')], {'transfer': [part]})
        self.assertEqual('LEGACY_UNSPECIFIED', legacy['business_outcomes'][0]['action_contract'])
        self.assertNotIn('需要继续办理', legacy['text'])

    def test_action_is_current_not_inherited_and_source_cannot_be_forged(self):
        messages = [{'message_id': 'one', 'text': '从A转到B，173.00元，准备'},
                    {'message_id': 'two', 'text': '先不办理，只核对'}]
        first = item({'source_account': slot('one', 'A', 'A'), 'target_account': slot('one', 'B', 'B'),
                      'amount_minor': slot('one', '173.00元', 17300)})
        first['action'] = action('PREPARE', '准备', 'one')
        c1 = build('one', 'one', messages[:1], [], read_only=False)
        parent = {**validate([first], c1)['items'][0], 'reference': 'one:transfer'}
        c2 = build('two', 'two', messages, [parent], read_only=False)
        next_item = item(parent='one:transfer')
        derived = validate([next_item], c2)['items'][0]
        self.assertEqual('UNDETERMINED', derived['action']['kind'])
        self.assertEqual(17300, derived['slots']['amount_minor']['value'])
        for source in [action('PREPARE', '准备', 'one'), action('PREPARE', '现在准备', 'two'),
                       action('PREPARE', '准备', 'assistant')]:
            next_item['action'] = source
            with self.assertRaisesRegex(ValueError, 'ACTION_REQUIRES_CURRENT_USER_SOURCE'):
                validate([next_item], c2)
        next_item['action'] = action('ANALYZE', '先不办理，只核对', 'two')
        self.assertEqual('ANALYZE', validate([next_item], c2)['items'][0]['action']['effective_kind'])

    def test_advice_to_preparation_keeps_parameters_and_reserve_constraint(self):
        messages = [{'message_id': 'one', 'text': '从A到B 173.00元，还要保留100.00元，先分析'},
                    {'message_id': 'two', 'text': '就按这些条件准备'}]
        request = item({'source_account': slot('one', 'A', 'A'), 'target_account': slot('one', 'B', 'B'),
                        'amount_minor': slot('one', '173.00元', 17300), 'reserve_minor': slot('one', '100.00元', 10000)},
                       intent='transfer_advice')
        request['action'] = action('ANALYZE', '先分析', 'one')
        parent = {**validate([request], build('one', 'one', messages[:1], [], read_only=False))['items'][0],
                  'reference': 'one:transfer'}
        context = build('two', 'two', messages, [parent], read_only=False)
        prepare = item(parent='one:transfer')
        with self.assertRaisesRegex(ValueError, 'CONTINUATION_INTENT_CHANGED'):
            validate([prepare], context)
        prepare['action'] = action('PREPARE', '就按这些条件准备', 'two')
        actual = validate([prepare], context)['items'][0]
        self.assertEqual('transfer', actual['intent'])
        self.assertEqual(10000, actual['slots']['reserve_minor']['value'])
        self.assertEqual(17300, actual['slots']['amount_minor']['value'])

    def test_analyze_status_and_undetermined_cannot_hide_preparation(self):
        op = self.d.f.transfer(); part = self.d.f.part(op)
        before = self.d.f.service.bank.state()
        for kind in ('ANALYZE', 'CHECK_STATUS', 'UNDETERMINED'):
            request = self.d.request(); request['action'] = action(kind, '本轮要求')
            result = self.d.answer([request], {'transfer': [part]}, prompt='A B 173.00元 本轮要求')
            self.assertFalse(result['ok'])
            self.assertEqual('REQUEST_ACTION_WRITE_CONTRADICTION', result['answer_validation']['reason'])
            self.assertIn('不会自动撤回', result['error'])
        self.assertEqual(before, self.d.f.service.bank.state())  # Rejection never claims rollback/pre-call prevention.
        request['action'] = action('PREPARE', '本轮要求')
        allowed = self.d.answer([request], {'transfer': [part]}, prompt='A B 173.00元 本轮要求')
        self.assertTrue(allowed['ok'], allowed)
        self.d.f.trace = []
        request['action'] = action('CHECK_STATUS', '查原操作')
        observed = self.d.answer([request], {'transfer': [self.d.f.part(op)]}, prompt='A B 173.00元 查原操作')
        self.assertTrue(observed['ok'], observed)
        self.assertEqual('WAITING_CONFIRMATION', observed['business_outcomes'][0]['execution'])

    def test_failed_prepare_is_still_an_action_contradiction(self):
        part = self.d.failure({'source_account': 'B', 'target_account': 'A', 'amount_minor': 128050, 'idempotency_key': 'refused'})
        request = self.d.request(128050, 'B', 'A'); request['action'] = action('ANALYZE', '只分析')
        result = self.d.answer([request], {'transfer': [part]}, prompt='B A 1280.50元 只分析')
        self.assertEqual('REQUEST_ACTION_WRITE_CONTRADICTION', result['answer_validation']['reason'])

    def test_mixed_analysis_and_prepare_remain_per_item(self):
        part = self.d.calculation(97525)
        op = self.d.f.transfer()
        analyze = self.d.request(97525, 'B', 'A', 'analysis'); analyze['action'] = action('ANALYZE', '这笔只分析')
        prepare = self.d.request(); prepare['action'] = action('PREPARE', '另一笔准备')
        result = self.d.answer([analyze, prepare], {'analysis': [part], 'transfer': [self.d.f.part(op)]},
                              prompt='B A 975.25元这笔只分析；A B 173.00元另一笔准备')
        self.assertTrue(result['ok'], result)
        self.assertEqual(['NOT_REQUESTED', 'WAITING_CONFIRMATION'], [o['execution'] for o in result['business_outcomes']])

    def test_host_readonly_overrides_prepare_without_inventing_authority(self):
        part = self.d.calculation(97525)
        prompt = 'B A 975.25元 准备转账'
        context = build('m', 'm', [{'message_id': 'm', 'text': prompt}], [], read_only=True)
        request = self.d.request(97525, 'B', 'A'); request['action'] = action('PREPARE', '准备转账')
        raw = response(context, [request], part); raw['trace'] = self.d.f.trace
        result = validate_response(raw, context)
        self.assertTrue(result['ok'], result)
        outcome = result['business_outcomes'][0]
        self.assertEqual(('PREPARE', 'ANALYZE', True), (outcome['requested_action'], outcome['effective_action'], outcome['host_read_only']))
        self.assertEqual('REVIEW_ADVICE', outcome['next_step'])

    def test_product_runner_rejects_missing_action_without_paid_repair(self):
        f = runners.RequestRunnerTests(); f.setUp(); self.addCleanup(f.doCleanups)
        job = f.jobs.add('帮我转账')
        raw = response(f.jobs.request_context(job['id']), [item(missing=['source_account', 'target_account', 'amount_minor'])],
                       {'format': 'request_clarification_v1', 'item_ids': ['transfer']})
        f.runner._run_model.return_value = raw
        f.worker.step()
        saved = f.jobs.get(job['id'])
        self.assertEqual('FAILED', saved['status'])
        self.assertEqual('CURRENT_REQUEST_ACTION_REQUIRED', saved['result']['answer_validation']['reason'])
        self.assertEqual(1, f.runner._run_model.call_count)

    def test_analysis_task_can_read_but_cannot_plan_bank_writes(self):
        for tool in ('accounts', 'prepare_transfer'):
            with self.subTest(tool=tool):
                self.d.f.trace = []
                args = {} if tool == 'accounts' else {'source_account': 'A', 'target_account': 'B', 'amount_minor': 17300}
                task = self.d.f.tool('submit_task_plan', {'prompt': '只核对', 'title': '分析', 'idempotency_key': tool,
                    'nodes': [{'id': 'node', 'tool': tool, 'args': args, 'depends_on': []}]})['data']['task']
                progress = self.d.f.tool('task_status', {'task_id': task['task_id']})['data']['task_progress']
                part = {'format': 'task_progress_v1', 'task_id': task['task_id'], 'source_id': progress['source_id']}
                request = item(intent='task_status'); request['action'] = action('ANALYZE', '只分析')
                result = self.d.answer([request], {'transfer': [part]}, prompt='只分析')
                self.assertEqual(tool == 'accounts', result['ok'], result)
                request['action'] = action('CHECK_STATUS', '只查状态')
                result = self.d.answer([request], {'transfer': [part]}, prompt='只查状态')
                self.assertEqual('REQUEST_ACTION_WRITE_CONTRADICTION', result['answer_validation']['reason'])

    def test_goal_draft_needs_prepare_and_is_never_confirmed_goal_or_payment(self):
        from tests.g2_goals import test_service as goals
        g = goals.G2Tests(); g.setUp(); self.addCleanup(g.doCleanups)
        args = {'expected_revision': 1, 'profile': g.profile, 'idempotency_key': 'draft-action'}
        body = g.service.call_tool('propose_goal', args)
        context = build('m', 'm', [{'message_id': 'm', 'text': '准备草案；只分析'}], [], read_only=False)
        part = {'format': 'goal_draft_v1', 'draft_id': body['data']['draft_id']}
        request = item(intent='goal_compare'); request['action'] = action('PREPARE', '准备草案')
        raw = response(context, [request], part); raw['trace'] = goals.trace_for(body, args, 'propose_goal')
        result = validate_response(raw, context)
        self.assertTrue(result['ok'], result)
        self.assertEqual('DRAFT', result['business_outcomes'][0]['goal_state'])
        self.assertEqual('NOT_REQUESTED', result['business_outcomes'][0]['execution'])
        request['action'] = action('ANALYZE', '只分析')
        raw = response(context, [request], part); raw['trace'] = goals.trace_for(body, args, 'propose_goal')
        result = validate_response(raw, context)
        self.assertEqual('REQUEST_ACTION_WRITE_CONTRADICTION', result['answer_validation']['reason'])

    def test_known_refusal_then_new_preparation_keeps_both_actual_results(self):
        args = {'source_account': 'B', 'target_account': 'A', 'amount_minor': 128050, 'idempotency_key': 'now-funded'}
        failed = self.d.failure(args)
        self.d.f.service.bank.user_action('inject_transaction', {'kind': 'income', 'account_id': 'B', 'amount_minor': 50000,
            'idempotency_key': 'funded', 'description': '补足资金'})
        op = self.d.f.tool('prepare_transfer', args)['data']['operation']
        request = self.d.request(128050, 'B', 'A'); request['action'] = action('PREPARE', '准备')
        result = self.d.answer([request], {'transfer': [failed, self.d.f.part(op)]}, prompt='B A 1280.50元 准备')
        self.assertTrue(result['ok'], result)
        self.assertEqual('WAITING_VERIFICATION', result['business_outcomes'][0]['execution'])
        self.assertIn('有一次失败尝试', result['text'])
        self.assertIn(op['operation_id'], result['text'])
