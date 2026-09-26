"""对账接口的操作者身份与权限范围校验。

通过请求头 `X-Actor-Id` 与 `X-Actor-Role` 识别操作者，
角色决定可访问的权限范围（scope）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from fastapi import Depends, Header, HTTPException, status

ROLE_SCOPES: dict[str, frozenset[str]] = {
    # 审计员：建批、运行、认领差异、导出
    "auditor": frozenset(
        {"recon:create", "recon:run", "recon:claim", "recon:export"}
    ),
    # 复核员：复核差异、导出
    "reviewer": frozenset({"recon:review", "recon:export"}),
    # 管理员：全部范围，含签署
    "admin": frozenset(
        {
            "recon:create",
            "recon:run",
            "recon:claim",
            "recon:review",
            "recon:export",
            "recon:sign",
        }
    ),
}


@dataclass(frozen=True)
class Actor:
    actor_id: str
    role: str
    scopes: frozenset[str]


def get_actor(
    x_actor_id: str | None = Header(default=None),
    x_actor_role: str | None = Header(default=None),
) -> Actor:
    if x_actor_id is None or not x_actor_id.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing X-Actor-Id header",
        )
    if x_actor_role is None or not x_actor_role.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing X-Actor-Role header",
        )
    role = x_actor_role.strip().lower()
    scopes = ROLE_SCOPES.get(role)
    if scopes is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"unknown actor role '{x_actor_role}'",
        )
    return Actor(actor_id=x_actor_id.strip(), role=role, scopes=scopes)


def require_scope(scope: str) -> Callable[[Actor], Actor]:
    """生成一个依赖项：操作者具备指定权限范围才放行，否则 403。"""

    def dependency(actor: Actor = Depends(get_actor)) -> Actor:
        if scope not in actor.scopes:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"role '{actor.role}' lacks scope '{scope}'",
            )
        return actor

    return dependency
