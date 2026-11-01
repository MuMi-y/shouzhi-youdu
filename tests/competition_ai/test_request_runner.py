"""Real queue/tools with constructed model outputs; not a live AI acceptance run."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import Mock

from tests.competition_ai import test_service_answer as bank_fixture
from tests.competition_ai.test_request_decision import item, slot, response
from src.bank_v1_web.jobs import JobStore
from src.bank_v1_web.worker import DshRunner, Worker


class RequestRunnerTests(unittest.TestCase):
    def setUp(self):
        self.f = bank_fixture.ServiceAnswerTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.jobs = JobStore(Path(self.f.temp.name) / 'jobs.sqlite3', clock=lambda: self.f.now,handoff=True)
        self.addCleanup(self.jobs.handoff.release_owner)
        self.f.service.request_gateway.store=self.jobs.handoff
        self.jobs.set_setting('schedule', {'enabled': False, 'cadence': 'daily'})
        self.runner = DshRunner('.', '.', 'http://127.0.0.1:1', 'unused', display_service=self.f.service)
        self.runner._run_model = Mock()
        self.runner._run_model.side_effect=self._scoped_model_fixture
        self.worker = Worker(self.f.service, self.jobs, self.runner, clock=lambda: self.f.now)

    def _scoped_model_fixture(self, prompt, session, **options):
        raw=deepcopy(self.runner._run_model.return_value)
        # Constructed read traces now cross the same scoped audit boundary.
        # These fixtures contain read-only calculations, never replay writes.
        from src.bank_requests.gateway import READ_ONLY
        gateway=self.f.service.request_gateway
        for record in raw.get('trace',[]):
            if record.get('kind')=='request':
                self.assertIn(record['name'],READ_ONLY)
                gateway.call(options['tool_token'],record['name'],record['arguments'])
        return raw

    def finish_clarification(self):
        job = self.jobs.add('从A转到B')
        context = self.jobs.request_context(job['id'])
        request = item({'source_account': slot(job['id'], 'A', 'A'),
                        'target_account': slot(job['id'], 'B', 'B')}, ['amount_minor'])
        request['action'] = {'kind': 'PREPARE', 'source': {'message_id': job['id'], 'quote': job['prompt']}}
        self.runner._run_model.return_value = response(context, [request], {
            'format': 'request_clarification_v1', 'item_ids': ['transfer']})
        before = self.f.service.bank.state()
        self.assertTrue(self.worker.step())
        saved = self.jobs.get(job['id'])
        self.assertEqual('DONE', saved['status'], saved['result'])
        self.assertIn('请补充：金额', saved['result']['text'])
        self.assertEqual(before, self.f.service.bank.state())
        return job

    def calculation_output(self, context, request, amount, quote):
        current = next(m for m in context['messages'] if m['message_id'] == context['current_message_id'])
        request = {**request, 'action': {'kind': 'ANALYZE', 'source': {'message_id': current['message_id'], 'quote': current['text']}}}
        self.f.trace = []
        accounts = self.f.tool('accounts', {})
        fact = next(row for row in accounts['fact_catalog']['facts'] if row['entity'] == 'A')
        calculation = self.f.tool('money_calculate', {
            'operation': 'subtract', 'currency': 'CNY', 'left_minor': fact['value'],
            'left_source': {'kind': 'bank_fact', 'tool': 'accounts', 'arguments': {}, 'fact_id': fact['fact_id']},
            'right_minor': amount, 'right_source': {'kind': 'user_assumption', 'quote': quote}})
        raw = response(context, [request], {'format': 'bank_facts_v1', 'fact_ids': [fact['fact_id']],
                                          'calculation_ids': [calculation['evidence_id']]})
        return {**raw, 'trace': deepcopy(self.f.trace)}

    def test_queue_restart_fill_revision_and_real_calculation_share_context(self):
        first = self.finish_clarification()
        self.jobs.handoff.release_owner()
        self.jobs = JobStore(self.jobs.path, clock=lambda: self.f.now,handoff=True)
        self.addCleanup(self.jobs.handoff.release_owner)
        self.f.service.request_gateway.store=self.jobs.handoff
        self.worker = Worker(self.f.service, self.jobs, self.runner, clock=lambda: self.f.now)
        second = self.jobs.add('1280.50元')
        c2 = self.jobs.request_context(second['id'])
        self.assertEqual(first['id'] + ':transfer', c2['previous_items'][0]['reference'])
        i2 = item({'amount_minor': slot(second['id'], '1280.50元', 128050)}, parent=first['id'] + ':transfer')
        self.runner._run_model.return_value = self.calculation_output(c2, i2, 128050, '1280.50元')
        before = self.f.service.bank.state()
        self.worker.step()
        saved = self.jobs.get(second['id'])
        self.assertEqual('DONE', saved['status'], saved['result'])
        self.assertEqual(128050, saved['result']['request_decision']['items'][0]['slots']['amount_minor']['value'])
        third = self.jobs.add('改为975.25元')
        c3 = self.jobs.request_context(third['id'])
        i3 = item({'amount_minor': slot(third['id'], '975.25元', 97525)}, parent=second['id'] + ':transfer')
        self.runner._run_model.return_value = self.calculation_output(c3, i3, 97525, '975.25元')
        self.worker.step()
        saved = self.jobs.get(third['id'])
        self.assertEqual('DONE', saved['status'], saved['result'])
        slots = saved['result']['request_decision']['items'][0]['slots']
        self.assertEqual('A', slots['source_account']['value'])
        self.assertEqual(97525, slots['amount_minor']['value'])
        self.assertEqual(3, self.runner._run_model.call_count)
        self.assertEqual(before, self.f.service.bank.state())

    def test_selected_historical_amount_is_usable_but_revised_amount_is_not(self):
        first = self.finish_clarification()
        second = self.jobs.add('1280.50元')
        c2 = self.jobs.request_context(second['id'])
        i2 = item({'amount_minor': slot(second['id'], '1280.50元', 128050)}, parent=first['id'] + ':transfer')
        self.runner._run_model.return_value = self.calculation_output(c2, i2, 128050, '1280.50元')
        self.worker.step()
        third = self.jobs.add('沿用刚才的金额，只计算')
        c3 = self.jobs.request_context(third['id'])
        i3 = item(parent=second['id'] + ':transfer')
        self.runner._run_model.return_value = self.calculation_output(c3, i3, 128050, '1280.50元')
        self.worker.step()
        self.assertEqual('DONE', self.jobs.get(third['id'])['status'])
        fourth = self.jobs.add('改为975.25元')
        c4 = self.jobs.request_context(fourth['id'])
        i4 = item({'amount_minor': slot(fourth['id'], '975.25元', 97525)}, parent=third['id'] + ':transfer')
        self.runner._run_model.return_value = self.calculation_output(c4, i4, 128050, '1280.50元')
        before = self.runner._run_model.call_count
        self.worker.step()
        saved = self.jobs.get(fourth['id'])
        self.assertEqual('FAILED', saved['status'])
        self.assertEqual('UNBOUND_USER_CALCULATION_ASSUMPTION', saved['result']['answer_validation']['reason'])
        self.assertEqual(before + 1, self.runner._run_model.call_count)
        self.assertIsNone(self.jobs.setting('chat_session'))
        followup = self.jobs.add('重新核对原条件')
        context = self.jobs.request_context(followup['id'])
        parent = next(p for p in context['previous_items'] if p['reference'] == fourth['id'] + ':transfer')
        self.assertEqual(97525, parent['slots']['amount_minor']['value'])
        self.assertEqual('FAILED', saved['status'])  # Retained intent is not a successful bank result.

    def test_successful_readonly_retry_decision_is_available_to_later_chat(self):
        first = self.finish_clarification()
        self.jobs.finish(first['id'], {**self.jobs.get(first['id'])['result'], 'ok': False})
        retry = self.jobs.retry(first['id'])
        context = self.jobs.request_context(retry['id'])
        request = item({'source_account': slot(first['id'], 'A', 'A'),
                        'target_account': slot(first['id'], 'B', 'B')}, ['amount_minor'])
        request['action'] = {'kind': 'PREPARE', 'source': {'message_id': first['id'], 'quote': first['prompt']}}
        self.runner._run_model.return_value = response(context, [request], {
            'format': 'request_clarification_v1', 'item_ids': ['transfer']})
        self.worker.step()
        self.assertEqual('DONE', self.jobs.get(retry['id'])['status'])
        current = self.jobs.add('1280.50元')
        context = self.jobs.request_context(current['id'])
        self.assertIn(retry['id'] + ':transfer', [x['reference'] for x in context['previous_items']])
        self.assertNotIn(retry['id'], [x['message_id'] for x in context['messages']])

    def test_readonly_retry_uses_only_original_user_message_and_no_new_retry(self):
        job = self.jobs.add('转点钱')
        self.jobs.finish(job['id'], {'ok': False, 'text': '模型伪造999元'})
        retry = self.jobs.retry(job['id'])
        self.runner._run_model.return_value = {'ok': False, 'error': {'code': 'AccountQuotaExceeded'}}
        self.worker.step()
        args, kwargs = self.runner._run_model.call_args
        supplied = json.loads(args[0].split('宿主上下文：\n', 1)[1])
        self.assertEqual([{'message_id': job['id'], 'text': job['prompt']}], supplied['messages'])
        self.assertTrue(kwargs['readonly'])
        self.assertIsNone(args[1])
        self.assertEqual(1, self.runner._run_model.call_count)
        saved = self.jobs.get(retry['id'])
        self.assertEqual('AccountQuotaExceeded', saved['result']['error']['code'])

    def test_valid_legacy_free_text_is_not_new_request_contract_success(self):
        job = self.jobs.add('转点钱')
        self.runner._run_model.return_value = {'ok': True, 'text': '您希望转多少钱？', 'trace': []}
        self.worker.step()
        self.assertEqual('FAILED', self.jobs.get(job['id'])['status'])
        self.assertEqual(1, self.runner._run_model.call_count)
