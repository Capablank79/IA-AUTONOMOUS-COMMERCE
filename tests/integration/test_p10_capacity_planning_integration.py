"""
Pruebas de integración para Capacity Planning (Hito P.10 — Production / Operations).

Cubre los 10 escenarios canónicos:
A. stable low demand -> LOW risk / NO_ACTION
B. rising demand -> SCALE_SOON
C. demand exceeds capacity -> HIGH/CRITICAL & SCALE_IMMEDIATELY
D. unknown capacity -> UNKNOWN & INVESTIGATE_UNKNOWN_CAPACITY
E. latency/error pressure -> higher risk evaluation
F. provider pressure -> provider capacity warning
G. DB pressure -> database recommendation
H. DEV metrics do not affect PROD (Environment isolation)
I. tenant demand aggregated safely
J. insufficient history -> low confidence / INSUFFICIENT_DATA
"""

from datetime import datetime, timezone, timedelta
import pytest

from src.domain.deployment.models import ApplicationEnvironment
from src.domain.monitoring.models import (
    MetricSample,
    MetricType,
    MetricUnit,
    MonitoringScope,
)
from src.domain.capacity_planning.models import (
    CapacityConfidence,
    CapacityEvaluation,
    CapacityForecast,
    CapacityRecommendation,
    CapacityRecommendationType,
    CapacityResourceLimits,
    CapacityRisk,
    CapacityScope,
    CapacitySnapshot,
    ForecastHorizon,
    ResourceDimension,
)
from src.domain.reliability.ports import ClockPort
from src.application.capacity_planning.capacity_planning_service import (
    CapacityPlanningService,
    DefaultCapacityConfiguration,
    ProductionCapacityDataProvider,
)


class FixedClock(ClockPort):
    def __init__(self, current_time: datetime):
        self._now = current_time

    def now(self) -> datetime:
        return self._now

    def sleep(self, seconds: float) -> None:
        pass


class MockIntegratedMetricRepository:
    """Mock in-memory repository para pruebas de integración con filtrado por entorno y scope."""

    def __init__(self, samples=None):
        self._samples = list(samples or [])

    def record_sample(self, sample: MetricSample) -> None:
        self._samples.append(sample)

    def record_samples(self, samples: list[MetricSample]) -> None:
        self._samples.extend(samples)

    def get_samples(
        self,
        environment: ApplicationEnvironment,
        metric_type=None,
        start_time=None,
        end_time=None,
        scope=None,
        tenant_id=None,
    ) -> tuple[MetricSample, ...]:
        matched = []
        for s in self._samples:
            if s.environment != environment:
                continue
            if metric_type and s.metric_type != metric_type:
                continue
            if scope and s.scope != scope:
                continue
            if tenant_id and s.tenant_id != tenant_id:
                continue
            if start_time and s.timestamp < start_time:
                continue
            if end_time and s.timestamp > end_time:
                continue
            matched.append(s)
        return tuple(matched)


def build_samples_for_scenario(
    metric_type: MetricType,
    values: list[float],
    environment: ApplicationEnvironment = ApplicationEnvironment.PRODUCTION,
    scope: MonitoringScope = MonitoringScope.PLATFORM,
    tenant_id: str = None,
    base_time: datetime = None,
    interval_minutes: int = 5,
) -> list[MetricSample]:
    if base_time is None:
        base_time = datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc)
    
    samples = []
    for i, val in enumerate(values):
        t = base_time + timedelta(minutes=i * interval_minutes)
        samples.append(
            MetricSample(
                metric_type=metric_type,
                value=val,
                timestamp=t,
                environment=environment,
                scope=scope,
                tenant_id=tenant_id,
                unit=MetricUnit.COUNT,
            )
        )
    return samples


