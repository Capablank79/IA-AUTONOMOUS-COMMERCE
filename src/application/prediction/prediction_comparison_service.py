from datetime import datetime, timezone
from typing import Optional, List, Any
import math

from src.domain.market_intelligence.models import Confidence
from src.domain.supplier_intelligence.models import EvidenceProvenanceType
from src.domain.outcome.models import OutcomeRecord, OutcomeStatus
from src.domain.prediction.models import PredictionRecord, PredictionComparison, ComparisonStatus
from src.domain.prediction.ports import PredictionRepository


class PredictionComparisonService:
    """
    Servicio de Aplicación para gestionar Predicciones y realizar la comparación determinista
    PREDICTION vs ACTUAL (Task I.2).
    Preserva idempotencia, temporalidad, procedencia, confianza y desacoplamiento.
    """

    def __init__(self, prediction_repo: PredictionRepository):
        self.prediction_repo = prediction_repo

    def register_prediction(
        self,
        prediction_id: str,
        mission_id: str,
        decision_id: str,
        action_id: Optional[str] = None,
        target_metric: str = "general",
        predicted_value: Optional[Any] = None,
        predicted_class: Optional[str] = None,
        created_at: Optional[datetime] = None,
        expected_at: Optional[datetime] = None,
        confidence: Confidence = Confidence.MEDIUM,
        provenance: EvidenceProvenanceType = EvidenceProvenanceType.DERIVED,
        evidence_reference: Optional[str] = None,
        correlation_id: str = "default-correlation",
        idempotency_key: str = "default-idempotency",
        metadata: Optional[dict] = None,
    ) -> PredictionRecord:
        """
        Registra una predicción realizada de manera idempotente.
        """
        if idempotency_key:
            existing = self.prediction_repo.get_prediction_by_idempotency_key(idempotency_key)
            if existing:
                return existing

        now = created_at or datetime.now(timezone.utc)
        record = PredictionRecord(
            prediction_id=prediction_id,
            mission_id=mission_id,
            decision_id=decision_id,
            action_id=action_id,
            target_metric=target_metric,
            predicted_value=predicted_value,
            predicted_class=predicted_class,
            created_at=now,
            expected_at=expected_at,
            confidence=confidence,
            provenance=provenance,
            evidence_reference=evidence_reference,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            version=1,
            metadata=metadata or {},
        )
        self.prediction_repo.save_prediction(record)
        return record

    def compare_prediction_vs_actual(
        self,
        comparison_id: str,
        prediction: PredictionRecord,
        outcome: OutcomeRecord,
        evaluated_at: Optional[datetime] = None,
        correlation_id: str = "default-correlation",
        idempotency_key: str = "default-idempotency",
        metadata: Optional[dict] = None,
    ) -> PredictionComparison:
        """
        Compara una predicción con un outcome real preservando temporalidad, delta y status.
        Garantiza idempotencia estricta por idempotency_key.
        """
        if idempotency_key:
            existing = self.prediction_repo.get_comparison_by_idempotency_key(idempotency_key)
            if existing:
                return existing

        now = evaluated_at or datetime.now(timezone.utc)

        # Validar orden temporal: prediction.created_at <= outcome.observed_at
        if prediction.created_at > outcome.observed_at:
            raise ValueError(
                f"Temporal order violation: prediction created_at ({prediction.created_at.isoformat()}) "
                f"is after outcome observed_at ({outcome.observed_at.isoformat()})"
            )

        # Extraer actual_value del outcome
        actual_val: Optional[Any] = None
        if outcome.status == OutcomeStatus.UNKNOWN or not outcome.value_metrics:
            actual_val = None
        else:
            # Buscar en value_metrics por target_metric o tomar el primer valor numérico/clase disponible
            if prediction.target_metric in outcome.value_metrics:
                actual_val = outcome.value_metrics[prediction.target_metric]
            elif "actual_value" in outcome.value_metrics:
                actual_val = outcome.value_metrics["actual_value"]
            elif "units_sold" in outcome.value_metrics and prediction.target_metric == "units_sold":
                actual_val = outcome.value_metrics["units_sold"]
            elif "realized_margin" in outcome.value_metrics and prediction.target_metric == "realized_margin":
                actual_val = outcome.value_metrics["realized_margin"]
            elif "realized_revenue_clp" in outcome.value_metrics and prediction.target_metric == "realized_revenue_clp":
                actual_val = outcome.value_metrics["realized_revenue_clp"]
            elif len(outcome.value_metrics) == 1:
                actual_val = list(outcome.value_metrics.values())[0]

        # Calcular comparación (Expected vs Actual)
        status = ComparisonStatus.UNKNOWN
        delta: Optional[float] = None

        has_prediction = prediction.predicted_value is not None or prediction.predicted_class is not None

        if not has_prediction or actual_val is None or outcome.status == OutcomeStatus.UNKNOWN:
            status = ComparisonStatus.UNKNOWN
            delta = None
        else:
            # Caso Numérico vs Categórico
            pred_num = self._to_float(prediction.predicted_value)
            act_num = self._to_float(actual_val)

            if pred_num is not None and act_num is not None:
                delta = act_num - pred_num
                # Si la diferencia absoluta es despreciable (< 1e-5), se considera MATCH
                if math.isclose(act_num, pred_num, rel_tol=1e-5, abs_tol=1e-5):
                    status = ComparisonStatus.MATCH
                else:
                    status = ComparisonStatus.MISS
            else:
                # Comparación cualitativa / categórica
                pred_str = str(prediction.predicted_class or prediction.predicted_value).strip().upper()
                act_str = str(actual_val).strip().upper()

                if pred_str == act_str:
                    status = ComparisonStatus.MATCH
                else:
                    status = ComparisonStatus.MISS

        comparison = PredictionComparison(
            comparison_id=comparison_id,
            prediction_id=prediction.prediction_id,
            outcome_id=outcome.outcome_id,
            mission_id=prediction.mission_id,
            decision_id=prediction.decision_id,
            action_id=prediction.action_id or outcome.action_id,
            target_metric=prediction.target_metric,
            expected_value=prediction.predicted_value if prediction.predicted_value is not None else prediction.predicted_class,
            actual_value=actual_val,
            delta=delta,
            status=status,
            evaluated_at=now,
            prediction_timestamp=prediction.created_at,
            outcome_timestamp=outcome.observed_at,
            prediction_provenance=prediction.provenance,
            outcome_provenance=outcome.provenance,
            prediction_confidence=prediction.confidence,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            version=1,
            metadata=metadata or {},
        )

        self.prediction_repo.save_comparison(comparison)
        return comparison

    def get_prediction(self, prediction_id: str) -> Optional[PredictionRecord]:
        return self.prediction_repo.get_prediction_by_id(prediction_id)

    def get_comparison(self, comparison_id: str) -> Optional[PredictionComparison]:
        return self.prediction_repo.get_comparison_by_id(comparison_id)

    def get_predictions_for_decision(self, decision_id: str) -> List[PredictionRecord]:
        return self.prediction_repo.get_predictions_by_decision_id(decision_id)

    def get_predictions_for_mission(self, mission_id: str) -> List[PredictionRecord]:
        return self.prediction_repo.get_predictions_by_mission_id(mission_id)

    def get_comparisons_for_prediction(self, prediction_id: str) -> List[PredictionComparison]:
        return self.prediction_repo.get_comparisons_by_prediction_id(prediction_id)

    def get_comparisons_for_outcome(self, outcome_id: str) -> List[PredictionComparison]:
        return self.prediction_repo.get_comparisons_by_outcome_id(outcome_id)

    def get_comparisons_for_decision(self, decision_id: str) -> List[PredictionComparison]:
        return self.prediction_repo.get_comparisons_by_decision_id(decision_id)

    def get_comparisons_for_mission(self, mission_id: str) -> List[PredictionComparison]:
        return self.prediction_repo.get_comparisons_by_mission_id(mission_id)

    @staticmethod
    def _to_float(val: Any) -> Optional[float]:
        if val is None:
            return None
        if isinstance(val, (int, float)):
            return float(val)
        try:
            return float(str(val))
        except (ValueError, TypeError):
            return None
