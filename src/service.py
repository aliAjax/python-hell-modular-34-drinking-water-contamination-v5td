from . import domain, rules
from .channels import ChannelGateway, CHANNELS
from .domain import DomainError
from .repository import now_iso


class Service:
    def __init__(self, repository, gateway=None):
        self.repository = repository
        self.gateway = gateway or ChannelGateway(repository)

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
        if expected_version is not None and int(expected_version) != int(item["version"]):
            raise self.repository.conflict_details(item_id, expected_version)

        if action == "advise":
            self.issue_notification(item_id, payload, actor, role, expected_version)
            return self.get_item(item_id)

        if action == "restore":
            new_status, new_payload, event_payload = rules.apply_action(item, action, payload, actor, role)
            self.repository.restore_item(
                item_id, expected_version, actor, role, new_status, new_payload, event_payload
            )
            return self.get_item(item_id)

        new_status, new_payload, event_payload = rules.apply_action(item, action, payload, actor, role)
        self.repository.apply_action(
            item_id, action, actor, role, new_status, new_payload, event_payload, expected_version
        )
        return self.get_item(item_id)

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

    # ------------------------------------------------------------------
    # 通知台账：下发、回执、重试与恢复核对
    # ------------------------------------------------------------------

    def issue_notification(self, item_id, payload, actor, role, expected_version=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.ACTION_ROLES["advise"]:
            raise DomainError("forbidden", "当前角色不能下发通知", 403)
        if expected_version is None:
            raise DomainError("expected_version_required", "下发通知需要 expected_version", 400)
        item = self.repository.get_item(item_id)
        notice_id = domain.require_text(payload, "notice_id")
        kind = domain.require_text(payload, "kind")
        message = domain.require_text(payload, "message")
        channels = payload.get("channels") or list(CHANNELS)
        if not isinstance(channels, list) or not channels:
            raise DomainError("invalid_channels", "渠道必须是非空列表", 400)
        if any(channel not in CHANNELS for channel in channels):
            raise DomainError("unknown_channel", "不支持的渠道，可选 sms/broadcast", 400)
        channels = list(dict.fromkeys(channels))
        target_zones = payload.get("zone_ids") or item["payload"].get("zone_ids", [])
        if not isinstance(target_zones, list) or not target_zones:
            raise DomainError("zones_required", "通知至少要覆盖一个片区", 400)
        target_zones = [str(zone).strip() for zone in target_zones if str(zone).strip()]
        if not target_zones:
            raise DomainError("zones_required", "通知至少要覆盖一个片区", 400)
        basis = rules.current_basis(item)
        self.repository.issue_notification(
            item_id, notice_id, kind, message, channels, target_zones, basis,
            expected_version, actor, role,
        )
        return self.get_item(item_id)

    def record_receipt(self, item_id, notice_id, payload, actor="channel", role="system"):
        channel = domain.require_text(payload, "channel")
        zone_id = domain.require_text(payload, "zone_id")
        status = domain.require_text(payload, "status")
        if channel not in CHANNELS:
            raise DomainError("unknown_channel", "不支持的渠道，可选 sms/broadcast", 400)
        if status not in ("success", "failed"):
            raise DomainError("invalid_receipt_status", "回执状态必须是 success 或 failed", 400)
        channel_time = payload.get("channel_time")
        if channel_time:
            channel_time = domain.parse_timestamp(payload, "channel_time")
        else:
            channel_time = now_iso()
        error = payload.get("error")
        return self.repository.record_receipt(
            item_id, notice_id, channel, zone_id, status, channel_time, error, actor, role
        )

    def list_notifications(self, item_id):
        item = self.repository.get_item(item_id)
        basis = rules.current_basis(item)
        notices = self.repository.list_notifications(item_id)
        for notice in notices:
            notice["stale"] = (notice["basis"] != basis)
        return {"notifications": notices, "current_basis": basis}

    def resume_notifications(self, item_id=None, actor="channel", role="system"):
        pending = self.repository.list_pending_deliveries(item_id)
        sent = []
        deferred = []
        for delivery in pending:
            channel = delivery["channel"]
            if not self.gateway.is_available(channel):
                deferred.append({
                    "notice_id": delivery["notice_id"], "channel": channel,
                    "zone_id": delivery["zone_id"], "reason": "channel_unavailable",
                })
                continue
            result = self.gateway.send(channel, None, delivery["zone_id"])
            if result["status"] == "success":
                self.repository.record_receipt(
                    delivery["item_id"], delivery["notice_id"], channel, delivery["zone_id"],
                    "success", result["channel_time"], None, actor, role,
                )
                sent.append({
                    "notice_id": delivery["notice_id"], "channel": channel,
                    "zone_id": delivery["zone_id"],
                })
            else:
                deferred.append({
                    "notice_id": delivery["notice_id"], "channel": channel,
                    "zone_id": delivery["zone_id"], "reason": result["error"],
                })
        return {"sent": sent, "deferred": deferred, "pending_total": len(pending)}

    def channel_status(self):
        return self.gateway.status()

    def set_channel_availability(self, channel, available):
        self.gateway.set_available(channel, available)
        return self.gateway.status()
