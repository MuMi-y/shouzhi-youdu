"""Independent factors and bank side effects in temporary synthetic fixtures."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest
from src.bank_v1 import Service, DomainError
from src.bank_v1.security import SecurityPolicy, TotpPasswordVerifier, enroll, totp


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'bank.db'
        self.credentials = Path(self.tmp.name)/'auth.db'
        self.now = datetime(2026,10,12,5,tzinfo=timezone.utc)
        self.password = 'Synthetic-Password-Only-2026'
        # RFC6238 published interoperability secret; never a real credential.
        self.secret = 'GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ'
        enroll(self.credentials,self.password,self.secret,provenance='TEST_FIXTURE')
        self.policy = SecurityPolicy('independent',TotpPasswordVerifier(self.credentials))
        self.service = Service(self.path,clock=lambda:self.now,security_policy=self.policy)

    def prepare(self, key='one', amount=120000):
        return self.service.call_tool('prepare_transfer',{'source_account':'A','target_account':'B','amount_minor':amount,'idempotency_key':key})['data']['operation']

    def factors(self):
        return {'password':self.password,'otp':totp(self.secret,int(self.now.timestamp())//30)}

    def verify(self, op, **extra):
        return self.service.user_action('verify_operation',{'operation_id':op['operation_id'],'expected_summary':op['summary'],**self.factors(),**extra})

    def confirm(self, op, **extra):
        return self.service.user_action('confirm',{'operation_id':op['operation_id'],'expected_summary':op['summary'],**extra})

    def fail(self, code, fn):
        with self.assertRaises(DomainError) as exc:
            fn()
        self.assertEqual(exc.exception.code,code)

    def bank(self):
        state=self.service.state()
        return {k:state[k] for k in ('accounts','transactions','arrangements','mandates')}

    def test_rfc6238_sha1_vectors(self):
        for seconds,expected in [(59,'94287082'),(1111111109,'07081804'),(1111111111,'14050471'),(1234567890,'89005924'),(2000000000,'69279037'),(20000000000,'65353130')]:
            self.assertEqual(totp(self.secret,seconds//30,8),expected)

    def test_real_factor_grant_then_commit_and_idempotency(self):
        op=self.prepare()
        original=self.bank()
        grant=self.verify(op)
        self.assertEqual(original,self.bank())
        self.assertEqual(grant['credential_provenance'],'TEST_FIXTURE')
        done=self.confirm(op,verification_token=grant['verification_token'])
        self.assertEqual(done['receipt']['verification_mode'],'independent')
        self.assertEqual(done,self.confirm(op,verification_token=grant['verification_token']))
        self.assertEqual(self.service.state()['accounts'][0]['balance_minor'],380000)

    def test_no_simulated_challenge_in_independent_mode(self):
        op=self.prepare()
        self.fail('SIMULATION_DISABLED',lambda:self.service.user_action('challenge',{'operation_id':op['operation_id']}))
        self.fail('STRONG_VERIFICATION_REQUIRED',lambda:self.confirm(op,challenge_answer='000000'))
        self.assertEqual(self.service.state()['accounts'][0]['balance_minor'],500000)

    def test_wrong_password_and_otp_lock_persists_and_blocks_domain(self):
        op=self.prepare()
        before=self.bank()
        for _ in range(3):
            self.fail('VERIFICATION_FAILED',lambda:self.verify(op,password='not-the-password'))
        self.service=Service(self.path,clock=lambda:self.now,security_policy=self.policy)
        self.assertTrue(self.service.state()['security']['locked'])
        self.fail('SECURITY_LOCKED',lambda:self.prepare('blocked',100))
        self.fail('SECURITY_LOCKED',lambda:self.confirm(op))
        self.assertEqual(before,self.bank())
        self.assertEqual(self.service.call_tool('accounts',{})['data']['total_balance_minor'],600000)
        self.assertEqual(self.service.user_action('reject',{'operation_id':op['operation_id']})['status'],'REJECTED')

    def test_unlock_requires_correct_independent_factors(self):
        op=self.prepare()
        for _ in range(3):
            self.fail('VERIFICATION_FAILED',lambda:self.verify(op,otp='wrong'))
        self.fail('VERIFICATION_FAILED',lambda:self.service.user_action('security_unlock',{'password':'wrong','otp':'wrong','reason':'Review'}))
        result=self.service.user_action('security_unlock',{**self.factors(),'reason':'Reviewed synthetic incident'})
        self.assertFalse(result['locked'])
        self.assertEqual(result['failures'],0)
        self.prepare('available',100)

    def test_grant_cannot_authorize_another_operation(self):
        one,two=self.prepare(),self.prepare('two')
        grant=self.verify(one)
        self.fail('INVALID_VERIFICATION_GRANT',lambda:self.confirm(two,verification_token=grant['verification_token']))
        self.assertEqual(self.confirm(one,verification_token=grant['verification_token'])['status'],'SUCCEEDED')

    def test_otp_replay_cannot_create_second_grant(self):
        one,two=self.prepare(),self.prepare('two')
        self.verify(one)
        self.fail('VERIFICATION_FAILED',lambda:self.verify(two))

    def test_grant_expires_without_posting(self):
        op=self.prepare()
        grant=self.verify(op)
        self.now+=timedelta(seconds=121)
        before=self.bank()
        self.fail('INVALID_VERIFICATION_GRANT',lambda:self.confirm(op,verification_token=grant['verification_token']))
        self.assertEqual(before,self.bank())

    def test_summary_bound_before_verification(self):
        op=self.prepare()
        changed={**op['summary'],'kind':'other'}
        self.fail('SUMMARY_MISMATCH',lambda:self.verify(op,expected_summary=changed))
        self.assertTrue(self.verify(op)['verification_token'])

    def test_model_cannot_get_or_approve_factors(self):
        names={s['name'] for s in self.service.tool_specs()}
        for name in ['verify_operation','security_unlock','challenge','confirm']:
            self.assertNotIn(name,names)
        for _ in range(3):
            self.fail('UNKNOWN_TOOL',lambda:self.service.call_tool('confirm',{}))
        self.assertTrue(self.service.state()['security']['locked'])

    def test_repeat_forbidden_account_access_locks(self):
        for index in range(3):
            self.fail('FORBIDDEN',lambda:self.service.call_tool('prepare_transfer',{'source_account':'X','target_account':'A','amount_minor':1,'idempotency_key':str(index)}))
        self.assertTrue(self.service.state()['security']['locked'])

    def test_unconfigured_restart_does_not_downgrade(self):
        self.service=Service(self.path,clock=lambda:self.now)
        self.assertEqual(self.service.state()['security']['mode'],'independent')
        op=self.prepare()
        self.fail('VERIFIER_UNAVAILABLE',lambda:self.verify(op))
        self.fail('SECURITY_DOWNGRADE_DENIED',lambda:Service(self.path,clock=lambda:self.now,security_policy=SecurityPolicy()))

    def test_blank_verifier_is_fail_closed_without_creating_credentials(self):
        missing=Path(self.tmp.name)/'missing.db'
        self.service=Service(self.path,clock=lambda:self.now,security_policy=SecurityPolicy('independent',TotpPasswordVerifier(missing)))
        op=self.prepare()
        self.fail('VERIFIER_UNAVAILABLE',lambda:self.verify(op))
        self.assertFalse(missing.exists())

    def test_limit_is_cumulative_and_checked_at_commit(self):
        one,two=self.prepare('one',60000),self.prepare('two',60000)
        self.confirm(one)
        self.fail('STRONG_VERIFICATION_REQUIRED',lambda:self.confirm(two))
        grant=self.verify(two)
        self.assertEqual(self.confirm(two,verification_token=grant['verification_token'])['receipt']['verification'],'STRONG')

    def test_concurrent_confirm_consumes_once(self):
        op=self.prepare()
        grant=self.verify(op)
        with ThreadPoolExecutor(max_workers=4) as pool:
            result=list(pool.map(lambda _:self.confirm(op,verification_token=grant['verification_token']),range(4)))
        self.assertEqual(len({r['receipt']['receipt_id'] for r in result}),1)
        self.assertEqual(len([x for x in self.service.state()['transactions'] if x['operation_id']==op['operation_id']]),2)

    def test_audit_redacts_password_otp_and_tokens(self):
        op=self.prepare()
        grant=self.verify(op)
        self.confirm(op,verification_token=grant['verification_token'])
        with closing(sqlite3.connect(self.path)) as db:
            output=json.dumps(db.execute('SELECT * FROM audit').fetchall())
            actions={r[0] for r in db.execute('SELECT action FROM security_audit')}
        for value in [self.password,self.secret,grant['verification_token']]:
            self.assertNotIn(value,output)
        self.assertIn('INDEPENDENT_VERIFIED',actions)
        self.assertIn('VERIFICATION_CONSUMED',actions)

    def test_ordinary_model_failure_does_not_lock(self):
        event=self.service.user_action('due_check',{})['event_id']
        for _ in range(3):
            self.service.finish_event(event,'',success=False,error='provider timeout')
        self.assertFalse(self.service.state()['security']['locked'])
        self.assertEqual(self.service.state()['security']['failures'],0)

    def test_enrollment_cannot_silently_overwrite(self):
        with self.assertRaises(FileExistsError):
            enroll(self.credentials,'Another-Password',self.secret,provenance='TEST_FIXTURE')

    def test_lock_and_unlock_revoke_previously_issued_grants(self):
        op=self.prepare()
        grant=self.verify(op)
        for _ in range(3):
            self.fail('UNKNOWN_TOOL',lambda:self.service.call_tool('model_approve',{}))
        self.now+=timedelta(seconds=30)
        self.service.user_action('security_unlock',{**self.factors(),'reason':'Review'})
        self.fail('INVALID_VERIFICATION_GRANT',lambda:self.confirm(op,verification_token=grant['verification_token']))

    def test_threshold_exactly_1000_is_yellow(self):
        op=self.prepare(amount=100000)
        self.assertEqual(self.confirm(op)['receipt']['verification'],'YELLOW')

    def test_failed_attempt_audit_survives_business_rollback(self):
        op=self.prepare()
        before=self.bank()
        self.fail('VERIFICATION_FAILED',lambda:self.verify(op,otp='wrong'))
        with closing(sqlite3.connect(self.path)) as db:
            detail=db.execute("SELECT detail_json FROM security_audit WHERE action='SECURITY_DENIED'").fetchone()[0]
        self.assertEqual(json.loads(detail),{'code':'VERIFICATION_FAILED','counted':True})
        self.assertEqual(before,self.bank())


if __name__=='__main__':
    unittest.main()
