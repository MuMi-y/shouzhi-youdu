"""Lost bridge stdout recovery from current bounded run files only."""
import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4
from src.bank_v1_web.run_trace import inventory,recover


class TraceRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)

    def files(self,prompt='current',tail=''):
        run=self.root/'dsh'/'runs'/str(uuid4());run.mkdir(parents=True)
        (run/'request.json').write_text(json.dumps({'prompt':prompt,'resume':False,'session_id':'synthetic','data_scope':'bank'}),encoding='utf-8')
        (run/'trace.jsonl').write_text(json.dumps({'kind':'request','name':'accounts','arguments':{}})+'\n'+tail,encoding='utf-8')
        return run

    def test_only_new_matching_run_is_read_old_files_never_replayed(self):
        self.files('old');before=inventory(self.root);self.files()
        value=recover(self.root,before,'current',None)
        self.assertTrue(value['trace_recovery']['complete'])
        self.assertEqual(1,len(value['trace']))

    def test_ambiguity_wrong_prompt_and_truncated_tail_are_incomplete(self):
        before=inventory(self.root);self.files('different')
        self.assertFalse(recover(self.root,before,'current',None)['trace_recovery']['complete'])
        before=inventory(self.root);self.files(tail='{"kind":')
        value=recover(self.root,before,'current',None)
        self.assertFalse(value['trace_recovery']['complete']);self.assertEqual(1,len(value['trace']))
        self.files()
        self.assertEqual([],recover(self.root,before,'current',None)['trace'])

    def test_outer_timeout_reads_current_trace_through_runner_and_persisted_job(self):
        import subprocess
        from unittest.mock import patch
        from tests.competition_ai import test_handoff_protocol as fixture
        from src.bank_requests.execution import run_chat
        f=fixture.HandoffProtocolTests();f.setUp();self.addCleanup(f.doCleanups)
        root=f.runner.runtime_dir
        class Process:
            pid=1;returncode=0
            def communicate(self,data=None,timeout=None):
                if data is None:return b'',b''
                incoming=json.loads(data)
                run=root/'dsh'/'runs'/str(uuid4());run.mkdir(parents=True)
                (run/'request.json').write_text(json.dumps({**incoming,'resume':False,'session_id':'offline','data_scope':'bank'}),encoding='utf-8')
                (run/'trace.jsonl').write_text(json.dumps({'kind':'request','call_id':'c1','name':'accounts','arguments':{}})+'\n',encoding='utf-8')
                raise subprocess.TimeoutExpired('offline-fixture',timeout)
            def wait(self,timeout=None):return 0
        with patch('src.bank_v1_web.worker.subprocess.Popen',return_value=Process()):
            result=run_chat(f.runner,f.jobs,'查询余额',None)
        self.assertFalse(result['ok'])
        saved=f.jobs.recent()[-1]
        self.assertEqual(1,len(saved['result']['trace']))
        snap=f.store.snapshot(f.store.for_job(saved['id']).run_id)
        self.assertEqual('INCOMPLETE',snap['run']['completeness'])
        self.assertEqual('MODEL_TRACE',snap['attempts'][0]['actor'])
        self.assertNotIn('DISPATCHED',[e['phase'] for e in snap['events']])
