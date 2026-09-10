from datetime import datetime, timezone
from dataclasses import FrozenInstanceError

import pytest

from src.application.tenant_configuration.tenant_configuration_service import (
    TenantConfigurationService,
    CONFIGURATION_CATALOG,
)
from src.domain.model_gateway.models import TenantModelConfig
from src.domain.secrets.models import SecretReference
from src.domain.tenant_configuration.models import (
    ConfigurationScope,
    ConfigurationValidationError,
    ConfigurationVersionConflictError,
    TenantConfigurationKey,
    TenantConfigurationValue,
    TenantConfigurationVersion,
    TenantConfiguration,
    ConfigurationDecision,
)
from src.infrastructure.persistence.data.json.tenant_configuration_repository import JsonTenantConfigurationRepository


def service(tmp_path):
    return TenantConfigurationService(JsonTenantConfigurationRepository(tmp_path))


def test_models_are_frozen_and_version_history_is_preserved(tmp_path):
    svc = service(tmp_path)
    first = svc.set_configuration("tenant-a", "branding.display_name", "Alpha", "admin", expected_version=0)
    second = svc.set_configuration("tenant-a", "branding.display_name", "Beta", "admin", expected_version=1)
    assert [item.version.value for item in svc.get_history("tenant-a", "branding.display_name")] == [1, 2]
    assert first.effective_from.tzinfo and second.updated_at.tzinfo and second.checksum
    with pytest.raises(FrozenInstanceError):
        second.updated_by = "other"


def test_tenant_required_and_unknown_key_rejected(tmp_path):
    svc = service(tmp_path)
    with pytest.raises(ValueError):
        svc.set_configuration("", "branding.theme", "dark", "admin")
    with pytest.raises(ConfigurationValidationError):
        svc.set_configuration("tenant-a", "unknown.key.does.not.exist", "value", "admin")


def test_catalog_rejects_unknown_types_ranges_security_and_raw_secrets_before_persistence(tmp_path):
    svc = service(tmp_path)
    invalid = [
        ("unknown.key", "x"),
        ("feature.notifications_enabled", "yes"),
        ("operational.notification_threshold_percent", 101),
        ("operational.notification_threshold_percent", 0),
        ("security.mfa_required", True),
        ("global.security.bypass_rbac", True),
        ("rbac.admin_override", True),
        ("authorization.disabled", True),
        ("model.credential_reference", "raw-api-key"),
        ("api_key", "raw-api-key"),
        ("branding.theme", "unsupported_theme"),
        ("branding.logo_url", "http://insecure-logo.com/logo.png"),
        ("branding.locale", "invalid_locale"),
        ("marketplace.currency", "INVALID_CURRENCY"),
    ]
    for key, value in invalid:
        with pytest.raises(ConfigurationValidationError):
            svc.set_configuration("tenant-a", key, value, "admin")
    assert svc.repository.list_latest("tenant-a") == ()


def test_secret_reference_is_allowed_only_for_explicit_key(tmp_path):
    svc = service(tmp_path)
    ref = SecretReference("ref-1", "vault", "tenant-model-key")
    saved = svc.set_configuration("tenant-a", "model.credential_reference", ref, "admin")
    assert saved.value.value == ref
    with pytest.raises(ConfigurationValidationError):
        svc.set_configuration("tenant-a", "branding.display_name", ref, "admin")


def test_idempotency_and_optimistic_version_conflict(tmp_path):
    svc = service(tmp_path)
    first = svc.set_configuration("tenant-a", "branding.theme", "dark", "admin", expected_version=0)
    replay = svc.set_configuration("tenant-a", "branding.theme", "dark", "admin", expected_version=1)
    assert replay == first
    assert len(svc.get_history("tenant-a", "branding.theme")) == 1
    with pytest.raises(ConfigurationVersionConflictError):
        svc.set_configuration("tenant-a", "branding.theme", "light", "admin", expected_version=0)


def test_precedence_bounds_and_actual_usage_notification(tmp_path):
    svc = service(tmp_path)
    svc.set_configuration("tenant-a", "branding.locale", "es-CL", "admin")
    svc.set_configuration("tenant-a", "operational.quota_preference", 900, "admin")
    svc.set_configuration(
        "tenant-a", "branding.locale", "pt-BR", "org-admin",
        scope=ConfigurationScope.ORGANIZATION, organization_id="org-1",
    )
    decision = svc.resolve_effective(
        "tenant-a", "org-1", plan_values={"branding.locale": "en-US"},
        quota_bounds={"operational.quota_preference": 500}, actual_usage=400,
    )
    assert decision.effective_values["branding.locale"] == "pt-BR"
    assert decision.sources["branding.locale"] == "ORGANIZATION"
    assert decision.effective_values["operational.quota_preference"] == 500
    assert decision.sources["operational.quota_preference"] == "BOUND"
    assert decision.effective_values["operational.quota_notification_due"] is True


def test_feature_flag_effective_logic_respects_plan_entitlement(tmp_path):
    svc = service(tmp_path)
    # Plan allows experimental = False, but tenant enables it
    svc.set_configuration("tenant-a", "feature.experimental_enabled", True, "admin")
    decision = svc.resolve_effective("tenant-a", plan_values={"feature.experimental_enabled": False})
    assert decision.effective_values["feature.experimental_enabled"] is False
    assert decision.sources["feature.experimental_enabled"] == "PLAN"

    # Plan allows experimental = True, tenant enables it
    decision_allowed = svc.resolve_effective("tenant-a", plan_values={"feature.experimental_enabled": True})
    assert decision_allowed.effective_values["feature.experimental_enabled"] is True
    assert decision_allowed.sources["feature.experimental_enabled"] == "TENANT"


def test_organization_never_crosses_tenant(tmp_path):
    svc = service(tmp_path)
    svc.set_configuration(
        "tenant-a", "branding.theme", "dark", "admin",
        scope=ConfigurationScope.ORGANIZATION, organization_id="org-1",
    )
    assert svc.resolve_effective("tenant-b", "org-1").effective_values["branding.theme"] == "system"


def test_o5_adapter_preserves_allowed_providers_models_and_fallback(tmp_path):
    svc = service(tmp_path)
    base = TenantModelConfig(
        tenant_id="tenant-a", allowed_providers=("openai",),
        allowed_models=("gpt-safe",), allow_fallback=False,
    )
    svc.set_configuration("tenant-a", "model.preferred_provider", "anthropic", "admin")
    svc.set_configuration("tenant-a", "model.preferred_model", "forbidden-model", "admin")
    effective = svc.to_tenant_model_config("tenant-a", base)
    assert effective.allowed_providers == ("openai",)
    assert effective.allowed_models == ("gpt-safe",)
    assert effective.preferred_route_id is None
    assert effective.allow_fallback is False

    # When provider and model are in allowed lists
    svc.set_configuration("tenant-a", "model.preferred_provider", "openai", "admin")
    svc.set_configuration("tenant-a", "model.preferred_model", "gpt-safe", "admin")
    effective_valid = svc.to_tenant_model_config("tenant-a", base)
    assert effective_valid.preferred_route_id == "openai:gpt-safe"
