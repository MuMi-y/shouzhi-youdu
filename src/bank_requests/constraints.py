"""Bind user constraints to validated delivery evidence, never execute tools.

This layer reports what remains unverified instead of treating a saved slot as
a fulfilled condition. It cannot undo an operation prepared earlier by a tool.
"""
from copy import deepcopy
from datetime import datetime
import hashlib

from src.bank_v1.common import canonical, business_date
from src.bank_v1.mandate_match import normalize as normalize_merchant
from .normalization import calendar_day, period_range, phrase_day, goal_text_matches
from .schema import PARAMETER_BINDINGS, GOAL_BINDINGS, GOAL_TEXT_BINDINGS, DAY_FIELDS, OBJECT_KINDS


def date_value(item, field, business_day=None):
    anchor = item.get('constraint_anchors', {}).get(field)
    if anchor:
        return anchor['canonical_value']
    value = item['slots'][field]['value']
    absolute = calendar_day(value)
    if absolute:
        return absolute
    if field in item.get('inherited_constraint_fields', []):
        return None  # Old unanchored relative words cannot slide to today's date.
    return phrase_day(value, business_day)


def period_value(item, business_day):
    anchor = item.get('constraint_anchors', {}).get('period_text')
    if anchor:
        return anchor['canonical_value']
    if 'period_text' in item.get('inherited_constraint_fields', []):
        return None
    return period_range(item['slots']['period_text']['value'], business_day)


def parameter_values(item, business_day=None):
    values = {}
    for field, parameter in PARAMETER_BINDINGS.get(item['intent'], {}).items():
        if field not in item['slots']:
            continue
        value = item['slots'][field]['value']
        if field == 'date_text':
            value = date_value(item, field, business_day)
            if value is None:
                continue  # evaluate() retains this as UNVERIFIED.
        if parameter in values and values[parameter] != value:
            raise ValueError('CONFLICTING_REQUEST_CONSTRAINTS')
        values[parameter] = value
    if item['intent'] == 'arrangement':
        for field, kind in (('income_minor', 'income'), ('expense_minor', 'expense')):
            if field in item['slots']:
                if 'kind' in values and values['kind'] != kind:
                    raise ValueError('CONFLICTING_REQUEST_CONSTRAINTS')
                values['kind'] = kind
    return values


def parameters_match(params, item, business_day=None):
    return all(params.get(key) == value for key, value in parameter_values(item, business_day).items())


def resolved(field, slot, status='UNVERIFIED', rule='NO_BOUND_RESULT', refs=()):
    return {'field': field, 'value': deepcopy(slot['value']), 'source': deepcopy(slot['source']),
            'status': status, 'rule': rule, 'evidence_refs': list(refs)}


def mark(checks, field, rule, refs=(), *, status='BOUND', **extra):
    if field in checks:
        checks[field].update(status=status, rule=rule, evidence_refs=list(refs), **extra)


def require_equal(actual, expected, reason):
    if actual != expected:
        raise ValueError(reason)


def billing_constraints(item, components, checks):
    for component in components:
        if component['metadata']['format'] != 'bank_billing_v1':
            continue
        snapshot = component['validated']['bank_facts_basis']['snapshots'][0]
        args, day, ref = snapshot['arguments'], snapshot['business_date'], snapshot['evidence_id']
        from .billing_dates import verify_report
        periods = verify_report(item, snapshot)
        requested_account = item['slots'].get('source_account', {}).get('value')
        require_equal(args.get('account_id'), requested_account, 'BILLING_ACCOUNT_DOES_NOT_MATCH_REQUEST')
        if requested_account is not None:
            mark(checks, 'source_account', 'REPORT_AUTHORIZED_ACCOUNT', [ref])
        for field, info in periods.items():
            require_equal({key: args.get(key) for key in info['observable_range']}, info['observable_range'],
                          'BILLING_PERIOD_DOES_NOT_MATCH_REQUEST')
        for field in ('start_date', 'end_date'):
            if field in item['slots']:
                require_equal(args.get(field), item['slots'][field]['value'], 'BILLING_PERIOD_DOES_NOT_MATCH_REQUEST')
                mark(checks, field, 'REPORT_QUERY_PERIOD', [ref],
                     **({'billing_resolution': periods[field]} if field in periods else {}))
        if 'period_text' in item['slots']:
            expected = periods['period_text']['observable_range'] if 'period_text' in periods else period_value(item, day)
            if expected is not None:
                require_equal({key: args.get(key) for key in expected}, expected, 'BILLING_PERIOD_DOES_NOT_MATCH_REQUEST')
                mark(checks, 'period_text', 'REPORT_BUSINESS_CALENDAR', [ref], canonical_value=expected,
                     **({'billing_resolution': periods['period_text']} if 'period_text' in periods else {}))


