"""
Módulo de aplicación para Autorización (Hito N.3).
"""

from .authorization_service import AuthorizationService
from .authorization_guarded_action_executor import AuthorizationGuardedActionExecutor

__all__ = [
    "AuthorizationService",
    "AuthorizationGuardedActionExecutor",
]
