"""Cross-turn object identity with real isolated bank/queue and constructed AI.

These tests prove host contracts, not live language understanding.
"""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from tests.competition_ai import test_service_answer as fixtures
from tests.competition_ai.test_request_decision import item, slot, response
from src.bank_requests.response import validate
from src.bank_requests.schema import FIELDS, model_contract
from src.bank_requests.context import model_prompt
from src.bank_v1_web.jobs import JobStore


class RequestObjectTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.ServiceAnswerTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.jobs = JobStore(Path(self.f.temp.name) / 'jobs.sqlite3', clock=lambda: self.f.now)
        self.count = 0

    def begin(self, text, kind, intent='transfer', parent=None):
        self.f.trace = []
        job = self.jobs.add(text)
        request = item(intent=intent, parent=parent)
        request['action'] = {'kind': kind, 'source': {'message_id': job['id'], 'quote': text}}
        return job, request

    def finish(self, job, request, parts, *, broken=False):
        context = self.jobs.request_context(job['id'])
        context['require_current_action'] = True
        raw = response(context, [request], {'format': 'bank_outcomes_v1', 'items': [
            {'item_id': request['id'], 'components': parts}]})
        raw['trace'] = deepcopy(self.f.trace)
        if broken:
            raw['text'] = 'model failed after tool execution'
        result = validate(raw, context)
        self.jobs.finish(job['id'], result)
        return result

    def prepare(self, amount=17300, parent=None, *, broken=False, twice=False):
        text = f'准备从A转到B {amount // 100}.{amount % 100:02d}元'
        job, request = self.begin(text, 'PREPARE', parent=parent)
        request['slots'] = {'source_account': slot(job['id'], 'A', 'A'), 'target_account': slot(job['id'], 'B', 'B'),
                            'amount_minor': slot(job['id'], text.split()[-1], amount)}
        operations, parts = [], []
        for _ in range(2 if twice else 1):
            self.count += 1
            op = self.f.tool('prepare_transfer', {'source_account': 'A', 'target_account': 'B', 'amount_minor': amount,
                                                  'idempotency_key': 'object-test-' + str(self.count)})['data']['operation']
            operations.append(op); parts.append(self.f.part(op))
        result = self.finish(job, request, parts, broken=broken)
        self.assertEqual(not broken, result['ok'], result)
        return job['id'] + ':transfer', operations, result

    def query(self, parent, op, text='查原操作的结果'):
        job, request = self.begin(text, 'CHECK_STATUS', parent=parent)
        result = self.finish(job, request, [self.f.part(op)])
        return job, result

    def clarify(self, parent=None, intent='transfer'):
        job, request = self.begin('请核对原来的结果', 'CHECK_STATUS', intent=intent, parent=parent)
        return self.finish(job, request, [{'format': 'request_clarification_v1', 'item_ids': [request['id']]}])

    def test_rejected_original_survives_queue_restart_without_repreparing(self):
        parent, ops, _ = self.prepare(); op = ops[0]
        self.f.service.bank.user_action('reject', {'operation_id': op['operation_id']})
        self.jobs = JobStore(self.jobs.path, clock=lambda: self.f.now)
        before = self.f.service.bank.state()
        job, result = self.query(parent, op)
        self.assertTrue(result['ok'], result)
        self.assertEqual('REJECTED', result['business_outcomes'][0]['execution'])
        self.assertEqual(['operation'], [r['name'] for r in self.f.trace if r['kind'] == 'request'])
        self.assertEqual(before, self.f.service.bank.state())
        current = self.jobs.add('再查一次')
        linked = next(i for i in self.jobs.request_context(current['id'])['previous_items'] if i['reference'] == job['id'] + ':transfer')
        self.assertEqual(op['operation_id'], linked['related_objects'][0]['object_id'])
        self.assertNotIn('status', linked['related_objects'][0])

    def test_same_amount_different_operation_cannot_substitute_identity(self):
        first, ops1, _ = self.prepare(); _, ops2, _ = self.prepare()
        before = self.f.service.bank.state()
        _, bad = self.query(first, ops2[0])
        self.assertFalse(bad['ok'])
        self.assertEqual('STATUS_OBJECT_NOT_ASSOCIATED_WITH_REQUEST', bad['answer_validation']['reason'])
        _, good = self.query(first, ops1[0])
        self.assertTrue(good['ok'], good)
        self.assertEqual(before, self.f.service.bank.state())

    def test_revised_parameters_do_not_rewrite_old_operation_and_old_request_can_be_read(self):
        parent, old, _ = self.prepare()
        newer, new, _ = self.prepare(97525, parent=parent)
        _, result = self.query(parent, old[0])
        self.assertTrue(result['ok'], result)
        self.assertIn('173.00', result['text'])
        _, wrong = self.query(newer, old[0])
        self.assertEqual('STATUS_OBJECT_NOT_ASSOCIATED_WITH_REQUEST', wrong['answer_validation']['reason'])
        job, request = self.begin('按原参数再准备', 'PREPARE', parent=parent)
        failed = self.finish(job, request, [self.f.part(new[0])])
        self.assertEqual('SUPERSEDED_PARENT_ITEM', failed['answer_validation']['reason'])

    def test_changed_analysis_can_still_check_original_actual_amount(self):
        parent, ops, _ = self.prepare()
        job, request = self.begin('改成195.37元，先分析', 'ANALYZE', parent=parent)
        request['slots'] = {'amount_minor': slot(job['id'], '195.37元', 19537)}
        body = self.f.tool('accounts', {})
        facts = {'format': 'bank_facts_v1', 'fact_ids': [body['fact_catalog']['facts'][0]['fact_id']], 'calculation_ids': []}
        analyzed = self.finish(job, request, [facts]); self.assertTrue(analyzed['ok'], analyzed)
        _, result = self.query(job['id'] + ':transfer', ops[0])
        self.assertTrue(result['ok'], result)
        self.assertIn('173.00', result['text'])
        self.assertNotIn('195.37', result['text'])

    def test_multiple_records_require_clarification_or_exact_selection(self):
        parent, ops, _ = self.prepare(twice=True)
        result = self.clarify(parent)
        self.assertTrue(result['ok'], result); self.assertIn('多笔', result['text'])
        _, bad = self.query(parent, ops[0])
        self.assertEqual('AMBIGUOUS_OBJECT', bad['answer_validation']['reason'])
        _, right = self.query(parent, ops[0], '核对 ' + ops[0]['operation_id'])
        self.assertTrue(right['ok'], right)
        _, wrong = self.query(parent, ops[1], '核对 ' + ops[0]['operation_id'])
        self.assertEqual('STATUS_OBJECT_DIFFERS_FROM_EXPLICIT_SELECTION', wrong['answer_validation']['reason'])

    def test_missing_object_is_not_missing_new_transfer_amount(self):
        result = self.clarify()
        self.assertTrue(result['ok'], result)
        self.assertIn('哪次', result['text']); self.assertNotIn('请补充：金额', result['text'])
        self.assertEqual('CLARIFY', result['business_outcomes'][0]['next_step'])

    def test_bad_internal_id_slot_is_still_rejected(self):
        parent, ops, _ = self.prepare()
        job, request = self.begin('查刚才那笔', 'CHECK_STATUS', parent=parent)
        request['slots'] = {'task_id': slot(job['id'], '刚才那笔', ops[0]['operation_id'])}
        result = self.finish(job, request, [self.f.part(ops[0])])
        self.assertEqual('UNKNOWN_SLOT_FIELD', result['answer_validation']['reason'])

    def test_failed_current_object_read_is_unknown_not_success_or_new_preparation(self):
        parent, ops, _ = self.prepare()
        job, request = self.begin('核对原操作', 'CHECK_STATUS', parent=parent)
        args = {'operation_id': ops[0]['operation_id']}
        self.f.trace = [{'kind': 'request', 'call_id': 'failure', 'name': 'operation', 'arguments': args},
            {'kind': 'response', 'call_id': 'failure', 'tool': 'operation', 'args': args,
             'status': 503, 'ok': False, 'response': {'error': {'code': 'UNAVAILABLE'}}}]
        part = {'format': 'bank_tool_outcome_v1', 'tool': 'operation', 'arguments': args, 'error_code': 'UNAVAILABLE'}
        result = self.finish(job, request, [part]); self.assertTrue(result['ok'], result)
        self.assertEqual('UNKNOWN', result['business_outcomes'][0]['execution'])
        self.assertEqual('RECONCILE_ORIGINAL', result['business_outcomes'][0]['next_step'])

    def test_failed_delivery_exposes_only_trace_candidate_then_read_recovers(self):
        parent, ops, _ = self.prepare(broken=True)
        before = self.f.service.bank.state()
        job, request = self.begin('核对上次失败留下的操作', 'CHECK_STATUS')
        context = self.jobs.request_context(job['id'])
        self.assertEqual([], context['previous_items'])
        self.assertEqual(ops[0]['operation_id'], context['recovery_objects'][0]['object_id'])
        self.assertEqual('TOOL_TRACE_CANDIDATE', context['recovery_objects'][0]['confidence'])
        result = self.finish(job, request, [self.f.part(ops[0])])
        self.assertTrue(result['ok'], result)
        self.assertEqual('WAITING_CONFIRMATION', result['business_outcomes'][0]['execution'])
        self.assertEqual(before, self.f.service.bank.state())

    def test_incomplete_trace_or_model_prose_is_not_an_object_source(self):
        for trace in ([], [{'kind': 'request', 'call_id': 'x', 'name': 'prepare_transfer', 'arguments': {}}]):
            job = self.jobs.add('办理请求')
            self.jobs.finish(job['id'], {'ok': False, 'text': '已生成 op_fake', 'trace': trace})
        next_job = self.jobs.add('核对原操作')
        context = self.jobs.request_context(next_job['id'])
        self.assertFalse(context['recovery_objects'])
        self.assertEqual(2, len(context['recovery_notices']))

    def test_task_references_keep_bank_operations_as_children(self):
        job, request = self.begin('建立调拨任务', 'PREPARE', intent='task_status')
        task = self.f.tool('submit_task_plan', {'prompt': '调拨', 'title': '调拨', 'idempotency_key': 'objects-task',
            'nodes': [{'id': 'move', 'tool': 'prepare_transfer', 'args': {'source_account': 'A', 'target_account': 'B',
                      'amount_minor': 17300}, 'depends_on': []}]})['data']['task']
        self.f.service.tasks.tick(task['task_id'])
        progress = self.f.tool('task_status', {'task_id': task['task_id']})['data']['task_progress']
        part = {'format': 'task_progress_v1', 'task_id': task['task_id'], 'source_id': progress['source_id']}
        result = self.finish(job, request, [part]); self.assertTrue(result['ok'], result)
        later, check = self.begin('核对原任务', 'CHECK_STATUS', intent='task_status', parent=job['id'] + ':transfer')
        parent = self.jobs.request_context(later['id'])['previous_items'][0]
        self.assertEqual({'task', 'operation'}, {v['kind'] for v in parent['related_objects']})
        operation = next(iter(progress['data']['operations'].values()))
        wrong = self.finish(later, check, [self.f.part(operation)])
        self.assertEqual('WRONG_STATUS_OBJECT_TYPE', wrong['answer_validation']['reason'])
        current, check = self.begin('核对原任务', 'CHECK_STATUS', intent='task_status', parent=job['id'] + ':transfer')
        current_progress = self.f.tool('task_status', {'task_id': task['task_id']})['data']['task_progress']
        part['source_id'] = current_progress['source_id']
        right = self.finish(current, check, [part]); self.assertTrue(right['ok'], right)

    def test_model_contract_uses_validator_fields_and_transport_keeps_current_source(self):
        contract = json.loads(model_contract())
        self.assertEqual({key: sorted(value) for key, value in FIELDS.items()},
                         {key: sorted(set(value['allowed_user_slots']) |
                                      set(value.get('readonly_preview_references', {}).get('fields', [])))
                          for key, value in contract.items()})
        job = self.jobs.add('只查状态')
        prompt, context = model_prompt(self.jobs.request_context(job['id']))
        self.assertIn(model_contract(), prompt)
        self.assertEqual(context, json.loads(prompt.split('宿主上下文：\n', 1)[1]))
        self.assertNotIn('一般文本按需要', prompt)

    def test_truncated_history_does_not_guess_from_model_session(self):
        parent, _, _ = self.prepare()
        for _ in range(9):
            job = self.jobs.add('另一个问题'); self.jobs.finish(job['id'], {'ok': True})
        job, request = self.begin('核对很久之前那次', 'CHECK_STATUS')
        context = self.jobs.request_context(job['id'])
        self.assertTrue(context['truncated']); self.assertFalse(context['previous_items'])
        result = self.finish(job, request, [{'format': 'request_clarification_v1', 'item_ids': ['transfer']}])
        self.assertTrue(result['ok'], result)


