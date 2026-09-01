import uuid
from datetime import timezone
from typing import Optional, List

from src.domain.outcome.models import OutcomeRecord, OutcomeStatus
from src.domain.prediction.models import PredictionComparison, ComparisonStatus
from src.domain.calibration.models import DecisionCalibrationRecord, CalibrationStatus
from src.domain.product_performance.models import ProductPerformanceRecord, PerformanceStatus as ProductPerformanceStatus
from src.domain.supplier_performance.models import SupplierPerformanceRecord, SupplierPerformanceStatus
from src.domain.strategy_performance.models import StrategyPerformanceRecord, StrategyPerformanceStatus
from src.domain.learning_signals.models import (
    LearningSignalRecord,
    LearningSignalType,
    LearningSignalSubjectType,
    LearningSignalSourceType,
    SignalEvidenceClassification,
    SignalStatus,
)


class SignalGenerationError(Exception):
    """Excepción base para errores de generación de señales de aprendizaje."""
    pass


class LearningSignalGenerator:
    """
    Motor determinista de generación de señales de aprendizaje estructuradas (Task I.7).

    Reglas de Dominio:
    - Generación determinista basada exclusivamente en evidencia verificada.
    - Cero invención o alucinación.
    - UNKNOWN ≠ FAILURE, UNKNOWN ≠ SUCCESS. No se generan señales de Outcome cuando status == UNKNOWN.
    - INSUFFICIENT_DATA ≠ FAILURE/SUCCESS. No se generan señales de superioridad/inferioridad cuando performance == INSUFFICIENT_DATA.
    - Se preservan la clasificación de la evidencia (OBSERVED, DERIVED, INFERRED).
    - Preserva temporalidad y enlaces causales completos.
    - Las señales NUNCA son recomendaciones ni ejecutan acciones.
    """

    @staticmethod
    def generate_from_outcome(outcome: OutcomeRecord) -> Optional[LearningSignalRecord]:
        """
        Genera una señal de aprendizaje a partir de un OutcomeRecord (I.1).
        Si status es UNKNOWN o PENDING, NO genera señal POSITIVE/NEGATIVE_OUTCOME.
        """
        if outcome.status == OutcomeStatus.UNKNOWN or outcome.status == OutcomeStatus.PENDING:
            return None

        if outcome.status == OutcomeStatus.SUCCESS:
            sig_type = LearningSignalType.POSITIVE_OUTCOME
            summary = f"Positive outcome observed for mission {outcome.mission_id} action {outcome.action_id}"
        elif outcome.status == OutcomeStatus.FAILURE:
            sig_type = LearningSignalType.NEGATIVE_OUTCOME
            summary = f"Negative outcome observed for mission {outcome.mission_id} action {outcome.action_id}"
        elif outcome.status == OutcomeStatus.PARTIAL:
            sig_type = LearningSignalType.PARTIAL_OUTCOME
            summary = f"Partial outcome observed for mission {outcome.mission_id} action {outcome.action_id}"
        else:
            return None

        signal_id = f"sig-outcome-{outcome.outcome_id}"
        idempotency_key = f"idemp-sig-outcome-{outcome.idempotency_key}"

        return LearningSignalRecord(
            signal_id=signal_id,
            signal_type=sig_type,
            subject_type=LearningSignalSubjectType.ACTION,
            subject_id=outcome.action_id,
            source_type=LearningSignalSourceType.OUTCOME_TRACKING,
            source_id=outcome.outcome_id,
            evidence_classification=SignalEvidenceClassification.OBSERVED,
            status=SignalStatus.VALID,
            mission_id=outcome.mission_id,
            decision_id=outcome.decision_id,
            action_id=outcome.action_id,
            result_id=outcome.result_id,
            outcome_id=outcome.outcome_id,
            signal_value={
                "outcome_status": outcome.status.value,
                "outcome_type": outcome.outcome_type,
                "value_metrics": dict(outcome.value_metrics),
                "error_message": outcome.error_message,
            },
            summary=summary,
            observed_at=outcome.observed_at,
            evidence_reference=outcome.evidence_reference,
            confidence=outcome.confidence,
            provenance=outcome.provenance,
            correlation_id=outcome.correlation_id,
            idempotency_key=idempotency_key,
        )

    @staticmethod
    def generate_from_prediction_comparison(comparison: PredictionComparison) -> Optional[LearningSignalRecord]:
        """
        Genera una señal de aprendizaje a partir de una PredictionComparison (I.2).
        Si status es UNKNOWN, NO genera MATCH o MISS.
        """
        if comparison.status == ComparisonStatus.UNKNOWN:
            return None

        if comparison.status == ComparisonStatus.MATCH:
            sig_type = LearningSignalType.PREDICTION_MATCH
            summary = f"Prediction match on {comparison.target_metric} for decision {comparison.decision_id}"
        elif comparison.status == ComparisonStatus.MISS:
            sig_type = LearningSignalType.PREDICTION_MISS
            summary = f"Prediction miss on {comparison.target_metric} (delta={comparison.delta}) for decision {comparison.decision_id}"
        else:
            return None

        signal_id = f"sig-pred-{comparison.comparison_id}"
        idempotency_key = f"idemp-sig-pred-{comparison.idempotency_key}"

        return LearningSignalRecord(
            signal_id=signal_id,
            signal_type=sig_type,
            subject_type=LearningSignalSubjectType.PREDICTION,
            subject_id=comparison.prediction_id,
            source_type=LearningSignalSourceType.PREDICTION_COMPARISON,
            source_id=comparison.comparison_id,
            evidence_classification=SignalEvidenceClassification.DERIVED,
            status=SignalStatus.VALID,
            mission_id=comparison.mission_id,
            decision_id=comparison.decision_id,
            action_id=comparison.action_id,
            outcome_id=comparison.outcome_id,
            prediction_id=comparison.prediction_id,
            comparison_id=comparison.comparison_id,
            signal_value={
                "comparison_status": comparison.status.value,
                "target_metric": comparison.target_metric,
                "expected_value": comparison.expected_value,
                "actual_value": comparison.actual_value,
                "delta": comparison.delta,
            },
            summary=summary,
            observed_at=comparison.evaluated_at,
            confidence=comparison.prediction_confidence,
            provenance=comparison.outcome_provenance,
            correlation_id=comparison.correlation_id,
            idempotency_key=idempotency_key,
        )

    @staticmethod
    def generate_from_calibration(calibration: DecisionCalibrationRecord) -> Optional[LearningSignalRecord]:
        """
        Genera una señal de aprendizaje a partir de un DecisionCalibrationRecord (I.3).
        Si status es UNKNOWN o INSUFFICIENT_DATA, genera señal de INSUFFICIENT_DATA / DATA_QUALITY si corresponde.
        """
        if calibration.status in (CalibrationStatus.UNKNOWN, CalibrationStatus.NOT_CALIBRATED):
            return None

        if calibration.status == CalibrationStatus.INSUFFICIENT_DATA:
            sig_type = LearningSignalType.INSUFFICIENT_DATA
            summary = f"Insufficient data for calibration of decision {calibration.decision_id or 'aggregate'}"
        elif calibration.status == CalibrationStatus.OVER_CONFIDENT:
            sig_type = LearningSignalType.OVER_CONFIDENCE
            summary = f"Over-confidence detected (calibration_error={calibration.calibration_error}) for decision {calibration.decision_id or 'aggregate'}"
        elif calibration.status == CalibrationStatus.UNDER_CONFIDENT:
            sig_type = LearningSignalType.UNDER_CONFIDENCE
            summary = f"Under-confidence detected (calibration_error={calibration.calibration_error}) for decision {calibration.decision_id or 'aggregate'}"
        elif calibration.status == CalibrationStatus.WELL_CALIBRATED:
            sig_type = LearningSignalType.PREDICTION_MATCH
            summary = f"Well calibrated predictions for decision {calibration.decision_id or 'aggregate'}"
        else:
            return None

        signal_id = f"sig-calib-{calibration.calibration_id}"
        idempotency_key = f"idemp-sig-calib-{calibration.idempotency_key}"
        subj_id = calibration.decision_id or calibration.calibration_id

        return LearningSignalRecord(
            signal_id=signal_id,
            signal_type=sig_type,
            subject_type=LearningSignalSubjectType.DECISION,
            subject_id=subj_id,
            source_type=LearningSignalSourceType.DECISION_CALIBRATION,
            source_id=calibration.calibration_id,
            evidence_classification=SignalEvidenceClassification.DERIVED,
            status=SignalStatus.VALID,
            mission_id=calibration.mission_id,
            decision_id=calibration.decision_id,
            calibration_id=calibration.calibration_id,
            signal_value={
                "calibration_status": calibration.status.value,
                "accuracy": calibration.accuracy,
                "error_rate": calibration.error_rate,
                "calibration_error": calibration.calibration_error,
                "brier_score": calibration.brier_score,
                "total_samples": calibration.total_samples,
                "valid_samples": calibration.valid_samples,
                "prediction_ids": list(calibration.prediction_ids),
                "outcome_ids": list(calibration.outcome_ids),
                "comparison_ids": list(calibration.comparison_ids),
            },
            summary=summary,
            observed_at=calibration.calculated_at,
            correlation_id=calibration.correlation_id,
            idempotency_key=idempotency_key,
        )

    @staticmethod
    def generate_from_product_performance(perf: ProductPerformanceRecord) -> Optional[LearningSignalRecord]:
        """
        Genera una señal de aprendizaje a partir de un ProductPerformanceRecord (I.4).
        """
        if perf.status in (ProductPerformanceStatus.UNKNOWN, ProductPerformanceStatus.INSUFFICIENT_DATA):
            sig_type = LearningSignalType.INSUFFICIENT_DATA
            summary = f"Insufficient data for product performance of SKU {perf.sku}"
        else:
            sig_type = LearningSignalType.PRODUCT_PERFORMANCE
            summary = f"Product performance measured for SKU {perf.sku} (outcome_success_rate={perf.derived_metrics.outcome_success_rate})"

        signal_id = f"sig-prod-{perf.performance_id}"
        idempotency_key = f"idemp-sig-prod-{perf.idempotency_key}"

        return LearningSignalRecord(
            signal_id=signal_id,
            signal_type=sig_type,
            subject_type=LearningSignalSubjectType.PRODUCT,
            subject_id=perf.sku,
            source_type=LearningSignalSourceType.PRODUCT_PERFORMANCE,
            source_id=perf.performance_id,
            evidence_classification=SignalEvidenceClassification.DERIVED,
            status=SignalStatus.VALID,
            mission_id=perf.mission_ids[0] if perf.mission_ids else None,
            decision_id=perf.decision_ids[0] if perf.decision_ids else None,
            outcome_id=perf.outcome_ids[0] if perf.outcome_ids else None,
            product_performance_id=perf.performance_id,
            signal_value={
                "performance_status": perf.status.value,
                "product_id": perf.product_id,
                "sku": perf.sku,
                "sample_count": perf.sample_count,
                "observed_sales_units": perf.observed_metrics.observed_sales_units,
                "outcome_success_rate": perf.derived_metrics.outcome_success_rate,
                "return_rate": perf.derived_metrics.return_rate,
                "gross_margin_percentage": perf.derived_metrics.gross_margin_percentage,
            },
            summary=summary,
            observed_at=perf.calculated_at,
            confidence=perf.confidence,
            provenance=perf.provenance,
            correlation_id=perf.correlation_id,
            idempotency_key=idempotency_key,
        )

    @staticmethod
    def generate_from_supplier_performance(perf: SupplierPerformanceRecord) -> Optional[LearningSignalRecord]:
        """
        Genera una señal de aprendizaje a partir de un SupplierPerformanceRecord (I.5).
        """
        if perf.status in (SupplierPerformanceStatus.UNKNOWN, SupplierPerformanceStatus.INSUFFICIENT_DATA):
            sig_type = LearningSignalType.INSUFFICIENT_DATA
            summary = f"Insufficient data for supplier performance of supplier {perf.supplier_id}"
        else:
            sig_type = LearningSignalType.SUPPLIER_PERFORMANCE
            summary = f"Supplier performance measured for supplier {perf.supplier_id} (outcome_success_rate={perf.derived_metrics.outcome_success_rate})"

        signal_id = f"sig-supp-{perf.performance_id}"
        idempotency_key = f"idemp-sig-supp-{perf.idempotency_key}"

        return LearningSignalRecord(
            signal_id=signal_id,
            signal_type=sig_type,
            subject_type=LearningSignalSubjectType.SUPPLIER,
            subject_id=perf.supplier_id,
            source_type=LearningSignalSourceType.SUPPLIER_PERFORMANCE,
            source_id=perf.performance_id,
            evidence_classification=SignalEvidenceClassification.DERIVED,
            status=SignalStatus.VALID,
            mission_id=perf.mission_ids[0] if perf.mission_ids else None,
            decision_id=perf.decision_ids[0] if perf.decision_ids else None,
            action_id=perf.action_ids[0] if perf.action_ids else None,
            outcome_id=perf.outcome_ids[0] if perf.outcome_ids else None,
            supplier_performance_id=perf.performance_id,
            signal_value={
                "performance_status": perf.status.value,
                "supplier_id": perf.supplier_id,
                "sample_count": perf.sample_count,
                "total_orders_placed": perf.observed_metrics.total_orders_placed,
                "total_fulfilled_orders": perf.observed_metrics.total_fulfilled_orders,
                "outcome_success_rate": perf.derived_metrics.outcome_success_rate,
                "delivery_on_time_rate": perf.derived_metrics.delivery_on_time_rate,
                "defect_return_rate": perf.derived_metrics.defect_return_rate,
            },
            summary=summary,
            observed_at=perf.calculated_at,
            confidence=perf.confidence,
            provenance=perf.provenance,
            correlation_id=perf.correlation_id,
            idempotency_key=idempotency_key,
        )

    @staticmethod
    def generate_from_strategy_performance(perf: StrategyPerformanceRecord) -> Optional[LearningSignalRecord]:
        """
        Genera una señal de aprendizaje a partir de un StrategyPerformanceRecord (I.6).
        """
        if perf.status in (StrategyPerformanceStatus.UNKNOWN, StrategyPerformanceStatus.INSUFFICIENT_DATA):
            sig_type = LearningSignalType.INSUFFICIENT_DATA
            summary = f"Insufficient data for strategy performance of strategy {perf.strategy_id}"
        else:
            sig_type = LearningSignalType.STRATEGY_PERFORMANCE
            summary = f"Strategy performance measured for strategy {perf.strategy_id} (success_rate={perf.derived_metrics.success_rate})"

        signal_id = f"sig-strat-{perf.performance_id}"
        idempotency_key = f"idemp-sig-strat-{perf.idempotency_key}"

        return LearningSignalRecord(
            signal_id=signal_id,
            signal_type=sig_type,
            subject_type=LearningSignalSubjectType.STRATEGY,
            subject_id=perf.strategy_id,
            source_type=LearningSignalSourceType.STRATEGY_PERFORMANCE,
            source_id=perf.performance_id,
            evidence_classification=SignalEvidenceClassification.DERIVED,
            status=SignalStatus.VALID,
            mission_id=perf.mission_ids[0] if perf.mission_ids else None,
            decision_id=perf.decision_ids[0] if perf.decision_ids else None,
            action_id=perf.action_ids[0] if perf.action_ids else None,
            outcome_id=perf.outcome_ids[0] if perf.outcome_ids else None,
            strategy_performance_id=perf.performance_id,
            signal_value={
                "performance_status": perf.status.value,
                "strategy_id": perf.strategy_id,
                "sample_count": perf.sample_count,
                "success_count": perf.observed_metrics.success_count,
                "failure_count": perf.observed_metrics.failure_count,
                "success_rate": perf.derived_metrics.success_rate,
                "return_rate": perf.derived_metrics.return_rate,
                "average_margin_percentage": perf.derived_metrics.average_margin_percentage,
            },
            summary=summary,
            observed_at=perf.calculated_at,
            confidence=perf.confidence,
            provenance=perf.provenance,
            correlation_id=perf.correlation_id,
            idempotency_key=idempotency_key,
        )
