"""
Modelos de dominio para Plans & Pricing Tiers SaaS (Hito O.8 — Plans & Pricing Tiers).

Define:
- PlanTier: Tiers canónicos de suscripción (FREE, PRO, ENTERPRISE, CUSTOM).
- PlanStatus: Estados del ciclo de vida del Plan (ACTIVE, DEPRECATED, ARCHIVED, DRAFT).
- PlanFeature: Catálogo explícito de capabilities funcionales/comerciales gobernadas por planes.
- PlanLimits: Límites cuantitativos del plan (p.ej. max_users, max_custom_models, rate limits base).
- PlanQuotaTemplate: Plantilla de reglas de cuotas O.7 materializables determinísticamente.
- Plan: Entidad inmutable y versionada que define capabilities, límites y plantilla de cuotas.
- PlanAssignmentStatus: Estados de la asignación de plan al tenant (ACTIVE, SUSPENDED, EXPIRED, SUPERSEDED).
- PlanAssignment: Asignación inmutable tenant-scoped de un plan y versión efectiva a un Tenant.
- PlanEntitlementStatus: Estados de decisión de entitlement (ALLOW, DENY, UNKNOWN, ERROR).
- PlanEntitlementRequest: Petición de evaluación de capability/feature/model de un tenant.
- PlanEntitlementDecision: Decisión inmutable, determinista y auditable de entitlement con checksum SHA-256.
- Excepciones de dominio para Plans & Pricing Tiers.

Principios O.8:
1. Responde a: "¿Qué capacidades y límites base obtiene un tenant según su plan?".
2. Plan Entitlement AND RBAC/Authorization deben cumplirse (Plan habilita capability comercial; RBAC decide permisos del usuario).
3. No Payment/Billing (O.9): Cero campos de tarjetas, cobros, facturas, impuestos, Stripe/MercadoPago.
4. Determinismo y versión inmutable: Cambios en catálogo o límites requieren nueva versión o actualización controlada.
5. Fail-Safe: Missing plan o plan desconocido resulta en UNKNOWN / restricted safe default (nunca Enterprise por defecto).
6. Aislamiento Multi-Tenant estricto: Las asignaciones de un tenant no afectan a otros.
7. Mapeo determinista Plan -> QuotaPolicy O.7 sin duplicar source of truth.
8. Preservación histórica: Upgrade / Downgrade no borran ni manipulan el usage histórico de O.6.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
import hashlib
import json
from types import MappingProxyType
from typing import Mapping, Optional, Any, Tuple, Sequence, Dict, Union, List
import uuid

from src.domain.security.models import (
    validate_safe_identifier,
    sanitize_security_data,
    deep_freeze,
)
from src.domain.quota_management.models import (
    QuotaRule,
    QuotaPolicy,
    QuotaType,
    QuotaScope,
    QuotaWindowType,
)


class PlanManagementError(Exception):
    """Excepción base para errores del dominio de Plans & Pricing Tiers."""
    pass


class PlanNotFoundError(PlanManagementError):
    """Se lanza cuando un plan solicitado no existe en el catálogo."""
    pass


class PlanVersionNotFoundError(PlanManagementError):
    """Se lanza cuando una versión específica de un plan no existe."""
    pass


class PlanAssignmentNotFoundError(PlanManagementError):
    """Se lanza cuando un tenant no posee asignación activa de plan."""
    pass


class PlanIntegrityError(PlanManagementError):
    """Se lanza cuando se detecta corrupción o discrepancia de checksum en un Plan o PlanAssignment."""
    pass


class PlanTier(str, Enum):
    """Tiers canónicos de planes SaaS."""
    FREE = "FREE"
    PRO = "PRO"
    ENTERPRISE = "ENTERPRISE"
    CUSTOM = "CUSTOM"


class PlanStatus(str, Enum):
    """Estados del ciclo de vida de un Plan en el catálogo."""
    ACTIVE = "ACTIVE"
    DEPRECATED = "DEPRECATED"
    ARCHIVED = "ARCHIVED"
    DRAFT = "DRAFT"


class PlanFeature(str, Enum):
    """
    Catálogo explícito de capabilities y features gobernadas por planes comerciales.
    Nota: Estas capabilities representan habilitación comercial SaaS, NO permisos RBAC individuales.
    """
    MODEL_INFERENCE = "MODEL_INFERENCE"
    ADVANCED_MODELS = "ADVANCED_MODELS"
    AUTONOMOUS_MISSIONS = "AUTONOMOUS_MISSIONS"
    MARKETPLACE_OPERATIONS = "MARKETPLACE_OPERATIONS"
    MULTI_USER = "MULTI_USER"
    ADVANCED_ANALYTICS = "ADVANCED_ANALYTICS"
    CUSTOM_INTEGRATIONS = "CUSTOM_INTEGRATIONS"
    BYO_KEY = "BYO_KEY"


class PlanAssignmentStatus(str, Enum):
    """Estados del ciclo de vida de una asignación de plan a un Tenant."""
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    EXPIRED = "EXPIRED"
    SUPERSEDED = "SUPERSEDED"


class PlanEntitlementStatus(str, Enum):
    """Estados canónicos de decisión de entitlement."""
    ALLOW = "ALLOW"
    DENY = "DENY"
    UNKNOWN = "UNKNOWN"
    ERROR = "ERROR"


@dataclass(frozen=True)
class PlanLimits:
    """
    Límites cuantitativos base configurados a nivel de plan.
    """
    max_users: int = 1
    max_organizations: int = 1
    max_concurrent_missions: int = 1
    max_marketplace_accounts: int = 1
    custom_limits: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.max_users < 1:
            raise ValueError("max_users must be at least 1")
        if self.max_organizations < 1:
            raise ValueError("max_organizations must be at least 1")
        if self.max_concurrent_missions < 0:
            raise ValueError("max_concurrent_missions cannot be negative")
        if self.max_marketplace_accounts < 0:
            raise ValueError("max_marketplace_accounts cannot be negative")

        if not isinstance(self.custom_limits, MappingProxyType):
            object.__setattr__(self, "custom_limits", MappingProxyType(dict(self.custom_limits)))


@dataclass(frozen=True)
class PlanQuotaTemplate:
    """
    Plantilla inmutable de reglas de cuota (O.7) asociadas a una versión de plan.
    Permite materializar determinísticamente una QuotaPolicy para el Tenant.
    """
    rules: Tuple[QuotaRule, ...] = field(default_factory=tuple)
    is_unlimited: bool = False
    description: Optional[str] = None

    def __post_init__(self):
        if isinstance(self.rules, (list, Sequence)):
            object.__setattr__(self, "rules", tuple(self.rules))


@dataclass(frozen=True)
class Plan:
    """
    Entidad inmutable y versionada que define capabilities, límites y plantilla de cuotas.
    """
    plan_id: str
    name: str
    tier: PlanTier
    version: str = "1.0.0"
    status: PlanStatus = PlanStatus.ACTIVE
    features: Tuple[PlanFeature, ...] = field(default_factory=tuple)
    limits: PlanLimits = field(default_factory=PlanLimits)
    quota_template: PlanQuotaTemplate = field(default_factory=PlanQuotaTemplate)
    allowed_model_classes: Tuple[str, ...] = field(default_factory=tuple)
    allowed_providers: Tuple[str, ...] = field(default_factory=tuple)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    checksum: str = field(default="")

    def __post_init__(self):
        validate_safe_identifier(self.plan_id, "plan_id")
        if not self.name or not self.name.strip():
            raise ValueError("Plan name cannot be empty")
        if not self.version or not self.version.strip():
            raise ValueError("Plan version cannot be empty")

        if isinstance(self.features, (list, Sequence)):
            # Asegurar deduplicación determinista ordenada por enum value
            features_sorted = tuple(sorted(set(self.features), key=lambda x: x.value if isinstance(x, PlanFeature) else str(x)))
            object.__setattr__(self, "features", features_sorted)

        if isinstance(self.allowed_model_classes, (list, Sequence)):
            object.__setattr__(self, "allowed_model_classes", tuple(sorted(set(self.allowed_model_classes))))

        if isinstance(self.allowed_providers, (list, Sequence)):
            object.__setattr__(self, "allowed_providers", tuple(sorted(set(self.allowed_providers))))

        if not isinstance(self.metadata, MappingProxyType):
            sanitized = sanitize_security_data(dict(self.metadata))
            object.__setattr__(self, "metadata", MappingProxyType(sanitized))

        calc_checksum = self._compute_checksum()
        if not self.checksum:
            object.__setattr__(self, "checksum", calc_checksum)
        elif self.checksum != calc_checksum:
            raise PlanIntegrityError(
                f"Plan checksum mismatch for plan_id={self.plan_id}, version={self.version}: "
                f"provided={self.checksum}, calculated={calc_checksum}"
            )

    def _compute_checksum(self) -> str:
        feats = ",".join(f.value for f in self.features)
        models = ",".join(self.allowed_model_classes)
        provs = ",".join(self.allowed_providers)
        rules_repr = [
            f"{r.rule_id}:{r.quota_type.value}:{r.scope.value}:{r.limit_value}:{r.window_type.value}"
            for r in sorted(self.quota_template.rules, key=lambda x: x.rule_id)
        ]
        quota_str = f"{self.quota_template.is_unlimited}|{','.join(rules_repr)}"
        raw = f"{self.plan_id}|{self.tier.value}|{self.version}|{self.status.value}|{feats}|{models}|{provs}|{quota_str}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def verify_integrity(self) -> bool:
        return self.checksum == self._compute_checksum()

    def has_feature(self, feature: Union[PlanFeature, str]) -> bool:
        if isinstance(feature, str):
            try:
                feature = PlanFeature(feature)
            except ValueError:
                return False
        return feature in self.features

    def is_model_allowed(self, model_class_or_id: Optional[str]) -> bool:
        if not model_class_or_id:
            return True
        if not self.allowed_model_classes:
            return True
        return model_class_or_id in self.allowed_model_classes or "*" in self.allowed_model_classes

    def is_provider_allowed(self, provider: Optional[str]) -> bool:
        if not provider:
            return True
        if not self.allowed_providers:
            return True
        return provider.lower() in [p.lower() for p in self.allowed_providers] or "*" in self.allowed_providers

    def materialize_quota_policy(self, tenant_id: str, policy_id_override: Optional[str] = None) -> QuotaPolicy:
        """
        Materializa determinísticamente una QuotaPolicy de O.7 asociada al tenant para esta versión del plan.
        """
        validate_safe_identifier(tenant_id, "tenant_id")
        policy_id = policy_id_override or f"qpol_{tenant_id}_{self.plan_id.lower()}_{self.version.replace('.', '_')}"
        return QuotaPolicy(
            policy_id=policy_id,
            tenant_id=tenant_id,
            rules=self.quota_template.rules,
            policy_version=self.version,
            is_unlimited=self.quota_template.is_unlimited,
            description=f"Materialized Quota Policy from Plan {self.name} (v{self.version})",
        )


@dataclass(frozen=True)
class PlanAssignment:
    """
    Asignación explícita e inmutable de un Plan y versión a un Tenant específico.
    """
    assignment_id: str
    tenant_id: str
    plan_id: str
    plan_version: str
    assigned_at: datetime
    effective_from: datetime
    status: PlanAssignmentStatus = PlanAssignmentStatus.ACTIVE
    effective_until: Optional[datetime] = None
    source_reason: str = "INITIAL_ASSIGNMENT"
    assigned_by_actor_id: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    checksum: str = field(default="")

    def __post_init__(self):
        validate_safe_identifier(self.assignment_id, "assignment_id")
        validate_safe_identifier(self.tenant_id, "tenant_id")
        validate_safe_identifier(self.plan_id, "plan_id")
        if self.assigned_by_actor_id:
            validate_safe_identifier(self.assigned_by_actor_id, "assigned_by_actor_id")

        if self.assigned_at.tzinfo is None:
            object.__setattr__(self, "assigned_at", self.assigned_at.replace(tzinfo=timezone.utc))
        if self.effective_from.tzinfo is None:
            object.__setattr__(self, "effective_from", self.effective_from.replace(tzinfo=timezone.utc))
        if self.effective_until is not None and self.effective_until.tzinfo is None:
            object.__setattr__(self, "effective_until", self.effective_until.replace(tzinfo=timezone.utc))

        if self.effective_until is not None and self.effective_from > self.effective_until:
            raise ValueError(f"effective_from {self.effective_from} cannot be after effective_until {self.effective_until}")

        if not isinstance(self.metadata, MappingProxyType):
            sanitized = sanitize_security_data(dict(self.metadata))
            object.__setattr__(self, "metadata", MappingProxyType(sanitized))

        calc_checksum = self._compute_checksum()
        if not self.checksum:
            object.__setattr__(self, "checksum", calc_checksum)
        elif self.checksum != calc_checksum:
            raise PlanIntegrityError(
                f"PlanAssignment checksum mismatch for assignment_id={self.assignment_id}: "
                f"provided={self.checksum}, calculated={calc_checksum}"
            )

    def _compute_checksum(self) -> str:
        until_str = self.effective_until.isoformat() if self.effective_until else "NONE"
        raw = f"{self.assignment_id}|{self.tenant_id}|{self.plan_id}|{self.plan_version}|{self.effective_from.isoformat()}|{until_str}|{self.status.value}|{self.source_reason}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def verify_integrity(self) -> bool:
        return self.checksum == self._compute_checksum()

    def is_effective(self, current_time: datetime) -> bool:
        """Determina si la asignación está activa y dentro de su rango de vigencia."""
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=timezone.utc)
        if self.status != PlanAssignmentStatus.ACTIVE:
            return False
        if current_time < self.effective_from:
            return False
        if self.effective_until is not None and current_time >= self.effective_until:
            return False
        return True


@dataclass(frozen=True)
class PlanEntitlementRequest:
    """
    Petición de evaluación de capability / feature / model para un tenant.
    """
    tenant_id: str
    feature: Optional[Union[PlanFeature, str]] = None
    model_id: Optional[str] = None
    provider: Optional[str] = None
    requested_user_count: Optional[int] = None
    correlation_id: Optional[str] = None
    request_timestamp: Optional[datetime] = None

    def __post_init__(self):
        validate_safe_identifier(self.tenant_id, "tenant_id")
        if self.request_timestamp is None:
            object.__setattr__(self, "request_timestamp", datetime.now(timezone.utc))
        elif self.request_timestamp.tzinfo is None:
            object.__setattr__(self, "request_timestamp", self.request_timestamp.replace(tzinfo=timezone.utc))


@dataclass(frozen=True)
class PlanEntitlementDecision:
    """
    Decisión inmutable y auditable de entitlement comercial de un tenant.
    """
    decision_id: str
    status: PlanEntitlementStatus
    tenant_id: str
    plan_id: Optional[str]
    plan_version: Optional[str]
    feature: Optional[str]
    is_entitled: bool
    reason_code: str
    limits_ref: Optional[PlanLimits] = None
    evaluated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    correlation_id: Optional[str] = None
    rationale: Optional[str] = None
    checksum: str = field(default="")

    def __post_init__(self):
        validate_safe_identifier(self.decision_id, "decision_id")
        validate_safe_identifier(self.tenant_id, "tenant_id")
        if self.plan_id:
            validate_safe_identifier(self.plan_id, "plan_id")

        if self.evaluated_at.tzinfo is None:
            object.__setattr__(self, "evaluated_at", self.evaluated_at.replace(tzinfo=timezone.utc))

        calc_checksum = self._compute_checksum()
        if not self.checksum:
            object.__setattr__(self, "checksum", calc_checksum)
        elif self.checksum != calc_checksum:
            raise PlanIntegrityError(
                f"PlanEntitlementDecision checksum mismatch: provided={self.checksum}, calculated={calc_checksum}"
            )

    def _compute_checksum(self) -> str:
        raw = f"{self.decision_id}|{self.status.value}|{self.tenant_id}|{self.plan_id}|{self.plan_version}|{self.feature}|{self.is_entitled}|{self.reason_code}|{self.evaluated_at.isoformat()}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def verify_integrity(self) -> bool:
        return self.checksum == self._compute_checksum()
