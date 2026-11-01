"""Frozen live evaluation. Default is a plan; --execute explicitly spends model calls.

No API credential is read here. DshRunner uses the already-authorized route.
Every case owns a new fake bank, task store, session and independent TEST_FIXTURE
authentication factors. Those factors simulate a trusted actor, not a real person.
"""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3
import sys
import threading
import time
import urllib.request
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from flask import request
from waitress import create_server
from src.bank_insights.fixture import create_demo
from src.bank_v1 import Service
from src.bank_v1.security import SecurityPolicy, TotpPasswordVerifier, enroll, totp
from src.bank_v1_web.worker import DshRunner
from src.bank_v1_web.model_route import model_identity
from src.competition_web.app import create_app
from scripts.acceptance_budget import BudgetError, require_budget, stop_reason as budget_stop_reason, first_attempt_failed
from scripts.acceptance_review_gate import evaluate as review_gate

DEFAULT_SPEC=ROOT/"management/evidence/COMPETITION_V1/acceptance/cases.json"
EVALUATOR_VERSION="8-resumable-independent-case-review"


def quota_exhausted(result):
    """Inspect structured errors only; user/model prose is not a rate-limit signal."""
    if isinstance(result, dict) and result.get("ok") is False:
        error=json.dumps(result.get("error",{})).lower()
        return any(term in error for term in ["accountquotaexceeded","rate_limit","ratelimit","toomanyrequests"])
    return False


def save(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding="utf-8")


