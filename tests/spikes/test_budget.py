import math

import pytest

from scripts.spikes.model_choices.budget import (
    ReservationSettledError,
    SpendCapExceededError,
    SpendLedger,
)


def test_request_that_could_pass_the_cap_is_refused() -> None:
    ledger = SpendLedger(cap_usd=2.0, spent_before_usd=1.95)
    ledger.authorize(0.05)
    with pytest.raises(SpendCapExceededError):
        ledger.authorize(0.051)


def test_earlier_and_returned_charges_add_up() -> None:
    ledger = SpendLedger(cap_usd=2.0, spent_before_usd=0.5)
    ledger.settle(ledger.reserve(1.0), returned_usd=0.25)
    assert ledger.cumulative_usd == pytest.approx(0.75)
    assert ledger.unconfirmed_usd == 0.0


def test_response_without_a_charge_is_counted_at_its_estimate() -> None:
    ledger = SpendLedger(cap_usd=1.0, spent_before_usd=0.0)
    ledger.settle(ledger.reserve(0.6), returned_usd=None)
    assert ledger.unconfirmed_usd == pytest.approx(0.6)
    with pytest.raises(SpendCapExceededError):
        ledger.authorize(0.5)


def test_ledger_does_not_start_at_or_over_the_cap() -> None:
    with pytest.raises(SpendCapExceededError):
        SpendLedger(cap_usd=2.0, spent_before_usd=2.0)


NOT_FINITE = [
    pytest.param(math.nan, id="NaN"),
    pytest.param(math.inf, id="infinity"),
    pytest.param(-math.inf, id="minus infinity"),
]


@pytest.mark.parametrize("spent", [*NOT_FINITE, pytest.param(-0.01, id="negative")])
def test_earlier_spending_must_be_finite_and_not_negative(spent: float) -> None:
    with pytest.raises(ValueError, match="earlier spending"):
        SpendLedger(cap_usd=2.0, spent_before_usd=spent)


@pytest.mark.parametrize(
    "cap", [*NOT_FINITE, pytest.param(0.0, id="zero"), pytest.param(-1.0, id="negative")]
)
def test_cap_must_be_finite_and_positive(cap: float) -> None:
    with pytest.raises(ValueError, match="cap"):
        SpendLedger(cap_usd=cap, spent_before_usd=0.0)


@pytest.mark.parametrize("estimate", [*NOT_FINITE, pytest.param(-0.01, id="negative")])
def test_estimate_must_be_finite_and_not_negative(estimate: float) -> None:
    ledger = SpendLedger(cap_usd=2.0, spent_before_usd=0.0)
    with pytest.raises(ValueError, match="estimate"):
        ledger.authorize(estimate)
    with pytest.raises(ValueError, match="estimate"):
        ledger.reserve(estimate)
    assert ledger.cumulative_usd == 0.0


@pytest.mark.parametrize("charge", [*NOT_FINITE, pytest.param(-0.5, id="negative")])
def test_malformed_returned_charge_keeps_the_estimate_reserved(charge: float) -> None:
    ledger = SpendLedger(cap_usd=1.0, spent_before_usd=0.0)

    ledger.settle(ledger.reserve(0.6), returned_usd=charge)

    assert ledger.returned_usd == 0.0
    assert ledger.unconfirmed_usd == pytest.approx(0.6)
    assert ledger.cumulative_usd == pytest.approx(0.6)
    with pytest.raises(SpendCapExceededError):
        ledger.authorize(0.5)


def test_reserved_estimate_counts_before_the_outcome_is_known() -> None:
    ledger = SpendLedger(cap_usd=1.0, spent_before_usd=0.2)

    ledger.reserve(0.6)

    assert ledger.unconfirmed_usd == pytest.approx(0.6)
    assert ledger.cumulative_usd == pytest.approx(0.8)
    with pytest.raises(SpendCapExceededError):
        ledger.reserve(0.3)
    assert ledger.unconfirmed_usd == pytest.approx(0.6)


def test_reservation_that_is_never_settled_stays_reserved() -> None:
    ledger = SpendLedger(cap_usd=1.0, spent_before_usd=0.0)
    cancelled = ledger.reserve(0.3)
    ledger.settle(ledger.reserve(0.3), returned_usd=0.1)

    assert not cancelled.settled
    assert ledger.returned_usd == pytest.approx(0.1)
    assert ledger.unconfirmed_usd == pytest.approx(0.3)


def test_settled_reservations_leave_no_rounding_residue() -> None:
    ledger = SpendLedger(cap_usd=2.0, spent_before_usd=0.0)
    for estimate in (0.1, 0.2, 0.3, 0.1, 0.2, 0.3):
        ledger.settle(ledger.reserve(estimate), returned_usd=0.01)

    assert ledger.unconfirmed_usd == 0.0


@pytest.mark.parametrize("first_charge", [0.1, None])
def test_reservation_cannot_be_settled_twice(first_charge: float | None) -> None:
    ledger = SpendLedger(cap_usd=1.0, spent_before_usd=0.0)
    reservation = ledger.reserve(0.3)
    ledger.settle(reservation, returned_usd=first_charge)
    spent_after_first = ledger.cumulative_usd

    with pytest.raises(ReservationSettledError):
        ledger.settle(reservation, returned_usd=0.2)

    assert ledger.cumulative_usd == spent_after_first


def test_request_refused_at_the_cap_reserves_nothing() -> None:
    ledger = SpendLedger(cap_usd=1.0, spent_before_usd=0.9)

    with pytest.raises(SpendCapExceededError):
        ledger.reserve(0.2)

    assert ledger.unconfirmed_usd == 0.0
