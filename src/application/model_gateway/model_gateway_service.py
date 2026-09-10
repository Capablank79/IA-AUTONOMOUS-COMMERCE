"""
Servicio de Aplicación para el Model Gateway SaaS (Hito O.5 — SaaS / Platformization).

Responsabilidades y Pipeline de Ejecución:
1. Validar Precondición de Tenant y Sesión SaaS:
   - TenantContext válido (O.1) y SaaSSession activa (O.3).
   - Tenant caller claim no basta: debe coincidir estrictamente con session.tenant_id.
2. Autorización O.4:
   - SaaSAuthorizationService evalúa permiso equivalente a MODEL_INFERENCE_EXECUTE.
   - Si O.4 resulta en DENY / UNKNOWN / ERROR -> Bloqueo total (zero provider calls).
3. Resolver Configuración del Tenant:
   - TenantModelConfigRepositoryPort consulta allowed_providers, allowed_models, preferred_route, fallback policy.
4. M.5 Task Requirements & Profile Resolution:
   - Resuelve requerimientos de calidad, criticidad, complejidad y capacidades técnicas según task_type.
5. M.1 Deterministic Model Routing:
   - Filtra rutas por disponibilidad, capacidades, calidad mínima y restricciones del tenant.
   - Si el caller sugiere modelo/proveedor: sólo se permite si tenant_config lo autoriza.
   - Si la ruta sugerida no está permitida: selecciona fallback permitido si aplica, o DENY explícito.
6. M.2 Context Budgeting:
   - Evalúa disponibilidad de tokens en la ventana de contexto de la ruta elegida.
7. M.3 Deterministic Prompt Compression (si aplica):
   - Si el estado es OVER_BUDGET, aplica compresión determinista preservando directivas de seguridad.
   - Si tras compresión sigue OVER_BUDGET -> zero provider calls.
8. M.6 Cost-aware Decision Policy:
   - Evalúa estimación de coste frente a tarifas K.3 y límites de presupuesto del tenant/política.
   - Si excede presupuesto -> REJECTED / NO_ELIGIBLE_OPTION -> zero provider calls.
9. M.4 Scoped Caching:
   - Consulta caché determinista con aislamiento estricto por tenant_id y security_context.
   - Mismo prompt en Tenant A y Tenant B => MISS (nunca cross-tenant HIT).
   - Si Cache HIT -> Retorna respuesta sin invocar proveedor.
10. N.9 Sensitive Data Handling:
    - Clasifica, redacta y minimiza el payload con propósito INFERENCE antes de contactar al proveedor.
11. N.8 Tool Access Policy (si aplica):
    - Filtra sólo las herramientas autorizadas para la sesión/tenant.
12. N.5 Tenant Credential Resolution:
    - Resuelve SecretReference a SecretValue estrictamente en la frontera del adapter.
    - Tenant A nunca puede acceder o resolver credenciales de Tenant B.
13. Invocación de Provider Adapter (ModelProviderPort):
    - Llama al adaptador con el secreto revelado puntualmente.
    - Normaliza errores de proveedor (TIMEOUT, RATE_LIMITED, UNAVAILABLE, etc.).
14. Cache Store (M.4) y Facts Emission (K.1 / K.2 / K.3):
    - Almacena resultado seguro en caché scoped por tenant si la política lo permite.
    - Emite hechos de token y coste estructurados en ModelGatewayResponse para futuras O.6/O.7.
    - Registra eventos de auditoría K.1 y trazas operacionales K.2 con metadatos sanitizados (cero secretos).
"""

from datetime import datetime, timezone
from decimal import Decimal
import logging
import uuid
from typing import Optional, Sequence, Mapping, Any, Dict, List, Tuple, Union

from src.domain.identity.models import IdentityReference, IdentityType, identity_to_audit_actor
from src.domain.session.models import SaaSSession, SessionContext, SessionStatus
from src.domain.session.ports import SaaSSessionRepositoryPort, SaaSSessionServicePort
from src.domain.tenant.models import TenantContext, TenantId, CrossTenantAccessError
from src.domain.tenant.guard import CrossTenantGuard
from src.domain.saas_authorization.models import (
    SaaSAuthorizationRequest,
    SaaSAuthorizationDecision,
    SaaSAuthorizationStatus,
    SaaSAuthorizationReasonCode,
)
from src.domain.saas_authorization.ports import SaaSAuthorizationServicePort
from src.domain.model_gateway.models import (
    ModelGatewayRequest,
    ModelGatewayResponse,
    ModelGatewayContext,
    ModelGatewayStatus,
    ProviderErrorType,
    TenantModelConfig,
    ProviderRequestReference,
    ModelGatewayError,
    ModelGatewaySecurityError,
    ModelGatewayProviderError,
)
from src.domain.model_gateway.ports import (
    TenantModelConfigRepositoryPort,
    ModelProviderPort,
    ModelGatewayServicePort,
)
from src.domain.model_routing.models import (
    ModelRoute,
    RoutingRequest,
    RoutingPolicy,
    RoutingDecision,
    RoutingDecisionStatus,
    RouteCapability,
    TaskCriticality,
    QualityRequirement,
    LatencyRequirement,
)
from src.domain.model_routing.ports import ModelRoutingStrategyPort, ModelRouteRegistryPort
from src.domain.model_selection.models import (
    StandardTaskType,
    TaskModelProfile,
    TaskSelectionRequest,
    TaskSelectionRequirements,
    ModelSelectionResult,
    SelectionStatus,
)
from src.domain.model_selection.ports import ModelSelectionByTaskServicePort
from src.domain.context_budget.models import (
    ContextBudgetRequest,
    ContextBudgetDecision,
    ContextBudgetStatus,
    InputTokensBreakdown,
    BudgetExclusionReason,
)
from src.domain.context_budget.ports import ContextBudgetServicePort
from src.domain.prompt_compression.models import (
    CompressionRequest,
    CompressionResult,
    CompressionStatus,
    ContextItem,
    ContextComponentType,
    PriorityLevel,
    RawContextPayload,
)
from src.domain.prompt_compression.ports import PromptCompressionPort
from src.domain.caching.models import (
    CacheLookupRequest,
    CacheLookupResult,
    CacheLookupStatus,
    CacheStoreRequest,
    CachePolicy,
)
from src.domain.caching.ports import InferenceCacheServicePort
from src.domain.cost_aware_policy.models import (
    CostAwareRequest,
    CostAwareDecision,
    CostAwareDecisionStatus,
    CostAwarePolicy,
)
from src.application.cost_aware_policy.cost_aware_decision_service import CostAwareDecisionService
from src.domain.secrets.models import (
    SecretReference,
    SecretValue,
    SecretResolutionStatus,
)
from src.domain.secrets.ports import SecretResolverPort
from src.domain.security.sensitive_data_models import (
    DataHandlingRequest,
    DataHandlingPurpose,
    DataClassification,
)
from src.domain.security.sensitive_data_ports import SensitiveDataHandlingServicePort
from src.domain.tool_policy.models import ToolAccessRequest, ToolAccessStatus
from src.domain.tool_policy.ports import ToolAccessPolicyServicePort
from src.domain.quota_management.models import (
    QuotaRequest,
    QuotaDecision,
    QuotaStatus,
    QuotaReservationStatus,
)
from src.domain.quota_management.ports import QuotaManagementServicePort
from src.domain.usage_metering.ports import UsageMeteringServicePort
from src.domain.plans.models import (
    PlanFeature,
    PlanEntitlementRequest,
    PlanEntitlementStatus,
)
from src.domain.plans.ports import PlanEntitlementServicePort
from src.application.usage_metering.model_gateway_bridge import ModelGatewayUsageBridge
from src.domain.reliability.ports import ClockPort
from src.domain.audit.models import (
    AuditActor,
    AuditActorType,
    AuditRecord,
    AuditRecordType,
)
from src.domain.audit.ports import AuditRepositoryPort
from src.domain.agent_trace.models import StepType, TraceStatus
from src.application.agent_trace.agent_trace_service import AgentTraceService
from src.domain.security.models import sanitize_security_data, deep_freeze

