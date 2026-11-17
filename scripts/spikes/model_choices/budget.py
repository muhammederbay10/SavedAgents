"""A spending ledger that stops live calls before they pass the ticket's cap.

Every amount must be a finite number of dollars. NaN and infinity are refused,
because any comparison with NaN is false and would let every request through.

A request's estimate is reserved before the request is sent, and replaced by
the real charge only when a usable one comes back. A request that fails, is
cancelled or reports no usable charge therefore still counts, at its estimate.
"""

import math
from dataclasses import dataclass, field


class SpendCapExceededError(RuntimeError):
    """Raised when the next request could take the total spend over the cap."""


class InvalidAmountError(ValueError):
    """Raised when a cap, an earlier spend or an estimate is not a usable amount."""


class ReservationSettledError(RuntimeError):
    """Raised when a reservation is settled a second time."""


def is_valid_amount(value: object) -> bool:
    """Return whether a value is a finite, non-negative number (and not a boolean)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    return math.isfinite(value) and value >= 0


@dataclass(eq=False)
class Reservation:
    """The estimate set aside for one request, from before it is sent.

    Attributes:
        estimate_usd: The most the request is expected to cost.
        settled: Whether the request's outcome has been recorded.
    """

    estimate_usd: float
    settled: bool = False


@dataclass
class SpendLedger:
    """Tracks what the spike has spent and refuses requests that could pass the cap.

    The cap is cumulative: `spent_before_usd` is everything charged in earlier runs.

    Attributes:
        cap_usd: The limit for all runs together.
        spent_before_usd: Charges from earlier runs.
        returned_usd: Charges the API reported in this run.
    """

    cap_usd: float
    spent_before_usd: float
    returned_usd: float = field(default=0.0, init=False)
    _reserved: list[Reservation] = field(default_factory=list, init=False, repr=False)

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
    def unconfirmed_usd(self) -> float:
        """Return the estimates still reserved in this run.

        These are requests that are in flight, were cancelled, failed, or came
        back without a usable charge. They are exposure, not confirmed charges.
        """
        return math.fsum(reservation.estimate_usd for reservation in self._reserved)

    @property
    def cumulative_usd(self) -> float:
        """Return earlier spending plus everything counted in this run."""
        return self.spent_before_usd + self.returned_usd + self.unconfirmed_usd

    def authorize(self, estimate_usd: float) -> None:
        """Check that a cost of this size stays under the cap, without reserving it.

        Args:
            estimate_usd: The most the work is expected to cost.

        Raises:
            SpendCapExceededError: If it could take the total over the cap.
            InvalidAmountError: If the estimate is not a finite, non-negative number.
        """
        if not is_valid_amount(estimate_usd):
            raise InvalidAmountError("a cost estimate must be a finite number, zero or more")
        if self.cumulative_usd + estimate_usd > self.cap_usd:
            raise SpendCapExceededError(
                f"refused: {self.cumulative_usd:.5f} spent, {estimate_usd:.5f} more could pass "
                f"the {self.cap_usd:.2f} cap"
            )

    def reserve(self, estimate_usd: float) -> Reservation:
        """Set a request's estimate aside. Call this before the request is sent.

        From here on the estimate counts as spent, whatever happens to the request.

        Args:
            estimate_usd: The most the request is expected to cost.

        Returns:
            The reservation, to pass to `settle` when the request's outcome is known.

        Raises:
            SpendCapExceededError: If the request could take the total over the cap.
            InvalidAmountError: If the estimate is not a finite, non-negative number.
        """
        self.authorize(estimate_usd)
        reservation = Reservation(estimate_usd)
        self._reserved.append(reservation)
        return reservation

    def settle(self, reservation: Reservation, returned_usd: float | None) -> None:
        """Record a request's outcome, once.

        A usable charge replaces the reservation. A charge that is missing, not
        finite or negative is not trusted: the estimate stays reserved, so later
        cap checks still hold. A request that is never settled, because it was
        cancelled, stays reserved as well.

        Args:
            reservation: The reservation `reserve` returned for the request.
            returned_usd: The charge the API reported, or `None` if it reported none.

        Raises:
            ReservationSettledError: If the reservation was settled before.
        """
        if reservation.settled:
            raise ReservationSettledError("this reservation was already settled")
        reservation.settled = True
        if returned_usd is not None and is_valid_amount(returned_usd):
            self._reserved.remove(reservation)
            self.returned_usd += returned_usd
