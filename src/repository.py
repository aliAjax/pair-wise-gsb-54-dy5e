"""SQLite 表结构与事务访问。"""
import json
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from .domain import Conflict, NotFound


SCHEMA_VERSION = 1
RETRYABLE_DB_ERRORS = (sqlite3.OperationalError,)
MAX_WRITE_ATTEMPTS = 5


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS resource_occupations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    reference TEXT NOT NULL,
                    resource_type TEXT NOT NULL,
                    resource_name TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    sailing_from TEXT,
                    sailing_to TEXT,
                    reserved_km REAL,
                    used_km REAL,
                    released_reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS spare_stock (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    total_km REAL
                );
                CREATE TABLE IF NOT EXISTS vessel_registry (
                    vessel_name TEXT PRIMARY KEY,
                    permit_no TEXT NOT NULL DEFAULT '',
                    permit_expiry TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_occ_type_status ON resource_occupations(resource_type, status);
                CREATE INDEX IF NOT EXISTS idx_occ_record ON resource_occupations(record_id);
                CREATE INDEX IF NOT EXISTS idx_occ_vessel ON resource_occupations(resource_name, status);
                """
            )
            self._migrate(connection)

    def _migrate(self, connection: sqlite3.Connection) -> None:
        """已有数据库升级：回填存量与未结束抢修的占用，保证旧数据兼容。"""
        row = connection.execute("PRAGMA user_version").fetchone()
        version = int(row[0]) if row else 0
        if version < 1:
            self._backfill_occupations(connection)
            connection.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)

    @staticmethod
    def _backfill_occupations(connection: sqlite3.Connection) -> None:
        existing = connection.execute("SELECT COUNT(*) AS total FROM resource_occupations").fetchone()
        if int(existing["total"]) > 0:
            return
        now = _now()
        rows = connection.execute("SELECT id, reference, state, payload, created_at FROM records ORDER BY id").fetchall()
        declared_max = 0.0
        for row in rows:
            payload = json.loads(row["payload"])
            declared_max = max(declared_max, float(payload.get("spare_length_km", 0) or 0))
            if row["state"] in {"restored", "cancelled"}:
                continue
            # 未结束抢修：备缆仍被占用；已接续的按实际用量，否则按申报需求预留
            used_km = payload.get("spare_used_km")
            if used_km is not None:
                status, reserved, used = "committed", None, float(used_km)
            else:
                status, reserved, used = "held", float(payload.get("required_spare_km", payload.get("spare_length_km", 0)) or 0), None
            connection.execute(
                "INSERT INTO resource_occupations(record_id,reference,resource_type,resource_name,status,sailing_from,sailing_to,reserved_km,used_km,released_reason,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (row["id"], row["reference"], "spare", "", status, None, None, reserved, used, "", row["created_at"], now),
            )
            vessel = payload.get("vessel_name")
            if vessel:
                sailing_from = payload.get("sailing_from") or row["created_at"]
                sailing_to = payload.get("sailing_to") or now
                connection.execute(
                    "INSERT INTO resource_occupations(record_id,reference,resource_type,resource_name,status,sailing_from,sailing_to,reserved_km,used_km,released_reason,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (row["id"], row["reference"], "vessel", vessel, "held", sailing_from, sailing_to, None, None, "", row["created_at"], now),
                )
        # 存量取所有申报值的最大值，确保至少能容下任意一份历史计划；之后可用接口调整
        connection.execute("INSERT OR IGNORE INTO spare_stock(id,total_km) VALUES(1,?)", (declared_max if declared_max > 0 else None,))

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    @staticmethod
    def _occ_row(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state}, ensure_ascii=False, sort_keys=True), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        return self.commit_action(
            record_id=record_id,
            expected_version=expected_version,
            state=state,
            payload=payload,
            actor_id=actor_id,
            action=action,
            details=details,
            worker=None,
        )

    def commit_action(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any], worker: Optional[Callable[[sqlite3.Connection], None]] = None) -> Dict[str, Any]:
        """单个写事务内完成版本检查、资源占用变更（worker）和记录落库。

        BEGIN IMMEDIATE立即获取写锁：两名调度员并发提交同一艘船时，
        后到者在同一把写锁内重新核对占用必然发现冲突。
        遇到database is locked等可重试错误时指数退避重试。
        """
        last_error: Optional[Exception] = None
        for attempt in range(MAX_WRITE_ATTEMPTS):
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
                if row is None:
                    connection.rollback()
                    connection.close()
                    raise NotFound("记录不存在")
                if int(row["version"]) != int(expected_version):
                    connection.rollback()
                    connection.close()
                    raise Conflict("版本冲突，请刷新后重试")
                if worker is not None:
                    worker(connection)
                version = int(expected_version) + 1
                now = _now()
                connection.execute(
                    "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                    (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
                )
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
                )
                result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
                connection.commit()
                connection.close()
                return self._row(result)
            except RETRYABLE_DB_ERRORS as exc:
                last_error = exc
                try:
                    connection.rollback()
                    connection.close()
                except sqlite3.Error:
                    pass
                if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                    raise
                time.sleep(0.02 * (2 ** attempt))
                continue
            except Exception:
                try:
                    connection.rollback()
                    connection.close()
                except sqlite3.Error:
                    pass
                raise
        raise Conflict("写入冲突重试已耗尽，请稍后重试") from last_error

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    # ---- 资源占用 ----

    def spare_total(self, connection: sqlite3.Connection = None) -> Optional[float]:
        own = connection is None
        conn = connection or self._connect()
        try:
            row = conn.execute("SELECT total_km FROM spare_stock WHERE id=1").fetchone()
            value = None if row is None or row["total_km"] is None else float(row["total_km"])
        finally:
            if own:
                conn.close()
        return value

    def set_spare_total(self, total_km: Optional[float], actor_id: str) -> Optional[float]:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO spare_stock(id,total_km) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET total_km=excluded.total_km",
                (total_km,),
            )
            row = connection.execute("SELECT total_km FROM spare_stock WHERE id=1").fetchone()
        return None if row["total_km"] is None else float(row["total_km"])

    def upsert_vessel(self, vessel_name: str, permit_no: str, permit_expiry: Optional[str], actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO vessel_registry(vessel_name,permit_no,permit_expiry,updated_at) VALUES(?,?,?,?)"
                " ON CONFLICT(vessel_name) DO UPDATE SET permit_no=excluded.permit_no,permit_expiry=excluded.permit_expiry,updated_at=excluded.updated_at",
                (vessel_name, permit_no, permit_expiry, now),
            )
            row = connection.execute("SELECT * FROM vessel_registry WHERE vessel_name=?", (vessel_name,)).fetchone()
        return dict(row)

    def get_vessel(self, connection: sqlite3.Connection, vessel_name: str) -> Optional[Dict[str, Any]]:
        row = connection.execute("SELECT * FROM vessel_registry WHERE vessel_name=?", (vessel_name,)).fetchone()
        return dict(row) if row else None

    def active_occupations(self, connection: sqlite3.Connection = None) -> List[Dict[str, Any]]:
        own = connection is None
        conn = connection or self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM resource_occupations WHERE status IN ('held','committed') ORDER BY id"
            ).fetchall()
        finally:
            if own:
                conn.close()
        return [self._occ_row(row) for row in rows]

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
