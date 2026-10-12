"""Lovenest — Tax & Planning companion app.

Read-only FastAPI sidecar that adds tax/planning tools to Lovenest.
Tax math lives in tax_engine (owned elsewhere); this module only wires HTTP
endpoints and read-only Postgres reporting for the write-off tab.
"""

import json
import os
import re
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import date, timedelta

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import tax_engine

PORT = int(os.environ.get("PORT", "8088"))
DATABASE_URL = os.environ.get("DATABASE_URL", "")
HUB_WORKSPACE_ID = os.environ.get("HUB_WORKSPACE_ID", "")
INVESTMENT_WORKSPACE_ID = os.environ.get("INVESTMENT_WORKSPACE_ID", "")
BACKEND_URL = os.environ.get("BACKEND_URL", "http://backend:8000").rstrip("/")

# Public sub-path the reverse proxy mounts this app under (it strips the prefix
# before forwarding, so backend routes stay unprefixed). Injected into the page
# as <base> so the SPA's relative asset/API URLs resolve under it. "/" serves
# from the root unchanged for direct :8088 access.
BASE_PATH = "/" + os.environ.get("BASE_PATH", "/tax").strip("/")
BASE_HREF = BASE_PATH if BASE_PATH.endswith("/") else BASE_PATH + "/"

YTD_START = "2026-01-01"
HERE = os.path.dirname(os.path.abspath(__file__))
INDEX_HTML = os.path.join(HERE, "index.html")

# Tag matched in transaction notes free-text. Captures the full tag including
# any subtag, e.g. "#ded", "#ded-charity", "#ded-salt". The trailing lookahead
# requires a token boundary so "#dedicated" / "#deductible" don't match as a
# bare "#ded".
DED_TAG_RE = re.compile(r"#ded(?:-[a-z0-9_]+)?(?![a-z0-9_-])", re.IGNORECASE)

# Copied from backend/app/services/transaction_semantics.py, which decides which
# rows repay a card rather than buy or refund something; test_app_ledger fails
# when the two drift apart.
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
_LEDGER_PARAMS = {
    **{
        f"card_pmt_{i}": [f"%{marker}%" for marker in group]
        for i, group in enumerate(_CARD_CREDIT_PAYMENT_MARKERS)
    },
    **{
        f"bank_pmt_{i}": [f"%{marker}%" for marker in group]
        for i, group in enumerate(_BANK_DEBIT_CARD_PAYMENT_MARKERS)
    },
}


def _matches_any(t, prefix):
    return " OR ".join(
        f"{t}.description ILIKE ALL (%({key})s)" for key in _LEDGER_PARAMS if key.startswith(prefix)
    )


# Ledger query fragments over a transaction alias, its account and its
# category (LEFT JOINed), mirroring backend/app/services/_query_filters.py.
def _card_payment_sql(t, a):
    return f"""(
    ({a}.type = 'credit_card' AND {t}.type = 'credit'
     AND ({_matches_any(t, "card_pmt_")} OR UPPER(TRIM({t}.description)) = 'BILL PAYMENT'))
    OR ({a}.type <> 'credit_card' AND {t}.type = 'debit' AND (
        {_matches_any(t, "bank_pmt_")}
        OR UPPER(TRIM({t}.description)) = 'CAPITAL ONE'
        OR ({t}.description ILIKE '%%BILT CARD%%' AND {t}.description ILIKE '%%PMT%%'
            AND {t}.description NOT ILIKE '%%HOUSING%%' AND {t}.description NOT ILIKE '%%RENT%%')
    ))
)"""


def _counts_as_user_pnl_sql(t, a, c):
    """`counts_as_user_pnl`: not a matched transfer, ignored, excluded, a card
    payment or a settlement, and not filed under an ignored or transfer-like
    category, except that a card refund still counts under the latter."""
    return f"""(
    NOT EXISTS (
        SELECT 1 FROM transactions {t}_pair
        WHERE {t}_pair.transfer_pair_id = {t}.transfer_pair_id AND {t}_pair.id <> {t}.id
    )
    AND {t}.is_ignored = false
    AND NOT {_card_payment_sql(t, a)}
    AND {t}.exclude_from_pnl = false
    AND {t}.source <> 'settlement'
    AND ({t}.category_id IS NULL OR (
        {c}.is_ignored = false
        AND ({c}.treat_as_transfer = false OR ({a}.type = 'credit_card' AND {t}.type = 'credit'))
    ))
)"""


