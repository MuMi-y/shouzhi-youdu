"""Portable synthetic tests: no historical evidence, model, or real user data."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from src.bank_v1.service import Service
from src.bank_v1.storage import connection


class RejectedAtTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = datetime(2026, 10, 12, 4, tzinfo=timezone.utc)
        self.bank = Service(Path(self.directory.name) / 'bank.sqlite3', clock=lambda: self.now)

    def prepare(self, key='test-transfer'):
        return self.bank.call_tool('prepare_transfer', {
            'source_account': 'A', 'target_account': 'B',
            'amount_minor': 10000, 'idempotency_key': key,
        })['data']['operation']

    def query(self, operation):
        return self.bank.call_tool('operation', {
            'operation_id': operation['operation_id'],
        })['data']['operation']

    def reject(self, operation):
        return self.bank.user_action('reject', {'operation_id': operation['operation_id']})

    def test_pending_is_null_and_existing_summary_stays_exact(self):
        op = self.prepare()
        self.assertIsNone(op['rejected_at'])
        self.now += timedelta(seconds=20)
        current = self.query(op)
        self.assertIsNone(current['rejected_at'])
        self.assertEqual(current['summary'], op['summary'])

    def test_rejected_time_is_actual_audit_event_not_creation_or_observation(self):
        op = self.prepare()
        self.now += timedelta(seconds=37)
        rejected = self.reject(op)
        expected = self.now.isoformat()
        self.assertEqual(rejected['rejected_at'], expected)
        self.assertNotEqual(expected, op['created_at'])
        self.assertNotEqual(expected, op['expires_at'])
        self.now += timedelta(hours=2)
        response = self.bank.call_tool('operation', {'operation_id': op['operation_id']})
        self.assertEqual(response['data']['operation']['rejected_at'], expected)
        self.assertNotEqual(response['observed_at'], expected)

    def test_duplicate_rejection_preserves_first_time_and_one_event(self):
        op = self.prepare()
        self.now += timedelta(seconds=30)
        first = self.reject(op)
        self.now += timedelta(hours=1)
        second = self.reject(op)
        self.assertEqual(first['rejected_at'], second['rejected_at'])
        with connection(self.bank.db_path) as db:
            count = db.execute("SELECT count(*) FROM audit WHERE action='REJECTED' AND reference_id=?", (op['operation_id'],)).fetchone()[0]
        self.assertEqual(count, 1)

    def test_success_has_no_rejection_time_and_original_receipt(self):
        op = self.prepare()
        self.now += timedelta(seconds=10)
        done = self.bank.user_action('confirm', {
            'operation_id': op['operation_id'], 'expected_summary': op['summary'],
        })
        self.assertEqual(done['status'], 'SUCCEEDED')
        self.assertIsNone(done['rejected_at'])
        self.assertEqual(done['receipt']['committed_at'], self.now.isoformat())
        self.assertEqual(self.query(op)['receipt'], done['receipt'])

    def test_missing_historical_audit_is_unknown_not_creation(self):
        op = self.prepare()
        # This isolated synthetic legacy fixture deliberately lacks the event.
        with connection(self.bank.db_path, True) as db:
            db.execute("UPDATE operations SET status='REJECTED' WHERE operation_id=?", (op['operation_id'],))
        current = self.query(op)
        self.assertEqual(current['status'], 'REJECTED')
        self.assertIsNone(current['rejected_at'])

    def test_other_operation_or_other_audit_action_cannot_supply_time(self):
        other = self.prepare('other-transfer')
        self.now += timedelta(seconds=25)
        self.reject(other)
        current = self.prepare('current-transfer')
        with connection(self.bank.db_path, True) as db:
            db.execute("UPDATE operations SET status='REJECTED' WHERE operation_id=?", (current['operation_id'],))
        self.assertIsNone(self.query(current)['rejected_at'])

    def test_reopened_bank_preserves_timestamp_and_query_is_read_only(self):
        op = self.prepare()
        self.now += timedelta(seconds=40)
        first = self.reject(op)
        self.now += timedelta(days=1)
        self.bank = Service(self.bank.db_path, clock=lambda: self.now)
        with connection(self.bank.db_path) as db:
            before = list(db.execute('SELECT * FROM audit'))
        self.assertEqual(self.query(op)['rejected_at'], first['rejected_at'])
        with connection(self.bank.db_path) as db:
            after = list(db.execute('SELECT * FROM audit'))
        self.assertEqual([tuple(r) for r in before], [tuple(r) for r in after])


if __name__ == '__main__':
    unittest.main()
