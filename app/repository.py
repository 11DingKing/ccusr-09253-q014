"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .core.replay import Event as CoreEvent
from .core.replay import EventType
from .models import Event as EventModel
from .models import Freeze, Plan, ReconAudit, ReconBatch, ReconException, ReconItem


def get_plan(db: Session, plan_version: str) -> Plan | None:
    return db.get(Plan, plan_version)


def upsert_plan(
    db: Session,
    *,
    plan_version: str,
    iana_timezone: str,
    required_seconds: int,
) -> Plan:
    stmt = sqlite_insert(Plan).values(
        plan_version=plan_version,
        iana_timezone=iana_timezone,
        required_seconds=required_seconds,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["plan_version"],
        set_={
            "iana_timezone": iana_timezone,
            "required_seconds": required_seconds,
        },
    )
    db.execute(stmt)
    db.commit()
    plan = db.get(Plan, plan_version)
    assert plan is not None
    return plan


def _to_core_event(row: EventModel) -> CoreEvent:
    return CoreEvent(
        event_id=row.event_id,
        plan_version=row.plan_version,
        event_type=EventType(row.event_type),
        student_id=row.student_id,
        payload=dict(row.payload),
        created_at=row.created_at,
    )


def insert_events(
    db: Session,
    *,
    plan_version: str,
    events: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    """执行确定性的业务处理。"""
    accepted: list[str] = []
    duplicates: list[str] = []
    for e in events:
        stmt = sqlite_insert(EventModel).values(
            event_id=e["event_id"],
            plan_version=plan_version,
            student_id=e["student_id"],
            event_type=e["event_type"],
            payload=e["payload"],
        )
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["event_id", "plan_version"]
        ).returning(EventModel.id)
        inserted_id = db.execute(stmt).scalar_one_or_none()
        if inserted_id is not None:
            accepted.append(e["event_id"])
        else:
            duplicates.append(e["event_id"])
    db.commit()
    return accepted, duplicates


def load_events(db: Session, plan_version: str) -> list[CoreEvent]:
    stmt = select(EventModel).where(EventModel.plan_version == plan_version)
    rows = db.execute(stmt).scalars().all()
    return [_to_core_event(r) for r in rows]


def load_events_up_to(
    db: Session, plan_version: str, max_event_id: str
) -> list[CoreEvent]:
    """执行确定性的业务处理。"""
    stmt = (
        select(EventModel)
        .where(EventModel.plan_version == plan_version)
        .where(EventModel.event_id <= max_event_id)
    )
    rows = db.execute(stmt).scalars().all()
    return [_to_core_event(r) for r in rows]


def max_event_id(db: Session, plan_version: str) -> str | None:
    stmt = (
        select(EventModel.event_id)
        .where(EventModel.plan_version == plan_version)
        .order_by(EventModel.event_id.desc())
        .limit(1)
    )
    return db.execute(stmt).scalar_one_or_none()


def get_freeze(
    db: Session, plan_version: str, freeze_id: str
) -> Freeze | None:
    return db.get(Freeze, (plan_version, freeze_id))


def insert_freeze(
    db: Session,
    *,
    plan_version: str,
    freeze_id: str,
    snapshot: dict[str, Any],
    event_cutoff_id: str | None,
) -> Freeze | None:
    """执行确定性的业务处理。"""
    stmt = sqlite_insert(Freeze).values(
        plan_version=plan_version,
        freeze_id=freeze_id,
        snapshot=snapshot,
        event_cutoff_id=event_cutoff_id,
    )
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["plan_version", "freeze_id"]
    ).returning(Freeze.plan_version)
    inserted = db.execute(stmt).scalar_one_or_none()
    db.commit()
    if inserted is not None:
        return db.get(Freeze, (plan_version, freeze_id))
    return None


# --- 对账批次 ---
# 以下函数只负责 flush，事务边界（每个条目一次提交的检查点）由服务层控制。


def insert_recon_batch(
    db: Session,
    *,
    batch_id: str,
    title: str,
    dept: str,
    created_by: str,
    external_entries: list[dict[str, Any]],
    external_fingerprint: str,
    supersedes_batch_id: str | None,
    total_items: int,
) -> ReconBatch:
    batch = ReconBatch(
        batch_id=batch_id,
        title=title,
        dept=dept,
        created_by=created_by,
        status="created",
        external_entries=external_entries,
        external_fingerprint=external_fingerprint,
        supersedes_batch_id=supersedes_batch_id,
        total_items=total_items,
    )
    db.add(batch)
    db.flush()
    return batch


