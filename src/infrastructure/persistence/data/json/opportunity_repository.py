import json
import os
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal
from typing import Union, Optional, Any, Dict, List, Tuple
from types import MappingProxyType

from src.domain.opportunity_detection.models import (
    OpportunityRecord,
    OpportunityType,
    OpportunityStatus,
    ObservedOpportunityMetrics,
    DerivedOpportunityMetrics,
)
from src.domain.opportunity_detection.ports import OpportunityRepositoryPort
from src.domain.market_monitoring.models import NormalizedPrice
from src.domain.market_intelligence.models import Marketplace, Confidence


class JsonOpportunityRepositoryError(Exception):
    """Excepción base para errores en el repositorio de oportunidades."""
    pass


class CorruptedOpportunityDataError(JsonOpportunityRepositoryError):
    """Se lanza cuando los datos de un registro de oportunidad están corruptos."""
    pass


SENSITIVE_KEYS = {
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "pan",
    "cvv",
    "private_key",
    "credential",
    "access_token",
    "refresh_token",
    "authorization",
}


def _encode_json_value(val: Any) -> Any:
    """Serializa estructuras de forma determinista y sanitiza claves sensibles recursivamente."""
    if isinstance(val, datetime):
        return val.isoformat()
    if isinstance(val, Decimal):
        return str(val)
    if hasattr(val, "value"):
        return val.value
    if isinstance(val, (dict, MappingProxyType)):
        cleaned = {}
        for k, v in val.items():
            k_str = str(k).lower()
            if any(s in k_str for s in SENSITIVE_KEYS):
                continue
            cleaned[str(k)] = _encode_json_value(v)
        return cleaned
    if isinstance(val, (list, tuple)):
        return [_encode_json_value(v) for v in val]
    return val


def _decode_opportunity_record(data: Dict[str, Any]) -> OpportunityRecord:
    """Reconstruye una instancia de OpportunityRecord a partir de un dict JSON."""
    try:
        opp_id = data["opportunity_id"]
        canonical_product_id = data["canonical_product_id"]
        marketplace = Marketplace(data["marketplace"])

        detected_at = datetime.fromisoformat(data["detected_at"])
        if detected_at.tzinfo is None:
            detected_at = detected_at.replace(tzinfo=timezone.utc)

        opp_type = OpportunityType(data["opportunity_type"])
        status = OpportunityStatus(data["status"])
        confidence = Confidence(data["confidence"])
        source_observation_ids = tuple(data["source_observation_ids"])

        # Decodificar observed_metrics
        obs_m = data["observed_metrics"]
        obs_price = None
        if obs_m.get("observed_price"):
            p_data = obs_m["observed_price"]
            obs_price = NormalizedPrice(
                amount=Decimal(str(p_data["amount"])),
                currency=p_data["currency"]
            )

        lowest_comp_price = None
        if obs_m.get("lowest_competitor_price"):
            p_data = obs_m["lowest_competitor_price"]
            lowest_comp_price = NormalizedPrice(
                amount=Decimal(str(p_data["amount"])),
                currency=p_data["currency"]
            )

        buy_box_price = None
        if obs_m.get("buy_box_winner_price"):
            p_data = obs_m["buy_box_winner_price"]
            buy_box_price = NormalizedPrice(
                amount=Decimal(str(p_data["amount"])),
                currency=p_data["currency"]
            )

        observed_metrics = ObservedOpportunityMetrics(
            observed_price=obs_price,
            observed_sold_quantity=obs_m.get("observed_sold_quantity"),
            observed_stock=obs_m.get("observed_stock"),
            observed_competitor_count=obs_m.get("observed_competitor_count"),
            lowest_competitor_price=lowest_comp_price,
            buy_box_winner_price=buy_box_price,
            observations_count=obs_m.get("observations_count", 1),
        )

        # Decodificar derived_metrics
        der_m = data["derived_metrics"]
        derived_metrics = DerivedOpportunityMetrics(
            price_gap_amount=Decimal(str(der_m["price_gap_amount"])) if der_m.get("price_gap_amount") is not None else None,
            price_gap_ratio=Decimal(str(der_m["price_gap_ratio"])) if der_m.get("price_gap_ratio") is not None else None,
            potential_margin_ratio=Decimal(str(der_m["potential_margin_ratio"])) if der_m.get("potential_margin_ratio") is not None else None,
            competition_density=der_m.get("competition_density"),
            demand_intensity=der_m.get("demand_intensity"),
            opportunity_score=Decimal(str(der_m["opportunity_score"])) if der_m.get("opportunity_score") is not None else None,
            scoring_rationale=tuple(der_m.get("scoring_rationale", [])),
        )

        return OpportunityRecord(
            opportunity_id=opp_id,
            canonical_product_id=canonical_product_id,
            marketplace=marketplace,
            detected_at=detected_at,
            opportunity_type=opp_type,
            status=status,
            confidence=confidence,
            source_observation_ids=source_observation_ids,
            observed_metrics=observed_metrics,
            derived_metrics=derived_metrics,
            category=data.get("category"),
            title=data.get("title"),
            product_sku=data.get("product_sku"),
            product_memory_id_ref=data.get("product_memory_id_ref"),
            supplier_memory_id_ref=data.get("supplier_memory_id_ref"),
            provenance=data.get("provenance", "LIVE"),
            correlation_id=data.get("correlation_id", "default-correlation"),
            idempotency_key=data.get("idempotency_key", ""),
            reasons=tuple(data.get("reasons", [])),
            unknown_fields=tuple(data.get("unknown_fields", [])),
            metadata=data.get("metadata", {}),
        )
    except Exception as e:
        raise CorruptedOpportunityDataError(f"Failed to decode OpportunityRecord: {str(e)}") from e


