"""Every API route's access-token scope, checked against a reviewed list.

`access_token_scopes` opens routes by path prefix, so a new route under a
mapped prefix is open to tokens the moment it exists — as the fork's tax and
evidence routes under /api/assets were. This test fails on any route whose
scope is not in `access_token_routes.txt` until someone classifies it.

After a deliberate change, regenerate the list and review its diff:

    UPDATE_ACCESS_TOKEN_ROUTES=1 uv run --no-sync pytest tests/test_access_token_route_map.py
"""
import os
from pathlib import Path

from app.core.access_token_scopes import required_scope
from app.main import app

ROUTES = Path(__file__).with_name("access_token_routes.txt")


def _current() -> list[str]:
    lines = []
    for path, operations in app.openapi()["paths"].items():
        if not path.startswith("/api/"):
            continue
        for method in operations:
            scope = required_scope(method.upper(), path)
            label = "closed" if scope is None else scope or "any-token"
            lines.append(f"{label} {method.upper()} {path}")
    return sorted(lines)


def test_every_route_has_a_reviewed_access_token_scope():
    current = _current()
    if os.environ.get("UPDATE_ACCESS_TOKEN_ROUTES"):
        ROUTES.write_text("\n".join(current) + "\n")
    reviewed = ROUTES.read_text().splitlines()
    assert current == reviewed, (
        "Routes and their token scopes changed; classify them in "
        "app/core/access_token_scopes.py and regenerate access_token_routes.txt "
        "(see this module's docstring)."
    )


def test_the_forks_tax_and_evidence_routes_are_closed_to_tokens():
    for method, path in [
        ("GET", "/api/assets/evidence"),
        ("POST", "/api/assets/evidence/confirm"),
        ("GET", "/api/assets/evidence/source-reviews/export"),
        ("GET", "/api/assets/recovery/export"),
        ("GET", "/api/assets/timeline/sources/abc"),
        ("GET", "/api/assets/income"),
        ("GET", "/api/assets/reportable-gain"),
        ("GET", "/api/assets/reportable-income"),
        ("GET", "/api/assets/projection-feed"),
        ("GET", "/api/assets/0b6f5d1e-0000-4000-8000-000000000000/tax-lots"),
        ("GET", "/api/assets/0b6f5d1e-0000-4000-8000-000000000000/wash-sale"),
    ]:
        assert required_scope(method, path) is None, path
    assert required_scope("GET", "/api/assets/0b6f5d1e-0000-4000-8000-000000000000/transactions") == "assets:read"
    assert required_scope("GET", "/api/assets/income-statement") == "assets:read"
