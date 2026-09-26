"""对账纯领域逻辑的单元测试。"""

from __future__ import annotations

import pytest

from app import recon
from app.recon import ReconDataError


def _snapshot() -> dict:
    return {
        "plan_version": "P1",
        "freeze_id": "F-01",
        "students": [
            {
                "student_id": "S1",
                "checkins": [
                    {
                        "event_id": "E-01",
                        "activity_type": "regular",
                        "counts": True,
                        "academic_days": [
                            {"day": "2024-03-15", "seconds": 3600},
                            {"day": "2024-03-16", "seconds": 1800},
                        ],
                    },
                    {
                        "event_id": "E-02",
                        "activity_type": "internship",
                        "counts": False,  # 未确认，不计入
                        "academic_days": [{"day": "2024-03-15", "seconds": 900}],
                    },
                ],
            },
            {
                "student_id": "S2",
                "checkins": [
                    {
                        "event_id": "E-03",
                        "activity_type": "regular",
                        "counts": True,
                        "academic_days": [{"day": "2024-03-15", "seconds": 7200}],
                    }
                ],
            },
        ],
    }


def test_snapshot_row_map_only_counts_confirmed_segments():
    rows = recon.snapshot_row_map(_snapshot())
    assert rows == {
        ("S1", "regular", "2024-03-15"): 3600,
        ("S1", "regular", "2024-03-16"): 1800,
        ("S2", "regular", "2024-03-15"): 7200,
    }


def test_snapshot_row_map_merges_same_key_segments():
    snapshot = _snapshot()
    snapshot["students"][0]["checkins"].append(
        {
            "event_id": "E-04",
            "activity_type": "regular",
            "counts": True,
            "academic_days": [{"day": "2024-03-15", "seconds": 600}],
        }
    )
    rows = recon.snapshot_row_map(snapshot)
    assert rows[("S1", "regular", "2024-03-15")] == 4200


def test_content_fingerprint_is_order_insensitive_for_keys():
    a = {"x": 1, "y": [1, 2], "z": {"a": "b"}}
    b = {"z": {"a": "b"}, "y": [1, 2], "x": 1}
    assert recon.content_fingerprint(a) == recon.content_fingerprint(b)
    assert recon.content_fingerprint(a) != recon.content_fingerprint({"x": 1})


def test_external_row_map_dedupes_identical_rows():
    rows = [
        {"student_id": "S1", "activity_type": "regular",
         "academic_day": "2024-03-15", "seconds": 100},
        {"student_id": "S1", "activity_type": "regular",
         "academic_day": "2024-03-15", "seconds": 100},
    ]
    assert recon.external_row_map(rows) == {("S1", "regular", "2024-03-15"): 100}


def test_external_row_map_rejects_conflicting_duplicates():
    rows = [
        {"student_id": "S1", "activity_type": "regular",
         "academic_day": "2024-03-15", "seconds": 100},
        {"student_id": "S1", "activity_type": "regular",
         "academic_day": "2024-03-15", "seconds": 200},
    ]
    with pytest.raises(ReconDataError):
        recon.external_row_map(rows)


def test_normalize_external_rows_validates_fields():
    with pytest.raises(ReconDataError):
        recon.normalize_external_rows(
            [{"student_id": "", "activity_type": "regular",
              "academic_day": "2024-03-15", "seconds": 1}]
        )
    with pytest.raises(ReconDataError):
        recon.normalize_external_rows(
            [{"student_id": "S1", "activity_type": "regular",
              "academic_day": "2024-03-15", "seconds": -5}]
        )
    with pytest.raises(ReconDataError):
        recon.normalize_external_rows(
            [{"student_id": "S1", "activity_type": "regular",
              "academic_day": "2024-03-15", "seconds": True}]
        )


def test_diff_row_maps_categorizes_and_sorts():
    snapshot_map = {
        ("S1", "regular", "2024-03-15"): 3600,  # 与外部一致
        ("S1", "regular", "2024-03-16"): 1800,  # 外部缺失
        ("S2", "regular", "2024-03-15"): 7200,  # 秒数不一致
    }
    external_map = {
        ("S1", "regular", "2024-03-15"): 3600,
        ("S2", "regular", "2024-03-15"): 7000,
        ("S3", "regular", "2024-03-15"): 900,  # 快照缺失
    }
    diffs = recon.diff_row_maps(snapshot_map, external_map)
    assert [d["category"] for d in diffs] == [
        recon.MISSING_IN_EXTERNAL,
        recon.SECONDS_MISMATCH,
        recon.MISSING_IN_SNAPSHOT,
    ]
    by_student = {d["student_id"]: d for d in diffs}
    assert by_student["S1"]["snapshot_seconds"] == 1800
    assert by_student["S1"]["external_seconds"] == 0
    assert by_student["S1"]["delta_seconds"] == 1800
    assert by_student["S2"]["delta_seconds"] == 200
    assert by_student["S3"]["delta_seconds"] == -900


def test_exception_id_is_deterministic():
    id1 = recon.exception_id_for("B1", "B1-0001", "S1", "regular",
                                 "2024-03-15", recon.SECONDS_MISMATCH)
    id2 = recon.exception_id_for("B1", "B1-0001", "S1", "regular",
                                 "2024-03-15", recon.SECONDS_MISMATCH)
    id3 = recon.exception_id_for("B1", "B1-0001", "S1", "regular",
                                 "2024-03-15", recon.MISSING_IN_EXTERNAL)
    assert id1 == id2
    assert id1 != id3
    assert len(id1) == 64
