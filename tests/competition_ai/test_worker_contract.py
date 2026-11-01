"""Verify the shared DSH boundary and durable rejection behavior without a model."""
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from src.bank_v1_web.jobs import JobStore
from src.bank_v1_web.worker import DshRunner, Worker


class SharedRunnerTests(unittest.TestCase):
    def runner(self, value):
        runner = DshRunner('.', '.', 'http://127.0.0.1:1', 'test-unused')
        runner._run_model = Mock(return_value=value)
        return runner

    def test_direct_runner_uses_same_gate_without_second_model_call(self):
        runner = self.runner({'ok':True,'text':'已为您准备好代扣查询操作，请在银行面板确认。',
                              'session_id':'bad','trace':[]})
        result = runner('请查询代扣', 'old')
        self.assertFalse(result['ok'])
        self.assertTrue(result['answer_validation']['gate_applied'])
        runner._run_model.assert_called_once_with('请查询代扣', 'old', readonly=False, data_scope='bank')

    def test_model_failure_preserved_without_automatic_retry(self):
        runner = self.runner({'ok':False,'error':{'code':'AccountQuotaExceeded'}})
        result = runner('请查询', None)
        self.assertEqual('AccountQuotaExceeded',result['error']['code'])
        self.assertEqual('MODEL_FAILED',result['answer_validation']['status'])
        self.assertEqual(1,runner._run_model.call_count)

    def test_history_keeps_its_existing_separate_contract(self):
        raw = {'ok':True,'text':'历史结果','trace':[]}
        runner = self.runner(raw)
        self.assertEqual(raw,runner('历史查询',None,readonly=True,data_scope='moneydata'))


class DurableRejectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.clock = lambda: datetime(2026,10,12,tzinfo=timezone.utc)
        self.jobs = JobStore(Path(self.temp.name)/'jobs.sqlite3',clock=self.clock)
        self.jobs.set_setting('schedule',{'enabled':False,'cadence':'daily'})
        self.service = Mock()
        self.service.pending_events.return_value = []

    def tearDown(self):
        self.temp.cleanup()

    def test_failed_resumed_session_is_not_reused(self):
        self.jobs.set_setting('chat_session','old-session')
        job = self.jobs.add('准备查询代扣')
        runner = DshRunner('.', '.', 'http://127.0.0.1:1', 'test-unused')
        runner._run_model = Mock(return_value={'ok':True,'text':'已准备好代扣查询操作。',
                                               'session_id':'old-session','trace':[]})
        worker = Worker(self.service,self.jobs,runner,clock=self.clock)
        worker.step()
        self.assertEqual('FAILED',self.jobs.get(job['id'])['status'])
        self.assertIsNone(self.jobs.setting('chat_session'))
        self.assertIn('已准备好',self.jobs.get(job['id'])['result']['text'])
        self.assertEqual(1,runner._run_model.call_count)
        self.service.user_action.assert_not_called()

    def test_atomic_session_clear_preserves_newer_conversation(self):
        self.jobs.set_setting('chat_session','new-session')
        self.assertFalse(self.jobs.clear_setting_if_matches('chat_session','old-session'))
        self.assertEqual('new-session',self.jobs.setting('chat_session'))

    def test_crash_inside_resumed_model_cannot_leave_default_session(self):
        self.jobs.set_setting('chat_session','old-session')
        job = self.jobs.add('准备查询代扣')
        def interrupted(prompt, session_id, **kwargs):
            self.assertEqual('old-session',session_id)
            self.assertIsNone(self.jobs.setting('chat_session'))
            raise SystemExit('simulated process termination')
        worker = Worker(self.service,self.jobs,interrupted,clock=self.clock)
        with self.assertRaises(SystemExit):
            worker.step()
        reopened = JobStore(self.jobs.path,clock=self.clock)
        reopened.recover()
        self.assertEqual('INTERRUPTED',reopened.get(job['id'])['status'])
        self.assertIsNone(reopened.setting('chat_session'))

    def test_legitimate_clarification_saves_session_and_has_no_action(self):
        job = self.jobs.add('转点钱')
        runner = DshRunner('.', '.', 'http://127.0.0.1:1', 'test-unused')
        from tests.competition_ai.test_request_decision import item, response
        request = item(missing=['source_account', 'target_account', 'amount_minor'])
        request['action'] = {'kind': 'PREPARE', 'source': {'message_id': job['id'], 'quote': '转点钱'}}
        raw = response(self.jobs.request_context(job['id']),
            [request],
            {'format': 'request_clarification_v1', 'item_ids': ['transfer']})
        runner._run_model = Mock(return_value={**raw, 'session_id': 'good'})
        Worker(self.service,self.jobs,runner,clock=self.clock).step()
        self.assertEqual('DONE',self.jobs.get(job['id'])['status'])
        self.assertEqual('good',self.jobs.setting('chat_session'))
        self.service.user_action.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
