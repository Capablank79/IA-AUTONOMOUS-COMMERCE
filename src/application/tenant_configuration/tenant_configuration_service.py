"""Servicio de configuración efectiva y gobernada por tenant (O.11)."""

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import uuid
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from src.domain.audit.models import AuditActor, AuditActorType, AuditRecord, AuditRecordType
from src.domain.audit.ports import AuditRepositoryPort
from src.domain.model_gateway.models import TenantModelConfig
from src.domain.security.models import SENSITIVE_KEYS, validate_safe_identifier
from src.domain.secrets.models import SecretReference
from src.domain.tenant_configuration.models import (
    ConfigurationDecision, ConfigurationScope, ConfigurationValidationError,
    ConfigurationVersionConflictError, TenantConfiguration, TenantConfigurationKey,
    TenantConfigurationValue, TenantConfigurationVersion,
)
from src.domain.tenant_configuration.ports import TenantConfigurationRepositoryPort


@dataclass(frozen=True)
class _Schema:
    value_type: Any
    default: Any
    minimum: Optional[int] = None
    maximum: Optional[int] = None
    choices: Tuple[str, ...] = ()
    allow_secret_reference: bool = False


CONFIGURATION_CATALOG: Mapping[str, _Schema] = {
    "branding.display_name": _Schema(str, "Autonomous Commerce"),
    "branding.logo_url": _Schema(str, ""),
    "branding.theme": _Schema(str, "system", choices=("light", "dark", "system")),
    "branding.locale": _Schema(str, "en-US"),
    "model.preferred_provider": _Schema(str, ""),
    "model.preferred_model": _Schema(str, ""),
    "model.fallback_enabled": _Schema(bool, True),
    "model.credential_reference": _Schema(SecretReference, None, allow_secret_reference=True),
    "marketplace.locale": _Schema(str, "en-US"),
    "marketplace.currency": _Schema(str, "USD", choices=("USD", "EUR", "GBP", "CLP", "BRL")),
    "marketplace.default_marketplace": _Schema(str, ""),
    "feature.experimental_enabled": _Schema(bool, False),
    "feature.notifications_enabled": _Schema(bool, True),
    "operational.quota_preference": _Schema(int, 100, minimum=1, maximum=1_000_000_000),
    "operational.notification_threshold_percent": _Schema(int, 80, minimum=1, maximum=100),
}

_GLOBAL_SECURITY_PREFIXES = ("security.", "global.security.", "authentication.", "authorization.", "rbac.")


