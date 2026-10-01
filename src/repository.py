from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import ID_PREFIX, ITEM_STATUSES, STATES, SUPERSEDED, can_add_record


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()
        self._migrate()

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in ITEM_STATUSES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS renewals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT UNIQUE,
                    facility_id TEXT NOT NULL,
                    old_item_id INTEGER NOT NULL REFERENCES items(id),
                    old_version INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT,
                    severity TEXT NOT NULL DEFAULT 'low',
                    quantity REAL NOT NULL,
                    threshold REAL NOT NULL,
                    equipment_list TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'draft'
                        CHECK(status IN ('draft','approved')),
                    records_snapshot TEXT NOT NULL,
                    open_count INTEGER NOT NULL DEFAULT 0,
                    new_item_id INTEGER REFERENCES items(id),
                    new_version INTEGER,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS attachments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    filename TEXT NOT NULL,
                    content_type TEXT NOT NULL DEFAULT 'text/plain',
                    data TEXT NOT NULL,
                    uploaded_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS idempotency_keys (
                    request_id TEXT PRIMARY KEY,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
            """)

    def _migrate(self) -> None:
        with self._lock, self.conn:
            cols = [r[1] for r in self.conn.execute("PRAGMA table_info(items)").fetchall()]
            if "facility_id" not in cols:
                self.conn.execute("ALTER TABLE items ADD COLUMN facility_id TEXT")
                self.conn.execute("UPDATE items SET facility_id=CAST(id AS TEXT) WHERE facility_id IS NULL")
            if "equipment_list" not in cols:
                self.conn.execute("ALTER TABLE items ADD COLUMN equipment_list TEXT NOT NULL DEFAULT '[]'")

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str, facility_id: Optional[str] = None,
                    equipment_list: Optional[List[str]] = None) -> Dict[str, Any]:
        now = utc_now()
        equip_json = json.dumps(equipment_list or [], ensure_ascii=False)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at,
                       facility_id, equipment_list)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now, facility_id, equip_json),
                )
                item_id = int(cur.lastrowid)
                if facility_id is None:
                    self.conn.execute(
                        "UPDATE items SET facility_id=? WHERE id=?",
                        (str(item_id), item_id),
                    )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def mark_item_superseded(self, item_id: int) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                "UPDATE items SET status=?, version=version+1, updated_at=? WHERE id=?",
                (SUPERSEDED, now, item_id),
            )
            if cur.rowcount == 0:
                raise NotFoundError("项目不存在")
        return self.get_item(item_id)

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        item = self.get_item(item_id)
        if not can_add_record(item["status"]):
            raise ConflictError("许可已被替代，不能补录检查或整改记录")
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def close_record(self, item_id: int, record_id: int) -> Dict[str, Any]:
        self.get_item(item_id)
        with self._lock, self.conn:
            cur = self.conn.execute(
                "UPDATE records SET status='closed' WHERE id=? AND item_id=?",
                (record_id, item_id),
            )
            if cur.rowcount == 0:
                raise NotFoundError("记录不存在")
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM records WHERE id=?", (record_id,)
            ).fetchone()
        return dict(row)

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    def facility_records(self, facility_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT r.* FROM records r JOIN items i ON r.item_id=i.id
                   WHERE i.facility_id=? ORDER BY r.id""",
                (facility_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def facility_open_record_count(self, facility_id: str) -> int:
        with self._lock:
            row = self.conn.execute(
                """SELECT COUNT(*) AS n FROM records r JOIN items i ON r.item_id=i.id
                   WHERE i.facility_id=? AND r.status='open'""",
                (facility_id,),
            ).fetchone()
        return int(row["n"])

    def inspector_authorized(self, actor: str, facility_id: str) -> bool:
        with self._lock:
            row = self.conn.execute(
                """SELECT 1 FROM records r JOIN items i ON r.item_id=i.id
                   WHERE r.created_by=? AND i.facility_id=? LIMIT 1""",
                (actor, facility_id),
            ).fetchone()
        return row is not None

    # ---- renewals ----
    def create_renewal(self, request_id: Optional[str], facility_id: str,
                       old_item_id: int, old_version: int, title: str,
                       description: Optional[str], severity: str, quantity: float,
                       threshold: float, equipment_list: List[str],
                       records_snapshot: List[Dict[str, Any]], open_count: int,
                       actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO renewals(request_id, facility_id, old_item_id, old_version,
                       title, description, severity, quantity, threshold, equipment_list,
                       status, records_snapshot, open_count, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (request_id, facility_id, old_item_id, old_version, title, description,
                     severity, quantity, threshold,
                     json.dumps(equipment_list, ensure_ascii=False), 'draft',
                     json.dumps(records_snapshot, ensure_ascii=False), open_count,
                     actor, now, now),
                )
                renewal_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            if request_id:
                existing = self.get_renewal_by_request(request_id)
                if existing is not None:
                    return existing
            raise ConflictError("续期请求已存在") from exc
        return self.get_renewal(renewal_id)

    def get_renewal(self, renewal_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM renewals WHERE id=?", (renewal_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("续期单不存在")
        return dict(row)

    def get_renewal_by_request(self, request_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM renewals WHERE request_id=?", (request_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def list_renewals(self, facility_id: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM renewals"
        params: tuple = ()
        if facility_id:
            sql += " WHERE facility_id=?"
            params = (facility_id,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def approve_renewal(self, renewal_id: int, new_item_id: int, new_version: int,
                        records_snapshot: List[Dict[str, Any]],
                        open_count: int) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            self.conn.execute(
                """UPDATE renewals SET status='approved', new_item_id=?, new_version=?,
                   records_snapshot=?, open_count=?, updated_at=? WHERE id=?""",
                (new_item_id, new_version,
                 json.dumps(records_snapshot, ensure_ascii=False), open_count, now,
                 renewal_id),
            )
        return self.get_renewal(renewal_id)

    # ---- attachments ----
    def create_attachment(self, item_id: int, filename: str, content_type: str,
                          data: str, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO attachments(item_id, filename, content_type, data,
                   uploaded_by, created_at) VALUES(?,?,?,?,?,?)""",
                (item_id, filename, content_type, data, actor, now),
            )
            attachment_id = int(cur.lastrowid)
        return self.get_attachment(attachment_id)

    def get_attachment(self, attachment_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM attachments WHERE id=?", (attachment_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("附件不存在")
        return dict(row)

    def list_attachments(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                """SELECT id, item_id, filename, content_type, uploaded_by, created_at
                   FROM attachments WHERE item_id=? ORDER BY id""",
                (item_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    # ---- idempotency ----
    def save_idempotency(self, request_id: str, entity_type: str, entity_id: int,
                         response: Dict[str, Any]) -> None:
        now = utc_now()
        with self._lock, self.conn:
            self.conn.execute(
                """INSERT OR REPLACE INTO idempotency_keys
                   (request_id, entity_type, entity_id, response_json, created_at)
                   VALUES(?,?,?,?,?)""",
                (request_id, entity_type, entity_id,
                 json.dumps(response, ensure_ascii=False, default=str), now),
            )

    def get_idempotency(self, request_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM idempotency_keys WHERE request_id=?", (request_id,)
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["response"] = json.loads(result["response_json"])
        return result

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            cur = self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
                   previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"], event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]),
            )
            event_id = int(cur.lastrowid)
        event["id"] = event_id
        return event

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
            previous = "GENESIS"
            for row in rows:
                if row["previous_hash"] != previous:
                    return False
                payload = {
                    "action": row["action"], "entity_type": row["entity_type"],
                    "entity_id": row["entity_id"], "actor": row["actor"],
                    "detail": json.loads(row["detail"]), "created_at": row["created_at"],
                }
                if calculate_hash(previous, payload) != row["entry_hash"]:
                    return False
                previous = row["entry_hash"]
        return True

    def close(self) -> None:
        with self._lock:
            self.conn.close()
