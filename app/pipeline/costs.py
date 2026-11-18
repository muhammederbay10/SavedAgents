"""What a step's model calls cost, as the provider reported it.

A provider does not always report a charge. Such a call is counted, not guessed,
so a total is known to be complete only when no call was unpriced.
"""

from dataclasses import dataclass
from typing import Self


@dataclass(frozen=True)
class CallCosts:
    """The charges of some model calls.

    Attributes:
        cost_usd: The sum of the charges that were reported.
        calls: How many calls were made.
        unpriced_calls: How many of them reported no charge.
    """

    cost_usd: float = 0.0
    calls: int = 0
    unpriced_calls: int = 0

    def add(self, cost_usd: float | None) -> Self:
        """Count one more call.

        Args:
            cost_usd: Its charge; `None` if the provider reported none.

        Returns:
            The new totals.
        """
        return type(self)(
            cost_usd=self.cost_usd + (cost_usd or 0.0),
            calls=self.calls + 1,
            unpriced_calls=self.unpriced_calls + (1 if cost_usd is None else 0),
        )

    def __add__(self, other: "CallCosts") -> "CallCosts":
        """Combine two totals.

        Args:
            other: The other totals.

        Returns:
            The sum of both.
        """
        return CallCosts(
            cost_usd=self.cost_usd + other.cost_usd,
            calls=self.calls + other.calls,
            unpriced_calls=self.unpriced_calls + other.unpriced_calls,
        )

    def to_output(self) -> dict[str, object]:
        """Return the totals for a step's `enrichment_run.output`.

        Returns:
            A JSON object with every attribute.
        """
        return {
            "cost_usd": self.cost_usd,
            "calls": self.calls,
            "unpriced_calls": self.unpriced_calls,
        }

    @classmethod
    def from_output(cls, output: object) -> "CallCosts":
        """Read totals back from a step's output.

        Args:
            output: What `to_output` returned, or anything else for none.

        Returns:
            The totals; zero when the output holds none.
        """
        if not isinstance(output, dict):
            return cls()
        cost = output.get("cost_usd")
        calls = output.get("calls")
        unpriced = output.get("unpriced_calls")
        return cls(
            cost_usd=float(cost) if isinstance(cost, int | float) else 0.0,
            calls=calls if isinstance(calls, int) else 0,
            unpriced_calls=unpriced if isinstance(unpriced, int) else 0,
        )
