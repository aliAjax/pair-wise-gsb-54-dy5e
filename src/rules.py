"""跨海光缆故障与抢修协调领域规则与状态转换。"""
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, number, optional_text, text, text_list


INITIAL_STATE = "detected"
CREATE_ROLES = {'noc_operator'}
ACTION_ROLES = {'approve': {'repair_manager'}, 'mobilize': {'vessel_master'}, 'survey': {'cable_engineer'}, 'splice': {'cable_engineer'}, 'test': {'noc_operator'}, 'restore': {'noc_operator', 'repair_manager'}, 'cancel': {'repair_manager'}, 'reassign': {'repair_manager', 'vessel_master'}}
TRANSITIONS = {'approve': {'detected': 'approved'}, 'mobilize': {'approved': 'mobilized'}, 'survey': {'mobilized': 'surveyed'}, 'splice': {'surveyed': 'spliced'}, 'test': {'spliced': 'tested'}, 'restore': {'tested': 'restored'}, 'cancel': {'detected': 'cancelled', 'approved': 'cancelled', 'mobilized': 'cancelled'}, 'reassign': {'approved': 'approved', 'mobilized': 'mobilized'}}
REASSIGN_STATES = {'approved', 'mobilized'}
TERMINAL_STATES = {'restored', 'cancelled'}


@dataclass
class ResourcePlan:
    """一次动作对船舶/备缆占用的处理计划。"""
    mode: str  # none/reserve/reassign/adjust/return/release
    vessel: str = ""
    voyage_start: Optional[str] = None
    voyage_end: Optional[str] = None
    permit_expires_at: Optional[str] = None
    declared_spare: float = 0.0
    reserved: float = 0.0
    used: float = 0.0
    returned: float = 0.0

    @staticmethod
    def empty() -> "ResourcePlan":
        return ResourcePlan(mode="none")


