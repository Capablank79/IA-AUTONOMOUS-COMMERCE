import json
import os
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal
from typing import Union, Optional, Any, Dict, List
from types import MappingProxyType

from src.domain.market_intelligence.models import Confidence
from src.domain.supplier_intelligence.models import EvidenceProvenanceType
from src.domain.product_performance.models import (
    ProductPerformanceRecord,
    PerformanceStatus,
    TemporalPeriod,
    ObservedProductMetrics,
    DerivedProductMetrics,
)
from src.domain.product_performance.ports import ProductPerformanceRepository


class JsonProductPerformanceRepositoryError(Exception):
    """Excepción base para errores de JsonProductPerformanceRepository."""
    pass


class InvalidProductPerformanceDataError(JsonProductPerformanceRepositoryError):
    """Se lanza cuando los datos de product performance leídos están corruptos o son inválidos."""
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


def _decode_temporal_period(data: Dict[str, Any]) -> TemporalPeriod:
    p_start = datetime.fromisoformat(data["period_start"]) if data.get("period_start") else None
    p_end = datetime.fromisoformat(data["period_end"]) if data.get("period_end") else None
    return TemporalPeriod(
        period_type=data.get("period_type", "POINT_IN_TIME"),
        period_start=p_start,
        period_end=p_end,
    )


def _decode_observed_metrics(data: Dict[str, Any]) -> ObservedProductMetrics:
    rev = Decimal(data["observed_revenue"]) if data.get("observed_revenue") is not None else None
    price = Decimal(data["observed_price"]) if data.get("observed_price") is not None else None
    cost = Decimal(data["observed_cost"]) if data.get("observed_cost") is not None else None
    return ObservedProductMetrics(
        observed_sales_units=data.get("observed_sales_units"),
        observed_revenue=rev,
        observed_cancellations_units=data.get("observed_cancellations_units"),
        observed_returns_units=data.get("observed_returns_units"),
        observed_stock_level=data.get("observed_stock_level"),
        observed_price=price,
        observed_cost=cost,
        currency=data.get("currency", "CLP"),
    )


def _decode_derived_metrics(data: Dict[str, Any]) -> DerivedProductMetrics:
    gm_amt = Decimal(data["gross_margin_amount"]) if data.get("gross_margin_amount") is not None else None
    avg_p = Decimal(data["average_selling_price"]) if data.get("average_selling_price") is not None else None
    return DerivedProductMetrics(
        gross_margin_amount=gm_amt,
        gross_margin_percentage=data.get("gross_margin_percentage"),
        cancellation_rate=data.get("cancellation_rate"),
        return_rate=data.get("return_rate"),
        outcome_success_rate=data.get("outcome_success_rate"),
        average_selling_price=avg_p,
    )


