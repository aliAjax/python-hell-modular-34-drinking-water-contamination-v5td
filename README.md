# 市政饮用水污染响应

模块化纯 Python 3.9.6+ 标准库项目，默认端口 `8334`。

- `app.py`：服务生命周期和依赖组装。
- `src/domain.py`：水源、污染物、区域和来源校验。
- `src/rules.py`：污染评分、通知去重、停水、切换水源、冲洗、消毒、复检和恢复状态机。
- `src/repository.py`：SQLite、事务、重复保护、乐观版本和审计链。
- `src/service.py`：身份、角色和用例编排。
- `src/http_api.py`：JSON 接口与静态首页。
- `src/audit.py`：审计哈希。

```bash
python3 app.py --init --db ./data.db
python3 app.py --db ./data.db --port 8334
python3 -m unittest discover -s tests -v
```

接口包括 `GET /health`、`GET /api/state`、`POST /api/items`、`POST /api/items/<id>/sources`、`POST /api/items/<id>/actions` 和审计查询。

通知台账相关接口：

- `GET /api/items/<id>/notifications`：通知与渠道回执台账（含是否过期）。
- `POST /api/items/<id>/notifications`：下发通知（`notice_id`、`kind`、`message`、`channels`、`zone_ids`，需 `expected_version`）。
- `POST /api/items/<id>/notifications/<notice_id>/receipts`：登记渠道回执（`channel`、`zone_id`、`status`、`channel_time`）。
- `POST /api/items/<id>/notifications/resume`（或 `POST /api/resume`）：渠道恢复后重试未完成投递，已确认回执不重复发送。
- `GET /api/channels/status`、`POST /api/channels/<channel>/availability`：渠道可用状态（短信/应急广播），用于模拟渠道中断与恢复。

台账规则：同一通知编号的重复回执只记一次，回执按渠道真实时间推进、乱序不到退；投递失败或渠道不可用时保留为待重试，恢复后接着处理。恢复供水前核对当前受影响片区的成功回执，任一片区缺少回执即拦住并列出缺口；水源切换后原有通知依据作废，需按新水源重新下发并取得回执。并发提交采用乐观版本控制，落后方收到最新版本号与冲突编号（`CF-<id>`）。测试覆盖完整响应流程、重复事件、重复通知、复检阈值、权限和版本冲突。内置规则不替代真实水质模型、法定通报渠道或供水控制系统的联锁。
