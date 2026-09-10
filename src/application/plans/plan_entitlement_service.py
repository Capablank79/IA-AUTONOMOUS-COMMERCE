"""
Servicio de Aplicación para Plans & Pricing Tiers SaaS (Hito O.8 — Plans & Pricing Tiers).

Responsabilidades:
- Implementar PlanEntitlementServicePort.
- Administrar el ciclo de vida de asignaciones de planes por tenant (assign, upgrade, downgrade).
- Evaluar determinísticamente capabilities comerciales (features, model access, provider access, user limits).
- Integrar con O.7 Quota Management: materializar determinísticamente QuotaPolicy en base a la plantilla del plan activo.
- Mantener estricto aislamiento multi-tenant y fail-safe (missing plan -> UNKNOWN/DENY, corrupt assignment -> DENY).
- Preservar histórico de asignaciones sin manipular usage histórico de O.6 ni implementar billing (O.9).
- Emitir trazas y auditoría seguras (Audit K.1, Trace K.2) sin datos sensibles.
"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
import threading
from typing import Optional, List, Dict, Any, Mapping, Tuple, Union
import uuid

from src.domain.tenant.models import (
    TenantContext,
    CrossTenantAccessError,
    TenantSecurityViolationError,
)
from src.domain.tenant.guard import CrossTenantGuard
from src.domain.reliability.ports import ClockPort
from src.domain.quota_management.models import QuotaPolicy
from src.domain.quota_management.ports import QuotaPolicyRepositoryPort
from src.domain.plans.models import (
    Plan,
    PlanTier,
    PlanStatus,
    PlanFeature,
    PlanLimits,
    PlanQuotaTemplate,
    PlanAssignment,
    PlanAssignmentStatus,
    PlanEntitlementRequest,
    PlanEntitlementDecision,
    PlanEntitlementStatus,
    PlanNotFoundError,
    PlanVersionNotFoundError,
    PlanAssignmentNotFoundError,
    PlanIntegrityError,
)
from src.domain.plans.ports import (
    PlanCatalogRepositoryPort,
    PlanAssignmentRepositoryPort,
    PlanEntitlementServicePort,
    PlanAuditPort,
)
from src.domain.audit.ports import AuditRepositoryPort
from src.domain.audit.models import (
    AuditRecord,
    AuditRecordType,
    AuditActor,
    AuditActorType,
)
from src.domain.agent_trace.ports import AgentTraceRepositoryPort


class PlanEntitlementService(PlanEntitlementServicePort):
    """
    Servicio central de gobernanza de planes, pricing tiers y entitlements de SaaS.
    """

    def __init__(
        self,
        catalog_repository: PlanCatalogRepositoryPort,
        assignment_repository: PlanAssignmentRepositoryPort,
        quota_policy_repository: Optional[QuotaPolicyRepositoryPort] = None,
        clock: Optional[ClockPort] = None,
        audit_repository: Optional[AuditRepositoryPort] = None,
        agent_trace_repository: Optional[AgentTraceRepositoryPort] = None,
        default_fallback_plan_id: Optional[str] = None,
    ):
        self.catalog_repository = catalog_repository
        self.assignment_repository = assignment_repository
        self.quota_policy_repository = quota_policy_repository
        self.clock = clock
        self.audit_repository = audit_repository
        self.agent_trace_repository = agent_trace_repository
        self.default_fallback_plan_id = default_fallback_plan_id
        self._lock = threading.RLock()

    def _get_now(self) -> datetime:
        if self.clock is not None:
            return self.clock.now()
        return datetime.now(timezone.utc)

    def _emit_audit(
        self,
        record_type: AuditRecordType,
        tenant_id: str,
        actor_id: Optional[str],
        action: str,
        status: str,
        details: Mapping[str, Any],
        correlation_id: Optional[str] = None,
    ) -> None:
        if self.audit_repository is None:
            return
        try:
            rec = AuditRecord(
                audit_id=f"aud_plan_{uuid.uuid4().hex[:12]}",
                occurred_at=self._get_now(),
                actor=AuditActor(
                    actor_type=AuditActorType.USER if actor_id else AuditActorType.SYSTEM,
                    actor_id=actor_id or tenant_id,
                ),
                record_type=record_type,
                subject_type="PLAN_ENTITLEMENT",
                subject_id=tenant_id,
                action_or_operation=action,
                status=status,
                correlation_id=correlation_id or f"corr_{uuid.uuid4().hex[:8]}",
                metadata=dict(details),
            )
            self.audit_repository.append(rec)
        except Exception:
            pass

    def assign_plan(
        self,
        tenant_id: str,
        plan_id: str,
        plan_version: Optional[str] = None,
        effective_from: Optional[datetime] = None,
        effective_until: Optional[datetime] = None,
        source_reason: str = "ADMIN_ASSIGNMENT",
        actor_id: Optional[str] = None,
        context: Optional[TenantContext] = None,
    ) -> PlanAssignment:
        """
        Asigna un plan a un tenant, marca asignaciones anteriores como SUPERSEDED
        y sincroniza determinísticamente la QuotaPolicy en O.7 si está configurado.
        """
        if context is not None:
            CrossTenantGuard.ensure_tenant_context(context)
            CrossTenantGuard.assert_same_tenant(context, tenant_id, operation_name="assign_plan")

        now = self._get_now()
        eff_from = effective_from or now
        if eff_from.tzinfo is None:
            eff_from = eff_from.replace(tzinfo=timezone.utc)

        with self._lock:
            # 1. Validar que el plan existe en catálogo
            plan = self.catalog_repository.get_plan(plan_id, version=plan_version)
            if plan is None:
                raise PlanNotFoundError(f"Plan with id '{plan_id}' (version: {plan_version}) not found in catalog.")

            # 2. Desactivar asignación activa anterior si la hubiere
            existing_active = self.assignment_repository.get_active_assignment(tenant_id, eff_from)
            if existing_active:
                superseded = PlanAssignment(
                    assignment_id=existing_active.assignment_id,
                    tenant_id=existing_active.tenant_id,
                    plan_id=existing_active.plan_id,
                    plan_version=existing_active.plan_version,
                    assigned_at=existing_active.assigned_at,
                    effective_from=existing_active.effective_from,
                    status=PlanAssignmentStatus.SUPERSEDED,
                    effective_until=eff_from,
                    source_reason=f"SUPERSEDED_BY_{plan.plan_id}",
                    assigned_by_actor_id=existing_active.assigned_by_actor_id,
                    metadata=existing_active.metadata,
                )
                self.assignment_repository.save_assignment(superseded)

            # 3. Crear nueva asignación
            assignment_id = f"passign_{uuid.uuid4().hex[:16]}"
            new_assignment = PlanAssignment(
                assignment_id=assignment_id,
                tenant_id=tenant_id,
                plan_id=plan.plan_id,
                plan_version=plan.version,
                assigned_at=now,
                effective_from=eff_from,
                status=PlanAssignmentStatus.ACTIVE,
                effective_until=effective_until,
                source_reason=source_reason,
                assigned_by_actor_id=actor_id,
            )
            self.assignment_repository.save_assignment(new_assignment)

            # 4. Materializar QuotaPolicy en O.7 si está inyectado el repositorio
            if self.quota_policy_repository is not None:
                q_policy = plan.materialize_quota_policy(tenant_id)
                self.quota_policy_repository.save_policy(q_policy)

            # 5. Emitir auditoría
            self._emit_audit(
                record_type=AuditRecordType.ACTION_EXECUTED,
                tenant_id=tenant_id,
                actor_id=actor_id,
                action="PLAN_ASSIGNED",
                status="ACTIVE",
                details={
                    "assignment_id": assignment_id,
                    "plan_id": plan.plan_id,
                    "plan_version": plan.version,
                    "tier": plan.tier.value,
                    "effective_from": eff_from.isoformat(),
                },
            )

            return new_assignment

    def change_plan(
        self,
        tenant_id: str,
        new_plan_id: str,
        new_plan_version: Optional[str] = None,
        effective_from: Optional[datetime] = None,
        reason: str = "PLAN_UPGRADE_OR_DOWNGRADE",
        actor_id: Optional[str] = None,
        context: Optional[TenantContext] = None,
    ) -> PlanAssignment:
        """
        Cambio controlado de plan (upgrade / downgrade). Reutiliza assign_plan preservando historial.
        """
        return self.assign_plan(
            tenant_id=tenant_id,
            plan_id=new_plan_id,
            plan_version=new_plan_version,
            effective_from=effective_from,
            source_reason=reason,
            actor_id=actor_id,
            context=context,
        )

    def get_active_plan(
        self,
        tenant_id: str,
        current_time: Optional[datetime] = None,
        context: Optional[TenantContext] = None,
    ) -> Optional[Plan]:
        """
        Obtiene la definición de Plan activa para el tenant en el instante dado.
        """
        if context is not None:
            CrossTenantGuard.ensure_tenant_context(context)
            CrossTenantGuard.assert_same_tenant(context, tenant_id, operation_name="get_active_plan")

        check_time = current_time or self._get_now()
        with self._lock:
            assignment = self.assignment_repository.get_active_assignment(tenant_id, check_time)
            if assignment is None:
                if self.default_fallback_plan_id:
                    return self.catalog_repository.get_plan(self.default_fallback_plan_id)
                return None

            return self.catalog_repository.get_plan(assignment.plan_id, version=assignment.plan_version)

    def evaluate_entitlement(
        self,
        request: PlanEntitlementRequest,
        context: Optional[TenantContext] = None,
    ) -> PlanEntitlementDecision:
        """
        Evalúa determinísticamente si un tenant tiene entitlement sobre una capability, feature o modelo.
        Fail-Safe: Missing plan o plan no encontrado resulta en UNKNOWN o DENY.
        """
        if context is not None:
            CrossTenantGuard.ensure_tenant_context(context)
            CrossTenantGuard.assert_same_tenant(context, request.tenant_id, operation_name="evaluate_entitlement")

        now = self._get_now()
        decision_id = f"pentdec_{uuid.uuid4().hex[:16]}"
        correlation_id = request.correlation_id or f"pcorr_{uuid.uuid4().hex[:12]}"

        with self._lock:
            active_assignment = self.assignment_repository.get_active_assignment(
                request.tenant_id,
                request.request_timestamp or now,
            )

            if active_assignment is None:
                if self.default_fallback_plan_id:
                    plan = self.catalog_repository.get_plan(self.default_fallback_plan_id)
                    if plan is None:
                        return self._make_fail_safe_decision(
                            decision_id=decision_id,
                            tenant_id=request.tenant_id,
                            reason_code="FALLBACK_PLAN_NOT_FOUND",
                            status=PlanEntitlementStatus.UNKNOWN,
                            correlation_id=correlation_id,
                            now=now,
                            rationale="Fallback plan configured but not found in catalog.",
                        )
                else:
                    return self._make_fail_safe_decision(
                        decision_id=decision_id,
                        tenant_id=request.tenant_id,
                        reason_code="NO_ACTIVE_PLAN_ASSIGNMENT",
                        status=PlanEntitlementStatus.UNKNOWN,
                        correlation_id=correlation_id,
                        now=now,
                        rationale=f"Tenant {request.tenant_id} has no active plan assignment (fail-safe).",
                    )
            else:
                plan = self.catalog_repository.get_plan(
                    active_assignment.plan_id,
                    version=active_assignment.plan_version,
                )
                if plan is None:
                    return self._make_fail_safe_decision(
                        decision_id=decision_id,
                        tenant_id=request.tenant_id,
                        plan_id=active_assignment.plan_id,
                        plan_version=active_assignment.plan_version,
                        reason_code="ASSIGNED_PLAN_VERSION_NOT_FOUND",
                        status=PlanEntitlementStatus.UNKNOWN,
                        correlation_id=correlation_id,
                        now=now,
                        rationale=f"Assigned plan {active_assignment.plan_id} v{active_assignment.plan_version} missing from catalog.",
                    )

            # Evaluar Feature si se solicita
            if request.feature is not None:
                feat_str = request.feature.value if isinstance(request.feature, PlanFeature) else str(request.feature)
                if not plan.has_feature(request.feature):
                    return PlanEntitlementDecision(
                        decision_id=decision_id,
                        status=PlanEntitlementStatus.DENY,
                        tenant_id=request.tenant_id,
                        plan_id=plan.plan_id,
                        plan_version=plan.version,
                        feature=feat_str,
                        is_entitled=False,
                        reason_code="FEATURE_NOT_IN_PLAN",
                        limits_ref=plan.limits,
                        evaluated_at=now,
                        correlation_id=correlation_id,
                        rationale=f"Feature {feat_str} is not enabled in plan {plan.plan_id} (tier {plan.tier.value}).",
                    )

            # Evaluar Model Class / ID si se solicita
            if request.model_id is not None:
                if not plan.is_model_allowed(request.model_id):
                    return PlanEntitlementDecision(
                        decision_id=decision_id,
                        status=PlanEntitlementStatus.DENY,
                        tenant_id=request.tenant_id,
                        plan_id=plan.plan_id,
                        plan_version=plan.version,
                        feature="MODEL_ACCESS",
                        is_entitled=False,
                        reason_code="MODEL_NOT_PERMITTED_BY_PLAN",
                        limits_ref=plan.limits,
                        evaluated_at=now,
                        correlation_id=correlation_id,
                        rationale=f"Model {request.model_id} is not permitted under plan {plan.plan_id}.",
                    )

            # Evaluar Provider si se solicita
            if request.provider is not None:
                if not plan.is_provider_allowed(request.provider):
                    return PlanEntitlementDecision(
                        decision_id=decision_id,
                        status=PlanEntitlementStatus.DENY,
                        tenant_id=request.tenant_id,
                        plan_id=plan.plan_id,
                        plan_version=plan.version,
                        feature="PROVIDER_ACCESS",
                        is_entitled=False,
                        reason_code="PROVIDER_NOT_PERMITTED_BY_PLAN",
                        limits_ref=plan.limits,
                        evaluated_at=now,
                        correlation_id=correlation_id,
                        rationale=f"Provider {request.provider} is not permitted under plan {plan.plan_id}.",
                    )

            # Evaluar Límite de Usuarios si se solicita
            if request.requested_user_count is not None:
                if request.requested_user_count > plan.limits.max_users:
                    return PlanEntitlementDecision(
                        decision_id=decision_id,
                        status=PlanEntitlementStatus.DENY,
                        tenant_id=request.tenant_id,
                        plan_id=plan.plan_id,
                        plan_version=plan.version,
                        feature=PlanFeature.MULTI_USER.value,
                        is_entitled=False,
                        reason_code="MAX_USERS_EXCEEDED",
                        limits_ref=plan.limits,
                        evaluated_at=now,
                        correlation_id=correlation_id,
                        rationale=f"Requested users ({request.requested_user_count}) exceeds plan max ({plan.limits.max_users}).",
                    )

            # Todo concedido
            feat_val = None
            if request.feature is not None:
                feat_val = request.feature.value if isinstance(request.feature, PlanFeature) else str(request.feature)

            return PlanEntitlementDecision(
                decision_id=decision_id,
                status=PlanEntitlementStatus.ALLOW,
                tenant_id=request.tenant_id,
                plan_id=plan.plan_id,
                plan_version=plan.version,
                feature=feat_val,
                is_entitled=True,
                reason_code="PLAN_ENTITLED",
                limits_ref=plan.limits,
                evaluated_at=now,
                correlation_id=correlation_id,
                rationale=f"Plan {plan.plan_id} v{plan.version} allows requested capabilities.",
            )

    def _make_fail_safe_decision(
        self,
        decision_id: str,
        tenant_id: str,
        reason_code: str,
        status: PlanEntitlementStatus,
        correlation_id: str,
        now: datetime,
        rationale: str,
        plan_id: Optional[str] = None,
        plan_version: Optional[str] = None,
    ) -> PlanEntitlementDecision:
        return PlanEntitlementDecision(
            decision_id=decision_id,
            status=status,
            tenant_id=tenant_id,
            plan_id=plan_id,
            plan_version=plan_version,
            feature=None,
            is_entitled=False,
            reason_code=reason_code,
            limits_ref=None,
            evaluated_at=now,
            correlation_id=correlation_id,
            rationale=rationale,
        )
