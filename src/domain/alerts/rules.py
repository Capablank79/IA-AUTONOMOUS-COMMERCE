"""
Reglas deterministas de evaluación de alertas para el Hito J.6.

Evalúa eventos de dominio (CHANGE_DETECTED, OPPORTUNITY_DETECTED, etc.)
y determina si son elegibles para generar una alerta, asignando su severidad y plantilla.

Límites:
- NO usa LLMs ni algoritmos no deterministas.
- UNKNOWN no genera CRITICAL falso; preserva incertidumbre.
- No inventa umbrales comerciales arbitrarios: evalúa ChangeSignificance, OpportunityStatus/Confidence
  o fallos de fuente documentados.
"""

from typing import Optional, Dict, Any, Tuple
from datetime import datetime, timezone
import uuid
from types import MappingProxyType

from src.domain.events.models import EventRecord, EventType
from src.domain.alerts.models import (
    AlertRecord,
    AlertType,
    AlertSeverity,
    AlertStatus,
    AlertDeliveryStatus,
)
from src.domain.change_detection.models import ChangeSignificance, ChangeType
from src.domain.opportunity_detection.models import OpportunityStatus
from src.domain.market_intelligence.models import Confidence


class AlertEvaluationResult:
    """Resultado determinista de la evaluación de una regla de alerta."""
    def __init__(
        self,
        is_eligible: bool,
        alert: Optional[AlertRecord] = None,
        reason: Optional[str] = None,
    ):
        self.is_eligible = is_eligible
        self.alert = alert
        self.reason = reason


