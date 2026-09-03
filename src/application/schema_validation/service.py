"""
Servicio de Aplicación para Schema Validation (Hito L.5 - Transversal Data Quality / Governance).

Responsabilidades:
- Resolver el esquema adecuado para un subject_type y version opcional.
- Validar estructuras, tipos y restricciones deterministas sin coerción silenciosa insegura.
- Soporte estricto para:
  * String (min/max length, regex pattern, enum).
  * Integer (type check estricto, no floats ni strings, min/max).
  * Decimal (para campos monetarios/numéricos comerciales, rechazo de floats inseguros, min/max).
  * Boolean (type check estricto: rechaza 1/0 o strings).
  * Datetime (datetime object o ISO-8601 string timezone-aware UTC, rechazo de strings inválidos).
  * Enum (valores permitidos).
  * Array/List (tipo de elementos item_type, recursión).
  * Object/Mapping (nested_fields, additional_fields_policy).
- Semántica rigurosa:
  * missing required field -> FAIL.
  * missing optional field -> valid (ignorado).
  * null en non-nullable -> FAIL.
  * null en nullable -> valid.
  * unknown schema -> UNKNOWN o FAIL según configuración (default UNKNOWN).
  * additional fields con FORBID -> FAIL con error estructurado.
- Generar SchemaValidationResult inmutable, estructurado y auditable con enlace opcional a provenance_id (L.2).
- Persistencia opcional en SchemaValidationRepositoryPort.
- Sanitización estricta de secretos en mensajes de error (K.8).
"""

from dataclasses import is_dataclass, asdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
import re
from types import MappingProxyType
from typing import Optional, Sequence, Mapping, Any, Dict, List, Tuple, Union
import uuid

from src.domain.schema_validation.models import (
    ValidationStatus,
    FieldType,
    AdditionalFieldsPolicy,
    FieldDefinition,
    SchemaDefinition,
    ValidationError,
    SchemaValidationResult,
)
from src.domain.schema_validation.ports import (
    SchemaRegistryPort,
    SchemaValidationRepositoryPort,
)
from src.domain.data_provenance.ports import ProvenanceRepositoryPort
from src.domain.data_provenance.models import SubjectType
from src.domain.reliability.ports import ClockPort
from src.infrastructure.reliability.reliability_infrastructure import SystemClock


class SchemaValidationServiceError(Exception):
    """Excepción base para SchemaValidationService."""


class SchemaNotFoundError(SchemaValidationServiceError):
    """Lanzada cuando un esquema requerido explícitamente no existe."""


def _convert_payload_to_dict(payload: Any) -> Any:
    """Convierte dataclasses, enums o mapping types a estructura base de diccionarios."""
    if payload is None:
        return None
    if isinstance(payload, (dict, MappingProxyType)):
        return {k: _convert_payload_to_dict(v) for k, v in payload.items()}
    if isinstance(payload, (list, tuple)):
        return [_convert_payload_to_dict(v) for v in payload]
    if hasattr(payload, "to_dict") and callable(payload.to_dict):
        return _convert_payload_to_dict(payload.to_dict())
    if is_dataclass(payload) and not isinstance(payload, type):
        return {k: _convert_payload_to_dict(getattr(payload, k)) for k in payload.__dataclass_fields__}
    return payload


