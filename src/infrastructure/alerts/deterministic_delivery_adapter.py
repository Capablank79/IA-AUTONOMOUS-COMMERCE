"""
Adaptadores de entrega deterministas para alertas (Autonomous Alerts - Hito J.6).

Provee implementaciones desacopladas de AlertDeliveryPort:
- InMemoryAlertDeliveryAdapter: Para testing y despacho in-process determinista.
- ConfigurableDeterministicDeliveryAdapter: Permite simular respuestas de entrega (DELIVERED, FAILED, UNKNOWN)
  sin llamar a servicios externos reales.
"""

from datetime import datetime, timezone
import time
import uuid
from typing import Optional, List, Dict, Callable
from types import MappingProxyType

from src.domain.alerts.models import (
    AlertRecord,
    AlertDeliveryResult,
    AlertDeliveryStatus,
)
from src.domain.alerts.ports import AlertDeliveryPort


class InMemoryAlertDeliveryAdapter(AlertDeliveryPort):
    """
    Adaptador de entrega en memoria determinista.
    """

    def __init__(self, channel_name: str = "IN_MEMORY", simulated_status: AlertDeliveryStatus = AlertDeliveryStatus.DELIVERED):
        self._channel_name = channel_name
        self.simulated_status = simulated_status
        self.delivered_alerts: List[AlertRecord] = []
        self.delivery_results: List[AlertDeliveryResult] = []
        self.failure_error_message: Optional[str] = None

    @property
    def channel_name(self) -> str:
        return self._channel_name

    def set_simulated_status(self, status: AlertDeliveryStatus, error_msg: Optional[str] = None) -> None:
        self.simulated_status = status
        self.failure_error_message = error_msg

    def deliver(self, alert: AlertRecord) -> AlertDeliveryResult:
        if not isinstance(alert, AlertRecord):
            raise ValueError("alert must be an instance of AlertRecord")

        start_time = time.perf_counter()
        now = datetime.now(timezone.utc)
        delivery_id = f"deliv-alt-{alert.alert_id[:8]}-{uuid.uuid4().hex[:6]}"

        if self.simulated_status == AlertDeliveryStatus.DELIVERED:
            self.delivered_alerts.append(alert)
            duration_ms = (time.perf_counter() - start_time) * 1000.0
            res = AlertDeliveryResult(
                delivery_id=delivery_id,
                alert_id=alert.alert_id,
                channel=self.channel_name,
                status=AlertDeliveryStatus.DELIVERED,
                attempted_at=now,
                correlation_id=alert.correlation_id,
                recipient="internal://default-recipient",
                provider_reference=f"mem-ref-{alert.alert_id}",
                error_category=None,
                error_message=None,
                execution_duration_ms=round(duration_ms, 2),
                metadata=MappingProxyType({"delivery_type": "in_memory_simulation"}),
            )
        elif self.simulated_status == AlertDeliveryStatus.FAILED:
            duration_ms = (time.perf_counter() - start_time) * 1000.0
            res = AlertDeliveryResult(
                delivery_id=delivery_id,
                alert_id=alert.alert_id,
                channel=self.channel_name,
                status=AlertDeliveryStatus.FAILED,
                attempted_at=now,
                correlation_id=alert.correlation_id,
                recipient="internal://default-recipient",
                provider_reference=None,
                error_category="SIMULATED_FAILURE",
                error_message=self.failure_error_message or "Simulated channel delivery failure",
                execution_duration_ms=round(duration_ms, 2),
                metadata=MappingProxyType({"delivery_type": "in_memory_simulation"}),
            )
        else:
            # UNKNOWN or SUPPRESSED
            duration_ms = (time.perf_counter() - start_time) * 1000.0
            res = AlertDeliveryResult(
                delivery_id=delivery_id,
                alert_id=alert.alert_id,
                channel=self.channel_name,
                status=self.simulated_status,
                attempted_at=now,
                correlation_id=alert.correlation_id,
                recipient="internal://default-recipient",
                provider_reference=None,
                error_category="UNKNOWN_STATE" if self.simulated_status == AlertDeliveryStatus.UNKNOWN else None,
                error_message=self.failure_error_message or "Channel status indeterminate",
                execution_duration_ms=round(duration_ms, 2),
                metadata=MappingProxyType({"delivery_type": "in_memory_simulation"}),
            )

        self.delivery_results.append(res)
        return res
