import json
import os
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal
from typing import Union, Optional, Any, Dict, List
from types import MappingProxyType

from src.domain.market_intelligence.models import Confidence
from src.domain.supplier_intelligence.models import EvidenceProvenanceType
from src.domain.strategy_performance.models import (
    StrategyPerformanceRecord,
    StrategyPerformanceStatus,
    StrategyTemporalPeriod,
    ObservedStrategyMetrics,
    DerivedStrategyMetrics,
)
from src.domain.strategy_performance.ports import StrategyPerformanceRepositoryPort


class JsonStrategyPerformanceRepositoryError(Exception):
    """Excepción base para errores de JsonStrategyPerformanceRepository."""
    pass


class InvalidStrategyPerformanceDataError(JsonStrategyPerformanceRepositoryError):
    """Se lanza cuando los datos de strategy performance leídos están corruptos o son inválidos."""
    pass


SENSITIVE_KEYS = {
    "password", "secret", "token", "api_key", "apikey",
    "access_token", "refresh_token", "auth", "authorization",
    "private_key", "credentials", "payment", "card", "pan", "cvv"
}


def _encode_json_value(val: Any) -> Any:
    if isinstance(val, datetime):
        return val.isoformat()
    if isinstance(val, Decimal):
        return str(val)
    if hasattr(val, "value"):
        return val.value
    if isinstance(val, (dict, MappingProxyType)):
        cleaned = {}
        for k, v in val.items():
            if any(s in str(k).lower() for s in SENSITIVE_KEYS):
                continue
            cleaned[str(k)] = _encode_json_value(v)
        return cleaned
    if isinstance(val, (list, tuple)):
        return [_encode_json_value(v) for v in val]
    return val


def _decode_strategy_temporal_period(data: Dict[str, Any]) -> StrategyTemporalPeriod:
    p_start = datetime.fromisoformat(data["period_start"]) if data.get("period_start") else None
    p_end = datetime.fromisoformat(data["period_end"]) if data.get("period_end") else None
    return StrategyTemporalPeriod(
        period_type=data.get("period_type", "POINT_IN_TIME"),
        period_start=p_start,
        period_end=p_end,
    )


def _decode_observed_strategy_metrics(data: Dict[str, Any]) -> ObservedStrategyMetrics:
    prof = Decimal(data["observed_profit"]) if data.get("observed_profit") is not None else None
    rev = Decimal(data["observed_revenue"]) if data.get("observed_revenue") is not None else None
    return ObservedStrategyMetrics(
        total_decisions_observed=int(data.get("total_decisions_observed", 0)),
        total_actions_executed=int(data.get("total_actions_executed", 0)),
        total_outcomes_observed=int(data.get("total_outcomes_observed", 0)),
        success_count=int(data.get("success_count", 0)),
        failure_count=int(data.get("failure_count", 0)),
        partial_count=int(data.get("partial_count", 0)),
        cancelled_count=int(data.get("cancelled_count", 0)),
        unknown_count=int(data.get("unknown_count", 0)),
        observed_profit=prof,
        observed_revenue=rev,
        observed_cancellations=int(data.get("observed_cancellations", 0)),
        observed_returns=int(data.get("observed_returns", 0)),
        currency=data.get("currency", "CLP"),
    )


def _decode_derived_strategy_metrics(data: Dict[str, Any]) -> DerivedStrategyMetrics:
    avg_p = Decimal(data["average_realized_profit"]) if data.get("average_realized_profit") is not None else None
    avg_r = Decimal(data["average_realized_revenue"]) if data.get("average_realized_revenue") is not None else None
    return DerivedStrategyMetrics(
        success_rate=data.get("success_rate"),
        outcome_success_rate=data.get("outcome_success_rate"),
        failure_rate=data.get("failure_rate"),
        cancellation_rate=data.get("cancellation_rate"),
        return_rate=data.get("return_rate"),
        average_realized_profit=avg_p,
        average_margin_percentage=data.get("average_margin_percentage"),
        average_realized_revenue=avg_r,
    )


