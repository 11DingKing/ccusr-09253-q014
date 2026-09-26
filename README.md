# 实训学时合规与冻结服务

该服务汇聚学员签到、导师确认和请假修正事件，按培养方案与时区重放学时状态，并保存可追溯的学期冻结快照。项目还提供导师分配、证明材料、豁免复核、规则版本、名额、通知和数据留存等领域模块，供后续业务扩展时复用统一的状态与审计约束。

## 运行方式

默认数据保存在项目目录的 SQLite 文件中。安装依赖后执行 `uvicorn app.main:app --host 127.0.0.1 --port 8000`，健康检查地址为 `/health`，业务接口位于 `/api`。

## 测试

```bash
python3 -m pytest -q
```

## 编译检查

```bash
python3 -m compileall -q app tests
```

测试覆盖事件幂等导入、跨时区与跨日学时合并、实习确认、负向修正、冻结快照和差异查询；运行过程中不需要单独的数据库或网络服务。

## 对账批次

审计场景下可将多个培养方案的冻结快照与院校报送汇总表组成对账批次，接口位于 `/api/recon-batches`：

- `POST /api/recon-batches` 创建批次，固定各冻结快照与外部汇总的内容指纹（SHA-256）；内容相同的重复创建返回已有批次，外部文件更正须用新 `batch_id` 并通过 `supersedes_batch_id` 关联原批次，原批次结果保持不变。
- `POST /api/recon-batches/{id}/run` 运行对账作业：每个条目一次提交（断点续跑），已完成条目跳过、失败条目幂等重试，崩溃残留的条目在下次运行时自动恢复；差异按学生、活动类型和日期解释为异常项。
- 异常项支持认领（`POST .../exceptions/{eid}/claim`）与复核（`POST .../exceptions/{eid}/review`），并发操作只有一人成功，复核人不能是认领人；复核驳回会退回重新认领。
- `POST /api/recon-batches/{id}/sign` 在全部异常项复核完成后签署结果，已签署批次拒绝重跑与变更；`GET /api/recon-batches/{id}/export` 导出带内容指纹的确定性清单。
- 操作者通过 `X-Actor-Id`、`X-Actor-Role`（`auditor`/`reviewer`）与 `X-Actor-Dept` 请求头标识，批次按部门隔离权限范围。