def operation_constraints(item, operations, failures, checks):
    relevant = [(op, observed) for op, observed, _ in operations if op['kind'] == item['intent']]
    for op, observed in relevant:
        day = business_date(datetime.fromisoformat(observed))
        if not parameters_match(op['summary']['params'], item, day):
            raise ValueError('OPERATION_PARAMETERS_DO_NOT_MATCH_REQUEST')
        values = parameter_values(item, day)
        for field, parameter in PARAMETER_BINDINGS.get(item['intent'], {}).items():
            if parameter in values:
                mark(checks, field, 'BANK_OPERATION_PARAMETERS', [op['operation_id']], canonical_value=values[parameter])
        if item['intent'] == 'mandate_cancel' and 'merchant_text' in item['slots']:
            name = op['summary'].get('mandate_snapshot', {}).get('name')
            # Exact bank identity only. A substring is not proof of uniqueness.
            if name and normalize_merchant(name) == normalize_merchant(item['slots']['merchant_text']['value']):
                mark(checks, 'merchant_text', 'EXACT_BANK_MERCHANT', [op['operation_id']])
    if not relevant:
        expected = {'transfer': 'prepare_transfer', 'arrangement': 'propose_arrangement',
                    'mandate_cancel': 'prepare_mandate_cancel'}.get(item['intent'])
        for attempt in failures:
            if attempt['tool'] == expected:
                if not parameters_match(attempt['args'], item):
                    raise ValueError('FAILURE_PARAMETERS_DO_NOT_MATCH_REQUEST')
                values = parameter_values(item)
                for field, parameter in PARAMETER_BINDINGS.get(item['intent'], {}).items():
                    if parameter in values:
                        mark(checks, field, 'FAILED_ATTEMPT_PARAMETERS')


def facts_and_calculations(components):
    facts, calculations = [], []
    for component in components:
        basis = component['validated'].get('bank_facts_basis', {})
        facts.extend(basis.get('facts', []))
        calculations.extend(basis.get('calculations', []))
    return facts, calculations


def account_calculation_constraints(item, components, checks):
    """Bind selected account facts and arithmetic independently of intent.

    This proves the inputs of a snapshot calculation, not future cashflow.
    """
    slots = item['slots']
    facts, calculations = facts_and_calculations(components)
    # Selected account facts remain useful even if no operation was prepared.
    for field in ('source_account', 'target_account'):
        if field in slots:
            matching = [f for f in facts if f['source_tool'] == 'accounts' and f['entity'] == slots[field]['value']]
            if matching:
                mark(checks, field, 'ACCOUNT_OBSERVED', [f['fact_id'] for f in matching])
    if not {'source_account', 'amount_minor'} <= slots.keys():
        return
    account, amount = slots['source_account']['value'], slots['amount_minor']['value']
    remaining = None
    for calc in calculations:
        inputs, provenance = calc['inputs'], calc.get('input_provenance', {})
        left, right = provenance.get('left', {}), provenance.get('right', {})
        if inputs['operation'] != 'subtract':
            continue
        # Accept either subtraction direction as a sourced amount/account
        # comparison; only balance-minus-amount can establish the reserve.
        for bank, proposed in ((left, right), (right, left)):
            fact = bank.get('fact', {})
            if (bank.get('kind') == 'bank_fact' and fact.get('source_tool') == 'accounts'
                    and fact.get('entity') == account and fact.get('field') == 'balance_minor'
                    and proposed.get('kind') == 'user_assumption' and proposed.get('value_minor') == amount):
                ref = 'calculation:' + hashlib.sha256(canonical(calc).encode('utf8')).hexdigest()
                mark(checks, 'source_account', 'BOUND_ACCOUNT_CALCULATION', [ref])
                mark(checks, 'amount_minor', 'BOUND_ACCOUNT_CALCULATION', [ref])
                if bank is left:
                    remaining = (calc['result']['amount_minor'], ref)
        # Other formulas/assumptions remain visible evidence, not reserve proof.
    return remaining


