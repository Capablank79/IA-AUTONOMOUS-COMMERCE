"""Log Retention Domain Package (P.9)."""

from src.domain.log_retention.models import (
    RetentionClass,
    RetentionAction,
    RetentionStatus,
    RetentionPolicy,
    RetentionDecision,
    RetentionResult,
    RetentionStatusSummary,
    LogRetentionError,
    LogRetentionSecurityError,
    LogRetentionPolicyError,
)
from src.domain.log_retention.ports import (
    LogRetentionStorePort,
    LogRetentionPolicyRegistryPort,
    LogRetentionAuditEmitterPort,
)
from src.domain.log_retention.policies import (
    DEFAULT_RETENTION_DAYS,
    DefaultRetentionPolicyRegistry,
)

__all__ = [
    "RetentionClass",
    "RetentionAction",
    "RetentionStatus",
    "RetentionPolicy",
    "RetentionDecision",
    "RetentionResult",
    "RetentionStatusSummary",
    "LogRetentionError",
    "LogRetentionSecurityError",
    "LogRetentionPolicyError",
    "LogRetentionStorePort",
    "LogRetentionPolicyRegistryPort",
    "LogRetentionAuditEmitterPort",
    "DEFAULT_RETENTION_DAYS",
    "DefaultRetentionPolicyRegistry",
]
