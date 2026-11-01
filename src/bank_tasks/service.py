"""Small durable executor. Bank authorization is never delegated to this module."""
from contextlib import contextmanager, closing
from datetime import datetime, timezone
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
from uuid import uuid4
from .validation import TaskError, PREPARES, arguments, require, resolve, validate_plan


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def definition(node):
    return {key: node[key] for key in ("id", "tool", "args", "depends_on")}


def fresh(node, task_id, version):
    return {**node, "state": "PENDING", "attempt": 0, "retry_count": 0, "result": None, "error": None,
            "operation_id": None, "execution_key": f"task:{task_id}:v{version}:{node['id']}"}


def is_bank_precondition_rejection(error):
    # Both import styles are supported by existing project entry points. Only the
    # trusted bank's own exception class has the transaction-rollback guarantee.
    # A random callback carrying the same code is not sufficient evidence.
    cls = type(error)
    return (cls.__module__ in {"bank_v1.common", "src.bank_v1.common"} and cls.__name__ == "DomainError"
            and error.code in {"INSUFFICIENT_FUNDS", "RESERVE_SHORTFALL", "INVALID_ACCOUNT", "NOT_FOUND", "FORBIDDEN",
                               "VERSION_CONFLICT", "INVALID_ARGUMENTS", "ALREADY_MATCHED", "ALREADY_CANCELLED",
                               "QUERY_RECEIPT_STALE", "INVALID_QUERY_RECEIPT"})


def safe_tool_error(error):
    """Only fixed domain codes get friendly text; never copy untrusted errors."""
    messages = {
        "MANDATE_NO_MATCH": "查询回执没有符合条件的授权，请澄清目标。",
        "MANDATE_AMBIGUOUS": "查询回执有多个符合条件的授权，请补充商户条件。",
        "QUERY_RECEIPT_STALE": "查询回执已过期或跨业务日，需重新查询并确认。",
        "INVALID_QUERY_RECEIPT": "没有有效的本人成功查询回执，已停止后续办理。",
        "VERSION_CONFLICT": "授权或业务版本已改变，请重新核对并规划。",
    }
    if isinstance(error, TaskError):
        return error.message
    cls = type(error)
    if cls.__module__ in {"bank_v1.common", "src.bank_v1.common"} and cls.__name__ == "DomainError":
        return messages.get(error.code, "工具调用未完成，请核对原操作后安全恢复。")
    return "工具调用未完成，请核对原操作后安全恢复。"