def get_recon_batch(db: Session, batch_id: str) -> ReconBatch | None:
    return db.get(ReconBatch, batch_id)


def list_recon_batches(db: Session, dept: str) -> list[ReconBatch]:
    stmt = (
        select(ReconBatch)
        .where(ReconBatch.dept == dept)
        .order_by(ReconBatch.batch_id)
    )
    return list(db.execute(stmt).scalars().all())


def insert_recon_item(
    db: Session,
    *,
    batch_id: str,
    plan_version: str,
    freeze_id: str,
    snapshot_fingerprint: str | None,
) -> ReconItem:
    item = ReconItem(
        batch_id=batch_id,
        plan_version=plan_version,
        freeze_id=freeze_id,
        status="pending",
        snapshot_fingerprint=snapshot_fingerprint,
    )
    db.add(item)
    db.flush()
    return item


def list_recon_items(db: Session, batch_id: str) -> list[ReconItem]:
    stmt = (
        select(ReconItem)
        .where(ReconItem.batch_id == batch_id)
        .order_by(ReconItem.plan_version, ReconItem.freeze_id)
    )
    return list(db.execute(stmt).scalars().all())


def reset_stale_processing_items(db: Session, batch_id: str) -> int:
    """把崩溃残留的 processing 条目重置为 pending，用于重启恢复。"""
    stmt = (
        update(ReconItem)
        .where(ReconItem.batch_id == batch_id)
        .where(ReconItem.status == "processing")
        .values(status="pending")
    )
    result = db.execute(stmt)
    return result.rowcount or 0


def runnable_recon_items(db: Session, batch_id: str) -> list[ReconItem]:
    """待处理与失败条目（断点续跑跳过已完成条目）。"""
    stmt = (
        select(ReconItem)
        .where(ReconItem.batch_id == batch_id)
        .where(ReconItem.status.in_(["pending", "error"]))
        .order_by(ReconItem.plan_version, ReconItem.freeze_id)
    )
    return list(db.execute(stmt).scalars().all())


def delete_item_exceptions(
    db: Session, *, batch_id: str, plan_version: str, freeze_id: str
) -> None:
    stmt = delete(ReconException).where(
        ReconException.batch_id == batch_id,
        ReconException.plan_version == plan_version,
        ReconException.freeze_id == freeze_id,
    )
    db.execute(stmt)


def insert_recon_exceptions(
    db: Session, rows: list[dict[str, Any]]
) -> None:
    if not rows:
        return
    db.add_all(ReconException(**row) for row in rows)
    db.flush()


def get_recon_exception(
    db: Session, *, batch_id: str, exception_id: str
) -> ReconException | None:
    stmt = select(ReconException).where(
        ReconException.batch_id == batch_id,
        ReconException.exception_id == exception_id,
    )
    return db.execute(stmt).scalar_one_or_none()


def list_recon_exceptions(
    db: Session, batch_id: str, status: str | None = None
) -> list[ReconException]:
    stmt = select(ReconException).where(ReconException.batch_id == batch_id)
    if status is not None:
        stmt = stmt.where(ReconException.status == status)
    stmt = stmt.order_by(ReconException.exception_id)
    return list(db.execute(stmt).scalars().all())


def count_recon_exceptions(db: Session, batch_id: str) -> dict[str, int]:
    stmt = (
        select(ReconException.status, func.count())
        .where(ReconException.batch_id == batch_id)
        .group_by(ReconException.status)
    )
    counts = {"open": 0, "claimed": 0, "reviewed": 0}
    for status_value, n in db.execute(stmt).all():
        counts[status_value] = n
    return counts


def add_recon_audit(
    db: Session,
    *,
    batch_id: str,
    action: str,
    actor_id: str,
    detail: str = "",
) -> ReconAudit:
    stmt = select(func.coalesce(func.max(ReconAudit.seq), 0)).where(
        ReconAudit.batch_id == batch_id
    )
    seq = db.execute(stmt).scalar_one()
    entry = ReconAudit(
        batch_id=batch_id,
        seq=seq + 1,
        action=action,
        actor_id=actor_id,
        detail=detail,
    )
    db.add(entry)
    db.flush()
    return entry


def list_recon_audits(db: Session, batch_id: str) -> list[ReconAudit]:
    stmt = (
        select(ReconAudit)
        .where(ReconAudit.batch_id == batch_id)
        .order_by(ReconAudit.seq)
    )
    return list(db.execute(stmt).scalars().all())
