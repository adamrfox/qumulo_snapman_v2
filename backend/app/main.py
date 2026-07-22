from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import warm_sweep
from app.bootstrap import ensure_admin
from app.database import SessionLocal
from app.routers import admin_logs, auth, clusters, inspect, users


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with SessionLocal() as db:
        await ensure_admin(db)
    await warm_sweep.start()
    yield
    await warm_sweep.stop()


app = FastAPI(title="snapman", lifespan=lifespan)

app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
app.include_router(users.router, prefix="/api/users", tags=["users"])
app.include_router(clusters.router, prefix="/api/clusters", tags=["clusters"])
app.include_router(inspect.router, prefix="/api/clusters", tags=["inspect"])
app.include_router(admin_logs.router, prefix="/api/admin", tags=["admin"])
