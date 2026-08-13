"""Reading the account-allowlist keys out of a connection's settings blob.

`BankConnection.settings` is one untyped JSON column, and three of its keys —
`account_allowlist`, `seen_account_ids`, `reviewed_account_ids` — only mean
anything together. They are read from both the schema layer (the connection
read model derives a pending count) and the service layer (account discovery
derives a per-account status), so the rules live here rather than in either,
and the two cannot drift apart.
"""

from typing import Iterable, Literal, Optional

AccountStatus = Literal["included", "excluded", "pending"]


def allowlist_ids(settings: Optional[dict]) -> Optional[set[str]]:
    """Read the tri-state `account_allowlist`.

    The difference between the first two states is the compatibility contract:

    - absent: sync every account the provider returns (what every connection
      did before this setting existed), signalled by None;
    - present: sync only the listed provider account ids;
    - present and empty: sync nothing. A valid state, not an error.

    A non-list value reads as absent — a malformed setting must not silently
    stop a connection from syncing.
    """
    raw = (settings or {}).get("account_allowlist")
    if not isinstance(raw, list):
        return None
    return {str(item) for item in raw}


def seen_account_ids(settings: Optional[dict]) -> set[str]:
    """Every provider account id the last sync saw, before the allowlist filter."""
    return {str(item) for item in (settings or {}).get("seen_account_ids") or []}


def known_account_ids(
    settings: Optional[dict], imported_ids: Iterable[str] = ()
) -> set[str]:
    """The accounts the user has already had a chance to see.

    The pinned `reviewed_account_ids` when there is one. Without it — an
    allowlist configured before that set was pinned — sync's rolling record and
    the connection's own account rows are the best evidence left, and both err
    towards excluded, the quieter of the two wrong answers for an account the
    user did uncheck. That fallback also makes the pending count 0 rather than
    an approximation, since it can never be missing a seen id.
    """
    reviewed = (settings or {}).get("reviewed_account_ids")
    if isinstance(reviewed, list):
        return {str(item) for item in reviewed}
    return seen_account_ids(settings) | {str(item) for item in imported_ids}


def account_status(
    settings: Optional[dict], external_id: str, known: set[str]
) -> AccountStatus:
    """Classify one provider account against the allowlist.

    Derived at read time, never stored, so it cannot drift from the allowlist
    it describes.
    """
    allowlist = allowlist_ids(settings)
    if allowlist is None or external_id in allowlist:
        return "included"
    return "excluded" if external_id in known else "pending"