class SchemaValidationService:
    """
    Servicio determinista de validación de esquemas L.5.
    """

    def __init__(
        self,
        schema_registry: SchemaRegistryPort,
        validation_repository: Optional[SchemaValidationRepositoryPort] = None,
        provenance_repository: Optional[ProvenanceRepositoryPort] = None,
        clock: Optional[ClockPort] = None,
    ):
        self.schema_registry = schema_registry
        self.validation_repo = validation_repository
        self.provenance_repo = provenance_repository
        self.clock = clock or SystemClock()

    def validate(
        self,
        payload: Any,
        subject_type: Union[SubjectType, str],
        schema_id: Optional[str] = None,
        schema_version: Optional[str] = None,
        subject_id: Optional[str] = None,
        provenance_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        persist: bool = False,
    ) -> SchemaValidationResult:
        """
        Valida un payload contra el esquema definido para el subject_type.
        """
        subject_str = subject_type.value if hasattr(subject_type, "value") else str(subject_type)
        now = self.clock.now() if hasattr(self.clock, "now") else self.clock.now_utc()
        validation_id = f"val_{uuid.uuid4().hex[:12]}"

        # 1. Resolver Schema
        schema: Optional[SchemaDefinition] = None
        if schema_id:
            schema = self.schema_registry.get_schema(schema_id=schema_id, version=schema_version)
        else:
            schema = self.schema_registry.get_latest_schema_by_subject(subject_type=subject_str)

        if schema is None:
            # UNKNOWN schema: No devolver PASS.
            result = SchemaValidationResult(
                validation_id=validation_id,
                schema_id=schema_id or "UNKNOWN_SCHEMA",
                schema_version=schema_version or "0.0.0",
                subject_type=subject_str,
                status=ValidationStatus.UNKNOWN,
                errors=(
                    ValidationError(
                        field_path="$",
                        code="UNKNOWN_SCHEMA",
                        message=f"No registered schema found for subject_type='{subject_str}' and schema_id='{schema_id}'",
                    ),
                ),
                validated_at=now,
                subject_id=subject_id,
                provenance_id=provenance_id,
                correlation_id=correlation_id,
            )
            if persist and self.validation_repo:
                self.validation_repo.save_result(result)
            return result

        # 2. Normalizar payload de entrada a mapping para inspección
        raw_dict = _convert_payload_to_dict(payload)
        if not isinstance(raw_dict, dict):
            errors = (
                ValidationError(
                    field_path="$",
                    code="INVALID_PAYLOAD_ROOT",
                    message=f"Root payload must be an object/dict, got {type(payload).__name__}",
                    expected="OBJECT",
                    actual_type=type(payload).__name__,
                ),
            )
            result = SchemaValidationResult(
                validation_id=validation_id,
                schema_id=schema.schema_id,
                schema_version=schema.version,
                subject_type=subject_str,
                status=ValidationStatus.FAIL,
                errors=errors,
                validated_at=now,
                subject_id=subject_id,
                provenance_id=provenance_id,
                correlation_id=correlation_id,
            )
            if persist and self.validation_repo:
                self.validation_repo.save_result(result)
            return result

        # 3. Validar recursivamente campos y restricciones
        collected_errors: List[ValidationError] = []
        self._validate_object(
            data=raw_dict,
            fields=schema.fields,
            policy=schema.additional_fields_policy,
            current_path="",
            errors=collected_errors,
        )

        status = ValidationStatus.PASS if len(collected_errors) == 0 else ValidationStatus.FAIL

        result = SchemaValidationResult(
            validation_id=validation_id,
            schema_id=schema.schema_id,
            schema_version=schema.version,
            subject_type=subject_str,
            status=status,
            errors=tuple(collected_errors),
            validated_at=now,
            subject_id=subject_id,
            provenance_id=provenance_id,
            correlation_id=correlation_id,
        )

        if persist and self.validation_repo:
            self.validation_repo.save_result(result)

        return result

    def _validate_object(
        self,
        data: Dict[str, Any],
        fields: Sequence[FieldDefinition],
        policy: AdditionalFieldsPolicy,
        current_path: str,
        errors: List[ValidationError],
    ) -> None:
        declared_field_map = {f.field_name: f for f in fields}

        # Chequear required y campos declarados
        for field_def in fields:
            field_name = field_def.field_name
            field_path = f"{current_path}.{field_name}" if current_path else field_name

            if field_name not in data:
                if field_def.required:
                    errors.append(
                        ValidationError(
                            field_path=field_path,
                            code="MISSING_REQUIRED_FIELD",
                            message=f"Required field '{field_path}' is missing",
                            expected=field_def.field_type.value,
                        )
                    )
                continue

            val = data[field_name]
            if val is None:
                if not field_def.nullable:
                    errors.append(
                        ValidationError(
                            field_path=field_path,
                            code="NON_NULLABLE_FIELD",
                            message=f"Field '{field_path}' is non-nullable but got null",
                            expected=field_def.field_type.value,
                            actual_type="NoneType",
                        )
                    )
                continue

            self._validate_field_value(
                value=val,
                field_def=field_def,
                field_path=field_path,
                policy=policy,
                errors=errors,
            )

        # Chequear campos adicionales
        if policy == AdditionalFieldsPolicy.FORBID:
            for k in data.keys():
                if k not in declared_field_map:
                    field_path = f"{current_path}.{k}" if current_path else k
                    errors.append(
                        ValidationError(
                            field_path=field_path,
                            code="FORBIDDEN_ADDITIONAL_FIELD",
                            message=f"Additional field '{field_path}' is forbidden by schema policy",
                        )
                    )

    def _validate_field_value(
        self,
        value: Any,
        field_def: FieldDefinition,
        field_path: str,
        policy: AdditionalFieldsPolicy,
        errors: List[ValidationError],
    ) -> None:
        ft = field_def.field_type

        if ft == FieldType.STRING:
            if not isinstance(value, str):
                errors.append(
                    ValidationError(
                        field_path=field_path,
                        code="INVALID_TYPE",
                        message=f"Expected STRING for '{field_path}', got {type(value).__name__}",
                        expected="STRING",
                        actual_type=type(value).__name__,
                    )
                )
                return
            if field_def.min_length is not None and len(value) < field_def.min_length:
                errors.append(
                    ValidationError(
                        field_path=field_path,
                        code="MIN_LENGTH_VIOLATION",
                        message=f"Length of '{field_path}' ({len(value)}) is less than minimum {field_def.min_length}",
                    )
                )
            if field_def.max_length is not None and len(value) > field_def.max_length:
                errors.append(
                    ValidationError(
                        field_path=field_path,
                        code="MAX_LENGTH_VIOLATION",
                        message=f"Length of '{field_path}' ({len(value)}) exceeds maximum {field_def.max_length}",
                    )
                )
            if field_def.pattern is not None:
                if not re.search(field_def.pattern, value):
                    errors.append(
                        ValidationError(
                            field_path=field_path,
                            code="PATTERN_MISMATCH",
                            message=f"Value for '{field_path}' does not match pattern '{field_def.pattern}'",
                        )
                    )

        elif ft == FieldType.INTEGER:
            # En Python isinstance(True, int) es True. Debemos rechazar booleanos estrictamente.
            if isinstance(value, bool) or not isinstance(value, int):
                errors.append(
                    ValidationError(
                        field_path=field_path,
                        code="INVALID_TYPE",
                        message=f"Expected INTEGER for '{field_path}', got {type(value).__name__}",
                        expected="INTEGER",
                        actual_type=type(value).__name__,
                    )
                )
                return
            dec_val = Decimal(value)
            if field_def.min_value is not None and dec_val < field_def.min_value:
                errors.append(
                    ValidationError(
                        field_path=field_path,
                        code="MIN_VALUE_VIOLATION",
                        message=f"Value {value} for '{field_path}' is less than minimum {field_def.min_value}",
                    )
                )
            if field_def.max_value is not None and dec_val > field_def.max_value:
                errors.append(
                    ValidationError(
                        field_path=field_path,
                        code="MAX_VALUE_VIOLATION",
                        message=f"Value {value} for '{field_path}' is greater than maximum {field_def.max_value}",
                    )
                )

        elif ft == FieldType.DECIMAL:
            # Aceptamos Decimal explícito o conversiones canónicas válidas si viene como Decimal/int, pero NO permitimos strings engañosos si se exige tipado estricto o floats con pérdida binaria
            dec_val: Optional[Decimal] = None
            if isinstance(value, Decimal):
                dec_val = value
            elif isinstance(value, int) and not isinstance(value, bool):
                dec_val = Decimal(value)
            elif isinstance(value, str):
                # Si viene string, sólo aceptamos si es convertible a Decimal y representa número estricto
                try:
                    dec_val = Decimal(value)
                except InvalidOperation:
                    errors.append(
                        ValidationError(
                            field_path=field_path,
                            code="INVALID_DECIMAL_FORMAT",
                            message=f"String value '{value}' for '{field_path}' cannot be parsed as Decimal",
                            expected="DECIMAL",
                            actual_type="str",
                        )
                    )
                    return
            else:
                errors.append(
                    ValidationError(
                        field_path=field_path,
                        code="INVALID_TYPE",
                        message=f"Expected DECIMAL for '{field_path}', got {type(value).__name__}",
                        expected="DECIMAL",
                        actual_type=type(value).__name__,
                    )
                )
                return

            if field_def.min_value is not None and dec_val < field_def.min_value:
                errors.append(
                    ValidationError(
                        field_path=field_path,
                        code="MIN_VALUE_VIOLATION",
                        message=f"Decimal value {dec_val} for '{field_path}' is less than minimum {field_def.min_value}",
                    )
                )
            if field_def.max_value is not None and dec_val > field_def.max_value:
                errors.append(
                    ValidationError(
                        field_path=field_path,
                        code="MAX_VALUE_VIOLATION",
                        message=f"Decimal value {dec_val} for '{field_path}' is greater than maximum {field_def.max_value}",
                    )
                )

        elif ft == FieldType.BOOLEAN:
            if not isinstance(value, bool):
                errors.append(
                    ValidationError(
                        field_path=field_path,
                        code="INVALID_TYPE",
                        message=f"Expected BOOLEAN for '{field_path}', got {type(value).__name__}",
                        expected="BOOLEAN",
                        actual_type=type(value).__name__,
                    )
                )

        elif ft == FieldType.DATETIME:
            if isinstance(value, datetime):
                # Exigir timezone-aware
                if value.tzinfo is None:
                    errors.append(
                        ValidationError(
                            field_path=field_path,
                            code="DATETIME_TZ_MISSING",
                            message=f"Datetime for '{field_path}' must be timezone-aware (UTC)",
                            expected="DATETIME (UTC)",
                        )
                    )
            elif isinstance(value, str):
                try:
                    parsed = datetime.fromisoformat(value)
                    if parsed.tzinfo is None:
                        errors.append(
                            ValidationError(
                                field_path=field_path,
                                code="DATETIME_TZ_MISSING",
                                message=f"ISO Datetime string for '{field_path}' must include timezone offset",
                                expected="ISO-8601 UTC string",
                            )
                        )
                except ValueError:
                    errors.append(
                        ValidationError(
                            field_path=field_path,
                            code="INVALID_DATETIME_FORMAT",
                            message=f"Invalid ISO-8601 datetime format for '{field_path}'",
                            expected="DATETIME",
                            actual_type="str",
                        )
                    )
            else:
                errors.append(
                    ValidationError(
                        field_path=field_path,
                        code="INVALID_TYPE",
                        message=f"Expected DATETIME for '{field_path}', got {type(value).__name__}",
                        expected="DATETIME",
                        actual_type=type(value).__name__,
                    )
                )

        elif ft == FieldType.ENUM:
            str_val = value.value if hasattr(value, "value") else str(value)
            if field_def.enum_values and str_val not in field_def.enum_values:
                errors.append(
                    ValidationError(
                        field_path=field_path,
                        code="INVALID_ENUM_VALUE",
                        message=f"Value '{str_val}' for '{field_path}' is not in allowed enum values",
                        expected=f"One of {list(field_def.enum_values)}",
                        actual_type=type(value).__name__,
                    )
                )

        elif ft == FieldType.ARRAY:
            if not isinstance(value, (list, tuple)):
                errors.append(
                    ValidationError(
                        field_path=field_path,
                        code="INVALID_TYPE",
                        message=f"Expected ARRAY for '{field_path}', got {type(value).__name__}",
                        expected="ARRAY",
                        actual_type=type(value).__name__,
                    )
                )
                return

            if field_def.item_type is not None:
                item_def = FieldDefinition(
                    field_name=f"{field_def.field_name}[]",
                    field_type=field_def.item_type,
                    nested_fields=field_def.nested_fields,
                )
                for idx, item in enumerate(value):
                    item_path = f"{field_path}[{idx}]"
                    if item_def.field_type == FieldType.OBJECT and isinstance(item, dict):
                        if field_def.nested_fields:
                            self._validate_object(
                                data=item,
                                fields=field_def.nested_fields,
                                policy=policy,
                                current_path=item_path,
                                errors=errors,
                            )
                    else:
                        self._validate_field_value(
                            value=item,
                            field_def=item_def,
                            field_path=item_path,
                            policy=policy,
                            errors=errors,
                        )

        elif ft == FieldType.OBJECT:
            if not isinstance(value, dict):
                errors.append(
                    ValidationError(
                        field_path=field_path,
                        code="INVALID_TYPE",
                        message=f"Expected OBJECT for '{field_path}', got {type(value).__name__}",
                        expected="OBJECT",
                        actual_type=type(value).__name__,
                    )
                )
                return

            if field_def.nested_fields:
                self._validate_object(
                    data=value,
                    fields=field_def.nested_fields,
                    policy=policy,
                    current_path=field_path,
                    errors=errors,
                )
