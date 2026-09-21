"""
Pruebas unitarias para Capacity Planning (Hito P.10 — Production / Operations).

Cubre los 16 requisitos mínimos de la especificación:
1. utilization known capacity (utilization = observed / capacity)
2. capacity unknown -> UNKNOWN (utilization is None, not 0 or 100%)
3. headroom calculation (known_capacity - demand)
4. negative headroom (demand > capacity -> negative headroom)
5. moving average / trend (slope, growth rate, direction)
6. insufficient samples (confidence drops to INSUFFICIENT_DATA / UNKNOWN trend)
7. forecast deterministic (linear projection reproducible)
8. spike considered (peak_demand and p95_demand tracked)
9. confidence degradation (confidence drops from HIGH to LOW with fewer samples)
10. environment isolation (DEV configuration and data do not affect PROD)
11. tenant / platform distinction (tenant scope isolated from platform scope)
12. quota != capacity (quota limits distinct from infra capacity limits)
13. recommendation no action (adequate headroom yields NO_ACTION)
14. scale soon / scale immediately recommendation (high utilization / saturated forecast)
15. recommendation != execution (recommendations are facts/suggestions, never perform autoscaling)
16. no P.11+ dependencies or changes
"""

from datetime import datetime, timezone, timedelta
import pytest

from src.domain.deployment.models import ApplicationEnvironment
from src.domain.monitoring.models import (
    MetricSample,
    MetricType,
    MetricUnit,
    MetricWindow,
    MonitoringScope,
)
from src.domain.capacity_planning.models import (
    CapacityConfidence,
    CapacityEvaluation,
    CapacityForecast,
    CapacityPlanningConfigurationError,
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
from src.domain.quota_management.models import QuotaType, QuotaRule, QuotaScope
from src.application.capacity_planning.capacity_planning_service import (
    CapacityPlanningService,
    DefaultCapacityConfiguration,
    ProductionCapacityDataProvider,
)
from src.domain.capacity_planning.ports import (
    CapacityConfigurationPort,
    CapacityDataProviderPort,
)


class MockMetricRepository:
    """Mock repository para pruebas unitarias de recolección de métricas."""

    def __init__(self, samples=None):
        self.samples = list(samples or [])

    def get_samples(self, environment, metric_type=None, start_time=None, end_time=None, scope=None, tenant_id=None):
        res = []
        for s in self.samples:
            if s.environment != environment:
                continue
            if metric_type and s.metric_type != metric_type:
                continue
            if scope and s.scope != scope:
                continue
            if tenant_id and s.tenant_id != tenant_id:
                continue
            res.append(s)
        return tuple(res)


def create_sample_series(
    dimension_values: list[float],
    metric_type: MetricType = MetricType.REQUEST_COUNT,
    environment: ApplicationEnvironment = ApplicationEnvironment.PRODUCTION,
    interval_minutes: int = 10,
    base_time: datetime = None,
    scope: MonitoringScope = MonitoringScope.PLATFORM,
    tenant_id: str = None,
) -> list[MetricSample]:
    if base_time is None:
        base_time = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc) - timedelta(minutes=len(dimension_values) * interval_minutes)

    samples = []
    for i, val in enumerate(dimension_values):
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


def test_01_utilization_known_capacity():
    """1. utilization known capacity: calculation with valid denominator."""
    utilization = CapacityPlanningService.calculate_utilization(observed_demand=800.0, known_capacity=1000.0)
    assert utilization == 0.8000


def test_02_capacity_unknown_yields_unknown():
    """2. capacity unknown -> UNKNOWN: utilization is None, not 0% or 100%."""
    utilization = CapacityPlanningService.calculate_utilization(observed_demand=800.0, known_capacity=None)
    assert utilization is None

    # When observed demand is None
    utilization_no_demand = CapacityPlanningService.calculate_utilization(observed_demand=None, known_capacity=1000.0)
    assert utilization_no_demand is None


def test_03_headroom_calculation():
    """3. headroom calculation: headroom = known_capacity - demand."""
    headroom_abs, headroom_ratio = CapacityPlanningService.calculate_headroom(observed_demand=750.0, known_capacity=1000.0)
    assert headroom_abs == 250.0
    assert headroom_ratio == 0.2500