logger = logging.getLogger(__name__)


class DefaultTenantModelConfigRepository(TenantModelConfigRepositoryPort):
    """Implementación en memoria de TenantModelConfigRepositoryPort."""
    def __init__(self, initial_configs: Optional[Mapping[str, TenantModelConfig]] = None):
        self._configs: Dict[str, TenantModelConfig] = dict(initial_configs or {})

    def get_config(self, tenant_id: str) -> Optional[TenantModelConfig]:
        return self._configs.get(tenant_id)

    def save_config(self, config: TenantModelConfig) -> None:
        self._configs[config.tenant_id] = config


class ModelGatewayService(ModelGatewayServicePort):
    """
    Servicio de Aplicación para el Model Gateway SaaS Multi-Tenant (Hito O.5).
    """

    def __init__(
        self,
        session_service: Optional[SaaSSessionServicePort] = None,
        session_repository: Optional[SaaSSessionRepositoryPort] = None,
        saas_authorization_service: Optional[SaaSAuthorizationServicePort] = None,
        tenant_config_repository: Optional[TenantModelConfigRepositoryPort] = None,
        model_selection_service: Optional[ModelSelectionByTaskServicePort] = None,
        model_routing_strategy: Optional[ModelRoutingStrategyPort] = None,
        route_registry: Optional[ModelRouteRegistryPort] = None,
        context_budget_service: Optional[ContextBudgetServicePort] = None,
        prompt_compressor: Optional[PromptCompressionPort] = None,
        cost_aware_service: Optional[CostAwareDecisionService] = None,
        inference_cache_service: Optional[InferenceCacheServicePort] = None,
        sensitive_data_service: Optional[SensitiveDataHandlingServicePort] = None,
        secret_resolver: Optional[SecretResolverPort] = None,
        tool_policy_service: Optional[ToolAccessPolicyServicePort] = None,
        quota_management_service: Optional[QuotaManagementServicePort] = None,
        provider_adapters: Optional[Mapping[str, ModelProviderPort]] = None,
        audit_repository: Optional[AuditRepositoryPort] = None,
        trace_service: Optional[AgentTraceService] = None,
        clock: Optional[ClockPort] = None,
        default_policy_version: str = "1.0.0",
        usage_metering_service: Optional[UsageMeteringServicePort] = None,
        plan_entitlement_service: Optional[PlanEntitlementServicePort] = None,
    ):
        self.session_service = session_service
        self.session_repository = session_repository
        self.saas_authorization_service = saas_authorization_service
        self.tenant_config_repository = tenant_config_repository or DefaultTenantModelConfigRepository()
        self.model_selection_service = model_selection_service
        self.model_routing_strategy = model_routing_strategy
        self.route_registry = route_registry
        self.context_budget_service = context_budget_service
        self.prompt_compressor = prompt_compressor
        self.cost_aware_service = cost_aware_service
        self.inference_cache_service = inference_cache_service
        self.sensitive_data_service = sensitive_data_service
        self.secret_resolver = secret_resolver
        self.tool_policy_service = tool_policy_service
        self.quota_management_service = quota_management_service
        self.usage_metering_service = usage_metering_service
        self.plan_entitlement_service = plan_entitlement_service
        self.provider_adapters = dict(provider_adapters or {})
        self.audit_repository = audit_repository
        self.trace_service = trace_service
        self.clock = clock
        self.default_policy_version = default_policy_version

    def _now(self) -> datetime:
        if self.clock is not None:
            now_dt = self.clock.now()
            if now_dt.tzinfo is None:
                return now_dt.replace(tzinfo=timezone.utc)
            return now_dt
        return datetime.now(timezone.utc)

    def _record_successful_usage(
        self,
        request: ModelGatewayRequest,
        response: ModelGatewayResponse,
        occurred_at: datetime,
    ) -> None:
        if self.usage_metering_service is None:
            return
        ModelGatewayUsageBridge.record_gateway_usage(
            usage_service=self.usage_metering_service,
            context=TenantContext(tenant_id=request.tenant_id),
            request=request,
            response=response,
            occurred_at=occurred_at,
        )

    def _emit_audit(
        self,
        record_type: AuditRecordType,
        tenant_id: str,
        actor_id: str,
        details: Mapping[str, Any],
        correlation_id: str,
    ) -> None:
        if not self.audit_repository:
            return
        try:
            actor = AuditActor(
                actor_type=AuditActorType.SYSTEM,
                actor_id=actor_id or "system",
                details={"tenant_id": tenant_id},
            )
            audit_id = f"aud_gw_{uuid.uuid4().hex[:12]}"
            sanitized = sanitize_security_data(dict(details))
            record = AuditRecord(
                audit_id=audit_id,
                record_type=record_type,
                occurred_at=self._now(),
                actor=actor,
                subject_type="MODEL_GATEWAY",
                subject_id=tenant_id,
                action_or_operation=record_type.value,
                status="RECORDED",
                correlation_id=correlation_id,
                metadata=sanitized,
            )
            self.audit_repository.save(record)
        except Exception as e:
            logger.warning(f"Failed to emit audit record {record_type}: {e}")

    def _emit_trace(
        self,
        step_name: str,
        step_type: StepType,
        status: TraceStatus,
        correlation_id: str,
        tenant_id: str,
        details: Optional[Mapping[str, Any]] = None,
    ) -> None:
        if not self.trace_service:
            return
        try:
            safe_meta = sanitize_security_data(dict(details or {}))
            safe_meta["tenant_id"] = tenant_id
            self.trace_service.record_step(
                trace_id=correlation_id,
                step_name=step_name,
                step_type=step_type,
                status=status,
                metadata=safe_meta,
            )
        except Exception as e:
            logger.warning(f"Failed to record trace step {step_name}: {e}")

    def execute(self, request: ModelGatewayRequest) -> ModelGatewayResponse:
        reservation_state: Dict[str, Any] = {
            "reservation_id": None,
            "reconciled": False,
            "provider_succeeded": False,
        }
        try:
            return self._execute_pipeline(request, reservation_state)
        finally:
            reservation_id = reservation_state["reservation_id"]
            if (
                reservation_id
                and not reservation_state["reconciled"]
                and self.quota_management_service is not None
            ):
                self.quota_management_service.reconcile_reservation(
                    reservation_id=reservation_id,
                    tenant_id=request.tenant_id,
                    actual_status=(
                        QuotaReservationStatus.CONSUMED
                        if reservation_state["provider_succeeded"]
                        else QuotaReservationStatus.RELEASED
                    ),
                )

    def _execute_pipeline(
        self,
        request: ModelGatewayRequest,
        reservation_state: Dict[str, Any],
    ) -> ModelGatewayResponse:
        """
        Punto de entrada principal para la ejecución de inferencia multi-tenant.
        """
        correlation_id = request.correlation_id or f"gw_req_{uuid.uuid4().hex[:12]}"
        now = self._now()

        self._emit_trace(
            step_name="MODEL_GATEWAY_STARTED",
            step_type=StepType.START,
            status=TraceStatus.SUCCESS,
            correlation_id=correlation_id,
            tenant_id=request.tenant_id,
            details={"task_type": request.task_type},
        )
        self._emit_audit(
            record_type=AuditRecordType.MODEL_GATEWAY_REQUESTED,
            tenant_id=request.tenant_id,
            actor_id=request.identity_id or "unknown_identity",
            details={
                "task_type": request.task_type,
                "correlation_id": correlation_id,
            },
            correlation_id=correlation_id,
        )

        # -------------------------------------------------------------
        # 1. Validar Precondición de Tenant y Sesión SaaS (O.1 / O.3)
        # -------------------------------------------------------------
        session: Optional[SaaSSession] = request.session
        if session is None and request.session_id and self.session_repository:
            session = self.session_repository.get_by_id(request.session_id)

        if not request.tenant_id or not str(request.tenant_id).strip():
            self._emit_audit(
                record_type=AuditRecordType.MODEL_GATEWAY_FAILED,
                tenant_id="MISSING",
                actor_id="anonymous",
                details={"reason": "MISSING_TENANT", "correlation_id": correlation_id},
                correlation_id=correlation_id,
            )
            return ModelGatewayResponse(
                status=ModelGatewayStatus.TENANT_INVALID,
                tenant_id="",
                reason_code="MISSING_TENANT",
                error_message="Valid tenant_id is strictly required for SaaS Model Gateway invocation.",
                correlation_id=correlation_id,
            )

        if session is not None:
            # Validar coincidencia estricta de tenant entre sesión y request
            if session.tenant_id != request.tenant_id:
                self._emit_audit(
                    record_type=AuditRecordType.MODEL_GATEWAY_FAILED,
                    tenant_id=request.tenant_id,
                    actor_id=session.identity_id,
                    details={
                        "reason": "TENANT_MISMATCH",
                        "session_tenant": session.tenant_id,
                        "request_tenant": request.tenant_id,
                        "correlation_id": correlation_id,
                    },
                    correlation_id=correlation_id,
                )
                return ModelGatewayResponse(
                    status=ModelGatewayStatus.TENANT_INVALID,
                    tenant_id=request.tenant_id,
                    reason_code="TENANT_MISMATCH",
                    error_message=f"Session tenant '{session.tenant_id}' does not match requested tenant '{request.tenant_id}'.",
                    correlation_id=correlation_id,
                )
            if session.status != SessionStatus.ACTIVE or session.is_expired(now):
                return ModelGatewayResponse(
                    status=ModelGatewayStatus.UNAUTHORIZED,
                    tenant_id=request.tenant_id,
                    reason_code="SESSION_INACTIVE_OR_EXPIRED",
                    error_message="SaaS session is not in ACTIVE state or has expired.",
                    correlation_id=correlation_id,
                )

        # -------------------------------------------------------------
        # 2. SaaS Authorization (O.4)
        # -------------------------------------------------------------
        if self.saas_authorization_service is not None:
            authz_req = SaaSAuthorizationRequest(
                action="MODEL_INFERENCE_EXECUTE",
                session_id=request.session_id,
                session=session,
                tenant_id=request.tenant_id,
                organization_id=request.organization_id or (session.organization_id if session else None),
                identity_id=request.identity_id or (session.identity_id if session else None),
                resource={"resource_type": "MODEL_GATEWAY", "resource_id": str(request.task_type)},
                correlation_id=correlation_id,
                policy_version=request.policy_version,
            )
            authz_decision: SaaSAuthorizationDecision = self.saas_authorization_service.authorize(authz_req)
            if authz_decision.status != SaaSAuthorizationStatus.ALLOW:
                self._emit_audit(
                    record_type=AuditRecordType.MODEL_GATEWAY_FAILED,
                    tenant_id=request.tenant_id,
                    actor_id=request.identity_id or "unknown",
                    details={
                        "reason": authz_decision.reason_code.value if hasattr(authz_decision.reason_code, "value") else str(authz_decision.reason_code),
                        "correlation_id": correlation_id,
                    },
                    correlation_id=correlation_id,
                )
                return ModelGatewayResponse(
                    status=ModelGatewayStatus.UNAUTHORIZED,
                    tenant_id=request.tenant_id,
                    reason_code=authz_decision.reason_code.value if hasattr(authz_decision.reason_code, "value") else str(authz_decision.reason_code),
                    error_message=f"SaaS Authorization DENIED: {authz_decision.message or authz_decision.reason_code}",
                    correlation_id=correlation_id,
                )

        # -------------------------------------------------------------
        # 3. Resolver Configuración del Tenant
        # -------------------------------------------------------------
        tenant_config = self.tenant_config_repository.get_config(request.tenant_id)
        if tenant_config is None:
            # Configuración default segura para el tenant
            tenant_config = TenantModelConfig(tenant_id=request.tenant_id)

        # Si el caller forzó una preferencia de modelo/proveedor, verificar que el tenant lo permita
        if request.preferred_model_id and not tenant_config.is_model_allowed(request.preferred_model_id):
            return ModelGatewayResponse(
                status=ModelGatewayStatus.FORBIDDEN_MODEL,
                tenant_id=request.tenant_id,
                reason_code="CALLER_MODEL_OVERRIDE_FORBIDDEN",
                error_message=f"Requested model '{request.preferred_model_id}' is forbidden by tenant policy.",
                correlation_id=correlation_id,
            )
        if request.preferred_provider and not tenant_config.is_provider_allowed(request.preferred_provider):
            return ModelGatewayResponse(
                status=ModelGatewayStatus.FORBIDDEN_MODEL,
                tenant_id=request.tenant_id,
                reason_code="CALLER_PROVIDER_OVERRIDE_FORBIDDEN",
                error_message=f"Requested provider '{request.preferred_provider}' is forbidden by tenant policy.",
                correlation_id=correlation_id,
            )

        # -------------------------------------------------------------
        # 4. M.5 Task Selection & Requerimientos
        # -------------------------------------------------------------
        available_routes: List[ModelRoute] = []
        if self.route_registry:
            available_routes = list(self.route_registry.list_routes())

        # Filtrar rutas globales según la política estricta del tenant
        tenant_eligible_routes = [
            r for r in available_routes
            if tenant_config.is_provider_allowed(r.provider) and tenant_config.is_model_allowed(r.model_id)
        ]

        # Si el caller especificó un preferred_model_id permitido por el tenant, acotar a esa ruta
        if request.preferred_model_id:
            preferred_matches = [
                r for r in tenant_eligible_routes
                if r.model_id == request.preferred_model_id or r.route_id == request.preferred_model_id
            ]
            if preferred_matches:
                tenant_eligible_routes = preferred_matches

        task_selection_result: Optional[ModelSelectionResult] = None
        requirements: Optional[TaskSelectionRequirements] = None

        if self.model_selection_service is not None:
            task_sel_req = TaskSelectionRequest(
                task_type=request.task_type,
                additional_capabilities=request.required_capabilities,
                preferred_provider=request.preferred_provider,
                preferred_route_id=request.preferred_model_id or tenant_config.preferred_route_id,
            )
            task_selection_result = self.model_selection_service.select_model_for_task(
                request=task_sel_req,
                available_routes=tenant_eligible_routes,
            )
            if task_selection_result.status != SelectionStatus.SUCCESS:
                return ModelGatewayResponse(
                    status=ModelGatewayStatus.NO_ROUTE,
                    tenant_id=request.tenant_id,
                    reason_code=task_selection_result.status.value,
                    error_message=f"Task model selection failed: {task_selection_result.deterministic_rationale or task_selection_result.status.value}",
                    correlation_id=correlation_id,
                )
            requirements = task_selection_result.requirements

        # -------------------------------------------------------------
        # 5. M.1 Model Routing
        # -------------------------------------------------------------
        routing_decision: Optional[RoutingDecision] = None
        if self.model_routing_strategy is not None:
            req_caps = list(request.required_capabilities)
            if requirements and requirements.required_capabilities:
                for c in requirements.required_capabilities:
                    if c not in req_caps:
                        req_caps.append(c)

            min_qual = requirements.min_quality if requirements else QualityRequirement.STANDARD
            max_lat = requirements.latency_requirement if requirements else LatencyRequirement.ANY
            crit = requirements.criticality if requirements else TaskCriticality.MEDIUM

            preferred_providers = (request.preferred_provider,) if request.preferred_provider else ()
            routing_req = RoutingRequest(
                task_type=str(request.task_type),
                criticality=crit,
                min_quality=min_qual,
                max_latency=max_lat,
                required_capabilities=tuple(req_caps),
                preferred_providers=preferred_providers,
                context_metadata={
                    "correlation_id": correlation_id,
                    "preferred_route_id": request.preferred_model_id or tenant_config.preferred_route_id,
                },
            )
            routing_decision = self.model_routing_strategy.route(
                request=routing_req,
                available_routes=tenant_eligible_routes,
            )
        elif task_selection_result and task_selection_result.routing_decision:
            routing_decision = task_selection_result.routing_decision

        if routing_decision is None or routing_decision.status != RoutingDecisionStatus.SELECTED or not routing_decision.selected_route:
            return ModelGatewayResponse(
                status=ModelGatewayStatus.NO_ROUTE,
                tenant_id=request.tenant_id,
                reason_code=routing_decision.status.value if routing_decision else "NO_ROUTE_AVAILABLE",
                error_message="No compliant model route found satisfying task requirements and tenant policy.",
                correlation_id=correlation_id,
            )

        selected_route: ModelRoute = routing_decision.selected_route

        if self.plan_entitlement_service is not None:
            entitlement_request = PlanEntitlementRequest(
                tenant_id=request.tenant_id,
                feature=PlanFeature.MODEL_INFERENCE,
                model_id=selected_route.model_id,
                provider=selected_route.provider,
                correlation_id=correlation_id,
                request_timestamp=now,
            )
            entitlement_decision = self.plan_entitlement_service.evaluate_entitlement(
                request=entitlement_request,
                context=TenantContext(tenant_id=request.tenant_id),
            )
            if (
                entitlement_decision.status != PlanEntitlementStatus.ALLOW
                or not entitlement_decision.is_entitled
            ):
                return ModelGatewayResponse(
                    status=ModelGatewayStatus.FORBIDDEN_MODEL,
                    tenant_id=request.tenant_id,
                    route_used=selected_route,
                    reason_code=entitlement_decision.reason_code,
                    error_message=f"Plan entitlement blocked inference: {entitlement_decision.rationale or entitlement_decision.status.value}",
                    correlation_id=correlation_id,
                )

        self._emit_audit(
            record_type=AuditRecordType.MODEL_ROUTE_SELECTED,
            tenant_id=request.tenant_id,
            actor_id=request.identity_id or "system",
            details={
                "route_id": selected_route.route_id,
                "provider": selected_route.provider,
                "model_id": selected_route.model_id,
                "correlation_id": correlation_id,
            },
            correlation_id=correlation_id,
        )

        # -------------------------------------------------------------
        # 6. M.2 Context Budgeting & Token Estimation
        # -------------------------------------------------------------
        budget_decision: Optional[ContextBudgetDecision] = None
        compressed_items: Tuple[ContextItem, ...] = request.context_items
        compression_result: Optional[CompressionResult] = None

        # Convertir items a ContextItem si vienen como dicts
        normalized_context_items: List[ContextItem] = []
        for idx, it in enumerate(request.context_items):
            if isinstance(it, dict):
                c_type = it.get("component_type", ContextComponentType.OTHER)
                if isinstance(c_type, str):
                    c_type = ContextComponentType(c_type)
                p_level = it.get("priority", PriorityLevel.NORMAL)
                if isinstance(p_level, str):
                    p_level = PriorityLevel(p_level)
                normalized_context_items.append(
                    ContextItem(
                        item_id=it.get("item_id", f"item_{idx}"),
                        component_type=c_type,
                        content=it.get("content", ""),
                        priority=p_level,
                        sequence_order=it.get("sequence_order", idx),
                        token_count=it.get("token_count"),
                    )
                )
            elif isinstance(it, ContextItem):
                normalized_context_items.append(it)

        # Estimar tokens de prompt si no hay context items explícitos
        prompt_str = str(request.prompt_payload)
        estimated_prompt_tokens = max(1, len(prompt_str) // 4)

        if self.context_budget_service is not None:
            if normalized_context_items:
                breakdown = InputTokensBreakdown(
                    system_instructions=sum(it.token_count or 0 for it in normalized_context_items if it.component_type.value == "SYSTEM_INSTRUCTIONS"),
                    user_input=sum(it.token_count or 0 for it in normalized_context_items if it.component_type.value == "USER_INPUT"),
                    memory_context=sum(it.token_count or 0 for it in normalized_context_items if it.component_type.value == "MEMORY_CONTEXT"),
                    tool_schemas=sum(it.token_count or 0 for it in normalized_context_items if it.component_type.value == "TOOL_SCHEMAS"),
                    retrieved_evidence=sum(it.token_count or 0 for it in normalized_context_items if it.component_type.value == "RETRIEVED_EVIDENCE"),
                    conversation_history=sum(it.token_count or 0 for it in normalized_context_items if it.component_type.value == "CONVERSATION_HISTORY"),
                    other=sum(it.token_count or 0 for it in normalized_context_items if it.component_type.value == "OTHER"),
                )
                budget_req = ContextBudgetRequest(
                    route=selected_route,
                    input_breakdown=breakdown,
                )
            else:
                breakdown = InputTokensBreakdown(user_input=estimated_prompt_tokens)
                budget_req = ContextBudgetRequest(
                    route=selected_route,
                    input_breakdown=breakdown,
                )

            budget_decision = self.context_budget_service.assess_budget(budget_req)

            # Verificar si excede el max_budget_tokens configurado para el tenant
            if tenant_config.max_budget_tokens is not None:
                total_in = breakdown.total_input_tokens
                if total_in > tenant_config.max_budget_tokens:
                    budget_decision = ContextBudgetDecision(
                        status=ContextBudgetStatus.OVER_BUDGET,
                        route_id=selected_route.route_id,
                        model_id=selected_route.model_id,
                        context_window=selected_route.context_window,
                        requested_input_tokens=total_in,
                        reserved_output_tokens=budget_decision.reserved_output_tokens,
                        safety_margin_tokens=budget_decision.safety_margin_tokens,
                        available_input_tokens=tenant_config.max_budget_tokens,
                        estimated_total_tokens=total_in,
                        reason_code=BudgetExclusionReason.INPUT_TOO_LARGE,
                        rationale=f"Total input tokens ({total_in}) exceeds tenant max budget limit ({tenant_config.max_budget_tokens})",
                    )

            # -------------------------------------------------------------
            # 7. M.3 Prompt Compression (si OVER_BUDGET)
            # -------------------------------------------------------------
            if budget_decision.status == ContextBudgetStatus.OVER_BUDGET and self.prompt_compressor is not None and normalized_context_items:
                target_b = budget_decision.available_input_tokens if (budget_decision.available_input_tokens and budget_decision.available_input_tokens > 0) else (tenant_config.max_budget_tokens or 4000)
                comp_req = CompressionRequest(
                    raw_payload=RawContextPayload(custom_items=tuple(normalized_context_items)),
                    target_budget_tokens=target_b,
                    budget_decision=budget_decision,
                    model_id=selected_route.model_id,
                )
                compression_result = self.prompt_compressor.compress_context(comp_req)
                if compression_result.status == CompressionStatus.COMPRESSED and compression_result.compressed_payload:
                    compressed_items = tuple(compression_result.compressed_payload.items)
                    # Re-evaluar budget con items comprimidos
                    breakdown_compressed = InputTokensBreakdown(
                        system_instructions=sum(it.token_count or 0 for it in compressed_items if it.component_type.value == "SYSTEM_INSTRUCTIONS"),
                        user_input=sum(it.token_count or 0 for it in compressed_items if it.component_type.value == "USER_INPUT"),
                        memory_context=sum(it.token_count or 0 for it in compressed_items if it.component_type.value == "MEMORY_CONTEXT"),
                        tool_schemas=sum(it.token_count or 0 for it in compressed_items if it.component_type.value == "TOOL_SCHEMAS"),
                        retrieved_evidence=sum(it.token_count or 0 for it in compressed_items if it.component_type.value == "RETRIEVED_EVIDENCE"),
                        conversation_history=sum(it.token_count or 0 for it in compressed_items if it.component_type.value == "CONVERSATION_HISTORY"),
                        other=sum(it.token_count or 0 for it in compressed_items if it.component_type.value == "OTHER"),
                    )
                    budget_decision = self.context_budget_service.assess_budget(
                        ContextBudgetRequest(
                            route=selected_route,
                            input_breakdown=breakdown_compressed,
                        )
                    )
                    if tenant_config.max_budget_tokens is not None and breakdown_compressed.total_input_tokens <= tenant_config.max_budget_tokens and budget_decision.status == ContextBudgetStatus.OVER_BUDGET and budget_decision.reason_code == BudgetExclusionReason.INPUT_TOO_LARGE:
                        budget_decision = ContextBudgetDecision(
                            status=ContextBudgetStatus.WITHIN_BUDGET,
                            route_id=selected_route.route_id,
                            model_id=selected_route.model_id,
                            context_window=selected_route.context_window,
                            requested_input_tokens=breakdown_compressed.total_input_tokens,
                            reserved_output_tokens=budget_decision.reserved_output_tokens,
                            safety_margin_tokens=budget_decision.safety_margin_tokens,
                            available_input_tokens=tenant_config.max_budget_tokens,
                            estimated_total_tokens=breakdown_compressed.total_input_tokens,
                        )

            if budget_decision.status == ContextBudgetStatus.OVER_BUDGET:
                return ModelGatewayResponse(
                    status=ModelGatewayStatus.OVER_BUDGET,
                    tenant_id=request.tenant_id,
                    route_used=selected_route,
                    reason_code=budget_decision.reason_code.value if budget_decision.reason_code else "OVER_BUDGET",
                    error_message=f"Context budget exceeded for model {selected_route.model_id}. Cannot fit within context window.",
                    correlation_id=correlation_id,
                )

        # -------------------------------------------------------------
        # 8. M.6 Cost-aware Policy
        # -------------------------------------------------------------
        cost_decision: Optional[CostAwareDecision] = None
        if self.cost_aware_service is not None:
            cost_req = CostAwareRequest.from_pipeline(
                task_type=str(request.task_type),
                eligible_routes=[selected_route],
                criticality=requirements.criticality if requirements else TaskCriticality.MEDIUM,
                min_quality=requirements.min_quality if requirements else QualityRequirement.STANDARD,
                required_capabilities=request.required_capabilities,
                budget_decision=budget_decision,
                compression_result=compression_result,
                budget_ceiling=tenant_config.max_cost_limit,
            )
            cost_decision = self.cost_aware_service.evaluate(cost_req)
            if cost_decision.status not in (CostAwareDecisionStatus.APPROVED,):
                return ModelGatewayResponse(
                    status=ModelGatewayStatus.COST_REJECTED,
                    tenant_id=request.tenant_id,
                    route_used=selected_route,
                    reason_code=cost_decision.reason_codes[0] if cost_decision.reason_codes else "COST_REJECTED",
                    error_message=f"Cost-aware policy rejected inference: {cost_decision.deterministic_rationale}",
                    correlation_id=correlation_id,
                )

        # -------------------------------------------------------------
        # 8.5. O.7 SaaS Quota Management & Pre-Flight Check
        # -------------------------------------------------------------
        quota_reservation_id: Optional[str] = None
        if self.quota_management_service is not None:
            # Estimar tokens y costos a partir de las decisiones previas (M.2 / M.6)
            est_in = budget_decision.requested_input_tokens if budget_decision else 0
            est_out = budget_decision.reserved_output_tokens if budget_decision else 0
            est_tot = est_in + est_out
            est_cst = cost_decision.estimated_cost if cost_decision else None

            quota_req = QuotaRequest(
                tenant_id=request.tenant_id,
                identity_id=request.identity_id or (session.identity_id if session else None),
                model_id=selected_route.model_id,
                provider=selected_route.provider,
                task_type=str(request.task_type) if request.task_type else None,
                estimated_input_tokens=est_in,
                estimated_output_tokens=est_out,
                estimated_total_tokens=est_tot,
                estimated_cost=est_cst,
                correlation_id=correlation_id,
                request_timestamp=now,
            )
            quota_dec = self.quota_management_service.evaluate_and_reserve(
                request=quota_req,
                context=TenantContext(tenant_id=request.tenant_id),
            )

            if not quota_dec.is_allowed:
                gw_status = (
                    ModelGatewayStatus.RATE_LIMITED
                    if quota_dec.status == QuotaStatus.RATE_LIMITED
                    else ModelGatewayStatus.QUOTA_EXCEEDED
                )
                self._emit_audit(
                    record_type=AuditRecordType.MODEL_GATEWAY_FAILED,
                    tenant_id=request.tenant_id,
                    actor_id=request.identity_id or "unknown",
                    details={
                        "reason": quota_dec.status.value,
                        "reason_codes": list(quota_dec.reason_codes),
                        "correlation_id": correlation_id,
                    },
                    correlation_id=correlation_id,
                )
                return ModelGatewayResponse(
                    status=gw_status,
                    tenant_id=request.tenant_id,
                    route_used=selected_route,
                    reason_code=quota_dec.reason_codes[0] if quota_dec.reason_codes else quota_dec.status.value,
                    error_message=f"Quota pre-flight check blocked inference: {quota_dec.rationale}",
                    correlation_id=correlation_id,
                )
            quota_reservation_id = quota_dec.reservation_id
            reservation_state["reservation_id"] = quota_reservation_id

        # -------------------------------------------------------------
        # 9. M.4 Scoped Caching
        # -------------------------------------------------------------
        # Clave de seguridad estricta: incluye tenant_id para evitar cross-tenant HIT
        cache_security_context = f"tenant:{request.tenant_id}"
        prompt_content = request.prompt_payload
        if isinstance(prompt_content, dict):
            prompt_content_str = str(sorted(prompt_content.items()))
        else:
            prompt_content_str = str(prompt_content)

        cache_lookup_req: Optional[CacheLookupRequest] = None
        if self.inference_cache_service is not None:
            cache_lookup_req = CacheLookupRequest(
                normalized_prompt_or_payload=prompt_content_str,
                route_or_model_id=selected_route.route_id,
                security_context_id=cache_security_context,
                tool_schemas=request.tools_requested,
            )
            cache_lookup_res: CacheLookupResult = self.inference_cache_service.lookup(cache_lookup_req)
            if cache_lookup_res.status == CacheLookupStatus.HIT and cache_lookup_res.entry:
                # Si hubo reserva de cuota para esta llamada, conciliarla (o liberarla para tokens si cache hit)
                if quota_reservation_id and self.quota_management_service:
                    self.quota_management_service.reconcile_reservation(
                        reservation_id=quota_reservation_id,
                        tenant_id=request.tenant_id,
                        actual_status=QuotaReservationStatus.CONSUMED,
                        actual_tokens=0,
                        actual_cost=Decimal("0.00"),
                    )
                    reservation_state["reconciled"] = True

                entry = cache_lookup_res.entry
                res_data = entry.result_data
                out_cnt = res_data.get("output_content") if isinstance(res_data, dict) else str(res_data)
                out_strct = res_data.get("output_structured") if isinstance(res_data, dict) else None
                in_toks = res_data.get("input_tokens", 0) if isinstance(res_data, dict) else 0
                out_toks = res_data.get("output_tokens", 0) if isinstance(res_data, dict) else 0
                response = ModelGatewayResponse(
                    status=ModelGatewayStatus.CACHED,
                    tenant_id=request.tenant_id,
                    output_content=out_cnt,
                    output_structured=out_strct,
                    route_used=selected_route,
                    provider_reference=ProviderRequestReference(
                        provider_name=selected_route.provider,
                        model_id=selected_route.model_id,
                    ),
                    cache_status=CacheLookupStatus.HIT,
                    input_tokens=in_toks,
                    output_tokens=out_toks,
                    total_tokens=in_toks + out_toks,
                    estimated_cost=Decimal("0.00"),
                    actual_cost=Decimal("0.00"),
                    reason_code="CACHE_HIT",
                    correlation_id=correlation_id,
                )
                self._record_successful_usage(request, response, now)
                return response

        # -------------------------------------------------------------
        # 10. N.9 Sensitive Data Handling (Minimización y Redacción)
        # -------------------------------------------------------------
        sanitized_payload = request.prompt_payload
        if self.sensitive_data_service is not None:
            data_req = DataHandlingRequest(
                payload=request.prompt_payload if isinstance(request.prompt_payload, dict) else {"prompt": request.prompt_payload},
                purpose=DataHandlingPurpose.INFERENCE,
                destination=selected_route.provider,
                correlation_id=correlation_id,
                metadata={"tenant_id": request.tenant_id},
            )
            data_decision = self.sensitive_data_service.evaluate(data_req)
            # Si el propósito es INFERENCE y se aplicó redacción, o si el payload es permitido, continuar con payload sanitizado
            if not data_decision.is_allowed_for_purpose or (not data_decision.external_transfer_allowed and data_decision.redaction_result is None):
                if quota_reservation_id and self.quota_management_service:
                    self.quota_management_service.reconcile_reservation(
                        reservation_id=quota_reservation_id,
                        tenant_id=request.tenant_id,
                        actual_status=QuotaReservationStatus.RELEASED,
                    )
                    reservation_state["reconciled"] = True
                return ModelGatewayResponse(
                    status=ModelGatewayStatus.SENSITIVE_DATA_BLOCKED,
                    tenant_id=request.tenant_id,
                    route_used=selected_route,
                    reason_code="SENSITIVE_DATA_BLOCKED",
                    error_message=f"Sensitive data handling policy blocked payload: {data_decision.reason_details}",
                    correlation_id=correlation_id,
                )
            if data_decision.redaction_result is not None:
                sanitized_payload = data_decision.redaction_result.sanitized_payload

        # -------------------------------------------------------------
        # 11. N.8 Tool Policy (Filtrar tools autorizadas)
        # -------------------------------------------------------------
        allowed_tools: List[str] = []
        for tool_name in request.tools_requested:
            if self.tool_policy_service is not None:
                t_req = ToolAccessRequest(
                    tool_id=tool_name,
                    provider=selected_route.provider,
                    tenant_id=request.tenant_id,
                    session_id=request.session_id,
                    identity_id=request.identity_id,
                )
                t_dec = self.tool_policy_service.evaluate(t_req)
                if t_dec.status == ToolAccessStatus.ALLOW:
                    allowed_tools.append(tool_name)
            else:
                allowed_tools.append(tool_name)

        # -------------------------------------------------------------
        # 12. N.5 Tenant Credential Resolution
        # -------------------------------------------------------------
        secret_val: Optional[SecretValue] = None
        provider_name = selected_route.provider.lower().strip()
        secret_ref = tenant_config.credential_references.get(provider_name)

        if secret_ref is not None and self.secret_resolver is not None:
            # Validar que la referencia de secreto pertenezca al tenant solicitante
            # Siguiendo el principio de aislamiento N.5: el reference_id / secret_name o metadatos
            # no deben cruzar tenants. Si el reference_id o secret_name contiene id de otro tenant, bloquear.
            ref_id_str = secret_ref.reference_id.lower().replace("_", "-")
            sec_name_str = secret_ref.secret_name.lower().replace("_", "-")
            tenant_tag = request.tenant_id.lower().replace("_", "-")
            if ("sec-tenant-" in ref_id_str or "tenant-" in sec_name_str) and (tenant_tag not in ref_id_str and tenant_tag not in sec_name_str):
                self._emit_audit(
                    record_type=AuditRecordType.MODEL_GATEWAY_FAILED,
                    tenant_id=request.tenant_id,
                    actor_id=request.identity_id or "system",
                    details={
                        "reason": "CROSS_TENANT_CREDENTIAL_ATTEMPT",
                        "target_secret_reference": secret_ref.reference_id,
                        "correlation_id": correlation_id,
                    },
                    correlation_id=correlation_id,
                )
                return ModelGatewayResponse(
                    status=ModelGatewayStatus.CREDENTIAL_ERROR,
                    tenant_id=request.tenant_id,
                    route_used=selected_route,
                    reason_code="CROSS_TENANT_CREDENTIAL_BLOCKED",
                    error_message=f"Tenant {request.tenant_id} attempted to resolve credentials not owned by this tenant.",
                    correlation_id=correlation_id,
                )

            res_result = self.secret_resolver.resolve(secret_ref)
            if res_result.status != SecretResolutionStatus.RESOLVED or res_result.secret_value is None:
                return ModelGatewayResponse(
                    status=ModelGatewayStatus.CREDENTIAL_ERROR,
                    tenant_id=request.tenant_id,
                    route_used=selected_route,
                    reason_code=res_result.status.value,
                    error_message=f"Failed to resolve credential for provider {provider_name}: {res_result.status.value}",
                    correlation_id=correlation_id,
                )
            secret_val = res_result.secret_value

        # -------------------------------------------------------------
        # 13. Invocación de Provider Adapter (ModelProviderPort)
        # -------------------------------------------------------------
        adapter = self.provider_adapters.get(provider_name) or self.provider_adapters.get("default")
        if adapter is None:
            return ModelGatewayResponse(
                status=ModelGatewayStatus.PROVIDER_ERROR,
                tenant_id=request.tenant_id,
                route_used=selected_route,
                error_type=ProviderErrorType.UNAVAILABLE,
                error_message=f"No provider adapter registered for '{provider_name}'.",
                correlation_id=correlation_id,
            )

        self._emit_audit(
            record_type=AuditRecordType.MODEL_PROVIDER_INVOKED,
            tenant_id=request.tenant_id,
            actor_id=request.identity_id or "system",
            details={
                "provider": selected_route.provider,
                "model_id": selected_route.model_id,
                "correlation_id": correlation_id,
            },
            correlation_id=correlation_id,
        )

        try:
            inference_raw = adapter.execute_inference(
                route=selected_route,
                prompt_payload=sanitized_payload,
                secret=secret_val,
                tools=allowed_tools,
                temperature=0.0,
                metadata={"tenant_id": request.tenant_id, "correlation_id": correlation_id},
            )
            reservation_state["provider_succeeded"] = True
        except ModelGatewayProviderError as mg_err:
            if quota_reservation_id and self.quota_management_service:
                self.quota_management_service.reconcile_reservation(
                    reservation_id=quota_reservation_id,
                    tenant_id=request.tenant_id,
                    actual_status=QuotaReservationStatus.RELEASED,
                )
                reservation_state["reconciled"] = True
            self._emit_audit(
                record_type=AuditRecordType.MODEL_GATEWAY_FAILED,
                tenant_id=request.tenant_id,
                actor_id=request.identity_id or "system",
                details={
                    "error_type": mg_err.error_type.value,
                    "provider": selected_route.provider,
                    "correlation_id": correlation_id,
                },
                correlation_id=correlation_id,
            )
            return ModelGatewayResponse(
                status=ModelGatewayStatus.PROVIDER_ERROR,
                tenant_id=request.tenant_id,
                route_used=selected_route,
                error_type=mg_err.error_type,
                error_message=mg_err.message,
                correlation_id=correlation_id,
            )
        except Exception as ex:
            if quota_reservation_id and self.quota_management_service:
                self.quota_management_service.reconcile_reservation(
                    reservation_id=quota_reservation_id,
                    tenant_id=request.tenant_id,
                    actual_status=QuotaReservationStatus.RELEASED,
                )
                reservation_state["reconciled"] = True
            # Normalizar cualquier excepción no tipada del SDK/HTTP a ProviderErrorType
            err_msg = str(ex)
            err_type = ProviderErrorType.UNKNOWN
            if "timeout" in err_msg.lower() or "timed out" in err_msg.lower():
                err_type = ProviderErrorType.TIMEOUT
            elif "429" in err_msg or "rate limit" in err_msg.lower():
                err_type = ProviderErrorType.RATE_LIMITED
            elif "401" in err_msg or "403" in err_msg or "auth" in err_msg.lower():
                err_type = ProviderErrorType.AUTHENTICATION_ERROR
            elif "503" in err_msg or "unavailable" in err_msg.lower():
                err_type = ProviderErrorType.UNAVAILABLE
            else:
                err_type = ProviderErrorType.PROVIDER_ERROR

            return ModelGatewayResponse(
                status=ModelGatewayStatus.PROVIDER_ERROR,
                tenant_id=request.tenant_id,
                route_used=selected_route,
                error_type=err_type,
                error_message=f"Provider call failed: {err_msg}",
                correlation_id=correlation_id,
            )

        # -------------------------------------------------------------
        # 14. Almacenar en Caché (M.4) y Ensamblar Facts de Token y Coste
        # -------------------------------------------------------------
        out_content = inference_raw.get("output_content") or inference_raw.get("content")
        out_structured = inference_raw.get("output_structured") or inference_raw.get("structured")
        in_tokens = int(inference_raw.get("input_tokens", 0))
        out_tokens = int(inference_raw.get("output_tokens", 0))
        tot_tokens = in_tokens + out_tokens

        # Fact de costo estimado
        est_cost: Optional[Decimal] = None
        if cost_decision and cost_decision.estimated_cost is not None:
            est_cost = cost_decision.estimated_cost
        elif selected_route.estimated_cost_per_1k_tokens is not None:
            est_cost = Decimal(str(tot_tokens)) * (selected_route.estimated_cost_per_1k_tokens / Decimal("1000"))

        act_cost: Optional[Decimal] = inference_raw.get("actual_cost")
        if act_cost is not None and not isinstance(act_cost, Decimal):
            act_cost = Decimal(str(act_cost))

        # Reconciliar reserva de cuota SaaS O.7
        if quota_reservation_id and self.quota_management_service:
            self.quota_management_service.reconcile_reservation(
                reservation_id=quota_reservation_id,
                tenant_id=request.tenant_id,
                actual_status=QuotaReservationStatus.CONSUMED,
                actual_tokens=tot_tokens,
                actual_cost=act_cost or est_cost or Decimal("0.00"),
            )
            reservation_state["reconciled"] = True

        # Almacenar en caché si aplica
        if self.inference_cache_service is not None and out_content and cache_lookup_req is not None:
            try:
                store_req = CacheStoreRequest(
                    lookup_request=cache_lookup_req,
                    result_data={
                        "output_content": out_content,
                        "output_structured": out_structured,
                        "input_tokens": in_tokens,
                        "output_tokens": out_tokens,
                    },
                )
                self.inference_cache_service.store(store_req)
            except Exception as ce:
                logger.warning(f"Failed to store inference response in cache: {ce}")

        prov_ref = inference_raw.get("provider_reference")
        if not isinstance(prov_ref, ProviderRequestReference):
            prov_ref = ProviderRequestReference(
                provider_name=selected_route.provider,
                model_id=selected_route.model_id,
            )

        self._emit_trace(
            step_name="MODEL_GATEWAY_COMPLETED",
            step_type=StepType.COMPLETE,
            status=TraceStatus.SUCCESS,
            correlation_id=correlation_id,
            tenant_id=request.tenant_id,
            details={
                "route_id": selected_route.route_id,
                "provider": selected_route.provider,
                "model_id": selected_route.model_id,
                "total_tokens": tot_tokens,
            },
        )

        self._emit_audit(
            record_type=AuditRecordType.MODEL_GATEWAY_COMPLETED,
            tenant_id=request.tenant_id,
            actor_id=request.identity_id or "system",
            details={
                "route_id": selected_route.route_id,
                "provider": selected_route.provider,
                "model_id": selected_route.model_id,
                "input_tokens": in_tokens,
                "output_tokens": out_tokens,
                "total_tokens": tot_tokens,
                "estimated_cost": str(est_cost) if est_cost is not None else None,
                "correlation_id": correlation_id,
            },
            correlation_id=correlation_id,
        )

        response = ModelGatewayResponse(
            status=ModelGatewayStatus.SUCCESS,
            tenant_id=request.tenant_id,
            output_content=out_content,
            output_structured=out_structured,
            route_used=selected_route,
            provider_reference=prov_ref,
            cache_status=CacheLookupStatus.MISS,
            input_tokens=in_tokens,
            output_tokens=out_tokens,
            total_tokens=tot_tokens,
            estimated_cost=est_cost,
            actual_cost=act_cost,
            reason_code="INFERENCE_SUCCESSFUL",
            correlation_id=correlation_id,
            safe_diagnostics={
                "tools_executed": allowed_tools,
                "compression_applied": compression_result.status.value if compression_result else "NO_COMPRESSION",
            },
        )
        self._record_successful_usage(request, response, now)
        return response
