"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, PermissionDenied, text
from .repository import Repository
from .rules import DomainRules, ResourcePlan


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

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

        def work(connection) -> Dict[str, Any]:
            self.rules.check_create_conflicts(prepared, self.repository.tx_list_records(connection, limit=500))
            return self.repository.tx_create_record(connection, reference, self.rules.INITIAL_STATE, prepared, actor.user_id)

        record = self.repository.run_write(work, label="create_record")
        record["resource"] = None
        return record

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get(record_id)

    def _apply_plan(self, connection, plan: ResourcePlan, record_id: int, actor_id: str) -> Dict[str, Any]:
        current = self.repository.tx_held_allocation(connection, record_id)
        resource_summary: Dict[str, Any] = {"action": plan.mode}
        if plan.mode == "reserve":
            self.repository.tx_reserve_allocation(connection, plan, record_id, actor_id)
            resource_summary.update({"vessel": plan.vessel, "reserved_km": plan.reserved, "declared_spare_km": plan.declared_spare})
        elif plan.mode == "reassign":
            old_vessel = current["vessel"] if current else ""
            self.repository.tx_reassign_allocation(connection, plan, record_id, actor_id)
            resource_summary.update({"from_vessel": old_vessel, "vessel": plan.vessel, "reserved_km": plan.reserved})
        elif plan.mode == "adjust":
            self.repository.tx_adjust_allocation(connection, int(current["id"]), plan.used, actor_id)
            resource_summary.update({"vessel": current["vessel"], "used_km": plan.used})
        elif plan.mode in {"return", "release"}:
            self.repository.tx_release_allocation(
                connection,
                int(current["id"]),
                plan.returned,
                plan.used if plan.mode == "release" else None,
                actor_id,
            )
            resource_summary.update({"vessel": current["vessel"], "returned_km": plan.returned, "used_km": float(current["used_km"])})
        return resource_summary

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        data = data or {}

        def work(connection) -> Dict[str, Any]:
            record = self.repository.tx_get_record(connection, record_id)
            current = self.repository.tx_held_allocation(connection, record_id)
            new_state, new_payload, summary = self.rules.apply_action(record, action, data)
            plan = self.rules.plan_resource(action, record, new_payload, data, current)
            resource_summary = {"action": "none"}
            if plan.mode in {"reserve", "reassign"}:
                holders = [h for h in self.repository.tx_active_holders(connection) if int(h["record_id"]) != int(record_id)]
                self.rules.check_resource_conflicts(plan, record_id, holders)
            if plan.mode != "none":
                resource_summary = self._apply_plan(connection, plan, record_id, actor.user_id)
            mutated = self.repository.tx_mutate_record(
                connection,
                record_id=record_id,
                expected_version=int(expected_version),
                state=new_state,
                payload=new_payload,
                actor_id=actor.user_id,
                action=action,
                details={
                    "summary": summary,
                    "input": data,
                    "from": record["state"],
                    "to": new_state,
                    "resource": resource_summary,
                },
            )
            latest = connection.execute(
                "SELECT * FROM resource_allocations WHERE record_id=? ORDER BY id DESC LIMIT 1", (record_id,)
            ).fetchone()
            mutated["resource"] = self.repository._allocation_row(latest) if latest else None
            return mutated

        return self.repository.run_write(work, label="action_%s" % action)

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()