def test_scenario_a_stable_low_demand_leads_to_low_risk_no_action():
    """Escenario A: Demanda estable y baja (20% utilización) -> LOW risk / NO_ACTION."""
    now = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
    # 200 req/min de 1000 capacidad
    samples = build_samples_for_scenario(
        metric_type=MetricType.REQUEST_COUNT,
        values=[200.0 + (i % 3) for i in range(15)],
        base_time=now - timedelta(hours=2),
    )
    repo = MockIntegratedMetricRepository(samples)
    service = CapacityPlanningService(data_provider=ProductionCapacityDataProvider(repo))
    
    eval_res = service.evaluate_dimension(
        dimension=ResourceDimension.REQUEST_THROUGHPUT,
        start_time=now - timedelta(hours=3),
        end_time=now,
    )
    assert eval_res.overall_risk == CapacityRisk.LOW
    assert eval_res.recommendation.recommendation_type == CapacityRecommendationType.NO_ACTION
    assert eval_res.current_utilization_ratio < 0.30
    assert eval_res.current_headroom > 700.0


def test_scenario_b_rising_demand_leads_to_scale_soon():
    """Escenario B: Crecimiento sostenido proyectado hacia la saturación -> SCALE_SOON."""
    now = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
    # Demanda subiendo de 500 a 750 (capacidad 1000)
    values = [500.0 + (i * 20.0) for i in range(12)]  # 500 .. 720
    samples = build_samples_for_scenario(
        metric_type=MetricType.REQUEST_COUNT,
        values=values,
        base_time=now - timedelta(hours=1),
        interval_minutes=5,
    )
    repo = MockIntegratedMetricRepository(samples)
    service = CapacityPlanningService(data_provider=ProductionCapacityDataProvider(repo))
    
    eval_res = service.evaluate_dimension(
        dimension=ResourceDimension.REQUEST_THROUGHPUT,
        start_time=now - timedelta(hours=2),
        end_time=now,
    )
    assert eval_res.recommendation.recommendation_type in (
        CapacityRecommendationType.SCALE_SOON,
        CapacityRecommendationType.SCALE_IMMEDIATELY,
    )
    assert eval_res.trend.direction == "GROWING"
    assert eval_res.trend.growth_rate_per_hour > 0


def test_scenario_c_demand_exceeds_capacity_leads_to_critical_and_scale_immediately():
    """Escenario C: Demanda sobrepasa capacidad o umbral crítico -> CRITICAL & SCALE_IMMEDIATELY."""
    now = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
    # Demanda a 950 req/min (capacidad 1000, umbral crítico 85%)
    samples = build_samples_for_scenario(
        metric_type=MetricType.REQUEST_COUNT,
        values=[920.0, 930.0, 940.0, 950.0, 960.0, 950.0, 955.0, 960.0, 970.0, 980.0],
        base_time=now - timedelta(hours=1),
    )
    repo = MockIntegratedMetricRepository(samples)
    service = CapacityPlanningService(data_provider=ProductionCapacityDataProvider(repo))
    
    eval_res = service.evaluate_dimension(
        dimension=ResourceDimension.REQUEST_THROUGHPUT,
        start_time=now - timedelta(hours=2),
        end_time=now,
    )
    assert eval_res.overall_risk == CapacityRisk.CRITICAL
    assert eval_res.recommendation.recommendation_type == CapacityRecommendationType.SCALE_IMMEDIATELY
    assert eval_res.recommendation.suggested_capacity is not None
    assert eval_res.recommendation.suggested_capacity >= 1500.0


