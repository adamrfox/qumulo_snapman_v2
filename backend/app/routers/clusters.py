import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CurrentUser
from app.config import settings
from app.database import get_db
from app.models import Cluster, User
from app.qumulo.client import ApiError, login as qumulo_login

router = APIRouter()


def _serialize(c: Cluster, owner_username: str | None = None) -> dict:
    d = {
        "id": c.id,
        "display_name": c.display_name,
        "host": c.host,
        "port": c.port,
        "insecure": c.insecure,
        "created_at": c.created_at.isoformat(),
        "owner_id": c.owner_id,
    }
    if owner_username is not None:
        d["owner_username"] = owner_username
    return d


async def get_authorized_cluster(
    cluster_id: str, user: CurrentUser, db: AsyncSession
) -> Cluster:
    result = await db.execute(select(Cluster).where(Cluster.id == cluster_id))
    cluster = result.scalar_one_or_none()
    if cluster is None:
        raise HTTPException(404, "Cluster not found")
    if user.role != "admin" and cluster.owner_id != user.id:
        raise HTTPException(403, "Not authorized")
    return cluster


def decrypt_token(encrypted: str) -> str:
    return settings.fernet.decrypt(encrypted.encode()).decode()


class ClusterCreate(BaseModel):
    display_name: str
    host: str
    port: int = 8000
    token: str | None = None
    username: str | None = None
    password: str | None = None
    insecure: bool = False

    @model_validator(mode="after")
    def _check_auth(self) -> "ClusterCreate":
        if not self.token and not (self.username and self.password):
            raise ValueError("Provide either a token or a username and password")
        return self


class ClusterUpdate(BaseModel):
    display_name: str | None = None
    token: str | None = None
    insecure: bool | None = None
    host: str | None = None
    port: int | None = None


@router.get("/")
async def list_clusters(user: CurrentUser, db: AsyncSession = Depends(get_db)):
    if user.role == "admin":
        result = await db.execute(select(Cluster).order_by(Cluster.created_at))
        clusters = result.scalars().all()
        owner_ids = list({c.owner_id for c in clusters})
        users_result = await db.execute(select(User).where(User.id.in_(owner_ids)))
        user_map = {u.id: u.username for u in users_result.scalars().all()}
        return [_serialize(c, user_map.get(c.owner_id)) for c in clusters]
    else:
        result = await db.execute(
            select(Cluster).where(Cluster.owner_id == user.id).order_by(Cluster.created_at)
        )
        return [_serialize(c) for c in result.scalars().all()]


@router.post("/", status_code=201)
async def create_cluster(
    req: ClusterCreate, user: CurrentUser, db: AsyncSession = Depends(get_db)
):
    if req.token:
        token = req.token
    else:
        try:
            token = await asyncio.get_event_loop().run_in_executor(
                None, qumulo_login, req.host, req.port, req.username, req.password, req.insecure
            )
        except ApiError as e:
            raise HTTPException(400, f"Qumulo login failed: {e}")
        except Exception as e:
            raise HTTPException(400, f"Could not reach cluster: {e}")

    encrypted = settings.fernet.encrypt(token.encode()).decode()
    cluster = Cluster(
        owner_id=user.id,
        display_name=req.display_name,
        host=req.host,
        port=req.port,
        token_encrypted=encrypted,
        insecure=req.insecure,
    )
    db.add(cluster)
    await db.commit()
    await db.refresh(cluster)
    return _serialize(cluster)


@router.get("/{cluster_id}")
async def get_cluster(
    cluster_id: str, user: CurrentUser, db: AsyncSession = Depends(get_db)
):
    cluster = await get_authorized_cluster(cluster_id, user, db)
    return _serialize(cluster)


@router.patch("/{cluster_id}")
async def update_cluster(
    cluster_id: str,
    req: ClusterUpdate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
):
    cluster = await get_authorized_cluster(cluster_id, user, db)
    if req.display_name is not None:
        cluster.display_name = req.display_name
    if req.host is not None:
        cluster.host = req.host
    if req.port is not None:
        cluster.port = req.port
    if req.token is not None:
        cluster.token_encrypted = settings.fernet.encrypt(req.token.encode()).decode()
    if req.insecure is not None:
        cluster.insecure = req.insecure
    await db.commit()
    return _serialize(cluster)


@router.delete("/{cluster_id}")
async def delete_cluster(
    cluster_id: str, user: CurrentUser, db: AsyncSession = Depends(get_db)
):
    cluster = await get_authorized_cluster(cluster_id, user, db)
    await db.delete(cluster)
    await db.commit()
    return {"ok": True}
