"""对账核心纯逻辑：内容指纹、内部事实提取、差异解释与分组汇总。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Iterable, Mapping, Sequence

ADJUSTMENT_ACTIVITY_TYPE = "adjustment"

CATEGORY_MISSING_IN_EXTERNAL = "missing_in_external"
CATEGORY_MISSING_IN_SNAPSHOT = "missing_in_snapshot"
CATEGORY_AMOUNT_MISMATCH = "amount_mismatch"


def canonical_fingerprint(payload: Any) -> str:
    """对任意 JSON 可序列化内容计算确定性的 SHA-256 内容指纹。"""
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256(raw.encode("utf-8")).hexdigest()


def normalize_external_entries(
    entries: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """规范化院校报送汇总行，排序后用于内容指纹与逐项比对。"""
    normalized: list[dict[str, Any]] = []
    for entry in entries:
        normalized.append(
            {
                "plan_version": str(entry["plan_version"]),
                "student_id": str(entry["student_id"]),
                "activity_type": str(entry.get("activity_type") or "regular"),
                "academic_day": str(entry.get("academic_day") or ""),
                "seconds": int(entry["seconds"]),
            }
        )
    normalized.sort(
        key=lambda e: (
            e["plan_version"],
            e["student_id"],
            e["activity_type"],
            e["academic_day"],
        )
    )
    return normalized


@dataclass(frozen=True)
class FactKey:
    """对账比对的最小粒度：学生 × 活动类型 × 教学日。"""

    student_id: str
    activity_type: str
    academic_day: str


def extract_internal_facts(snapshot: Mapping[str, Any]) -> dict[FactKey, int]:
    """从冻结快照提取 (学生, 活动类型, 日期) → 秒数 的内部事实。

    只统计已确认（counts=True）的签到；请假修正记为 adjustment 类型、
    日期为空串的独立事实行。
    """
    facts: dict[FactKey, int] = {}
    for student in snapshot.get("students", []):
        student_id = student["student_id"]
        for checkin in student.get("checkins", []):
            if not checkin.get("counts"):
                continue
            activity_type = checkin.get("activity_type") or "regular"
            for segment in checkin.get("academic_days", []):
                key = FactKey(student_id, activity_type, segment["day"])
                facts[key] = facts.get(key, 0) + int(segment["seconds"])
        for adjustment in student.get("adjustments", []):
            seconds = int(adjustment["seconds"])
            if seconds == 0:
                continue
            key = FactKey(student_id, ADJUSTMENT_ACTIVITY_TYPE, "")
            facts[key] = facts.get(key, 0) + seconds
    return facts


def aggregate_external(
    entries: Sequence[Mapping[str, Any]], plan_version: str
) -> dict[FactKey, int]:
    """按培养方案聚合外部汇总行，键粒度与内部事实一致。"""
    facts: dict[FactKey, int] = {}
    for entry in entries:
        if entry["plan_version"] != plan_version:
            continue
        key = FactKey(
            str(entry["student_id"]),
            str(entry.get("activity_type") or "regular"),
            str(entry.get("academic_day") or ""),
        )
        facts[key] = facts.get(key, 0) + int(entry["seconds"])
    return facts


def reconcile_facts(
    internal: Mapping[FactKey, int], external: Mapping[FactKey, int]
) -> tuple[int, list[dict[str, Any]]]:
    """逐项比对，返回 (匹配条数, 差异列表)。

    每条差异都按学生、活动类型和日期解释：缺失方向（missing_in_external /
    missing_in_snapshot）或秒数不一致（amount_mismatch），delta 为内部减外部。
    """
    matched = 0
    discrepancies: list[dict[str, Any]] = []
    ordered = sorted(
        set(internal) | set(external),
        key=lambda k: (k.student_id, k.activity_type, k.academic_day),
    )
    for key in ordered:
        internal_seconds = internal.get(key, 0)
        external_seconds = external.get(key, 0)
        in_internal = key in internal
        in_external = key in external
        if in_internal and in_external:
            if internal_seconds == external_seconds:
                matched += 1
                continue
            category = CATEGORY_AMOUNT_MISMATCH
        elif in_internal:
            category = CATEGORY_MISSING_IN_EXTERNAL
        else:
            category = CATEGORY_MISSING_IN_SNAPSHOT
        discrepancies.append(
            {
                "student_id": key.student_id,
                "activity_type": key.activity_type,
                "academic_day": key.academic_day,
                "category": category,
                "internal_seconds": internal_seconds,
                "external_seconds": external_seconds,
                "delta_seconds": internal_seconds - external_seconds,
            }
        )
    return matched, discrepancies


def group_discrepancies(
    discrepancies: Iterable[Mapping[str, Any]],
) -> dict[str, dict[str, dict[str, int]]]:
    """按学生、活动类型和日期三个维度汇总差异，供导出解释。"""
    grouped: dict[str, dict[str, dict[str, int]]] = {
        "by_student": {},
        "by_activity_type": {},
        "by_academic_day": {},
    }
    dimensions = {
        "by_student": "student_id",
        "by_activity_type": "activity_type",
        "by_academic_day": "academic_day",
    }
    for disc in discrepancies:
        for group_name, field in dimensions.items():
            bucket = grouped[group_name].setdefault(
                str(disc[field]),
                {
                    "count": 0,
                    "internal_seconds": 0,
                    "external_seconds": 0,
                    "delta_seconds": 0,
                },
            )
            bucket["count"] += 1
            bucket["internal_seconds"] += int(disc["internal_seconds"])
            bucket["external_seconds"] += int(disc["external_seconds"])
            bucket["delta_seconds"] += int(disc["delta_seconds"])
    return {
        group_name: {key: buckets[key] for key in sorted(buckets)}
        for group_name, buckets in grouped.items()
    }


def exception_identifier(
    batch_id: str, plan_version: str, freeze_id: str, disc: Mapping[str, Any]
) -> str:
    """由差异内容派生确定性的异常项标识，保证续跑重试幂等。"""
    raw = "|".join(
        [
            batch_id,
            plan_version,
            freeze_id,
            str(disc["student_id"]),
            str(disc["activity_type"]),
            str(disc["academic_day"]),
            str(disc["category"]),
        ]
    )
    return "exc-" + sha256(raw.encode("utf-8")).hexdigest()[:24]