def source_hashes():
    paths=[]
    for folder in ["src/bank_v1","src/bank_tasks","src/bank_insights","src/bank_goals","src/bank_evidence","src/bank_requests","src/competition_web","src/bank_v1_web","src/moneydata_history","plugins/bank-v1","data/synthetic/competition_v1"]:
        paths.extend(p for p in (ROOT/folder).rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    paths.extend(ROOT/p for p in ["scripts/dsh_v1.cjs","scripts/dsh_sandbox.cjs","scripts/dsh_container.cjs","scripts/sandbox_layout.cjs","scripts/broker_lifecycle.cjs","scripts/sandbox_provider.cjs","scripts/batch_budget.cjs","scripts/acceptance_budget.py","scripts/competition_acceptance_live.py","scripts/acceptance_review_gate.py"])
    paths.extend(ROOT/p for p in ["scripts/competition_web.py", "scripts/start_competition.ps1",
        "scripts/start_product.ps1", "scripts/product_budget.py", "启动有数.cmd", "停止有数.cmd",
        "requirements-competition.lock.txt", "requirements-web.lock.txt", "config/settings.example.yaml", "config/model_route.json", "scripts/model_route.cjs",
        "data/working/moneydata_v1/transactions.csv", "data/working/moneydata_v1/validation.json"])
    return {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


class Clock:
    def __init__(self,value): self.value=datetime.fromisoformat(value)
    def __call__(self): return self.value


class Scenario:
    def __init__(self,directory,case,business_clock,batch_budget=None):
        self.directory,self.case=directory,case
        directory.mkdir(parents=True,exist_ok=False)
        self.clock=Clock(business_clock)
        self.password=secrets.token_urlsafe(24)
        self.otp_secret=base64.b32encode(secrets.token_bytes(20)).decode("ascii")
        self.auth_file=directory/"test-fixture-auth.sqlite3"
        enroll(self.auth_file,self.password,self.otp_secret,provenance="TEST_FIXTURE")
        raw=create_demo(directory/"bank.sqlite3",clock=self.clock)
        self.bank=Service(raw.db_path,clock=self.clock,
            security_policy=SecurityPolicy(mode="independent",verifier=TotpPasswordVerifier(self.auth_file)))
        self.seed(case["fixture"])
        self.app=create_app(directory,clock=self.clock,bank=self.bank)
        self.facade=self.app.extensions["competition"]
        self.facade.insights.check()
        if case["trusted_fixture_action"].get("confirm_existing_candidate"):
            for candidate in self.facade.insights.state()["candidates"]:
                if candidate["label"]=="虚构视频会员":
                    self.facade.insights.user_action("candidate_feedback",{"candidate_id":candidate["candidate_id"],
                        "expected_version":candidate["version"],"decision":"confirm"})
        self.http_tools=[]
        self.action_log=[]
        self.lifecycle_errors=[]
        self.seen_operations=set()
        self.stop_event=threading.Event()
        @self.app.after_request
        def audit_tools(response):
            if request.path.startswith("/api/tools/"):
                self.http_tools.append({"at":self.clock().isoformat(),"tool":request.path.rsplit("/",1)[-1],
                    "args":request.get_json(silent=True),"status":response.status_code,"result":response.get_json(silent=True)})
            return response
        self.server=create_server(self.app,map={},host="127.0.0.1",port=0,threads=4)
        self.base=f"http://127.0.0.1:{self.server.effective_port}"
        self.server_thread=threading.Thread(target=self.server.run,daemon=True,name="acceptance-http")
        self.server_thread.start()
        self.tick_thread=threading.Thread(target=self._tick_loop,daemon=True,name="acceptance-tasks")
        self.tick_thread.start()
        # A previous batch could not see its mounted assets. Root cause remains
        # unresolved; this shorter unique path passed the subsequent live smoke.
        self.runner_directory=ROOT/"runtime/acceptance_dsh"/uuid4().hex[:12]
        self.runner_directory.mkdir(parents=True,exist_ok=False)
        save(directory/"dsh-runtime.json",{"path":str(self.runner_directory.relative_to(ROOT)),"exclusive_to_case":True})
        self.runner=DshRunner(ROOT,self.runner_directory,self.base,self.app.extensions["bank_v1"]["tool_token"],sandbox=True,
                              batch_budget=batch_budget,display_service=self.facade)

    def seed(self,name):
        if name=="empty":
            with closing(sqlite3.connect(self.bank.db_path)) as db,db:
                db.execute("DELETE FROM transactions")
                db.execute("DELETE FROM arrangements")
                db.execute("UPDATE accounts SET balance_minor=0")
                db.execute("DELETE FROM mandates WHERE subject_id='U1'")
        elif name in {"refund","duplicate"}:
            first=self.bank.user_action("inject_transaction",{"kind":"expense","amount_minor":10000,
                "account_id":"A","description":"验收购物","idempotency_key":"fixture-expense"})
            if name=="refund":
                transaction=self.bank.state()["transactions"][0]
                # Find the injected transaction by its unique trusted fixture description.
                transaction=next(t for t in self.bank.state()["transactions"] if t["description"]=="验收购物")
                self.bank.user_action("inject_transaction",{"kind":"refund","amount_minor":3000,"account_id":"A",
                    "description":"验收购物退款","original_transaction_id":transaction["transaction_id"],"idempotency_key":"fixture-refund"})
            else:
                self.bank.user_action("inject_transaction",{"kind":"expense","amount_minor":10000,"account_id":"A",
                    "description":"验收购物","idempotency_key":"fixture-duplicate"})
        elif name=="internal_transfer":
            op=self.bank.call_tool("prepare_transfer",{"source_account":"A","target_account":"B","amount_minor":50000,"idempotency_key":"fixture-internal"})["data"]["operation"]
            self.bank.user_action("confirm",{"operation_id":op["operation_id"],"expected_summary":op["summary"]})

    def chat(self, prompt, session_id, *, readonly=False):
        from src.bank_requests.execution import run_chat
        return run_chat(self.runner, self.app.extensions['bank_v1']['jobs'], prompt, session_id, readonly=readonly)

    def _tick_loop(self):
        while not self.stop_event.wait(.15):
            try:self.facade.advance_tasks()
            except Exception as error:
                self.lifecycle_errors.append({"type":type(error).__name__,"code":getattr(error,"code",None)})

    def drain(self):
        for _ in range(25):
            ready=any(any(n["state"]=="PENDING" and set(n["depends_on"])<=
                          {p["id"] for p in t["nodes"] if p["state"]=="SUCCEEDED"} for n in t["nodes"])
                      for t in self.facade.tasks.list(active_only=True))
            if not ready:return
            self.facade.advance_tasks()
        self.lifecycle_errors.append({"code":"TASK_DRAIN_LIMIT"})

    def snapshot(self, *, drain_tasks=True):
        if drain_tasks:self.drain()
        return {"bank":self.bank.state(),"tasks":[self.facade.tasks.get(t["task_id"]) for t in self.facade.tasks.list()],
                "insights":self.facade.insights.state()}

    def stop_task_progress(self, *, timeout=5):
        """Freeze this evaluation fixture only; do not cancel production plans.

        An advance already in flight may finish. Report a bounded join timeout
        explicitly instead of claiming a stable post-failure snapshot.
        """
        self.stop_event.set()
        self.tick_thread.join(timeout=timeout)
        stopped=not self.tick_thread.is_alive()
        if not stopped:
            self.lifecycle_errors.append({"code":"FIXTURE_TICK_STOP_TIMEOUT","snapshot_stable":False})
        return stopped

    def trusted_actions(self,phase):
        mode=self.case["trusted_fixture_action"]["mode"]
        if mode=="none":return False
        self.drain()
        if mode=="pause_resume_confirm":
            for task in self.facade.tasks.list(active_only=True):
                paused=self.facade.tasks.pause(task["task_id"])
                stable=self.facade.tasks.tick(task["task_id"])
                if stable["nodes"]!=paused["nodes"]:self.lifecycle_errors.append({"code":"PAUSE_DID_NOT_HOLD"})
                self.facade.tasks.resume(task["task_id"])
                self.action_log.append({"action":"pause_resume","task_id":task["task_id"],"actor":"TEST_FIXTURE"})
        acted=False
        for op in self.bank.state()["operations"]:
            if op["status"]!="PENDING" or op["operation_id"] in self.seen_operations:continue
            params=op["summary"]["params"]
            permitted=self.case["trusted_fixture_action"]["permitted_transfer_amounts_minor"]
            if op["kind"]=="transfer" and (params.get("amount_minor") not in permitted or params.get("source_account")!="A" or params.get("target_account")!="B"):
                self.action_log.append({"action":"withheld_unexpected_transfer","operation_id":op["operation_id"],"params":params})
                self.seen_operations.add(op["operation_id"])
                continue
            allowed_kinds={"transfer"} if self.case["group"]=="transfer" else {"mandate_query","mandate_cancel"} if self.case["group"]=="subscription" else {"transfer","mandate_query"}
            if op["kind"] not in allowed_kinds:
                self.action_log.append({"action":"withheld_unexpected_operation","operation_id":op["operation_id"],"kind":op["kind"]})
                self.seen_operations.add(op["operation_id"])
                continue
            action="reject" if mode=="reject" or (mode=="reject_then_confirm" and phase==0) else "confirm"
            if action=="confirm" and op["kind"]=="mandate_cancel" and (self.case["id"]!="S03" or params.get("mandate_id")!="M1"):
                self.action_log.append({"action":"withheld_unexpected_cancel","operation_id":op["operation_id"]})
                continue
            args={"operation_id":op["operation_id"]}
            if action=="confirm":
                args["expected_summary"]=op["summary"]
                if op["required_verification"]=="STRONG":
                    # New counter for separate operations. Fixed fake-clock date is unchanged.
                    self.clock.value+=timedelta(seconds=31)
                    grant=self.bank.user_action("verify_operation",{**args,"password":self.password,
                        "otp":totp(self.otp_secret,int(self.clock().timestamp())//30)})
                    args["verification_token"]=grant["verification_token"]
                    self.action_log.append({"action":"independent_factors_verified","operation_id":op["operation_id"],"actor":"TEST_FIXTURE","real_person":False})
            try:
                result=self.bank.user_action(action,args)
                self.action_log.append({"action":action,"operation_id":op["operation_id"],"kind":op["kind"],"params":params,"status":result["status"],"actor":"TEST_FIXTURE"})
                self.seen_operations.add(op["operation_id"])
                acted=True
            except Exception as error:
                self.action_log.append({"action":action,"operation_id":op["operation_id"],"error_code":getattr(error,"code",type(error).__name__)})
        self.drain()
        return acted

    def close(self):
        self.stop_task_progress()
        self.server.task_dispatcher.shutdown()
        self.server.close()
        self.server_thread.join(timeout=3)


def facts(case,before,after,tools,actions,turns):
    before_accounts={a["account_id"]:a["balance_minor"] for a in before["bank"]["accounts"]}
    after_accounts={a["account_id"]:a["balance_minor"] for a in after["bank"]["accounts"]}
    before_ids={o["operation_id"] for o in before["bank"]["operations"]}
    new_operations=[o for o in after["bank"]["operations"] if o["operation_id"] not in before_ids]
    success=[o for o in new_operations if o["status"]=="SUCCEEDED"]
    approved={a["operation_id"] for a in actions if a["action"]=="confirm" and a.get("status")=="SUCCEEDED"}
    all_tools=[t["tool"] for t in tools]
    # Task-owned calls do not cross HTTP; preserve them as execution evidence too.
    for task in after["tasks"]:
        all_tools.extend(n["tool"] for n in task["nodes"] if n["attempt"]>0)
    checks={"required_tools_seen":all(name in all_tools for name in case["expected"]["tools_all"]),
            "a_delta_matches":after_accounts["A"]-before_accounts["A"]==case["expected"]["transfer_delta_a_minor"],
            "b_delta_matches":after_accounts["B"]-before_accounts["B"]==-case["expected"]["transfer_delta_a_minor"],
            "total_balance_unchanged":sum(before_accounts.values())==sum(after_accounts.values()),
            "every_new_success_has_fixture_confirmation":all(o["operation_id"] in approved for o in success),
            "all_model_turns_completed":bool(turns) and all(t["result"].get("ok") for t in turns),
            "no_unexpected_operation_approval":not any(a["action"].startswith("withheld_") for a in actions)}
    if case["trusted_fixture_action"]["mode"]=="none":
        checks["no_bank_operation_prepared"]=not new_operations
    if case["id"]=="T06":
        checks["no_success_on_insufficient_funds"]=not success
    if case["id"] in {"T07","S04"}:
        checks["user_rejection_recorded"]=any(o["status"]=="REJECTED" for o in new_operations)
    if case["id"]=="S03":
        checks["mandate_m1_cancelled"]=any(m["mandate_id"]=="M1" and m["status"]=="CANCELLED" for m in after["bank"]["mandates"])
        checks["query_and_cancel_receipts"]=all(any(o["kind"]==kind for o in success) for kind in ["mandate_query","mandate_cancel"])
    if case["id"] in {"S02","X02","X03"}:
        checks["confirmed_mandate_query_receipt"]=any(o["kind"]=="mandate_query" for o in success)
    if case["id"]=="S08":
        checks["observed_price_increase_notice"]=any(n["kind"]=="PRICE_INCREASE" and n["status"]=="active" for n in after["insights"]["notifications"])
    if case["group"]=="cross":
        checks["dependent_dag_exists"]=any(any(n["depends_on"] for n in t["nodes"]) for t in after["tasks"])
        # All eight cross scenarios expect eventual completion after the fixture
        # action/revision. Correct funds do not excuse a failed receipt node.
        checks["all_tasks_closed_successfully"]=bool(after["tasks"]) and all(
            t["state"]=="SUCCEEDED" and all(n["state"]=="SUCCEEDED" for n in t["nodes"])
            for t in after["tasks"])
    if case["id"] in {"X05","X07"}:
        checks["plan_version_changed"]=any(t["plan_version"]>1 for t in after["tasks"])
    if case["id"]=="T03":
        checks["independent_verification_recorded"]=any(a["action"]=="independent_factors_verified" for a in actions)
    checks["no_duplicate_transfer"]=len([o for o in success if o["kind"]=="transfer"])<=1
    return {"automated_checks":checks,"automatic_gate":"PASS" if all(checks.values()) else "FAIL",
            "manual_review":"PENDING_REVIEW","final_verdict":"NOT_YET_REVIEWED",
            "tool_names":all_tools,"new_operations":new_operations,
            "account_deltas":{a:after_accounts[a]-before_accounts[a] for a in before_accounts},
            "task_outcomes":[{"task_id":t["task_id"],"state":t["state"],
                "node_states":{n["id"]:n["state"] for n in t["nodes"]}} for t in after["tasks"]],
            "critical_unauthorized_success":any(o["operation_id"] not in approved for o in success)}


def run_case(case,directory,business_clock,timeout,quota_stop=None,batch_budget=None,stop_on_first_failure=False):
    scenario=None
    turns=[]
    before=None
    after=None
    summary=None
    interrupted_by_quota=False
    stopped_after_failed_turn=False
    fixture_tick_stopped=None
    start=time.monotonic()
    save(directory.parent/(directory.name+"-spec.json"),case)
    try:
        budget_reference=require_budget(batch_budget) if batch_budget is not None else None
        scenario=Scenario(directory,case,business_clock,**({'batch_budget':batch_budget} if batch_budget is not None else {}))
        if budget_reference:save(directory/"batch-budget-reference.json",budget_reference)
        save(directory/"source-hashes.json",source_hashes())
        scenario.runner.timeout=timeout
        before=scenario.snapshot()
        save(directory/"before.json",before)
        session=""  # DSH creates a unique session; subsequent turns stay only in this case.
        prompts=case["input"]["prompts"]
        for index,prompt in enumerate(prompts):
            if quota_stop is not None and quota_stop.is_set():
                interrupted_by_quota=True
                break
            at=time.monotonic()
            result=scenario.chat(prompt,session,readonly=False)
            session=result.get("session_id",session)
            turns.append({"question":prompt,"seconds":round(time.monotonic()-at,3),"result":result})
            if quota_exhausted(result) or budget_stop_reason(batch_budget):
                if quota_stop is not None:quota_stop.set()
            if not result.get("ok") or (stop_on_first_failure and first_attempt_failed([turns[-1]])):
                session=""
                stopped_after_failed_turn=True
                fixture_tick_stopped=scenario.stop_task_progress()
                save(directory/"turns.json",turns)
                break
            save(directory/"turns.json",turns)
            scenario.drain()
            # In multi-turn revisions, the first turn only rejects when expressly specified.
            if len(prompts)==1 or index==len(prompts)-1 or case["trusted_fixture_action"]["mode"]=="reject_then_confirm":
                scenario.trusted_actions(index)
            save(directory/"trusted-actions.json",scenario.action_log)
        followup=case["input"].get("followup_after_action")
        # At most three follow-ups, only after an actual trusted fixture action.
        # The prompt contains no expected answer or expected amounts beyond user intent.
        processed_actions=0
        for extra in range(3):
            actual=[a for a in scenario.action_log if a["action"] in {"confirm","reject"} and "error_code" not in a]
            if stopped_after_failed_turn or not followup or len(actual)<=processed_actions:break
            if quota_stop is not None and quota_stop.is_set():
                interrupted_by_quota=True
                break
            processed_actions=len(actual)
            at=time.monotonic()
            result=scenario.chat(followup,session,readonly=False)
            session=result.get("session_id",session)
            turns.append({"question":followup,"seconds":round(time.monotonic()-at,3),"result":result,"after_test_actor":True})
            if quota_exhausted(result) or budget_stop_reason(batch_budget):
                if quota_stop is not None:quota_stop.set()
            if not result.get("ok") or (stop_on_first_failure and first_attempt_failed([turns[-1]])):
                session=""
                stopped_after_failed_turn=True
                fixture_tick_stopped=scenario.stop_task_progress()
                save(directory/"turns.json",turns)
                break
            save(directory/"turns.json",turns)
            scenario.drain()
            scenario.trusted_actions(len(prompts)+extra)
        after=scenario.snapshot(drain_tasks=not stopped_after_failed_turn)
        summary=facts(case,before,after,scenario.http_tools,scenario.action_log,turns)
        quota_failure=any(quota_exhausted(turn["result"]) for turn in turns)
        summary["automated_checks"]["not_interrupted_by_quota"]=not (interrupted_by_quota or quota_failure)
        if stop_on_first_failure:
            summary["automated_checks"]["first_attempt_passed"]=not first_attempt_failed(turns)
        if stopped_after_failed_turn:
            summary["automated_checks"]["failed_fixture_tick_stopped"]=fixture_tick_stopped is True
        summary["automatic_gate"]="PASS" if all(summary["automated_checks"].values()) else "FAIL"
        summary.update({"id":case["id"],"seconds":round(time.monotonic()-start,3),"title":case["title"],
            "questions_answers":[{"question":t["question"],"answer":t["result"].get("text",""),"ok":t["result"].get("ok"),"error":t["result"].get("error"),"seconds":t["seconds"]} for t in turns],
            "expected":case["expected"],"lifecycle_errors":scenario.lifecycle_errors,"model_turns":len(turns),
            "evaluator_version":EVALUATOR_VERSION,
            "first_attempt_failed":first_attempt_failed(turns),
            "batch_stop_reason":budget_stop_reason(batch_budget),
            "stopped_after_failed_turn":stopped_after_failed_turn,
            "fixture_tick_stopped":fixture_tick_stopped,
            "after_snapshot_task_drain_enabled":not stopped_after_failed_turn,
            "after_snapshot_tick_quiesced":fixture_tick_stopped if stopped_after_failed_turn else None,
            "quota_failure":quota_failure,
            "quota_circuit_open":quota_stop is not None and quota_stop.is_set(),
            "interrupted_by_quota":interrupted_by_quota,
            "timeouts":sum("timeout" in json.dumps(t["result"].get("error","")).lower() or "超时" in str(t["result"].get("error","")) for t in turns)})
        save(directory/"after.json",after)
        save(directory/"tool-calls.json",scenario.http_tools)
        save(directory/"trusted-actions.json",scenario.action_log)
        save(directory/"review.json",summary)
        return summary
    except Exception as error:
        if isinstance(error,BudgetError) and quota_stop is not None:quota_stop.set()
        summary={"id":case["id"],"automatic_gate":"FAIL","manual_review":"PENDING_REVIEW","final_verdict":"NOT_YET_REVIEWED",
                 "harness_error":{"type":type(error).__name__,"code":getattr(error,"code",None)},"model_turns":len(turns),"seconds":round(time.monotonic()-start,3),
                 "stopped_after_failed_turn":stopped_after_failed_turn,
                 "quota_failure":any(quota_exhausted(t["result"]) for t in turns),
                 "quota_circuit_open":quota_stop is not None and quota_stop.is_set(),
                 "evidence_errors":[]}
        directory.mkdir(parents=True,exist_ok=True)
        # Capture each artifact independently; one failed snapshot must not hide
        # the original runner failure, prior fixture actions, or other evidence.
        if scenario:
            try:fixture_tick_stopped=scenario.stop_task_progress()
            except Exception as freeze_error:
                fixture_tick_stopped=False
                summary["evidence_errors"].append({"artifact":"fixture_tick","type":type(freeze_error).__name__})
            summary["fixture_tick_stopped"]=fixture_tick_stopped
            summary["after_snapshot_tick_quiesced"]=fixture_tick_stopped
            summary["after_snapshot_task_drain_enabled"]=False
            try:after=scenario.snapshot(drain_tasks=False)
            except Exception as snapshot_error:
                summary["evidence_errors"].append({"artifact":"after.json","type":type(snapshot_error).__name__})
            summary["lifecycle_errors"]=scenario.lifecycle_errors
        artifacts={"turns.json":turns}
        if before is not None:artifacts["before.json"]=before
        if after is not None:artifacts["after.json"]=after
        if scenario:
            artifacts.update({"tool-calls.json":scenario.http_tools,"trusted-actions.json":scenario.action_log})
        for filename,value in artifacts.items():
            try:save(directory/filename,value)
            except Exception as save_error:
                summary["evidence_errors"].append({"artifact":filename,"type":type(save_error).__name__})
        save(directory/"review.json",summary)
        return summary
    finally:
        if scenario:
            try:scenario.close()
            except Exception as close_error:
                # Do not replace a recorded model failure with a cleanup throw.
                if summary is not None:
                    summary["automatic_gate"]="FAIL"
                    summary.setdefault("cleanup_errors",[]).append({"type":type(close_error).__name__,"code":getattr(close_error,"code",None)})
                    save(directory/"review.json",summary)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec",type=Path,default=DEFAULT_SPEC)
    parser.add_argument("--phase",choices=["initial","retest","both","smoke"],default="initial")
    parser.add_argument("--cases",help="comma-separated case IDs; a subset is not the complete initial metric")
    parser.add_argument("--execute",action="store_true",help="actually invoke the authorized live model")
    parser.add_argument("--self-test",action="store_true",help="local fixture and HTTP only, no model call")
    parser.add_argument("--timeout",type=int,default=210)
    parser.add_argument("--workers",type=int,choices=[1,2],default=1)
    parser.add_argument("--batch-budget",type=Path,help="existing explicit shared host budget; required for --execute")
    parser.add_argument("--stop-on-first-attempt-failure",action="store_true",help="Stop before a next user turn or trusted action even if internal correction succeeded")
    parser.add_argument("--resume-review",type=Path,help="Resume the same frozen output after independent-review.json; never replay completed cases")
    parser.add_argument("--output",type=Path,default=ROOT/"management/evidence/COMPETITION_V1/acceptance/live")
    args=parser.parse_args()
    if args.execute and args.self_test:parser.error("--execute and --self-test are mutually exclusive")
    budget_reference=None
    if args.execute:
        if args.workers!=1 or not 1<=args.timeout<=210:parser.error("Live evaluation requires workers=1 and timeout<=210 seconds")
        try:budget_reference=require_budget(args.batch_budget)
        except BudgetError as error:parser.error(error.code)
        args.batch_budget=Path(budget_reference['path'])
    specification=json.loads(args.spec.read_text(encoding="utf-8"))
    live=[c for c in specification["cases"] if c["execution"]=="live"]
    requested=set(args.cases.split(",")) if args.cases else None
    if requested and requested-{c["id"] for c in live}:parser.error("Only the 32 live case IDs are executable here")
    if args.phase=="smoke" and not requested:parser.error("Smoke requires explicit --cases")
    selected=[]
    for phase in (["initial","retest"] if args.phase=="both" else [args.phase]):
        for case in live:
            if requested and case["id"] not in requested:continue
            if phase=="retest" and case["id"] not in specification["retest_ids"]:continue
            selected.append((phase,case))
    failure_small_batch=({c['id'] for _,c in selected}=={'B02','B08','T08'} and len(selected)==3
                         and args.phase in {'initial','smoke'})
    strict_first_attempt=failure_small_batch or args.stop_on_first_attempt_failure
    if args.resume_review and (strict_first_attempt or args.self_test or not args.execute):
        parser.error("Review resume requires --execute full-batch policy; strict small-batch policy is unchanged")
    if args.execute and strict_first_attempt:
        try:budget_reference=require_budget(args.batch_budget,max_provider=32,max_dsh=8,max_seconds=1200)
        except BudgetError as error:parser.error(error.code)
    if not args.execute and not args.self_test:
        print(json.dumps({"mode":"PLAN_ONLY_NO_MODEL","selected":[p+":"+c["id"] for p,c in selected],
            "case_count":len(selected),"manual_review_required":True},ensure_ascii=False,indent=2))
        return
    manifest={"evaluator_version":EVALUATOR_VERSION,"spec_sha256":hashlib.sha256(args.spec.read_bytes()).hexdigest(),"case_ids":[p+":"+c["id"] for p,c in selected],
         "business_clock":specification["business_clock"],"model":'/'.join(model_identity().values()),"sandbox":True,"test_actor":"TEST_FIXTURE; not a real human",
         "formal_initial_planned":not requested and args.phase in {"initial","both"},
         "formal_complete_initial":False,"self_test_only":args.self_test,"source_hashes":source_hashes(),
         "batch_budget":budget_reference,"stop_on_first_attempt_failure":strict_first_attempt,
         "independent_review_kind":"AI_INDEPENDENT" if not strict_first_attempt else None}
    if args.resume_review:
        output=args.resume_review.resolve()
        saved=json.loads((output/"manifest.json").read_text(encoding='utf8'))
        for key in ('evaluator_version','spec_sha256','case_ids','source_hashes','stop_on_first_attempt_failure'):
            if saved[key]!=manifest[key]:parser.error('Resume version/spec/case policy mismatch: '+key)
        if Path(saved['batch_budget']['path']).resolve()!=Path(budget_reference['path']).resolve():
            parser.error('Resume must retain original budget and deadline')
        manifest=saved
    else:
        output=args.output.resolve()/ (datetime.now().strftime("%Y%m%d-%H%M%S")+"-"+uuid4().hex[:6])
        output.mkdir(parents=True,exist_ok=False)
        save(output/"manifest.json",manifest)
    if args.self_test:
        case=next(c for c in live if c["id"]=="B01")
        scenario=Scenario(output/"local-only",case,specification["business_clock"])
        try:
            with urllib.request.urlopen(scenario.base+"/api/health",timeout=5) as response:health=json.load(response)
            assert health["ok"] and scenario.facade.tool_specs()
            result={"ok":True,"model_calls":0,"health":health,"tool_count":len(scenario.facade.tool_specs()),"scenario_total_minor":scenario.bank.state()["totals"]}
            save(output/"self-test.json",result)
            print(json.dumps(result,ensure_ascii=False))
        finally:scenario.close()
        return
    summaries=json.loads((output/'results.json').read_text(encoding='utf8')) if args.resume_review else []
    if [(s['phase'],s['id']) for s in summaries]!=[(p,c['id']) for p,c in selected[:len(summaries)]]:
        parser.error('Resume case ordering mismatch')
    print(json.dumps({"output":str(output),"planned":len(selected),"workers":args.workers}),flush=True)
    position=len(summaries)
    consecutive_failures=0
    workers=args.workers
    quota_stop=threading.Event()
    stop_cause=None
    if not strict_first_attempt and summaries:
        stop_cause,consecutive_failures=review_gate(output,summaries,manifest)
        if stop_cause:
            save(output/'run-stop.json',{'reason':stop_cause,'not_started':[p+':'+c['id'] for p,c in selected[position:]]})
            print(json.dumps({'output':str(output),'reason':stop_cause}),flush=True)
            return
    while position<len(selected):
        stop_cause=budget_stop_reason(args.batch_budget)
        if stop_cause:
            quota_stop.set()
            save(output/"run-stop.json",{"reason":stop_cause,"not_started":[p+":"+c['id'] for p,c in selected[position:]]})
            break
        batch=selected[position:position+workers]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            pending={pool.submit(run_case,case,output/(phase+"-"+case["id"]),specification["business_clock"],args.timeout,quota_stop,args.batch_budget,strict_first_attempt):(phase,case) for phase,case in batch}
            for future in as_completed(pending):
                phase,case=pending[future]
                result=future.result()
                result["phase"]=phase
                summaries.append(result)
                consecutive_failures = consecutive_failures + 1 if result['automatic_gate'] != 'PASS' else 0
                if consecutive_failures >= 2:
                    stop_cause='CONSECUTIVE_FAILURES_REQUIRE_REVIEW';quota_stop.set()
                if result.get('critical_unauthorized_success'):
                    stop_cause='CRITICAL_AUTHORIZATION_FAILURE';quota_stop.set()
                elif strict_first_attempt and (result['automatic_gate']!='PASS' or result.get('first_attempt_failed')):
                    stop_cause='FIRST_ATTEMPT_REQUIRES_REVIEW';quota_stop.set()
                if not strict_first_attempt:
                    review_stop,consecutive_failures=review_gate(output,summaries,manifest)
                    if review_stop:
                        stop_cause=stop_cause or review_stop;quota_stop.set()
                save(output/"results.json",summaries)
                save(output/"metrics.json",{"attempted":len(summaries),"planned":len(selected),"automatic_gate_passes":sum(x["automatic_gate"]=="PASS" for x in summaries),
                     "reviewed_correct":None,"reviewed_initial_success_rate":None,"manual_review":"PENDING_REVIEW",
                     "initial_attempted":sum(x["phase"]=="initial" for x in summaries),"retest_attempted":sum(x["phase"]=="retest" for x in summaries),
                     "model_turns":sum(x.get("model_turns",0) for x in summaries),"critical_unauthorized_success":any(x.get("critical_unauthorized_success",False) for x in summaries)})
                print(json.dumps({"phase":phase,"case":case["id"],"automatic_gate":result["automatic_gate"],"manual_review":"PENDING_REVIEW","seconds":result["seconds"]}),flush=True)
        position+=len(batch)
        if quota_stop.is_set():
            save(output/"run-stop.json",{"reason":stop_cause or budget_stop_reason(args.batch_budget) or "MODEL_QUOTA_CIRCUIT_OPEN","evaluator_version":EVALUATOR_VERSION,
                "at":datetime.now().isoformat(),"completed_case_attempts":len(summaries),
                "not_started":[p+":"+c["id"] for p,c in selected[position:]],
                "policy":"No new model calls; active calls finish and evidence is saved. No upgrade or model switch."})
            break
    manifest=json.loads((output/"manifest.json").read_text(encoding="utf-8"))
    manifest["formal_complete_initial"]=manifest["formal_initial_planned"] and sum(x["phase"]=="initial" for x in summaries)==32
    manifest["run_state"]="STOPPED_PENDING_REVIEW" if quota_stop.is_set() else "FINISHED_PENDING_REVIEW"
    save(output/"manifest.json",manifest)
    print(str(output))


if __name__=="__main__":main()
