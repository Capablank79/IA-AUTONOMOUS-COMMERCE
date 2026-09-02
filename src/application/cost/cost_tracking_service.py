"""
Servicio de Aplicación para Medición y Registro de Costes Operacionales (Cost Tracking - Hito K.3).

Responsabilidades:
- Calcular de manera determinista los costes asociados a usos de inferencia, llamadas a tools y APIs externas.
- Gestionar semánticas de UNKNOWN_COST vs ZERO_COST con exactitud en Decimal.
- Persistir registros CostRecord inmutables e idempotentes.
- Conectar con AgentTraceService (K.2) y AuditTrailService (K.1) para mantener trazabilidad y auditoría sin duplicación de dominio.
- Proveer resúmenes agregados CostSummary por mission_id, execution_id, cycle_id o filtros avanzados.
- Aislar fallos (failure isolation) para no derribar la misión de negocio si ocurre un error de medición.
"""

from datetime import datetime, timezone
from decimal import Decimal
import logging
from typing import Optional, List, Dict, Any, Union
import uuid

from src.domain.cost.models import (
    CostRecord,
    CostSummary,
    CostType,
    UsageRecord,
    UsageUnit,
    PricingRate,
)
from src.domain.cost.ports import (
    CostRepositoryPort,
    PricingCatalogPort,
)
from src.application.cost.pricing_catalog import get_default_pricing_catalog
from src.domain.audit.models import AuditRecordType, AuditActor, AuditActorType
from src.domain.audit.ports import AuditRepositoryPort

logger = logging.getLogger(__name__)


