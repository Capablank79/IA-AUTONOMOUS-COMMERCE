"""
Módulo de aplicación para Evaluación (Hito K.4 - Evaluation Harness).
"""

from .evaluation_harness_service import (
    EvaluationHarnessService,
    CallableTargetAdapter,
)

__all__ = [
    "EvaluationHarnessService",
    "CallableTargetAdapter",
]