_CARD_PAYMENT_SQL = _card_payment_sql("t", "a")

# ADR 0011: a credit filed under a non-income category, or an uncategorized
# card refund, is money back on spending rather than income.
_SPENDING_OFFSET_SQL = f"""(
    t.type = 'credit' AND NOT {_CARD_PAYMENT_SQL} AND (
        (t.category_id IS NOT NULL AND c.is_income = false)
        OR (t.category_id IS NULL AND a.type = 'credit_card')
    )
)"""


# ADR 0012: a link is in effect only while both of its rows are in user P&L on
# open accounts. The backend checks only the row's own account, since every
# report there already drops a row outside user P&L; write-offs keep such
# rows, so here both sides are checked in full.
def _link_side_counts_sql(t, a, c):
    return f"({_counts_as_user_pnl_sql(t, a, c)} AND {a}.is_closed = false)"


_LINKS_JOIN = f"""
    LEFT JOIN LATERAL (
        SELECT SUM(r.amount) AS amount
        FROM transaction_reimbursements r
        JOIN transactions o ON o.id = CASE
            WHEN r.reimbursement_id = t.id THEN r.expense_id ELSE r.reimbursement_id
        END
        JOIN accounts oa ON oa.id = o.account_id
        LEFT JOIN categories oc ON oc.id = o.category_id
        WHERE t.id IN (r.reimbursement_id, r.expense_id)
          AND {_link_side_counts_sql("o", "oa", "oc")}
          AND {_link_side_counts_sql("t", "a", "c")}
    ) linked ON true
"""

# A link records how much of one row repaid another, so each side counts only
# for the part no link explains.
_PNL_AMOUNT_SQL = """(
    ABS(COALESCE(t.amount_primary, t.amount))
    * (1 - LEAST(1, COALESCE(linked.amount / NULLIF(ABS(t.amount), 0), 0)))
)"""

app = FastAPI(title="Lovenest — Tax & Planning")
app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")


def _ytd_end():
    """Exclusive upper bound that advances daily in the configured timezone."""
    return min(date.today() + timedelta(days=1), date(2027, 1, 1)).isoformat()


@contextmanager
def db_conn():
    """Yield a short-lived read-only psycopg connection.

    Driver is imported lazily so the app still boots (and /health works) when
    psycopg or the DB are unavailable. asyncpg-style URLs from the core Lovenest/Securo compose
    (postgresql+asyncpg://) are normalized to a libpq DSN psycopg understands.
    """
    import psycopg

    dsn = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
    conn = psycopg.connect(dsn, connect_timeout=5)
    try:
        conn.read_only = True
        yield conn
    finally:
        conn.close()


def _allowed_workspace_ids():
    """Workspaces this planner may be used from. Read at call time so a
    redeploy-free config change (and tests) see the current value."""
    return {w for w in (HUB_WORKSPACE_ID, INVESTMENT_WORKSPACE_ID) if w}


def _workspace_clause(table_alias, workspace_id):
    """SQL fragment + params restricting rows to the workspace the caller
    selected — never to a configured constant.

    More than one workspace is accepted now, and require_auth only verifies
    membership of the one in the request. Pinning to a constant would serve the
    hub's ledger to someone signed in to another accepted workspace. An empty
    id matches no row, so a missing header fails closed.
    """
    return f" AND {table_alias}.workspace_id = %(ws)s", {"ws": workspace_id}


@app.get("/")
def index():
    with open(INDEX_HTML, encoding="utf-8") as fh:
        html = fh.read()
    return HTMLResponse(
        html.replace("{{BASE_HREF}}", BASE_HREF),
        headers={
            "Content-Security-Policy": (
                "default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
                "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                "font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; "
                "connect-src 'self'; base-uri 'self'; frame-ancestors 'none'"
            )
        },
    )


@app.get("/health")
def health():
    return {"status": "ok"}


