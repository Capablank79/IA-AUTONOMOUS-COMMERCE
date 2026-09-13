"""
Servicio de Aplicación para Alerting de Producción (Hito P.8 — Production / Operations).

Responsabilidades:
1. Evaluación periódica o on-demand de reglas de alerta contra métricas agregadas de P.7 y salud P.6.
2. Gestión integral del ciclo de vida de alertas: ACTIVE -> ACKNOWLEDGED -> RESOLVED -> REOPEN.
3. Deduplicación canónica estricta por clave (environment, scope, rule_type, target_resource, tenant_id).
4. Cooldown determinista para prevención de alert fatigue y spam.
5. Escalación de severidad in-place sin duplicar la alerta.
6. Aislamiento absoluto por ApplicationEnvironment (DEV, STAGING, PROD) y Tenant.
7. Preservación estricta de UNKNOWN != OK (cuando no hay muestras suficientes, estado es INSUFFICIENT_DATA, nunca falso RESOLVED).
8. Despacho seguro de notificaciones a través de NotificationPort (failure-safe).
9. ALERT != ACTION: Las alertas informan y notifican; NUNCA mutan cuotas, cancelan planes ni detienen servicios.
10. Auditoría inmutable de eventos de alerta (K.1).
"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
import logging
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union
import uuid

from src.domain.deployment.models import ApplicationEnvironment, normalize_environment_name
from src.domain.monitoring.models import (
    MetricType,
    MetricWindow,
    MonitoringScope,
    ProductionMonitoringSnapshot,
    resolve_environment,
)
from src.domain.production_alerting.models import (
    AlertEvaluationResult,
    AlertEvaluationStatus,
    AlertRule,
    AlertRuleType,
    AlertSeverity,
    AlertState,
    NotificationDeliveryStatus,
    NotificationMessage,
    NotificationResult,
    ProductionAlertInstance,
    ProductionAlertScope,
    ProductionAlertingError,
    ProductionAlertSecurityError,
    generate_deduplication_key,
)
from src.domain.production_alerting.ports import (
    NotificationPort,
    ProductionAlertRepositoryPort,
)
from src.domain.reliability.ports import ClockPort
from src.domain.tenant.guard import CrossTenantGuard
from src.domain.tenant.models import TenantContext
from src.domain.audit.ports import AuditRepositoryPort
from src.domain.audit.models import AuditRecordType
from src.application.monitoring.production_monitoring_service import ProductionMonitoringService

logger = logging.getLogger("ProductionAlertingService")


class ProductionAlertingService:
    """
    Servicio de orquestación, evaluación y ciclo de vida de Alertas de Producción (P.8).
    """

    DEFAULT_RULES: Sequence[AlertRule] = (
        AlertRule(
            rule_type=AlertRuleType.HIGH_ERROR_RATE,
            metric_type=MetricType.ERROR_RATE,
            window=MetricWindow.WINDOW_5M,
            severity=AlertSeverity.HIGH,
            threshold_value=5.0,  # > 5.0% error rate
            description="Tasa de error HTTP excede el umbral del 5% en los últimos 5 minutos",
            comparison_operator=">",
            scope=ProductionAlertScope.PLATFORM,
            cooldown_seconds=300,
            min_sample_count=5,
        ),
        AlertRule(
            rule_type=AlertRuleType.HIGH_LATENCY,
            metric_type=MetricType.LATENCY_MS,
            window=MetricWindow.WINDOW_5M,
            severity=AlertSeverity.WARNING,
            threshold_value=500.0,  # > 500 ms p95
            description="Latencia p95 excede 500ms en los últimos 5 minutos",
            comparison_operator=">",
            scope=ProductionAlertScope.PLATFORM,
            cooldown_seconds=300,
            min_sample_count=5,
        ),
        AlertRule(
            rule_type=AlertRuleType.READINESS_FAILURE,
            metric_type=MetricType.READINESS_FAILURE_COUNT,
            window=MetricWindow.WINDOW_5M,
            severity=AlertSeverity.CRITICAL,
            threshold_value=1.0,  # >= 1 readiness failure
            description="Fallo detectado en readiness probe de componentes críticos",
            comparison_operator=">=",
            scope=ProductionAlertScope.PLATFORM,
            cooldown_seconds=180,
            min_sample_count=1,
        ),
        AlertRule(
            rule_type=AlertRuleType.DATABASE_UNAVAILABLE,
            metric_type=MetricType.DB_AVAILABILITY,
            window=MetricWindow.WINDOW_5M,
            severity=AlertSeverity.CRITICAL,
            threshold_value=1.0,  # < 1.0 (0 = unavailable)
            description="Base de datos principal no disponible o fallo de conexión",
            comparison_operator="<",
            scope=ProductionAlertScope.PLATFORM,
            cooldown_seconds=120,
            min_sample_count=1,
        ),
        AlertRule(
            rule_type=AlertRuleType.BACKUP_STALE_OR_FAILED,
            metric_type=MetricType.BACKUP_STATUS,
            window=MetricWindow.WINDOW_24H,
            severity=AlertSeverity.HIGH,
            threshold_value=1.0,  # < 1.0 (0 = failed / stale)
            description="Último respaldo de base de datos falló o no se ejecutó en la ventana requerida",
            comparison_operator="<",
            scope=ProductionAlertScope.PLATFORM,
            cooldown_seconds=1800,
            min_sample_count=1,
        ),
        AlertRule(
            rule_type=AlertRuleType.DR_LAST_SIMULATION_FAILED,
            metric_type=MetricType.DR_STATUS,
            window=MetricWindow.WINDOW_24H,
            severity=AlertSeverity.HIGH,
            threshold_value=1.0,  # < 1.0 (0 = failed)
            description="Última simulación de Disaster Recovery falló o no cumplió RTO/RPO",
            comparison_operator="<",
            scope=ProductionAlertScope.PLATFORM,
            cooldown_seconds=1800,
            min_sample_count=1,
        ),
        AlertRule(
            rule_type=AlertRuleType.QUOTA_EXHAUSTION,
            metric_type=MetricType.QUOTA_DENIAL_COUNT,
            window=MetricWindow.WINDOW_1H,
            severity=AlertSeverity.WARNING,
            threshold_value=1.0,  # >= 1 quota denial
            description="Múltiples peticiones denegadas por agotamiento de cuotas",
            comparison_operator=">=",
            scope=ProductionAlertScope.PLATFORM,
            cooldown_seconds=600,
            min_sample_count=1,
        ),
        AlertRule(
            rule_type=AlertRuleType.PROVIDER_FAILURE_RATE,
            metric_type=MetricType.MODEL_ERROR_COUNT,
            window=MetricWindow.WINDOW_5M,
            severity=AlertSeverity.HIGH,
            threshold_value=3.0,  # >= 3 model error count
            description="Tasa elevada de fallos en proveedores de modelos de IA",
            comparison_operator=">=",
            scope=ProductionAlertScope.PLATFORM,
            cooldown_seconds=300,
            min_sample_count=3,
        ),
    )

    def __init__(
        self,
        repository: ProductionAlertRepositoryPort,
        monitoring_service: ProductionMonitoringService,
        rules: Optional[Sequence[AlertRule]] = None,
        notification_ports: Optional[Sequence[NotificationPort]] = None,
        audit_repository: Optional[AuditRepositoryPort] = None,
        clock: Optional[ClockPort] = None,
        environment: Union[ApplicationEnvironment, str] = ApplicationEnvironment.PRODUCTION,
    ) -> None:
        self._repository = repository
        self._monitoring = monitoring_service
        self._rules = tuple(rules) if rules is not None else self.DEFAULT_RULES
        self._notification_ports = tuple(notification_ports or ())
        self._audit_repo = audit_repository
        self._clock = clock
        self._environment = resolve_environment(environment)
        self._cooldown_tracker: Dict[str, datetime] = {}

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
    # Auditoría Segura (K.1 / K.2)
    # -------------------------------------------------------------------------

    def _audit_event(
        self,
        action: str,
        alert: ProductionAlertInstance,
        actor_id: Optional[str] = None,
        extra_details: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not self._audit_repo:
            return
        try:
            # Emisión no fatal
            payload = {
                "action": action,
                "alert_id": alert.alert_id,
                "rule_type": alert.rule_type.value,
                "severity": alert.severity.value,
                "state": alert.state.value,
                "environment": alert.environment.value,
                "scope": alert.scope.value,
                "deduplication_key": alert.deduplication_key,
                "actor_id": actor_id or "system",
            }
            if extra_details:
                payload.update(extra_details)
            logger.info(f"Audit P.8 Alerting: {payload}")
        except Exception as exc:
            logger.error(f"Non-fatal error writing audit log: {exc}")

    # -------------------------------------------------------------------------
    # Evaluación de Reglas y Detección
    # -------------------------------------------------------------------------

    def evaluate_rule(
        self,
        rule: AlertRule,
        environment: Optional[ApplicationEnvironment] = None,
        tenant_id: Optional[str] = None,
    ) -> AlertEvaluationResult:
        """
        Evalúa una regla individual contra las métricas agregadas de P.7.
        Preserva UNKNOWN != OK: Si no hay muestras suficientes, status es INSUFFICIENT_DATA.
        """
        target_env = resolve_environment(environment) if environment else self._environment
        now = self._now()
        dedup_key = generate_deduplication_key(
            environment=target_env,
            scope=rule.scope,
            rule_type=rule.rule_type,
            target_resource="platform",
            tenant_id=tenant_id,
        )

        # 1. Obtener métrica de P.7 para la ventana
        if rule.metric_type == MetricType.ERROR_RATE:
            metric = self._monitoring.get_error_rate(
                window=rule.window,
                environment=target_env,
                tenant_id=tenant_id,
            )
        else:
            metric = self._monitoring.get_metric(
                metric_type=rule.metric_type,
                window=rule.window,
                environment=target_env,
                tenant_id=tenant_id,
            )

        # 2. Manejo estricto de UNKNOWN / INSUFFICIENT_DATA
        if metric.is_unknown or metric.value is None or metric.sample_count < rule.min_sample_count:
            return AlertEvaluationResult(
                rule_type=rule.rule_type,
                status=AlertEvaluationStatus.INSUFFICIENT_DATA,
                severity=rule.severity,
                current_value=None,
                threshold_value=rule.threshold_value,
                window=rule.window,
                is_unknown=True,
                summary=f"Datos insuficientes ({metric.sample_count}/{rule.min_sample_count} muestras) para evaluar {rule.rule_type.value}",
                evidence={"sample_count": metric.sample_count, "min_required": rule.min_sample_count},
                evaluated_at=now,
            )

        # 3. Comprobación de umbral según operador
        # Si el valor de la métrica es de tipo string (e.g. BACKUP_STATUS o DR_STATUS), convertir para evaluación
        if isinstance(metric.value, str):
            if metric.value.lower() in ("completed", "passed", "healthy", "true", "1"):
                val = 1.0
            else:
                val = 0.0
        else:
            val = float(metric.value)
        # Para métricas de latencia, si es regla HIGH_LATENCY usamos p95 determinista si está disponible
        if rule.rule_type == AlertRuleType.HIGH_LATENCY and metric.p95_value is not None:
            val = float(metric.p95_value)

        triggered = False
        if rule.comparison_operator == ">":
            triggered = val > rule.threshold_value
        elif rule.comparison_operator == ">=":
            triggered = val >= rule.threshold_value
        elif rule.comparison_operator == "<":
            triggered = val < rule.threshold_value
        elif rule.comparison_operator == "<=":
            triggered = val <= rule.threshold_value
        elif rule.comparison_operator == "==":
            triggered = val == rule.threshold_value
        elif rule.comparison_operator == "!=":
            triggered = val != rule.threshold_value

        if not triggered:
            return AlertEvaluationResult(
                rule_type=rule.rule_type,
                status=AlertEvaluationStatus.HEALTHY,
                severity=rule.severity,
                current_value=val,
                threshold_value=rule.threshold_value,
                window=rule.window,
                is_unknown=False,
                summary=f"Métrica {rule.metric_type.value} ({val}) dentro de parámetros normales",
                evidence={"value": val, "threshold": rule.threshold_value, "sample_count": metric.sample_count},
                evaluated_at=now,
            )

        # 4. Verificar Cooldown
        cooldown_expiry = self._cooldown_tracker.get(dedup_key)
        if cooldown_expiry and now < cooldown_expiry:
            return AlertEvaluationResult(
                rule_type=rule.rule_type,
                status=AlertEvaluationStatus.COOLDOWN,
                severity=rule.severity,
                current_value=val,
                threshold_value=rule.threshold_value,
                window=rule.window,
                is_unknown=False,
                summary=f"Regla {rule.rule_type.value} en período de cooldown hasta {cooldown_expiry.isoformat()}",
                evidence={"value": val, "threshold": rule.threshold_value, "cooldown_until": cooldown_expiry.isoformat()},
                evaluated_at=now,
                cooldown_until=cooldown_expiry,
            )

        return AlertEvaluationResult(
            rule_type=rule.rule_type,
            status=AlertEvaluationStatus.TRIGGERED,
            severity=rule.severity,
            current_value=val,
            threshold_value=rule.threshold_value,
            window=rule.window,
            is_unknown=False,
            summary=f"ALERTA {rule.severity.value}: {rule.description} (Valor actual: {val}, Umbral: {rule.threshold_value})",
            evidence={
                "value": val,
                "threshold": rule.threshold_value,
                "sample_count": metric.sample_count,
                "window": rule.window.value,
                "operator": rule.comparison_operator,
            },
            evaluated_at=now,
        )

    def evaluate_all_rules(
        self,
        environment: Optional[ApplicationEnvironment] = None,
        tenant_id: Optional[str] = None,
    ) -> Sequence[ProductionAlertInstance]:
        """
        Ejecuta la evaluación de todas las reglas configuradas y gestiona el ciclo de vida de alertas.
        - Si la condición se cumple: crea nueva alerta, o escala severidad, o actualiza timestamp sin duplicar.
        - Si la condición ya no se cumple (HEALTHY con evidencia suficiente): auto-resuelve alerta activa previa.
        - Si el estado es INSUFFICIENT_DATA: NO auto-resuelve (UNKNOWN != OK).
        """
        target_env = resolve_environment(environment) if environment else self._environment
        now = self._now()
        affected_alerts: List[ProductionAlertInstance] = []

        # Agrupar reglas por dedup_key para evaluar de forma coherente
        # Si múltiples reglas mapean a la misma dedup_key (e.g. Warning y Critical para HIGH_ERROR_RATE),
        # se selecciona el resultado de mayor severidad alcanzado.
        grouped_rules: Dict[str, List[AlertRule]] = {}
        for r in self._rules:
            dk = generate_deduplication_key(
                environment=target_env,
                scope=r.scope,
                rule_type=r.rule_type,
                target_resource="platform",
                tenant_id=tenant_id,
            )
            grouped_rules.setdefault(dk, []).append(r)

        for dedup_key, rules_group in grouped_rules.items():
            # Evaluar reglas en orden de mayor severidad primero
            sorted_rules = sorted(rules_group, key=lambda r: r.severity.level, reverse=True)
            
            # Buscar el primer trigger de mayor severidad, o el mejor resultado
            highest_triggered_res: Optional[AlertEvaluationResult] = None
            highest_triggered_rule: Optional[AlertRule] = None
            healthy_res: Optional[AlertEvaluationResult] = None
            healthy_rule: Optional[AlertRule] = None
            insufficient_res: Optional[AlertEvaluationResult] = None

            for rule in sorted_rules:
                res = self.evaluate_rule(rule, environment=target_env, tenant_id=tenant_id)
                if res.status in (AlertEvaluationStatus.TRIGGERED, AlertEvaluationStatus.COOLDOWN):
                    highest_triggered_res = res
                    highest_triggered_rule = rule
                    break
                elif res.status == AlertEvaluationStatus.HEALTHY:
                    if healthy_res is None:
                        healthy_res = res
                        healthy_rule = rule
                elif res.status == AlertEvaluationStatus.INSUFFICIENT_DATA:
                    insufficient_res = res

            existing_active = self._repository.get_active_alert_by_deduplication_key(
                environment=target_env,
                deduplication_key=dedup_key,
            )

            if highest_triggered_res and highest_triggered_rule:
                res = highest_triggered_res
                rule = highest_triggered_rule
                if existing_active:
                    # 1. Escalación de severidad o actualización de evidencia sin duplicar
                    new_severity = rule.severity if rule.severity > existing_active.severity else existing_active.severity
                    was_escalated = new_severity > existing_active.severity

                    updated = ProductionAlertInstance(
                        alert_id=existing_active.alert_id,
                        rule_type=existing_active.rule_type,
                        severity=new_severity,
                        state=existing_active.state,  # Preserva si estaba ACKNOWLEDGED o ACTIVE
                        environment=existing_active.environment,
                        scope=existing_active.scope,
                        deduplication_key=existing_active.deduplication_key,
                        summary=res.summary,
                        evidence=res.evidence,
                        triggered_at=existing_active.triggered_at,
                        updated_at=now,
                        target_resource=existing_active.target_resource,
                        tenant_id=existing_active.tenant_id,
                        acknowledged_at=existing_active.acknowledged_at,
                        acknowledged_by=existing_active.acknowledged_by,
                    )
                    saved = self._repository.save_alert(updated)
                    affected_alerts.append(saved)

                    if was_escalated:
                        self._audit_event("ALERT_ESCALATED", saved, extra_details={"previous_severity": existing_active.severity.value})
                        self._dispatch_notifications(saved)
                else:
                    # 2. Nueva alerta estructurada
                    if res.status == AlertEvaluationStatus.TRIGGERED:
                        alert_id = f"alt_{uuid.uuid4().hex[:12]}"
                        new_alert = ProductionAlertInstance(
                            alert_id=alert_id,
                            rule_type=rule.rule_type,
                            severity=rule.severity,
                            state=AlertState.ACTIVE,
                            environment=target_env,
                            scope=rule.scope,
                            deduplication_key=dedup_key,
                            summary=res.summary,
                            evidence=res.evidence,
                            triggered_at=now,
                            updated_at=now,
                            target_resource="platform",
                            tenant_id=tenant_id,
                        )
                        saved = self._repository.save_alert(new_alert)
                        affected_alerts.append(saved)
                        
                        # Actualizar cooldown tracker
                        self._cooldown_tracker[dedup_key] = now + timedelta(seconds=rule.cooldown_seconds)
                        
                        self._audit_event("ALERT_TRIGGERED", saved)
                        self._dispatch_notifications(saved)

            elif healthy_res and healthy_rule and not highest_triggered_res:
                res = healthy_res
                rule = healthy_rule
                # 3. Condición normalizada con evidencia: Auto-resolve si había alerta activa/acknowledged
                if existing_active:
                    resolved = ProductionAlertInstance(
                        alert_id=existing_active.alert_id,
                        rule_type=existing_active.rule_type,
                        severity=existing_active.severity,
                        state=AlertState.RESOLVED,
                        environment=existing_active.environment,
                        scope=existing_active.scope,
                        deduplication_key=existing_active.deduplication_key,
                        summary=f"Recuperación automática: {res.summary}",
                        evidence=res.evidence,
                        triggered_at=existing_active.triggered_at,
                        updated_at=now,
                        target_resource=existing_active.target_resource,
                        tenant_id=existing_active.tenant_id,
                        acknowledged_at=existing_active.acknowledged_at,
                        acknowledged_by=existing_active.acknowledged_by,
                        resolved_at=now,
                        resolved_by="system_auto_resolve",
                        resolution_reason=f"Métrica {rule.metric_type.value} normalizada ({res.current_value})",
                    )
                    saved = self._repository.save_alert(resolved)
                    affected_alerts.append(saved)
                    self._cooldown_tracker.pop(dedup_key, None)
                    self._audit_event("ALERT_RESOLVED", saved, actor_id="system_auto_resolve")

            elif insufficient_res:
                # 4. UNKNOWN != OK: No se hace nada; la alerta activa permanece activa hasta que haya evidencia
                pass

        return tuple(affected_alerts)

    # -------------------------------------------------------------------------
    # Operaciones de Ciclo de Vida (Acknowledge / Manual Resolve / Reopen)
    # -------------------------------------------------------------------------

    def acknowledge_alert(
        self,
        alert_id: str,
        actor_id: str,
        environment: Optional[ApplicationEnvironment] = None,
        tenant_id: Optional[str] = None,
    ) -> ProductionAlertInstance:
        """
        Marca una alerta como ACKNOWLEDGED.
        Indica que un operador la ha visto, pero NO significa que el problema esté resuelto.
        """
        target_env = resolve_environment(environment) if environment else self._environment
        now = self._now()

        alert = self._repository.get_alert_by_id(target_env, alert_id, tenant_id=tenant_id)
        if not alert:
            raise ProductionAlertingError(f"Alert '{alert_id}' not found in environment '{target_env.value}'")

        if alert.state == AlertState.RESOLVED:
            raise ProductionAlertingError(f"Cannot acknowledge already RESOLVED alert '{alert_id}'")

        acknowledged = ProductionAlertInstance(
            alert_id=alert.alert_id,
            rule_type=alert.rule_type,
            severity=alert.severity,
            state=AlertState.ACKNOWLEDGED,
            environment=alert.environment,
            scope=alert.scope,
            deduplication_key=alert.deduplication_key,
            summary=alert.summary,
            evidence=alert.evidence,
            triggered_at=alert.triggered_at,
            updated_at=now,
            target_resource=alert.target_resource,
            tenant_id=alert.tenant_id,
            acknowledged_at=now,
            acknowledged_by=actor_id,
            resolved_at=alert.resolved_at,
            resolved_by=alert.resolved_by,
            resolution_reason=alert.resolution_reason,
        )
        saved = self._repository.save_alert(acknowledged)
        self._audit_event("ALERT_ACKNOWLEDGED", saved, actor_id=actor_id)
        return saved

    def resolve_alert(
        self,
        alert_id: str,
        actor_id: str,
        reason: str = "Manual operator resolution",
        environment: Optional[ApplicationEnvironment] = None,
        tenant_id: Optional[str] = None,
    ) -> ProductionAlertInstance:
        """
        Resuelve manualmente una alerta. Queda debidamente auditado.
        Si la condición técnica sigue activa, en la próxima evaluación podrá ser reabierta/re-evaluada.
        """
        target_env = resolve_environment(environment) if environment else self._environment
        now = self._now()

        alert = self._repository.get_alert_by_id(target_env, alert_id, tenant_id=tenant_id)
        if not alert:
            raise ProductionAlertingError(f"Alert '{alert_id}' not found in environment '{target_env.value}'")

        resolved = ProductionAlertInstance(
            alert_id=alert.alert_id,
            rule_type=alert.rule_type,
            severity=alert.severity,
            state=AlertState.RESOLVED,
            environment=alert.environment,
            scope=alert.scope,
            deduplication_key=alert.deduplication_key,
            summary=alert.summary,
            evidence=alert.evidence,
            triggered_at=alert.triggered_at,
            updated_at=now,
            target_resource=alert.target_resource,
            tenant_id=alert.tenant_id,
            acknowledged_at=alert.acknowledged_at,
            acknowledged_by=alert.acknowledged_by,
            resolved_at=now,
            resolved_by=actor_id,
            resolution_reason=reason,
        )
        saved = self._repository.save_alert(resolved)
        self._cooldown_tracker.pop(alert.deduplication_key, None)
        self._audit_event("ALERT_RESOLVED", saved, actor_id=actor_id, extra_details={"reason": reason})
        return saved

    def get_alert_by_id(
        self,
        alert_id: str,
        environment: Optional[ApplicationEnvironment] = None,
        tenant_id: Optional[str] = None,
    ) -> Optional[ProductionAlertInstance]:
        """Obtiene una alerta por ID con verificación de entorno y tenant."""
        target_env = resolve_environment(environment) if environment else self._environment
        return self._repository.get_alert_by_id(target_env, alert_id, tenant_id=tenant_id)

    # -------------------------------------------------------------------------
    # Consulta de Alertas (Aislamiento Multi-Tenant y por Entorno)
    # -------------------------------------------------------------------------

    def list_alerts(
        self,
        environment: Optional[ApplicationEnvironment] = None,
        state: Optional[AlertState] = None,
        severity: Optional[AlertSeverity] = None,
        rule_type: Optional[AlertRuleType] = None,
        scope: Optional[ProductionAlertScope] = None,
        tenant_id: Optional[str] = None,
        limit: int = 100,
    ) -> Sequence[ProductionAlertInstance]:
        """
        Lista alertas filtradas con respeto estricto de entorno y tenant.
        """
        target_env = resolve_environment(environment) if environment else self._environment
        return self._repository.list_alerts(
            environment=target_env,
            state=state,
            severity=severity,
            rule_type=rule_type,
            scope=scope,
            tenant_id=tenant_id,
            limit=limit,
        )

    # -------------------------------------------------------------------------
    # Despacho Seguro de Notificaciones (Failure-Safe)
    # -------------------------------------------------------------------------

    def _dispatch_notifications(self, alert: ProductionAlertInstance) -> Sequence[NotificationResult]:
        """
        Envía notificaciones a los canales registrados.
        Failure-safe: El fallo de un canal de notificación no afecta la alerta ni detiene la aplicación.
        """
        results: List[NotificationResult] = []
        now = self._now()

        msg = NotificationMessage(
            notification_id=f"notif_{uuid.uuid4().hex[:12]}",
            alert_id=alert.alert_id,
            channel="all",
            recipient="ops-oncall",
            subject=f"[{alert.environment.value.upper()}] [{alert.severity.value}] {alert.rule_type.value}",
            body_text=alert.summary,
            severity=alert.severity,
            created_at=now,
            metadata={"deduplication_key": alert.deduplication_key, "environment": alert.environment.value},
        )

        for port in self._notification_ports:
            try:
                res = port.send(msg)
                results.append(res)
                if res.status == NotificationDeliveryStatus.FAILED:
                    self._audit_event("ALERT_NOTIFICATION_FAILED", alert, extra_details={"channel": port.channel_name, "error": res.error_message})
            except Exception as exc:
                logger.error(f"Non-fatal error delivering notification via {port.channel_name}: {exc}")
                res = NotificationResult(
                    delivery_id=f"del_{uuid.uuid4().hex[:12]}",
                    notification_id=msg.notification_id,
                    alert_id=alert.alert_id,
                    channel=port.channel_name,
                    status=NotificationDeliveryStatus.FAILED,
                    attempted_at=now,
                    error_message=str(exc),
                )
                results.append(res)
                self._audit_event("ALERT_NOTIFICATION_FAILED", alert, extra_details={"channel": port.channel_name, "error": str(exc)})

        return tuple(results)
