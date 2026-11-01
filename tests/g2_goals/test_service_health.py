"""Recovery episodes preserve failures and never replay bank operations."""
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from scripts.g1_goal_demo import seed_new_fixture
from src.bank_v1 import DomainError
from src.bank_v1.storage import connection
from src.competition_web.app import create_app


class ServiceHealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='goal-health-')
        self.directory = Path(self.temp.name)
        self.now = datetime(2026, 11, 2, 2, tzinfo=timezone.utc)
        seed_new_fixture(self.directory / 'bank.sqlite3', self.now)
        self.app = self.open_app()
        self.service = self.app.extensions['competition']
        self.follow = self.service.goal_followup
        self.jobs = self.app.extensions['bank_v1']['jobs']
        self.profile = {'title': '学习电脑', 'user_reason': '学习', 'priority_reason': '先还款',
            'preferred_amount_minor': 100000, 'min_amount_minor': 100000,
            'preferred_date': '2026-11-22', 'latest_date': '2026-11-30', 'reserve_floor_minor': 0}
        self.service.goals.action('save', {'expected_revision': 0, 'profile': self.profile})

    def open_app(self):
        if hasattr(self,'active_app'):
            self.active_app.extensions['bank_v1']['jobs'].handoff.release_owner()
        self.active_app=create_app(self.directory, clock=lambda: self.now,
                                  goals_enabled=True, goal_followup=True, goal_ai=True)
        return self.active_app

    def tearDown(self):
        self.active_app.extensions['bank_v1']['jobs'].handoff.release_owner()
        self.temp.cleanup()

    def fail(self):
        check = self.follow.observe()
        self.follow.start(check['check_id'])
        self.follow.finish(check['check_id'], {'ok': False, 'error': 'OFFLINE_SYNTHETIC_FAILURE'})
        return check

    def expense(self, amount=17, key='tiny'):
        self.service.bank.user_action('inject_transaction', {
            'kind': 'expense', 'amount_minor': amount, 'idempotency_key': key})

    def test_failure_same_facts_quiet_change_keeps_unfinished_health(self):
        first = self.fail()
        self.assertEqual('FAILED', self.follow.observe()['state'])
        self.expense()
        check = self.follow.observe(periodic_due=False)
        state = self.follow.state()
        self.assertEqual('QUIET', check['state'])
        self.assertEqual('QUIET', state['service_health']['notice_decision'])
        self.assertEqual('NEEDS_RECOVERY', state['service_health']['state'])
        self.assertEqual(first['check_id'], state['service_health']['last_attempt_check_id'])
        self.assertTrue(state['service_health']['can_retry'])
        self.follow.enqueue(self.jobs)
        self.assertEqual([], self.jobs.recent())

    def test_retry_uses_latest_facts_is_idempotent_and_only_enqueues_readonly(self):
        first = self.fail(); self.expense(); self.follow.observe(periodic_due=False)
        before = self.service.bank.state()
        check = self.follow.retry(first['check_id'])
        again = self.follow.retry(first['check_id'])
        self.assertEqual(check['check_id'], again['check_id'])
        self.assertNotEqual(first['basis_hash'], check['basis_hash'])
        self.follow.enqueue(self.jobs, observe=False)
        self.follow.enqueue(self.jobs, observe=False)
        jobs = self.jobs.recent()
        self.assertEqual(1, len(jobs))
        self.assertEqual('goal', jobs[0]['kind'])
        self.assertTrue(jobs[0]['read_only'])
        self.assertEqual(before, self.service.bank.state())

    def test_retry_failure_hits_limit_and_quiet_changes_do_not_reset_it(self):
        first = self.fail()
        retry = self.follow.retry(first['check_id']); self.follow.start(retry['check_id'])
        self.follow.finish(retry['check_id'], {'ok': False, 'error': 'OFFLINE_SECOND_FAILURE'})
        for i in range(2):
            self.expense(key=f'quiet-{i}'); self.follow.observe(periodic_due=False)
            health = self.follow.state()['service_health']
            self.assertEqual('RECOVERY_EXHAUSTED', health['state'])
            self.assertFalse(health['can_retry'])
            self.assertEqual(1, health['recovery_count'])
        with self.assertRaises(DomainError) as error:
            self.follow.retry(retry['check_id'])
        self.assertEqual('GOAL_RECOVERY_LIMIT', error.exception.code)
        self.assertEqual(retry['check_id'], self.follow.retry(first['check_id'])['check_id'])

    def test_important_new_revision_creates_new_episode(self):
        first = self.fail()
        retry = self.follow.retry(first['check_id'])
        self.follow.start(retry['check_id'])
        self.follow.finish(retry['check_id'], {'ok': False, 'error': 'OFFLINE_SECOND_FAILURE'})
        self.service.goals.action('save', {'expected_revision': 1,
            'profile': {**self.profile, 'preferred_amount_minor': 200000}})
        current = self.follow.observe(periodic_due=False)
        self.assertEqual('PENDING', current['state'])
        health = self.follow.state()['service_health']
        self.assertEqual(0, health['recovery_count'])
        self.assertEqual(current['check_id'], health['pending_check_id'])

    def test_restart_and_legacy_table_migration_keep_failure_history(self):
        first = self.fail(); self.expense(); self.follow.observe(periodic_due=False)
        reopened = self.open_app().extensions['competition'].goal_followup
        self.assertEqual('NEEDS_RECOVERY', reopened.state()['service_health']['state'])
        with connection(self.service.goals.db_path, True) as db:
            db.execute('DROP TABLE goal_service_health')  # Disposable migration fixture only.
        migrated = self.open_app().extensions['competition'].goal_followup
        health = migrated.state()['service_health']
        self.assertEqual(first['check_id'], health['last_attempt_check_id'])
        self.assertEqual('NEEDS_RECOVERY', health['state'])

    def test_old_async_failure_cannot_replace_new_pending_service(self):
        old = self.follow.observe(); self.follow.start(old['check_id'])
        self.service.goals.action('save', {'expected_revision': 1,
            'profile': {**self.profile, 'preferred_amount_minor': 200000}})
        new = self.follow.observe()
        self.follow.finish(old['check_id'], {'ok': False, 'error': 'OLD_FAILURE'})
        health = self.follow.state()['service_health']
        self.assertEqual(new['check_id'], health['pending_check_id'])
        self.assertEqual('PENDING', health['state'])

    def test_http_requires_trusted_browser_and_model_cannot_retry(self):
        first = self.fail()
        client = self.app.test_client()
        url = '/api/competition/goal-followup/retry'
        body = {'expected_check_id': first['check_id']}
        token = self.app.extensions['bank_v1']['tool_token']
        self.assertEqual(403, client.post(url, json=body, headers={'Authorization': 'Bearer ' + token}).status_code)
        self.assertEqual(403, client.post(url, json=body).status_code)
        client.get('/api/state')
        with client.session_transaction() as session:
            csrf = session['csrf_token']
        response = client.post(url, json=body, headers={'Origin': 'http://localhost', 'X-CSRF-Token': csrf})
        self.assertEqual(200, response.status_code, response.json)
        self.assertEqual('PENDING', response.json['service_health']['state'])
        self.assertEqual(1, len(self.jobs.recent()))
        self.assertNotIn('retry_goal_analysis', [s['name'] for s in self.service.tool_specs()])


if __name__ == '__main__':
    unittest.main()
