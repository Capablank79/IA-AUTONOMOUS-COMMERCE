"""
Servicio de Aplicación para SaaS Observability (Hito O.12 — SaaS / Platformization).

Responsabilidades:
- Implementar TenantObservabilityServicePort.
- Proyectar métricas operativas tenant-scoped sin duplicar hechos de K.1, K.2, K.3, O.5, O.6, O.7, O.9, N.11.
- Generar y consolidar TenantOperationalSnapshot de forma determinista y reproducible.
- Evaluar reglas de salud operacional (HEALTHY, DEGRADED, UNHEALTHY, UNKNOWN) preservando UNKNOWN != ZERO.
- Gestionar ciclo de vida de OperationalAlerts (deduplicación por ventana, acknowledge, resolve).
- Proteger privacidad y sanitización (N.9) proscribiendo prompts, credenciales, PAN/CVV y CoT.
- Precedencia de thresholds: platform security limits > tenant configuration preferences.
"""

from collections import defaultdict
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import logging
from typing import Optional, List, Dict, Any, Mapping, Sequence, Tuple
import uuid

from src.domain.security.models import validate_safe_identifier, sanitize_security_data
from src.domain.tenant.models import TenantContext, CrossTenantAccessError
from src.domain.tenant.guard import CrossTenantGuard
from src.domain.reliability.ports import ClockPort
from src.domain.saas_observability.models import (
    ObservabilityScope,
    MetricType,
    MetricUnit,
    TenantHealthStatus,
    AlertSeverity,
    AlertStatus,
    OperationalAlertType,
    ObservabilityMetric,
    OperationalAlert,
    TenantOperationalSnapshot,
    ObservabilityError,
    ObservabilityAccessError,
)
from src.domain.saas_observability.ports import (
    OperationalAlertRepositoryPort,
    TenantObservabilityServicePort,
)
from src.domain.agent_trace.ports import AgentTraceRepositoryPort
from src.domain.audit.ports import AuditRepositoryPort
from src.domain.audit.models import AuditRecordType
from src.domain.usage_metering.ports import UsageMeteringServicePort
from src.domain.usage_metering.models import UsageQuery, UsagePeriod, UsagePeriodType
from src.domain.quota_management.ports import QuotaPolicyRepositoryPort
from src.domain.billing.ports import SubscriptionRepositoryPort
from src.domain.billing.models import SubscriptionStatus
from src.domain.emergency_stop.ports import EmergencyStopRepositoryPort
from src.domain.tenant_configuration.ports import TenantConfigurationRepositoryPort
from src.domain.tenant_configuration.models import ConfigurationScope


