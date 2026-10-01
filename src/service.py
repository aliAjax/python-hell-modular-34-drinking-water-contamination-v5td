from . import domain, ledger, rules
from .channels import ChannelUnavailable, InMemoryChannelGateway
from .domain import DomainError


class Service:
    def __init__(self, repository, gateway=None):
        self.repository = repository
        self.gateway = gateway or InMemoryChannelGateway()

    def create_item(self, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.CREATE_ROLES:
            raise DomainError("forbidden", "当前角色不能创建此类业务记录", 403)
        normalized = domain.normalize_create(payload)
        stable_key = normalized.pop("_stable_key")
        return self.repository.create_item(
            rules.ENTITY_TYPE, stable_key, rules.INITIAL_STATUS, normalized, actor, role
        )

    def add_source(self, item_id, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.SOURCE_ROLES:
            raise DomainError("forbidden", "当前角色不能提交来源记录", 403)
        item = self.repository.get_item(item_id)
        normalized = domain.normalize_source(payload)
        if region and rules.ENFORCE_REGION and role != "regulator" and normalized.get("region") and normalized["region"] != region:
            raise DomainError("region_mismatch", "来源记录不属于当前管辖区域", 403)
        result = self.repository.add_source(
            item_id,
            normalized.pop("source_type"),
            normalized.pop("external_id"),
            normalized,
            normalized.pop("observed_at"),
            actor,
            role,
        )
        return result

    def act(self, item_id, action, payload, actor, role, expected_version=None, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        item = self.repository.get_item(item_id)
        allowed = rules.ACTION_ROLES.get(action, set())
        if role not in allowed:
            raise DomainError("forbidden", "当前角色不能执行该操作", 403)
        if rules.ENFORCE_REGION and action in rules.REGION_SENSITIVE_ACTIONS and region and role != "regulator":
            if item["payload"].get("region") != region:
                raise DomainError("region_mismatch", "不能处理其他区域的记录", 403)
        if action in rules.ACTION_REQUIRES_VERSION and expected_version is None:
            raise DomainError("expected_version_required", "该操作需要 expected_version", 400)
        if action in rules.ACTION_REQUIRES_VERSION and expected_version is not None and int(expected_version) != int(item["version"]):
            # 版本已被别人推进：直接判冲突，让败方看到最新版本和冲突编号
            self.repository.raise_conflict(item_id, actor, role, {
                "code": "version_conflict",
                "latest_version": int(item["version"]),
                "detail": action,
            })
        invalidate_notices = False
        if action == "restore":
            # 恢复审批并入通知账本：任一片区缺成功回执即拦截并列出缺口
            basis, gaps = ledger.restore_gaps(
                item,
                self.repository.list_notices(item_id),
                self.repository.list_deliveries(item_id),
            )
            if gaps:
                raise DomainError(
                    "receipt_gap",
                    "存在片区缺少成功回执，恢复请求被拦截",
                    409,
                    details={"notice_basis": basis, "gaps": gaps},
                )
        new_status, new_payload, event_payload = rules.apply_action(item, action, payload, actor, role)
        if action == "switch_source" and event_payload.get("zones_changed"):
            invalidate_notices = True
        self.repository.apply_action(
            item_id, action, actor, role, new_status, new_payload, event_payload, expected_version,
            invalidate_notices=invalidate_notices,
        )
        return self.get_item(item_id)

    def dispatch_notice(self, item_id, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in ledger.DISPATCH_ROLES:
            raise DomainError("forbidden", "当前角色不能下发通知", 403)
        item = self.repository.get_item(item_id)
        if item["status"] not in ledger.DISPATCH_STATUSES:
            raise DomainError("invalid_state", "当前状态 %s 不允许下发通知" % item["status"], 409)
        expected_version = payload.get("expected_version")
        if expected_version is None:
            raise DomainError("expected_version_required", "该操作需要 expected_version", 400)
        notice = ledger.normalize_notice(payload, item)
        deliveries = ledger.plan_deliveries(item_id, notice)
        stored = self.repository.dispatch_notice(item_id, notice, deliveries, actor, role, expected_version)
        summary = self._attempt_deliveries(self.repository.list_deliveries(item_id, notice["notice_id"]), notice)
        return {
            "notice": stored,
            "deliveries": self.repository.list_deliveries(item_id, notice["notice_id"]),
            "dispatch": summary,
            "item_version": self.repository.get_item(item_id)["version"],
        }

    def record_receipt(self, item_id, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in ledger.RECEIPT_ROLES:
            raise DomainError("forbidden", "当前角色不能登记回执", 403)
        receipt = ledger.normalize_receipt(payload)
        return self.repository.record_receipt(item_id, receipt, actor, role)

    def resume_pending(self, actor, role, item_id=None):
        """渠道恢复后续传：只处理活动通知下待重试的投递，已确认/已发送的不重发。"""
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in ledger.RESUME_ROLES:
            raise DomainError("forbidden", "当前角色不能续传通知", 403)
        pending = self.repository.pending_deliveries(item_id)
        summary = self._attempt_deliveries(pending, None)
        summary["remaining"] = len(self.repository.pending_deliveries(item_id))
        return summary

    def _attempt_deliveries(self, deliveries, notice):
        summary = {"sent": [], "pending_retry": [], "channel_available": True}
        for delivery in deliveries:
            message = delivery.get("notice_message") if notice is None else notice["message"]
            key = "%s:%s:%s:%s" % (
                delivery["item_id"], delivery["notice_id"], delivery["zone_id"], delivery["channel"],
            )
            try:
                self.gateway.send(key, delivery["channel"], delivery["zone_id"], message)
            except ChannelUnavailable as exc:
                self.repository.mark_delivery_retry(delivery, str(exc))
                summary["pending_retry"].append(key)
                summary["channel_available"] = False
            else:
                self.repository.mark_delivery_sent(delivery, key)
                summary["sent"].append(key)
        return summary

    def ledger_view(self, item_id):
        item = self.repository.get_item(item_id)
        notices = self.repository.list_notices(item_id)
        deliveries = self.repository.list_deliveries(item_id)
        receipts = self.repository.list_receipts(item_id)
        basis, gaps = ledger.restore_gaps(item, notices, deliveries)
        return {
            "item_id": item_id,
            "notice_basis": basis,
            "notices": notices,
            "deliveries": deliveries,
            "receipts": receipts,
            "restore_gaps": gaps,
        }

    def get_item(self, item_id):
        item = self.repository.get_item(item_id)
        item["sources"] = self.repository.list_sources(item_id)
        item["audit"] = self.repository.audit_trail(item_id)
        item["assessment"] = rules.assess(item["payload"])
        return item

    def list_items(self, status=None):
        return self.repository.list_items(status)

    def state(self):
        return self.repository.state_summary()
