"""Shared transaction semantics used by matching and P&L queries."""

import re

from sqlalchemy import and_, func, or_, select

from app.models.account import Account
from app.models.transaction import Transaction


_BANK_DEBIT_CARD_PAYMENT_MARKERS = (
    ("CHASE CREDIT CRD", "AUTOPAY"),
    ("CAPITAL ONE", "CRCARDPMT"),
    ("BANK OF AMERICA", "PAYMENT"),
    ("BK OF AMER VISA", "PMT"),
    ("AMERICAN EXPRESS", "ACH PMT"),
    ("APPLECARD GSBANK", "PAYMENT"),
    ("WELLS FARGO CARD", "CCPYMT"),
    ("WF CREDIT CARD", "AUTO PAY"),
)

_CARD_CREDIT_PAYMENT_MARKERS = (
    ("ACH DEPOSIT INTERNET TRANSFER FROM ACCOUNT",),
    ("AUTOMATIC PAYMENT",),
    ("AUTOPAY PAYMENT",),
    ("AUTOPAY PYMT",),
    ("BA ELECTRONIC PAYMENT",),
    ("CAPITAL ONE MOBILE PYMT",),
    ("ONLINE/MOBILE PAYMENT",),
    ("ONLINE/MOBILE RECURRING",),
    ("PENDING PAYMENT",),
)


def _matches_markers(description: str, marker_groups: tuple[tuple[str, ...], ...]) -> bool:
    normalized = description.upper()
    return any(all(marker in normalized for marker in group) for group in marker_groups)


def _is_bilt_card_payment(description: str) -> bool:
    normalized = description.upper()
    return (
        "BILT CARD" in normalized
        and "PMT" in normalized
        and "HOUSING" not in normalized
        and "RENT" not in normalized
    )


def is_credit_card_payment(description: str, txn_type: str, account_type: str) -> bool:
    """Recognize card payments without confusing card purchases that mention payment."""
    if account_type == "credit_card":
        return txn_type == "credit" and (
            _matches_markers(description, _CARD_CREDIT_PAYMENT_MARKERS)
            or description.strip().upper() == "BILL PAYMENT"
        )
    return txn_type == "debit" and (
        _matches_markers(description, _BANK_DEBIT_CARD_PAYMENT_MARKERS)
        or _is_bilt_card_payment(description)
        or description.strip().upper() == "CAPITAL ONE"
    )


def is_credit_card_payment_pair(
    debit_description: str,
    debit_account_type: str,
    credit_description: str,
    credit_account_type: str,
    debit_account_name: str = "",
    credit_account_name: str = "",
) -> bool:
    """Recognize a payment only when both sides provide compatible evidence."""
    debit_payment = is_credit_card_payment(
        debit_description, "debit", debit_account_type
    )
    credit_payment = is_credit_card_payment(
        credit_description, "credit", credit_account_type
    )
    if debit_payment and credit_payment:
        debit_issuer = credit_card_issuer(debit_description, debit_account_name)
        credit_issuer = credit_card_issuer(credit_description, credit_account_name)
        return not (debit_issuer and credit_issuer) or debit_issuer == credit_issuer

    return (
        debit_account_type != "credit_card"
        and debit_description.strip().upper() == "CAPITAL ONE"
        and credit_account_type == "credit_card"
        and "CAPITAL ONE MOBILE PYMT" in credit_description.upper()
        and credit_card_issuer(credit_description, credit_account_name) == "capital_one"
    )


def credit_card_issuer(description: str, account_name: str = "") -> str | None:
    """Return an issuer only when the description or account name identifies one."""
    normalized = f"{description} {account_name}".upper()
    markers = (
        ("capital_one", ("CAPITAL ONE", "VENTURE X")),
        ("chase", ("CHASE CREDIT", "AMAZON PRIME REWARDS", "CHASE CARD")),
        ("bank_of_america", ("BANK OF AMERICA", "BK OF AMER", "BA ELECTRONIC")),
        ("american_express", ("AMERICAN EXPRESS", "AMEX", "BONVOY")),
        ("apple", ("APPLECARD", "APPLE CARD")),
        ("wells_fargo", ("WELLS FARGO", "WF CREDIT CARD", "BILT")),
    )
    return next(
        (issuer for issuer, issuer_markers in markers if any(m in normalized for m in issuer_markers)),
        None,
    )


