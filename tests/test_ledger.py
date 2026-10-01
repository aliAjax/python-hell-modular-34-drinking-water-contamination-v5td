import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.channels import InMemoryChannelGateway
from src.domain import ConflictError, DomainError

T0 = "2026-09-30T08:00:00+00:00"


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.gateway = InMemoryChannelGateway()
        self.service = Service(self.repo, self.gateway)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def make_item(self, zones=("Z-1", "Z-2")):
        item = self.service.create_item({
            "source_id": "SRC-L",
            "contaminant": "nitrate",
            "detected_at": "2026-09-30T06:00:00+00:00",
            "concentration": 20,
            "limit": 10,
            "zone_ids": list(zones),
            "population": 3000,
        }, "analyst-1", "analyst")
        item = self.service.act(item["id"], "verify", {"sample_count": 1}, "analyst-1", "analyst", item["version"])
        item = self.service.act(item["id"], "advise", {"notice_id": "ADV-1", "kind": "boil", "message": "煮沸"}, "disp-1", "dispatcher", item["version"])
        return item

    def dispatch(self, item, notice_id="N-1", **extra):
        payload = {
            "notice_id": notice_id,
            "kind": "boil",
            "message": "停水通知",
            "expected_version": item["version"],
        }
        payload.update(extra)
        return self.service.dispatch_notice(item["id"], payload, "disp-1", "dispatcher")

    def receipt(self, item_id, zone, channel, result="success", reported_at=T0, key=None, notice="N-1"):
        payload = {
            "notice_id": notice,
            "zone_id": zone,
            "channel": channel,
            "result": result,
            "reported_at": reported_at,
        }
        if key:
            payload["receipt_key"] = key
        return self.service.record_receipt(item_id, payload, "chan-1", "channel_agent")

    def confirm_all(self, item_id, zones=("Z-1", "Z-2"), notice="N-1"):
        for zone in zones:
            for channel in ("sms", "broadcast"):
                self.receipt(item_id, zone, channel, notice=notice)

    def walk_to_sampled(self, item):
        item = self.service.act(item["id"], "flush", {"zone_id": item["payload"]["zone_ids"][0]}, "field-1", "field_operator", item["version"])
        item = self.service.act(item["id"], "disinfect", {"zone_id": item["payload"]["zone_ids"][0], "completed": True}, "field-1", "field_operator", item["version"])
        item = self.service.act(item["id"], "sample", {"sample_id": "S-1", "zone_id": item["payload"]["zone_ids"][0], "concentration": 2}, "lab-1", "lab", item["version"])
        return item

    def test_dispatch_fans_out_and_duplicate_notice_conflicts(self):
        item = self.make_item()
        result = self.dispatch(item)
        self.assertEqual(len(result["deliveries"]), 4)
        self.assertTrue(all(d["status"] == "sent" for d in result["deliveries"]))
        self.assertEqual(len(self.gateway.attempts), 4)
        self.assertEqual(len({a["idempotency_key"] for a in self.gateway.attempts}), 4)

        item = self.service.get_item(item["id"])
        with self.assertRaises(ConflictError) as context:
            self.dispatch(item)  # 同一通知编号再下发
        self.assertEqual(context.exception.code, "duplicate_notice")
        details = context.exception.details
        self.assertEqual(details["latest_version"], item["version"])
        self.assertTrue(details["conflict_id"].startswith("CFL-"))
        audit = self.repo.audit_trail(item["id"])
        conflicts = [e for e in audit if e["event_type"] == "conflict_rejected"]
        self.assertEqual(conflicts[-1]["payload"]["conflict_id"], details["conflict_id"])

    def test_receipt_dedup_and_channel_time_order(self):
        item = self.make_item()
        self.dispatch(item)

        first = self.receipt(item["id"], "Z-1", "sms", reported_at="2026-09-30T08:00:00+00:00", key="R1")
        self.assertTrue(first["recorded"])
        self.assertTrue(first["applied"])
        self.assertEqual(first["delivery"]["status"], "confirmed")

        again = self.receipt(item["id"], "Z-1", "sms", reported_at="2026-09-30T08:00:00+00:00", key="R1")
        self.assertFalse(again["recorded"])
        self.assertEqual(again["reason"], "duplicate")
        self.assertEqual(len(self.repo.list_receipts(item["id"])), 1)

        stale = self.receipt(item["id"], "Z-1", "sms", result="failed", reported_at="2026-09-30T07:00:00+00:00", key="R2")
        self.assertTrue(stale["recorded"])
        self.assertFalse(stale["applied"])
        self.assertEqual(stale["reason"], "stale")
        self.assertEqual(stale["delivery"]["status"], "confirmed")

        later_fail = self.receipt(item["id"], "Z-1", "sms", result="failed", reported_at="2026-09-30T09:00:00+00:00", key="R3")
        self.assertTrue(later_fail["applied"])
        self.assertEqual(later_fail["delivery"]["status"], "failed")

        later_ok = self.receipt(item["id"], "Z-1", "sms", reported_at="2026-09-30T10:00:00+00:00", key="R4")
        self.assertTrue(later_ok["applied"])
        self.assertEqual(later_ok["delivery"]["status"], "confirmed")
        self.assertEqual(later_ok["delivery"]["confirmed_at"], "2026-09-30T10:00:00+00:00")

        # 渠道未给回执号时按内容合成键，重复回执同样只记一次
        synth1 = self.receipt(item["id"], "Z-2", "sms", reported_at="2026-09-30T08:30:00+00:00")
        synth2 = self.receipt(item["id"], "Z-2", "sms", reported_at="2026-09-30T08:30:00+00:00")
        self.assertTrue(synth1["recorded"])
        self.assertFalse(synth2["recorded"])

    def test_failed_send_retried_without_resending(self):
        item = self.make_item()
        blocked_key = "%s:N-1:Z-2:sms" % item["id"]
        self.gateway.fail_keys.add(blocked_key)
        result = self.dispatch(item)
        self.assertFalse(result["dispatch"]["channel_available"])
        self.assertEqual(result["dispatch"]["pending_retry"], [blocked_key])
        self.assertEqual(len(result["dispatch"]["sent"]), 3)

        self.gateway.fail_keys.clear()
        summary = self.service.resume_pending("disp-1", "dispatcher", item_id=item["id"])
        self.assertEqual(summary["sent"], [blocked_key])
        self.assertEqual(summary["remaining"], 0)
        # 已发出的三条没有重发
        keys = [a["idempotency_key"] for a in self.gateway.attempts]
        self.assertEqual(len(keys), len(set(keys)))
        for delivery in self.repo.list_deliveries(item["id"]):
            self.assertEqual(delivery["status"], "sent")

    def test_restore_blocked_until_all_zones_confirmed(self):
        item = self.make_item()
        self.dispatch(item)
        self.confirm_all(item["id"], zones=("Z-1",))
        item = self.walk_to_sampled(self.service.get_item(item["id"]))
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "restore", {"all_zones_cleared": True}, "coord-1", "coordinator", item["version"])
        self.assertEqual(context.exception.code, "receipt_gap")
        self.assertEqual(context.exception.status, 409)
        gaps = context.exception.details["gaps"]
        self.assertEqual({(g["zone_id"], g["channel"]) for g in gaps}, {("Z-2", "sms"), ("Z-2", "broadcast")})

        self.confirm_all(item["id"], zones=("Z-2",))
        item = self.service.get_item(item["id"])
        item = self.service.act(item["id"], "restore", {"all_zones_cleared": True}, "coord-1", "coordinator", item["version"])
        self.assertEqual(item["status"], "restored")

    def test_switch_source_invalidates_notice_basis(self):
        item = self.make_item()
        self.dispatch(item)
        self.confirm_all(item["id"], zones=("Z-1",))
        item = self.service.get_item(item["id"])
        item = self.service.act(
            item["id"], "switch_source",
            {"alternate_source_id": "ALT-1", "zone_ids": ["Z-1", "Z-3"]},
            "coord-1", "coordinator", item["version"],
        )
        self.assertEqual(item["payload"]["notice_basis"], 2)
        notices = self.repo.list_notices(item["id"])
        self.assertEqual(notices[0]["status"], "superseded")
        deliveries = self.repo.list_deliveries(item["id"])
        by_zone = {}
        for d in deliveries:
            by_zone.setdefault(d["zone_id"], set()).add(d["status"])
        self.assertEqual(by_zone["Z-1"], {"confirmed"})  # 已确认的回执是历史事实，保留
        self.assertEqual(by_zone["Z-2"], {"void"})  # 未完成的投递随依据作废

        item = self.walk_to_sampled(item)
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "restore", {"all_zones_cleared": True}, "coord-1", "coordinator", item["version"])
        self.assertEqual(context.exception.code, "receipt_gap")
        self.assertEqual(context.exception.details["notice_basis"], 2)
        self.assertEqual({g["zone_id"] for g in context.exception.details["gaps"]}, {"Z-1", "Z-3"})

        # 作废投递不参与续传
        before = len(self.gateway.attempts)
        self.service.resume_pending("disp-1", "dispatcher", item_id=item["id"])
        self.assertEqual(len(self.gateway.attempts), before)

        item = self.service.get_item(item["id"])
        self.dispatch(item, notice_id="N-2")
        self.confirm_all(item["id"], zones=("Z-1", "Z-3"), notice="N-2")
        item = self.service.get_item(item["id"])
        item = self.service.act(item["id"], "restore", {"all_zones_cleared": True}, "coord-1", "coordinator", item["version"])
        self.assertEqual(item["status"], "restored")

    def test_concurrent_submissions_single_winner(self):
        # 两名值班员同时下发不同编号的通知：版本只认一方
        item = self.make_item()
        stale_version = item["version"]
        self.dispatch(item, notice_id="N-1")
        with self.assertRaises(ConflictError) as context:
            self.dispatch({"id": item["id"], "version": stale_version}, notice_id="N-9")
        self.assertEqual(context.exception.code, "version_conflict")
        self.assertEqual(context.exception.details["latest_version"], stale_version + 1)
        self.assertIn("conflict_id", context.exception.details)

        # 两名值班员同时提交恢复：只落一方
        self.confirm_all(item["id"])
        item = self.walk_to_sampled(self.service.get_item(item["id"]))
        version = item["version"]
        restored = self.service.act(item["id"], "restore", {"all_zones_cleared": True}, "coord-1", "coordinator", version)
        self.assertEqual(restored["status"], "restored")
        with self.assertRaises(ConflictError) as context:
            self.service.act(item["id"], "restore", {"all_zones_cleared": True}, "coord-2", "coordinator", version)
        self.assertEqual(context.exception.code, "version_conflict")
        self.assertEqual(context.exception.details["latest_version"], version + 1)
        self.assertIn("conflict_id", context.exception.details)

    def test_channel_outage_keeps_receipts_and_restart_resumes(self):
        item = self.make_item()

        # 渠道宕机时下发：全部留成待重试，不丢单
        self.gateway.set_available(False)
        result = self.dispatch(item)
        self.assertFalse(result["dispatch"]["channel_available"])
        self.assertEqual(len(result["dispatch"]["pending_retry"]), 4)
        self.assertTrue(all(d["status"] == "pending" for d in result["deliveries"]))

        # 宕机期间 Z-1 经电话确认后手工登记回执：已确认回执保留在账上
        self.confirm_all(item["id"], zones=("Z-1",))
        summary = self.service.resume_pending("disp-1", "dispatcher", item_id=item["id"])
        self.assertFalse(summary["channel_available"])
        self.assertEqual(summary["remaining"], 2)
        deliveries = {d["zone_id"]: d for d in self.repo.list_deliveries(item["id"]) if d["channel"] == "sms"}
        self.assertEqual(deliveries["Z-1"]["status"], "confirmed")
        self.assertEqual(deliveries["Z-2"]["status"], "pending")

        item = self.walk_to_sampled(self.service.get_item(item["id"]))
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "restore", {"all_zones_cleared": True}, "coord-1", "coordinator", item["version"])
        self.assertEqual(context.exception.code, "receipt_gap")

        # 重启：新服务实例接着处理未完成通知，已确认的不再重发
        gateway2 = InMemoryChannelGateway()
        service2 = Service(self.repo, gateway2)
        summary = service2.resume_pending("disp-1", "dispatcher", item_id=item["id"])
        self.assertEqual(summary["remaining"], 0)
        self.assertEqual(len(gateway2.attempts), 2)
        self.assertTrue(all("Z-2" in a["idempotency_key"] for a in gateway2.attempts))

        for channel in ("sms", "broadcast"):
            service2.record_receipt(item["id"], {
                "notice_id": "N-1", "zone_id": "Z-2", "channel": channel,
                "result": "success", "reported_at": "2026-09-30T11:00:00+00:00",
            }, "chan-1", "channel_agent")
        item = service2.get_item(item["id"])
        item = service2.act(item["id"], "restore", {"all_zones_cleared": True}, "coord-1", "coordinator", item["version"])
        self.assertEqual(item["status"], "restored")


if __name__ == "__main__":
    unittest.main()
