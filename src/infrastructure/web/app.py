"""Fábrica canónica de la aplicación ASGI unificada de la plataforma (O.13).

Proporciona:
1. Endpoints de salud operacional:
   - `/health` (Health check estándar de la plataforma)
   - `/healthz` (Alias para orquestadores Kubernetes/Docker)
   - `/ready` / `/readyz` (Readiness check con verificación de almacenamiento persistente)
2. Integración de la Admin Console (O.10/O.11/O.12) y servicios expuestos según configuración validada.
3. Rutas seguras y desacopladas de secretos o tenants hardcodeados.
"""

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
from typing import Optional

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, Mount

from src.domain.deployment.models import DeploymentConfig, DeploymentEnvironment
from src.domain.reliability.ports import ClockPort
from src.infrastructure.deployment.config_validator import DeploymentConfigValidator
from src.infrastructure.web.admin_app import create_admin_app
from src.infrastructure.persistence.database.config import DatabaseConfig, DatabaseConnectionFactory
from scripts.db_migrate import check_schema_compatibility
from src.infrastructure.health.service import HealthCheckService
from src.domain.health.models import HealthStatus, DependencyClassification
from src.application.admin_console.admin_console_service import AdminConsoleService
from src.infrastructure.persistence.data.json.session_repository import JsonSaaSSessionRepository
from src.infrastructure.persistence.data.json.organization_repository import (
    JsonOrganizationRepository,
    JsonMembershipRepository,
)
from src.infrastructure.persistence.data.json.rbac_repository import (
    JsonRoleRepository,
    JsonRoleAssignmentRepository,
)
from src.infrastructure.persistence.data.json.plan_repository import (
    JsonPlanCatalogRepository,
    JsonPlanAssignmentRepository,
    InMemoryPlanCatalogRepository,
    InMemoryPlanAssignmentRepository,
)
from src.infrastructure.persistence.data.json.quota_repository import (
    JsonQuotaPolicyRepository,
    JsonQuotaReservationRepository,
    InMemoryQuotaPolicyRepository,
    InMemoryQuotaReservationRepository,
)
from src.infrastructure.persistence.data.json.usage_event_repository import (
    JsonUsageEventRepository,
    InMemoryUsageEventRepository,
)
from src.infrastructure.persistence.data.json.billing_repository import (
    JsonSubscriptionRepository,
    JsonInvoiceRepository,
    InMemorySubscriptionRepository,
    InMemoryInvoiceRepository,
)
from src.infrastructure.persistence.data.json.audit_repository import JsonAuditRepository
from src.infrastructure.persistence.data.json.agent_trace_repository import JsonAgentTraceRepository
from src.infrastructure.persistence.data.json.tenant_configuration_repository import JsonTenantConfigurationRepository
from src.infrastructure.persistence.data.json.operational_alert_repository import JsonOperationalAlertRepository
from src.application.organization.organization_service import (
    OrganizationService,
    OrganizationMembershipService,
)
from src.application.rbac.rbac_service import RBACService
from src.application.saas_authorization.saas_authorization_service import SaaSAuthorizationService
from src.application.usage_metering.usage_metering_service import UsageMeteringService
from src.application.quota_management.quota_management_service import QuotaManagementService
from src.application.plans.plan_entitlement_service import PlanEntitlementService
from src.application.billing.subscription_service import SubscriptionService
from src.application.tenant_configuration.tenant_configuration_service import TenantConfigurationService
from src.application.saas_observability.tenant_observability_service import TenantObservabilityService
from src.application.monitoring.production_monitoring_service import ProductionMonitoringService
from src.infrastructure.persistence.data.json.metric_repository import JsonMetricRepository, InMemoryMetricRepository
from src.infrastructure.web.monitoring_middleware import ProductionMonitoringMiddleware

logger = logging.getLogger(__name__)


class SystemClock(ClockPort):
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def sleep(self, seconds: float) -> None:
        import time
        time.sleep(seconds)


