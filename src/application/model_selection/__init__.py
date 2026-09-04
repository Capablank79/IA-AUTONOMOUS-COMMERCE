"""
Módulo de Aplicación para Selección de Modelos por Tarea (M.5).
"""

from src.application.model_selection.model_selection_service import (
    ModelSelectionByTaskService,
    DefaultTaskSelectionPolicyProvider,
    get_default_task_profiles,
    create_default_task_selection_policy,
)

__all__ = [
    "ModelSelectionByTaskService",
    "DefaultTaskSelectionPolicyProvider",
    "get_default_task_profiles",
    "create_default_task_selection_policy",
]