def _decode_performance_record(data: Dict[str, Any]) -> StrategyPerformanceRecord:
    try:
        calculated_at = (
            datetime.fromisoformat(data["calculated_at"])
            if isinstance(data.get("calculated_at"), str)
            else datetime.now(timezone.utc)
        )
        status = StrategyPerformanceStatus(data.get("status", StrategyPerformanceStatus.UNKNOWN.value))
        confidence = Confidence(data.get("confidence", Confidence.MEDIUM.value))
        provenance = EvidenceProvenanceType(data.get("provenance", EvidenceProvenanceType.DERIVED.value))

        period = _decode_strategy_temporal_period(data.get("period", {"period_type": "POINT_IN_TIME"}))
        obs_metrics = _decode_observed_strategy_metrics(data.get("observed_metrics", {}))
        der_metrics = _decode_derived_strategy_metrics(data.get("derived_metrics", {}))

        return StrategyPerformanceRecord(
            performance_id=data["performance_id"],
            strategy_id=data["strategy_id"],
            period=period,
            status=status,
            sample_count=int(data.get("sample_count", 0)),
            decision_sample_count=int(data.get("decision_sample_count", 0)),
            action_sample_count=int(data.get("action_sample_count", 0)),
            outcome_sample_count=int(data.get("outcome_sample_count", 0)),
            observed_metrics=obs_metrics,
            derived_metrics=der_metrics,
            decision_ids=tuple(data.get("decision_ids", [])),
            action_ids=tuple(data.get("action_ids", [])),
            result_ids=tuple(data.get("result_ids", [])),
            outcome_ids=tuple(data.get("outcome_ids", [])),
            mission_ids=tuple(data.get("mission_ids", [])),
            product_ids=tuple(data.get("product_ids", [])),
            supplier_ids=tuple(data.get("supplier_ids", [])),
            calibration_context_id=data.get("calibration_context_id"),
            contextual_prediction_error=data.get("contextual_prediction_error"),
            evidence_reference=data.get("evidence_reference"),
            confidence=confidence,
            provenance=provenance,
            calculated_at=calculated_at,
            correlation_id=data.get("correlation_id", "default-correlation"),
            idempotency_key=data.get("idempotency_key", "default-idempotency"),
            version=int(data.get("version", 1)),
            metadata=data.get("metadata", {}),
        )
    except Exception as e:
        raise InvalidStrategyPerformanceDataError(f"Failed to decode StrategyPerformanceRecord: {e}") from e


