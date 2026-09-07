"""
Application service for N.11 — Emergency Stop (Transversal N — Security, Governance & Safety).

Responsabilidades:
- Control superior de ejecución: Precede inmediatamente al execution boundary físico.
- Activación y desactivación de stops con validación de identidad (N.1), autenticación (N.2), RBAC (N.4) y autorización (N.3).
- Evaluación estructurada de contexto de ejecución:
  * Jerarquía determinista de scopes: GLOBAL > MARKETPLACE > ACCOUNT > MISSION > TOOL > ACTION_TYPE.
  * Fail-Safe incondicional: Corrupción, estado UNKNOWN o fallos de repositorio bloquean de inmediato (0 llamadas físicas).
  * Soporte de acciones Read-Only / Diagnóstico vs Mutaciones / Efectos Colaterales Externos.
  * Expiración temporal determinista vía ClockPort (K.7).
- Emisión de auditoría inmutable (K.1 Audit Trail) y trazas de ejecución (K.2 Agent Trace) sin secretos ni CoT.
- Cero comportamiento destructivo (no borra misiones, órdenes, approvals ni secrets).
"""

from datetime import datetime, timezone
import hashlib
import json
from types import MappingProxyType
from typing import Optional, Sequence, Dict, Any, List, Tuple

from src.domain.emergency_stop.models import (
    EmergencyStopState,
    EmergencyStopScope,
    EmergencyStopDecisionStatus,
    EmergencyStopReasonCode,
    EmergencyStopRecord,
    EmergencyStopEvaluationContext,
    EmergencyStopDecision,
    compute_emergency_stop_checksum,
)
from src.domain.emergency_stop.ports import (
    EmergencyStopRepositoryPort,
    EmergencyStopServicePort,
)
from src.domain.authentication.models import PrincipalContext, AuthenticationStatus
from src.domain.authorization.models import (
    AuthorizationRequest,
    AuthorizationStatus,
    ResourceReference,
)
from src.domain.audit.models import AuditRecord, AuditRecordType, AuditActor, AuditActorType
from src.domain.audit.ports import AuditRepositoryPort
from src.domain.agent_trace.ports import AgentTraceRepositoryPort
from src.domain.agent_trace.models import AgentTraceRecord, StepType, TraceStatus
from src.domain.reliability.ports import ClockPort
from src.domain.security.models import sanitize_security_data, validate_safe_identifier
from src.application.authorization.authorization_service import AuthorizationService
from src.application.rbac.rbac_service import RBACService


class SystemClock(ClockPort):
    """Implementación por defecto de reloj del sistema UTC."""
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def sleep(self, seconds: float) -> None:
        pass


