"""Validate model-selected request slots, without claiming to prove intent."""
from copy import deepcopy
import re

from src.bank_evidence.assumption_scope import AssumptionScope
from src.bank_evidence.calculation import assumption_matches
from .actions import normalize as normalize_action
from .schema import AMOUNTS, ACCOUNTS, FIELDS, REQUIRED, FIELD_SPECS, DAY_FIELDS, DATE_FIELDS, required_fields, validate_slot_structure


ITEM_KEYS = {'id', 'intent', 'continuation_of', 'slots', 'missing_fields', 'conflicts'}


def require(condition, code):
    if not condition:
        raise ValueError(code)


def field_list(value, allowed):
    require(type(value) is list and all(type(field) is str for field in value), 'INVALID_FIELD_LIST')
    require(len(value) == len(set(value)) and set(value) <= allowed, 'UNKNOWN_OR_DUPLICATE_FIELD')
    return set(value)


def validate_slot(field, slot, messages, tool_records=(), source_contract="legacy_source_v1", *, cashflow=False):
    validate_slot_structure(field, slot, source_contract)
    source = slot['source']
    identifier, quote = source['message_id'], source['quote']
    require(type(identifier) is str and identifier in messages, 'UNKNOWN_USER_MESSAGE')
    require(type(quote) is str and 1 <= len(quote) <= 1000 and quote in messages[identifier], 'UNBOUND_SLOT_QUOTE')
    value = slot['value']
    if field in ACCOUNTS:
        from .normalization import account_source
        account_source(slot, tool_records)
        return deepcopy(slot)
    if cashflow and field in {'date_text', 'through_date_text'}:
        from .cashflow_dates import validate_slot as validate_cashflow_date
        return validate_cashflow_date(slot, tool_records)
    span = None
    if field in {'start_date', 'end_date', 'period_text'}:
        from .billing_dates import clock_and_reports, validate_source_value
        span = validate_source_value(field, slot, messages[identifier], clock_and_reports(tool_records)[0])
    if field in DAY_FIELDS:
        from .normalization import date_source_is_explicit
        require(date_source_is_explicit(quote, messages[identifier]), 'UNRESOLVED_DATE_SOURCE')
    if 'normalization' in source:
        from .normalization import normalized_value
        derived_source = source
        if span:
            index = 1 if span['expression_kind'] == 'range' and field == 'end_date' else 0
            require(field != 'period_text' or span['expression_kind'] == 'single', 'INVALID_PERIOD_NORMALIZATION')
            derived_source = {**source, 'quote': span['endpoints'][index]['text']}
        normalized = normalized_value(field, derived_source, tool_records)
        expected = derived_source['quote'] if span and field == 'period_text' else value
        require(type(value) is str and expected == normalized, 'INCORRECT_NORMALIZED_VALUE')
        return deepcopy(slot)
    if FIELD_SPECS[field].value_type == 'integer_minor':
        require(type(value) is int and 0 <= value <= 100_000_000_000 and
                assumption_matches({'quote': quote, 'value_minor': value}, messages[identifier]), 'UNBOUND_SLOT_AMOUNT')
    else:
        from .normalization import sourced_day, date_atom
        equivalent_day = (field in {'start_date', 'end_date'} or
                          field in DAY_FIELDS and type(value) is str and sourced_day(slot, tool_records) is not None)
        require(type(value) is str and 1 <= len(value) <= 300 and (value in quote or equivalent_day), 'UNBOUND_SLOT_TEXT')
        if field in DAY_FIELDS:
            atom = date_atom(quote)
            require(atom is not None and (value in {quote, atom} or equivalent_day), 'UNRESOLVED_DATE_SOURCE')
        if FIELD_SPECS[field].value_type == 'date':
            from .normalization import calendar_day
            require(calendar_day(value) is not None, 'INVALID_ISO_DATE')
    return deepcopy(slot)


