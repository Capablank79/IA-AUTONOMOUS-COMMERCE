"""Modelos de dominio para Health Checks, Liveness y Readiness (P.6 — Health Checks).

Principios:
1. Responde a: "¿Puede un orquestador u operador determinar de forma segura si el proceso está vivo,
   si la aplicación puede atender tráfico y qué dependencia crítica impide readiness?".
2. Separación estricta entre LIVENESS, READINESS y DEGRADED/DEPENDENCY STATUS.
3. Clasificación clara de dependencias: CRITICAL, OPTIONAL, EXTERNAL_NON_BLOCKING.
4. UNKNOWN != HEALTHY: La falta de evidencia o incertidumbre en dependencia crítica genera UNKNOWN/UNHEALTHY,
   nunca éxito artificial.
5. Inmutabilidad y sanitización estricta: Cero credenciales, passwords, hostnames internos o paths vulnerables en los contratos.
6. Desacoplamiento de O.12: P.6 representa el estado técnico operacional inmediato, no la agregación histórica SaaS.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from types import MappingProxyType


class HealthStatus(str, Enum):
    """Estados canónicos de salud operacional técnica."""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


class DependencyClassification(str, Enum):
    """Clasificación de dependencias según su impacto en el servicio."""
    CRITICAL = "critical"
    OPTIONAL = "optional"
    EXTERNAL_NON_BLOCKING = "external_non_blocking"


class ProbeType(str, Enum):
    """Tipo de probe operacional."""
    LIVENESS = "liveness"
    READINESS = "readiness"
    STARTUP = "startup"


@dataclass(frozen=True)
class DependencyCheckResult:
    """Resultado inmutable de la verificación individual de una dependencia."""
    name: str
    classification: DependencyClassification
    status: HealthStatus
    message: Optional[str] = None
    latency_ms: Optional[float] = None
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.details, MappingProxyType):
            object.__setattr__(self, "details", MappingProxyType(dict(self.details)))

    def to_dict(self) -> Dict[str, Any]:
        """Serialización segura y sanitizada."""
        res: Dict[str, Any] = {
            "name": self.name,
            "classification": self.classification.value,
            "status": self.status.value,
        }
        if self.message:
            res["message"] = self.message
        if self.latency_ms is not None:
            res["latency_ms"] = round(self.latency_ms, 3)
        if self.details:
            res["details"] = dict(self.details)
        return res


@dataclass(frozen=True)
class LivenessResult:
    """Resultado inmutable de la comprobación de Liveness."""
    status: HealthStatus
    service: str
    version: str
    environment: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_alive(self) -> bool:
        return self.status == HealthStatus.HEALTHY

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": "ok" if self.status == HealthStatus.HEALTHY else self.status.value,
            "service": self.service,
            "version": self.version,
            "environment": self.environment,
            "timestamp": self.timestamp.isoformat(),
        }


@dataclass(frozen=True)
class ReadinessResult:
    """Resultado inmutable de la comprobación de Readiness."""
    status: HealthStatus
    service: str
    version: str
    environment: str
    checks: Tuple[DependencyCheckResult, ...] = field(default_factory=tuple)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_ready(self) -> bool:
        """La app está lista si el status general es HEALTHY o DEGRADED (si ninguna crítica falló)."""
        return self.status in {HealthStatus.HEALTHY, HealthStatus.DEGRADED}

    @property
    def http_status_code(self) -> int:
        """200 si ready (incluso si degraded en dependencias opcionales), 503 si no ready (crítica falla/unknown)."""
        return 200 if self.is_ready else 503

    def to_dict(self) -> Dict[str, Any]:
        """Contrato de respuesta estructurado, versionable y seguro."""
        checks_list = [c.to_dict() for c in self.checks]

        # Compatibilidad hacia atrás con campos directos esperados por O.13/P.2/P.3
        storage_check = next((c for c in self.checks if c.name == "storage"), None)
        db_check = next((c for c in self.checks if c.name == "database"), None)

        data: Dict[str, Any] = {
            "status": "ready" if self.status == HealthStatus.HEALTHY else ("degraded" if self.status == HealthStatus.DEGRADED else "unhealthy"),
            "service": self.service,
            "version": self.version,
            "environment": self.environment,
            "timestamp": self.timestamp.isoformat(),
            "checks": checks_list,
        }

        if storage_check and storage_check.status != HealthStatus.HEALTHY:
            data["storage_writable"] = False
            data["reason"] = storage_check.message or "storage_not_writable"
            data["data_dir"] = storage_check.details.get("data_dir")
        elif storage_check:
            data["storage_writable"] = True

        if db_check and db_check.status == HealthStatus.HEALTHY and db_check.details:
            data["database"] = {
                "status": "compatible",
                "current_revision": db_check.details.get("current_revision"),
                "head_revision": db_check.details.get("head_revision"),
            }
        elif db_check and db_check.status != HealthStatus.HEALTHY:
            if "reason" not in data:
                data["reason"] = db_check.message or "database_dependency_failure"

        return data
