"""Calendar and attribution regressions found while integrating recurring invoices."""
import uuid
from datetime import date, datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core import app_clock
from app.models.app_settings import AppSetting
from app.models.user import User
from app.models.workspace import Workspace
from app.services import admin_service, invoice_schedule_service as schedules
from app.tasks import invoice_schedule_tasks


@pytest.fixture
def frozen_clock(monkeypatch):
    class FixedDatetime(datetime):
        instant = datetime(2026, 10, 5, 1, tzinfo=timezone.utc)

        @classmethod
        def now(cls, tz=None):
            return cls.instant.astimezone(tz)

    monkeypatch.setattr(app_clock, "datetime", FixedDatetime)
    monkeypatch.setattr(schedules, "datetime", FixedDatetime)
    return FixedDatetime


@pytest.mark.parametrize(
    "zone,instant,local_day",
    [
        ("America/Los_Angeles", datetime(2026, 10, 5, 1, tzinfo=timezone.utc), date(2026, 10, 4)),
        ("Asia/Tokyo", datetime(2026, 10, 4, 23, tzinfo=timezone.utc), date(2026, 10, 5)),
    ],
)
async def test_schedule_creation_keeps_the_workspace_first_period(
    client, auth_headers, session, test_workspace, frozen_clock, zone, instant, local_day
):
    frozen_clock.instant = instant
    test_workspace.kind = "business"
    test_workspace.timezone = zone
    session.add(AppSetting(key="timezone", value="UTC"))
    await session.commit()
    headers = {**auth_headers, "X-Workspace-Id": str(test_workspace.id)}

    # Both choosing "today" and leaving the default must keep period one.
    for dates in ({"start_date": local_day.isoformat()}, {}):
        response = await client.post(
            "/api/invoice-schedules",
            headers=headers,
            json={
                "name": "Monthly retainer", "frequency": "monthly", **dates,
                "lines": [{"description": "Service", "unit_price": "100"}],
            },
        )
        assert response.status_code == 201, response.text
        assert response.json()["start_date"] == local_day.isoformat()
        assert response.json()["next_sequence"] == 1
        assert response.json()["next_period_start"] == local_day.isoformat()


@pytest.mark.parametrize(
    "zone,instant,expected_count",
    [
        ("America/Los_Angeles", datetime(2026, 10, 5, 1, tzinfo=timezone.utc), 0),
        ("Asia/Tokyo", datetime(2026, 10, 4, 23, tzinfo=timezone.utc), 1),
    ],
)
async def test_schedule_worker_uses_each_workspace_calendar(
    session, test_user, test_workspace, frozen_clock, zone, instant, expected_count
):
    frozen_clock.instant = instant
    test_workspace.timezone = zone
    session.add(AppSetting(key="timezone", value="UTC"))
    schedule = await schedules.create_schedule(
        session, test_workspace.id, test_user.id,
        {"name": "Retainer", "frequency": "monthly", "start_date": date(2026, 10, 5),
         "lines": [{"description": "Service", "unit_price": "100"}]},
        today=date(2026, 10, 1),
    )
    await session.commit()
    maker = async_sessionmaker(session.bind, expire_on_commit=False)

    assert await invoice_schedule_tasks._generate_one(maker, schedule.id) == expected_count
    # The next hourly pass cannot bill the same period twice.
    assert await invoice_schedule_tasks._generate_one(maker, schedule.id) == 0
    await session.refresh(schedule)
    assert schedule.next_sequence == 1 + expected_count


@pytest.mark.parametrize(
    "cached_zone,saved_zone,expected_count",
    [("UTC", "America/Los_Angeles", 0), ("America/Los_Angeles", "UTC", 1)],
)
async def test_schedule_worker_reads_past_a_stale_application_timezone(
    session, test_user, test_workspace, frozen_clock, cached_zone, saved_zone, expected_count
):
    setting = AppSetting(key="timezone", value=cached_zone)
    session.add(setting)
    schedule = await schedules.create_schedule(
        session, test_workspace.id, test_user.id,
        {"name": "Retainer", "frequency": "monthly", "start_date": date(2026, 10, 5),
         "lines": [{"description": "Service", "unit_price": "100"}]},
        today=date(2026, 10, 1),
    )
    await session.commit()
    assert str(await app_clock.get_timezone(session)) == cached_zone
    # A different process saves the setting without invalidating this cache.
    setting.value = saved_zone
    await session.commit()
    assert str(await app_clock.get_timezone(session)) == cached_zone
    maker = async_sessionmaker(session.bind, expire_on_commit=False)

    assert await invoice_schedule_tasks._generate_one(maker, schedule.id) == expected_count
    await session.refresh(schedule)
    assert schedule.next_sequence == 1 + expected_count


async def test_deleting_a_schedule_author_preserves_and_continues_billing(postgres_sessions):
    async with postgres_sessions() as session:
        admin = User(
            id=uuid.uuid4(), email="admin@example.invalid", hashed_password="unused",
            is_active=True, is_superuser=True, is_verified=True,
        )
        author = User(
            id=uuid.uuid4(), email="author@example.invalid", hashed_password="unused",
            is_active=True, is_superuser=False, is_verified=True,
        )
        session.add_all([admin, author])
        await session.flush()
        workspace = Workspace(
            id=uuid.uuid4(), name="Business", kind="business", created_by_user_id=admin.id,
        )
        session.add(workspace)
        await session.flush()
        schedule = await schedules.create_schedule(
            session, workspace.id, author.id,
            {"name": "Retainer", "frequency": "monthly", "start_date": date(2026, 10, 5),
             "lines": [{"description": "Service", "unit_price": "100"}]},
            today=date(2026, 10, 1),
        )
        author_id = author.id
        await session.commit()

        assert await admin_service.delete_user(session, author_id, admin.id)
        await session.refresh(schedule)
        assert await session.get(User, author_id) is None
        assert schedule.user_id is None
        assert schedule.status == "active"
        [invoice] = await schedules.generate_due(session, schedule, today=date(2026, 10, 5))
        await session.commit()
        assert invoice.user_id is None
        assert invoice.workspace_id == workspace.id
        assert invoice.status == "open"
        assert invoice.sequence == 1 and invoice.total == 100
        assert schedule.next_sequence == 2
