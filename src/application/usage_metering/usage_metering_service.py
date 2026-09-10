"""
Servicio de Dominio / Aplicación para Usage Metering SaaS (Hito O.6 — Usage Metering).

Responsabilidades:
- Implementar UsageMeteringServicePort.
- Registrar eventos de uso (UsageEvent) de forma idempotente, inmutable y aislada por tenant.
- Calcular agregaciones multidimensionales deterministas (por modelo, identidad/usuario, proveedor, tipo de tarea y período).
- Soportar consultas seguras sin cross-tenant leakage.
- Preservar UNKNOWN tokens y UNKNOWN actual cost sin convertirlos en 0 espurios.
- Respetar aislamiento de K.3, K.7 (ClockPort), K.1 (Audit) y K.2 (Trace).
- Prohibición explícita de evaluar cuotas o bloquear peticiones (eso es Hito O.7).
"""

from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, List, Dict, Any, Mapping

from src.domain.tenant.models import (
    TenantContext,
    CrossTenantAccessError,
    TenantSecurityViolationError,
)
from src.domain.tenant.guard import CrossTenantGuard
from src.domain.reliability.ports import ClockPort
from src.domain.caching.models import CacheLookupStatus
from src.domain.usage_metering.models import (
    UsageEvent,
    UsageQuery,
    UsageRequestStatus,
    DimensionUsageSummary,
    UsageAggregate,
    UsageQueryError,
)
from src.domain.usage_metering.ports import (
    UsageEventRepositoryPort,
    UsageMeteringServicePort,
)
from src.domain.audit.ports import AuditRepositoryPort
from src.domain.audit.models import (
    AuditRecord,
    AuditRecordType,
    AuditActor,
    AuditActorType,
)
from src.domain.agent_trace.ports import AgentTraceRepositoryPort


