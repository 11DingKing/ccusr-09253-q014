"""批量对账 API 的集成测试。

覆盖：创建/运行/认领/复核/签署/导出、内容指纹固定、部分失败、
幂等重试、并发认领与复核、权限范围、断点续跑与重启恢复。
"""

from __future__ import annotations

import threading

import pytest

from app import recon, recon_services
from tests.conftest import SHANGHAI_PLAN, TestSessionLocal

AUDITOR = {"X-Actor-Id": "aud-1", "X-Actor-Role": "auditor"}
AUDITOR2 = {"X-Actor-Id": "aud-2", "X-Actor-Role": "auditor"}
REVIEWER = {"X-Actor-Id": "rev-1", "X-Actor-Role": "reviewer"}
REVIEWER2 = {"X-Actor-Id": "rev-2", "X-Actor-Role": "reviewer"}
ADMIN = {"X-Actor-Id": "adm-1", "X-Actor-Role": "admin"}


def _events() -> list[dict]:
    return [
        {
            "event_id": "E-01",
            "event_type": "checkin",
            "student_id": "S1",
            "payload": {
                "activity_id": "A1",
                "activity_type": "regular",
                "check_in_at": "2024-03-15T08:00:00+08:00",
                "check_out_at": "2024-03-15T10:00:00+08:00",
            },
        },
        {
            "event_id": "E-02",
            "event_type": "checkin",
            "student_id": "S1",
            "payload": {
                "activity_id": "A2",
                "activity_type": "regular",
                "check_in_at": "2024-03-15T22:00:00+08:00",
                "check_out_at": "2024-03-16T00:30:00+08:00",
            },
        },
        {
            "event_id": "E-03",
            "event_type": "checkin",
            "student_id": "S2",
            "payload": {
                "activity_id": "A3",
                "activity_type": "internship",
                "check_in_at": "2024-03-15T09:00:00+08:00",
                "check_out_at": "2024-03-15T11:00:00+08:00",
            },
        },
        {
            "event_id": "E-04",
            "event_type": "mentor_confirm",
            "student_id": "S2",
            "payload": {"checkin_event_id": "E-03"},
        },
        {
            "event_id": "E-05",
            "event_type": "checkin",
            "student_id": "S2",
            "payload": {
                "activity_id": "A4",
                "activity_type": "regular",
                "check_in_at": "2024-03-16T13:00:00+08:00",
                "check_out_at": "2024-03-16T14:00:00+08:00",
            },
        },
    ]


def _setup_freeze(client, freeze_id: str = "F-01", events: list[dict] | None = None):
    resp = client.post("/api/plans", json=SHANGHAI_PLAN)
    assert resp.status_code == 201, resp.text
    pv = SHANGHAI_PLAN["plan_version"]
    resp = client.post(f"/api/plans/{pv}/events", json={"events": events or _events()})
    assert resp.status_code == 201, resp.text
    resp = client.post(f"/api/plans/{pv}/freezes/{freeze_id}", json={})
    assert resp.status_code == 201, resp.text
    return pv


def _external_rows() -> list[dict]:
    """与 _events() 产生的快照行对应：2 条一致、1 条不一致、1 条多报。"""
    return [
        {"student_id": "S1", "activity_type": "regular",
         "academic_day": "2024-03-15", "seconds": 14400},
        {"student_id": "S1", "activity_type": "regular",
         "academic_day": "2024-03-16", "seconds": 1500},  # 快照为 1800
        {"student_id": "S2", "activity_type": "regular",
         "academic_day": "2024-03-16", "seconds": 3600},
        {"student_id": "S3", "activity_type": "regular",
         "academic_day": "2024-03-15", "seconds": 600},  # 快照不存在
        # (S2, internship, 2024-03-15) 未报送 -> missing_in_external
    ]


