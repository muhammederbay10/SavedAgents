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


def test_negative_amounts_are_rejected() -> None:
    with pytest.raises(ValueError, match="negative"):
        SpendLedger(cap_usd=2.0, spent_before_usd=-0.01)
    with pytest.raises(ValueError, match="negative"):
        SpendLedger(cap_usd=2.0, spent_before_usd=0.0).authorize(-1.0)