def _serialize_opportunity_record(opp: OpportunityRecord) -> Dict[str, Any]:
    """Serializa un OpportunityRecord a diccionario sanitizado para persistencia JSON."""
    raw_dict = {
        "opportunity_id": opp.opportunity_id,
        "canonical_product_id": opp.canonical_product_id,
        "marketplace": opp.marketplace.value,
        "detected_at": opp.detected_at,
        "opportunity_type": opp.opportunity_type.value,
        "status": opp.status.value,
        "confidence": opp.confidence.value,
        "source_observation_ids": list(opp.source_observation_ids),
        "observed_metrics": {
            "observed_price": {
                "amount": opp.observed_metrics.observed_price.amount,
                "currency": opp.observed_metrics.observed_price.currency,
            } if opp.observed_metrics.observed_price else None,
            "observed_sold_quantity": opp.observed_metrics.observed_sold_quantity,
            "observed_stock": opp.observed_metrics.observed_stock,
            "observed_competitor_count": opp.observed_metrics.observed_competitor_count,
            "lowest_competitor_price": {
                "amount": opp.observed_metrics.lowest_competitor_price.amount,
                "currency": opp.observed_metrics.lowest_competitor_price.currency,
            } if opp.observed_metrics.lowest_competitor_price else None,
            "buy_box_winner_price": {
                "amount": opp.observed_metrics.buy_box_winner_price.amount,
                "currency": opp.observed_metrics.buy_box_winner_price.currency,
            } if opp.observed_metrics.buy_box_winner_price else None,
            "observations_count": opp.observed_metrics.observations_count,
        },
        "derived_metrics": {
            "price_gap_amount": opp.derived_metrics.price_gap_amount,
            "price_gap_ratio": opp.derived_metrics.price_gap_ratio,
            "potential_margin_ratio": opp.derived_metrics.potential_margin_ratio,
            "competition_density": opp.derived_metrics.competition_density,
            "demand_intensity": opp.derived_metrics.demand_intensity,
            "opportunity_score": opp.derived_metrics.opportunity_score,
            "scoring_rationale": list(opp.derived_metrics.scoring_rationale),
        },
        "category": opp.category,
        "title": opp.title,
        "product_sku": opp.product_sku,
        "product_memory_id_ref": opp.product_memory_id_ref,
        "supplier_memory_id_ref": opp.supplier_memory_id_ref,
        "provenance": opp.provenance,
        "correlation_id": opp.correlation_id,
        "idempotency_key": opp.idempotency_key,
        "reasons": list(opp.reasons),
        "unknown_fields": list(opp.unknown_fields),
        "metadata": dict(opp.metadata),
    }
    return _encode_json_value(raw_dict)


