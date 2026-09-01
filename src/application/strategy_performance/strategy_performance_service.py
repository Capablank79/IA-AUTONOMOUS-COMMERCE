from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, List, Dict, Any, Tuple
import math

from src.domain.market_intelligence.models import Confidence
from src.domain.supplier_intelligence.models import EvidenceProvenanceType
from src.domain.decision.models import DecisionRecord, DecisionOutcome
from src.domain.outcome.models import OutcomeRecord, OutcomeStatus
from src.domain.calibration.models import DecisionCalibrationRecord
from src.domain.product_performance.models import ProductPerformanceRecord
from src.domain.supplier_performance.models import SupplierPerformanceRecord
from src.domain.strategy_performance.models import (
    StrategyPerformanceRecord,
    StrategyPerformanceStatus,
    StrategyTemporalPeriod,
    ObservedStrategyMetrics,
    DerivedStrategyMetrics,
)
from src.domain.strategy_performance.ports import StrategyPerformanceRepositoryPort

DEFAULT_MIN_SAMPLE_THRESHOLD = 1

SENSITIVE_KEYS = {
    "password", "secret", "token", "api_key", "apikey",
    "access_token", "refresh_token", "auth", "authorization",
    "private_key", "credentials", "payment", "card"
}


def _sanitize_metadata(data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not data:
        return {}
    sanitized = {}
    for k, v in data.items():
        if any(s in k.lower() for s in SENSITIVE_KEYS):
            continue
        if isinstance(v, dict):
            sanitized[k] = _sanitize_metadata(v)
        else:
            sanitized[k] = v
    return sanitized


class StrategyPerformanceService:
    """
    Servicio de Aplicación para medir el Desempeño Observable de Estrategias (Task I.6).
    Reutiliza y agrega evidencias observadas de decisiones, acciones, resultados y outcomes reales de negocio.

    Reglas y Garantías de Dominio:
    1. Responde determinísticamente: "What strategy was used + What happened = How the strategy performed".
    2. NO modifica políticas, NO recalibra decisiones, NO genera reglas ni señales de aprendizaje (I.7).
    3. Idempotencia: Retorna registro existente si se especifica idempotency_key.
    4. Trazabilidad causal completa: Decision -> Action -> Result -> Outcome -> Performance.
    5. Cero invención: Métrica faltante se preserva como None (UNKNOWN), missing profit != 0 profit.
    6. Exclusión estricta de credenciales y datos sensibles.
    """

    def __init__(
        self,
        performance_repo: Optional[StrategyPerformanceRepositoryPort] = None,
        min_sample_threshold: int = DEFAULT_MIN_SAMPLE_THRESHOLD,
    ):
        self.performance_repo = performance_repo
        self.min_sample_threshold = min_sample_threshold

    def calculate_performance(
        self,
        performance_id: str,
        strategy_id: str,
        period: StrategyTemporalPeriod,
        decision_records: Optional[List[DecisionRecord]] = None,
        action_records: Optional[List[Any]] = None,
        result_records: Optional[List[Any]] = None,
        outcome_records: Optional[List[OutcomeRecord]] = None,
        calibration_context: Optional[DecisionCalibrationRecord] = None,
        product_performance_records: Optional[List[ProductPerformanceRecord]] = None,
        supplier_performance_records: Optional[List[SupplierPerformanceRecord]] = None,
        calculated_at: Optional[datetime] = None,
        correlation_id: str = "default-correlation",
        idempotency_key: str = "default-idempotency",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> StrategyPerformanceRecord:
        """
        Calcula determinísticamente StrategyPerformanceRecord combinando registros observados.
        """
        if self.performance_repo and idempotency_key:
            existing = self.performance_repo.get_performance_by_idempotency_key(idempotency_key)
            if existing:
                return existing

        now = calculated_at or datetime.now(timezone.utc)
        sanitized_meta = _sanitize_metadata(metadata)

        decisions = decision_records or []
        actions = action_records or []
        results = result_records or []
        outcomes = outcome_records or []
        prod_perfs = product_performance_records or []
        supp_perfs = supplier_performance_records or []

        # Filtrado explícito por estrategia si el objeto contiene metadatos de estrategia
        filtered_decisions = []
        for d in decisions:
            explicit_strat = (
                d.parameters.get("strategy_id")
                or d.metadata.get("strategy_id")
                or d.parameters.get("strategy_type")
                or d.metadata.get("strategy_type")
                or d.future_action_type
                or d.decision_type.value
            )
            # Si el registro especifica una estrategia distinta a la evaluada, se descarta
            if (
                d.parameters.get("strategy_id") and d.parameters.get("strategy_id") != strategy_id
            ) or (
                d.metadata.get("strategy_id") and d.metadata.get("strategy_id") != strategy_id
            ):
                continue
            filtered_decisions.append(d)
        decisions = filtered_decisions

        # Filtrar por período temporal si corresponde
        if period.period_start or period.period_end:
            decisions = [
                d for d in decisions
                if (not period.period_start or d.created_at >= period.period_start)
                and (not period.period_end or d.created_at <= period.period_end)
            ]
            outcomes = [
                oc for oc in outcomes
                if (not period.period_start or oc.observed_at >= period.period_start)
                and (not period.period_end or oc.observed_at <= period.period_end)
            ]

        dec_count = len(decisions)
        act_count = len(actions)
        res_count = len(results)
        out_count = len(outcomes)
        total_samples = dec_count + act_count + out_count

        # Extracción trazable de IDs (ordenados determinísticamente)
        dec_ids = tuple(sorted(list({d.decision_id for d in decisions})))
        act_ids = tuple(sorted(list({
            getattr(a, "action_id", str(a)) for a in actions if getattr(a, "action_id", None)
        })))
        res_ids = tuple(sorted(list({
            getattr(r, "result_id", str(r)) for r in results if getattr(r, "result_id", None)
        })))
        out_ids = tuple(sorted(list({o.outcome_id for o in outcomes})))

        mission_ids_set = {d.mission_id for d in decisions if d.mission_id}
        mission_ids_set.update({o.mission_id for o in outcomes if o.mission_id})
        mission_ids = tuple(sorted(list(mission_ids_set)))

        prod_ids_set = {p.product_id for p in prod_perfs if p.product_id}
        for d in decisions:
            if "product_id" in d.parameters:
                prod_ids_set.add(str(d.parameters["product_id"]))
            if "product_id" in d.metadata:
                prod_ids_set.add(str(d.metadata["product_id"]))
        for o in outcomes:
            if "product_id" in o.value_metrics:
                prod_ids_set.add(str(o.value_metrics["product_id"]))
            if "product_id" in o.metadata:
                prod_ids_set.add(str(o.metadata["product_id"]))
        product_ids = tuple(sorted(list(prod_ids_set)))

        supp_ids_set = {s.supplier_id for s in supp_perfs if s.supplier_id}
        for d in decisions:
            if "supplier_id" in d.parameters:
                supp_ids_set.add(str(d.parameters["supplier_id"]))
            if "supplier_id" in d.metadata:
                supp_ids_set.add(str(d.metadata["supplier_id"]))
        for o in outcomes:
            if "supplier_id" in o.value_metrics:
                supp_ids_set.add(str(o.value_metrics["supplier_id"]))
            if "supplier_id" in o.metadata:
                supp_ids_set.add(str(o.metadata["supplier_id"]))
        supplier_ids = tuple(sorted(list(supp_ids_set)))

        # Insuficiencia de muestra
        if total_samples < self.min_sample_threshold:
            record = StrategyPerformanceRecord(
                performance_id=performance_id,
                strategy_id=strategy_id,
                period=period,
                status=StrategyPerformanceStatus.INSUFFICIENT_DATA,
                sample_count=total_samples,
                decision_sample_count=dec_count,
                action_sample_count=act_count,
                outcome_sample_count=out_count,
                observed_metrics=ObservedStrategyMetrics(
                    total_decisions_observed=dec_count,
                    total_actions_executed=act_count,
                    total_outcomes_observed=out_count,
                ),
                derived_metrics=DerivedStrategyMetrics(),
                decision_ids=dec_ids,
                action_ids=act_ids,
                result_ids=res_ids,
                outcome_ids=out_ids,
                mission_ids=mission_ids,
                product_ids=product_ids,
                supplier_ids=supplier_ids,
                calibration_context_id=calibration_context.calibration_id if calibration_context else None,
                contextual_prediction_error=calibration_context.calibration_error if calibration_context else None,
                calculated_at=now,
                correlation_id=correlation_id,
                idempotency_key=idempotency_key,
                version=1,
                metadata=sanitized_meta,
            )
            if self.performance_repo:
                self.performance_repo.save_performance(record)
            return record

        # Conteo de resultados observados (Observed Strategy Metrics)
        success_count = 0
        failure_count = 0
        partial_count = 0
        cancelled_count = 0
        unknown_count = 0
        observed_profit: Optional[Decimal] = None
        observed_revenue: Optional[Decimal] = None
        observed_cancellations = 0
        observed_returns = 0

        # Si existen outcomes observados de negocio
        if outcomes:
            for oc in outcomes:
                if oc.status == OutcomeStatus.SUCCESS:
                    success_count += 1
                elif oc.status == OutcomeStatus.FAILURE:
                    failure_count += 1
                elif oc.status == OutcomeStatus.PARTIAL:
                    partial_count += 1
                elif oc.status == OutcomeStatus.CANCELLED:
                    cancelled_count += 1
                else:
                    unknown_count += 1

                m = oc.value_metrics
                # Profit
                profit_val = m.get("profit") or m.get("realized_profit") or m.get("gross_margin_amount")
                if profit_val is not None:
                    try:
                        p_dec = Decimal(str(profit_val))
                        observed_profit = (observed_profit or Decimal("0")) + p_dec
                    except Exception:
                        pass

                # Revenue
                rev_val = m.get("revenue") or m.get("realized_revenue") or m.get("realized_revenue_clp")
                if rev_val is not None:
                    try:
                        r_dec = Decimal(str(rev_val))
                        observed_revenue = (observed_revenue or Decimal("0")) + r_dec
                    except Exception:
                        pass

                # Cancellations & Returns
                if "cancellations" in m and m["cancellations"] is not None:
                    try:
                        observed_cancellations += int(m["cancellations"])
                    except Exception:
                        pass

                if "returns" in m and m["returns"] is not None:
                    try:
                        observed_returns += int(m["returns"])
                    except Exception:
                        pass
        else:
            # Si no hay outcomes pero hay decisiones con outcome cualitativo
            for d in decisions:
                if d.outcome == DecisionOutcome.SUCCESS:
                    success_count += 1
                elif d.outcome in (DecisionOutcome.FAILURE, DecisionOutcome.CANCELLED):
                    failure_count += 1
                elif d.outcome == DecisionOutcome.PARTIAL:
                    partial_count += 1
                else:
                    unknown_count += 1

        obs_metrics = ObservedStrategyMetrics(
            total_decisions_observed=dec_count,
            total_actions_executed=act_count,
            total_outcomes_observed=out_count,
            success_count=success_count,
            failure_count=failure_count,
            partial_count=partial_count,
            cancelled_count=cancelled_count,
            unknown_count=unknown_count,
            observed_profit=observed_profit,
            observed_revenue=observed_revenue,
            observed_cancellations=observed_cancellations,
            observed_returns=observed_returns,
        )

        # Métricas Derivadas determinísticas (Derived Strategy Metrics)
        total_evaluable = success_count + failure_count + partial_count + cancelled_count
        success_rate: Optional[float] = None
        outcome_success_rate: Optional[float] = None
        failure_rate: Optional[float] = None
        cancellation_rate: Optional[float] = None
        return_rate: Optional[float] = None
        avg_profit: Optional[Decimal] = None
        avg_revenue: Optional[Decimal] = None
        avg_margin_pct: Optional[float] = None

        if total_evaluable > 0:
            success_rate = round(float(success_count / total_evaluable), 4)
            failure_rate = round(float(failure_count / total_evaluable), 4)

        if out_count > 0 and (success_count + failure_count + partial_count) > 0:
            outcome_success_rate = round(float(success_count / (success_count + failure_count + partial_count)), 4)

        if act_count > 0 and observed_cancellations > 0:
            cancellation_rate = round(float(observed_cancellations / act_count), 4)

        if act_count > 0 and observed_returns > 0:
            return_rate = round(float(observed_returns / act_count), 4)

        if observed_profit is not None and success_count > 0:
            avg_profit = observed_profit / Decimal(success_count)

        if observed_revenue is not None and total_evaluable > 0:
            avg_revenue = observed_revenue / Decimal(total_evaluable)

        if observed_revenue is not None and observed_profit is not None and observed_revenue > 0:
            avg_margin_pct = round(float(observed_profit / observed_revenue), 4)

        derived_metrics = DerivedStrategyMetrics(
            success_rate=success_rate,
            outcome_success_rate=outcome_success_rate,
            failure_rate=failure_rate,
            cancellation_rate=cancellation_rate,
            return_rate=return_rate,
            average_realized_profit=avg_profit,
            average_margin_percentage=avg_margin_pct,
            average_realized_revenue=avg_revenue,
        )

        record = StrategyPerformanceRecord(
            performance_id=performance_id,
            strategy_id=strategy_id,
            period=period,
            status=StrategyPerformanceStatus.SUFFICIENT_DATA,
            sample_count=total_samples,
            decision_sample_count=dec_count,
            action_sample_count=act_count,
            outcome_sample_count=out_count,
            observed_metrics=obs_metrics,
            derived_metrics=derived_metrics,
            decision_ids=dec_ids,
            action_ids=act_ids,
            result_ids=res_ids,
            outcome_ids=out_ids,
            mission_ids=mission_ids,
            product_ids=product_ids,
            supplier_ids=supplier_ids,
            calibration_context_id=calibration_context.calibration_id if calibration_context else None,
            contextual_prediction_error=calibration_context.calibration_error if calibration_context else None,
            calculated_at=now,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            version=1,
            metadata=sanitized_meta,
        )

        if self.performance_repo:
            self.performance_repo.save_performance(record)

        return record

    def get_performance(self, performance_id: str) -> Optional[StrategyPerformanceRecord]:
        if not self.performance_repo:
            return None
        return self.performance_repo.get_performance_by_id(performance_id)

    def get_performances_for_strategy(self, strategy_id: str) -> List[StrategyPerformanceRecord]:
        if not self.performance_repo:
            return []
        return self.performance_repo.get_performances_by_strategy_id(strategy_id)
