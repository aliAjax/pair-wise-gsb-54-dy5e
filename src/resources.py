"""抢修船、备缆、许可的资源占用核对与释放。

所有核对与占用变更都在commit_action的BEGIN IMMEDIATE写事务内执行：
并发提交同一艘船时，后到者拿到写锁后重新扫描，必能看到先提交者的占用。
"""
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .domain import ResourceConflict, ValidationError, dt_text


VESSEL = "vessel"
SPARE = "spare"
HELD = "held"
COMMITTED = "committed"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class ResourceManager:
    def __init__(self, repository: Any) -> None:
        self.repository = repository

    # ---- 备缆余量 ----

    @staticmethod
    def _spare_holders(connection: sqlite3.Connection, exclude_record_id: int = None) -> List[sqlite3.Row]:
        rows = connection.execute(
            "SELECT * FROM resource_occupations WHERE resource_type=? AND status IN (?,?) ORDER BY id",
            (SPARE, HELD, COMMITTED),
        ).fetchall()
        if exclude_record_id is not None:
            rows = [row for row in rows if int(row["record_id"]) != int(exclude_record_id)]
        return rows

    def _spare_total(self, connection: sqlite3.Connection) -> Tuple[Optional[float], str]:
        """备缆总存量。未显式配置时，取各记录申报spare_length_km的最大值
        （取最大值才能容下任意一份历史抢修计划）。"""
        configured = self.repository.spare_total(connection)
        if configured is not None:
            return configured, "configured"
        row = connection.execute("SELECT MAX(CAST(json_extract(payload,'$.spare_length_km') AS REAL)) AS m FROM records").fetchone()
        fallback = float(row["m"]) if row and row["m"] is not None else 0.0
        return fallback, "default_max_declared"

    def spare_available(self, connection: sqlite3.Connection, exclude_record_id: int = None, extra_committed: float = 0.0) -> Dict[str, Any]:
        total, source = self._spare_total(connection)
        held = sum(float(r["reserved_km"] or 0) for r in self._spare_holders(connection, exclude_record_id) if r["status"] == HELD)
        committed = sum(float(r["used_km"] or 0) for r in self._spare_holders(connection, exclude_record_id) if r["status"] == COMMITTED)
        return {"total_km": round(total, 3), "held_km": round(held, 3), "committed_km": round(committed + extra_committed, 3),
                "available_km": round(total - held - committed - extra_committed, 3), "source": source}

    def reserve_spare(self, connection: sqlite3.Connection, record: Dict[str, Any], required_km: float) -> Dict[str, Any]:
        stock = self.spare_available(connection)
        if stock["available_km"] + 1e-9 < float(required_km):
            holder = self._spare_holders(connection)
            raise ResourceConflict(
                "备缆余量不足：剩余%s公里，本次需%s公里" % (round(stock["available_km"], 2), round(required_km, 2)),
                {"resource_type": SPARE, "required_km": float(required_km), **stock,
                 "occupied_by": [self._holder_ref(row) for row in holder]},
            )
        now = dt_text(_now())
        connection.execute(
            "INSERT INTO resource_occupations(record_id,reference,resource_type,resource_name,status,sailing_from,sailing_to,reserved_km,used_km,released_reason,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (record["id"], record["reference"], SPARE, "", HELD, None, None, float(required_km), None, "", now, now),
        )
        return {"resource_type": SPARE, "reserved_km": float(required_km), **stock}

    def commit_spare_usage(self, connection: sqlite3.Connection, record: Dict[str, Any], used_km: float) -> Dict[str, Any]:
        """接续按实际用量扣减：核对余量，预留置实际用量，释放预留余量。"""
        stock = self.spare_available(connection, exclude_record_id=record["id"], extra_committed=float(used_km))
        if stock["available_km"] + 1e-9 < 0:
            holder = self._spare_holders(connection, exclude_record_id=record["id"])
            raise ResourceConflict(
                "备缆余量不足：实际使用%s公里后将超出存量" % round(float(used_km), 2),
                {"resource_type": SPARE, "used_km": float(used_km), **stock,
                 "occupied_by": [self._holder_ref(row) for row in holder]},
            )
        now = dt_text(_now())
        cur = connection.execute(
            "UPDATE resource_occupations SET status=?,reserved_km=NULL,used_km=?,updated_at=? WHERE record_id=? AND resource_type=? AND status=?",
            (COMMITTED, float(used_km), now, record["id"], SPARE, HELD),
        )
        if cur.rowcount == 0:
            # 旧数据升级后没有预留行：直接补记一条实际占用
            connection.execute(
                "INSERT INTO resource_occupations(record_id,reference,resource_type,resource_name,status,sailing_from,sailing_to,reserved_km,used_km,released_reason,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (record["id"], record["reference"], SPARE, "", COMMITTED, None, None, None, float(used_km), "", now, now),
            )
        return {"resource_type": SPARE, "used_km": float(used_km), **stock}

    def release_spare(self, connection: sqlite3.Connection, record: Dict[str, Any], reason: str) -> Dict[str, Any]:
        now = dt_text(_now())
        cur = connection.execute(
            "UPDATE resource_occupations SET status='released',reserved_km=NULL,released_reason=?,updated_at=? WHERE record_id=? AND resource_type=? AND status IN (?,?)",
            (reason, now, record["id"], SPARE, HELD, COMMITTED),
        )
        return {"resource_type": SPARE, "released_rows": cur.rowcount, "reason": reason}

    def return_spare(self, connection: sqlite3.Connection, record: Dict[str, Any], returned_km: float) -> Dict[str, Any]:
        """恢复后把余量记回：实际占用行关闭，记一条returned的余量回流。"""
        now = dt_text(_now())
        row = connection.execute(
            "SELECT used_km FROM resource_occupations WHERE record_id=? AND resource_type=? AND status=?",
            (record["id"], SPARE, COMMITTED),
        ).fetchone()
        used = float(row["used_km"]) if row else 0.0
        connection.execute(
            "UPDATE resource_occupations SET status='consumed',released_reason='restored',updated_at=? WHERE record_id=? AND resource_type=? AND status=?",
            (now, record["id"], SPARE, COMMITTED),
        )
        connection.execute(
            "UPDATE resource_occupations SET status='released',released_reason='restored',updated_at=? WHERE record_id=? AND resource_type=? AND status=?",
            (now, record["id"], SPARE, HELD),
        )
        returned = min(float(returned_km), used)
        if returned > 0:
            # returned行仅作审计记录；余量计算只统计held/committed，行关闭即回流
            connection.execute(
                "INSERT INTO resource_occupations(record_id,reference,resource_type,resource_name,status,sailing_from,sailing_to,reserved_km,used_km,released_reason,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (record["id"], record["reference"], SPARE, "", "returned", None, None, None, -returned, "restored", now, now),
            )
        return {"resource_type": SPARE, "used_km": used, "returned_km": returned}

    # ---- 抢修船同期航行 ----

    @staticmethod
    def _holder_ref(row: sqlite3.Row) -> Dict[str, Any]:
        return {"record_id": int(row["record_id"]), "reference": row["reference"],
                "resource_name": row["resource_name"], "sailing_from": row["sailing_from"],
                "sailing_to": row["sailing_to"], "reserved_km": row["reserved_km"], "used_km": row["used_km"]}

    def _vessel_holders(self, connection: sqlite3.Connection, vessel_name: str, exclude_record_id: int = None) -> List[sqlite3.Row]:
        rows = connection.execute(
            "SELECT * FROM resource_occupations WHERE resource_type=? AND status=? AND resource_name=? ORDER BY id",
            (VESSEL, HELD, vessel_name),
        ).fetchall()
        if exclude_record_id is not None:
            rows = [row for row in rows if int(row["record_id"]) != int(exclude_record_id)]
        return rows

    @staticmethod
    def _overlaps(a_from: datetime, a_to: datetime, b_from: str, b_to: str) -> bool:
        bf, bt = _parse(b_from), _parse(b_to)
        if bf is None or bt is None:
            # 历史占用缺少时间窗时视为可能冲突，强制人工核对
            return True
        return a_from < bt and a_to > bf

    def acquire_vessel(self, connection: sqlite3.Connection, record: Dict[str, Any], vessel_name: str, sailing_from: str, sailing_to: str) -> Dict[str, Any]:
        """占用抢修船。本记录已有held船占用时就地改到新船/新窗口（供动员确认与改派），
        冲突核对排除本记录自身；有冲突直接抛错，事务回滚后原占用保持不变。"""
        start, end = _parse(sailing_from), _parse(sailing_to)
        holders = self._vessel_holders(connection, vessel_name, exclude_record_id=record["id"])
        blocking = [row for row in holders if self._overlaps(start, end, row["sailing_from"], row["sailing_to"])]
        if blocking:
            holder = blocking[0]
            raise ResourceConflict(
                "抢修船%s在%s至%s期间被抢修单%s占住（其航行窗%s至%s）" % (
                    vessel_name, sailing_from, sailing_to, holder["reference"], holder["sailing_from"], holder["sailing_to"]),
                {"resource_type": VESSEL, "resource_name": vessel_name, "sailing_from": sailing_from, "sailing_to": sailing_to,
                 "occupied_by": [self._holder_ref(row) for row in blocking]},
            )
        now = dt_text(_now())
        existing = connection.execute(
            "SELECT id FROM resource_occupations WHERE record_id=? AND resource_type=? AND status=?",
            (record["id"], VESSEL, HELD),
        ).fetchone()
        if existing:
            connection.execute(
                "UPDATE resource_occupations SET resource_name=?,sailing_from=?,sailing_to=?,updated_at=? WHERE id=?",
                (vessel_name, sailing_from, sailing_to, now, existing["id"]),
            )
        else:
            connection.execute(
                "INSERT INTO resource_occupations(record_id,reference,resource_type,resource_name,status,sailing_from,sailing_to,reserved_km,used_km,released_reason,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (record["id"], record["reference"], VESSEL, vessel_name, HELD, sailing_from, sailing_to, None, None, "", now, now),
            )
        return {"resource_type": VESSEL, "resource_name": vessel_name, "sailing_from": sailing_from, "sailing_to": sailing_to}

    def release_vessel(self, connection: sqlite3.Connection, record: Dict[str, Any], reason: str) -> Dict[str, Any]:
        now = dt_text(_now())
        cur = connection.execute(
            "UPDATE resource_occupations SET status='released',released_reason=?,updated_at=? WHERE record_id=? AND resource_type=? AND status=?",
            (reason, now, record["id"], VESSEL, HELD),
        )
        return {"resource_type": VESSEL, "released_rows": cur.rowcount, "reason": reason}

    def swap_vessel(self, connection: sqlite3.Connection, record: Dict[str, Any], vessel_name: str, sailing_from: str, sailing_to: str) -> Dict[str, Any]:
        """改派：冲突则抛错回滚、原占用保留；通过后原占用行改记到新船。"""
        return self.acquire_vessel(connection, record, vessel_name, sailing_from, sailing_to)

    # ---- 许可有效期 ----

    def verify_permit(self, connection: sqlite3.Connection, payload: Dict[str, Any], vessel_name: str = None, at: datetime = None) -> Dict[str, Any]:
        at = at or _now()
        expiry_text = payload.get("permit_expiry")
        source = "record"
        if not expiry_text and vessel_name:
            vessel = self.repository.get_vessel(connection, vessel_name)
            if vessel and vessel.get("permit_expiry"):
                expiry_text = vessel["permit_expiry"]
                source = "vessel_registry"
        expiry = _parse(expiry_text) if expiry_text else None
        if expiry is not None and expiry < at:
            raise ValidationError("船机许可已于%s过期" % expiry_text)
        return {"permit_expiry": expiry_text, "permit_source": source, "valid": not bool(payload.get("permit_valid") is False)}

    # ---- 动作编排（在写事务连接上执行） ----

    def apply(self, connection: sqlite3.Connection, record: Dict[str, Any], action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if action == "approve":
            return self._approve(connection, record, payload)
        if action == "mobilize":
            return self._mobilize(connection, record, payload)
        if action == "splice":
            return self.commit_spare_usage(connection, record, float(payload["spare_used_km"]))
        if action == "restore":
            vessel_release = self.release_vessel(connection, record, "restored")
            spare_result = self.return_spare(connection, record, float(payload.get("spare_returned_km", 0) or 0))
            return {"vessel": vessel_release, "spare": spare_result}
        if action == "cancel":
            return {"releases": [
                self.release_vessel(connection, record, "cancelled"),
                self.release_spare(connection, record, "cancelled"),
            ]}
        if action == "reassign":
            return self._reassign(connection, record, payload)
        return {}

    def _approve(self, connection: sqlite3.Connection, record: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        required = float(payload.get("required_spare_km", 0) or 0)
        result["spare"] = self.reserve_spare(connection, record, required)
        vessel_name = payload.get("vessel_name")
        if vessel_name:
            self.verify_permit(connection, payload, vessel_name)
            result["vessel"] = self.acquire_vessel(
                connection, record, vessel_name, payload["sailing_from"], payload["sailing_to"])
        else:
            result["vessel"] = None
        return result

    def _mobilize(self, connection: sqlite3.Connection, record: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
        self.verify_permit(connection, payload, payload["vessel_name"])
        # 审批时已占船则就地确认窗口；旧版本审批未占船则在此占用。均在写事务内核对同期航行。
        vessel = self.acquire_vessel(connection, record, payload["vessel_name"], payload["sailing_from"], payload["sailing_to"])
        spare = connection.execute(
            "SELECT id FROM resource_occupations WHERE record_id=? AND resource_type=? AND status=?",
            (record["id"], SPARE, HELD),
        ).fetchone()
        if not spare:
            # 旧版本审批未预留备缆：动员时补占
            self.reserve_spare(connection, record, float(payload.get("required_spare_km", 0) or 0))
        return {"vessel": vessel}

    def _reassign(self, connection: sqlite3.Connection, record: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
        self.verify_permit(connection, payload, payload["vessel_name"])
        return {"vessel": self.swap_vessel(
            connection, record, payload["vessel_name"], payload["sailing_from"], payload["sailing_to"])}

    # ---- 总览 ----

    def snapshot(self) -> Dict[str, Any]:
        with self.repository._connect() as connection:
            stock = self.spare_available(connection)
            vessels: Dict[str, Any] = {}
            for row in connection.execute(
                "SELECT resource_name, COUNT(*) AS jobs, MIN(sailing_from) AS frm, MAX(sailing_to) AS til FROM resource_occupations"
                " WHERE resource_type=? AND status=? GROUP BY resource_name ORDER BY resource_name",
                (VESSEL, HELD),
            ).fetchall():
                vessels[row["resource_name"]] = {"active_jobs": int(row["jobs"]), "sailing_from": row["frm"], "sailing_to": row["til"]}
            occupations = [dict(row) for row in connection.execute(
                "SELECT id,record_id,reference,resource_type,resource_name,status,sailing_from,sailing_to,reserved_km,used_km FROM resource_occupations"
                " WHERE status IN ('held','committed') ORDER BY id").fetchall()]
        return {"spare": stock, "vessels": vessels, "occupations": occupations}
