"""Deterministic historical totals and dated scenario projections."""
from datetime import date, timedelta
import hashlib
from .common import DomainError, business_date, require_amount, require_date
from .common import canonical
from . import matching


def arrangements(db):
    return [dict(r) for r in db.execute('''SELECT a.* FROM arrangements a JOIN
        (SELECT arrangement_id,MAX(version) v FROM arrangements GROUP BY arrangement_id) latest
        ON a.arrangement_id=latest.arrangement_id AND a.version=latest.v ORDER BY due_date,arrangement_id''')]


def accounts(db):
    rows = [dict(r) for r in db.execute("SELECT * FROM accounts WHERE subject_id='U1' ORDER BY account_id")]
    return {"accounts": rows, "total_balance_minor": sum(r["balance_minor"] for r in rows)}


def transactions(db, start=None, end=None, limit=100):
    if start: require_date(start)
    if end: require_date(end)
    if start and end and start > end:
        raise DomainError("INVALID_ARGUMENTS", "起始日期不能晚于结束日期。")
    return [dict(r) for r in db.execute('''SELECT t.* FROM transactions t JOIN accounts a USING(account_id)
       WHERE a.subject_id='U1' AND (? IS NULL OR business_date>=?) AND (? IS NULL OR business_date<=?)
       ORDER BY business_date DESC,t.rowid DESC LIMIT ?''', (start,start,end,end,limit))]


def spending(db, start, end):
    rows = transactions(db, start, end, 1000000)
    sums = {kind: sum(abs(r["amount_minor"]) for r in rows if r["kind"] == kind) for kind in ("income", "expense", "refund")}
    return {"start_date": start, "end_date": end, "income_minor": sums["income"], "expense_minor": sums["expense"],
            "refund_minor": sums["refund"], "net_expense_minor": sums["expense"]-sums["refund"],
            "excluded_kinds": ["OPENING", "TRANSFER_IN", "TRANSFER_OUT"], "evidence_refs": [r["transaction_id"] for r in rows]}


def arrangement_timing(snapshot, today, through_date):
    """Describe projection timing without changing recorded dates or posting money."""
    items = []
    for item in snapshot:
        status, kind, due = item["status"], item["kind"], item["due_date"]
        effective = None
        if status == "completed":
            reason = "COMPLETED_EXCLUDED"
        elif status == "cancelled":
            reason = "CANCELLED_EXCLUDED"
        elif due > through_date:
            reason = "AFTER_WINDOW_EXCLUDED"
        elif kind == "income" and due < today:
            reason = "PAST_INCOME_UNCONFIRMED_EXCLUDED"
        elif due < today:
            reason = "PAST_OUTFLOW_UNCONFIRMED_CARRIED_FORWARD"
            effective = today
        else:
            reason = "ORIGINAL_DATE_IN_WINDOW"
            effective = due
        items.append({
            **{key: item[key] for key in ("arrangement_id", "version", "title", "kind", "amount_minor")},
            "arrangement_status": status, "original_due_date": due,
            "forecast_effective_date": effective, "included_in_forecast": effective is not None,
            "inclusion_reason": reason,
            "fulfillment_confirmation": {"active": "NOT_CONFIRMED", "completed": "RECORDED_COMPLETED",
                                         "cancelled": "NOT_ASSERTED"}[status],
            "evidence_ref": f'{item["arrangement_id"]}:v{item["version"]}'})
    return {"contract_version": 1, "date_basis": "BUSINESS_DATE_UTC_PLUS_08",
            "inclusion_scope": "PRIMARY_SCENARIO", "conservative_future_income": "EXCLUDED",
            "legal_overdue_status": "NOT_DETERMINED", "date_passage_posts_transactions": False,
            "items": items}



def scenario_minimum(daily, balance_field, minimum, today, future_income):
    """Describe an already calculated minimum using the legacy date selection."""
    point = next((day for day in daily if day[balance_field] == minimum), None)
    return {"minimum_balance_minor": minimum,
            "minimum_balance_date": point["date"] if point is not None else today,
            "minimum_point": "DAILY_PROJECTION" if point is not None else "CURRENT_SNAPSHOT",
            "future_income": future_income}