def _decode_performance_record(data: Dict[str, Any]) -> ProductPerformanceRecord:
    try:
        calculated_at = (
            datetime.fromisoformat(data["calculated_at"])
            if isinstance(data.get("calculated_at"), str)
            else datetime.now(timezone.utc)
        )
        status = PerformanceStatus(data.get("status", PerformanceStatus.UNKNOWN.value))
        confidence = Confidence(data.get("confidence", Confidence.MEDIUM.value))
        provenance = EvidenceProvenanceType(data.get("provenance", EvidenceProvenanceType.DERIVED.value))

        period = _decode_temporal_period(data.get("period", {"period_type": "POINT_IN_TIME"}))
        obs_metrics = _decode_observed_metrics(data.get("observed_metrics", {}))
        der_metrics = _decode_derived_metrics(data.get("derived_metrics", {}))

        return ProductPerformanceRecord(
            performance_id=data["performance_id"],
            product_id=data["product_id"],
            sku=data["sku"],
            period=period,
            status=status,
            sample_count=int(data.get("sample_count", 0)),
            observation_sample_count=int(data.get("observation_sample_count", 0)),
            outcome_sample_count=int(data.get("outcome_sample_count", 0)),
            observed_metrics=obs_metrics,
            derived_metrics=der_metrics,
            product_memory_ids=tuple(data.get("product_memory_ids", [])),
            outcome_ids=tuple(data.get("outcome_ids", [])),
            mission_ids=tuple(data.get("mission_ids", [])),
            decision_ids=tuple(data.get("decision_ids", [])),
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
        raise InvalidProductPerformanceDataError(f"Failed to decode ProductPerformanceRecord: {e}") from e


class JsonProductPerformanceRepository(ProductPerformanceRepository):
    """
    Implementación JSON durable del puerto ProductPerformanceRepository.
    Mantiene registros de performance de productos en almacenamiento JSON persistente con escrituras atómicas.
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
            raise InvalidProductPerformanceDataError(f"Corrupted JSON file at {self.file_path}: {e}") from e
        except Exception as e:
            raise JsonProductPerformanceRepositoryError(f"Error reading file {self.file_path}: {e}") from e

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
            raise JsonProductPerformanceRepositoryError(f"Failed to save performances to {self.file_path}: {e}") from e

    def save_performance(self, performance: ProductPerformanceRecord) -> None:
        root = self._load_all()
        encoded = _encode_json_value({
            "performance_id": performance.performance_id,
            "product_id": performance.product_id,
            "sku": performance.sku,
            "period": {
                "period_type": performance.period.period_type,
                "period_start": performance.period.period_start,
                "period_end": performance.period.period_end,
            },
            "status": performance.status,
            "sample_count": performance.sample_count,
            "observation_sample_count": performance.observation_sample_count,
            "outcome_sample_count": performance.outcome_sample_count,
            "observed_metrics": {
                "observed_sales_units": performance.observed_metrics.observed_sales_units,
                "observed_revenue": performance.observed_metrics.observed_revenue,
                "observed_cancellations_units": performance.observed_metrics.observed_cancellations_units,
                "observed_returns_units": performance.observed_metrics.observed_returns_units,
                "observed_stock_level": performance.observed_metrics.observed_stock_level,
                "observed_price": performance.observed_metrics.observed_price,
                "observed_cost": performance.observed_metrics.observed_cost,
                "currency": performance.observed_metrics.currency,
            },
            "derived_metrics": {
                "gross_margin_amount": performance.derived_metrics.gross_margin_amount,
                "gross_margin_percentage": performance.derived_metrics.gross_margin_percentage,
                "cancellation_rate": performance.derived_metrics.cancellation_rate,
                "return_rate": performance.derived_metrics.return_rate,
                "outcome_success_rate": performance.derived_metrics.outcome_success_rate,
                "average_selling_price": performance.derived_metrics.average_selling_price,
            },
            "product_memory_ids": list(performance.product_memory_ids),
            "outcome_ids": list(performance.outcome_ids),
            "mission_ids": list(performance.mission_ids),
            "decision_ids": list(performance.decision_ids),
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

    def get_performance_by_id(self, performance_id: str) -> Optional[ProductPerformanceRecord]:
        root = self._load_all()
        data = root["performances"].get(performance_id)
        if not data:
            return None
        return _decode_performance_record(data)

    def get_performances_by_product_id(self, product_id: str) -> List[ProductPerformanceRecord]:
        root = self._load_all()
        results = []
        for data in root["performances"].values():
            if data.get("product_id") == product_id:
                results.append(_decode_performance_record(data))
        return results

    def get_performances_by_sku(self, sku: str) -> List[ProductPerformanceRecord]:
        root = self._load_all()
        results = []
        for data in root["performances"].values():
            if data.get("sku") == sku:
                results.append(_decode_performance_record(data))
        return results

    def get_performance_by_idempotency_key(self, idempotency_key: str) -> Optional[ProductPerformanceRecord]:
        root = self._load_all()
        for data in root["performances"].values():
            if data.get("idempotency_key") == idempotency_key:
                return _decode_performance_record(data)
        return None
