"""Validador de configuración de runtime y pre-startup para despliegue (O.13).

Comprueba de forma determinista:
1. Variables de entorno requeridas y valores por defecto controlados.
2. Validación de tipos, puertos (1-65535), hosts válidos y formatos.
3. Rutas de almacenamiento persistente (`DATA_DIR`) seguras, válidas y fuera de la capa inmutable de la imagen.
4. Rechazo estricto de secretos en texto plano en variables inseguras o patrones prohibidos.
5. Detección y rechazo de configuraciones tenant hardcodeadas en variables de plataforma.
6. Fail-fast en arranque si falta configuración crítica.
"""

from enum import Enum
import ipaddress
import os
from pathlib import Path
import re
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple

from src.domain.deployment.models import (
    DeploymentEnvironment,
    DeploymentConfig,
    DeploymentConfigError,
    SecretLeakError,
    HardcodedTenantConfigError,
    StoragePathSecurityError,
)
from src.infrastructure.deployment.environment_policy import (
    EnvironmentResolutionError,
    resolve_application_environment,
    validate_environment_policy,
)

# Patrones de variables prohibidas que sugieren secretos en texto plano o bypass no autorizado
FORBIDDEN_SECRET_PATTERNS = [
    re.compile(r"^PLAIN_TEXT_.*", re.IGNORECASE),
    re.compile(r"^INSECURE_.*", re.IGNORECASE),
    re.compile(r".*_RAW_KEY$", re.IGNORECASE),
    re.compile(r".*_RAW_SECRET$", re.IGNORECASE),
    re.compile(r"^HARDCODED_(?!TENANT_).*", re.IGNORECASE),
]

# Patrones de variables que indican configuración de tenant horneada en variables globales
FORBIDDEN_TENANT_PATTERNS = [
    re.compile(r"^TENANT_[A-Za-z0-9_]+_CONFIG$", re.IGNORECASE),
    re.compile(r"^BAKED_TENANT_.*", re.IGNORECASE),
    re.compile(r"^DEFAULT_TENANT_ID$", re.IGNORECASE),
    re.compile(r"^HARDCODED_TENANT_.*", re.IGNORECASE),
    re.compile(r"^EMBEDDED_TENANTS$", re.IGNORECASE),
]

# Rutas inseguras del sistema de archivos donde NO debe apuntar DATA_DIR
INSECURE_SYSTEM_PATHS = {
    "/", "/root", "/bin", "/sbin", "/usr", "/usr/bin", "/usr/sbin", "/etc", "/lib", "/lib64",
    "/sys", "/proc", "/dev", "/boot",
    "C:\\", "C:\\Windows", "C:\\Windows\\System32", "C:\\Program Files", "C:\\Program Files (x86)",
}


