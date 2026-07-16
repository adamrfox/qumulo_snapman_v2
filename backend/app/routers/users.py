from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CurrentUser, RequireAdmin, hash_password
from app.database import get_db
from app.models import User

router = APIRouter()

VALID_ROLES = {"admin", "operator", "viewer"}


def _serialize(u: User) -> dict:
    return {
        "id": u.id,
        "username": u.username,
        "role": u.role,
        "is_active": u.is_active,
        "created_at": u.created_at.isoformat(),
    }


class CreateUserRequest(BaseModel):
    username: str
    password: str
    role: str


class UpdateUserRequest(BaseModel):
    role: str | None = None
    password: str | None = None
    is_active: bool | None = None


@router.get("/")
async def list_users(admin: RequireAdmin, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).order_by(User.created_at))
    return [_serialize(u) for u in result.scalars().all()]


@router.post("/", status_code=201)
async def create_user(
    req: CreateUserRequest, admin: RequireAdmin, db: AsyncSession = Depends(get_db)
):
    if req.role not in VALID_ROLES:
        raise HTTPException(400, f"role must be one of: {', '.join(sorted(VALID_ROLES))}")
    existing = await db.execute(select(User).where(User.username == req.username))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(409, "Username already exists")

    user = User(
        username=req.username,
        password_hash=hash_password(req.password),
        role=req.role,
        created_by=admin.id,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return _serialize(user)


@router.patch("/{user_id}")
async def update_user(
    user_id: str,
    req: UpdateUserRequest,
    admin: RequireAdmin,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(404, "User not found")

    if req.role is not None:
        if req.role not in VALID_ROLES:
            raise HTTPException(400, f"role must be one of: {', '.join(sorted(VALID_ROLES))}")
        user.role = req.role
    if req.password is not None:
        user.password_hash = hash_password(req.password)
    if req.is_active is not None:
        if not req.is_active and user.id == admin.id:
            raise HTTPException(400, "Cannot deactivate yourself")
        user.is_active = req.is_active

    await db.commit()
    return _serialize(user)


@router.delete("/{user_id}")
async def deactivate_user(
    user_id: str, admin: RequireAdmin, db: AsyncSession = Depends(get_db)
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(404, "User not found")
    if user.id == admin.id:
        raise HTTPException(400, "Cannot deactivate yourself")
    user.is_active = False
    await db.commit()
    return {"ok": True}


@router.post("/me/password")
async def change_own_password(
    req: UpdateUserRequest, user: CurrentUser, db: AsyncSession = Depends(get_db)
):
    if req.password is None:
        raise HTTPException(400, "password is required")
    result = await db.execute(select(User).where(User.id == user.id))
    db_user = result.scalar_one()
    db_user.password_hash = hash_password(req.password)
    await db.commit()
    return {"ok": True}
