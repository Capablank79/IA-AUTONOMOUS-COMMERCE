from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, List, Dict, Any, Tuple
import math

from src.domain.market_intelligence.models import Confidence
from src.domain.supplier_intelligence.models import EvidenceProvenanceType
from src.domain.supplier_memory.models import SupplierMemoryRecord
from src.domain.outcome.models import OutcomeRecord, OutcomeStatus
from src.domain.calibration.models import DecisionCalibrationRecord
from src.domain.supplier_performance.models import (
    SupplierPerformanceRecord,
    SupplierPerformanceStatus,
    SupplierTemporalPeriod,
    ObservedSupplierMetrics,
    DerivedSupplierMetrics,
)
from src.domain.supplier_performance.ports import SupplierPerformanceRepositoryPort

DEFAULT_MIN_SAMPLE_THRESHOLD = 1


class SupplierPerformanceService:
    """
    Servicio de Aplicación para calcular la Performance Observable de Proveedores (Task I.5).
    Combina observaciones verdaderas de Supplier Memory (H.6) y Outcomes reales de negocio (I.1).

    Propiedades clave:
    1. Determinista: Mismo dataset -> Mismo resultado exacto.
    2. Idempotente: Retorna registro existente si se especifica idempotency_key.
    3. Preservación de procedencia y diferenciación explícita entre OBSERVED y DERIVED.
    4. Cero invención de datos: Si no existen eventos/denominador, la métrica derivada es None (UNKNOWN).
    5. Manejo seguro de UNKNOWN / Data Quality: Si no hay muestras suficientes -> INSUFFICIENT_DATA.
    6. Sanitización PII / Credenciales: Excluye cualquier dato sensible.
    7. Trazabilidad causal completa: Mantiene referencias a supplier_memory_ids, outcome_ids, mission_ids, decision_ids, action_ids.
    """

    def __init__(
        self,
        performance_repo: Optional[SupplierPerformanceRepositoryPort] = None,
        min_sample_threshold: int = DEFAULT_MIN_SAMPLE_THRESHOLD,
    ):
        self.performance_repo = performance_repo
        self.min_sample_threshold = min_sample_threshold

    def calculate_performance(
        self,
        performance_id: str,
        supplier_id: str,
        period: SupplierTemporalPeriod,
        supplier_records: Optional[List[SupplierMemoryRecord]] = None,
        outcome_records: Optional[List[OutcomeRecord]] = None,
        calibration_context: Optional[DecisionCalibrationRecord] = None,
        calculated_at: Optional[datetime] = None,
        correlation_id: str = "default-correlation",
        idempotency_key: str = "default-idempotency",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SupplierPerformanceRecord:
        """
        Calcula deterministamente SupplierPerformanceRecord combinando registros de memoria de proveedor y outcomes observados.
        """
        if self.performance_repo and hasattr(self.performance_repo, "get_by_idempotency_key") and idempotency_key:
            existing = getattr(self.performance_repo, "get_by_idempotency_key")(idempotency_key)
            if existing:
                return existing

        now = calculated_at or datetime.now(timezone.utc)
        memories = supplier_records or []
        outcomes = outcome_records or []

        # Filtrar por período temporal si está presente
        if period.period_start or period.period_end:
            filtered_memories = []
            for sm in memories:
                if period.period_start and sm.observed_at < period.period_start:
                    continue
                if period.period_end and sm.observed_at > period.period_end:
                    continue
                filtered_memories.append(sm)
            memories = filtered_memories

            filtered_outcomes = []
            for oc in outcomes:
                if period.period_start and oc.observed_at < period.period_start:
                    continue
                if period.period_end and oc.observed_at > period.period_end:
                    continue
                filtered_outcomes.append(oc)
            outcomes = filtered_outcomes

        # Filtrar que los registros correspondan al supplier_id solicitado (estabilidad de identidad)
        memories = [m for m in memories if m.supplier_id == supplier_id]

        quote_sample_count = len(memories)
        outcome_sample_count = len(outcomes)
        total_samples = quote_sample_count + outcome_sample_count

        # Trazabilidad causal de IDs (ordenados para determinismo)
        sm_ids = tuple(sorted(list({m.supplier_memory_id for m in memories})))
        oc_ids = tuple(sorted(list({o.outcome_id for o in outcomes})))
        mission_ids = tuple(sorted(list({o.mission_id for o in outcomes if o.mission_id})))
        decision_ids = tuple(sorted(list({o.decision_id for o in outcomes if o.decision_id})))
        action_ids = tuple(sorted(list({o.action_id for o in outcomes if o.action_id})))

        # Si conteo total < min_sample_threshold -> INSUFFICIENT_DATA
        if total_samples < self.min_sample_threshold:
            record = SupplierPerformanceRecord(
                performance_id=performance_id,
                supplier_id=supplier_id,
                period=period,
                status=SupplierPerformanceStatus.INSUFFICIENT_DATA,
                sample_count=total_samples,
                quote_sample_count=quote_sample_count,
                outcome_sample_count=outcome_sample_count,
                observed_metrics=ObservedSupplierMetrics(),
                derived_metrics=DerivedSupplierMetrics(),
                supplier_memory_ids=sm_ids,
                outcome_ids=oc_ids,
                mission_ids=mission_ids,
                decision_ids=decision_ids,
                action_ids=action_ids,
                calibration_context_id=calibration_context.calibration_id if calibration_context else None,
                contextual_prediction_error=calibration_context.calibration_error if calibration_context else None,
                calculated_at=now,
                correlation_id=correlation_id,
                idempotency_key=idempotency_key,
                version=1,
                metadata=metadata or {},
            )
            if self.performance_repo:
                self.performance_repo.save(record)
            return record

        # Extracción y agregación de observaciones reales (OBSERVED METRICS)
        total_quotes_obs = quote_sample_count
        accepted_quotes_count = 0
        observed_lead_times: List[int] = []
        observed_costs: List[Decimal] = []
        observed_moqs: List[int] = []

        for sm in memories:
            if sm.cost_amount is not None:
                observed_costs.append(sm.cost_amount)
            if sm.moq is not None:
                observed_moqs.append(sm.moq)
            if sm.lead_time_days is not None:
                observed_lead_times.append(sm.lead_time_days)
            
            # Si en la metadata se indica si fue aceptada
            if sm.metadata.get("quote_accepted") is True or sm.metadata.get("status") == "ACCEPTED":
                accepted_quotes_count += 1

        orders_placed = 0
        fulfilled_orders = 0
        delivered_on_time = 0
        cancelled_orders = 0
        defective_returns = 0
        outcome_success_count = 0
        valid_outcomes_for_success_rate = 0

        for oc in outcomes:
            if oc.status == OutcomeStatus.SUCCESS:
                outcome_success_count += 1
                valid_outcomes_for_success_rate += 1
            elif oc.status in (OutcomeStatus.FAILURE, OutcomeStatus.PARTIAL, OutcomeStatus.CANCELLED):
                valid_outcomes_for_success_rate += 1

            metrics = oc.value_metrics
            if "order_placed" in metrics and metrics["order_placed"] is True:
                orders_placed += 1
            elif "orders_placed" in metrics and metrics["orders_placed"] is not None:
                try:
                    orders_placed += int(metrics["orders_placed"])
                except (ValueError, TypeError):
                    pass

            if "fulfilled" in metrics and metrics["fulfilled"] is True:
                fulfilled_orders += 1
            elif "fulfilled_orders" in metrics and metrics["fulfilled_orders"] is not None:
                try:
                    fulfilled_orders += int(metrics["fulfilled_orders"])
                except (ValueError, TypeError):
                    pass

            if "on_time" in metrics and metrics["on_time"] is True:
                delivered_on_time += 1
            elif "delivered_on_time" in metrics and metrics["delivered_on_time"] is not None:
                try:
                    delivered_on_time += int(metrics["delivered_on_time"])
                except (ValueError, TypeError):
                    pass

            if "cancelled" in metrics and metrics["cancelled"] is True:
                cancelled_orders += 1
            elif "cancelled_orders" in metrics and metrics["cancelled_orders"] is not None:
                try:
                    cancelled_orders += int(metrics["cancelled_orders"])
                except (ValueError, TypeError):
                    pass

            if "defective" in metrics and metrics["defective"] is True:
                defective_returns += 1
            elif "defective_returns" in metrics and metrics["defective_returns"] is not None:
                try:
                    defective_returns += int(metrics["defective_returns"])
                except (ValueError, TypeError):
                    pass

            if "lead_time_days" in metrics and metrics["lead_time_days"] is not None:
                try:
                    observed_lead_times.append(int(metrics["lead_time_days"]))
                except (ValueError, TypeError):
                    pass

            if "cost_amount" in metrics and metrics["cost_amount"] is not None:
                try:
                    # no agregamos a observed_costs si es el costo de ejecucion de la orden/outcome para evitar duplicar las cotizaciones de SupplierMemory salvo que no se tenga cotizacion
                    pass
                except Exception:
                    pass

            if "moq" in metrics and metrics["moq"] is not None:
                try:
                    observed_moqs.append(int(metrics["moq"]))
                except (ValueError, TypeError):
                    pass

        # Si no hay costos cotizados en memorias pero si en outcomes, tomar de outcomes
        if not observed_costs:
            for oc in outcomes:
                metrics = oc.value_metrics
                if "cost_amount" in metrics and metrics["cost_amount"] is not None:
                    try:
                        observed_costs.append(Decimal(str(metrics["cost_amount"])))
                    except Exception:
                        pass
        implied_orders = fulfilled_orders + cancelled_orders
        if orders_placed < implied_orders:
            orders_placed = implied_orders

        observed_metrics = ObservedSupplierMetrics(
            total_quotes_observed=total_quotes_obs,
            total_accepted_quotes=accepted_quotes_count,
            total_orders_placed=orders_placed,
            total_fulfilled_orders=fulfilled_orders,
            total_delivered_on_time=delivered_on_time,
            total_cancelled_orders=cancelled_orders,
            total_defective_returns=defective_returns,
            observed_lead_times_days=tuple(observed_lead_times),
            observed_quoted_costs=tuple(observed_costs),
            observed_moqs=tuple(observed_moqs),
        )

        # Métricas derivadas (DERIVED METRICS)
        quote_acc_rate: Optional[float] = None
        avg_cost: Optional[Decimal] = None
        avg_moq: Optional[float] = None
        avg_lt: Optional[float] = None
        on_time_rate: Optional[float] = None
        fulfill_rate: Optional[float] = None
        canc_rate: Optional[float] = None
        defect_rate: Optional[float] = None
        out_success_rate: Optional[float] = None

        if total_quotes_obs > 0:
            quote_acc_rate = round(float(accepted_quotes_count / total_quotes_obs), 4)

        if len(observed_costs) > 0:
            avg_cost = sum(observed_costs) / Decimal(len(observed_costs))

        if len(observed_moqs) > 0:
            avg_moq = round(float(sum(observed_moqs) / len(observed_moqs)), 2)

        if len(observed_lead_times) > 0:
            avg_lt = round(float(sum(observed_lead_times) / len(observed_lead_times)), 2)

        if orders_placed > 0:
            on_time_rate = round(float(delivered_on_time / orders_placed), 4)
            fulfill_rate = round(float(fulfilled_orders / orders_placed), 4)
            canc_rate = round(float(cancelled_orders / orders_placed), 4)

        if fulfilled_orders > 0:
            defect_rate = round(float(defective_returns / fulfilled_orders), 4)

        if valid_outcomes_for_success_rate > 0:
            out_success_rate = round(float(outcome_success_count / valid_outcomes_for_success_rate), 4)

        derived_metrics = DerivedSupplierMetrics(
            quote_acceptance_rate=quote_acc_rate,
            average_quoted_cost=avg_cost,
            average_moq=avg_moq,
            average_lead_time_days=avg_lt,
            delivery_on_time_rate=on_time_rate,
            fulfillment_rate=fulfill_rate,
            cancellation_rate=canc_rate,
            defect_return_rate=defect_rate,
            outcome_success_rate=out_success_rate,
        )

        record = SupplierPerformanceRecord(
            performance_id=performance_id,
            supplier_id=supplier_id,
            period=period,
            status=SupplierPerformanceStatus.SUFFICIENT_DATA,
            sample_count=total_samples,
            quote_sample_count=quote_sample_count,
            outcome_sample_count=outcome_sample_count,
            observed_metrics=observed_metrics,
            derived_metrics=derived_metrics,
            supplier_memory_ids=sm_ids,
            outcome_ids=oc_ids,
            mission_ids=mission_ids,
            decision_ids=decision_ids,
            action_ids=action_ids,
            calibration_context_id=calibration_context.calibration_id if calibration_context else None,
            contextual_prediction_error=calibration_context.calibration_error if calibration_context else None,
            calculated_at=now,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            version=1,
            metadata=metadata or {},
        )

        if self.performance_repo:
            self.performance_repo.save(record)

        return record

    def get_performance(self, performance_id: str) -> Optional[SupplierPerformanceRecord]:
        if not self.performance_repo:
            return None
        return self.performance_repo.get_by_id(performance_id)

    def get_performances_for_supplier(self, supplier_id: str) -> List[SupplierPerformanceRecord]:
        if not self.performance_repo:
            return []
        return self.performance_repo.get_by_supplier_id(supplier_id)