class DeploymentConfigValidator:
    """Validador estricto de configuración previa al arranque de la aplicación."""

    def __init__(self, env: Optional[Mapping[str, str]] = None) -> None:
        self._env = dict(env if env is not None else os.environ)

    def validate(self) -> DeploymentConfig:
        """Ejecuta todas las reglas de validación y retorna el objeto DeploymentConfig inmutable."""
        self._check_forbidden_secret_vars()
        self._check_forbidden_tenant_vars()

        # 1. ENVIRONMENT — fuente canónica única (P.2):
        #    APP_ENV es el single source of truth; ENVIRONMENT se conserva
        #    solo como compatibilidad legacy. Si ambos divergen → fail-fast.
        try:
            environment = resolve_application_environment(self._env)
        except EnvironmentResolutionError:
            raise
        except DeploymentConfigError as exc:
            valid_envs = [e.value for e in DeploymentEnvironment]
            raise DeploymentConfigError(
                f"Invalid ENVIRONMENT. Must be one of: {valid_envs} ({exc})"
            ) from exc

        # 2. HOST
        host = self._env.get("HOST", "0.0.0.0").strip()
        if not host:
            raise DeploymentConfigError("HOST environment variable cannot be empty.")
        self._validate_host(host)

        # 3. PORT
        port_raw = self._env.get("PORT", "8000").strip()
        try:
            port = int(port_raw)
            if not (1 <= port <= 65535):
                raise ValueError()
        except ValueError:
            raise DeploymentConfigError(
                f"Invalid PORT '{port_raw}'. Must be an integer between 1 and 65535."
            )

        # 4. DATA_DIR
        data_dir_raw = self._env.get("DATA_DIR", "data").strip()
        if not data_dir_raw:
            raise DeploymentConfigError("DATA_DIR environment variable cannot be empty.")
        data_dir = self._validate_data_dir(data_dir_raw)

        # 5. LOG_LEVEL
        log_level = self._env.get("LOG_LEVEL", "INFO").strip().upper()
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise DeploymentConfigError(
                f"Invalid LOG_LEVEL '{log_level}'. Must be one of DEBUG, INFO, WARNING, ERROR, CRITICAL."
            )

        # 6. Flags booleanos
        enable_admin_console = self._parse_bool(self._env.get("ENABLE_ADMIN_CONSOLE", "true"))
        enable_oauth = self._parse_bool(self._env.get("ENABLE_OAUTH", "true"))
        readiness_probes_enabled = self._parse_bool(self._env.get("READINESS_PROBES_ENABLED", "true"))
        liveness_probes_enabled = self._parse_bool(self._env.get("LIVENESS_PROBES_ENABLED", "true"))
        app_version = self._env.get("APP_VERSION", "0.1.0").strip()

        # 7. Allowed Hosts
        allowed_hosts_raw = self._env.get("ALLOWED_HOSTS", "*").strip()
        allowed_hosts = tuple(h.strip() for h in allowed_hosts_raw.split(",") if h.strip())
        if not allowed_hosts:
            allowed_hosts = ("*",)

        # 8. Verificación de secretos específicos en producción
        self._check_production_secrets(environment)

        # 9. Política de separación de entornos (P.2):
        #    - production safety (debug, log level, mock providers, hosts)
        #    - aislamiento de data roots (cross-env guard)
        #    - aislamiento de secret namespaces (cross-env guard)
        validate_environment_policy(environment, self._env, data_dir=str(data_dir))

        return DeploymentConfig(
            environment=environment,
            host=host,
            port=port,
            data_dir=data_dir,
            log_level=log_level,
            enable_admin_console=enable_admin_console,
            enable_oauth=enable_oauth,
            app_version=app_version,
            allowed_hosts=allowed_hosts,
            readiness_probes_enabled=readiness_probes_enabled,
            liveness_probes_enabled=liveness_probes_enabled,
        )

    def _check_forbidden_secret_vars(self) -> None:
        """Rechaza variables de entorno que exponen secretos o credenciales en texto plano."""
        for key, val in self._env.items():
            for pattern in FORBIDDEN_SECRET_PATTERNS:
                if pattern.match(key):
                    raise SecretLeakError(
                        f"Insecure secret environment variable detected: '{key}'. Plaintext secrets are strictly forbidden."
                    )
            # Detectar valores que parecen secretos directos en variables de configuración genéricas
            if key in {"ADMIN_PASSWORD", "SECRET_KEY_RAW", "DATABASE_PASSWORD_PLAIN"}:
                raise SecretLeakError(
                    f"Direct plaintext secret in environment variable '{key}' is forbidden."
                )

    def _check_forbidden_tenant_vars(self) -> None:
        """Rechaza configuraciones tenant embebidas o predefinidas en variables de nivel plataforma."""
        for key in self._env.keys():
            for pattern in FORBIDDEN_TENANT_PATTERNS:
                if pattern.match(key):
                    raise HardcodedTenantConfigError(
                        f"Baked tenant configuration detected in environment variable: '{key}'. "
                        "Tenant configuration must be dynamic and tenant-isolated via O.11/O.1 repos."
                    )

    def _validate_host(self, host: str) -> None:
        """Verifica que el host sea una IP válida o un nombre de host DNS/localhost válido."""
        if host in {"0.0.0.0", "127.0.0.1", "::1", "localhost", "::"}:
            return
        try:
            ipaddress.ip_address(host)
            return
        except ValueError:
            pass
        # Comprobar regex de hostname
        hostname_regex = re.compile(
            r"^([a-z0-9]([a-z0-9\-]{0,61}[a-z0-9])?\.)+[a-z]{2,6}$|^[a-z0-9\-]+$",
            re.IGNORECASE
        )
        if not hostname_regex.match(host):
            raise DeploymentConfigError(f"Invalid HOST name or IP address: '{host}'")

    def _validate_data_dir(self, data_dir_str: str) -> Path:
        """Valida que la ruta de almacenamiento persistente sea segura."""
        path = Path(data_dir_str)

        # Evitar path traversal relativo peligroso
        if ".." in path.parts:
            raise StoragePathSecurityError(f"Path traversal detected in DATA_DIR: '{data_dir_str}'")

        resolved = str(path.resolve())
        normalized_str = str(path).replace("\\", "/")

        for insecure in INSECURE_SYSTEM_PATHS:
            if resolved == str(Path(insecure).resolve()) or normalized_str == insecure:
                raise StoragePathSecurityError(
                    f"DATA_DIR points to critical system directory '{insecure}', which is forbidden."
                )

        return path

    def _check_production_secrets(self, environment: DeploymentEnvironment) -> None:
        """En entorno production, verifica que no existan bypasses de pruebas."""
        if environment == DeploymentEnvironment.PRODUCTION:
            if self._env.get("ALLOW_ANONYMOUS_ADMIN", "").lower() in {"1", "true", "yes"}:
                raise DeploymentConfigError(
                    "ALLOW_ANONYMOUS_ADMIN cannot be enabled in PRODUCTION environment."
                )

    def _parse_bool(self, value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if not isinstance(value, str):
            return bool(value)
        v = value.strip().lower()
        if v in {"true", "1", "yes", "on", "t"}:
            return True
        if v in {"false", "0", "no", "off", "f", ""}:
            return False
        raise DeploymentConfigError(f"Invalid boolean value: '{value}'")