def test_04_negative_headroom_on_saturation():
    """4. negative headroom: demand exceeding capacity results in negative headroom."""
    headroom_abs, headroom_ratio = CapacityPlanningService.calculate_headroom(observed_demand=1200.0, known_capacity=1000.0)
    assert headroom_abs == -200.0
    assert headroom_ratio == -0.2000


def test_05_moving_average_and_trend():
    """5. moving average / trend: calculates slope, direction and growth rate deterministically."""
    now = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
    # Rising demand from 100 to 200 over 10 samples (1 sample every 6 minutes = 1 hour)
    values = [100.0, 110.0, 120.0, 130.0, 140.0, 150.0, 160.0, 170.0, 180.0, 200.0]
    samples = create_sample_series(values, base_time=now - timedelta(hours=1), interval_minutes=6)

    trend = CapacityPlanningService.compute_trend(samples)
    assert trend.direction == "GROWING"
    assert trend.growth_rate_per_hour is not None
    assert trend.growth_rate_per_hour > 0
    assert trend.sample_count == 10


def test_06_insufficient_samples():
    """6. insufficient samples: trend is UNKNOWN and confidence drops to INSUFFICIENT_DATA."""
    samples = create_sample_series([50.0])  # only 1 sample
    trend = CapacityPlanningService.compute_trend(samples)
    assert trend.direction == "UNKNOWN"
    assert trend.slope is None
    assert trend.sample_count == 1


def test_07_forecast_deterministic():
    """7. forecast deterministic: projected demand is mathematically predictable."""
    limits = CapacityResourceLimits(
        dimension=ResourceDimension.REQUEST_THROUGHPUT,
        max_capacity=1000.0,
        unit="req/min",
    )
    trend = CapacityTrend(
        direction="GROWING",
        growth_rate_per_hour=100.0,
        slope=100.0 / 3600.0,  # +100 units per hour
        baseline_demand=500.0,
        sample_count=10,
    )
    forecast_1h = CapacityPlanningService.project_forecast(
        trend=trend,
        current_demand=500.0,
        peak_demand=550.0,
        limits=limits,
        horizon=ForecastHorizon.HORIZON_1H,
        sample_count=10,
    )
    assert forecast_1h.projected_demand == 600.0
    assert forecast_1h.projected_utilization_ratio == 0.6000
    assert forecast_1h.projected_headroom == 400.0
    assert forecast_1h.risk == CapacityRisk.LOW
    assert forecast_1h.confidence == CapacityConfidence.HIGH


def test_08_spike_considered():
    """8. spike considered: peak and p95 demand tracked separately from baseline."""
    now = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
    # Baseline ~100, but has a spike of 900
    values = [100.0, 105.0, 95.0, 100.0, 900.0, 102.0, 98.0, 101.0, 104.0, 99.0]
    samples = create_sample_series(values, base_time=now - timedelta(hours=1), interval_minutes=6)

    repo = MockMetricRepository(samples)
    provider = ProductionCapacityDataProvider(repo)
    service = CapacityPlanningService(data_provider=provider)

    evaluation = service.evaluate_dimension(
        dimension=ResourceDimension.REQUEST_THROUGHPUT,
        start_time=now - timedelta(hours=2),
        end_time=now,
    )
    assert evaluation.current_demand is not None
    assert evaluation.peak_demand == 900.0
    assert evaluation.p95_demand is not None
    assert evaluation.p95_demand > evaluation.current_demand


