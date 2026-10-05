"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, PermissionDenied, ResourceConflict, ValidationError, text
from .repository import Repository
from .resources import ResourceManager
from .rules import DomainRules


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)
        self.resources = ResourceManager(repository)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self._with_occupations(self.repository.get(record_id))

    def _with_occupations(self, record: Dict[str, Any]) -> Dict[str, Any]:
        record["occupations"] = [
            item for item in self.repository.active_occupations() if int(item["record_id"]) == int(record["id"])
        ]
        return record

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        resource_info: Dict[str, Any] = {}

        def worker(connection) -> None:
            # 在写事务内核对船的同期航行、备缆余量和许可有效期
            resource_info.update(self.resources.apply(connection, record, action, new_payload))

        details = {"summary": summary, "input": data or {}, "from": record["state"], "to": new_state}
        try:
            result = self.repository.commit_action(
                record_id=record_id,
                expected_version=int(expected_version),
                state=new_state,
                payload=new_payload,
                actor_id=actor.user_id,
                action=action,
                details=details,
                worker=worker,
            )
        except ResourceConflict as exc:
            # 冲突：记录停在当前状态，写明被哪项抢修占住；输入不入库，由调用方保留后重试
            self._note_blocked(record_id, actor, action, data or {}, exc)
            raise
        details["resources"] = resource_info
        return self._with_occupations(result)

    def _note_blocked(self, record_id: int, actor: Actor, action: str, data: Dict[str, Any], exc: ResourceConflict) -> None:
        try:
            self.audit.note(record_id, actor.user_id, "blocked",
                           {"action": action, "reason": str(exc), "conflict": exc.details, "input": data})
        except Exception:
            # 审计记录失败不应掩盖原始冲突
            pass

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()

    # ---- 资源台账 ----

    def resources_snapshot(self, actor: Actor) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.resources.snapshot()

    def set_spare_total(self, actor: Actor, total_km) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if actor.role not in {"admin", "repair_manager"}:
            raise PermissionDenied("角色无权调整备缆存量")
        if total_km is not None:
            from .domain import number
            total_km = number({"total_km": total_km}, "total_km", 0)
        value = self.repository.set_spare_total(total_km, actor.user_id)
        return {"total_km": value}

    def register_vessel(self, actor: Actor, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if actor.role not in {"admin", "repair_manager", "vessel_master"}:
            raise PermissionDenied("角色无权登记船舶许可")
        vessel_name = text(data, "vessel_name")
        permit_no = data.get("permit_no", "")
        if permit_no is not None and not isinstance(permit_no, str):
            raise ValidationError("permit_no必须是文本")
        from .domain import iso_dt, dt_text
        expiry = iso_dt(data, "permit_expiry")
        vessel = self.repository.upsert_vessel(vessel_name, (permit_no or "").strip(), dt_text(expiry) if expiry else None, actor.user_id)
        return vessel
