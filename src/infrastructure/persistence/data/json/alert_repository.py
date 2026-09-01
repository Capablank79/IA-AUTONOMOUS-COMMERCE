"""
Implementación JSON atómica y persistente del AlertRepository (Hito J.6).

Garantiza:
- Atomic write (.tmp -> os.replace)
- Sanitización recursiva de datos sensibles
- Idempotencia estricta por alert_id y por idempotency_key
- Persistencia de resultados de entrega por alerta
- Manejo de corrupción y resiliencia ante reinicios
- Cero almacenamiento de credenciales o secretos
"""

import json
import os
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal
from typing import Union, Optional, Any, Dict, List
from types import MappingProxyType

from src.domain.alerts.models import (
    AlertRecord,
    AlertType,
    AlertSeverity,
    AlertStatus,
    AlertDeliveryStatus,
    AlertDeliveryResult,
)
from src.domain.alerts.ports import AlertRepositoryPort


class JsonAlertRepositoryError(Exception):
    """Excepción base para errores en el repositorio JSON de alertas."""
    pass


class CorruptedAlertDataError(JsonAlertRepositoryError):
    """Se lanza cuando los datos de una alerta o entrega están corruptos."""
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
                cleaned[str(k)] = "[REDACTED]"
            else:
                cleaned[str(k)] = _encode_json_value(v)
        return cleaned
    if isinstance(val, (list, tuple)):
        return [_encode_json_value(v) for v in val]
    return val


def _decode_alert_record(data: Dict[str, Any]) -> AlertRecord:
    """Reconstruye una instancia de AlertRecord a partir de un dict JSON."""
    try:
        alert_id = data["alert_id"]
        alert_type = AlertType(data["alert_type"])
        severity = AlertSeverity(data["severity"])
        status = AlertStatus(data["status"])
        subject_type = data["subject_type"]
        subject_id = data["subject_id"]
        title = data["title"]
        message = data["message"]
        event_id = data["event_id"]

        occurred_at = datetime.fromisoformat(data["occurred_at"])
        if occurred_at.tzinfo is None:
            occurred_at = occurred_at.replace(tzinfo=timezone.utc)

        created_at = datetime.fromisoformat(data["created_at"])
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)

        correlation_id = data["correlation_id"]
        causation_id = data.get("causation_id")
        provenance = data.get("provenance", "SYSTEM")
        idempotency_key = data.get("idempotency_key", "")
        evidence_reference = data.get("evidence_reference")
        delivery_status = AlertDeliveryStatus(data.get("delivery_status", AlertDeliveryStatus.PENDING.value))

        template_data = data.get("template_data", {})
        channel_metadata = data.get("channel_metadata", {})
        metadata = data.get("metadata", {})

        return AlertRecord(
            alert_id=alert_id,
            alert_type=alert_type,
            severity=severity,
            status=status,
            subject_type=subject_type,
            subject_id=subject_id,
            title=title,
            message=message,
            event_id=event_id,
            occurred_at=occurred_at,
            created_at=created_at,
            correlation_id=correlation_id,
            causation_id=causation_id,
            provenance=provenance,
            idempotency_key=idempotency_key,
            evidence_reference=evidence_reference,
            delivery_status=delivery_status,
            template_data=template_data,
            channel_metadata=channel_metadata,
            metadata=metadata,
        )
    except Exception as e:
        raise CorruptedAlertDataError(f"Failed to decode AlertRecord from JSON: {e}") from e


def _decode_delivery_result(data: Dict[str, Any]) -> AlertDeliveryResult:
    """Reconstruye una instancia de AlertDeliveryResult a partir de un dict JSON."""
    try:
        delivery_id = data["delivery_id"]
        alert_id = data["alert_id"]
        channel = data["channel"]
        status = AlertDeliveryStatus(data["status"])

        attempted_at = datetime.fromisoformat(data["attempted_at"])
        if attempted_at.tzinfo is None:
            attempted_at = attempted_at.replace(tzinfo=timezone.utc)

        correlation_id = data["correlation_id"]
        recipient = data.get("recipient")
        provider_reference = data.get("provider_reference")
        error_category = data.get("error_category")
        error_message = data.get("error_message")
        execution_duration_ms = data.get("execution_duration_ms")
        metadata = data.get("metadata", {})

        return AlertDeliveryResult(
            delivery_id=delivery_id,
            alert_id=alert_id,
            channel=channel,
            status=status,
            attempted_at=attempted_at,
            correlation_id=correlation_id,
            recipient=recipient,
            provider_reference=provider_reference,
            error_category=error_category,
            error_message=error_message,
            execution_duration_ms=execution_duration_ms,
            metadata=metadata,
        )
    except Exception as e:
        raise CorruptedAlertDataError(f"Failed to decode AlertDeliveryResult from JSON: {e}") from e


