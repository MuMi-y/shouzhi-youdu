"""Presentation-only synthetic inputs, evaluated by the frozen pure forecast. No DB/AI."""
from pathlib import Path
import sys,json,copy,hashlib
ROOT=Path(__file__).resolve().parents[1]
SOURCE=ROOT
sys.path.insert(0,str(SOURCE))
from src.bank_v1.finance import forecast_from_snapshot


def arrangement(id,kind,amount,day):
    return dict(arrangement_id=id,version=1,title=id,kind=kind,amount_minor=amount*100,due_date=day,status='active')
snapshot={'business_date':'2026-11-01','accounts':{'total_balance_minor':1800000,'accounts':[{'account_id':'A','balance_minor':1800000,'version':1}]},'arrangements':[arrangement('生活开销','expense',3000,'2026-11-06'),arrangement('预计收入','income',10000,'2026-11-21'),arrangement('电脑计划','goal',20000,'2026-11-26')],'candidates':[],'matched_refunds':[]}
cases={}
cases['base']=forecast_from_snapshot(snapshot,'2026-12-01')
cases['spend100']=forecast_from_snapshot(snapshot,'2026-12-01',10000,'2026-11-01')
cases['spend2500']=forecast_from_snapshot(snapshot,'2026-12-01',250000,'2026-11-01')
late=copy.deepcopy(snapshot);late['arrangements'][1]['due_date']='2026-11-29'
cases['late_income']=forecast_from_snapshot(late,'2026-12-01')
posted=copy.deepcopy(snapshot);posted['accounts']['total_balance_minor']=1790000;posted['accounts']['accounts'][0]['balance_minor']=1790000
cases['posted_expense100']=forecast_from_snapshot(posted,'2026-12-01')
posted['accounts']['total_balance_minor']=1810000;posted['accounts']['accounts'][0]['balance_minor']=1810000
cases['posted_income200']=forecast_from_snapshot(posted,'2026-12-01')
summary={key:{'minimum':v['lowest_balance_minor']/100,'date':v['lowest_balance_date'],'without_income_minimum':v['without_future_income_lowest_balance_minor']/100,'end':v['daily'][-1]['projected_balance_minor']/100,'reserve_shortfall':max(0,3000-v['lowest_balance_minor']/100)} for key,v in cases.items()}
assert summary['base']['minimum']==5000 and summary['base']['without_income_minimum']==-5000
assert summary['spend2500']['reserve_shortfall']==500
assert summary['late_income']['minimum']==-5000 and summary['late_income']['end']==5000
assert summary['posted_expense100']['minimum']==4900 and summary['posted_income200']['minimum']==5100
out={'data_class':'SYNTHETIC_DEMONSTRATION','not_live_user_data':True,'no_ai_calls':True,'reserve_floor':3000,'input':snapshot,'cases':cases,'summary':summary,'derived_design':{'conditional_margin':2000,'without_income_margin':0,'minimum_current_with_income':16000,'minimum_current_without_income':26000,'independent_product_threshold_implemented':False},'source_sha256':hashlib.sha256((SOURCE/'src/bank_v1/finance.py').read_bytes()).hexdigest()}
target=ROOT/'submission/algorithm-examples.json'
target.write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(summary,ensure_ascii=False,indent=2))
