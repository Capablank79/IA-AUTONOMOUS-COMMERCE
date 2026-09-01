import json
import os
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal
from typing import Union, Optional, Any, Dict, List
from types import MappingProxyType

from src.domain.market_intelligence.models import Confidence
from src.domain.supplier_intelligence.models import EvidenceProvenanceType
from src.domain.supplier_performance.models import (
    SupplierPerformanceRecord,
    SupplierPerformanceStatus,
    SupplierTemporalPeriod,
    ObservedSupplierMetrics,
    DerivedSupplierMetrics,
)
from src.domain.supplier_performance.ports import SupplierPerformanceRepositoryPort


class JsonSupplierPerformanceRepositoryError(Exception):
    """Excepción base para errores de JsonSupplierPerformanceRepository."""
    pass


class InvalidSupplierPerformanceDataError(JsonSupplierPerformanceRepositoryError):
    """Se lanza cuando los datos de supplier performance leídos están corruptos o son inválidos."""
    pass


SENSITIVE_KEYS = {"password", "secret", "token", "api_key", "apikey", "pan", "cvv", "private_key", "credential"}


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


def _decode_supplier_temporal_period(data: Dict[str, Any]) -> SupplierTemporalPeriod:
    p_start = datetime.fromisoformat(data["period_start"]) if data.get("period_start") else None
    p_end = datetime.fromisoformat(data["period_end"]) if data.get("period_end") else None
    return SupplierTemporalPeriod(
        period_type=data.get("period_type", "POINT_IN_TIME"),
        period_start=p_start,
        period_end=p_end,
    )


def _decode_observed_metrics(data: Dict[str, Any]) -> ObservedSupplierMetrics:
    lead_times = tuple(int(x) for x in data.get("observed_lead_times_days", []))
    quoted_costs = tuple(Decimal(str(x)) for x in data.get("observed_quoted_costs", []))
    moqs = tuple(int(x) for x in data.get("observed_moqs", []))

    return ObservedSupplierMetrics(
        total_quotes_observed=int(data.get("total_quotes_observed", 0)),
        total_accepted_quotes=int(data.get("total_accepted_quotes", 0)),
        total_orders_placed=int(data.get("total_orders_placed", 0)),
        total_fulfilled_orders=int(data.get("total_fulfilled_orders", 0)),
        total_delivered_on_time=int(data.get("total_delivered_on_time", 0)),
        total_cancelled_orders=int(data.get("total_cancelled_orders", 0)),
        total_defective_returns=int(data.get("total_defective_returns", 0)),
        observed_lead_times_days=lead_times,
        observed_quoted_costs=quoted_costs,
        observed_moqs=moqs,
        currency=data.get("currency", "CLP"),
    )


def _decode_derived_metrics(data: Dict[str, Any]) -> DerivedSupplierMetrics:
    avg_cost = Decimal(str(data["average_quoted_cost"])) if data.get("average_quoted_cost") is not None else None
    return DerivedSupplierMetrics(
        quote_acceptance_rate=data.get("quote_acceptance_rate"),
        average_quoted_cost=avg_cost,
        average_moq=data.get("average_moq"),
        average_lead_time_days=data.get("average_lead_time_days"),
        delivery_on_time_rate=data.get("delivery_on_time_rate"),
        fulfillment_rate=data.get("fulfillment_rate"),
        cancellation_rate=data.get("cancellation_rate"),
        defect_return_rate=data.get("defect_return_rate"),
        outcome_success_rate=data.get("outcome_success_rate"),
    )


