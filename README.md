# 市政饮用水污染响应

模块化纯 Python 3.9.6+ 标准库项目，默认端口 `8334`。

- `app.py`：服务生命周期和依赖组装。
- `src/domain.py`：水源、污染物、区域和来源校验。
- `src/rules.py`：污染评分、通知去重、停水、切换水源、冲洗、消毒、复检和恢复状态机。
- `src/ledger.py`：通知账本规则——下发展开、回执去重与时间推进、恢复缺口对账。
- `src/channels.py`：渠道网关抽象（短信/应急广播），幂等发送，可替换真实适配器。
- `src/repository.py`：SQLite、事务、重复保护、乐观版本、通知/投递/回执账本和审计链。
- `src/service.py`：身份、角色和用例编排。
- `src/http_api.py`：JSON 接口与静态首页。
- `src/audit.py`：审计哈希。

```bash
python3 app.py --init --db ./data.db
python3 app.py --db ./data.db --port 8334
python3 -m unittest discover -s tests -v
```

接口包括 `GET /health`、`GET /api/state`、`POST /api/items`、`POST /api/items/<id>/sources`、`POST /api/items/<id>/actions` 和审计查询。测试覆盖完整响应流程、重复事件、重复通知、复检阈值、权限和版本冲突。内置规则不替代真实水质模型、法定通报渠道或供水控制系统的联锁。

## 通知账本（下发、回执、恢复审批一本账）

- `POST /api/items/<id>/notices`：下发通知。按 片区×渠道（`sms`/`broadcast`）展开投递行；
  需要 `expected_version`。同一通知编号重复下发或版本过期返回 409，
  `details` 带 `latest_version` 和 `conflict_id`（冲突同时写入审计链）。
- `POST /api/items/<id>/receipts`：登记渠道回执。`receipt_key` 缺省时按内容合成，
  同一通知编号的重复回执只记一次（返回 200 + `reason=duplicate`）；
  回执按渠道真实时间 `reported_at` 推进，早于已记账时间的回执只存档不改状态
  （`reason=stale`），更新的失败回执会重新打开投递等待重试。
- `POST /api/items/<id>/notices/resume` 与 `POST /api/notices/resume`：续传待重试投递。
  只处理活动通知下 `pending`/`failed` 的行；已发送、已确认、已作废的不重发，
  发送按幂等键去重，写入失败留下的部分重试时不会重复外发。
- `GET /api/items/<id>/ledger`：通知、投递、回执和恢复缺口的整本账。

恢复审批并入账本：`restore` 前逐片区×渠道核对当前依据版本下的成功回执，
任一片区缺成功回执即返回 409 `receipt_gap`，`details.gaps` 列出缺口片区与渠道。
`switch_source` 携带 `zone_ids` 且片区发生变化时，通知依据版本 `notice_basis` 升版，
原通知作废、未完成投递置为 `void`，须重新下发并收齐回执才能恢复。
两名值班员同时提交同一通知或恢复请求时只落一方，另一方收到 409 及最新版本、冲突编号。
渠道服务不可用时已确认回执保留在 SQLite 账上，重启后调用 resume 接口接着处理未完成通知。
