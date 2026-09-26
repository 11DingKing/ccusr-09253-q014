"""对账批次 API 与服务层测试。"""

from __future__ import annotations

import threading

import pytest

from app import recon_service
from app.auth import Actor
from app.core.reconcile import canonical_fingerprint, normalize_external_entries
from tests.conftest import TestSessionLocal

PLAN_A = {
    "plan_version": "P-A-2024",
    "iana_timezone": "Asia/Shanghai",
    "required_seconds": 3600,
}
PLAN_B = {
    "plan_version": "P-B-2024",
    "iana_timezone": "Asia/Shanghai",
    "required_seconds": 3600,
}


def _headers(actor_id="aud-1", role="auditor", dept="east"):
    return {
        "X-Actor-Id": actor_id,
        "X-Actor-Role": role,
        "X-Actor-Dept": dept,
    }


def _actor(actor_id="aud-1", role="auditor", dept="east"):
    return Actor(actor_id=actor_id, role=role, dept=dept)


def _checkin(eid, student, start, end, activity_type="regular"):
    return {
        "event_id": eid,
        "event_type": "checkin",
        "student_id": student,
        "payload": {
            "activity_id": "A1",
            "activity_type": activity_type,
            "check_in_at": start,
            "check_out_at": end,
        },
    }


def _adjustment(eid, student, seconds, reason=""):
    return {
        "event_id": eid,
        "event_type": "leave_correction",
        "student_id": student,
        "payload": {"adjustment_seconds": seconds, "reason": reason},
    }


def _seed_plan(client, plan, events, freeze_id):
    resp = client.post("/api/plans", json=plan)
    assert resp.status_code == 201, resp.text
    if events:
        resp = client.post(
            f"/api/plans/{plan['plan_version']}/events", json={"events": events}
        )
        assert resp.status_code == 201, resp.text
    resp = client.post(f"/api/plans/{plan['plan_version']}/freezes/{freeze_id}", json={})
    assert resp.status_code == 201, resp.text


def _events_a():
    return [
        _checkin(
            "E-A1", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"
        ),
        _checkin(
            "E-A2", "S1", "2024-03-17T08:00:00+08:00", "2024-03-17T08:30:00+08:00"
        ),
        _adjustment("E-A3", "S1", 1800, "approved make-up"),
        _checkin(
            "E-A4",
            "S2",
            "2024-03-15T09:00:00+08:00",
            "2024-03-15T10:00:00+08:00",
            activity_type="internship",
        ),
        _checkin(
            "E-A5", "S3", "2024-03-16T08:00:00+08:00", "2024-03-16T09:00:00+08:00"
        ),
    ]


def _events_b():
    return [
        _checkin(
            "E-B1", "S9", "2024-03-15T08:00:00+08:00", "2024-03-15T09:00:00+08:00"
        )
    ]


def _external_entries():
    return [
        {
            "plan_version": PLAN_A["plan_version"],
            "student_id": "S1",
            "activity_type": "regular",
            "academic_day": "2024-03-15",
            "seconds": 7200,
        },
        {
            "plan_version": PLAN_A["plan_version"],
            "student_id": "S1",
            "activity_type": "adjustment",
            "academic_day": "",
            "seconds": 1800,
        },
        {
            "plan_version": PLAN_A["plan_version"],
            "student_id": "S3",
            "activity_type": "regular",
            "academic_day": "2024-03-16",
            "seconds": 3000,
        },
        {
            "plan_version": PLAN_A["plan_version"],
            "student_id": "S2",
            "activity_type": "regular",
            "academic_day": "2024-03-15",
            "seconds": 3600,
        },
        {
            "plan_version": PLAN_B["plan_version"],
            "student_id": "S9",
            "activity_type": "regular",
            "academic_day": "2024-03-15",
            "seconds": 3600,
        },
    ]


def _default_items():
    return [
        {"plan_version": PLAN_A["plan_version"], "freeze_id": "F-A1"},
        {"plan_version": PLAN_B["plan_version"], "freeze_id": "F-B1"},
    ]


def _create_batch(client, batch_id="B-1", headers=None, **overrides):
    body = {
        "batch_id": batch_id,
        "title": "2024 春季审计抽核",
        "items": _default_items(),
        "external_entries": _external_entries(),
    }
    body.update(overrides)
    return client.post("/api/recon-batches", json=body, headers=headers or _headers())


def _seed_two_plans(client):
    _seed_plan(client, PLAN_A, _events_a(), "F-A1")
    _seed_plan(client, PLAN_B, _events_b(), "F-B1")


