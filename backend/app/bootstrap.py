import secrets
import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import hash_password
from app.config import settings
from app.models import User


async def ensure_admin(db: AsyncSession) -> None:
    result = await db.execute(select(User).limit(1))
    if result.scalar_one_or_none() is not None:
        return

    password = settings.admin_password
    if not password:
        password = secrets.token_urlsafe(16)
        print(
            f"[snapman] ADMIN_PASSWORD not set. Generated admin password: {password}",
            file=sys.stderr,
        )

    admin = User(username="admin", password_hash=hash_password(password), role="admin")
    db.add(admin)
    await db.commit()
    print("[snapman] Default admin user created (username: admin).", file=sys.stderr)
