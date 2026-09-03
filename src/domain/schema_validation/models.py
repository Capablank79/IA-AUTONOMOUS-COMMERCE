"""
Modelos de dominio para Schema Validation (Hito L.5 - Transversal Data Quality / Governance).

Define:
- ValidationStatus: Estados canónicos de validación de esquemas (PASS, FAIL, UNKNOWN, ERROR).
- FieldType: Tipos de datos soportados deterministamente (STRING, INTEGER, DECIMAL, BOOLEAN, DATETIME, ENUM, ARRAY, OBJECT).
- AdditionalFieldsPolicy: Políticas sobre campos adicionales no declarados (ALLOW, IGNORE, FORBID).
- FieldDefinition: Definición inmutable de restricciones y tipos para un campo.
- SchemaDefinition: Entidad inmutable de dominio que define la estructura y restricciones de un tipo/sujeto.
- ValidationError: Error estructurado de validación con ruta de campo (field_path) y sanitización de secretos.
- SchemaValidationResult: Agregado inmutable que encapsula el resultado determinista y reproducible de la validación.

Principios L.5:
- Inmutabilidad estricta (frozen=True, MappingProxyType, tuples).
- L.5 responde exclusivamente: "¿Este dato cumple exactamente la estructura, tipos y restricciones esperadas para su tipo/versión?".
- L.5 NO evalúa procedencia de fuentes (L.1/L.2), no calcula frescura temporal (L.3), no calcula confianza (L.4), no resuelve entidades (L.6), no detecta duplicados (L.7) ni resuelve conflictos (L.8).
- Tipado estricto y determinista: prohibida la coerción silenciosa insegura (e.g. "10" no es Decimal 10, True no es 1 entero para campos numéricos).
- Uso estricto de Decimal para valores monetarios y restricciones numéricas comerciales.
- Semántica rigurosa: missing required -> FAIL, optional missing -> PASS, nullable -> evaluado explícitamente.
- Errores estructurados con field_path granular (e.g. "seller.address.country").
- Sanitización estricta de credenciales, secretos y datos confidenciales en mensajes de error y metadata (K.8).
- Integridad verificable por checksum SHA-256 canónico en SchemaDefinition y SchemaValidationResult.
- Versionado semántico SemVer en esquemas y trazabilidad de correlation_id y provenance_id (L.2).
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
import hashlib
import json
import re
from types import MappingProxyType
from typing import Mapping, Optional, Any, Tuple, Dict, Sequence, List, Union

from src.domain.security.models import (
    validate_safe_identifier,
    sanitize_security_data,
    deep_freeze,
    SENSITIVE_KEYS,
)
from src.domain.freshness.models import validate_semver


class ValidationStatus(str, Enum):
    """
    Estados canónicos de validación estructural y de restricciones de esquema.
    UNKNOWN permanece estrictamente separado de PASS, FAIL y ERROR.
    """
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"
    ERROR = "ERROR"


class FieldType(str, Enum):
    """
    Tipos de datos soportados deterministamente en la validación de esquemas.
    """
    STRING = "STRING"
    INTEGER = "INTEGER"
    DECIMAL = "DECIMAL"
    BOOLEAN = "BOOLEAN"
    DATETIME = "DATETIME"
    ENUM = "ENUM"
    ARRAY = "ARRAY"
    OBJECT = "OBJECT"


class AdditionalFieldsPolicy(str, Enum):
    """
    Política ante la presencia de campos no declarados explícitamente en el esquema.
    """
    ALLOW = "ALLOW"
    IGNORE = "IGNORE"
    FORBID = "FORBID"


@dataclass(frozen=True)
class FieldDefinition:
    """
    Definición inmutable de restricciones y tipos para un campo en un esquema.
    """
    field_name: str
    field_type: FieldType
    required: bool = True
    nullable: bool = False
    enum_values: Optional[Tuple[str, ...]] = None
    min_value: Optional[Decimal] = None
    max_value: Optional[Decimal] = None
    min_length: Optional[int] = None
    max_length: Optional[int] = None
    pattern: Optional[str] = None
    item_type: Optional[FieldType] = None
    nested_fields: Optional[Tuple["FieldDefinition", ...]] = None
    description: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.field_name or not isinstance(self.field_name, str):
            raise ValueError("field_name must be a non-empty string")
        if not isinstance(self.field_type, FieldType):
            raise ValueError(f"field_type must be an instance of FieldType, got {self.field_type}")

        if self.enum_values is not None:
            if not isinstance(self.enum_values, (tuple, list)):
                raise ValueError("enum_values must be a sequence of strings")
            object.__setattr__(self, "enum_values", tuple(str(v) for v in self.enum_values))

        if self.min_value is not None and not isinstance(self.min_value, Decimal):
            raise ValueError(f"min_value must be a Decimal, got {type(self.min_value)}")
        if self.max_value is not None and not isinstance(self.max_value, Decimal):
            raise ValueError(f"max_value must be a Decimal, got {type(self.max_value)}")

        if self.min_length is not None and (not isinstance(self.min_length, int) or self.min_length < 0):
            raise ValueError("min_length must be a non-negative integer")
        if self.max_length is not None and (not isinstance(self.max_length, int) or self.max_length < 0):
            raise ValueError("max_length must be a non-negative integer")

        if self.pattern is not None:
            if not isinstance(self.pattern, str):
                raise ValueError("pattern must be a string regex")
            try:
                re.compile(self.pattern)
            except re.error as e:
                raise ValueError(f"Invalid regex pattern '{self.pattern}': {e}")

        if self.item_type is not None and not isinstance(self.item_type, FieldType):
            raise ValueError(f"item_type must be an instance of FieldType, got {self.item_type}")

        if self.nested_fields is not None:
            if not isinstance(self.nested_fields, (tuple, list)):
                raise ValueError("nested_fields must be a sequence of FieldDefinition")
            object.__setattr__(self, "nested_fields", tuple(self.nested_fields))

        sanitized_meta = sanitize_security_data(dict(self.metadata))
        object.__setattr__(self, "metadata", deep_freeze(sanitized_meta))

    def to_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "field_name": self.field_name,
            "field_type": self.field_type.value,
            "required": self.required,
            "nullable": self.nullable,
        }
        if self.enum_values is not None:
            result["enum_values"] = list(self.enum_values)
        if self.min_value is not None:
            result["min_value"] = str(self.min_value)
        if self.max_value is not None:
            result["max_value"] = str(self.max_value)
        if self.min_length is not None:
            result["min_length"] = self.min_length
        if self.max_length is not None:
            result["max_length"] = self.max_length
        if self.pattern is not None:
            result["pattern"] = self.pattern
        if self.item_type is not None:
            result["item_type"] = self.item_type.value
        if self.nested_fields is not None:
            result["nested_fields"] = [f.to_dict() for f in self.nested_fields]
        if self.description is not None:
            result["description"] = self.description
        if self.metadata:
            result["metadata"] = dict(self.metadata)
        return result


def compute_schema_checksum(
    schema_id: str,
    name: str,
    version: str,
    subject_type: str,
    fields: Sequence[FieldDefinition],
    additional_fields_policy: AdditionalFieldsPolicy,
    metadata: Mapping[str, Any],
) -> str:
    """Calcula el checksum SHA-256 canónico para una SchemaDefinition."""
    sanitized_meta = sanitize_security_data(dict(metadata))
    payload = {
        "schema_id": schema_id,
        "name": name,
        "version": version,
        "subject_type": subject_type,
        "additional_fields_policy": additional_fields_policy.value,
        "fields": [f.to_dict() for f in fields],
        "metadata": sanitized_meta,
    }
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SchemaDefinition:
    """
    Entidad de dominio inmutable que define el contrato de esquema para un subject_type.
    """
    schema_id: str
    name: str
    version: str
    subject_type: str
    fields: Tuple[FieldDefinition, ...]
    additional_fields_policy: AdditionalFieldsPolicy = AdditionalFieldsPolicy.FORBID
    description: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    checksum: str = field(init=False)

    def __post_init__(self):
        validate_safe_identifier(self.schema_id, "schema_id")
        if not self.name or not isinstance(self.name, str):
            raise ValueError("name must be a non-empty string")
        validate_semver(self.version, "version")
        if not self.subject_type or not isinstance(self.subject_type, str):
            raise ValueError("subject_type must be a non-empty string")

        if not isinstance(self.fields, (tuple, list)):
            raise ValueError("fields must be a sequence of FieldDefinition")
        object.__setattr__(self, "fields", tuple(self.fields))

        if not isinstance(self.additional_fields_policy, AdditionalFieldsPolicy):
            raise ValueError(
                f"additional_fields_policy must be an AdditionalFieldsPolicy, got {self.additional_fields_policy}"
            )

        sanitized_meta = sanitize_security_data(dict(self.metadata))
        object.__setattr__(self, "metadata", deep_freeze(sanitized_meta))

        calculated_checksum = compute_schema_checksum(
            schema_id=self.schema_id,
            name=self.name,
            version=self.version,
            subject_type=self.subject_type,
            fields=self.fields,
            additional_fields_policy=self.additional_fields_policy,
            metadata=self.metadata,
        )
        object.__setattr__(self, "checksum", calculated_checksum)

    def to_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "schema_id": self.schema_id,
            "name": self.name,
            "version": self.version,
            "subject_type": self.subject_type,
            "additional_fields_policy": self.additional_fields_policy.value,
            "fields": [f.to_dict() for f in self.fields],
            "checksum": self.checksum,
        }
        if self.description is not None:
            result["description"] = self.description
        if self.metadata:
            result["metadata"] = dict(self.metadata)
        return result


def _sanitize_message(message: str) -> str:
    """Sanitiza mensajes para evitar fugas de contraseñas, tokens o secretos."""
    clean = message
    for key in SENSITIVE_KEYS:
        # Reemplazar patrones como "token=xyz" o "secret: xyz"
        pattern = re.compile(rf"({re.escape(key)}\s*[:=]\s*)([^\s,;\)]+)", re.IGNORECASE)
        clean = pattern.sub(r"\1[REDACTED]", clean)
    return clean


@dataclass(frozen=True)
class ValidationError:
    """
    Error de validación estructurado con ruta de campo explícita y mensaje sanitizado.
    """
    field_path: str
    code: str
    message: str
    expected: Optional[str] = None
    actual_type: Optional[str] = None

    def __post_init__(self):
        if not self.field_path or not isinstance(self.field_path, str):
            raise ValueError("field_path must be a non-empty string")
        if not self.code or not isinstance(self.code, str):
            raise ValueError("code must be a non-empty string")
        if not self.message or not isinstance(self.message, str):
            raise ValueError("message must be a non-empty string")

        sanitized_msg = _sanitize_message(self.message)
        object.__setattr__(self, "message", sanitized_msg)

    def to_dict(self) -> Dict[str, Any]:
        res = {
            "field_path": self.field_path,
            "code": self.code,
            "message": self.message,
        }
        if self.expected is not None:
            res["expected"] = self.expected
        if self.actual_type is not None:
            res["actual_type"] = self.actual_type
        return res


def compute_validation_result_checksum(
    validation_id: str,
    schema_id: str,
    schema_version: str,
    subject_type: str,
    subject_id: Optional[str],
    provenance_id: Optional[str],
    status: ValidationStatus,
    errors: Sequence[ValidationError],
    validated_at: datetime,
    correlation_id: Optional[str],
    metadata: Mapping[str, Any],
) -> str:
    """Calcula el checksum SHA-256 canónico para un SchemaValidationResult."""
    sanitized_meta = sanitize_security_data(dict(metadata))
    payload = {
        "validation_id": validation_id,
        "schema_id": schema_id,
        "schema_version": schema_version,
        "subject_type": subject_type,
        "subject_id": subject_id or "",
        "provenance_id": provenance_id or "",
        "status": status.value,
        "errors": [e.to_dict() for e in errors],
        "validated_at": validated_at.astimezone(timezone.utc).isoformat(),
        "correlation_id": correlation_id or "",
        "metadata": sanitized_meta,
    }
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SchemaValidationResult:
    """
    Agregado inmutable que encapsula el resultado determinista de una validación de esquema.
    """
    validation_id: str
    schema_id: str
    schema_version: str
    subject_type: str
    status: ValidationStatus
    errors: Tuple[ValidationError, ...]
    validated_at: datetime
    subject_id: Optional[str] = None
    provenance_id: Optional[str] = None
    correlation_id: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    checksum: str = field(init=False)

    def __post_init__(self):
        validate_safe_identifier(self.validation_id, "validation_id")
        if not self.schema_id or not isinstance(self.schema_id, str):
            raise ValueError("schema_id must be a non-empty string")
        if not self.schema_version or not isinstance(self.schema_version, str):
            raise ValueError("schema_version must be a non-empty string")
        if not self.subject_type or not isinstance(self.subject_type, str):
            raise ValueError("subject_type must be a non-empty string")

        if not isinstance(self.status, ValidationStatus):
            raise ValueError(f"status must be a ValidationStatus, got {self.status}")

        if not isinstance(self.errors, (tuple, list)):
            raise ValueError("errors must be a sequence of ValidationError")
        object.__setattr__(self, "errors", tuple(self.errors))

        if not isinstance(self.validated_at, datetime):
            raise ValueError("validated_at must be a datetime")
        if self.validated_at.tzinfo is None:
            raise ValueError("validated_at must be timezone-aware (UTC)")

        sanitized_meta = sanitize_security_data(dict(self.metadata))
        object.__setattr__(self, "metadata", deep_freeze(sanitized_meta))

        calculated_checksum = compute_validation_result_checksum(
            validation_id=self.validation_id,
            schema_id=self.schema_id,
            schema_version=self.schema_version,
            subject_type=self.subject_type,
            subject_id=self.subject_id,
            provenance_id=self.provenance_id,
            status=self.status,
            errors=self.errors,
            validated_at=self.validated_at,
            correlation_id=self.correlation_id,
            metadata=self.metadata,
        )
        object.__setattr__(self, "checksum", calculated_checksum)

    def to_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "validation_id": self.validation_id,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "subject_type": self.subject_type,
            "status": self.status.value,
            "errors": [e.to_dict() for e in self.errors],
            "validated_at": self.validated_at.isoformat(),
            "checksum": self.checksum,
        }
        if self.subject_id is not None:
            result["subject_id"] = self.subject_id
        if self.provenance_id is not None:
            result["provenance_id"] = self.provenance_id
        if self.correlation_id is not None:
            result["correlation_id"] = self.correlation_id
        if self.metadata:
            result["metadata"] = dict(self.metadata)
        return result