def require_auth(
    authorization: str = Header(default=""),
    workspace_id: str = Header(default="", alias="X-Workspace-Id"),
):
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Sign in to Lovenest first")
    allowed = _allowed_workspace_ids()
    if allowed and workspace_id not in allowed:
        raise HTTPException(status_code=403, detail="Select a workspace this planner is configured for")
    headers = {
        "Authorization": authorization,
        **({"X-Workspace-Id": workspace_id} if workspace_id else {}),
    }
    request = urllib.request.Request(
        f"{BACKEND_URL}/api/workspaces/current" if allowed else f"{BACKEND_URL}/api/users/me",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            if response.status != 200:
                raise HTTPException(status_code=401, detail="Invalid Lovenest session")
    except urllib.error.HTTPError as exc:
        raise HTTPException(status_code=401, detail="Invalid Lovenest session") from exc
    except urllib.error.URLError as exc:
        raise HTTPException(status_code=503, detail="Lovenest authentication unavailable") from exc
    return headers


@app.post("/api/tax/estimate")
def tax_estimate(body: dict, _auth=Depends(require_auth)):
    return _calculate(tax_engine.estimate_taxes, body)


@app.post("/api/paycheck")
def paycheck(body: dict, _auth=Depends(require_auth)):
    return _calculate(tax_engine.paycheck, body)


@app.post("/api/retirement-plan")
def retirement_plan(body: dict, _auth=Depends(require_auth)):
    return _calculate(tax_engine.plan_contributions, body)


@app.post("/api/projection")
def projection(body: dict, _auth=Depends(require_auth)):
    return _calculate(tax_engine.project_retirement, body)


def _calculate(fn, body):
    try:
        return fn(body)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _net_groups(rows, key_name):
    """Group signed (key, amount, item) rows, largest net first.

    A group netting to zero or less is dropped, as in the dashboard's category
    breakdown: a refund landing a year after its expense is no deduction now
    and must not shrink the other groups.
    """
    groups = {}
    for key, amount, item in rows:
        group = groups.setdefault(key, {"total": 0.0, "count": 0, "items": []})
        group["total"] += amount
        group["count"] += 1
        group["items"].append({**item, "amount": round(amount, 2)})
    return sorted(
        (
            {key_name: key, "total": round(g["total"], 2), "count": g["count"], "items": g["items"]}
            for key, g in groups.items()
            if round(g["total"], 2) > 0
        ),
        key=lambda x: x["total"],
        reverse=True,
    )


@app.get("/api/writeoffs")
def writeoffs(auth=Depends(require_auth)):
    """YTD-2026 deductible totals from the Lovenest ledger (read-only).

    (a) debits in any category under the "Business / Schedule C" group,
        grouped by category name;
    (b) debits whose notes contain a #ded tag, grouped by the exact tag.
    Only posted, non-ignored rows dated from 2026-01-01. A spending-offset
    credit (refund, reimbursement) filed in the same category or carrying the
    same tag nets against it, and every row counts only for the part no
    reimbursement link covers.

    The category or tag is the user's statement that a row is deductible, so
    the user P&L exclusions (transfers, transfer-like categories,
    exclude_from_pnl, settlements) deliberately do not apply: a #ded-tagged
    fee filed under a transfer category is still a deduction.
    """
    empty = {
        "schedule_c": [],
        "deduction_tags": [],
        "totals": {"schedule_c_total": 0.0, "ded_total": 0.0},
    }

    if not DATABASE_URL:
        empty["error"] = "DATABASE_URL not configured"
        return empty
    if not _allowed_workspace_ids():
        empty["error"] = "no workspace configured — set HUB_WORKSPACE_ID or INVESTMENT_WORKSPACE_ID"
        return empty

    ws_sql, ws_params = _workspace_clause("t", auth.get("X-Workspace-Id", ""))
    params = {"start": YTD_START, "end": _ytd_end(), **ws_params, **_LEDGER_PARAMS}
    signed_amount = f"(CASE WHEN t.type = 'debit' THEN 1 ELSE -1 END * {_PNL_AMOUNT_SQL})::float8"

    try:
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute(f"""
                SELECT c.name, t.date, t.description, {signed_amount}
                FROM transactions t
                JOIN accounts a ON a.id = t.account_id
                JOIN categories c ON c.id = t.category_id
                JOIN category_groups g ON g.id = c.group_id
                {_LINKS_JOIN}
                WHERE (t.type = 'debit' OR {_SPENDING_OFFSET_SQL})
                  AND t.is_ignored = false
                  AND t.status = 'posted'
                  AND t.date >= %(start)s
                  AND t.date < %(end)s
                  AND g.name = 'Business / Schedule C'
                  {ws_sql}
                ORDER BY c.name, t.date DESC
            """, params)
            schedule_c = _net_groups(
                (
                    (category, amount, {"date": day.isoformat(), "description": description or ""})
                    for category, day, description, amount in cur.fetchall()
                    if amount
                ),
                "category",
            )

            # Tags live as free text in notes, so they are extracted in Python.
            cur.execute(f"""
                SELECT t.notes, t.date, t.description, {signed_amount}
                FROM transactions t
                JOIN accounts a ON a.id = t.account_id
                LEFT JOIN categories c ON c.id = t.category_id
                {_LINKS_JOIN}
                WHERE (t.type = 'debit' OR {_SPENDING_OFFSET_SQL})
                  AND t.is_ignored = false
                  AND t.status = 'posted'
                  AND t.date >= %(start)s
                  AND t.date < %(end)s
                  AND t.notes ILIKE '%%#ded%%'
                  {ws_sql}
                ORDER BY t.date DESC
            """, params)
            tagged = []
            for notes, day, description, amount in cur.fetchall():
                if not amount:
                    continue
                item = {"date": day.isoformat(), "description": description or "", "notes": notes or ""}
                # A transaction repeating a tag in its notes counts once toward it.
                for tag in dict.fromkeys(m.lower() for m in DED_TAG_RE.findall(notes or "")):
                    tagged.append((tag, amount, item))
            deduction_tags = _net_groups(tagged, "tag")

        return {
            "schedule_c": schedule_c,
            "deduction_tags": deduction_tags,
            "totals": {
                "schedule_c_total": round(sum(r["total"] for r in schedule_c), 2),
                "ded_total": round(sum(r["total"] for r in deduction_tags), 2),
            },
        }
    except Exception as exc:  # DB down / driver missing — stay up, report it.
        empty["error"] = f"database unavailable: {exc}"
        return empty


def _reportable_gain(auth):
    """YTD capital gains for the estimator, from the backend rather than this
    app's own SQL: the Taxable-Wallet allowlist that decides what is Reportable
    lives there, and duplicating it here is how a Roth gain ends up taxed.

    `non_reportable_gain` is the rest of the Realised Gain — tax-advantaged
    wallets, wallets marked `other`, and wallet-less holdings — carried only so
    the UI can say what it left out. It must never reach a tax figure.
    """
    url = f"{BACKEND_URL}/api/assets/reportable-gain?start={YTD_START}&end={_ytd_end()}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=auth), timeout=5) as response:
            data = json.load(response)
        result = {
            key: float(data[key]) if data.get(key) is not None else None
            for key in ("reportable_gain", "non_reportable_gain")
        }
        for key in ("known_reportable_gain", "known_non_reportable_gain", "basis_complete", "non_reportable_basis_complete"):
            if key in data:
                result[key] = data[key]
        if result["reportable_gain"] is None or data.get("basis_complete") is False:
            result["reportable_gain"] = None
            result["gain_error"] = "original acquisition evidence is incomplete; a known subtotal is not a complete reportable gain"
        if result["non_reportable_gain"] is None or data.get("non_reportable_basis_complete") is False:
            result["non_reportable_gain"] = None
            result["non_reportable_gain_error"] = "excluded gains have incomplete acquisition evidence"
        return result
    except (OSError, ValueError) as exc:
        # OSError covers URLError and the bare TimeoutError a read timeout
        # raises: a slow backend must degrade to a hint, not 500 the whole form.
        return {"gain_error": f"capital gains unavailable: {exc}"}


def _reportable_income(auth):
    """YTD investment income arising in Taxable Wallets, from the backend for
    the same reason the gain comes from there: the allowlist that decides what
    is Reportable lives in one place, and a Roth dividend summed here is money
    the user is taxed on and should not be.

    ``interest_income`` is deliberately *not* taken from this call. The
    backend figure covers income a description attributed to a Holding, while
    the estimator's own interest query covers every account including the
    banks — and where a broker writes "interest" next to a ticker the two
    would overlap. Dividends and rewards cannot overlap it: a "DIVIDEND
    RECEIVED" line does not say interest, and a staking payout is not in the
    transaction feed at all.
    """
    url = f"{BACKEND_URL}/api/assets/reportable-income?start={YTD_START}&end={_ytd_end()}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=auth), timeout=5) as response:
            data = json.load(response)
        return {
            "ordinary_dividends": float(data.get("ordinary_dividends") or 0.0),
            "other_ordinary_income": float(data.get("other_ordinary_income") or 0.0),
            "non_reportable_income": float(data.get("non_reportable_income") or 0.0),
        }
    except (OSError, ValueError) as exc:
        return {"income_error": f"investment income unavailable: {exc}"}


def _projection_feed(auth):
    """Live balances, Roth basis and this year's contributions for the
    projection form, straight from the backend under the key names
    tax_engine.project_retirement already reads.

    Deliberately not folded into _reportable_gain: two feeds, two failure
    modes — a projection must not lose its balances because the gain endpoint
    blinked, and neither may substitute a zero for a number it never got.
    """
    url = f"{BACKEND_URL}/api/assets/projection-feed"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=auth), timeout=5) as response:
            data = json.load(response)
        if not isinstance(data, dict):
            raise ValueError("unexpected payload")
        return data
    except (OSError, ValueError) as exc:
        # A zero balance is a real answer the engine would act on, so degrade to
        # a hint and let every value key stay absent.
        return {"projection_error": f"live balances unavailable: {exc}"}


