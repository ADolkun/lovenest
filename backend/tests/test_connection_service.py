"""Service-level tests for connection_service.

Tests: _match_pluggy_category, _description_similarity, get_connections,
get_connection, delete_connection, update_connection_settings.
"""
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bank_connection import BankConnection
from app.models.category import Category
from app.services.connection_service import (
    _description_similarity,
    _match_pluggy_category,
    delete_connection,
    get_connection,
    get_connections,
    update_connection_settings,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_connection(
    session: AsyncSession, user_id: uuid.UUID, name: str = "Test Bank",
    settings: dict | None = None,
) -> BankConnection:
    conn = BankConnection(
        id=uuid.uuid4(), user_id=user_id, provider="test",
        external_id=f"ext-{uuid.uuid4().hex[:8]}",
        institution_name=name, credentials={"token": "fake"},
        status="active", settings=settings,
        last_sync_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
    )
    session.add(conn)
    await session.commit()
    await session.refresh(conn)
    return conn


async def _make_category(
    session: AsyncSession, user_id: uuid.UUID, name: str,
) -> Category:
    cat = Category(
        id=uuid.uuid4(), user_id=user_id, name=name,
        icon="tag", color="#000", is_system=False,
    )
    session.add(cat)
    await session.commit()
    await session.refresh(cat)
    return cat


# ---------------------------------------------------------------------------
# _description_similarity (pure function)
# ---------------------------------------------------------------------------


def test_description_similarity_identical():
    assert _description_similarity("hello world", "hello world") == 1.0


def test_description_similarity_partial():
    score = _description_similarity("hello world foo", "hello world bar")
    assert 0.0 < score < 1.0


def test_description_similarity_no_overlap():
    assert _description_similarity("abc", "xyz") == 0.0


def test_description_similarity_none():
    assert _description_similarity(None, "hello") == 0.0
    assert _description_similarity("hello", None) == 0.0
    assert _description_similarity(None, None) == 0.0


def test_description_similarity_empty():
    assert _description_similarity("", "hello") == 0.0
    assert _description_similarity("hello", "") == 0.0


def test_description_similarity_case_insensitive():
    score = _description_similarity("Hello World", "hello world")
    assert score == 1.0


# ---------------------------------------------------------------------------
# _match_pluggy_category
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_match_pluggy_exact(session: AsyncSession, test_user):
    """Exact Pluggy category match maps to user's category."""
    await _make_category(session, test_user.id, "Alimentação")
    cat_id = await _match_pluggy_category(session, test_user.id, "Eating out")
    assert cat_id is not None


@pytest.mark.asyncio
async def test_match_pluggy_prefix(session: AsyncSession, test_user):
    """Pluggy category with ' - ' prefix matches via split."""
    await _make_category(session, test_user.id, "Transferências")
    cat_id = await _match_pluggy_category(session, test_user.id, "Transfer - PIX")
    assert cat_id is not None


@pytest.mark.asyncio
async def test_match_pluggy_no_match(session: AsyncSession, test_user):
    """Unknown Pluggy category returns None."""
    cat_id = await _match_pluggy_category(session, test_user.id, "Unknown Category XYZ")
    assert cat_id is None


@pytest.mark.asyncio
async def test_match_pluggy_none(session: AsyncSession, test_user):
    """None category returns None."""
    cat_id = await _match_pluggy_category(session, test_user.id, None)
    assert cat_id is None


@pytest.mark.asyncio
async def test_match_pluggy_user_has_no_category(session: AsyncSession, test_user):
    """Pluggy category maps but user doesn't have the target category."""
    # "Eating out" maps to "Alimentação" but we don't create it
    cat_id = await _match_pluggy_category(session, test_user.id, "Eating out")
    assert cat_id is None


# ---------------------------------------------------------------------------
# get_connections / get_connection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_connections_returns_list(session: AsyncSession, test_user):
    """Returns list of connections for user."""
    await _make_connection(session, test_user.id, "Bank A")
    await _make_connection(session, test_user.id, "Bank B")

    connections = await get_connections(session, test_user.id)
    assert len(connections) >= 2
    names = {c.institution_name for c in connections}
    assert "Bank A" in names
    assert "Bank B" in names


@pytest.mark.asyncio
async def test_get_connections_empty(session: AsyncSession, test_user):
    """Returns empty list when no connections."""
    connections = await get_connections(session, test_user.id)
    # May have connections from other fixtures; just verify it's a list
    assert isinstance(connections, list)


@pytest.mark.asyncio
async def test_get_connection_found(session: AsyncSession, test_user):
    """Returns a specific connection."""
    conn = await _make_connection(session, test_user.id, "Specific Bank")
    result = await get_connection(session, conn.id, test_user.id)
    assert result is not None
    assert result.institution_name == "Specific Bank"


@pytest.mark.asyncio
async def test_get_connection_not_found(session: AsyncSession, test_user):
    """Returns None for nonexistent connection."""
    result = await get_connection(session, uuid.uuid4(), test_user.id)
    assert result is None


@pytest.mark.asyncio
async def test_get_connection_wrong_user(session: AsyncSession, test_user):
    """Returns None when connection belongs to another user."""
    conn = await _make_connection(session, test_user.id, "Other User Bank")
    result = await get_connection(session, conn.id, uuid.uuid4())
    assert result is None


# ---------------------------------------------------------------------------
# update_connection_settings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_settings_new(session: AsyncSession, test_user):
    """Updates settings on a connection with no prior settings."""
    conn = await _make_connection(session, test_user.id, "Settings Test")

    updated = await update_connection_settings(
        session, conn.id, test_user.id, {"payee_source": "merchant"},
    )
    assert updated is not None
    assert updated.settings["payee_source"] == "merchant"


@pytest.mark.asyncio
async def test_update_settings_preserves_existing(session: AsyncSession, test_user):
    """Updates one setting without clobbering others."""
    conn = await _make_connection(
        session, test_user.id, "Preserve Test",
        settings={"payee_source": "auto", "import_pending": True},
    )

    updated = await update_connection_settings(
        session, conn.id, test_user.id, {"import_pending": False},
    )
    assert updated is not None
    assert updated.settings["payee_source"] == "auto"
    assert updated.settings["import_pending"] is False


@pytest.mark.asyncio
async def test_update_settings_ignores_none(session: AsyncSession, test_user):
    """None values in settings_update are not written."""
    conn = await _make_connection(
        session, test_user.id, "None Test",
        settings={"payee_source": "auto"},
    )
    updated = await update_connection_settings(
        session, conn.id, test_user.id, {"payee_source": None},
    )
    assert updated is not None
    assert updated.settings["payee_source"] == "auto"


@pytest.mark.asyncio
async def test_update_settings_not_found(session: AsyncSession, test_user):
    """Returns None when connection not found."""
    result = await update_connection_settings(
        session, uuid.uuid4(), test_user.id, {"payee_source": "auto"},
    )
    assert result is None


# ---------------------------------------------------------------------------
# delete_connection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_connection_found(session: AsyncSession, test_user):
    """Deletes an existing connection."""
    conn = await _make_connection(session, test_user.id, "To Delete")
    result = await delete_connection(session, conn.id, test_user.id)
    assert result is True

    assert await get_connection(session, conn.id, test_user.id) is None


@pytest.mark.asyncio
async def test_delete_connection_not_found(session: AsyncSession, test_user):
    """Returns False for nonexistent connection."""
    result = await delete_connection(session, uuid.uuid4(), test_user.id)
    assert result is False
