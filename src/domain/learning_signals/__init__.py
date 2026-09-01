"""
Dominio puro de señales de aprendizaje (Task I.7).
"""

from src.domain.learning_signals.models import (
    LearningSignalRecord,
    LearningSignalType,
    LearningSignalSubjectType,
    LearningSignalSourceType,
    SignalEvidenceClassification,
    SignalStatus,
)
from src.domain.learning_signals.ports import LearningSignalRepositoryPort
from src.domain.learning_signals.services import LearningSignalGenerator

__all__ = [
    "LearningSignalRecord",
    "LearningSignalType",
    "LearningSignalSubjectType",
    "LearningSignalSourceType",
    "SignalEvidenceClassification",
    "SignalStatus",
    "LearningSignalRepositoryPort",
    "LearningSignalGenerator",
]
