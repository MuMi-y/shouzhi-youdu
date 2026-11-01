"""Actual SQLite queue provenance, independent of model memory and bank writes."""
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from src.bank_v1_web.jobs import JobStore
from src.bank_requests.context import build, model_prompt


class RequestContextTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='request-context-')
        self.addCleanup(temporary.cleanup)
        self.clock = lambda: datetime(2026, 10, 19, tzinfo=timezone.utc)
        self.jobs = JobStore(Path(temporary.name) / 'jobs.sqlite3', clock=self.clock)

    def message(self, text, **kwargs):
        job = self.jobs.add(text, **kwargs)
        self.jobs.finish(job['id'], {'ok': True, 'text': 'fixture response'})
        return job

    def test_same_timestamp_future_and_other_scope_are_excluded(self):
        old = self.message('先从A转到B')
        self.message('MoneyData历史3500元', scope='moneydata')
        self.message('自动事件并非用户说要转钱', kind='event')
        current = self.message('金额改为1280.50元')
        self.message('未来排队的另一个金额700元')
        context = self.jobs.request_context(current['id'])
        self.assertEqual([old['id'], current['id']], [m['message_id'] for m in context['messages']])
        self.assertFalse(context['truncated'])

    def test_restart_and_failed_model_keep_user_text_not_assistant_summary(self):
        old = self.message('考虑从A转3500元到B')
        self.jobs.finish(old['id'], {'ok': False, 'text': '模型编造：用户改为9000元'})
        current = self.message('按刚才那个金额先试算')
        reopened = JobStore(self.jobs.path, clock=self.clock)
        context = reopened.request_context(current['id'])
        self.assertEqual('考虑从A转3500元到B', context['messages'][0]['text'])
        self.assertNotIn('9000', json.dumps(context, ensure_ascii=False))

    def test_retry_uses_original_cutoff_and_preserves_readonly(self):
        old = self.message('请分析3500元支出')
        self.jobs.finish(old['id'], {'ok': False, 'text': 'failed'})
        self.message('后来另一个需求900元')
        retry = self.jobs.retry(old['id'])
        context = self.jobs.request_context(retry['id'])
        self.assertEqual(retry['id'], context['request_id'])
        self.assertEqual(old['id'], context['current_message_id'])
        self.assertEqual([{'message_id': old['id'], 'text': old['prompt']}], context['messages'])
        self.assertTrue(context['read_only'])

    def test_bounded_messages_never_cut_inside_user_text(self):
        jobs = [self.message('第' + str(i) + '轮') for i in range(10)]
        context = self.jobs.request_context(jobs[-1]['id'])
        self.assertEqual([x['id'] for x in jobs[-8:]], [m['message_id'] for m in context['messages']])
        self.assertTrue(context['truncated'])
        for i in range(4):
            current = self.message(str(i) * 6000)
        context = self.jobs.request_context(current['id'])
        self.assertEqual(3, len(context['messages']))
        self.assertEqual(18000, sum(len(m['text']) for m in context['messages']))

    def test_transport_prunes_whole_old_messages_with_exact_matching_context(self):
        messages = [{'message_id': str(i), 'text': '\n' * 5000} for i in range(3)]
        messages.append({'message_id': 'current', 'text': '当前原话不截断'})
        context = build('request', 'current', messages, [], read_only=True)
        prompt, selected = model_prompt(context)
        self.assertLessEqual(len(prompt.encode('utf-16-le')) // 2, 24000)
        self.assertLess(len(json.dumps({'prompt': prompt}, ensure_ascii=False).encode('utf-8')), 95000)
        self.assertTrue(selected['truncated'])
        self.assertEqual(messages[-1], selected['messages'][-1])
        self.assertEqual(selected, json.loads(prompt.split('宿主上下文：\n', 1)[1]))

    def test_non_chat_or_history_cannot_be_bank_request_context(self):
        for job in (self.message('历史', scope='moneydata'), self.message('事件', kind='event')):
            with self.assertRaises(ValueError):
                self.jobs.request_context(job['id'])
