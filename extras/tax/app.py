"""Lovenest — Tax & Planning companion app.

Read-only FastAPI sidecar that adds tax/planning tools to Lovenest.
Tax math lives in tax_engine (owned elsewhere); this module only wires HTTP
endpoints and read-only Postgres reporting for the write-off tab.
"""

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


def _workspace_clause(table_alias):
    """SQL fragment + params restricting rows to the configured workspace.

    API handlers require HUB_WORKSPACE_ID, so this fallback only supports
    isolated tests and direct engine development.
    """
    if HUB_WORKSPACE_ID:
        return f" AND {table_alias}.workspace_id = %(ws)s", {"ws": HUB_WORKSPACE_ID}
    return "", {}


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
    if HUB_WORKSPACE_ID and workspace_id != HUB_WORKSPACE_ID:
        raise HTTPException(status_code=403, detail="Select the configured tax workspace")
    request = urllib.request.Request(
        f"{BACKEND_URL}/api/workspaces/current" if HUB_WORKSPACE_ID else f"{BACKEND_URL}/api/users/me",
        headers={
            "Authorization": authorization,
            **({"X-Workspace-Id": workspace_id} if workspace_id else {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            if response.status != 200:
                raise HTTPException(status_code=401, detail="Invalid Lovenest session")
    except urllib.error.HTTPError as exc:
        raise HTTPException(status_code=401, detail="Invalid Lovenest session") from exc
    except urllib.error.URLError as exc:
        raise HTTPException(status_code=503, detail="Lovenest authentication unavailable") from exc


@app.post("/api/tax/estimate")
def tax_estimate(body: dict, _auth=Depends(require_auth)):
    return _calculate(tax_engine.estimate_taxes, body)


@app.post("/api/paycheck")
def paycheck(body: dict, _auth=Depends(require_auth)):
    return _calculate(tax_engine.paycheck, body)


@app.post("/api/retirement-plan")
def retirement_plan(body: dict, _auth=Depends(require_auth)):
    return _calculate(tax_engine.plan_contributions, body)


def _calculate(fn, body):
    try:
        return fn(body)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/writeoffs")
def writeoffs(_auth=Depends(require_auth)):
    """YTD-2026 deductible totals from the Lovenest ledger (read-only).

    (a) debits in any category under the "Business / Schedule C" group,
        grouped by category name;
    (b) debits whose notes contain a #ded tag, grouped by the exact tag.
    Only type='debit', is_ignored=false, date >= 2026-01-01.
    """
    empty = {
        "schedule_c": [],
        "deduction_tags": [],
        "totals": {"schedule_c_total": 0.0, "ded_total": 0.0},
    }

    if not DATABASE_URL:
        empty["error"] = "DATABASE_URL not configured"
        return empty
    if not HUB_WORKSPACE_ID:
        empty["error"] = "HUB_WORKSPACE_ID not configured"
        return empty

    ws_sql, ws_params = _workspace_clause("t")

    try:
        with db_conn() as conn, conn.cursor() as cur:
            sched_sql = f"""
                SELECT c.name AS category,
                       t.date,
                       t.description,
                       COALESCE(t.amount_primary, t.amount)::float8 AS amount
                FROM transactions t
                JOIN categories c ON c.id = t.category_id
                JOIN category_groups g ON g.id = c.group_id
                WHERE t.type = 'debit'
                  AND t.is_ignored = false
                  AND t.status = 'posted'
                  AND t.date >= %(start)s
                  AND t.date < %(end)s
                  AND g.name = 'Business / Schedule C'
                  {ws_sql}
                ORDER BY c.name, t.date DESC
            """
            cur.execute(sched_sql, {"start": YTD_START, "end": _ytd_end(), **ws_params})
            sched_agg = {}
            for category, date, description, amount in cur.fetchall():
                agg = sched_agg.setdefault(category, {"total": 0.0, "count": 0, "items": []})
                agg["total"] += amount
                agg["count"] += 1
                agg["items"].append({
                    "date": date.isoformat(),
                    "description": description or "",
                    "amount": round(amount, 2),
                })
            schedule_c = sorted(
                (
                    {"category": cat, "total": round(v["total"], 2),
                     "count": v["count"], "items": v["items"]}
                    for cat, v in sched_agg.items()
                ),
                key=lambda x: x["total"],
                reverse=True,
            )

            # Tags live as free text in notes, so aggregate in Python: pull
            # matching rows, extract each #ded tag, and sum per exact tag.
            ded_sql = f"""
                SELECT t.notes,
                       COALESCE(t.amount_primary, t.amount)::float8,
                       t.date,
                       t.description
                FROM transactions t
                WHERE t.type = 'debit'
                  AND t.is_ignored = false
                  AND t.status = 'posted'
                  AND t.date >= %(start)s
                  AND t.date < %(end)s
                  AND t.notes ILIKE '%%#ded%%'
                  {ws_sql}
                ORDER BY t.date DESC
            """
            cur.execute(ded_sql, {"start": YTD_START, "end": _ytd_end(), **ws_params})

            tag_totals = {}
            for notes, amount, date, description in cur.fetchall():
                seen = set()
                for m in DED_TAG_RE.findall(notes or ""):
                    tag = m.lower()
                    # Count a transaction once per distinct tag even if it
                    # repeats the tag in its notes.
                    if tag in seen:
                        continue
                    seen.add(tag)
                    agg = tag_totals.setdefault(tag, {"total": 0.0, "count": 0, "items": []})
                    agg["total"] += amount
                    agg["count"] += 1
                    agg["items"].append({
                        "date": date.isoformat(),
                        "description": description or "",
                        "amount": round(amount, 2),
                        "notes": notes or "",
                    })

        deduction_tags = sorted(
            (
                {"tag": tag, "total": round(v["total"], 2),
                 "count": v["count"], "items": v["items"]}
                for tag, v in tag_totals.items()
            ),
            key=lambda x: x["total"],
            reverse=True,
        )

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


@app.get("/api/prefill")
def prefill(_auth=Depends(require_auth)):
    """Best-effort 2026 YTD figures to seed the estimator form.

    total_income: sum of non-transfer, non-ignored credits.
    interest_income: credits in a category named 'Income' or whose description
    mentions 'interest' — heuristic, surfaced as a suggestion only.
    """
    result = {
        "year": 2026,
        "total_income": 0.0,
        "interest_income": 0.0,
    }

    if not DATABASE_URL:
        result["error"] = "DATABASE_URL not configured"
        return result
    if not HUB_WORKSPACE_ID:
        result["error"] = "HUB_WORKSPACE_ID not configured"
        return result

    ws_sql, ws_params = _workspace_clause("t")

    try:
        with db_conn() as conn, conn.cursor() as cur:
            income_sql = f"""
                SELECT COALESCE(SUM(COALESCE(t.amount_primary, t.amount)), 0)::float8
                FROM transactions t
                LEFT JOIN categories c ON c.id = t.category_id
                WHERE t.type = 'credit'
                  AND t.is_ignored = false
                  AND t.status = 'posted'
                  AND t.date >= %(start)s
                  AND t.date < %(end)s
                  AND t.transfer_pair_id IS NULL
                  AND COALESCE(c.treat_as_transfer, false) = false
                  {ws_sql}
            """
            cur.execute(income_sql, {"start": YTD_START, "end": _ytd_end(), **ws_params})
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
