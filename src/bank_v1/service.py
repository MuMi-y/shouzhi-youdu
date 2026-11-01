"""Stable facade: untrusted model tools and trusted UI actions stay separate."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from . import events, finance, operations, schemas, matching, security, mandate_match
from .common import DomainError, business_date, canonical, evidence
from .storage import connection, emit, initialize
from . import plan_projection


class Service:
    def __init__(self, db_path, *, clock, security_policy=None):
        self.db_path = Path(db_path)
        self.clock = clock
        self.security_policy = security_policy
        initialize(self.db_path,self._now())
        security.initialize_security(self.db_path, self._now(), security_policy)

    def _verifier(self):
        return self.security_policy.verifier if self.security_policy else None

    def _secure_call(self, callback, name, args):
        try:
            return callback(name, args)
        except DomainError as exc:
            # Independent transaction preserves denied-attempt audit after the
            # bank transaction rolls back. Never persist untrusted arguments.
            code = ('VERIFICATION_FAILED' if exc.code == 'STRONG_VERIFICATION_REQUIRED'
                    and type(args) is dict and args.get('challenge_answer') else exc.code)
            security.record_failure(self.db_path, code, name, self._now())
            raise

    def _now(self):
        value = self.clock()
        if not isinstance(value,datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock must return an aware datetime")
        return value.astimezone(timezone.utc)

    def tool_specs(self):
        return schemas.tool_specs()

    def call_tool(self, name, args):
        return self._secure_call(self._call_tool, name, args)

    def _call_tool(self, name, args):
        schemas.validate(name,args,self.tool_specs())
        writes = {"prepare_transfer":"transfer","prepare_mandate_query":"mandate_query",
                  "prepare_mandate_cancel":"mandate_cancel","propose_arrangement":"arrangement"}
        with connection(self.db_path,name in writes) as db:
            now = self._now()
            if name in writes:
                data = {"operation":operations.prepare(db,writes[name],args,now)}
            elif name == "accounts":
                data = finance.accounts(db)
            elif name == "transactions":
                data = {"transactions":finance.transactions(db,args.get("start_date"),args.get("end_date"),args.get("limit",100))}
            elif name == "spending_summary":
                data = finance.spending(db,args["start_date"],args["end_date"])
            elif name == "arrangements":
                data = {"arrangements":finance.arrangements(db)}
            elif name == "forecast":
                options = dict(args)
                coverage = options.pop('plan_scope', 'window')
                project = plan_projection.forecast if coverage == 'all' else plan_projection.exact_forecast
                data = project(finance.forecast_snapshot(db, now), **options)
            elif name == "match_mandate_from_query":
                data = {"match":mandate_match.match_from_query(db,args,now)}
            elif name == "operation":
                data = {"operation":operations.get_operation(db,args["operation_id"],now)}
            else:
                data = events.context(db,args["event_id"],now)
            return evidence(data,now)

    def user_action(self,name,args):
        return self._secure_call(self._user_action, name, args)

    def _user_action(self,name,args):
        schemas.validate(name,args,schemas.action_specs())
        with connection(self.db_path,True) as db:
            now = self._now()
            if name == 'security_status': return security.status(db,self._verifier())
            if name == 'security_unlock': return security.unlock(db,args,now,self._verifier())
            if name == 'verify_operation':
                op = operations.pending(db,args['operation_id'],now)
                return security.verify_operation(db,op,args,now,self._verifier())
            if name in {'inject_transaction','prepare_match','reject_match'}:
                security.assert_unlocked(db)
            if name == "confirm": return operations.confirm(db,args,now)
            if name == "reject": return operations.reject(db,args["operation_id"],now)
            if name == "challenge": return operations.challenge(db,args["operation_id"],now)
            if name == "inject_transaction": return events.inject(db,args,now)
            if name == "retry_event": return events.retry(db,args["event_id"],now)
            if name == "prepare_match": return matching.prepare(db,args,now)
            if name == "reject_match": return matching.reject_candidate(db,args,now)
            day = args.get("date",business_date(now))
            if day != business_date(now):
                raise DomainError("INVALID_ARGUMENTS","到期检查只接受当前业务日期；演示未来请注入受控时钟。")
            event_key = "due:" + day if not args.get("idempotency_key") else "manual-check:" + day + ":" + args["idempotency_key"]
            eid = emit(db,event_key,"due_check",{"date":day},now)
            return {"event_id":eid,"date":day}

    def state(self):
        with connection(self.db_path) as db:
            now = self._now()
            account_data = finance.accounts(db)
            projection = finance.forecast(db,now,business_date(now+timedelta(days=30)))
            event_rows = [events.event_dict(r) for r in db.execute("SELECT * FROM events ORDER BY rowid DESC LIMIT 100")]
            current_facts = events.facts(projection)
            return {"scenario":json.loads(db.execute("SELECT value FROM metadata WHERE key='scenario'").fetchone()[0]),
                    "accounts":account_data["accounts"],"totals":[{"currency":"CNY","balance_minor":account_data["total_balance_minor"]}],
                    "arrangements":finance.arrangements(db),
                    "operations":[operations.get_operation(db,r[0],now) for r in db.execute("SELECT operation_id FROM operations WHERE subject_id='U1' ORDER BY rowid DESC")],
                    "mandates":[dict(r) for r in db.execute("SELECT * FROM mandates WHERE subject_id='U1'")],
                    "events":event_rows,
                    "event_facts":{row["event_id"]:current_facts for row in event_rows},
                    "match_candidates":projection["unconfirmed_matches"],"arrangement_matches":matching.matches(db),
                    "notifications":[dict(r) for r in db.execute("SELECT * FROM notifications ORDER BY rowid DESC LIMIT 100")],
                    "transactions":finance.transactions(db,limit=500),"business_date":business_date(now),
                    "security":security.status(db,self._verifier()),
                    "forecast":evidence(projection,now)}

    def pending_events(self,limit=20):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise DomainError("INVALID_ARGUMENTS","事件读取数量须为1至100。")
        with connection(self.db_path) as db:
            return [events.event_dict(r) for r in db.execute("SELECT * FROM events WHERE state IN ('PENDING','FAILED') AND attempts<3 ORDER BY rowid LIMIT ?",(limit,))]

    def finish_event(self,event_id,analysis_text,*,success=True,error=None,expected_evidence_refs=None):
        if type(event_id) is not str or type(analysis_text) is not str or len(analysis_text)>100000 or type(success) is not bool or (error is not None and type(error) is not str):
            raise DomainError("INVALID_ARGUMENTS","分析结果字段类型或长度无效。")
        if expected_evidence_refs is not None and (type(expected_evidence_refs) is not list or any(type(ref) is not str for ref in expected_evidence_refs)):
            raise DomainError("INVALID_ARGUMENTS","分析证据引用须为字符串列表。")
        with connection(self.db_path,True) as db:
            return events.finish(db,event_id,analysis_text,success,error,self._now(),expected_evidence_refs)
