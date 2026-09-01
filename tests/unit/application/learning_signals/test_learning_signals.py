import pytest
from datetime import datetime, timezone
from decimal import Decimal

from src.domain.outcome.models import OutcomeRecord, OutcomeStatus
from src.domain.prediction.models import PredictionComparison, ComparisonStatus
from src.domain.calibration.models import DecisionCalibrationRecord, CalibrationStatus
from src.domain.product_performance.models import ProductPerformanceRecord, PerformanceStatus as ProductPerformanceStatus, ObservedProductMetrics, DerivedProductMetrics, TemporalPeriod
from src.domain.supplier_performance.models import SupplierPerformanceRecord, SupplierPerformanceStatus, ObservedSupplierMetrics, DerivedSupplierMetrics, SupplierTemporalPeriod
from src.domain.strategy_performance.models import StrategyPerformanceRecord, StrategyPerformanceStatus, ObservedStrategyMetrics, DerivedStrategyMetrics, StrategyTemporalPeriod
from src.domain.learning_signals.models import (
    LearningSignalRecord,
    LearningSignalType,
    LearningSignalSubjectType,
    LearningSignalSourceType,
    SignalEvidenceClassification,
    SignalStatus,
)
from src.domain.learning_signals.services import LearningSignalGenerator


def test_outcome_signal_positive_and_negative():
    pos_outcome = OutcomeRecord(
        outcome_id="out-1",
        mission_id="m-1",
        decision_id="d-1",
        action_id="a-1",
        status=OutcomeStatus.SUCCESS,
        value_metrics={"revenue": "1000"},
        correlation_id="corr-1",
        idempotency_key="idemp-out-1",
    )
    sig_pos = LearningSignalGenerator.generate_from_outcome(pos_outcome)
    assert sig_pos is not None
    assert sig_pos.signal_type == LearningSignalType.POSITIVE_OUTCOME
    assert sig_pos.evidence_classification == SignalEvidenceClassification.OBSERVED
    assert sig_pos.action_id == "a-1"
    assert sig_pos.mission_id == "m-1"

    neg_outcome = OutcomeRecord(
        outcome_id="out-2",
        mission_id="m-1",
        decision_id="d-1",
        action_id="a-2",
        status=OutcomeStatus.FAILURE,
        error_message="Action failed",
        correlation_id="corr-1",
        idempotency_key="idemp-out-2",
    )
    sig_neg = LearningSignalGenerator.generate_from_outcome(neg_outcome)
    assert sig_neg is not None
    assert sig_neg.signal_type == LearningSignalType.NEGATIVE_OUTCOME
    assert sig_neg.evidence_classification == SignalEvidenceClassification.OBSERVED


def test_outcome_signal_unknown_handling():
    unk_outcome = OutcomeRecord(
        outcome_id="out-unk",
        mission_id="m-1",
        decision_id="d-1",
        action_id="a-1",
        status=OutcomeStatus.UNKNOWN,
    )
    sig_unk = LearningSignalGenerator.generate_from_outcome(unk_outcome)
    assert sig_unk is None, "UNKNOWN outcome must not generate POSITIVE/NEGATIVE signal"


def test_prediction_comparison_match_and_miss():
    match_comp = PredictionComparison(
        comparison_id="comp-1",
        prediction_id="p-1",
        outcome_id="out-1",
        mission_id="m-1",
        decision_id="d-1",
        target_metric="margin",
        status=ComparisonStatus.MATCH,
    )
    sig_match = LearningSignalGenerator.generate_from_prediction_comparison(match_comp)
    assert sig_match is not None
    assert sig_match.signal_type == LearningSignalType.PREDICTION_MATCH
    assert sig_match.evidence_classification == SignalEvidenceClassification.DERIVED

    miss_comp = PredictionComparison(
        comparison_id="comp-2",
        prediction_id="p-2",
        outcome_id="out-2",
        mission_id="m-1",
        decision_id="d-1",
        target_metric="margin",
        delta=0.25,
        status=ComparisonStatus.MISS,
    )
    sig_miss = LearningSignalGenerator.generate_from_prediction_comparison(miss_comp)
    assert sig_miss is not None
    assert sig_miss.signal_type == LearningSignalType.PREDICTION_MISS
    assert sig_miss.signal_value["delta"] == 0.25


def test_prediction_comparison_unknown_handling():
    unk_comp = PredictionComparison(
        comparison_id="comp-unk",
        prediction_id="p-1",
        outcome_id="out-1",
        mission_id="m-1",
        decision_id="d-1",
        status=ComparisonStatus.UNKNOWN,
    )
    sig_unk = LearningSignalGenerator.generate_from_prediction_comparison(unk_comp)
    assert sig_unk is None, "UNKNOWN prediction comparison must not generate MATCH or MISS"


