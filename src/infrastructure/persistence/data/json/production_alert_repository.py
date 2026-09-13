"""
Adaptador de repositorio JSON y memoria para Alertas de Producción (Hito P.8).
Garantiza persistencia atómica, verificación de checksum SHA-256 y aislamiento por entorno y tenant.
"""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
from typing import Any, Dict, List, Optional, Sequence, Union

from src.domain.deployment.models import ApplicationEnvironment, normalize_environment_name
from src.domain.production_alerting.models import (
    AlertRuleType,
    AlertSeverity,
    AlertState,
    ProductionAlertInstance,
    ProductionAlertIntegrityError,
    ProductionAlertScope,
    compute_alert_checksum,
)
from src.domain.production_alerting.ports import ProductionAlertRepositoryPort
from src.domain.security.models import validate_safe_identifier


class JsonProductionAlertRepository(ProductionAlertRepositoryPort):
    """
    Repositorio particionado por entorno en JSON o memoria con bloqueo reentrante y checksums.
    """

    def __init__(self, base_dir: Optional[Union[str, Path]] = None, alerts_base_dir: Optional[Union[str, Path]] = None) -> None:
        target_dir = base_dir or alerts_base_dir
        self.base_dir = Path(target_dir) if target_dir else None
        if self.base_dir:
            self.alerts_base = self.base_dir / "production_alerts"
            self.alerts_base.mkdir(parents=True, exist_ok=True)
        else:
            self.alerts_base = None

        self._in_memory_alerts: Dict[str, Dict[str, ProductionAlertInstance]] = {
            env.value: {} for env in ApplicationEnvironment
        }
        self._lock = threading.RLock()

    def _env_dir(self, environment: ApplicationEnvironment) -> Path:
        if not self.alerts_base:
            raise ValueError("Persistent base_dir not configured")
        env_val = environment.value if isinstance(environment, ApplicationEnvironment) else str(environment)
        d = self.alerts_base / env_val
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _deserialize_alert(self, data: Dict[str, Any]) -> ProductionAlertInstance:
        alert = ProductionAlertInstance(
            alert_id=data["alert_id"],
            rule_type=AlertRuleType(data["rule_type"]),
            severity=AlertSeverity(data["severity"]),
            state=AlertState(data["state"]),
            environment=ApplicationEnvironment(data["environment"]),
            scope=ProductionAlertScope(data["scope"]),
            deduplication_key=data["deduplication_key"],
            summary=data["summary"],
            evidence=data.get("evidence", {}),
            triggered_at=datetime.fromisoformat(data["triggered_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
            target_resource=data.get("target_resource", "platform"),
            tenant_id=data.get("tenant_id"),
            acknowledged_at=datetime.fromisoformat(data["acknowledged_at"]) if data.get("acknowledged_at") else None,
            acknowledged_by=data.get("acknowledged_by"),
            resolved_at=datetime.fromisoformat(data["resolved_at"]) if data.get("resolved_at") else None,
            resolved_by=data.get("resolved_by"),
            resolution_reason=data.get("resolution_reason"),
            checksum=data.get("checksum", ""),
        )
        
        # Validar checksum
        payload = {
            "alert_id": alert.alert_id,
            "rule_type": alert.rule_type.value,
            "severity": alert.severity.value,
            "state": alert.state.value,
            "environment": alert.environment.value,
            "scope": alert.scope.value,
            "deduplication_key": alert.deduplication_key,
            "summary": alert.summary,
            "evidence": dict(alert.evidence),
            "triggered_at": alert.triggered_at,
            "updated_at": alert.updated_at,
            "target_resource": alert.target_resource,
            "tenant_id": alert.tenant_id,
            "acknowledged_at": alert.acknowledged_at,
            "acknowledged_by": alert.acknowledged_by,
            "resolved_at": alert.resolved_at,
            "resolved_by": alert.resolved_by,
            "resolution_reason": alert.resolution_reason,
        }
        computed = compute_alert_checksum(payload)
        if alert.checksum and alert.checksum != computed:
            raise ProductionAlertIntegrityError(
                f"Checksum mismatch for alert {alert.alert_id}: expected {alert.checksum}, computed {computed}"
            )
        return alert

    def save_alert(self, alert: ProductionAlertInstance) -> ProductionAlertInstance:
        with self._lock:
            env_key = alert.environment.value
            if env_key not in self._in_memory_alerts:
                self._in_memory_alerts[env_key] = {}
            self._in_memory_alerts[env_key][alert.alert_id] = alert

            if self.alerts_base:
                env_path = self._env_dir(alert.environment)
                file_path = env_path / f"{alert.alert_id}.json"
                tmp_path = file_path.with_suffix(".tmp")
                payload = alert.to_dict()
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(payload, f, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, file_path)

            return alert

    def get_alert_by_id(
        self,
        environment: ApplicationEnvironment,
        alert_id: str,
        tenant_id: Optional[str] = None,
    ) -> Optional[ProductionAlertInstance]:
        validate_safe_identifier(alert_id, "alert_id")
        with self._lock:
            env_key = environment.value
            alert = self._in_memory_alerts.get(env_key, {}).get(alert_id)
            
            if not alert and self.alerts_base:
                file_path = self._env_dir(environment) / f"{alert_id}.json"
                if file_path.exists():
                    try:
                        with open(file_path, "r", encoding="utf-8") as f:
                            data = json.load(f)
                        alert = self._deserialize_alert(data)
                        self._in_memory_alerts[env_key][alert.alert_id] = alert
                    except Exception:
                        return None

            if alert:
                if tenant_id is not None and alert.tenant_id != tenant_id:
                    return None
                return alert
            return None

    def get_active_alert_by_deduplication_key(
        self,
        environment: ApplicationEnvironment,
        deduplication_key: str,
    ) -> Optional[ProductionAlertInstance]:
        with self._lock:
            env_key = environment.value
            for alert in self._in_memory_alerts.get(env_key, {}).values():
                if alert.deduplication_key == deduplication_key and alert.state in (AlertState.ACTIVE, AlertState.ACKNOWLEDGED):
                    return alert
            return None

    def list_alerts(
        self,
        environment: ApplicationEnvironment,
        state: Optional[AlertState] = None,
        severity: Optional[AlertSeverity] = None,
        rule_type: Optional[AlertRuleType] = None,
        scope: Optional[ProductionAlertScope] = None,
        tenant_id: Optional[str] = None,
        limit: int = 100,
    ) -> Sequence[ProductionAlertInstance]:
        with self._lock:
            env_key = environment.value
            candidates = list(self._in_memory_alerts.get(env_key, {}).values())

            results: List[ProductionAlertInstance] = []
            for alert in candidates:
                if state is not None and alert.state != state:
                    continue
                if severity is not None and alert.severity != severity:
                    continue
                if rule_type is not None and alert.rule_type != rule_type:
                    continue
                if scope is not None and alert.scope != scope:
                    continue
                if tenant_id is not None and alert.tenant_id != tenant_id:
                    continue
                results.append(alert)

            results.sort(key=lambda a: a.triggered_at, reverse=True)
            return tuple(results[:limit])

    def clear(self, environment: Optional[ApplicationEnvironment] = None) -> None:
        with self._lock:
            if environment:
                self._in_memory_alerts[environment.value] = {}
            else:
                for env in ApplicationEnvironment:
                    self._in_memory_alerts[env.value] = {}
