"""批量对账的编排服务：创建、运行、认领、复核、签署与导出。

运行按批次项逐个处理，每个分片提交一次断点；差异记录使用确定性主键，
因此崩溃恢复与幂等重试不会产生重复数据。已签署批次拒绝一切变更。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import recon, recon_repository as repo, repository as core_repository
from .recon import ReconDataError
from .services import FreezeNotFoundError, PlanNotFoundError

DEFAULT_CHUNK_SIZE = 200

# 允许启动（或恢复）运行的批次状态
RUNNABLE_STATES = ("created", "running", "failed", "completed_with_failures")
TERMINAL_EXCEPTION_STATES = ("resolved", "dismissed")


class BatchNotFoundError(Exception):
    pass


class BatchConflictError(Exception):
    """批次状态或内容指纹不允许当前操作。"""


class ExceptionNotFoundError(Exception):
    pass


class ExceptionStateError(Exception):
    """差异记录状态或版本不允许当前操作。"""


class ReviewForbiddenError(Exception):
    """违反复核分离约束（认领人与复核人须不同）。"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- 序列化


def _item_out(item) -> dict[str, Any]:
    return {
        "item_id": item.item_id,
        "batch_id": item.batch_id,
        "seq": item.seq,
        "plan_version": item.plan_version,
        "freeze_id": item.freeze_id,
        "snapshot_fingerprint": item.snapshot_fingerprint,
        "external_fingerprint": item.external_fingerprint,
        "status": item.status,
        "attempts": item.attempts,
        "cursor": item.cursor,
        "matched_count": item.matched_count,
        "exception_count": item.exception_count,
        "error": item.error,
    }


def _exception_out(exc) -> dict[str, Any]:
    return {
        "exception_id": exc.exception_id,
        "batch_id": exc.batch_id,
        "item_id": exc.item_id,
        "student_id": exc.student_id,
        "activity_type": exc.activity_type,
        "academic_day": exc.academic_day,
        "category": exc.category,
        "snapshot_seconds": exc.snapshot_seconds,
        "external_seconds": exc.external_seconds,
        "delta_seconds": exc.delta_seconds,
        "status": exc.status,
        "assignee": exc.assignee,
        "claimed_at": exc.claimed_at,
        "reviewer": exc.reviewer,
        "review_note": exc.review_note,
        "reviewed_at": exc.reviewed_at,
        "version": exc.version,
    }


def _batch_out(db: Session, batch, *, with_items: bool) -> dict[str, Any]:
    items = repo.list_items(db, batch.batch_id)
    exc_counts = repo.count_exceptions_by_status(db, batch.batch_id)
    out: dict[str, Any] = {
        "batch_id": batch.batch_id,
        "created_by": batch.created_by,
        "status": batch.status,
        "version": batch.version,
        "signed_by": batch.signed_by,
        "signed_at": batch.signed_at,
        "created_at": batch.created_at,
        "item_count": len(items),
        "done_count": sum(1 for i in items if i.status == "done"),
        "failed_count": sum(1 for i in items if i.status == "failed"),
        "exception_count": sum(exc_counts.values()),
        "open_exception_count": exc_counts.get("open", 0)
        + exc_counts.get("claimed", 0),
    }
    if with_items:
        out["items"] = [_item_out(i) for i in items]
    return out


def _require_batch(db: Session, batch_id: str):
    batch = repo.get_batch(db, batch_id)
    if batch is None:
        raise BatchNotFoundError(f"recon batch '{batch_id}' does not exist")
    return batch


# ---------------------------------------------------------------- 创建


