"""Offline frozen-label stories and unlabeled disjoint MoneyData exploration."""
from contextlib import closing
from datetime import datetime, timedelta, timezone, date
import csv
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.bank_insights import InsightService
from src.bank_insights.rules import detect_recurring, ALGORITHM


def write_json(path, value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')


def synthetic(case):
    """Minimal explicit bank adapter fixture; no original/demo reset."""
    with tempfile.TemporaryDirectory() as folder:
        path=Path(folder)/'bank.sqlite3'
        now=datetime.fromisoformat(case['as_of']+'T12:00:00+08:00')
        with closing(sqlite3.connect(path)) as db:
            db.execute('CREATE TABLE accounts(account_id TEXT,subject_id TEXT,currency TEXT)')
            db.execute("INSERT INTO accounts VALUES('A','U1','CNY')")
            db.execute('CREATE TABLE transactions(transaction_id TEXT,account_id TEXT,kind TEXT,amount_minor INTEGER,description TEXT,business_date TEXT,created_at TEXT,source TEXT)')
            for i,(day,amount) in enumerate(zip(case['days'],case['amounts'])):
                signed=amount if case['kind']=='refund' else -amount
                db.execute('INSERT INTO transactions VALUES(?,?,?,?,?,?,?,?)',
                    (str(i),'A',case['kind'],signed,'冻结测试商户',day,day+'T00:00:00+08:00','SYNTHETIC'))
            db.commit()
        clock=[now]
        service=InsightService(Path(folder)/'insights.sqlite3',bank=SimpleNamespace(db_path=path),clock=lambda:clock[0])
        # Sequential prefix checkpoints assert no early candidate before third observation.
        prefixes=[]
        for day in sorted(set(case['days'])):
            if day > case['as_of']: continue
            clock[0]=datetime.fromisoformat(day+'T12:00:00+08:00')
            value=service.check()['data']
            prefixes.append({'as_of':day,'candidate_count':len(value['candidates']),
                             'notifications':len(value['new_notifications'])})
        clock[0]=now
        initial=service.check()['data']
        if case['feedback'] and initial['candidates']:
            item=initial['candidates'][0]
            service.user_action('candidate_feedback',{'candidate_id':item['candidate_id'],
                'expected_version':item['version'],'decision':case['feedback'],**case.get('corrections',{})})
        result=service.check()['data']
        repeat=service.check()['data']
        kinds=sorted(n['kind'] for n in result['new_notifications'])
        return {'id':case['id'],'prefixes':prefixes,'candidate_count':len(result['candidates']),
            'notification_kinds':kinds,'repeat_new_count':len(repeat['new_notifications']),
            'renewal_lead_days':[(date.fromisoformat(n['due_date'])-now.date()).days for n in result['new_notifications'] if n['kind']=='RENEWAL_DUE'],
            'passed':len(result['candidates'])==case['expected_candidates'] and kinds==sorted(case['expected_notices']) and not repeat['new_notifications']}


def history(path):
    rows=[]
    with path.open(encoding='utf-8-sig',newline='') as stream:
        for item in csv.DictReader(stream):
            debit=int(item['debit_minor'] or 0); credit=int(item['credit_minor'] or 0)
            rows.append({'id':'moneydata:'+item['source_transaction_number'],'date':item['transaction_date'],
                'account_id':None,'kind':'outflow' if debit else 'inflow','amount_minor':credit-debit,
                'description':item['description_raw'],'category':item['category_raw'],'source':'MoneyData'})
    windows=[]
    for role,start,end in [('development_exploration','2019-01-01','2019-12-31'),('heldout_exploration','2020-01-01','2020-12-31')]:
        in_window=[r for r in rows if start<=r['date']<=end]
        checkpoints=[]
        cursor=date.fromisoformat(start)
        while cursor.isoformat()<=end:
            cutoff=cursor.isoformat()
            known=[r for r in in_window if r['date']<=cutoff]
            candidates=detect_recurring(known,as_of=cutoff,currency=None)
            assert all(max(c['observed_dates'])<=cutoff for c in candidates)
            checkpoints.append({'as_of':cutoff,'available_rows':len(known),'candidate_count':len(candidates),
                'candidates':[{'id':c['candidate_id'],'merchant_key':c['merchant_key'],'evidence_refs':c['evidence_refs'],
                    'observed_dates':c['observed_dates'],'candidate_next_date':c['next_due_date'],'currency':None} for c in candidates]})
            cursor+=timedelta(days=14)
        windows.append({'role':role,'start':start,'end':end,'row_count':len(in_window),'checkpoints':checkpoints})
    return {'source_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'windows':windows,
        'no_future_evidence_assertions_passed':True,'currency':None,'parameter_tuning_on_evaluation':False,
        'precision':None,'recall':None,'user_satisfaction':None,
        'limitation':'公开历史没有订阅/提醒真值；只统计截至当日的周期出账候选，不估真实续费准确率或用户满意度；不并入银行余额。'}


def main():
    output=ROOT/'management/evidence/COMPETITION_V1/insights'
    output.mkdir(parents=True,exist_ok=True)
    fixture=ROOT/'data/synthetic/competition_v1/replay_cases.json'
    cases=json.loads(fixture.read_text(encoding='utf-8'))['cases']
    started=time.perf_counter()
    results=[synthetic(c) for c in cases]
    evaluation={'algorithm':ALGORITHM,'fixture_sha256':hashlib.sha256(fixture.read_bytes()).hexdigest(),
        'cases':results,'passed':sum(c['passed'] for c in results),'total':len(results),
        'notification_false_positive_cases':sum(bool(set(r['notification_kinds'])-set(c['expected_notices'])) for r,c in zip(results,cases)),
        'notification_false_negative_cases':sum(bool(set(c['expected_notices'])-set(r['notification_kinds'])) for r,c in zip(results,cases)),
        'repeat_notification_count':sum(r['repeat_new_count'] for r in results),
        'model_calls':0,'seconds':time.perf_counter()-started,
        'scope':'冻结的14条确定性有限规则故事，不是实际用户效果。资金风险/退款跟进由bank_v1验证，此处只验证周期候选与关注。'}
    tp=sum(r['candidate_count'] > 0 and c['expected_candidates'] > 0 for r,c in zip(results,cases))
    predicted=sum(r['candidate_count'] > 0 for r in results)
    truth=sum(c['expected_candidates'] > 0 for c in cases)
    evaluation['candidate_precision_on_synthetic']=tp/predicted if predicted else None
    evaluation['candidate_recall_on_synthetic']=tp/truth if truth else None
    write_json(output/'synthetic-replay.json',evaluation)
    write_json(output/'moneydata-exploration.json',history(ROOT/'data/working/moneydata_v1/transactions.csv'))
    print(json.dumps({k:v for k,v in evaluation.items() if k!='cases'},ensure_ascii=False))
    if evaluation['passed'] != len(results): raise SystemExit(1)


if __name__=='__main__': main()