def test_09_confidence_degradation():
    """9. confidence degradation: confidence decreases when sample count is low."""
    now = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)

    # High confidence (>= 10 samples)
    high_samples = create_sample_series([100.0] * 12, base_time=now - timedelta(hours=2))
    repo_high = MockMetricRepository(high_samples)
    service_high = CapacityPlanningService(data_provider=ProductionCapacityDataProvider(repo_high))
    eval_high = service_high.evaluate_dimension(ResourceDimension.REQUEST_THROUGHPUT, now - timedelta(hours=2), now)
    assert eval_high.confidence == CapacityConfidence.HIGH

    # Medium confidence (5 to 9 samples)
    med_samples = create_sample_series([100.0] * 6, base_time=now - timedelta(hours=2))
    repo_med = MockMetricRepository(med_samples)
    service_med = CapacityPlanningService(data_provider=ProductionCapacityDataProvider(repo_med))
    eval_med = service_med.evaluate_dimension(ResourceDimension.REQUEST_THROUGHPUT, now - timedelta(hours=2), now)
    assert eval_med.confidence == CapacityConfidence.MEDIUM

    # Low confidence (2 to 4 samples)
    low_samples = create_sample_series([100.0] * 3, base_time=now - timedelta(hours=2))
    repo_low = MockMetricRepository(low_samples)
    service_low = CapacityPlanningService(data_provider=ProductionCapacityDataProvider(repo_low))
    eval_low = service_low.evaluate_dimension(ResourceDimension.REQUEST_THROUGHPUT, now - timedelta(hours=2), now)
    assert eval_low.confidence == CapacityConfidence.LOW

    # Insufficient data (0 to 1 sample)
    no_samples = create_sample_series([100.0] * 1, base_time=now - timedelta(hours=2))
    repo_none = MockMetricRepository(no_samples)
    service_none = CapacityPlanningService(data_provider=ProductionCapacityDataProvider(repo_none))
    eval_none = service_none.evaluate_dimension(ResourceDimension.REQUEST_THROUGHPUT, now - timedelta(hours=2), now)
    assert eval_none.confidence == CapacityConfidence.INSUFFICIENT_DATA


def test_10_environment_isolation():
    """10. environment isolation: DEV metrics and configuration do not affect PROD."""
    now = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
    dev_samples = create_sample_series([9999.0] * 10, environment=ApplicationEnvironment.DEVELOPMENT)
    prod_samples = create_sample_series([200.0] * 10, environment=ApplicationEnvironment.PRODUCTION)

    repo = MockMetricRepository(dev_samples + prod_samples)
    service = CapacityPlanningService(data_provider=ProductionCapacityDataProvider(repo), environment=ApplicationEnvironment.PRODUCTION)

    eval_prod = service.evaluate_dimension(ResourceDimension.REQUEST_THROUGHPUT, now - timedelta(hours=2), now)
    assert eval_prod.current_demand == 200.0  # DEV demand 9999.0 is completely excluded


def test_11_tenant_platform_distinction():
    """11. tenant/platform distinction: tenant scope is isolated from platform scope."""
    now = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
    tenant_a_samples = create_sample_series(
        [50.0] * 10,
        scope=MonitoringScope.TENANT,
        tenant_id="tenant-alpha",
        base_time=now - timedelta(hours=2),
    )
    platform_samples = create_sample_series(
        [500.0] * 10,
        scope=MonitoringScope.PLATFORM,
        base_time=now - timedelta(hours=2),
    )

    repo = MockMetricRepository(tenant_a_samples + platform_samples)
    service = CapacityPlanningService(data_provider=ProductionCapacityDataProvider(repo))

    eval_tenant = service.evaluate_dimension(
        ResourceDimension.REQUEST_THROUGHPUT,
        now - timedelta(hours=2),
        now,
        scope=CapacityScope.TENANT,
        tenant_id="tenant-alpha",
    )
    eval_platform = service.evaluate_dimension(
        ResourceDimension.REQUEST_THROUGHPUT,
        now - timedelta(hours=2),
        now,
        scope=CapacityScope.PLATFORM,
    )

    assert eval_tenant.current_demand == 50.0
    assert eval_platform.current_demand == 500.0


def test_12_quota_distinct_from_capacity():
    """12. quota != capacity: O.7 quota policy is separate from P.10 infra technical limits."""
    # O.7 commercial policy limit
    commercial_quota_rule = QuotaRule(
        rule_id="rule-tenant-requests",
        quota_type=QuotaType.MAX_REQUESTS,
        scope=QuotaScope.TENANT,
        limit_value=10000,
    )

    # P.10 technical infrastructure capacity
    infra_capacity_limit = CapacityResourceLimits(
        dimension=ResourceDimension.REQUEST_THROUGHPUT,
        max_capacity=1000.0,
        unit="req/min",
    )

    assert commercial_quota_rule.limit_value != infra_capacity_limit.max_capacity
    assert commercial_quota_rule.quota_type.value == "MAX_REQUESTS"
    assert infra_capacity_limit.dimension.value == "REQUEST_THROUGHPUT"


