"""Módulo de dominio para Schema Validation (Hito L.5)."""

from .models import (
    ValidationStatus,
    FieldType,
    AdditionalFieldsPolicy,
    FieldDefinition,
    SchemaDefinition,
    ValidationError,
    SchemaValidationResult,
    compute_schema_checksum,
    compute_validation_result_checksum,
)
from .ports import SchemaRegistryPort, SchemaValidationRepositoryPort

__all__ = [
    "ValidationStatus",
    "FieldType",
    "AdditionalFieldsPolicy",
    "FieldDefinition",
    "SchemaDefinition",
    "ValidationError",
    "SchemaValidationResult",
    "compute_schema_checksum",
    "compute_validation_result_checksum",
    "SchemaRegistryPort",
    "SchemaValidationRepositoryPort",
]
