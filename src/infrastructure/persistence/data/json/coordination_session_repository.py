"""
Adaptador de Repositorio en memoria y archivo JSON para CoordinationSession (Hito R.4).

Implementa:
- InMemoryCoordinationSessionRepository
- JsonCoordinationSessionRepository

Garantías:
- Aislamiento multi-tenant estricto vía CrossTenantGuard.
- Operaciones atómicas y seguras ante concurrencia (threading.RLock).
- Serialización y deserialización fidedigna de CoordinationSession, CoordinationTask, TaskClaim, AgentHandoff, SharedCoordinationContext.
"""

from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import threading
from typing import Optional, List, Dict, Any, Union, Tuple

from src.domain.security.models import validate_safe_identifier
from src.domain.tenant.models import TenantContext
from src.domain.tenant.guard import CrossTenantGuard
from src.domain.planning.models import PlanBudget
from src.domain.agent_coordination.models import (
    CoordinationSession,
    CoordinationTask,
    CoordinationStatus,
    CoordinationTaskStatus,
    CoordinationFailureType,
    CoordinationPolicy,
    MergeStrategy,
    TaskClaim,
    AgentHandoff,
    SharedCoordinationContext,
)
from src.domain.agent_coordination.ports import CoordinationSessionRepositoryPort


class InMemoryCoordinationSessionRepository(CoordinationSessionRepositoryPort):
    """
    Implementación en memoria aislada por tenant para testing y ejecución rápida.
    """

    def __init__(self):
        self._sessions: Dict[Tuple[str, str], CoordinationSession] = {}  # (tenant_id, session_id) -> CoordinationSession
        self._lock = threading.RLock()

    def save_session(
        self,
        session: CoordinationSession,
        tenant_context: TenantContext,
    ) -> CoordinationSession:
        CrossTenantGuard.assert_same_tenant(tenant_context, session.tenant_id, "save_session")
        with self._lock:
            key = (session.tenant_id, session.session_id)
            self._sessions[key] = session
            return session

    def get_session(
        self,
        session_id: str,
        tenant_context: TenantContext,
    ) -> Optional[CoordinationSession]:
        CrossTenantGuard.ensure_tenant_context(tenant_context)
        with self._lock:
            tenant_id_val = tenant_context.tenant_id if isinstance(tenant_context.tenant_id, str) else tenant_context.tenant_id.value
            key = (tenant_id_val, session_id)
            session = self._sessions.get(key)
            if session is not None:
                return session
            owner_tenant_id = next(
                (stored_tenant_id for stored_tenant_id, stored_session_id in self._sessions if stored_session_id == session_id),
                None,
            )
            if owner_tenant_id is not None:
                CrossTenantGuard.assert_same_tenant(tenant_context, owner_tenant_id, "get_session")
            return None

    def get_session_by_mission(
        self,
        mission_id: str,
        tenant_context: TenantContext,
    ) -> Optional[CoordinationSession]:
        CrossTenantGuard.ensure_tenant_context(tenant_context)
        with self._lock:
            tenant_id_val = tenant_context.tenant_id if isinstance(tenant_context.tenant_id, str) else tenant_context.tenant_id.value
            tenant_sessions = [
                s for (t_id, _), s in self._sessions.items()
                if t_id == tenant_id_val and s.mission_id == mission_id
            ]
            if not tenant_sessions:
                return None
            return sorted(tenant_sessions, key=lambda s: s.updated_at or s.created_at, reverse=True)[0]

    def list_sessions_for_tenant(
        self,
        tenant_context: TenantContext,
    ) -> List[CoordinationSession]:
        CrossTenantGuard.ensure_tenant_context(tenant_context)
        with self._lock:
            tenant_id_val = tenant_context.tenant_id if isinstance(tenant_context.tenant_id, str) else tenant_context.tenant_id.value
            return [
                s for (t_id, _), s in self._sessions.items()
                if t_id == tenant_id_val
            ]


