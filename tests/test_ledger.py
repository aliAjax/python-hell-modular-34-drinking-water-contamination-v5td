import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import DomainError, ConflictError
from src.rules import apply_receipt


class ReceiptRuleTest(unittest.TestCase):
    def test_receipt_advances_in_channel_time_order(self):
        # pending -> failed
        status, t, attempts, changed = apply_receipt("pending", None, 0, "failed", "2026-09-27T08:00:00+00:00")
        self.assertEqual((status, attempts, changed), ("failed", 1, True))
        # failed -> success（渠道真实时间更晚）
        status, t, attempts, changed = apply_receipt("failed", "2026-09-27T08:00:00+00:00", 1, "success", "2026-09-27T09:00:00+00:00")
        self.assertEqual((status, attempts, changed), ("success", 2, True))
        # success 终态，重复回执只记一次
        status, t, attempts, changed = apply_receipt("success", "2026-09-27T09:00:00+00:00", 2, "failed", "2026-09-27T10:00:00+00:00")
        self.assertEqual((status, attempts, changed), ("success", 2, False))
        # 乱序（更早的渠道时间）不能回滚状态
        status, t, attempts, changed = apply_receipt("failed", "2026-09-27T09:00:00+00:00", 1, "success", "2026-09-27T08:00:00+00:00")
        self.assertEqual((status, changed), ("failed", False))


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _verified_item(self, zones=("Z-1", "Z-2"), source="SRC-1"):
        item = self.service.create_item({
            "source_id": source, "contaminant": "nitrate",
            "detected_at": "2026-09-27T06:00:00+00:00", "concentration": 20, "limit": 10,
            "zone_ids": list(zones), "population": 5000, "complaints": 4,
        }, "analyst-1", "analyst")
        item = self.service.act(item["id"], "verify", {"sample_count": 2}, "analyst-1", "analyst", item["version"])
        return item

    def _issue(self, item, notice_id="N-1", channels=("sms", "broadcast"), zones=None):
        payload = {"notice_id": notice_id, "kind": "boil", "message": "煮沸", "channels": list(channels)}
        if zones is not None:
            payload["zone_ids"] = list(zones)
        return self.service.act(item["id"], "advise", payload, "disp-1", "dispatcher", item["version"])

    def test_duplicate_receipt_recorded_once(self):
        item = self._verified_item()
        item = self._issue(item)
        self.service.record_receipt(item["id"], "N-1", {"channel": "sms", "zone_id": "Z-1", "status": "success"}, "channel", "system")
        # 渠道重复回调同一回执
        self.service.record_receipt(item["id"], "N-1", {"channel": "sms", "zone_id": "Z-1", "status": "success"}, "channel", "system")
        notices = self.service.list_notifications(item["id"])["notifications"]
        deliveries = [r for r in notices[0]["receipts"] if r["channel"] == "sms" and r["zone_id"] == "Z-1"]
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0]["status"], "success")
        self.assertEqual(deliveries[0]["attempts"], 1)

    def test_failed_receipt_blocks_restore_and_lists_gap(self):
        item = self._verified_item()
        item = self._issue(item, channels=("sms",))
        self.service.record_receipt(item["id"], "N-1", {"channel": "sms", "zone_id": "Z-1", "status": "success"}, "channel", "system")
        self.service.record_receipt(item["id"], "N-1", {"channel": "sms", "zone_id": "Z-2", "status": "failed", "error": "send_failed"}, "channel", "system")
        item = self.service.act(item["id"], "flush", {"zone_id": "Z-1"}, "field-1", "field_operator", item["version"])
        item = self.service.act(item["id"], "disinfect", {"zone_id": "Z-1", "completed": True}, "field-1", "field_operator", item["version"])
        item = self.service.act(item["id"], "sample", {"sample_id": "S-1", "zone_id": "Z-1", "concentration": 2}, "lab-1", "lab", item["version"])
        with self.assertRaises(DomainError) as ctx:
            self.service.act(item["id"], "restore", {"all_zones_cleared": True}, "coord-1", "coordinator", item["version"])
        self.assertEqual(ctx.exception.code, "notification_gaps")
        self.assertEqual(ctx.exception.details["gaps"], [{"zone_id": "Z-2", "missing": ["sms"]}])
        # 补齐失败片区后恢复成功
        self.service.record_receipt(item["id"], "N-1", {"channel": "sms", "zone_id": "Z-2", "status": "success"}, "channel", "system")
        item = self.service.act(item["id"], "restore", {"all_zones_cleared": True}, "coord-1", "coordinator", item["version"])
        self.assertEqual(item["status"], "restored")

    def test_source_switch_invalidates_notice_basis(self):
        item = self._verified_item()
        item = self._issue(item, notice_id="N-1")
        item = self.service.act(item["id"], "switch_source", {"alternate_source_id": "ALT-1"}, "coord-1", "coordinator", item["version"])
        item = self.service.act(item["id"], "flush", {"zone_id": "Z-1"}, "field-1", "field_operator", item["version"])
        item = self.service.act(item["id"], "disinfect", {"zone_id": "Z-1", "completed": True}, "field-1", "field_operator", item["version"])
        item = self.service.act(item["id"], "sample", {"sample_id": "S-1", "zone_id": "Z-1", "concentration": 2}, "lab-1", "lab", item["version"])
        # 未按新水源重新核对/下发通知，恢复被拦并列出过期通知
        with self.assertRaises(DomainError) as ctx:
            self.service.act(item["id"], "restore", {"all_zones_cleared": True}, "coord-1", "coordinator", item["version"])
        self.assertEqual(ctx.exception.code, "notification_gaps")
        stale = {n["notice_id"] for n in ctx.exception.details["stale_notices"]}
        self.assertEqual(stale, {"N-1"})
        # 按新水源重新下发并取得回执
        item = self._issue(item, notice_id="N-2")
        for zone in ("Z-1", "Z-2"):
            for channel in ("sms", "broadcast"):
                self.service.record_receipt(item["id"], "N-2", {"channel": channel, "zone_id": zone, "status": "success"}, "channel", "system")
        item = self.service.act(item["id"], "restore", {"all_zones_cleared": True}, "coord-1", "coordinator", item["version"])
        self.assertEqual(item["status"], "restored")

    def test_switch_with_new_zones_requires_cover_of_new_zones(self):
        item = self._verified_item(zones=("Z-1", "Z-2"))
        item = self._issue(item, notice_id="N-1")
        # 切换水源且受影响片区变为 Z-3
        item = self.service.act(item["id"], "switch_source",
                               {"alternate_source_id": "ALT-1", "zone_ids": ["Z-3"]},
                               "coord-1", "coordinator", item["version"])
        item = self.service.act(item["id"], "flush", {"zone_id": "Z-3"}, "field-1", "field_operator", item["version"])
        item = self.service.act(item["id"], "disinfect", {"zone_id": "Z-3", "completed": True}, "field-1", "field_operator", item["version"])
        item = self.service.act(item["id"], "sample", {"sample_id": "S-3", "zone_id": "Z-3", "concentration": 2}, "lab-1", "lab", item["version"])
        # 旧通知依据作废，且不覆盖新片区 Z-3
        with self.assertRaises(DomainError) as ctx:
            self.service.act(item["id"], "restore", {"all_zones_cleared": True}, "coord-1", "coordinator", item["version"])
        self.assertEqual(ctx.exception.code, "notification_gaps")
        gap_zones = {g["zone_id"] for g in ctx.exception.details["gaps"]}
        self.assertEqual(gap_zones, {"Z-3"})
        self.assertEqual({n["notice_id"] for n in ctx.exception.details["stale_notices"]}, {"N-1"})
        # 按新依据、新片区重新下发并取得回执
        item = self._issue(item, notice_id="N-2", zones=("Z-3",))
        for channel in ("sms", "broadcast"):
            self.service.record_receipt(item["id"], "N-2", {"channel": channel, "zone_id": "Z-3", "status": "success"}, "channel", "system")
        item = self.service.act(item["id"], "restore", {"all_zones_cleared": True}, "coord-1", "coordinator", item["version"])
        self.assertEqual(item["status"], "restored")

    def test_concurrent_notification_only_one_wins(self):
        item = self._verified_item()
        version = item["version"]
        item = self.service.act(item["id"], "advise", {"notice_id": "N-1", "kind": "boil", "message": "煮沸"}, "disp-1", "dispatcher", version)
        with self.assertRaises(ConflictError) as ctx:
            self.service.act(item["id"], "advise", {"notice_id": "N-1", "kind": "boil", "message": "煮沸"}, "disp-2", "dispatcher", version)
        self.assertEqual(ctx.exception.code, "version_conflict")
        self.assertEqual(ctx.exception.details["current_version"], item["version"])
        self.assertTrue(ctx.exception.details["conflict_id"].startswith("CF-"))

    def test_concurrent_restore_only_one_wins(self):
        item = self._verified_item()
        item = self._issue(item)
        item = self.service.act(item["id"], "flush", {"zone_id": "Z-1"}, "field-1", "field_operator", item["version"])
        item = self.service.act(item["id"], "disinfect", {"zone_id": "Z-1", "completed": True}, "field-1", "field_operator", item["version"])
        item = self.service.act(item["id"], "sample", {"sample_id": "S-1", "zone_id": "Z-1", "concentration": 2}, "lab-1", "lab", item["version"])
        for zone in ("Z-1", "Z-2"):
            for channel in ("sms", "broadcast"):
                self.service.record_receipt(item["id"], "N-1", {"channel": channel, "zone_id": zone, "status": "success"}, "channel", "system")
        version = item["version"]
        item = self.service.act(item["id"], "restore", {"all_zones_cleared": True}, "coord-1", "coordinator", version)
        with self.assertRaises(ConflictError) as ctx:
            self.service.act(item["id"], "restore", {"all_zones_cleared": True}, "coord-2", "coordinator", version)
        self.assertEqual(ctx.exception.code, "version_conflict")
        self.assertEqual(ctx.exception.details["current_version"], item["version"])
        self.assertTrue(ctx.exception.details["conflict_id"].startswith("CF-"))

    def test_channel_down_keeps_confirmed_receipts_and_resumes_after_restart(self):
        item = self._verified_item()
        self.service.set_channel_availability("sms", False)
        item = self._issue(item, channels=("sms", "broadcast"))
        # 渠道不可用：短信全部 pending，应急广播成功
        result = self.service.resume_notifications(item["id"])
        self.assertEqual(len(result["sent"]), 2)
        self.assertEqual({d["channel"] for d in result["sent"]}, {"broadcast"})
        self.assertEqual(len(result["deferred"]), 2)
        # 重启：基于同一数据库文件重新装配服务
        restarted = Service(Repository(self.tmp.name))
        notices = restarted.list_notifications(item["id"])["notifications"]
        deliveries = notices[0]["receipts"]
        self.assertEqual(len(deliveries), 4)  # 已确认回执保留，未重复发送
        sms = [d for d in deliveries if d["channel"] == "sms"]
        self.assertTrue(all(d["status"] == "pending" for d in sms))
        self.assertTrue(all(d["status"] == "success" for d in deliveries if d["channel"] == "broadcast"))
        # 渠道恢复后接着处理未完成通知
        restarted.set_channel_availability("sms", True)
        result = restarted.resume_notifications(item["id"])
        self.assertEqual(len(result["sent"]), 2)
        self.assertEqual(len(result["deferred"]), 0)
        notices = restarted.list_notifications(item["id"])["notifications"]
        self.assertTrue(all(r["status"] == "success" for r in notices[0]["receipts"]))
        self.assertEqual(len(notices[0]["receipts"]), 4)


if __name__ == "__main__":
    unittest.main()
