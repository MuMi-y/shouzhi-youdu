"""Task progress across actual local bank confirmations; no model and no user data."""
from copy import deepcopy
from datetime import timedelta
import json
import unittest

from tests.competition_tasks.test_mandate_match import MandateMatchTests
from src.bank_v1_web.bank_answer import validate_bank_answer
from src.bank_v1_web.task_progress_answer import refresh
from src.bank_v1_web.worker import Worker


def answer(body, call_id='status1'):
    task_id = body['data']['task']['task_id']
    args = {'task_id': task_id}
    return {'ok': True, 'text': json.dumps({'format': 'task_progress_v1', 'task_id': task_id,
            'source_id': body['data']['task_progress']['source_id']}), 'trace': [
        {'kind': 'request', 'call_id': call_id, 'name': 'task_status', 'arguments': args},
        {'kind': 'response', 'call_id': call_id, 'tool': 'task_status', 'args': args,
         'ok': True, 'status': 200, 'response': body}]}


class TaskProgressTests(unittest.TestCase):
    def setUp(self):
        self.f = MandateMatchTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.tid = self.f.submit()

    def read(self):
        return self.f.service.call_tool('task_status', {'task_id': self.tid})

    def checked(self):
        result = validate_bank_answer(answer(self.read()), '核对任务进度')
        self.assertTrue(result['ok'], result)
        return result

    def operation(self):
        return next(op for op in self.f.bank.state()['operations'] if op['status'] == 'PENDING')

    def test_ready_has_no_card_and_read_does_not_advance(self):
        before = self.f.bank.state()
        task = self.f.service.tasks.get(self.tid)
        result = self.checked()
        self.assertIn('尚未生成银行确认卡', result['text'])
        self.assertIn('当前没有可供确认', result['text'])
        self.assertNotIn('请在银行待确认区核对', result['text'])
        self.assertEqual(before, self.f.bank.state())
        self.assertEqual(task, self.f.service.tasks.get(self.tid))

    def test_two_separate_confirmations_then_actual_receipts(self):
        self.f.advance(self.tid)
        text = self.checked()['text']
        self.assertIn('已有待确认操作', text)
        query = self.operation()
        self.assertEqual(query['kind'], 'mandate_query')
        self.assertEqual(len(self.f.bank.state()['operations']), 1)
        self.f.confirm(query)
        # The bank has committed, but task runner has not yet observed it.
        interim = self.checked()['text']
        self.assertIn('银行操作已成功', interim)
        self.assertIn('任务节点尚待同步核对', interim)
        self.assertNotIn('请在银行待确认区核对', interim)
        self.f.advance(self.tid, 3)
        cancel = self.operation()
        self.assertEqual(cancel['kind'], 'mandate_cancel')
        self.assertEqual(cancel['summary']['params']['mandate_id'], 'random_target_73')
        self.assertIn('已有待确认操作', self.checked()['text'])
        self.f.confirm(cancel)
        self.f.advance(self.tid, 2)
        final = self.checked()['text']
        self.assertIn('任务节点均已完成', final)
        self.assertIn('不等于解除商户合同', final)
        self.assertEqual(self.f.bank.state()['transactions'], self.f.before)
        self.assertEqual(len(self.f.bank.state()['operations']), 2)
        self.assertEqual(next(m for m in self.f.bank.state()['mandates'] if m['mandate_id']=='random_target_73')['status'], 'CANCELLED')

    def test_expired_and_rejected_operations_are_not_actionable(self):
        self.f.advance(self.tid)
        self.f.now += timedelta(hours=1)
        self.assertIn('原确认已过期', self.checked()['text'])
        self.assertNotIn('请在银行待确认区核对', self.checked()['text'])
        self.f.bank.user_action('reject', {'operation_id': self.operation()['operation_id']})
        self.assertIn('原操作已拒绝', self.checked()['text'])
        self.f.advance(self.tid)
        self.assertIn('任务尚未完成', self.checked()['text'])

    def test_paused_and_cancelled_do_not_suggest_automatic_progress(self):
        self.f.service.tasks.pause(self.tid)
        self.assertIn('已暂停自动推进', self.checked()['text'])
        self.f.service.tasks.cancel(self.tid)
        self.assertIn('任务已取消', self.checked()['text'])

    def test_wrong_source_wrong_subject_and_extra_prose_rejected(self):
        raw = answer(self.read())
        variants = []
        for key, value in [('source_id', 'wrong'), ('task_id', 'wrong'), ('comment', '已撤销')]:
            copy = deepcopy(raw); document = json.loads(copy['text']); document[key] = value
            copy['text'] = json.dumps(document); variants.append(copy)
        copy = deepcopy(raw); copy['trace'][-1]['response']['data']['task']['subject_id'] = 'U2'; variants.append(copy)
        copy = deepcopy(raw); copy['trace'][0]['arguments'] = {'task_id': 'other'}; variants.append(copy)
        copy = deepcopy(raw); copy['trace'] = copy['trace'][1:]; variants.append(copy)
        for item in variants:
            self.assertFalse(validate_bank_answer(item, '进度')['ok'])

    def test_old_status_cannot_override_later_status(self):
        raw = answer(self.read())
        self.f.advance(self.tid)
        raw['trace'].extend(answer(self.read(), 'status2')['trace'])
        self.assertFalse(validate_bank_answer(raw, '进度')['ok'])

    def test_refresh_uses_current_bank_status_without_replaying(self):
        raw = self.checked()
        self.f.advance(self.tid)
        bank_before = self.f.bank.state()
        refreshed = refresh(raw, self.f.service.call_tool)
        self.assertTrue(refreshed['ok'])
        self.assertIn('请在银行待确认区核对', refreshed['text'])
        self.assertEqual(refreshed['task_progress_refresh']['actor'], 'SYSTEM_READ_ONLY')
        self.assertNotEqual(refreshed['task_progress_refresh']['model_source_id'],
                            refreshed['task_progress_refresh']['display_source']['source_id'])
        self.assertEqual(bank_before, self.f.bank.state())

    def test_original_free_prose_failure_remains_rejected(self):
        raw = answer(self.read())
        raw['text'] = '已生成银行查询确认卡，请在银行面板确认。'
        self.assertFalse(validate_bank_answer(raw, '进度')['ok'])

    def test_worker_refreshes_before_committing_chat_answer(self):
        raw = self.checked()
        self.f.jobs.set_setting('schedule', {'enabled': False, 'cadence': 'daily'})
        job = self.f.jobs.add('查看当前任务进度')
        self.f.advance(self.tid)
        bank_before = self.f.bank.state()
        worker = Worker(self.f.service, self.f.jobs, lambda *a, **kw: raw, clock=lambda: self.f.now)
        self.assertTrue(worker.step())
        saved = self.f.jobs.get(job['id'])
        self.assertEqual(saved['status'], 'DONE')
        self.assertIn('系统已在 AI 查询后重新读取', saved['result']['text'])
        self.assertIn('请在银行待确认区核对', saved['result']['text'])
        self.assertEqual(bank_before, self.f.bank.state())


if __name__ == '__main__':
    unittest.main()
