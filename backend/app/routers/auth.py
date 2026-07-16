from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CurrentUser, create_token, set_auth_cookie, verify_password
from app.database import get_db
from app.models import User

router = APIRouter()


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/login")
async def login(
    req: LoginRequest, response: Response, db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(User).where(User.username == req.username, User.is_active == True)  # noqa: E712
    )
    user = result.scalar_one_or_none()
    if user is None or not verify_password(req.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = create_token(user.id, user.role)
    set_auth_cookie(response, token)
    return {"username": user.username, "role": user.role, "id": user.id}


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie("access_token")
    return {"ok": True}


@router.get("/me")
async def me(user: CurrentUser):
    return {"id": user.id, "username": user.username, "role": user.role}
