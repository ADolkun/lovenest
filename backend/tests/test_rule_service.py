import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.transaction import Transaction
from app.schemas.rule import RuleAction, RuleCondition, RuleCreate, RuleUpdate
from app.services.rule_service import (
    DuplicateRuleError,
    apply_all_rules,
    apply_rules_to_transaction,
    create_default_rules,
    create_rule,
    delete_rule,
    get_installed_packs,
    get_rule,
    get_rules,
    install_rule_pack,
    update_rule,
)
from app.services.category_service import create_default_categories


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_rule(session: AsyncSession, test_user, test_categories):
    data = RuleCreate(
        name="My Rule",
        conditions_op="or",
        conditions=[RuleCondition(field="description", op="contains", value="UBER")],
        actions=[RuleAction(op="set_category", value=str(test_categories[1].id))],
        priority=10,
    )
    rule = await create_rule(session, test_user.id, data)

    assert rule.id is not None
    assert rule.name == "My Rule"
    assert rule.conditions_op == "or"
    assert len(rule.conditions) == 1
    assert len(rule.actions) == 1
    assert rule.is_active is True


@pytest.mark.asyncio
async def test_get_rules(session: AsyncSession, test_user, test_categories):
    for name in ["Rule A", "Rule B"]:
        await create_rule(
            session,
            test_user.id,
            RuleCreate(
                name=name,
                conditions=[RuleCondition(field="description", op="contains", value="X")],
                actions=[RuleAction(op="set_category", value=str(test_categories[0].id))],
            ),
        )

    rules = await get_rules(session, test_user.id)
    assert len(rules) >= 2
    names = {r.name for r in rules}
    assert "Rule A" in names
    assert "Rule B" in names


@pytest.mark.asyncio
async def test_get_rule_by_id(session: AsyncSession, test_user, test_categories):
    created = await create_rule(
        session,
        test_user.id,
        RuleCreate(
            name="Lookup Rule",
            conditions=[RuleCondition(field="description", op="contains", value="X")],
            actions=[RuleAction(op="set_category", value=str(test_categories[0].id))],
        ),
    )
    fetched = await get_rule(session, created.id, test_user.id)
    assert fetched is not None
    assert fetched.id == created.id


@pytest.mark.asyncio
async def test_get_rule_not_found(session: AsyncSession, test_user):
    result = await get_rule(session, uuid.uuid4(), test_user.id)
    assert result is None


@pytest.mark.asyncio
async def test_update_rule(session: AsyncSession, test_user, test_categories):
    rule = await create_rule(
        session,
        test_user.id,
        RuleCreate(
            name="Original",
            conditions=[RuleCondition(field="description", op="contains", value="OLD")],
            actions=[RuleAction(op="set_category", value=str(test_categories[0].id))],
            priority=5,
        ),
    )
    updated = await update_rule(
        session,
        rule.id,
        test_user.id,
        RuleUpdate(name="Updated", priority=20),
    )
    assert updated is not None
    assert updated.name == "Updated"
    assert updated.priority == 20


@pytest.mark.asyncio
async def test_update_rule_not_found(session: AsyncSession, test_user):
    result = await update_rule(
        session,
        uuid.uuid4(),
        test_user.id,
        RuleUpdate(name="Nope"),
    )
    assert result is None


@pytest.mark.asyncio
async def test_delete_rule(session: AsyncSession, test_user, test_categories):
    rule = await create_rule(
        session,
        test_user.id,
        RuleCreate(
            name="ToDelete",
            conditions=[RuleCondition(field="description", op="contains", value="X")],
            actions=[RuleAction(op="set_category", value=str(test_categories[0].id))],
        ),
    )
    assert await delete_rule(session, rule.id, test_user.id) is True
    assert await get_rule(session, rule.id, test_user.id) is None


@pytest.mark.asyncio
async def test_delete_rule_not_found(session: AsyncSession, test_user):
    assert await delete_rule(session, uuid.uuid4(), test_user.id) is False


# ---------------------------------------------------------------------------
# DuplicateRuleError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_duplicate_rule_raises(session: AsyncSession, test_user, test_categories):
    data = RuleCreate(
        name="Unique Name",
        conditions=[RuleCondition(field="description", op="contains", value="X")],
        actions=[RuleAction(op="set_category", value=str(test_categories[0].id))],
    )
    await create_rule(session, test_user.id, data)

    with pytest.raises(DuplicateRuleError):
        await create_rule(session, test_user.id, data)


@pytest.mark.asyncio
async def test_update_rule_duplicate_name_raises(session: AsyncSession, test_user, test_categories):
    rule_a = await create_rule(
        session,
        test_user.id,
        RuleCreate(
            name="Name A",
            conditions=[RuleCondition(field="description", op="contains", value="X")],
            actions=[RuleAction(op="set_category", value=str(test_categories[0].id))],
        ),
    )
    await create_rule(
        session,
        test_user.id,
        RuleCreate(
            name="Name B",
            conditions=[RuleCondition(field="description", op="contains", value="Y")],
            actions=[RuleAction(op="set_category", value=str(test_categories[0].id))],
        ),
    )

    with pytest.raises(DuplicateRuleError):
        await update_rule(
            session,
            rule_a.id,
            test_user.id,
            RuleUpdate(name="Name B"),
        )


