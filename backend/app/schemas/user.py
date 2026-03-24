import uuid
from typing import Optional

from fastapi_users import schemas
from pydantic import BaseModel


class UserPreferences(BaseModel):
    language: str = "en"
    date_format: str = "MM/DD/YYYY"
    timezone: str = "UTC"
    currency_display: str = "USD"
    onboarding_completed: bool = False


class UserRead(schemas.BaseUser[uuid.UUID]):
    preferences: Optional[dict] = None


class UserCreate(schemas.BaseUserCreate):
    pass


class UserUpdate(schemas.BaseUserUpdate):
    preferences: Optional[dict] = None