def check_storage_writable(data_dir: Path) -> bool:
    """Verifica que el directorio de almacenamiento persistente exista y sea escribible."""
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        probe_file = data_dir / ".health_probe"
        probe_file.write_text("ok", encoding="utf-8")
        probe_file.unlink(missing_ok=True)
        return True
    except Exception as exc:
        logger.error(f"Storage writable check failed on {data_dir}: {exc}")
        return False


def build_default_admin_service(data_dir: Path, clock: Optional[ClockPort] = None) -> AdminConsoleService:
    """Construye un AdminConsoleService cableado a los repositorios persistentes bajo DATA_DIR."""
    clock = clock or SystemClock()
    base_data = Path(data_dir)

    session_repo = JsonSaaSSessionRepository(base_data / "sessions")
    org_repo = JsonOrganizationRepository(base_data / "organizations")
    membership_repo = JsonMembershipRepository(base_data / "memberships")
    role_repo = JsonRoleRepository(base_data / "roles")
    assignment_repo = JsonRoleAssignmentRepository(base_data / "role_assignments")
    plan_repo = JsonPlanCatalogRepository(base_data)
    plan_assignment_repo = JsonPlanAssignmentRepository(base_data)
    quota_policy_repo = JsonQuotaPolicyRepository(base_data)
    quota_reservation_repo = JsonQuotaReservationRepository(base_data)
    usage_repo = JsonUsageEventRepository(base_data)
    sub_repo = JsonSubscriptionRepository(base_data)
    invoice_repo = JsonInvoiceRepository(base_data)
    audit_repo = JsonAuditRepository(base_data / "audit")
    trace_repo = JsonAgentTraceRepository(base_data / "traces")
    config_repo = JsonTenantConfigurationRepository(base_data / "tenant_configurations")
    alert_repo = JsonOperationalAlertRepository(base_data / "observability_alerts")

    rbac_svc = RBACService(
        role_repository=role_repo,
        assignment_repository=assignment_repo,
        clock=clock,
    )
    auth_svc = SaaSAuthorizationService(
        session_repository=session_repo,
        organization_repository=org_repo,
        membership_repository=membership_repo,
        rbac_service=rbac_svc,
        clock=clock,
    )
    org_svc = OrganizationService(
        organization_repo=org_repo,
        audit_repository=audit_repo,
    )
    membership_svc = OrganizationMembershipService(
        membership_repo=membership_repo,
        organization_repo=org_repo,
        audit_repository=audit_repo,
    )
    usage_svc = UsageMeteringService(
        repository=usage_repo,
        clock=clock,
    )
    quota_svc = QuotaManagementService(
        policy_repository=quota_policy_repo,
        reservation_repository=quota_reservation_repo,
        usage_metering_service=usage_svc,
        clock=clock,
    )
    plan_svc = PlanEntitlementService(
        catalog_repository=plan_repo,
        assignment_repository=plan_assignment_repo,
        quota_policy_repository=quota_policy_repo,
        clock=clock,
    )
    sub_svc = SubscriptionService(
        subscription_repository=sub_repo,
        plan_catalog_repository=plan_repo,
        plan_entitlement_service=plan_svc,
        clock=clock,
    )
    tenant_config_svc = TenantConfigurationService(
        repository=config_repo,
    )
    obs_svc = TenantObservabilityService(
        alert_repository=alert_repo,
        clock=clock,
        usage_metering_service=usage_svc,
        subscription_repository=sub_repo,
    )

    return AdminConsoleService(
        session_repository=session_repo,
        authorization_service=auth_svc,
        organization_service=org_svc,
        membership_service=membership_svc,
        usage_metering_service=usage_svc,
        quota_management_service=quota_svc,
        quota_policy_repository=quota_policy_repo,
        quota_reservation_repository=quota_reservation_repo,
        plan_entitlement_service=plan_svc,
        plan_repository=plan_repo,
        plan_assignment_repository=plan_assignment_repo,
        subscription_service=sub_svc,
        subscription_repository=sub_repo,
        invoice_repository=invoice_repo,
        rbac_service=rbac_svc,
        audit_repository=audit_repo,
        trace_repository=trace_repo,
        tenant_configuration_service=tenant_config_svc,
        tenant_observability_service=obs_svc,
        clock=clock,
    )