def forecast(db, now, through_date, proposed_expense_minor=0, proposed_date=None):
    return forecast_from_snapshot(forecast_snapshot(db, now), through_date,
                                  proposed_expense_minor, proposed_date)


def forecast_snapshot(db, now):
    """Read only through the caller's single bank transaction."""
    refunds = [f'已关联安排 {row["arrangement_id"]} 的支出发生退款 {row["amount_minor"]/100:.2f} 元（{row["business_date"]}）；安排未自动恢复，原付款义务是否仍在需用户核对，若仍须履行请新增安排。'
        for row in db.execute('''SELECT m.arrangement_id,r.amount_minor,r.business_date FROM arrangement_matches m
            JOIN transactions r ON r.original_transaction_id=m.transaction_id WHERE r.kind='refund' ORDER BY r.transaction_id''')]
    return {"business_date": business_date(now), "accounts": accounts(db),
            "arrangements": arrangements(db), "candidates": matching.candidates(db),
            "matches": matching.matches(db), "matched_refunds": refunds,
            "match_rejections": [dict(row) for row in db.execute(
                'SELECT * FROM match_rejections ORDER BY transaction_id,arrangement_id,expected_version')]}


def snapshot_hash(snapshot):
    return hashlib.sha256(canonical(snapshot).encode()).hexdigest()


