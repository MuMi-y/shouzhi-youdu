"""Shared balance condition, independent of evidence, authorization and rendering."""
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class BalanceAssessment:
    balance_minor: int
    proposed_minor: int
    status: Literal['BALANCE_SUFFICIENT', 'INSUFFICIENT_FUNDS', 'RESERVE_SHORTFALL']


def assess_balance(balance_minor: int, proposed_minor: int, *, reserve_minor: int = 0) -> BalanceAssessment:
    """Assess only one account's balance; callers own identity and freshness.

    Sufficient balance grants no permission and says nothing about future bills.
    Derived amounts still come from the calculation tool, not this condition.
    """
    if type(balance_minor) is not int or type(proposed_minor) is not int or proposed_minor <= 0:
        raise ValueError('INTEGER_BALANCE_AND_POSITIVE_AMOUNT_REQUIRED')
    if type(reserve_minor) is not int or reserve_minor < 0:
        raise ValueError('NONNEGATIVE_INTEGER_RESERVE_REQUIRED')
    status = ('INSUFFICIENT_FUNDS' if balance_minor < proposed_minor else
              'RESERVE_SHORTFALL' if balance_minor < proposed_minor + reserve_minor else 'BALANCE_SUFFICIENT')
    return BalanceAssessment(balance_minor, proposed_minor, status)