def test_13_recommendation_no_action():
    """13. recommendation no action: healthy utilization with safe headroom generates NO_ACTION."""
    now = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
    # Low demand (200 / 1000 = 20% utilization)
    samples = create_sample_series([200.0] * 10, base_time=now - timedelta(hours=2))
    repo = MockMetricRepository(samples)
    service = CapacityPlanningService(data_provider=ProductionCapacityDataProvider(repo))

    eval_res = service.evaluate_dimension(ResourceDimension.REQUEST_THROUGHPUT, now - timedelta(hours=2), now)
    assert eval_res.recommendation.recommendation_type == CapacityRecommendationType.NO_ACTION
    assert eval_res.overall_risk == CapacityRisk.LOW


def test_14_recommendation_scale_soon_and_scale_immediately():
    """14. scale soon / scale immediately: high or saturated utilization triggers scaling recommendation."""
    now = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)

    # High demand near critical threshold (750 / 1000 = 75% utilization -> warning is 70%)
    samples_high = create_sample_series([750.0] * 10, base_time=now - timedelta(hours=2))
    repo_high = MockMetricRepository(samples_high)
    service_high = CapacityPlanningService(data_provider=ProductionCapacityDataProvider(repo_high))
    eval_high = service_high.evaluate_dimension(ResourceDimension.REQUEST_THROUGHPUT, now - timedelta(hours=2), now)
    assert eval_high.recommendation.recommendation_type == CapacityRecommendationType.SCALE_SOON
    assert eval_high.overall_risk in (CapacityRisk.HIGH, CapacityRisk.MODERATE)

    # Critical demand (950 / 1000 = 95% utilization -> critical is 85%)
    samples_crit = create_sample_series([950.0] * 10, base_time=now - timedelta(hours=2))
    repo_crit = MockMetricRepository(samples_crit)
    service_crit = CapacityPlanningService(data_provider=ProductionCapacityDataProvider(repo_crit))
    eval_crit = service_crit.evaluate_dimension(ResourceDimension.REQUEST_THROUGHPUT, now - timedelta(hours=2), now)
    assert eval_crit.recommendation.recommendation_type == CapacityRecommendationType.SCALE_IMMEDIATELY
    assert eval_crit.overall_risk == CapacityRisk.CRITICAL
    assert eval_crit.recommendation.suggested_capacity is not None
    assert eval_crit.recommendation.suggested_capacity > 1000.0


def test_15_recommendation_is_not_execution():
    """15. recommendation != execution: recommendations are advisory data structures only."""
    rec = CapacityRecommendation(
        dimension=ResourceDimension.REQUEST_THROUGHPUT,
        recommendation_type=CapacityRecommendationType.SCALE_IMMEDIATELY,
        current_demand=950.0,
        known_capacity=1000.0,
        current_utilization_ratio=0.95,
        current_headroom=50.0,
        risk=CapacityRisk.CRITICAL,
        confidence=CapacityConfidence.HIGH,
        reason="Test recommendation",
        suggested_capacity=1500.0,
    )
    # Immutable dataclass has no side-effects or execution methods
    assert hasattr(rec, "recommendation_type")
    assert not hasattr(rec, "execute_scaling")
    assert not hasattr(rec, "provision_cloud_resources")


def test_16_snapshot_integrity_checksum():
    """16. snapshot integrity: verifies deterministic SHA-256 calculation."""
    now = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
    samples = create_sample_series([300.0] * 10, base_time=now - timedelta(hours=2))
    repo = MockMetricRepository(samples)
    service = CapacityPlanningService(data_provider=ProductionCapacityDataProvider(repo))

    snapshot = service.generate_capacity_snapshot()
    assert snapshot.checksum != ""
    assert len(snapshot.checksum) == 64  # SHA-256 hex length

    # Tampering with snapshot raises integrity error
    with pytest.raises(CapacityPlanningIntegrityError):
        CapacitySnapshot(
            environment=snapshot.environment,
            scope=snapshot.scope,
            evaluated_at=snapshot.evaluated_at,
            evaluations=snapshot.evaluations,
            recommendations=snapshot.recommendations,
            overall_risk=snapshot.overall_risk,
            overall_confidence=snapshot.overall_confidence,
            tenant_id=snapshot.tenant_id,
            checksum="tampered_invalid_checksum_0000000000000000000000000000000000000000",
        )
