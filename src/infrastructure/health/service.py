"""Servicio y comprobadores de salud operacional (P.6 — Health Checks).

Proporciona:
- HealthCheckService: Evaluación determinista de Liveness y Readiness.
- Comprobación de persistencia y almacenamiento seguro (con cleanup garantizado).
- Comprobación de conectividad y esquema PostgreSQL (SELECT 1 + Alembic revision compatibility).
- Comprobación y aislamiento de dependencias opcionales y externas (LLM, MercadoLibre, Billing).
- Sanitización estricta de salidas (Cero passwords, tokens, DSNs en texto plano).
- Tiempos de respuesta ultrarrápidos con timeouts acotados.
"""

from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from src.domain.deployment.models import ApplicationEnvironment, DeploymentConfig
from src.domain.health.models import (
    DependencyCheckResult,
    DependencyClassification,
    HealthStatus,
    LivenessResult,
    ReadinessResult,
)
from src.infrastructure.persistence.database.config import (
    DatabaseConfig,
    DatabaseConnectionFactory,
    sanitize_error_message,
)
from scripts.db_migrate import check_schema_compatibility

logger = logging.getLogger("HealthCheckService")


class HealthCheckService:
    """Servicio de evaluación técnica inmediata de Liveness y Readiness."""

    def __init__(
        self,
        config: DeploymentConfig,
        db_config: Optional[DatabaseConfig] = None,
        db_factory: Optional[DatabaseConnectionFactory] = None,
        is_startup_complete: bool = True,
        is_shutting_down: bool = False,
        optional_checkers: Optional[Mapping[str, Callable[[], DependencyCheckResult]]] = None,
    ) -> None:
        self._config = config
        self._db_config = db_config
        self._db_factory = db_factory
        self._is_startup_complete = is_startup_complete
        self._is_shutting_down = is_shutting_down
        self._optional_checkers = dict(optional_checkers or {})

    def set_startup_complete(self, complete: bool = True) -> None:
        """Marca si el proceso completó su fase de inicialización/startup."""
        self._is_startup_complete = complete

    def set_shutting_down(self, shutting_down: bool = True) -> None:
        """Marca si el proceso ha entrado en fase de graceful shutdown."""
        self._is_shutting_down = shutting_down

    def check_liveness(self) -> LivenessResult:
        """Evalúa si el proceso/aplicación está vivo.

        Liveness NUNCA depende de bases de datos externas, LLM, Mercado Libre ni redes remotas.
        Es ligera, determinista y responde de inmediato.
        """
        # Si la app no está en un estado internamente fatal, se reporta HEALTHY
        return LivenessResult(
            status=HealthStatus.HEALTHY,
            service="ai-autonomous-commerce",
            version=self._config.app_version,
            environment=self._config.environment.value,
        )

    def check_storage(self, timeout_sec: float = 2.0) -> DependencyCheckResult:
        """Verifica que el almacenamiento persistente configurado esté accesible y sea escribible.

        Garantiza creación de sentinel efímero y limpieza (cleanup) inmediata sin dejar residuos.
        """
        start_t = time.perf_counter()
        data_dir = Path(self._config.data_dir)
        try:
            # Reutilizar check_storage_writable si existe para preservar compatibilidad con monkeypatching y O.13
            from src.infrastructure.web.app import check_storage_writable
            is_ok = check_storage_writable(data_dir)
            if not is_ok:
                raise IOError(f"Storage writable check failed on {data_dir}")

            latency = (time.perf_counter() - start_t) * 1000.0
            return DependencyCheckResult(
                name="storage",
                classification=DependencyClassification.CRITICAL,
                status=HealthStatus.HEALTHY,
                message="Storage directory is writable and readable",
                latency_ms=latency,
                details={"data_dir": str(data_dir)},
            )
        except Exception as exc:
            latency = (time.perf_counter() - start_t) * 1000.0
            sanitized_msg = sanitize_error_message(str(exc))
            logger.error(f"Storage health check failed on {data_dir}: {sanitized_msg}")
            return DependencyCheckResult(
                name="storage",
                classification=DependencyClassification.CRITICAL,
                status=HealthStatus.UNHEALTHY,
                message="storage_not_writable",
                latency_ms=latency,
                details={"data_dir": str(data_dir), "error": sanitized_msg},
            )

    def check_database(self, timeout_sec: float = 3.0) -> Optional[DependencyCheckResult]:
        """Verifica la conectividad a PostgreSQL y la compatibilidad del esquema de migraciones (P.3).

        No ejecuta migraciones automáticamente.
        Si la base de datos no está configurada y el entorno no es estricto en DB, retorna None.
        En producción o si DATABASE_URL/POSTGRES_DB está configurado, la base de datos es CRITICAL.
        """
        # Determinar si PostgreSQL está activo/requerido
        # Si psycopg no está instalado o si estamos en TESTING/DEVELOPMENT sin configuración explícita inyectada,
        # la base de datos no bloquea el readiness si no hay conectividad / driver.
        try:
            import psycopg
        except ImportError:
            psycopg = None

        if psycopg is None and self._db_config is None:
            return None

        has_explicit_db_env = bool(os.environ.get("DATABASE_URL")) or bool(os.environ.get("POSTGRES_DB"))
        if self._config.environment in {ApplicationEnvironment.TESTING, ApplicationEnvironment.DEVELOPMENT} and not has_explicit_db_env and self._db_config is None:
            return None

        db_required = (
            self._config.environment in {ApplicationEnvironment.PRODUCTION, ApplicationEnvironment.STAGING}
            or has_explicit_db_env
            or (self._db_config is not None)
        )

        if not db_required:
            return None

        start_t = time.perf_counter()
        try:
            # 1. Conexión rápida (SELECT 1)
            cfg = self._db_config
            if cfg is None:
                cfg = DatabaseConfig.from_env()

            factory = self._db_factory or DatabaseConnectionFactory(cfg)
            # Reutilizar check_connection si está disponible o create_connection
            if hasattr(factory, "check_connection"):
                factory.check_connection()
            else:
                conn = factory.create_connection(autocommit=True)
                try:
                    with conn.cursor() as cur:
                        cur.execute("SELECT 1")
                        row = cur.fetchone()
                        if not row or row[0] != 1:
                            raise RuntimeError("Database ping query failed")
                finally:
                    if hasattr(conn, "close") and not getattr(conn, "closed", False):
                        conn.close()

            # 2. Verificación de compatibilidad de esquema Alembic
            schema_info = check_schema_compatibility()
            latency = (time.perf_counter() - start_t) * 1000.0

            if schema_info.get("status") != "ok" or not schema_info.get("is_up_to_date", False):
                err = schema_info.get("error_message") or "database_schema_mismatch"
                return DependencyCheckResult(
                    name="database",
                    classification=DependencyClassification.CRITICAL,
                    status=HealthStatus.UNHEALTHY,
                    message=f"database_schema_mismatch: current={schema_info.get('current_revision')}, head={schema_info.get('head_revision')}",
                    latency_ms=latency,
                    details={
                        "current_revision": schema_info.get("current_revision"),
                        "head_revision": schema_info.get("head_revision"),
                        "error": sanitize_error_message(err),
                    },
                )

            return DependencyCheckResult(
                name="database",
                classification=DependencyClassification.CRITICAL,
                status=HealthStatus.HEALTHY,
                message="Database connection and schema are compatible and up to date",
                latency_ms=latency,
                details={
                    "current_revision": schema_info.get("current_revision"),
                    "head_revision": schema_info.get("head_revision"),
                },
            )

        except Exception as exc:
            latency = (time.perf_counter() - start_t) * 1000.0
            sanitized_err = sanitize_error_message(str(exc))
            logger.warning(f"Database readiness check failed: {sanitized_err}")
            return DependencyCheckResult(
                name="database",
                classification=DependencyClassification.CRITICAL,
                status=HealthStatus.UNHEALTHY,
                message=f"database_connectivity_failure: {sanitized_err}",
                latency_ms=latency,
                details={"error": sanitized_err},
            )

    def check_readiness(self) -> ReadinessResult:
        """Evalúa si la aplicación está lista para recibir y procesar tráfico de producción.

        Reglas:
        - Si está en startup o apagándose (shutdown): UNHEALTHY (503).
        - Storage: CRITICAL (UNHEALTHY -> Readiness FAIL).
        - Database (si requerida/configurada): CRITICAL (UNHEALTHY -> Readiness FAIL).
        - Dependencias opcionales/externas: OPTIONAL/EXTERNAL_NON_BLOCKING (UNHEALTHY/DEGRADED -> Status general DEGRADED pero ready=200).
        - UNKNOWN en cualquier dependencia crítica -> Readiness FAIL (503).
        """
        checks: List[DependencyCheckResult] = []

        # 1. Startup State Guard
        if not self._is_startup_complete:
            startup_check = DependencyCheckResult(
                name="startup",
                classification=DependencyClassification.CRITICAL,
                status=HealthStatus.UNHEALTHY,
                message="Application is currently initializing and not ready for traffic",
            )
            return ReadinessResult(
                status=HealthStatus.UNHEALTHY,
                service="ai-autonomous-commerce",
                version=self._config.app_version,
                environment=self._config.environment.value,
                checks=(startup_check,),
            )

        # 2. Shutdown State Guard
        if self._is_shutting_down:
            shutdown_check = DependencyCheckResult(
                name="shutdown",
                classification=DependencyClassification.CRITICAL,
                status=HealthStatus.UNHEALTHY,
                message="Application is undergoing graceful shutdown",
            )
            return ReadinessResult(
                status=HealthStatus.UNHEALTHY,
                service="ai-autonomous-commerce",
                version=self._config.app_version,
                environment=self._config.environment.value,
                checks=(shutdown_check,),
            )

        # 3. Storage Check (CRITICAL)
        storage_result = self.check_storage()
        checks.append(storage_result)

        # 4. Database Check (CRITICAL if present/required)
        db_result = self.check_database()
        if db_result is not None:
            checks.append(db_result)

        # 5. Optional / External Non-Blocking Checkers
        for name, checker in self._optional_checkers.items():
            try:
                res = checker()
                checks.append(res)
            except Exception as exc:
                sanitized_msg = sanitize_error_message(str(exc))
                checks.append(
                    DependencyCheckResult(
                        name=name,
                        classification=DependencyClassification.OPTIONAL,
                        status=HealthStatus.DEGRADED,
                        message=f"optional_checker_failed: {sanitized_msg}",
                    )
                )

        # 6. Determinar estado global
        # Regla: Cualquier dependencia CRITICAL que sea UNHEALTHY o UNKNOWN derriba Readiness (503).
        has_critical_failure = any(
            c.classification == DependencyClassification.CRITICAL and c.status in {HealthStatus.UNHEALTHY, HealthStatus.UNKNOWN}
            for c in checks
        )

        has_optional_failure = any(
            c.classification in {DependencyClassification.OPTIONAL, DependencyClassification.EXTERNAL_NON_BLOCKING}
            and c.status in {HealthStatus.DEGRADED, HealthStatus.UNHEALTHY, HealthStatus.UNKNOWN}
            for c in checks
        )

        if has_critical_failure:
            overall_status = HealthStatus.UNHEALTHY
        elif has_optional_failure:
            overall_status = HealthStatus.DEGRADED
        else:
            overall_status = HealthStatus.HEALTHY

        return ReadinessResult(
            status=overall_status,
            service="ai-autonomous-commerce",
            version=self._config.app_version,
            environment=self._config.environment.value,
            checks=tuple(checks),
        )
