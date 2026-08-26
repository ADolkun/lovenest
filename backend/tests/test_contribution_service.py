"""Contributions and Distributions: classification, and Net Contribution.

The pure half of `contribution_service` — what a broker's action text means,
and what the rows add up to once employer money and vesting are taken into
account. No database: the arithmetic is where this feature is wrong or right.
"""

from datetime import date
from decimal import Decimal

import pytest

from app.services import contribution_service as cs

# ---------------------------------------------------------------------------
# Pure: classification (no DB)
# ---------------------------------------------------------------------------
#
# The action strings below are the vocabulary a real Fidelity account-history
# export uses, verbatim in shape. The amounts are invented.


@pytest.mark.parametrize(
    "action,amount,expected",
    [
        ("CASH CONTRIBUTION CURRENT YEAR (Cash)", Decimal("1000"), "contribution"),
        ("Electronic Funds Transfer Received (Cash)", Decimal("2500"), "contribution"),
        ("Electronic Funds Transfer Paid (Cash)", Decimal("-400"), "distribution"),
        ("ROLLOVER CONTRIBUTION", Decimal("15000"), "contribution"),
        ("RMD DISTRIBUTION", Decimal("-1200"), "distribution"),
    ],
)
def test_money_crossing_the_boundary_is_classified(action, amount, expected):
    assert cs.classify_flow(action, amount) == expected


@pytest.mark.parametrize(
    "action,amount",
    [
        # A dividend is growth, and it says "received" — the collision the
        # internal list exists for.
        ("DIVIDEND RECEIVED FIDELITY GOVERNMENT MONEY MARKET (SPAXX) (Cash)", Decimal("3.10")),
        ("REINVESTMENT FIDELITY ZERO TOTAL MARKET INDEX (FZROX) (Cash)", Decimal("-22.01")),
        ("PURCHASE INTO CORE ACCOUNT FIDELITY GOVERNMENT MONEY MARKET (SPAXX)", Decimal("-100")),
        ("REDEMPTION FROM CORE ACCOUNT FIDELITY GOVERNMENT MONEY MARKET (SPAXX)", Decimal("76.04")),
        ("LONG-TERM CAP GAIN FIDELITY ZERO TOTAL MARKET INDEX (FZROX)", Decimal("5")),
        ("YOU BOUGHT TESLA INC COM (TSLA) (Cash)", Decimal("-500")),
        ("YOU SOLD ADVANCED MICRO DEVICES INC (AMD) (Cash)", Decimal("500")),
        ("WIRE TRANSFER FEE", Decimal("-25")),
    ],
)
def test_money_that_never_left_the_account_is_not_a_flow(action, amount):
    assert cs.classify_flow(action, amount) is None


def test_a_sell_is_not_a_distribution():
    """CONTEXT.md: selling converts a Holding to Liquid Cash inside the
    account; only a Distribution removes money from it."""
    assert cs.classify_flow("YOU SOLD TESLA INC COM (TSLA) (Cash)", Decimal("9000")) is None


def test_the_two_sides_of_one_transfer_are_read_from_the_sign_not_the_wording():
    """One real transfer is filed in both accounts, and both rows say
    "CONTRIBUTION". Reading the word would book two deposits for one."""
    paying_out = "TRANSFERRED TO VS 000-000000-1 CURRENT CONTRIBUTION (Cash)"
    receiving = "CASH CONTRIBUTION CURRENT YEAR (Cash)"
    assert cs.classify_flow(paying_out, Decimal("-6500")) == "distribution"
    assert cs.classify_flow(receiving, Decimal("6500")) == "contribution"


@pytest.mark.parametrize("amount", [None, Decimal("0")])
def test_a_row_with_no_amount_is_not_classified(amount):
    assert cs.classify_flow("CASH CONTRIBUTION CURRENT YEAR", amount) is None


def test_an_unrecognised_action_is_declined_rather_than_guessed():
    assert cs.classify_flow("JOURNALED SHARES", Decimal("100")) is None


def test_employer_money_is_told_apart_from_the_users_own():
    assert cs.classify_party("EMPLOYER CONTRIBUTION") == "employer"
    assert cs.classify_party("401K EMPLOYER MATCH") == "employer"
    assert cs.classify_party("CASH CONTRIBUTION CURRENT YEAR") == "self"


def test_a_contribution_designated_for_the_prior_year_counts_against_that_year():
    """An IRA contribution paid in April may be designated for the year
    before, and the annual limit it consumes is that year's."""
    paid = date(2026, 4, 10)
    assert cs.tax_year_for("CASH CONTRIBUTION PRIOR YEAR", paid) == 2025
    assert cs.tax_year_for("CASH CONTRIBUTION CURRENT YEAR", paid) == 2026


# ---------------------------------------------------------------------------
# Pure: Net Contribution (no DB)
# ---------------------------------------------------------------------------

TODAY = date(2026, 8, 25)


def _row(kind, amount, *, party="self", when=None, tax_year=None, vested_on=None):
    """An unpersisted stand-in for AssetContribution — `summarise` reads
    attributes, not a session."""
    when = when or date(2026, 3, 1)
    return type(
        "Row",
        (),
        {
            "kind": kind,
            "party": party,
            "amount": Decimal(str(amount)),
            "date": when,
            "tax_year": tax_year if tax_year is not None else when.year,
            "vested_on": vested_on,
        },
    )()


def test_net_contribution_is_contributions_minus_distributions():
    summary = cs.summarise(
        [_row("contribution", 7000), _row("contribution", 1000), _row("distribution", 2000)],
        as_of=TODAY,
    )
    assert summary["own_contributions"] == 8000.0
    assert summary["distributions"] == 2000.0
    assert summary["net"] == 6000.0


