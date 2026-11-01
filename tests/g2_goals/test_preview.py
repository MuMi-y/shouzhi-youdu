"""New fictional fixtures only. Offline proof is not real model acceptance."""
from copy import deepcopy
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import hashlib
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from scripts.g1_goal_demo import seed_new_fixture
from src.competition_web.app import create_app
from src.bank_v1 import DomainError
from src.bank_tasks.validation import TaskError
from src.bank_v1.storage import connection
from src.bank_v1_web.bank_answer import validate_bank_answer
from src.bank_v1_web.worker import Worker, DshRunner
from src.bank_goals.preview_answer import recheck


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = datetime(2026, 11, 2, 2, tzinfo=timezone.utc)
        seed_new_fixture(self.root/'bank.sqlite3', self.now)
        self.app = create_app(self.root, clock=lambda: self.now, goals_enabled=True, goal_followup=True)
        self.service = self.app.extensions['competition']
        self.jobs = self.app.extensions['bank_v1']['jobs']
        self.addCleanup(self.jobs.handoff.release_owner)
        self.jobs.set_setting('schedule', {'enabled': False, 'cadence': 'daily'})
        self.profile = {'title': '学习电脑', 'user_reason': '学习', 'priority_reason': '先保证还款',
            'preferred_amount_minor': 450000, 'min_amount_minor': 300000,
            'preferred_date': '2026-11-22', 'latest_date': '2026-11-30', 'reserve_floor_minor': 200000}

    def call(self, profile=None):
        return self.service.call_tool('preview_goal_options', {'profile': profile or self.profile})

    def raw(self, response=None):
        response = response or self.call()
        args = {'profile': response['data']['unconfirmed_profile']}
        answer = {'format': 'goal_preview_answer_v1', 'source_id': response['source_id'],
            'claims': [{k: c[k] for k in ('path', 'value')} for c in response['claim_catalog']],
            'next_step': 'REVIEW_GOAL_FORM'}
        return {'ok': True, 'text': json.dumps(answer, ensure_ascii=False), 'execution_mode': 'OFFLINE_STUB',
            'trace': [{'kind': 'request', 'call_id': 'preview-1', 'name': 'preview_goal_options', 'arguments': args},
                {'kind': 'response', 'call_id': 'preview-1', 'tool': 'preview_goal_options', 'args': args,
                 'status': 200, 'ok': True, 'response': response}]}

    def databases(self):
        files, dumps = {}, {}
        for path in self.root.glob('*.sqlite3*'):
            files[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
            if path.suffix == '.sqlite3':
                with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)) as db:
                    dumps[path.name] = list(db.iterdump())
        return files, dumps

    def test_entire_database_unchanged_and_independent_integer_oracle(self):
        before = self.databases()
        response = self.call(); data = response['data']
        self.assertEqual(before, self.databases())
        self.assertEqual(response['scope'], 'goal_preview')
        self.assertNotIn('revision', data); self.assertNotIn('basis_hash', data)
        self.assertEqual([o['primary']['minimum_minor'] for o in data['options']],
                         [1000000-300000-200000-450000, 1000000-300000-200000-300000,
                          1000000-300000-200000+250000-450000])
        self.assertEqual([o['without_income']['minimum_minor'] for o in data['options']], [50000, 200000, 50000])
        self.assertEqual([o['primary']['date'] for o in data['options']],
                         ['2026-11-22', '2026-11-22', '2026-11-30'])
        self.assertEqual([o['primary']['reserve_shortfall_minor'] for o in data['options']], [150000, 0, 0])
        self.assertEqual([o['without_income']['reserve_shortfall_minor'] for o in data['options']], [150000, 0, 150000])

    def test_http_tool_discovery_authority_and_disabled_feature(self):
        client = self.app.test_client()
        headers = {'Authorization': 'Bearer '+self.app.extensions['bank_v1']['tool_token']}
        catalog = client.get('/api/tool-schema', headers=headers).get_json()
        self.assertIn('preview_goal_options', [t['name'] for t in catalog['tools']])
        response = client.post('/api/tools/preview_goal_options', json={'profile': self.profile}, headers=headers)
        self.assertEqual(response.status_code, 200); self.assertEqual(response.get_json(), self.call())
        self.assertNotEqual(client.post('/api/tools/preview_goal_options', json={'profile': self.profile}).status_code, 200)
        other = create_app(self.root/'disabled', clock=lambda: self.now)
        self.addCleanup(other.extensions['bank_v1']['jobs'].handoff.release_owner)
        self.assertNotIn('preview_goal_options', [t['name'] for t in other.extensions['competition'].tool_specs()])
        with self.assertRaises(TaskError):
            self.service.tasks.submit('试算', {'title': '试算', 'nodes': [
                {'id': 'p', 'tool': 'preview_goal_options', 'args': {'profile': self.profile}, 'depends_on': []}]}, 'p')

    def test_strict_fields_dates_amounts_and_existing_goal(self):
        for changes in [{'preferred_amount_minor': 450000.0}, {'reserve_floor_minor': True},
                        {'latest_date': '2026-02-30'}, {'preferred_date': '2026-10-31'},
                        {'latest_date': '2028-01-01'}, {'min_amount_minor': 500000}, {'title': ' '}]:
            with self.subTest(changes=changes), self.assertRaises(DomainError):
                self.call({**self.profile, **changes})
        missing = dict(self.profile); missing.pop('priority_reason')
        with self.assertRaises(DomainError): self.call(missing)
        self.service.goals.action('save', {'expected_revision': 0, 'profile': self.profile})
        with self.assertRaises(DomainError) as caught: self.call()
        self.assertEqual(caught.exception.code, 'GOAL_EXISTS')

    def test_independent_cross_year_dates_no_income_and_deduplication(self):
        self.now = datetime(2026, 12, 20, 2, tzinfo=timezone.utc)
        with connection(self.service.bank.db_path, True) as db:
            db.execute('DELETE FROM arrangements')
        profile = {**self.profile, 'preferred_date': '2026-12-31', 'latest_date': '2027-01-09',
                   'preferred_amount_minor': 123457, 'min_amount_minor': 123457}
        data = self.call(profile)['data']
        self.assertEqual(data['income_conditions'], [])
        self.assertEqual(len(data['options']), 2)
        for option in data['options']:
            self.assertEqual(option['primary'], option['without_income'])
            self.assertEqual(option['primary']['minimum_minor'], 1000000-123457)
            self.assertEqual(option['primary']['date'], option['due_date'])

    def test_bound_claims_render_revalidate_and_refuse_tampering(self):
        raw = self.raw(); checked = validate_bank_answer(raw, '先不要保存')
        self.assertTrue(checked['ok']); self.assertIn('未保存目标', checked['text'])
        self.assertIn('未到账', checked['text']); self.assertIn('3000.00元', checked['text'])
        self.assertTrue(validate_bank_answer(checked, '先不要保存')['ok'])
        mutations = []
        for scope in ['bank', 'goal_plan', 'moneydata']:
            value = deepcopy(raw); value['trace'][1]['response']['scope'] = scope; mutations.append(value)
        value = deepcopy(raw); value['trace'][1]['args'] = {}; mutations.append(value)
        value = deepcopy(raw); value['trace'][1]['response']['claim_catalog'][0]['display'] = '已经付款'; mutations.append(value)
        value = deepcopy(raw); value['trace'] = value['trace'][1:]; mutations.append(value)
        value = deepcopy(raw); value['trace'].append(value['trace'][1]); mutations.append(value)
        for changes in [{'source_id': 'invented'}, {'format': 'goal_answer_v1'}, {'comment': '收入已到账'},
                        {'claims': json.loads(raw['text'])['claims'][:-1]}, {'next_step': 'PAY_NOW'}]:
            value = deepcopy(raw); value['text'] = json.dumps({**json.loads(raw['text']), **changes}); mutations.append(value)
        value = deepcopy(raw); answer = json.loads(value['text']); answer['claims'][0]['value'] = 1000001
        value['text'] = json.dumps(answer); mutations.append(value)
        for i, value in enumerate(mutations):
            with self.subTest(i=i): self.assertFalse(validate_bank_answer(value, '试算')['ok'])

    def test_any_write_attempt_and_automatic_goal_check_fail_closed(self):
        raw = self.raw()
        for name in ['propose_goal', 'prepare_transfer', 'submit_task_plan', 'check_insights']:
            value = deepcopy(raw); value['trace'].insert(0, {'kind': 'request', 'call_id': 'write', 'name': name, 'arguments': {}})
            self.assertFalse(validate_bank_answer(value, '试算')['ok'])
        missing = [{'kind': 'request', 'call_id': 'missing', 'name': 'goal_context', 'arguments': {'goal_id': 'focus'}},
            {'kind': 'response', 'call_id': 'missing', 'tool': 'goal_context', 'args': {'goal_id': 'focus'},
             'ok': False, 'status': 409, 'response': {'error': {'code': 'GOAL_MISSING'}}}]
        self.assertTrue(validate_bank_answer({**raw, 'trace': missing+raw['trace']}, '试算')['ok'])
        for row in missing:
            row['arguments' if row['kind'] == 'request' else 'args']['check_id'] = 'automatic-check'
        self.assertFalse(validate_bank_answer({**raw, 'trace': missing+raw['trace']}, '试算')['ok'])

    def test_freshness_rechecks_bank_clock_and_confirmed_goal(self):
        raw = self.raw()
        self.assertTrue(recheck(raw, self.service.goal_tools)['ok'])
        self.now += timedelta(days=1)
        self.assertFalse(recheck(raw, self.service.goal_tools)['ok'])
        raw = self.raw()
        self.service.bank.user_action('inject_transaction', {'kind': 'expense', 'amount_minor': 1, 'idempotency_key': 'fresh'})
        self.assertFalse(recheck(raw, self.service.goal_tools)['ok'])
        raw = self.raw()
        self.service.goals.action('save', {'expected_revision': 0, 'profile': self.profile})
        self.assertFalse(recheck(raw, self.service.goal_tools)['ok'])
        self.assertFalse(recheck(raw, None)['ok'])

    def test_actual_worker_rejects_changed_bank_without_remembering_session(self):
        raw = validate_bank_answer(self.raw(), '试算')
        raw['session_id'] = 'bank-old-session'
        job = self.jobs.add('先试算不要保存')
        def runner(*args, **kwargs):
            self.service.bank.user_action('inject_transaction', {'kind': 'expense', 'amount_minor': 1, 'idempotency_key': 'race'})
            return raw
        worker = Worker(self.service, self.jobs, runner, clock=lambda: self.now)
        self.assertTrue(worker.step())
        result = self.jobs.get(job['id'])
        self.assertEqual(result['status'], 'FAILED')
        self.assertEqual(result['result']['answer_validation']['status'], 'STALE')
        self.assertIsNone(self.jobs.setting('chat_session'))

    def test_preview_failure_has_no_internal_model_retry(self):
        raw = self.raw(); raw['text'] = '我已经确认了目标'
        runner = DshRunner(self.root, self.root/'dsh', 'http://127.0.0.1:1', 'offline')
        with patch.object(runner, '_run_model', return_value=raw) as model:
            result = runner('试算', None)
        self.assertFalse(result['ok']); self.assertEqual(model.call_count, 1)


if __name__ == '__main__': unittest.main()
