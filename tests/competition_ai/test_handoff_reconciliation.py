"""Original-key reconciliation, restart and migration use real disposable stores."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from tests.competition_ai import test_handoff_protocol as fixture
from tests.g2_goals import test_preview as goal_fixture
from src.bank_requests.attempt_projection import collect,disclose
from src.bank_v1_web.job_display import project
from src.bank_v1_web.handoff_reconcile import reconcile
from src.bank_v1_web.jobs import JobStore
from src.competition_web.app import create_app


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.HandoffProtocolTests();self.f.setUp();self.addCleanup(self.f.doCleanups)

    def restart(self):
        f=self.f;f.store.release_owner()
        app=create_app(f.root,clock=lambda:f.now)
        jobs=app.extensions['bank_v1']['jobs'];self.addCleanup(jobs.handoff.release_owner)
        return jobs,app.extensions['competition']

    def capacity(self,store):
        with store.jobs.connection() as db:return store._meta(db,'capacity_v1')

    def pending(self):
        f=self.f;job=f.jobs.add('准备从A转500元到B');f.jobs.claim(job_id=job['id'])
        h=f.store.for_job(job['id']);args={'source_account':'A','target_account':'B','amount_minor':50000,'idempotency_key':'lost'}
        a=f.store.begin_attempt(h,'prepare_transfer',args,'MODEL_GATEWAY',effect_class='BANK_PREPARE')
        f.store.validated(a,args);f.store.append(a,'DISPATCHED',{})
        f.service.bank.call_tool('prepare_transfer',args)
        return job,h,a

    def test_same_epoch_certain_recovery_releases_reservations_idempotently(self):
        f=self.f;job,h,a=self.pending()
        f.jobs.finish(job['id'],{'ok':False,'error':'lost'});f.store.producers_finished(h)
        self.assertGreater(f.store.snapshot(h.run_id)['attempts'][0]['reserved_event_slots'],0)
        project(f.jobs.get(job['id']),f.service,f.store)
        self.assertEqual(0,f.store.snapshot(h.run_id)['attempts'][0]['reserved_event_slots'])
        capacity=self.capacity(f.store)
        project(f.jobs.get(job['id']),f.service,f.store)
        self.assertEqual(capacity,self.capacity(f.store))

    def test_new_epoch_recovers_original_key_but_old_handles_stay_revoked(self):
        f=self.f;job,h,a=self.pending();jobs,service=self.restart();store=jobs.handoff
        project(jobs.get(job['id']),service,store)
        snap=store.snapshot(h.run_id)
        self.assertEqual(0,snap['attempts'][0]['reserved_event_slots'])
        self.assertTrue(any(e['data'].get('producer_boundary')=='EXCLUSIVE_HOST_EPOCH_RETIRED' for e in snap['events']))
        with self.assertRaises(ValueError):store.begin_attempt(h,'prepare_transfer',{},'MODEL_GATEWAY')
        self.assertEqual(1,len(service.bank.state()['operations']))
        before=self.capacity(store);project(jobs.get(job['id']),service,store)
        self.assertEqual(before,self.capacity(store))

    def test_unpaired_trace_survives_restart_as_unknown_not_pre_rejection(self):
        f=self.f;job=f.jobs.add('查询');f.jobs.claim(job_id=job['id']);h=f.store.for_job(job['id'])
        collect(f.store,h,[{'kind':'request','call_id':'unknown','name':'prepare_transfer','arguments':{}}])
        jobs,service=self.restart();shown=project(jobs.get(job['id']),service,jobs.handoff)
        entry=shown['result']['host_disclosure']['entries'][0]
        self.assertEqual('UNKNOWN',entry['status']);self.assertNotIn('没有执行',entry['text'])
        snap=jobs.handoff.snapshot(h.run_id)
        self.assertGreater(snap['attempts'][0]['reserved_event_slots'],0)
        self.assertFalse({'DISPATCHED','REJECTED_BEFORE_DISPATCH'} & {e['phase'] for e in snap['events']})

    def test_legacy_migration_backs_up_and_never_claims_full_history(self):
        with tempfile.TemporaryDirectory(prefix='handoff-legacy-') as root:
            path=Path(root);old=JobStore(path/'jobs.sqlite3',clock=lambda:self.f.now)
            job=old.add('历史请求');old.finish(job['id'],{'ok':False,'error':'历史错误'})
            app=create_app(path,clock=lambda:self.f.now);jobs=app.extensions['bank_v1']['jobs']
            try:
                backups=list(path.glob('handoff-before-v1-*/manifest.json'))
                self.assertTrue(backups)
                for manifest in backups:
                    self.assertIn('jobs.sqlite3',json.loads(manifest.read_text(encoding='utf-8')))
                h=jobs.handoff.for_job(job['id'])
                self.assertEqual('legacy_source_v1',h.source_contract)
                self.assertEqual('HISTORY',jobs.handoff.snapshot(h.run_id)['run']['state'])
                shown=project(jobs.get(job['id']),app.extensions['competition'],jobs.handoff)
                self.assertFalse(shown['result']['host_disclosure']['complete'])
                self.assertEqual([],shown['result']['host_disclosure']['entries'])
                self.assertEqual('历史错误',shown['result']['error'])
            finally:jobs.handoff.release_owner()

    def test_task_saved_before_gateway_response_loss_is_recovered_by_exact_binding(self):
        f=self.f;job=f.jobs.add('准备从A转500元到B');f.jobs.claim(job_id=job['id']);h=f.store.for_job(job['id'])
        token=f.gateway.open(f.jobs.request_context(job['id']),readonly=False,handle=h)
        args={'prompt':'准备转账','title':'任务','idempotency_key':'task-lost','nodes':[{'id':'t','tool':'prepare_transfer',
            'args':{'source_account':'A','target_account':'B','amount_minor':50000},'depends_on':[]}],
            'request_binding':{'items':[f.item(job['id'])],'node_items':{'t':'transfer'}}}
        original=f.store.append
        def lost(handle,phase,payload,**kw):
            if phase=='RETURNED':raise RuntimeError('synthetic lost response persistence')
            return original(handle,phase,payload,**kw)
        with patch.object(f.store,'append',side_effect=lost):
            with self.assertRaises(RuntimeError):f.gateway.call(token,'submit_task_plan',args)
        f.gateway.close(token);f.jobs.finish(job['id'],{'ok':False,'error':'lost'});f.store.producers_finished(h)
        shown=project(f.jobs.get(job['id']),f.service,f.store)
        refs=shown['result']['host_disclosure']['entries'][0]['objects']
        self.assertEqual('task',refs[0]['kind'])
        self.assertEqual(0,f.store.snapshot(h.run_id)['attempts'][0]['reserved_event_slots'])
        self.assertEqual('WAITING_CONFIRMATION',f.service.tasks.tick(refs[0]['id'])['state'])

    def test_goal_compound_failure_keeps_bank_pending_and_draft_key_matches_exactly(self):
        g=goal_fixture.PreviewTests();g.setUp();self.addCleanup(g.doCleanups)
        service=g.service;store=g.jobs.handoff
        service.goals.action('save',{'expected_revision':0,'profile':g.profile})
        assessed=service.goals.action('compare',{'expected_revision':1})
        choice=service.goals.action('select',{'expected_revision':1,'assessment_id':assessed['assessment_id'],
            'option_id':'reduced','wait_for_income':False,'idempotency_key':'choice'})
        original=service.goals._prepare
        def fail_after_bank(*args):original(*args);raise RuntimeError('goal commit lost')
        with patch.object(service.goals,'_prepare',side_effect=fail_after_bank):
            with self.assertRaises(RuntimeError):service.host_action('goals.prepare',{'choice_id':choice['choice_id']},
                lambda:service.goals.action('prepare',{'choice_id':choice['choice_id']}),effect_class='GOAL_ACTION')
        with g.jobs.connection() as db:run=db.execute("SELECT run_id FROM handoff_attempts WHERE entry_name='goals.prepare'").fetchone()[0]
        reconcile(store,store.snapshot(run),service)
        snap=store.snapshot(run);view=disclose(snap,service)
        self.assertEqual('UNKNOWN',view['entries'][0]['status'])
        self.assertTrue(any(r['kind']=='operation' and r['state']=='PENDING' for r in view['entries'][0]['objects']))
        self.assertTrue(any(r['kind']=='choice' and r['id']==choice['choice_id'] and r['state']=='SELECTED'
                            for r in view['entries'][0]['objects']))
        self.assertGreater(snap['attempts'][0]['reserved_event_slots'],0)
        self.assertEqual(1,len(service.bank.state()['operations']))
        args={'profile':g.profile,'expected_revision':1,'idempotency_key':'lost-draft'}
        h=store.open_host_run('SYSTEM',{'test':'draft'});a=store.begin_attempt(h,'propose_goal',args,'SYSTEM',effect_class='GOAL_DRAFT')
        store.validated(a,args);store.append(a,'DISPATCHED',{})
        draft=service.call_tool('propose_goal',args)
        store.close(h,{'ok':False});store.producers_finished(h)
        reconcile(store,store.snapshot(h.run_id),service)
        view=disclose(store.snapshot(h.run_id),service)
        self.assertEqual(draft['data']['draft_id'],view['entries'][0]['objects'][0]['id'])
        self.assertEqual('UNCONFIRMED_DRAFT',view['entries'][0]['objects'][0]['state'])
        self.assertEqual(0,store.snapshot(h.run_id)['attempts'][0]['reserved_event_slots'])
        with patch('src.bank_v1_web.handoff_reconcile.read',return_value=None):
            unavailable_draft=disclose(store.snapshot(h.run_id),service)
            unavailable_choice=disclose(snap,service)
        self.assertEqual('UNKNOWN',unavailable_draft['entries'][0]['objects'][0]['state'])
        self.assertTrue(any(r['kind']=='choice' and r['state']=='UNKNOWN'
                            for r in unavailable_choice['entries'][0]['objects']))
