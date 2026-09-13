"""Catálogo de migraciones registradas del proyecto (P.3)."""

from typing import List

from src.infrastructure.persistence.database.migrations.runner import Migration
from src.infrastructure.persistence.database.migrations.versions.v001_initial_saas_schema import (
    Migration001InitialSaasSchema,
)

REGISTERED_MIGRATIONS: List[Migration] = [
    Migration001InitialSaasSchema(),
]

__all__ = ["REGISTERED_MIGRATIONS"]