def validate(items, context, tool_records=()):
    from .protocol import version, MINIMAL
    protocol = version(context)
    if protocol == MINIMAL:
        for item in items if isinstance(items, list) else []:
            for slot in item.get('slots', {}).values() if isinstance(item, dict) else []:
                require(isinstance(slot, dict) and 'normalization' not in slot.get('source', {}), 'SOURCE_PROTOCOL_MISMATCH')
    require(type(items) is list and 1 <= len(items) <= 10, 'INVALID_REQUEST_ITEMS')
    messages = {row['message_id']: row['text'] for row in context['messages']}
    parents = {item['reference']: item for item in context['previous_items']}
    superseded = {item['continuation_of'] for item in context['previous_items'] if item.get('continuation_of')}
    output, seen, continued, preview_calls = [], set(), set(), set()
    from .cashflow_dates import INTENTS as CASHFLOW, owned_regions, bind as bind_cashflow
    cashflow_scopes = owned_regions(items, context)
    for item in items:
        require(type(item) is dict and set(item) in (ITEM_KEYS, ITEM_KEYS | {'action'}), 'INVALID_REQUEST_ITEM')
        require(not context.get('require_current_action') or item.get('action') is not None, 'CURRENT_REQUEST_ACTION_REQUIRED')
        action = normalize_action(item.get('action'), context)
        identifier, intent = item['id'], item['intent']
        require(type(identifier) is str and re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', identifier)
                and identifier not in seen, 'INVALID_OR_DUPLICATE_ITEM_ID')
        require(type(intent) is str and intent in FIELDS, 'UNKNOWN_REQUEST_INTENT')
        seen.add(identifier)
        reference = item['continuation_of']
        require(reference is None or type(reference) is str, 'INVALID_CONTINUATION')
        parent = parents.get(reference) if reference else None
        require(reference is None or parent is not None and reference not in continued, 'UNKNOWN_OR_REUSED_PARENT_ITEM')
        require(reference not in superseded or action['effective_kind'] == 'CHECK_STATUS', 'SUPERSEDED_PARENT_ITEM')
        if parent:
            same_transfer = ({parent['intent'], intent} <= {'transfer', 'transfer_advice'} and
                             action['contract'] == 'CURRENT_SOURCE_VALIDATED')
            status_transition = (action['effective_kind'] == 'CHECK_STATUS' and
                (intent == 'task_status' and any(value['kind'] == 'task' for value in parent.get('related_objects', []))
                 or {parent['intent'], intent} <= {'goal_compare', 'goal_followup'}))
            require(parent['intent'] == intent or same_transfer or status_transition, 'CONTINUATION_INTENT_CHANGED')
            continued.add(reference)
        require(type(item['slots']) is dict and set(item['slots']) <= FIELDS[intent], 'UNKNOWN_SLOT_FIELD')
        from .goal_profile_reference import resolve as resolve_goal_profile
        profile_reference = resolve_goal_profile(item, action, context, tool_records)
        if profile_reference:
            require(profile_reference[1]['call_id'] not in preview_calls, 'GOAL_PROFILE_REFERENCE_SHARED_BY_ITEMS')
            preview_calls.add(profile_reference[1]['call_id'])
        from .goal_continuation import prepare_parent
        goal_parent = prepare_parent(parent, item, action, context)
        # A status-only view may change from a business request to its task view;
        # irrelevant old user fields do not become legal fields of the new intent.
        slots = {key: deepcopy(value) for key, value in parent['slots'].items() if key in FIELDS[intent]} if parent else {}
        if goal_parent:
            # Explicit host type transition of an accepted parent, never repair
            # an invalid current model field (resolve above already rejects it).
            slots.pop('goal_title', None)
        for field, slot in item['slots'].items():
            from .billing_dates import inherited_anchor, has_period_source
            if (intent in CASHFLOW and parent and field in DATE_FIELDS and slot == slots.get(field)
                    and slot['source']['message_id'] != context['current_message_id']):
                validated = deepcopy(slot)  # Re-prove its historical role/anchor below, never re-date it.
            elif (intent == 'billing' and parent and field in slots and slot == slots[field]
                    and slot['source']['message_id'] != context['current_message_id']
                    and has_period_source(field, slot['source'], messages[slot['source']['message_id']])):
                inherited_anchor(field, slot, parent, messages)
                validated = deepcopy(slot)
            else:
                validated = (profile_reference[0][field] if profile_reference and field in profile_reference[0]
                             else validate_slot(field, slot, messages, tool_records, context.get("source_contract", "legacy_source_v1"),
                                                cashflow=intent in CASHFLOW))
            # A revision is explicit and must come from the current message.
            if field not in slots or slots[field] != validated:
                require(validated['source']['message_id'] == context['current_message_id'], 'REVISION_REQUIRES_CURRENT_MESSAGE')
            slots[field] = validated
        conflicts = field_list(item['conflicts'], FIELDS[intent])
        require(not (conflicts & set(item['slots'])), 'CONFLICT_USED_AS_KNOWN_VALUE')
        if parent:
            unresolved = set(parent['conflicts']) - set(item['slots'])
            require(unresolved <= conflicts, 'OMITTED_UNRESOLVED_CONFLICT')
        for field in conflicts:
            slots.pop(field, None)
        missing = field_list(item['missing_fields'], FIELDS[intent])
        require('goal_title' not in slots or profile_reference is not None, 'GOAL_PROFILE_REFERENCE_REQUIRES_CURRENT_BINDING')
        require(not (missing & (set(slots) | conflicts)), 'KNOWN_OR_CONFLICTING_FIELD_REQUESTED_AS_MISSING')
        required = required_fields(intent, action)
        require(required - set(slots) - conflicts <= missing, 'OMITTED_REQUIRED_PARAMETER')
        for field, slot in slots.items():
            # Inherited slots are validated user choices, never cached bank balances.
            source = slot['source']
            require(source['message_id'] in messages and source['quote'] in messages[source['message_id']],
                    'INHERITED_SOURCE_NOT_VISIBLE')
        resolved = {'id': identifier, 'intent': intent, 'continuation_of': reference,
                    'slots': slots, 'missing_fields': sorted(missing), 'conflicts': sorted(conflicts), 'action': action}
        from .normalization import account_source
        bindings = parent.get('account_source_bindings', {}) if parent else {}
        resolved['account_source_bindings'] = {field: deepcopy(basis) for field, basis in bindings.items()
            if field in slots and field not in item['slots'] and basis.get('source') == slots[field]['source']
            and basis.get('value') == slots[field]['value']}
        for field in ACCOUNTS & item['slots'].keys():
            resolved['account_source_bindings'][field] = {**account_source(slots[field], tool_records),
                'request_id': context['request_id'], 'field': field,
                'source': deepcopy(slots[field]['source']), 'value': slots[field]['value']}
        if profile_reference:
            resolved['goal_profile_binding'] = profile_reference[1]
        if goal_parent:
            resolved['goal_prepare_parent'] = goal_parent
        # Persist resolved user dates, never stale bank facts. These fields are
        # host-owned and are not accepted among the model's ITEM_KEYS.
        anchors = parent.get('constraint_anchors', {}) if parent else {}
        resolved['constraint_anchors'] = {field: deepcopy(anchor) for field, anchor in anchors.items()
            if field in slots and field in DATE_FIELDS | {'period_text'}
            and anchor['value'] == slots[field]['value'] and anchor['source'] == slots[field]['source']}
        resolved['inherited_constraint_fields'] = [field for field, slot in slots.items()
            if slot['source']['message_id'] != context['current_message_id']]
        from .normalization import sourced_day
        for field in DAY_FIELDS & item['slots'].keys() if intent not in CASHFLOW else ():
            slot = slots[field]
            day = sourced_day(slot, tool_records)
            if day is not None:
                resolved['constraint_anchors'][field] = {'field': field, 'value': slot['value'],
                    'source': deepcopy(slot['source']), 'status': 'BOUND',
                    'canonical_value': day, 'rule': 'SOURCED_CALENDAR_DAY'}
        from .billing_dates import bind_item
        bind_item(resolved, parent, context, tool_records)
        from .billing_accounts import validate_request as validate_billing_account
        validate_billing_account(resolved, context, tool_records)
        if intent in CASHFLOW:
            bind_cashflow(resolved, parent, context, tool_records, cashflow_scopes[identifier],
                          supplied_fields=frozenset(item['slots']))
        if context.get('object_contract') == 1:
            from .objects import status_context
            resolved.update(status_context(resolved, parent, context))
        output.append(resolved)
    return {'version': 1, 'request_id': context['request_id'], 'items': output}


def assumptions(decision, context):
    messages = {row['message_id']: row['text'] for row in context['messages']}
    selected = tuple((messages[slot['source']['message_id']], slot['value'])
                     for item in decision['items'] for field, slot in item['slots'].items() if field in AMOUNTS)
    return AssumptionScope(selected)


def validate_questions(answer, decision):
    """The old guidance renderer becomes a vocabulary, not a missing-slot oracle."""
    if answer.get('format') != 'bank_service_v1':
        return
    mapping = {'prepare_transfer': 'transfer', 'billing_report': 'billing'}
    for part in answer.get('parts', []):
        if part.get('format') != 'bank_guidance_v1' or part.get('kind') != 'request_parameters':
            continue
        candidates = [item for item in decision['items'] if item['intent'] == mapping.get(part.get('tool'))]
        require(len(candidates) == 1, 'AMBIGUOUS_PARAMETER_QUESTION')
        require(set(part.get('fields', [])) <= set(candidates[0]['missing_fields']), 'QUESTION_FOR_KNOWN_OR_UNDECLARED_FIELD')
