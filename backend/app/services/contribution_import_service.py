"""Import Contributions and Distributions from a brokerage account history.

An *account* history is not an order history. The same download carries the
buys, the dividends, the sweeps into the core account and — a dozen rows in
between — the money that actually crossed the account's boundary. Which is
which is `contribution_service.classify_flow`'s reading, not this module's;
the job here is to find the rows in a file a broker did not lay out for us,
and to not write the same one twice.
"""

import csv
import io
import re
import uuid
from collections import Counter
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.asset_contribution import AssetContribution
from app.models.import_log import ImportLog
from app.schemas.contribution_import import (
    ContributionImportPreview,
    ContributionImportResult,
    ContributionImportRow,
    ContributionImportSkip,
    ContributionImportWarning,
)
from app.services import contribution_service
from app.services.asset_import_service import (
    _auto_mapping,
    _date_formats,
    _decode,
    _header_index,
    _parse_date,
    _parse_decimal,
)
from app.services.contribution_service import classify_flow, classify_party, tax_year_for
from app.services.import_service import _sniff_csv_dialect

_CENTS = Decimal("0.01")

#: What `_decode` strips off the front, in bytes. Counting the lines it took
#: keeps `row_number` pointing at the line the user will find in their own
#: file rather than at the line left after the preamble was dropped.
_PREAMBLE = re.compile(rb"^(?:\xef\xbb\xbf|[\r\n \t])*")

#: The three columns this import needs, and every spelling of them seen in the
#: wild. Order matters: the first candidate that matches an unclaimed header
#: wins, so a Fidelity file's `Run Date` is the date even though `Settlement
#: Date` would also answer, and its `Action` is the action even though
#: `Description` and `Type` sit in the same file.
_COLUMN_CANDIDATES = {
    "date": ("run date", "date", "trade date", "transaction date", "settlement date"),
    "action": (
        "action",
        "description",
        "transaction description",
        "type",
        "transaction type",
        "activity",
    ),
    "amount": ("amount", "net amount", "net cash amount", "cash amount"),
}

#: Not required, and read for a different purpose. One export routinely covers
#: several of the user's accounts — three of six real Fidelity downloads do —
#: while an import writes into exactly one Wallet. Without this column every
#: taxable-brokerage transfer in such a file would be filed as an IRA
#: contribution, against the wrong annual limit and the wrong withdrawable
#: basis.
_ACCOUNT_CANDIDATES = ("account", "account name", "account type", "account number")


def _account_column(headers: list[str], mapping: dict[str, str]) -> Optional[str]:
    """The account header, if the file has one the required columns did not
    already claim."""
    index = _header_index(headers)
    claimed = set(mapping.values())
    for candidate in _ACCOUNT_CANDIDATES:
        header = index.get(candidate)
        if header is not None and header not in claimed:
            return header
    return None


def _find_header(rows: list[list[str]]) -> tuple[int, list[str], dict[str, str]]:
    """The header row, wherever the broker put it.

    Fidelity's export opens with a BOM line and a blank line and signs off with
    several paragraphs of disclaimer, so the header is neither the first line
    nor at a fixed offset — the next broker pads by a different amount. The row
    that names all three required columns *is* the header; nothing else has to
    be assumed about the shape of the file.
    """
    best: dict[str, str] = {}
    for position, row in enumerate(rows):
        headers = [cell.strip() for cell in row]
        mapping = _auto_mapping(headers, _COLUMN_CANDIDATES)
        if len(mapping) == len(_COLUMN_CANDIDATES):
            return position, headers, mapping
        if len(mapping) > len(best):
            best = mapping
    missing = [field for field in _COLUMN_CANDIDATES if field not in best]
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"The file has no {' or '.join(missing)} column",
    )


def _crosses_boundary(action: str) -> bool:
    """Whether the action text alone would have made this a contribution.

    Asked by handing the classifier an amount it cannot object to, so the
    phrase lists stay in one place. It separates "this row is a dividend" from
    "this row is a contribution whose amount cell is unreadable" — the first is
    the file working as intended, the second is something to go and look at.
    """
    return classify_flow(action, Decimal("1")) is not None


