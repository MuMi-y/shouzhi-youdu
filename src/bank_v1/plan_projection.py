"""Shared coverage for all-plan decisions, independent of short dashboard summaries."""
from datetime import date, timedelta
from . import finance
from .common import DomainError, require_date


def window(snapshot, requested=None):
    today = date.fromisoformat(snapshot['business_date'])
    maximum = today + timedelta(days=366)
    requested = requested or (today + timedelta(days=30)).isoformat()
    require_date(requested)
    if not today.isoformat() <= requested <= maximum.isoformat():
        raise DomainError('INVALID_ARGUMENTS', '预测截止日期须在今日至一年内。')
    active = [a for a in snapshot['arrangements'] if a['status'] == 'active']
    end = min(max([requested, (today + timedelta(days=30)).isoformat()] +
                  [a['due_date'] for a in active]), maximum.isoformat())
    omitted = [a for a in active if a['due_date'] > end]
    return {'through_date': end, 'maximum_date': maximum.isoformat(),
            'active_count': len(active), 'outside_window': omitted,
            'complete_for_active_dates': not omitted,
            'notice': '仅按已记录的一次性安排计算；不推断每月工资或生活费。超过窗口的安排未计入，不能据此判断其可行。'}


def forecast(snapshot, through_date=None, proposed_expense_minor=0, proposed_date=None):
    coverage = window(snapshot, through_date)
    result = finance.forecast_from_snapshot(snapshot, coverage['through_date'],
                                            proposed_expense_minor, proposed_date)
    result.update(requested_through_date=through_date, plan_coverage=coverage)
    return result


def exact_forecast(snapshot, through_date, proposed_expense_minor=0, proposed_date=None):
    result = finance.forecast_from_snapshot(snapshot, through_date, proposed_expense_minor, proposed_date)
    active = [a for a in snapshot['arrangements'] if a['status'] == 'active']
    omitted = [a for a in active if a['due_date'] > through_date]
    result['plan_coverage'] = {'through_date': through_date, 'active_count': len(active),
        'outside_window': omitted, 'complete_for_active_dates': not omitted,
        'notice': '只评估明确指定的日期窗口；窗口以后的计划未计入，不能据此断言全部计划不受影响。'}
    return result
