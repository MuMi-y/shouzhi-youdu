"""Prepare, exactly confirm, and receipt bank operations in one transaction."""
from datetime import datetime, timedelta
import json
import secrets
from .common import DomainError, business_date, canonical, identifier, row_account
from .finance import arrangements
from .storage import audit, emit
from . import matching, mandate_match
from . import security
from . import basis_guard
from .balance_conditions import assess_balance


def rejected_at(db, operation_id, status):
    """Return the first recorded rejection event, never an inferred timestamp."""
    if status != "REJECTED":
        return None
    row = db.execute(
        "SELECT created_at FROM audit WHERE action='REJECTED' AND reference_id=? "
        "ORDER BY rowid ASC LIMIT 1", (operation_id,),
    ).fetchone()
    return row["created_at"] if row else None


def get_operation(db, operation_id, now):
    row = db.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
    if not row:
        raise DomainError("NOT_FOUND", "操作不存在；不能将未知结果报告为成功。")
    if row["subject_id"] != "U1":
        raise DomainError("FORBIDDEN", "无权访问此操作。")
    result = {k:row[k] for k in ("operation_id","kind","status","created_at","expires_at")}
    result.update(summary=json.loads(row["summary_json"]), receipt=json.loads(row["receipt_json"]) if row["receipt_json"] else None)
    result["rejected_at"] = rejected_at(db, operation_id, row["status"])
    result["required_verification"] = "STRONG" if row["kind"] == "transfer" and daily_total(db, now)+result["summary"]["params"]["amount_minor"] > 100000 else "YELLOW"
    if result["receipt"]:
        result["required_verification"] = result["receipt"]["verification"]
    return result


def daily_total(db, now):
    rows = db.execute("SELECT receipt_json FROM operations WHERE kind='transfer' AND status='SUCCEEDED' AND subject_id='U1'")
    return sum(r["amount_minor"] for r in (json.loads(row[0]) for row in rows) if r["business_date"] == business_date(now))


def validate_params(db, kind, params):
    if kind == "transfer":
        source = row_account(db, params["source_account"])
        row_account(db, params["target_account"])
        if params["source_account"] == params["target_account"]:
            raise DomainError("INVALID_ACCOUNT", "转出转入账户必须不同。")
        condition = assess_balance(source["balance_minor"], params["amount_minor"], reserve_minor=params.get('reserve_minor', 0))
        if condition.status == 'INSUFFICIENT_FUNDS':
            raise DomainError("INSUFFICIENT_FUNDS", "转出账户余额不足。")
        if condition.status == 'RESERVE_SHORTFALL':
            raise DomainError('RESERVE_SHORTFALL', '转账后无法满足本次要求保留的账户余额，请重新核对。')
    elif kind == "arrangement":
        aid = params.get("arrangement_id")
        existing = next((a for a in arrangements(db) if a["arrangement_id"] == aid), None)
        if aid and not existing:
            raise DomainError("NOT_FOUND", "待修改安排不存在。")
        if existing and params.get("expected_version") != existing["version"]:
            raise DomainError("VERSION_CONFLICT", "安排版本已改变，请重新读取并提出草案。")
        if not aid and "expected_version" in params:
            raise DomainError("INVALID_ARGUMENTS", "新增安排不接受既有版本。")
        if aid and db.execute("SELECT 1 FROM arrangement_matches WHERE arrangement_id=?",(aid,)).fetchone() and params.get("status","active") != "completed":
            raise DomainError("ALREADY_MATCHED", "已关联流水的安排不能重新激活；新义务请新增安排。")
    elif kind == "arrangement_match":
        matching.validate_confirmation(db, params)
    elif kind == "mandate_cancel":
        mandate = db.execute("SELECT * FROM mandates WHERE mandate_id=?", (params["mandate_id"],)).fetchone()
        if not mandate:
            raise DomainError("NOT_FOUND", "代扣授权不存在。")
        if mandate["subject_id"] != "U1":
            raise DomainError("FORBIDDEN", "无权访问该代扣授权。")
        if "expected_version" in params and params["expected_version"] != mandate["version"]:
            raise DomainError("VERSION_CONFLICT", "授权版本已变化，需重新查询与匹配。")
        if mandate["status"] != "ACTIVE":
            raise DomainError("ALREADY_CANCELLED", "该代扣授权已经撤销。")


