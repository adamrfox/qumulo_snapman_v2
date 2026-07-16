from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.bootstrap import ensure_admin
from app.database import SessionLocal
from app.routers import auth, clusters, inspect, users


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with SessionLocal() as db:
        await ensure_admin(db)
    yield


app = FastAPI(title="snapman", lifespan=lifespan)

app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
app.include_router(users.router, prefix="/api/users", tags=["users"])
app.include_router(clusters.router, prefix="/api/clusters", tags=["clusters"])
app.include_router(inspect.router, prefix="/api/clusters", tags=["inspect"])
