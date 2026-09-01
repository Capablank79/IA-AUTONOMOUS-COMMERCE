"""
Adaptadores de integración para transformar registros de dominio (J.2, J.3, J.4)
en EventRecords canónicos para su publicación en el Event Bus (Hito J.5).

Mantiene desacoplados los servicios de detección/monitoreo del bus de eventos.
"""

from datetime import datetime, timezone
from typing import Optional, Dict, Any

from src.domain.events.models import EventRecord, EventType
from src.domain.market_monitoring.models import MarketObservation
from src.domain.opportunity_detection.models import OpportunityRecord
from src.domain.change_detection.models import ChangeRecord


def build_change_detected_event(
    change_record: ChangeRecord,
    provenance: str = "J4_CHANGE_DETECTION",
) -> EventRecord:
    """
    Construye un EventRecord de tipo CHANGE_DETECTED a partir de un ChangeRecord (J.4).
    Preserva correlación, causación (change_id), procedencia e inmutabilidad.
    No duplica innecesariamente payloads masivos; referencia la entidad persistida.
    """
    event_id = f"evt-chg-{change_record.change_id}"

    # Payload mínimo referencial y resumen de hechos observados
    payload = {
        "change_id": change_record.change_id,
        "change_type": change_record.change_type.value,
        "significance": change_record.significance.value,
        "changed_fields": list(change_record.changed_fields),
        "previous_reference": change_record.previous_reference,
        "current_reference": change_record.current_reference,
        "observed_changes_count": len(change_record.observed_changes),
        "derived_deltas_count": len(change_record.derived_deltas),
        "unknown_fields": list(change_record.unknown_fields),
    }

    return EventRecord(
        event_id=event_id,
        event_type=EventType.CHANGE_DETECTED,
        subject_type=change_record.subject_type.value,
        subject_id=change_record.subject_id,
        occurred_at=change_record.detected_at,
        recorded_at=datetime.now(timezone.utc),
        correlation_id=change_record.correlation_id,
        causation_id=change_record.change_id,
        provenance=provenance,
        payload_reference=change_record.change_id,
        payload=payload,
        metadata={"idempotency_key": change_record.idempotency_key},
    )


def build_market_observation_event(
    observation: MarketObservation,
    provenance: str = "J2_MARKET_MONITORING",
) -> EventRecord:
    """
    Construye un EventRecord de tipo MARKET_OBSERVATION_CREATED a partir de un MarketObservation (J.2).
    """
    event_id = f"evt-obs-{observation.observation_id}"
    payload = {
        "observation_id": observation.observation_id,
        "source": observation.source,
        "source_type": observation.source_type.value,
        "entity_id": observation.entity_id,
        "price": str(observation.price.amount) if observation.price is not None else None,
        "currency": observation.price.currency if observation.price is not None else None,
        "stock": observation.stock,
        "status": observation.status.value,
    }

    return EventRecord(
        event_id=event_id,
        event_type=EventType.MARKET_OBSERVATION_CREATED,
        subject_type="MARKET_OBSERVATION",
        subject_id=observation.entity_id,
        occurred_at=observation.observed_at,
        recorded_at=datetime.now(timezone.utc),
        correlation_id=observation.correlation_id,
        causation_id=observation.observation_id,
        provenance=provenance,
        payload_reference=observation.observation_id,
        payload=payload,
        metadata={"source": observation.source},
    )


def build_opportunity_detected_event(
    opportunity: OpportunityRecord,
    provenance: str = "J3_OPPORTUNITY_DETECTION",
) -> EventRecord:
    """
    Construye un EventRecord de tipo OPPORTUNITY_DETECTED a partir de un OpportunityRecord (J.3).
    """
    event_id = f"evt-opp-{opportunity.opportunity_id}"
    score_val = None
    if opportunity.derived_metrics and opportunity.derived_metrics.opportunity_score is not None:
        score_val = str(opportunity.derived_metrics.opportunity_score)

    payload = {
        "opportunity_id": opportunity.opportunity_id,
        "canonical_product_id": opportunity.canonical_product_id,
        "status": opportunity.status.value,
        "score": score_val,
        "confidence": opportunity.confidence.value,
    }

    return EventRecord(
        event_id=event_id,
        event_type=EventType.OPPORTUNITY_DETECTED,
        subject_type="OPPORTUNITY",
        subject_id=opportunity.canonical_product_id,
        occurred_at=opportunity.detected_at,
        recorded_at=datetime.now(timezone.utc),
        correlation_id=opportunity.correlation_id,
        causation_id=opportunity.opportunity_id,
        provenance=provenance,
        payload_reference=opportunity.opportunity_id,
        payload=payload,
        metadata={"marketplace": opportunity.marketplace.value},
    )
