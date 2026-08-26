"""Tests for the contributions CSV import.

A brokerage *account* history in, Contributions and Distributions out. The
pure half — finding the header a broker buried, and reading the rows that
crossed the account's boundary — is pinned first, then the endpoints.

Every figure and account label here is invented. The file shape is real; the
data is not.
"""
import uuid
from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.asset_contribution import AssetContribution
from app.models.asset_group import AssetGroup
from app.services.contribution_import_service import _narrow_to_account, parse_history_csv

_HEADER = (
    "Run Date,Account,Account Number,Action,Symbol,Description,Type,Price ($),"
    "Quantity,Commission ($),Fees ($),Accrued Interest ($),Amount ($),Settlement Date"
)

_DISCLAIMER = (
    '"The data and information in this spreadsheet is provided to you solely for your use'
    ' and is not for distribution."\n'
    "Date downloaded 04/30/2026 09:15 am\n"
)


def _row(run_date: str, action: str, amount: str, account: str = "ROTH IRA") -> str:
    return f'{run_date},{account},X12345678,"{action}",,,Cash,"","","","","",{amount},'


def _history(*rows: str, preamble: str = "﻿\n\n") -> bytes:
    """A Fidelity-shaped export: junk above the header, disclaimer below."""
    return (preamble + "\n".join([_HEADER, *rows]) + "\n\n" + _DISCLAIMER).encode("utf-8")


_REINVESTMENT = "REINVESTMENT FIDELITY GOVERNMENT MONEY MARKET (SPAXX) (Cash)"
_DIVIDEND = "DIVIDEND RECEIVED FIDELITY GOVERNMENT MONEY MARKET (SPAXX) (Cash)"
_SWEEP = "PURCHASE INTO CORE ACCOUNT FIDELITY GOVERNMENT MONEY MARKET (SPAXX)"

_MIXED_HISTORY = _history(
    _row("01/15/2025", _REINVESTMENT, "-0.02"),
    _row("01/15/2025", _DIVIDEND, "0.02"),
    _row("02/03/2025", _SWEEP, "-500.00"),
    _row("02/03/2025", "YOU BOUGHT BROAD MARKET INDEX FUND (XXXXX)", "-450.00"),
    _row("03/14/2025", "YOU SOLD BROAD MARKET INDEX FUND (XXXXX)", "320.00"),
    _row("03/14/2025", "CASH CONTRIBUTION CURRENT YEAR", "6535.95"),
    _row("04/02/2025", "TRANSFERRED TO VS ROLLOVER IRA CURRENT CONTRIBUTION", "-6535.95"),
    _row("04/02/2025", "CASH CONTRIBUTION PRIOR YEAR", "1500.00"),
    _row("05/09/2025", "EMPLOYER MATCH CONTRIBUTION", "850.00"),
)


# ---------------------------------------------------------------------------
# Pure: reading the file (no DB)
# ---------------------------------------------------------------------------

def test_reads_only_the_rows_that_crossed_the_boundary():
    columns, matched, skipped = parse_history_csv(_MIXED_HISTORY)

    assert columns[0] == "Run Date"
    # Line 9 of the file as written: the BOM line, the blank line and the
    # header sit above the six data rows.
    assert matched[0].row_number == 9
    assert columns[-1] == "Settlement Date"
    assert [(r.date, r.kind, r.party, r.amount, r.tax_year) for r in matched] == [
        (date(2025, 3, 14), "contribution", "self", 6535.95, 2025),
        (date(2025, 4, 2), "distribution", "self", 6535.95, 2025),
        (date(2025, 4, 2), "contribution", "self", 1500.00, 2024),
        (date(2025, 5, 9), "contribution", "employer", 850.00, 2025),
    ]
    # The trade, the dividend, the reinvestment and the core-account sweep are
    # money that never left the account. The disclaimer is not a row at all.
    assert [s.action for s in skipped] == [
        _REINVESTMENT,
        _DIVIDEND,
        _SWEEP,
        "YOU BOUGHT BROAD MARKET INDEX FUND (XXXXX)",
        "YOU SOLD BROAD MARKET INDEX FUND (XXXXX)",
    ]
    assert {s.reason for s in skipped} == {"not a contribution or distribution"}


def test_a_paired_transfer_is_read_by_its_sign_not_its_wording():
    _, matched, _ = parse_history_csv(_history(
        _row("04/02/2025", "TRANSFERRED TO VS ROLLOVER IRA CURRENT CONTRIBUTION", "-6535.95"),
        _row("04/02/2025", "CASH CONTRIBUTION CURRENT YEAR", "6535.95"),
    ))

    # Both rows say "contribution"; only the positive one is one. Reading the
    # word would book the same 6,535.95 into the account twice.
    assert [(r.kind, r.amount) for r in matched] == [
        ("distribution", 6535.95),
        ("contribution", 6535.95),
    ]