class UsageMeteringService(UsageMeteringServicePort):
    """
    Servicio central de agregación y medición de uso de IA para arquitecturas SaaS Multi-Tenant.
    """

    def __init__(
        self,
        repository: UsageEventRepositoryPort,
        clock: Optional[ClockPort] = None,
        audit_repository: Optional[AuditRepositoryPort] = None,
        agent_trace_repository: Optional[AgentTraceRepositoryPort] = None,
    ):
        self.repository = repository
        self.clock = clock
        self.audit_repository = audit_repository
        self.agent_trace_repository = agent_trace_repository

    def _get_now(self) -> datetime:
        if self.clock is not None:
            return self.clock.now()
        return datetime.now(timezone.utc)

    def record_usage_event(self, context: TenantContext, event: UsageEvent) -> UsageEvent:
        """
        Registra un hecho atómico de consumo para el tenant verificado.
        """
        CrossTenantGuard.ensure_tenant_context(context)
        CrossTenantGuard.assert_same_tenant(context, event.tenant_id, operation_name="record_usage_event")

        saved_event = self.repository.append_event(context, event)

        # Registro opcional y seguro en Audit Trail (sin tokens individuales detallados ni PII)
        if self.audit_repository is not None:
            try:
                audit_record = AuditRecord(
                    audit_id=f"aud_use_{saved_event.usage_event_id}",
                    occurred_at=self._get_now(),
                    actor=AuditActor(
                        actor_type=AuditActorType.USER if saved_event.identity_id else AuditActorType.SYSTEM,
                        actor_id=saved_event.identity_id or context.tenant_id,
                    ),
                    record_type=AuditRecordType.ACTION_EXECUTED,
                    subject_type="UsageEvent",
                    subject_id=saved_event.usage_event_id,
                    action_or_operation="RECORD_USAGE_EVENT",
                    status="COMPLETED",
                    correlation_id=saved_event.correlation_id or saved_event.usage_event_id,
                    metadata={
                        "tenant_id": context.tenant_id,
                        "provider": saved_event.provider,
                        "model": saved_event.model,
                        "request_status": saved_event.request_status.value,
                        "total_tokens": saved_event.total_tokens,
                    },
                )
                self.audit_repository.append(audit_record)
            except Exception:
                pass

        return saved_event

    def get_event(self, context: TenantContext, usage_event_id: str) -> Optional[UsageEvent]:
        """
        Obtiene un evento de uso por su ID para el tenant verificado.
        """
        CrossTenantGuard.ensure_tenant_context(context)
        return self.repository.get_event_by_id(context, usage_event_id)

    def aggregate_usage(self, context: TenantContext, query: UsageQuery) -> UsageAggregate:
        """
        Calcula una proyección agregada determinista a partir de los eventos históricos del tenant.
        """
        CrossTenantGuard.ensure_tenant_context(context)
        CrossTenantGuard.assert_same_tenant(context, query.tenant_id, operation_name="aggregate_usage")

        events = self.repository.find_by_query(context, query)

        total_requests = 0
        successful_requests = 0
        failed_requests = 0
        cached_requests = 0

        total_input_tokens = 0
        total_output_tokens = 0
        total_tokens = 0
        unknown_token_events_count = 0

        total_estimated_cost = Decimal("0.00")
        total_actual_cost = Decimal("0.00")
        unknown_actual_cost_events_count = 0

        # Acumuladores multidimensionales
        model_accumulators: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
            "total_requests": 0, "successful_requests": 0, "failed_requests": 0, "cached_requests": 0,
            "total_input_tokens": 0, "total_output_tokens": 0, "total_tokens": 0, "unknown_tokens": 0,
            "total_estimated_cost": Decimal("0.00"), "total_actual_cost": Decimal("0.00"), "unknown_actual_cost": 0,
        })
        identity_accumulators: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
            "total_requests": 0, "successful_requests": 0, "failed_requests": 0, "cached_requests": 0,
            "total_input_tokens": 0, "total_output_tokens": 0, "total_tokens": 0, "unknown_tokens": 0,
            "total_estimated_cost": Decimal("0.00"), "total_actual_cost": Decimal("0.00"), "unknown_actual_cost": 0,
        })
        provider_accumulators: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
            "total_requests": 0, "successful_requests": 0, "failed_requests": 0, "cached_requests": 0,
            "total_input_tokens": 0, "total_output_tokens": 0, "total_tokens": 0, "unknown_tokens": 0,
            "total_estimated_cost": Decimal("0.00"), "total_actual_cost": Decimal("0.00"), "unknown_actual_cost": 0,
        })
        task_type_accumulators: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
            "total_requests": 0, "successful_requests": 0, "failed_requests": 0, "cached_requests": 0,
            "total_input_tokens": 0, "total_output_tokens": 0, "total_tokens": 0, "unknown_tokens": 0,
            "total_estimated_cost": Decimal("0.00"), "total_actual_cost": Decimal("0.00"), "unknown_actual_cost": 0,
        })

        for event in events:
            total_requests += 1

            # Clasificación de request
            if event.request_status == UsageRequestStatus.SUCCESS:
                successful_requests += 1
            elif event.request_status in (UsageRequestStatus.FAILED, UsageRequestStatus.BLOCKED):
                failed_requests += 1

            if event.cache_status == CacheLookupStatus.HIT or event.request_status == UsageRequestStatus.CACHED:
                cached_requests += 1

            # Tokens
            has_tokens = False
            if event.input_tokens is not None:
                total_input_tokens += event.input_tokens
                has_tokens = True
            if event.output_tokens is not None:
                total_output_tokens += event.output_tokens
                has_tokens = True
            if event.total_tokens is not None:
                total_tokens += event.total_tokens
                has_tokens = True
            elif event.input_tokens is not None and event.output_tokens is not None:
                total_tokens += (event.input_tokens + event.output_tokens)
                has_tokens = True

            if not has_tokens and event.request_status == UsageRequestStatus.SUCCESS and event.cache_status != CacheLookupStatus.HIT:
                unknown_token_events_count += 1

            # Costes
            if event.estimated_cost is not None:
                total_estimated_cost += event.estimated_cost

            if event.actual_cost is not None:
                total_actual_cost += event.actual_cost
            else:
                unknown_actual_cost_events_count += 1

            # Desglose Model
            m_key = event.model or "unknown"
            self._update_accumulator(model_accumulators[m_key], event)

            # Desglose Identity
            id_key = event.identity_id or "unassigned"
            self._update_accumulator(identity_accumulators[id_key], event)

            # Desglose Provider
            p_key = event.provider or "unknown"
            self._update_accumulator(provider_accumulators[p_key], event)

            # Desglose TaskType
            t_key = event.task_type or "unknown"
            self._update_accumulator(task_type_accumulators[t_key], event)

        # Construir diccionarios de resúmenes dimensionales
        breakdown_model = {
            k: DimensionUsageSummary(
                dimension_key="model",
                dimension_value=k,
                total_requests=v["total_requests"],
                successful_requests=v["successful_requests"],
                failed_requests=v["failed_requests"],
                cached_requests=v["cached_requests"],
                total_input_tokens=v["total_input_tokens"],
                total_output_tokens=v["total_output_tokens"],
                total_tokens=v["total_tokens"],
                unknown_token_events_count=v["unknown_tokens"],
                total_estimated_cost=v["total_estimated_cost"],
                total_actual_cost=v["total_actual_cost"],
                unknown_actual_cost_events_count=v["unknown_actual_cost"],
            )
            for k, v in sorted(model_accumulators.items())
        }

        breakdown_identity = {
            k: DimensionUsageSummary(
                dimension_key="identity",
                dimension_value=k,
                total_requests=v["total_requests"],
                successful_requests=v["successful_requests"],
                failed_requests=v["failed_requests"],
                cached_requests=v["cached_requests"],
                total_input_tokens=v["total_input_tokens"],
                total_output_tokens=v["total_output_tokens"],
                total_tokens=v["total_tokens"],
                unknown_token_events_count=v["unknown_tokens"],
                total_estimated_cost=v["total_estimated_cost"],
                total_actual_cost=v["total_actual_cost"],
                unknown_actual_cost_events_count=v["unknown_actual_cost"],
            )
            for k, v in sorted(identity_accumulators.items())
        }

        breakdown_provider = {
            k: DimensionUsageSummary(
                dimension_key="provider",
                dimension_value=k,
                total_requests=v["total_requests"],
                successful_requests=v["successful_requests"],
                failed_requests=v["failed_requests"],
                cached_requests=v["cached_requests"],
                total_input_tokens=v["total_input_tokens"],
                total_output_tokens=v["total_output_tokens"],
                total_tokens=v["total_tokens"],
                unknown_token_events_count=v["unknown_tokens"],
                total_estimated_cost=v["total_estimated_cost"],
                total_actual_cost=v["total_actual_cost"],
                unknown_actual_cost_events_count=v["unknown_actual_cost"],
            )
            for k, v in sorted(provider_accumulators.items())
        }

        breakdown_task = {
            k: DimensionUsageSummary(
                dimension_key="task_type",
                dimension_value=k,
                total_requests=v["total_requests"],
                successful_requests=v["successful_requests"],
                failed_requests=v["failed_requests"],
                cached_requests=v["cached_requests"],
                total_input_tokens=v["total_input_tokens"],
                total_output_tokens=v["total_output_tokens"],
                total_tokens=v["total_tokens"],
                unknown_token_events_count=v["unknown_tokens"],
                total_estimated_cost=v["total_estimated_cost"],
                total_actual_cost=v["total_actual_cost"],
                unknown_actual_cost_events_count=v["unknown_actual_cost"],
            )
            for k, v in sorted(task_type_accumulators.items())
        }

        return UsageAggregate(
            tenant_id=context.tenant_id,
            period=query.period,
            total_requests=total_requests,
            successful_requests=successful_requests,
            failed_requests=failed_requests,
            cached_requests=cached_requests,
            total_input_tokens=total_input_tokens,
            total_output_tokens=total_output_tokens,
            total_tokens=total_tokens,
            unknown_token_events_count=unknown_token_events_count,
            total_estimated_cost=total_estimated_cost,
            total_actual_cost=total_actual_cost,
            unknown_actual_cost_events_count=unknown_actual_cost_events_count,
            breakdown_by_model=breakdown_model,
            breakdown_by_identity=breakdown_identity,
            breakdown_by_provider=breakdown_provider,
            breakdown_by_task_type=breakdown_task,
            events_count=len(events),
        )

    @staticmethod
    def _update_accumulator(acc: Dict[str, Any], event: UsageEvent) -> None:
        acc["total_requests"] += 1
        if event.request_status == UsageRequestStatus.SUCCESS:
            acc["successful_requests"] += 1
        elif event.request_status in (UsageRequestStatus.FAILED, UsageRequestStatus.BLOCKED):
            acc["failed_requests"] += 1

        if event.cache_status == CacheLookupStatus.HIT or event.request_status == UsageRequestStatus.CACHED:
            acc["cached_requests"] += 1

        has_tok = False
        if event.input_tokens is not None:
            acc["total_input_tokens"] += event.input_tokens
            has_tok = True
        if event.output_tokens is not None:
            acc["total_output_tokens"] += event.output_tokens
            has_tok = True
        if event.total_tokens is not None:
            acc["total_tokens"] += event.total_tokens
            has_tok = True
        elif event.input_tokens is not None and event.output_tokens is not None:
            acc["total_tokens"] += (event.input_tokens + event.output_tokens)
            has_tok = True

        if not has_tok and event.request_status == UsageRequestStatus.SUCCESS and event.cache_status != CacheLookupStatus.HIT:
            acc["unknown_tokens"] += 1

        if event.estimated_cost is not None:
            acc["total_estimated_cost"] += event.estimated_cost
        if event.actual_cost is not None:
            acc["total_actual_cost"] += event.actual_cost
        else:
            acc["unknown_actual_cost"] += 1