class CostTrackingService:
    """
    Servicio de aplicación para medición, cálculo y registro de costes.
    """

    def __init__(
        self,
        cost_repository: CostRepositoryPort,
        pricing_catalog: Optional[PricingCatalogPort] = None,
        audit_repository: Optional[AuditRepositoryPort] = None,
        isolate_failures: bool = True,
    ):
        self.cost_repository = cost_repository
        self.pricing_catalog = pricing_catalog or get_default_pricing_catalog()
        self.audit_repository = audit_repository
        self.isolate_failures = isolate_failures

    def calculate_and_record(
        self,
        cost_type: Union[CostType, str],
        provider: str,
        service_or_model: str,
        execution_id: str,
        usage: Optional[UsageRecord] = None,
        occurred_at: Optional[datetime] = None,
        currency: Optional[str] = None,
        trace_id: Optional[str] = None,
        mission_id: Optional[str] = None,
        cycle_id: Optional[str] = None,
        correlation_id: str = "",
        causation_id: Optional[str] = None,
        provenance: str = "MEASUREMENT",
        metadata: Optional[Dict[str, Any]] = None,
        cost_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> Optional[CostRecord]:
        """
        Calcula el coste determinísticamente a partir del uso y tarifa vigente, y lo registra de forma persistente.
        """
        try:
            ct = cost_type if isinstance(cost_type, CostType) else CostType(cost_type)
            now = datetime.now(timezone.utc)
            occ = occurred_at or now
            if occ.tzinfo is None:
                occ = occ.replace(tzinfo=timezone.utc)

            actual_usage = usage or UsageRecord.unknown()

            # Consultar tarifa vigente en el catálogo
            rate: Optional[PricingRate] = self.pricing_catalog.get_rate(
                provider=provider,
                service_or_model=service_or_model,
                at_time=occ,
                cost_type=ct,
            )

            # Cálculo determinista con Decimal
            total_cost: Optional[Decimal] = None
            unit_cost: Optional[Decimal] = None
            rate_currency = currency or "USD"
            pricing_source = "CATALOG" if rate else "UNKNOWN"
            pricing_version = rate.version if rate else "UNKNOWN"

            if rate and actual_usage.unit != UsageUnit.UNKNOWN:
                rate_currency = currency or rate.currency
                # 1. Caso tarifa plana / por request
                if rate.flat_rate is not None:
                    unit_cost = rate.flat_rate
                    if actual_usage.unit == UsageUnit.REQUESTS and actual_usage.total_quantity is not None:
                        total_cost = rate.flat_rate * actual_usage.total_quantity
                    elif actual_usage.total_quantity is not None:
                        total_cost = rate.flat_rate * actual_usage.total_quantity
                    else:
                        total_cost = rate.flat_rate
                # 2. Caso tarifa por tokens (input / output)
                elif actual_usage.unit == UsageUnit.TOKENS and (actual_usage.input_quantity is not None or actual_usage.output_quantity is not None):
                    scale = rate.rate_scale or Decimal("1")
                    in_cost = Decimal("0.00")
                    out_cost = Decimal("0.00")

                    if actual_usage.input_quantity is not None and rate.input_rate is not None:
                        in_cost = (actual_usage.input_quantity / scale) * rate.input_rate
                    if actual_usage.output_quantity is not None and rate.output_rate is not None:
                        out_cost = (actual_usage.output_quantity / scale) * rate.output_rate

                    if rate.input_rate is not None or rate.output_rate is not None:
                        total_cost = in_cost + out_cost
                        unit_cost = (rate.input_rate or Decimal("0.00")) + (rate.output_rate or Decimal("0.00"))
                elif rate.input_rate is not None and actual_usage.total_quantity is not None:
                    scale = rate.rate_scale or Decimal("1")
                    total_cost = (actual_usage.total_quantity / scale) * rate.input_rate
                    unit_cost = rate.input_rate

            # Si no hay tarifa o el uso es indeterminado, total_cost permanece None (UNKNOWN_COST)
            cid = cost_id or f"cst-{execution_id[:8]}-{uuid.uuid4().hex[:8]}"

            record = CostRecord(
                cost_id=cid,
                occurred_at=occ,
                cost_type=ct,
                provider=provider,
                service_or_model=service_or_model,
                execution_id=execution_id,
                usage=actual_usage,
                currency=rate_currency,
                unit_cost=unit_cost,
                total_cost=total_cost,
                pricing_source=pricing_source,
                pricing_version=pricing_version,
                trace_id=trace_id,
                mission_id=mission_id,
                cycle_id=cycle_id,
                correlation_id=correlation_id or execution_id,
                causation_id=causation_id,
                provenance=provenance,
                idempotency_key=idempotency_key or "",
                metadata=metadata or {},
            )

            persisted = self.cost_repository.append(record)

            # Si existe AuditRepository, registrar referencia mínima al hecho sin duplicar métricas completas
            if self.audit_repository and persisted:
                try:
                    from src.domain.audit.models import AuditRecord
                    audit_entry = AuditRecord(
                        audit_id=f"aud-cst-{persisted.cost_id[:12]}",
                        record_type=AuditRecordType.RESULT_RECORDED,
                        occurred_at=persisted.occurred_at,
                        actor=AuditActor(actor_type=AuditActorType.SYSTEM, actor_id="cost_tracking_service"),
                        subject_type="CostRecord",
                        subject_id=persisted.cost_id,
                        action_or_operation="COST_RECORDED",
                        status="RECORDED",
                        correlation_id=persisted.correlation_id,
                        causation_id=persisted.causation_id or persisted.trace_id,
                        mission_id=persisted.mission_id,
                        entity_reference=f"cost:{persisted.cost_id}",
                        metadata={
                            "cost_type": persisted.cost_type.value,
                            "is_known": persisted.is_known,
                            "currency": persisted.currency,
                            "total_cost": str(persisted.total_cost) if persisted.total_cost is not None else "UNKNOWN",
                        },
                    )
                    self.audit_repository.append(audit_entry)
                except Exception as audit_err:
                    logger.debug(f"Non-fatal audit link error in CostTrackingService: {audit_err}")

            return persisted

        except Exception as e:
            logger.warning(f"Error in CostTrackingService.calculate_and_record: {e}")
            if not self.isolate_failures:
                raise
            return None

    def record_inference_cost(
        self,
        execution_id: str,
        provider: str,
        model: str,
        prompt_tokens: Optional[int] = None,
        completion_tokens: Optional[int] = None,
        total_tokens: Optional[int] = None,
        raw_usage: Optional[Dict[str, Any]] = None,
        trace_id: Optional[str] = None,
        mission_id: Optional[str] = None,
        cycle_id: Optional[str] = None,
        correlation_id: str = "",
        causation_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[CostRecord]:
        """Helper para registrar costo de inferencia LLM a partir de tokens observables."""
        usage = UsageRecord.from_tokens(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            raw_usage=raw_usage,
        )
        return self.calculate_and_record(
            cost_type=CostType.INFERENCE,
            provider=provider,
            service_or_model=model,
            execution_id=execution_id,
            usage=usage,
            trace_id=trace_id,
            mission_id=mission_id,
            cycle_id=cycle_id,
            correlation_id=correlation_id,
            causation_id=causation_id,
            metadata=metadata,
        )

    def record_tool_cost(
        self,
        execution_id: str,
        tool_name: str,
        provider: str = "internal",
        request_count: int = 1,
        trace_id: Optional[str] = None,
        mission_id: Optional[str] = None,
        cycle_id: Optional[str] = None,
        correlation_id: str = "",
        causation_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[CostRecord]:
        """Helper para registrar costo de invocación de herramienta o API."""
        usage = UsageRecord.from_requests(request_count=request_count)
        return self.calculate_and_record(
            cost_type=CostType.TOOL_CALL,
            provider=provider,
            service_or_model=tool_name,
            execution_id=execution_id,
            usage=usage,
            trace_id=trace_id,
            mission_id=mission_id,
            cycle_id=cycle_id,
            correlation_id=correlation_id,
            causation_id=causation_id,
            metadata=metadata,
        )

    def get_summary(
        self,
        mission_id: Optional[str] = None,
        execution_id: Optional[str] = None,
        cycle_id: Optional[str] = None,
    ) -> CostSummary:
        """Obtiene el resumen consolidado de costes con detalle multi-moneda."""
        return self.cost_repository.get_summary(
            mission_id=mission_id,
            execution_id=execution_id,
            cycle_id=cycle_id,
        )