class JsonStrategyPerformanceRepository(StrategyPerformanceRepositoryPort):
    """
    Implementación JSON durable del puerto StrategyPerformanceRepositoryPort.
    Mantiene registros de desempeño de estrategias en almacenamiento JSON persistente con escrituras atómicas.
    """

    def __init__(self, file_path: Union[str, Path]):
        self.file_path = Path(file_path)

    def _load_all(self) -> Dict[str, Any]:
        if not self.file_path.exists():
            return {"performances": {}}
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if not content:
                    return {"performances": {}}
                data = json.loads(content)
                if "performances" not in data:
                    return {"performances": {}}
                return data
        except json.JSONDecodeError as e:
            raise InvalidStrategyPerformanceDataError(f"Corrupted JSON file at {self.file_path}: {e}") from e
        except Exception as e:
            raise JsonStrategyPerformanceRepositoryError(f"Error reading file {self.file_path}: {e}") from e

    def _save_all(self, data: Dict[str, Any]) -> None:
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.file_path.with_suffix(".tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, self.file_path)
        except Exception as e:
            if tmp_path.exists():
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
            raise JsonStrategyPerformanceRepositoryError(f"Failed to save performances to {self.file_path}: {e}") from e

    def save_performance(self, performance: StrategyPerformanceRecord) -> None:
        root = self._load_all()
        encoded = _encode_json_value({
            "performance_id": performance.performance_id,
            "strategy_id": performance.strategy_id,
            "period": {
                "period_type": performance.period.period_type,
                "period_start": performance.period.period_start,
                "period_end": performance.period.period_end,
            },
            "status": performance.status,
            "sample_count": performance.sample_count,
            "decision_sample_count": performance.decision_sample_count,
            "action_sample_count": performance.action_sample_count,
            "outcome_sample_count": performance.outcome_sample_count,
            "observed_metrics": {
                "total_decisions_observed": performance.observed_metrics.total_decisions_observed,
                "total_actions_executed": performance.observed_metrics.total_actions_executed,
                "total_outcomes_observed": performance.observed_metrics.total_outcomes_observed,
                "success_count": performance.observed_metrics.success_count,
                "failure_count": performance.observed_metrics.failure_count,
                "partial_count": performance.observed_metrics.partial_count,
                "cancelled_count": performance.observed_metrics.cancelled_count,
                "unknown_count": performance.observed_metrics.unknown_count,
                "observed_profit": performance.observed_metrics.observed_profit,
                "observed_revenue": performance.observed_metrics.observed_revenue,
                "observed_cancellations": performance.observed_metrics.observed_cancellations,
                "observed_returns": performance.observed_metrics.observed_returns,
                "currency": performance.observed_metrics.currency,
            },
            "derived_metrics": {
                "success_rate": performance.derived_metrics.success_rate,
                "outcome_success_rate": performance.derived_metrics.outcome_success_rate,
                "failure_rate": performance.derived_metrics.failure_rate,
                "cancellation_rate": performance.derived_metrics.cancellation_rate,
                "return_rate": performance.derived_metrics.return_rate,
                "average_realized_profit": performance.derived_metrics.average_realized_profit,
                "average_margin_percentage": performance.derived_metrics.average_margin_percentage,
                "average_realized_revenue": performance.derived_metrics.average_realized_revenue,
            },
            "decision_ids": list(performance.decision_ids),
            "action_ids": list(performance.action_ids),
            "result_ids": list(performance.result_ids),
            "outcome_ids": list(performance.outcome_ids),
            "mission_ids": list(performance.mission_ids),
            "product_ids": list(performance.product_ids),
            "supplier_ids": list(performance.supplier_ids),
            "calibration_context_id": performance.calibration_context_id,
            "contextual_prediction_error": performance.contextual_prediction_error,
            "evidence_reference": performance.evidence_reference,
            "confidence": performance.confidence,
            "provenance": performance.provenance,
            "calculated_at": performance.calculated_at,
            "correlation_id": performance.correlation_id,
            "idempotency_key": performance.idempotency_key,
            "version": performance.version,
            "metadata": performance.metadata,
        })
        root["performances"][performance.performance_id] = encoded
        self._save_all(root)

    def get_performance_by_id(self, performance_id: str) -> Optional[StrategyPerformanceRecord]:
        root = self._load_all()
        data = root["performances"].get(performance_id)
        if not data:
            return None
        return _decode_performance_record(data)

    def get_performances_by_strategy_id(self, strategy_id: str) -> List[StrategyPerformanceRecord]:
        root = self._load_all()
        results = []
        for data in root["performances"].values():
            if data.get("strategy_id") == strategy_id:
                results.append(_decode_performance_record(data))
        return results

    def get_performance_by_idempotency_key(self, idempotency_key: str) -> Optional[StrategyPerformanceRecord]:
        root = self._load_all()
        for data in root["performances"].values():
            if data.get("idempotency_key") == idempotency_key:
                return _decode_performance_record(data)
        return None

    def list_all(self) -> List[StrategyPerformanceRecord]:
        root = self._load_all()
        return [_decode_performance_record(d) for d in root["performances"].values()]
