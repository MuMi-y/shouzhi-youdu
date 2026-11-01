"""Current chosen/bank-recorded plan, distinct from alternative trial projections."""
from src.bank_v1 import finance
from src.bank_v1 import plan_projection


def capture(choice, choice_state, operation, assessment, goal, snapshot):
    if choice is None:
        return None
    selected = next(o for o in assessment['result']['options'] if o['option_id'] == choice['option_id'])
    receipt = operation.get('receipt') if operation else None
    receipt = receipt if isinstance(receipt, dict) else {}
    linked = next((a for a in snapshot['arrangements']
                   if a['arrangement_id'] == goal['linked_arrangement_id']), None)
    state = 'SELECTED' if choice_state == 'SELECTED' else 'WAITING_CONFIRMATION' if choice_state == 'PENDING' else 'REVIEW_REQUIRED'
    valid_receipt = (choice_state == 'LINKED' and operation is not None and operation['status'] == 'SUCCEEDED'
        and receipt.get('kind') == 'arrangement' and receipt.get('status') == 'SUCCEEDED'
        and isinstance(receipt.get('receipt_id'), str) and bool(receipt['receipt_id'])
        and receipt.get('arrangement_id') == goal['linked_arrangement_id'])
    prediction = None
    if valid_receipt and linked:
        if linked['status'] != 'active':
            state = 'INACTIVE'
        elif linked['kind'] == 'expense':
            state = 'SAVED' if linked['version'] == receipt.get('version') else 'REVIEW_REQUIRED'
            forecast = plan_projection.forecast(snapshot, max(snapshot['business_date'], goal['profile']['latest_date']))
            reserve = goal['profile']['reserve_floor_minor']
            prediction = {}
            for branch, minimum, date in [
                ('primary', 'lowest_balance_minor', 'lowest_balance_date'),
                ('without_income', 'without_future_income_lowest_balance_minor', 'without_future_income_lowest_balance_date')]:
                prediction[branch] = {'minimum_minor': forecast[minimum], 'date': forecast[date],
                    'reserve_shortfall_minor': max(0, reserve-forecast[minimum])}
    return {'choice_id': choice['choice_id'], 'option_id': choice['option_id'], 'choice_state': choice_state,
        'selected_amount_minor': selected['amount_minor'], 'selected_date': selected['due_date'],
        'operation_id': operation['operation_id'] if operation else None,
        'receipt_id': receipt.get('receipt_id'), 'notice_state': state,
        'arrangement': ({k: linked[k] for k in ('arrangement_id', 'version', 'amount_minor', 'due_date', 'status')}
                        if linked and valid_receipt else None), 'projection': prediction}
