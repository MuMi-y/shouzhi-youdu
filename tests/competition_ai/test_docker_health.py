import subprocess
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.competition_web.docker_health import DockerHealth, probe
from src.competition_web.runtime_status import RuntimeStatus
from src.bank_v1_web.worker import Worker


class DockerHealthTests(unittest.TestCase):
    def test_missing_timeout_and_wrong_engine_fail_closed(self):
        for effect in (OSError('missing'), subprocess.TimeoutExpired('docker', 3)):
            with patch('src.competition_web.docker_health.subprocess.run', side_effect=effect):
                self.assertFalse(probe()['available'])
        with patch('src.competition_web.docker_health.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout='windows')):
            self.assertFalse(probe()['available'])
        with patch('src.competition_web.docker_health.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout='linux\n')):
            self.assertTrue(probe()['available'])

    def test_cache_refreshes_after_environment_recovers(self):
        check = Mock(side_effect=[{'available': False, 'reason': 'down'}, {'available': True, 'reason': ''}])
        cached = DockerHealth(check=check, ttl=5)
        with patch('src.competition_web.docker_health.time.monotonic', return_value=10):
            self.assertFalse(cached()['available'])
            self.assertFalse(cached()['available'])
        with patch('src.competition_web.docker_health.time.monotonic', return_value=16):
            self.assertTrue(cached()['available'])
        self.assertEqual(2, check.call_count)

    def test_worker_leaves_queued_requests_and_budget_untouched_when_unavailable(self):
        service, jobs, runner = Mock(), Mock(), Mock()
        worker = Worker(service, jobs, runner, clock=Mock(), can_run=lambda: False)
        self.assertFalse(worker.step())
        self.assertEqual([], service.mock_calls)
        self.assertEqual([], jobs.mock_calls)
        runner.assert_not_called()

    def test_runtime_does_not_advertise_ready_without_docker(self):
        worker = SimpleNamespace(thread=SimpleNamespace(is_alive=lambda: True), stop_event=threading.Event())
        with patch('src.competition_web.runtime_status.budget_status', return_value={'available': True}), \
             patch('src.competition_web.runtime_status.dependency_status', return_value={'available': True, 'reason': ''}):
            state = RuntimeStatus(mode='MODEL', worker=worker, budget_path='unused', build_id='test', capabilities={},
                                  environment=lambda: {'available': False, 'reason': 'Docker unavailable'}).view()
            self.assertFalse(state['ready_for_requests'])
            self.assertEqual('Docker unavailable', state['reason'])


if __name__ == '__main__':
    unittest.main()
