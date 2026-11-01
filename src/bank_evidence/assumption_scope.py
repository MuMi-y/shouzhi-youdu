"""Only host-validated selected user amounts may supply historical assumptions."""
from dataclasses import dataclass


@dataclass(frozen=True)
class AssumptionScope:
    # Each pair is an actual source message and an explicitly selected amount.
    selected: tuple[tuple[str, int], ...]
    # Only host-validated ANALYZE spending items may omit the CNY yuan suffix.
    spending_selected: tuple[tuple[str, int], ...] = ()

    def contains(self, quote, value_minor):
        return any(quote in text and value_minor == amount for text, amount in self.selected)

    def omitted_spending_matches(self, quote, value_minor):
        from .spending_amount import omitted_yuan_matches
        return any(value_minor == amount and omitted_yuan_matches(quote, text, amount)
                   for text, amount in self.spending_selected)
