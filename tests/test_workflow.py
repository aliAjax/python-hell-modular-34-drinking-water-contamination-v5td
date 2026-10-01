import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_complete_water_response_workflow(self):
        item = self.service.create_item({
            "source_id": "SRC-1",
            "contaminant": "nitrate",
            "detected_at": "2026-09-27T06:00:00+00:00",
            "concentration": 20,
            "limit": 10,
            "zone_ids": ["Z-1", "Z-2"],
            "population": 5000,
            "complaints": 4,
        }, "analyst-1", "analyst")
        item = self.service.act(item["id"], "verify", {"sample_count": 2}, "analyst-1", "analyst", item["version"])
        self.assertEqual(item["payload"]["assessment"]["level"], "high")
        item = self.service.act(item["id"], "advise", {"notice_id": "N-1", "kind": "boil", "message": "煮沸"}, "disp-1", "dispatcher", item["version"])
        item = self.service.act(item["id"], "switch_source", {"alternate_source_id": "ALT-1"}, "coord-1", "coordinator", item["version"])
        item = self.service.act(item["id"], "flush", {"zone_id": "Z-1"}, "field-1", "field_operator", item["version"])
        item = self.service.act(item["id"], "disinfect", {"zone_id": "Z-1", "completed": True}, "field-1", "field_operator", item["version"])
        item = self.service.act(item["id"], "sample", {"sample_id": "S-1", "zone_id": "Z-1", "concentration": 2}, "lab-1", "lab", item["version"])

        # 水源切换后原有通知依据作废，需按新水源重新下发通知
        item = self.service.act(item["id"], "advise", {"notice_id": "N-2", "kind": "boil", "message": "煮沸", "zone_ids": ["Z-1", "Z-2"]}, "disp-1", "dispatcher", item["version"])

        # 回执未齐：恢复请求被拦住并列出缺口
        from src.domain import DomainError
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "restore", {"all_zones_cleared": True}, "coord-1", "coordinator", item["version"])
        self.assertEqual(context.exception.code, "notification_gaps")
        gap_zones = {gap["zone_id"] for gap in context.exception.details["gaps"]}
        self.assertEqual(gap_zones, {"Z-1", "Z-2"})

        # 逐片区、逐渠道登记成功回执
        for zone in ("Z-1", "Z-2"):
            for channel in ("sms", "broadcast"):
                self.service.record_receipt(item["id"], "N-2", {"channel": channel, "zone_id": zone, "status": "success"}, "channel", "system")

        item = self.service.act(item["id"], "restore", {"all_zones_cleared": True}, "coord-1", "coordinator", item["version"])
        self.assertEqual(item["status"], "restored")
        self.assertGreaterEqual(len(item["audit"]), 8)


if __name__ == "__main__":
    unittest.main()
