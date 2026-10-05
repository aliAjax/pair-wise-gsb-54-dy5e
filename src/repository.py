"""SQLite 表结构与事务访问。"""
import json
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from .domain import Conflict, NotFound, WriteUnavailable


ACTIVE_STATES = ("approved", "mobilized", "surveyed", "spliced", "tested")
RETRY_ATTEMPTS = 4
RETRY_BASE_DELAY = 0.02


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_lock_error(exc: sqlite3.Error) -> bool:
    message = str(exc).lower()
    return "locked" in message or "busy" in message or "database is locked" in message


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
        connection = self._connect()
        try:
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
                CREATE TABLE IF NOT EXISTS resource_allocations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    vessel TEXT NOT NULL,
                    voyage_start TEXT,
                    voyage_end TEXT,
                    permit_expires_at TEXT,
                    declared_spare_km REAL NOT NULL DEFAULT 0,
                    reserved_km REAL NOT NULL DEFAULT 0,
                    used_km REAL NOT NULL DEFAULT 0,
                    returned_km REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'held',
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_alloc_vessel ON resource_allocations(vessel, status);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_alloc_held_record
                    ON resource_allocations(record_id) WHERE status = 'held';
                """
            )
            self._backfill_allocations(connection)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _backfill_allocations(self, connection: sqlite3.Connection) -> None:
        """为升级前已在进行中的抢修补齐资源占用，避免老数据绕过冲突检查。"""
        placeholders = ",".join("?" for _ in ACTIVE_STATES)
        rows = connection.execute(
            "SELECT * FROM records WHERE state IN (%s) ORDER BY id" % placeholders, ACTIVE_STATES
        ).fetchall()
        now = _now()
        for row in rows:
            exists = connection.execute(
                "SELECT 1 FROM resource_allocations WHERE record_id=? AND status='held'", (row["id"],)
            ).fetchone()
            if exists:
                continue
            payload = json.loads(row["payload"])
            vessel = payload.get("vessel_name")
            if not vessel:
                continue
            reserved = float(payload.get("required_spare_km", 0.0))
            used = float(payload.get("spare_used_km", 0.0)) if row["state"] in {"spliced", "tested"} else 0.0
            declared = float(payload.get("vessel_spare_km", payload.get("spare_length_km", 0.0)))
            connection.execute(
                "INSERT INTO resource_allocations(record_id,vessel,voyage_start,voyage_end,permit_expires_at,"
                "declared_spare_km,reserved_km,used_km,returned_km,status,created_by,updated_by,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,'held',?,?,?,?)",
                (
                    row["id"], vessel, payload.get("voyage_start"), payload.get("voyage_end"),
                    payload.get("permit_expires_at"), declared, reserved, used, 0.0,
                    row["created_by"], row["updated_by"], now, now,
                ),
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    @staticmethod
    def _allocation_row(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": int(row["id"]),
            "record_id": int(row["record_id"]),
            "vessel": row["vessel"],
            "voyage_start": row["voyage_start"],
            "voyage_end": row["voyage_end"],
            "permit_expires_at": row["permit_expires_at"],
            "declared_spare_km": float(row["declared_spare_km"]),
            "reserved_km": float(row["reserved_km"]),
            "used_km": float(row["used_km"]),
            "returned_km": float(row["returned_km"]),
            "status": row["status"],
            "updated_at": row["updated_at"],
        }

    # ---- 写入串行化与重试 --------------------------------------------

    def run_write(self, work: Callable[[sqlite3.Connection], Any], label: str = "write") -> Any:
        delay = RETRY_BASE_DELAY
        last_error: Optional[sqlite3.OperationalError] = None
        for attempt in range(RETRY_ATTEMPTS):
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                result = work(connection)
                connection.commit()
                return result
            except sqlite3.OperationalError as exc:
                connection.rollback()
                if not _is_lock_error(exc):
                    raise
                last_error = exc
                time.sleep(delay)
                delay *= 2
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
        raise WriteUnavailable("数据库写入繁忙，请重试（%s）" % label) from last_error

    # ---- 事务内原语 ---------------------------------------------------

    @staticmethod
    def tx_get_record(connection: sqlite3.Connection, record_id: int) -> Dict[str, Any]:
        row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return Repository._row(row)

    @staticmethod
    def tx_list_records(connection: sqlite3.Connection, state: Optional[str] = None, limit: int = 500) -> List[Dict[str, Any]]:
        if state:
            rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
        else:
            rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [Repository._row(row) for row in rows]

    @staticmethod
    def tx_held_allocation(connection: sqlite3.Connection, record_id: int) -> Optional[Dict[str, Any]]:
        row = connection.execute(
            "SELECT * FROM resource_allocations WHERE record_id=? AND status='held' ORDER BY id DESC", (record_id,)
        ).fetchone()
        return Repository._allocation_row(row) if row else None

    @staticmethod
    def tx_active_holders(connection: sqlite3.Connection) -> List[Dict[str, Any]]:
        rows = connection.execute(
            "SELECT a.*, r.reference AS reference FROM resource_allocations a "
            "JOIN records r ON r.id = a.record_id WHERE a.status='held'"
        ).fetchall()
        result = []
        for row in rows:
            item = Repository._allocation_row(row)
            item["reference"] = row["reference"]
            result.append(item)
        return result

    @staticmethod
    def tx_create_record(connection: sqlite3.Connection, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            cursor = connection.execute(
                "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
            )
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        record_id = int(cursor.lastrowid)
        connection.execute(
            "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
            (record_id, "created", actor_id, 1, json.dumps({"state": state}, ensure_ascii=False, sort_keys=True), now),
        )
        row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return Repository._row(row)

    @staticmethod
    def tx_mutate_record(connection: sqlite3.Connection, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        if int(row["version"]) != int(expected_version):
            raise Conflict("版本冲突，请刷新后重试")
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
        return Repository._row(result)

    @staticmethod
    def tx_add_audit(connection: sqlite3.Connection, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        connection.execute(
            "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
            (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
        )

    @staticmethod
    def tx_reserve_allocation(connection: sqlite3.Connection, plan: Any, record_id: int, actor_id: str) -> None:
        now = _now()
        connection.execute(
            "INSERT INTO resource_allocations(record_id,vessel,voyage_start,voyage_end,permit_expires_at,"
            "declared_spare_km,reserved_km,used_km,returned_km,status,created_by,updated_by,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,0,0,'held',?,?,?,?)",
            (
                record_id, plan.vessel, plan.voyage_start, plan.voyage_end, plan.permit_expires_at,
                float(plan.declared_spare), float(plan.reserved), actor_id, actor_id, now, now,
            ),
        )

    @staticmethod
    def tx_reassign_allocation(connection: sqlite3.Connection, plan: Any, record_id: int, actor_id: str) -> None:
        now = _now()
        connection.execute(
            "UPDATE resource_allocations SET status='released',updated_by=?,updated_at=? WHERE record_id=? AND status='held'",
            (actor_id, now, record_id),
        )
        Repository.tx_reserve_allocation(connection, plan, record_id, actor_id)

    @staticmethod
    def tx_adjust_allocation(connection: sqlite3.Connection, allocation_id: int, used_km: float, actor_id: str) -> None:
        connection.execute(
            "UPDATE resource_allocations SET used_km=?,updated_by=?,updated_at=? WHERE id=?",
            (float(used_km), actor_id, _now(), allocation_id),
        )

    @staticmethod
    def tx_release_allocation(connection: sqlite3.Connection, allocation_id: int, returned_km: float, used_km: Optional[float], actor_id: str) -> None:
        if used_km is None:
            connection.execute(
                "UPDATE resource_allocations SET returned_km=?,status='released',updated_by=?,updated_at=? WHERE id=?",
                (float(returned_km), actor_id, _now(), allocation_id),
            )
        else:
            connection.execute(
                "UPDATE resource_allocations SET returned_km=?,used_km=?,status='released',updated_by=?,updated_at=? WHERE id=?",
                (float(returned_km), float(used_km), actor_id, _now(), allocation_id),
            )

    # ---- 只读接口 -----------------------------------------------------

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            record = self.tx_get_record(connection, record_id)
            allocations = self._allocations_for(connection, [record_id])
        record["resource"] = allocations.get(record_id)
        return record

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            records = self.tx_list_records(connection, state=state, limit=limit)
            allocations = self._allocations_for(connection, [item["id"] for item in records])
        for item in records:
            item["resource"] = allocations.get(item["id"])
        return records

    @staticmethod
    def _allocations_for(connection: sqlite3.Connection, record_ids: List[int]) -> Dict[int, Dict[str, Any]]:
        if not record_ids:
            return {}
        placeholders = ",".join("?" for _ in record_ids)
        rows = connection.execute(
            "SELECT * FROM resource_allocations WHERE record_id IN (%s) ORDER BY id DESC" % placeholders,
            tuple(record_ids),
        ).fetchall()
        result: Dict[int, Dict[str, Any]] = {}
        for row in rows:
            record_id = int(row["record_id"])
            if record_id not in result:  # 最新一行优先（held或最近一次释放）
                result[record_id] = Repository._allocation_row(row)
        return result

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        def work(connection: sqlite3.Connection) -> Dict[str, Any]:
            return self.tx_create_record(connection, reference, state, payload, actor_id)

        record = self.run_write(work, label="create_record")
        record["resource"] = None
        return record

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        self.run_write(lambda connection: self.tx_add_audit(connection, record_id, actor_id, action, details), label="add_audit")

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

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