def _decode_performance_record(data: Dict[str, Any]) -> SupplierPerformanceRecord:
    try:
        calculated_at = (
            datetime.fromisoformat(data["calculated_at"])
            if isinstance(data.get("calculated_at"), str)
            else datetime.now(timezone.utc)
        )
        status = SupplierPerformanceStatus(data.get("status", SupplierPerformanceStatus.UNKNOWN.value))
        confidence = Confidence(data.get("confidence", Confidence.MEDIUM.value))
        provenance = EvidenceProvenanceType(data.get("provenance", EvidenceProvenanceType.DERIVED.value))

        period = _decode_supplier_temporal_period(data.get("period", {"period_type": "POINT_IN_TIME"}))
        obs_metrics = _decode_observed_metrics(data.get("observed_metrics", {}))
        der_metrics = _decode_derived_metrics(data.get("derived_metrics", {}))

        return SupplierPerformanceRecord(
            performance_id=data["performance_id"],
            supplier_id=data["supplier_id"],
            period=period,
            status=status,
            sample_count=int(data.get("sample_count", 0)),
            quote_sample_count=int(data.get("quote_sample_count", 0)),
            outcome_sample_count=int(data.get("outcome_sample_count", 0)),
            observed_metrics=obs_metrics,
            derived_metrics=der_metrics,
            supplier_memory_ids=tuple(data.get("supplier_memory_ids", [])),
            outcome_ids=tuple(data.get("outcome_ids", [])),
            mission_ids=tuple(data.get("mission_ids", [])),
            decision_ids=tuple(data.get("decision_ids", [])),
            action_ids=tuple(data.get("action_ids", [])),
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
        raise InvalidSupplierPerformanceDataError(f"Failed to decode SupplierPerformanceRecord: {e}") from e


class JsonSupplierPerformanceRepository(SupplierPerformanceRepositoryPort):
    """
    Implementación JSON durable del puerto SupplierPerformanceRepositoryPort.
    Mantiene registros de performance de proveedores en almacenamiento JSON persistente con escrituras atómicas.
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
            raise InvalidSupplierPerformanceDataError(f"Corrupted JSON file at {self.file_path}: {e}") from e
        except Exception as e:
            raise JsonSupplierPerformanceRepositoryError(f"Error reading file {self.file_path}: {e}") from e

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
            raise JsonSupplierPerformanceRepositoryError(f"Failed to save performances to {self.file_path}: {e}") from e

    def save(self, record: SupplierPerformanceRecord) -> SupplierPerformanceRecord:
        root = self._load_all()
        encoded = _encode_json_value({
            "performance_id": record.performance_id,
            "supplier_id": record.supplier_id,
            "period": {
                "period_type": record.period.period_type,
                "period_start": record.period.period_start,
                "period_end": record.period.period_end,
            },
            "status": record.status,
            "sample_count": record.sample_count,
            "quote_sample_count": record.quote_sample_count,
            "outcome_sample_count": record.outcome_sample_count,
            "observed_metrics": {
                "total_quotes_observed": record.observed_metrics.total_quotes_observed,
                "total_accepted_quotes": record.observed_metrics.total_accepted_quotes,
                "total_orders_placed": record.observed_metrics.total_orders_placed,
                "total_fulfilled_orders": record.observed_metrics.total_fulfilled_orders,
                "total_delivered_on_time": record.observed_metrics.total_delivered_on_time,
                "total_cancelled_orders": record.observed_metrics.total_cancelled_orders,
                "total_defective_returns": record.observed_metrics.total_defective_returns,
                "observed_lead_times_days": list(record.observed_metrics.observed_lead_times_days),
                "observed_quoted_costs": list(record.observed_metrics.observed_quoted_costs),
                "observed_moqs": list(record.observed_metrics.observed_moqs),
                "currency": record.observed_metrics.currency,
            },
            "derived_metrics": {
                "quote_acceptance_rate": record.derived_metrics.quote_acceptance_rate,
                "average_quoted_cost": record.derived_metrics.average_quoted_cost,
                "average_moq": record.derived_metrics.average_moq,
                "average_lead_time_days": record.derived_metrics.average_lead_time_days,
                "delivery_on_time_rate": record.derived_metrics.delivery_on_time_rate,
                "fulfillment_rate": record.derived_metrics.fulfillment_rate,
                "cancellation_rate": record.derived_metrics.cancellation_rate,
                "defect_return_rate": record.derived_metrics.defect_return_rate,
                "outcome_success_rate": record.derived_metrics.outcome_success_rate,
            },
            "supplier_memory_ids": list(record.supplier_memory_ids),
            "outcome_ids": list(record.outcome_ids),
            "mission_ids": list(record.mission_ids),
            "decision_ids": list(record.decision_ids),
            "action_ids": list(record.action_ids),
            "calibration_context_id": record.calibration_context_id,
            "contextual_prediction_error": record.contextual_prediction_error,
            "evidence_reference": record.evidence_reference,
            "confidence": record.confidence,
            "provenance": record.provenance,
            "calculated_at": record.calculated_at,
            "correlation_id": record.correlation_id,
            "idempotency_key": record.idempotency_key,
            "version": record.version,
            "metadata": record.metadata,
        })
        root["performances"][record.performance_id] = encoded
        self._save_all(root)
        return record

    def get_by_id(self, performance_id: str) -> Optional[SupplierPerformanceRecord]:
        root = self._load_all()
        data = root["performances"].get(performance_id)
        if not data:
            return None
        return _decode_performance_record(data)

    def get_by_supplier_id(self, supplier_id: str) -> List[SupplierPerformanceRecord]:
        root = self._load_all()
        results = []
        for data in root["performances"].values():
            if data.get("supplier_id") == supplier_id:
                results.append(_decode_performance_record(data))
        return results

    def get_by_idempotency_key(self, idempotency_key: str) -> Optional[SupplierPerformanceRecord]:
        root = self._load_all()
        for data in root["performances"].values():
            if data.get("idempotency_key") == idempotency_key:
                return _decode_performance_record(data)
        return None

    def list_all(self) -> List[SupplierPerformanceRecord]:
        root = self._load_all()
        return [_decode_performance_record(data) for data in root["performances"].values()]
