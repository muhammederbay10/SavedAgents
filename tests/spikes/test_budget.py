import math

import pytest

from scripts.spikes.model_choices.budget import SpendCapExceededError, SpendLedger


def test_request_that_could_pass_the_cap_is_refused() -> None:
    ledger = SpendLedger(cap_usd=2.0, spent_before_usd=1.95)
    ledger.authorize(0.05)
    with pytest.raises(SpendCapExceededError):
        ledger.authorize(0.051)


def test_earlier_and_returned_charges_add_up() -> None:
    ledger = SpendLedger(cap_usd=2.0, spent_before_usd=0.5)
    ledger.record(returned_usd=0.25, estimate_usd=1.0)
    assert ledger.cumulative_usd == pytest.approx(0.75)
    assert ledger.unconfirmed_usd == 0.0


def test_response_without_a_charge_is_counted_at_its_estimate() -> None:
    ledger = SpendLedger(cap_usd=1.0, spent_before_usd=0.0)
    ledger.record(returned_usd=None, estimate_usd=0.6)
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
        ledger.record(returned_usd=0.1, estimate_usd=estimate)
    assert ledger.cumulative_usd == 0.0


@pytest.mark.parametrize("charge", [*NOT_FINITE, pytest.param(-0.5, id="negative")])
def test_malformed_returned_charge_keeps_the_estimate_reserved(charge: float) -> None:
    ledger = SpendLedger(cap_usd=1.0, spent_before_usd=0.0)

    ledger.record(returned_usd=charge, estimate_usd=0.6)

    assert ledger.returned_usd == 0.0
    assert ledger.unconfirmed_usd == pytest.approx(0.6)
    assert ledger.cumulative_usd == pytest.approx(0.6)
    with pytest.raises(SpendCapExceededError):
        ledger.authorize(0.5)
