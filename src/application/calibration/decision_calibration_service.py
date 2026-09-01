from datetime import datetime, timezone
from typing import Optional, List, Dict, Tuple, Any
import math

from src.domain.market_intelligence.models import Confidence
from src.domain.prediction.models import PredictionComparison, ComparisonStatus
from src.domain.calibration.models import (
    CalibrationStatus,
    ConfidenceBin,
    DecisionCalibrationRecord,
)
from src.domain.calibration.ports import CalibrationRepository


# Mapeo determinista de enum Confidence a score probabilístico numérico nominal (0.0 a 1.0)
CONFIDENCE_SCORE_MAP: Dict[Confidence, float] = {
    Confidence.HIGH: 0.90,
    Confidence.MEDIUM: 0.70,
    Confidence.LOW: 0.40,
    Confidence.UNKNOWN: 0.50,
}

DEFAULT_MIN_SAMPLE_THRESHOLD = 5


class DecisionCalibrationService:
    """
    Servicio de Aplicación para agregar y evaluar la calibración de decisiones (Task I.3).
    Transforma historiales verificables de PredictionComparison en métricas y estados de calibración.

    Propiedades clave:
    1. Determinista: Mismo conjunto de comparaciones -> Mismo resultado exacto.
    2. Idempotente: Si se provee idempotency_key, retorna el registro existente.
    3. Exclusión de UNKNOWN: Las comparaciones con status UNKNOWN o confidence UNKNOWN se excluyen
       de métricas probabilísticas/calibración y se registran en unknown_excluded_samples.
    4. Data Sufficiency: Si valid_samples < min_sample_threshold, asigna status INSUFFICIENT_DATA.
    5. Preservación de Trazabilidad: Vincula IDs de comparaciones, predicciones, outcomes, decisión y misión.
    6. Sin efectos colaterales de Learning/Tuning: Exclusivamente evalúa y documenta.
    """

    def __init__(
        self,
        calibration_repo: Optional[CalibrationRepository] = None,
        min_sample_threshold: int = DEFAULT_MIN_SAMPLE_THRESHOLD,
    ):
        self.calibration_repo = calibration_repo
        self.min_sample_threshold = min_sample_threshold

    def calculate_calibration(
        self,
        calibration_id: str,
        comparisons: List[PredictionComparison],
        decision_id: Optional[str] = None,
        mission_id: Optional[str] = None,
        target_metric: str = "general",
        calculated_at: Optional[datetime] = None,
        correlation_id: str = "default-correlation",
        idempotency_key: str = "default-idempotency",
        metadata: Optional[dict] = None,
    ) -> DecisionCalibrationRecord:
        """
        Calcula de forma determinista la calibración a partir de una lista de PredictionComparison.
        """
        if self.calibration_repo and idempotency_key:
            existing = self.calibration_repo.get_calibration_by_idempotency_key(idempotency_key)
            if existing:
                return existing

        now = calculated_at or datetime.now(timezone.utc)

        total_samples = len(comparisons)
        if total_samples == 0:
            record = DecisionCalibrationRecord(
                calibration_id=calibration_id,
                decision_id=decision_id,
                mission_id=mission_id,
                target_metric=target_metric,
                status=CalibrationStatus.INSUFFICIENT_DATA,
                total_samples=0,
                valid_samples=0,
                unknown_excluded_samples=0,
                match_count=0,
                miss_count=0,
                accuracy=0.0,
                error_rate=0.0,
                expected_confidence_score=0.0,
                brier_score=None,
                calibration_error=0.0,
                confidence_bins=(),
                comparison_ids=(),
                prediction_ids=(),
                outcome_ids=(),
                calculated_at=now,
                correlation_id=correlation_id,
                idempotency_key=idempotency_key,
                version=1,
                metadata=metadata or {},
            )
            if self.calibration_repo:
                self.calibration_repo.save_calibration(record)
            return record

        # Filtrar comparaciones pertenecientes a target_metric si corresponde, o agrupar todas
        target_comparisons = [c for c in comparisons if c.target_metric == target_metric] if target_metric != "general" else comparisons
        if not target_comparisons and comparisons:
            target_comparisons = comparisons

        # Extraer IDs para trazabilidad (ordenados para determinismo)
        comp_ids = tuple(sorted(list({c.comparison_id for c in target_comparisons})))
        pred_ids = tuple(sorted(list({c.prediction_id for c in target_comparisons})))
        out_ids = tuple(sorted(list({c.outcome_id for c in target_comparisons})))

        # Clasificar comparaciones: válidas vs UNKNOWN
        valid_comps: List[PredictionComparison] = []
        unknown_excluded_count = 0

        for c in target_comparisons:
            if c.status == ComparisonStatus.UNKNOWN:
                unknown_excluded_count += 1
            else:
                valid_comps.append(c)

        valid_samples = len(valid_comps)

        # Si no hay suficientes muestras válidas -> INSUFFICIENT_DATA
        if valid_samples < self.min_sample_threshold:
            record = DecisionCalibrationRecord(
                calibration_id=calibration_id,
                decision_id=decision_id,
                mission_id=mission_id,
                target_metric=target_metric,
                status=CalibrationStatus.INSUFFICIENT_DATA,
                total_samples=len(target_comparisons),
                valid_samples=valid_samples,
                unknown_excluded_samples=unknown_excluded_count,
                match_count=sum(1 for c in valid_comps if c.status == ComparisonStatus.MATCH),
                miss_count=sum(1 for c in valid_comps if c.status == ComparisonStatus.MISS),
                accuracy=0.0,
                error_rate=0.0,
                expected_confidence_score=0.0,
                brier_score=None,
                calibration_error=0.0,
                confidence_bins=(),
                comparison_ids=comp_ids,
                prediction_ids=pred_ids,
                outcome_ids=out_ids,
                calculated_at=now,
                correlation_id=correlation_id,
                idempotency_key=idempotency_key,
                version=1,
                metadata=metadata or {},
            )
            if self.calibration_repo:
                self.calibration_repo.save_calibration(record)
            return record

        # Con suficiente data válida, calcular métricas
        match_count = sum(1 for c in valid_comps if c.status == ComparisonStatus.MATCH)
        miss_count = sum(1 for c in valid_comps if c.status == ComparisonStatus.MISS)

        accuracy = match_count / valid_samples
        error_rate = miss_count / valid_samples

        # Agrupar por nivel de confianza (Confidence enum: HIGH, MEDIUM, LOW, UNKNOWN)
        bins_dict: Dict[Confidence, List[PredictionComparison]] = {
            Confidence.HIGH: [],
            Confidence.MEDIUM: [],
            Confidence.LOW: [],
            Confidence.UNKNOWN: [],
        }

        for c in valid_comps:
            bins_dict[c.prediction_confidence].append(c)

        confidence_bins_list: List[ConfidenceBin] = []
        brier_sum = 0.0
        weighted_calib_error_sum = 0.0
        expected_conf_sum = 0.0

        # Iterar sobre las categorías de confianza ordenadas para asegurar determinismo
        for conf in [Confidence.HIGH, Confidence.MEDIUM, Confidence.LOW]:
            bin_comps = bins_dict[conf]
            bin_size = len(bin_comps)
            if bin_size == 0:
                continue

            bin_matches = sum(1 for c in bin_comps if c.status == ComparisonStatus.MATCH)
            bin_misses = sum(1 for c in bin_comps if c.status == ComparisonStatus.MISS)
            bin_unknowns = sum(1 for c in bin_comps if c.status == ComparisonStatus.UNKNOWN)

            obs_success_rate = bin_matches / bin_size
            expected_score = CONFIDENCE_SCORE_MAP.get(conf, 0.50)
            calib_gap = obs_success_rate - expected_score

            confidence_bins_list.append(
                ConfidenceBin(
                    confidence_level=conf,
                    sample_count=bin_size,
                    match_count=bin_matches,
                    miss_count=bin_misses,
                    unknown_count=bin_unknowns,
                    observed_success_rate=round(obs_success_rate, 4),
                    expected_confidence_score=round(expected_score, 4),
                    calibration_gap=round(calib_gap, 4),
                )
            )

            expected_conf_sum += expected_score * bin_size
            weighted_calib_error_sum += abs(calib_gap) * bin_size

            # Brier Score acumulado: (p_i - y_i)^2
            # p_i es el expected_score del bin, y_i es 1.0 si MATCH else 0.0
            for c in bin_comps:
                y_i = 1.0 if c.status == ComparisonStatus.MATCH else 0.0
                brier_sum += (expected_score - y_i) ** 2

        overall_expected_conf = expected_conf_sum / valid_samples
        brier_score = brier_sum / valid_samples
        calibration_error = weighted_calib_error_sum / valid_samples

        # Evaluar estado global de calibración
        # WELL_CALIBRATED: calibration_error <= 0.15
        # OVER_CONFIDENT: overall_expected_conf > accuracy + 0.15
        # UNDER_CONFIDENT: accuracy > overall_expected_conf + 0.15
        if calibration_error <= 0.15:
            status = CalibrationStatus.WELL_CALIBRATED
        elif overall_expected_conf > (accuracy + 0.15):
            status = CalibrationStatus.OVER_CONFIDENT
        elif accuracy > (overall_expected_conf + 0.15):
            status = CalibrationStatus.UNDER_CONFIDENT
        else:
            status = CalibrationStatus.NOT_CALIBRATED

        record = DecisionCalibrationRecord(
            calibration_id=calibration_id,
            decision_id=decision_id,
            mission_id=mission_id,
            target_metric=target_metric,
            status=status,
            total_samples=len(target_comparisons),
            valid_samples=valid_samples,
            unknown_excluded_samples=unknown_excluded_count,
            match_count=match_count,
            miss_count=miss_count,
            accuracy=round(accuracy, 4),
            error_rate=round(error_rate, 4),
            expected_confidence_score=round(overall_expected_conf, 4),
            brier_score=round(brier_score, 4),
            calibration_error=round(calibration_error, 4),
            confidence_bins=tuple(confidence_bins_list),
            comparison_ids=comp_ids,
            prediction_ids=pred_ids,
            outcome_ids=out_ids,
            calculated_at=now,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            version=1,
            metadata=metadata or {},
        )

        if self.calibration_repo:
            self.calibration_repo.save_calibration(record)

        return record

    def get_calibration(self, calibration_id: str) -> Optional[DecisionCalibrationRecord]:
        if not self.calibration_repo:
            return None
        return self.calibration_repo.get_calibration_by_id(calibration_id)

    def get_calibrations_for_decision(self, decision_id: str) -> List[DecisionCalibrationRecord]:
        if not self.calibration_repo:
            return []
        return self.calibration_repo.get_calibrations_by_decision_id(decision_id)

    def get_calibrations_for_mission(self, mission_id: str) -> List[DecisionCalibrationRecord]:
        if not self.calibration_repo:
            return []
        return self.calibration_repo.get_calibrations_by_mission_id(mission_id)