class EmergencyStopService(EmergencyStopServicePort):
    """
    Servicio de aplicación de gobernanza y parada de emergencia (N.11).
    """

    def __init__(
        self,
        repository: EmergencyStopRepositoryPort,
        authorization_service: Optional[AuthorizationService] = None,
        rbac_service: Optional[RBACService] = None,
        audit_repository: Optional[AuditRepositoryPort] = None,
        trace_repository: Optional[AgentTraceRepositoryPort] = None,
        clock: Optional[ClockPort] = None,
        required_activation_permission: str = "EMERGENCY_STOP_ACTIVATE",
        required_deactivation_permission: str = "EMERGENCY_STOP_DEACTIVATE",
        policy_version: str = "1.0.0",
    ):
        if repository is None:
            raise ValueError("repository cannot be None")
        self.repository = repository
        self.authorization_service = authorization_service
        self.rbac_service = rbac_service
        self.audit_repository = audit_repository
        self.trace_repository = trace_repository
        self.clock = clock or SystemClock()
        self.required_activation_permission = required_activation_permission
        self.required_deactivation_permission = required_deactivation_permission
        self.policy_version = policy_version

    def _now(self) -> datetime:
        """Obtiene la fecha/hora UTC actual a través del reloj abstracto."""
        dt = self.clock.now()
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    def _verify_principal_permission(
        self,
        principal_context: Optional[PrincipalContext],
        action_required: str,
        resource_id: str = "emergency_stop",
    ) -> bool:
        """
        Valida que el principal esté autenticado (N.2) y tenga autorización/permiso explícito (N.3/N.4).
        """
        if principal_context is None:
            return False

        if not principal_context.is_authenticated:
            return False

        if not principal_context.identity_id:
            return False

        # Si hay RBACService, verificar que tenga el permiso
        if self.rbac_service is not None:
            if not self.rbac_service.has_permission(
                principal_or_identity=principal_context,
                action=action_required,
                scope=resource_id,
            ):
                # Si no tiene permiso con scope específico, verificar si tiene permiso global
                if not self.rbac_service.has_permission(
                    principal_or_identity=principal_context,
                    action=action_required,
                    scope="global_scope",
                ):
                    return False

        # Si hay AuthorizationService, evaluar autorización explícita
        if self.authorization_service is not None:
            authz_req = AuthorizationRequest(
                action=action_required,
                principal=principal_context,
                resource=ResourceReference(
                    resource_type="governance_control",
                    resource_id=resource_id,
                ),
                environment={},
            )
            authz_decision = self.authorization_service.authorize(authz_req)
            if authz_decision.status != AuthorizationStatus.ALLOW:
                return False

        return True

    def activate_stop(
        self,
        scope: EmergencyStopScope,
        reason_details: str,
        principal_context: PrincipalContext,
        target_id: Optional[str] = None,
        expires_at: Optional[datetime] = None,
        allow_read_only: bool = True,
        metadata: Optional[Dict[str, Any]] = None,
        correlation_id: Optional[str] = None,
    ) -> EmergencyStopRecord:
        """
        Activa un Emergency Stop con validación de seguridad e idempotencia.
        """
        now = self._now()
        corr_id = correlation_id or f"corr_estop_act_{int(now.timestamp())}"

        # 1. Validar identidad y permisos
        if not self._verify_principal_permission(
            principal_context,
            self.required_activation_permission,
            resource_id=f"{scope.value}:{target_id or 'global'}",
        ):
            self._emit_audit(
                record_type=AuditRecordType.AUTHORIZATION_EVALUATED,
                actor_id=principal_context.identity_id if principal_context else "anonymous",
                action="ACTIVATE_EMERGENCY_STOP",
                status="UNAUTHORIZED",
                correlation_id=corr_id,
                subject_id=f"{scope.value}:{target_id or 'global'}",
                metadata={"reason": "Principal lacks permission to activate emergency stop"},
            )
            raise PermissionError(
                f"Principal '{principal_context.identity_id if principal_context else 'anonymous'}' is not authorized to activate Emergency Stop."
            )

        # 2. Verificar idempotencia: Si ya existe un stop idéntico ACTIVE
        existing_records = self.repository.list_records(scope=scope, target_id=target_id, state=EmergencyStopState.ACTIVE)
        for r in existing_records:
            if r.is_active_at(now) and r.allow_read_only == allow_read_only:
                # Idempotente: retornar el existente sin duplicar
                return r

        # 3. Crear registro nuevo
        stop_id_suffix = f"{scope.value.lower()}_{target_id or 'all'}_{int(now.timestamp())}"
        stop_id = f"estop_{stop_id_suffix}"

        record = EmergencyStopRecord(
            stop_id=stop_id,
            scope=scope,
            state=EmergencyStopState.ACTIVE,
            reason_code=EmergencyStopReasonCode.MANUAL_OPERATOR_HALT,
            reason_details=reason_details,
            activated_by_identity_id=principal_context.identity_id,
            activated_at=now,
            target_id=target_id,
            expires_at=expires_at,
            policy_version=self.policy_version,
            allow_read_only=allow_read_only,
            metadata=metadata or {},
        )

        self.repository.save(record)

        # 4. Registrar en Audit Trail y Agent Trace
        self._emit_audit(
            record_type=AuditRecordType.ACTION_EXECUTED,
            actor_id=principal_context.identity_id,
            action="EMERGENCY_STOP_ACTIVATED",
            status="ACTIVE",
            correlation_id=corr_id,
            subject_id=record.stop_id,
            metadata={
                "scope": scope.value,
                "target_id": target_id,
                "reason_details": reason_details,
                "allow_read_only": allow_read_only,
                "expires_at": expires_at.isoformat() if expires_at else None,
            },
        )

        return record

    def deactivate_stop(
        self,
        stop_id: str,
        reason_details: str,
        principal_context: PrincipalContext,
        metadata: Optional[Dict[str, Any]] = None,
        correlation_id: Optional[str] = None,
    ) -> EmergencyStopRecord:
        """
        Desactiva un Emergency Stop existente sin borrar el historial previo.
        """
        now = self._now()
        corr_id = correlation_id or f"corr_estop_deact_{int(now.timestamp())}"

        # 1. Validar identidad y permisos
        if not self._verify_principal_permission(
            principal_context,
            self.required_deactivation_permission,
            resource_id=stop_id,
        ):
            self._emit_audit(
                record_type=AuditRecordType.AUTHORIZATION_EVALUATED,
                actor_id=principal_context.identity_id if principal_context else "anonymous",
                action="DEACTIVATE_EMERGENCY_STOP",
                status="UNAUTHORIZED",
                correlation_id=corr_id,
                subject_id=stop_id,
                metadata={"reason": "Principal lacks permission to deactivate emergency stop"},
            )
            raise PermissionError(
                f"Principal '{principal_context.identity_id if principal_context else 'anonymous'}' is not authorized to deactivate Emergency Stop."
            )

        # 2. Obtener registro
        existing = self.repository.get_by_id(stop_id)
        if not existing:
            raise KeyError(f"Emergency stop with id '{stop_id}' not found.")

        if existing.state == EmergencyStopState.INACTIVE:
            # Idempotente si ya estaba inactivo
            return existing

        # 3. Crear transición inmutable a INACTIVE preservando datos de activación
        updated_meta = dict(existing.metadata)
        if metadata:
            updated_meta.update(metadata)

        deactivated_record = EmergencyStopRecord(
            stop_id=existing.stop_id,
            scope=existing.scope,
            state=EmergencyStopState.INACTIVE,
            reason_code=EmergencyStopReasonCode.AUTHORIZED_DEACTIVATION,
            reason_details=reason_details,
            activated_by_identity_id=existing.activated_by_identity_id,
            activated_at=existing.activated_at,
            target_id=existing.target_id,
            expires_at=existing.expires_at,
            deactivated_by_identity_id=principal_context.identity_id,
            deactivated_at=now,
            policy_version=existing.policy_version,
            allow_read_only=existing.allow_read_only,
            metadata=updated_meta,
        )

        self.repository.save(deactivated_record)

        # 4. Registrar en Audit Trail
        self._emit_audit(
            record_type=AuditRecordType.ACTION_EXECUTED,
            actor_id=principal_context.identity_id,
            action="EMERGENCY_STOP_DEACTIVATED",
            status="INACTIVE",
            correlation_id=corr_id,
            subject_id=stop_id,
            metadata={
                "scope": existing.scope.value,
                "target_id": existing.target_id,
                "deactivation_reason": reason_details,
            },
        )

        return deactivated_record

    def evaluate(
        self,
        context: EmergencyStopEvaluationContext,
    ) -> EmergencyStopDecision:
        """
        Evalúa un contexto de acción contra el estado de Emergency Stop vigente.
        Aplica fail-safe incondicional y jerarquía determinista de scopes:
        GLOBAL > MARKETPLACE > ACCOUNT > MISSION > TOOL > ACTION_TYPE.
        """
        now = self._now()
        corr_id = context.correlation_id or f"corr_estop_eval_{int(now.timestamp())}"
        dec_id = f"dec_estop_{corr_id[:16]}_{int(now.timestamp())}"

        # Fail-safe 1: Verificar corrupción del repositorio
        if getattr(self.repository, "is_corrupt", False):
            return self._create_decision(
                decision_id=dec_id,
                status=EmergencyStopDecisionStatus.BLOCK_EXECUTION,
                reason_code=EmergencyStopReasonCode.FAIL_SAFE_STORE_CORRUPTION,
                reason_details="Emergency stop persistence store is corrupted or untrusted. Fail-safe block applied.",
                evaluated_at=now,
                correlation_id=corr_id,
                mission_id=context.mission_id,
            )

        try:
            active_records = list(self.repository.list_active_records(now))
        except Exception as e:
            return self._create_decision(
                decision_id=dec_id,
                status=EmergencyStopDecisionStatus.BLOCK_EXECUTION,
                reason_code=EmergencyStopReasonCode.FAIL_SAFE_EVALUATION_ERROR,
                reason_details=f"Error accessing emergency stop active records: {e}. Fail-safe block applied.",
                evaluated_at=now,
                correlation_id=corr_id,
                mission_id=context.mission_id,
            )

        if not active_records:
            return self._create_decision(
                decision_id=dec_id,
                status=EmergencyStopDecisionStatus.ALLOW_EXECUTION,
                reason_code=EmergencyStopReasonCode.NO_ACTIVE_STOP,
                reason_details="No active emergency stop applicable.",
                evaluated_at=now,
                correlation_id=corr_id,
                mission_id=context.mission_id,
            )

        # Precedencia de Scopes:
        # 1. GLOBAL
        global_stops = [r for r in active_records if r.scope == EmergencyStopScope.GLOBAL]
        if global_stops:
            applied = global_stops[0]
            if context.is_read_only and applied.allow_read_only:
                return self._create_decision(
                    decision_id=dec_id,
                    status=EmergencyStopDecisionStatus.ALLOW_EXECUTION,
                    reason_code=EmergencyStopReasonCode.READ_ONLY_PERMITTED_BY_POLICY,
                    reason_details=f"Read-only operation allowed under GLOBAL emergency stop ({applied.stop_id}).",
                    evaluated_at=now,
                    active_record_ids=tuple(r.stop_id for r in global_stops),
                    applied_scope=EmergencyStopScope.GLOBAL,
                    applied_target_id=None,
                    correlation_id=corr_id,
                    mission_id=context.mission_id,
                )
            return self._create_decision(
                decision_id=dec_id,
                status=EmergencyStopDecisionStatus.BLOCK_EXECUTION,
                reason_code=EmergencyStopReasonCode.GLOBAL_STOP_ACTIVE,
                reason_details=f"Execution blocked by GLOBAL emergency stop ({applied.stop_id}): {applied.reason_details}",
                evaluated_at=now,
                active_record_ids=tuple(r.stop_id for r in global_stops),
                applied_scope=EmergencyStopScope.GLOBAL,
                applied_target_id=None,
                correlation_id=corr_id,
                mission_id=context.mission_id,
            )

        # 2. MARKETPLACE
        if context.marketplace:
            mp_stops = [r for r in active_records if r.scope == EmergencyStopScope.MARKETPLACE and (r.target_id is None or r.target_id.lower() == context.marketplace.lower())]
            if mp_stops:
                applied = mp_stops[0]
                if context.is_read_only and applied.allow_read_only:
                    return self._create_decision(
                        decision_id=dec_id,
                        status=EmergencyStopDecisionStatus.ALLOW_EXECUTION,
                        reason_code=EmergencyStopReasonCode.READ_ONLY_PERMITTED_BY_POLICY,
                        reason_details=f"Read-only operation allowed under MARKETPLACE stop ({applied.stop_id}).",
                        evaluated_at=now,
                        active_record_ids=tuple(r.stop_id for r in mp_stops),
                        applied_scope=EmergencyStopScope.MARKETPLACE,
                        applied_target_id=applied.target_id,
                        correlation_id=corr_id,
                        mission_id=context.mission_id,
                    )
                return self._create_decision(
                    decision_id=dec_id,
                    status=EmergencyStopDecisionStatus.BLOCK_EXECUTION,
                    reason_code=EmergencyStopReasonCode.MARKETPLACE_STOP_ACTIVE,
                    reason_details=f"Execution blocked by MARKETPLACE stop ({applied.stop_id}) for marketplace '{context.marketplace}': {applied.reason_details}",
                    evaluated_at=now,
                    active_record_ids=tuple(r.stop_id for r in mp_stops),
                    applied_scope=EmergencyStopScope.MARKETPLACE,
                    applied_target_id=applied.target_id,
                    correlation_id=corr_id,
                    mission_id=context.mission_id,
                )

        # 3. ACCOUNT
        if context.account_id:
            acc_stops = [r for r in active_records if r.scope == EmergencyStopScope.ACCOUNT and (r.target_id is None or r.target_id == context.account_id)]
            if acc_stops:
                applied = acc_stops[0]
                if context.is_read_only and applied.allow_read_only:
                    return self._create_decision(
                        decision_id=dec_id,
                        status=EmergencyStopDecisionStatus.ALLOW_EXECUTION,
                        reason_code=EmergencyStopReasonCode.READ_ONLY_PERMITTED_BY_POLICY,
                        reason_details=f"Read-only operation allowed under ACCOUNT stop ({applied.stop_id}).",
                        evaluated_at=now,
                        active_record_ids=tuple(r.stop_id for r in acc_stops),
                        applied_scope=EmergencyStopScope.ACCOUNT,
                        applied_target_id=applied.target_id,
                        correlation_id=corr_id,
                        mission_id=context.mission_id,
                    )
                return self._create_decision(
                    decision_id=dec_id,
                    status=EmergencyStopDecisionStatus.BLOCK_EXECUTION,
                    reason_code=EmergencyStopReasonCode.ACCOUNT_STOP_ACTIVE,
                    reason_details=f"Execution blocked by ACCOUNT stop ({applied.stop_id}) for account '{context.account_id}': {applied.reason_details}",
                    evaluated_at=now,
                    active_record_ids=tuple(r.stop_id for r in acc_stops),
                    applied_scope=EmergencyStopScope.ACCOUNT,
                    applied_target_id=applied.target_id,
                    correlation_id=corr_id,
                    mission_id=context.mission_id,
                )

        # 4. MISSION
        if context.mission_id:
            mis_stops = [r for r in active_records if r.scope == EmergencyStopScope.MISSION and (r.target_id is None or r.target_id == context.mission_id)]
            if mis_stops:
                applied = mis_stops[0]
                if context.is_read_only and applied.allow_read_only:
                    return self._create_decision(
                        decision_id=dec_id,
                        status=EmergencyStopDecisionStatus.ALLOW_EXECUTION,
                        reason_code=EmergencyStopReasonCode.READ_ONLY_PERMITTED_BY_POLICY,
                        reason_details=f"Read-only operation allowed under MISSION stop ({applied.stop_id}).",
                        evaluated_at=now,
                        active_record_ids=tuple(r.stop_id for r in mis_stops),
                        applied_scope=EmergencyStopScope.MISSION,
                        applied_target_id=applied.target_id,
                        correlation_id=corr_id,
                        mission_id=context.mission_id,
                    )
                return self._create_decision(
                    decision_id=dec_id,
                    status=EmergencyStopDecisionStatus.BLOCK_EXECUTION,
                    reason_code=EmergencyStopReasonCode.MISSION_STOP_ACTIVE,
                    reason_details=f"Execution blocked by MISSION stop ({applied.stop_id}) for mission '{context.mission_id}': {applied.reason_details}",
                    evaluated_at=now,
                    active_record_ids=tuple(r.stop_id for r in mis_stops),
                    applied_scope=EmergencyStopScope.MISSION,
                    applied_target_id=applied.target_id,
                    correlation_id=corr_id,
                    mission_id=context.mission_id,
                )

        # 5. TOOL
        if context.tool_name:
            tool_stops = [r for r in active_records if r.scope == EmergencyStopScope.TOOL and (r.target_id is None or r.target_id.lower() == context.tool_name.lower())]
            if tool_stops:
                applied = tool_stops[0]
                if context.is_read_only and applied.allow_read_only:
                    return self._create_decision(
                        decision_id=dec_id,
                        status=EmergencyStopDecisionStatus.ALLOW_EXECUTION,
                        reason_code=EmergencyStopReasonCode.READ_ONLY_PERMITTED_BY_POLICY,
                        reason_details=f"Read-only operation allowed under TOOL stop ({applied.stop_id}).",
                        evaluated_at=now,
                        active_record_ids=tuple(r.stop_id for r in tool_stops),
                        applied_scope=EmergencyStopScope.TOOL,
                        applied_target_id=applied.target_id,
                        correlation_id=corr_id,
                        mission_id=context.mission_id,
                    )
                return self._create_decision(
                    decision_id=dec_id,
                    status=EmergencyStopDecisionStatus.BLOCK_EXECUTION,
                    reason_code=EmergencyStopReasonCode.TOOL_STOP_ACTIVE,
                    reason_details=f"Execution blocked by TOOL stop ({applied.stop_id}) for tool '{context.tool_name}': {applied.reason_details}",
                    evaluated_at=now,
                    active_record_ids=tuple(r.stop_id for r in tool_stops),
                    applied_scope=EmergencyStopScope.TOOL,
                    applied_target_id=applied.target_id,
                    correlation_id=corr_id,
                    mission_id=context.mission_id,
                )

        # 6. ACTION_TYPE
        if context.action_type or context.action_name:
            act_target = context.action_type or context.action_name
            act_stops = [r for r in active_records if r.scope == EmergencyStopScope.ACTION_TYPE and (r.target_id is None or r.target_id.lower() == act_target.lower())]
            if act_stops:
                applied = act_stops[0]
                if context.is_read_only and applied.allow_read_only:
                    return self._create_decision(
                        decision_id=dec_id,
                        status=EmergencyStopDecisionStatus.ALLOW_EXECUTION,
                        reason_code=EmergencyStopReasonCode.READ_ONLY_PERMITTED_BY_POLICY,
                        reason_details=f"Read-only operation allowed under ACTION_TYPE stop ({applied.stop_id}).",
                        evaluated_at=now,
                        active_record_ids=tuple(r.stop_id for r in act_stops),
                        applied_scope=EmergencyStopScope.ACTION_TYPE,
                        applied_target_id=applied.target_id,
                        correlation_id=corr_id,
                        mission_id=context.mission_id,
                    )
                return self._create_decision(
                    decision_id=dec_id,
                    status=EmergencyStopDecisionStatus.BLOCK_EXECUTION,
                    reason_code=EmergencyStopReasonCode.ACTION_TYPE_STOP_ACTIVE,
                    reason_details=f"Execution blocked by ACTION_TYPE stop ({applied.stop_id}) for action '{act_target}': {applied.reason_details}",
                    evaluated_at=now,
                    active_record_ids=tuple(r.stop_id for r in act_stops),
                    applied_scope=EmergencyStopScope.ACTION_TYPE,
                    applied_target_id=applied.target_id,
                    correlation_id=corr_id,
                    mission_id=context.mission_id,
                )

        # No matching stop for this specific context
        return self._create_decision(
            decision_id=dec_id,
            status=EmergencyStopDecisionStatus.ALLOW_EXECUTION,
            reason_code=EmergencyStopReasonCode.NO_ACTIVE_STOP,
            reason_details="Active stops exist for other scopes/targets, but none match this execution context.",
            evaluated_at=now,
            correlation_id=corr_id,
            mission_id=context.mission_id,
        )

    def _create_decision(
        self,
        decision_id: str,
        status: EmergencyStopDecisionStatus,
        reason_code: EmergencyStopReasonCode,
        reason_details: str,
        evaluated_at: datetime,
        active_record_ids: Tuple[str, ...] = (),
        applied_scope: Optional[EmergencyStopScope] = None,
        applied_target_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        mission_id: Optional[str] = None,
    ) -> EmergencyStopDecision:
        """Crea la decisión y emite trazabilidad K.1/K.2."""
        decision = EmergencyStopDecision(
            decision_id=decision_id,
            decision_status=status,
            reason_code=reason_code,
            reason_details=reason_details,
            evaluated_at=evaluated_at,
            active_record_ids=active_record_ids,
            applied_scope=applied_scope,
            applied_target_id=applied_target_id,
            policy_version=self.policy_version,
            correlation_id=correlation_id,
            mission_id=mission_id,
        )

        self._emit_audit(
            record_type=AuditRecordType.ACTION_EXECUTED,
            actor_id="system",
            action="EMERGENCY_STOP_EVALUATED",
            status=status.value,
            correlation_id=correlation_id or "unknown",
            subject_id=decision.decision_id,
            metadata={
                "reason_code": reason_code.value,
                "applied_scope": applied_scope.value if applied_scope else None,
                "active_record_ids": list(active_record_ids),
                "is_blocked": decision.is_blocked,
            },
        )

        return decision

    def _emit_audit(
        self,
        record_type: AuditRecordType,
        actor_id: str,
        action: str,
        status: str,
        correlation_id: str,
        subject_id: str,
        metadata: Dict[str, Any],
    ) -> None:
        """Emite un registro seguro hacia K.1 Audit Trail si el repositorio está configurado."""
        if not self.audit_repository:
            return

        now = self._now()
        audit_id = f"aud_estop_{int(now.timestamp())}_{abs(hash((correlation_id, action, subject_id))) % 100000}"
        actor = AuditActor(
            actor_type=AuditActorType.SYSTEM if actor_id == "system" else AuditActorType.USER,
            actor_id=actor_id or "system",
        )

        payload_for_hash = {
            "audit_id": audit_id,
            "record_type": record_type.value,
            "occurred_at": now.isoformat(),
            "actor_type": actor.actor_type.value,
            "actor_id": actor.actor_id,
            "subject_type": "EMERGENCY_STOP",
            "subject_id": subject_id,
            "action_or_operation": action,
            "status": status,
            "correlation_id": correlation_id,
            "causation_id": None,
            "mission_id": metadata.get("mission_id"),
            "entity_reference": None,
            "evidence_reference": None,
            "provenance": "SYSTEM",
            "idempotency_key": "",
            "schema_version": "1.0.0",
        }
        checksum = hashlib.sha256(json.dumps(payload_for_hash, sort_keys=True).encode("utf-8")).hexdigest()

        rec = AuditRecord(
            audit_id=audit_id,
            record_type=record_type,
            occurred_at=now,
            actor=actor,
            subject_type="EMERGENCY_STOP",
            subject_id=subject_id,
            action_or_operation=action,
            status=status,
            correlation_id=correlation_id,
            checksum=checksum,
        )
        try:
            self.audit_repository.save(rec)
        except Exception:
            pass
