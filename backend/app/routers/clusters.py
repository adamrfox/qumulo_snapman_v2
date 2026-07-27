import asyncio
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CurrentUser
from app.config import settings
from app.database import get_db
from app.models import Cluster, User, to_utc_iso
from app.qumulo.client import ApiError, create_access_token, login as qumulo_login, revoke_access_token, who_am_i
from app.routers.admin_settings import get_settings as get_app_settings

router = APIRouter()


def _serialize(c: Cluster, owner_username: str | None = None) -> dict:
    d = {
        "id": c.id,
        "display_name": c.display_name,
        "host": c.host,
        "port": c.port,
        "insecure": c.insecure,
        "created_at": to_utc_iso(c.created_at),
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


async def _login_and_create_access_token(
    host: str, port: int, username: str, password: str, insecure: bool, db: AsyncSession
) -> tuple[str, str, datetime | None]:
    """Log in with username/password, then immediately trade that session for
    a Qumulo access token (see app/qumulo/client.py) with the admin-configured
    lifetime -- never store the raw session token, since its lifetime is
    fixed and outside this app's control. Returns (token_id, bearer_token,
    expires_at)."""
    loop = asyncio.get_event_loop()
    session_token = await loop.run_in_executor(None, qumulo_login, host, port, username, password, insecure)
    who = await loop.run_in_executor(None, who_am_i, host, port, session_token, insecure)

    lifetime_days = (await get_app_settings(db)).access_token_lifetime_days
    expiration: str | None = None
    expires_at: datetime | None = None
    if lifetime_days:
        # Naive-but-UTC, matching every other datetime column in this app
        # (see to_utc_iso) -- token_expires_at is TIMESTAMP WITHOUT TIME ZONE,
        # which asyncpg refuses to accept a tz-aware value for.
        expires_at = datetime.utcnow() + timedelta(days=lifetime_days)
        expiration = expires_at.strftime("%Y-%m-%dT%H:%M:%SZ")

    token_id, bearer_token = await loop.run_in_executor(
        None, create_access_token, host, port, session_token, {"sid": who["sid"]}, expiration, insecure
    )
    return token_id, bearer_token, expires_at


async def _revoke_outgoing_token_at(
    host: str, port: int, token_encrypted: str, token_id: str, insecure: bool
) -> None:
    """Best-effort: revoke an access token this app previously created,
    against the host/port/credentials it actually belongs to. A stale
    unrevoked token left behind on the customer's cluster is a minor
    cleanliness issue, not worth failing the request over -- so any failure
    here is swallowed."""
    try:
        current_token = decrypt_token(token_encrypted)
        await asyncio.get_event_loop().run_in_executor(
            None, revoke_access_token, host, port, current_token, token_id, insecure
        )
    except Exception:
        pass


async def _revoke_outgoing_token(cluster: Cluster) -> None:
    if cluster.token_id:
        await _revoke_outgoing_token_at(
            cluster.host, cluster.port, cluster.token_encrypted, cluster.token_id, cluster.insecure
        )


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
    username: str | None = None
    password: str | None = None
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
    token_id: str | None = None
    token_expires_at: datetime | None = None
    if req.token:
        token = req.token
    else:
        try:
            token_id, token, token_expires_at = await _login_and_create_access_token(
                req.host, req.port, req.username, req.password, req.insecure, db
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
        token_id=token_id,
        token_expires_at=token_expires_at,
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
    # Snapshot the outgoing host/port/token before anything is mutated below
    # -- a revoke has to target wherever the *old* token actually lives, not
    # a host/port this same request may also be changing.
    old_host, old_port = cluster.host, cluster.port
    old_token_encrypted, old_token_id = cluster.token_encrypted, cluster.token_id

    if req.display_name is not None:
        cluster.display_name = req.display_name
    if req.host is not None:
        cluster.host = req.host
    if req.port is not None:
        cluster.port = req.port
    if req.insecure is not None:
        cluster.insecure = req.insecure

    if req.token is not None:
        if old_token_id:
            await _revoke_outgoing_token_at(old_host, old_port, old_token_encrypted, old_token_id, cluster.insecure)
        cluster.token_encrypted = settings.fernet.encrypt(req.token.encode()).decode()
        cluster.token_id = None
        cluster.token_expires_at = None
    elif req.username and req.password:
        try:
            token_id, token, token_expires_at = await _login_and_create_access_token(
                cluster.host, cluster.port, req.username, req.password, cluster.insecure, db
            )
        except ApiError as e:
            raise HTTPException(400, f"Qumulo login failed: {e}")
        except Exception as e:
            raise HTTPException(400, f"Could not reach cluster: {e}")
        if old_token_id:
            await _revoke_outgoing_token_at(old_host, old_port, old_token_encrypted, old_token_id, cluster.insecure)
        cluster.token_encrypted = settings.fernet.encrypt(token.encode()).decode()
        cluster.token_id = token_id
        cluster.token_expires_at = token_expires_at

    await db.commit()
    return _serialize(cluster)


@router.delete("/{cluster_id}")
async def delete_cluster(
    cluster_id: str, user: CurrentUser, db: AsyncSession = Depends(get_db)
):
    cluster = await get_authorized_cluster(cluster_id, user, db)
    await _revoke_outgoing_token(cluster)
    await db.delete(cluster)
    await db.commit()
    return {"ok": True}