class JsonCoordinationSessionRepository(CoordinationSessionRepositoryPort):
    """
    Implementación persistente en disco en formato JSON con bloqueo thread-safe y aislamiento multi-tenant.
    """

    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _get_tenant_dir(self, tenant_id: str) -> Path:
        validate_safe_identifier(tenant_id, "tenant_id")
        t_dir = self.base_dir / tenant_id / "coordination_sessions"
        t_dir.mkdir(parents=True, exist_ok=True)
        return t_dir

    def _get_session_file(self, tenant_id: str, session_id: str) -> Path:
        validate_safe_identifier(session_id, "session_id")
        return self._get_tenant_dir(tenant_id) / f"{session_id}.json"

    def save_session(
        self,
        session: CoordinationSession,
        tenant_context: TenantContext,
    ) -> CoordinationSession:
        CrossTenantGuard.assert_same_tenant(tenant_context, session.tenant_id, "save_session")
        with self._lock:
            file_path = self._get_session_file(session.tenant_id, session.session_id)
            serialized = self._serialize_session(session)
            tmp_path = file_path.with_suffix(".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(serialized, f, indent=2, default=str)
            tmp_path.replace(file_path)
            return session

    def get_session(
        self,
        session_id: str,
        tenant_context: TenantContext,
    ) -> Optional[CoordinationSession]:
        CrossTenantGuard.ensure_tenant_context(tenant_context)
        with self._lock:
            tenant_id_val = tenant_context.tenant_id if isinstance(tenant_context.tenant_id, str) else tenant_context.tenant_id.value
            file_path = self._get_session_file(tenant_id_val, session_id)
            if not file_path.exists():
                filename = f"{session_id}.json"
                owner_tenant_id = next(
                    (
                        candidate.parent.parent.name
                        for candidate in self.base_dir.glob(f"*/coordination_sessions/{filename}")
                        if candidate.is_file()
                    ),
                    None,
                )
                if owner_tenant_id is not None:
                    CrossTenantGuard.assert_same_tenant(tenant_context, owner_tenant_id, "get_session")
                return None
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return self._deserialize_session(data)
            except Exception:
                return None

    def get_session_by_mission(
        self,
        mission_id: str,
        tenant_context: TenantContext,
    ) -> Optional[CoordinationSession]:
        sessions = self.list_sessions_for_tenant(tenant_context)
        matching = [s for s in sessions if s.mission_id == mission_id]
        if not matching:
            return None
        return sorted(matching, key=lambda s: s.updated_at or s.created_at, reverse=True)[0]

    def list_sessions_for_tenant(
        self,
        tenant_context: TenantContext,
    ) -> List[CoordinationSession]:
        CrossTenantGuard.ensure_tenant_context(tenant_context)
        with self._lock:
            tenant_id_val = tenant_context.tenant_id if isinstance(tenant_context.tenant_id, str) else tenant_context.tenant_id.value
            tenant_dir = self._get_tenant_dir(tenant_id_val)
            sessions = []
            for file_path in tenant_dir.glob("*.json"):
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    sessions.append(self._deserialize_session(data))
                except Exception:
                    continue
            return sessions

    def _serialize_session(self, s: CoordinationSession) -> Dict[str, Any]:
        tasks_dict = {}
        for tid, t in s.tasks.items():
            tasks_dict[tid] = {
                "task_id": t.task_id,
                "session_id": t.session_id,
                "tenant_id": t.tenant_id,
                "required_capability": t.required_capability,
                "action_type": t.action_type,
                "assigned_agent_id": t.assigned_agent_id,
                "status": t.status.value,
                "dependencies": list(t.dependencies),
                "required_input_keys": list(t.required_input_keys),
                "inputs": dict(t.inputs),
                "outputs": dict(t.outputs),
                "allocated_budget": {
                    "max_tokens": t.allocated_budget.max_tokens,
                    "max_cost": str(t.allocated_budget.max_cost) if t.allocated_budget.max_cost is not None else None,
                    "max_steps": t.allocated_budget.max_steps,
                    "max_wall_clock_seconds": t.allocated_budget.max_wall_clock_seconds,
                    "is_cost_unknown": t.allocated_budget.is_cost_unknown,
                } if t.allocated_budget else None,
                "claim": {
                    "task_id": t.claim.task_id,
                    "agent_id": t.claim.agent_id,
                    "claimed_at": t.claim.claimed_at.isoformat(),
                    "expires_at": t.claim.expires_at.isoformat() if t.claim.expires_at else None,
                    "lease_id": t.claim.lease_id,
                    "is_active": t.claim.is_active,
                } if t.claim else None,
                "failure_type": t.failure_type.value if t.failure_type else None,
                "failure_reason": t.failure_reason,
                "evidence_refs": list(t.evidence_refs),
                "is_side_effecting": t.is_side_effecting,
                "resource_id": t.resource_id,
                "attempt": t.attempt,
                "assignment_version": t.assignment_version,
                "delegation_history": list(t.delegation_history),
                "metadata": dict(t.metadata),
            }

        handoffs_list = []
        for h in s.handoffs:
            handoffs_list.append({
                "handoff_id": h.handoff_id,
                "source_agent_id": h.source_agent_id,
                "target_task_id": h.target_task_id,
                "payload": dict(h.payload),
                "output_contract_keys": list(h.output_contract_keys),
                "evidence_refs": list(h.evidence_refs),
                "correlation_id": h.correlation_id,
                "created_at": h.created_at.isoformat() if h.created_at else None,
                "target_agent_id": h.target_agent_id,
            })

        active_leases_dict = {}
        for k, l in s.active_leases.items():
            active_leases_dict[k] = {
                "task_id": l.task_id,
                "agent_id": l.agent_id,
                "claimed_at": l.claimed_at.isoformat(),
                "expires_at": l.expires_at.isoformat() if l.expires_at else None,
                "lease_id": l.lease_id,
                "is_active": l.is_active,
            }

        return {
            "session_id": s.session_id,
            "tenant_id": s.tenant_id,
            "mission_id": s.mission_id,
            "correlation_id": s.correlation_id,
            "plan_id": s.plan_id,
            "plan_version": s.plan_version,
            "sub_mission_ids": list(s.sub_mission_ids),
            "status": s.status.value,
            "tasks": tasks_dict,
            "shared_context": {
                "session_id": s.shared_context.session_id,
                "tenant_id": s.shared_context.tenant_id,
                "mission_id": s.shared_context.mission_id,
                "facts": dict(s.shared_context.facts),
                "decisions": dict(s.shared_context.decisions),
                "evidence_refs": list(s.shared_context.evidence_refs),
                "version": s.shared_context.version,
                "updated_at": s.shared_context.updated_at.isoformat() if s.shared_context.updated_at else None,
            } if s.shared_context else None,
            "policy": {
                "max_concurrent_agents": s.policy.max_concurrent_agents,
                "max_concurrent_tasks": s.policy.max_concurrent_tasks,
                "task_claim_timeout_seconds": s.policy.task_claim_timeout_seconds,
                "default_merge_strategy": s.policy.default_merge_strategy.value,
                "fail_fast_on_policy_denied": s.policy.fail_fast_on_policy_denied,
            },
            "handoffs": handoffs_list,
            "active_leases": active_leases_dict,
            "resource_locks": dict(s.resource_locks),
            "created_at": s.created_at.isoformat() if s.created_at else None,
            "updated_at": s.updated_at.isoformat() if s.updated_at else None,
            "checksum": s.checksum,
        }

    def _deserialize_session(self, d: Dict[str, Any]) -> CoordinationSession:
        tasks = {}
        for tid, td in d.get("tasks", {}).items():
            budget = None
            if td.get("allocated_budget"):
                b_data = td["allocated_budget"]
                budget = PlanBudget(
                    max_tokens=b_data.get("max_tokens"),
                    max_cost=Decimal(str(b_data["max_cost"])) if b_data.get("max_cost") is not None else None,
                    max_steps=b_data.get("max_steps"),
                    max_wall_clock_seconds=b_data.get("max_wall_clock_seconds"),
                    is_cost_unknown=b_data.get("is_cost_unknown", False),
                )
            claim = None
            if td.get("claim"):
                c_data = td["claim"]
                claim = TaskClaim(
                    task_id=c_data["task_id"],
                    agent_id=c_data["agent_id"],
                    claimed_at=datetime.fromisoformat(c_data["claimed_at"]),
                    expires_at=datetime.fromisoformat(c_data["expires_at"]) if c_data.get("expires_at") else None,
                    lease_id=c_data.get("lease_id", ""),
                    is_active=c_data.get("is_active", True),
                )
            failure_type = CoordinationFailureType(td["failure_type"]) if td.get("failure_type") else None

            tasks[tid] = CoordinationTask(
                task_id=td["task_id"],
                session_id=td["session_id"],
                tenant_id=td["tenant_id"],
                required_capability=td["required_capability"],
                action_type=td["action_type"],
                assigned_agent_id=td.get("assigned_agent_id"),
                status=CoordinationTaskStatus(td["status"]),
                dependencies=tuple(td.get("dependencies", ())),
                required_input_keys=tuple(td.get("required_input_keys", ())),
                inputs=td.get("inputs", {}),
                outputs=td.get("outputs", {}),
                allocated_budget=budget,
                claim=claim,
                failure_type=failure_type,
                failure_reason=td.get("failure_reason"),
                evidence_refs=tuple(td.get("evidence_refs", ())),
                is_side_effecting=td.get("is_side_effecting", False),
                resource_id=td.get("resource_id"),
                attempt=td.get("attempt", 1),
                assignment_version=td.get("assignment_version", 1),
                delegation_history=tuple(td.get("delegation_history", ())),
                metadata=td.get("metadata", {}),
            )

        handoffs = []
        for hd in d.get("handoffs", []):
            handoffs.append(AgentHandoff(
                handoff_id=hd["handoff_id"],
                source_agent_id=hd["source_agent_id"],
                target_task_id=hd["target_task_id"],
                payload=hd.get("payload", {}),
                output_contract_keys=tuple(hd.get("output_contract_keys", ())),
                evidence_refs=tuple(hd.get("evidence_refs", ())),
                correlation_id=hd.get("correlation_id", ""),
                created_at=datetime.fromisoformat(hd["created_at"]) if hd.get("created_at") else None,
                target_agent_id=hd.get("target_agent_id"),
            ))

        active_leases = {}
        for lk, ld in d.get("active_leases", {}).items():
            active_leases[lk] = TaskClaim(
                task_id=ld["task_id"],
                agent_id=ld["agent_id"],
                claimed_at=datetime.fromisoformat(ld["claimed_at"]),
                expires_at=datetime.fromisoformat(ld["expires_at"]) if ld.get("expires_at") else None,
                lease_id=ld.get("lease_id", ""),
                is_active=ld.get("is_active", True),
            )

        sc_data = d.get("shared_context")
        sc = None
        if sc_data:
            sc = SharedCoordinationContext(
                session_id=sc_data["session_id"],
                tenant_id=sc_data["tenant_id"],
                mission_id=sc_data["mission_id"],
                facts=sc_data.get("facts", {}),
                decisions=sc_data.get("decisions", {}),
                evidence_refs=tuple(sc_data.get("evidence_refs", ())),
                version=sc_data.get("version", 1),
                updated_at=datetime.fromisoformat(sc_data["updated_at"]) if sc_data.get("updated_at") else None,
            )

        p_data = d.get("policy", {})
        policy = CoordinationPolicy(
            max_concurrent_agents=p_data.get("max_concurrent_agents", 5),
            max_concurrent_tasks=p_data.get("max_concurrent_tasks", 10),
            task_claim_timeout_seconds=p_data.get("task_claim_timeout_seconds", 300),
            default_merge_strategy=MergeStrategy(p_data.get("default_merge_strategy", "KEYED_MERGE")),
            fail_fast_on_policy_denied=p_data.get("fail_fast_on_policy_denied", True),
            max_delegations_per_task=p_data.get("max_delegations_per_task", 3),
            allow_fallback_to_degraded=p_data.get("allow_fallback_to_degraded", False),
        )

        return CoordinationSession(
            session_id=d["session_id"],
            tenant_id=d["tenant_id"],
            mission_id=d["mission_id"],
            correlation_id=d.get("correlation_id", ""),
            plan_id=d.get("plan_id"),
            plan_version=d.get("plan_version", 1),
            sub_mission_ids=tuple(d.get("sub_mission_ids", ())),
            status=CoordinationStatus(d["status"]),
            tasks=tasks,
            shared_context=sc,
            policy=policy,
            handoffs=tuple(handoffs),
            active_leases=active_leases,
            resource_locks=d.get("resource_locks", {}),
            created_at=datetime.fromisoformat(d["created_at"]) if d.get("created_at") else None,
            updated_at=datetime.fromisoformat(d["updated_at"]) if d.get("updated_at") else None,
        )
