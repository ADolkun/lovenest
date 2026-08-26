"""The contributions endpoints, the summary, and the projection feed.

What the pure tests cannot reach: tenancy, the write gate, the constraints that
keep meaningless rows out of the table, and the aggregate the tax sidecar's
projection reads.
"""

from datetime import date

import pytest

TODAY = date.today()


async def _wallet(client, headers, name, tax_treatment="taxable"):
    response = await client.post(
        "/api/asset-groups",
        json={"name": name, "tax_treatment": tax_treatment},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _contribute(client, headers, group_id, **overrides):
    body = {
        "group_id": group_id,
        "kind": "contribution",
        "amount": 1000,
        "date": TODAY.isoformat(),
        **overrides,
    }
    return await client.post("/api/contributions", json=body, headers=headers)


@pytest.mark.asyncio
async def test_a_contribution_and_a_distribution_can_each_be_recorded(client, auth_headers):
    wallet = await _wallet(client, auth_headers, "Brokerage")

    created = await _contribute(client, auth_headers, wallet, amount=5000)
    assert created.status_code == 201, created.text
    assert created.json()["kind"] == "contribution"
    assert created.json()["amount"] == 5000.0

    out = await _contribute(client, auth_headers, wallet, kind="distribution", amount=750)
    assert out.status_code == 201, out.text
    assert out.json()["kind"] == "distribution"

    listed = await client.get(f"/api/contributions?group_id={wallet}", headers=auth_headers)
    assert {row["kind"] for row in listed.json()} == {"contribution", "distribution"}


@pytest.mark.asyncio
async def test_net_contribution_falls_when_a_distribution_is_recorded(client, auth_headers):
    wallet = await _wallet(client, auth_headers, "Roth IRA", tax_treatment="roth")
    await _contribute(client, auth_headers, wallet, amount=7000)

    before = (await client.get("/api/contributions/summary", headers=auth_headers)).json()
    assert before[0]["net"] == 7000.0

    await _contribute(client, auth_headers, wallet, kind="distribution", amount=1500)
    after = (await client.get("/api/contributions/summary", headers=auth_headers)).json()
    assert after[0]["net"] == 5500.0
    assert after[0]["distributions"] == 1500.0


@pytest.mark.asyncio
async def test_the_summary_carries_the_employer_and_vesting_split(client, auth_headers):
    wallet = await _wallet(client, auth_headers, "401(k)", tax_treatment="traditional")
    await _contribute(client, auth_headers, wallet, amount=10000)
    await _contribute(
        client,
        auth_headers,
        wallet,
        amount=3000,
        party="employer",
        vested_on=date(TODAY.year + 2, 1, 1).isoformat(),
    )

    summary = (await client.get("/api/contributions/summary", headers=auth_headers)).json()[0]
    assert summary["own_contributions"] == 10000.0
    assert summary["employer_contributions"] == 3000.0
    assert summary["employer_unvested"] == 3000.0
    assert summary["net"] == 10000.0


@pytest.mark.asyncio
async def test_a_year_a_contribution_counts_against_can_differ_from_its_date(
    client, auth_headers
):
    """A prior-year total typed in today still belongs to that year."""
    wallet = await _wallet(client, auth_headers, "Roth IRA", tax_treatment="roth")
    await _contribute(client, auth_headers, wallet, amount=5500, tax_year=2019)

    summary = (await client.get("/api/contributions/summary", headers=auth_headers)).json()[0]
    assert [year["tax_year"] for year in summary["years"]] == [2019]

    filtered = await client.get("/api/contributions?tax_year=2019", headers=auth_headers)
    assert len(filtered.json()) == 1
    assert (await client.get(
        f"/api/contributions?tax_year={TODAY.year}", headers=auth_headers
    )).json() == []


@pytest.mark.asyncio
async def test_a_tax_year_defaults_to_the_year_it_was_paid(client, auth_headers):
    wallet = await _wallet(client, auth_headers, "Brokerage")
    created = await _contribute(client, auth_headers, wallet, date="2024-06-01")
    assert created.json()["tax_year"] == 2024


@pytest.mark.asyncio
async def test_combinations_that_mean_nothing_are_refused(client, auth_headers):
    wallet = await _wallet(client, auth_headers, "Brokerage")

    employer_paying_out = await _contribute(
        client, auth_headers, wallet, kind="distribution", party="employer"
    )
    assert employer_paying_out.status_code == 422

    own_money_vesting = await _contribute(
        client, auth_headers, wallet, vested_on=TODAY.isoformat()
    )
    assert own_money_vesting.status_code == 422

    nothing_at_all = await _contribute(client, auth_headers, wallet, amount=0)
    assert nothing_at_all.status_code == 422


@pytest.mark.asyncio
async def test_editing_a_row_keeps_the_same_rules(client, auth_headers):
    wallet = await _wallet(client, auth_headers, "Brokerage")
    row = (await _contribute(client, auth_headers, wallet, amount=1000)).json()

    edited = await client.patch(
        f"/api/contributions/{row['id']}", json={"amount": 1200}, headers=auth_headers
    )
    assert edited.status_code == 200
    assert edited.json()["amount"] == 1200.0

    broken = await client.patch(
        f"/api/contributions/{row['id']}",
        json={"vested_on": TODAY.isoformat()},
        headers=auth_headers,
    )
    assert broken.status_code == 422


@pytest.mark.asyncio
async def test_moving_a_date_moves_the_year_it_counts_against_with_it(client, auth_headers):
    wallet = await _wallet(client, auth_headers, "Brokerage")
    row = (await _contribute(client, auth_headers, wallet, date="2024-06-01")).json()

    moved = await client.patch(
        f"/api/contributions/{row['id']}", json={"date": "2025-06-01"}, headers=auth_headers
    )
    assert moved.json()["tax_year"] == 2025

    pinned = await client.patch(
        f"/api/contributions/{row['id']}",
        json={"date": "2026-04-01", "tax_year": 2025},
        headers=auth_headers,
    )
    assert pinned.json()["tax_year"] == 2025


@pytest.mark.asyncio
async def test_a_deleted_row_stops_counting(client, auth_headers):
    wallet = await _wallet(client, auth_headers, "Brokerage")
    row = (await _contribute(client, auth_headers, wallet, amount=1000)).json()

    deleted = await client.delete(f"/api/contributions/{row['id']}", headers=auth_headers)
    assert deleted.status_code == 204
    assert (await client.get("/api/contributions/summary", headers=auth_headers)).json() == []
    assert (
        await client.delete(f"/api/contributions/{row['id']}", headers=auth_headers)
    ).status_code == 404


@pytest.mark.asyncio
async def test_another_workspace_neither_sees_nor_writes_these_rows(
    client, auth_headers, other_workspace_headers
):
    wallet = await _wallet(client, auth_headers, "Brokerage")
    row = (await _contribute(client, auth_headers, wallet, amount=1000)).json()

    assert (await client.get("/api/contributions", headers=other_workspace_headers)).json() == []
    assert (
        await client.get("/api/contributions/summary", headers=other_workspace_headers)
    ).json() == []

    written = await _contribute(client, other_workspace_headers, wallet, amount=1)
    assert written.status_code == 404

    for method in ("patch", "delete"):
        response = await getattr(client, method)(
            f"/api/contributions/{row['id']}",
            headers=other_workspace_headers,
            **({"json": {"amount": 2}} if method == "patch" else {}),
        )
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_a_viewer_may_read_the_figures_but_not_change_them(
    client, auth_headers, viewer_auth_headers
):
    wallet = await _wallet(client, auth_headers, "Brokerage")
    row = (await _contribute(client, auth_headers, wallet, amount=1000)).json()

    assert (
        await client.get("/api/contributions/summary", headers=viewer_auth_headers)
    ).status_code == 200
    assert (await _contribute(client, viewer_auth_headers, wallet)).status_code == 403
    assert (
        await client.delete(f"/api/contributions/{row['id']}", headers=viewer_auth_headers)
    ).status_code == 403


# ---------------------------------------------------------------------------
# The projection feed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_feed_uses_the_key_names_the_projection_already_reads(client, auth_headers):
    """The sidecar's engine reads a plain dict; a renamed key is silently
    read as zero, so the contract is the names themselves."""
    feed = (await client.get("/api/assets/projection-feed", headers=auth_headers)).json()
    for key in (
        "trad_401k", "roth_ira", "hsa", "taxable", "roth_basis",
        "annual_trad_401k", "annual_roth", "annual_hsa", "annual_taxable",
    ):
        assert key in feed, key
    assert feed["annual_is_year_to_date"] is True


@pytest.mark.asyncio
async def test_the_feed_buckets_wallets_by_tax_character(client, auth_headers):
    roth = await _wallet(client, auth_headers, "Roth IRA", tax_treatment="roth")
    await _wallet(client, auth_headers, "401(k)", tax_treatment="traditional")
    await _contribute(client, auth_headers, roth, amount=7000)

    feed = (await client.get("/api/assets/projection-feed", headers=auth_headers)).json()
    assert feed["annual_roth"] == 7000.0
    assert feed["annual_trad_401k"] == 0.0
    assert "annual_roth" in feed["live"]
    assert "hsa_receipts" not in feed["live"]


@pytest.mark.asyncio
async def test_the_feeds_withdrawable_basis_is_the_roth_wallets_net_contribution(
    client, auth_headers
):
    roth = await _wallet(client, auth_headers, "Roth IRA", tax_treatment="roth")
    await _contribute(client, auth_headers, roth, amount=7000)
    await _contribute(client, auth_headers, roth, kind="distribution", amount=1000)

    feed = (await client.get("/api/assets/projection-feed", headers=auth_headers)).json()
    assert feed["roth_basis"] == 6000.0


@pytest.mark.asyncio
async def test_one_over_distributed_wallet_does_not_eat_anothers_basis(client, auth_headers):
    """The penalty rule is applied account by account, so the floor is too."""
    first = await _wallet(client, auth_headers, "Roth IRA A", tax_treatment="roth")
    second = await _wallet(client, auth_headers, "Roth IRA B", tax_treatment="roth")
    await _contribute(client, auth_headers, first, amount=7000)
    await _contribute(client, auth_headers, second, kind="distribution", amount=9000)

    feed = (await client.get("/api/assets/projection-feed", headers=auth_headers)).json()
    assert feed["roth_basis"] == 7000.0


@pytest.mark.asyncio
async def test_the_feed_counts_employer_money_towards_the_year_even_unvested(
    client, auth_headers
):
    """Unvested money still lands in the account and still compounds."""
    plan = await _wallet(client, auth_headers, "401(k)", tax_treatment="traditional")
    await _contribute(client, auth_headers, plan, amount=20000)
    await _contribute(
        client,
        auth_headers,
        plan,
        amount=5000,
        party="employer",
        vested_on=date(TODAY.year + 2, 1, 1).isoformat(),
    )

    feed = (await client.get("/api/assets/projection-feed", headers=auth_headers)).json()
    assert feed["annual_trad_401k"] == 25000.0


@pytest.mark.asyncio
async def test_the_feed_says_what_it_could_not_bucket(client, auth_headers):
    other = await _wallet(client, auth_headers, "Trust", tax_treatment="other")
    await _contribute(client, auth_headers, other, amount=1000)

    feed = (await client.get("/api/assets/projection-feed", headers=auth_headers)).json()
    assert set(feed["excluded"]) == {"other", "ungrouped"}
    assert feed["taxable"] == 0.0


@pytest.mark.asyncio
async def test_the_feed_is_scoped_to_one_workspace(
    client, auth_headers, other_workspace_headers
):
    roth = await _wallet(client, auth_headers, "Roth IRA", tax_treatment="roth")
    await _contribute(client, auth_headers, roth, amount=7000)

    feed = (await client.get(
        "/api/assets/projection-feed", headers=other_workspace_headers
    )).json()
    assert feed["annual_roth"] == 0.0
    assert feed["roth_basis"] == 0.0