class TenantConfigurationService:
    def __init__(
        self, repository: TenantConfigurationRepositoryPort,
        audit_repository: Optional[AuditRepositoryPort] = None,
        clock: Optional[Any] = None,
    ) -> None:
        self.repository = repository
        self.audit_repository = audit_repository
        self.clock = clock

    def _now(self) -> datetime:
        value = self.clock.now() if self.clock else datetime.now(timezone.utc)
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    def _audit(
        self, record_type: AuditRecordType, tenant_id: str, actor_id: str,
        key: str, status: str, correlation_id: Optional[str], details: Mapping[str, Any],
    ) -> None:
        if not self.audit_repository:
            return
        self.audit_repository.append(AuditRecord(
            audit_id=f"audit_cfg_{uuid.uuid4().hex[:16]}", record_type=record_type,
            occurred_at=self._now(), actor=AuditActor(AuditActorType.USER, actor_id),
            subject_type="TENANT_CONFIGURATION", subject_id=f"{tenant_id}:{key}",
            action_or_operation=record_type.value, status=status,
            correlation_id=correlation_id or f"cfg_{uuid.uuid4().hex[:16]}",
            provenance="TENANT_CONFIGURATION_SERVICE",
            metadata={"tenant_id": tenant_id, "key": key, **dict(details)},
        ))

    def validate_value(self, key: str, value: Any) -> TenantConfigurationValue:
        normalized_key = TenantConfigurationKey(key).value
        if normalized_key.startswith(_GLOBAL_SECURITY_PREFIXES):
            raise ConfigurationValidationError("global security configuration is not tenant-configurable")
        schema = CONFIGURATION_CATALOG.get(normalized_key)
        if schema is None:
            raise ConfigurationValidationError(f"unknown configuration key: {normalized_key}")
        key_parts = set(normalized_key.replace("-", "_").split("."))
        if key_parts.intersection(SENSITIVE_KEYS) and not schema.allow_secret_reference:
            raise ConfigurationValidationError("sensitive configuration keys are forbidden")
        if schema.allow_secret_reference:
            if not isinstance(value, SecretReference):
                raise ConfigurationValidationError(f"{normalized_key} requires SecretReference, never a raw secret")
            return TenantConfigurationValue(value)
        expected = schema.value_type
        if expected is int:
            valid_type = isinstance(value, int) and not isinstance(value, bool)
        else:
            valid_type = isinstance(value, expected)
        if not valid_type:
            raise ConfigurationValidationError(f"{normalized_key} requires {expected.__name__}")
        if isinstance(value, str):
            if normalized_key != "branding.logo_url" and not value.strip() and schema.default != "":
                raise ConfigurationValidationError(f"{normalized_key} cannot be blank")
            if len(value) > 2048:
                raise ConfigurationValidationError(f"{normalized_key} exceeds maximum length")
            if schema.choices and value not in schema.choices:
                raise ConfigurationValidationError(f"{normalized_key} must be one of {schema.choices}")
            if normalized_key == "branding.logo_url" and value and not value.startswith("https://"):
                raise ConfigurationValidationError("branding.logo_url must use https")
            if normalized_key.endswith(".locale") and value and (len(value) > 20 or "-" not in value):
                raise ConfigurationValidationError(f"invalid locale: {value}")
        if isinstance(value, int):
            if schema.minimum is not None and value < schema.minimum:
                raise ConfigurationValidationError(f"{normalized_key} is below minimum {schema.minimum}")
            if schema.maximum is not None and value > schema.maximum:
                raise ConfigurationValidationError(f"{normalized_key} exceeds maximum {schema.maximum}")
        return TenantConfigurationValue(value)

    def set_configuration(
        self, tenant_id: str, key: str, value: Any, updated_by: str,
        expected_version: Optional[int] = None,
        scope: ConfigurationScope = ConfigurationScope.TENANT,
        organization_id: Optional[str] = None,
        effective_from: Optional[datetime] = None,
        correlation_id: Optional[str] = None,
    ) -> TenantConfiguration:
        validate_safe_identifier(tenant_id, "tenant_id")
        normalized_key = TenantConfigurationKey(key).value
        try:
            validated = self.validate_value(normalized_key, value)
            current = self.repository.get_latest(tenant_id, normalized_key, scope, organization_id)
            current_version = current.version.value if current else 0
            if expected_version is not None and expected_version != current_version:
                raise ConfigurationVersionConflictError(
                    f"stale configuration version: expected {expected_version}, current {current_version}"
                )
            if current and current.value == validated:
                return current
            now = self._now()
            configuration = TenantConfiguration(
                tenant_id=tenant_id, key=TenantConfigurationKey(normalized_key), value=validated,
                version=TenantConfigurationVersion(current_version + 1), scope=scope,
                organization_id=organization_id, effective_from=effective_from or now,
                updated_at=now, updated_by=updated_by,
            )
            saved = self.repository.save(configuration)
            self._audit(AuditRecordType.TENANT_CONFIGURATION_UPDATED, tenant_id, updated_by,
                        normalized_key, "SUCCESS", correlation_id,
                        {"scope": scope.value, "version": saved.version.value,
                         "organization_id": organization_id})
            return saved
        except (ConfigurationValidationError, ConfigurationVersionConflictError, ValueError) as exc:
            self._audit(AuditRecordType.TENANT_CONFIGURATION_REJECTED, tenant_id, updated_by,
                        normalized_key, "REJECTED", correlation_id,
                        {"reason": str(exc), "scope": getattr(scope, "value", str(scope))})
            raise

    def get_configuration(
        self, tenant_id: str, key: str, scope: ConfigurationScope = ConfigurationScope.TENANT,
        organization_id: Optional[str] = None,
    ) -> Optional[TenantConfiguration]:
        return self.repository.get_latest(tenant_id, TenantConfigurationKey(key).value, scope, organization_id)

    def get_history(
        self, tenant_id: str, key: str, scope: ConfigurationScope = ConfigurationScope.TENANT,
        organization_id: Optional[str] = None,
    ) -> Sequence[TenantConfiguration]:
        return self.repository.list_history(tenant_id, TenantConfigurationKey(key).value, scope, organization_id)

    def resolve_effective(
        self, tenant_id: str, organization_id: Optional[str] = None,
        plan_values: Optional[Mapping[str, Any]] = None,
        model_bounds: Optional[Mapping[str, Any]] = None,
        quota_bounds: Optional[Mapping[str, Any]] = None,
        actual_usage: Optional[int] = None,
        actor_id: str = "system", correlation_id: Optional[str] = None,
    ) -> ConfigurationDecision:
        validate_safe_identifier(tenant_id, "tenant_id")
        effective: Dict[str, Any] = {key: schema.default for key, schema in CONFIGURATION_CATALOG.items()}
        sources: Dict[str, str] = {key: "DEFAULT" for key in effective}
        for source, values in (("PLAN", plan_values or {}), ("MODEL_BOUND", model_bounds or {}),
                               ("QUOTA_BOUND", quota_bounds or {})):
            for key, value in values.items():
                if key in CONFIGURATION_CATALOG:
                    effective[key] = self.validate_value(key, value).value
                    sources[key] = source
        versions = []
        now = self._now()
        for config in self.repository.list_latest(tenant_id, ConfigurationScope.TENANT):
            if config.effective_from <= now:
                effective[config.key.value] = config.value.value
                sources[config.key.value] = "TENANT"
                versions.append(f"TENANT:{config.key.value}:{config.version.value}")
        if organization_id:
            validate_safe_identifier(organization_id, "organization_id")
            for config in self.repository.list_latest(tenant_id, ConfigurationScope.ORGANIZATION, organization_id):
                if config.effective_from <= now:
                    effective[config.key.value] = config.value.value
                    sources[config.key.value] = "ORGANIZATION"
                    versions.append(f"ORGANIZATION:{organization_id}:{config.key.value}:{config.version.value}")
        # Hard plan/model/quota bounds are ceilings and cannot be bypassed by tenant/org preferences.
        # 1. Feature flags respect plan capability (effective = plan_entitled AND tenant_enabled)
        for key, value in (plan_values or {}).items():
            if key.startswith("feature.") and isinstance(value, bool):
                # If plan disables a feature, tenant/org cannot enable it
                if not value and effective.get(key) is True:
                    effective[key] = False
                    sources[key] = "PLAN"

        # 2. Hard numeric bounds (model, quota)
        all_bounds = {**(model_bounds or {}), **(quota_bounds or {})}
        for key, bound in all_bounds.items():
            if key in effective and isinstance(bound, int) and not isinstance(bound, bool):
                if isinstance(effective[key], int) and not isinstance(effective[key], bool) and effective[key] > bound:
                    effective[key] = bound
                    sources[key] = "BOUND"
        if actual_usage is not None and "operational.quota_preference" in effective:
            threshold = int(effective["operational.quota_preference"] *
                            effective["operational.notification_threshold_percent"] / 100)
            effective["operational.quota_notification_due"] = bool(
                effective["feature.notifications_enabled"] and actual_usage >= threshold
            )
            sources["operational.quota_notification_due"] = "ACTUAL_USAGE"
        decision = ConfigurationDecision(tenant_id, organization_id, effective, sources, now, tuple(versions))
        self._audit(AuditRecordType.TENANT_CONFIGURATION_RESOLVED, tenant_id, actor_id,
                    "effective", "SUCCESS", correlation_id,
                    {"organization_id": organization_id, "applied_versions": list(versions)})
        return decision

    def to_tenant_model_config(
        self, tenant_id: str, base_config: Optional[TenantModelConfig] = None,
        organization_id: Optional[str] = None,
        plan_values: Optional[Mapping[str, Any]] = None,
        model_bounds: Optional[Mapping[str, Any]] = None,
    ) -> TenantModelConfig:
        base = base_config or TenantModelConfig(tenant_id=tenant_id)
        if base.tenant_id != tenant_id:
            raise ConfigurationValidationError("base model configuration belongs to another tenant")
        decision = self.resolve_effective(tenant_id, organization_id, plan_values, model_bounds)
        provider = decision.effective_values["model.preferred_provider"]
        model = decision.effective_values["model.preferred_model"]
        if provider and not base.is_provider_allowed(provider):
            provider = ""
        if model and not base.is_model_allowed(model):
            model = ""
        route = f"{provider}:{model}" if provider and model else base.preferred_route_id
        refs = dict(base.credential_references)
        ref = decision.effective_values.get("model.credential_reference")
        if isinstance(ref, SecretReference) and provider:
            refs[provider] = ref
        return TenantModelConfig(
            tenant_id=tenant_id, allowed_providers=base.allowed_providers,
            allowed_models=base.allowed_models, preferred_route_id=route,
            credential_references=refs,
            allow_fallback=bool(decision.effective_values["model.fallback_enabled"] and base.allow_fallback),
            max_budget_tokens=base.max_budget_tokens, max_cost_limit=base.max_cost_limit,
            metadata={**dict(base.metadata), "configuration_versions": decision.applied_versions},
        )
