"""
Servicio de Aplicación para Q.5 — Agent Cost Dashboard (Hito Q — Business Intelligence).

Responsabilidades y Principios:
1. DASHBOARD != BILLING ENGINE:
   - Estrictamente consultivo, proyectivo y explicable.
   - Cero facturación SaaS (O.9), cero alteración de suscripciones, cuotas (O.7) o budgets (M.1-M.6).
2. STRICT TENANT ISOLATION (O.1):
   - Todas las consultas operan bajo TenantContext verificado. Cero fugas cross-tenant.
3. SAAS AUTHENTICATION & RBAC (O.3 / O.4 / N.4):
   - Valida sesión activa y permisos AGENT_COST_DASHBOARD_READ o BUSINESS_INTELLIGENCE_READ.
4. MONEY PRECISION (Decimal):
   - Todos los costos usan Decimal con divisa explícita. Cero floats.
5. MULTI-CURRENCY SAFETY:
   - Los totales agregados se desglosan por divisa. Prohibido sumar monedas heterogéneas sin FX canónico.
6. UNKNOWN != 0:
   - Si un evento no tiene tokens o precio/costo conocido, se preserva como UNKNOWN sin convertir a 0.00.
7. COST ATTRIBUTION:
   - Los costos sólo se atribuyen a misiones/agentes si existe correlación real. Sin correlation => unattributed.
8. SENSITIVE DATA DEFENSE (N.9) & NO PRIVATE CoT:
   - Excluye tokens de sesión, claves de API, contraseñas, razonamiento interno y CoT.
9. DETERMINISMO Y ORDENACIÓN:
   - Ordenación determinista con regla de desempate estable por occurred_at y item_id.
10. PAGINACIÓN ACOTADA:
    - Paginación obligatoria y límites estrictos de tamaño de página (máximo 100).
11. INTEGRACIÓN CON SOURCE OF TRUTH:
    - Consulta hechos de consumo desde UsageEventRepositoryPort (O.6) y CostRepositoryPort (K.3).
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, List, Dict, Any, Tuple, Sequence, Mapping
from types import MappingProxyType
import math

from src.domain.tenant.models import TenantContext
from src.domain.tenant.guard import CrossTenantGuard
from src.domain.security.models import validate_safe_identifier, sanitize_security_data
from src.domain.agent_cost_dashboard.models import (
    AgentCostSortField,
    SortOrder,
    CostConfidenceSource,
    AgentCostDashboardItem,
    CurrencyCostBreakdown,
    DimensionCostBreakdown,
    MissionCostSummaryItem,
    AgentCostDashboardSummary,
    AgentCostDashboardQuery,
    AgentCostDashboardPage,
)
from src.domain.agent_cost_dashboard.ports import AgentCostDashboardServicePort
from src.domain.usage_metering.ports import UsageEventRepositoryPort
from src.domain.usage_metering.models import (
    UsageEvent,
    UsageQuery,
    UsagePeriod,
    UsagePeriodType,
    UsageRequestStatus,
)
from src.domain.cost.ports import CostRepositoryPort, PricingCatalogPort
from src.domain.cost.models import CostRecord, CostType, UsageUnit
from src.domain.saas_authorization.models import (
    SaaSAuthorizationRequest,
    SaaSAuthorizationStatus,
    SaaSAuthorizationReasonCode,
)
from src.application.saas_authorization.saas_authorization_service import SaaSAuthorizationService
from src.domain.session.ports import SaaSSessionRepositoryPort
from src.domain.session.models import SessionStatus
from src.domain.admin_console.models import (
    AdminAuthenticationError,
    AdminAuthorizationError,
    AdminResourceNotFoundError,
    AdminInvalidRequestError,
)
from src.domain.reliability.ports import ClockPort
from src.infrastructure.reliability.reliability_infrastructure import SystemClock


class AgentCostAuthenticationError(AdminAuthenticationError):
    """Lanzada cuando la sesión es inválida, expirada o ausente."""
    pass


class AgentCostAuthorizationError(AdminAuthorizationError):
    """Lanzada cuando el usuario o tenant no tiene permisos para ver el dashboard de costos de agentes."""
    pass


class AgentCostNotFoundError(AdminResourceNotFoundError):
    """Lanzada cuando el registro de costo no existe en el tenant especificado."""
    pass


class AgentCostInvalidRequestError(AdminInvalidRequestError):
    """Lanzada cuando los parámetros de consulta de costos son inválidos."""
    pass


SENSITIVE_KEYS = {
    "password", "secret", "token", "api_key", "apikey", "pan", "cvv",
    "private_key", "credential", "access_token", "refresh_token", "authorization",
    "chain_of_thought", "reasoning", "reasoning_tokens", "internal_scratchpad",
    "card_number", "bearer", "auth_header",
}


def _sanitize_dict(data: Any) -> Any:
    """Sanitiza recursivamente metadatos para vistas de BI eliminando secretos y CoT."""
    if isinstance(data, (dict, MappingProxyType)):
        clean = {}
        for k, v in data.items():
            k_str = str(k).lower()
            if any(s in k_str for s in SENSITIVE_KEYS) and not k_str.endswith("_tokens"):
                continue
            clean[str(k)] = _sanitize_dict(v)
        return clean
    elif isinstance(data, (list, tuple, set)):
        return [_sanitize_dict(item) for item in data]
    return data


class AgentCostDashboardService(AgentCostDashboardServicePort):
    """
    Servicio de Aplicación para el Agent Cost Dashboard (Q.5).
    """

    def __init__(
        self,
        usage_repository: Optional[UsageEventRepositoryPort] = None,
        cost_repository: Optional[CostRepositoryPort] = None,
        pricing_catalog: Optional[PricingCatalogPort] = None,
        authorization_service: Optional[SaaSAuthorizationService] = None,
        session_repository: Optional[SaaSSessionRepositoryPort] = None,
        clock: Optional[ClockPort] = None,
    ):
        self.usage_repository = usage_repository
        self.cost_repository = cost_repository
        self.pricing_catalog = pricing_catalog
        self.authorization_service = authorization_service
        self.session_repository = session_repository
        self.clock = clock or SystemClock()

    def _authenticate_and_authorize(
        self,
        tenant_id: str,
        session_id: Optional[str],
        action: str = "AGENT_COST_DASHBOARD_READ",
        resource_id: Optional[str] = None,
    ) -> TenantContext:
        """Valida autenticación de sesión y autorización RBAC para el tenant objetivo."""
        validate_safe_identifier(tenant_id, field_name="tenant_id")

        if self.authorization_service is not None:
            if not session_id:
                raise AgentCostAuthenticationError("Authentication required: missing session_id")

            # Verificar permiso primario o fallback general de BI
            auth_req = SaaSAuthorizationRequest(
                action=action,
                session_id=session_id,
                tenant_id=tenant_id,
                resource=resource_id,
            )
            decision = self.authorization_service.authorize(auth_req)
            if not decision.is_allowed:
                # Si falló, intentar fallback a BUSINESS_INTELLIGENCE_READ
                if action != "BUSINESS_INTELLIGENCE_READ":
                    fallback_req = SaaSAuthorizationRequest(
                        action="BUSINESS_INTELLIGENCE_READ",
                        session_id=session_id,
                        tenant_id=tenant_id,
                        resource=resource_id,
                    )
                    fallback_decision = self.authorization_service.authorize(fallback_req)
                    if fallback_decision.is_allowed:
                        return TenantContext(tenant_id=tenant_id)

                auth_reasons = {
                    SaaSAuthorizationReasonCode.SESSION_INVALID,
                    SaaSAuthorizationReasonCode.SESSION_NOT_FOUND,
                    SaaSAuthorizationReasonCode.SESSION_EXPIRED,
                    SaaSAuthorizationReasonCode.SESSION_REVOKED,
                }
                if decision.reason_code in auth_reasons:
                    raise AgentCostAuthenticationError(
                        f"Authentication failed: {decision.reason_code.value}"
                    )
                raise AgentCostAuthorizationError(
                    f"Access denied for action '{action}' on tenant '{tenant_id}': {decision.reason_code.value}"
                )

        elif self.session_repository is not None and session_id:
            session = self.session_repository.get_by_id(session_id)
            if not session:
                raise AgentCostAuthenticationError(f"Session '{session_id}' not found")
            now = self.clock.now()
            if session.is_expired(now):
                raise AgentCostAuthenticationError(f"Session '{session_id}' has expired")
            if session.status != SessionStatus.ACTIVE:
                raise AgentCostAuthenticationError(f"Session '{session_id}' is not active")
            if session.tenant_id != tenant_id:
                raise AgentCostAuthorizationError(
                    f"Cross-tenant access violation: session tenant '{session.tenant_id}' does not match target tenant '{tenant_id}'"
                )

        return TenantContext(tenant_id=tenant_id)

    def _convert_usage_event_to_item(self, event: UsageEvent) -> AgentCostDashboardItem:
        """Proyecta un UsageEvent a un AgentCostDashboardItem seguro."""
        details_clean = _sanitize_dict(dict(event.details))

        # Atribución
        mission_id = details_clean.get("mission_id") or event.details.get("mission_id")
        agent_type = details_clean.get("agent_type") or details_clean.get("agent_name") or event.details.get("agent_type") or event.details.get("agent_name")
        execution_id = details_clean.get("execution_id") or event.details.get("execution_id")

        # Validar identificador de mission_id si viene
        safe_mission_id = None
        if mission_id and isinstance(mission_id, str):
            try:
                validate_safe_identifier(mission_id, field_name="mission_id")
                safe_mission_id = mission_id
            except Exception:
                safe_mission_id = None

        safe_execution_id = None
        if execution_id and isinstance(execution_id, str):
            try:
                validate_safe_identifier(execution_id, field_name="execution_id")
                safe_execution_id = execution_id
            except Exception:
                safe_execution_id = None

        is_attributed = safe_mission_id is not None or agent_type is not None

        # Costo y certeza
        total_cost = event.actual_cost if event.actual_cost is not None else event.estimated_cost
        is_known_cost = total_cost is not None

        if event.actual_cost is not None:
            cost_source = CostConfidenceSource.ACTUAL_RECORDED
        elif event.estimated_cost is not None:
            cost_source = CostConfidenceSource.ESTIMATED_METERED
        else:
            cost_source = CostConfidenceSource.UNKNOWN_UNPRICED

        # Unknown fields tracking
        unknown_fields: List[str] = []
        if event.input_tokens is None:
            unknown_fields.append("input_tokens")
        if event.output_tokens is None:
            unknown_fields.append("output_tokens")
        if event.total_tokens is None:
            unknown_fields.append("total_tokens")
        if total_cost is None:
            unknown_fields.append("total_cost")
        if not is_attributed:
            unknown_fields.append("attribution")

        cached_tokens = details_clean.get("cached_tokens")
        if cached_tokens is not None:
            try:
                cached_tokens = int(cached_tokens)
            except Exception:
                cached_tokens = None

        input_tokens = event.input_tokens
        if input_tokens is None and "input_tokens" in details_clean:
            try:
                input_tokens = int(details_clean["input_tokens"])
            except Exception:
                input_tokens = None

        output_tokens = event.output_tokens
        if output_tokens is None and "output_tokens" in details_clean:
            try:
                output_tokens = int(details_clean["output_tokens"])
            except Exception:
                output_tokens = None

        total_tokens = event.total_tokens
        if total_tokens is None and "total_tokens" in details_clean:
            try:
                total_tokens = int(details_clean["total_tokens"])
            except Exception:
                total_tokens = None
        if total_tokens is None and input_tokens is not None and output_tokens is not None:
            total_tokens = input_tokens + output_tokens

        currency = details_clean.get("currency", "USD")

        return AgentCostDashboardItem(
            item_id=event.usage_event_id,
            tenant_id=event.tenant_id,
            occurred_at=event.occurred_at,
            request_status=event.request_status.value if hasattr(event.request_status, "value") else str(event.request_status),
            is_known_cost=is_known_cost,
            is_attributed=is_attributed,
            provider=event.provider,
            model=event.model,
            agent_type=str(agent_type) if agent_type is not None else None,
            mission_id=safe_mission_id,
            execution_id=safe_execution_id,
            task_type=event.task_type,
            request_count=1,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens,
            total_tokens=total_tokens,
            unit_cost=None,
            total_cost=total_cost,
            currency=currency,
            cost_source=cost_source,
            correlation_id=event.correlation_id,
            details=details_clean,
            unknown_fields=tuple(unknown_fields),
        )

    def _convert_cost_record_to_item(self, record: CostRecord, tenant_id: str) -> AgentCostDashboardItem:
        """Proyecta un CostRecord de K.3 a un AgentCostDashboardItem seguro."""
        details_clean = _sanitize_dict(dict(record.metadata or {}))

        agent_type = details_clean.get("agent_type") or details_clean.get("agent_name") or record.metadata.get("agent_type") or record.metadata.get("agent_name")
        task_type = details_clean.get("task_type") or (record.cost_type.value if hasattr(record.cost_type, "value") else str(record.cost_type))

        safe_mission_id = None
        if record.mission_id and isinstance(record.mission_id, str):
            try:
                validate_safe_identifier(record.mission_id, field_name="mission_id")
                safe_mission_id = record.mission_id
            except Exception:
                safe_mission_id = None

        safe_execution_id = None
        if record.execution_id and isinstance(record.execution_id, str):
            try:
                validate_safe_identifier(record.execution_id, field_name="execution_id")
                safe_execution_id = record.execution_id
            except Exception:
                safe_execution_id = None

        is_attributed = safe_mission_id is not None or agent_type is not None

        is_known_cost = record.total_cost is not None
        cost_source = CostConfidenceSource.ACTUAL_RECORDED if is_known_cost else CostConfidenceSource.UNKNOWN_UNPRICED

        # Tokens
        input_tokens = None
        output_tokens = None
        total_tokens = None
        cached_tokens = None
        request_count = 1

        if record.usage:
            if record.usage.unit == UsageUnit.TOKENS:
                input_tokens = int(record.usage.input_quantity) if record.usage.input_quantity is not None else None
                output_tokens = int(record.usage.output_quantity) if record.usage.output_quantity is not None else None
                total_tokens = int(record.usage.total_quantity) if record.usage.total_quantity is not None else None
            elif record.usage.unit == UsageUnit.REQUESTS:
                request_count = int(record.usage.total_quantity) if record.usage.total_quantity is not None else 1

        cached_val = details_clean.get("cached_tokens")
        if cached_val is not None:
            try:
                cached_tokens = int(cached_val)
            except Exception:
                cached_tokens = None

        unknown_fields: List[str] = []
        if input_tokens is None and record.usage and record.usage.unit == UsageUnit.TOKENS:
            unknown_fields.append("input_tokens")
        if output_tokens is None and record.usage and record.usage.unit == UsageUnit.TOKENS:
            unknown_fields.append("output_tokens")
        if total_tokens is None and record.usage and record.usage.unit == UsageUnit.TOKENS:
            unknown_fields.append("total_tokens")
        if record.total_cost is None:
            unknown_fields.append("total_cost")
        if not is_attributed:
            unknown_fields.append("attribution")

        return AgentCostDashboardItem(
            item_id=record.cost_id,
            tenant_id=tenant_id,
            occurred_at=record.occurred_at,
            request_status="SUCCESS",
            is_known_cost=is_known_cost,
            is_attributed=is_attributed,
            provider=record.provider,
            model=record.service_or_model,
            agent_type=str(agent_type) if agent_type is not None else None,
            mission_id=safe_mission_id,
            execution_id=safe_execution_id,
            task_type=task_type,
            request_count=request_count,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens,
            total_tokens=total_tokens,
            unit_cost=record.unit_cost,
            total_cost=record.total_cost,
            currency=record.currency or "USD",
            cost_source=cost_source,
            correlation_id=record.trace_id,
            details=details_clean,
            unknown_fields=tuple(unknown_fields),
        )

    def _collect_items_for_tenant(
        self,
        context: TenantContext,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        mission_id: Optional[str] = None,
    ) -> List[AgentCostDashboardItem]:
        """Recolecta items combinando de forma segura UsageEventRepository y CostRepository sin duplicar."""
        items: List[AgentCostDashboardItem] = []
        seen_item_ids = set()

        # 1. De UsageEventRepository (O.6)
        if self.usage_repository is not None:
            # Construir UsageQuery
            period = None
            if date_from or date_to:
                start = date_from or datetime(1970, 1, 1, tzinfo=timezone.utc)
                end = date_to or datetime(2100, 1, 1, tzinfo=timezone.utc)
                period = UsagePeriod(start_time=start, end_time=end, period_type=UsagePeriodType.CUSTOM)

            u_query = UsageQuery(
                tenant_id=context.tenant_id,
                period=period,
            )
            usage_events = self.usage_repository.find_by_query(context, u_query)
            for ev in usage_events:
                # Validar aislamiento de tenant
                if ev.tenant_id != context.tenant_id:
                    continue
                if mission_id:
                    ev_mission = ev.details.get("mission_id")
                    if ev_mission != mission_id:
                        continue
                item = self._convert_usage_event_to_item(ev)
                if item.item_id not in seen_item_ids:
                    seen_item_ids.add(item.item_id)
                    items.append(item)

        # 2. De CostRepository (K.3) si existe
        if self.cost_repository is not None:
            records = self.cost_repository.list_records(
                mission_id=mission_id,
                from_time=date_from,
                to_time=date_to,
                limit=5000,
            )
            for rec in records:
                # CostRecord puede incluir tenant_id en metadata o details de usage
                rec_tenant = rec.metadata.get("tenant_id") if rec.metadata else None
                if rec_tenant is None and rec.usage and rec.usage.details:
                    rec_tenant = rec.usage.details.get("tenant_id")
                if rec_tenant is not None and rec_tenant != context.tenant_id:
                    continue
                item = self._convert_cost_record_to_item(rec, context.tenant_id)
                if item.item_id not in seen_item_ids:
                    seen_item_ids.add(item.item_id)
                    items.append(item)

        return items

    def get_summary(
        self,
        tenant_id: str,
        session_id: Optional[str] = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
    ) -> AgentCostDashboardSummary:
        """Calcula el resumen financiero y operacional global de costos de agentes para el tenant."""
        context = self._authenticate_and_authorize(tenant_id=tenant_id, session_id=session_id)
        items = self._collect_items_for_tenant(context, date_from=date_from, date_to=date_to)

        now = self.clock.now()
        total_requests = 0
        total_in_tokens = 0
        total_out_tokens = 0
        total_cached_tokens = 0
        total_tokens_sum = 0
        has_in_tokens = False
        has_out_tokens = False
        has_cached_tokens = False
        has_total_tokens = False

        costs_by_currency: Dict[str, Decimal] = {}
        items_count_by_currency: Dict[str, int] = {}
        unknown_cost_count = 0
        attributed_count = 0
        unattributed_count = 0

        # Dimensiones
        prov_map: Dict[str, Dict[str, Any]] = {}
        model_map: Dict[str, Dict[str, Any]] = {}
        agent_map: Dict[str, Dict[str, Any]] = {}
        mission_map: Dict[str, Dict[str, Any]] = {}

        for item in items:
            total_requests += item.request_count

            if item.input_tokens is not None:
                total_in_tokens += item.input_tokens
                has_in_tokens = True
            if item.output_tokens is not None:
                total_out_tokens += item.output_tokens
                has_out_tokens = True
            if item.cached_tokens is not None:
                total_cached_tokens += item.cached_tokens
                has_cached_tokens = True
            if item.total_tokens is not None:
                total_tokens_sum += item.total_tokens
                has_total_tokens = True

            if item.is_attributed:
                attributed_count += 1
            else:
                unattributed_count += 1

            if item.is_known_cost and item.total_cost is not None and item.currency:
                curr = item.currency.upper()
                costs_by_currency[curr] = costs_by_currency.get(curr, Decimal("0")) + item.total_cost
                items_count_by_currency[curr] = items_count_by_currency.get(curr, 0) + item.request_count
            else:
                unknown_cost_count += 1

            # Dimensión Proveedor
            p_key = item.provider or "unknown_provider"
            if p_key not in prov_map:
                prov_map[p_key] = {"requests": 0, "tokens": 0, "has_tokens": False, "costs": {}, "unknown_costs": 0}
            prov_map[p_key]["requests"] += item.request_count
            if item.total_tokens is not None:
                prov_map[p_key]["tokens"] += item.total_tokens
                prov_map[p_key]["has_tokens"] = True
            if item.is_known_cost and item.total_cost is not None and item.currency:
                c = item.currency.upper()
                prov_map[p_key]["costs"][c] = prov_map[p_key]["costs"].get(c, Decimal("0")) + item.total_cost
            else:
                prov_map[p_key]["unknown_costs"] += 1

            # Dimensión Modelo
            m_key = item.model or "unknown_model"
            if m_key not in model_map:
                model_map[m_key] = {"requests": 0, "tokens": 0, "has_tokens": False, "costs": {}, "unknown_costs": 0}
            model_map[m_key]["requests"] += item.request_count
            if item.total_tokens is not None:
                model_map[m_key]["tokens"] += item.total_tokens
                model_map[m_key]["has_tokens"] = True
            if item.is_known_cost and item.total_cost is not None and item.currency:
                c = item.currency.upper()
                model_map[m_key]["costs"][c] = model_map[m_key]["costs"].get(c, Decimal("0")) + item.total_cost
            else:
                model_map[m_key]["unknown_costs"] += 1

            # Dimensión Agente
            a_key = item.agent_type or "unattributed_agent"
            if a_key not in agent_map:
                agent_map[a_key] = {"requests": 0, "tokens": 0, "has_tokens": False, "costs": {}, "unknown_costs": 0}
            agent_map[a_key]["requests"] += item.request_count
            if item.total_tokens is not None:
                agent_map[a_key]["tokens"] += item.total_tokens
                agent_map[a_key]["has_tokens"] = True
            if item.is_known_cost and item.total_cost is not None and item.currency:
                c = item.currency.upper()
                agent_map[a_key]["costs"][c] = agent_map[a_key]["costs"].get(c, Decimal("0")) + item.total_cost
            else:
                agent_map[a_key]["unknown_costs"] += 1

            # Misión
            if item.mission_id:
                ms_key = item.mission_id
                if ms_key not in mission_map:
                    mission_map[ms_key] = {"requests": 0, "tokens": 0, "has_tokens": False, "costs": {}, "unknown_costs": 0}
                mission_map[ms_key]["requests"] += item.request_count
                if item.total_tokens is not None:
                    mission_map[ms_key]["tokens"] += item.total_tokens
                    mission_map[ms_key]["has_tokens"] = True
                if item.is_known_cost and item.total_cost is not None and item.currency:
                    c = item.currency.upper()
                    mission_map[ms_key]["costs"][c] = mission_map[ms_key]["costs"].get(c, Decimal("0")) + item.total_cost
                else:
                    mission_map[ms_key]["unknown_costs"] += 1

        # Construir desglose de divisas
        currency_breakdowns: List[CurrencyCostBreakdown] = []
        for curr, tot_cost in sorted(costs_by_currency.items()):
            req_cnt = items_count_by_currency.get(curr, 0)
            avg_cost = (tot_cost / Decimal(str(req_cnt))).quantize(Decimal("0.000001")) if req_cnt > 0 else None
            currency_breakdowns.append(
                CurrencyCostBreakdown(
                    currency=curr,
                    total_known_cost=tot_cost,
                    known_cost_items_count=req_cnt,
                    avg_cost_per_request=avg_cost,
                )
            )

        # Construir dimensiones
        cost_by_provider = {
            k: DimensionCostBreakdown(
                dimension_key=k,
                request_count=v["requests"],
                total_tokens=v["tokens"] if v["has_tokens"] else None,
                cost_by_currency=v["costs"],
                unknown_cost_events_count=v["unknown_costs"],
            ) for k, v in sorted(prov_map.items())
        }

        cost_by_model = {
            k: DimensionCostBreakdown(
                dimension_key=k,
                request_count=v["requests"],
                total_tokens=v["tokens"] if v["has_tokens"] else None,
                cost_by_currency=v["costs"],
                unknown_cost_events_count=v["unknown_costs"],
            ) for k, v in sorted(model_map.items())
        }

        cost_by_agent = {
            k: DimensionCostBreakdown(
                dimension_key=k,
                request_count=v["requests"],
                total_tokens=v["tokens"] if v["has_tokens"] else None,
                cost_by_currency=v["costs"],
                unknown_cost_events_count=v["unknown_costs"],
            ) for k, v in sorted(agent_map.items())
        }

        # Top missions by cost (ordenadas por max known cost USD o alfabético)
        top_missions: List[MissionCostSummaryItem] = []
        for ms_id, v in mission_map.items():
            top_missions.append(
                MissionCostSummaryItem(
                    mission_id=ms_id,
                    request_count=v["requests"],
                    total_tokens=v["tokens"] if v["has_tokens"] else None,
                    cost_by_currency=v["costs"],
                    unknown_cost_events_count=v["unknown_costs"],
                    mission_link=f"/api/bi/tenants/{tenant_id}/missions/{ms_id}",
                )
            )
        # Orden determinista de misiones por costo principal descendente y luego mission_id
        top_missions.sort(
            key=lambda m: (
                max(m.cost_by_currency.values()) if m.cost_by_currency else Decimal("0"),
                m.mission_id
            ),
            reverse=True
        )

        return AgentCostDashboardSummary(
            tenant_id=tenant_id,
            generated_at=now,
            total_requests=total_requests,
            total_input_tokens=total_in_tokens if has_in_tokens else None,
            total_output_tokens=total_out_tokens if has_out_tokens else None,
            total_cached_tokens=total_cached_tokens if has_cached_tokens else None,
            total_tokens=total_tokens_sum if has_total_tokens else None,
            total_known_cost_by_currency=costs_by_currency,
            currency_breakdowns=tuple(currency_breakdowns),
            unknown_cost_events_count=unknown_cost_count,
            attributed_cost_events_count=attributed_count,
            unattributed_cost_events_count=unattributed_count,
            cost_by_provider=cost_by_provider,
            cost_by_model=cost_by_model,
            cost_by_agent=cost_by_agent,
            top_missions_by_cost=tuple(top_missions[:10]),
        )

    def list_agent_costs(
        self,
        tenant_id: str,
        query: AgentCostDashboardQuery,
        session_id: Optional[str] = None,
    ) -> AgentCostDashboardPage:
        """Lista registros de costo/uso con filtrado, ordenación determinista y paginación."""
        context = self._authenticate_and_authorize(tenant_id=tenant_id, session_id=session_id)
        all_items = self._collect_items_for_tenant(
            context,
            date_from=query.date_from,
            date_to=query.date_to,
            mission_id=query.mission_id,
        )

        filtered: List[AgentCostDashboardItem] = []
        for item in all_items:
            # 1. Filtro provider
            if query.provider and (not item.provider or query.provider.lower() not in item.provider.lower()):
                continue

            # 2. Filtro model
            if query.model and (not item.model or query.model.lower() not in item.model.lower()):
                continue

            # 3. Filtro agent_type
            if query.agent_type and (not item.agent_type or query.agent_type.lower() not in item.agent_type.lower()):
                continue

            # 4. Filtro mission_id
            if query.mission_id and item.mission_id != query.mission_id:
                continue

            # 5. Filtro currency
            if query.currency and (not item.currency or query.currency.upper() != item.currency.upper()):
                continue

            # 6. Filtro is_attributed
            if query.is_attributed is not None and item.is_attributed != query.is_attributed:
                continue

            # 7. Filtro is_known_cost
            if query.is_known_cost is not None and item.is_known_cost != query.is_known_cost:
                continue

            # 8. Filtros min_cost / max_cost
            if query.min_cost is not None:
                if item.total_cost is None or item.total_cost < query.min_cost:
                    continue
            if query.max_cost is not None:
                if item.total_cost is None or item.total_cost > query.max_cost:
                    continue

            # 9. Filtro search_text
            if query.search_text:
                st = query.search_text.lower()
                matches = (
                    (item.provider and st in item.provider.lower()) or
                    (item.model and st in item.model.lower()) or
                    (item.agent_type and st in item.agent_type.lower()) or
                    (item.mission_id and st in item.mission_id.lower()) or
                    (item.item_id and st in item.item_id.lower()) or
                    (item.task_type and st in item.task_type.lower())
                )
                if not matches:
                    continue

            filtered.append(item)

        # Ordenación determinista con tie-break por item_id
        is_desc = query.sort_order == SortOrder.DESC

        def sort_key(x: AgentCostDashboardItem):
            if query.sort_by == AgentCostSortField.OCCURRED_AT:
                val = x.occurred_at
                return (val, x.item_id)
            elif query.sort_by == AgentCostSortField.TOTAL_COST:
                val = x.total_cost if x.total_cost is not None else (Decimal("-Infinity") if is_desc else Decimal("Infinity"))
                return (val, x.occurred_at, x.item_id)
            elif query.sort_by == AgentCostSortField.TOTAL_TOKENS:
                val = x.total_tokens if x.total_tokens is not None else (-1 if is_desc else 999999999)
                return (val, x.occurred_at, x.item_id)
            elif query.sort_by == AgentCostSortField.REQUEST_COUNT:
                return (x.request_count, x.occurred_at, x.item_id)
            elif query.sort_by == AgentCostSortField.PROVIDER:
                return (x.provider or "", x.occurred_at, x.item_id)
            elif query.sort_by == AgentCostSortField.MODEL:
                return (x.model or "", x.occurred_at, x.item_id)
            elif query.sort_by == AgentCostSortField.AGENT_TYPE:
                return (x.agent_type or "", x.occurred_at, x.item_id)
            elif query.sort_by == AgentCostSortField.MISSION_ID:
                return (x.mission_id or "", x.occurred_at, x.item_id)
            else:
                return (x.item_id, x.occurred_at)

        filtered.sort(key=sort_key, reverse=is_desc)

        # Paginación
        total_items = len(filtered)
        page_size = query.page_size
        total_pages = math.ceil(total_items / page_size) if total_items > 0 else 1
        page = min(query.page, total_pages) if total_pages > 0 else 1
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        page_items = filtered[start_idx:end_idx]

        return AgentCostDashboardPage(
            items=tuple(page_items),
            page=page,
            page_size=page_size,
            total_items=total_items,
            total_pages=total_pages,
            has_next=page < total_pages,
            has_previous=page > 1,
        )

    def get_cost_detail(
        self,
        tenant_id: str,
        item_id: str,
        session_id: Optional[str] = None,
    ) -> AgentCostDashboardItem:
        """Obtiene el detalle seguro de un evento de costo verificando pertenencia al tenant."""
        context = self._authenticate_and_authorize(tenant_id=tenant_id, session_id=session_id)
        validate_safe_identifier(item_id, field_name="item_id")

        # 1. Buscar en UsageEventRepository
        if self.usage_repository is not None:
            ev = self.usage_repository.get_event_by_id(context, item_id)
            if ev is not None:
                if ev.tenant_id != tenant_id:
                    raise AgentCostNotFoundError(f"Cost item '{item_id}' not found in tenant '{tenant_id}'")
                return self._convert_usage_event_to_item(ev)

        # 2. Buscar en CostRepository
        if self.cost_repository is not None:
            rec = self.cost_repository.get_by_id(item_id)
            if rec is not None:
                rec_tenant = rec.details.get("tenant_id") if rec.details else None
                if rec_tenant is not None and rec_tenant != tenant_id:
                    raise AgentCostNotFoundError(f"Cost item '{item_id}' not found in tenant '{tenant_id}'")
                return self._convert_cost_record_to_item(rec, tenant_id)

        raise AgentCostNotFoundError(f"Cost item '{item_id}' not found in tenant '{tenant_id}'")

    def get_mission_cost_summary(
        self,
        tenant_id: str,
        mission_id: str,
        session_id: Optional[str] = None,
    ) -> MissionCostSummaryItem:
        """Obtiene el resumen de costos atribuido a una misión específica."""
        context = self._authenticate_and_authorize(tenant_id=tenant_id, session_id=session_id)
        validate_safe_identifier(mission_id, field_name="mission_id")

        items = self._collect_items_for_tenant(context, mission_id=mission_id)

        request_count = 0
        total_tokens = 0
        has_tokens = False
        costs: Dict[str, Decimal] = {}
        unknown_costs = 0

        for item in items:
            # Confirmar que realmente esté atribuida a esta misión
            if item.mission_id != mission_id:
                continue
            request_count += item.request_count
            if item.total_tokens is not None:
                total_tokens += item.total_tokens
                has_tokens = True
            if item.is_known_cost and item.total_cost is not None and item.currency:
                c = item.currency.upper()
                costs[c] = costs.get(c, Decimal("0")) + item.total_cost
            else:
                unknown_costs += 1

        return MissionCostSummaryItem(
            mission_id=mission_id,
            request_count=request_count,
            total_tokens=total_tokens if has_tokens else None,
            cost_by_currency=costs,
            unknown_cost_events_count=unknown_costs,
            mission_link=f"/api/bi/tenants/{tenant_id}/missions/{mission_id}",
        )
