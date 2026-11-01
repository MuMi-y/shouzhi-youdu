"""Date review from existing evidence, independent of saving or paying actions."""


def context(data):
    today = data['business_date']
    plan = data.get('current_plan')
    if plan:
        stage = plan['notice_state']
        if stage in {'INACTIVE', 'REVIEW_REQUIRED'}:
            return None
        if stage == 'SAVED' and plan['arrangement']['due_date'] < today:
            return {'business_date': today, 'reason': 'SAVED_DATE_PASSED',
                    'reference_date': plan['arrangement']['due_date']}
        if stage in {'SELECTED', 'WAITING_CONFIRMATION'} and plan['selected_date'] < today:
            return {'business_date': today, 'reason': 'SELECTED_DATE_PASSED',
                    'reference_date': plan['selected_date']}
    if data['status'] == 'DATE_REVIEW_REQUIRED':
        return {'business_date': today, 'reason': 'GOAL_DATE_PASSED',
                'reference_date': data['profile']['preferred_date']}
    return None


def display(review):
    date_text = f"截至{review['business_date']}，原定{review['reference_date']}已过。"
    if review['reason'] == 'SAVED_DATE_PASSED':
        return (date_text + '这笔已保存支出安排仍未确认履行，预测保守计入当前业务日；'
                '预测计入日不是新的付款日，日期经过也不证明已经付款或现实违约。')
    if review['reason'] == 'SELECTED_DATE_PASSED':
        return date_text + '该日期属于尚未完成保存的所选方案，请先核对日期和原待办，不据此重复办理。'
    return date_text + '这是目标的原期望日期，候选方案需重新核对；实际已保存安排仍按其自己的日期和状态处理。'


def next_step(review):
    if review['reason'] == 'SAVED_DATE_PASSED':
        return ('下一步：先核对这笔安排是否已履行；已履行时关联原流水，避免重复预测或付款；'
                '尚未履行时核对新的安排日期，再独立确认调整。')
    if review['reason'] == 'SELECTED_DATE_PASSED':
        return '下一步：先核对所选方案的日期和原待办；重新比较后再决定办理，避免确认过期方案或重复提交。'
    return '下一步：先核对目标的期望日期，再重新比较；已有银行安排不会因修改目标而自动更改。'
