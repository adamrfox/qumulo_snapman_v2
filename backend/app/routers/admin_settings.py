"""Admin-editable app-wide settings (as opposed to app/config.py's env-var
settings, which are fixed at deploy time). Currently just one: how long a
Qumulo access token this app derives from a username+password login should
live (see clusters.py) -- None means it never expires.
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import RequireAdmin
from app.database import get_db
from app.models import AppSettings

router = APIRouter()

DEFAULT_ACCESS_TOKEN_LIFETIME_DAYS = 365


async def get_settings(db: AsyncSession) -> AppSettings:
    result = await db.execute(select(AppSettings).where(AppSettings.id == 1))
    row = result.scalar_one_or_none()
    if row is None:
        row = AppSettings(id=1, access_token_lifetime_days=DEFAULT_ACCESS_TOKEN_LIFETIME_DAYS)
        db.add(row)
        await db.commit()
        await db.refresh(row)
    return row


def _serialize(s: AppSettings) -> dict:
    return {"access_token_lifetime_days": s.access_token_lifetime_days}


class SettingsUpdate(BaseModel):
    access_token_lifetime_days: int | None

    @field_validator("access_token_lifetime_days")
    @classmethod
    def _positive(cls, v: int | None) -> int | None:
        if v is not None and v <= 0:
            raise ValueError("access_token_lifetime_days must be positive, or null for never-expiring")
        return v


@router.get("/settings")
async def get_settings_endpoint(admin: RequireAdmin, db: AsyncSession = Depends(get_db)):
    return _serialize(await get_settings(db))


@router.patch("/settings")
async def update_settings(
    req: SettingsUpdate, admin: RequireAdmin, db: AsyncSession = Depends(get_db)
):
    settings_row = await get_settings(db)
    settings_row.access_token_lifetime_days = req.access_token_lifetime_days
    await db.commit()
    return _serialize(settings_row)
