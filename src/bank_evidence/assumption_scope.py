"""Only host-validated selected user amounts may supply historical assumptions."""
from dataclasses import dataclass


@dataclass(frozen=True)
class AssumptionScope:
    # Each pair is an actual source message and an explicitly selected amount.
    selected: tuple[tuple[str, int], ...]

    def contains(self, quote, value_minor):
        return any(quote in text and value_minor == amount for text, amount in self.selected)
