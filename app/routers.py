"""服务端业务模块。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from . import recon_services, services
from .auth import Actor, get_actor, require_scope
from .db import get_db
from .recon import ReconDataError
from .schemas import (
    DiffOut,
    EventBatchIn,
    FreezeIn,
    ImportResult,
    PlanIn,
    PlanOut,
    ReconBatchIn,
    ReconBatchOut,
    ReconExceptionOut,
    ReconExportOut,
    ReconReviewIn,
    SnapshotOut,
    StudentProgressOut,
)

router = APIRouter(prefix="/api")


@router.post("/plans", response_model=PlanOut, status_code=status.HTTP_201_CREATED)
def create_plan(body: PlanIn, db: Session = Depends(get_db)) -> Any:
    return services.ensure_plan(
        db,
        plan_version=body.plan_version,
        iana_timezone=body.iana_timezone,
        required_seconds=body.required_seconds,
    )


@router.get("/plans/{plan_version}", response_model=PlanOut)
def read_plan(plan_version: str, db: Session = Depends(get_db)) -> Any:
    plan = services.get_plan_plain(db, plan_version)
    if plan is None:
        raise HTTPException(status_code=404, detail="plan not found")
    return plan


@router.post(
    "/plans/{plan_version}/events",
    response_model=ImportResult,
    status_code=status.HTTP_201_CREATED,
)
def post_events(
    plan_version: str, body: EventBatchIn, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.import_events(
            db,
            plan_version=plan_version,
            events=[e.model_dump() for e in body.events],
        )
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/snapshot",
    response_model=SnapshotOut,
)
def get_snapshot(plan_version: str, db: Session = Depends(get_db)) -> Any:
    try:
        snap = services.current_snapshot(db, plan_version)
        return snap.to_dict()
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/students/{student_id}/progress",
    response_model=StudentProgressOut,
)
def get_progress(
    plan_version: str, student_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        result = services.student_progress(db, plan_version, student_id)
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="student not found")
    return result


@router.post(
    "/plans/{plan_version}/freezes/{freeze_id}",
    response_model=SnapshotOut,
    status_code=status.HTTP_201_CREATED,
)
def post_freeze(
    plan_version: str,
    freeze_id: str,
    body: FreezeIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        snap, _ = services.freeze_semester(
            db, plan_version=plan_version, freeze_id=freeze_id
        )
        return snap.to_dict()
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/freezes/{freeze_id}",
    response_model=SnapshotOut,
)
def get_freeze(
    plan_version: str, freeze_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        snap = services.get_frozen_snapshot(db, plan_version, freeze_id)
        return snap.to_dict()
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.FreezeNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/freezes/{freeze_id}/explain/{student_id}",
    response_model=StudentProgressOut,
)
def explain_freeze_student(
    plan_version: str,
    freeze_id: str,
    student_id: str,
    db: Session = Depends(get_db),
) -> Any:
    try:
        result = services.explain_frozen_student(
            db, plan_version, freeze_id, student_id
        )
    except (services.PlanNotFoundError, services.FreezeNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="student not found")
    return result


@router.get(
    "/plans/{plan_version}/freezes/{freeze_id}/diff/{other_freeze_id}",
    response_model=DiffOut,
)
def get_diff(
    plan_version: str,
    freeze_id: str,
    other_freeze_id: str,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return services.diff_freezes(
            db, plan_version, freeze_id, other_freeze_id
        )
    except (services.PlanNotFoundError, services.FreezeNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ---------------------------------------------------------------- 批量对账


def _map_recon_errors(exc: Exception) -> HTTPException:
    if isinstance(
        exc,
        (
            recon_services.BatchNotFoundError,
            recon_services.ExceptionNotFoundError,
            services.PlanNotFoundError,
            services.FreezeNotFoundError,
        ),
    ):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, recon_services.ReviewForbiddenError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, ReconDataError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(
        exc,
        (recon_services.BatchConflictError, recon_services.ExceptionStateError),
    ):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


@router.post(
    "/recon-batches/{batch_id}",
    response_model=ReconBatchOut,
    status_code=status.HTTP_201_CREATED,
    tags=["recon"],
)
def create_recon_batch(
    batch_id: str,
    body: ReconBatchIn,
    db: Session = Depends(get_db),
    actor: Actor = Depends(require_scope("recon:create")),
) -> Any:
    """创建对账批次，固定每个批次项的快照指纹与外部汇总指纹。

    相同批次号重复创建且内容指纹一致时幂等返回；外部文件更正后
    必须使用新的批次号创建新批次。
    """
    try:
        result, _ = recon_services.create_batch(
            db,
            batch_id=batch_id,
            items=[item.model_dump() for item in body.items],
            actor_id=actor.actor_id,
        )
        return result
    except Exception as exc:
        raise _map_recon_errors(exc) from exc


@router.get("/recon-batches", response_model=list[ReconBatchOut], tags=["recon"])
def list_recon_batches(
    db: Session = Depends(get_db), actor: Actor = Depends(get_actor)
) -> Any:
    return recon_services.list_batches(db)


@router.get(
    "/recon-batches/{batch_id}", response_model=ReconBatchOut, tags=["recon"]
)
def get_recon_batch(
    batch_id: str,
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_actor),
) -> Any:
    try:
        return recon_services.get_batch_detail(db, batch_id)
    except Exception as exc:
        raise _map_recon_errors(exc) from exc


@router.post(
    "/recon-batches/{batch_id}/run",
    response_model=ReconBatchOut,
    tags=["recon"],
)
def run_recon_batch(
    batch_id: str,
    db: Session = Depends(get_db),
    actor: Actor = Depends(require_scope("recon:run")),
) -> Any:
    """运行或恢复对账作业；已完成项自动跳过，失败项可幂等重试。"""
    try:
        return recon_services.run_batch(
            db, batch_id=batch_id, actor_id=actor.actor_id
        )
    except Exception as exc:
        raise _map_recon_errors(exc) from exc


@router.post(
    "/recon-batches/{batch_id}/sign",
    response_model=ReconBatchOut,
    tags=["recon"],
)
def sign_recon_batch(
    batch_id: str,
    db: Session = Depends(get_db),
    actor: Actor = Depends(require_scope("recon:sign")),
) -> Any:
    """签署批次：全部差异复核终结后生效，签署后结果保持不变。"""
    try:
        return recon_services.sign_batch(
            db, batch_id=batch_id, actor_id=actor.actor_id
        )
    except Exception as exc:
        raise _map_recon_errors(exc) from exc


@router.get(
    "/recon-batches/{batch_id}/exceptions",
    response_model=list[ReconExceptionOut],
    tags=["recon"],
)
def list_recon_exceptions(
    batch_id: str,
    status_filter: str | None = Query(default=None, alias="status"),
    assignee: str | None = Query(default=None),
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_actor),
) -> Any:
    try:
        return recon_services.list_exceptions(
            db, batch_id, status=status_filter, assignee=assignee
        )
    except Exception as exc:
        raise _map_recon_errors(exc) from exc


@router.get(
    "/recon-batches/{batch_id}/export",
    response_model=ReconExportOut,
    tags=["recon"],
)
def export_recon_batch(
    batch_id: str,
    db: Session = Depends(get_db),
    actor: Actor = Depends(require_scope("recon:export")),
) -> Any:
    """导出对账清单，含输入指纹、差异明细与按学生/活动类型/日期的汇总。"""
    try:
        return recon_services.export_batch(
            db, batch_id=batch_id, actor_id=actor.actor_id
        )
    except Exception as exc:
        raise _map_recon_errors(exc) from exc


@router.post(
    "/recon-exceptions/{exception_id}/claim",
    response_model=ReconExceptionOut,
    tags=["recon"],
)
def claim_recon_exception(
    exception_id: str,
    db: Session = Depends(get_db),
    actor: Actor = Depends(require_scope("recon:claim")),
) -> Any:
    """认领差异项；并发认领只有一个成功。"""
    try:
        return recon_services.claim_exception(
            db, exception_id=exception_id, actor_id=actor.actor_id
        )
    except Exception as exc:
        raise _map_recon_errors(exc) from exc


@router.post(
    "/recon-exceptions/{exception_id}/review",
    response_model=ReconExceptionOut,
    tags=["recon"],
)
def review_recon_exception(
    exception_id: str,
    body: ReconReviewIn,
    db: Session = Depends(get_db),
    actor: Actor = Depends(require_scope("recon:review")),
) -> Any:
    """复核差异项；要求先认领、复核人不同于认领人、版本一致。"""
    try:
        return recon_services.review_exception(
            db,
            exception_id=exception_id,
            actor_id=actor.actor_id,
            decision=body.decision,
            note=body.note,
            expected_version=body.expected_version,
        )
    except Exception as exc:
        raise _map_recon_errors(exc) from exc