def parse_history_csv(
    content: bytes, date_format: Optional[str] = None
) -> tuple[list[str], list[ContributionImportRow], list[ContributionImportSkip]]:
    """Read an account history into the rows that cross the boundary, plus the
    rows that do not and why."""
    text = _decode(content)
    rows = list(csv.reader(io.StringIO(text), dialect=_sniff_csv_dialect(text)))
    header_at, headers, mapping = _find_header(rows)
    positions = {field: headers.index(header) for field, header in mapping.items()}
    account_column = _account_column(headers, mapping)
    account_at = headers.index(account_column) if account_column else None

    # A row with fewer cells than the header is the disclaimer the export signs
    # off with, or a blank line. Reporting those as skipped rows would bury the
    # ones the user can act on, so they are not rows at all.
    first_line = header_at + 2 + _PREAMBLE.match(content).group().count(b"\n")
    data = [
        (first_line + offset, row)
        for offset, row in enumerate(rows[header_at + 1:])
        if len(row) >= len(headers)
    ]

    formats = _date_formats(date_format, [row[positions["date"]] for _, row in data])

    matched: list[ContributionImportRow] = []
    skipped: list[ContributionImportSkip] = []
    for number, row in data:
        cells = {field: row[position].strip() for field, position in positions.items()}
        action = cells["action"]
        amount = _parse_decimal(cells["amount"])

        kind = classify_flow(action, amount)
        if kind is None:
            skipped.append(ContributionImportSkip(
                row_number=number,
                action=action,
                reason=(
                    "could not read the amount"
                    if amount is None and _crosses_boundary(action)
                    else "not a contribution or distribution"
                ),
            ))
            continue

        when = _parse_date(cells["date"], formats)
        if when is None:
            skipped.append(ContributionImportSkip(
                row_number=number, action=action, reason="could not read the date"
            ))
            continue

        matched.append(ContributionImportRow(
            row_number=number,
            date=when,
            tax_year=tax_year_for(action, when),
            kind=kind,
            # A distribution has no party — it is the account paying out,
            # whoever put the money there — and the table's check constraint
            # says as much. "EMPLOYER CONTRIBUTION" on the paying-out side of a
            # transfer would otherwise be refused by the database.
            party=classify_party(action) if kind == "contribution" else "self",
            amount=float(abs(amount).quantize(_CENTS, rounding=ROUND_HALF_UP)),
            action=action,
            account=row[account_at].strip() or None if account_at is not None else None,
        ))
    return headers, matched, skipped


def _accounts_in(matched: list[ContributionImportRow]) -> list[str]:
    return sorted({row.account for row in matched if row.account})


def _narrow_to_account(
    matched: list[ContributionImportRow],
    skipped: list[ContributionImportSkip],
    account: Optional[str],
) -> list[ContributionImportRow]:
    """Keep only the rows belonging to the account being imported.

    A file covering more than one account and no account named is refused
    rather than merged: an import writes into one Wallet, and filing another
    account's transfers under it would put money against the wrong annual
    limit and the wrong withdrawable basis. Refusing costs the user one
    dropdown; guessing costs them a figure they would believe.
    """
    found = _accounts_in(matched)
    wanted = (account or "").strip()
    if not wanted:
        if len(found) > 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "This file covers more than one account "
                    f"({', '.join(found)}). Choose which one this wallet is."
                ),
            )
        return matched

    kept = []
    for row in matched:
        if row.account is None or row.account.casefold() == wanted.casefold():
            kept.append(row)
        else:
            skipped.append(ContributionImportSkip(
                row_number=row.row_number,
                action=row.action,
                reason=f"belongs to another account ({row.account})",
            ))
    return kept


def _amount(row: ContributionImportRow) -> Decimal:
    return Decimal(str(row.amount))


def _fingerprint(kind: str, party: str, amount: Decimal, when) -> tuple:
    """What makes two contributions the same contribution (ADR 0005).

    The wallet is not in the key because the count is only ever taken within
    one wallet.
    """
    return (kind, party, amount.quantize(_CENTS, rounding=ROUND_HALF_UP), when)


