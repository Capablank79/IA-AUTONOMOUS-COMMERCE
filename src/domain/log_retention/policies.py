"""
Registro y Definición Centralizada de Políticas de Retención de Logs por Defecto (P.9).

Alineado con:
- P.2 Environment Separation (DEV, STAGING, PROD)
- K.1 Audit Trail Protection (Auditoría legal y forense separada e inmutable)
- P.7 Monitoring & P.8 Alerting (Protección de alertas vivas y ventanas de métricas)
"""

from typing import Dict, List, Mapping, Optional, Sequence, Tuple
from src.domain.deployment.models import ApplicationEnvironment, normalize_environment_name
from src.domain.log_retention.models import (
    RetentionClass,
    RetentionPolicy,
    LogRetentionPolicyError,
)
from src.domain.log_retention.ports import LogRetentionPolicyRegistryPort


# Defaults operativos centralizados por entorno y clase de log
DEFAULT_RETENTION_DAYS: Mapping[Tuple[ApplicationEnvironment, RetentionClass], int] = {
    # APPLICATION_LOG
    (ApplicationEnvironment.DEVELOPMENT, RetentionClass.APPLICATION_LOG): 7,
    (ApplicationEnvironment.STAGING, RetentionClass.APPLICATION_LOG): 14,
    (ApplicationEnvironment.PRODUCTION, RetentionClass.APPLICATION_LOG): 30,

    # ACCESS_LOG
    (ApplicationEnvironment.DEVELOPMENT, RetentionClass.ACCESS_LOG): 7,
    (ApplicationEnvironment.STAGING, RetentionClass.ACCESS_LOG): 14,
    (ApplicationEnvironment.PRODUCTION, RetentionClass.ACCESS_LOG): 90,

    # ERROR_LOG
    (ApplicationEnvironment.DEVELOPMENT, RetentionClass.ERROR_LOG): 14,
    (ApplicationEnvironment.STAGING, RetentionClass.ERROR_LOG): 30,
    (ApplicationEnvironment.PRODUCTION, RetentionClass.ERROR_LOG): 90,

    # TRACE_LOG (K.2 Agent Traces)
    (ApplicationEnvironment.DEVELOPMENT, RetentionClass.TRACE_LOG): 7,
    (ApplicationEnvironment.STAGING, RetentionClass.TRACE_LOG): 14,
    (ApplicationEnvironment.PRODUCTION, RetentionClass.TRACE_LOG): 30,

    # MONITORING_SAMPLE (P.7 Metrics)
    (ApplicationEnvironment.DEVELOPMENT, RetentionClass.MONITORING_SAMPLE): 3,
    (ApplicationEnvironment.STAGING, RetentionClass.MONITORING_SAMPLE): 7,
    (ApplicationEnvironment.PRODUCTION, RetentionClass.MONITORING_SAMPLE): 30,

    # ALERT_HISTORY (P.8 Production Alerts - Solo RESOLVED)
    (ApplicationEnvironment.DEVELOPMENT, RetentionClass.ALERT_HISTORY): 14,
    (ApplicationEnvironment.STAGING, RetentionClass.ALERT_HISTORY): 30,
    (ApplicationEnvironment.PRODUCTION, RetentionClass.ALERT_HISTORY): 180,

    # AUDIT_RECORD (K.1 Audit Trail - Protegido por defecto)
    (ApplicationEnvironment.DEVELOPMENT, RetentionClass.AUDIT_RECORD): 365,
    (ApplicationEnvironment.STAGING, RetentionClass.AUDIT_RECORD): 365,
    (ApplicationEnvironment.PRODUCTION, RetentionClass.AUDIT_RECORD): 2555,  # 7 años para compliance
}


class DefaultRetentionPolicyRegistry(LogRetentionPolicyRegistryPort):
    """
    Registro en memoria y configurable de políticas de retención con aislamiento estricto.
    """

    def __init__(self, overrides: Optional[Sequence[RetentionPolicy]] = None) -> None:
        self._overrides: Dict[Tuple[ApplicationEnvironment, RetentionClass, Optional[str]], RetentionPolicy] = {}
        if overrides:
            for p in overrides:
                self.register_policy(p)

    def register_policy(self, policy: RetentionPolicy) -> None:
        key = (policy.environment, policy.data_class, policy.tenant_id)
        self._overrides[key] = policy

    def get_policy(
        self,
        environment: ApplicationEnvironment,
        data_class: RetentionClass,
        tenant_id: Optional[str] = None,
    ) -> RetentionPolicy:
        env = normalize_environment_name(environment)
        if not isinstance(data_class, RetentionClass):
            try:
                data_class = RetentionClass(str(data_class).upper())
            except ValueError:
                raise LogRetentionPolicyError(f"Clase de retención no válida: {data_class}")

        # 1. Buscar override tenant-specific
        if tenant_id:
            specific_key = (env, data_class, tenant_id)
            if specific_key in self._overrides:
                return self._overrides[specific_key]

        # 2. Buscar override general para el entorno
        general_key = (env, data_class, None)
        if general_key in self._overrides:
            return self._overrides[general_key]

        # 3. Retornar default centralizado
        default_days = DEFAULT_RETENTION_DAYS.get((env, data_class), 30)
        is_audit = (data_class == RetentionClass.AUDIT_RECORD)

        return RetentionPolicy(
            environment=env,
            data_class=data_class,
            retention_days=default_days,
            enabled=True,
            purge_batch_size=500,
            protect_latest=5 if is_audit else 0,
            dry_run=False,
            tenant_id=tenant_id,
            is_audit_protected=is_audit,
        )

    def list_policies(
        self,
        environment: Optional[ApplicationEnvironment] = None,
        tenant_id: Optional[str] = None,
    ) -> Sequence[RetentionPolicy]:
        policies = []
        environments = [normalize_environment_name(environment)] if environment else list(ApplicationEnvironment)
        classes = list(RetentionClass)

        for env in environments:
            for d_class in classes:
                policies.append(self.get_policy(env, d_class, tenant_id=tenant_id))
        return tuple(policies)