def test_scenario_d_unknown_capacity_leads_to_unknown_risk_and_investigate():
    """Escenario D: Dimensión sin capacidad conocida -> UNKNOWN & INVESTIGATE_UNKNOWN_CAPACITY."""
    now = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
    samples = build_samples_for_scenario(
        metric_type=MetricType.REQUEST_COUNT,
        values=[100.0] * 10,
        base_time=now - timedelta(hours=1),
    )
    repo = MockIntegratedMetricRepository(samples)
    
    # Custom config provider with unknown limit for REQUEST_THROUGHPUT
    custom_limits = {
        ApplicationEnvironment.PRODUCTION: {
            ResourceDimension.REQUEST_THROUGHPUT: CapacityResourceLimits(
                dimension=ResourceDimension.REQUEST_THROUGHPUT,
                max_capacity=None,  # UNKNOWN CAPACITY
                unit="req/min",
            )
        }
    }
    config = DefaultCapacityConfiguration(custom_limits=custom_limits)
    service = CapacityPlanningService(
        data_provider=ProductionCapacityDataProvider(repo),
        config_provider=config,
    )
    
    eval_res = service.evaluate_dimension(
        dimension=ResourceDimension.REQUEST_THROUGHPUT,
        start_time=now - timedelta(hours=2),
        end_time=now,
    )
    assert eval_res.overall_risk == CapacityRisk.UNKNOWN
    assert eval_res.current_utilization_ratio is None
    assert eval_res.current_headroom is None
    assert eval_res.recommendation.recommendation_type == CapacityRecommendationType.INVESTIGATE_UNKNOWN_CAPACITY


def test_scenario_e_error_pressure_evaluates_higher_risk():
    """Escenario E: Presión de errores técnicos (ERROR_RATE) incrementa el riesgo de saturación."""
    now = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
    # Error rate en 4.5% (capacidad máxima aceptable 5.0%, crítico 80% = 4.0%)
    samples = build_samples_for_scenario(
        metric_type=MetricType.ERROR_RATE,
        values=[4.2, 4.3, 4.4, 4.5, 4.6, 4.5, 4.7, 4.8, 4.9, 4.6],
        base_time=now - timedelta(hours=1),
    )
    repo = MockIntegratedMetricRepository(samples)
    service = CapacityPlanningService(data_provider=ProductionCapacityDataProvider(repo))
    
    eval_res = service.evaluate_dimension(
        dimension=ResourceDimension.ERROR_PRESSURE,
        start_time=now - timedelta(hours=2),
        end_time=now,
    )
    assert eval_res.overall_risk in (CapacityRisk.HIGH, CapacityRisk.CRITICAL)
    assert eval_res.recommendation.recommendation_type in (
        CapacityRecommendationType.SCALE_SOON,
        CapacityRecommendationType.SCALE_IMMEDIATELY,
    )


def test_scenario_f_ai_provider_pressure_warning():
    """Escenario F: Presión de throughput en proveedores AI genera recomendaciones de escalado/revisión."""
    now = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
    # AI provider throughput 110 calls/min (capacidad conocida 120 calls/min)
    samples = build_samples_for_scenario(
        metric_type=MetricType.MODEL_REQUEST_COUNT,
        values=[105.0, 108.0, 110.0, 112.0, 114.0, 110.0, 115.0, 116.0, 115.0, 118.0],
        base_time=now - timedelta(hours=1),
    )
    repo = MockIntegratedMetricRepository(samples)
    service = CapacityPlanningService(data_provider=ProductionCapacityDataProvider(repo))
    
    eval_res = service.evaluate_dimension(
        dimension=ResourceDimension.AI_PROVIDER_THROUGHPUT,
        start_time=now - timedelta(hours=2),
        end_time=now,
    )
    assert eval_res.overall_risk in (CapacityRisk.HIGH, CapacityRisk.CRITICAL)
    assert eval_res.current_utilization_ratio > 0.85


def test_scenario_g_database_pressure_recommendation():
    """Escenario G: Latencia de queries en DB degradada (DB_QUERY_LATENCY) genera recomendación de capacidad DB."""
    now = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
    # Latencia DB promedio 450 ms (capacidad SLA 500 ms, umbral crítico 80% = 400ms)
    samples = build_samples_for_scenario(
        metric_type=MetricType.DB_QUERY_LATENCY,
        values=[420.0, 430.0, 440.0, 450.0, 460.0, 455.0, 470.0, 465.0, 480.0, 475.0],
        base_time=now - timedelta(hours=1),
    )
    repo = MockIntegratedMetricRepository(samples)
    service = CapacityPlanningService(data_provider=ProductionCapacityDataProvider(repo))
    
    eval_res = service.evaluate_dimension(
        dimension=ResourceDimension.DATABASE_QUERY_LATENCY,
        start_time=now - timedelta(hours=2),
        end_time=now,
    )
    assert eval_res.overall_risk in (CapacityRisk.HIGH, CapacityRisk.CRITICAL)
    assert eval_res.recommendation.recommendation_type in (
        CapacityRecommendationType.SCALE_SOON,
        CapacityRecommendationType.SCALE_IMMEDIATELY,
    )