async def _already_imported(
    session: AsyncSession, workspace_id: uuid.UUID, group_id: uuid.UUID
) -> Counter:
    rows = (
        await session.execute(
            select(AssetContribution).where(
                AssetContribution.group_id == group_id,
                AssetContribution.workspace_id == workspace_id,
            )
        )
    ).scalars().all()
    return Counter(
        _fingerprint(row.kind, row.party, Decimal(str(row.amount)), row.date) for row in rows
    )


def _mark_duplicates(matched: list[ContributionImportRow], stored: Counter) -> int:
    """Flag the rows the wallet already holds, counting repeats (ADR 0005).

    A matched row consumes one stored occurrence rather than merely testing for
    one, and an accepted row is deliberately *not* added back: two identical
    contributions in one file are two real contributions, and a file holding
    that pair imports both the first time and neither the second.
    """
    duplicates = 0
    for row in matched:
        key = _fingerprint(row.kind, row.party, _amount(row), row.date)
        if stored[key]:
            stored[key] -= 1
            row.duplicate = True
            duplicates += 1
    return duplicates


async def preview(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    content: bytes,
    *,
    group_id: uuid.UUID,
    account: Optional[str] = None,
    date_format: Optional[str] = None,
) -> ContributionImportPreview:
    """What importing this file would do, without doing any of it."""
    await contribution_service._require_group(session, group_id, workspace_id)
    columns, matched, skipped = parse_history_csv(content, date_format)
    accounts = _accounts_in(matched)

    # A preview of a multi-account file asks for the account rather than
    # refusing: the accounts are in the response, so the panel can offer them.
    # The write path still refuses — that is where a wrong guess costs money.
    warnings = []
    if not (account or "").strip() and len(accounts) > 1:
        matched = []
        warnings.append(ContributionImportWarning(code="choose_account", count=len(accounts)))
    else:
        matched = _narrow_to_account(matched, skipped, account)
        _mark_duplicates(matched, await _already_imported(session, workspace_id, group_id))
        if not matched:
            warnings.append(ContributionImportWarning(code="no_rows"))

    return ContributionImportPreview(
        columns=columns,
        accounts=accounts,
        total_rows=len(matched) + len(skipped),
        matched=matched,
        skipped=skipped,
        warnings=warnings,
    )


async def import_contributions(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    content: bytes,
    *,
    group_id: uuid.UUID,
    account: Optional[str] = None,
    filename: Optional[str] = None,
    date_format: Optional[str] = None,
) -> ContributionImportResult:
    """Write the file's contributions to one wallet, once."""
    await contribution_service._require_group(session, group_id, workspace_id)
    _columns, matched, skipped = parse_history_csv(content, date_format)
    matched = _narrow_to_account(matched, skipped, account)
    duplicates = _mark_duplicates(matched, await _already_imported(session, workspace_id, group_id))
    fresh = [row for row in matched if not row.duplicate]

    if not fresh:
        # No log for an import that wrote nothing: the history list is a list of
        # undoable things, and this one has nothing to take back.
        return ContributionImportResult(
            import_id=None, created=0, duplicates=duplicates, skipped=len(skipped)
        )

    log = ImportLog(
        user_id=user_id,
        workspace_id=workspace_id,
        account_id=None,
        entity="asset_contributions",
        filename=filename or "contributions.csv",
        format="csv",
        transaction_count=len(fresh),
        total_credit=sum((_amount(r) for r in fresh if r.kind == "contribution"), Decimal("0")),
        total_debit=sum((_amount(r) for r in fresh if r.kind == "distribution"), Decimal("0")),
    )
    session.add(log)
    await session.flush()

    for row in fresh:
        session.add(AssetContribution(
            workspace_id=workspace_id,
            group_id=group_id,
            kind=row.kind,
            party=row.party,
            amount=_amount(row),
            date=row.date,
            tax_year=row.tax_year,
            source="import",
            import_id=log.id,
        ))
    await session.commit()

    return ContributionImportResult(
        import_id=log.id, created=len(fresh), duplicates=duplicates, skipped=len(skipped)
    )
