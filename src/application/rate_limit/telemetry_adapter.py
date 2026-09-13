"""
Adaptador de telemetría P.11 hacia Monitoreo de Producción (P.7) y Alertas (P.8).
"""

from typing import Optional
from src.domain.rate_limit.ports import RateLimitTelemetryPort
from src.domain.rate_limit.models import RateLimitDecision, RateLimitRequest, RateLimitStatus
from src.domain.monitoring.models import (
    MetricSample,
    MetricType,
    MetricUnit,
    MonitoringScope,
    resolve_environment,
)
from src.domain.monitoring.ports import MetricRepositoryPort
from src.application.monitoring.production_monitoring_service import ProductionMonitoringService


class MonitoringRateLimitTelemetryAdapter(RateLimitTelemetryPort):
    """
    Emite métricas de rate limiting (RATE_LIMIT_ALLOW, RATE_LIMIT_DENIED)
    al repositorio o servicio de monitoreo P.7.
    """

    def __init__(
        self,
        metric_repository: Optional[MetricRepositoryPort] = None,
        monitoring_service: Optional[ProductionMonitoringService] = None,
    ) -> None:
        self._repository = metric_repository or (monitoring_service._repository if monitoring_service else None)

    def emit_rate_limit_fact(
        self,
        decision: RateLimitDecision,
        request: RateLimitRequest,
    ) -> None:
        if not self._repository:
            return

        try:
            now = decision.evaluated_at or request.requested_at
            env = resolve_environment(request.environment)
            labels = {
                "status": decision.status.value,
                "scope": decision.scope_violated.value if decision.scope_violated else "platform",
            }
            if request.provider:
                labels["provider"] = request.provider
            if request.model_id:
                labels["model_id"] = request.model_id

            sample = MetricSample(
                metric_type=MetricType.QUOTA_DENIAL_COUNT if decision.status == RateLimitStatus.DENY else MetricType.REQUEST_COUNT,
                value=1,
                timestamp=now,
                environment=env,
                scope=MonitoringScope.TENANT if request.tenant_id else MonitoringScope.PLATFORM,
                tenant_id=request.tenant_id,
                unit=MetricUnit.COUNT,
                labels=labels,
            )
            self._repository.record_sample(sample)
        except Exception:
            pass
