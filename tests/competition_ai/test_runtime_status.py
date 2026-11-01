"""No paid calls: runtime truth and persistent host budget selection."""
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest

from scripts.product_budget import select_session, SessionError
from src.competition_web.runtime_status import budget_status, RuntimeStatus
from src.competition_web.app import create_app


class RuntimeStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='runtime-status-')
        self.directory = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def update(self, path, sql):
        with closing(sqlite3.connect(path)) as db, db:
            db.execute(sql)

    def test_product_restart_preserves_identity_deadline_and_counters(self):
        first = select_session(self.directory)
        self.update(first['path'], 'UPDATE batch SET provider_used=5,dsh_used=2')
        second = select_session(self.directory)
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(first['deadline_ms'], second['deadline_ms'])
        self.assertEqual(27, second['provider_remaining'])
        self.assertEqual(6, second['dsh_remaining'])
        self.assertFalse(second['created'])
        with self.assertRaises(SessionError) as error:
            select_session(self.directory, new_session=True)
        self.assertEqual('SESSION_STILL_ACTIVE', error.exception.code)

    def test_expired_session_needs_explicit_new_and_old_file_remains(self):
        first = select_session(self.directory)
        self.update(first['path'], 'UPDATE batch SET deadline_ms=1')
        with self.assertRaises(SessionError) as error:
            select_session(self.directory)
        self.assertEqual('SESSION_ENDED', error.exception.code)
        second = select_session(self.directory, new_session=True)
        self.assertNotEqual(first['id'], second['id'])
        self.assertTrue(Path(first['path']).is_file())
        self.assertEqual('EXPIRED', budget_status(first['path'])['state'])

    def test_bad_pointer_not_silently_replaced(self):
        first = select_session(self.directory)
        pointer = self.directory / 'ai-sessions/current.json'
        pointer.write_text('{"file":"../outside.sqlite3","id":"wrong"}', encoding='utf-8')
        with self.assertRaises(SessionError):
            select_session(self.directory, new_session=True)
        self.assertTrue(Path(first['path']).exists())

    def test_thread_and_budget_are_both_required_for_ready(self):
        session = select_session(self.directory)
        worker = SimpleNamespace(thread=SimpleNamespace(is_alive=lambda: True), stop_event=threading.Event())
        status = RuntimeStatus(mode='MODEL', worker=worker, budget_path=session['path'], build_id='test', capabilities={},
                               environment=lambda: {'available': True, 'reason': ''})
        self.assertTrue(status.view()['ready_for_requests'])
        self.update(session['path'], 'UPDATE batch SET dsh_used=dsh_limit')
        self.assertEqual('EXHAUSTED', status.view()['budget']['state'])
        self.assertFalse(status.view()['ready_for_requests'])
        worker.stop_event.set()
        self.assertFalse(status.view()['worker_running'])
        self.assertEqual('UNREADABLE', budget_status(self.directory / 'missing.sqlite3')['state'])

    def test_no_model_chat_rejected_without_queued_job(self):
        app = create_app(self.directory, clock=lambda: datetime(2026, 10, 19, tzinfo=timezone.utc),
                         goals_enabled=True, goal_followup=True)
        app.extensions['runtime_status'] = RuntimeStatus(mode='NO_MODEL', worker=None, budget_path=None,
            build_id='test', capabilities={'goals': True}).view
        client = app.test_client()
        client.get('/')
        state = client.get('/api/state').json
        self.assertFalse(state['model']['ready_for_requests'])
        response = client.post('/api/chat', json={'prompt': '查余额'},
                               headers={'Origin': 'http://localhost', 'X-CSRF-Token': state['csrf_token']})
        self.assertEqual(503, response.status_code, response.json)
        self.assertEqual('AI_UNAVAILABLE', response.json['error']['code'])
        self.assertEqual([], app.extensions['bank_v1']['jobs'].recent(50))
        self.assertTrue(state['competition']['goals']['enabled'])
        headers={'Origin': 'http://localhost', 'X-CSRF-Token': state['csrf_token']}
        for url, body in [('/api/jobs/missing/retry', {}), ('/api/actions/retry_event', {'event_id':'missing'}),
                          ('/api/competition/goal-followup/retry', {'expected_check_id':'missing'})]:
            response=client.post(url,json=body,headers=headers)
            self.assertEqual(503,response.status_code,(url,response.json))
        self.assertEqual([],app.extensions['bank_v1']['jobs'].recent(50))


if __name__ == '__main__':
    unittest.main()
