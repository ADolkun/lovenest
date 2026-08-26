import uuid
from datetime import date as _date

from pydantic import BaseModel


class ContributionImportRow(BaseModel):
    """One row of the file that will become a Contribution or a Distribution."""

    row_number: int
    date: _date | None
    tax_year: int | None
    kind: str | None  # contribution | distribution
    party: str  # self | employer
    amount: float | None  # always positive; `kind` carries the sign
    #: The raw action text, so the reader can see *why* the row was read this
    #: way. Classification is a guess at prose, and a guess has to be checkable.
    action: str
    #: The account the broker filed this row under, where the file says. One
    #: export routinely covers several accounts, and a row's account decides
    #: whether it belongs in the wallet being imported into at all.
    account: str | None = None
    #: Already stored against this wallet, so it will not be written again.
    duplicate: bool = False


class ContributionImportSkip(BaseModel):
    row_number: int
    action: str
    #: A sentence, not a code: the reasons are all things the user can act on,
    #: and "not a contribution or distribution" needs no translation table.
    reason: str


class ContributionImportPreview(BaseModel):
    columns: list[str] = []
    #: Every account named in the file, so the caller can say which one this
    #: wallet is. Empty when the file has no account column.
    accounts: list[str] = []
    total_rows: int = 0
    matched: list[ContributionImportRow] = []
    skipped: list[ContributionImportSkip] = []
    warnings: list[str] = []


class ContributionImportResult(BaseModel):
    import_id: uuid.UUID | None
    created: int
    duplicates: int
    skipped: int