def test_finds_the_header_under_a_title_line():
    _, matched, _ = parse_history_csv(_history(
        _row("03/14/2025", "CASH CONTRIBUTION CURRENT YEAR", "1000.00"),
        preamble="﻿\n\nBrokerage account activity report\n\n",
    ))

    assert len(matched) == 1
    assert matched[0].row_number == 6


def test_a_malformed_cell_is_told_apart_from_a_row_this_does_not_model():
    _, _, skipped = parse_history_csv(_history(
        _row("03/14/2025", "CASH CONTRIBUTION CURRENT YEAR", "n/a"),
        _row("not a date", "CASH CONTRIBUTION CURRENT YEAR", "1000.00"),
        _row("03/14/2025", _DIVIDEND, "12.00"),
    ))

    assert [s.reason for s in skipped] == [
        "could not read the amount",
        "could not read the date",
        "not a contribution or distribution",
    ]


def test_a_file_without_an_amount_column_names_what_is_missing():
    headerless = b"Run Date,Action,Symbol\n03/14/2025,CASH CONTRIBUTION CURRENT YEAR,\n"

    with pytest.raises(HTTPException) as exc:
        parse_history_csv(headerless)

    assert exc.value.status_code == 400
    assert "amount" in exc.value.detail


def test_a_thousands_separator_is_not_a_decimal_point():
    """`7,000` is seven thousand dollars. Read as `7.000` it becomes seven,
    and a deposit the feature exists to recognise is off by 1000x."""
    _headers, matched, _skipped = parse_history_csv(
        _history(_row("03/14/2025", "CASH CONTRIBUTION CURRENT YEAR", '"7,000"'))
    )

    assert [row.amount for row in matched] == [7000.00]


def test_a_decimal_comma_is_still_a_decimal_comma():
    """The grouping is what tells them apart, so a two-place comma survives."""
    _headers, matched, _skipped = parse_history_csv(
        _history(_row("03/14/2025", "CASH CONTRIBUTION CURRENT YEAR", '"1442,20"'))
    )

    assert [row.amount for row in matched] == [1442.20]


def test_a_row_the_file_names_no_account_for_is_not_merged_in():
    """In a file covering several accounts, whose an unattributed row is
    cannot be read — and merging it is the guess this refuses to make."""
    _headers, matched, skipped = parse_history_csv(
        _history(
            _row("03/14/2025", "CASH CONTRIBUTION CURRENT YEAR", "6535.95"),
            _row("02/13/2025", "Electronic Funds Transfer Received", "10000.00",
                 account="Individual - TOD"),
            _row("01/09/2025", "Electronic Funds Transfer Received", "3000.00", account=""),
        )
    )
    kept = _narrow_to_account(matched, skipped, "ROTH IRA")

    assert [row.amount for row in kept] == [6535.95]
    assert "names no account" in " ".join(s.reason for s in skipped)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def wallet(session: AsyncSession, test_user, test_workspace) -> uuid.UUID:
    """A wallet in the test workspace. Only its id is ever needed, and holding
    the instance would mean touching an expired object after the API's own
    session has committed underneath this one."""
    group = AssetGroup(
        id=uuid.uuid4(),
        user_id=test_user.id,
        workspace_id=test_workspace.id,
        name="Retirement Wallet",
        source="manual",
    )
    session.add(group)
    await session.commit()
    return group.id


def _upload(content: bytes, wallet: uuid.UUID, account: str | None = None) -> dict:
    return {
        "files": {"file": ("Accounts_History.csv", content, "text/csv")},
        "data": {"group_id": str(wallet), **({"account": account} if account else {})},
    }


#: What three of six real Fidelity downloads look like: one file, two accounts.
_TWO_ACCOUNT_HISTORY = _history(
    _row("03/14/2025", "CASH CONTRIBUTION CURRENT YEAR", "6535.95"),
    _row("03/06/2025", "TRANSFERRED TO VS ROLLOVER IRA CURRENT CONTRIBUTION", "-6535.95",
         account="Individual - TOD"),
    _row("02/13/2025", "Electronic Funds Transfer Received", "10000.00",
         account="Individual - TOD"),
)