def prepare(db, kind, args, now):
    security.assert_unlocked(db)
    params = {k:v for k,v in args.items() if k != "idempotency_key"}
    old = db.execute("SELECT * FROM operations WHERE idempotency_key=?", (args["idempotency_key"],)).fetchone()
    if old:
        if old["kind"] != kind or old["params_json"] != canonical(params):
            raise DomainError("IDEMPOTENCY_CONFLICT", "相同请求编号不能更换参数。")
        return get_operation(db, old["operation_id"], now)
    validate_params(db, kind, params)
    oid = identifier("op")
    expiry = (now+timedelta(minutes=5)).isoformat()
    summary = {"operation_id":oid,"kind":kind,"params":params,"expires_at":expiry}
    if kind == "mandate_cancel":
        summary["mandate_snapshot"] = mandate_match.cancel_snapshot(db, params, now)
    db.execute("INSERT INTO operations VALUES(?,?, 'U1',?,?,?,'PENDING',?,?,NULL)",
               (oid,kind,args["idempotency_key"],canonical(params),canonical(summary),now.isoformat(),expiry))
    audit(db,"PREPARED",oid,now,summary)
    return get_operation(db,oid,now)


def pending(db, operation_id, now):
    op = get_operation(db, operation_id, now)
    if op["status"] != "PENDING":
        raise DomainError(op["status"], "操作已完成或被拒绝。")
    if now >= datetime.fromisoformat(op["expires_at"]):
        raise DomainError("EXPIRED", "确认已过期，请重新发起。")
    return op


def challenge(db, operation_id, now):
    security.assert_unlocked(db)
    if security.status(db)['mode'] != 'simulation':
        raise DomainError('SIMULATION_DISABLED', '当前为独立验证模式，不提供同页模拟验证码。')
    op = pending(db,operation_id,now)
    if op["kind"] != "transfer":
        raise DomainError("INVALID_ARGUMENTS", "只有转账使用模拟强验证。")
    answer = str(secrets.randbelow(900000)+100000)
    db.execute("INSERT OR REPLACE INTO challenges VALUES(?,?,?,0)", (operation_id,answer,op["expires_at"]))
    return {"challenge_id":operation_id,"challenge_answer":answer,"expires_at":op["expires_at"],"simulation_only":True}


def reject(db, operation_id, now):
    op = get_operation(db,operation_id,now)
    if op["status"] == "SUCCEEDED":
        raise DomainError("ALREADY_SUCCEEDED", "操作已成功，不能再拒绝。")
    if op["status"] != "REJECTED":
        db.execute("UPDATE operations SET status='REJECTED' WHERE operation_id=?",(operation_id,))
        audit(db,"REJECTED",operation_id,now,{})
    return get_operation(db,operation_id,now)


def post_transfer(db, op, now):
    params, oid = op["summary"]["params"], op["operation_id"]
    ids = []
    for account, kind, signed in [(params["source_account"],"TRANSFER_OUT",-params["amount_minor"]),
                                  (params["target_account"],"TRANSFER_IN",params["amount_minor"])]:
        tid = identifier("tx")
        db.execute("UPDATE accounts SET balance_minor=balance_minor+?,version=version+1 WHERE account_id=?",(signed,account))
        db.execute("INSERT INTO transactions VALUES(?,?,?,?,?,?,?,?,?,?)",(tid,account,kind,signed,"本人账户转账",business_date(now),now.isoformat(),oid,None,"SIMULATED_BANK"))
        ids.append(tid)
    return {"amount_minor":params["amount_minor"],"source_account":params["source_account"],"target_account":params["target_account"],"entry_ids":ids}


def apply_arrangement(db, params, now):
    aid = params.get("arrangement_id") or identifier("arrangement")
    version = params.get("expected_version",0)+1
    db.execute("INSERT INTO arrangements(arrangement_id,version,title,kind,amount_minor,due_date,created_at,source,status) VALUES(?,?,?,?,?,?,?,?,?)",(aid,version,params["title"],params["kind"],params["amount_minor"],params["due_date"],now.isoformat(),"USER_CONFIRMED",params.get("status","active")))
    return {"arrangement_id":aid,"version":version}


