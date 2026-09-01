"""
Servicio de Aplicación para Alertas Autónomas (Autonomous Alerts - Hito J.6).

Coordina:
- Evaluación determinista de eventos.
- Deduplicación e Idempotencia estricta.
- Throttling / Cooldown determinista por sujeto y tipo de alerta.
- Persistencia durable vía AlertRepositoryPort.
- Despacho y entrega desacoplada vía AlertDeliveryPort.
- Aislamiento riguroso de fallos.
- Cero creación de Decisiones de negocio, Acciones de Marketplace ni Misiones Continuas (J.7).
"""

from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Tuple
import logging
from types import MappingProxyType

from src.domain.events.models import EventRecord
from src.domain.alerts.models import (
    AlertRecord,
    AlertType,
    AlertSeverity,
    AlertStatus,
    AlertDeliveryStatus,
    AlertDeliveryResult,
)
from src.domain.alerts.ports import (
    AlertRepositoryPort,
    AlertDeliveryPort,
)
from src.domain.alerts.rules import (
    DeterministicAlertRulesEngine,
    AlertEvaluationResult,
)
from src.domain.scheduling.models import Clock, SystemClock

logger = logging.getLogger(__name__)


class AlertService:
    """
    Servicio de Alertas Autónomas.
    """

    def __init__(
        self,
        alert_repository: AlertRepositoryPort,
        delivery_port: Optional[AlertDeliveryPort] = None,
        clock: Optional[Clock] = None,
        cooldown_seconds: float = 0.0,
    ):
        self.alert_repository = alert_repository
        self.delivery_port = delivery_port
        self.clock = clock or SystemClock()
        self.cooldown_seconds = cooldown_seconds
        self._last_alert_timestamps: Dict[Tuple[str, str], datetime] = {}

    def process_event(self, event: EventRecord) -> Optional[AlertRecord]:
        """
        Procesa un evento:
        1. Evalúa si amerita alerta con reglas deterministas.
        2. Aplica deduplicación e idempotencia por idempotency_key.
        3. Aplica throttling/cooldown por (subject_id, alert_type) si está configurado.
        4. Persiste AlertRecord de forma durable.
        5. Intenta despacho vía AlertDeliveryPort si está configurado.
        6. Retorna el AlertRecord creado o existente.
        """
        if not isinstance(event, EventRecord):
            raise ValueError("event must be an instance of EventRecord")

        now = self.clock.now()

        # 1. Evaluar evento determinísticamente
        eval_result: AlertEvaluationResult = DeterministicAlertRulesEngine.evaluate(event, now=now)
        if not eval_result.is_eligible or eval_result.alert is None:
            return None

        candidate_alert = eval_result.alert

        # 2. Deduplicación e Idempotencia previa
        existing_alert = self.alert_repository.get_by_idempotency_key(candidate_alert.idempotency_key)
        if existing_alert:
            # Replay seguro: retornar la alerta ya existente sin re-despachar
            return existing_alert

        # 3. Throttling / Cooldown por (subject_id, alert_type)
        # CRITICAL bypasses throttling si es necesario; para J.6 aplicamos cooldown general con bypass de CRITICAL
        throttle_key = (candidate_alert.subject_id, candidate_alert.alert_type.value)
        if self.cooldown_seconds > 0 and candidate_alert.severity != AlertSeverity.CRITICAL:
            last_time = self._last_alert_timestamps.get(throttle_key)
            if last_time and (now - last_time).total_seconds() < self.cooldown_seconds:
                # Suprimir alerta por cooldown
                suppressed_alert = AlertRecord(
                    alert_id=candidate_alert.alert_id,
                    alert_type=candidate_alert.alert_type,
                    severity=candidate_alert.severity,
                    status=AlertStatus.SUPPRESSED,
                    subject_type=candidate_alert.subject_type,
                    subject_id=candidate_alert.subject_id,
                    title=candidate_alert.title,
                    message=f"[SUPPRESSED BY COOLDOWN] {candidate_alert.message}",
                    event_id=candidate_alert.event_id,
                    occurred_at=candidate_alert.occurred_at,
                    created_at=now,
                    correlation_id=candidate_alert.correlation_id,
                    causation_id=candidate_alert.causation_id,
                    provenance=candidate_alert.provenance,
                    idempotency_key=candidate_alert.idempotency_key,
                    evidence_reference=candidate_alert.evidence_reference,
                    delivery_status=AlertDeliveryStatus.SUPPRESSED,
                    template_data=candidate_alert.template_data,
                    channel_metadata=candidate_alert.channel_metadata,
                    metadata=MappingProxyType({"suppression_reason": "COOLDOWN_ACTIVE"}),
                )
                return self.alert_repository.save(suppressed_alert)

        # Guardar en memoria de timestamp para cooldown
        self._last_alert_timestamps[throttle_key] = now

        # 4. Persistir alerta como creada
        persisted_alert = self.alert_repository.save(candidate_alert)

        # 5. Despacho por delivery port si existe
        if self.delivery_port:
            try:
                delivery_result = self.delivery_port.deliver(persisted_alert)
                self.alert_repository.record_delivery_result(delivery_result)

                # Si el estado de entrega fue confirmado
                if delivery_result.status != persisted_alert.delivery_status:
                    updated_alert = AlertRecord(
                        alert_id=persisted_alert.alert_id,
                        alert_type=persisted_alert.alert_type,
                        severity=persisted_alert.severity,
                        status=AlertStatus.PROCESSED if delivery_result.status == AlertDeliveryStatus.DELIVERED else persisted_alert.status,
                        subject_type=persisted_alert.subject_type,
                        subject_id=persisted_alert.subject_id,
                        title=persisted_alert.title,
                        message=persisted_alert.message,
                        event_id=persisted_alert.event_id,
                        occurred_at=persisted_alert.occurred_at,
                        created_at=persisted_alert.created_at,
                        correlation_id=persisted_alert.correlation_id,
                        causation_id=persisted_alert.causation_id,
                        provenance=persisted_alert.provenance,
                        idempotency_key=persisted_alert.idempotency_key,
                        evidence_reference=persisted_alert.evidence_reference,
                        delivery_status=delivery_result.status,
                        template_data=persisted_alert.template_data,
                        channel_metadata=persisted_alert.channel_metadata,
                        metadata=persisted_alert.metadata,
                    )
                    # Persistir estado actualizado
                    # Nota: JsonAlertRepository.save es inmutable por ID pero para actualizar podemos sobrescribir archivo de alerta
                    # Para mantener inmutabilidad y atomicidad, actualizamos archivo
                    persisted_alert = self._update_alert_status(updated_alert)
            except Exception as e:
                logger.warning(
                    "Error inesperado en delivery port para alerta %s: %s",
                    persisted_alert.alert_id,
                    str(e),
                )
                failed_res = AlertDeliveryResult(
                    delivery_id=f"deliv-err-{persisted_alert.alert_id[:8]}",
                    alert_id=persisted_alert.alert_id,
                    channel=self.delivery_port.channel_name,
                    status=AlertDeliveryStatus.FAILED,
                    attempted_at=now,
                    correlation_id=persisted_alert.correlation_id,
                    error_category="UNEXPECTED_PORT_EXCEPTION",
                    error_message=str(e),
                    metadata=MappingProxyType({"exception": str(e)}),
                )
                self.alert_repository.record_delivery_result(failed_res)
                failed_alert = AlertRecord(
                    alert_id=persisted_alert.alert_id,
                    alert_type=persisted_alert.alert_type,
                    severity=persisted_alert.severity,
                    status=persisted_alert.status,
                    subject_type=persisted_alert.subject_type,
                    subject_id=persisted_alert.subject_id,
                    title=persisted_alert.title,
                    message=persisted_alert.message,
                    event_id=persisted_alert.event_id,
                    occurred_at=persisted_alert.occurred_at,
                    created_at=persisted_alert.created_at,
                    correlation_id=persisted_alert.correlation_id,
                    causation_id=persisted_alert.causation_id,
                    provenance=persisted_alert.provenance,
                    idempotency_key=persisted_alert.idempotency_key,
                    evidence_reference=persisted_alert.evidence_reference,
                    delivery_status=AlertDeliveryStatus.FAILED,
                    template_data=persisted_alert.template_data,
                    channel_metadata=persisted_alert.channel_metadata,
                    metadata=persisted_alert.metadata,
                )
                persisted_alert = self._update_alert_status(failed_alert)

        return persisted_alert

    def _update_alert_status(self, alert: AlertRecord) -> AlertRecord:
        """Actualiza el archivo de alerta si cambió su estado de delivery."""
        if hasattr(self.alert_repository, "_atomic_write_json") and hasattr(self.alert_repository, "alerts_dir"):
            file_path = self.alert_repository.alerts_dir / f"{alert.alert_id}.json"
            payload = {
                "alert_id": alert.alert_id,
                "alert_type": alert.alert_type.value,
                "severity": alert.severity.value,
                "status": alert.status.value,
                "subject_type": alert.subject_type,
                "subject_id": alert.subject_id,
                "title": alert.title,
                "message": alert.message,
                "event_id": alert.event_id,
                "occurred_at": alert.occurred_at.isoformat(),
                "created_at": alert.created_at.isoformat(),
                "correlation_id": alert.correlation_id,
                "causation_id": alert.causation_id,
                "provenance": alert.provenance,
                "idempotency_key": alert.idempotency_key,
                "evidence_reference": alert.evidence_reference,
                "delivery_status": alert.delivery_status.value,
                "template_data": dict(alert.template_data),
                "channel_metadata": dict(alert.channel_metadata),
                "metadata": dict(alert.metadata),
            }
            self.alert_repository._atomic_write_json(file_path, payload)
        return alert

    def get_alert(self, alert_id: str) -> Optional[AlertRecord]:
        return self.alert_repository.get_by_id(alert_id)

    def list_alerts(
        self,
        alert_type: Optional[AlertType] = None,
        severity: Optional[AlertSeverity] = None,
        subject_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[AlertRecord]:
        return self.alert_repository.list_alerts(
            alert_type=alert_type,
            severity=severity,
            subject_id=subject_id,
            correlation_id=correlation_id,
            limit=limit,
        )

    def get_deliveries_for_alert(self, alert_id: str) -> List[AlertDeliveryResult]:
        return self.alert_repository.list_delivery_results_by_alert(alert_id)
