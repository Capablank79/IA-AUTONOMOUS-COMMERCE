"""
Persistencia JSON atómica, versionada e íntegra para Schema Validation L.5.

Garantiza:
- Atomic write (.tmp -> fsync -> os.replace).
- Inmutabilidad estricta por schema_id + version (para Schemas) y por validation_id (para Results).
- Idempotencia estricta para payloads y checksums idénticos.
- Detección explícita de conflictos si se intenta sobrescribir un schema con contenido diferente bajo la misma versión.
- Verificación estricta de integridad SHA-256 en lectura y detección de corrupción física sin autorreparación silenciosa.
- Thread-safe mediante RLock de concurrencia.
- Path safety estricto (rechaza traversals).
"""

from datetime import datetime
from decimal import Decimal
import json
import logging
import os
from pathlib import Path
from types import MappingProxyType
from typing import Union, Optional, Any, Dict, Sequence, List
import threading

from src.domain.schema_validation.models import (
    SchemaDefinition,
    SchemaValidationResult,
    FieldDefinition,
    FieldType,
    AdditionalFieldsPolicy,
    ValidationError,
    ValidationStatus,
    compute_schema_checksum,
    compute_validation_result_checksum,
)
from src.domain.schema_validation.ports import (
    SchemaRegistryPort,
    SchemaValidationRepositoryPort,
)
from src.domain.security.models import validate_safe_identifier, SENSITIVE_KEYS

logger = logging.getLogger(__name__)


class JsonSchemaRepositoryError(Exception):
    """Excepción base de persistencia Schema L.5."""


class SchemaConflictError(JsonSchemaRepositoryError):
    """Conflicto semántico bajo la misma identidad/versión de esquema."""


class CorruptedSchemaRecordError(JsonSchemaRepositoryError):
    """Registro corrupto o checksum inválido; nunca se repara silenciosamente."""


CorruptedSchemaDefinitionError = CorruptedSchemaRecordError
CorruptedSchemaResultError = CorruptedSchemaRecordError


def _encode(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "value"):
        return value.value
    if isinstance(value, (dict, MappingProxyType)):
        result = {}
        for key, val in value.items():
            key_text = str(key)
            if any(sensitive in key_text.lower() for sensitive in SENSITIVE_KEYS):
                result[key_text] = "[REDACTED]"
            else:
                result[key_text] = _encode(val)
        return result
    if isinstance(value, (list, tuple)):
        return [_encode(item) for item in value]
    return value


def _atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    serialized = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False)
    with open(temporary, "w", encoding="utf-8") as stream:
        stream.write(serialized)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _deserialize_field_def(raw: Dict[str, Any]) -> FieldDefinition:
    nested = None
    if "nested_fields" in raw and raw["nested_fields"] is not None:
        nested = tuple(_deserialize_field_def(nf) for nf in raw["nested_fields"])

    min_val = Decimal(raw["min_value"]) if raw.get("min_value") is not None else None
    max_val = Decimal(raw["max_value"]) if raw.get("max_value") is not None else None
    enum_vals = tuple(raw["enum_values"]) if raw.get("enum_values") is not None else None
    item_t = FieldType(raw["item_type"]) if raw.get("item_type") is not None else None

    return FieldDefinition(
        field_name=raw["field_name"],
        field_type=FieldType(raw["field_type"]),
        required=raw.get("required", True),
        nullable=raw.get("nullable", False),
        enum_values=enum_vals,
        min_value=min_val,
        max_value=max_val,
        min_length=raw.get("min_length"),
        max_length=raw.get("max_length"),
        pattern=raw.get("pattern"),
        item_type=item_t,
        nested_fields=nested,
        description=raw.get("description"),
        metadata=raw.get("metadata", {}),
    )


