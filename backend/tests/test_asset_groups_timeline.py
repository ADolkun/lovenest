"""Historical activity can select persisted wallets without active holdings."""
import uuid

import pytest

from app.models.asset_group import AssetGroup
from app.models.workspace import Workspace
from app.models.bank_connection import BankConnection
from app.models.investment_evidence import InvestmentHistoryCollection


@pytest.mark.asyncio
async def test_empty_wallet_opt_in_is_scoped_and_preserves_default(client, auth_headers, session, test_workspace, test_user):
    foreign = Workspace(id=uuid.uuid4(), name="Synthetic other workspace", created_by_user_id=test_user.id)
    session.add(foreign)
    await session.flush()
    ours = AssetGroup(workspace_id=test_workspace.id, user_id=test_user.id, name="Synthetic history wallet", source="onchain")
    theirs = AssetGroup(workspace_id=foreign.id, user_id=test_user.id, name="Synthetic foreign wallet", source="onchain")
    session.add_all([ours, theirs])
    await session.commit()
    ordinary = await client.get("/api/asset-groups", headers=auth_headers)
    history = await client.get("/api/asset-groups", headers=auth_headers, params={"include_empty": "true"})
    assert ordinary.status_code == history.status_code == 200
    assert str(ours.id) not in {row["id"] for row in ordinary.json()}
    assert str(ours.id) in {row["id"] for row in history.json()}
    assert str(theirs.id) not in {row["id"] for row in history.json()}


@pytest.mark.asyncio
async def test_retained_watched_address_mapping_does_not_spread_across_connection_wallets(client, auth_headers, session, test_workspace, test_user):
    connection = BankConnection(id=uuid.uuid4(), workspace_id=test_workspace.id, user_id=test_user.id, provider="onchain",
        external_id="synthetic-watch", institution_name="Synthetic", credentials={"addresses": ["solana:owned-A", "solana:owned-B"]})
    session.add(connection)
    await session.flush()
    groups = [AssetGroup(id=uuid.uuid4(), workspace_id=test_workspace.id, user_id=test_user.id, name=f"Synthetic {name}",
                         connection_id=connection.id, source="onchain") for name in ("A", "B")]
    session.add_all(groups)
    await session.flush()
    for group, address in zip(groups, ("owned-A", "owned-B")):
        session.add(InvestmentHistoryCollection(workspace_id=test_workspace.id, group_id=group.id, connection_id=connection.id,
            request={"chain": "solana", "address": address}, payload={}, revision="synthetic", size_bytes=0))
    await session.commit()
    response = await client.get("/api/asset-groups", headers=auth_headers, params={"include_empty": "true"})
    assert response.status_code == 200, response.text
    rows = {row["id"]: row for row in response.json()}
    assert rows[str(groups[0].id)]["watched_address_keys"] == ["solana:owned-A"]
    assert rows[str(groups[1].id)]["watched_address_keys"] == ["solana:owned-B"]