def forecast_from_snapshot(snapshot, through_date, proposed_expense_minor=0, proposed_date=None):
    """Pure projection: no database, clock reads, or mutation of the snapshot."""
    require_date(through_date)
    today = snapshot["business_date"]
    if through_date < today or (date.fromisoformat(through_date)-date.fromisoformat(today)).days > 366:
        raise DomainError("INVALID_ARGUMENTS", "预测截止日期须在今日至一年内。")
    require_amount(proposed_expense_minor, zero=True)
    proposed_date = proposed_date or today
    require_date(proposed_date)
    if not today <= proposed_date <= through_date:
        raise DomainError("INVALID_ARGUMENTS", "拟议消费日期不在预测窗口内。")
    account_snapshot = snapshot["accounts"]
    current = account_snapshot["total_balance_minor"]
    arrangement_snapshot = snapshot["arrangements"]
    active = [a for a in arrangement_snapshot if a["status"] == "active"]
    overdue_income = [a for a in active if a["kind"] == "income" and a["due_date"] < today]
    plans = [a for a in active if a["due_date"] <= through_date and a not in overdue_income]
    balance = conservative = current
    daily = []
    cursor = date.fromisoformat(today)
    while cursor.isoformat() <= through_date:
        day = cursor.isoformat()
        # Past-due arrangements remain obligations until user revises their date.
        due = [a for a in plans if max(today, a["due_date"]) == day]
        inflow = sum(a["amount_minor"] for a in due if a["kind"] == "income")
        outflow = sum(a["amount_minor"] for a in due if a["kind"] in ("expense", "goal"))
        proposed = proposed_expense_minor if day == proposed_date else 0
        balance += inflow-outflow-proposed
        conservative -= outflow+proposed
        daily.append({"date": day, "expected_income_minor": inflow, "planned_outflow_minor": outflow,
                      "proposed_expense_minor": proposed, "projected_balance_minor": balance,
                      "without_future_income_minor": conservative,
                      "arrangement_refs": [f'{a["arrangement_id"]}:v{a["version"]}' for a in due]})
        cursor += timedelta(days=1)
    lowest = min([current]+[d["projected_balance_minor"] for d in daily])
    conservative_low = min([current]+[d["without_future_income_minor"] for d in daily])
    minima = {"contract_version": 1, "scope": "COMBINED_ACCOUNTS", "currency": "CNY",
              "window_start": today, "window_end": through_date, "date_basis": "BUSINESS_DATE_UTC_PLUS_08",
              "selection_rule": "FIRST_MATCHING_DAILY_DATE_ELSE_CURRENT_SNAPSHOT",
              "PRIMARY": scenario_minimum(daily, "projected_balance_minor", lowest, today,
                                          "ASSUMED_FROM_ACTIVE_NON_OVERDUE_ARRANGEMENTS"),
              "WITHOUT_FUTURE_INCOME": scenario_minimum(daily, "without_future_income_minor",
                                                        conservative_low, today, "EXCLUDED")}
    candidates = snapshot["candidates"]
    warnings = [f'流水 {item["transaction_id"]} 可能对应“{item["title"]}”（{item["due_date"]}，{item["amount_minor"]/100:.2f}元），尚未确认关联。该流水已记账，安排仍在预测中，可能重复计入；请核对。' for item in candidates]
    matched_refunds = snapshot["matched_refunds"]
    warnings.extend(matched_refunds)
    return {"scope": "COMBINED_ACCOUNTS",
            "arrangement_timing": arrangement_timing(arrangement_snapshot, today, through_date),
            "account_allocation": {
                "status": "NOT_MODELED",
                "included_accounts": [{"account_id": a["account_id"], "current_balance_minor": a["balance_minor"],
                                       "version": a["version"]} for a in account_snapshot["accounts"]],
                "arrangement_account_assignment": "NOT_RECORDED",
                "proposed_expense_account_id": None,
                "per_account_projection": None,
                "per_account_transfer_capacity_minor": None},
            "scenario_basis": {
                "primary_future_income": "ASSUMED_FROM_ACTIVE_NON_OVERDUE_ARRANGEMENTS",
                "conservative_future_income": "EXCLUDED",
                "date_passage_posts_transactions": False},
            "as_of_date": today, "through_date": through_date, "current_balance_minor": current,
            "lowest_balance_minor": lowest, "gap_minor": max(0,-lowest), "without_future_income_gap_minor": max(0,-conservative_low),
            "lowest_balance_date": minima["PRIMARY"]["minimum_balance_date"],
            "without_future_income_lowest_balance_minor": conservative_low,
            "without_future_income_lowest_balance_date": minima["WITHOUT_FUTURE_INCOME"]["minimum_balance_date"],
            "scenario_minima": minima,
            "first_gap_date":next((d["date"] for d in daily if d["projected_balance_minor"] < 0),None),
            "first_without_income_gap_date":next((d["date"] for d in daily if d["without_future_income_minor"] < 0),None),
            "income_conditions":[{"arrangement_id":a["arrangement_id"],"title":a["title"],"due_date":a["due_date"],"amount_minor":a["amount_minor"]} for a in plans if a["kind"] == "income"],
            "unconfirmed_matches":candidates,"reconciliation_warnings":warnings,"matched_refund_warnings":matched_refunds,
            "overdue_income_excluded":overdue_income,
            "daily": daily, "assumptions": ["合计两个本人账户；实际付款仍须检查付款账户余额。", "本人内部账户转账只改变余额分布，不改变 total_balance，不减少合计资金缺口；任何借款或额外收入必须确认，不能假设存在或已到账。", "未来收入是用户安排，未计入当前余额；已逾期未到账收入不纳入主预测。", "目标金额按到期预留处理；逾期支出安排保守计入今日。", "不自动匹配流水和安排履行；到账或支出对应安排需经用户确认改为 completed，以免重复预测。", "此预测未自动生成用户未确认的生活开销。", "本工具不分配安排收入/支出或拟消费到具体账户，不提供单账户未来余额或可转额度；合计未来收入不能推定进入某账户。", "日期经过不代表安排已履行或收入已入账；实际付款会减少资金，不能把到期日过去当作增加可挪余额。"],
            "evidence_refs": [f'forecast_window:{today}:{through_date}'] + [f'{a["arrangement_id"]}:v{a["version"]}' for a in plans] + [f'account:{a["account_id"]}:v{a["version"]}' for a in account_snapshot["accounts"]] + ["matches:"+hashlib.sha256(canonical(candidates).encode()).hexdigest()]}
