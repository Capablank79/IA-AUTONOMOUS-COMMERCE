"""
Módulo de dominio para Security Checks (Hito K.8).
"""

from .models import (
    SecurityCheckStatus,
    SecurityCategory,
    SecuritySeverity,
    SecurityCheckResult,
    SecurityCheckEvaluation,
    SENSITIVE_KEYS,
    sanitize_security_data,
    deep_freeze,
    validate_safe_identifier,
)
from .ports import (
    SecurityCheckPort,
    SecurityCheckServicePort,
)

__all__ = [
    "SecurityCheckStatus",
    "SecurityCategory",
    "SecuritySeverity",
    "SecurityCheckResult",
    "SecurityCheckEvaluation",
    "SENSITIVE_KEYS",
    "sanitize_security_data",
    "deep_freeze",
    "validate_safe_identifier",
    "SecurityCheckPort",
    "SecurityCheckServicePort",
]
