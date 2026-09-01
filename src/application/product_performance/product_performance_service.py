from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, List, Dict, Any, Tuple
import math

from src.domain.market_intelligence.models import Confidence
from src.domain.supplier_intelligence.models import EvidenceProvenanceType
from src.domain.product_memory.models import ProductMemoryRecord
from src.domain.outcome.models import OutcomeRecord, OutcomeStatus
from src.domain.calibration.models import DecisionCalibrationRecord
from src.domain.product_performance.models import (
    ProductPerformanceRecord,
    PerformanceStatus,
    TemporalPeriod,
    ObservedProductMetrics,
    DerivedProductMetrics,
)
from src.domain.product_performance.ports import ProductPerformanceRepository

DEFAULT_MIN_SAMPLE_THRESHOLD = 1


class ProductPerformanceService:
    """
    Servicio de Aplicación para calcular la Performance Observable de Productos (Task I.4).
    Combina observaciones verdaderas de Product Memory y Outcomes reales de negocio.

    Propiedades clave:
    1. Determinista: Mismo dataset -> Mismo resultado exacto.
    2. Idempotente: Retorna registro existente si se especifica idempotency_key.
    3. Preservación de procedencia y diferenciación explicita entre OBSERVED y DERIVED.
    4. Cero invención de datos: Si falta costo, no calcula margen; si falta venta/denominador, no calcula tasa.
    5. Manejo seguro de UNKNOWN / Data Quality: Si no hay muestras suficientes -> INSUFFICIENT_DATA.
    6. Sanitización PII / Credenciales: Excluye cualquier dato sensible.
    7. Trazabilidad causal completa: Mantiene referencias a product_memory_ids, outcome_ids, mission_ids, decision_ids.
    """

    def __init__(
        self,
        performance_repo: Optional[ProductPerformanceRepository] = None,
        min_sample_threshold: int = DEFAULT_MIN_SAMPLE_THRESHOLD,
    ):
        self.performance_repo = performance_repo
        self.min_sample_threshold = min_sample_threshold

    def calculate_performance(
        self,
        performance_id: str,
        product_id: str,
        sku: str,
        period: TemporalPeriod,
        product_records: Optional[List[ProductMemoryRecord]] = None,
        outcome_records: Optional[List[OutcomeRecord]] = None,
        calibration_context: Optional[DecisionCalibrationRecord] = None,
        calculated_at: Optional[datetime] = None,
        correlation_id: str = "default-correlation",
        idempotency_key: str = "default-idempotency",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ProductPerformanceRecord:
        """
        Calcula deterministamente ProductPerformanceRecord combinando registros de producto y outcomes observados.
        """
        if self.performance_repo and idempotency_key:
            existing = self.performance_repo.get_performance_by_idempotency_key(idempotency_key)
            if existing:
                return existing

        now = calculated_at or datetime.now(timezone.utc)
        memories = product_records or []
        outcomes = outcome_records or []

        # Filtrar registros por período temporal si corresponden
        if period.period_start or period.period_end:
            filtered_memories = []
            for pm in memories:
                if period.period_start and pm.observed_at < period.period_start:
                    continue
                if period.period_end and pm.observed_at > period.period_end:
                    continue
                filtered_memories.append(pm)
            memories = filtered_memories

            filtered_outcomes = []
            for oc in outcomes:
                if period.period_start and oc.observed_at < period.period_start:
                    continue
                if period.period_end and oc.observed_at > period.period_end:
                    continue
                filtered_outcomes.append(oc)
            outcomes = filtered_outcomes

        obs_count = len(memories)
        out_count = len(outcomes)
        total_samples = obs_count + out_count

        # Trazabilidad de IDs (ordenados para determinismo)
        pm_ids = tuple(sorted(list({m.product_memory_id for m in memories})))
        oc_ids = tuple(sorted(list({o.outcome_id for o in outcomes})))
        mission_ids = tuple(sorted(list({o.mission_id for o in outcomes if o.mission_id})))
        decision_ids = tuple(sorted(list({o.decision_id for o in outcomes if o.decision_id})))

        # Si el conteo total de muestras es inferior al umbral -> INSUFFICIENT_DATA
        if total_samples < self.min_sample_threshold:
            record = ProductPerformanceRecord(
                performance_id=performance_id,
                product_id=product_id,
                sku=sku,
                period=period,
                status=PerformanceStatus.INSUFFICIENT_DATA,
                sample_count=total_samples,
                observation_sample_count=obs_count,
                outcome_sample_count=out_count,
                observed_metrics=ObservedProductMetrics(),
                derived_metrics=DerivedProductMetrics(),
                product_memory_ids=pm_ids,
                outcome_ids=oc_ids,
                mission_ids=mission_ids,
                decision_ids=decision_ids,
                calibration_context_id=calibration_context.calibration_id if calibration_context else None,
                contextual_prediction_error=calibration_context.calibration_error if calibration_context else None,
                calculated_at=now,
                correlation_id=correlation_id,
                idempotency_key=idempotency_key,
                version=1,
                metadata=metadata or {},
            )
            if self.performance_repo:
                self.performance_repo.save_performance(record)
            return record

        # Extracción y agregación de observaciones reales (OBSERVED METRICS)
        sales_units_sum: Optional[int] = None
        revenue_sum: Optional[Decimal] = None
        cancellations_sum: Optional[int] = None
        returns_sum: Optional[int] = None
        latest_stock: Optional[int] = None
        latest_price: Optional[Decimal] = None
        latest_cost: Optional[Decimal] = None

        # 1. Extraer desde product memories
        latest_pm_date = None
        for pm in memories:
            if pm.sold_quantity is not None:
                sales_units_sum = (sales_units_sum or 0) + pm.sold_quantity
                if pm.price_amount is not None and pm.price_amount > 0:
                    added_rev = pm.price_amount * Decimal(pm.sold_quantity)
                    revenue_sum = (revenue_sum or Decimal("0")) + added_rev

            if latest_pm_date is None or pm.observed_at >= latest_pm_date:
                latest_pm_date = pm.observed_at
                latest_stock = pm.available_quantity
                latest_price = pm.price_amount
                if "cost" in pm.metadata and pm.metadata["cost"] is not None:
                    try:
                        latest_cost = Decimal(str(pm.metadata["cost"]))
                    except Exception:
                        pass

        # 2. Extraer desde outcomes observados de negocio
        outcome_success_count = 0
        valid_outcomes_for_success_rate = 0

        for oc in outcomes:
            if oc.status == OutcomeStatus.SUCCESS:
                outcome_success_count += 1
                valid_outcomes_for_success_rate += 1
            elif oc.status in (OutcomeStatus.FAILURE, OutcomeStatus.PARTIAL, OutcomeStatus.CANCELLED):
                valid_outcomes_for_success_rate += 1
            # OutcomeStatus.UNKNOWN o PENDING no cuentan como denominadores de éxito válido

            metrics = oc.value_metrics
            if "units_sold" in metrics and metrics["units_sold"] is not None:
                try:
                    u_sold = int(metrics["units_sold"])
                    sales_units_sum = (sales_units_sum or 0) + u_sold
                except (ValueError, TypeError):
                    pass

            if "revenue" in metrics and metrics["revenue"] is not None:
                try:
                    rev = Decimal(str(metrics["revenue"]))
                    revenue_sum = (revenue_sum or Decimal("0")) + rev
                except Exception:
                    pass

            if "cancellations" in metrics and metrics["cancellations"] is not None:
                try:
                    canc = int(metrics["cancellations"])
                    cancellations_sum = (cancellations_sum or 0) + canc
                except (ValueError, TypeError):
                    pass

            if "returns" in metrics and metrics["returns"] is not None:
                try:
                    ret = int(metrics["returns"])
                    returns_sum = (returns_sum or 0) + ret
                except (ValueError, TypeError):
                    pass

            if "cost" in metrics and metrics["cost"] is not None:
                try:
                    latest_cost = Decimal(str(metrics["cost"]))
                except Exception:
                    pass

            if "price" in metrics and metrics["price"] is not None:
                try:
                    latest_price = Decimal(str(metrics["price"]))
                except Exception:
                    pass

        observed_metrics = ObservedProductMetrics(
            observed_sales_units=sales_units_sum,
            observed_revenue=revenue_sum,
            observed_cancellations_units=cancellations_sum,
            observed_returns_units=returns_sum,
            observed_stock_level=latest_stock,
            observed_price=latest_price,
            observed_cost=latest_cost,
        )

        # Cálculo de métricas derivadas (DERIVED METRICS) sin suposiciones
        gross_margin_amount: Optional[Decimal] = None
        gross_margin_pct: Optional[float] = None
        canc_rate: Optional[float] = None
        ret_rate: Optional[float] = None
        out_success_rate: Optional[float] = None
        avg_price: Optional[Decimal] = None

        # Gross margin sólo si existen price y cost
        if latest_price is not None and latest_cost is not None and latest_price > 0:
            gross_margin_amount = latest_price - latest_cost
            gross_margin_pct = round(float(gross_margin_amount / latest_price), 4)

        # Average selling price
        if revenue_sum is not None and sales_units_sum is not None and sales_units_sum > 0:
            avg_price = revenue_sum / Decimal(sales_units_sum)

        # Cancellation rate sólo si existen cancellation units y sales_units denominador
        if cancellations_sum is not None and sales_units_sum is not None and sales_units_sum > 0:
            canc_rate = round(float(cancellations_sum / sales_units_sum), 4)

        # Return rate sólo si existen return units y sales_units denominador
        if returns_sum is not None and sales_units_sum is not None and sales_units_sum > 0:
            ret_rate = round(float(returns_sum / sales_units_sum), 4)

        # Outcome success rate
        if valid_outcomes_for_success_rate > 0:
            out_success_rate = round(float(outcome_success_count / valid_outcomes_for_success_rate), 4)

        derived_metrics = DerivedProductMetrics(
            gross_margin_amount=gross_margin_amount,
            gross_margin_percentage=gross_margin_pct,
            cancellation_rate=canc_rate,
            return_rate=ret_rate,
            outcome_success_rate=out_success_rate,
            average_selling_price=avg_price,
        )

        record = ProductPerformanceRecord(
            performance_id=performance_id,
            product_id=product_id,
            sku=sku,
            period=period,
            status=PerformanceStatus.SUFFICIENT_DATA,
            sample_count=total_samples,
            observation_sample_count=obs_count,
            outcome_sample_count=out_count,
            observed_metrics=observed_metrics,
            derived_metrics=derived_metrics,
            product_memory_ids=pm_ids,
            outcome_ids=oc_ids,
            mission_ids=mission_ids,
            decision_ids=decision_ids,
            calibration_context_id=calibration_context.calibration_id if calibration_context else None,
            contextual_prediction_error=calibration_context.calibration_error if calibration_context else None,
            calculated_at=now,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            version=1,
            metadata=metadata or {},
        )

        if self.performance_repo:
            self.performance_repo.save_performance(record)

        return record

    def get_performance(self, performance_id: str) -> Optional[ProductPerformanceRecord]:
        if not self.performance_repo:
            return None
        return self.performance_repo.get_performance_by_id(performance_id)

    def get_performances_for_product(self, product_id: str) -> List[ProductPerformanceRecord]:
        if not self.performance_repo:
            return []
        return self.performance_repo.get_performances_by_product_id(product_id)

    def get_performances_for_sku(self, sku: str) -> List[ProductPerformanceRecord]:
        if not self.performance_repo:
            return []
        return self.performance_repo.get_performances_by_sku(sku)
