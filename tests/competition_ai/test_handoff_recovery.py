"""Real SQLite restart, ownership and admission limits; synthetic data only."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from src.bank_v1_web.jobs import JobStore
from src.bank_v1_web.handoff_store import HandoffError, service_ownership
from src.competition_web.app import create_app


class HandoffRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='handoff-recovery-')
        self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'jobs.sqlite3'
        self.clock=lambda:datetime(2026,10,30,2,tzinfo=timezone.utc)
        self.restart()

    def restart(self):
        if hasattr(self,'store'):
            self.store.release_owner()
        self.jobs=JobStore(self.path,clock=self.clock,handoff=True)
        self.store=self.jobs.handoff
        self.addCleanup(self.store.release_owner)

    def run_handle(self):
        job=self.jobs.add('查询')
        self.jobs.claim(job_id=job['id'])
        return self.store.for_job(job['id'])

    def counters(self):
        with self.jobs.connection() as db:
            capacity=self.store._meta(db,'capacity_v1')
            for field in ('used_bytes','reserved_bytes','used_event_slots','reserved_event_slots'):
                total=sum(db.execute(f'SELECT COALESCE(sum({field}),0) FROM {table}').fetchone()[0]
                          for table in ('handoff_runs','handoff_attempts'))
                self.assertEqual(total,capacity[field],field)
            return capacity

    def test_restart_rebinds_only_never_started_queue_and_rejects_old_handle(self):
        queued=self.jobs.add('还没开始')
        before=self.store.for_job(queued['id'])
        running=self.run_handle()
        attempt=self.store.begin_attempt(running,'prepare_transfer',{},'MODEL_GATEWAY')
        self.store.validated(attempt,{'idempotency_key':'once'})
        self.store.append(attempt,'DISPATCHED',{})
        instance=self.store.instance
        self.restart()
        self.assertEqual(instance,self.store.instance)
        self.assertNotEqual(before.epoch_id,self.store.epoch)
        with self.assertRaises(ValueError):
            self.store.begin_attempt(running,'prepare_transfer',{},'MODEL_GATEWAY')
        self.jobs.claim(job_id=queued['id'])
        after=self.store.for_job(queued['id'])
        self.assertEqual(before.run_id,after.run_id)
        self.assertEqual(before.epoch_id,self.store.snapshot(after.run_id)['run']['created_epoch_id'])
        self.assertEqual('INTERRUPTED',self.store.snapshot(running.run_id)['run']['state'])
        self.assertIn('UNKNOWN',[e['phase'] for e in self.store.snapshot(running.run_id)['events']])
        self.counters()

    def test_closed_unknown_keeps_reserved_tail_and_late_result_does_not_replay(self):
        handle=self.run_handle()
        a=self.store.begin_attempt(handle,'prepare_transfer',{},'MODEL_GATEWAY')
        self.store.validated(a,{'idempotency_key':'once'})
        self.store.append(a,'DISPATCHED',{})
        self.jobs.finish(handle.job_id,{'ok':False,'error':'interrupted'})
        self.store.producers_finished(handle)
        old=self.store.snapshot(handle.run_id)['attempts'][0]
        self.assertGreater(old['reserved_event_slots'],0)
        self.store.append(a,'LATE_RETURN',{'refs':[{'kind':'operation','id':'op_synthetic'}]})
        self.store.producers_finished(handle)
        self.assertEqual(0,self.store.snapshot(handle.run_id)['attempts'][0]['reserved_event_slots'])
        self.store.producers_finished(handle)
        events=self.store.snapshot(handle.run_id)['events']
        self.assertEqual(1,sum(e['phase']=='DISPATCHED' for e in events))
        self.assertIn('UNKNOWN',[e['phase'] for e in events])
        self.counters()

    def test_identical_append_race_consumes_one_slot_conflict_is_rejected(self):
        handle=self.run_handle();a=self.store.begin_attempt(handle,'accounts',{},'MODEL_GATEWAY')
        def append(_):
            return self.store.append(a,'TRACE_LINK',{'call_id':'c1'},producer_key='same-event')
        with ThreadPoolExecutor(max_workers=4) as pool:
            sequences=list(pool.map(append,range(8)))
        self.assertEqual(1,len(set(sequences)))
        with self.assertRaisesRegex(HandoffError,'EVENT_KEY_CONFLICT'):
            self.store.append(a,'TRACE_LINK',{'call_id':'c2'},producer_key='same-event')
        self.assertEqual(2,len(self.store.snapshot(handle.run_id)['events']))
        self.counters()

    def test_event_slots_and_bytes_are_independent_admission_limits(self):
        for kind in ('max_slots','max_bytes'):
            with self.subTest(kind=kind):
                handle=self.run_handle()
                with self.jobs.connection() as db:
                    value=self.store._meta(db,'capacity_v1')
                    old=value[kind]
                    value[kind]=(value['used_event_slots']+value['reserved_event_slots']+1 if kind=='max_slots'
                                 else value['used_bytes']+value['reserved_bytes']+1)
                    self.store._set_meta(db,'capacity_v1',value)
                with self.assertRaisesRegex(HandoffError,'AUDIT_CAPACITY_EXCEEDED'):
                    self.store.begin_attempt(handle,'prepare_transfer',{},'MODEL_GATEWAY')
                self.assertEqual([],self.store.snapshot(handle.run_id)['attempts'])
                self.assertEqual('AUDIT_BLOCKED',self.store.snapshot(handle.run_id)['run']['state'])
                with self.jobs.connection() as db:
                    value=self.store._meta(db,'capacity_v1');value[kind]=old;self.store._set_meta(db,'capacity_v1',value)
                self.counters()

    def test_tail_slots_cannot_be_consumed_by_normal_events(self):
        handle=self.run_handle();a=self.store.begin_attempt(handle,'accounts',{},'MODEL_GATEWAY')
        for i in range(11):
            self.store.append(a,'TRACE_LINK',{'index':i},producer_key='trace:'+str(i))
        with self.assertRaisesRegex(HandoffError,'AUDIT_EVENT_LIMIT'):
            self.store.append(a,'TRACE_LINK',{'index':12},producer_key='trace:12')
        self.store.append(a,'UNKNOWN',{'reason':'interrupted'})
        self.store.append(a,'LATE_RETURN',{'refs':[]})
        self.counters()

    def test_old_launcher_lock_blocks_migration_and_inherited_lock_does_not_self_lock(self):
        with tempfile.TemporaryDirectory(prefix='old-r7-lock-') as d:
            owner,_=service_ownership(d)
            try:
                with self.assertRaisesRegex(HandoffError,'SERVICE_INSTANCE_ALREADY_ACTIVE'):
                    create_app(d,clock=self.clock)
                self.assertFalse((Path(d)/'bank.sqlite3').exists())
                app=create_app(d,clock=self.clock,service_lock=owner)
                app.extensions['bank_v1']['jobs'].handoff.release_owner()
                self.assertFalse(owner.closed)
            finally:
                owner.close()

    def test_same_return_retry_after_close_keeps_original_event_and_no_new_capacity(self):
        h=self.run_handle();a=self.store.begin_attempt(h,'accounts',{},'MODEL_GATEWAY')
        self.store.validated(a,{});self.store.append(a,'DISPATCHED',{})
        first=self.store.append(a,'RETURNED',{'refs':[]})
        self.jobs.finish(h.job_id,{'ok':True});self.store.producers_finished(h)
        self.assertEqual(first,self.store.append(a,'RETURNED',{'refs':[]}))
        with self.assertRaisesRegex(HandoffError,'EVENT_KEY_CONFLICT'):
            self.store.append(a,'RETURNED',{'refs':[{'kind':'operation','id':'different'}]})
        self.counters()

    def test_missing_producer_exit_does_not_release_even_known_results(self):
        h=self.run_handle();a=self.store.begin_attempt(h,'accounts',{},'MODEL_GATEWAY')
        self.store.append(a,'RETURNED',{})
        self.jobs.finish(h.job_id,{'ok':True})
        self.store.producers_finished(h,trace_finalized=False)
        snapshot=self.store.snapshot(h.run_id)
        self.assertEqual(0,snapshot['run']['producers_stopped'])
        self.assertGreater(snapshot['attempts'][0]['reserved_event_slots'],0)


if __name__=='__main__':
    unittest.main()
