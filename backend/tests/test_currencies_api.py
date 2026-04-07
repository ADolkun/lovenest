import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_list_currencies(client: AsyncClient):
    response = await client.get("/api/currencies")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert len(data) > 0
    for currency in data:
        assert "code" in currency
        assert "symbol" in currency
        assert "name" in currency
        assert "flag" in currency


@pytest.mark.asyncio
async def test_currencies_include_brl_and_usd(client: AsyncClient):
    response = await client.get("/api/currencies")
    codes = [c["code"] for c in response.json()]
    assert "BRL" in codes
    assert "USD" in codes