def test_scenario_h_dev_metrics_do_not_affect_prod_snapshot():
    """Escenario H: Las métricas de DEV están completamente aisladas y no afectan a PROD."""
    now = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
    # DEV tiene saturación masiva (9999.0)
    dev_samples = build_samples_for_scenario(
        metric_type=MetricType.REQUEST_COUNT,
        values=[9999.0] * 10,
        environment=ApplicationEnvironment.DEVELOPMENT,
        base_time=now - timedelta(hours=1),
    )
    # PROD tiene tráfico saludable (150.0)
    prod_samples = build_samples_for_scenario(
        metric_type=MetricType.REQUEST_COUNT,
        values=[150.0] * 10,
        environment=ApplicationEnvironment.PRODUCTION,
        base_time=now - timedelta(hours=1),
    )
    repo = MockIntegratedMetricRepository(dev_samples + prod_samples)
    clock = FixedClock(now)
    service = CapacityPlanningService(
        data_provider=ProductionCapacityDataProvider(repo),
        environment=ApplicationEnvironment.PRODUCTION,
        clock=clock,
    )
    
    snapshot_prod = service.generate_capacity_snapshot()
    req_eval = snapshot_prod.evaluations[ResourceDimension.REQUEST_THROUGHPUT]
    assert req_eval.current_demand == 150.0
    assert req_eval.overall_risk == CapacityRisk.LOW
    assert snapshot_prod.environment == ApplicationEnvironment.PRODUCTION


def test_scenario_i_tenant_demand_aggregated_safely_without_leak():
    """Escenario I: Demanda por tenant se analiza de forma aislada sin fuga cruzada."""
    now = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
    tenant_1_samples = build_samples_for_scenario(
        metric_type=MetricType.REQUEST_COUNT,
        values=[300.0] * 10,
        scope=MonitoringScope.TENANT,
        tenant_id="tenant-acme",
        base_time=now - timedelta(hours=1),
    )
    tenant_2_samples = build_samples_for_scenario(
        metric_type=MetricType.REQUEST_COUNT,
        values=[700.0] * 10,
        scope=MonitoringScope.TENANT,
        tenant_id="tenant-beta",
        base_time=now - timedelta(hours=1),
    )
    repo = MockIntegratedMetricRepository(tenant_1_samples + tenant_2_samples)
    clock = FixedClock(now)
    service = CapacityPlanningService(data_provider=ProductionCapacityDataProvider(repo), clock=clock)
    
    snapshot_t1 = service.generate_capacity_snapshot(
        scope=CapacityScope.TENANT,
        tenant_id="tenant-acme",
    )
    assert snapshot_t1.tenant_id == "tenant-acme"
    assert snapshot_t1.evaluations[ResourceDimension.REQUEST_THROUGHPUT].current_demand == 300.0


def test_scenario_j_insufficient_history_results_in_low_confidence():
    """Escenario J: Histórico insuficiente (< 2 muestras) produce confianza INSUFFICIENT_DATA y riesgo UNKNOWN."""
    now = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
    samples = build_samples_for_scenario(
        metric_type=MetricType.REQUEST_COUNT,
        values=[100.0],  # 1 sola muestra
        base_time=now - timedelta(hours=1),
    )
    repo = MockIntegratedMetricRepository(samples)
    service = CapacityPlanningService(data_provider=ProductionCapacityDataProvider(repo))
    
    eval_res = service.evaluate_dimension(
        dimension=ResourceDimension.REQUEST_THROUGHPUT,
        start_time=now - timedelta(hours=2),
        end_time=now,
    )
    assert eval_res.confidence == CapacityConfidence.INSUFFICIENT_DATA
    assert eval_res.trend.direction == "UNKNOWN"
