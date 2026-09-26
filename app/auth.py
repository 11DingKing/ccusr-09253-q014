"""轻量操作者身份与权限范围（按部门隔离对账批次）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Header, HTTPException, status

ROLES = ("auditor", "reviewer")
DEFAULT_DEPT = "audit"


@dataclass(frozen=True)
class Actor:
    """请求操作者：标识、角色与所属部门（权限范围）。"""

    actor_id: str
    role: str
    dept: str


def get_actor(
    x_actor_id: Annotated[str | None, Header()] = None,
    x_actor_role: Annotated[str | None, Header()] = None,
    x_actor_dept: Annotated[str | None, Header()] = None,
) -> Actor:
    if x_actor_id is None or not x_actor_id.strip():
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "missing X-Actor-Id header"
        )
    role = (x_actor_role or "auditor").strip()
    if role not in ROLES:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, f"unknown actor role '{role}'"
        )
    dept = (x_actor_dept or DEFAULT_DEPT).strip() or DEFAULT_DEPT
    return Actor(actor_id=x_actor_id.strip(), role=role, dept=dept)
