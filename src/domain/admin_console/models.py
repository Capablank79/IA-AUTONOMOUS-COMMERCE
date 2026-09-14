"""
Modelos de dominio y View Models para Admin Console & Multi-Tenant Management (Hito O.10 — SaaS / Platformization).

Define:
- AdminAction: Acciones y permisos canónicos administrativos.
- TenantAdminSummary: Vista resumida segura del tenant para operadores.
- OrganizationAdminView: Vista administrativa de organizaciones del tenant.
- MembershipAdminView: Vista de membresías de usuarios con PII enmascarada.
- UsageAdminSummary: Vista agregada de uso de inferencia sin prompts ni CoT.
- QuotaAdminView: Vista de políticas y reservas de cuotas.
- PlanAdminView: Vista de planes y catálogo comercial.
- BillingAdminView: Vista financiera segura sin PAN, CVV ni secretos.
- AuditAdminView: Vista de hechos de auditoría sanitizados.
- TraceAdminView: Vista de trazas operacionales sanitizadas.
- Excepciones de dominio para Admin Console.

Principios O.10:
1. Responde a: "¿Puede un operador autorizado administrar tenants, organizations, users, plans, quotas y billing desde una consola segura sin romper el aislamiento multi-tenant?".
2. Seguridad estricta: Toda acción requiere sesión O.3 válida, autorización O.4 y permiso RBAC explícito.
3. Separación estricta de permisos de lectura y escritura (READ != MANAGE).
4. No secretos, PAN, CVV, prompts privados ni CoT en ninguna vista.
5. Inmutabilidad y determinismo con sanitización N.9.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
import hashlib
import json
import re
from types import MappingProxyType
from typing import Mapping, Optional, Any, Tuple, Sequence, Dict, Union, List

from src.domain.security.models import (
    validate_safe_identifier,
    sanitize_security_data,
    deep_freeze,
)


class AdminConsoleError(Exception):
    """Excepción base para operaciones de Admin Console."""
    pass


class AdminAuthenticationError(AdminConsoleError):
    """Se lanza ante ausencia, expiración o invalidez de sesión (401)."""
    pass


class AdminAuthorizationError(AdminConsoleError):
    """Se lanza ante falta de permisos administrativos o violación cross-tenant (403)."""
    pass


class AdminResourceNotFoundError(AdminConsoleError):
    """Se lanza cuando un recurso consultado no existe o no pertenece al tenant (404)."""
    pass


class AdminConflictError(AdminConsoleError):
    """Se lanza ante conflictos de estado o mutaciones concurrentes incompatibles (409)."""
    pass


class AdminInvalidRequestError(AdminConsoleError):
    """Se lanza cuando los parámetros de la solicitud son inválidos (422)."""
    pass


class AdminAction(str, Enum):
    """
    Acciones canónicas administrativas para RBAC (N.4 / O.4).
    Normalizadas con normalize_action_token.
    """
    TENANT_READ = "TENANT_READ"
    TENANT_MANAGE = "TENANT_MANAGE"
    TENANT_CONFIG_READ = "TENANT_CONFIG_READ"
    TENANT_CONFIG_MANAGE = "TENANT_CONFIG_MANAGE"
    ORGANIZATION_READ = "ORGANIZATION_READ"
    ORGANIZATION_MANAGE = "ORGANIZATION_MANAGE"
    USER_MEMBERSHIP_MANAGE = "USER_MEMBERSHIP_MANAGE"
    PLAN_READ = "PLAN_READ"
    PLAN_ASSIGN = "PLAN_ASSIGN"
    QUOTA_READ = "QUOTA_READ"
    QUOTA_MANAGE = "QUOTA_MANAGE"
    BILLING_READ = "BILLING_READ"
    BILLING_MANAGE = "BILLING_MANAGE"
    AUDIT_READ = "AUDIT_READ"
    TRACE_READ = "TRACE_READ"
    OBSERVABILITY_READ = "OBSERVABILITY_READ"
    OBSERVABILITY_ALERT_MANAGE = "OBSERVABILITY_ALERT_MANAGE"
    BUSINESS_INTELLIGENCE_READ = "BUSINESS_INTELLIGENCE_READ"
    BUSINESS_KPI_READ = "BUSINESS_KPI_READ"
    OPPORTUNITY_DASHBOARD_READ = "OPPORTUNITY_DASHBOARD_READ"
    SUPPLIER_DASHBOARD_READ = "SUPPLIER_DASHBOARD_READ"
    PROFIT_DASHBOARD_READ = "PROFIT_DASHBOARD_READ"
    MISSION_DASHBOARD_READ = "MISSION_DASHBOARD_READ"
    AGENT_COST_DASHBOARD_READ = "AGENT_COST_DASHBOARD_READ"


class AdminPermission(str, Enum):
    """Alias explícito para compatibilidad con catálogos de permisos."""
    TENANT_READ = "TENANT_READ"
    TENANT_MANAGE = "TENANT_MANAGE"
    ORGANIZATION_READ = "ORGANIZATION_READ"
    ORGANIZATION_MANAGE = "ORGANIZATION_MANAGE"
    USER_MEMBERSHIP_MANAGE = "USER_MEMBERSHIP_MANAGE"
    PLAN_READ = "PLAN_READ"
    PLAN_ASSIGN = "PLAN_ASSIGN"
    QUOTA_READ = "QUOTA_READ"
    QUOTA_MANAGE = "QUOTA_MANAGE"
    BILLING_READ = "BILLING_READ"
    BILLING_MANAGE = "BILLING_MANAGE"
    AUDIT_READ = "AUDIT_READ"
    TRACE_READ = "TRACE_READ"
    OBSERVABILITY_READ = "OBSERVABILITY_READ"
    OBSERVABILITY_ALERT_MANAGE = "OBSERVABILITY_ALERT_MANAGE"
    BUSINESS_INTELLIGENCE_READ = "BUSINESS_INTELLIGENCE_READ"
    BUSINESS_KPI_READ = "BUSINESS_KPI_READ"
    OPPORTUNITY_DASHBOARD_READ = "OPPORTUNITY_DASHBOARD_READ"
    SUPPLIER_DASHBOARD_READ = "SUPPLIER_DASHBOARD_READ"
    PROFIT_DASHBOARD_READ = "PROFIT_DASHBOARD_READ"
    MISSION_DASHBOARD_READ = "MISSION_DASHBOARD_READ"
    AGENT_COST_DASHBOARD_READ = "AGENT_COST_DASHBOARD_READ"


def mask_pii(text: Optional[str]) -> Optional[str]:
    """
    Enmascara strings que puedan contener emails o identidades con PII sensible.
    Ejemplo: 'usuario@empresa.com' -> 'u***o@empresa.com'
              'user_123456789' -> 'us***89'
    """
    if text is None:
        return None
    s = str(text).strip()
    if not s:
        return s

    # Si es email
    if "@" in s:
        parts = s.split("@", 1)
        user_part, domain_part = parts[0], parts[1]
        if len(user_part) <= 2:
            masked_user = user_part[0] + "***"
        else:
            masked_user = user_part[0] + "***" + user_part[-1]
        return f"{masked_user}@{domain_part}"

    # Si es un ID largo
    if len(s) > 6:
        return s[:2] + "***" + s[-2:]
    elif len(s) > 2:
        return s[0] + "***"
    return "***"


# -----------------------------------------------------------------------------
# View Models Seguros (DTOs)
# -----------------------------------------------------------------------------


def _to_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _to_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_json_value(item) for item in value]
    if isinstance(value, Decimal):
        return str(value)
    return value


def _safe_display(value: Optional[str]) -> str:
    """Saneamiento mínimo para vistas O.12: reemplaza caracteres no alfanuméricos seguros con '_'."""
    if value is None:
        return ""
    return "".join(ch if (ch.isalnum() or ch in "._-") else "_" for ch in str(value))


def _json_safe(value: Any) -> Any:
    """Convierte recursivamente estructuras congeladas (MappingProxyType/tuples) en JSON-serializable."""
    if isinstance(value, MappingProxyType):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [_json_safe(v) for v in value]
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


@dataclass(frozen=True)
class OrganizationAdminView:
    """Vista segura de una Organización para la consola de administración."""
    organization_id: str
    name: str
    status: str
    tenant_id: str
    created_at: Optional[str] = None
    members_count: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        validate_safe_identifier(self.organization_id, field_name="organization_id")
        validate_safe_identifier(self.tenant_id, field_name="tenant_id")
        object.__setattr__(self, "metadata", deep_freeze(sanitize_security_data(dict(self.metadata))))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "organization_id": self.organization_id,
            "name": self.name,
            "status": self.status,
            "tenant_id": self.tenant_id,
            "created_at": self.created_at,
            "members_count": self.members_count,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class MembershipAdminView:
    """Vista segura de membresía de usuario con PII enmascarada."""
    membership_id: str
    organization_id: str
    tenant_id: str
    identity_id_masked: str
    role: str
    status: str
    created_at: Optional[str] = None

    def __post_init__(self):
        validate_safe_identifier(self.membership_id, field_name="membership_id")
        validate_safe_identifier(self.organization_id, field_name="organization_id")
        validate_safe_identifier(self.tenant_id, field_name="tenant_id")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "membership_id": self.membership_id,
            "organization_id": self.organization_id,
            "tenant_id": self.tenant_id,
            "identity_id_masked": self.identity_id_masked,
            "role": self.role,
            "status": self.status,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class UsageAdminSummary:
    """Vista agregada de uso de inferencia (O.6). Cero prompts ni CoT."""
    tenant_id: str
    period_start: str
    period_end: str
    total_requests: int
    successful_requests: int
    failed_requests: int
    cached_requests: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    estimated_cost: str
    actual_cost: str
    model_breakdown: Mapping[str, Any] = field(default_factory=dict)
    provider_breakdown: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        validate_safe_identifier(self.tenant_id, field_name="tenant_id")
        object.__setattr__(self, "model_breakdown", deep_freeze(dict(self.model_breakdown)))
        object.__setattr__(self, "provider_breakdown", deep_freeze(dict(self.provider_breakdown)))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "period_start": self.period_start,
            "period_end": self.period_end,
            "total_requests": self.total_requests,
            "successful_requests": self.successful_requests,
            "failed_requests": self.failed_requests,
            "cached_requests": self.cached_requests,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "estimated_cost": self.estimated_cost,
            "actual_cost": self.actual_cost,
            "model_breakdown": _to_json_value(self.model_breakdown),
            "provider_breakdown": _to_json_value(self.provider_breakdown),
        }


@dataclass(frozen=True)
class QuotaAdminView:
    """Vista de políticas de cuota y reservas en vuelo (O.7)."""
    tenant_id: str
    policy_id: Optional[str]
    is_active: bool
    rules_count: int
    active_reservations_count: int
    rules_summary: Sequence[Mapping[str, Any]] = field(default_factory=tuple)

    def __post_init__(self):
        validate_safe_identifier(self.tenant_id, field_name="tenant_id")
        object.__setattr__(self, "rules_summary", tuple(deep_freeze(r) for r in self.rules_summary))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "policy_id": self.policy_id,
            "is_active": self.is_active,
            "rules_count": self.rules_count,
            "active_reservations_count": self.active_reservations_count,
            "rules_summary": [dict(r) for r in self.rules_summary],
        }


@dataclass(frozen=True)
class PlanAdminView:
    """Vista de planes comerciales y entitlements (O.8)."""
    plan_id: str
    name: str
    tier: str
    version: str
    status: str
    description: Optional[str] = None
    limits: Mapping[str, Any] = field(default_factory=dict)
    features: Sequence[str] = field(default_factory=tuple)

    def __post_init__(self):
        validate_safe_identifier(self.plan_id, field_name="plan_id")
        object.__setattr__(self, "limits", deep_freeze(dict(self.limits)))
        object.__setattr__(self, "features", tuple(self.features))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "name": self.name,
            "tier": self.tier,
            "version": self.version,
            "status": self.status,
            "description": self.description,
            "limits": dict(self.limits),
            "features": list(self.features),
        }


@dataclass(frozen=True)
class BillingInvoiceSummary:
    """Resumen de factura sin datos sensibles."""
    invoice_id: str
    status: str
    total_amount: str
    currency: str
    due_date: Optional[str] = None
    paid_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "invoice_id": self.invoice_id,
            "status": self.status,
            "total_amount": self.total_amount,
            "currency": self.currency,
            "due_date": self.due_date,
            "paid_at": self.paid_at,
        }


@dataclass(frozen=True)
class BillingAdminView:
    """Vista financiera segura de suscripción y facturación (O.9). Cero PAN/CVV."""
    tenant_id: str
    subscription_id: Optional[str]
    subscription_status: str
    plan_id: Optional[str]
    plan_version: Optional[str]
    billing_cycle: Optional[str]
    current_period_start: Optional[str] = None
    current_period_end: Optional[str] = None
    invoices_count: int = 0
    recent_invoices: Sequence[BillingInvoiceSummary] = field(default_factory=tuple)

    def __post_init__(self):
        validate_safe_identifier(self.tenant_id, field_name="tenant_id")
        object.__setattr__(self, "recent_invoices", tuple(self.recent_invoices))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "subscription_id": self.subscription_id,
            "subscription_status": self.subscription_status,
            "plan_id": self.plan_id,
            "plan_version": self.plan_version,
            "billing_cycle": self.billing_cycle,
            "current_period_start": self.current_period_start,
            "current_period_end": self.current_period_end,
            "invoices_count": self.invoices_count,
            "recent_invoices": [inv.to_dict() for inv in self.recent_invoices],
        }


@dataclass(frozen=True)
class TenantAdminSummary:
    """
    Vista global consolidada para un Tenant en la Consola de Administración.
    Reúne información de O.1, O.2, O.6, O.7, O.8, O.9 sin romper aislamiento ni exponer secretos.
    """
    tenant_id: str
    tenant_name: Optional[str]
    organizations_count: int
    active_plan: Optional[PlanAdminView]
    quota_summary: Optional[QuotaAdminView]
    usage_summary: Optional[UsageAdminSummary]
    billing_summary: Optional[BillingAdminView]

    def __post_init__(self):
        validate_safe_identifier(self.tenant_id, field_name="tenant_id")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "tenant_name": self.tenant_name,
            "organizations_count": self.organizations_count,
            "active_plan": self.active_plan.to_dict() if self.active_plan else None,
            "quota_summary": self.quota_summary.to_dict() if self.quota_summary else None,
            "usage_summary": self.usage_summary.to_dict() if self.usage_summary else None,
            "billing_summary": self.billing_summary.to_dict() if self.billing_summary else None,
        }


@dataclass(frozen=True)
class AuditAdminView:
    """Vista segura de registros de auditoría administrativa (K.1)."""
    audit_id: str
    timestamp: str
    actor_id_masked: str
    record_type: str
    operation: str
    target_tenant_id: str
    details_sanitized: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        validate_safe_identifier(self.target_tenant_id, field_name="target_tenant_id")
        object.__setattr__(self, "details_sanitized", deep_freeze(sanitize_security_data(dict(self.details_sanitized))))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "audit_id": self.audit_id,
            "timestamp": self.timestamp,
            "actor_id_masked": self.actor_id_masked,
            "record_type": self.record_type,
            "operation": self.operation,
            "target_tenant_id": self.target_tenant_id,
            "details_sanitized": dict(self.details_sanitized),
        }


@dataclass(frozen=True)
class TraceAdminView:
    """Vista de trazas operacionales sanitizadas (K.2)."""
    trace_id: str
    execution_id: str
    component_name: str
    step_number: int
    step_type: str
    operation: str
    status: str
    correlation_id: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "execution_id": self.execution_id,
            "component_name": self.component_name,
            "step_number": self.step_number,
            "step_type": self.step_type,
            "operation": self.operation,
            "status": self.status,
            "correlation_id": self.correlation_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }


# -----------------------------------------------------------------------------
# O.12 SaaS Observability Admin Views
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class ObservabilityAlertAdminView:
    """Vista segura de alerta operacional para la consola de administración (O.12)."""
    alert_id: str
    tenant_id: str
    alert_type: str
    severity: str
    status: str
    summary: str
    triggered_at: str
    resolved_at: Optional[str] = None
    acknowledged_at: Optional[str] = None
    organization_id: Optional[str] = None

    def __post_init__(self):
        validate_safe_identifier(self.alert_id, field_name="alert_id")
        validate_safe_identifier(self.tenant_id, field_name="tenant_id")
        if self.organization_id:
            validate_safe_identifier(self.organization_id, field_name="organization_id")
        object.__setattr__(self, "alert_type", _safe_display(self.alert_type))
        object.__setattr__(self, "severity", _safe_display(self.severity))
        object.__setattr__(self, "status", _safe_display(self.status))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "alert_id": self.alert_id,
            "tenant_id": self.tenant_id,
            "alert_type": self.alert_type,
            "severity": self.severity,
            "status": self.status,
            "summary": self.summary,
            "triggered_at": self.triggered_at,
            "resolved_at": self.resolved_at,
            "acknowledged_at": self.acknowledged_at,
            "organization_id": self.organization_id,
        }


@dataclass(frozen=True)
class ObservabilityAdminView:
    """Vista consolidada de observabilidad operacional del tenant (O.12). Sin PII ni secretos."""
    tenant_id: str
    health_status: str
    window_seconds: int
    evaluated_at: str
    request_count: Optional[int] = None
    error_rate: Optional[float] = None
    avg_latency_ms: Optional[float] = None
    p95_latency_ms: Optional[float] = None
    total_tokens: Optional[int] = None
    total_cost_usd: Optional[str] = None
    quota_status: str = "UNKNOWN"
    billing_status: str = "UNKNOWN"
    active_alerts_count: int = 0
    active_alerts: Sequence[ObservabilityAlertAdminView] = field(default_factory=tuple)
    metrics: Mapping[str, Any] = field(default_factory=dict)
    checksum: str = ""

    def __post_init__(self):
        validate_safe_identifier(self.tenant_id, field_name="tenant_id")
        object.__setattr__(self, "active_alerts", tuple(self.active_alerts))
        object.__setattr__(self, "metrics", deep_freeze(sanitize_security_data(dict(self.metrics))))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "health_status": self.health_status,
            "window_seconds": self.window_seconds,
            "evaluated_at": self.evaluated_at,
            "request_count": self.request_count,
            "error_rate": self.error_rate,
            "avg_latency_ms": self.avg_latency_ms,
            "p95_latency_ms": self.p95_latency_ms,
            "total_tokens": self.total_tokens,
            "total_cost_usd": self.total_cost_usd,
            "quota_status": self.quota_status,
            "billing_status": self.billing_status,
            "active_alerts_count": self.active_alerts_count,
            "active_alerts": [a.to_dict() for a in self.active_alerts],
            "metrics": _json_safe(self.metrics),
            "checksum": self.checksum,
        }