class GoalObjectTests(unittest.TestCase):
    def test_goal_and_draft_remain_distinct_across_turns(self):
        from tests.g2_goals import test_service as fixtures
        f = fixtures.G2Tests(); f.setUp(); self.addCleanup(f.doCleanups)
        job = f.jobs.add('准备一个目标草案')
        context = f.jobs.request_context(job['id'])
        # This exercises the retained legacy interpreter on explicit domain fixtures.
        context['source_contract'] = 'legacy_source_v1'
        request = item(intent='goal_compare')
        request['action'] = {'kind': 'PREPARE', 'source': {'message_id': job['id'], 'quote': job['prompt']}}
        args = {'expected_revision': 1, 'profile': f.profile, 'idempotency_key': 'object-draft'}
        body = f.service.call_tool('propose_goal', args)
        raw = response(context, [request], {'format': 'goal_draft_v1', 'draft_id': body['data']['draft_id']})
        raw['trace'] = fixtures.trace_for(body, args, 'propose_goal')
        result = validate(raw, context); self.assertTrue(result['ok'], result); f.jobs.finish(job['id'], result)
        current = f.jobs.add('核对刚才草案的状态')
        context = f.jobs.request_context(current['id'])
        context['source_contract'] = 'legacy_source_v1'
        self.assertEqual('goal_draft', context['previous_items'][0]['related_objects'][0]['kind'])
        request = item(intent='goal_followup', parent=job['id'] + ':transfer')
        request['action'] = {'kind': 'CHECK_STATUS', 'source': {'message_id': current['id'], 'quote': current['prompt']}}
        confirmed = f.fresh(); answer = json.loads(fixtures.stub_result(confirmed)['text'])
        raw = response(context, [request], answer); raw['trace'] = fixtures.trace_for(confirmed)
        bad = validate(raw, context)
        self.assertEqual('DRAFT_STATUS_TOOL_UNAVAILABLE', bad['answer_validation']['reason'])
        raw = response(context, [request], {'format': 'request_clarification_v1', 'item_ids': ['transfer']})
        clear = validate(raw, context); self.assertTrue(clear['ok'], clear)
        self.assertIn('不能用已确认目标', clear['text'])
        # A separate user request asks about the confirmed goal, not the draft.
        current = f.jobs.add('查询当前已确认的重点目标')
        context = f.jobs.request_context(current['id'])
        context['source_contract'] = 'legacy_source_v1'
        request['continuation_of'] = None
        request['action']['source'] = {'message_id': current['id'], 'quote': current['prompt']}
        raw = response(context, [request], answer); raw['trace'] = fixtures.trace_for(confirmed)
        good = validate(raw, context); self.assertTrue(good['ok'], good)


if __name__ == '__main__':
    unittest.main()
