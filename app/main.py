"""服务端业务模块。"""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from .recon_service import (
    ReconConflictError,
    ReconNotFoundError,
    ReconPermissionError,
)
from .routers import router

app = FastAPI(
    title="Practice Hours Guard",
    version="0.1.0",
    description=(
        "Event-sourced practice-hours compliance service. Check-ins, mentor "
        "confirmations and leave corrections are append-only; compliance is "
        "derived by replay and can be frozen into an immutable snapshot."
    ),
)

app.include_router(router)


@app.exception_handler(ReconNotFoundError)
def recon_not_found_handler(request: Request, exc: ReconNotFoundError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND, content={"detail": str(exc)}
    )


@app.exception_handler(ReconConflictError)
def recon_conflict_handler(request: Request, exc: ReconConflictError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT, content={"detail": str(exc)}
    )


@app.exception_handler(ReconPermissionError)
def recon_permission_handler(
    request: Request, exc: ReconPermissionError
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_403_FORBIDDEN, content={"detail": str(exc)}
    )


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}
