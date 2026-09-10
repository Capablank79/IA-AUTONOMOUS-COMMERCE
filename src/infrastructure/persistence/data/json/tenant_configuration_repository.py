"""Repositorio JSON tenant-scoped, versionado, íntegro y crash-safe para O.11."""

from datetime import datetime
import json
import os
from pathlib import Path
import threading
from typing import Any, Dict, List, Optional, Sequence, Union

from src.domain.security.models import validate_safe_identifier
from src.domain.secrets.models import SecretReference
from src.domain.tenant_configuration.models import (
    ConfigurationIntegrityError, ConfigurationScope, ConfigurationVersionConflictError,
    TenantConfiguration, TenantConfigurationKey, TenantConfigurationValue,
    TenantConfigurationVersion,
)
from src.domain.tenant_configuration.ports import TenantConfigurationRepositoryPort


class JsonTenantConfigurationRepository(TenantConfigurationRepositoryPort):
    def __init__(self, base_dir: Union[str, Path]):
        self.base_dir = Path(base_dir)
        self.tenants_dir = self.base_dir / "tenants"
        self.tenants_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _directory(
        self, tenant_id: str, scope: ConfigurationScope, organization_id: Optional[str]
    ) -> Path:
        validate_safe_identifier(tenant_id, "tenant_id")
        scope = ConfigurationScope(scope)
        directory = self.tenants_dir / tenant_id / "configuration" / "tenant"
        if scope == ConfigurationScope.ORGANIZATION:
            if not organization_id:
                raise ValueError("organization_id is required for ORGANIZATION scope")
            validate_safe_identifier(organization_id, "organization_id")
            directory = self.tenants_dir / tenant_id / "configuration" / "organizations" / organization_id
        elif organization_id is not None:
            raise ValueError("organization_id is only valid for ORGANIZATION scope")
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _path(self, tenant_id: str, key: str, scope: ConfigurationScope,
              organization_id: Optional[str], version: int) -> Path:
        validate_safe_identifier(key.replace(".", "_"), "configuration_key")
        return self._directory(tenant_id, scope, organization_id) / f"{key.replace('.', '__')}.v{version}.json"

    @staticmethod
    def _encode(value: Any) -> Any:
        if isinstance(value, SecretReference):
            return {"__type__": "SecretReference", **value.to_dict()}
        if isinstance(value, dict):
            return {k: JsonTenantConfigurationRepository._encode(v) for k, v in value.items()}
        if isinstance(value, (tuple, list)):
            return [JsonTenantConfigurationRepository._encode(v) for v in value]
        return value

    @staticmethod
    def _decode(value: Any) -> Any:
        if isinstance(value, dict) and value.get("__type__") == "SecretReference":
            payload = dict(value)
            payload.pop("__type__")
            return SecretReference.from_dict(payload)
        if isinstance(value, dict):
            return {k: JsonTenantConfigurationRepository._decode(v) for k, v in value.items()}
        if isinstance(value, list):
            return tuple(JsonTenantConfigurationRepository._decode(v) for v in value)
        return value

    def _to_dict(self, config: TenantConfiguration) -> Dict[str, Any]:
        return {
            "tenant_id": config.tenant_id, "scope": config.scope.value,
            "organization_id": config.organization_id, "key": config.key.value,
            "value": self._encode(config.value.value), "version": config.version.value,
            "effective_from": config.effective_from.isoformat(),
            "updated_at": config.updated_at.isoformat(), "updated_by": config.updated_by,
            "checksum": config.checksum,
        }

    def _from_dict(self, data: Dict[str, Any]) -> TenantConfiguration:
        try:
            return TenantConfiguration(
                tenant_id=data["tenant_id"], key=TenantConfigurationKey(data["key"]),
                value=TenantConfigurationValue(self._decode(data["value"])),
                version=TenantConfigurationVersion(int(data["version"])),
                scope=ConfigurationScope(data["scope"]), organization_id=data.get("organization_id"),
                effective_from=datetime.fromisoformat(data["effective_from"]),
                updated_at=datetime.fromisoformat(data["updated_at"]), updated_by=data["updated_by"],
                checksum=data["checksum"],
            )
        except ConfigurationIntegrityError:
            raise
        except Exception as exc:
            raise ConfigurationIntegrityError("corrupted tenant configuration record") from exc

    def _read(self, path: Path) -> TenantConfiguration:
        try:
            with open(path, "r", encoding="utf-8") as stream:
                data = json.load(stream)
        except Exception as exc:
            raise ConfigurationIntegrityError(f"cannot read tenant configuration record: {path.name}") from exc
        return self._from_dict(data)

    def save(self, configuration: TenantConfiguration) -> TenantConfiguration:
        if not configuration.verify_integrity():
            raise ConfigurationIntegrityError("tenant configuration checksum mismatch")
        with self._lock:
            latest = self.get_latest(configuration.tenant_id, configuration.key.value,
                                     configuration.scope, configuration.organization_id)
            expected = (latest.version.value + 1) if latest else 1
            if configuration.version.value != expected:
                if latest and latest.checksum == configuration.checksum:
                    return latest
                raise ConfigurationVersionConflictError(
                    f"configuration version must be {expected}, got {configuration.version.value}"
                )
            path = self._path(configuration.tenant_id, configuration.key.value,
                              configuration.scope, configuration.organization_id,
                              configuration.version.value)
            if path.exists():
                existing = self._read(path)
                if existing.checksum == configuration.checksum:
                    return existing
                raise ConfigurationVersionConflictError("configuration version already exists")
            tmp = path.with_suffix(path.suffix + ".tmp")
            try:
                with open(tmp, "w", encoding="utf-8") as stream:
                    json.dump(self._to_dict(configuration), stream, sort_keys=True,
                              indent=2, ensure_ascii=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(tmp, path)
                if hasattr(os, "O_DIRECTORY"):
                    fd = os.open(str(path.parent), os.O_DIRECTORY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
            finally:
                if tmp.exists():
                    tmp.unlink()
            return configuration

    def _matching(
        self, tenant_id: str, scope: ConfigurationScope, organization_id: Optional[str],
        key: Optional[str] = None,
    ) -> List[TenantConfiguration]:
        directory = self._directory(tenant_id, scope, organization_id)
        configs = []
        prefix = f"{key.replace('.', '__')}.v" if key else None
        for path in directory.glob("*.json"):
            if prefix and not path.name.startswith(prefix):
                continue
            config = self._read(path)
            if config.tenant_id != tenant_id or config.scope != ConfigurationScope(scope) or config.organization_id != organization_id:
                raise ConfigurationIntegrityError("tenant/scope mismatch in configuration record")
            configs.append(config)
        return configs

    def get_latest(self, tenant_id: str, key: str,
                   scope: ConfigurationScope = ConfigurationScope.TENANT,
                   organization_id: Optional[str] = None) -> Optional[TenantConfiguration]:
        with self._lock:
            items = self._matching(tenant_id, scope, organization_id, key)
            return max(items, key=lambda item: item.version.value) if items else None

    def list_latest(self, tenant_id: str,
                    scope: ConfigurationScope = ConfigurationScope.TENANT,
                    organization_id: Optional[str] = None) -> Sequence[TenantConfiguration]:
        with self._lock:
            latest: Dict[str, TenantConfiguration] = {}
            for item in self._matching(tenant_id, scope, organization_id):
                current = latest.get(item.key.value)
                if current is None or item.version.value > current.version.value:
                    latest[item.key.value] = item
            return tuple(sorted(latest.values(), key=lambda item: item.key.value))

    def list_history(self, tenant_id: str, key: str,
                     scope: ConfigurationScope = ConfigurationScope.TENANT,
                     organization_id: Optional[str] = None) -> Sequence[TenantConfiguration]:
        with self._lock:
            return tuple(sorted(self._matching(tenant_id, scope, organization_id, key),
                                key=lambda item: item.version.value))


FilesystemTenantConfigurationRepository = JsonTenantConfigurationRepository