class DeterministicAlertRulesEngine:
    """
    Motor de reglas deterministas de evaluación de eventos para generar alertas en J.6.
    """

    @staticmethod
    def evaluate(event: EventRecord, now: Optional[datetime] = None) -> AlertEvaluationResult:
        """
        Evalúa un EventRecord y retorna si amerita una alerta estructurada.
        """
        if not isinstance(event, EventRecord):
            return AlertEvaluationResult(is_eligible=False, reason="Invalid event format")

        eval_time = now or datetime.now(timezone.utc)
        if eval_time.tzinfo is None:
            eval_time = eval_time.replace(tzinfo=timezone.utc)

        # 1. Evaluar eventos de cambio (CHANGE_DETECTED)
        if event.event_type == EventType.CHANGE_DETECTED:
            return DeterministicAlertRulesEngine._evaluate_change_detected(event, eval_time)

        # 2. Evaluar eventos de oportunidad (OPPORTUNITY_DETECTED)
        if event.event_type == EventType.OPPORTUNITY_DETECTED:
            return DeterministicAlertRulesEngine._evaluate_opportunity_detected(event, eval_time)

        # 3. MARKET_OBSERVATION_CREATED: por diseño y control de alert fatigue, no genera alerta
        # salvo que contenga un fallo técnico explícito (SOURCE_FAILURE).
        if event.event_type == EventType.MARKET_OBSERVATION_CREATED:
            return DeterministicAlertRulesEngine._evaluate_market_observation(event, eval_time)

        return AlertEvaluationResult(is_eligible=False, reason=f"EventType {event.event_type.value} not eligible for alert")

    @staticmethod
    def _evaluate_change_detected(event: EventRecord, now: datetime) -> AlertEvaluationResult:
        payload = event.payload
        significance_raw = payload.get("significance", "NONE")
        try:
            significance = ChangeSignificance(significance_raw)
        except Exception:
            significance = ChangeSignificance.UNKNOWN

        change_type_raw = payload.get("change_type", "NO_CHANGE")
        try:
            change_type = ChangeType(change_type_raw)
        except Exception:
            change_type = ChangeType.UNKNOWN_CHANGED

        # Regla: NONE o NEGLIGIBLE no generan alerta
        if significance in (ChangeSignificance.NONE, ChangeSignificance.NEGLIGIBLE):
            return AlertEvaluationResult(
                is_eligible=False,
                reason=f"Change significance '{significance.value}' does not warrant an alert",
            )

        # Determinar Severidad y AlertType
        if change_type == ChangeType.SOURCE_STATUS_CHANGED:
            alert_type = AlertType.SOURCE_FAILURE
            severity = AlertSeverity.WARNING if significance != ChangeSignificance.CRITICAL else AlertSeverity.HIGH
        else:
            alert_type = AlertType.SIGNIFICANT_CHANGE
            if significance == ChangeSignificance.CRITICAL:
                severity = AlertSeverity.CRITICAL
            elif significance == ChangeSignificance.SIGNIFICANT:
                severity = AlertSeverity.HIGH
            elif significance == ChangeSignificance.MODERATE:
                severity = AlertSeverity.WARNING
            else:
                # UNKNOWN
                severity = AlertSeverity.INFO

        title = f"Cambio detectado en {event.subject_type}: {change_type.value}"
        summary = payload.get("change_summary") or f"Cambio de significancia {significance.value} en {event.subject_id}."
        message = f"[{severity.value}] {summary}"

        meta_dict = dict(event.metadata) if event.metadata else {}
        meta_dict["source_event_type"] = event.event_type.value

        alert = AlertRecord(
            alert_id=f"alt-chg-{event.event_id[:8]}-{uuid.uuid4().hex[:6]}",
            alert_type=alert_type,
            severity=severity,
            status=AlertStatus.CREATED,
            subject_type=event.subject_type,
            subject_id=event.subject_id,
            title=title,
            message=message,
            event_id=event.event_id,
            occurred_at=event.occurred_at,
            created_at=now,
            correlation_id=event.correlation_id,
            causation_id=event.causation_id,
            provenance=event.provenance,
            evidence_reference=event.payload_reference,
            delivery_status=AlertDeliveryStatus.PENDING,
            template_data=MappingProxyType({
                "change_type": change_type.value,
                "significance": significance.value,
                "observed_changes_count": payload.get("observed_changes_count", 0),
            }),
            metadata=MappingProxyType(meta_dict),
        )

        return AlertEvaluationResult(is_eligible=True, alert=alert)

    @staticmethod
    def _evaluate_opportunity_detected(event: EventRecord, now: datetime) -> AlertEvaluationResult:
        payload = event.payload
        status_raw = payload.get("status", "UNKNOWN")
        try:
            status = OpportunityStatus(status_raw)
        except Exception:
            status = OpportunityStatus.UNKNOWN

        # Regla: INVALID, DISCARDED o INSUFFICIENT_DATA no generan alertas de alta prioridad
        if status in (OpportunityStatus.INVALID, OpportunityStatus.DISCARDED):
            return AlertEvaluationResult(
                is_eligible=False,
                reason=f"Opportunity status '{status.value}' is not eligible for alert",
            )

        confidence_raw = payload.get("confidence", "UNKNOWN")
        try:
            confidence = Confidence(confidence_raw)
        except Exception:
            confidence = Confidence.UNKNOWN

        opportunity_type = payload.get("opportunity_type", "COMMERCIAL")
        score = payload.get("opportunity_score")

        # Determinar Severidad determinística
        if status == OpportunityStatus.VALID and confidence == Confidence.HIGH:
            severity = AlertSeverity.HIGH
        elif status == OpportunityStatus.VALID and confidence == Confidence.MEDIUM:
            severity = AlertSeverity.WARNING
        elif status == OpportunityStatus.INSUFFICIENT_DATA or confidence == Confidence.LOW:
            severity = AlertSeverity.INFO
        elif confidence == Confidence.UNKNOWN or status == OpportunityStatus.UNKNOWN:
            severity = AlertSeverity.INFO
        else:
            severity = AlertSeverity.INFO

        title = f"Oportunidad detectada: {opportunity_type} en {event.subject_id}"
        message = (
            f"[{severity.value}] Oportunidad {opportunity_type} identificada para {event.subject_id} "
            f"con estado {status.value} y confianza {confidence.value}."
        )

        opp_meta_dict = dict(event.metadata) if event.metadata else {}
        opp_meta_dict["source_event_type"] = event.event_type.value

        alert = AlertRecord(
            alert_id=f"alt-opp-{event.event_id[:8]}-{uuid.uuid4().hex[:6]}",
            alert_type=AlertType.OPPORTUNITY_DETECTED,
            severity=severity,
            status=AlertStatus.CREATED,
            subject_type=event.subject_type,
            subject_id=event.subject_id,
            title=title,
            message=message,
            event_id=event.event_id,
            occurred_at=event.occurred_at,
            created_at=now,
            correlation_id=event.correlation_id,
            causation_id=event.causation_id,
            provenance=event.provenance,
            evidence_reference=event.payload_reference,
            delivery_status=AlertDeliveryStatus.PENDING,
            template_data=MappingProxyType({
                "opportunity_type": str(opportunity_type),
                "status": status.value,
                "confidence": confidence.value,
                "score": str(score) if score is not None else None,
            }),
            metadata=MappingProxyType(opp_meta_dict),
        )

        return AlertEvaluationResult(is_eligible=True, alert=alert)

    @staticmethod
    def _evaluate_market_observation(event: EventRecord, now: datetime) -> AlertEvaluationResult:
        payload = event.payload
        is_failure = payload.get("is_source_failure", False) or payload.get("source_status") == "FAILED"
        if is_failure:
            title = f"Fallo técnico de fuente en {event.subject_id}"
            message = f"[WARNING] La observación de mercado reportó un fallo de captura o fuente en {event.subject_id}."
            alert = AlertRecord(
                alert_id=f"alt-src-{event.event_id[:8]}-{uuid.uuid4().hex[:6]}",
                alert_type=AlertType.SOURCE_FAILURE,
                severity=AlertSeverity.WARNING,
                status=AlertStatus.CREATED,
                subject_type=event.subject_type,
                subject_id=event.subject_id,
                title=title,
                message=message,
                event_id=event.event_id,
                occurred_at=event.occurred_at,
                created_at=now,
                correlation_id=event.correlation_id,
                causation_id=event.causation_id,
                provenance=event.provenance,
                evidence_reference=event.payload_reference,
                delivery_status=AlertDeliveryStatus.PENDING,
                template_data=MappingProxyType({"failure_reason": payload.get("error_message", "Source error")}),
                metadata=MappingProxyType({"source_event_type": event.event_type.value}),
            )
            return AlertEvaluationResult(is_eligible=True, alert=alert)

        return AlertEvaluationResult(
            is_eligible=False,
            reason="Market observation is normal operation and does not trigger alert",
        )