class TenantObservabilityService(TenantObservabilityServicePort):
    """
    Servicio central de observabilidad operacional para arquitecturas multi-tenant SaaS.
    """

    def __init__(
        self,
        alert_repository: OperationalAlertRepositoryPort,
        clock: Optional[ClockPort] = None,
        usage_metering_service: Optional[UsageMeteringServicePort] = None,
        agent_trace_repository: Optional[AgentTraceRepositoryPort] = None,
        audit_repository: Optional[AuditRepositoryPort] = None,
        quota_policy_repository: Optional[QuotaPolicyRepositoryPort] = None,
        subscription_repository: Optional[SubscriptionRepositoryPort] = None,
        emergency_stop_repository: Optional[EmergencyStopRepositoryPort] = None,
        tenant_config_repository: Optional[TenantConfigurationRepositoryPort] = None,
    ):
        self.alert_repository = alert_repository
        self.clock = clock
        self.usage_metering_service = usage_metering_service
        self.agent_trace_repository = agent_trace_repository
        self.audit_repository = audit_repository
        self.quota_policy_repository = quota_policy_repository
        self.subscription_repository = subscription_repository
        self.emergency_stop_repository = emergency_stop_repository
        self.tenant_config_repository = tenant_config_repository

    def _get_now(self) -> datetime:
        if self.clock is not None:
            return self.clock.now()
        return datetime.now(timezone.utc)

    def _get_tenant_threshold_preference(self, tenant_id: str, key: str, default: Any) -> Any:
        if self.tenant_config_repository:
            try:
                cfg = self.tenant_config_repository.get_current_configuration(
                    tenant_id=tenant_id,
                    key=key,
                    scope=ConfigurationScope.TENANT,
                )
                if cfg:
                    return cfg.value.raw_value
            except Exception:
                pass
        return default

    def get_tenant_snapshot(
        self,
        tenant_id: str,
        organization_id: Optional[str] = None,
        window_seconds: int = 3600,
    ) -> TenantOperationalSnapshot:
        validate_safe_identifier(tenant_id, "tenant_id")
        if organization_id:
            validate_safe_identifier(organization_id, "organization_id")

        now = self._get_now()
        start_time = now - timedelta(seconds=window_seconds)

        # 1. Recolectar métricas de Usage Metering (O.6)
        request_count: Optional[int] = None
        error_rate: Optional[float] = None
        total_tokens: Optional[int] = None
        total_cost_usd: Optional[Decimal] = None
        cache_hit_rate: Optional[float] = None
        metrics: Dict[str, ObservabilityMetric] = {}

        if self.usage_metering_service:
            try:
                context = TenantContext(tenant_id=tenant_id)
                query = UsageQuery(
                    tenant_id=tenant_id,
                    organization_id=organization_id,
                    period=UsagePeriod(start_time=start_time, end_time=now, period_type=UsagePeriodType.CUSTOM),
                )
                aggregate = self.usage_metering_service.aggregate_usage(context, query)
                request_count = aggregate.total_requests
                total_tokens = aggregate.total_tokens if aggregate.unknown_token_events_count == 0 or aggregate.total_tokens > 0 else None
                total_cost_usd = aggregate.total_actual_cost if aggregate.unknown_actual_cost_events_count == 0 else aggregate.total_estimated_cost

                if aggregate.total_requests > 0:
                    error_rate = round(aggregate.failed_requests / aggregate.total_requests, 4)
                    cache_hit_rate = round(aggregate.cached_requests / aggregate.total_requests, 4)
                else:
                    # Sin tráfico: error_rate y cache_hit_rate no son cero matemático, sino None / 0.0 explícito
                    error_rate = None
                    cache_hit_rate = None

                model_request_count = None
                if aggregate.breakdown_by_model:
                    model_request_count = sum(m.total_requests for m in aggregate.breakdown_by_model.values())

                metrics[MetricType.REQUEST_COUNT.value] = ObservabilityMetric(
                    metric_type=MetricType.REQUEST_COUNT,
                    value=aggregate.total_requests,
                    unit=MetricUnit.COUNT,
                    sample_count=aggregate.total_requests,
                    evaluated_at=now,
                )
                metrics[MetricType.SUCCESS_COUNT.value] = ObservabilityMetric(
                    metric_type=MetricType.SUCCESS_COUNT,
                    value=aggregate.successful_requests,
                    unit=MetricUnit.COUNT,
                    sample_count=aggregate.total_requests,
                    evaluated_at=now,
                )
                metrics[MetricType.FAILURE_COUNT.value] = ObservabilityMetric(
                    metric_type=MetricType.FAILURE_COUNT,
                    value=aggregate.failed_requests,
                    unit=MetricUnit.COUNT,
                    sample_count=aggregate.total_requests,
                    evaluated_at=now,
                )
                if error_rate is not None:
                    metrics[MetricType.ERROR_RATE.value] = ObservabilityMetric(
                        metric_type=MetricType.ERROR_RATE,
                        value=error_rate,
                        unit=MetricUnit.RATIO,
                        sample_count=aggregate.total_requests,
                        evaluated_at=now,
                    )
                if total_tokens is not None:
                    metrics[MetricType.TOKEN_USAGE.value] = ObservabilityMetric(
                        metric_type=MetricType.TOKEN_USAGE,
                        value=total_tokens,
                        unit=MetricUnit.TOKENS,
                        sample_count=aggregate.total_requests,
                        evaluated_at=now,
                    )
                if total_cost_usd is not None:
                    metrics[MetricType.AI_COST.value] = ObservabilityMetric(
                        metric_type=MetricType.AI_COST,
                        value=total_cost_usd,
                        unit=MetricUnit.CURRENCY,
                        sample_count=aggregate.total_requests,
                        evaluated_at=now,
                    )
                if cache_hit_rate is not None:
                    metrics[MetricType.CACHE_HIT_RATE.value] = ObservabilityMetric(
                        metric_type=MetricType.CACHE_HIT_RATE,
                        value=cache_hit_rate,
                        unit=MetricUnit.RATIO,
                        sample_count=aggregate.total_requests,
                        evaluated_at=now,
                    )
                if model_request_count is not None:
                    metrics[MetricType.MODEL_REQUEST_COUNT.value] = ObservabilityMetric(
                        metric_type=MetricType.MODEL_REQUEST_COUNT,
                        value=model_request_count,
                        unit=MetricUnit.COUNT,
                        sample_count=model_request_count,
                        evaluated_at=now,
                    )
            except Exception as e:
                logging.warning(f"Error querying usage metering for tenant {tenant_id}: {e}")

        # 2. Recolectar latencias desde Agent Trace (K.2)
        # Nota: AgentTraceRepositoryPort no posee tenant_id canónico ni list_by_tenant,
        # por lo que preserva latencia como None (UNKNOWN != ZERO) salvo fuente tenant-safe.
        avg_latency_ms: Optional[float] = None
        p95_latency_ms: Optional[float] = None

        # 3. Recolectar estado de cuotas (O.7)
        quota_status = "UNKNOWN"
        if self.quota_policy_repository:
            try:
                policy = self.quota_policy_repository.get_policy(tenant_id)
                if policy:
                    quota_status = "CONFIGURED"
                else:
                    quota_status = "UNCONFIGURED"
            except Exception:
                quota_status = "UNKNOWN"

        # 4. Recolectar estado de billing (O.9)
        billing_status = "UNKNOWN"
        if self.subscription_repository:
            try:
                sub = self.subscription_repository.get_active_subscription(tenant_id, current_time=now)
                if sub:
                    billing_status = sub.status.value
                else:
                    billing_status = "NO_SUBSCRIPTION"
            except Exception:
                billing_status = "UNKNOWN"

        # 5. Evaluar y obtener alertas activas
        active_alerts_seq = self.evaluate_tenant_health_and_alerts(
            tenant_id=tenant_id,
            organization_id=organization_id,
            window_seconds=window_seconds,
        )
        active_alerts_tuple = tuple(active_alerts_seq)

        # 6. Determinar Health Status determinísticamente
        # Reglas:
        # UNHEALTHY: Alerta CRITICAL activa o emergency stop activo o billing PAST_DUE persistente
        # DEGRADED: Alerta HIGH/WARNING activa o error_rate > 0.05
        # HEALTHY: Hay evidencia de tráfico y cero alertas HIGH/CRITICAL y error_rate <= 0.05
        # UNKNOWN: Insuficiente evidencia (cero requests y sin alertas)
        health_status = TenantHealthStatus.UNKNOWN

        has_critical = any(a.severity == AlertSeverity.CRITICAL for a in active_alerts_tuple)
        has_high_warning = any(a.severity in (AlertSeverity.HIGH, AlertSeverity.WARNING) for a in active_alerts_tuple)

        if has_critical:
            health_status = TenantHealthStatus.UNHEALTHY
        elif has_high_warning or (error_rate is not None and error_rate > 0.10):
            health_status = TenantHealthStatus.DEGRADED
        elif request_count is not None and request_count > 0:
            if error_rate is not None and error_rate > 0.05:
                health_status = TenantHealthStatus.DEGRADED
            else:
                health_status = TenantHealthStatus.HEALTHY
        elif billing_status == SubscriptionStatus.ACTIVE.value:
            # Sin tráfico pero suscripción activa y sin anomalías -> HEALTHY
            health_status = TenantHealthStatus.HEALTHY
        else:
            health_status = TenantHealthStatus.UNKNOWN

        return TenantOperationalSnapshot(
            tenant_id=tenant_id,
            health_status=health_status,
            window_seconds=window_seconds,
            evaluated_at=now,
            request_count=request_count,
            error_rate=error_rate,
            avg_latency_ms=avg_latency_ms,
            p95_latency_ms=p95_latency_ms,
            total_tokens=total_tokens,
            total_cost_usd=total_cost_usd,
            quota_status=quota_status,
            billing_status=billing_status,
            active_alerts=active_alerts_tuple,
            metrics=metrics,
            organization_id=organization_id,
        )

    def evaluate_tenant_health_and_alerts(
        self,
        tenant_id: str,
        organization_id: Optional[str] = None,
        window_seconds: int = 3600,
    ) -> Sequence[OperationalAlert]:
        validate_safe_identifier(tenant_id, "tenant_id")
        now = self._get_now()
        start_time = now - timedelta(seconds=window_seconds)

        generated_or_active_alerts: List[OperationalAlert] = []

        # A. Señal de Emergency Stop (N.11)
        if self.emergency_stop_repository:
            try:
                active_stops = self.emergency_stop_repository.list_active_records(now)
                # Filtrar si hay stop global o que afecte al tenant
                tenant_stopped = any(
                    s.state.value == "ACTIVE" and (s.scope.value == "GLOBAL" or s.target_id == tenant_id)
                    for s in active_stops
                )
                dedup_key = f"stop_active:{tenant_id}"
                existing_alert = self.alert_repository.get_active_alert_by_deduplication_key(tenant_id, dedup_key)

                if tenant_stopped:
                    if not existing_alert:
                        alert = OperationalAlert(
                            alert_id=f"alt_stop_{uuid.uuid4().hex[:12]}",
                            tenant_id=tenant_id,
                            alert_type=OperationalAlertType.EMERGENCY_STOP_ACTIVE,
                            severity=AlertSeverity.CRITICAL,
                            status=AlertStatus.ACTIVE,
                            summary=f"Emergency stop is currently active for tenant {tenant_id}",
                            details={"tenant_id": tenant_id, "reason": "Operational safety containment"},
                            triggered_at=now,
                            deduplication_key=dedup_key,
                            organization_id=organization_id,
                        )
                        self.alert_repository.save_alert(alert)
                        generated_or_active_alerts.append(alert)
                    else:
                        generated_or_active_alerts.append(existing_alert)
                elif existing_alert:
                    # Resolver alerta si el stop ya no está activo
                    self.resolve_alert(tenant_id, existing_alert.alert_id, actor_id="system", reason="Emergency stop deactivated")
            except Exception as e:
                logging.warning(f"Error checking emergency stops for tenant {tenant_id}: {e}")

        # B. Señal de Billing Past Due (O.9)
        if self.subscription_repository:
            try:
                sub = self.subscription_repository.get_active_subscription(tenant_id, current_time=now)
                dedup_key = f"billing_past_due:{tenant_id}"
                existing_alert = self.alert_repository.get_active_alert_by_deduplication_key(tenant_id, dedup_key)

                if sub and hasattr(sub, "status") and sub.status.value == "PAST_DUE":
                    if not existing_alert:
                        alert = OperationalAlert(
                            alert_id=f"alt_bill_{uuid.uuid4().hex[:12]}",
                            tenant_id=tenant_id,
                            alert_type=OperationalAlertType.BILLING_PAST_DUE,
                            severity=AlertSeverity.HIGH,
                            status=AlertStatus.ACTIVE,
                            summary=f"Tenant {tenant_id} subscription is PAST_DUE",
                            details={"plan_id": sub.plan_id, "status": sub.status.value},
                            triggered_at=now,
                            deduplication_key=dedup_key,
                            organization_id=organization_id,
                        )
                        self.alert_repository.save_alert(alert)
                        generated_or_active_alerts.append(alert)
                    else:
                        generated_or_active_alerts.append(existing_alert)
                elif existing_alert:
                    self.resolve_alert(tenant_id, existing_alert.alert_id, actor_id="system", reason="Subscription restored")
            except Exception as e:
                logging.warning(f"Error evaluating billing status for tenant {tenant_id}: {e}")

        # C. Señal de Tasa de Error / Proveedor (O.6)
        if self.usage_metering_service:
            try:
                context = TenantContext(tenant_id=tenant_id)
                query = UsageQuery(
                    tenant_id=tenant_id,
                    organization_id=organization_id,
                    period=UsagePeriod(start_time=start_time, end_time=now, period_type=UsagePeriodType.CUSTOM),
                )
                agg = self.usage_metering_service.aggregate_usage(context, query)
                dedup_key = f"high_error_rate:{tenant_id}"
                existing_alert = self.alert_repository.get_active_alert_by_deduplication_key(tenant_id, dedup_key)

                # Umbral de error rate (Platform safety threshold = 0.20, default = 0.10)
                if agg.total_requests >= 5 and (agg.failed_requests / agg.total_requests) >= 0.20:
                    err_pct = round((agg.failed_requests / agg.total_requests) * 100, 1)
                    if not existing_alert:
                        alert = OperationalAlert(
                            alert_id=f"alt_err_{uuid.uuid4().hex[:12]}",
                            tenant_id=tenant_id,
                            alert_type=OperationalAlertType.HIGH_ERROR_RATE,
                            severity=AlertSeverity.HIGH,
                            status=AlertStatus.ACTIVE,
                            summary=f"High error rate detected ({err_pct}%) for tenant {tenant_id}",
                            details={"total_requests": agg.total_requests, "failed_requests": agg.failed_requests, "error_rate_pct": err_pct},
                            triggered_at=now,
                            deduplication_key=dedup_key,
                            organization_id=organization_id,
                        )
                        self.alert_repository.save_alert(alert)
                        generated_or_active_alerts.append(alert)
                    else:
                        generated_or_active_alerts.append(existing_alert)
                elif existing_alert:
                    self.resolve_alert(tenant_id, existing_alert.alert_id, actor_id="system", reason="Error rate normalized")
            except Exception as e:
                logging.warning(f"Error evaluating error rate for tenant {tenant_id}: {e}")

        # D. Otras alertas activas en el repositorio no recreadas en este ciclo
        existing_active = self.alert_repository.list_alerts(tenant_id=tenant_id, status=AlertStatus.ACTIVE, organization_id=organization_id)
        current_ids = {a.alert_id for a in generated_or_active_alerts}
        for a in existing_active:
            if a.alert_id not in current_ids:
                generated_or_active_alerts.append(a)

        return generated_or_active_alerts

    def list_alerts(
        self,
        tenant_id: str,
        status: Optional[AlertStatus] = None,
        organization_id: Optional[str] = None,
        limit: int = 100,
    ) -> Sequence[OperationalAlert]:
        validate_safe_identifier(tenant_id, "tenant_id")
        if organization_id:
            validate_safe_identifier(organization_id, "organization_id")
        return self.alert_repository.list_alerts(
            tenant_id=tenant_id,
            status=status,
            organization_id=organization_id,
            limit=limit,
        )

    def acknowledge_alert(
        self, tenant_id: str, alert_id: str, actor_id: str
    ) -> OperationalAlert:
        validate_safe_identifier(tenant_id, "tenant_id")
        validate_safe_identifier(alert_id, "alert_id")
        alert = self.alert_repository.get_alert_by_id(tenant_id, alert_id)
        if not alert:
            raise ObservabilityError(f"Alert {alert_id} not found for tenant {tenant_id}")
        if alert.status != AlertStatus.ACTIVE:
            return alert

        now = self._get_now()
        updated_alert = OperationalAlert(
            alert_id=alert.alert_id,
            tenant_id=alert.tenant_id,
            alert_type=alert.alert_type,
            severity=alert.severity,
            status=AlertStatus.ACKNOWLEDGED,
            summary=alert.summary,
            details=dict(alert.details),
            triggered_at=alert.triggered_at,
            deduplication_key=alert.deduplication_key,
            resolved_at=alert.resolved_at,
            acknowledged_at=now,
            organization_id=alert.organization_id,
        )
        return self.alert_repository.save_alert(updated_alert)

    def resolve_alert(
        self, tenant_id: str, alert_id: str, actor_id: str, reason: str = ""
    ) -> OperationalAlert:
        validate_safe_identifier(tenant_id, "tenant_id")
        validate_safe_identifier(alert_id, "alert_id")
        alert = self.alert_repository.get_alert_by_id(tenant_id, alert_id)
        if not alert:
            raise ObservabilityError(f"Alert {alert_id} not found for tenant {tenant_id}")
        if alert.status == AlertStatus.RESOLVED:
            return alert

        now = self._get_now()
        updated_details = dict(alert.details)
        if reason:
            updated_details["resolution_reason"] = reason
        updated_details["resolved_by"] = actor_id

        updated_alert = OperationalAlert(
            alert_id=alert.alert_id,
            tenant_id=alert.tenant_id,
            alert_type=alert.alert_type,
            severity=alert.severity,
            status=AlertStatus.RESOLVED,
            summary=alert.summary,
            details=updated_details,
            triggered_at=alert.triggered_at,
            deduplication_key=alert.deduplication_key,
            resolved_at=now,
            acknowledged_at=alert.acknowledged_at,
            organization_id=alert.organization_id,
        )
        return self.alert_repository.save_alert(updated_alert)