class JsonOpportunityRepository(OpportunityRepositoryPort):
    """
    Adaptador de Persistencia JSON Durable para OpportunityRecord (Hito J.3).

    Características:
    - Escrituras atómicas con `.tmp` y `os.replace`.
    - Idempotencia estricta vía ID y clave determinista.
    - Sanitización recursiva de secretos.
    - Soporte completo para reinicio del sistema (durabilidad en disco).
    """

    def __init__(self, file_path: Union[str, Path]):
        self.file_path = Path(file_path)
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.file_path.exists():
            self._write_atomic([])

    def _read_raw(self) -> List[Dict[str, Any]]:
        if not self.file_path.exists():
            return []
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                content = f.read()
                if not content.strip():
                    return []
                return json.loads(content)
        except json.JSONDecodeError as e:
            raise CorruptedOpportunityDataError(f"Corrupted JSON in {self.file_path}") from e

    def _write_atomic(self, data: List[Dict[str, Any]]) -> None:
        tmp_file = self.file_path.with_suffix(".tmp")
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_file, self.file_path)

    def save(self, opportunity: OpportunityRecord) -> None:
        records = self._read_raw()
        serialized = _serialize_opportunity_record(opportunity)

        for i, r in enumerate(records):
            if (
                r.get("opportunity_id") == opportunity.opportunity_id
                or (opportunity.idempotency_key and r.get("idempotency_key") == opportunity.idempotency_key)
            ):
                records[i] = serialized
                self._write_atomic(records)
                return

        records.append(serialized)
        self._write_atomic(records)

    def save_all(self, opportunities: List[OpportunityRecord]) -> int:
        if not opportunities:
            return 0
        records = self._read_raw()
        existing_ids = {r.get("opportunity_id") for r in records}
        existing_idemp = {r.get("idempotency_key") for r in records if r.get("idempotency_key")}

        added_count = 0
        for opp in opportunities:
            if opp.opportunity_id in existing_ids or (opp.idempotency_key and opp.idempotency_key in existing_idemp):
                continue
            records.append(_serialize_opportunity_record(opp))
            existing_ids.add(opp.opportunity_id)
            if opp.idempotency_key:
                existing_idemp.add(opp.idempotency_key)
            added_count += 1

        if added_count > 0:
            self._write_atomic(records)
        return added_count

    def get_by_id(self, opportunity_id: str) -> Optional[OpportunityRecord]:
        records = self._read_raw()
        for r in records:
            if r.get("opportunity_id") == opportunity_id:
                return _decode_opportunity_record(r)
        return None

    def get_by_idempotency_key(self, idempotency_key: str) -> Optional[OpportunityRecord]:
        if not idempotency_key:
            return None
        records = self._read_raw()
        for r in records:
            if r.get("idempotency_key") == idempotency_key:
                return _decode_opportunity_record(r)
        return None

    def list_by_product(self, canonical_product_id: str, limit: int = 100) -> List[OpportunityRecord]:
        records = self._read_raw()
        matched = [
            _decode_opportunity_record(r)
            for r in records
            if r.get("canonical_product_id") == canonical_product_id
        ]
        matched.sort(key=lambda o: o.detected_at, reverse=True)
        return matched[:limit]

    def list_by_type(self, opportunity_type: OpportunityType, limit: int = 100) -> List[OpportunityRecord]:
        records = self._read_raw()
        matched = [
            _decode_opportunity_record(r)
            for r in records
            if r.get("opportunity_type") == opportunity_type.value
        ]
        matched.sort(key=lambda o: o.detected_at, reverse=True)
        return matched[:limit]

    def list_by_status(self, status: OpportunityStatus, limit: int = 100) -> List[OpportunityRecord]:
        records = self._read_raw()
        matched = [
            _decode_opportunity_record(r)
            for r in records
            if r.get("status") == status.value
        ]
        matched.sort(key=lambda o: o.detected_at, reverse=True)
        return matched[:limit]

    def list_all(self, limit: int = 1000) -> List[OpportunityRecord]:
        records = self._read_raw()
        decoded = [_decode_opportunity_record(r) for r in records]
        decoded.sort(key=lambda o: o.detected_at, reverse=True)
        return decoded[:limit]