def test_calibration_over_and_under_confidence():
    over_calib = DecisionCalibrationRecord(
        calibration_id="calib-1",
        decision_id="d-1",
        status=CalibrationStatus.OVER_CONFIDENT,
        calibration_error=0.35,
    )
    sig_over = LearningSignalGenerator.generate_from_calibration(over_calib)
    assert sig_over is not None
    assert sig_over.signal_type == LearningSignalType.OVER_CONFIDENCE

    under_calib = DecisionCalibrationRecord(
        calibration_id="calib-2",
        decision_id="d-2",
        status=CalibrationStatus.UNDER_CONFIDENT,
        calibration_error=-0.25,
    )
    sig_under = LearningSignalGenerator.generate_from_calibration(under_calib)
    assert sig_under is not None
    assert sig_under.signal_type == LearningSignalType.UNDER_CONFIDENCE


def test_insufficient_data_handling():
    insuff_calib = DecisionCalibrationRecord(
        calibration_id="calib-insuff",
        status=CalibrationStatus.INSUFFICIENT_DATA,
    )
    sig_insuff = LearningSignalGenerator.generate_from_calibration(insuff_calib)
    assert sig_insuff is not None
    assert sig_insuff.signal_type == LearningSignalType.INSUFFICIENT_DATA

    insuff_prod = ProductPerformanceRecord(
        performance_id="perf-prod-1",
        product_id="PROD-1",
        sku="SKU-1",
        period=TemporalPeriod(period_type="POINT_IN_TIME"),
        status=ProductPerformanceStatus.INSUFFICIENT_DATA,
    )
    sig_prod = LearningSignalGenerator.generate_from_product_performance(insuff_prod)
    assert sig_prod is not None
    assert sig_prod.signal_type == LearningSignalType.INSUFFICIENT_DATA


def test_product_supplier_strategy_performance_signals():
    prod_perf = ProductPerformanceRecord(
        performance_id="perf-prod-2",
        product_id="PROD-2",
        sku="SKU-2",
        period=TemporalPeriod(period_type="POINT_IN_TIME"),
        status=ProductPerformanceStatus.SUFFICIENT_DATA,
        observed_metrics=ObservedProductMetrics(observed_sales_units=10),
        derived_metrics=DerivedProductMetrics(outcome_success_rate=0.8),
    )
    sig_prod = LearningSignalGenerator.generate_from_product_performance(prod_perf)
    assert sig_prod is not None
    assert sig_prod.signal_type == LearningSignalType.PRODUCT_PERFORMANCE
    assert sig_prod.subject_type == LearningSignalSubjectType.PRODUCT
    assert sig_prod.subject_id == "SKU-2"

    supp_perf = SupplierPerformanceRecord(
        performance_id="perf-supp-1",
        supplier_id="SUPP-1",
        period=SupplierTemporalPeriod(period_type="POINT_IN_TIME"),
        status=SupplierPerformanceStatus.SUFFICIENT_DATA,
        observed_metrics=ObservedSupplierMetrics(total_orders_placed=10, total_fulfilled_orders=9),
        derived_metrics=DerivedSupplierMetrics(outcome_success_rate=0.9),
    )
    sig_supp = LearningSignalGenerator.generate_from_supplier_performance(supp_perf)
    assert sig_supp is not None
    assert sig_supp.signal_type == LearningSignalType.SUPPLIER_PERFORMANCE
    assert sig_supp.subject_type == LearningSignalSubjectType.SUPPLIER
    assert sig_supp.subject_id == "SUPP-1"

    strat_perf = StrategyPerformanceRecord(
        performance_id="perf-strat-1",
        strategy_id="STRAT-1",
        period=StrategyTemporalPeriod(period_type="POINT_IN_TIME"),
        status=StrategyPerformanceStatus.SUFFICIENT_DATA,
        observed_metrics=ObservedStrategyMetrics(success_count=10),
        derived_metrics=DerivedStrategyMetrics(success_rate=1.0),
    )
    sig_strat = LearningSignalGenerator.generate_from_strategy_performance(strat_perf)
    assert sig_strat is not None
    assert sig_strat.signal_type == LearningSignalType.STRATEGY_PERFORMANCE
    assert sig_strat.subject_type == LearningSignalSubjectType.STRATEGY
    assert sig_strat.subject_id == "STRAT-1"