class TaskService:
    def __init__(self, path, *, clock, tool_specs, call_tool, before_call=None, attempt_recorder=None, recover_prepare=None):
        self.path, self.clock, self.call_tool = Path(path), clock, call_tool
        self.before_call = before_call
        self.attempt_recorder = attempt_recorder
        self.recover_prepare = recover_prepare
        self.intent_path = self.path.with_name(self.path.name + ".intents.sqlite3")
        values = tool_specs() if callable(tool_specs) else tool_specs
        self.specs = {spec["name"]: copy.deepcopy(spec) for spec in values}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript("""CREATE TABLE IF NOT EXISTS tasks(
                task_id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL,
                request_hash TEXT NOT NULL, document TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS task_audit(
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                at TEXT NOT NULL, event TEXT NOT NULL, details TEXT NOT NULL);""")
        with closing(sqlite3.connect(self.intent_path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS invocation_intents(execution_key TEXT PRIMARY KEY, at TEXT NOT NULL)")

    def _record_intent(self, execution_key):
        # Independent commit bridges two databases: an interrupted task transaction
        # must not forget that the bank may already hold a pending operation.
        with closing(sqlite3.connect(self.intent_path)) as db, db:
            db.execute("INSERT OR IGNORE INTO invocation_intents VALUES(?,?)", (execution_key, self._now()))

    def _was_invoked(self, execution_key):
        with closing(sqlite3.connect(self.intent_path)) as db:
            return db.execute("SELECT 1 FROM invocation_intents WHERE execution_key=?", (execution_key,)).fetchone() is not None

    def _resolve_no_operation(self, execution_key):
        with closing(sqlite3.connect(self.intent_path)) as db, db:
            db.execute("DELETE FROM invocation_intents WHERE execution_key=?", (execution_key,))

    def _now(self):
        value = self.clock()
        require(isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None,
                "clock须返回有时区datetime。", "INVALID_CLOCK")
        return value.astimezone(timezone.utc).isoformat()

    @contextmanager
    def _db(self, write=False):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _load(self, db, task_id):
        row = db.execute("SELECT document FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        require(row is not None, "任务不存在。", "NOT_FOUND")
        return json.loads(row[0])

    def _save(self, db, task):
        task["updated_at"] = self._now()
        db.execute("UPDATE tasks SET document=? WHERE task_id=?", (encode(task), task["task_id"]))

    def _audit(self, db, task, event, details=None):
        db.execute("INSERT INTO task_audit(task_id,at,event,details) VALUES(?,?,?,?)",
                   (task["task_id"], self._now(), event, encode(details or {})))

    def _replan_guidance(self, task):
        """Snapshot guidance only; replan remains the authoritative validator."""
        blocked = []
        if task["state"] in {"SUCCEEDED", "CANCELLED"}:
            blocked.append("TERMINAL_TASK")
        if task["plan_version"] >= 10:
            blocked.append("PLAN_VERSION_LIMIT")
        operation_checks = []
        unresolved = []
        for node in task["nodes"]:
            if node["state"] == "SUCCEEDED":
                continue
            if node["operation_id"]:
                operation_checks.append({"node_id": node["id"], "operation_id": node["operation_id"],
                                         "snapshot_node_state": node["state"]})
            elif node["tool"] in PREPARES and self._was_invoked(node["execution_key"]):
                unresolved.append(node["id"])
        return {
            "contract_version": 1, "snapshot_plan_version": task["plan_version"],
            "eligibility": "BLOCKED" if blocked else "CONDITIONAL", "blocked_reasons": blocked,
            "max_plan_version": 10, "remaining_replans": max(0, 10 - task["plan_version"]),
            "must_preserve_succeeded_node_ids": [n["id"] for n in task["nodes"] if n["state"] == "SUCCEEDED"],
            "operation_checks_on_changed_nodes": operation_checks,
            "reconciliation_on_changed_node_ids": unresolved,
            "current_auto_advance_paused": task["state"] in {"PAUSED", "NEEDS_HUMAN"},
            "conditions": [
                "CONDITIONAL只表示当前状态和版本未禁止提交，不保证新计划可执行。FAILED仍可按条件重规划。",
                "expected_version须等于当前plan_version，最多到版本10；单节点retry最多2次是不同限制。",
                "必须提交有效完整DAG；保留已成功节点的id/tool/args/depends_on，不删除或重做成功交易。",
                "变更或删除未成功节点时，原银行操作须实时核验为REJECTED或已过期PENDING；未过期待确认须本人拒绝，实际已成功须先恢复核对回执。",
                "存在未知提交intent的变更节点须用原幂等键恢复对账；FAILED节点即使定义不变也视为重规划变更。",
                "新参数和余额仍由银行验证；重规划不代替本人确认。不变的等待节点保留原操作。",
                "PAUSED/NEEDS_HUMAN当前不自动推进，但并非replan禁态；成功重规划会置READY并按节点状态刷新。",
            ],
        }

    def _view(self, db, task):
        task = copy.deepcopy(task)
        task["replan_guidance"] = self._replan_guidance(task)
        task["audit"] = [{**dict(row), "details": json.loads(row["details"])} for row in
                         db.execute("SELECT * FROM task_audit WHERE task_id=? ORDER BY sequence", (task["task_id"],))]
        return task

    def submit(self, prompt, plan, idempotency_key, *, request_contract=None):
        require(type(prompt) is str and 1 <= len(prompt) <= 8000, "任务描述长度无效。")
        require(type(idempotency_key) is str and 1 <= len(idempotency_key) <= 200, "请求编号无效。")
        require(len(encode(plan)) <= 50000, "计划过大。")
        nodes = validate_plan(plan, self.specs)
        identity = [prompt, plan] if request_contract is None else [prompt, plan, request_contract]
        digest = hashlib.sha256(encode(identity).encode()).hexdigest()
        with self._db(True) as db:
            old = db.execute("SELECT request_hash,document FROM tasks WHERE request_key=?", (idempotency_key,)).fetchone()
            if old:
                require(old["request_hash"] == digest, "同一请求编号不能更改计划。", "IDEMPOTENCY_CONFLICT")
                return self._view(db, json.loads(old["document"]))
            identifier = uuid4().hex
            now = self._now()
            task = {"task_id": identifier, "prompt": prompt, "title": plan["title"], "scope": "bank",
                    "subject_id": "U1", "plan_version": 1, "state": "READY", "created_at": now,
                    "updated_at": now, "nodes": [fresh(n, identifier, 1) for n in nodes], "plan_history": []}
            if request_contract is not None:
                task['request_contract'] = copy.deepcopy(request_contract)
                task['node_origins'] = {n['id']:{'plan_version':1,'contract':copy.deepcopy(request_contract)} for n in nodes}
            db.execute("INSERT INTO tasks VALUES(?,?,?,?)", (identifier, idempotency_key, digest, encode(task)))
            self._audit(db, task, "SUBMITTED", {"plan_version": 1})
            return self._view(db, task)

    def get(self, task_id):
        with self._db() as db:
            return self._view(db, self._load(db, task_id))

    def list(self, limit=50, active_only=False):
        require(type(limit) is int and 1 <= limit <= 200, "读取数量须为1至200。")
        with self._db() as db:
            result = []
            for row in db.execute("SELECT document FROM tasks ORDER BY rowid DESC"):
                task = json.loads(row[0])
                if active_only and task["state"] not in {"READY", "WAITING_CONFIRMATION", "WAITING_VERIFICATION"}:
                    continue
                result.append(task)
                if len(result) >= limit:
                    break
            return result

    def _refresh(self, task):
        if task["state"] in {"PAUSED", "CANCELLED", "NEEDS_HUMAN"}:
            return
        states = {n["state"] for n in task["nodes"]}
        if states == {"SUCCEEDED"}:
            task["state"] = "SUCCEEDED"
        elif "FAILED" in states:
            task["state"] = "FAILED"
        elif "WAITING_VERIFICATION" in states:
            task["state"] = "WAITING_VERIFICATION"
        elif "WAITING_CONFIRMATION" in states:
            task["state"] = "WAITING_CONFIRMATION"
        else:
            task["state"] = "READY"

    def _bank_result(self, node, result):
        op = result.get("data", result).get("operation")
        require(type(op) is dict and type(op.get("operation_id")) is str, "银行未返回有效操作编号。", "INVALID_RESULT")
        if node["operation_id"]:
            require(op["operation_id"] == node["operation_id"], "银行操作编号不一致。", "INVALID_RESULT")
        node["operation_id"], node["result"] = op["operation_id"], result
        status = op.get("status")
        if status == "SUCCEEDED":
            require(type(op.get("receipt")) is dict and op["receipt"].get("status") == "SUCCEEDED",
                    "成功操作缺少真实成功回执。", "INVALID_RESULT")
            node["state"] = "SUCCEEDED"
        elif status == "PENDING":
            expiry = op.get("expires_at")
            if expiry and datetime.fromisoformat(expiry) <= datetime.fromisoformat(self._now()):
                raise TaskError("EXPIRED", "银行确认已过期，需重新规划准备新操作。")
            node["state"] = "WAITING_VERIFICATION" if op.get("required_verification") == "STRONG" else "WAITING_CONFIRMATION"
        else:
            raise TaskError(str(status or "INVALID_RESULT"), "银行操作未成功；下游已停止，请核对后重新规划。")

    def tick(self, task_id):
        attempts = []
        try:
            result = self._tick(task_id,attempts)
        except BaseException:
            if self.attempt_recorder:
                for attempt in attempts:
                    self.attempt_recorder.task_end(attempt,committed=False)
            raise
        if self.attempt_recorder:
            for attempt in attempts:
                self.attempt_recorder.task_end(attempt,committed=True)
        return result

    def _recovery_needed(self, db, task, node):
        if not self.attempt_recorder or node['tool'] not in PREPARES or node['state']!='PENDING':
            return False
        if self._was_invoked(node['execution_key']):
            return True
        snapshots = self.attempt_recorder.task_dispatches(task,node)
        if not snapshots:
            return False
        # Only the trusted bank exception branch authors this flag. A callback's
        # arbitrary exception code is never proof of rollback before operation.
        unknown = [s for s in snapshots if not any(e['phase']=='RAISED' and
            e['data'].get('bank_no_operation') is True for e in s['events'])]
        if not unknown:
            return False
        # Compatibility with the pre-flag audit, only for one unambiguous attempt.
        if len(snapshots)==1:
            s=snapshots[0]
            audit=[(r['sequence'],r['event'],json.loads(r['details'])) for r in db.execute(
                'SELECT sequence,event,details FROM task_audit WHERE task_id=? ORDER BY sequence',(task['task_id'],))]
            starts=[r for r in audit if r[1]=='NODE_STARTED' and r[2].get('node_id')==node['id']]
            rejected=[r for r in audit if r[1]=='BANK_REJECTED_BEFORE_OPERATION' and r[2].get('node_id')==node['id']]
            if starts and rejected and rejected[-1][0]>starts[-1][0] and starts[-1][2].get('attempt')==node['attempt']:
                versions=[r[2]['plan_version'] for r in audit if r[0]<starts[-1][0] and r[1] in {'SUBMITTED','REPLANNED'}]
                identity=json.loads(s['run']['identity_json'])
                if (versions and identity.get('plan_version')==versions[-1]
                        and any(e['phase']=='DOMAIN_COMMITTED' for e in s['events'])
                        and any(e['phase']=='RAISED' and e['data'].get('code')==rejected[-1][2].get('code') for e in s['events'])):
                    return False
        return True

    def _tick(self, task_id, attempts):
        with self._db(True) as db:
            task = self._load(db, task_id)
            if task["state"] in {"PAUSED", "CANCELLED", "NEEDS_HUMAN", "SUCCEEDED", "FAILED"}:
                return self._view(db, task)
            succeeded = {n["id"] for n in task["nodes"] if n["state"] == "SUCCEEDED"}
            node = next((n for n in task["nodes"] if n["state"] == "PENDING" and set(n["depends_on"]) <= succeeded), None)
            if node is None:
                node = next((n for n in task["nodes"] if n["state"].startswith("WAITING_")), None)
            if node is None:
                self._refresh(task)
                self._save(db, task)
                return self._view(db, task)
            old_state = node["state"]
            recorder = self.attempt_recorder
            recovering = self._recovery_needed(db,task,node)
            attempt = recorder.task_begin(task,node,reconcile=recovering) if recorder else None
            if attempt:
                attempts.append(attempt)
            dispatched = False
            try:
                if recovering:
                    require(callable(self.recover_prepare),'上次调用结果未确定，请核对原操作后恢复，不重复办理。','PREVIOUS_DISPATCH_UNRESOLVED')
                    origin=recorder.task_origin(task,node)
                    recorder.bind_task_attempt(attempt,origin)
                    values={n['id']:n['result'] for n in task['nodes'] if n['state']=='SUCCEEDED'}
                    args=resolve(node['args'],values);args['idempotency_key']=node['execution_key']
                    arguments(args,self.specs[node['tool']]['parameters'])
                    recorder.validated(attempt,{'execution_key':node['execution_key']})
                    recorder.append(attempt,'DISPATCHED',{'read_only':True});dispatched=True
                    observation=self.recover_prepare(task,node,args)
                    require(observation['outcome']=='FOUND',
                        '原操作核对仍不确定（'+observation['outcome']+'），未重复办理。','PREVIOUS_DISPATCH_UNRESOLVED')
                    result=observation['operation_result']
                    proof={k:v for k,v in observation.items() if k!='operation_result'}
                    recorder.append(attempt,'TRACE_LINK',{'reconciliation':proof})
                    self._audit(db,task,'NODE_RECONCILED',proof)
                    self._bank_result(node,result)
                elif old_state.startswith("WAITING_"):
                    if recorder:
                        # History may be queried after restart; no new write is
                        # authorized by an old origin. Link only proven origins.
                        try:
                            recorder.bind_task_attempt(attempt,recorder.task_origin(task,node))
                        except ValueError:
                            pass
                        recorder.validated(attempt,{'operation_id':node['operation_id']})
                        recorder.append(attempt,'DISPATCHED',{'read_only':True})
                        dispatched = True
                    result = self.call_tool("operation", {"operation_id": node["operation_id"]})
                    self._bank_result(node, result)
                else:
                    origin = recorder.task_origin(task,node) if recorder else task.get('request_contract')
                    if recorder:
                        recorder.bind_task_attempt(attempt,origin)
                    values = {n["id"]: n["result"] for n in task["nodes"] if n["state"] == "SUCCEEDED"}
                    args = resolve(node["args"], values)
                    if node["tool"] in PREPARES:
                        args["idempotency_key"] = node["execution_key"]
                    if origin is not None and origin.get('origin_kind') != 'TRUSTED_ROOT':
                        require(callable(self.before_call), '当前执行器缺少请求条件核验，不能继续此任务。', 'REQUEST_POLICY_UNAVAILABLE')
                        args = self.before_call(node['tool'], args, node['id'], origin)
                    arguments(args, self.specs[node["tool"]]["parameters"])
                    if recorder:
                        recorder.validated(attempt,args)
                    node["state"], node["attempt"] = "RUNNING", node["attempt"] + 1
                    self._audit(db, task, "NODE_STARTED", {"node_id": node["id"], "attempt": node["attempt"]})
                    if node["tool"] in PREPARES:
                        self._record_intent(node["execution_key"])
                    if recorder:
                        recorder.append(attempt,'DISPATCHED',{})
                        dispatched = True
                    result = self.call_tool(node["tool"], args)
                    require(type(result) is dict, "工具未返回对象。", "INVALID_RESULT")
                    require(len(encode(result)) <= 2000000, "工具结果过大。", "INVALID_RESULT")
                    node["result"] = result
                    if node["tool"] in PREPARES:
                        self._bank_result(node, result)
                    else:
                        node["state"] = "SUCCEEDED"
                if recorder:
                    from src.bank_v1_web.handoff_contracts import object_refs
                    recorder.append(attempt,'RETURNED',{'refs':object_refs(result)})
                node["error"] = None
            except Exception as error:
                if recorder:
                    recorder.append(attempt,'RAISED' if dispatched else 'REJECTED_BEFORE_DISPATCH',{'code':getattr(error,'code',str(error) if isinstance(error,ValueError) else type(error).__name__),
                        **({'bank_no_operation':True} if not recovering and is_bank_precondition_rejection(error) else {})})
                if not recovering and node["tool"] in PREPARES and not node["operation_id"] and is_bank_precondition_rejection(error):
                    self._resolve_no_operation(node["execution_key"])
                    self._audit(db, task, "BANK_REJECTED_BEFORE_OPERATION", {"node_id": node["id"], "code": error.code})
                node["state"] = "FAILED"
                node["error"] = {"code": getattr(error, "code", "TOOL_FAILED"),
                                 "message": safe_tool_error(error)}
            if node["state"] != old_state or not old_state.startswith("WAITING_"):
                self._audit(db, task, "NODE_STATE", {"node_id": node["id"], "from": old_state,
                            "to": node["state"], "operation_id": node["operation_id"], "error": node["error"]})
            self._refresh(task)
            self._save(db, task)
            return self._view(db, task)

    def _control(self, task_id, action, reason=None, node_id=None):
        with self._db(True) as db:
            task = self._load(db, task_id)
            require(task["state"] not in {"SUCCEEDED", "CANCELLED"}, "任务已结束。", "INVALID_STATE")
            if action in {"pause", "handoff"}:
                task["state"] = "PAUSED" if action == "pause" else "NEEDS_HUMAN"
            elif action == "resume":
                require(task["state"] in {"PAUSED", "NEEDS_HUMAN"}, "任务没有暂停或接管。", "INVALID_STATE")
                task["state"] = "READY"
                self._refresh(task)
            elif action == "retry":
                node = next((n for n in task["nodes"] if n["id"] == node_id), None)
                require(node is not None and node["state"] == "FAILED", "仅失败节点可以重试。", "INVALID_STATE")
                require(node["retry_count"] < 2, "已达2次恢复上限，请接管或重新规划。", "RETRY_LIMIT")
                require(node["error"]["code"] not in {"REJECTED", "EXPIRED"}, "拒绝或过期操作须重新规划。", "INVALID_STATE")
                node["state"] = "WAITING_CONFIRMATION" if node["operation_id"] else "PENDING"
                node["retry_count"] += 1
                node["error"] = None
                task["state"] = "READY"
                self._refresh(task)
            elif action == "cancel":
                self._assert_operations_closed(task)
                for node in task["nodes"]:
                    if node["state"] != "SUCCEEDED":
                        node["state"] = "CANCELLED"
                task["state"] = "CANCELLED"
            self._audit(db, task, action.upper(), {"reason": reason, "node_id": node_id})
            self._save(db, task)
            return self._view(db, task)

    def _assert_operations_closed(self, task, changed_ids=None):
        for node in task["nodes"]:
            if (changed_ids is None or node["id"] in changed_ids) and node["tool"] in PREPARES and self._was_invoked(node["execution_key"]) and not node["operation_id"]:
                raise TaskError("RECONCILIATION_REQUIRED", "准备请求结果未知，请先以原幂等编号恢复核对，不能新建或取消。")
            if node["operation_id"] and node["state"] != "SUCCEEDED" and (changed_ids is None or node["id"] in changed_ids):
                result = self.call_tool("operation", {"operation_id": node["operation_id"]})
                op = result.get("data", result).get("operation", {})
                require(op.get("operation_id") == node["operation_id"], "银行操作核对失败。", "INVALID_RESULT")
                require(op.get("status") == "REJECTED" or (op.get("status") == "PENDING" and
                        op.get("expires_at") and datetime.fromisoformat(op["expires_at"]) <= datetime.fromisoformat(self._now())),
                        "须先拒绝尚可执行的银行操作；若已成功，请恢复任务核对回执。", "PENDING_OPERATION")

    def pause(self, task_id):
        return self._control(task_id, "pause")

    def resume(self, task_id):
        return self._control(task_id, "resume")

    def cancel(self, task_id):
        return self._control(task_id, "cancel")

    def retry(self, task_id, node_id):
        return self._control(task_id, "retry", node_id=node_id)

    def handoff(self, task_id, reason):
        require(type(reason) is str and 1 <= len(reason) <= 1000, "接管原因长度无效。")
        return self._control(task_id, "handoff", reason)

    def replan(self, task_id, expected_version, nodes, reason, *, request_contract=None):
        require(type(reason) is str and 1 <= len(reason) <= 1000, "重规划原因长度无效。")
        with self._db(True) as db:
            task = self._load(db, task_id)
            require(type(expected_version) is int and task["plan_version"] == expected_version, "计划版本已变化。", "VERSION_CONFLICT")
            require(task["state"] not in {"SUCCEEDED", "CANCELLED"}, "已结束任务不能重规划。", "INVALID_STATE")
            require(len(encode(nodes)) <= 50000 and expected_version < 10, "计划过大或版本超过10。")
            new = validate_plan({"title": task["title"], "nodes": nodes}, self.specs)
            by_id = {n["id"]: n for n in new}
            changed = {n["id"] for n in task["nodes"] if by_id.get(n["id"]) != definition(n) or n["state"] == "FAILED"}
            for old in task["nodes"]:
                require(old["state"] != "SUCCEEDED" or old["id"] not in changed,
                        "已成功节点不可更改或删除；补偿操作须另行明确确认。", "IMMUTABLE_SUCCESS")
            self._assert_operations_closed(task, changed)
            task["plan_history"].append({"version": task["plan_version"], "nodes": copy.deepcopy(task["nodes"]),
                                         "replaced_at": self._now(), "reason": reason,
                                         "request_contract": copy.deepcopy(task.get('request_contract'))})
            if request_contract is not None:
                task['request_contract'] = copy.deepcopy(request_contract)
            lookup = {n["id"]: n for n in task["nodes"]}
            task["plan_version"] += 1
            task["nodes"] = [lookup[n["id"]] if n["id"] in lookup and n["id"] not in changed and
                              lookup[n["id"]]["state"] != "FAILED" else fresh(n, task_id, task["plan_version"]) for n in new]
            origins = task.get('node_origins',{})
            task['node_origins'] = {n['id']: origins[n['id']] if n['id'] in origins and n['id'] not in changed else
                {'plan_version':task['plan_version'],'contract':copy.deepcopy(request_contract)} for n in new}
            task["state"] = "READY"
            self._refresh(task)
            self._audit(db, task, "REPLANNED", {"plan_version": task["plan_version"], "reason": reason})
            self._save(db, task)
            return self._view(db, task)
