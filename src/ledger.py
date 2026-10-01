import hashlib
from datetime import datetime, timezone

from .domain import DomainError

# 渠道与回执取值
CHANNELS = {"sms", "broadcast"}
RECEIPT_RESULTS = {"success", "failed"}

# 投递状态机：pending -> sent -> confirmed；failed 回到待重试；void 为作废（依据失效）
DELIVERY_PENDING = "pending"
DELIVERY_SENT = "sent"
DELIVERY_CONFIRMED = "confirmed"
DELIVERY_FAILED = "failed"
DELIVERY_VOID = "void"
RETRYABLE_STATUSES = {DELIVERY_PENDING, DELIVERY_FAILED}

DISPATCH_ROLES = {"dispatcher", "coordinator"}
RECEIPT_ROLES = {"dispatcher", "coordinator", "channel_agent"}
RESUME_ROLES = {"dispatcher", "coordinator"}
DISPATCH_STATUSES = {"verified", "advisory", "switched", "flushing", "disinfected", "sampled"}


def _text(payload, name):
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise DomainError("field_required", "%s 不能为空" % name)
    return value.strip()


def parse_channel_time(value, field="reported_at"):
    """把渠道上报时间规范成 UTC ISO 字符串，保证字符串可比较。"""
    if not isinstance(value, str) or not value.strip():
        raise DomainError("field_required", "%s 不能为空" % field)
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise DomainError("invalid_timestamp", "%s 必须是 ISO 时间" % field)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat()


def normalize_notice(payload, item):
    notice_id = _text(payload, "notice_id")
    kind = _text(payload, "kind")
    message = _text(payload, "message")
    channels = payload.get("channels") or ["sms", "broadcast"]
    if not isinstance(channels, list) or not channels:
        raise DomainError("invalid_channels", "channels 必须是非空列表")
    if any(channel not in CHANNELS for channel in channels):
        raise DomainError("invalid_channels", "渠道只支持 sms/broadcast")
    channels = list(dict.fromkeys(channels))
    item_zones = list(item["payload"].get("zone_ids", []))
    zones = payload.get("zones") or item_zones
    if not isinstance(zones, list) or not zones:
        raise DomainError("zones_required", "至少需要一个受影响区域")
    if any(not isinstance(zone, str) or not zone.strip() for zone in zones):
        raise DomainError("invalid_zones", "区域编号必须是字符串列表")
    zones = [zone.strip() for zone in zones]
    unknown = sorted(set(zones) - set(item_zones))
    if unknown:
        raise DomainError("invalid_zones", "通知片区超出事件受影响范围: %s" % ",".join(unknown))
    basis = int(item["payload"].get("notice_basis", 1))
    return {
        "notice_id": notice_id,
        "kind": kind,
        "message": message,
        "channels": channels,
        "zones": zones,
        "basis": basis,
    }


def plan_deliveries(item_id, notice):
    """一条通知展开成 片区×渠道 的投递行，每行独立推进、独立重试。"""
    return [
        {
            "item_id": item_id,
            "notice_id": notice["notice_id"],
            "basis": notice["basis"],
            "zone_id": zone,
            "channel": channel,
        }
        for zone in notice["zones"]
        for channel in notice["channels"]
    ]


def normalize_receipt(payload):
    notice_id = _text(payload, "notice_id")
    zone_id = _text(payload, "zone_id")
    channel = _text(payload, "channel")
    if channel not in CHANNELS:
        raise DomainError("invalid_channel", "渠道只支持 sms/broadcast")
    result = _text(payload, "result")
    if result not in RECEIPT_RESULTS:
        raise DomainError("invalid_result", "result 必须是 success/failed")
    reported_at = parse_channel_time(payload.get("reported_at"))
    receipt_key = payload.get("receipt_key")
    if not isinstance(receipt_key, str) or not receipt_key.strip():
        # 渠道没给回执号时按内容合成，同一通知编号的重复回执只记一次
        receipt_key = hashlib.sha256(
            "|".join([notice_id, zone_id, channel, result, reported_at]).encode("utf-8")
        ).hexdigest()[:24]
    return {
        "notice_id": notice_id,
        "zone_id": zone_id,
        "channel": channel,
        "result": result,
        "reported_at": reported_at,
        "receipt_key": receipt_key.strip(),
    }


def restore_gaps(item, notices, deliveries):
    """恢复前对账：当前依据版本下，每个片区每个渠道都要有成功回执。

    返回 (basis, gaps)；gaps 为空才允许恢复。
    """
    basis = int(item["payload"].get("notice_basis", 1))
    zones = list(item["payload"].get("zone_ids", []))
    active = [n for n in notices if n["status"] == "active" and int(n["basis"]) == basis]
    gaps = []
    if not active:
        for zone in zones:
            gaps.append({"zone_id": zone, "missing": "notice", "notice_basis": basis})
        return basis, gaps
    notice = active[-1]
    covered = set(notice["zones"])
    for zone in zones:
        if zone not in covered:
            gaps.append({"zone_id": zone, "missing": "coverage", "notice_id": notice["notice_id"]})
    status_by_pair = {}
    for delivery in deliveries:
        if delivery["notice_id"] == notice["notice_id"]:
            status_by_pair[(delivery["zone_id"], delivery["channel"])] = delivery["status"]
    for zone in zones:
        if zone not in covered:
            continue
        for channel in notice["channels"]:
            status = status_by_pair.get((zone, channel))
            if status != DELIVERY_CONFIRMED:
                gaps.append({
                    "zone_id": zone,
                    "channel": channel,
                    "status": status or "missing",
                    "notice_id": notice["notice_id"],
                })
    return basis, gaps