def _run(client, batch_id="B-1", headers=None):
    resp = client.post(f"/api/recon-batches/{batch_id}/run", headers=headers or _headers())
    assert resp.status_code == 200, resp.text
    return resp.json()


def _exceptions(client, batch_id="B-1", headers=None):
    resp = client.get(
        f"/api/recon-batches/{batch_id}/exceptions", headers=headers or _headers()
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_create_run_and_export_explains_differences(client):
    """创建→运行→导出：差异按学生、活动类型和日期解释，指纹固定。"""
    _seed_two_plans(client)
    created = _create_batch(client)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == "created"
    assert body["total_items"] == 2
    assert body["external_fingerprint"] == canonical_fingerprint(
        normalize_external_entries(_external_entries())
    )
    assert all(item["snapshot_fingerprint"] for item in body["items"])

    ran = _run(client)
    assert ran["status"] == "completed"
    assert ran["processed_items"] == 2
    assert ran["matched_count"] == 3
    assert ran["discrepancy_count"] == 3

    exceptions = _exceptions(client)
    assert len(exceptions) == 3
    by_key = {
        (e["student_id"], e["activity_type"], e["academic_day"]): e
        for e in exceptions
    }
    mismatch = by_key[("S3", "regular", "2024-03-16")]
    assert mismatch["category"] == "amount_mismatch"
    assert mismatch["internal_seconds"] == 3600
    assert mismatch["external_seconds"] == 3000
    assert mismatch["delta_seconds"] == 600
    missing_internal = by_key[("S2", "regular", "2024-03-15")]
    assert missing_internal["category"] == "missing_in_snapshot"
    assert missing_internal["delta_seconds"] == -3600
    missing_external = by_key[("S1", "regular", "2024-03-17")]
    assert missing_external["category"] == "missing_in_external"
    assert missing_external["delta_seconds"] == 1800

    manifest = client.get("/api/recon-batches/B-1/export", headers=_headers()).json()
    assert manifest["manifest_fingerprint"] == canonical_fingerprint(
        {k: v for k, v in manifest.items() if k != "manifest_fingerprint"}
    )
    assert manifest["summary"]["discrepancy_count"] == 3
    grouped = manifest["grouped"]
    assert grouped["by_student"]["S3"]["delta_seconds"] == 600
    assert grouped["by_student"]["S1"]["count"] == 1
    assert grouped["by_activity_type"]["regular"]["count"] == 3
    assert grouped["by_academic_day"]["2024-03-17"]["delta_seconds"] == 1800
    assert grouped["by_academic_day"]["2024-03-15"]["delta_seconds"] == -3600
    assert [a["action"] for a in manifest["audit"]][:2] == [
        "batch_created",
        "run_started",
    ]


def test_snapshot_fingerprint_matches_freeze_content(client):
    """批次固定的快照指纹与冻结快照内容一致。"""
    _seed_two_plans(client)
    _create_batch(client)
    freeze = client.get(f"/api/plans/{PLAN_A['plan_version']}/freezes/F-A1").json()
    detail = client.get("/api/recon-batches/B-1", headers=_headers()).json()
    item_a = next(i for i in detail["items"] if i["plan_version"] == PLAN_A["plan_version"])
    assert item_a["snapshot_fingerprint"] == canonical_fingerprint(freeze)


def test_create_replay_returns_existing_and_conflicts_on_change(client):
    """创建幂等：内容相同返回已有批次，内容不同冲突。"""
    _seed_two_plans(client)
    first = _create_batch(client)
    assert first.status_code == 201
    replay = _create_batch(client)
    assert replay.status_code == 200
    assert replay.json()["external_fingerprint"] == first.json()["external_fingerprint"]
    changed = _create_batch(client, external_entries=_external_entries()[:1])
    assert changed.status_code == 409


def test_external_correction_creates_new_batch_and_preserves_old(client):
    """外部文件更正生成新批次，原批次结果保持不变。"""
    _seed_two_plans(client)
    _create_batch(client, batch_id="B-V1")
    _run(client, batch_id="B-V1")
    before = client.get("/api/recon-batches/B-V1/export", headers=_headers()).json()

    corrected = _external_entries()
    corrected[2] = {**corrected[2], "seconds": 3600}  # 院校更正 S3 报送秒数
    resp = client.post(
        "/api/recon-batches",
        json={
            "batch_id": "B-V2",
            "title": "更正后报送",
            "items": _default_items(),
            "external_entries": corrected,
            "supersedes_batch_id": "B-V1",
        },
        headers=_headers(),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["supersedes_batch_id"] == "B-V1"
    assert resp.json()["external_fingerprint"] != before["external_fingerprint"]

    _run(client, batch_id="B-V2")
    v2 = client.get("/api/recon-batches/B-V2", headers=_headers()).json()
    assert v2["status"] == "completed"
    assert v2["discrepancy_count"] == 2  # S3 的 amount_mismatch 已消除

    after = client.get("/api/recon-batches/B-V1/export", headers=_headers()).json()
    assert after == before


def test_supersedes_must_exist_and_share_scope(client):
    _seed_two_plans(client)
    _create_batch(client, batch_id="B-V1")
    missing = _create_batch(client, batch_id="B-ORPHAN", supersedes_batch_id="B-GONE")
    assert missing.status_code == 404
    cross_dept = _create_batch(
        client,
        batch_id="B-CROSS",
        supersedes_batch_id="B-V1",
        headers=_headers(dept="west"),
    )
    assert cross_dept.status_code == 403


def test_partial_failure_then_resume_after_freeze_created(client):
    """部分失败：缺失快照的条目标记失败，补齐后断点续跑完成。"""
    _seed_two_plans(client)
    items = [
        {"plan_version": PLAN_A["plan_version"], "freeze_id": "F-A1"},
        {"plan_version": PLAN_A["plan_version"], "freeze_id": "F-LATE"},
    ]
    created = _create_batch(client, batch_id="B-PART", items=items)
    assert created.status_code == 201

    ran = _run(client, batch_id="B-PART")
    assert ran["status"] == "partial"
    assert ran["processed_items"] == 1
    assert ran["error_count"] == 1
    err_item = next(i for i in ran["items"] if i["status"] == "error")
    assert err_item["freeze_id"] == "F-LATE"
    assert "F-LATE" in err_item["error"]

    # 补齐冻结快照后断点续跑：已完成条目不重跑，失败条目重试成功。
    client.post(f"/api/plans/{PLAN_A['plan_version']}/freezes/F-LATE", json={})
    resumed = _run(client, batch_id="B-PART")
    assert resumed["status"] == "completed"
    assert resumed["processed_items"] == 2
    assert resumed["discrepancy_count"] == 6  # 两个快照内容相同，各 3 条差异
    by_freeze = {i["freeze_id"]: i for i in resumed["items"]}
    assert by_freeze["F-A1"]["attempt"] == 1  # 已完成条目未重跑
    assert by_freeze["F-LATE"]["attempt"] == 2  # 失败条目幂等重试成功
    assert by_freeze["F-LATE"]["snapshot_fingerprint"]


def test_run_retry_is_idempotent(client):
    """幂等重试：重复运行不改变结果，不产生重复异常项。"""
    _seed_two_plans(client)
    _create_batch(client)
    first = _run(client)
    exc_first = _exceptions(client)
    export_first = client.get("/api/recon-batches/B-1/export", headers=_headers()).json()

    second = _run(client)
    assert second["status"] == "completed"
    assert second["discrepancy_count"] == first["discrepancy_count"]
    assert second["matched_count"] == first["matched_count"]
    assert all(i["attempt"] == 1 for i in second["items"])

    exc_second = _exceptions(client)
    assert [e["exception_id"] for e in exc_second] == [
        e["exception_id"] for e in exc_first
    ]
    export_second = client.get("/api/recon-batches/B-1/export", headers=_headers()).json()
    assert export_second["exceptions"] == export_first["exceptions"]
    assert export_second["summary"] == export_first["summary"]


def test_restart_recovery_after_crash(client, db, monkeypatch):
    """重启恢复：崩溃残留的 processing 条目被回收并重跑。"""
    _seed_two_plans(client)
    _create_batch(client)

    original = recon_service._process_item
    calls: list[str] = []

    class SimulatedCrash(BaseException):
        pass

    def flaky(db_, batch, item):
        calls.append(item.plan_version)
        if len(calls) == 2:
            raise SimulatedCrash("power loss")
        return original(db_, batch, item)

    monkeypatch.setattr(recon_service, "_process_item", flaky)
    with pytest.raises(SimulatedCrash):
        recon_service.run_batch(db, batch_id="B-1", actor=_actor())

    # 崩溃现场：批次停在 running，第二个条目卡在 processing。
    detail = client.get("/api/recon-batches/B-1", headers=_headers()).json()
    assert detail["status"] == "running"
    stuck = [i for i in detail["items"] if i["status"] == "processing"]
    assert len(stuck) == 1
    assert stuck[0]["plan_version"] == PLAN_B["plan_version"]

    monkeypatch.setattr(recon_service, "_process_item", original)
    recovered = _run(client)
    assert recovered["status"] == "completed"
    assert recovered["processed_items"] == 2
    assert recovered["discrepancy_count"] == 3
    attempts = {i["plan_version"]: i["attempt"] for i in recovered["items"]}
    assert attempts[PLAN_A["plan_version"]] == 1
    assert attempts[PLAN_B["plan_version"]] == 2  # 崩溃条目恢复后重跑


def test_claim_review_sign_flow_and_signed_batch_immutable(client):
    """认领→复核→签署全流程；已签署结果保持不变。"""
    _seed_two_plans(client)
    _create_batch(client)
    _run(client)
    exc_ids = [e["exception_id"] for e in _exceptions(client)]

    # 未复核完成不能签署。
    assert (
        client.post("/api/recon-batches/B-1/sign", headers=_headers("rev-1", "reviewer")).status_code
        == 409
    )

    claim = client.post(
        f"/api/recon-batches/B-1/exceptions/{exc_ids[0]}/claim", headers=_headers("aud-1")
    )
    assert claim.status_code == 200, claim.text
    assert claim.json()["status"] == "claimed"
    assert claim.json()["assignee"] == "aud-1"
    # 重复认领冲突。
    assert (
        client.post(
            f"/api/recon-batches/B-1/exceptions/{exc_ids[0]}/claim",
            headers=_headers("aud-2"),
        ).status_code
        == 409
    )
    # 认领人不能复核本人认领的项。
    assert (
        client.post(
            f"/api/recon-batches/B-1/exceptions/{exc_ids[0]}/review",
            json={"verdict": "confirmed"},
            headers=_headers("aud-1", "reviewer"),
        ).status_code
        == 403
    )
    # auditor 角色不能复核。
    assert (
        client.post(
            f"/api/recon-batches/B-1/exceptions/{exc_ids[0]}/review",
            json={"verdict": "confirmed"},
            headers=_headers("aud-2", "auditor"),
        ).status_code
        == 403
    )
    reviewed = client.post(
        f"/api/recon-batches/B-1/exceptions/{exc_ids[0]}/review",
        json={"verdict": "confirmed", "note": "与院校确认一致"},
        headers=_headers("rev-1", "reviewer"),
    )
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["status"] == "reviewed"
    assert reviewed.json()["reviewed_by"] == "rev-1"

    for exc_id in exc_ids[1:]:
        assert (
            client.post(
                f"/api/recon-batches/B-1/exceptions/{exc_id}/claim",
                headers=_headers("aud-2"),
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"/api/recon-batches/B-1/exceptions/{exc_id}/review",
                json={"verdict": "confirmed"},
                headers=_headers("rev-1", "reviewer"),
            ).status_code
            == 200
        )

    # auditor 角色不能签署。
    assert (
        client.post("/api/recon-batches/B-1/sign", headers=_headers("aud-1", "auditor")).status_code
        == 403
    )
    signed = client.post("/api/recon-batches/B-1/sign", headers=_headers("rev-1", "reviewer"))
    assert signed.status_code == 200, signed.text
    assert signed.json()["status"] == "signed"
    assert signed.json()["signed_by"] == "rev-1"

    # 已签署结果保持不变：重跑、认领、复核、再次签署均被拒绝。
    assert client.post("/api/recon-batches/B-1/run", headers=_headers()).status_code == 409
    assert (
        client.post(
            f"/api/recon-batches/B-1/exceptions/{exc_ids[0]}/claim",
            headers=_headers(),
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"/api/recon-batches/B-1/exceptions/{exc_ids[0]}/review",
            json={"verdict": "confirmed"},
            headers=_headers("rev-2", "reviewer"),
        ).status_code
        == 409
    )
    assert (
        client.post("/api/recon-batches/B-1/sign", headers=_headers("rev-1", "reviewer")).status_code
        == 409
    )
    export_a = client.get("/api/recon-batches/B-1/export", headers=_headers()).json()
    export_b = client.get("/api/recon-batches/B-1/export", headers=_headers()).json()
    assert export_a == export_b
    assert export_a["status"] == "signed"
    assert export_a["summary"]["reviewed_exceptions"] == 3


def test_review_rejection_reopens_exception(client):
    """复核驳回：异常项回到 open 并可重新认领。"""
    _seed_two_plans(client)
    _create_batch(client)
    _run(client)
    exc_id = _exceptions(client)[0]["exception_id"]

    client.post(
        f"/api/recon-batches/B-1/exceptions/{exc_id}/claim", headers=_headers("aud-1")
    )
    rejected = client.post(
        f"/api/recon-batches/B-1/exceptions/{exc_id}/review",
        json={"verdict": "rejected", "note": "解释不充分"},
        headers=_headers("rev-1", "reviewer"),
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "open"
    assert rejected.json()["assignee"] is None
    assert rejected.json()["review_verdict"] == "rejected"

    reclaimed = client.post(
        f"/api/recon-batches/B-1/exceptions/{exc_id}/claim", headers=_headers("aud-2")
    )
    assert reclaimed.status_code == 200
    assert reclaimed.json()["assignee"] == "aud-2"


def test_review_requires_claimed_state(client):
    _seed_two_plans(client)
    _create_batch(client)
    _run(client)
    exc_id = _exceptions(client)[0]["exception_id"]
    resp = client.post(
        f"/api/recon-batches/B-1/exceptions/{exc_id}/review",
        json={"verdict": "confirmed"},
        headers=_headers("rev-1", "reviewer"),
    )
    assert resp.status_code == 409


def test_export_requires_finished_batch(client):
    _seed_two_plans(client)
    _create_batch(client)
    assert client.get("/api/recon-batches/B-1/export", headers=_headers()).status_code == 409


def test_concurrent_claim_only_one_wins(client):
    """并发认领：同一异常项只有一人认领成功。"""
    _seed_two_plans(client)
    _create_batch(client)
    _run(client)
    exc_id = _exceptions(client)[0]["exception_id"]

    outcomes: list[str] = []
    lock = threading.Lock()

    def _claim(aid: str) -> None:
        session = TestSessionLocal()
        try:
            recon_service.claim_exception(
                session, batch_id="B-1", exception_id=exc_id, actor=_actor(aid)
            )
            outcome = "ok"
        except recon_service.ReconConflictError:
            outcome = "conflict"
        finally:
            session.close()
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=_claim, args=(f"aud-{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert outcomes.count("ok") == 1
    assert outcomes.count("conflict") == 3
    final = _exceptions(client)[0]
    assert final["status"] == "claimed"
    assert final["assignee"] is not None


def test_concurrent_review_only_one_wins(client):
    """并发复核：同一已认领异常项只有一人复核成功。"""
    _seed_two_plans(client)
    _create_batch(client)
    _run(client)
    exc_id = _exceptions(client)[0]["exception_id"]
    # 通过 API 认领，避免共享会话。
    client.post(
        f"/api/recon-batches/B-1/exceptions/{exc_id}/claim", headers=_headers("aud-1")
    )

    outcomes: list[str] = []
    lock = threading.Lock()

    def _review(rid: str) -> None:
        session = TestSessionLocal()
        try:
            recon_service.review_exception(
                session,
                batch_id="B-1",
                exception_id=exc_id,
                actor=_actor(rid, role="reviewer"),
                verdict="confirmed",
            )
            outcome = "ok"
        except recon_service.ReconConflictError:
            outcome = "conflict"
        finally:
            session.close()
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=_review, args=(f"rev-{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert outcomes.count("ok") == 1
    assert outcomes.count("conflict") == 3
    final = _exceptions(client)[0]
    assert final["status"] == "reviewed"
    assert final["reviewed_by"] is not None


def test_permission_scope_by_department_and_role(client):
    """权限范围：身份必需、角色受限、部门隔离。"""
    _seed_two_plans(client)
    # 缺少操作者身份。
    assert client.post(
        "/api/recon-batches",
        json={
            "batch_id": "B-NOAUTH",
            "items": _default_items(),
            "external_entries": [],
        },
    ).status_code == 401
    # 未知角色。
    assert (
        _create_batch(client, batch_id="B-BADROLE", headers=_headers(role="superuser")).status_code
        == 403
    )

    assert _create_batch(client, batch_id="B-EAST").status_code == 201
    # 跨部门不可读、不可操作。
    assert client.get("/api/recon-batches/B-EAST", headers=_headers(dept="west")).status_code == 403
    assert (
        client.post("/api/recon-batches/B-EAST/run", headers=_headers(dept="west")).status_code
        == 403
    )
    assert (
        client.get("/api/recon-batches/B-EAST/export", headers=_headers(dept="west")).status_code
        == 403
    )
    # 跨部门重复创建同样被拒绝。
    assert (
        _create_batch(client, batch_id="B-EAST", headers=_headers(dept="west")).status_code
        == 403
    )
    west_list = client.get("/api/recon-batches", headers=_headers(dept="west")).json()
    assert all(b["dept"] == "west" for b in west_list)
    east_list = client.get("/api/recon-batches", headers=_headers(dept="east")).json()
    assert [b["batch_id"] for b in east_list] == ["B-EAST"]
    # 同部门可运行。
    assert (
        client.post("/api/recon-batches/B-EAST/run", headers=_headers(dept="east")).status_code
        == 200
    )