def confirm(db, args, now):
    from . import projection_journal
    op = get_operation(db,args["operation_id"],now)
    if canonical(args["expected_summary"]) != canonical(op["summary"]):
        raise DomainError("SUMMARY_MISMATCH", "确认内容与待执行操作不完全一致。")
    if op["status"] == "SUCCEEDED":
        return op
    security.assert_unlocked(db)
    pending(db,op["operation_id"],now)
    basis_guard.assert_current(db, op["operation_id"], now)
    params = op["summary"]["params"]
    validate_params(db,op["kind"],params)
    if op["kind"] == "mandate_cancel" and "mandate_snapshot" in op["summary"]:
        current = mandate_match.cancel_snapshot(db, params, now)
        if current != op["summary"]["mandate_snapshot"]:
            raise DomainError("VERSION_CONFLICT", "待确认授权内容已变化，需拒绝旧卡并重新查询。")
    if op["required_verification"] == "STRONG":
        if security.status(db)['mode'] == 'independent':
            if not args.get('verification_token'):
                raise DomainError('STRONG_VERIFICATION_REQUIRED', '需要口令和独立设备验证码验证本次精确操作。')
            security.consume_grant(db, op, args['verification_token'], now)
        else:
            row = db.execute("SELECT * FROM challenges WHERE operation_id=?",(op["operation_id"],)).fetchone()
            if not row or row["answer"] != args.get("challenge_answer") or now >= datetime.fromisoformat(row["expires_at"]):
                raise DomainError("STRONG_VERIFICATION_REQUIRED", "需要可信界面的本地模拟强验证。")
    predictive = op['kind'] in {'transfer', 'arrangement', 'arrangement_match'}
    before = projection_journal.capture(db, now) if predictive else None
    if op["kind"] == "transfer":
        result = post_transfer(db,op,now)
    elif op["kind"] == "arrangement":
        result = apply_arrangement(db,params,now)
    elif op["kind"] == "arrangement_match":
        result = matching.apply_match(db,params,op["operation_id"],now)
    elif op["kind"] == "mandate_query":
        result = {"mandates":[dict(r) for r in db.execute("SELECT * FROM mandates WHERE subject_id='U1'")]}
    else:
        db.execute("UPDATE mandates SET status='CANCELLED',version=version+1 WHERE mandate_id=?",(params["mandate_id"],))
        result = {"mandate_id":params["mandate_id"],"mandate_status":"CANCELLED","notice":"仅撤销银行代扣授权，商户合同和应付债务仍需另行处理。"}
    receipt = {"receipt_id":identifier("receipt"),"operation_id":op["operation_id"],"kind":op["kind"],"status":"SUCCEEDED",
               "committed_at":now.isoformat(),"business_date":business_date(now),"currency":"CNY","verification":op["required_verification"],**result}
    receipt['verification_mode'] = security.status(db)['mode']
    db.execute("UPDATE operations SET status='SUCCEEDED',receipt_json=? WHERE operation_id=?",(canonical(receipt),op["operation_id"]))
    db.execute("DELETE FROM challenges WHERE operation_id=?",(op["operation_id"],))
    audit(db,"SUCCEEDED",op["operation_id"],now,{"summary":op["summary"],"receipt":receipt})
    if op["kind"] != "mandate_query":
        emit(db,"operation:"+op["operation_id"],op["kind"],{"operation_id":op["operation_id"],"receipt_id":receipt["receipt_id"]},now)
    if predictive:
        projection_journal.append(db, before, 'operation:' + op['operation_id'],
            {'kind': op['kind'], 'operation_id': op['operation_id'], 'receipt_id': receipt['receipt_id'],
             'transaction_ids': result.get('entry_ids', [result['transaction_id']] if result.get('transaction_id') else []),
             'arrangement_id': result.get('arrangement_id'),
             'arrangement_version': result.get('version', result.get('completed_version'))}, now)
    return get_operation(db,op["operation_id"],now)
