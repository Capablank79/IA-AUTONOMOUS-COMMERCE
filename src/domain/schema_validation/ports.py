"""Puertos de dominio para Schema Validation (Hito L.5)."""

from typing import Optional, Protocol, Sequence
from .models import SchemaDefinition, SchemaValidationResult


class SchemaRegistryPort(Protocol):
    """Contrato de persistencia y consulta de definiciones de esquemas versionados."""

    def save_schema(self, schema: SchemaDefinition) -> SchemaDefinition:
        ...

    def get_schema(self, schema_id: str, version: Optional[str] = None) -> Optional[SchemaDefinition]:
        ...

    def get_latest_schema_by_subject(self, subject_type: str) -> Optional[SchemaDefinition]:
        ...

    def list_schemas(self, subject_type: Optional[str] = None) -> Sequence[SchemaDefinition]:
        ...


class SchemaValidationRepositoryPort(Protocol):
    """Contrato de persistencia y consulta de resultados de validación de esquemas."""

    def save_result(self, result: SchemaValidationResult) -> SchemaValidationResult:
        ...

    def get_result(self, validation_id: str) -> Optional[SchemaValidationResult]:
        ...

    def find_by_subject(
        self,
        subject_id: str,
        subject_type: Optional[str] = None,
    ) -> Sequence[SchemaValidationResult]:
        ...

    def get_latest_by_subject(
        self,
        subject_id: str,
        subject_type: Optional[str] = None,
    ) -> Optional[SchemaValidationResult]:
        ...