@app.get("/api/projection-prefill")
def projection_prefill(auth=Depends(require_auth)):
    """Live figures for the projection form. Ages, rates, spend, conversions
    and banked HSA receipts are never in here — nothing in Lovenest knows
    them, so they stay the user's assumptions."""
    return _projection_feed(auth)


@app.get("/api/prefill")
def prefill(auth=Depends(require_auth)):
    """Best-effort 2026 YTD figures to seed the estimator form.

    total_income: user P&L income — credits filed under an income category, or
    uncategorized and not a card refund. Refunds and reimbursements filed
    under a spending category are money back, not income, and a credit linked
    to the expense it repaid counts only for the part the link leaves over.
    interest_income: credits in a category named 'Income' or whose description
    mentions 'interest' — heuristic, surfaced as a suggestion only.
    reportable_gain: Realised Gain arising in Taxable Wallets only.
    ordinary_dividends / other_ordinary_income: investment income arising in
    Taxable Wallets only. Qualified dividends are not derived — the holding
    period they turn on is not in any description — so the user's own figure
    stands.
    """
    result = {
        "year": 2026,
        "total_income": 0.0,
        "interest_income": 0.0,
        **_reportable_gain(auth),
        **_reportable_income(auth),
    }

    if not DATABASE_URL:
        result["error"] = "DATABASE_URL not configured"
        return result
    if not _allowed_workspace_ids():
        result["error"] = "no workspace configured — set HUB_WORKSPACE_ID or INVESTMENT_WORKSPACE_ID"
        return result

    ws_sql, ws_params = _workspace_clause("t", auth.get("X-Workspace-Id", ""))

    try:
        with db_conn() as conn, conn.cursor() as cur:
            # counts_as_user_pnl for credits, plus the posted / not-opening-
            # balance filters every user P&L figure adds.
            income_sql = f"""
                SELECT COALESCE(SUM({_PNL_AMOUNT_SQL}), 0)::float8
                FROM transactions t
                JOIN accounts a ON a.id = t.account_id
                LEFT JOIN categories c ON c.id = t.category_id
                {_LINKS_JOIN}
                WHERE t.type = 'credit'
                  AND t.status = 'posted'
                  AND t.source <> 'opening_balance'
                  AND {_counts_as_user_pnl_sql("t", "a", "c")}
                  AND NOT {_SPENDING_OFFSET_SQL}
                  AND t.date >= %(start)s
                  AND t.date < %(end)s
                  {ws_sql}
            """
            cur.execute(
                income_sql,
                {"start": YTD_START, "end": _ytd_end(), **ws_params, **_LEDGER_PARAMS},
            )
            result["total_income"] = round(cur.fetchone()[0], 2)

            interest_sql = f"""
                SELECT COALESCE(SUM(COALESCE(t.amount_primary, t.amount)), 0)::float8
                FROM transactions t
                LEFT JOIN categories c ON c.id = t.category_id
                WHERE t.type = 'credit'
                  AND t.is_ignored = false
                  AND t.status = 'posted'
                  AND t.date >= %(start)s
                  AND t.date < %(end)s
                  AND t.description ILIKE '%%interest%%'
                  {ws_sql}
            """
            cur.execute(interest_sql, {"start": YTD_START, "end": _ytd_end(), **ws_params})
            result["interest_income"] = round(cur.fetchone()[0], 2)

        return result
    except Exception as exc:
        result["error"] = f"database unavailable: {exc}"
        return result


@app.exception_handler(Exception)
def unhandled(_request, exc):
    return JSONResponse(status_code=500, content={"error": str(exc)})
