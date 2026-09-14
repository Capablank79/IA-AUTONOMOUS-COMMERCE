"""
Servicio de Aplicación para Q.4 — Mission Dashboard (Hito Q — Business Intelligence).

Responsabilidades y Principios:
1. DASHBOARD != ORCHESTRATOR / EXECUTION ENGINE:
   - Estrictamente consultivo, proyectivo y explicable.
   - Cero mutaciones de misiones, cero reintentos autónomos, cero invocación de herramientas o scrapers.
2. TENANT ISOLATION (O.1):
   - Cada consulta opera bajo un TenantContext estricto. Cero fugas cross-tenant.
3. SAAS AUTHENTICATION & RBAC (O.3 / O.4 / N.4):
   - Valida sesión activa y permisos MISSION_DASHBOARD_READ / BUSINESS_INTELLIGENCE_READ.
4. UNKNOWN != 0 / UNKNOWN != SUCCESS:
   - Duración desconocida != 0s. Progreso desconocido != 0%.
   - Los valores ausentes o no disponibles se preservan estrictamente como None o UNKNOWN.
5. SENSITIVE DATA SANITIZATION (N.9) & NO PRIVATE CoT:
   - Sanitización recursiva de secretos, tokens, credenciales y exclusión total de Chain-of-Thought (CoT) privado.
6. DETERMINISMO Y ORDENACIÓN:
   - Ordenación determinista con regla de desempate estable por mission_id.
7. PAGINACIÓN ACOTADA:
   - Paginación segura y acotada (page >= 1, 1 <= page_size <= 100).
8. TIMELINE AUDITABLE Y EXPLICABLE:
   - Reconstrucción estructurada y cronológica a partir de trazas de loop, trazas de agentes (K.2) y registros de auditoría (K.1).
9. CROSS-DOMAIN LINKING:
   - Enlaces consultivos seguros hacia Oportunidades (Q.1), Proveedores (Q.2) y Rentabilidad (Q.3).
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, List, Dict, Any, Tuple, Sequence, Mapping
from types import MappingProxyType
from dataclasses import asdict
import math

from src.domain.tenant.models import TenantContext
from src.domain.tenant.guard import CrossTenantGuard
from src.domain.security.models import validate_safe_identifier, sanitize_security_data
from src.domain.mission.models import (
    Mission,
    MissionType,
    MissionStatus,
    MissionPriority,
    MissionResult,
    MissionTraceEntry,
    LoopTraceEntry,
)
from src.domain.mission_dashboard.models import (
    MissionSortField,
    SortOrder,
    MissionDashboardItem,
    MissionDashboardSummary,
    MissionTimelineEntry,
    MissionDashboardDetail,
    MissionDashboardQuery,
    MissionDashboardPage,
)
from src.domain.mission_dashboard.ports import TenantMissionRepositoryPort
from src.domain.opportunity_dashboard.ports import TenantOpportunityRepositoryPort
from src.domain.supplier_dashboard.ports import TenantSupplierRepositoryPort
from src.domain.profit_dashboard.ports import TenantProfitRepositoryPort
from src.domain.agent_trace.ports import AgentTraceRepositoryPort
from src.domain.agent_trace.models import AgentTraceRecord
from src.domain.audit.ports import AuditRepositoryPort
from src.domain.audit.models import AuditRecord
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


class MissionDashboardAuthenticationError(AdminAuthenticationError):
    """Lanzada cuando la sesión es inválida, expirada o ausente."""
    pass


class MissionDashboardAuthorizationError(AdminAuthorizationError):
    """Lanzada cuando el usuario o tenant no tiene permisos para ver el dashboard de misiones."""
    pass


class MissionNotFoundError(AdminResourceNotFoundError):
    """Lanzada cuando la misión no existe en el tenant especificado."""
    pass


class MissionInvalidRequestError(AdminInvalidRequestError):
    """Lanzada cuando los parámetros de consulta de misiones son inválidos."""
    pass


SENSITIVE_KEYS = {
    "password", "secret", "token", "api_key", "apikey", "pan", "cvv",
    "private_key", "credential", "access_token", "refresh_token", "authorization",
    "chain_of_thought", "reasoning", "reasoning_tokens", "internal_scratchpad",
}


def _sanitize_dict(data: Any) -> Any:
    """Sanitiza recursivamente datos para vistas de BI eliminando secretos y CoT."""
    if isinstance(data, (dict, MappingProxyType)):
        clean = {}
        for k, v in data.items():
            k_str = str(k).lower()
            if any(s in k_str for s in SENSITIVE_KEYS):
                continue
            clean[str(k)] = _sanitize_dict(v)
        return clean
    elif isinstance(data, (list, tuple, set)):
        return [_sanitize_dict(x) for x in data]
    elif isinstance(data, Decimal):
        return str(data)
    elif isinstance(data, datetime):
        return data.isoformat()
    return data


class MissionDashboardService:
    """
    Servicio de Aplicación de Business Intelligence para Mission Dashboard (Q.4).
    """

    def __init__(
        self,
        repository: TenantMissionRepositoryPort,
        opportunity_repository: Optional[TenantOpportunityRepositoryPort] = None,
        supplier_repository: Optional[TenantSupplierRepositoryPort] = None,
        profit_repository: Optional[TenantProfitRepositoryPort] = None,
        agent_trace_repository: Optional[AgentTraceRepositoryPort] = None,
        audit_repository: Optional[AuditRepositoryPort] = None,
        authorization_service: Optional[SaaSAuthorizationService] = None,
        session_repository: Optional[SaaSSessionRepositoryPort] = None,
        clock: Optional[ClockPort] = None,
    ):
        self.repository = repository
        self.opportunity_repository = opportunity_repository
        self.supplier_repository = supplier_repository
        self.profit_repository = profit_repository
        self.agent_trace_repository = agent_trace_repository
        self.audit_repository = audit_repository
        self.authorization_service = authorization_service
        self.session_repository = session_repository
        self.clock = clock or SystemClock()

    def _authenticate_and_authorize(
        self,
        tenant_id: str,
        session_id: Optional[str],
        action: str = "MISSION_DASHBOARD_READ",
        resource_id: Optional[str] = None,
    ) -> TenantContext:
        """
        Valida la identidad de la sesión (O.3) y los permisos RBAC/SaaS (O.4).
        Retorna el TenantContext seguro.
        """
        validate_safe_identifier(tenant_id, field_name="tenant_id")

        if self.authorization_service is not None:
            if not session_id:
                raise MissionDashboardAuthenticationError("Authentication required: missing session_id")
            auth_req = SaaSAuthorizationRequest(
                action=action,
                session_id=session_id,
                tenant_id=tenant_id,
                resource=resource_id,
            )
            decision = self.authorization_service.authorize(auth_req)
            if not decision.is_allowed:
                auth_reasons = {
                    SaaSAuthorizationReasonCode.SESSION_INVALID,
                    SaaSAuthorizationReasonCode.SESSION_NOT_FOUND,
                    SaaSAuthorizationReasonCode.SESSION_EXPIRED,
                    SaaSAuthorizationReasonCode.SESSION_REVOKED,
                }
                if decision.reason_code in auth_reasons:
                    raise MissionDashboardAuthenticationError(
                        f"Authentication failed: {decision.reason_code.value}"
                    )
                raise MissionDashboardAuthorizationError(
                    f"Access denied for action '{action}' on tenant '{tenant_id}': {decision.reason_code.value}"
                )

        elif self.session_repository is not None and session_id:
            session = self.session_repository.get_by_id(session_id)
            if not session:
                raise MissionDashboardAuthenticationError(f"Session '{session_id}' not found")
            now = self.clock.now()
            if session.is_expired(now):
                raise MissionDashboardAuthenticationError(f"Session '{session_id}' has expired")
            if session.status != SessionStatus.ACTIVE:
                raise MissionDashboardAuthenticationError(f"Session '{session_id}' is not active")
            if session.tenant_id != tenant_id:
                raise MissionDashboardAuthorizationError(
                    f"Cross-tenant access violation: session tenant '{session.tenant_id}' does not match target tenant '{tenant_id}'"
                )

        return TenantContext(tenant_id=tenant_id)

    def _project_mission_item(
        self,
        mission: Mission,
        result: Optional[MissionResult] = None,
    ) -> MissionDashboardItem:
        """
        Proyecta una entidad Mission (y su opcional MissionResult) a un MissionDashboardItem seguro.
        Preserva estrictamente la semántica de incertidumbre (UNKNOWN != 0).
        """
        params = mission.parameters or {}
        unknown_fields: List[str] = []

        # Enlaces referenciales en parámetros o metadata
        opportunity_id = params.get("opportunity_id")
        supplier_id = params.get("supplier_id")
        product_id = params.get("product_id") or params.get("canonical_product_id")
        marketplace = params.get("marketplace")
        category = params.get("category")
        goal = params.get("goal") or params.get("objective") or params.get("description")
        target = params.get("target")

        # Estado y tiempos
        status_str = mission.status.value if hasattr(mission.status, "value") else str(mission.status)
        type_str = mission.type.value if hasattr(mission.type, "value") else str(mission.type)
        priority_str = mission.priority.value if hasattr(mission.priority, "value") else str(mission.priority)

        created_at = mission.created_at
        updated_at = mission.updated_at
        finished_at = result.finished_at if (result and result.finished_at) else None

        # Duración: Solo computable si finished_at y created_at están presentes
        duration_seconds: Optional[float] = None
        if finished_at and created_at:
            delta = (finished_at - created_at).total_seconds()
            duration_seconds = max(0.0, float(delta))
        elif status_str in ("COMPLETED", "FAILED", "ABORTED") and not finished_at:
            unknown_fields.append("duration_seconds")
        elif status_str == "RUNNING":
            # Para misiones en ejecución, la duración final es UNKNOWN (None)
            unknown_fields.append("duration_seconds")

        # Progreso e iteraciones
        progress_pct: Optional[float] = None
        iteration_count: Optional[int] = None
        if "progress_pct" in params:
            try:
                progress_pct = float(params["progress_pct"])
            except (ValueError, TypeError):
                progress_pct = None
        elif "progress" in params and isinstance(params["progress"], (int, float)):
            progress_pct = float(params["progress"])

        if "iteration_count" in params:
            iteration_count = int(params["iteration_count"])
        elif "iterations" in params:
            iteration_count = int(params["iterations"])

        # Métricas de traza, evidencias, decisiones, bloques y errores
        error_count = 0
        block_count = 0
        evidence_count = 0
        decision_count = 0
        outcome_summary: Optional[str] = None

        if result:
            error_count = len(result.errors or [])
            block_count = len(result.blocks or [])
            evidence_count = len(result.evidences or [])
            decision_count = len(result.trace or [])

            # Resumen del outcome
            if result.output:
                if "summary" in result.output:
                    outcome_summary = str(result.output["summary"])
                elif "outcome" in result.output:
                    outcome_summary = str(result.output["outcome"])
                elif "status_message" in result.output:
                    outcome_summary = str(result.output["status_message"])
                elif "message" in result.output:
                    outcome_summary = str(result.output["message"])

        has_errors = bool(params.get("has_errors") or error_count > 0)

        # Si faltan campos clave, registrarlos en unknown_fields
        if progress_pct is None and status_str not in ("COMPLETED", "ABORTED"):
            unknown_fields.append("progress_pct")
        if iteration_count is None:
            unknown_fields.append("iteration_count")

        sanitized_params = _sanitize_dict(params)

        return MissionDashboardItem(
            mission_id=mission.mission_id,
            mission_type=type_str,
            status=status_str,
            priority=priority_str,
            created_at=created_at,
            updated_at=updated_at,
            finished_at=finished_at,
            duration_seconds=duration_seconds,
            progress_pct=progress_pct,
            iteration_count=iteration_count,
            goal=str(goal) if goal is not None else None,
            target=str(target) if target is not None else None,
            opportunity_id=str(opportunity_id) if opportunity_id else None,
            supplier_id=str(supplier_id) if supplier_id else None,
            product_id=str(product_id) if product_id else None,
            marketplace=str(marketplace) if marketplace else None,
            category=str(category) if category else None,
            error_count=error_count,
            block_count=block_count,
            evidence_count=evidence_count,
            decision_count=decision_count,
            has_errors=has_errors,
            outcome_summary=outcome_summary,
            result_summary=outcome_summary,
            parameters_summary=sanitized_params,
            unknown_fields=tuple(unknown_fields),
        )

    def list_missions(
        self,
        tenant_id: str,
        query: Optional[MissionDashboardQuery] = None,
        session_id: Optional[str] = None,
    ) -> MissionDashboardPage:
        """
        Lista misiones del tenant de forma paginada, filtrada y determinísticamente ordenada.
        """
        context = self._authenticate_and_authorize(tenant_id=tenant_id, session_id=session_id)
        q = query or MissionDashboardQuery()

        all_missions = self.repository.list_all(context)

        # Proyectar ítems con sus resultados correspondientes
        projected_items: List[MissionDashboardItem] = []
        for m in all_missions:
            res = self.repository.get_result(context, m.mission_id)
            item = self._project_mission_item(m, res)
            projected_items.append(item)

        # Aplicar filtros
        filtered: List[MissionDashboardItem] = []
        for item in projected_items:
            if q.mission_type:
                target_type = q.mission_type.value if hasattr(q.mission_type, "value") else str(q.mission_type)
                if item.mission_type.upper() != target_type.upper():
                    continue

            if q.status:
                target_status = q.status.value if hasattr(q.status, "value") else str(q.status)
                if item.status.upper() != target_status.upper():
                    continue

            if q.priority:
                target_prio = q.priority.value if hasattr(q.priority, "value") else str(q.priority)
                if item.priority.upper() != target_prio.upper():
                    continue

            if q.opportunity_id and item.opportunity_id != q.opportunity_id:
                continue

            if q.supplier_id and item.supplier_id != q.supplier_id:
                continue

            if q.product_id and item.product_id != q.product_id:
                continue

            if q.marketplace and (not item.marketplace or q.marketplace.lower() not in item.marketplace.lower()):
                continue

            if q.category and (not item.category or q.category.lower() not in item.category.lower()):
                continue

            if q.created_after and item.created_at < q.created_after:
                continue

            if q.created_before and item.created_at > q.created_before:
                continue

            if q.date_from and item.created_at < q.date_from:
                continue

            if q.date_to and item.created_at > q.date_to:
                continue

            if q.has_errors is not None:
                if q.has_errors and not item.has_errors:
                    continue
                if not q.has_errors and item.has_errors:
                    continue

            if q.search_text:
                st = q.search_text.lower()
                matches = (
                    (item.mission_id and st in item.mission_id.lower()) or
                    (item.goal and st in item.goal.lower()) or
                    (item.target and st in item.target.lower()) or
                    (item.mission_type and st in item.mission_type.lower()) or
                    (item.status and st in item.status.lower()) or
                    (item.outcome_summary and st in item.outcome_summary.lower()) or
                    (item.product_id and st in item.product_id.lower()) or
                    (item.supplier_id and st in item.supplier_id.lower()) or
                    (item.opportunity_id and st in item.opportunity_id.lower()) or
                    any(st in str(v).lower() for v in item.parameters_summary.values())
                )
                if not matches:
                    continue

            filtered.append(item)

        # Ordenación determinista con regla de desempate por mission_id
        def sort_key(it: MissionDashboardItem) -> Tuple[int, Any, str]:
            field_val = None
            if q.sort_by == MissionSortField.CREATED_AT:
                field_val = it.created_at
            elif q.sort_by == MissionSortField.UPDATED_AT:
                field_val = it.updated_at
            elif q.sort_by == MissionSortField.STATUS:
                field_val = it.status
            elif q.sort_by == MissionSortField.PRIORITY:
                # Prioridad numérica para orden lógico
                prio_map = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1}
                field_val = prio_map.get(it.priority.upper(), 0)
            elif q.sort_by == MissionSortField.DURATION:
                field_val = it.duration_seconds
            elif q.sort_by == MissionSortField.TYPE:
                field_val = it.mission_type
            elif q.sort_by == MissionSortField.MISSION_ID:
                field_val = it.mission_id

            if field_val is None:
                # NULLs al final
                return (1, 0 if isinstance(field_val, (int, float)) else "", it.mission_id)
            return (0, field_val, it.mission_id)

        reverse = (q.sort_order == SortOrder.DESC)
        filtered.sort(key=sort_key, reverse=reverse)

        # Paginación
        total_count = len(filtered)
        total_pages = max(1, math.ceil(total_count / q.page_size)) if total_count > 0 else 1
        page = min(max(1, q.page), total_pages)
        start_idx = (page - 1) * q.page_size
        end_idx = start_idx + q.page_size
        page_items = filtered[start_idx:end_idx]

        return MissionDashboardPage(
            items=page_items,
            total_count=total_count,
            page=page,
            page_size=q.page_size,
            total_pages=total_pages,
        )

    def get_summary(
        self,
        tenant_id: str,
        session_id: Optional[str] = None,
    ) -> MissionDashboardSummary:
        """
        Calcula determinísticamente el resumen analítico de misiones para el tenant.
        """
        context = self._authenticate_and_authorize(tenant_id=tenant_id, session_id=session_id)
        all_missions = self.repository.list_all(context)

        total_missions = len(all_missions)
        pending_count = 0
        running_count = 0
        completed_count = 0
        failed_count = 0
        blocked_count = 0
        aborted_count = 0
        missions_with_errors = 0

        durations: List[float] = []
        by_type: Dict[str, int] = {}
        by_status: Dict[str, int] = {}
        by_prio: Dict[str, int] = {}

        for m in all_missions:
            st = m.status.value if hasattr(m.status, "value") else str(m.status)
            tp = m.type.value if hasattr(m.type, "value") else str(m.type)
            pr = m.priority.value if hasattr(m.priority, "value") else str(m.priority)

            by_status[st] = by_status.get(st, 0) + 1
            by_type[tp] = by_type.get(tp, 0) + 1
            by_prio[pr] = by_prio.get(pr, 0) + 1

            if st == "PENDING":
                pending_count += 1
            elif st == "RUNNING":
                running_count += 1
            elif st == "COMPLETED":
                completed_count += 1
            elif st == "FAILED":
                failed_count += 1
            elif st == "BLOCKED":
                blocked_count += 1
            elif st == "ABORTED":
                aborted_count += 1

            # Duraciones computables reales y errores
            res = self.repository.get_result(context, m.mission_id)
            has_err = False
            if m.parameters and m.parameters.get("has_errors"):
                has_err = True
            if res and res.errors:
                has_err = True
            if has_err:
                missions_with_errors += 1

            if res and res.finished_at and m.created_at:
                d = max(0.0, float((res.finished_at - m.created_at).total_seconds()))
                durations.append(d)

        avg_duration = (sum(durations) / len(durations)) if durations else None
        shortest_duration = min(durations) if durations else None
        longest_duration = max(durations) if durations else None

        finished_total = completed_count + failed_count + aborted_count
        success_rate_pct = (completed_count / finished_total * 100.0) if finished_total > 0 else None

        return MissionDashboardSummary(
            tenant_id=tenant_id,
            total_missions=total_missions,
            pending_count=pending_count,
            running_count=running_count,
            completed_count=completed_count,
            failed_count=failed_count,
            blocked_count=blocked_count,
            aborted_count=aborted_count,
            average_duration_seconds=avg_duration,
            shortest_duration_seconds=shortest_duration,
            longest_duration_seconds=longest_duration,
            missions_by_type=by_type,
            missions_by_status=by_status,
            missions_by_priority=by_prio,
            success_rate_pct=success_rate_pct,
            missions_with_errors=missions_with_errors,
            generated_at=self.clock.now(),
        )

    def get_mission_detail(
        self,
        tenant_id: str,
        mission_id: str,
        session_id: Optional[str] = None,
    ) -> MissionDashboardDetail:
        """
        Obtiene la vista detallada de una misión, construyendo su timeline auditable
        (a partir de MissionResult, AgentTraceRecord K.2 y AuditRecord K.1) y vinculando
        referencias cruzadas a Oportunidades (Q.1), Proveedores (Q.2) y Rentabilidad (Q.3).
        """
        context = self._authenticate_and_authorize(
            tenant_id=tenant_id,
            session_id=session_id,
            resource_id=mission_id,
        )
        mission = self.repository.get_by_id(context, mission_id)
        if not mission:
            raise MissionNotFoundError(f"Mission '{mission_id}' not found for tenant '{tenant_id}'")

        result = self.repository.get_result(context, mission_id)
        item = self._project_mission_item(mission, result)

        # Construcción del Timeline cronológico
        timeline_entries: List[MissionTimelineEntry] = []

        # 1. Evento de creación base
        timeline_entries.append(
            MissionTimelineEntry(
                timestamp=mission.created_at,
                step_type="MISSION_CREATED",
                status="SUCCESS",
                actor_type="USER" if "user_id" in (mission.parameters or {}) else "SYSTEM",
                actor_id=str((mission.parameters or {}).get("user_id") or "system"),
                action="CREATE",
                description=f"Mission of type '{item.mission_type}' initialized with priority '{item.priority}'.",
                details=_sanitize_dict(item.parameters_summary),
            )
        )

        # 2. Trazas internas de MissionResult (si existen)
        if result and result.trace:
            for i, tr in enumerate(result.trace):
                t_time = tr.timestamp if hasattr(tr, "timestamp") and tr.timestamp else mission.updated_at
                t_step = tr.step if hasattr(tr, "step") else f"STEP_{i+1}"
                t_status = tr.status.value if hasattr(tr.status, "value") else str(tr.status)
                t_meta = getattr(tr, "metadata", {})
                timeline_entries.append(
                    MissionTimelineEntry(
                        timestamp=t_time,
                        step_type="MISSION_TRACE_STEP",
                        status=t_status,
                        actor_type="AGENT",
                        actor_id="autonomous_loop",
                        action=t_step,
                        description=f"Step '{t_step}' completed with status '{t_status}'.",
                        details=_sanitize_dict(t_meta),
                        iteration=i + 1,
                    )
                )

        # 3. Integración de Trazas Operacionales de Agentes (Agent Trace - Hito K.2)
        if self.agent_trace_repository:
            try:
                agent_records = self.agent_trace_repository.list_records(mission_id=mission_id)
                for rec in agent_records:
                    rec_status = rec.status.value if hasattr(rec.status, "value") else str(rec.status)
                    rec_step = rec.step_type.value if hasattr(rec.step_type, "value") else str(rec.step_type)
                    timeline_entries.append(
                        MissionTimelineEntry(
                            timestamp=rec.started_at,
                            step_type=f"AGENT_STEP_{rec_step}",
                            status=rec_status,
                            actor_type="AGENT",
                            actor_id=rec.component_name,
                            action=rec.operation,
                            description=f"Agent component '{rec.component_name}' executed operation '{rec.operation}'.",
                            details=_sanitize_dict(rec.metadata),
                            duration_seconds=rec.duration_seconds,
                            iteration=rec.step_number,
                        )
                    )
            except Exception:
                pass  # Fallback gracefully si el repositorio de trazas no está disponible o falla

        # 4. Integración de Hechos Auditables (Audit Trail - Hito K.1)
        if self.audit_repository:
            try:
                audit_records = self.audit_repository.list_records(mission_id=mission_id)
                for aud in audit_records:
                    aud_type = aud.record_type.value if hasattr(aud.record_type, "value") else str(aud.record_type)
                    timeline_entries.append(
                        MissionTimelineEntry(
                            timestamp=aud.occurred_at,
                            step_type=f"AUDIT_{aud_type}",
                            status="SUCCESS",
                            actor_type=aud.actor.actor_type.value if hasattr(aud.actor.actor_type, "value") else str(aud.actor.actor_type),
                            actor_id=aud.actor.actor_id,
                            action=aud_type,
                            description=f"Auditable event '{aud_type}' registered for subject '{aud.subject_id}'.",
                            details=_sanitize_dict(aud.metadata),
                        )
                    )
            except Exception:
                pass

        # 5. Evento de finalización si existe resultado
        if result and result.finished_at:
            res_status = result.status.value if hasattr(result.status, "value") else str(result.status)
            timeline_entries.append(
                MissionTimelineEntry(
                    timestamp=result.finished_at,
                    step_type="MISSION_FINISHED",
                    status=res_status,
                    actor_type="SYSTEM",
                    actor_id="orchestrator",
                    action="FINISH",
                    description=f"Mission concluded with final status '{res_status}'.",
                    details=_sanitize_dict(result.output),
                )
            )

        # Ordenar timeline cronológicamente con desempate determinista
        timeline_entries.sort(key=lambda e: (e.timestamp, e.step_type, e.actor_id))

        # Enlaces cruzados consultivos seguros
        associated_opportunity = None
        associated_supplier = None
        associated_profit = None

        if item.opportunity_id and self.opportunity_repository:
            try:
                opp = self.opportunity_repository.get_by_id(context, item.opportunity_id)
                if opp:
                    associated_opportunity = {
                        "opportunity_id": opp.opportunity_id,
                        "title": opp.title,
                        "marketplace": opp.marketplace.value if hasattr(opp.marketplace, "value") else str(opp.marketplace),
                        "opportunity_type": opp.opportunity_type.value if hasattr(opp.opportunity_type, "value") else str(opp.opportunity_type),
                        "status": opp.status.value if hasattr(opp.status, "value") else str(opp.status),
                        "confidence": opp.confidence.value if hasattr(opp.confidence, "value") else str(opp.confidence),
                    }
            except Exception:
                associated_opportunity = None

        if item.supplier_id and self.supplier_repository:
            try:
                sup = self.supplier_repository.get_by_id(context, item.supplier_id)
                if sup:
                    associated_supplier = {
                        "supplier_id": sup.supplier_id,
                        "name": sup.name,
                        "status": sup.status.value if hasattr(sup.status, "value") else str(sup.status),
                        "verification_status": sup.metadata.get("verification_status", "UNKNOWN") if hasattr(sup, "metadata") and sup.metadata else "UNKNOWN",
                        "risk_level": sup.metadata.get("risk_level", "UNKNOWN") if hasattr(sup, "metadata") and sup.metadata else "UNKNOWN",
                    }
            except Exception:
                associated_supplier = None

        if self.profit_repository:
            try:
                # Buscar por opportunity_id o supplier_id o product_id en el repositorio de rentabilidad
                profit_items = self.profit_repository.list_all(context)
                for pi in profit_items:
                    if (
                        (item.opportunity_id and pi.opportunity_id == item.opportunity_id) or
                        (item.supplier_id and pi.supplier_id == item.supplier_id) or
                        (item.product_id and pi.product_id == item.product_id)
                    ):
                        associated_profit = {
                            "item_id": pi.item_id,
                            "product_id": pi.product_id,
                            "completeness": pi.completeness.value if hasattr(pi.completeness, "value") else str(pi.completeness),
                            "currency": pi.currency,
                            "margin_pct": str(pi.margin_pct) if pi.margin_pct is not None else None,
                            "contribution_profit": str(pi.contribution_profit) if pi.contribution_profit is not None else None,
                        }
                        break
            except Exception:
                associated_profit = None

        # Desglose de decisiones, evidencias, bloques, errores y output de result
        decisions_list: List[Dict[str, Any]] = []
        evidences_list: List[Dict[str, Any]] = []
        blocks_list: List[Dict[str, Any]] = []
        errors_list: List[str] = []
        output_dict: Dict[str, Any] = {}

        if result:
            if result.trace:
                for tr in result.trace:
                    decisions_list.append(_sanitize_dict(asdict(tr) if hasattr(tr, "__dataclass_fields__") else tr))
            if result.evidences:
                for ev in result.evidences:
                    evidences_list.append(_sanitize_dict(asdict(ev) if hasattr(ev, "__dataclass_fields__") else ev))
            if result.blocks:
                for bl in result.blocks:
                    blocks_list.append(_sanitize_dict(bl))
            if result.errors:
                errors_list = list(result.errors)
            if result.output:
                output_dict = _sanitize_dict(result.output)

        return MissionDashboardDetail(
            item=item,
            timeline=tuple(timeline_entries),
            decisions=tuple(decisions_list),
            evidences=tuple(evidences_list),
            blocks=tuple(blocks_list),
            errors=tuple(errors_list),
            output=output_dict,
            associated_opportunity=associated_opportunity,
            associated_supplier=associated_supplier,
            associated_profit=associated_profit,
        )
