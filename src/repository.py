import json
import sqlite3
import uuid
from datetime import datetime, timezone

from .audit import audit_hash, canonical_json
from .domain import ConflictError, NotFoundError, DomainError


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
                CREATE TABLE IF NOT EXISTS notices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    notice_id TEXT NOT NULL,
                    basis INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    message TEXT NOT NULL,
                    channels TEXT NOT NULL,
                    zones TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_by TEXT NOT NULL,
                    created_role TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    superseded_at TEXT,
                    UNIQUE(item_id, notice_id),
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );
                CREATE TABLE IF NOT EXISTS deliveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    notice_id TEXT NOT NULL,
                    basis INTEGER NOT NULL,
                    zone_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_attempt_at TEXT,
                    last_receipt_at TEXT,
                    confirmed_at TEXT,
                    last_error TEXT,
                    updated_at TEXT NOT NULL,
                    UNIQUE(item_id, notice_id, zone_id, channel),
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );
                CREATE TABLE IF NOT EXISTS receipts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    notice_id TEXT NOT NULL,
                    zone_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    receipt_key TEXT NOT NULL,
                    result TEXT NOT NULL,
                    reported_at TEXT NOT NULL,
                    applied INTEGER NOT NULL DEFAULT 0,
                    actor TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, notice_id, zone_id, channel, receipt_key),
                    FOREIGN KEY(item_id) REFERENCES items(id)
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

    def apply_action(self, item_id, action, actor, role, new_status, new_payload, event_payload, expected_version=None, invalidate_notices=False):
        conn = self.connect()
        conflict = None
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            if expected_version is not None and int(expected_version) != int(row["version"]):
                conflict = {"code": "version_conflict", "latest_version": int(row["version"]), "detail": action}
                conn.execute("ROLLBACK")
            else:
                version = int(row["version"]) + 1
                conn.execute(
                    "UPDATE items SET status=?,version=?,payload=?,updated_at=? WHERE id=?",
                    (new_status, version, canonical_json(new_payload), now_iso(), item_id),
                )
                conn.execute(
                    "INSERT INTO actions(item_id,action,actor,role,payload,created_at) VALUES(?,?,?,?,?,?)",
                    (item_id, action, actor, role, canonical_json(event_payload), now_iso()),
                )
                if invalidate_notices:
                    superseded = self._supersede_notices(conn, item_id)
                    event_payload = dict(event_payload)
                    event_payload["superseded_notices"] = superseded
                self.append_audit(conn, item_id, action, actor, role, event_payload)
                conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()
        if conflict is not None:
            self.raise_conflict(item_id, actor, role, conflict)
        return self.get_item(item_id)

    def _supersede_notices(self, conn, item_id):
        """作废旧依据下的活动通知，并把其未完成的投递置为 void（不再重试）。"""
        rows = conn.execute(
            "SELECT notice_id FROM notices WHERE item_id=? AND status='active'", (item_id,)
        ).fetchall()
        notice_ids = [row["notice_id"] for row in rows]
        if notice_ids:
            marks = ",".join("?" for _ in notice_ids)
            conn.execute(
                "UPDATE notices SET status='superseded', superseded_at=? WHERE item_id=? AND status='active'",
                (now_iso(), item_id),
            )
            conn.execute(
                "UPDATE deliveries SET status='void', updated_at=? WHERE item_id=? AND status IN ('pending','failed','sent') AND notice_id IN (%s)" % marks,
                (now_iso(), item_id, *notice_ids),
            )
        return notice_ids

    def _log_conflict(self, item_id, actor, role, detail):
        """冲突也进账本：生成冲突编号并写入审计链，供败方核对。"""
        conflict_id = "CFL-" + uuid.uuid4().hex[:12]
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self.append_audit(
                conn,
                item_id,
                "conflict_rejected",
                actor,
                role,
                {"conflict_id": conflict_id, "detail": detail},
            )
            conn.execute("COMMIT")
        except sqlite3.Error:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        finally:
            conn.close()
        return conflict_id

    def raise_conflict(self, item_id, actor, role, conflict):
        conflict_id = self._log_conflict(item_id, actor, role, conflict["detail"])
        raise ConflictError(
            conflict["code"],
            "提交与其他值班员冲突，请以最新版本为准",
            details={
                "latest_version": conflict["latest_version"],
                "conflict_id": conflict_id,
            },
        )

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

    # ---- 通知账本：下发、投递、回执 ----

    def _notice_view(self, row):
        value = dict(row)
        value["channels"] = json.loads(value["channels"])
        value["zones"] = json.loads(value["zones"])
        return value

    def dispatch_notice(self, item_id, notice, deliveries, actor, role, expected_version=None):
        """同一事务：校验版本 -> 作废旧通知 -> 写入通知与投递行 -> 更新事件版本。

        同一通知编号重复下发或版本过期都会转成带冲突编号的 409。
        """
        conn = self.connect()
        conflict = None
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            if expected_version is not None and int(expected_version) != int(row["version"]):
                conflict = {"code": "version_conflict", "latest_version": int(row["version"]), "detail": "dispatch:%s" % notice["notice_id"]}
                conn.execute("ROLLBACK")
            else:
                superseded = self._supersede_notices(conn, item_id)
                try:
                    conn.execute(
                        "INSERT INTO notices(item_id,notice_id,basis,kind,message,channels,zones,status,created_by,created_role,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            item_id,
                            notice["notice_id"],
                            notice["basis"],
                            notice["kind"],
                            notice["message"],
                            canonical_json(notice["channels"]),
                            canonical_json(notice["zones"]),
                            "active",
                            actor,
                            role,
                            now_iso(),
                        ),
                    )
                except sqlite3.IntegrityError:
                    conflict = {"code": "duplicate_notice", "latest_version": int(row["version"]), "detail": "dispatch:%s" % notice["notice_id"]}
                    conn.execute("ROLLBACK")
                else:
                    for delivery in deliveries:
                        conn.execute(
                            "INSERT INTO deliveries(item_id,notice_id,basis,zone_id,channel,status,updated_at) VALUES(?,?,?,?,?,?,?)",
                            (
                                item_id,
                                delivery["notice_id"],
                                delivery["basis"],
                                delivery["zone_id"],
                                delivery["channel"],
                                "pending",
                                now_iso(),
                            ),
                        )
                    payload = json.loads(row["payload"])
                    payload.setdefault("notice_basis", notice["basis"])
                    payload.setdefault("notifications", []).append({
                        "notice_id": notice["notice_id"],
                        "kind": notice["kind"],
                        "message": notice["message"],
                        "basis": notice["basis"],
                        "zones": notice["zones"],
                        "channels": notice["channels"],
                        "dispatched_by": actor,
                    })
                    conn.execute(
                        "UPDATE items SET version=?,payload=?,updated_at=? WHERE id=?",
                        (int(row["version"]) + 1, canonical_json(payload), now_iso(), item_id),
                    )
                    self.append_audit(
                        conn,
                        item_id,
                        "notice_dispatched",
                        actor,
                        role,
                        {
                            "notice_id": notice["notice_id"],
                            "basis": notice["basis"],
                            "zones": notice["zones"],
                            "channels": notice["channels"],
                            "superseded_notices": superseded,
                        },
                    )
                    conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()
        if conflict is not None:
            self.raise_conflict(item_id, actor, role, conflict)
        return self.get_notice(item_id, notice["notice_id"])

    def get_notice(self, item_id, notice_id):
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT * FROM notices WHERE item_id=? AND notice_id=?", (item_id, notice_id)
            ).fetchone()
            if row is None:
                raise NotFoundError("notice_not_found", "通知不存在")
            return self._notice_view(row)
        finally:
            conn.close()

    def list_notices(self, item_id):
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM notices WHERE item_id=? ORDER BY id", (item_id,)).fetchall()
            return [self._notice_view(row) for row in rows]
        finally:
            conn.close()

    def list_deliveries(self, item_id, notice_id=None):
        conn = self.connect()
        try:
            if notice_id:
                rows = conn.execute(
                    "SELECT * FROM deliveries WHERE item_id=? AND notice_id=? ORDER BY id",
                    (item_id, notice_id),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM deliveries WHERE item_id=? ORDER BY id", (item_id,)).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def list_receipts(self, item_id, notice_id=None):
        conn = self.connect()
        try:
            if notice_id:
                rows = conn.execute(
                    "SELECT * FROM receipts WHERE item_id=? AND notice_id=? ORDER BY id",
                    (item_id, notice_id),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM receipts WHERE item_id=? ORDER BY id", (item_id,)).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def record_receipt(self, item_id, receipt, actor, role):
        """回执入账：按键去重，按渠道真实时间推进，旧回执只存档不改状态。"""
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            item = conn.execute("SELECT id FROM items WHERE id=?", (item_id,)).fetchone()
            if item is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            notice = conn.execute(
                "SELECT * FROM notices WHERE item_id=? AND notice_id=?",
                (item_id, receipt["notice_id"]),
            ).fetchone()
            if notice is None:
                raise NotFoundError("notice_not_found", "通知不存在")
            delivery = conn.execute(
                "SELECT * FROM deliveries WHERE item_id=? AND notice_id=? AND zone_id=? AND channel=?",
                (item_id, receipt["notice_id"], receipt["zone_id"], receipt["channel"]),
            ).fetchone()
            if delivery is None:
                raise NotFoundError("delivery_not_found", "该片区/渠道不在通知范围内")
            duplicate = conn.execute(
                "SELECT * FROM receipts WHERE item_id=? AND notice_id=? AND zone_id=? AND channel=? AND receipt_key=?",
                (item_id, receipt["notice_id"], receipt["zone_id"], receipt["channel"], receipt["receipt_key"]),
            ).fetchone()
            if duplicate is not None:
                conn.execute("COMMIT")
                return {
                    "recorded": False,
                    "applied": False,
                    "reason": "duplicate",
                    "receipt": dict(duplicate),
                    "delivery": dict(delivery),
                }
            last = delivery["last_receipt_at"]
            applied = 1 if last is None or receipt["reported_at"] >= last else 0
            conn.execute(
                "INSERT INTO receipts(item_id,notice_id,zone_id,channel,receipt_key,result,reported_at,applied,actor,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    item_id,
                    receipt["notice_id"],
                    receipt["zone_id"],
                    receipt["channel"],
                    receipt["receipt_key"],
                    receipt["result"],
                    receipt["reported_at"],
                    applied,
                    actor,
                    now_iso(),
                ),
            )
            if applied:
                status = "confirmed" if receipt["result"] == "success" else "failed"
                confirmed_at = receipt["reported_at"] if receipt["result"] == "success" else None
                conn.execute(
                    "UPDATE deliveries SET status=?,last_receipt_at=?,confirmed_at=?,updated_at=? WHERE id=?",
                    (status, receipt["reported_at"], confirmed_at, now_iso(), delivery["id"]),
                )
            self.append_audit(
                conn,
                item_id,
                "receipt_recorded",
                actor,
                role,
                {
                    "notice_id": receipt["notice_id"],
                    "zone_id": receipt["zone_id"],
                    "channel": receipt["channel"],
                    "result": receipt["result"],
                    "reported_at": receipt["reported_at"],
                    "receipt_key": receipt["receipt_key"],
                    "applied": bool(applied),
                },
            )
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()
        updated = self.list_deliveries(item_id, receipt["notice_id"])
        current = next(
            d for d in updated
            if d["zone_id"] == receipt["zone_id"] and d["channel"] == receipt["channel"]
        )
        return {
            "recorded": True,
            "applied": bool(applied),
            "reason": "applied" if applied else "stale",
            "delivery": current,
        }

    def pending_deliveries(self, item_id=None):
        """待重试投递：只取活动通知下 pending/failed 的行，作废与已确认的不重发。"""
        sql = (
            "SELECT d.*, n.message AS notice_message, n.kind AS notice_kind "
            "FROM deliveries d JOIN notices n ON n.item_id=d.item_id AND n.notice_id=d.notice_id "
            "WHERE n.status='active' AND d.status IN ('pending','failed')"
        )
        params = ()
        if item_id is not None:
            sql += " AND d.item_id=?"
            params = (item_id,)
        sql += " ORDER BY d.id"
        conn = self.connect()
        try:
            rows = conn.execute(sql, params).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def mark_delivery_sent(self, delivery, idempotency_key):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE deliveries SET status='sent', attempts=attempts+1, last_attempt_at=?, last_error=NULL, updated_at=? WHERE id=? AND status IN ('pending','failed')",
                (now_iso(), now_iso(), delivery["id"]),
            )
            self.append_audit(
                conn,
                delivery["item_id"],
                "delivery_sent",
                "channel_gateway",
                "channel_agent",
                {
                    "notice_id": delivery["notice_id"],
                    "zone_id": delivery["zone_id"],
                    "channel": delivery["channel"],
                    "idempotency_key": idempotency_key,
                },
            )
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def mark_delivery_retry(self, delivery, error):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE deliveries SET attempts=attempts+1, last_attempt_at=?, last_error=?, updated_at=? WHERE id=? AND status IN ('pending','failed')",
                (now_iso(), error, now_iso(), delivery["id"]),
            )
            self.append_audit(
                conn,
                delivery["item_id"],
                "delivery_retry_pending",
                "channel_gateway",
                "channel_agent",
                {
                    "notice_id": delivery["notice_id"],
                    "zone_id": delivery["zone_id"],
                    "channel": delivery["channel"],
                    "error": error,
                },
            )
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()
