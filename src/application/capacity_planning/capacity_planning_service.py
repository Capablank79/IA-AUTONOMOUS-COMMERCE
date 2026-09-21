"""
Servicio de Aplicación para Capacity Planning (Hito P.10 — Production / Operations).

Responsabilidades:
1. Extraer telemetría técnica histórica desde P.7 Monitoring (sin duplicar recolección).
2. Calcular utilización determinista con denominador real (observed_demand / known_capacity).
3. Preservar UNKNOWN != SAFE: si la capacidad máxima o la demanda no se conocen, utilization es UNKNOWN.
4. Calcular headroom seguro (known_capacity - current_demand) y detectar saturación (negative headroom).
5. Analizar tendencias lineales y tasas de crecimiento deterministas.
6. Proyectar forecast a horizontes estándar (1h, 24h, 7d) considerando baseline y picos (p95 / peak).
7. Evaluar calidad de datos (cobertura temporal, número mínimo de muestras) y degradar confianza ante datos insuficientes.
8. Generar recomendaciones estructuradas de capacidad no ejecutivas (NO_ACTION, REVIEW_CAPACITY, SCALE_SOON, SCALE_IMMEDIATELY, INVESTIGATE_UNKNOWN_CAPACITY).
9. Garantizar aislamiento estricto entre entornos (DEV metrics never inform PROD) y alcance Tenant vs Platform.
10. Integrar con P.8 Alerting publicando hechos de riesgo sin ejecutar acciones automáticas peligrosas.
"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
import logging
import math
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from src.domain.deployment.models import ApplicationEnvironment, normalize_environment_name
from src.domain.monitoring.models import (
    MetricSample,
    MetricSeries,
    MetricType,
    MetricUnit,
    MetricWindow,
    MonitoringMetric,
    MonitoringScope,
    calculate_percentile,
    resolve_environment,
)
from src.domain.monitoring.ports import MetricRepositoryPort
from src.domain.capacity_planning.models import (
    CapacityConfidence,
    CapacityEvaluation,
    CapacityForecast,
    CapacityPlanningConfigurationError,
    CapacityPlanningError,
    CapacityPlanningIntegrityError,
    CapacityRecommendation,
    CapacityRecommendationType,
    CapacityResourceLimits,
    CapacityRisk,
    CapacityScope,
    CapacitySnapshot,
    CapacityTrend,
    ForecastHorizon,
    ResourceDimension,
    compute_capacity_checksum,
)
from src.domain.capacity_planning.ports import (
    CapacityConfigurationPort,
    CapacityDataProviderPort,
    CapacitySnapshotRepositoryPort,
)
from src.domain.reliability.ports import ClockPort
from src.domain.tenant.models import TenantContext
from src.domain.tenant.guard import CrossTenantGuard

logger = logging.getLogger("CapacityPlanningService")


class DefaultCapacityConfiguration(CapacityConfigurationPort):
    """
    Configuración en memoria de límites de capacidad conocidos para entornos de producción y desarrollo.
    """

    DEFAULT_LIMITS: Dict[ApplicationEnvironment, Dict[ResourceDimension, CapacityResourceLimits]] = {
        ApplicationEnvironment.PRODUCTION: {
            ResourceDimension.REQUEST_THROUGHPUT: CapacityResourceLimits(
                dimension=ResourceDimension.REQUEST_THROUGHPUT,
                max_capacity=1000.0,  # 1000 requests/min o req/seg
                unit="req/min",
                target_headroom_ratio=0.20,
                warning_utilization_ratio=0.70,
                critical_utilization_ratio=0.85,
            ),
            ResourceDimension.REQUEST_CONCURRENCY: CapacityResourceLimits(
                dimension=ResourceDimension.REQUEST_CONCURRENCY,
                max_capacity=100.0,  # 100 peticiones concurrentes
                unit="concurrency",
                target_headroom_ratio=0.25,
                warning_utilization_ratio=0.70,
                critical_utilization_ratio=0.85,
            ),
            ResourceDimension.DATABASE_CONNECTIONS: CapacityResourceLimits(
                dimension=ResourceDimension.DATABASE_CONNECTIONS,
                max_capacity=50.0,  # 50 conexiones máximas en pool
                unit="connections",
                target_headroom_ratio=0.20,
                warning_utilization_ratio=0.75,
                critical_utilization_ratio=0.90,
            ),
            ResourceDimension.DATABASE_QUERY_LATENCY: CapacityResourceLimits(
                dimension=ResourceDimension.DATABASE_QUERY_LATENCY,
                max_capacity=500.0,  # 500 ms SLA umbral de saturación
                unit="ms",
                target_headroom_ratio=0.20,
                warning_utilization_ratio=0.60,
                critical_utilization_ratio=0.80,
            ),
            ResourceDimension.AI_PROVIDER_THROUGHPUT: CapacityResourceLimits(
                dimension=ResourceDimension.AI_PROVIDER_THROUGHPUT,
                max_capacity=120.0,  # 120 calls/min provider rate limit
                unit="calls/min",
                target_headroom_ratio=0.20,
                warning_utilization_ratio=0.70,
                critical_utilization_ratio=0.85,
            ),
            ResourceDimension.TOKEN_THROUGHPUT: CapacityResourceLimits(
                dimension=ResourceDimension.TOKEN_THROUGHPUT,
                max_capacity=500000.0,  # 500k tokens/min
                unit="tokens/min",
                target_headroom_ratio=0.20,
                warning_utilization_ratio=0.75,
                critical_utilization_ratio=0.90,
            ),
            ResourceDimension.STORAGE_GROWTH: CapacityResourceLimits(
                dimension=ResourceDimension.STORAGE_GROWTH,
                max_capacity=100000.0,  # 100 GB (100,000 MB)
                unit="MB",
                target_headroom_ratio=0.20,
                warning_utilization_ratio=0.70,
                critical_utilization_ratio=0.85,
            ),
            ResourceDimension.BACKUP_SIZE: CapacityResourceLimits(
                dimension=ResourceDimension.BACKUP_SIZE,
                max_capacity=50000.0,  # 50 GB
                unit="MB",
                target_headroom_ratio=0.20,
                warning_utilization_ratio=0.70,
                critical_utilization_ratio=0.85,
            ),
            ResourceDimension.ERROR_PRESSURE: CapacityResourceLimits(
                dimension=ResourceDimension.ERROR_PRESSURE,
                max_capacity=5.0,  # 5% error rate max acceptable threshold
                unit="percent",
                target_headroom_ratio=0.40,
                warning_utilization_ratio=0.50,
                critical_utilization_ratio=0.80,
            ),
        }
    }

    def __init__(
        self,
        custom_limits: Optional[Mapping[ApplicationEnvironment, Mapping[ResourceDimension, CapacityResourceLimits]]] = None,
    ) -> None:
        self._limits: Dict[ApplicationEnvironment, Dict[ResourceDimension, CapacityResourceLimits]] = {}
        # Inicializar default
        for env, dims in self.DEFAULT_LIMITS.items():
            self._limits[env] = dict(dims)

        if custom_limits:
            for env, dims in custom_limits.items():
                resolved_env = resolve_environment(env)
                if resolved_env not in self._limits:
                    self._limits[resolved_env] = {}
                for dim, lim in dims.items():
                    self._limits[resolved_env][dim] = lim

    def set_limit(self, environment: ApplicationEnvironment, limit: CapacityResourceLimits) -> None:
        resolved_env = resolve_environment(environment)
        if resolved_env not in self._limits:
            self._limits[resolved_env] = {}
        self._limits[resolved_env][limit.dimension] = limit

    def get_resource_limits(
        self,
        environment: ApplicationEnvironment,
        dimension: ResourceDimension,
    ) -> CapacityResourceLimits:
        resolved_env = resolve_environment(environment)
        env_limits = self._limits.get(resolved_env, {})
        if dimension in env_limits:
            return env_limits[dimension]

        # Si no existe configuración explícita, la capacidad es UNKNOWN
        return CapacityResourceLimits(
            dimension=dimension,
            max_capacity=None,
            unit="units",
            target_headroom_ratio=0.20,
            warning_utilization_ratio=0.70,
            critical_utilization_ratio=0.85,
        )

    def get_all_resource_limits(
        self,
        environment: ApplicationEnvironment,
    ) -> Mapping[ResourceDimension, CapacityResourceLimits]:
        resolved_env = resolve_environment(environment)
        return dict(self._limits.get(resolved_env, {}))


class ProductionCapacityDataProvider(CapacityDataProviderPort):
    """
    Proveedor de datos de capacidad que consulta el repositorio de métricas técnicas P.7.
    Mapea ResourceDimension a MetricType de monitoreo.
    """

    DIMENSION_TO_METRIC_MAP: Mapping[ResourceDimension, MetricType] = {
        ResourceDimension.REQUEST_THROUGHPUT: MetricType.REQUEST_COUNT,
        ResourceDimension.REQUEST_CONCURRENCY: MetricType.REQUEST_COUNT,
        ResourceDimension.DATABASE_CONNECTIONS: MetricType.DB_AVAILABILITY,
        ResourceDimension.DATABASE_QUERY_LATENCY: MetricType.DB_QUERY_LATENCY,
        ResourceDimension.AI_PROVIDER_THROUGHPUT: MetricType.MODEL_REQUEST_COUNT,
        ResourceDimension.TOKEN_THROUGHPUT: MetricType.TOKEN_USAGE,
        ResourceDimension.STORAGE_GROWTH: MetricType.BACKUP_STATUS,
        ResourceDimension.BACKUP_SIZE: MetricType.BACKUP_STATUS,
        ResourceDimension.ERROR_PRESSURE: MetricType.ERROR_RATE,
    }

    def __init__(self, metric_repository: MetricRepositoryPort) -> None:
        self._metric_repo = metric_repository

    def get_dimension_samples(
        self,
        environment: ApplicationEnvironment,
        dimension: ResourceDimension,
        start_time: datetime,
        end_time: datetime,
        scope: CapacityScope = CapacityScope.PLATFORM,
        tenant_id: Optional[str] = None,
    ) -> Tuple[MetricSample, ...]:
        resolved_env = resolve_environment(environment)
        metric_type = self.DIMENSION_TO_METRIC_MAP.get(dimension)
        if not metric_type:
            return ()

        monitoring_scope = MonitoringScope.TENANT if scope == CapacityScope.TENANT else MonitoringScope.PLATFORM
        samples = self._metric_repo.get_samples(
            environment=resolved_env,
            metric_type=metric_type,
            start_time=start_time,
            end_time=end_time,
            scope=monitoring_scope,
            tenant_id=tenant_id,
        )
        return samples


class InMemoryCapacitySnapshotRepository(CapacitySnapshotRepositoryPort):
    """Repositorio en memoria para almacenar snapshots de capacidad evaluados."""

    def __init__(self) -> None:
        self._snapshots: List[CapacitySnapshot] = []

    def save_snapshot(self, snapshot: CapacitySnapshot) -> None:
        self._snapshots.append(snapshot)

    def get_latest_snapshot(
        self,
        environment: ApplicationEnvironment,
        scope: CapacityScope = CapacityScope.PLATFORM,
        tenant_id: Optional[str] = None,
    ) -> Optional[CapacitySnapshot]:
        resolved_env = resolve_environment(environment)
        filtered = [
            s for s in self._snapshots
            if s.environment == resolved_env and s.scope == scope and s.tenant_id == tenant_id
        ]
        if not filtered:
            return None
        return sorted(filtered, key=lambda s: s.evaluated_at, reverse=True)[0]


def _extract_numeric_value(sample: MetricSample) -> Optional[float]:
    """Extrae de forma segura el valor numérico de una muestra de telemetría."""
    val = sample.value
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, Decimal):
        return float(val)
    if isinstance(val, bool):
        return 1.0 if val else 0.0
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


class CapacityPlanningService:
    """
    Servicio de orquestación de Capacity Planning (P.10).
    """

    MINIMUM_SAMPLES_FOR_HIGH_CONFIDENCE = 10
    MINIMUM_SAMPLES_FOR_MEDIUM_CONFIDENCE = 5
    MINIMUM_SAMPLES_FOR_EVALUATION = 2

    def __init__(
        self,
        data_provider: CapacityDataProviderPort,
        config_provider: Optional[CapacityConfigurationPort] = None,
        snapshot_repository: Optional[CapacitySnapshotRepositoryPort] = None,
        environment: Union[ApplicationEnvironment, str] = ApplicationEnvironment.PRODUCTION,
        clock: Optional[ClockPort] = None,
    ) -> None:
        self._data_provider = data_provider
        self._config_provider = config_provider or DefaultCapacityConfiguration()
        self._snapshot_repo = snapshot_repository or InMemoryCapacitySnapshotRepository()
        self._environment = resolve_environment(environment)
        self._clock = clock

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
    # Core Mathematical Engine: Utilization, Headroom, Trend & Forecast
    # -------------------------------------------------------------------------

    @staticmethod
    def calculate_utilization(
        observed_demand: Optional[float],
        known_capacity: Optional[float],
    ) -> Optional[float]:
        """
        Calcula la utilización de capacidad: observed / capacity.
        Si la capacidad o la demanda son desconocidas o <= 0, retorna None (UNKNOWN).
        """
        if observed_demand is None or known_capacity is None:
            return None
        if known_capacity <= 0:
            return None
        return round(observed_demand / known_capacity, 4)

    @staticmethod
    def calculate_headroom(
        observed_demand: Optional[float],
        known_capacity: Optional[float],
    ) -> Tuple[Optional[float], Optional[float]]:
        """
        Calcula el headroom absoluto y porcentual respecto a la capacidad conocida.
        headroom = known_capacity - observed_demand
        headroom_ratio = (known_capacity - observed_demand) / known_capacity
        Retorna (None, None) si la capacidad es desconocida.
        """
        if observed_demand is None or known_capacity is None:
            return None, None
        if known_capacity <= 0:
            return None, None

        headroom_abs = round(known_capacity - observed_demand, 4)
        headroom_ratio = round(headroom_abs / known_capacity, 4)
        return headroom_abs, headroom_ratio

    @classmethod
    def compute_trend(
        cls,
        samples: Sequence[MetricSample],
    ) -> CapacityTrend:
        """
        Calcula la tendencia lineal determinista a partir de las muestras temporales.
        """
        if len(samples) < cls.MINIMUM_SAMPLES_FOR_EVALUATION:
            baseline = _extract_numeric_value(samples[0]) if samples else None
            return CapacityTrend(
                direction="UNKNOWN",
                growth_rate_per_hour=None,
                slope=None,
                baseline_demand=baseline,
                sample_count=len(samples),
            )

        # Ordenar por timestamp
        sorted_samples = sorted(samples, key=lambda s: s.timestamp)
        t0 = sorted_samples[0].timestamp.timestamp()

        points: List[Tuple[float, float]] = []
        for s in sorted_samples:
            val = _extract_numeric_value(s)
            if val is not None:
                t = s.timestamp.timestamp() - t0
                points.append((t, float(val)))

        if len(points) < 2:
            return CapacityTrend(
                direction="UNKNOWN",
                growth_rate_per_hour=None,
                slope=None,
                baseline_demand=points[0][1] if points else None,
                sample_count=len(points),
            )

        n = len(points)
        sum_x = sum(p[0] for p in points)
        sum_y = sum(p[1] for p in points)
        sum_xy = sum(p[0] * p[1] for p in points)
        sum_xx = sum(p[0] * p[0] for p in points)

        denominator = (n * sum_xx - sum_x * sum_x)
        if abs(denominator) < 1e-9:
            slope = 0.0
            intercept = sum_y / n
        else:
            slope = (n * sum_xy - sum_x * sum_y) / denominator
            intercept = (sum_y - slope * sum_x) / n

        baseline = round(intercept, 4)
        growth_per_hour = round(slope * 3600.0, 4)

        # Determinar dirección
        if abs(growth_per_hour) < 0.001:
            direction = "STABLE"
        elif growth_per_hour > 0:
            direction = "GROWING"
        else:
            direction = "DECLINING"

        return CapacityTrend(
            direction=direction,
            growth_rate_per_hour=growth_per_hour,
            slope=round(slope, 6),
            baseline_demand=baseline,
            sample_count=n,
        )

    @classmethod
    def project_forecast(
        cls,
        trend: CapacityTrend,
        current_demand: Optional[float],
        peak_demand: Optional[float],
        limits: CapacityResourceLimits,
        horizon: ForecastHorizon,
        sample_count: int,
    ) -> CapacityForecast:
        """
        Proyecta la demanda futura considerando tendencia lineal, picos observados y límites.
        """
        if current_demand is None or trend.slope is None or sample_count < cls.MINIMUM_SAMPLES_FOR_EVALUATION:
            return CapacityForecast(
                horizon=horizon,
                projected_demand=None,
                projected_peak_demand=None,
                projected_utilization_ratio=None,
                projected_headroom=None,
                projected_headroom_ratio=None,
                risk=CapacityRisk.UNKNOWN,
                confidence=CapacityConfidence.INSUFFICIENT_DATA,
            )

        delta_seconds = float(horizon.seconds)
        slope = trend.slope or 0.0
        projected = max(0.0, current_demand + (slope * delta_seconds))

        # Picos proyectados manteniendo ratio peak / demand
        peak_ratio = 1.0
        if peak_demand is not None and current_demand > 0:
            peak_ratio = max(1.0, peak_demand / current_demand)
        projected_peak = round(projected * peak_ratio, 4)
        projected_val = round(projected, 4)

        if not limits.is_capacity_known:
            return CapacityForecast(
                horizon=horizon,
                projected_demand=projected_val,
                projected_peak_demand=projected_peak,
                projected_utilization_ratio=None,
                projected_headroom=None,
                projected_headroom_ratio=None,
                risk=CapacityRisk.UNKNOWN,
                confidence=CapacityConfidence.LOW if sample_count >= cls.MINIMUM_SAMPLES_FOR_MEDIUM_CONFIDENCE else CapacityConfidence.INSUFFICIENT_DATA,
            )

        max_cap = limits.max_capacity
        assert max_cap is not None
        proj_utilization = round(projected_val / max_cap, 4)
        proj_headroom = round(max_cap - projected_val, 4)
        proj_headroom_ratio = round(proj_headroom / max_cap, 4)

        # Evaluar riesgo en horizonte
        if proj_utilization >= 1.0 or (projected_peak / max_cap) >= 1.05:
            risk = CapacityRisk.CRITICAL
        elif proj_utilization >= limits.critical_utilization_ratio:
            risk = CapacityRisk.HIGH
        elif proj_utilization >= limits.warning_utilization_ratio:
            risk = CapacityRisk.MODERATE
        else:
            risk = CapacityRisk.LOW

        # Evaluar confianza
        if sample_count >= cls.MINIMUM_SAMPLES_FOR_HIGH_CONFIDENCE:
            confidence = CapacityConfidence.HIGH
        elif sample_count >= cls.MINIMUM_SAMPLES_FOR_MEDIUM_CONFIDENCE:
            confidence = CapacityConfidence.MEDIUM
        else:
            confidence = CapacityConfidence.LOW

        return CapacityForecast(
            horizon=horizon,
            projected_demand=projected_val,
            projected_peak_demand=projected_peak,
            projected_utilization_ratio=proj_utilization,
            projected_headroom=proj_headroom,
            projected_headroom_ratio=proj_headroom_ratio,
            risk=risk,
            confidence=confidence,
        )

    # -------------------------------------------------------------------------
    # Recommendation Engine
    # -------------------------------------------------------------------------

    @classmethod
    def derive_recommendation(
        cls,
        dimension: ResourceDimension,
        current_demand: Optional[float],
        peak_demand: Optional[float],
        limits: CapacityResourceLimits,
        trend: CapacityTrend,
        forecasts: Mapping[str, CapacityForecast],
        overall_risk: CapacityRisk,
        confidence: CapacityConfidence,
    ) -> CapacityRecommendation:
        """
        Genera una recomendación estructurada y no ejecutiva con trazabilidad explicable.
        """
        now = datetime.now(timezone.utc)

        if not limits.is_capacity_known:
            return CapacityRecommendation(
                dimension=dimension,
                recommendation_type=CapacityRecommendationType.INVESTIGATE_UNKNOWN_CAPACITY,
                current_demand=current_demand,
                known_capacity=None,
                current_utilization_ratio=None,
                current_headroom=None,
                risk=CapacityRisk.UNKNOWN,
                confidence=confidence,
                reason=f"Resource {dimension.value} has unknown maximum capacity limits. Capacity planning cannot guarantee headroom without explicit baseline limits.",
                suggested_capacity=None,
                created_at=now,
            )

        max_cap = limits.max_capacity
        assert max_cap is not None
        utilization = cls.calculate_utilization(current_demand, max_cap)
        headroom, _ = cls.calculate_headroom(current_demand, max_cap)

        # Verificar forecast en 24h y 7d
        fc_24h = forecasts.get(ForecastHorizon.HORIZON_24H.value)
        fc_7d = forecasts.get(ForecastHorizon.HORIZON_7D.value)

        if overall_risk == CapacityRisk.CRITICAL or (utilization is not None and utilization >= limits.critical_utilization_ratio):
            suggested = round(max_cap * 1.5, 2)
            return CapacityRecommendation(
                dimension=dimension,
                recommendation_type=CapacityRecommendationType.SCALE_IMMEDIATELY,
                current_demand=current_demand,
                known_capacity=max_cap,
                current_utilization_ratio=utilization,
                current_headroom=headroom,
                risk=CapacityRisk.CRITICAL,
                confidence=confidence,
                reason=f"Resource {dimension.value} utilization ({utilization * 100 if utilization else 'N/A'}%) or projected demand exceeds critical threshold ({limits.critical_utilization_ratio * 100}%). Immediate scale expansion recommended.",
                suggested_capacity=suggested,
                forecast_horizon_analyzed=ForecastHorizon.HORIZON_24H,
                created_at=now,
            )

        if overall_risk == CapacityRisk.HIGH or (fc_24h and fc_24h.risk in (CapacityRisk.HIGH, CapacityRisk.CRITICAL)):
            suggested = round(max_cap * 1.25, 2)
            return CapacityRecommendation(
                dimension=dimension,
                recommendation_type=CapacityRecommendationType.SCALE_SOON,
                current_demand=current_demand,
                known_capacity=max_cap,
                current_utilization_ratio=utilization,
                current_headroom=headroom,
                risk=CapacityRisk.HIGH,
                confidence=confidence,
                reason=f"Resource {dimension.value} shows high saturation risk within 24h horizon. Scaling planned expansion is advised.",
                suggested_capacity=suggested,
                forecast_horizon_analyzed=ForecastHorizon.HORIZON_24H,
                created_at=now,
            )

        if overall_risk == CapacityRisk.MODERATE or (fc_7d and fc_7d.risk in (CapacityRisk.HIGH, CapacityRisk.MODERATE)):
            return CapacityRecommendation(
                dimension=dimension,
                recommendation_type=CapacityRecommendationType.REVIEW_CAPACITY,
                current_demand=current_demand,
                known_capacity=max_cap,
                current_utilization_ratio=utilization,
                current_headroom=headroom,
                risk=CapacityRisk.MODERATE,
                confidence=confidence,
                reason=f"Resource {dimension.value} is approaching warning headroom ratio ({limits.warning_utilization_ratio * 100}%). Review growth trend during next operational cycle.",
                suggested_capacity=None,
                forecast_horizon_analyzed=ForecastHorizon.HORIZON_7D,
                created_at=now,
            )

        return CapacityRecommendation(
            dimension=dimension,
            recommendation_type=CapacityRecommendationType.NO_ACTION,
            current_demand=current_demand,
            known_capacity=max_cap,
            current_utilization_ratio=utilization,
            current_headroom=headroom,
            risk=CapacityRisk.LOW,
            confidence=confidence,
            reason=f"Resource {dimension.value} is operating within safe capacity limits with adequate headroom (>{limits.target_headroom_ratio * 100}%).",
            suggested_capacity=None,
            created_at=now,
        )

    # -------------------------------------------------------------------------
    # Evaluation Workflow
    # -------------------------------------------------------------------------

    def evaluate_dimension(
        self,
        dimension: ResourceDimension,
        start_time: datetime,
        end_time: datetime,
        environment: Optional[ApplicationEnvironment] = None,
        scope: CapacityScope = CapacityScope.PLATFORM,
        tenant_id: Optional[str] = None,
    ) -> CapacityEvaluation:
        """
        Evalúa integralmente una dimensión técnica de capacidad para el entorno y ventana temporal dada.
        """
        target_env = resolve_environment(environment) if environment else self._environment
        limits = self._config_provider.get_resource_limits(target_env, dimension)
        now = self._now()

        samples = self._data_provider.get_dimension_samples(
            environment=target_env,
            dimension=dimension,
            start_time=start_time,
            end_time=end_time,
            scope=scope,
            tenant_id=tenant_id,
        )

        quality_issues: List[str] = []
        if not samples:
            quality_issues.append("No metric samples collected in window.")
            trend = CapacityTrend(
                direction="UNKNOWN",
                growth_rate_per_hour=None,
                slope=None,
                baseline_demand=None,
                sample_count=0,
            )
            empty_forecasts = {
                h.value: CapacityForecast(
                    horizon=h,
                    projected_demand=None,
                    projected_peak_demand=None,
                    projected_utilization_ratio=None,
                    projected_headroom=None,
                    projected_headroom_ratio=None,
                    risk=CapacityRisk.UNKNOWN,
                    confidence=CapacityConfidence.INSUFFICIENT_DATA,
                )
                for h in ForecastHorizon
            }
            rec = self.derive_recommendation(
                dimension=dimension,
                current_demand=None,
                peak_demand=None,
                limits=limits,
                trend=trend,
                forecasts=empty_forecasts,
                overall_risk=CapacityRisk.UNKNOWN,
                confidence=CapacityConfidence.INSUFFICIENT_DATA,
            )
            return CapacityEvaluation(
                dimension=dimension,
                current_demand=None,
                peak_demand=None,
                p95_demand=None,
                known_capacity=limits.max_capacity,
                current_utilization_ratio=None,
                current_headroom=None,
                current_headroom_ratio=None,
                trend=trend,
                forecasts=empty_forecasts,
                overall_risk=CapacityRisk.UNKNOWN,
                confidence=CapacityConfidence.INSUFFICIENT_DATA,
                recommendation=rec,
                sample_count=0,
                evaluated_at=now,
                data_quality_issues=tuple(quality_issues),
            )

        numeric_vals = [
            _extract_numeric_value(s)
            for s in samples
            if _extract_numeric_value(s) is not None
        ]
        if not numeric_vals:
            quality_issues.append("Samples contain non-numeric data.")
            numeric_vals = [0.0]

        # Métrica agregada actual (promedio o suma según dimensión)
        sorted_vals = sorted(numeric_vals)
        current_demand = round(sum(sorted_vals) / len(sorted_vals), 4)
        peak_demand = round(sorted_vals[-1], 4)
        p95_demand = round(calculate_percentile(sorted_vals, 0.95) or peak_demand, 4)

        if len(samples) < self.MINIMUM_SAMPLES_FOR_EVALUATION:
            quality_issues.append("Insufficient samples for robust linear regression.")

        trend = self.compute_trend(samples)

        # Proyecciones por horizonte
        forecasts: Dict[str, CapacityForecast] = {}
        forecast_risks: List[CapacityRisk] = []
        for h in ForecastHorizon:
            fc = self.project_forecast(
                trend=trend,
                current_demand=current_demand,
                peak_demand=peak_demand,
                limits=limits,
                horizon=h,
                sample_count=len(samples),
            )
            forecasts[h.value] = fc
            forecast_risks.append(fc.risk)

        # Utilización y Headroom actual
        utilization = self.calculate_utilization(current_demand, limits.max_capacity)
        headroom, headroom_ratio = self.calculate_headroom(current_demand, limits.max_capacity)

        # Determinar overall_risk
        if not limits.is_capacity_known:
            overall_risk = CapacityRisk.UNKNOWN
        elif utilization is not None and utilization >= limits.critical_utilization_ratio:
            overall_risk = CapacityRisk.CRITICAL
        elif utilization is not None and utilization >= limits.warning_utilization_ratio:
            overall_risk = CapacityRisk.HIGH
        elif any(r == CapacityRisk.CRITICAL for r in forecast_risks):
            overall_risk = CapacityRisk.HIGH
        elif any(r == CapacityRisk.HIGH for r in forecast_risks):
            overall_risk = CapacityRisk.MODERATE
        else:
            overall_risk = CapacityRisk.LOW

        # Determinar confidence
        if len(samples) >= self.MINIMUM_SAMPLES_FOR_HIGH_CONFIDENCE:
            confidence = CapacityConfidence.HIGH
        elif len(samples) >= self.MINIMUM_SAMPLES_FOR_MEDIUM_CONFIDENCE:
            confidence = CapacityConfidence.MEDIUM
        elif len(samples) >= self.MINIMUM_SAMPLES_FOR_EVALUATION:
            confidence = CapacityConfidence.LOW
        else:
            confidence = CapacityConfidence.INSUFFICIENT_DATA

        recommendation = self.derive_recommendation(
            dimension=dimension,
            current_demand=current_demand,
            peak_demand=peak_demand,
            limits=limits,
            trend=trend,
            forecasts=forecasts,
            overall_risk=overall_risk,
            confidence=confidence,
        )

        return CapacityEvaluation(
            dimension=dimension,
            current_demand=current_demand,
            peak_demand=peak_demand,
            p95_demand=p95_demand,
            known_capacity=limits.max_capacity,
            current_utilization_ratio=utilization,
            current_headroom=headroom,
            current_headroom_ratio=headroom_ratio,
            trend=trend,
            forecasts=forecasts,
            overall_risk=overall_risk,
            confidence=confidence,
            recommendation=recommendation,
            sample_count=len(samples),
            evaluated_at=now,
            data_quality_issues=tuple(quality_issues),
        )

    def generate_capacity_snapshot(
        self,
        environment: Optional[ApplicationEnvironment] = None,
        scope: CapacityScope = CapacityScope.PLATFORM,
        tenant_id: Optional[str] = None,
        lookback_window: timedelta = timedelta(hours=24),
    ) -> CapacitySnapshot:
        """
        Genera un CapacitySnapshot consolidado evaluando todas las dimensiones de recursos.
        Aplica aislamiento estricto por ApplicationEnvironment.
        """
        target_env = resolve_environment(environment) if environment else self._environment
        now = self._now()
        start_time = now - lookback_window

        evaluations: Dict[ResourceDimension, CapacityEvaluation] = {}
        recommendations: List[CapacityRecommendation] = []

        for dimension in ResourceDimension:
            evaluation = self.evaluate_dimension(
                dimension=dimension,
                start_time=start_time,
                end_time=now,
                environment=target_env,
                scope=scope,
                tenant_id=tenant_id,
            )
            evaluations[dimension] = evaluation
            recommendations.append(evaluation.recommendation)

        # Riesgo global: el máximo riesgo encontrado
        risks = [e.overall_risk for e in evaluations.values()]
        if any(r == CapacityRisk.CRITICAL for r in risks):
            overall_risk = CapacityRisk.CRITICAL
        elif any(r == CapacityRisk.HIGH for r in risks):
            overall_risk = CapacityRisk.HIGH
        elif any(r == CapacityRisk.MODERATE for r in risks):
            overall_risk = CapacityRisk.MODERATE
        elif all(r == CapacityRisk.UNKNOWN for r in risks):
            overall_risk = CapacityRisk.UNKNOWN
        else:
            overall_risk = CapacityRisk.LOW

        # Confianza global: el mínimo nivel de confianza representativo
        confidences = [e.confidence for e in evaluations.values()]
        if all(c == CapacityConfidence.HIGH for c in confidences):
            overall_confidence = CapacityConfidence.HIGH
        elif any(c == CapacityConfidence.HIGH for c in confidences) or any(c == CapacityConfidence.MEDIUM for c in confidences):
            overall_confidence = CapacityConfidence.MEDIUM
        elif any(c == CapacityConfidence.LOW for c in confidences):
            overall_confidence = CapacityConfidence.LOW
        else:
            overall_confidence = CapacityConfidence.INSUFFICIENT_DATA

        snapshot = CapacitySnapshot(
            environment=target_env,
            scope=scope,
            evaluated_at=now,
            evaluations=evaluations,
            recommendations=tuple(recommendations),
            overall_risk=overall_risk,
            overall_confidence=overall_confidence,
            tenant_id=tenant_id,
        )

        self._snapshot_repo.save_snapshot(snapshot)
        return snapshot
