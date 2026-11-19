"""Access tokens against Lovenest's own routes: the tax and evidence routes
under /api/assets stay closed whatever the scope, and undoing an import needs
the scope of what the undo removes, not only `transactions:write`."""
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.import_log import ImportLog


async def _token(client: AsyncClient, headers: dict, scopes: list[str]) -> dict:
    resp = await client.post("/api/access-tokens", headers=headers, json={"name": "script", "scopes": scopes})
    assert resp.status_code == 201, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


@pytest.mark.asyncio
async def test_an_assets_token_reads_holdings_but_not_tax_or_evidence(client: AsyncClient, auth_headers: dict):
    bearer = await _token(client, auth_headers, ["assets:write"])
    assert (await client.get("/api/assets", headers=bearer)).status_code == 200
    asset_id = uuid.uuid4()
    for path in (
        "/api/assets/reportable-gain", "/api/assets/projection-feed", "/api/assets/income",
        f"/api/assets/evidence?group_id={uuid.uuid4()}", "/api/assets/timeline",
        f"/api/assets/{asset_id}/tax-lots", f"/api/assets/{asset_id}/wash-sale",
    ):
        assert (await client.get(path, headers=bearer)).status_code == 403, path


async def _log(session: AsyncSession, workspace, user, entity: str) -> ImportLog:
    log = ImportLog(user_id=user.id, workspace_id=workspace.id, entity=entity, filename="synthetic.csv",
                    format="csv", transaction_count=0)
    session.add(log)
    await session.commit()
    return log


@pytest.mark.asyncio
@pytest.mark.parametrize("entity,scopes,allowed", [
    ("asset_orders", ["transactions:write"], False),
    ("asset_orders", ["transactions:write", "assets:write"], True),
    ("asset_evidence", ["transactions:write", "assets:write"], False),
    ("asset_contributions", ["transactions:write", "assets:write"], False),
    ("transactions", ["transactions:write"], True),
])
async def test_undoing_an_import_needs_the_scope_of_what_it_removes(
    client: AsyncClient, auth_headers: dict, session: AsyncSession, test_workspace, test_user,
    entity, scopes, allowed,
):
    log = await _log(session, test_workspace, test_user, entity)
    bearer = await _token(client, auth_headers, scopes)
    resp = await client.delete(f"/api/import-logs/{log.id}", headers=bearer)
    if allowed:
        assert resp.status_code == 204, resp.text
    else:
        assert resp.status_code == 403, resp.text
        assert await session.get(ImportLog, log.id) is not None
