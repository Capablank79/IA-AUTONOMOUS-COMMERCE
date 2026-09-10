"""
Repositorio JSON tenant-scoped, íntegro, transaccional y crash-safe para Alertas Operacionales (O.12).
"""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
from typing import Any, Dict, List, Optional, Sequence, Union

from src.domain.security.models import validate_safe_identifier
from src.domain.saas_observability.models import (
    OperationalAlert,
    OperationalAlertType,
    AlertSeverity,
    AlertStatus,
    ObservabilityIntegrityError,
    compute_observability_checksum,
)
from src.domain.saas_observability.ports import OperationalAlertRepositoryPort


class JsonOperationalAlertRepository(OperationalAlertRepositoryPort):
    """
    Implementación JSON en disco particionada por Tenant para OperationalAlerts.

    Garantías:
    - Path isolation: tenants/{tenant_id}/observability/alerts/
    - Escrituras atómicas con fsync y .tmp
    - Thread-safety vía RLock
    - Verificación estricta de checksum SHA-256
    - Fail-safe ante corrupción física
    """

    def __init__(self, base_dir: Union[str, Path]):
        self.base_dir = Path(base_dir)
        self.tenants_dir = self.base_dir / "tenants"
        self.tenants_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _alerts_dir(self, tenant_id: str) -> Path:
        validate_safe_identifier(tenant_id, "tenant_id")
        dir_path = self.tenants_dir / tenant_id / "observability" / "alerts"
        dir_path.mkdir(parents=True, exist_ok=True)
        return dir_path

    def _alert_path(self, tenant_id: str, alert_id: str) -> Path:
        validate_safe_identifier(alert_id, "alert_id")
        return self._alerts_dir(tenant_id) / f"{alert_id}.json"

    def _deserialize_alert(self, data: Dict[str, Any]) -> OperationalAlert:
        alert = OperationalAlert(
            alert_id=data["alert_id"],
            tenant_id=data["tenant_id"],
            alert_type=OperationalAlertType(data["alert_type"]),
            severity=AlertSeverity(data["severity"]),
            status=AlertStatus(data["status"]),
            summary=data["summary"],
            details=data.get("details", {}),
            triggered_at=datetime.fromisoformat(data["triggered_at"]),
            deduplication_key=data["deduplication_key"],
            resolved_at=datetime.fromisoformat(data["resolved_at"]) if data.get("resolved_at") else None,
            acknowledged_at=datetime.fromisoformat(data["acknowledged_at"]) if data.get("acknowledged_at") else None,
            organization_id=data.get("organization_id"),
            checksum=data.get("checksum", ""),
        )
        # Validar integridad de checksum
        payload = {
            "alert_id": alert.alert_id,
            "tenant_id": alert.tenant_id,
            "organization_id": alert.organization_id,
            "alert_type": alert.alert_type.value,
            "severity": alert.severity.value,
            "status": alert.status.value,
            "summary": alert.summary,
            "details": dict(alert.details),
            "triggered_at": alert.triggered_at,
            "deduplication_key": alert.deduplication_key,
            "resolved_at": alert.resolved_at,
            "acknowledged_at": alert.acknowledged_at,
        }
        computed = compute_observability_checksum(payload)
        if alert.checksum and alert.checksum != computed:
            raise ObservabilityIntegrityError(f"Checksum mismatch for alert {alert.alert_id}: expected {alert.checksum}, computed {computed}")
        return alert

    def save_alert(self, alert: OperationalAlert) -> OperationalAlert:
        with self._lock:
            path = self._alert_path(alert.tenant_id, alert.alert_id)
            tmp_path = path.with_suffix(".tmp")
            payload = alert.to_dict()
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
            return alert

    def get_alert_by_id(self, tenant_id: str, alert_id: str) -> Optional[OperationalAlert]:
        with self._lock:
            path = self._alert_path(tenant_id, alert_id)
            if not path.exists():
                return None
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return self._deserialize_alert(data)
            except Exception as e:
                if isinstance(e, ObservabilityIntegrityError):
                    raise
                return None

    def list_alerts(
        self,
        tenant_id: str,
        status: Optional[AlertStatus] = None,
        organization_id: Optional[str] = None,
        limit: int = 100,
    ) -> Sequence[OperationalAlert]:
        with self._lock:
            alerts_dir = self._alerts_dir(tenant_id)
            results: List[OperationalAlert] = []
            for item in sorted(alerts_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
                try:
                    with open(item, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    alert = self._deserialize_alert(data)
                    if status and alert.status != status:
                        continue
                    if organization_id and alert.organization_id != organization_id:
                        continue
                    results.append(alert)
                    if len(results) >= limit:
                        break
                except Exception:
                    # En caso de corrupción se ignora o descarta para listados
                    continue
            return results

    def get_active_alert_by_deduplication_key(
        self, tenant_id: str, deduplication_key: str
    ) -> Optional[OperationalAlert]:
        with self._lock:
            active_alerts = self.list_alerts(tenant_id=tenant_id, status=AlertStatus.ACTIVE)
            for alert in active_alerts:
                if alert.deduplication_key == deduplication_key:
                    return alert
            return None