def test_a_distribution_lowers_net_contribution():
    before = cs.summarise([_row("contribution", 7000)], as_of=TODAY)["net"]
    after = cs.summarise(
        [_row("contribution", 7000), _row("distribution", 500)], as_of=TODAY
    )["net"]
    assert after == before - 500


def test_return_is_shown_net_of_contributions():
    """A balance that rose only because money was paid in is not growth."""
    summary = cs.summarise(
        [_row("contribution", 10000)], as_of=TODAY, current_value=Decimal("10000")
    )
    assert summary["return_net_of_contributions"] == 0.0

    grown = cs.summarise(
        [_row("contribution", 10000)], as_of=TODAY, current_value=Decimal("11500")
    )
    assert grown["return_net_of_contributions"] == 1500.0


def test_unvested_employer_money_is_not_counted_as_return():
    """It sits in the balance but nobody paid it as growth."""
    summary = cs.summarise(
        [
            _row("contribution", 5000),
            _row("contribution", 4000, party="employer", vested_on=date(2027, 6, 1)),
        ],
        as_of=TODAY,
        current_value=Decimal("10000"),
    )
    assert summary["net"] == 5000.0
    assert summary["return_net_of_contributions"] == 1000.0


def test_return_is_unknown_rather_than_zero_when_the_wallet_has_no_value():
    summary = cs.summarise([_row("contribution", 100)], as_of=TODAY)
    assert summary["return_net_of_contributions"] is None
    assert summary["current_value"] is None


def test_employer_money_is_totalled_apart_from_the_users_own():
    summary = cs.summarise(
        [_row("contribution", 7000), _row("contribution", 5000, party="employer")],
        as_of=TODAY,
    )
    assert summary["own_contributions"] == 7000.0
    assert summary["employer_contributions"] == 5000.0


def test_unvested_employer_money_is_excluded_from_the_users_own_figure():
    unvested = _row("contribution", 5000, party="employer", vested_on=date(2027, 6, 1))
    summary = cs.summarise([_row("contribution", 7000), unvested], as_of=TODAY)
    assert summary["employer_unvested"] == 5000.0
    assert summary["employer_vested"] == 0.0
    assert summary["net"] == 7000.0


def test_employer_money_joins_the_users_own_figure_once_it_vests():
    vested = _row("contribution", 5000, party="employer", vested_on=date(2026, 1, 1))
    summary = cs.summarise([_row("contribution", 7000), vested], as_of=TODAY)
    assert summary["employer_vested"] == 5000.0
    assert summary["employer_unvested"] == 0.0
    assert summary["net"] == 12000.0


def test_money_vests_on_its_vesting_day_not_the_day_after():
    assert cs.is_vested(TODAY, as_of=TODAY) is True
    assert cs.is_vested(date(2026, 8, 26), as_of=TODAY) is False


def test_employer_money_with_no_vesting_date_is_the_users_own():
    """An absent restriction is no restriction — an immediately-vested
    safe-harbour match has no date to record."""
    summary = cs.summarise([_row("contribution", 5000, party="employer")], as_of=TODAY)
    assert summary["employer_vested"] == 5000.0
    assert summary["net"] == 5000.0


def test_totals_are_tracked_per_year():
    summary = cs.summarise(
        [
            _row("contribution", 6000, when=date(2024, 4, 1)),
            _row("contribution", 7000, when=date(2025, 4, 1)),
            _row("contribution", 500, when=date(2025, 9, 1)),
            _row("distribution", 200, when=date(2025, 11, 1)),
        ],
        as_of=TODAY,
    )
    years = {y["tax_year"]: y for y in summary["years"]}
    assert [y["tax_year"] for y in summary["years"]] == [2025, 2024]
    assert years[2024]["own"] == 6000.0
    assert years[2025]["own"] == 7500.0
    assert years[2025]["distributions"] == 200.0
    assert years[2025]["net"] == 7300.0


def test_a_prior_year_total_lands_in_the_year_it_counts_against():
    """A figure typed today for a year that predates provider coverage is
    still that year's progress against its limit."""
    summary = cs.summarise(
        [_row("contribution", 5500, when=TODAY, tax_year=2019)], as_of=TODAY
    )
    assert [y["tax_year"] for y in summary["years"]] == [2019]
    assert summary["years"][0]["own"] == 5500.0


def test_an_annual_total_counts_employer_money_gross_because_a_limit_does():
    """A limit is measured on money paid in; vesting has nothing to do with
    it. So a year's row and the wallet's own-money figure disagree while any
    employer money is unvested, on purpose."""
    unvested = _row("contribution", 5000, party="employer", vested_on=date(2027, 6, 1))
    summary = cs.summarise([_row("contribution", 7000), unvested], as_of=TODAY)
    assert summary["years"][0]["employer"] == 5000.0
    assert summary["years"][0]["net"] == 12000.0
    assert summary["net"] == 7000.0


def test_withdrawable_basis_never_goes_below_nothing():
    """Distributions can only take back money that went in, so an
    over-distributed wallet has an empty basis, not a negative one."""
    rows = [_row("contribution", 1000), _row("distribution", 4000)]
    assert cs.summarise(rows, as_of=TODAY)["net"] == -3000.0
    assert cs.withdrawable_basis(rows, as_of=TODAY) == Decimal("0")


def test_no_rows_is_every_figure_at_zero():
    summary = cs.summarise([], as_of=TODAY)
    assert summary["net"] == 0.0
    assert summary["years"] == []


def test_cents_survive_a_long_run_of_rows():
    """Decimal end to end: a hundred rows of a third of a cent must not drift
    into a figure the user can see is wrong."""
    rows = [_row("contribution", "0.01") for _ in range(1000)]
    assert cs.summarise(rows, as_of=TODAY)["net"] == 10.0