def transfer_constraints(item, components, checks, operations, remaining):
    slots = item['slots']
    if 'target_account' in slots and item['intent'] in {'transfer', 'transfer_advice'}:
        # Only a transfer destination is a choice; an observed comparison
        # account must still be backed by selected bank facts.
        mark(checks, 'target_account', 'VALIDATED_DESTINATION_CHOICE')
    if 'reserve_minor' in slots and remaining is not None:
        value, ref = remaining
        mark(checks, 'reserve_minor', 'POST_TRANSFER_ACCOUNT_RESERVE', [ref],
             status='BOUND' if value >= slots['reserve_minor']['value'] else 'UNMET',
             observed_value=value)
    if 'date_text' in slots:
        for op, observed, _ in operations:
            if op['kind'] != 'transfer':
                continue
            day = business_date(datetime.fromisoformat(observed))
            requested = date_value(item, 'date_text', day)
            if requested == day:
                mark(checks, 'date_text', 'CURRENT_DAY_PREPARATION_ONLY', [op['operation_id']], canonical_value=requested)


def comparison_constraints(item, components, checks):
    for component in components:
        meta = component['metadata']
        if meta['format'] != 'bank_spending_comparison_v1':
            continue
        row = meta['proposed_forecast']
        args, data = row['arguments'], row['data']
        ref = row['evidence_id']
        for field in ('amount_minor', 'expense_minor'):
            if field in item['slots']:
                require_equal(args.get('proposed_expense_minor'), item['slots'][field]['value'], 'COMPARISON_AMOUNT_DOES_NOT_MATCH_REQUEST')
                mark(checks, field, 'PROPOSED_FORECAST', [ref])
        if 'date_text' in item['slots']:
            expected = date_value(item, 'date_text', data['as_of_date'])
            if item['intent'] == 'cashflow_compare':
                require_equal('date_text' in item.get('cashflow_contract', {}).get('date_roles', {}), True,
                              'COMPARISON_DATE_ROLE_UNPROVEN')
            if expected:
                require_equal(args.get('proposed_date', data['as_of_date']), expected, 'COMPARISON_DATE_DOES_NOT_MATCH_REQUEST')
                mark(checks, 'date_text', 'PROPOSED_FORECAST_DATE', [ref], canonical_value=expected,
                     role='SPENDING_DATE', call_id=row.get('call_id'), parameter='proposed_date')
        if 'through_date_text' in item.get('cashflow_contract', {}).get('date_roles', {}):
            from .cashflow_dates import cutoff
            expected = date_value(item, 'through_date_text')
            for name in ('baseline_forecast', 'proposed_forecast'):
                cutoff(meta[name], expected)
            mark(checks, 'through_date_text', 'BOTH_FORECAST_CUTOFFS',
                 [meta['baseline_forecast']['evidence_id'], ref], canonical_value=expected,
                 role='FORECAST_CUTOFF', call_ids=[meta[n]['call_id'] for n in ('baseline_forecast', 'proposed_forecast')],
                 parameter='through_date')
        if 'reserve_minor' in item['slots']:
            floor = item['slots']['reserve_minor']['value']
            values = {key: data['scenario_minima'][key]['minimum_balance_minor']
                      for key in ('PRIMARY', 'WITHOUT_FUTURE_INCOME')}
            mark(checks, 'reserve_minor', 'BOTH_FORECAST_SCENARIOS', [ref],
                 status='BOUND' if all(value >= floor for value in values.values()) else 'UNMET', scenarios=values)


