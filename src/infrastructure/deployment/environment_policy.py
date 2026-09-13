"""Política de separación de entornos (P.2 — Environment Separation).

Define perfiles canónicos de ejecución (development / staging / production),
reglas de aislamiento de datos, secretos y proveedores, y guards de
cross-environment contamination sin duplicar la lógica de O.13.

REUSE:
- DeploymentEnvironment / ApplicationEnvironment (models.py O.13)
- DeploymentConfigValidator (config_validator.py O.13)

Este módulo solo contiene reglas de validación de seguridad por environment,
invocables desde el validator, el entrypoint, deploy_validate y los tests.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Mapping, Optional

from src.domain.deployment.models import (
    ApplicationEnvironment,
    DeploymentConfigError,
    DeploymentEnvironment,
    normalize_environment_name,
)


# ---------------------------------------------------------------------------
# Environment Cross-Acceso — Errores específicos
# ---------------------------------------------------------------------------

class CrossEnvironmentAccessError(DeploymentConfigError):
    """Un recurso fue dirigido al root o namespace de un environment distinto."""


class EnvironmentSafetyError(DeploymentConfigError):
    """Una configuración de runtime viola la política de seguridad del environment."""


class EnvironmentResolutionError(DeploymentConfigError):
    """APP_ENV y ENVIRONMENT están presentes con valores divergentes."""


# ---------------------------------------------------------------------------
# Profiles canónicos
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EnvironmentProfile:
    """Perfil determinista por environment con configuración segura por defecto."""
    environment: ApplicationEnvironment
    data_root_name: str          # Nombre canónico bajo el base (data/<name>)
    secret_namespace: str        # Namespace de secretos (dev / staging / prod)
    debug_allowed: bool          # Permitir LOG_LEVEL=DEBUG en este env
    mock_providers_allowed: bool # Permitir flags de mock/sandbox
    production_like: bool        # Igual que producción en controles de seguridad
    log_level_default: str       # Nivel de log por defecto
    description: str = ""


ENVIRONMENT_PROFILES: Dict[ApplicationEnvironment, EnvironmentProfile] = {
    ApplicationEnvironment.DEVELOPMENT: EnvironmentProfile(
        environment=ApplicationEnvironment.DEVELOPMENT,
        data_root_name="development",
        secret_namespace="dev",
        debug_allowed=True,
        mock_providers_allowed=True,
        production_like=False,
        log_level_default="DEBUG",
        description="Entorno local de desarrollo con mocks permitidos.",
    ),
    ApplicationEnvironment.STAGING: EnvironmentProfile(
        environment=ApplicationEnvironment.STAGING,
        data_root_name="staging",
        secret_namespace="staging",
        debug_allowed=True,  # Preferentemente off, pero permitido para diagnóstico
        mock_providers_allowed=False,  # Production-like safety
        production_like=True,
        log_level_default="INFO",
        description="Entorno pre-producción con seguridad production-like.",
    ),
    ApplicationEnvironment.PRODUCTION: EnvironmentProfile(
        environment=ApplicationEnvironment.PRODUCTION,
        data_root_name="production",
        secret_namespace="prod",
        debug_allowed=False,
        mock_providers_allowed=False,
        production_like=True,
        log_level_default="WARNING",
        description="Entorno productivo con máximas restricciones.",
    ),
    ApplicationEnvironment.TESTING: EnvironmentProfile(
        environment=ApplicationEnvironment.TESTING,
        data_root_name="testing",
        secret_namespace="test",
        debug_allowed=True,
        mock_providers_allowed=True,
        production_like=False,
        log_level_default="WARNING",
        description="Entorno de pruebas automatizadas (CI / tests unitarios).",
    ),
}


# ---------------------------------------------------------------------------
# Patrones de flags de mock provider (PRODUCCIÓN los rechaza)
# ---------------------------------------------------------------------------

# Prefijos de variable de entorno que indican modo mock
_MOCK_FLAG_PREFIXES = (
    "USE_MOCK_",
    "MOCK_",
    "ENABLE_MOCK_",
)

# Nombres de variable que terminan en sufijo y cuyo valor indica mock
_MOCK_VALUE_SUFFIXES = (
    "_PROVIDER",
    "_MODE",
    "_IMPLEMENTATION",
    "_ADAPTER",
    "_BACKEND",
)

_MOCK_VALUES = {"mock", "fake", "stub", "disabled"}


def _has_mock_provider_flags(env_map: Mapping[str, str]) -> List[str]:
    """Detecta variables que sugieren uso de proveedores mock/fake.

    Returns:
        Lista de nombres de variable que activarían modo mock.
    """
    found: List[str] = []
    for key, val in env_map.items():
        norm_key = key.strip().upper()
        val_norm = str(val).strip().lower()

        # Prefijos directos
        for prefix in _MOCK_FLAG_PREFIXES:
            if norm_key.startswith(prefix) and val_norm in {"1", "true", "yes"}:
                found.append(key)
                break
        else:
            # Sufijos con valor mock
            for suffix in _MOCK_VALUE_SUFFIXES:
                if norm_key.endswith(suffix) and val_norm in _MOCK_VALUES:
                    found.append(key)
                    break

    return sorted(found)


# ---------------------------------------------------------------------------
# Resolución canónica de entorno
# ---------------------------------------------------------------------------

def _normalize_canonical(raw: str) -> ApplicationEnvironment:
    """Normaliza solo con los valores canónicos del enum O.13 (sin alias).

    Usado en el arranque (DeploymentConfigValidator) para preservar la
    semántica estricta de O.13: ENVIRONMENT='prod'/'dev' no son válidos.
    """
    key = str(raw).strip().lower()
    try:
        return ApplicationEnvironment(key)
    except ValueError as e:
        valid = sorted({e.value for e in ApplicationEnvironment})
        raise ValueError(f"Unknown environment '{raw}'. Must be one of: {valid}") from e


def resolve_application_environment(
    env_map: Mapping[str, str],
    *,
    strict_explicit: bool = False,
    use_aliases: bool = False,
) -> ApplicationEnvironment:
    """Resuelve el entorno de aplicación desde variables de entorno.

    Reglas:
    1. Si APP_ENV y ENVIRONMENT ambos definidos y difieren → EnvironmentResolutionError.
    2. Si APP_ENV definido (sin ENVIRONMENT) → APP_ENV es la fuente (explícito).
    3. Si solo ENVIRONMENT → se usa (legacy, compatibilidad con O.13).
    4. Si ninguno → default production (fail-safe, behavior actual de O.13).

    use_aliases=False (default, arranque):
        Solo acepta valores canónicos exactos del enum (development/staging/
        production/testing). 'prod'/'dev' son rechazados (compatibilidad O.13).

    use_aliases=True (tooling / deploy_validate / tests):
        Convierte al formato canónico (dev → development, prod → production, etc.).

    strict_explicit:
        Si True, exige que APP_ENV o ENVIRONMENT estén presentes en env_map
        (no admite default). Se usa para producir error en despliegues reales
        donde el operador debe declarar el environment explícitamente.
    """
    raw_app_env = env_map.get("APP_ENV", "").strip()
    raw_env = env_map.get("ENVIRONMENT", "").strip()

    normalizer = normalize_environment_name if use_aliases else _normalize_canonical

    def _normalize(raw: str) -> ApplicationEnvironment:
        try:
            return normalizer(raw)
        except ValueError as e:
            raise DeploymentConfigError(str(e)) from e

    if raw_app_env and raw_env:
        # Ambos presentes: normalizan y verifican coincidencia
        norm_app = _normalize(raw_app_env)
        norm_env = _normalize(raw_env)
        if norm_app != norm_env:
            raise EnvironmentResolutionError(
                f"APP_ENV='{raw_app_env}' and ENVIRONMENT='{raw_env}' are both defined "
                f"and resolve to different environments ({norm_app.value} vs {norm_env.value}). "
                "Only one source of truth is allowed."
            )
        return norm_app

    # APP_ENV explícito manda sobre legacy ENVIRONMENT
    raw = raw_app_env or raw_env or ""

    if not raw:
        if strict_explicit:
            raise DeploymentConfigError(
                "Neither APP_ENV nor ENVIRONMENT is set. "
                "The environment must be declared explicitly in this context."
            )
        # Default fail-safe: production (comportamiento actual de O.13)
        return ApplicationEnvironment.PRODUCTION

    return _normalize(raw)


# ---------------------------------------------------------------------------
# Data Root helpers
# ---------------------------------------------------------------------------

def default_data_root(environment: ApplicationEnvironment, base: str = "data") -> str:
    """Retorna el data root por defecto para un environment dado: <base>/<env>."""
    profile = ENVIRONMENT_PROFILES[environment]
    return f"{base}/{profile.data_root_name}"


def normalize_path(p: str) -> str:
    """Normaliza un path a forward slashes para comparaciones deterministas."""
    return str(Path(p)).replace("\\", "/")


def resolve_data_root_aliases(environment: ApplicationEnvironment) -> set:
    """Conjunto normalizado de data roots 'privados' de otros environments.

    Si el data_dir configurado termina dentro de uno de estos roots,
    significa un cross-env access no autorizado.
    """
    roots = set()
    for env_e, prof in ENVIRONMENT_PROFILES.items():
        if env_e == environment:
            continue
        # Variantes canónicas del root
        roots.add(prof.data_root_name)
        roots.add(prof.secret_namespace)
    return roots


def cross_env_data_root_violation(
    environment: ApplicationEnvironment,
    data_dir: str,
) -> Optional[str]:
    """Verifica si data_dir apunta al root o namespace de otro environment.

    Returns:
        None si no hay violación, o string descriptiva de la violación.
    """
    norm = normalize_path(data_dir)
    parts = set(Path(norm).parts)
    others = resolve_data_root_aliases(environment)

    for other_root in others:
        # Rechazo si el nombre/namespace de otro environment aparece como
        # componente EXACTO del path (aislamiento estructural, no por naming
        # de substring: evita falsos positivos con nombres tipo 'test_*').
        if other_root in parts:
            return (
                f"DATA_DIR '{data_dir}' contains the environment name or namespace "
                f"'{other_root}' as a path component, which belongs to a different "
                "environment."
            )

    return None


# ---------------------------------------------------------------------------
# Secret Namespace guards
# ---------------------------------------------------------------------------

def validate_secret_namespace(
    environment: ApplicationEnvironment,
    env_map: Mapping[str, str],
) -> None:
    """Verifica que SECRET_NAMESPACE, si está definido, pertenezca al environment actual."""
    secret_ns = env_map.get("SECRET_NAMESPACE", "").strip()
    if not secret_ns:
        return  # Opcional; solo validamos si fue declarado explícitamente

    expected_ns = ENVIRONMENT_PROFILES[environment].secret_namespace
    if secret_ns.lower() != expected_ns.lower():
        raise CrossEnvironmentAccessError(
            f"SECRET_NAMESPACE='{secret_ns}' does not match environment "
            f"'{environment.value}' (expected '{expected_ns}'). "
            "Cannot use another environment's secret namespace."
        )


# ---------------------------------------------------------------------------
# Production safety checks
# ---------------------------------------------------------------------------

def validate_production_safety(
    environment: ApplicationEnvironment,
    env_map: Mapping[str, str],
) -> None:
    """Aplica restricciones de seguridad de PRODUCCIÓN.

    Raises EnvironmentSafetyError si:
    - DEBUG está habilitado cuando el perfil no lo permite (producción)
    - LOG_LEVEL es DEBUG cuando el perfil no lo permite (producción)
    - Flags de mock provider detectados en entornos production-like
    - Host loopback (127.0.0.1, localhost) en PRODUCTION (bind inalcanzable)

    Nota: STAGING conserva compatibilidad O.13 con configs custom
    (HOST=127.0.0.1 en pruebas unitarias existentes); la restricción de
    bind loopback aplica solo a PRODUCTION.
    """
    profile = ENVIRONMENT_PROFILES[environment]

    # --- DEBUG flag ---
    debug_val = env_map.get("DEBUG", "").strip().lower()
    if debug_val in {"1", "true", "yes"}:
        if not profile.debug_allowed:
            raise EnvironmentSafetyError(
                "DEBUG mode is not allowed in PRODUCTION. "
                "Set DEBUG=false or remove it for production deployments."
            )

    # --- LOG_LEVEL=DEBUG ---
    log_level = env_map.get("LOG_LEVEL", "").strip().upper()
    if log_level == "DEBUG" and not profile.debug_allowed:
        raise EnvironmentSafetyError(
            "LOG_LEVEL=DEBUG is not permitted in PRODUCTION. "
            "Use INFO, WARNING or ERROR."
        )

    # --- Mock provider flags ---
    if not profile.mock_providers_allowed:
        mock_flags = _has_mock_provider_flags(env_map)
        if mock_flags:
            raise EnvironmentSafetyError(
                f"Mock provider configuration detected in environment "
                f"'{environment.value}' (flags: {', '.join(mock_flags)}). "
                "Mock/fake providers are not permitted in production-like environments."
            )

    # --- Host loopback: solo PRODUCTION (bind inalcanzable = misconfig) ---
    if environment == ApplicationEnvironment.PRODUCTION:
        host = env_map.get("HOST", "").strip().lower()
        if host in {"127.0.0.1", "localhost", "::1", "::"}:
            raise EnvironmentSafetyError(
                f"HOST='{host}' is a loopback/local address and is not permitted "
                "in PRODUCTION. Use a routable address or 0.0.0.0 for all interfaces."
            )


# ---------------------------------------------------------------------------
# Validación integrada de safety por environment
# ---------------------------------------------------------------------------

def validate_environment_policy(
    environment: ApplicationEnvironment,
    env_map: Mapping[str, str],
    data_dir: Optional[str] = None,
) -> None:
    """Punto de entrada unificado de validación de la política de entornos.

    Ejecuta todos los checks de seguridad en orden:
    1. Production safety (debug, log level, mock providers, hosts)
    2. Cross-env data root isolation
    3. Secret namespace isolation

    Esta función debe ser invocada desde el DeploymentConfigValidator
    después de resolver el environment.
    """
    # 1. Safety por environment
    validate_production_safety(environment, env_map)

    # 2. Data root cross-env guard
    if data_dir:
        violation = cross_env_data_root_violation(environment, data_dir)
        if violation:
            raise CrossEnvironmentAccessError(violation)

    # 3. Secret namespace isolation
    validate_secret_namespace(environment, env_map)
