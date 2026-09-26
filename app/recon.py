"""批量对账的纯领域逻辑：内容指纹、快照行提取与差异分类。

本模块不依赖数据库，所有函数对相同输入返回相同结果，
保证批次重跑与断点续跑的幂等性。
"""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any, Iterable

# 差异类别
MISSING_IN_EXTERNAL = "missing_in_external"  # 快照有、外部汇总缺失
MISSING_IN_SNAPSHOT = "missing_in_snapshot"  # 外部汇总多报、快照不存在
SECONDS_MISMATCH = "seconds_mismatch"  # 双方都有但秒数不一致

CATEGORIES = (MISSING_IN_EXTERNAL, MISSING_IN_SNAPSHOT, SECONDS_MISMATCH)

# 对账键：(学生, 活动类型, 教学日)
RowKey = tuple[str, str, str]


class ReconDataError(ValueError):
    """外部汇总内容存在确定性数据问题，该批次项无法完成。"""


def content_fingerprint(payload: Any) -> str:
    """对任意 JSON 可序列化内容计算稳定的内容指纹。"""
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def exception_id_for(
    batch_id: str,
    item_id: str,
    student_id: str,
    activity_type: str,
    academic_day: str,
    category: str,
) -> str:
    """差异记录使用确定性主键，重试插入时天然幂等。"""
    raw = "|".join(
        [batch_id, item_id, student_id, activity_type, academic_day, category]
    )
    return sha256(raw.encode("utf-8")).hexdigest()


def normalize_external_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """把外部汇总行规范化为可存储、可指纹计算的稳定结构。"""
    normalized: list[dict[str, Any]] = []
    for row in rows:
        student_id = str(row.get("student_id", "")).strip()
        activity_type = str(row.get("activity_type", "")).strip()
        academic_day = str(row.get("academic_day", "")).strip()
        seconds = row.get("seconds")
        if not student_id or not activity_type or not academic_day:
            raise ReconDataError("外部汇总行缺少学生、活动类型或日期")
        if not isinstance(seconds, int) or isinstance(seconds, bool) or seconds < 0:
            raise ReconDataError(f"外部汇总行秒数非法: {seconds!r}")
        normalized.append(
            {
                "student_id": student_id,
                "activity_type": activity_type,
                "academic_day": academic_day,
                "seconds": seconds,
            }
        )
    return normalized


def external_row_map(rows: Iterable[dict[str, Any]]) -> dict[RowKey, int]:
    """外部汇总行按键聚合；完全重复的行去重，键冲突则视为数据错误。"""
    result: dict[RowKey, int] = {}
    for row in rows:
        key: RowKey = (row["student_id"], row["activity_type"], row["academic_day"])
        seconds = int(row["seconds"])
        if key in result:
            if result[key] == seconds:
                continue  # 完全重复的行，幂等去重
            raise ReconDataError(
                "外部汇总存在冲突行: "
                f"学生 {key[0]} 活动 {key[1]} 日期 {key[2]} "
                f"同时报送 {result[key]} 与 {seconds} 秒"
            )
        result[key] = seconds
    return result


def snapshot_row_map(snapshot: dict[str, Any]) -> dict[RowKey, int]:
    """从冻结快照提取 (学生, 活动类型, 教学日) -> 已确认秒数。

    只统计计入合规结果的签到段（counts=True），与快照的 total_seconds 口径一致。
    """
    result: dict[RowKey, int] = {}
    for student in snapshot.get("students", []):
        student_id = student["student_id"]
        for checkin in student.get("checkins", []):
            if not checkin.get("counts", False):
                continue
            activity_type = checkin.get("activity_type", "regular")
            for segment in checkin.get("academic_days", []):
                key: RowKey = (student_id, activity_type, segment["day"])
                result[key] = result.get(key, 0) + int(segment["seconds"])
    return result


def diff_row_maps(
    snapshot_map: dict[RowKey, int], external_map: dict[RowKey, int]
) -> list[dict[str, Any]]:
    """比较两侧汇总，返回按 (学生, 活动类型, 日期) 排序的差异列表。"""
    diffs: list[dict[str, Any]] = []
    for key in sorted(set(snapshot_map) | set(external_map)):
        snap_seconds = snapshot_map.get(key)
        ext_seconds = external_map.get(key)
        if snap_seconds is not None and ext_seconds is None:
            category = MISSING_IN_EXTERNAL
        elif snap_seconds is None and ext_seconds is not None:
            category = MISSING_IN_SNAPSHOT
        elif snap_seconds != ext_seconds:
            category = SECONDS_MISMATCH
        else:
            continue
        snap_value = snap_seconds if snap_seconds is not None else 0
        ext_value = ext_seconds if ext_seconds is not None else 0
        diffs.append(
            {
                "student_id": key[0],
                "activity_type": key[1],
                "academic_day": key[2],
                "category": category,
                "snapshot_seconds": snap_value,
                "external_seconds": ext_value,
                "delta_seconds": snap_value - ext_value,
            }
        )
    return diffs
