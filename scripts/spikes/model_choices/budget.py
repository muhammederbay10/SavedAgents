"""A spending ledger that stops live calls before they pass the ticket's cap.

Every amount must be a finite number of dollars. NaN and infinity are refused,
because any comparison with NaN is false and would let every request through.
"""

import math
from dataclasses import dataclass, field


class SpendCapExceededError(RuntimeError):
    """Raised when the next request could take the total spend over the cap."""


class InvalidAmountError(ValueError):
    """Raised when a cap, an earlier spend or an estimate is not a usable amount."""


def is_valid_amount(value: object) -> bool:
    """Return whether a value is a finite, non-negative number (and not a boolean)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    return math.isfinite(value) and value >= 0


@dataclass
class SpendLedger:
    """Tracks what the spike has spent and refuses requests that could pass the cap.

    The cap is cumulative: `spent_before_usd` is everything charged in earlier
    runs. A response that reports no usable charge is counted at its estimate,
    so a missing or malformed number can never hide spending.

    Attributes:
        cap_usd: The limit for all runs together.
        spent_before_usd: Charges from earlier runs.
        returned_usd: Charges the API reported in this run.
        unconfirmed_usd: Estimates kept for requests whose response reported no usable charge.
    """

    cap_usd: float
    spent_before_usd: float
    returned_usd: float = field(default=0.0, init=False)
    unconfirmed_usd: float = field(default=0.0, init=False)

    def __post_init__(self) -> None:
        """Check the starting amounts.

        Raises:
            InvalidAmountError: If the cap is not a finite positive number, or the
                earlier spending is not a finite, non-negative number.
            SpendCapExceededError: If the earlier spending already reached the cap.
        """
        if not is_valid_amount(self.cap_usd) or self.cap_usd == 0:
            raise InvalidAmountError("the cap must be a finite number above zero")
        if not is_valid_amount(self.spent_before_usd):
            raise InvalidAmountError("the earlier spending must be a finite number, zero or more")
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
            InvalidAmountError: If the estimate is not a finite, non-negative number.
        """
        _require_estimate(estimate_usd)
        if self.cumulative_usd + estimate_usd > self.cap_usd:
            raise SpendCapExceededError(
                f"refused: {self.cumulative_usd:.5f} spent, {estimate_usd:.5f} more could pass "
                f"the {self.cap_usd:.2f} cap"
            )

    def record(self, returned_usd: float | None, estimate_usd: float) -> None:
        """Count a finished request.

        A charge that is missing, not finite or negative is not trusted: the
        estimate stays reserved instead, so later cap checks still hold.

        Args:
            returned_usd: The charge the API reported, or `None` if it reported none.
            estimate_usd: The estimate that was authorized for the request.

        Raises:
            InvalidAmountError: If the estimate is not a finite, non-negative number.
        """
        _require_estimate(estimate_usd)
        if returned_usd is not None and is_valid_amount(returned_usd):
            self.returned_usd += returned_usd
        else:
            self.unconfirmed_usd += estimate_usd


def _require_estimate(estimate_usd: float) -> None:
    if not is_valid_amount(estimate_usd):
        raise InvalidAmountError("a cost estimate must be a finite number, zero or more")