def goal_constraints(item, components, checks):
    binding = item.get('goal_profile_binding')
    if binding:
        from .goal_profile_reference import verify_component, FIELDS
        previews = [c for c in components if c['metadata']['format'] == 'goal_preview_answer_v1']
        require_equal(len(previews), 1, 'GOAL_PROFILE_REFERENCE_COMPONENT_REQUIRED')
        verify_component(item, previews[0])
        for field in FIELDS:
            mark(checks, field, 'EXACT_CURRENT_PREVIEW_FIELD', [binding['source_id']],
                 status='PREFERENCE_RECORDED' if field == 'priority' else 'BOUND',
                 call_id=binding['call_id'])
    for component in components:
        meta = component['metadata']
        if meta['format'] not in {'goal_answer_v1', 'goal_preview_answer_v1', 'goal_draft_v1'}:
            continue
        body = meta['body']
        data = body['data']
        profile = data.get('unconfirmed_profile', data.get('profile'))
        if not profile:
            continue
        ref = body.get('source_id', data.get('draft_id'))
        if 'goal_text' in item['slots']:
            from src.bank_goals.impact_contract import identity_matches
            target = {'profile': profile, 'arrangement': (data.get('current_plan') or {}).get('arrangement')}
            require_equal(identity_matches(item['slots']['goal_text']['value'], target), True, 'GOAL_IDENTITY_MISMATCH')
            mark(checks, 'goal_text', 'EXACT_GOAL_IDENTITY', [ref])
        for field, key in GOAL_BINDINGS.items():
            if field not in item['slots']:
                continue
            expected = item['slots'][field]['value']
            if field in DAY_FIELDS:
                expected = date_value(item, field, data.get('business_date'))
                if expected is None:
                    continue
            require_equal(profile.get(key), expected, 'GOAL_PROFILE_DOES_NOT_MATCH_REQUEST')
            mark(checks, field, 'GOAL_PROFILE_CONSTRAINT', [ref], canonical_value=expected)
        for field, keys in GOAL_TEXT_BINDINGS.items():
            if binding:
                continue
            if field not in item['slots']:
                continue
            value = item['slots'][field]['value']
            require_equal(goal_text_matches(field, value, profile, keys), True,
                          'GOAL_PROFILE_DOES_NOT_MATCH_REQUEST')
            mark(checks, field, 'GOAL_IDENTITY' if field == 'purpose' else 'GOAL_PREFERENCE_RECORDED', [ref],
                 status='BOUND' if field == 'purpose' else 'PREFERENCE_RECORDED')
        latest = date_value(item, 'latest_date_text', data.get('business_date')) if 'latest_date_text' in item['slots'] else None
        if latest:
            require_equal(all(option['due_date'] <= latest for option in data.get('options', [])), True,
                          'GOAL_OPTION_EXCEEDS_LATEST_DATE')


def impact_constraints(item, components, checks):
    from src.bank_goals.impact_contract import identity_matches
    for component in components:
        if component['metadata']['format'] != 'goal_impact_v1':
            continue
        body = component['metadata']['body']; data = body['data']; ref = body['source_id']
        for field in ('goal_text', 'purpose'):
            if field in item['slots']:
                require_equal(identity_matches(item['slots'][field]['value'], data['target']), True, 'GOAL_IDENTITY_MISMATCH')
                mark(checks, field, 'EXACT_GOAL_IDENTITY', [ref])
        if body['mode'] == 'WHAT_IF':
            for field in ('amount_minor', 'expense_minor'):
                if field in item['slots']:
                    require_equal(item['slots'][field]['value'], body['arguments']['amount_minor'], 'IMPACT_AMOUNT_MISMATCH')
                    mark(checks, field, 'GOAL_IMPACT_PROPOSED_EXPENSE', [ref])
            if 'date_text' in item['slots']:
                expected = date_value(item, 'date_text', data['business_date'])
                if expected:
                    require_equal(expected, body['arguments']['due_date'], 'IMPACT_DATE_MISMATCH')
                    mark(checks, 'date_text', 'GOAL_IMPACT_DATE', [ref], canonical_value=expected)
        if 'reserve_minor' in item['slots']:
            require_equal(item['slots']['reserve_minor']['value'], data['reserve_floor_minor'], 'IMPACT_RESERVE_MISMATCH')
            mark(checks, 'reserve_minor', 'GOAL_IMPACT_RESERVE', [ref],
                 status='UNMET' if any(s[b]['reserve_shortfall_minor'] for s in data['stages'][-1:]
                                      for b in ('primary', 'without_income')) else 'BOUND')


