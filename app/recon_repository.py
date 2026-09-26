"""批量对账的数据库访问层。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Sequence

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .models import ReconBatch, ReconException, ReconItem


def get_batch(db: Session, batch_id: str) -> ReconBatch | None:
    return db.get(ReconBatch, batch_id)


def list_batches(db: Session) -> Sequence[ReconBatch]:
    stmt = select(ReconBatch).order_by(ReconBatch.created_at, ReconBatch.batch_id)
    return db.execute(stmt).scalars().all()


def insert_batch(
    db: Session,
    *,
    batch_id: str,
    created_by: str,
    items: list[dict[str, Any]],
) -> ReconBatch:
    """写入批次与批次项；调用方需先确认 batch_id 不存在。"""
    batch = ReconBatch(batch_id=batch_id, created_by=created_by, status="created")
    db.add(batch)
    for item in items:
        db.add(ReconItem(**item))
    db.commit()
    db.refresh(batch)
    return batch


def list_items(db: Session, batch_id: str) -> list[ReconItem]:
    stmt = (
        select(ReconItem)
        .where(ReconItem.batch_id == batch_id)
        .order_by(ReconItem.seq)
    )
    return list(db.execute(stmt).scalars().all())


def get_item(db: Session, item_id: str) -> ReconItem | None:
    return db.get(ReconItem, item_id)


def claim_item_for_run(db: Session, item_id: str) -> ReconItem | None:
    """把待处理/失败/中断的批次项原子地标记为 running 并累计尝试次数。

    断点续跑依赖该操作是幂等的：已 done 的项返回 None，不再重复处理。
    """
    stmt = (
        update(ReconItem)
        .where(ReconItem.item_id == item_id)
        .where(ReconItem.status.in_(["pending", "failed", "running"]))
        .values(status="running", attempts=ReconItem.attempts + 1, error=None)
    )
    result = db.execute(stmt)
    db.commit()
    if result.rowcount == 0:  # type: ignore[union-attr]
        return None
    return db.get(ReconItem, item_id)


def save_item_progress(
    db: Session,
    item_id: str,
    *,
    cursor: int,
    matched_count: int,
    exception_count: int,
) -> None:
    """提交一个分片后的断点位置，崩溃后可从该位置继续。"""
    stmt = (
        update(ReconItem)
        .where(ReconItem.item_id == item_id)
        .values(
            cursor=cursor, matched_count=matched_count, exception_count=exception_count
        )
    )
    db.execute(stmt)
    db.commit()


def finish_item(
    db: Session,
    item_id: str,
    *,
    status: str,
    cursor: int,
    matched_count: int,
    exception_count: int,
    error: str | None,
) -> None:
    stmt = (
        update(ReconItem)
        .where(ReconItem.item_id == item_id)
        .values(
            status=status,
            cursor=cursor,
            matched_count=matched_count,
            exception_count=exception_count,
            error=error,
        )
    )
    db.execute(stmt)
    db.commit()


def insert_exceptions_ignore_duplicates(
    db: Session, rows: list[dict[str, Any]]
) -> int:
    """插入差异记录；确定性主键冲突时忽略，保证重试不产生重复。"""
    if not rows:
        return 0
    stmt = sqlite_insert(ReconException)
    stmt = stmt.values(rows).on_conflict_do_nothing(index_elements=["exception_id"])
    result = db.execute(stmt)
    return int(result.rowcount or 0)  # type: ignore[union-attr]


def update_batch_status(
    db: Session,
    batch_id: str,
    *,
    status: str,
    signed_by: str | None = None,
    signed_at: datetime | None = None,
) -> None:
    values: dict[str, Any] = {
        "status": status,
        "version": ReconBatch.version + 1,
    }
    if signed_by is not None:
        values["signed_by"] = signed_by
        values["signed_at"] = signed_at
    stmt = update(ReconBatch).where(ReconBatch.batch_id == batch_id).values(**values)
    db.execute(stmt)
    db.commit()


def get_exception(db: Session, exception_id: str) -> ReconException | None:
    return db.get(ReconException, exception_id)


def list_exceptions(
    db: Session,
    batch_id: str,
    *,
    status: str | None = None,
    assignee: str | None = None,
) -> list[ReconException]:
    stmt = (
        select(ReconException)
        .where(ReconException.batch_id == batch_id)
        .order_by(
            ReconException.student_id,
            ReconException.activity_type,
            ReconException.academic_day,
            ReconException.category,
        )
    )
    if status is not None:
        stmt = stmt.where(ReconException.status == status)
    if assignee is not None:
        stmt = stmt.where(ReconException.assignee == assignee)
    return list(db.execute(stmt).scalars().all())


def count_exceptions_by_status(db: Session, batch_id: str) -> dict[str, int]:
    stmt = (
        select(ReconException.status, func.count())
        .where(ReconException.batch_id == batch_id)
        .group_by(ReconException.status)
    )
    return {status: count for status, count in db.execute(stmt).all()}


def claim_exception(
    db: Session, exception_id: str, *, assignee: str, claimed_at: datetime
) -> bool:
    """原子认领：仅当差异仍处于 open 时成功，并发下只有一个认领者。"""
    stmt = (
        update(ReconException)
        .where(ReconException.exception_id == exception_id)
        .where(ReconException.status == "open")
        .values(
            status="claimed",
            assignee=assignee,
            claimed_at=claimed_at,
            version=ReconException.version + 1,
        )
    )
    result = db.execute(stmt)
    db.commit()
    return result.rowcount == 1  # type: ignore[union-attr]


def review_exception(
    db: Session,
    exception_id: str,
    *,
    expected_version: int,
    decision: str,
    reviewer: str,
    note: str,
    reviewed_at: datetime,
) -> bool:
    """原子复核：版本与状态同时匹配才生效，并发复核只有一个成功。"""
    stmt = (
        update(ReconException)
        .where(ReconException.exception_id == exception_id)
        .where(ReconException.status == "claimed")
        .where(ReconException.version == expected_version)
        .values(
            status=decision,
            reviewer=reviewer,
            review_note=note,
            reviewed_at=reviewed_at,
            version=ReconException.version + 1,
        )
    )
    result = db.execute(stmt)
    db.commit()
    return result.rowcount == 1  # type: ignore[union-attr]
