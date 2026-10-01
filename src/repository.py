import json
import sqlite3
from datetime import datetime, timezone

from .audit import audit_hash, canonical_json
from .domain import ConflictError, NotFoundError, DomainError
from . import rules


def now_iso():
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, path):
        self.path = path

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def initialize(self):
        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type TEXT NOT NULL,
                    stable_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_role TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(entity_type, stable_key)
                );
                CREATE TABLE IF NOT EXISTS sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    source_type TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, source_type, external_id),
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );
                CREATE TABLE IF NOT EXISTS actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    role TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER,
                    event_type TEXT NOT NULL,
                    actor TEXT,
                    role TEXT,
                    payload TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    notice_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    message TEXT NOT NULL,
                    channels TEXT NOT NULL,
                    target_zones TEXT NOT NULL,
                    basis TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'issued',
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(item_id, notice_id),
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );
                CREATE TABLE IF NOT EXISTS notification_receipts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    notification_id INTEGER NOT NULL,
                    item_id INTEGER NOT NULL,
                    notice_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    zone_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    channel_time TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(notification_id, channel, zone_id),
                    FOREIGN KEY(notification_id) REFERENCES notifications(id)
                );
                CREATE TABLE IF NOT EXISTS channel_state (
                    channel TEXT PRIMARY KEY,
                    available INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL
                );
                """
            )
        finally:
            conn.close()

    def _row_to_item(self, row):
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result

    def _last_hash(self, conn, item_id):
        row = conn.execute(
            "SELECT event_hash FROM audit_events WHERE item_id IS ? ORDER BY id DESC LIMIT 1",
            (item_id,),
        ).fetchone()
        return row["event_hash"] if row else "GENESIS"

    def append_audit(self, conn, item_id, event_type, actor, role, payload):
        previous = self._last_hash(conn, item_id)
        event = {
            "item_id": item_id,
            "event_type": event_type,
            "actor": actor,
            "role": role,
            "payload": payload,
            "created_at": now_iso(),
        }
        event_hash = audit_hash(previous, event)
        conn.execute(
            "INSERT INTO audit_events(item_id,event_type,actor,role,payload,previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (item_id, event_type, actor, role, canonical_json(payload), previous, event_hash, event["created_at"]),
        )

    def create_item(self, entity_type, stable_key, initial_status, payload, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO items(entity_type,stable_key,status,version,payload,created_by,created_role,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        entity_type,
                        stable_key,
                        initial_status,
                        1,
                        canonical_json(payload),
                        actor,
                        role,
                        now_iso(),
                        now_iso(),
                    ),
                )
            except sqlite3.IntegrityError:
                raise ConflictError("duplicate_item", "同一业务实体已经存在")
            item_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            self.append_audit(conn, item_id, "created", actor, role, {"stable_key": stable_key})
            conn.execute("COMMIT")
            return self.get_item(item_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def get_item(self, item_id):
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            return self._row_to_item(row)
        finally:
            conn.close()

    def list_items(self, status=None):
        conn = self.connect()
        try:
            if status:
                rows = conn.execute("SELECT * FROM items WHERE status=? ORDER BY id DESC", (status,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM items ORDER BY id DESC").fetchall()
            return [self._row_to_item(row) for row in rows]
        finally:
            conn.close()

    def add_source(self, item_id, source_type, external_id, payload, observed_at, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            item = conn.execute("SELECT id FROM items WHERE id=?", (item_id,)).fetchone()
            if item is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            try:
                conn.execute(
                    "INSERT INTO sources(item_id,source_type,external_id,payload,observed_at,created_at) VALUES(?,?,?,?,?,?)",
                    (item_id, source_type, external_id, canonical_json(payload), observed_at, now_iso()),
                )
            except sqlite3.IntegrityError:
                raise ConflictError("duplicate_source", "同一来源记录已经提交")
            source_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            self.append_audit(
                conn,
                item_id,
                "source_recorded",
                actor,
                role,
                {"source_id": source_id, "source_type": source_type, "external_id": external_id},
            )
            conn.execute("COMMIT")
            return {"id": source_id, "item_id": item_id, "source_type": source_type, "external_id": external_id, "payload": payload, "observed_at": observed_at}
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def list_sources(self, item_id):
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM sources WHERE item_id=? ORDER BY id DESC", (item_id,)).fetchall()
            result = []
            for row in rows:
                value = dict(row)
                value["payload"] = json.loads(value["payload"])
                result.append(value)
            return result
        finally:
            conn.close()

    def apply_action(self, item_id, action, actor, role, new_status, new_payload, event_payload, expected_version=None):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            if expected_version is not None and int(expected_version) != int(row["version"]):
                raise ConflictError("version_conflict", "记录已被其他操作更新，请重新读取")
            version = int(row["version"]) + 1
            conn.execute(
                "UPDATE items SET status=?,version=?,payload=?,updated_at=? WHERE id=?",
                (new_status, version, canonical_json(new_payload), now_iso(), item_id),
            )
            conn.execute(
                "INSERT INTO actions(item_id,action,actor,role,payload,created_at) VALUES(?,?,?,?,?,?)",
                (item_id, action, actor, role, canonical_json(event_payload), now_iso()),
            )
            self.append_audit(conn, item_id, action, actor, role, event_payload)
            conn.execute("COMMIT")
            return self.get_item(item_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def conflict_details(self, item_id, expected_version):
        conn = self.connect()
        try:
            row = conn.execute("SELECT version FROM items WHERE id=?", (item_id,)).fetchone()
            last_action = conn.execute(
                "SELECT id FROM actions WHERE item_id=? ORDER BY id DESC LIMIT 1", (item_id,)
            ).fetchone()
            current_version = int(row["version"]) if row else None
            conflict_id = "CF-%d" % (last_action["id"] if last_action else 0)
            return ConflictError(
                "version_conflict",
                "记录已被其他值班员更新，请读取最新版本后再提交",
                details={
                    "current_version": current_version,
                    "expected_version": expected_version,
                    "conflict_id": conflict_id,
                },
            )
        finally:
            conn.close()

    def _version_conflict(self, conn, item_id, expected_version):
        row = conn.execute("SELECT version FROM items WHERE id=?", (item_id,)).fetchone()
        current_version = int(row["version"]) if row else None
        last_action = conn.execute(
            "SELECT id FROM actions WHERE item_id=? ORDER BY id DESC LIMIT 1", (item_id,)
        ).fetchone()
        conflict_id = "CF-%d" % (last_action["id"] if last_action else 0)
        raise ConflictError(
            "version_conflict",
            "记录已被其他值班员更新，请读取最新版本后再提交",
            details={
                "current_version": current_version,
                "expected_version": expected_version,
                "conflict_id": conflict_id,
            },
        )

    def issue_notification(self, item_id, notice_id, kind, message, channels, target_zones,
                            basis, expected_version, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            if expected_version is not None and int(expected_version) != int(row["version"]):
                self._version_conflict(conn, item_id, expected_version)
            if row["status"] not in ("verified", "advisory", "switched", "flushing", "disinfected", "sampled"):
                raise DomainError("invalid_state", "当前状态 %s 不允许下发通知" % row["status"])
            dup = conn.execute(
                "SELECT id FROM notifications WHERE item_id=? AND notice_id=?", (item_id, notice_id)
            ).fetchone()
            if dup is not None:
                raise DomainError("duplicate_notification", "同一通知编号不能重复发送", 409)

            item = self._row_to_item(row)
            item["payload"].setdefault("notifications", []).append(
                {"notice_id": notice_id, "kind": kind, "message": message}
            )
            version = int(row["version"]) + 1
            now = now_iso()
            cur = conn.execute(
                "INSERT INTO notifications(item_id,notice_id,kind,message,channels,target_zones,basis,status,version,payload,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (item_id, notice_id, kind, message, canonical_json(channels), canonical_json(target_zones),
                 basis, "delivering", 1, canonical_json({"notice_id": notice_id, "kind": kind, "message": message}), now, now),
            )
            notification_id = cur.lastrowid
            for channel in channels:
                for zone in target_zones:
                    conn.execute(
                        "INSERT INTO notification_receipts(notification_id,item_id,notice_id,channel,zone_id,status,attempts,created_at,updated_at) "
                        "VALUES(?,?,?,?,?, 'pending', 0, ?, ?)",
                        (notification_id, item_id, notice_id, channel, zone, now, now),
                    )
            new_item_status = "advisory" if row["status"] == "verified" else row["status"]
            conn.execute(
                "UPDATE items SET status=?, version=?, payload=?, updated_at=? WHERE id=?",
                (new_item_status, version, canonical_json(item["payload"]), now_iso(), item_id),
            )
            conn.execute(
                "INSERT INTO actions(item_id,action,actor,role,payload,created_at) VALUES(?,?,?,?,?,?)",
                (item_id, "advise", actor, role,
                 canonical_json({"notice_id": notice_id, "kind": kind, "channels": channels, "target_zones": target_zones}), now_iso()),
            )
            self.append_audit(conn, item_id, "notification_issued", actor, role,
                              {"notice_id": notice_id, "channels": channels, "target_zones": target_zones, "basis": basis})
            conn.execute("COMMIT")
            return self.get_item(item_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def _row_to_delivery(self, row):
        result = dict(row)
        for key in ("channels",):
            if key in result and isinstance(result[key], str):
                try:
                    result[key] = json.loads(result[key])
                except (ValueError, TypeError):
                    pass
        return result

    def _row_to_notification(self, row):
        result = dict(row)
        result["channels"] = json.loads(result["channels"])
        result["target_zones"] = json.loads(result["target_zones"])
        result["payload"] = json.loads(result["payload"])
        return result

    def _list_notifications_conn(self, conn, item_id):
        rows = conn.execute(
            "SELECT * FROM notifications WHERE item_id=? ORDER BY id", (item_id,)
        ).fetchall()
        result = []
        for row in rows:
            notice = self._row_to_notification(row)
            receipts = conn.execute(
                "SELECT * FROM notification_receipts WHERE notification_id=? ORDER BY channel, zone_id",
                (notice["id"],),
            ).fetchall()
            notice["receipts"] = [self._row_to_delivery(r) for r in receipts]
            result.append(notice)
        return result

    def list_notifications(self, item_id):
        conn = self.connect()
        try:
            return self._list_notifications_conn(conn, item_id)
        finally:
            conn.close()

    def _refresh_notification_status(self, conn, notification_id):
        rows = conn.execute(
            "SELECT status FROM notification_receipts WHERE notification_id=?", (notification_id,)
        ).fetchall()
        statuses = [row["status"] for row in rows]
        if statuses and all(status == "success" for status in statuses):
            aggregate = "delivered"
        elif any(status == "pending" for status in statuses):
            aggregate = "delivering"
        else:
            aggregate = "partially_delivered"
        conn.execute(
            "UPDATE notifications SET status=?, updated_at=? WHERE id=?",
            (aggregate, now_iso(), notification_id),
        )

    def record_receipt(self, item_id, notice_id, channel, zone_id, status, channel_time,
                       error=None, actor="channel", role="system"):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            notice = conn.execute(
                "SELECT * FROM notifications WHERE item_id=? AND notice_id=?", (item_id, notice_id)
            ).fetchone()
            if notice is None:
                raise NotFoundError("notification_not_found", "通知不存在")
            row = conn.execute(
                "SELECT * FROM notification_receipts WHERE notification_id=? AND channel=? AND zone_id=?",
                (notice["id"], channel, zone_id),
            ).fetchone()
            if row is None:
                raise NotFoundError("delivery_not_found", "未找到该通知的投递记录")
            new_status, new_time, attempts, changed = rules.apply_receipt(
                row["status"], row["channel_time"], int(row["attempts"] or 0),
                status, channel_time, error,
            )
            if changed:
                conn.execute(
                    "UPDATE notification_receipts SET status=?, channel_time=?, attempts=?, last_error=?, updated_at=? WHERE id=?",
                    (new_status, new_time, attempts,
                     error if new_status == "failed" else row["last_error"], now_iso(), row["id"]),
                )
                self._refresh_notification_status(conn, notice["id"])
                self.append_audit(
                    conn, item_id, "receipt_recorded", actor, role,
                    {"notice_id": notice_id, "channel": channel, "zone_id": zone_id,
                     "status": new_status, "channel_time": new_time},
                )
            conn.execute("COMMIT")
            return self._row_to_delivery(conn.execute(
                "SELECT * FROM notification_receipts WHERE id=?", (row["id"],)
            ).fetchone())
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def list_pending_deliveries(self, item_id=None):
        conn = self.connect()
        try:
            if item_id is None:
                rows = conn.execute(
                    "SELECT r.*, n.notice_id, n.item_id FROM notification_receipts r "
                    "JOIN notifications n ON n.id=r.notification_id "
                    "WHERE r.status IN ('pending','failed') ORDER BY r.id"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT r.*, n.notice_id, n.item_id FROM notification_receipts r "
                    "JOIN notifications n ON n.id=r.notification_id "
                    "WHERE r.status IN ('pending','failed') AND n.item_id=? ORDER BY r.id",
                    (item_id,),
                ).fetchall()
            return [self._row_to_delivery(row) for row in rows]
        finally:
            conn.close()

    def is_channel_available(self, channel):
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT available FROM channel_state WHERE channel=?", (channel,)
            ).fetchone()
            return True if row is None else bool(row["available"])
        finally:
            conn.close()

    def set_channel_available(self, channel, available):
        conn = self.connect()
        try:
            conn.execute(
                "INSERT INTO channel_state(channel,available,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(channel) DO UPDATE SET available=excluded.available, updated_at=excluded.updated_at",
                (channel, 1 if available else 0, now_iso()),
            )
        finally:
            conn.close()

    def restore_item(self, item_id, expected_version, actor, role, new_status, new_payload, event_payload):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            if expected_version is not None and int(expected_version) != int(row["version"]):
                self._version_conflict(conn, item_id, expected_version)
            item = self._row_to_item(row)
            notices = self._list_notifications_conn(conn, item_id)
            gaps, stale = rules.receipt_gaps(item, notices)
            if gaps:
                raise DomainError(
                    "notification_gaps",
                    "仍有片区缺少成功回执，不能恢复供水",
                    409,
                    details={
                        "gaps": gaps,
                        "stale_notices": [
                            {"notice_id": n["notice_id"], "basis": n["basis"]} for n in stale
                        ],
                    },
                )
            version = int(row["version"]) + 1
            conn.execute(
                "UPDATE items SET status=?, version=?, payload=?, updated_at=? WHERE id=?",
                (new_status, version, canonical_json(new_payload), now_iso(), item_id),
            )
            conn.execute(
                "INSERT INTO actions(item_id,action,actor,role,payload,created_at) VALUES(?,?,?,?,?,?)",
                (item_id, "restore", actor, role, canonical_json(event_payload), now_iso()),
            )
            self.append_audit(conn, item_id, "restore_approved", actor, role, event_payload)
            conn.execute("COMMIT")
            return self.get_item(item_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def audit_trail(self, item_id):
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM audit_events WHERE item_id=? ORDER BY id", (item_id,)).fetchall()
            result = []
            for row in rows:
                value = dict(row)
                value["payload"] = json.loads(value["payload"])
                result.append(value)
            return result
        finally:
            conn.close()

    def state_summary(self):
        conn = self.connect()
        try:
            counts = {}
            for row in conn.execute("SELECT status, COUNT(*) AS total FROM items GROUP BY status").fetchall():
                counts[row["status"]] = row["total"]
            return {"counts": counts, "items": self.list_items()}
        finally:
            conn.close()