# ---------------------------------------------------------------------------
# apply_rules_to_transaction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_rules_to_transaction(session: AsyncSession, test_user, test_categories):
    # Create a rule matching UBER
    await create_rule(
        session,
        test_user.id,
        RuleCreate(
            name="UBER Rule",
            conditions_op="or",
            conditions=[RuleCondition(field="description", op="starts_with", value="UBER")],
            actions=[RuleAction(op="set_category", value=str(test_categories[1].id))],
            priority=10,
        ),
    )

    account = Account(
        id=uuid.uuid4(),
        user_id=test_user.id,
        name="RuleAcc",
        type="checking",
        balance=Decimal("1000"),
        currency="BRL",
    )
    session.add(account)
    await session.commit()

    txn = Transaction(
        id=uuid.uuid4(),
        user_id=test_user.id,
        account_id=account.id,
        description="UBER TRIP",
        amount=Decimal("25.50"),
        date=date(2025, 3, 1),
        type="debit",
        source="manual",
        created_at=datetime.now(timezone.utc),
    )
    session.add(txn)
    await session.commit()

    await apply_rules_to_transaction(session, test_user.id, txn)

    assert txn.category_id == test_categories[1].id


@pytest.mark.asyncio
async def test_apply_rules_no_match(session: AsyncSession, test_user, test_categories):
    await create_rule(
        session,
        test_user.id,
        RuleCreate(
            name="IFOOD Only",
            conditions_op="or",
            conditions=[RuleCondition(field="description", op="starts_with", value="IFOOD")],
            actions=[RuleAction(op="set_category", value=str(test_categories[0].id))],
        ),
    )

    account = Account(
        id=uuid.uuid4(),
        user_id=test_user.id,
        name="NoMatch",
        type="checking",
        balance=Decimal("1000"),
        currency="BRL",
    )
    session.add(account)
    await session.commit()

    txn = Transaction(
        id=uuid.uuid4(),
        user_id=test_user.id,
        account_id=account.id,
        description="RANDOM MERCHANT",
        amount=Decimal("10"),
        date=date(2025, 3, 1),
        type="debit",
        source="manual",
        created_at=datetime.now(timezone.utc),
    )
    session.add(txn)
    await session.commit()

    await apply_rules_to_transaction(session, test_user.id, txn)
    assert txn.category_id is None


# ---------------------------------------------------------------------------
# apply_all_rules
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_all_rules(session: AsyncSession, test_user, test_categories):
    account = Account(
        id=uuid.uuid4(),
        user_id=test_user.id,
        name="AllRules",
        type="checking",
        balance=Decimal("5000"),
        currency="BRL",
    )
    session.add(account)
    await session.commit()

    # Create transactions
    txn1 = Transaction(
        id=uuid.uuid4(),
        user_id=test_user.id,
        account_id=account.id,
        description="UBER RIDE",
        amount=Decimal("30"),
        date=date(2025, 3, 5),
        type="debit",
        source="manual",
        created_at=datetime.now(timezone.utc),
    )
    txn2 = Transaction(
        id=uuid.uuid4(),
        user_id=test_user.id,
        account_id=account.id,
        description="IFOOD RESTAURANTE",
        amount=Decimal("45"),
        date=date(2025, 3, 6),
        type="debit",
        source="manual",
        created_at=datetime.now(timezone.utc),
    )
    session.add_all([txn1, txn2])
    await session.commit()

    # Create rules
    await create_rule(
        session,
        test_user.id,
        RuleCreate(
            name="UBER apply-all",
            conditions_op="or",
            conditions=[RuleCondition(field="description", op="starts_with", value="UBER")],
            actions=[RuleAction(op="set_category", value=str(test_categories[1].id))],
            priority=10,
        ),
    )
    await create_rule(
        session,
        test_user.id,
        RuleCreate(
            name="IFOOD apply-all",
            conditions_op="or",
            conditions=[RuleCondition(field="description", op="starts_with", value="IFOOD")],
            actions=[RuleAction(op="set_category", value=str(test_categories[0].id))],
            priority=10,
        ),
    )

    count = await apply_all_rules(session, test_user.id)
    assert count >= 2

    await session.refresh(txn1)
    await session.refresh(txn2)
    assert txn1.category_id == test_categories[1].id  # transport
    assert txn2.category_id == test_categories[0].id  # food


# ---------------------------------------------------------------------------
# create_default_rules / install_rule_pack / get_installed_packs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_default_rules(session: AsyncSession, test_user):
    # Need default categories first so rule templates can resolve
    await create_default_categories(session, test_user.id, lang="pt-BR")

    rules = await create_default_rules(session, test_user.id, lang="pt-BR")
    assert len(rules) >= 3  # at least Streaming, Uber, Amazon, etc.

    names = {r.name for r in rules}
    assert "Uber" in names
    assert "Amazon" in names


@pytest.mark.asyncio
async def test_install_rule_pack_br(session: AsyncSession, test_user):
    await create_default_categories(session, test_user.id, lang="pt-BR")

    rules = await install_rule_pack(session, test_user.id, "BR", lang="pt-BR")
    assert len(rules) > 0

    names = {r.name for r in rules}
    assert "iFood / Rappi" in names


@pytest.mark.asyncio
async def test_install_rule_pack_skips_duplicates(session: AsyncSession, test_user):
    await create_default_categories(session, test_user.id, lang="pt-BR")

    first = await install_rule_pack(session, test_user.id, "BR", lang="pt-BR")
    second = await install_rule_pack(session, test_user.id, "BR", lang="pt-BR")

    assert len(first) > 0
    assert len(second) == 0  # all already installed


@pytest.mark.asyncio
async def test_install_rule_pack_unknown_returns_empty(session: AsyncSession, test_user):
    result = await install_rule_pack(session, test_user.id, "ZZ")
    assert result == []


@pytest.mark.asyncio
async def test_get_installed_packs(session: AsyncSession, test_user):
    await create_default_categories(session, test_user.id, lang="pt-BR")

    packs_before = await get_installed_packs(session, test_user.id)
    assert packs_before["BR"] is False

    await install_rule_pack(session, test_user.id, "BR", lang="pt-BR")

    packs_after = await get_installed_packs(session, test_user.id)
    assert packs_after["BR"] is True