def _prepare_items(
    db: Session, batch_id: str, items: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """校验每个批次项的输入并固定其内容指纹。"""
    prepared: list[dict[str, Any]] = []
    for seq, item_in in enumerate(items, start=1):
        plan_version = item_in["plan_version"]
        freeze_id = item_in["freeze_id"]
        if core_repository.get_plan(db, plan_version) is None:
            raise PlanNotFoundError(
                f"plan version '{plan_version}' is not registered"
            )
        freeze = core_repository.get_freeze(db, plan_version, freeze_id)
        if freeze is None:
            raise FreezeNotFoundError(
                f"freeze '{freeze_id}' for plan '{plan_version}' does not exist"
            )
        rows = recon.normalize_external_rows(item_in["external_rows"])
        prepared.append(
            {
                "item_id": f"{batch_id}-{seq:04d}",
                "batch_id": batch_id,
                "seq": seq,
                "plan_version": plan_version,
                "freeze_id": freeze_id,
                "snapshot_fingerprint": recon.content_fingerprint(freeze.snapshot),
                "external_fingerprint": recon.content_fingerprint(rows),
                "external_rows": rows,
                "status": "pending",
            }
        )
    return prepared


def _item_signature(item: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        item["plan_version"],
        item["freeze_id"],
        item["snapshot_fingerprint"],
        item["external_fingerprint"],
    )


def _matches_existing(
    db: Session, batch_id: str, prepared: list[dict[str, Any]]
) -> bool | None:
    """已有批次与本次请求的内容指纹是否一致；批次不存在返回 None。"""
    existing = repo.get_batch(db, batch_id)
    if existing is None:
        return None
    stored = repo.list_items(db, batch_id)
    stored_sig = [
        (
            i.plan_version,
            i.freeze_id,
            i.snapshot_fingerprint,
            i.external_fingerprint,
        )
        for i in stored
    ]
    return stored_sig == [_item_signature(p) for p in prepared]


def create_batch(
    db: Session, *, batch_id: str, items: list[dict[str, Any]], actor_id: str
) -> tuple[dict[str, Any], bool]:
    """创建对账批次；相同批次号且内容指纹一致时幂等返回已有批次。"""
    batch_id = batch_id.strip()
    if not batch_id:
        raise BatchConflictError("批次号不能为空")
    if not items:
        raise BatchConflictError("批次至少包含一个对账项")
    prepared = _prepare_items(db, batch_id, items)

    conflict = BatchConflictError(
        f"批次 '{batch_id}' 已存在且内容指纹不一致；外部文件更正后请使用新的批次号"
    )
    match = _matches_existing(db, batch_id, prepared)
    if match is True:
        return _batch_out(db, repo.get_batch(db, batch_id), with_items=True), False
    if match is False:
        raise conflict

    try:
        batch = repo.insert_batch(
            db, batch_id=batch_id, created_by=actor_id, items=prepared
        )
    except IntegrityError:
        # 并发创建同一批次号：按幂等规则重新判定
        db.rollback()
        if _matches_existing(db, batch_id, prepared) is True:
            return _batch_out(db, repo.get_batch(db, batch_id), with_items=True), False
        raise conflict from None
    return _batch_out(db, batch, with_items=True), True


# ---------------------------------------------------------------- 运行


def _reconcile_item(
    db: Session, *, batch_id: str, item, chunk_size: int
) -> None:
    """对单个批次项执行比对，按分片提交断点。

    键集合与差异完全由固定的输入指纹决定，因此重跑结果不变。
    """
    freeze = core_repository.get_freeze(db, item.plan_version, item.freeze_id)
    if freeze is None:  # 创建时已校验，此处为防御性检查
        raise ReconDataError(
            f"freeze '{item.freeze_id}' for plan '{item.plan_version}' does not exist"
        )
    snapshot_map = recon.snapshot_row_map(freeze.snapshot)
    external_map = recon.external_row_map(item.external_rows)
    diffs = recon.diff_row_maps(snapshot_map, external_map)
    diff_by_key = {
        (d["student_id"], d["activity_type"], d["academic_day"]): d for d in diffs
    }

    all_keys = sorted(set(snapshot_map) | set(external_map))
    matched = item.matched_count
    exceptions = item.exception_count
    for pos in range(item.cursor, len(all_keys)):
        diff = diff_by_key.get(all_keys[pos])
        if diff is None:
            matched += 1
        else:
            exceptions += 1
            repo.insert_exceptions_ignore_duplicates(
                db,
                [
                    {
                        "exception_id": recon.exception_id_for(
                            batch_id,
                            item.item_id,
                            diff["student_id"],
                            diff["activity_type"],
                            diff["academic_day"],
                            diff["category"],
                        ),
                        "batch_id": batch_id,
                        "item_id": item.item_id,
                        **diff,
                    }
                ],
            )
        if (pos + 1) % chunk_size == 0 or pos + 1 == len(all_keys):
            # 断点与差异插入在同一事务提交，崩溃后从断点继续
            repo.save_item_progress(
                db,
                item.item_id,
                cursor=pos + 1,
                matched_count=matched,
                exception_count=exceptions,
            )
    repo.finish_item(
        db,
        item.item_id,
        status="done",
        cursor=len(all_keys),
        matched_count=matched,
        exception_count=exceptions,
        error=None,
    )


def _finalize_batch(db: Session, batch_id: str) -> None:
    items = repo.list_items(db, batch_id)
    if all(i.status == "done" for i in items):
        status = "completed"
    elif any(i.status == "done" for i in items):
        status = "completed_with_failures"
    else:
        status = "failed"
    repo.update_batch_status(db, batch_id, status=status)


def run_batch(
    db: Session, *, batch_id: str, actor_id: str, chunk_size: int = DEFAULT_CHUNK_SIZE
) -> dict[str, Any]:
    """运行（或恢复）对账作业。

    - 已 done 的批次项直接跳过（断点续跑）；
    - 单个批次项失败不阻塞其他项（部分失败）；
    - 已 completed 的批次重复运行是幂等空操作；
    - 已 signed 的批次拒绝运行。
    """
    batch = _require_batch(db, batch_id)
    if batch.status == "signed":
        raise BatchConflictError("批次已签署，结果不可变更")
    if batch.status == "completed":
        return _batch_out(db, batch, with_items=True)
    if batch.status not in RUNNABLE_STATES:
        raise BatchConflictError(f"批次状态 {batch.status} 不允许运行")

    repo.update_batch_status(db, batch_id, status="running")
    for item in repo.list_items(db, batch_id):
        claimed = repo.claim_item_for_run(db, item.item_id)
        if claimed is None:
            continue  # 已完成，断点续跑跳过
        try:
            _reconcile_item(db, batch_id=batch_id, item=claimed, chunk_size=chunk_size)
        except Exception as exc:  # 单项失败不阻塞其余项，可稍后重试
            current = repo.get_item(db, item.item_id)
            repo.finish_item(
                db,
                item.item_id,
                status="failed",
                cursor=current.cursor if current else claimed.cursor,
                matched_count=current.matched_count if current else 0,
                exception_count=current.exception_count if current else 0,
                error=str(exc)[:512],
            )
    _finalize_batch(db, batch_id)
    return _batch_out(db, repo.get_batch(db, batch_id), with_items=True)


# ---------------------------------------------------------------- 认领与复核


def _require_exception(db: Session, exception_id: str):
    exc = repo.get_exception(db, exception_id)
    if exc is None:
        raise ExceptionNotFoundError(f"recon exception '{exception_id}' does not exist")
    return exc


def _ensure_batch_mutable(db: Session, batch_id: str) -> None:
    batch = _require_batch(db, batch_id)
    if batch.status == "signed":
        raise BatchConflictError("批次已签署，差异处理结果保持不变")


def claim_exception(
    db: Session, *, exception_id: str, actor_id: str
) -> dict[str, Any]:
    exc = _require_exception(db, exception_id)
    _ensure_batch_mutable(db, exc.batch_id)
    ok = repo.claim_exception(
        db, exception_id, assignee=actor_id, claimed_at=_utcnow()
    )
    if not ok:
        current = repo.get_exception(db, exception_id)
        raise ExceptionStateError(
            f"差异当前状态为 {current.status}"
            + (f"（认领人 {current.assignee}）" if current.assignee else "")
            + "，不可重复认领"
        )
    return _exception_out(repo.get_exception(db, exception_id))


def review_exception(
    db: Session,
    *,
    exception_id: str,
    actor_id: str,
    decision: str,
    note: str,
    expected_version: int,
) -> dict[str, Any]:
    exc = _require_exception(db, exception_id)
    _ensure_batch_mutable(db, exc.batch_id)
    if exc.status != "claimed":
        raise ExceptionStateError(f"差异当前状态为 {exc.status}，须先认领再复核")
    if exc.assignee == actor_id:
        raise ReviewForbiddenError("认领人与复核人必须为不同操作者")
    if exc.version != expected_version:
        raise ExceptionStateError(
            f"版本冲突：期望 {expected_version}，当前 {exc.version}"
        )
    ok = repo.review_exception(
        db,
        exception_id,
        expected_version=expected_version,
        decision=decision,
        reviewer=actor_id,
        note=note,
        reviewed_at=_utcnow(),
    )
    if not ok:
        raise ExceptionStateError("并发复核冲突，请刷新后重试")
    return _exception_out(repo.get_exception(db, exception_id))


# ---------------------------------------------------------------- 签署与导出


def sign_batch(db: Session, *, batch_id: str, actor_id: str) -> dict[str, Any]:
    batch = _require_batch(db, batch_id)
    if batch.status == "signed":
        return _batch_out(db, batch, with_items=True)  # 幂等
    if batch.status not in ("completed", "completed_with_failures"):
        raise BatchConflictError(f"批次状态 {batch.status} 未完成，不能签署")
    counts = repo.count_exceptions_by_status(db, batch_id)
    pending = sum(v for k, v in counts.items() if k not in TERMINAL_EXCEPTION_STATES)
    if pending:
        raise BatchConflictError(f"仍有 {pending} 条差异未复核终结，不能签署")
    repo.update_batch_status(
        db, batch_id, status="signed", signed_by=actor_id, signed_at=_utcnow()
    )
    return _batch_out(db, repo.get_batch(db, batch_id), with_items=True)


def _breakdown(exceptions: list[dict[str, Any]], dim: str) -> list[dict[str, Any]]:
    """按某一解释维度（学生/活动类型/日期）汇总差异。"""
    agg: dict[str, dict[str, int]] = {}
    for exc in exceptions:
        bucket = agg.setdefault(
            exc[dim],
            {
                "count": 0,
                "snapshot_seconds": 0,
                "external_seconds": 0,
                "delta_seconds": 0,
            },
        )
        bucket["count"] += 1
        bucket["snapshot_seconds"] += exc["snapshot_seconds"]
        bucket["external_seconds"] += exc["external_seconds"]
        bucket["delta_seconds"] += exc["delta_seconds"]
    return [{dim: key, **values} for key, values in sorted(agg.items())]


def export_batch(db: Session, *, batch_id: str, actor_id: str) -> dict[str, Any]:
    """导出对账清单；内容指纹覆盖全部稳定字段，可验证导出未被篡改。"""
    batch = _require_batch(db, batch_id)
    items = repo.list_items(db, batch_id)
    exceptions = [
        _exception_out(e) for e in repo.list_exceptions(db, batch_id)
    ]
    for exc in exceptions:  # 时间字段不参与指纹，导出内容保持确定性
        for key in ("claimed_at", "reviewed_at"):
            exc[key] = exc[key].isoformat() if exc[key] else None
    item_dicts = [_item_out(i) for i in items]
    payload: dict[str, Any] = {
        "batch_id": batch.batch_id,
        "status": batch.status,
        "created_by": batch.created_by,
        "signed_by": batch.signed_by,
        "items": item_dicts,
        "exceptions": exceptions,
        "breakdowns": {
            "by_student": _breakdown(exceptions, "student_id"),
            "by_activity_type": _breakdown(exceptions, "activity_type"),
            "by_academic_day": _breakdown(exceptions, "academic_day"),
        },
        "totals": {
            "item_count": len(item_dicts),
            "done_count": sum(1 for i in items if i.status == "done"),
            "failed_count": sum(1 for i in items if i.status == "failed"),
            "matched_count": sum(i.matched_count for i in items),
            "exception_count": len(exceptions),
            "open_exception_count": sum(
                1 for e in exceptions if e["status"] not in TERMINAL_EXCEPTION_STATES
            ),
        },
        "exported_by": actor_id,
    }
    payload["export_fingerprint"] = recon.content_fingerprint(payload)
    return payload


# ---------------------------------------------------------------- 查询


def get_batch_detail(db: Session, batch_id: str) -> dict[str, Any]:
    return _batch_out(db, _require_batch(db, batch_id), with_items=True)


def list_batches(db: Session) -> list[dict[str, Any]]:
    return [_batch_out(db, b, with_items=False) for b in repo.list_batches(db)]


def list_exceptions(
    db: Session,
    batch_id: str,
    *,
    status: str | None = None,
    assignee: str | None = None,
) -> list[dict[str, Any]]:
    _require_batch(db, batch_id)
    return [
        _exception_out(e)
        for e in repo.list_exceptions(
            db, batch_id, status=status, assignee=assignee
        )
    ]