def evaluate(item, components, operations, failures):
    checks = {field: resolved(field, slot) for field, slot in item['slots'].items()}
    if (item.get('object_contract') == 1 and item['intent'] in OBJECT_KINDS
            and item.get('action', {}).get('effective_kind') == 'CHECK_STATUS'):
        for field in checks:
            mark(checks, field, 'EXISTING_OBJECT_NOT_NEW_INSTRUCTION', status='STATUS_ONLY')
        return list(checks.values())
    if item['intent'] == 'billing':
        billing_constraints(item, components, checks)
    operation_constraints(item, operations, failures, checks)
    if item['intent'] in {'transfer', 'transfer_advice', 'account_overview', 'cashflow_compare'}:
        remaining = account_calculation_constraints(item, components, checks)
        if item['intent'] in {'transfer', 'transfer_advice', 'account_overview'}:
            transfer_constraints(item, components, checks, operations, remaining)
    if item['intent'] in {'cashflow_compare', 'transfer_advice', 'goal_compare'}:
        comparison_constraints(item, components, checks)
    if item['intent'] in {'goal_compare', 'goal_followup'}:
        goal_constraints(item, components, checks)
    impact_constraints(item, components, checks)
    if item['intent'] == 'cashflow_forecast':
        from .cashflow_dates import cutoff
        windows = [c['metadata']['cashflow_window'] for c in components if 'cashflow_window' in c['metadata']]
        # Do not silently satisfy a query using a different component/window.
        require_equal(len(windows) <= 1, True, 'MULTIPLE_QUERY_WINDOW_COMPONENTS')
        role = item.get('cashflow_contract', {}).get('date_roles', {}).get('through_date_text')
        if len(windows) == 1 and windows[0].get('forecast') and role:
            row = windows[0]['forecast']; cutoff(row, role['canonical_value'])
            mark(checks, 'through_date_text', 'SELECTED_FORECAST_CUTOFF', [row['evidence_id']],
                 canonical_value=role['canonical_value'], role='FORECAST_CUTOFF',
                 call_id=row['call_id'], parameter='through_date')
    from .cashflow_dates import apply_issues
    apply_issues(item, checks)
    return list(checks.values())


def apply_to_outcome(outcome, checks):
    outcome['constraint_checks'] = checks
    unmet = [check for check in checks if check['status'] == 'UNMET']
    unknown = [check for check in checks if check['status'] == 'UNVERIFIED']
    if unmet:
        outcome.update(assessment='CONDITION_UNMET', reason='USER_CONSTRAINT_UNMET', next_step='REVISE_REQUEST')
    elif unknown and outcome['assessment'] != 'NEEDS_INPUT':
        # Preserve prerequisite confirmation and actual bank status, not success
        # of the ultimate requested operation. Constraints are still disclosed.
        prerequisite = outcome['reason'] == 'PREREQUISITE_CONFIRMATION'
        outcome.update(assessment='NEEDS_EVIDENCE', reason='USER_CONSTRAINT_UNVERIFIED')
        if prerequisite:
            outcome['constraint_phase'] = 'PREREQUISITE_ONLY'
        if not prerequisite and outcome['next_step'] not in {'CLARIFY', 'WAIT_TASK', 'RECONCILE_ORIGINAL', 'REPLAN_UNFINISHED'}:
            outcome['next_step'] = 'READ_CURRENT'
    return outcome