def parse_time(value: Any, key: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("%s必须是ISO8601时间" % key)
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError("%s必须是ISO8601时间" % key) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def optional_iso(data: Dict[str, Any], key: str) -> Optional[str]:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        return None
    return parse_time(value, key).isoformat()


def voyage_window(data: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    start = optional_text(data, "voyage_start", "") or None
    end = optional_text(data, "voyage_end", "") or None
    if (start is None) != (end is None):
        raise ValidationError("航行开始和结束时间必须同时提供")
    if start is not None:
        start = parse_time(start, "voyage_start").isoformat()
        end = parse_time(end, "voyage_end").isoformat()
        if not (datetime.fromisoformat(end) > datetime.fromisoformat(start)):
            raise ValidationError("航行结束时间必须晚于开始时间")
    return start, end


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "cable")
        text(p, "segment")
        start = number(p, "start_km", 0)
        end = number(p, "end_km", 0)
        number(p, "depth_m", 1)
        integer(p, "sea_state", 0, 9)
        boolean(p, "vessel_available")
        number(p, "spare_length_km", 0)
        boolean(p, "permit_valid")
        integer(p, "capacity_gbps", 1)
        optional_iso(p, "permit_expires_at")
        if end <= start:
            raise ValidationError("结束里程必须大于开始里程")
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        distance = float(p["end_km"]) - float(p["start_km"])
        p["repair_distance_km"] = round(distance, 2)
        p["required_spare_km"] = round(distance * 1.05, 2)
        p["estimated_repair_hours"] = round(distance / 2.0 + float(p["depth_m"]) / 100.0 + int(p["sea_state"]) * 2.0, 2)
        p["repair_feasible"] = bool(p["vessel_available"] and p["permit_valid"] and p["spare_length_km"] >= p["required_spare_km"] and int(p["sea_state"]) <= 5)
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            if item["state"] in TERMINAL_STATES or item["payload"].get("cable") != payload.get("cable") or item["payload"].get("segment") != payload.get("segment"):
                continue
            if float(payload["start_km"]) < float(item["payload"].get("end_km", 0)) and float(payload["end_km"]) > float(item["payload"].get("start_km", 0)):
                raise Conflict("同一光缆区段已有未结束抢修")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def _permit_ok(self, p: Dict[str, Any], now: datetime) -> None:
        if not bool(p.get("permit_valid")):
            raise ValidationError("船机许可无效")
        expires = p.get("permit_expires_at")
        if expires and datetime.fromisoformat(expires) <= now:
            raise ValidationError("船机许可已过期（有效期至%s）" % expires)

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any], now: Optional[datetime] = None) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        now = now or datetime.now(timezone.utc)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action == "approve":
            if not bool(p["vessel_available"]):
                raise ValidationError("船舶条件不满足")
            self._permit_ok(p, now)
            changes["repair_manager"] = text(data, "repair_manager")
            vessel = optional_text(data, "vessel_name", "")
            if vessel:
                changes["vessel_name"] = vessel
            permit_expires = optional_iso(data, "permit_expires_at")
            if permit_expires is not None:
                changes["permit_expires_at"] = permit_expires
                self._permit_ok(dict(p, permit_expires_at=permit_expires), now)
            start, end = voyage_window(data)
            if start is not None:
                changes["voyage_start"] = start
                changes["voyage_end"] = end
            if data.get("vessel_spare_km") is not None:
                changes["vessel_spare_km"] = number(data, "vessel_spare_km", 0)
            summary = "抢修方案已批准"
        elif action == "mobilize":
            if float(data.get("weather_window_hours", 0)) < float(p["estimated_repair_hours"]):
                raise ValidationError("海况窗口不足以完成抢修")
            if float(data.get("available_spare_km", 0)) < float(p["required_spare_km"]):
                raise ValidationError("船上备缆不足")
            changes["weather_window_hours"] = float(data["weather_window_hours"])
            changes["vessel_name"] = text(data, "vessel_name")
            start, end = voyage_window(data)
            if start is not None:
                changes["voyage_start"] = start
                changes["voyage_end"] = end
            summary = "抢修船已动员"
        elif action == "survey":
            if not boolean(data, "survey_complete"):
                raise ValidationError("勘察尚未完成")
            fault_km = number(data, "fault_location_km", 0)
            if not (float(p["start_km"]) <= fault_km <= float(p["end_km"])):
                raise ValidationError("故障点不在申报区段")
            changes["fault_location_km"] = fault_km
            summary = "故障点勘察完成"
        elif action == "splice":
            loss = number(data, "splice_loss_db", 0)
            if loss > 0.2:
                raise ValidationError("接续损耗超过阈值")
            if float(data.get("spare_used_km", 0)) < float(p["repair_distance_km"]):
                raise ValidationError("备缆使用长度不足")
            changes["splice_loss_db"] = loss
            changes["spare_used_km"] = float(data["spare_used_km"])
            summary = "光缆接续完成"
        elif action == "test":
            end_loss = number(data, "end_to_end_loss_db", 0)
            if end_loss > 0.5:
                raise ValidationError("端到端损耗不合格")
            changes["end_to_end_loss_db"] = end_loss
            changes["test_passed"] = True
            summary = "系统测试通过"
        elif action == "restore":
            if not boolean(data, "traffic_restored"):
                raise ValidationError("业务流量尚未恢复")
            changes["traffic_restored"] = True
            changes["restore_capacity_gbps"] = integer(data, "restore_capacity_gbps", 1)
            summary = "通信恢复"
        elif action == "cancel":
            changes["cancel_reason"] = text(data, "cancel_reason")
            summary = "抢修取消"
        elif action == "reassign":
            if record["state"] not in REASSIGN_STATES:
                raise Conflict("仅已批准或已动员的抢修可以改派")
            vessel = text(data, "vessel_name")
            if vessel == p.get("vessel_name", "") and "voyage_start" not in data and "voyage_end" not in data:
                raise ValidationError("改派目标船与当前占用船相同")
            changes["vessel_name"] = vessel
            start, end = voyage_window(data)
            changes["voyage_start"] = start
            changes["voyage_end"] = end
            if data.get("permit_expires_at") is not None:
                permit_expires = optional_iso(data, "permit_expires_at")
                changes["permit_expires_at"] = permit_expires
            if data.get("vessel_spare_km") is not None:
                changes["vessel_spare_km"] = number(data, "vessel_spare_km", 0)
            self._permit_ok(dict(p, **changes), now)
            reason = optional_text(data, "reassign_reason", "")
            if reason:
                changes["reassign_reason"] = reason
            summary = "抢修船已改派为%s" % vessel
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)

    # ---- 资源占用 -----------------------------------------------------

    @staticmethod
    def _windows_overlap(plan: ResourcePlan, holder: Dict[str, Any]) -> bool:
        start = plan.voyage_start
        end = plan.voyage_end
        other_start = holder.get("voyage_start")
        other_end = holder.get("voyage_end")
        if not start or not end or not other_start or not other_end:
            # 任一方未声明航行窗口，视为同期占用，防止同船被两条记录重复预订
            return True
        return datetime.fromisoformat(start) < datetime.fromisoformat(other_end) and datetime.fromisoformat(other_start) < datetime.fromisoformat(end)

    def check_resource_conflicts(self, plan: ResourcePlan, record_id: int, holders: List[Dict[str, Any]]) -> None:
        """holders：其他进行中抢修对同一资源池的占用行（不含本记录）。"""
        if plan.mode not in {"reserve", "reassign"}:
            return
        others = [h for h in holders if h.get("vessel") == plan.vessel and not h.get("released")]
        for holder in others:
            if self._windows_overlap(plan, holder):
                window = ""
                if holder.get("voyage_start") and holder.get("voyage_end"):
                    window = "（航行窗口%s至%s）" % (holder["voyage_start"], holder["voyage_end"])
                raise Conflict(
                    "抢修船%s的同期航行已被抢修记录%s占用%s" % (plan.vessel, holder["reference"], window),
                    details={"resource": "vessel", "vessel": plan.vessel, "held_by": {"record_id": holder["record_id"], "reference": holder["reference"]}},
                )
        occupied = round(sum(float(h["reserved_km"]) - float(h["returned_km"]) for h in others), 2)
        remaining = round(plan.declared_spare - occupied, 2)
        if remaining + 1e-9 < plan.reserved:
            holder = others[0] if others else None
            held_by = {"record_id": holder["record_id"], "reference": holder["reference"]} if holder else None
            raise Conflict(
                "抢修船%s剩余备缆不足：需要%.2f公里，仅剩%.2f公里（在船备缆%.2f公里，%.2f公里已被其他抢修占用）" % (plan.vessel, plan.reserved, max(remaining, 0.0), plan.declared_spare, occupied),
                details={"resource": "spare", "vessel": plan.vessel, "required_km": plan.reserved, "remaining_km": max(remaining, 0.0), "held_by": held_by},
            )

    def plan_resource(self, action: str, record: Dict[str, Any], new_payload: Dict[str, Any], data: Dict[str, Any], current: Optional[Dict[str, Any]], now: Optional[datetime] = None) -> ResourcePlan:
        """依据动作、记录与当前占用行，生成资源处理计划。current为本记录未释放的占用行。"""
        now = now or datetime.now(timezone.utc)
        p = new_payload
        required = float(p["required_spare_km"])
        if action == "approve":
            vessel = p.get("vessel_name", "")
            if not vessel:
                return ResourcePlan.empty()
            declared = float(p.get("vessel_spare_km", p["spare_length_km"]))
            return ResourcePlan(
                mode="reserve",
                vessel=vessel,
                voyage_start=p.get("voyage_start"),
                voyage_end=p.get("voyage_end"),
                permit_expires_at=p.get("permit_expires_at"),
                declared_spare=declared,
                reserved=required,
            )
        if action == "mobilize":
            vessel = p["vessel_name"]
            if current is not None and not current.get("released"):
                if current["vessel"] != vessel:
                    raise ValidationError("动员船名与已批准占用的船%s不一致，请先改派" % current["vessel"])
                return ResourcePlan.empty()
            return ResourcePlan(
                mode="reserve",
                vessel=vessel,
                voyage_start=p.get("voyage_start"),
                voyage_end=p.get("voyage_end"),
                permit_expires_at=p.get("permit_expires_at"),
                declared_spare=float(data.get("available_spare_km", p["spare_length_km"])),
                reserved=required,
            )
        if action == "splice":
            if current is None or current.get("released"):
                return ResourcePlan.empty()
            return ResourcePlan(mode="adjust", vessel=current["vessel"], reserved=float(current["reserved_km"]), used=float(p["spare_used_km"]))
        if action == "restore":
            if current is None or current.get("released"):
                return ResourcePlan.empty()
            used = float(current.get("used_km") or 0.0)
            reserved = float(current["reserved_km"])
            return ResourcePlan(mode="return", vessel=current["vessel"], reserved=reserved, used=used, returned=max(0.0, round(reserved - used, 2)))
        if action == "cancel":
            if current is None or current.get("released"):
                return ResourcePlan.empty()
            used = float(current.get("used_km") or 0.0)
            reserved = float(current["reserved_km"])
            return ResourcePlan(mode="release", vessel=current["vessel"], reserved=reserved, used=used, returned=max(0.0, round(reserved - used, 2)))
        if action == "reassign":
            vessel = p["vessel_name"]
            declared = float(p.get("vessel_spare_km", record["payload"].get("spare_length_km", 0)))
            used = float(current["used_km"]) if current is not None and not current.get("released") else 0.0
            return ResourcePlan(
                mode="reassign",
                vessel=vessel,
                voyage_start=p.get("voyage_start"),
                voyage_end=p.get("voyage_end"),
                permit_expires_at=p.get("permit_expires_at"),
                declared_spare=declared,
                reserved=required,
                used=used,
            )
        return ResourcePlan.empty()
