"""
Módulo de dominio para Evaluación (Hito K.4 - Evaluation Harness).
"""

from .models import (
    EvaluationType,
    EvaluationStatus,
    EvaluationMetric,
    EvaluationCase,
    EvaluationResult,
    BatchEvaluationSummary,
)
from .ports import (
    EvaluatorPort,
    EvaluationTargetPort,
    EvaluationRepositoryPort,
)

__all__ = [
    "EvaluationType",
    "EvaluationStatus",
    "EvaluationMetric",
    "EvaluationCase",
    "EvaluationResult",
    "BatchEvaluationSummary",
    "EvaluatorPort",
    "EvaluationTargetPort",
    "EvaluationRepositoryPort",
]
