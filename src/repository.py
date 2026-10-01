from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import DEFAULT_FACILITY, ID_PREFIX, RENEWAL_STATES, STATES


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

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        renewal_statuses = ",".join("'" + s.replace("'", "''") + "'" for s in RENEWAL_STATES)
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
                    facility TEXT NOT NULL DEFAULT '{DEFAULT_FACILITY}',
                    equipment TEXT NOT NULL DEFAULT '[]',
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
                CREATE TABLE IF NOT EXISTS renewals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL UNIQUE,
                    facility TEXT NOT NULL,
                    permit_id INTEGER NOT NULL REFERENCES items(id),
                    permit_version INTEGER NOT NULL,
                    equipment TEXT NOT NULL,
                    emission_limits TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ({renewal_statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    snapshot TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS attachments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    renewal_id INTEGER NOT NULL REFERENCES renewals(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    content TEXT NOT NULL,
                    uploaded_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
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
            """)
        self._ensure_column("items", "facility",
                            f"TEXT NOT NULL DEFAULT '{DEFAULT_FACILITY}'")
        self._ensure_column("items", "equipment", "TEXT NOT NULL DEFAULT '[]'")

    def _ensure_column(self, table: str, column: str, ddl: str) -> None:
        with self._lock:
            columns = [row["name"] for row in
                       self.conn.execute(f"PRAGMA table_info({table})").fetchall()]
            if column not in columns:
                with self.conn:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["equipment"] = json.loads(item.get("equipment") or "[]")
        if not item.get("facility"):
            item["facility"] = DEFAULT_FACILITY
        return item

    @staticmethod
    def _renewal(row: sqlite3.Row) -> Dict[str, Any]:
        renewal = dict(row)
        renewal["equipment"] = json.loads(renewal["equipment"])
        renewal["emission_limits"] = json.loads(renewal["emission_limits"])
        renewal["snapshot"] = (json.loads(renewal["snapshot"])
                               if renewal["snapshot"] else None)
        return renewal

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str, facility: str = DEFAULT_FACILITY,
                    equipment: Optional[list] = None) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, facility, equipment, created_by,
                       created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, facility,
                     json.dumps(equipment or [], ensure_ascii=False, sort_keys=True),
                     actor, now, now),
                )
                item_id = int(cur.lastrowid)
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

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
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

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    def close_record(self, item_id: int, record_id: int) -> Dict[str, Any]:
        with self._lock, self.conn:
            cur = self.conn.execute(
                "UPDATE records SET status='closed' WHERE id=? AND item_id=? AND status='open'",
                (record_id, item_id),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute(
                    "SELECT 1 FROM records WHERE id=? AND item_id=?",
                    (record_id, item_id),
                ).fetchone()
                if exists is None:
                    raise NotFoundError("记录不存在")
                raise ConflictError("记录已关闭")
            row = self.conn.execute(
                "SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def records_for_facility(self, facility: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT r.* FROM records r JOIN items i ON r.item_id=i.id
                   WHERE i.facility=? ORDER BY r.id""",
                (facility,),
            ).fetchall()
        return [dict(row) for row in rows]

    def find_record_by_external_ref_in_facility(
            self, facility: str, external_ref: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                """SELECT r.* FROM records r JOIN items i ON r.item_id=i.id
                   WHERE i.facility=? AND r.external_ref=? LIMIT 1""",
                (facility, external_ref),
            ).fetchone()
        return dict(row) if row else None

    def create_renewal(self, request_id: str, facility: str, permit_id: int,
                       permit_version: int, equipment: list,
                       emission_limits: dict, actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO renewals(request_id, facility, permit_id, permit_version,
                       equipment, emission_limits, status, version, created_by,
                       created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (request_id, facility, permit_id, permit_version,
                     json.dumps(equipment, ensure_ascii=False, sort_keys=True),
                     json.dumps(emission_limits, ensure_ascii=False, sort_keys=True),
                     RENEWAL_STATES[0], 1, actor, now, now),
                )
                renewal_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("request_id已存在") from exc
        return self.get_renewal(renewal_id)

    def get_renewal(self, renewal_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM renewals WHERE id=?", (renewal_id,)).fetchone()
        if row is None:
            raise NotFoundError("续期单不存在")
        return self._renewal(row)

    def get_renewal_by_request(self, request_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM renewals WHERE request_id=?", (request_id,)).fetchone()
        return self._renewal(row) if row else None

    def list_renewals(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM renewals"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._renewal(row) for row in rows]

    def transition_renewal(self, renewal_id: int, target: str, expected_version: int,
                           snapshot: Optional[dict], actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            if snapshot is None:
                cur = self.conn.execute(
                    """UPDATE renewals SET status=?, version=version+1, updated_at=?
                       WHERE id=? AND version=?""",
                    (target, now, renewal_id, expected_version),
                )
            else:
                cur = self.conn.execute(
                    """UPDATE renewals SET status=?, version=version+1, updated_at=?,
                       snapshot=? WHERE id=? AND version=?""",
                    (target, now,
                     json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
                     renewal_id, expected_version),
                )
            if cur.rowcount == 0:
                exists = self.conn.execute(
                    "SELECT 1 FROM renewals WHERE id=?", (renewal_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("续期单不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_renewal(renewal_id)

    def add_attachment(self, renewal_id: int, name: str, content: str,
                       actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO attachments(renewal_id, name, content, uploaded_by,
                   created_at) VALUES(?,?,?,?,?)""",
                (renewal_id, name, content, actor, now),
            )
            attachment_id = int(cur.lastrowid)
            row = self.conn.execute(
                "SELECT * FROM attachments WHERE id=?", (attachment_id,)).fetchone()
        return dict(row)

    def list_attachments(self, renewal_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM attachments WHERE renewal_id=? ORDER BY id",
                (renewal_id,),
            ).fetchall()
        return [dict(row) for row in rows]

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
