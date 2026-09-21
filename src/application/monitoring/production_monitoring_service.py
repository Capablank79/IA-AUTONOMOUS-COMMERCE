"""
Servicio de Aplicación para Monitoreo de Producción (Hito P.7 — Production / Operations).

Responsabilidades:
1. Colección y proyección de muestras de telemetría técnica (HTTP traffic, DB availability/latency, Health checks, Backups, DR, Model AI/Cost, Quotas).
2. Agregación determinista en ventanas temporales (5m, 1h, 24h) en UTC.
3. Cálculo matemático exacto de percentiles (p50, p95, avg) con monotonic clock.
4. Preservación estricta de UNKNOWN != ZERO (cuando no hay muestras, value=None).
5. Aislamiento absoluto por ApplicationEnvironment (DEV, STAGING, PROD) y TenantContext.
6. Failure Safety: Si la persistencia o colector de métricas falla, la aplicación core sigue operando y el estado de monitoreo se reporta como DEGRADED/UNKNOWN.
7. Cero PII, secretos ni labels de alta cardinalidad.
"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
import logging
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from src.domain.deployment.models import ApplicationEnvironment, normalize_environment_name
from src.domain.health.models import HealthStatus, ReadinessResult, LivenessResult, DependencyCheckResult
from src.domain.monitoring.models import (
    MetricSample,
    MetricSeries,
    MetricType,
    MetricUnit,
    MetricWindow,
    MonitoringConfigurationError,
    MonitoringError,
    MonitoringIntegrityError,
    MonitoringMetric,
    MonitoringScope,
    ProductionMonitoringSnapshot,
    calculate_percentile,
    sanitize_route_template,
    validate_metric_labels,
    resolve_environment,
)
from src.domain.monitoring.ports import MetricRepositoryPort
from src.domain.reliability.ports import ClockPort
from src.domain.tenant.models import TenantContext
from src.domain.tenant.guard import CrossTenantGuard
from src.domain.backup.models import BackupExecutionResult, BackupStatus
from src.domain.disaster_recovery.models import DisasterRecoveryExecutionResult, RecoveryStatus
from src.domain.saas_observability.models import ObservabilityMetric

logger = logging.getLogger("ProductionMonitoringService")


class ProductionMonitoringService:
    """
    Servicio de orquestación y agregación de Monitoreo de Producción (P.7).
    """

    def __init__(
        self,
        repository: MetricRepositoryPort,
        environment: Union[ApplicationEnvironment, str] = ApplicationEnvironment.PRODUCTION,
        clock: Optional[ClockPort] = None,
    ) -> None:
        self._repository = repository
        self._environment = resolve_environment(environment)
        self._clock = clock
        self._collector_status = "healthy"

    @property
    def environment(self) -> ApplicationEnvironment:
        return self._environment

    def _now(self) -> datetime:
        if self._clock is not None:
            ts = self._clock.now()
            if ts.tzinfo is None:
                return ts.replace(tzinfo=timezone.utc)
            return ts
        return datetime.now(timezone.utc)

    # -------------------------------------------------------------------------
    # Recolección Segura de Muestras Técnicas
    # -------------------------------------------------------------------------

    def record_request_metric(
        self,
        method: str,
        path: str,
        status_code: int,
        duration_ms: float,
        environment: Optional[ApplicationEnvironment] = None,
        tenant_id: Optional[str] = None,
    ) -> None:
        """
        Registra telemetría de una petición HTTP.
        Aplica sanitización de template de ruta para evitar alta cardinalidad y excluye query/payload/auth.
        Failure-safe: si ocurre error, no interrumpe el flujo de la aplicación.
        """
        try:
            target_env = resolve_environment(environment) if environment else self._environment
            safe_route = sanitize_route_template(path)
            now = self._now()

            # Clasificación de status
            status_class = f"{status_code // 100}xx"
            is_success = 200 <= status_code < 400
            is_error = status_code >= 400

            labels = {
                "method": method.upper(),
                "route": safe_route,
                "status_code": str(status_code),
                "status_class": status_class,
            }

            samples: List[MetricSample] = [
                MetricSample(
                    metric_type=MetricType.REQUEST_COUNT,
                    value=1,
                    timestamp=now,
                    environment=target_env,
                    scope=MonitoringScope.TENANT if tenant_id else MonitoringScope.PLATFORM,
                    tenant_id=tenant_id,
                    unit=MetricUnit.COUNT,
                    labels=labels,
                ),
                MetricSample(
                    metric_type=MetricType.LATENCY_MS,
                    value=round(duration_ms, 3),
                    timestamp=now,
                    environment=target_env,
                    scope=MonitoringScope.TENANT if tenant_id else MonitoringScope.PLATFORM,
                    tenant_id=tenant_id,
                    unit=MetricUnit.MILLISECONDS,
                    labels=labels,
                ),
            ]

            if is_success:
                samples.append(
                    MetricSample(
                        metric_type=MetricType.SUCCESS_COUNT,
                        value=1,
                        timestamp=now,
                        environment=target_env,
                        scope=MonitoringScope.TENANT if tenant_id else MonitoringScope.PLATFORM,
                        tenant_id=tenant_id,
                        unit=MetricUnit.COUNT,
                        labels=labels,
                    )
                )
            elif is_error:
                samples.append(
                    MetricSample(
                        metric_type=MetricType.ERROR_COUNT,
                        value=1,
                        timestamp=now,
                        environment=target_env,
                        scope=MonitoringScope.TENANT if tenant_id else MonitoringScope.PLATFORM,
                        tenant_id=tenant_id,
                        unit=MetricUnit.COUNT,
                        labels=labels,
                    )
                )

            self._repository.record_samples(samples)
        except Exception as exc:
            logger.error(f"Non-fatal failure recording request metric: {exc}")
            self._collector_status = "degraded"

    def record_database_check(
        self,
        is_available: bool,
        latency_ms: Optional[float] = None,
        schema_compatible: Optional[bool] = None,
        error_message: Optional[str] = None,
        environment: Optional[ApplicationEnvironment] = None,
    ) -> None:
        """Registra métricas operacionales de base de datos P.3/P.6."""
        try:
            target_env = resolve_environment(environment) if environment else self._environment
            now = self._now()
            labels = {}
            if schema_compatible is not None:
                labels["schema_compatible"] = str(schema_compatible)
            if error_message:
                labels["error_type"] = "connection_failure"
            samples: List[MetricSample] = [
                MetricSample(
                    metric_type=MetricType.DB_AVAILABILITY,
                    value=1 if is_available else 0,
                    timestamp=now,
                    environment=target_env,
                    scope=MonitoringScope.PLATFORM,
                    unit=MetricUnit.BOOLEAN,
                    labels=labels,
                )
            ]
            if latency_ms is not None:
                samples.append(
                    MetricSample(
                        metric_type=MetricType.DB_QUERY_LATENCY,
                        value=round(latency_ms, 3),
                        timestamp=now,
                        environment=target_env,
                        scope=MonitoringScope.PLATFORM,
                        unit=MetricUnit.MILLISECONDS,
                    )
                )
            self._repository.record_samples(samples)
        except Exception as exc:
            logger.error(f"Non-fatal failure recording database check: {exc}")
            self._collector_status = "degraded"

    def record_health_check_result(
        self,
        result: Union[ReadinessResult, LivenessResult, DependencyCheckResult],
        environment: Optional[ApplicationEnvironment] = None,
    ) -> None:
        """Proyecta y registra el resultado instantáneo de P.6 en el histórico de monitoreo."""
        try:
            target_env = resolve_environment(environment) if environment else self._environment
            now = self._now()
            samples: List[MetricSample] = []

            if isinstance(result, LivenessResult):
                if result.status != HealthStatus.HEALTHY:
                    samples.append(
                        MetricSample(
                            metric_type=MetricType.LIVENESS_FAILURE_COUNT,
                            value=1,
                            timestamp=now,
                            environment=target_env,
                            scope=MonitoringScope.PLATFORM,
                            unit=MetricUnit.COUNT,
                            labels={"service": result.service},
                        )
                    )
            elif isinstance(result, ReadinessResult):
                if result.status != HealthStatus.HEALTHY:
                    samples.append(
                        MetricSample(
                            metric_type=MetricType.READINESS_FAILURE_COUNT,
                            value=1,
                            timestamp=now,
                            environment=target_env,
                            scope=MonitoringScope.PLATFORM,
                            unit=MetricUnit.COUNT,
                            labels={"service": result.service, "overall_status": result.status.value},
                        )
                    )
                # Extraer fallos individuales, latencia y disponibilidad de checks de dependencias
                for check in result.checks:
                    if check.status != HealthStatus.HEALTHY:
                        samples.append(
                            MetricSample(
                                metric_type=MetricType.READINESS_FAILURE_COUNT,
                                value=1,
                                timestamp=now,
                                environment=target_env,
                                scope=MonitoringScope.PLATFORM,
                                unit=MetricUnit.COUNT,
                                labels={"dependency": check.name, "status": check.status.value},
                            )
                        )
                    if check.name == "database":
                        self.record_database_check(
                            is_available=(check.status == HealthStatus.HEALTHY),
                            latency_ms=check.latency_ms,
                            environment=target_env,
                        )
            elif isinstance(result, DependencyCheckResult):
                if result.status != HealthStatus.HEALTHY:
                    samples.append(
                        MetricSample(
                            metric_type=MetricType.READINESS_FAILURE_COUNT,
                            value=1,
                            timestamp=now,
                            environment=target_env,
                            scope=MonitoringScope.PLATFORM,
                            unit=MetricUnit.COUNT,
                            labels={"dependency": result.name, "status": result.status.value},
                        )
                    )
                if result.name == "database":
                    self.record_database_check(
                        is_available=(result.status == HealthStatus.HEALTHY),
                        latency_ms=result.latency_ms,
                        environment=target_env,
                    )

            if samples:
                self._repository.record_samples(samples)
        except Exception as exc:
            logger.error(f"Non-fatal failure recording health check result: {exc}")
            self._collector_status = "degraded"

    def record_backup_result(
        self,
        result: BackupExecutionResult,
        environment: Optional[ApplicationEnvironment] = None,
    ) -> None:
        """Registra el estado operacional de ejecución y verificación de backups P.4."""
        try:
            target_env = resolve_environment(environment) if environment else self._environment
            now = self._now()
            backup_id = result.metadata.backup_id if result.metadata else "unknown"
            is_success = (result.status == BackupStatus.COMPLETED)
            self._repository.record_sample(
                MetricSample(
                    metric_type=MetricType.BACKUP_STATUS,
                    value=result.status.value,
                    timestamp=now,
                    environment=target_env,
                    scope=MonitoringScope.PLATFORM,
                    unit=MetricUnit.STATUS,
                    labels={
                        "backup_id": backup_id,
                        "success": str(is_success),
                    },
                )
            )
        except Exception as exc:
            logger.error(f"Non-fatal failure recording backup result: {exc}")
            self._collector_status = "degraded"

    def record_disaster_recovery_result(
        self,
        result: DisasterRecoveryExecutionResult,
        environment: Optional[ApplicationEnvironment] = None,
    ) -> None:
        """Registra el estado operacional de simulaciones o ejecuciones de Disaster Recovery P.5."""
        try:
            target_env = resolve_environment(environment) if environment else self._environment
            now = self._now()
            plan_id = result.plan.plan_id if result.plan else "unknown"
            scenario_val = result.plan.scenario.value if result.plan else "unknown"
            is_success = (result.status == RecoveryStatus.COMPLETED)
            self._repository.record_sample(
                MetricSample(
                    metric_type=MetricType.DR_STATUS,
                    value=result.status.value,
                    timestamp=now,
                    environment=target_env,
                    scope=MonitoringScope.PLATFORM,
                    unit=MetricUnit.STATUS,
                    labels={
                        "plan_id": plan_id,
                        "scenario": scenario_val,
                        "success": str(is_success),
                    },
                )
            )
        except Exception as exc:
            logger.error(f"Non-fatal failure recording DR result: {exc}")
            self._collector_status = "degraded"

    def record_ai_inference_metrics(
        self,
        provider: str,
        model: str,
        total_tokens: int,
        cost_usd: Union[float, Decimal],
        latency_ms: Optional[float] = None,
        is_error: bool = False,
        tenant_id: Optional[str] = None,
        environment: Optional[ApplicationEnvironment] = None,
    ) -> None:
        """Registra telemetría técnica y operacional de inferencias IA (O.5/K.3)."""
        try:
            target_env = resolve_environment(environment) if environment else self._environment
            now = self._now()
            labels = {
                "provider": provider,
                "model": model,
            }
            samples: List[MetricSample] = [
                MetricSample(
                    metric_type=MetricType.MODEL_REQUEST_COUNT,
                    value=1,
                    timestamp=now,
                    environment=target_env,
                    scope=MonitoringScope.TENANT if tenant_id else MonitoringScope.PLATFORM,
                    tenant_id=tenant_id,
                    unit=MetricUnit.COUNT,
                    labels=labels,
                ),
                MetricSample(
                    metric_type=MetricType.TOKEN_USAGE,
                    value=total_tokens,
                    timestamp=now,
                    environment=target_env,
                    scope=MonitoringScope.TENANT if tenant_id else MonitoringScope.PLATFORM,
                    tenant_id=tenant_id,
                    unit=MetricUnit.TOKENS,
                    labels=labels,
                ),
                MetricSample(
                    metric_type=MetricType.AI_COST,
                    value=Decimal(str(cost_usd)),
                    timestamp=now,
                    environment=target_env,
                    scope=MonitoringScope.TENANT if tenant_id else MonitoringScope.PLATFORM,
                    tenant_id=tenant_id,
                    unit=MetricUnit.CURRENCY,
                    labels=labels,
                ),
            ]
            if is_error:
                samples.append(
                    MetricSample(
                        metric_type=MetricType.MODEL_ERROR_COUNT,
                        value=1,
                        timestamp=now,
                        environment=target_env,
                        scope=MonitoringScope.TENANT if tenant_id else MonitoringScope.PLATFORM,
                        tenant_id=tenant_id,
                        unit=MetricUnit.COUNT,
                        labels=labels,
                    )
                )
            if latency_ms is not None:
                samples.append(
                    MetricSample(
                        metric_type=MetricType.LATENCY_MS,
                        value=round(latency_ms, 3),
                        timestamp=now,
                        environment=target_env,
                        scope=MonitoringScope.TENANT if tenant_id else MonitoringScope.PLATFORM,
                        tenant_id=tenant_id,
                        unit=MetricUnit.MILLISECONDS,
                        labels=labels,
                    )
                )
            self._repository.record_samples(samples)
        except Exception as exc:
            logger.error(f"Non-fatal failure recording AI inference metrics: {exc}")
            self._collector_status = "degraded"

    def record_quota_denial(
        self,
        quota_metric: str,
        tenant_id: Optional[str] = None,
        environment: Optional[ApplicationEnvironment] = None,
    ) -> None:
        """Registra presión y denegaciones de cuotas (O.7)."""
        try:
            target_env = normalize_environment_name(environment) if environment else self._environment
            now = self._now()
            self._repository.record_sample(
                MetricSample(
                    metric_type=MetricType.QUOTA_DENIAL_COUNT,
                    value=1,
                    timestamp=now,
                    environment=target_env,
                    scope=MonitoringScope.TENANT if tenant_id else MonitoringScope.PLATFORM,
                    tenant_id=tenant_id,
                    unit=MetricUnit.COUNT,
                    labels={"quota_metric": quota_metric},
                )
            )
        except Exception as exc:
            logger.error(f"Non-fatal failure recording quota denial: {exc}")
            self._collector_status = "degraded"

    # -------------------------------------------------------------------------
    # Agregación y Consultas de Monitoreo
    # -------------------------------------------------------------------------

    def get_metric(
        self,
        metric_type: MetricType,
        window: Union[MetricWindow, int] = MetricWindow.WINDOW_5M,
        environment: Optional[ApplicationEnvironment] = None,
        scope: Optional[MonitoringScope] = None,
        tenant_id: Optional[str] = None,
    ) -> MonitoringMetric:
        """
        Calcula una métrica agregada determinista en la ventana temporal especificada.
        Garantiza UNKNOWN != ZERO: si no hay muestras, value=None.
        """
        target_env = resolve_environment(environment) if environment else self._environment
        duration_sec = window.duration_seconds if isinstance(window, MetricWindow) else int(window)
        now = self._now()
        start_time = now - timedelta(seconds=duration_sec)

        samples = self._repository.get_samples(
            environment=target_env,
            metric_type=metric_type,
            start_time=start_time,
            end_time=now,
            scope=scope,
            tenant_id=tenant_id,
        )

        sample_count = len(samples)

        # Regla UNKNOWN != ZERO: si no hay muestras, el valor es None/desconocido
        if sample_count == 0:
            unit = self._resolve_default_unit(metric_type)
            return MonitoringMetric(
                metric_type=metric_type,
                value=None,
                unit=unit,
                sample_count=0,
                environment=target_env,
                evaluated_at=now,
                window_seconds=duration_sec,
                scope=scope or (MonitoringScope.TENANT if tenant_id else MonitoringScope.PLATFORM),
                tenant_id=tenant_id,
            )

        # Procesar valores según el tipo de métrica
        return self._aggregate_samples(
            metric_type=metric_type,
            samples=samples,
            target_env=target_env,
            now=now,
            duration_sec=duration_sec,
            scope=scope or (MonitoringScope.TENANT if tenant_id else MonitoringScope.PLATFORM),
            tenant_id=tenant_id,
        )

    def get_error_rate(
        self,
        window: Union[MetricWindow, int] = MetricWindow.WINDOW_5M,
        environment: Optional[ApplicationEnvironment] = None,
        tenant_id: Optional[str] = None,
    ) -> MonitoringMetric:
        """
        Calcula determinísticamente la tasa de error (ERROR_COUNT / REQUEST_COUNT).
        Preserva UNKNOWN != ZERO si no hay requests en la ventana.
        """
        target_env = resolve_environment(environment) if environment else self._environment
        duration_sec = window.duration_seconds if isinstance(window, MetricWindow) else int(window)
        now = self._now()
        start_time = now - timedelta(seconds=duration_sec)

        req_samples = self._repository.get_samples(
            environment=target_env,
            metric_type=MetricType.REQUEST_COUNT,
            start_time=start_time,
            end_time=now,
            tenant_id=tenant_id,
        )
        total_requests = sum(int(s.value) for s in req_samples if isinstance(s.value, (int, float, Decimal)))

        if total_requests == 0:
            return MonitoringMetric(
                metric_type=MetricType.ERROR_RATE,
                value=None,
                unit=MetricUnit.PERCENT,
                sample_count=0,
                environment=target_env,
                evaluated_at=now,
                window_seconds=duration_sec,
                scope=MonitoringScope.TENANT if tenant_id else MonitoringScope.PLATFORM,
                tenant_id=tenant_id,
            )

        err_samples = self._repository.get_samples(
            environment=target_env,
            metric_type=MetricType.ERROR_COUNT,
            start_time=start_time,
            end_time=now,
            tenant_id=tenant_id,
        )
        total_errors = sum(int(s.value) for s in err_samples if isinstance(s.value, (int, float, Decimal)))

        rate_percent = round((total_errors / total_requests) * 100.0, 3)

        return MonitoringMetric(
            metric_type=MetricType.ERROR_RATE,
            value=rate_percent,
            unit=MetricUnit.PERCENT,
            sample_count=len(req_samples) + len(err_samples),
            environment=target_env,
            evaluated_at=now,
            window_seconds=duration_sec,
            scope=MonitoringScope.TENANT if tenant_id else MonitoringScope.PLATFORM,
            tenant_id=tenant_id,
            avg_value=rate_percent,
        )

    def get_snapshot(
        self,
        window: Union[MetricWindow, int] = MetricWindow.WINDOW_5M,
        environment: Optional[ApplicationEnvironment] = None,
    ) -> ProductionMonitoringSnapshot:
        """
        Genera un snapshot integral e inmutable de monitoreo para un entorno en la ventana dada.
        """
        target_env = resolve_environment(environment) if environment else self._environment
        duration_sec = window.duration_seconds if isinstance(window, MetricWindow) else int(window)
        now = self._now()

        metrics_map: Dict[str, MonitoringMetric] = {}

        # 1. Traffic & Latency Metrics
        metrics_map[MetricType.REQUEST_COUNT.value] = self.get_metric(MetricType.REQUEST_COUNT, window, target_env)
        metrics_map[MetricType.SUCCESS_COUNT.value] = self.get_metric(MetricType.SUCCESS_COUNT, window, target_env)
        metrics_map[MetricType.ERROR_COUNT.value] = self.get_metric(MetricType.ERROR_COUNT, window, target_env)
        metrics_map[MetricType.ERROR_RATE.value] = self.get_error_rate(window, target_env)
        metrics_map[MetricType.LATENCY_MS.value] = self.get_metric(MetricType.LATENCY_MS, window, target_env)

        # 2. Health & Readiness History
        metrics_map[MetricType.READINESS_FAILURE_COUNT.value] = self.get_metric(MetricType.READINESS_FAILURE_COUNT, window, target_env)
        metrics_map[MetricType.LIVENESS_FAILURE_COUNT.value] = self.get_metric(MetricType.LIVENESS_FAILURE_COUNT, window, target_env)

        # 3. Database State
        metrics_map[MetricType.DB_AVAILABILITY.value] = self.get_metric(MetricType.DB_AVAILABILITY, window, target_env)
        metrics_map[MetricType.DB_QUERY_LATENCY.value] = self.get_metric(MetricType.DB_QUERY_LATENCY, window, target_env)

        # 4. AI & Model Metrics
        metrics_map[MetricType.MODEL_REQUEST_COUNT.value] = self.get_metric(MetricType.MODEL_REQUEST_COUNT, window, target_env)
        metrics_map[MetricType.MODEL_ERROR_COUNT.value] = self.get_metric(MetricType.MODEL_ERROR_COUNT, window, target_env)
        metrics_map[MetricType.TOKEN_USAGE.value] = self.get_metric(MetricType.TOKEN_USAGE, window, target_env)
        metrics_map[MetricType.AI_COST.value] = self.get_metric(MetricType.AI_COST, window, target_env)

        # 5. Quota & Operational Status
        metrics_map[MetricType.QUOTA_DENIAL_COUNT.value] = self.get_metric(MetricType.QUOTA_DENIAL_COUNT, window, target_env)
        metrics_map[MetricType.BACKUP_STATUS.value] = self.get_metric(MetricType.BACKUP_STATUS, window, target_env)
        metrics_map[MetricType.DR_STATUS.value] = self.get_metric(MetricType.DR_STATUS, window, target_env)

        latest_sample_at = self._repository.get_latest_sample_timestamp(target_env)

        return ProductionMonitoringSnapshot(
            environment=target_env,
            window_seconds=duration_sec,
            evaluated_at=now,
            metrics=metrics_map,
            collector_status=self._collector_status,
            last_sample_at=latest_sample_at,
        )

    # -------------------------------------------------------------------------
    # Métodos Auxiliares de Agregación Determinista
    # -------------------------------------------------------------------------

    def _resolve_default_unit(self, metric_type: MetricType) -> MetricUnit:
        if metric_type in {
            MetricType.REQUEST_COUNT,
            MetricType.SUCCESS_COUNT,
            MetricType.ERROR_COUNT,
            MetricType.READINESS_FAILURE_COUNT,
            MetricType.LIVENESS_FAILURE_COUNT,
            MetricType.MODEL_REQUEST_COUNT,
            MetricType.MODEL_ERROR_COUNT,
            MetricType.QUOTA_DENIAL_COUNT,
        }:
            return MetricUnit.COUNT
        elif metric_type in {MetricType.LATENCY_MS, MetricType.DB_QUERY_LATENCY}:
            return MetricUnit.MILLISECONDS
        elif metric_type == MetricType.ERROR_RATE:
            return MetricUnit.PERCENT
        elif metric_type == MetricType.TOKEN_USAGE:
            return MetricUnit.TOKENS
        elif metric_type == MetricType.AI_COST:
            return MetricUnit.CURRENCY
        elif metric_type == MetricType.DB_AVAILABILITY:
            return MetricUnit.BOOLEAN
        elif metric_type in {MetricType.BACKUP_STATUS, MetricType.DR_STATUS}:
            return MetricUnit.STATUS
        return MetricUnit.COUNT

    def _aggregate_samples(
        self,
        metric_type: MetricType,
        samples: Sequence[MetricSample],
        target_env: ApplicationEnvironment,
        now: datetime,
        duration_sec: int,
        scope: MonitoringScope,
        tenant_id: Optional[str],
    ) -> MonitoringMetric:
        unit = samples[0].unit if samples else self._resolve_default_unit(metric_type)

        # 1. Agregación de conteos acumulativos (REQUEST_COUNT, ERROR_COUNT, etc.)
        if metric_type in {
            MetricType.REQUEST_COUNT,
            MetricType.SUCCESS_COUNT,
            MetricType.ERROR_COUNT,
            MetricType.READINESS_FAILURE_COUNT,
            MetricType.LIVENESS_FAILURE_COUNT,
            MetricType.MODEL_REQUEST_COUNT,
            MetricType.MODEL_ERROR_COUNT,
            MetricType.QUOTA_DENIAL_COUNT,
            MetricType.TOKEN_USAGE,
        }:
            total_sum = sum(int(s.value) for s in samples if isinstance(s.value, (int, float, Decimal)))
            return MonitoringMetric(
                metric_type=metric_type,
                value=total_sum,
                unit=unit,
                sample_count=len(samples),
                environment=target_env,
                evaluated_at=now,
                window_seconds=duration_sec,
                scope=scope,
                tenant_id=tenant_id,
            )

        # 2. Agregación de costos (AI_COST)
        if metric_type == MetricType.AI_COST:
            total_cost = sum(
                (Decimal(str(s.value)) for s in samples if s.value is not None),
                Decimal("0.000000"),
            )
            return MonitoringMetric(
                metric_type=metric_type,
                value=total_cost,
                unit=unit,
                sample_count=len(samples),
                environment=target_env,
                evaluated_at=now,
                window_seconds=duration_sec,
                scope=scope,
                tenant_id=tenant_id,
            )

        # 3. Agregación cuantitativa continua con percentiles (LATENCY_MS, DB_QUERY_LATENCY)
        if metric_type in {MetricType.LATENCY_MS, MetricType.DB_QUERY_LATENCY}:
            numeric_vals = sorted(
                float(s.value)
                for s in samples
                if isinstance(s.value, (int, float, Decimal))
            )
            if not numeric_vals:
                return MonitoringMetric(
                    metric_type=metric_type,
                    value=None,
                    unit=unit,
                    sample_count=0,
                    environment=target_env,
                    evaluated_at=now,
                    window_seconds=duration_sec,
                    scope=scope,
                    tenant_id=tenant_id,
                )

            min_val = round(numeric_vals[0], 3)
            max_val = round(numeric_vals[-1], 3)
            avg_val = round(sum(numeric_vals) / len(numeric_vals), 3)
            p50_val = round(calculate_percentile(numeric_vals, 0.50) or avg_val, 3)
            p95_val = round(calculate_percentile(numeric_vals, 0.95) or max_val, 3)

            return MonitoringMetric(
                metric_type=metric_type,
                value=avg_val,
                unit=unit,
                sample_count=len(samples),
                environment=target_env,
                evaluated_at=now,
                window_seconds=duration_sec,
                scope=scope,
                tenant_id=tenant_id,
                min_value=min_val,
                max_value=max_val,
                avg_value=avg_val,
                p50_value=p50_val,
                p95_value=p95_val,
            )

        # 4. Agregación de disponibilidad booleana (DB_AVAILABILITY)
        if metric_type == MetricType.DB_AVAILABILITY:
            # Tomar el estado de la muestra más reciente en la ventana de evaluación
            sorted_samples = sorted(samples, key=lambda s: s.timestamp)
            latest_val = sorted_samples[-1].value
            is_healthy = latest_val in (1, True, "1", "true")
            return MonitoringMetric(
                metric_type=metric_type,
                value=1 if is_healthy else 0,
                unit=unit,
                sample_count=len(samples),
                environment=target_env,
                evaluated_at=now,
                window_seconds=duration_sec,
                scope=scope,
                tenant_id=tenant_id,
            )

        # 5. Agregación de estado discreto (BACKUP_STATUS, DR_STATUS)
        last_sample = sorted(samples, key=lambda s: s.timestamp)[-1]
        return MonitoringMetric(
            metric_type=metric_type,
            value=last_sample.value,
            unit=unit,
            sample_count=len(samples),
            environment=target_env,
            evaluated_at=now,
            window_seconds=duration_sec,
            scope=scope,
            tenant_id=tenant_id,
            labels=dict(last_sample.labels),
        )
