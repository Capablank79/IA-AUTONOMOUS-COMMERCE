"""
Módulo de inicialización del dominio Change Detection (Hito J.4).
"""

from .models import (
    ChangeRecord,
    ChangeSubjectType,
    ChangeType,
    ChangeSignificance,
    ObservedChangeField,
    DerivedChangeDelta,
)
from .ports import (
    ChangeDetectionEnginePort,
    ChangeRecordRepositoryPort,
)
from .engine import (
    ChangeDetectionEngine,
    TemporalOrderViolationError,
    InvalidSubjectComparisonError,
)

__all__ = [
    "ChangeRecord",
    "ChangeSubjectType",
    "ChangeType",
    "ChangeSignificance",
    "ObservedChangeField",
    "DerivedChangeDelta",
    "ChangeDetectionEnginePort",
    "ChangeRecordRepositoryPort",
    "ChangeDetectionEngine",
    "TemporalOrderViolationError",
    "InvalidSubjectComparisonError",
]
