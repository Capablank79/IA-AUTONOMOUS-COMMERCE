"""
Pruebas de integración de O.12 SaaS Observability con la consola administrativa O.10,
RBAC, autorización O.4, persistencia JSON y contratos reales de O.6, O.9 y N.11.

Cobertura:
- Aislamiento tenant estricto en snapshots, métricas y alertas.
- UNKNOWN != ZERO: sin tráfico ni fuentes, los campos ausentes son None / UNKNOWN (no ceros inventados).
- Agregación determinista desde eventos reales de Usage Metering (O.6).
- Integración con contratos reales de Billing (O.9) y Emergency Stop (N.11).
- Ciclo de vida y deduplicación de alertas operacionales (ACK, Resolve, idempotencia).
- Persistencia JSON en disco con verificación de checksum SHA-256 y resiliencia ante reinicios.
- Rechazo de path traversal e identificadores inseguros.
- Control de acceso RBAC: permisos OBSERVABILITY_READ, OBSERVABILITY_ALERT_MANAGE y cross-tenant.
- Endpoints HTTP O.12 con validación estricta de parámetros (window_seconds, status).
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import uuid

import pytest
from starlette.testclient import TestClient

from src.application.admin_console.admin_console_service import AdminConsoleService
from src.application.billing.subscription_service import SubscriptionService
from src.application.organization.organization_service import (
    OrganizationMembershipService,
    OrganizationService,
)
from src.application.plans.plan_entitlement_service import PlanEntitlementService
from src.application.quota_management.quota_management_service import QuotaManagementService
from src.application.rbac.rbac_service import RBACService
from src.application.saas_authorization.saas_authorization_service import SaaSAuthorizationService
from src.application.saas_observability.tenant_observability_service import (
    TenantObservabilityService,
)
from src.application.usage_metering.usage_metering_service import UsageMeteringService
from src.domain.admin_console.models import AdminAction
from src.domain.billing.models import (
    BillingCycle,
    Subscription,
    SubscriptionStatus,
)
from src.domain.emergency_stop.models import (
    EmergencyStopReasonCode,
    EmergencyStopRecord,
    EmergencyStopScope,
    EmergencyStopState,
)
from src.domain.rbac.models import (
    Permission,
    PermissionStatus,
    Role,
    RoleAssignment,
    RoleStatus,
)
from src.domain.reliability.ports import ClockPort
from src.domain.saas_observability.models import (
    AlertSeverity,
    AlertStatus,
    MetricType,
    OperationalAlert,
    OperationalAlertType,
    TenantHealthStatus,
)
from src.domain.session.models import SaaSSession, SessionStatus
from src.domain.tenant.models import TenantContext, TenantScope
from src.domain.usage_metering.models import (
    CacheLookupStatus,
    UsageEvent,
    UsageRequestStatus,
)
from src.infrastructure.persistence.data.json.audit_repository import JsonAuditRepository
from src.infrastructure.persistence.data.json.billing_repository import (
    InMemoryInvoiceRepository,
    InMemorySubscriptionRepository,
)
from src.infrastructure.persistence.data.json.emergency_stop_repository import (
    JsonEmergencyStopRepository,
)
from src.infrastructure.persistence.data.json.operational_alert_repository import (
    JsonOperationalAlertRepository,
)
from src.infrastructure.persistence.data.json.organization_repository import (
    JsonMembershipRepository,
    JsonOrganizationRepository,
)
from src.infrastructure.persistence.data.json.plan_repository import (
    InMemoryPlanAssignmentRepository,
    InMemoryPlanCatalogRepository,
)
from src.infrastructure.persistence.data.json.quota_repository import (
    InMemoryQuotaPolicyRepository,
    InMemoryQuotaReservationRepository,
)
from src.infrastructure.persistence.data.json.rbac_repository import (
    JsonRoleAssignmentRepository,
    JsonRoleRepository,
)
from src.infrastructure.persistence.data.json.session_repository import JsonSaaSSessionRepository
from src.infrastructure.persistence.data.json.usage_event_repository import (
    InMemoryUsageEventRepository,
)
from src.infrastructure.web.admin_app import create_admin_app


class FixedClock(ClockPort):
    def __init__(self):
        self.current = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)

    def now(self):
        return self.current

    def sleep(self, seconds):
        self.current += timedelta(seconds=seconds)


ALL_O12_ADMIN_ACTIONS = (
    "TENANT_READ",
    "ORGANIZATION_READ",
    "OBSERVABILITY_READ",
    "OBSERVABILITY_ALERT_MANAGE",
)


def _grant_session(env, tenant_id, identity_id, actions, scope_override=None):
    suffix = uuid.uuid4().hex[:8]
    role_id = f"role_{suffix}"
    permissions = tuple(
        Permission(
            permission_id=f"perm_{action.lower()}_{suffix}",
            action=action,
            status=PermissionStatus.ACTIVE,
        )
        for action in actions
    )
    env["role_repo"].save_role(
        Role(role_id=role_id, name=f"Role {suffix}", permissions=permissions, status=RoleStatus.ACTIVE)
    )
    assignment_scope = scope_override or TenantScope(tenant_id=tenant_id).canonical_scope
    env["assignment_repo"].save_assignment(
        RoleAssignment(
            assignment_id=f"assignment_{suffix}",
            identity_id=identity_id,
            role_id=role_id,
            scope=assignment_scope,
            assigned_at=env["clock"].now(),
        )
    )
    session_id = f"session_{suffix}"
    env["session_repo"].save(
        SaaSSession(
            session_id=session_id,
            identity_id=identity_id,
            tenant_id=tenant_id,
            status=SessionStatus.ACTIVE,
            created_at=env["clock"].now(),
            last_validated_at=env["clock"].now(),
            expires_at=env["clock"].now() + timedelta(hours=2),
        )
    )
    return session_id


def _build_env(storage_dir):
    clock = FixedClock()
    session_repo = JsonSaaSSessionRepository(storage_dir)
    org_repo = JsonOrganizationRepository(storage_dir)
    membership_repo = JsonMembershipRepository(storage_dir)
    role_repo = JsonRoleRepository(storage_dir)
    assignment_repo = JsonRoleAssignmentRepository(storage_dir)
    audit_repo = JsonAuditRepository(storage_dir / "audit")

    rbac_service = RBACService(role_repo, assignment_repo, clock=clock)
    authorization_service = SaaSAuthorizationService(
        session_repository=session_repo,
        organization_repository=org_repo,
        membership_repository=membership_repo,
        rbac_service=rbac_service,
        clock=clock,
    )
    organization_service = OrganizationService(org_repo, audit_repository=audit_repo)
    membership_service = OrganizationMembershipService(
        membership_repo, org_repo, audit_repository=audit_repo
    )
    usage_repo = InMemoryUsageEventRepository()
    usage_service = UsageMeteringService(usage_repo, clock=clock)
    quota_policy_repo = InMemoryQuotaPolicyRepository()
    quota_reservation_repo = InMemoryQuotaReservationRepository()
    quota_service = QuotaManagementService(
        quota_policy_repo, quota_reservation_repo, usage_service, clock=clock
    )
    plan_repo = InMemoryPlanCatalogRepository()
    plan_assignment_repo = InMemoryPlanAssignmentRepository()
    plan_service = PlanEntitlementService(
        plan_repo, plan_assignment_repo, quota_policy_repository=quota_policy_repo, clock=clock
    )
    subscription_repo = InMemorySubscriptionRepository()
    invoice_repo = InMemoryInvoiceRepository()
    subscription_service = SubscriptionService(
        subscription_repo, plan_repo, plan_entitlement_service=plan_service, clock=clock
    )

    alert_repo = JsonOperationalAlertRepository(storage_dir / "alerts")
    emergency_repo = JsonEmergencyStopRepository(str(storage_dir / "emergency_stop.json"))

    tenant_observability_service = TenantObservabilityService(
        alert_repository=alert_repo,
        clock=clock,
        usage_metering_service=usage_service,
        subscription_repository=subscription_repo,
        emergency_stop_repository=emergency_repo,
    )

    admin_service = AdminConsoleService(
        session_repository=session_repo,
        authorization_service=authorization_service,
        organization_service=organization_service,
        membership_service=membership_service,
        usage_metering_service=usage_service,
        quota_management_service=quota_service,
        quota_policy_repository=quota_policy_repo,
        quota_reservation_repository=quota_reservation_repo,
        plan_entitlement_service=plan_service,
        plan_repository=plan_repo,
        plan_assignment_repository=plan_assignment_repo,
        subscription_service=subscription_service,
        subscription_repository=subscription_repo,
        invoice_repository=invoice_repo,
        rbac_service=rbac_service,
        audit_repository=audit_repo,
        clock=clock,
        tenant_observability_service=tenant_observability_service,
    )

    env = locals()
    env["client"] = TestClient(create_admin_app(admin_service))
    return env


@pytest.fixture
def env(tmp_path):
    result = _build_env(tmp_path / "o12_env")
    result["admin_session"] = _grant_session(
        result, "tenant_a", "admin_a", ALL_O12_ADMIN_ACTIONS
    )
    return result


def _auth(session_id):
    return {"Authorization": f"Bearer {session_id}"}


# ---------------------------------------------------------------------------
# Test 1: Snapshot Inicial, Aislamiento y Regla UNKNOWN != ZERO
# ---------------------------------------------------------------------------


def test_snapshot_sin_actividad_preserva_unknown_en_tasas_no_definidas(env):
    """El cero de O.6 es un hecho; tasas sin muestras y fuentes ausentes siguen UNKNOWN."""
    response = env["client"].get(
        "/api/admin/tenants/tenant_a/observability",
        headers=_auth(env["admin_session"]),
    )
    assert response.status_code == 200
    data = response.json()

    assert data["tenant_id"] == "tenant_a"
    assert data["health_status"] == TenantHealthStatus.UNKNOWN.value
    assert data["request_count"] == 0
    assert data["error_rate"] is None
    assert data["avg_latency_ms"] is None
    assert data["p95_latency_ms"] is None
    assert data["total_tokens"] == 0
    assert data["total_cost_usd"] == "0.00"

    assert data["quota_status"] == "UNKNOWN"
    assert data["billing_status"] == "NO_SUBSCRIPTION"
    assert data["active_alerts_count"] == 0
    assert data["active_alerts"] == []
    assert data["metrics"][MetricType.REQUEST_COUNT.value]["value"] == 0
    assert MetricType.ERROR_RATE.value not in data["metrics"]
    assert MetricType.CACHE_HIT_RATE.value not in data["metrics"]
    assert MetricType.MODEL_REQUEST_COUNT.value not in data["metrics"]


# ---------------------------------------------------------------------------
# Test 2: Agregación O.6 y Métricas Derivadas (CACHE_HIT_RATE, MODEL_REQUEST_COUNT)
# ---------------------------------------------------------------------------


def test_snapshot_agrega_hechos_o6_y_aisla_tenants(env):
    """
    Graba eventos de uso reales en O.6 para Tenant A y verifica que:
    - Se agreguen métricas correctamente (REQUEST_COUNT, ERROR_RATE, CACHE_HIT_RATE, MODEL_REQUEST_COUNT).
    - Tenant B no reciba métricas de Tenant A.
    """
    ctx_a = TenantContext(tenant_id="tenant_a", identity_id="user_a")
    now = env["clock"].now()

    # Evento 1: exitoso con modelo gpt-4o
    env["usage_service"].record_usage_event(
        ctx_a,
        UsageEvent(
            usage_event_id="evt_1",
            tenant_id="tenant_a",
            occurred_at=now - timedelta(minutes=10),
            request_status=UsageRequestStatus.SUCCESS,
            identity_id="user_a",
            provider="openai",
            model="gpt-4o",
            cache_status=CacheLookupStatus.HIT,
            input_tokens=100,
            output_tokens=50,
            estimated_cost=Decimal("0.02"),
            actual_cost=Decimal("0.02"),
        ),
    )

    # Evento 2: fallido con modelo claude-3
    env["usage_service"].record_usage_event(
        ctx_a,
        UsageEvent(
            usage_event_id="evt_2",
            tenant_id="tenant_a",
            occurred_at=now - timedelta(minutes=5),
            request_status=UsageRequestStatus.FAILED,
            identity_id="user_a",
            provider="anthropic",
            model="claude-3",
            cache_status=CacheLookupStatus.MISS,
            input_tokens=200,
            output_tokens=0,
            estimated_cost=Decimal("0.03"),
            actual_cost=Decimal("0.03"),
        ),
    )

    response = env["client"].get(
        "/api/admin/tenants/tenant_a/observability",
        headers=_auth(env["admin_session"]),
    )
    assert response.status_code == 200
    data = response.json()

    assert data["request_count"] == 2
    assert data["error_rate"] == 0.5  # 1 fallido de 2
    assert data["total_tokens"] == 350
    assert data["total_cost_usd"] == "0.05"
    assert data["health_status"] == TenantHealthStatus.DEGRADED.value  # error_rate > 0.10

    metrics = data["metrics"]
    assert metrics[MetricType.REQUEST_COUNT.value]["value"] == 2
    assert metrics[MetricType.SUCCESS_COUNT.value]["value"] == 1
    assert metrics[MetricType.FAILURE_COUNT.value]["value"] == 1
    assert metrics[MetricType.ERROR_RATE.value]["value"] == 0.5
    assert metrics[MetricType.CACHE_HIT_RATE.value]["value"] == 0.5
    assert metrics[MetricType.MODEL_REQUEST_COUNT.value]["value"] == 2

    # Aislamiento Tenant B con su propia sesión
    session_b = _grant_session(env, "tenant_b", "admin_b", ALL_O12_ADMIN_ACTIONS)
    response_b = env["client"].get(
        "/api/admin/tenants/tenant_b/observability",
        headers=_auth(session_b),
    )
    assert response_b.status_code == 200
    assert response_b.json()["request_count"] == 0
    assert response_b.json()["metrics"][MetricType.REQUEST_COUNT.value]["value"] == 0
    assert MetricType.MODEL_REQUEST_COUNT.value not in response_b.json()["metrics"]


# ---------------------------------------------------------------------------
# Test 3: Contratos Reales de Billing y Emergency Stop
# ---------------------------------------------------------------------------


def test_integracion_contrato_billing_activo_y_emergency_stop(env):
    """
    Verifica que:
    1. Una suscripción real en InMemorySubscriptionRepository refleje 'ACTIVE' y HEALTHY.
    2. Un EmergencyStopRecord real en JsonEmergencyStopRepository genere alerta CRITICAL y UNHEALTHY.
    3. La alerta se deduplique en evaluaciones subsiguientes.
    """
    now = env["clock"].now()
    # 1. Configurar suscripción activa
    env["subscription_repo"].save_subscription(
        Subscription(
            subscription_id="sub_a",
            tenant_id="tenant_a",
            plan_id="plan_enterprise",
            plan_version="1.0.0",
            status=SubscriptionStatus.ACTIVE,
            billing_cycle=BillingCycle.MONTHLY,
            current_period_start=now - timedelta(days=10),
            current_period_end=now + timedelta(days=20),
            base_price=Decimal("100.00"),
        )
    )

    resp_sub = env["client"].get(
        "/api/admin/tenants/tenant_a/observability",
        headers=_auth(env["admin_session"]),
    )
    assert resp_sub.status_code == 200
    assert resp_sub.json()["billing_status"] == SubscriptionStatus.ACTIVE.value
    assert resp_sub.json()["health_status"] == TenantHealthStatus.HEALTHY.value

    # 2. Activar Emergency Stop para tenant_a
    env["emergency_repo"].save(
        EmergencyStopRecord(
            stop_id="stop_tenant_a",
            scope=EmergencyStopScope.ACCOUNT,
            state=EmergencyStopState.ACTIVE,
            reason_code=EmergencyStopReasonCode.SECURITY_INCIDENT,
            reason_details="Compromised credentials detected",
            activated_by_identity_id="sec_ops",
            activated_at=now - timedelta(minutes=2),
            target_id="tenant_a",
            expires_at=None,
        )
    )

    resp_stop = env["client"].get(
        "/api/admin/tenants/tenant_a/observability",
        headers=_auth(env["admin_session"]),
    )
    assert resp_stop.status_code == 200
    data_stop = resp_stop.json()
    assert data_stop["health_status"] == TenantHealthStatus.UNHEALTHY.value
    assert data_stop["active_alerts_count"] == 1
    alert = data_stop["active_alerts"][0]
    assert alert["alert_type"] == OperationalAlertType.EMERGENCY_STOP_ACTIVE.value
    assert alert["severity"] == AlertSeverity.CRITICAL.value
    assert alert["status"] == AlertStatus.ACTIVE.value

    # 3. Deduplicación: llamar de nuevo no crea una segunda alerta
    resp_dedup = env["client"].get(
        "/api/admin/tenants/tenant_a/observability",
        headers=_auth(env["admin_session"]),
    )
    assert resp_dedup.json()["active_alerts_count"] == 1
    assert resp_dedup.json()["active_alerts"][0]["alert_id"] == alert["alert_id"]


# ---------------------------------------------------------------------------
# Test 4: Ciclo de Vida de Alertas (List, Acknowledge, Resolve)
# ---------------------------------------------------------------------------


def test_ciclo_de_vida_alertas_http(env):
    """
    Prueba el ciclo de vida completo de una alerta operacional a través de HTTP:
    Listar -> Reconocer (ACK) -> Resolver (RESOLVE) con razón.
    """
    now = env["clock"].now()
    alert = OperationalAlert(
        alert_id="alt_high_err_1",
        tenant_id="tenant_a",
        alert_type=OperationalAlertType.HIGH_ERROR_RATE,
        severity=AlertSeverity.HIGH,
        status=AlertStatus.ACTIVE,
        summary="High error rate 25%",
        details={"error_rate": 0.25},
        triggered_at=now - timedelta(minutes=15),
        deduplication_key="high_error_rate:tenant_a",
    )
    env["alert_repo"].save_alert(alert)

    # 1. Listar alertas activas
    resp_list = env["client"].get(
        "/api/admin/tenants/tenant_a/observability/alerts?status=ACTIVE",
        headers=_auth(env["admin_session"]),
    )
    assert resp_list.status_code == 200
    alerts_data = resp_list.json()
    assert len(alerts_data) == 1
    assert alerts_data[0]["alert_id"] == "alt_high_err_1"
    assert alerts_data[0]["status"] == AlertStatus.ACTIVE.value

    # 2. Acknowledge alerta
    resp_ack = env["client"].post(
        f"/api/admin/tenants/tenant_a/observability/alerts/alt_high_err_1/acknowledge",
        headers=_auth(env["admin_session"]),
    )
    assert resp_ack.status_code == 200
    assert resp_ack.json()["status"] == AlertStatus.ACKNOWLEDGED.value
    assert resp_ack.json()["acknowledged_at"] is not None

    # 3. Resolve alerta
    resp_res = env["client"].post(
        f"/api/admin/tenants/tenant_a/observability/alerts/alt_high_err_1/resolve",
        headers=_auth(env["admin_session"]),
        json={"reason": "Investigated and fixed by on-call engineer"},
    )
    assert resp_res.status_code == 200
    assert resp_res.json()["status"] == AlertStatus.RESOLVED.value
    assert resp_res.json()["resolved_at"] is not None

    # 4. Verificar lista filtrando por RESOLVED
    resp_list_resolved = env["client"].get(
        "/api/admin/tenants/tenant_a/observability/alerts?status=RESOLVED",
        headers=_auth(env["admin_session"]),
    )
    assert resp_list_resolved.status_code == 200
    assert any(a["alert_id"] == "alt_high_err_1" for a in resp_list_resolved.json())


# ---------------------------------------------------------------------------
# Test 5: RBAC (Read vs Manage) y Cross-Tenant Access
# ---------------------------------------------------------------------------


def test_rbac_observability_read_manage_y_cross_tenant(env):
    """
    Verifica granularidad de RBAC:
    - OBSERVABILITY_READ permite GET snapshot y alerts.
    - Usuario sin OBSERVABILITY_ALERT_MANAGE no puede ACK ni RESOLVE (403).
    - Acceso cross-tenant denegado salvo plataforma.
    """
    now = env["clock"].now()
    env["alert_repo"].save_alert(
        OperationalAlert(
            alert_id="alt_rbac_test",
            tenant_id="tenant_a",
            alert_type=OperationalAlertType.HIGH_ERROR_RATE,
            severity=AlertSeverity.HIGH,
            status=AlertStatus.ACTIVE,
            summary="RBAC test alert",
            details={},
            triggered_at=now,
            deduplication_key="rbac_test",
        )
    )

    # Sesión con SOLO permiso de lectura
    read_only_session = _grant_session(
        env, "tenant_a", "reader_user", ("OBSERVABILITY_READ",)
    )

    # Lectura permitida
    resp_snap = env["client"].get(
        "/api/admin/tenants/tenant_a/observability",
        headers=_auth(read_only_session),
    )
    assert resp_snap.status_code == 200

    resp_alerts = env["client"].get(
        "/api/admin/tenants/tenant_a/observability/alerts",
        headers=_auth(read_only_session),
    )
    assert resp_alerts.status_code == 200

    # Mutación / Gestión denegada
    resp_ack = env["client"].post(
        "/api/admin/tenants/tenant_a/observability/alerts/alt_rbac_test/acknowledge",
        headers=_auth(read_only_session),
    )
    assert resp_ack.status_code == 403
    assert resp_ack.json()["error"] == "unauthorized"

    resp_res = env["client"].post(
        "/api/admin/tenants/tenant_a/observability/alerts/alt_rbac_test/resolve",
        headers=_auth(read_only_session),
        json={"reason": "try resolve"},
    )
    assert resp_res.status_code == 403

    # Cross-tenant: tenant_a intentando consultar tenant_b
    resp_cross = env["client"].get(
        "/api/admin/tenants/tenant_b/observability",
        headers=_auth(read_only_session),
    )
    assert resp_cross.status_code == 403

    # El pipeline exige permiso de acción en el tenant de la sesión y privilegio global separado.
    platform_session = _grant_session(
        env, "tenant_platform", "super_admin", ("OBSERVABILITY_READ",)
    )
    _grant_session(
        env,
        "tenant_platform",
        "super_admin",
        ("PLATFORM_ADMIN",),
        scope_override="PLATFORM",
    )
    resp_platform_cross = env["client"].get(
        "/api/admin/tenants/tenant_a/observability",
        headers=_auth(platform_session),
    )
    assert resp_platform_cross.status_code == 200
    assert resp_platform_cross.json()["tenant_id"] == "tenant_a"


# ---------------------------------------------------------------------------
# Test 6: Validación de Entradas HTTP (window_seconds y status)
# ---------------------------------------------------------------------------


def test_validacion_http_parametros_invalidos(env):
    """
    Verifica que:
    - window_seconds no numérico o fuera de los permitidos sea rechazado con 422.
    - status de alerta no válido sea rechazado con 422.
    """
    # 1. window_seconds no numérico
    resp_non_num = env["client"].get(
        "/api/admin/tenants/tenant_a/observability?window_seconds=not_a_number",
        headers=_auth(env["admin_session"]),
    )
    assert resp_non_num.status_code == 422
    assert resp_non_num.json()["error"] == "invalid_request"

    # 2. window_seconds no soportado
    resp_bad_win = env["client"].get(
        "/api/admin/tenants/tenant_a/observability?window_seconds=9999",
        headers=_auth(env["admin_session"]),
    )
    assert resp_bad_win.status_code == 422
    assert resp_bad_win.json()["error"] == "invalid_request"

    # 3. Status de alerta inexistente
    resp_bad_status = env["client"].get(
        "/api/admin/tenants/tenant_a/observability/alerts?status=INVALID_STATUS_XYZ",
        headers=_auth(env["admin_session"]),
    )
    assert resp_bad_status.status_code == 422
    assert resp_bad_status.json()["error"] == "invalid_request"


# ---------------------------------------------------------------------------
# Test 7: Persistencia en Disco, Verificación de Checksum y Path Traversal
# ---------------------------------------------------------------------------


def test_persistencia_json_checksum_y_path_traversal(tmp_path):
    """
    Verifica que:
    1. Las alertas operacionales persisten en disco con checksum SHA-256 y sobreviven a reinicios.
    2. La manipulación de un archivo JSON dispara la detección de checksum inválido.
    3. Intentos de path traversal en identificadores son rechazados.
    """
    storage = tmp_path / "o12_persistence"
    env_1 = _build_env(storage)
    session = _grant_session(env_1, "tenant_persist", "admin_persist", ALL_O12_ADMIN_ACTIONS)
    now = env_1["clock"].now()

    # Guardar alerta en env_1
    alert = OperationalAlert(
        alert_id="alt_persist_1",
        tenant_id="tenant_persist",
        alert_type=OperationalAlertType.PROVIDER_DEGRADED,
        severity=AlertSeverity.HIGH,
        status=AlertStatus.ACTIVE,
        summary="Provider OpenAI Degraded",
        details={"provider": "openai"},
        triggered_at=now,
        deduplication_key="provider_openai_degraded",
    )
    env_1["alert_repo"].save_alert(alert)

    # Re-instanciar entorno (simula reinicio del servidor)
    env_2 = _build_env(storage)
    resp = env_2["client"].get(
        "/api/admin/tenants/tenant_persist/observability/alerts",
        headers=_auth(session),
    )
    assert resp.status_code == 200
    alerts = resp.json()
    assert len(alerts) == 1
    assert alerts[0]["alert_id"] == "alt_persist_1"
    assert alerts[0]["summary"] == "Provider OpenAI Degraded"

    # Manipular el archivo JSON en disco alterando el summary sin actualizar el checksum
    alert_file = storage / "alerts" / "tenants" / "tenant_persist" / "observability" / "alerts" / "alt_persist_1.json"
    assert alert_file.exists()
    with open(alert_file, "r", encoding="utf-8") as f:
        raw_data = json.load(f)
    raw_data["summary"] = "TAMPERED_SUMMARY"
    with open(alert_file, "w", encoding="utf-8") as f:
        json.dump(raw_data, f)

    # get_alert_by_id debe fallar por mismatch de checksum
    with pytest.raises(Exception) as excinfo:
        env_2["alert_repo"].get_alert_by_id("tenant_persist", "alt_persist_1")
    assert "Checksum mismatch" in str(excinfo.value)

    # Path traversal en tenant_id o alert_id es rechazado
    with pytest.raises(ValueError):
        env_2["alert_repo"].get_alert_by_id("../evil", "alt_persist_1")
    with pytest.raises(ValueError):
        env_2["alert_repo"].get_alert_by_id("tenant_persist", "../../etc/passwd")
