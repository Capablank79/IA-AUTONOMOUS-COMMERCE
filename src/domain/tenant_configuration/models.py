"""Modelos de dominio para configuración versionada por tenant (O.11)."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
from types import MappingProxyType
from typing import Any, Mapping, Optional, Tuple

from src.domain.security.models import deep_freeze, validate_safe_identifier
from src.domain.secrets.models import SecretReference


class TenantConfigurationError(Exception):
    pass


class ConfigurationValidationError(TenantConfigurationError, ValueError):
    pass


class ConfigurationVersionConflictError(TenantConfigurationError):
    pass


class ConfigurationIntegrityError(TenantConfigurationError):
    pass


class ConfigurationScope(str, Enum):
    TENANT = "TENANT"
    ORGANIZATION = "ORGANIZATION"


@dataclass(frozen=True)
class TenantConfigurationKey:
    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or not self.value.strip():
            raise ConfigurationValidationError("configuration key must be a non-empty string")
        object.__setattr__(self, "value", self.value.strip().lower())

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class TenantConfigurationValue:
    value: Any

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", deep_freeze(self.value))


@dataclass(frozen=True, order=True)
class TenantConfigurationVersion:
    value: int

    def __post_init__(self) -> None:
        if isinstance(self.value, bool) or not isinstance(self.value, int) or self.value < 1:
            raise ConfigurationValidationError("configuration version must be an integer >= 1")

    def __int__(self) -> int:
        return self.value


ConfigurationKey = TenantConfigurationKey
ConfigurationValue = TenantConfigurationValue
ConfigurationVersion = TenantConfigurationVersion


def _json_value(value: Any) -> Any:
    if isinstance(value, SecretReference):
        return {"__type__": "SecretReference", **value.to_dict()}
    if isinstance(value, Mapping):
        return {str(k): _json_value(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(v) for v in value]
    return value


def compute_configuration_checksum(
    tenant_id: str, scope: ConfigurationScope, key: str, value: Any,
    version: int, organization_id: Optional[str], effective_from: datetime,
    updated_at: datetime, updated_by: str,
) -> str:
    payload = {
        "tenant_id": tenant_id, "scope": scope.value, "organization_id": organization_id,
        "key": key, "value": _json_value(value), "version": version,
        "effective_from": effective_from.isoformat(), "updated_at": updated_at.isoformat(),
        "updated_by": updated_by,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TenantConfiguration:
    tenant_id: str
    key: TenantConfigurationKey
    value: TenantConfigurationValue
    version: TenantConfigurationVersion
    scope: ConfigurationScope = ConfigurationScope.TENANT
    organization_id: Optional[str] = None
    effective_from: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_by: str = "system"
    checksum: str = ""

    def __post_init__(self) -> None:
        validate_safe_identifier(self.tenant_id, "tenant_id")
        if not isinstance(self.key, TenantConfigurationKey):
            object.__setattr__(self, "key", TenantConfigurationKey(str(self.key)))
        if not isinstance(self.value, TenantConfigurationValue):
            object.__setattr__(self, "value", TenantConfigurationValue(self.value))
        if not isinstance(self.version, TenantConfigurationVersion):
            object.__setattr__(self, "version", TenantConfigurationVersion(int(self.version)))
        if not isinstance(self.scope, ConfigurationScope):
            object.__setattr__(self, "scope", ConfigurationScope(self.scope))
        if self.scope == ConfigurationScope.ORGANIZATION:
            if not self.organization_id:
                raise ConfigurationValidationError("organization_id is required for ORGANIZATION scope")
            validate_safe_identifier(self.organization_id, "organization_id")
        elif self.organization_id is not None:
            raise ConfigurationValidationError("organization_id is only valid for ORGANIZATION scope")
        if not isinstance(self.updated_by, str) or not self.updated_by.strip():
            raise ConfigurationValidationError("updated_by must be a non-empty string")
        if self.effective_from.tzinfo is None or self.updated_at.tzinfo is None:
            raise ConfigurationValidationError("configuration timestamps must be timezone-aware")
        expected = compute_configuration_checksum(
            self.tenant_id, self.scope, self.key.value, self.value.value,
            self.version.value, self.organization_id, self.effective_from,
            self.updated_at, self.updated_by,
        )
        if self.checksum and self.checksum != expected:
            raise ConfigurationIntegrityError("tenant configuration checksum mismatch")
        object.__setattr__(self, "checksum", expected)

    def verify_integrity(self) -> bool:
        return self.checksum == compute_configuration_checksum(
            self.tenant_id, self.scope, self.key.value, self.value.value,
            self.version.value, self.organization_id, self.effective_from,
            self.updated_at, self.updated_by,
        )


@dataclass(frozen=True)
class ConfigurationDecision:
    tenant_id: str
    organization_id: Optional[str]
    effective_values: Mapping[str, Any]
    sources: Mapping[str, str]
    resolved_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    applied_versions: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        validate_safe_identifier(self.tenant_id, "tenant_id")
        if self.organization_id is not None:
            validate_safe_identifier(self.organization_id, "organization_id")
        if self.resolved_at.tzinfo is None:
            raise ConfigurationValidationError("resolved_at must be timezone-aware")
        object.__setattr__(self, "effective_values", deep_freeze(dict(self.effective_values)))
        object.__setattr__(self, "sources", MappingProxyType(dict(self.sources)))
        object.__setattr__(self, "applied_versions", tuple(self.applied_versions))
