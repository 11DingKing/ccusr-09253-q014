"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Plan(Base):
    __tablename__ = "plans"

    plan_version: Mapped[str] = mapped_column(String(128), primary_key=True)
    iana_timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    required_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (
        CheckConstraint("required_seconds >= 0", name="ck_plans_required_nonneg"),
    )


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    plan_version: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    student_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("event_id", "plan_version", name="uq_events_event_id_plan"),
        Index("ix_events_plan_student", "plan_version", "student_id"),
    )


class Freeze(Base):
    __tablename__ = "freezes"

    plan_version: Mapped[str] = mapped_column(String(128), primary_key=True)
    freeze_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    event_cutoff_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )


class ReconBatch(Base):
    """一次批量对账作业：固定若干冻结快照与外部汇总行的内容指纹。"""

    __tablename__ = "recon_batches"

    batch_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="created")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    signed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('created','running','completed','completed_with_failures',"
            "'failed','signed')",
            name="ck_recon_batches_status",
        ),
    )


class ReconItem(Base):
    """批次中的一个对账单元：一份冻结快照对一份外部汇总内容。"""

    __tablename__ = "recon_items"

    item_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    batch_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    plan_version: Mapped[str] = mapped_column(String(128), nullable=False)
    freeze_id: Mapped[str] = mapped_column(String(128), nullable=False)
    snapshot_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    external_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    external_rows: Mapped[list] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cursor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    matched_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    exception_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(String(512), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("batch_id", "seq", name="uq_recon_items_batch_seq"),
        CheckConstraint(
            "status IN ('pending','running','done','failed')",
            name="ck_recon_items_status",
        ),
        CheckConstraint("attempts >= 0", name="ck_recon_items_attempts_nonneg"),
        CheckConstraint("cursor >= 0", name="ck_recon_items_cursor_nonneg"),
        Index("ix_recon_items_batch_status", "batch_id", "status"),
    )


class ReconException(Base):
    """一条对账差异：按学生、活动类型和日期定位，可认领与复核。"""

    __tablename__ = "recon_exceptions"

    exception_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    batch_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    item_id: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    student_id: Mapped[str] = mapped_column(String(128), nullable=False)
    activity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    academic_day: Mapped[str] = mapped_column(String(10), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    snapshot_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    external_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    delta_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    assignee: Mapped[str | None] = mapped_column(String(128), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewer: Mapped[str | None] = mapped_column(String(128), nullable=True)
    review_note: Mapped[str | None] = mapped_column(String(512), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "item_id",
            "student_id",
            "activity_type",
            "academic_day",
            "category",
            name="uq_recon_exceptions_natural_key",
        ),
        CheckConstraint(
            "status IN ('open','claimed','resolved','dismissed')",
            name="ck_recon_exceptions_status",
        ),
        CheckConstraint(
            "category IN ('missing_in_external','missing_in_snapshot','seconds_mismatch')",
            name="ck_recon_exceptions_category",
        ),
        Index("ix_recon_exceptions_batch_status", "batch_id", "status"),
        Index("ix_recon_exceptions_assignee", "assignee"),
    )