class JsonAlertRepository(AlertRepositoryPort):
    """
    Repositorio durable de alertas en archivos JSON locales.
    """

    def __init__(self, base_directory: Union[str, Path]):
        self.base_dir = Path(base_directory)
        self.alerts_dir = self.base_dir / "alerts"
        self.deliveries_dir = self.base_dir / "alert_deliveries"

        self.alerts_dir.mkdir(parents=True, exist_ok=True)
        self.deliveries_dir.mkdir(parents=True, exist_ok=True)

    def _atomic_write_json(self, target_path: Path, data: Any) -> None:
        """Escribe datos serializados en JSON usando un archivo temporal y reemplazo atómico."""
        temp_path = target_path.with_suffix(".tmp")
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(_encode_json_value(data), f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, target_path)
        except Exception as e:
            if temp_path.exists():
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            raise JsonAlertRepositoryError(f"Failed atomic write to {target_path}: {e}") from e

    def save(self, alert: AlertRecord) -> AlertRecord:
        if not isinstance(alert, AlertRecord):
            raise ValueError("alert must be an instance of AlertRecord")

        # Verificar si ya existe por idempotency_key
        existing_by_key = self.get_by_idempotency_key(alert.idempotency_key)
        if existing_by_key:
            return existing_by_key

        file_path = self.alerts_dir / f"{alert.alert_id}.json"
        if file_path.exists():
            return self.get_by_id(alert.alert_id) or alert

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

        self._atomic_write_json(file_path, payload)
        return alert

    def get_by_id(self, alert_id: str) -> Optional[AlertRecord]:
        if not alert_id:
            return None
        file_path = self.alerts_dir / f"{alert_id}.json"
        if not file_path.exists():
            return None
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return _decode_alert_record(data)
        except Exception as e:
            raise CorruptedAlertDataError(f"Error reading alert file {file_path}: {e}") from e

    def get_by_idempotency_key(self, idempotency_key: str) -> Optional[AlertRecord]:
        if not idempotency_key:
            return None
        for alert_file in self.alerts_dir.glob("*.json"):
            try:
                with open(alert_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("idempotency_key") == idempotency_key:
                    return _decode_alert_record(data)
            except Exception:
                continue
        return None

    def list_alerts(
        self,
        alert_type: Optional[AlertType] = None,
        severity: Optional[AlertSeverity] = None,
        subject_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[AlertRecord]:
        records: List[AlertRecord] = []
        for alert_file in self.alerts_dir.glob("*.json"):
            try:
                with open(alert_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                rec = _decode_alert_record(data)

                if alert_type and rec.alert_type != alert_type:
                    continue
                if severity and rec.severity != severity:
                    continue
                if subject_id and rec.subject_id != subject_id:
                    continue
                if correlation_id and rec.correlation_id != correlation_id:
                    continue

                records.append(rec)
            except Exception:
                continue

        records.sort(key=lambda r: (r.occurred_at, r.created_at))
        return records[:limit]

    def record_delivery_result(self, result: AlertDeliveryResult) -> AlertDeliveryResult:
        if not isinstance(result, AlertDeliveryResult):
            raise ValueError("result must be an instance of AlertDeliveryResult")

        alert_dir = self.deliveries_dir / result.alert_id
        alert_dir.mkdir(parents=True, exist_ok=True)
        file_path = alert_dir / f"{result.delivery_id}.json"

        payload = {
            "delivery_id": result.delivery_id,
            "alert_id": result.alert_id,
            "channel": result.channel,
            "status": result.status.value,
            "attempted_at": result.attempted_at.isoformat(),
            "correlation_id": result.correlation_id,
            "recipient": result.recipient,
            "provider_reference": result.provider_reference,
            "error_category": result.error_category,
            "error_message": result.error_message,
            "execution_duration_ms": result.execution_duration_ms,
            "metadata": dict(result.metadata),
        }

        self._atomic_write_json(file_path, payload)
        return result

    def list_delivery_results_by_alert(self, alert_id: str) -> List[AlertDeliveryResult]:
        if not alert_id:
            return []
        alert_dir = self.deliveries_dir / alert_id
        if not alert_dir.exists():
            return []

        results: List[AlertDeliveryResult] = []
        for deliv_file in alert_dir.glob("*.json"):
            try:
                with open(deliv_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                results.append(_decode_delivery_result(data))
            except Exception:
                continue

        results.sort(key=lambda d: d.attempted_at)
        return results
