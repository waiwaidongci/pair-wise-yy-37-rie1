# 空气污染源许可与合规检查

管理设施、排放口、治理设备、现场检查、整改和许可续期。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8314
```

默认端口为`8314`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`，支持`facility`（设施）和`equipment`（治理设备名单，含覆盖污染物）
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/records/{rid}/close`，关闭检查/整改记录
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/audit`
- `POST /api/renewals`，按`request_id`幂等：重复提交返回原续期单（HTTP 200且`replayed=true`），只保留一张
- `GET /api/renewals`，可用`?status=`过滤
- `GET /api/renewals/{id}`
- `GET /api/renewals/by-request/{request_id}`，断网失败后按原请求编号恢复
- `POST /api/renewals/{id}/transition`，必须提交`expected_version`
- `POST /api/renewals/{id}/attachments`，仅applicant可上传
- `GET /api/renewals/{id}/attachments`，仅applicant和compliance_manager可看，检查员越权访问直接拒绝（403）

允许角色：applicant, inspector, compliance_manager, viewer。申报量超过许可量或检查发现高严重度问题时提高优先级；存在未关闭整改时不能批准。

## 续期规则

- 续期草案（draft）创建时快照旧许可版本（`permit_version`）和治理设备名单（`equipment`），并校验排放上限的污染物必须被设备名单覆盖，不一致直接拒绝。
- 检查（`inspection`）与整改（`rectification`）记录按同一设施（`facility`）跨许可归拢；同一设施内`external_ref`唯一，跨月补录重复记录会被拒绝，避免已关闭整改被带回来。
- 批准（approved）前校验：旧许可版本未变更、设备名单与旧许可一致、排放上限与设备名单匹配；已关闭整改不拦审批，仍开启的整改保留证据（编号、引用、详情）并挡住批准（409）。
- 批准后把新旧许可和检查、整改进度固化进`snapshot`，不再随后续记录变化；续期单和附件同时冻结。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
