"""A spending ledger that stops live calls before they pass the ticket's cap."""

from dataclasses import dataclass, field


class SpendCapExceededError(RuntimeError):
    """Raised when the next request could take the total spend over the cap."""


@dataclass
class SpendLedger:
    """Tracks what the spike has spent and refuses requests that could pass the cap.

    The cap is cumulative: `spent_before_usd` is everything charged in earlier
    runs. A response that reports no charge is counted at its estimate, so a
    missing number can never hide spending.

    Attributes:
        cap_usd: The limit for all runs together.
        spent_before_usd: Charges from earlier runs.
        returned_usd: Charges the API reported in this run.
        unconfirmed_usd: Estimates for requests whose response reported no charge.
    """

    cap_usd: float
    spent_before_usd: float
    returned_usd: float = field(default=0.0, init=False)
    unconfirmed_usd: float = field(default=0.0, init=False)

    def __post_init__(self) -> None:
        """Reject a ledger that starts below zero or already over its cap."""
        if self.spent_before_usd < 0 or self.cap_usd <= 0:
            raise ValueError("the cap must be positive and earlier spending cannot be negative")
        if self.spent_before_usd >= self.cap_usd:
            raise SpendCapExceededError("earlier spending already reached the cap")

    @property
    def cumulative_usd(self) -> float:
        """Return earlier spending plus everything counted in this run."""
        return self.spent_before_usd + self.returned_usd + self.unconfirmed_usd

    def authorize(self, estimate_usd: float) -> None:
        """Check that a request with this worst-case cost stays under the cap.

        Args:
            estimate_usd: The most the request is expected to cost.

        Raises:
            SpendCapExceededError: If the request could take the total over the cap.
            ValueError: If the estimate is negative.
        """
        if estimate_usd < 0:
            raise ValueError("a cost estimate cannot be negative")
        if self.cumulative_usd + estimate_usd > self.cap_usd:
            raise SpendCapExceededError(
                f"refused: {self.cumulative_usd:.5f} spent, {estimate_usd:.5f} more could pass "
                f"the {self.cap_usd:.2f} cap"
            )

    def record(self, returned_usd: float | None, estimate_usd: float) -> None:
        """Count a finished request.

        Args:
            returned_usd: The charge the API reported, or `None` if it reported none.
            estimate_usd: The estimate that was authorized for the request.
        """
        if returned_usd is None:
            self.unconfirmed_usd += estimate_usd
        else:
            self.returned_usd += returned_usd