def credit_card_payment_filter():
    """SQL predicate equivalent of :func:`is_credit_card_payment`."""
    account_is_card = Transaction.account_id.in_(
        select(Account.id).where(Account.type == "credit_card")
    )

    def marker_filter(groups: tuple[tuple[str, ...], ...]):
        return or_(
            *(
                and_(*(Transaction.description.ilike(f"%{marker}%") for marker in group))
                for group in groups
            )
        )

    return or_(
        and_(
            account_is_card,
            Transaction.type == "credit",
            or_(
                marker_filter(_CARD_CREDIT_PAYMENT_MARKERS),
                func.upper(func.trim(Transaction.description)) == "BILL PAYMENT",
            ),
        ),
        and_(
            ~account_is_card,
            Transaction.type == "debit",
            or_(
                marker_filter(_BANK_DEBIT_CARD_PAYMENT_MARKERS),
                func.upper(func.trim(Transaction.description)) == "CAPITAL ONE",
                and_(
                    Transaction.description.ilike("%BILT CARD%"),
                    Transaction.description.ilike("%PMT%"),
                    ~Transaction.description.ilike("%HOUSING%"),
                    ~Transaction.description.ilike("%RENT%"),
                ),
            ),
        ),
    )


def credit_card_refund_filter():
    """SQL predicate for card credits that are refunds, not repayments."""
    account_is_card = Transaction.account_id.in_(
        select(Account.id).where(Account.type == "credit_card")
    )
    return and_(
        account_is_card,
        Transaction.type == "credit",
        ~credit_card_payment_filter(),
    )


def transfer_provider(description: str) -> str | None:
    """Identify the provider/family for a non-card transfer description."""
    normalized = description.upper()
    providers = (
        ("zelle", ("ZELLE",)),
        ("venmo", ("VENMO",)),
        ("cash_app", ("CASH APP", "CASHAPP")),
        ("acorns", ("ACORNS",)),
        ("coinbase", ("COINBASE",)),
        ("moneyline", ("MONEYLINE",)),
    )
    for provider, markers in providers:
        if any(marker in normalized for marker in markers):
            return provider
    if normalized.strip() == "DEPOSIT" or "DAILY CASH ADJUSTMENT" in normalized:
        return "cash_reward"
    if any(marker in normalized for marker in ("TRANSFER", "WIRE TYPE")):
        return "bank_transfer"
    return None


def has_matching_transfer_reference(first: str, second: str) -> bool:
    """Require a provider reference shared by both transfer descriptions."""
    def references(value: str) -> set[str]:
        normalized = value.upper()
        patterns = (
            r"\bJPM([A-Z0-9]{8,})\b",
            r"\bBAC([A-Z0-9]{8,})\b",
            r"\bCONF(?:IRMATION)?\s*#?\s*([A-Z0-9]{8,})\b",
            r"\bREF(?:ERENCE)?\s*#?\s*([A-Z0-9]{5,})\b",
            r"\bTRACE\s*#?\s*([A-Z0-9]{8,})\b",
            r"\bID\s*#?\s*([A-Z0-9]{8,})\b",
        )
        return {
            match.group(1)
            for pattern in patterns
            for match in re.finditer(pattern, normalized)
        }

    first_refs = references(first)
    second_refs = references(second)
    return any(
        left == right or left.endswith(right) or right.endswith(left)
        for left in first_refs
        for right in second_refs
    )


def is_compatible_non_card_transfer(
    first: str,
    second: str,
    first_account_name: str = "",
    second_account_name: str = "",
) -> bool:
    """Match non-card transfers only with provider and reference evidence."""
    provider = transfer_provider(first)
    if provider is None or provider != transfer_provider(second):
        return False
    if provider == "cash_reward":
        descriptions = {first.strip().upper(), second.strip().upper()}
        apple_account = "APPLE CASH" in f"{first_account_name} {second_account_name}".upper()
        return apple_account and (
            descriptions == {"DEPOSIT"}
            or descriptions == {"DEPOSIT", "DAILY CASH ADJUSTMENT"}
        )
    return has_matching_transfer_reference(first, second)
