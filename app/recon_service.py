"""对账批次服务：创建、断点续跑、认领、复核、签署与导出。

事务边界：运行作业时每个对账条目一次提交（检查点），崩溃后可通过
reset_stale_processing_items 恢复并续跑；已完成条目永不重跑，失败条目
幂等重试（异常项标识由内容派生，重算结果一致）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import update
from sqlalchemy.orm import Session

from . import repository as repo
from .auth import Actor
from .core import reconcile as core
from .models import ReconBatch, ReconException, ReconItem


class ReconNotFoundError(Exception):
    """批次或异常项不存在。"""


class ReconConflictError(Exception):
    """状态冲突（已签署、已认领、未完成等）。"""


class ReconPermissionError(Exception):
    """操作者角色或权限范围不足。"""


class ReconItemError(Exception):
    """单个对账条目处理失败，可续跑重试。"""


SIGNABLE_STATUSES = ("completed", "partial")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _require_batch(db: Session, batch_id: str) -> ReconBatch:
    batch = repo.get_recon_batch(db, batch_id)
    if batch is None:
        raise ReconNotFoundError(f"recon batch '{batch_id}' does not exist")
    return batch


def _require_scope(batch: ReconBatch, actor: Actor) -> None:
    if batch.dept != actor.dept:
        raise ReconPermissionError("操作者不在该批次的权限范围内")


def _require_not_signed(batch: ReconBatch) -> None:
    if batch.status == "signed":
        raise ReconConflictError("批次已签署，结果保持不变")


def _item_detail(item: ReconItem) -> dict[str, Any]:
    return {
        "plan_version": item.plan_version,
        "freeze_id": item.freeze_id,
        "status": item.status,
        "snapshot_fingerprint": item.snapshot_fingerprint,
        "matched_count": item.matched_count,
        "discrepancy_count": item.discrepancy_count,
        "attempt": item.attempt,
        "error": item.error,
    }


def _batch_summary(batch: ReconBatch) -> dict[str, Any]:
    return {
        "batch_id": batch.batch_id,
        "title": batch.title,
        "dept": batch.dept,
        "created_by": batch.created_by,
        "status": batch.status,
        "external_fingerprint": batch.external_fingerprint,
        "supersedes_batch_id": batch.supersedes_batch_id,
        "total_items": batch.total_items,
        "processed_items": batch.processed_items,
        "matched_count": batch.matched_count,
        "discrepancy_count": batch.discrepancy_count,
        "error_count": batch.error_count,
        "signed_by": batch.signed_by,
        "signed_at": _iso(batch.signed_at),
        "created_at": _iso(batch.created_at),
    }


def _batch_detail(db: Session, batch: ReconBatch) -> dict[str, Any]:
    detail = _batch_summary(batch)
    detail["items"] = [
        _item_detail(i) for i in repo.list_recon_items(db, batch.batch_id)
    ]
    detail["exception_status"] = repo.count_recon_exceptions(db, batch.batch_id)
    return detail


def _exception_detail(exc: ReconException) -> dict[str, Any]:
    return {
        "exception_id": exc.exception_id,
        "batch_id": exc.batch_id,
        "plan_version": exc.plan_version,
        "freeze_id": exc.freeze_id,
        "student_id": exc.student_id,
        "activity_type": exc.activity_type,
        "academic_day": exc.academic_day,
        "category": exc.category,
        "internal_seconds": exc.internal_seconds,
        "external_seconds": exc.external_seconds,
        "delta_seconds": exc.delta_seconds,
        "status": exc.status,
        "assignee": exc.assignee,
        "claimed_at": _iso(exc.claimed_at),
        "reviewed_by": exc.reviewed_by,
        "reviewed_at": _iso(exc.reviewed_at),
        "review_verdict": exc.review_verdict,
        "review_note": exc.review_note,
        "version": exc.version,
    }


def create_batch(
    db: Session,
    *,
    actor: Actor,
    batch_id: str,
    title: str,
    items: list[dict[str, str]],
    external_entries: list[dict[str, Any]],
    supersedes_batch_id: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """创建对账批次并固定输入指纹；内容相同的重复创建返回已有批次。

    外部文件更正必须换新的 batch_id 并通过 supersedes_batch_id 关联原批次，
    原批次结果保持不变。
    """
    batch_id = batch_id.strip()
    if not batch_id:
        raise ReconConflictError("批次标识不能为空")
    keys = [(i["plan_version"], i["freeze_id"]) for i in items]
    if len(set(keys)) != len(keys):
        raise ReconConflictError("对账条目重复")
    normalized = core.normalize_external_entries(external_entries)
    fingerprint = core.canonical_fingerprint(normalized)

    existing = repo.get_recon_batch(db, batch_id)
    if existing is not None:
        _require_scope(existing, actor)
        existing_keys = sorted(
            (i.plan_version, i.freeze_id)
            for i in repo.list_recon_items(db, batch_id)
        )
        if (
            existing.external_fingerprint == fingerprint
            and existing_keys == sorted(keys)
            and (existing.supersedes_batch_id or None) == (supersedes_batch_id or None)
        ):
            return _batch_detail(db, existing), False
        raise ReconConflictError(f"批次 '{batch_id}' 已存在且内容不同")

    if supersedes_batch_id:
        superseded = repo.get_recon_batch(db, supersedes_batch_id)
        if superseded is None:
            raise ReconNotFoundError(
                f"被更正的批次 '{supersedes_batch_id}' 不存在"
            )
        _require_scope(superseded, actor)

    batch = repo.insert_recon_batch(
        db,
        batch_id=batch_id,
        title=title.strip(),
        dept=actor.dept,
        created_by=actor.actor_id,
        external_entries=normalized,
        external_fingerprint=fingerprint,
        supersedes_batch_id=supersedes_batch_id,
        total_items=len(items),
    )
    for plan_version, freeze_id in keys:
        freeze = repo.get_freeze(db, plan_version, freeze_id)
        snapshot_fp = (
            core.canonical_fingerprint(freeze.snapshot)
            if freeze is not None
            else None
        )
        repo.insert_recon_item(
            db,
            batch_id=batch_id,
            plan_version=plan_version,
            freeze_id=freeze_id,
            snapshot_fingerprint=snapshot_fp,
        )
    repo.add_recon_audit(
        db,
        batch_id=batch_id,
        action="batch_created",
        actor_id=actor.actor_id,
        detail=f"items={len(items)} external={fingerprint[:12]}",
    )
    db.commit()
    return _batch_detail(db, batch), True


def list_batches(db: Session, *, actor: Actor) -> list[dict[str, Any]]:
    return [_batch_summary(b) for b in repo.list_recon_batches(db, actor.dept)]


def get_batch_detail(
    db: Session, *, batch_id: str, actor: Actor
) -> dict[str, Any]:
    batch = _require_batch(db, batch_id)
    _require_scope(batch, actor)
    return _batch_detail(db, batch)


def _process_item(
    db: Session, batch: ReconBatch, item: ReconItem
) -> dict[str, int]:
    """处理单个对账条目：校验指纹、重算差异并整体替换该条目的异常项。"""
    freeze = repo.get_freeze(db, item.plan_version, item.freeze_id)
    if freeze is None:
        raise ReconItemError(
            f"freeze '{item.freeze_id}' for plan '{item.plan_version}' not found"
        )
    fingerprint = core.canonical_fingerprint(freeze.snapshot)
    if item.snapshot_fingerprint is None:
        # 创建时快照尚不存在，运行时补钉内容指纹。
        item.snapshot_fingerprint = fingerprint
    elif item.snapshot_fingerprint != fingerprint:
        raise ReconItemError("snapshot fingerprint mismatch: frozen input changed")
    internal = core.extract_internal_facts(freeze.snapshot)
    external = core.aggregate_external(batch.external_entries, item.plan_version)
    matched, discrepancies = core.reconcile_facts(internal, external)
    repo.delete_item_exceptions(
        db,
        batch_id=batch.batch_id,
        plan_version=item.plan_version,
        freeze_id=item.freeze_id,
    )
    rows = [
        {
            "exception_id": core.exception_identifier(
                batch.batch_id, item.plan_version, item.freeze_id, disc
            ),
            "batch_id": batch.batch_id,
            "plan_version": item.plan_version,
            "freeze_id": item.freeze_id,
            **disc,
        }
        for disc in discrepancies
    ]
    repo.insert_recon_exceptions(db, rows)
    return {"matched": matched, "discrepancies": len(discrepancies)}


def _refresh_counters(db: Session, batch: ReconBatch) -> None:
    db.flush()
    items = repo.list_recon_items(db, batch.batch_id)
    done = [i for i in items if i.status == "done"]
    batch.processed_items = len(done)
    batch.error_count = sum(1 for i in items if i.status == "error")
    batch.matched_count = sum(i.matched_count for i in done)
    batch.discrepancy_count = sum(i.discrepancy_count for i in done)
    db.flush()


def run_batch(db: Session, *, batch_id: str, actor: Actor) -> dict[str, Any]:
    """运行对账作业：断点续跑，已完成条目跳过，失败条目幂等重试。"""
    batch = _require_batch(db, batch_id)
    _require_scope(batch, actor)
    _require_not_signed(batch)

    recovered = repo.reset_stale_processing_items(db, batch_id)
    batch.status = "running"
    repo.add_recon_audit(
        db, batch_id=batch_id, action="run_started", actor_id=actor.actor_id
    )
    if recovered:
        repo.add_recon_audit(
            db,
            batch_id=batch_id,
            action="items_recovered",
            actor_id=actor.actor_id,
            detail=f"recovered={recovered}",
        )
    db.commit()

    for item in repo.runnable_recon_items(db, batch_id):
        item.status = "processing"
        item.attempt += 1
        item.error = None
        db.commit()  # 检查点：条目进入处理中
        try:
            result = _process_item(db, batch, item)
        except Exception as exc:  # 单条目失败不影响其他条目
            db.rollback()
            item.status = "error"
            item.error = f"{type(exc).__name__}: {exc}"[:500]
            repo.add_recon_audit(
                db,
                batch_id=batch_id,
                action="item_failed",
                actor_id=actor.actor_id,
                detail=f"{item.plan_version}/{item.freeze_id}: {exc}"[:500],
            )
            _refresh_counters(db, batch)
            db.commit()  # 检查点：失败已落库，可续跑重试
            continue
        except BaseException:
            db.rollback()
            raise
        item.status = "done"
        item.matched_count = result["matched"]
        item.discrepancy_count = result["discrepancies"]
        repo.add_recon_audit(
            db,
            batch_id=batch_id,
            action="item_completed",
            actor_id=actor.actor_id,
            detail=(
                f"{item.plan_version}/{item.freeze_id} "
                f"matched={result['matched']} discrepancies={result['discrepancies']}"
            ),
        )
        _refresh_counters(db, batch)
        db.commit()  # 检查点：条目完成

    _refresh_counters(db, batch)
    if batch.error_count == 0:
        batch.status = "completed"
    elif batch.processed_items == 0:
        batch.status = "failed"
    else:
        batch.status = "partial"
    repo.add_recon_audit(
        db,
        batch_id=batch_id,
        action="run_finished",
        actor_id=actor.actor_id,
        detail=(
            f"status={batch.status} "
            f"processed={batch.processed_items}/{batch.total_items} "
            f"discrepancies={batch.discrepancy_count}"
        ),
    )
    db.commit()
    return _batch_detail(db, batch)


def list_exceptions(
    db: Session, *, batch_id: str, actor: Actor, status: str | None = None
) -> list[dict[str, Any]]:
    batch = _require_batch(db, batch_id)
    _require_scope(batch, actor)
    return [
        _exception_detail(e)
        for e in repo.list_recon_exceptions(db, batch_id, status=status)
    ]


def claim_exception(
    db: Session, *, batch_id: str, exception_id: str, actor: Actor
) -> dict[str, Any]:
    """认领异常项：条件更新保证并发认领只有一人成功。"""
    batch = _require_batch(db, batch_id)
    _require_scope(batch, actor)
    _require_not_signed(batch)
    stmt = (
        update(ReconException)
        .where(ReconException.batch_id == batch_id)
        .where(ReconException.exception_id == exception_id)
        .where(ReconException.status == "open")
        .values(
            status="claimed",
            assignee=actor.actor_id,
            claimed_at=_utcnow(),
            version=ReconException.version + 1,
        )
    )
    result = db.execute(stmt)
    if result.rowcount != 1:
        db.rollback()
        exc = repo.get_recon_exception(
            db, batch_id=batch_id, exception_id=exception_id
        )
        if exc is None:
            raise ReconNotFoundError(
                f"exception '{exception_id}' does not exist"
            )
        raise ReconConflictError(f"异常项当前状态为 '{exc.status}'，无法认领")
    repo.add_recon_audit(
        db,
        batch_id=batch_id,
        action="exception_claimed",
        actor_id=actor.actor_id,
        detail=exception_id,
    )
    db.commit()
    exc = repo.get_recon_exception(db, batch_id=batch_id, exception_id=exception_id)
    assert exc is not None
    return _exception_detail(exc)


def review_exception(
    db: Session,
    *,
    batch_id: str,
    exception_id: str,
    actor: Actor,
    verdict: str,
    note: str = "",
) -> dict[str, Any]:
    """复核异常项：需 reviewer 角色且不能复核本人认领的项；并发复核只有一人成功。

    verdict=confirmed 进入 reviewed 终态；verdict=rejected 驳回回 open 重新认领。
    """
    if actor.role != "reviewer":
        raise ReconPermissionError("复核需要 reviewer 角色")
    batch = _require_batch(db, batch_id)
    _require_scope(batch, actor)
    _require_not_signed(batch)
    exc = repo.get_recon_exception(db, batch_id=batch_id, exception_id=exception_id)
    if exc is None:
        raise ReconNotFoundError(f"exception '{exception_id}' does not exist")
    if exc.status == "claimed" and exc.assignee == actor.actor_id:
        raise ReconPermissionError("认领人不能复核本人认领的异常项")
    now = _utcnow()
    if verdict == "confirmed":
        values: dict[str, Any] = {
            "status": "reviewed",
            "reviewed_by": actor.actor_id,
            "reviewed_at": now,
            "review_verdict": "confirmed",
            "review_note": note.strip(),
        }
    else:
        values = {
            "status": "open",
            "assignee": None,
            "claimed_at": None,
            "reviewed_by": actor.actor_id,
            "reviewed_at": now,
            "review_verdict": "rejected",
            "review_note": note.strip(),
        }
    values["version"] = ReconException.version + 1
    stmt = (
        update(ReconException)
        .where(ReconException.batch_id == batch_id)
        .where(ReconException.exception_id == exception_id)
        .where(ReconException.status == "claimed")
        .values(**values)
    )
    result = db.execute(stmt)
    if result.rowcount != 1:
        db.rollback()
        current = repo.get_recon_exception(
            db, batch_id=batch_id, exception_id=exception_id
        )
        assert current is not None
        raise ReconConflictError(f"异常项当前状态为 '{current.status}'，无法复核")
    repo.add_recon_audit(
        db,
        batch_id=batch_id,
        action="exception_reviewed",
        actor_id=actor.actor_id,
        detail=f"{exception_id} verdict={verdict}",
    )
    db.commit()
    exc = repo.get_recon_exception(db, batch_id=batch_id, exception_id=exception_id)
    assert exc is not None
    return _exception_detail(exc)


def sign_batch(db: Session, *, batch_id: str, actor: Actor) -> dict[str, Any]:
    """签署批次结果：需全部异常项复核完成；签署后结果保持不变。"""
    if actor.role != "reviewer":
        raise ReconPermissionError("签署需要 reviewer 角色")
    batch = _require_batch(db, batch_id)
    _require_scope(batch, actor)
    if batch.status == "signed":
        raise ReconConflictError("批次已签署")
    if batch.status not in SIGNABLE_STATUSES:
        raise ReconConflictError("批次尚未完成对账，无法签署")
    counts = repo.count_recon_exceptions(db, batch_id)
    unresolved = counts["open"] + counts["claimed"]
    if unresolved:
        raise ReconConflictError(f"仍有 {unresolved} 条异常项未复核")
    batch.status = "signed"
    batch.signed_by = actor.actor_id
    batch.signed_at = _utcnow()
    repo.add_recon_audit(
        db, batch_id=batch_id, action="batch_signed", actor_id=actor.actor_id
    )
    db.commit()
    return _batch_detail(db, batch)


def export_batch(db: Session, *, batch_id: str, actor: Actor) -> dict[str, Any]:
    """导出对账清单：内容完全由已落库状态派生，结果确定性且带内容指纹。"""
    batch = _require_batch(db, batch_id)
    _require_scope(batch, actor)
    if batch.status in ("created", "running"):
        raise ReconConflictError("批次尚未完成对账，无法导出")
    items = repo.list_recon_items(db, batch_id)
    exceptions = repo.list_recon_exceptions(db, batch_id)
    audits = repo.list_recon_audits(db, batch_id)
    counts = repo.count_recon_exceptions(db, batch_id)
    exception_dicts = [_exception_detail(e) for e in exceptions]
    manifest: dict[str, Any] = {
        "batch_id": batch.batch_id,
        "title": batch.title,
        "dept": batch.dept,
        "status": batch.status,
        "created_by": batch.created_by,
        "created_at": _iso(batch.created_at),
        "signed_by": batch.signed_by,
        "signed_at": _iso(batch.signed_at),
        "external_fingerprint": batch.external_fingerprint,
        "external_entries": batch.external_entries,
        "supersedes_batch_id": batch.supersedes_batch_id,
        "items": [_item_detail(i) for i in items],
        "summary": {
            "total_items": batch.total_items,
            "processed_items": batch.processed_items,
            "matched_count": batch.matched_count,
            "discrepancy_count": batch.discrepancy_count,
            "error_count": batch.error_count,
            "open_exceptions": counts["open"],
            "claimed_exceptions": counts["claimed"],
            "reviewed_exceptions": counts["reviewed"],
        },
        "exceptions": exception_dicts,
        "grouped": core.group_discrepancies(exception_dicts),
        "audit": [
            {
                "seq": a.seq,
                "action": a.action,
                "actor_id": a.actor_id,
                "detail": a.detail,
                "occurred_at": _iso(a.created_at),
            }
            for a in audits
        ],
    }
    manifest["manifest_fingerprint"] = core.canonical_fingerprint(manifest)
    return manifest