def _create_batch(client, batch_id: str, items: list[dict], headers: dict = AUDITOR):
    resp = client.post(
        f"/api/recon-batches/{batch_id}", json={"items": items}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _run_batch(client, batch_id: str, headers: dict = AUDITOR):
    resp = client.post(f"/api/recon-batches/{batch_id}/run", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _get_exceptions(client, batch_id: str, headers: dict = AUDITOR) -> list[dict]:
    resp = client.get(f"/api/recon-batches/{batch_id}/exceptions", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _resolve_all(client, batch_id: str) -> None:
    """认领并复核终结全部差异（认领人与复核人分离）。"""
    for exc in _get_exceptions(client, batch_id):
        resp = client.post(
            f"/api/recon-exceptions/{exc['exception_id']}/claim", headers=AUDITOR
        )
        assert resp.status_code == 200, resp.text
        resp = client.post(
            f"/api/recon-exceptions/{exc['exception_id']}/review",
            json={"decision": "resolved", "note": "已核实",
                  "expected_version": resp.json()["version"]},
            headers=REVIEWER,
        )
        assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------- 创建与运行


def test_create_run_and_export_happy_path(client):
    pv = _setup_freeze(client)
    freeze_body = client.get(f"/api/plans/{pv}/freezes/F-01").json()

    batch = _create_batch(
        client, "B-1",
        [{"plan_version": pv, "freeze_id": "F-01",
          "external_rows": _external_rows()}],
    )
    assert batch["status"] == "created"
    assert batch["item_count"] == 1
    item = batch["items"][0]
    # 内容指纹固定了输入：快照指纹与冻结内容一致，外部指纹可独立复算
    assert item["snapshot_fingerprint"] == recon.content_fingerprint(
        {k: v for k, v in freeze_body.items()}
    )
    assert item["external_fingerprint"] == recon.content_fingerprint(
        recon.normalize_external_rows(_external_rows())
    )

    batch = _run_batch(client, "B-1")
    assert batch["status"] == "completed"
    assert batch["done_count"] == 1
    assert batch["exception_count"] == 3
    item = batch["items"][0]
    assert item["matched_count"] == 2
    assert item["exception_count"] == 3
    assert item["attempts"] == 1

    exceptions = _get_exceptions(client, "B-1")
    assert [(e["student_id"], e["category"]) for e in exceptions] == [
        ("S1", "seconds_mismatch"),
        ("S2", "missing_in_external"),
        ("S3", "missing_in_snapshot"),
    ]
    by_student = {e["student_id"]: e for e in exceptions}
    assert by_student["S1"]["academic_day"] == "2024-03-16"
    assert by_student["S1"]["snapshot_seconds"] == 1800
    assert by_student["S1"]["external_seconds"] == 1500
    assert by_student["S1"]["delta_seconds"] == 300
    assert by_student["S2"]["activity_type"] == "internship"
    assert by_student["S2"]["delta_seconds"] == 7200
    assert by_student["S3"]["delta_seconds"] == -600

    # 重复运行是幂等空操作：不产生新差异，不重跑已完成项
    again = _run_batch(client, "B-1")
    assert again["status"] == "completed"
    assert again["exception_count"] == 3
    assert again["items"][0]["attempts"] == 1

    # 导出包含按学生/活动类型/日期的差异解释，指纹稳定
    export1 = client.get("/api/recon-batches/B-1/export", headers=AUDITOR).json()
    export2 = client.get("/api/recon-batches/B-1/export", headers=AUDITOR).json()
    assert export1["export_fingerprint"] == export2["export_fingerprint"]
    students = {b["student_id"]: b for b in export1["breakdowns"]["by_student"]}
    assert students["S2"]["delta_seconds"] == 7200
    types = {b["activity_type"]: b for b in export1["breakdowns"]["by_activity_type"]}
    assert types["internship"]["count"] == 1
    days = {b["academic_day"]: b for b in export1["breakdowns"]["by_academic_day"]}
    assert days["2024-03-15"]["count"] == 2
    assert days["2024-03-16"]["count"] == 1
    assert export1["totals"]["open_exception_count"] == 3


def test_create_is_idempotent_and_correction_requires_new_batch(client):
    pv = _setup_freeze(client)
    item = {"plan_version": pv, "freeze_id": "F-01",
            "external_rows": _external_rows()}

    first = _create_batch(client, "B-1", [item])
    second = _create_batch(client, "B-1", [item])
    assert first["items"][0]["external_fingerprint"] == (
        second["items"][0]["external_fingerprint"]
    )
    assert second["item_count"] == 1

    # 同一批次号但外部文件内容不同 -> 409，更正必须生成新批次
    changed = dict(item, external_rows=_external_rows()[:-1])
    resp = client.post(
        "/api/recon-batches/B-1", json={"items": [changed]}, headers=AUDITOR
    )
    assert resp.status_code == 409

    corrected_rows = [
        row if row["student_id"] != "S1" or row["academic_day"] != "2024-03-16"
        else {**row, "seconds": 1800}
        for row in _external_rows()
    ]
    batch = _create_batch(
        client, "B-2", [{"plan_version": pv, "freeze_id": "F-01",
                         "external_rows": corrected_rows}]
    )
    assert batch["items"][0]["external_fingerprint"] != (
        first["items"][0]["external_fingerprint"]
    )
    batch = _run_batch(client, "B-2")
    categories = {e["category"] for e in _get_exceptions(client, "B-2")}
    assert "seconds_mismatch" not in categories  # 更正后该差异消失


def test_batch_inputs_stay_pinned_after_late_events(client):
    pv = _setup_freeze(client)
    _create_batch(
        client, "B-1",
        [{"plan_version": pv, "freeze_id": "F-01",
          "external_rows": _external_rows()}],
    )
    before = client.get("/api/recon-batches/B-1", headers=AUDITOR).json()

    # 冻结之后又有新事件并生成新冻结，批次仍绑定原快照指纹
    client.post(
        f"/api/plans/{pv}/events",
        json={"events": [{
            "event_id": "E-09", "event_type": "leave_correction",
            "student_id": "S1",
            "payload": {"adjustment_seconds": 3600, "reason": "补时"},
        }]},
    )
    client.post(f"/api/plans/{pv}/freezes/F-02", json={})

    after = client.get("/api/recon-batches/B-1", headers=AUDITOR).json()
    assert before["items"][0]["snapshot_fingerprint"] == (
        after["items"][0]["snapshot_fingerprint"]
    )
    _run_batch(client, "B-1")
    export1 = client.get("/api/recon-batches/B-1/export", headers=AUDITOR).json()
    export2 = client.get("/api/recon-batches/B-1/export", headers=AUDITOR).json()
    assert export1["export_fingerprint"] == export2["export_fingerprint"]


# ---------------------------------------------------------------- 认领与复核


def test_claim_and_review_workflow(client):
    pv = _setup_freeze(client)
    _create_batch(
        client, "B-1",
        [{"plan_version": pv, "freeze_id": "F-01",
          "external_rows": _external_rows()}],
    )
    _run_batch(client, "B-1")
    exc = _get_exceptions(client, "B-1")[0]
    exc_id = exc["exception_id"]
    assert exc["status"] == "open"
    assert exc["version"] == 1

    # 未认领直接复核 -> 409
    resp = client.post(
        f"/api/recon-exceptions/{exc_id}/review",
        json={"decision": "resolved", "expected_version": 1},
        headers=REVIEWER,
    )
    assert resp.status_code == 409

    # 认领
    resp = client.post(f"/api/recon-exceptions/{exc_id}/claim", headers=AUDITOR)
    assert resp.status_code == 200, resp.text
    claimed = resp.json()
    assert claimed["status"] == "claimed"
    assert claimed["assignee"] == "aud-1"
    assert claimed["version"] == 2

    # 重复认领 -> 409
    resp = client.post(f"/api/recon-exceptions/{exc_id}/claim", headers=AUDITOR2)
    assert resp.status_code == 409

    # 版本不一致 -> 409
    resp = client.post(
        f"/api/recon-exceptions/{exc_id}/review",
        json={"decision": "resolved", "expected_version": 1},
        headers=REVIEWER,
    )
    assert resp.status_code == 409

    # 复核人与认领人相同 -> 403（职责分离）
    resp = client.post(
        f"/api/recon-exceptions/{exc_id}/review",
        json={"decision": "resolved", "expected_version": 2},
        headers=ADMIN | {"X-Actor-Id": "aud-1"},
    )
    assert resp.status_code == 403

    # 正常复核
    resp = client.post(
        f"/api/recon-exceptions/{exc_id}/review",
        json={"decision": "resolved", "note": "院校确认补报", "expected_version": 2},
        headers=REVIEWER,
    )
    assert resp.status_code == 200, resp.text
    reviewed = resp.json()
    assert reviewed["status"] == "resolved"
    assert reviewed["reviewer"] == "rev-1"
    assert reviewed["review_note"] == "院校确认补报"
    assert reviewed["version"] == 3

    # 已终结差异不能再认领
    resp = client.post(f"/api/recon-exceptions/{exc_id}/claim", headers=AUDITOR2)
    assert resp.status_code == 409


def test_concurrent_claim_only_one_wins(client):
    pv = _setup_freeze(client)
    _create_batch(
        client, "B-1",
        [{"plan_version": pv, "freeze_id": "F-01",
          "external_rows": _external_rows()}],
    )
    _run_batch(client, "B-1")
    exc_id = _get_exceptions(client, "B-1")[0]["exception_id"]

    outcomes: list[str] = []
    lock = threading.Lock()

    def _claim(actor: str) -> None:
        session = TestSessionLocal()
        try:
            recon_services.claim_exception(
                session, exception_id=exc_id, actor_id=actor
            )
            with lock:
                outcomes.append(actor)
        except recon_services.ExceptionStateError:
            with lock:
                outcomes.append("conflict")
        finally:
            session.close()

    threads = [
        threading.Thread(target=_claim, args=(f"aud-{i}",)) for i in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    winners = [o for o in outcomes if o != "conflict"]
    assert len(winners) == 1
    assert outcomes.count("conflict") == 3
    stored = _get_exceptions(client, "B-1")[0]
    assert stored["assignee"] == winners[0]
    assert stored["status"] == "claimed"


def test_concurrent_review_only_one_wins(client):
    pv = _setup_freeze(client)
    _create_batch(
        client, "B-1",
        [{"plan_version": pv, "freeze_id": "F-01",
          "external_rows": _external_rows()}],
    )
    _run_batch(client, "B-1")
    exc_id = _get_exceptions(client, "B-1")[0]["exception_id"]
    client.post(f"/api/recon-exceptions/{exc_id}/claim", headers=AUDITOR)

    outcomes: list[str] = []
    lock = threading.Lock()

    def _review(actor: str, decision: str) -> None:
        session = TestSessionLocal()
        try:
            recon_services.review_exception(
                session,
                exception_id=exc_id,
                actor_id=actor,
                decision=decision,
                note="并发复核",
                expected_version=2,
            )
            with lock:
                outcomes.append(actor)
        except recon_services.ExceptionStateError:
            with lock:
                outcomes.append("conflict")
        finally:
            session.close()

    threads = [
        threading.Thread(target=_review, args=("rev-1", "resolved")),
        threading.Thread(target=_review, args=("rev-2", "dismissed")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    winners = [o for o in outcomes if o != "conflict"]
    assert len(winners) == 1
    stored = _get_exceptions(client, "B-1")[0]
    assert stored["reviewer"] == winners[0]
    assert stored["status"] in ("resolved", "dismissed")
    assert stored["version"] == 3


# ---------------------------------------------------------------- 签署


def test_sign_requires_terminal_exceptions_then_locks_batch(client):
    pv = _setup_freeze(client)
    _create_batch(
        client, "B-1",
        [{"plan_version": pv, "freeze_id": "F-01",
          "external_rows": _external_rows()}],
    )
    _run_batch(client, "B-1")

    # 差异未终结 -> 不能签署
    resp = client.post("/api/recon-batches/B-1/sign", headers=ADMIN)
    assert resp.status_code == 409

    _resolve_all(client, "B-1")
    resp = client.post("/api/recon-batches/B-1/sign", headers=ADMIN)
    assert resp.status_code == 200, resp.text
    signed = resp.json()
    assert signed["status"] == "signed"
    assert signed["signed_by"] == "adm-1"
    assert signed["open_exception_count"] == 0

    export_after_sign = client.get(
        "/api/recon-batches/B-1/export", headers=AUDITOR
    ).json()

    # 已签署批次：不可重跑、不可认领、不可复核、不可覆盖创建
    resp = client.post("/api/recon-batches/B-1/run", headers=AUDITOR)
    assert resp.status_code == 409
    exc_id = export_after_sign["exceptions"][0]["exception_id"]
    resp = client.post(f"/api/recon-exceptions/{exc_id}/claim", headers=AUDITOR)
    assert resp.status_code == 409
    resp = client.post(
        f"/api/recon-exceptions/{exc_id}/review",
        json={"decision": "dismissed", "expected_version": 3},
        headers=REVIEWER,
    )
    assert resp.status_code == 409
    resp = client.post(
        "/api/recon-batches/B-1",
        json={"items": [{"plan_version": pv, "freeze_id": "F-01",
                         "external_rows": []}]},
        headers=AUDITOR,
    )
    assert resp.status_code == 409

    # 外部文件更正走新批次，已签署批次的导出结果保持不变
    _create_batch(
        client, "B-2",
        [{"plan_version": pv, "freeze_id": "F-01", "external_rows": []}],
    )
    _run_batch(client, "B-2")
    export_final = client.get(
        "/api/recon-batches/B-1/export", headers=AUDITOR
    ).json()
    assert export_final["export_fingerprint"] == export_after_sign["export_fingerprint"]
    assert export_final["status"] == "signed"


# ---------------------------------------------------------------- 权限范围


def test_permission_scopes(client):
    pv = _setup_freeze(client)
    item = {"plan_version": pv, "freeze_id": "F-01",
            "external_rows": _external_rows()}

    # 未携带操作者头 -> 401；未知角色 -> 401
    resp = client.post("/api/recon-batches/B-1", json={"items": [item]})
    assert resp.status_code == 401
    resp = client.post(
        "/api/recon-batches/B-1", json={"items": [item]},
        headers={"X-Actor-Id": "x", "X-Actor-Role": "ghost"},
    )
    assert resp.status_code == 401

    # 复核员不能建批/运行/认领
    resp = client.post("/api/recon-batches/B-1", json={"items": [item]},
                       headers=REVIEWER)
    assert resp.status_code == 403

    _create_batch(client, "B-1", [item])
    resp = client.post("/api/recon-batches/B-1/run", headers=REVIEWER)
    assert resp.status_code == 403
    _run_batch(client, "B-1")
    exc_id = _get_exceptions(client, "B-1")[0]["exception_id"]
    resp = client.post(f"/api/recon-exceptions/{exc_id}/claim", headers=REVIEWER)
    assert resp.status_code == 403

    # 审计员不能复核/签署
    client.post(f"/api/recon-exceptions/{exc_id}/claim", headers=AUDITOR)
    resp = client.post(
        f"/api/recon-exceptions/{exc_id}/review",
        json={"decision": "resolved", "expected_version": 2},
        headers=AUDITOR2,
    )
    assert resp.status_code == 403
    resp = client.post("/api/recon-batches/B-1/sign", headers=AUDITOR)
    assert resp.status_code == 403

    # 审计员与复核员都可导出；读取接口任意合法操作者可用
    assert client.get("/api/recon-batches/B-1/export", headers=AUDITOR).status_code == 200
    assert client.get("/api/recon-batches/B-1/export", headers=REVIEWER).status_code == 200
    assert client.get("/api/recon-batches/B-1", headers=REVIEWER).status_code == 200
    assert client.get("/api/recon-batches", headers=AUDITOR).status_code == 200
    assert client.get("/api/recon-batches/B-1").status_code == 401


# ---------------------------------------------------------------- 部分失败与重试


def test_partial_failure_and_idempotent_retry(client):
    pv = SHANGHAI_PLAN["plan_version"]
    client.post("/api/plans", json=SHANGHAI_PLAN)
    events = _events()
    client.post(f"/api/plans/{pv}/events", json={"events": events[:1]})
    client.post(f"/api/plans/{pv}/freezes/F-A", json={})
    client.post(f"/api/plans/{pv}/events", json={"events": events[1:]})
    client.post(f"/api/plans/{pv}/freezes/F-B", json={})

    batch = _create_batch(
        client, "B-P",
        [
            {"plan_version": pv, "freeze_id": "F-A",
             "external_rows": [
                 {"student_id": "S1", "activity_type": "regular",
                  "academic_day": "2024-03-15", "seconds": 7000}]},
            {"plan_version": pv, "freeze_id": "F-A",
             "external_rows": [
                 {"student_id": "S1", "activity_type": "regular",
                  "academic_day": "2024-03-15", "seconds": 100},
                 {"student_id": "S1", "activity_type": "regular",
                  "academic_day": "2024-03-15", "seconds": 200}]},  # 冲突行
            {"plan_version": pv, "freeze_id": "F-B",
             "external_rows": [
                 {"student_id": "S2", "activity_type": "internship",
                  "academic_day": "2024-03-15", "seconds": 7000}]},
        ],
    )
    assert batch["item_count"] == 3

    batch = _run_batch(client, "B-P")
    assert batch["status"] == "completed_with_failures"
    assert batch["done_count"] == 2
    assert batch["failed_count"] == 1
    items = {i["item_id"]: i for i in batch["items"]}
    assert items["B-P-0002"]["status"] == "failed"
    assert "冲突" in items["B-P-0002"]["error"]
    assert items["B-P-0001"]["status"] == "done"
    assert items["B-P-0003"]["status"] == "done"
    # 失败项不影响其他项产出差异（item1 一条不一致，item3 四条）
    exceptions = _get_exceptions(client, "B-P")
    assert len(exceptions) == 5
    assert {e["item_id"] for e in exceptions} == {"B-P-0001", "B-P-0003"}

    # 幂等重试：失败项重跑仍确定性地失败，成功项不重跑，差异不重复
    batch = _run_batch(client, "B-P")
    assert batch["status"] == "completed_with_failures"
    items = {i["item_id"]: i for i in batch["items"]}
    assert items["B-P-0002"]["attempts"] == 2
    assert items["B-P-0001"]["attempts"] == 1
    assert items["B-P-0003"]["attempts"] == 1
    assert len(_get_exceptions(client, "B-P")) == 5


# ---------------------------------------------------------------- 断点续跑与重启恢复


def _mismatching_rows() -> list[dict]:
    """使 4 个快照键全部产生差异的外部行。"""
    return [
        {"student_id": "S1", "activity_type": "regular",
         "academic_day": "2024-03-15", "seconds": 1},
        {"student_id": "S1", "activity_type": "regular",
         "academic_day": "2024-03-16", "seconds": 1},
        {"student_id": "S2", "activity_type": "internship",
         "academic_day": "2024-03-15", "seconds": 1},
        {"student_id": "S2", "activity_type": "regular",
         "academic_day": "2024-03-16", "seconds": 1},
    ]


def test_resume_from_checkpoint_after_mid_item_crash(client, monkeypatch):
    pv = _setup_freeze(client)
    _create_batch(
        client, "B-R",
        [{"plan_version": pv, "freeze_id": "F-01",
          "external_rows": _mismatching_rows()}],
    )

    # 第一个分片提交断点后模拟崩溃
    original_save = recon_services.repo.save_item_progress
    crashed = {"done": False}

    def flaky_save(db, item_id, **kwargs):
        result = original_save(db, item_id, **kwargs)
        if kwargs.get("cursor") == 2 and not crashed["done"]:
            crashed["done"] = True
            raise RuntimeError("模拟进程崩溃")
        return result

    monkeypatch.setattr(recon_services.repo, "save_item_progress", flaky_save)

    session = TestSessionLocal()
    try:
        batch = recon_services.run_batch(
            session, batch_id="B-R", actor_id="aud-1", chunk_size=2
        )
        assert batch["status"] == "failed"
        item = batch["items"][0]
        assert item["status"] == "failed"
        assert item["cursor"] == 2  # 断点已持久化
        assert "模拟进程崩溃" in item["error"]
    finally:
        session.close()

    # 重启恢复：新会话、计数插入调用，验证已处理键被跳过
    inserted: list[int] = []
    original_insert = recon_services.repo.insert_exceptions_ignore_duplicates

    def counting_insert(db, rows):
        inserted.append(len(rows))
        return original_insert(db, rows)

    monkeypatch.setattr(
        recon_services.repo, "insert_exceptions_ignore_duplicates", counting_insert
    )
    session = TestSessionLocal()
    try:
        batch = recon_services.run_batch(
            session, batch_id="B-R", actor_id="aud-1", chunk_size=2
        )
        assert batch["status"] == "completed"
        item = batch["items"][0]
        assert item["attempts"] == 2
        assert item["cursor"] == 4
        assert item["exception_count"] == 4
        assert sum(inserted) == 2  # 只补跑剩余 2 个键
    finally:
        session.close()

    exceptions = _get_exceptions(client, "B-R")
    assert len(exceptions) == 4
    assert len({e["exception_id"] for e in exceptions}) == 4  # 无重复


def test_resume_after_crash_between_items(client, monkeypatch):
    pv = _setup_freeze(client)
    _create_batch(
        client, "B-R2",
        [{"plan_version": pv, "freeze_id": "F-01",
          "external_rows": _mismatching_rows()}],
    )

    # 全部批次项完成后、批次收尾前崩溃 -> 批次停留在 running
    def boom(db, batch_id):
        raise RuntimeError("模拟收尾阶段崩溃")

    monkeypatch.setattr(recon_services, "_finalize_batch", boom)
    session = TestSessionLocal()
    try:
        with pytest.raises(RuntimeError):
            recon_services.run_batch(session, batch_id="B-R2", actor_id="aud-1")
    finally:
        session.close()

    session = TestSessionLocal()
    try:
        assert recon_services.get_batch_detail(session, "B-R2")["status"] == "running"
    finally:
        session.close()

    monkeypatch.undo()
    session = TestSessionLocal()
    try:
        batch = recon_services.run_batch(session, batch_id="B-R2", actor_id="aud-1")
        assert batch["status"] == "completed"
        assert batch["items"][0]["attempts"] == 1  # 已完成项未重跑
        assert batch["exception_count"] == 4
    finally:
        session.close()
    assert len(_get_exceptions(client, "B-R2")) == 4


# ---------------------------------------------------------------- 查询与 404


def test_list_and_filter_exceptions(client):
    pv = _setup_freeze(client)
    _create_batch(
        client, "B-1",
        [{"plan_version": pv, "freeze_id": "F-01",
          "external_rows": _external_rows()}],
    )
    _run_batch(client, "B-1")
    exc_id = _get_exceptions(client, "B-1")[0]["exception_id"]
    client.post(f"/api/recon-exceptions/{exc_id}/claim", headers=AUDITOR)

    resp = client.get("/api/recon-batches/B-1/exceptions?status=claimed",
                      headers=AUDITOR)
    assert [e["exception_id"] for e in resp.json()] == [exc_id]
    resp = client.get("/api/recon-batches/B-1/exceptions?assignee=aud-1",
                      headers=REVIEWER)
    assert [e["exception_id"] for e in resp.json()] == [exc_id]
    resp = client.get("/api/recon-batches/B-1/exceptions?status=open",
                      headers=AUDITOR)
    assert len(resp.json()) == 2

    batches = client.get("/api/recon-batches", headers=AUDITOR).json()
    assert len(batches) == 1
    assert batches[0]["batch_id"] == "B-1"
    assert batches[0]["exception_count"] == 3
    # 未终结 = open(2) + claimed(1)
    assert batches[0]["open_exception_count"] == 3


def test_not_found_errors(client):
    pv = _setup_freeze(client)
    resp = client.post(
        "/api/recon-batches/B-X",
        json={"items": [{"plan_version": pv, "freeze_id": "F-NO",
                         "external_rows": []}]},
        headers=AUDITOR,
    )
    assert resp.status_code == 404
    resp = client.post(
        "/api/recon-batches/B-X",
        json={"items": [{"plan_version": "P-NO", "freeze_id": "F-01",
                         "external_rows": []}]},
        headers=AUDITOR,
    )
    assert resp.status_code == 404
    assert client.get("/api/recon-batches/B-NO", headers=AUDITOR).status_code == 404
    assert client.post("/api/recon-batches/B-NO/run", headers=AUDITOR).status_code == 404
    assert client.get("/api/recon-batches/B-NO/export", headers=AUDITOR).status_code == 404
    resp = client.post("/api/recon-exceptions/NOPE/claim", headers=AUDITOR)
    assert resp.status_code == 404
    resp = client.post(
        "/api/recon-exceptions/NOPE/review",
        json={"decision": "resolved", "expected_version": 1},
        headers=REVIEWER,
    )
    assert resp.status_code == 404