@pytest.mark.asyncio
async def test_a_file_covering_two_accounts_asks_rather_than_merging(
    client, auth_headers, wallet
):
    """The preview names the accounts instead of guessing between them, so the
    panel can offer the choice without scraping it out of an error."""
    response = await client.post(
        "/api/contributions/import/preview",
        headers=auth_headers,
        **_upload(_TWO_ACCOUNT_HISTORY, wallet),
    )

    assert response.status_code == 200
    body = response.json()
    assert sorted(body["accounts"]) == ["Individual - TOD", "ROTH IRA"]
    assert body["matched"] == []
    assert [w["code"] for w in body["warnings"]] == ["choose_account"]


@pytest.mark.asyncio
async def test_importing_a_two_account_file_without_choosing_is_refused(
    client, auth_headers, session, wallet
):
    """An import writes into one Wallet. Filing the brokerage account's
    transfers under an IRA would put them against the wrong annual limit and
    the wrong withdrawable basis, so the write path refuses where the preview
    only asks."""
    response = await client.post(
        "/api/contributions/import",
        headers=auth_headers,
        **_upload(_TWO_ACCOUNT_HISTORY, wallet),
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "ROTH IRA" in detail and "Individual - TOD" in detail
    assert await _stored(session, wallet) == []


@pytest.mark.asyncio
async def test_naming_the_account_keeps_only_its_rows(
    client, auth_headers, session, wallet
):
    preview = await client.post(
        "/api/contributions/import/preview",
        headers=auth_headers,
        **_upload(_TWO_ACCOUNT_HISTORY, wallet, account="ROTH IRA"),
    )
    assert preview.status_code == 200
    body = preview.json()
    assert sorted(body["accounts"]) == ["Individual - TOD", "ROTH IRA"]
    assert [row["amount"] for row in body["matched"]] == [6535.95]
    assert any("Individual - TOD" in skip["reason"] for skip in body["skipped"])

    committed = await client.post(
        "/api/contributions/import",
        headers=auth_headers,
        **_upload(_TWO_ACCOUNT_HISTORY, wallet, account="ROTH IRA"),
    )
    assert committed.json()["created"] == 1
    assert [float(row.amount) for row in await _stored(session, wallet)] == [6535.95]


@pytest.mark.asyncio
async def test_a_single_account_file_needs_no_choice(client, auth_headers, wallet):
    response = await client.post(
        "/api/contributions/import/preview",
        headers=auth_headers,
        **_upload(_MIXED_HISTORY, wallet),
    )
    assert response.status_code == 200
    assert response.json()["accounts"] == ["ROTH IRA"]


async def _stored(session: AsyncSession, wallet: uuid.UUID) -> list[AssetContribution]:
    # The endpoints commit on their own session; this one has to let go of its
    # read snapshot before it can see them.
    await session.rollback()
    result = await session.execute(
        select(AssetContribution)
        .where(AssetContribution.group_id == wallet)
        .order_by(AssetContribution.date, AssetContribution.amount)
    )
    return list(result.scalars().all())


@pytest.mark.asyncio
async def test_preview_writes_nothing(client, auth_headers, session, wallet):
    response = await client.post(
        "/api/contributions/import/preview",
        headers=auth_headers,
        **_upload(_MIXED_HISTORY, wallet),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total_rows"] == 9
    assert len(body["matched"]) == 4
    assert len(body["skipped"]) == 5
    assert body["columns"][3] == "Action"
    assert await _stored(session, wallet) == []


@pytest.mark.asyncio
async def test_import_writes_the_contributions_and_can_be_undone(
    client, auth_headers, session, wallet
):
    response = await client.post(
        "/api/contributions/import", headers=auth_headers, **_upload(_MIXED_HISTORY, wallet)
    )

    assert response.status_code == 200
    body = response.json()
    assert (body["created"], body["duplicates"], body["skipped"]) == (4, 0, 5)

    rows = await _stored(session, wallet)
    assert [(r.kind, r.party, r.amount, r.tax_year) for r in rows] == [
        ("contribution", "self", Decimal("6535.95"), 2025),
        ("contribution", "self", Decimal("1500.00"), 2024),
        ("distribution", "self", Decimal("6535.95"), 2025),
        ("contribution", "employer", Decimal("850.00"), 2025),
    ]
    assert {r.source for r in rows} == {"import"}
    assert {str(r.import_id) for r in rows} == {body["import_id"]}

    undo = await client.delete(
        f"/api/import-logs/{body['import_id']}", headers=auth_headers
    )

    assert undo.status_code == 204
    assert await _stored(session, wallet) == []


@pytest.mark.asyncio
async def test_the_same_amount_for_two_tax_years_is_two_contributions(
    client, auth_headers, session, wallet
):
    """Between January and April 15 a wallet takes an identical amount on the
    same day for two different years. `tax_year` is the only thing telling
    them apart, so it has to be in the duplicate key."""
    prior = _history(_row("04/01/2026", "CASH CONTRIBUTION PRIOR YEAR", "7000.00"))
    current = _history(_row("04/01/2026", "CASH CONTRIBUTION CURRENT YEAR", "7000.00"))

    first = await client.post(
        "/api/contributions/import", headers=auth_headers, **_upload(prior, wallet)
    )
    second = await client.post(
        "/api/contributions/import", headers=auth_headers, **_upload(current, wallet)
    )

    assert first.json()["created"] == 1
    assert (second.json()["created"], second.json()["duplicates"]) == (1, 0)
    assert sorted(r.tax_year for r in await _stored(session, wallet)) == [2025, 2026]


@pytest.mark.asyncio
async def test_reimporting_the_same_file_creates_nothing(client, auth_headers, wallet):
    first = await client.post(
        "/api/contributions/import", headers=auth_headers, **_upload(_MIXED_HISTORY, wallet)
    )
    second = await client.post(
        "/api/contributions/import", headers=auth_headers, **_upload(_MIXED_HISTORY, wallet)
    )

    assert first.json()["created"] == 4
    assert second.json() == {
        "import_id": None, "created": 0, "duplicates": 4, "skipped": 5
    }


@pytest.mark.asyncio
async def test_two_identical_rows_in_one_file_are_two_contributions(
    client, auth_headers, session, wallet
):
    # ADR 0005: the fingerprint is content, and repeats are counted rather than
    # collapsed — someone really can pay the same amount in twice on one day.
    twice = _history(
        _row("03/14/2025", "CASH CONTRIBUTION CURRENT YEAR", "250.00"),
        _row("03/14/2025", "CASH CONTRIBUTION CURRENT YEAR", "250.00"),
    )

    first = await client.post(
        "/api/contributions/import", headers=auth_headers, **_upload(twice, wallet)
    )
    assert first.json()["created"] == 2
    assert len(await _stored(session, wallet)) == 2

    # And the second upload of that same pair adds neither.
    second = await client.post(
        "/api/contributions/import", headers=auth_headers, **_upload(twice, wallet)
    )
    assert (second.json()["created"], second.json()["duplicates"]) == (0, 2)


@pytest.mark.asyncio
async def test_one_stored_row_leaves_the_other_copy_importable(
    client, auth_headers, session, wallet
):
    once = _history(_row("03/14/2025", "CASH CONTRIBUTION CURRENT YEAR", "250.00"))
    twice = _history(
        _row("03/14/2025", "CASH CONTRIBUTION CURRENT YEAR", "250.00"),
        _row("03/14/2025", "CASH CONTRIBUTION CURRENT YEAR", "250.00"),
    )

    await client.post(
        "/api/contributions/import", headers=auth_headers, **_upload(once, wallet)
    )
    preview = await client.post(
        "/api/contributions/import/preview", headers=auth_headers, **_upload(twice, wallet)
    )
    second = await client.post(
        "/api/contributions/import", headers=auth_headers, **_upload(twice, wallet)
    )

    assert [r["duplicate"] for r in preview.json()["matched"]] == [True, False]
    assert (second.json()["created"], second.json()["duplicates"]) == (1, 1)
    assert len(await _stored(session, wallet)) == 2


@pytest.mark.asyncio
async def test_a_missing_column_is_a_400_naming_it(client, auth_headers, wallet):
    response = await client.post(
        "/api/contributions/import",
        headers=auth_headers,
        **_upload(b"Run Date,Action\n03/14/2025,CASH CONTRIBUTION CURRENT YEAR\n", wallet),
    )

    assert response.status_code == 400
    assert "amount" in response.json()["detail"]


@pytest.mark.asyncio
async def test_a_wallet_in_another_workspace_is_not_found(
    client, other_workspace_headers, session, wallet
):
    response = await client.post(
        "/api/contributions/import",
        headers=other_workspace_headers,
        **_upload(_MIXED_HISTORY, wallet),
    )

    assert response.status_code == 404
    assert await _stored(session, wallet) == []


@pytest.mark.asyncio
async def test_a_viewer_may_preview_but_not_import(client, viewer_auth_headers, wallet):
    preview = await client.post(
        "/api/contributions/import/preview",
        headers=viewer_auth_headers,
        **_upload(_MIXED_HISTORY, wallet),
    )
    commit = await client.post(
        "/api/contributions/import",
        headers=viewer_auth_headers,
        **_upload(_MIXED_HISTORY, wallet),
    )

    assert preview.status_code == 200
    assert commit.status_code == 403
