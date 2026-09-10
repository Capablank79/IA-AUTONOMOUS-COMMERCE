"""Modelos de dominio y contratos para automatización de despliegue (O.13).

Principios:
1. Determinismo, inmutabilidad y seguridad de configuración en runtime.
2. Rechazo estricto de secretos en texto plano en variables de entorno inseguras.
3. Rechazo de configuración de tenant hardcodeada en variables de nivel plataforma o imagen.
4. Preservación de UNKNOWN != ZERO y fail-fast en arranque ante configuración crítica faltante o inválida.
5. Verificación de almacenamiento persistente fuera de la capa inmutable de la imagen.
"""

from dataclasses import dataclass, field
from enum import Enum
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple


class DeploymentEnvironment(str, Enum):
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"
    TESTING = "testing"


class DeploymentConfigError(ValueError):
    """Error al validar la configuración de despliegue."""
    pass


class SecretLeakError(DeploymentConfigError):
    """Se detectó un secreto o credencial en texto plano en una variable insegura o no autorizada."""
    pass


class HardcodedTenantConfigError(DeploymentConfigError):
    """Se detectó configuración específica de tenant horneada en variables globales de plataforma."""
    pass


class StoragePathSecurityError(DeploymentConfigError):
    """La ruta de almacenamiento persistente no es segura o invade capas inmutables del sistema."""
    pass


@dataclass(frozen=True)
class DeploymentConfig:
    """Configuración validada e inmutable de despliegue."""
    environment: DeploymentEnvironment
    host: str
    port: int
    data_dir: Path
    log_level: str = "INFO"
    enable_admin_console: bool = True
    enable_oauth: bool = True
    app_version: str = "0.1.0"
    allowed_hosts: Tuple[str, ...] = ("*",)
    readiness_probes_enabled: bool = True
    liveness_probes_enabled: bool = True

    def __post_init__(self) -> None:
        if not (1 <= self.port <= 65535):
            raise DeploymentConfigError(f"Port must be between 1 and 65535, got {self.port}")
        if not self.host or not isinstance(self.host, str):
            raise DeploymentConfigError("Host must be a non-empty string")
        if not isinstance(self.data_dir, Path):
            object.__setattr__(self, "data_dir", Path(self.data_dir))
        if not isinstance(self.environment, DeploymentEnvironment):
            object.__setattr__(self, "environment", DeploymentEnvironment(self.environment))

    def to_dict(self) -> Dict[str, Any]:
        """Proyección segura sin secretos."""
        return {
            "environment": self.environment.value,
            "host": self.host,
            "port": self.port,
            "data_dir": str(self.data_dir),
            "log_level": self.log_level,
            "enable_admin_console": self.enable_admin_console,
            "enable_oauth": self.enable_oauth,
            "app_version": self.app_version,
            "allowed_hosts": list(self.allowed_hosts),
            "readiness_probes_enabled": self.readiness_probes_enabled,
            "liveness_probes_enabled": self.liveness_probes_enabled,
        }


@dataclass(frozen=True)
class HealthStatus:
    """Estado de salud y readiness del sistema en despliegue."""
    status: str  # 'ok', 'healthy', 'unhealthy', 'degraded'
    environment: str
    version: str
    storage_writable: bool
    details: Dict[str, Any] = field(default_factory=dict)
