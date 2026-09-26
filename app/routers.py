"""服务端业务模块。"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from . import recon_service, services
from .auth import Actor, get_actor
from .db import get_db
from .schemas import (
    DiffOut,
    EventBatchIn,
    FreezeIn,
    ImportResult,
    PlanIn,
    PlanOut,
    ReconBatchCreateIn,
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


# --- 对账批次 ---


@router.post("/recon-batches", status_code=status.HTTP_201_CREATED)
def create_recon_batch(
    body: ReconBatchCreateIn,
    response: Response,
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_actor),
) -> Any:
    """创建对账批次，固定快照与外部汇总的内容指纹；相同内容重复创建返回已有批次。"""
    detail, created = recon_service.create_batch(
        db,
        actor=actor,
        batch_id=body.batch_id,
        title=body.title,
        items=[i.model_dump() for i in body.items],
        external_entries=[e.model_dump() for e in body.external_entries],
        supersedes_batch_id=body.supersedes_batch_id,
    )
    if not created:
        response.status_code = status.HTTP_200_OK
    return detail


@router.get("/recon-batches")
def list_recon_batches(
    db: Session = Depends(get_db), actor: Actor = Depends(get_actor)
) -> Any:
    return recon_service.list_batches(db, actor=actor)


@router.get("/recon-batches/{batch_id}")
def get_recon_batch(
    batch_id: str,
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_actor),
) -> Any:
    return recon_service.get_batch_detail(db, batch_id=batch_id, actor=actor)


@router.post("/recon-batches/{batch_id}/run")
def run_recon_batch(
    batch_id: str,
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_actor),
) -> Any:
    """运行对账作业：断点续跑、失败条目幂等重试；已签署批次拒绝重跑。"""
    return recon_service.run_batch(db, batch_id=batch_id, actor=actor)


@router.get("/recon-batches/{batch_id}/exceptions")
def list_recon_exceptions(
    batch_id: str,
    exc_status: Annotated[str | None, Query(alias="status")] = None,
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_actor),
) -> Any:
    return recon_service.list_exceptions(
        db, batch_id=batch_id, actor=actor, status=exc_status
    )


@router.post("/recon-batches/{batch_id}/exceptions/{exception_id}/claim")
def claim_recon_exception(
    batch_id: str,
    exception_id: str,
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_actor),
) -> Any:
    return recon_service.claim_exception(
        db, batch_id=batch_id, exception_id=exception_id, actor=actor
    )


@router.post("/recon-batches/{batch_id}/exceptions/{exception_id}/review")
def review_recon_exception(
    batch_id: str,
    exception_id: str,
    body: ReconReviewIn,
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_actor),
) -> Any:
    return recon_service.review_exception(
        db,
        batch_id=batch_id,
        exception_id=exception_id,
        actor=actor,
        verdict=body.verdict,
        note=body.note,
    )


@router.post("/recon-batches/{batch_id}/sign")
def sign_recon_batch(
    batch_id: str,
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_actor),
) -> Any:
    return recon_service.sign_batch(db, batch_id=batch_id, actor=actor)


@router.get("/recon-batches/{batch_id}/export")
def export_recon_batch(
    batch_id: str,
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_actor),
) -> Any:
    return recon_service.export_batch(db, batch_id=batch_id, actor=actor)
