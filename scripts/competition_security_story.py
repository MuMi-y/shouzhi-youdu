"""Reproducible synthetic security story; secrets remain inside a temporary dir."""
from contextlib import closing
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from src.bank_v1 import Service,DomainError
from src.bank_v1.security import SecurityPolicy,TotpPasswordVerifier,enroll,totp


def main():
    root=Path(__file__).resolve().parents[1]
    out=root/'management/evidence/COMPETITION_V1/security'
    now=datetime(2026,10,12,5,tzinfo=timezone.utc)
    steps=[]
    with tempfile.TemporaryDirectory() as directory:
        path=Path(directory)/'bank.db'
        auth=Path(directory)/'auth.db'
        secret='GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ'
        password='Synthetic-Password-Only-2026'
        enroll(auth,password,secret,provenance='TEST_FIXTURE')
        policy=SecurityPolicy('independent',TotpPasswordVerifier(auth))
        service=Service(path,clock=lambda:now,security_policy=policy)
        operation=service.call_tool('prepare_transfer',{'source_account':'A','target_account':'B','amount_minor':120000,'idempotency_key':'security-story'})['data']['operation']
        confirm={'operation_id':operation['operation_id'],'expected_summary':operation['summary']}
        def denied(label,fn):
            try:
                fn()
                raise AssertionError('Expected refusal')
            except DomainError as exc:
                steps.append({'step':label,'code':exc.code,'security':service.state()['security'],
                              'accounts':service.state()['accounts']})
        denied('No factors cannot transfer',lambda:service.user_action('confirm',confirm))
        for _ in range(3):
            denied('Wrong factors',lambda:service.user_action('verify_operation',{**confirm,'password':'wrong','otp':'wrong'}))
        service=Service(path,clock=lambda:now,security_policy=policy)
        denied('Restart remains locked',lambda:service.user_action('confirm',confirm))
        assert service.call_tool('accounts',{})['data']['total_balance_minor']==600000
        factors={'password':password,'otp':totp(secret,int(now.timestamp())//30)}
        unlocked=service.user_action('security_unlock',{**factors,'reason':'Synthetic incident reviewed'})
        steps.append({'step':'Independent factors unlock','security':unlocked})
        now+=timedelta(seconds=30)
        factors['otp']=totp(secret,int(now.timestamp())//30)
        grant=service.user_action('verify_operation',{**confirm,**factors})
        done=service.user_action('confirm',{**confirm,'verification_token':grant['verification_token']})
        repeat=service.user_action('confirm',{**confirm,'verification_token':grant['verification_token']})
        assert repeat==done
        state=service.state()
        assert state['accounts'][0]['balance_minor']==380000
        assert state['accounts'][1]['balance_minor']==220000
        with closing(sqlite3.connect(path)) as db:
            db.row_factory=sqlite3.Row
            audit=[dict(r) for r in db.execute('SELECT * FROM security_audit ORDER BY rowid')]
            entries=db.execute('SELECT count(*) FROM transactions WHERE operation_id=?',(operation['operation_id'],)).fetchone()[0]
        assert entries==2
        steps.append({'step':'Verified transfer and repeated confirmation','receipt':done['receipt'],
                      'accounts':state['accounts'],'entries':entries,'duplicate_receipt_same':repeat==done})
        result={'provenance':'TEST_FIXTURE','passed':True,'steps':steps,'security_audit':audit}
        encoded=json.dumps(result,ensure_ascii=False,indent=2)
        assert password not in encoded and secret not in encoded and grant['verification_token'] not in encoded
        out.mkdir(parents=True,exist_ok=True)
        (out/'security-story.json').write_text(encoded,encoding='utf-8')
        print(json.dumps({'passed':True,'locked_persisted':True,'independent_unlock':True,'accounts_after':[380000,220000],'transfer_entries':entries,'duplicate_same_receipt':True,'credential_provenance':'TEST_FIXTURE'},ensure_ascii=False))


if __name__=='__main__':
    main()