def create_platform_app(
    config: Optional[DeploymentConfig] = None,
    admin_service: Optional[AdminConsoleService] = None,
    health_service: Optional[HealthCheckService] = None,
    monitoring_service: Optional[ProductionMonitoringService] = None,
) -> Starlette:
    """Crea la aplicación Starlette canónica para despliegue de plataforma."""
    if config is None:
        validator = DeploymentConfigValidator()
        config = validator.validate()

    if health_service is None:
        health_service = HealthCheckService(config=config)

    if monitoring_service is None:
        metric_repo = JsonMetricRepository(config.data_dir)
        monitoring_service = ProductionMonitoringService(
            repository=metric_repo,
            environment=config.environment,
        )

    async def liveness_probe(request: Request) -> JSONResponse:
        """Endpoint de liveness probe para orquestadores."""
        if not config.liveness_probes_enabled:
            return JSONResponse({"status": "disabled"}, status_code=404)
        result = health_service.check_liveness()
        # Registrar health projection en monitoring
        monitoring_service.record_health_check_result(result, environment=config.environment)
        return JSONResponse(result.to_dict(), status_code=200)

    async def readiness_probe(request: Request) -> JSONResponse:
        """Endpoint de readiness probe con verificación de almacenamiento y base de datos."""
        if not config.readiness_probes_enabled:
            return JSONResponse({"status": "disabled"}, status_code=404)

        result = health_service.check_readiness()
        # Registrar health projection en monitoring
        monitoring_service.record_health_check_result(result, environment=config.environment)
        return JSONResponse(result.to_dict(), status_code=result.http_status_code)

    async def metrics_endpoint(request: Request) -> JSONResponse:
        """Endpoint técnico de observabilidad de producción (P.7)."""
        window_str = request.query_params.get("window_seconds", "300")
        window_seconds = int(window_str) if window_str.lstrip("-").isdigit() else 300
        snapshot = monitoring_service.get_snapshot(
            window=window_seconds,
            environment=config.environment,
        )
        return JSONResponse(snapshot.to_dict(), status_code=200)

    routes = [
        Route("/health", liveness_probe, methods=["GET"]),
        Route("/healthz", liveness_probe, methods=["GET"]),
        Route("/ready", readiness_probe, methods=["GET"]),
        Route("/readyz", readiness_probe, methods=["GET"]),
        Route("/metrics", metrics_endpoint, methods=["GET"]),
    ]

    # Si Admin Console está habilitada, montar sus rutas
    if config.enable_admin_console:
        if admin_service is None:
            admin_service = build_default_admin_service(config.data_dir)
        admin_app = create_admin_app(admin_service)
        # Importante: Las rutas de admin_app se integran o montan
        for r in admin_app.routes:
            if r.path not in {"/health"}:  # Evitar sobrescribir liveness de plataforma
                routes.append(r)

    app = Starlette(debug=(config.environment == DeploymentEnvironment.DEVELOPMENT), routes=routes)
    app.add_middleware(ProductionMonitoringMiddleware, monitoring_service=monitoring_service, environment=config.environment)
    app.state.deployment_config = config
    app.state.monitoring_service = monitoring_service
    return app


def get_asgi_app() -> Starlette:
    """Punto de entrada canónico para servidores ASGI (e.g. uvicorn src.infrastructure.web.app:get_asgi_app)."""
    validator = DeploymentConfigValidator()
    config = validator.validate()
    return create_platform_app(config=config)
