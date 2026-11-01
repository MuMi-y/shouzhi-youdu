"""Product and standalone evaluator reject facts that changed after the model read."""
from pathlib import Path
from unittest.mock import patch
import json
import unittest

from tests.competition_ai import test_service_answer as fixtures
from src.bank_v1_web.worker import DshRunner
from src.bank_v1_web.display_check import check


class SharedDisplayTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.ServiceAnswerTests()
        self.f.setUp(); self.addCleanup(self.f.doCleanups)

    def test_standalone_and_product_reject_same_changed_operation(self):
        op = self.f.transfer()
        part = self.f.part(op)
        raw = {'ok': True, 'text': json.dumps({'format': 'bank_service_v1', 'parts': [part]}), 'trace': self.f.trace}
        accepted = self.f.answer([part])
        self.f.service.bank.user_action('reject', {'operation_id': op['operation_id']})
        runner = DshRunner(Path.cwd(), Path(self.f.temp.name)/'runner', 'http://127.0.0.1:1', 'test-only',
                           display_service=self.f.service)
        with patch.object(runner, '_run_model', return_value=raw) as model:
            standalone = runner('核对这笔转账', None)
        product = check(accepted, self.f.service)
        self.assertFalse(standalone['ok'])
        self.assertEqual(standalone['service_freshness'], product['service_freshness'])
        self.assertEqual(1, model.call_count)
        self.assertEqual('REJECTED', self.f.service.bank.state()['operations'][0]['status'])

    def test_live_read_only_source_is_checked_without_new_model_call(self):
        op = self.f.transfer(); part = self.f.part(op)
        raw = {'ok': True, 'text': json.dumps({'format': 'bank_service_v1', 'parts': [part]}), 'trace': self.f.trace}
        runner = DshRunner(Path.cwd(), Path(self.f.temp.name)/'runner', 'http://127.0.0.1:1', 'test-only',
                           display_service=self.f.service)
        with patch.object(runner, '_run_model', return_value=raw) as model:
            result = runner('核对', None)
        self.assertTrue(result['ok'], result)
        self.assertEqual('CURRENT_AT_DISPLAY_CHECK', result['service_freshness']['status'])
        self.assertEqual(1, model.call_count)

    def test_task_details_use_actual_changed_parameters(self):
        from src.bank_tasks.progress import operation_details
        op = self.f.transfer()
        self.assertIn('173.00元', operation_details(op))
        self.assertIn('账户 A → B', operation_details(op))


if __name__ == '__main__':
    unittest.main()
