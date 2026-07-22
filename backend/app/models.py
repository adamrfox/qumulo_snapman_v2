import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def to_utc_iso(dt: datetime) -> str:
    """Every datetime column here is naive but stores UTC (via datetime.utcnow()
    defaults) -- serializing with plain .isoformat() drops that fact, so a
    browser's `new Date(...)` parses the string as local time and displays a
    value that's numerically UTC but mislabeled as local. Appending the UTC
    designator lets the frontend convert correctly to the viewer's actual
    local time."""
    return dt.isoformat() + "Z"


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    created_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class Cluster(Base):
    __tablename__ = "clusters"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    owner_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    host: Mapped[str] = mapped_column(String(256), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False, default=8000)
    token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    insecure: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)

    jobs: Mapped[list["InspectJob"]] = relationship(
        "InspectJob", back_populates="cluster", cascade="all, delete-orphan"
    )
    warm_trees: Mapped[list["WarmTree"]] = relationship(
        "WarmTree", back_populates="cluster", cascade="all, delete-orphan"
    )


class InspectJob(Base):
    __tablename__ = "inspect_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    cluster_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("clusters.id"), nullable=False
    )
    cluster_name: Mapped[str] = mapped_column(String(256), nullable=False)
    source_file_id: Mapped[str] = mapped_column(String(64), nullable=False)
    path: Mapped[str] = mapped_column(String(4096), nullable=False)
    started_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    job_type: Mapped[str] = mapped_column(String(32), nullable=False, default="inspect")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    started_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    cluster: Mapped["Cluster"] = relationship("Cluster", back_populates="jobs")


class WarmTree(Base):
    __tablename__ = "warm_trees"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    cluster_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("clusters.id"), nullable=False
    )
    source_file_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )

    cluster: Mapped["Cluster"] = relationship("Cluster", back_populates="warm_trees")

    __table_args__ = (
        UniqueConstraint("cluster_id", "source_file_id", name="uq_warm_trees_cluster_tree"),
    )