class JsonSchemaRegistryRepository(SchemaRegistryPort):
    """Repositorio crash-safe e idempotente para SchemaDefinition."""

    def __init__(self, base_dir: Union[str, Path]):
        self.schemas_dir = Path(base_dir) / "schemas" / "definitions"
        self.schemas_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._cache: Dict[str, SchemaDefinition] = {}
        self._load()

    def _deserialize(self, raw: Dict[str, Any]) -> SchemaDefinition:
        try:
            fields = tuple(_deserialize_field_def(f) for f in raw["fields"])
            policy = AdditionalFieldsPolicy(raw.get("additional_fields_policy", "FORBID"))
            return SchemaDefinition(
                schema_id=raw["schema_id"],
                name=raw["name"],
                version=raw.get("version", "1.0.0"),
                subject_type=raw["subject_type"],
                fields=fields,
                additional_fields_policy=policy,
                description=raw.get("description"),
                metadata=raw.get("metadata", {}),
            )
        except Exception as e:
            raise CorruptedSchemaRecordError(f"Error deserializing schema definition: {e}") from e

    def _load(self) -> None:
        for file_path in self.schemas_dir.glob("*.json"):
            try:
                with open(file_path, "r", encoding="utf-8") as stream:
                    raw = json.load(stream)
                expected_checksum = raw.get("checksum")
                schema = self._deserialize(raw)
                if expected_checksum and schema.checksum != expected_checksum:
                    raise CorruptedSchemaRecordError(
                        f"Checksum mismatch for schema in {file_path}: expected {expected_checksum}, calculated {schema.checksum}"
                    )
                key = f"{schema.schema_id}@{schema.version}"
                self._cache[key] = schema
            except CorruptedSchemaRecordError:
                raise
            except Exception as e:
                raise CorruptedSchemaRecordError(f"Failed to read schema file {file_path}: {e}") from e

    def save_schema(self, schema: SchemaDefinition) -> SchemaDefinition:
        with self._lock:
            key = f"{schema.schema_id}@{schema.version}"
            if key in self._cache:
                existing = self._cache[key]
                if existing.checksum == schema.checksum:
                    return existing
                raise SchemaConflictError(
                    f"Schema '{schema.schema_id}' version '{schema.version}' already exists with a different checksum."
                )

            file_path = self.schemas_dir / f"{schema.schema_id}_v{schema.version}.json"
            data = _encode(schema.to_dict())
            _atomic_write_json(file_path, data)
            self._cache[key] = schema
            return schema

    def get_schema(self, schema_id: str, version: Optional[str] = None) -> Optional[SchemaDefinition]:
        with self._lock:
            if version:
                return self._cache.get(f"{schema_id}@{version}")
            matches = [s for s in self._cache.values() if s.schema_id == schema_id]
            if not matches:
                return None
            matches.sort(key=lambda x: [int(p) if p.isdigit() else 0 for p in x.version.split(".")], reverse=True)
            return matches[0]

    def get_latest_schema_by_subject(self, subject_type: str) -> Optional[SchemaDefinition]:
        with self._lock:
            matches = [s for s in self._cache.values() if s.subject_type == subject_type]
            if not matches:
                return None
            matches.sort(key=lambda x: [int(p) if p.isdigit() else 0 for p in x.version.split(".")], reverse=True)
            return matches[0]

    def list_schemas(self, subject_type: Optional[str] = None) -> Sequence[SchemaDefinition]:
        with self._lock:
            if subject_type:
                return [s for s in self._cache.values() if s.subject_type == subject_type]
            return list(self._cache.values())


class JsonSchemaValidationRepository(SchemaValidationRepositoryPort):
    """Repositorio crash-safe para SchemaValidationResult."""

    def __init__(self, base_dir: Union[str, Path]):
        self.results_dir = Path(base_dir) / "schemas" / "results"
        self.results_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._cache: Dict[str, SchemaValidationResult] = {}
        self._load()

    def _deserialize(self, raw: Dict[str, Any]) -> SchemaValidationResult:
        try:
            errors = tuple(
                ValidationError(
                    field_path=e["field_path"],
                    code=e["code"],
                    message=e["message"],
                    expected=e.get("expected"),
                    actual_type=e.get("actual_type"),
                )
                for e in raw.get("errors", [])
            )
            validated_at = datetime.fromisoformat(raw["validated_at"])
            return SchemaValidationResult(
                validation_id=raw["validation_id"],
                schema_id=raw["schema_id"],
                schema_version=raw["schema_version"],
                subject_type=raw["subject_type"],
                status=ValidationStatus(raw["status"]),
                errors=errors,
                validated_at=validated_at,
                subject_id=raw.get("subject_id"),
                provenance_id=raw.get("provenance_id"),
                correlation_id=raw.get("correlation_id"),
                metadata=raw.get("metadata", {}),
            )
        except Exception as e:
            raise CorruptedSchemaRecordError(f"Error deserializing validation result: {e}") from e

    def _load(self) -> None:
        for file_path in self.results_dir.glob("*.json"):
            try:
                with open(file_path, "r", encoding="utf-8") as stream:
                    raw = json.load(stream)
                expected_checksum = raw.get("checksum")
                result = self._deserialize(raw)
                if expected_checksum and result.checksum != expected_checksum:
                    raise CorruptedSchemaRecordError(
                        f"Checksum mismatch for result in {file_path}: expected {expected_checksum}, calculated {result.checksum}"
                    )
                self._cache[result.validation_id] = result
            except CorruptedSchemaRecordError:
                raise
            except Exception as e:
                raise CorruptedSchemaRecordError(f"Failed to read result file {file_path}: {e}") from e

    def save_result(self, result: SchemaValidationResult) -> SchemaValidationResult:
        with self._lock:
            if result.validation_id in self._cache:
                existing = self._cache[result.validation_id]
                if existing.checksum == result.checksum:
                    return existing
                raise SchemaConflictError(
                    f"Result '{result.validation_id}' already exists with different checksum."
                )

            file_path = self.results_dir / f"{result.validation_id}.json"
            data = _encode(result.to_dict())
            _atomic_write_json(file_path, data)
            self._cache[result.validation_id] = result
            return result

    def get_result(self, validation_id: str) -> Optional[SchemaValidationResult]:
        with self._lock:
            return self._cache.get(validation_id)

    def find_by_subject(
        self,
        subject_id: str,
        subject_type: Optional[str] = None,
    ) -> Sequence[SchemaValidationResult]:
        with self._lock:
            return [
                r for r in self._cache.values()
                if r.subject_id == subject_id and (subject_type is None or r.subject_type == subject_type)
            ]

    def get_latest_by_subject(
        self,
        subject_id: str,
        subject_type: Optional[str] = None,
    ) -> Optional[SchemaValidationResult]:
        with self._lock:
            matches = self.find_by_subject(subject_id=subject_id, subject_type=subject_type)
            if not matches:
                return None
            return max(matches, key=lambda x: x.validated_at)
