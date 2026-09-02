"""
Modelos de dominio para los Chequeos de Seguridad Transversales (Security Checks - Hito K.8).

Define:
- SecurityCheckStatus: Estados canónicos de verificación de seguridad (PASS, FAIL, UNKNOWN, ERROR).
- SecurityCategory: Categorías transversales de riesgo y control (AUTHENTICATION, AUTHORIZATION, POLICY_INTEGRITY, SECRET_PROTECTION, INPUT_SAFETY, PATH_SAFETY, PERSISTENCE_INTEGRITY, EVENT_SAFETY, REPLAY_INTEGRITY, AGENT_SAFETY).
- SecuritySeverity: Severidad del hallazgo (INFO, LOW, MEDIUM, HIGH, CRITICAL).
- SecurityCheckResult: Resultado inmutable de un chequeo de seguridad específico.
- SecurityCheckEvaluation: Agregado inmutable que encapsula una evaluación compuesta de múltiples chequeos de seguridad.

Principios K.8:
- Inmutabilidad estricta (frozen=True, MappingProxyType, tuples).
- K.8 responde: "¿La acción, payload, path, persistencia o evento satisface los controles de seguridad transversal?".
- K.8 orquesta y verifica controles sin duplicar PolicyEngine (Hito E.3), OAuth (Hito E), Audit Trail (K.1), Agent Trace (K.2), ni Reliability (K.7).
- Cero almacenamiento o propagación de secretos, PII o Chain-of-Thought (sanitización recursiva integral).
- Semántica estricta PASS/FAIL/UNKNOWN/ERROR (no fallar abierto ni silenciar fallos de seguridad).
- Determinismo y reproducibilidad.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import re
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Optional, Any, Tuple, Dict, Sequence, List, Union


SENSITIVE_KEYS = {
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "pan",
    "cvv",
    "private_key",
    "credential",
    "access_token",
    "refresh_token",
    "authorization",
    "chain_of_thought",
    "reasoning",
    "reasoning_tokens",
    "internal_scratchpad",
    "card_number",
    "auth_header",
    "bearer",
}


def sanitize_security_data(val: Any) -> Any:
    """Sanitiza recursivamente estructuras de datos para eliminar secretos o credenciales."""
    if isinstance(val, (dict, MappingProxyType)):
        cleaned = {}
        for k, v in val.items():
            k_str = str(k).lower()
            if any(s in k_str for s in SENSITIVE_KEYS) and not isinstance(v, (dict, MappingProxyType, list, tuple)):
                cleaned[str(k)] = "[REDACTED]"
            else:
                cleaned[str(k)] = sanitize_security_data(v)
        return cleaned
    if isinstance(val, (list, tuple)):
        return [sanitize_security_data(v) for v in val]
    return val


def deep_freeze(val: Any) -> Any:
    """Convierte recursivamente diccionarios en MappingProxyType y listas en tuplas."""
    if isinstance(val, (dict, MappingProxyType)):
        return MappingProxyType({k: deep_freeze(v) for k, v in val.items()})
    if isinstance(val, (list, tuple)):
        return tuple(deep_freeze(v) for v in val)
    return val


def validate_safe_identifier(identifier: str, field_name: str = "identifier") -> None:
    """
    Valida rigurosamente que un identificador sea seguro contra path traversal,
    inyecciones de ruta absoluta o separadores de directorio.
    """
    if not isinstance(identifier, str) or not identifier.strip():
        raise ValueError(f"{field_name} must be a non-empty string.")
    
    clean_id = identifier.strip()
    if "/" in clean_id or "\\" in clean_id or ".." in clean_id or ":" in clean_id:
        raise ValueError(f"{field_name} '{identifier}' contains unsafe path traversal sequences or separators.")
    
    if Path(clean_id).name != clean_id:
        raise ValueError(f"{field_name} '{identifier}' is not a safe basename.")


class SecurityCheckStatus(str, Enum):
    """
    Estados canónicos de evaluación de seguridad.
    - PASS: Control verificado y conforme.
    - FAIL: Violación explícita de seguridad detectada (bloqueante).
    - UNKNOWN: Evidencia insuficiente o indeterminada (no permite fallar abierto).
    - ERROR: Error durante la verificación o corrupción detectada.
    """
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"
    ERROR = "ERROR"


class SecurityCategory(str, Enum):
    """
    Categorías canónicas de chequeos de seguridad.
    """
    AUTHENTICATION = "AUTHENTICATION"
    AUTHORIZATION = "AUTHORIZATION"
    POLICY_INTEGRITY = "POLICY_INTEGRITY"
    SECRET_PROTECTION = "SECRET_PROTECTION"
    INPUT_SAFETY = "INPUT_SAFETY"
    PATH_SAFETY = "PATH_SAFETY"
    PERSISTENCE_INTEGRITY = "PERSISTENCE_INTEGRITY"
    EVENT_SAFETY = "EVENT_SAFETY"
    REPLAY_INTEGRITY = "REPLAY_INTEGRITY"
    AGENT_SAFETY = "AGENT_SAFETY"


class SecuritySeverity(str, Enum):
    """
    Severidad de un hallazgo o chequeo.
    """
    INFO = "INFO"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True)
class SecurityCheckResult:
    """
    Resultado inmutable de un chequeo de seguridad específico.
    """
    check_id: str
    category: SecurityCategory
    target: str
    status: SecurityCheckStatus
    severity: SecuritySeverity
    message: str
    code: str
    details: Mapping[str, Any] = field(default_factory=dict)
    evidence_refs: Tuple[str, ...] = field(default_factory=tuple)
    correlation_id: Optional[str] = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        if not self.check_id or not isinstance(self.check_id, str):
            raise ValueError("check_id must be a non-empty string.")
        if not isinstance(self.category, SecurityCategory):
            try:
                object.__setattr__(self, "category", SecurityCategory(self.category))
            except Exception as e:
                raise ValueError(f"Invalid category: {self.category}") from e
        if not isinstance(self.status, SecurityCheckStatus):
            try:
                object.__setattr__(self, "status", SecurityCheckStatus(self.status))
            except Exception as e:
                raise ValueError(f"Invalid status: {self.status}") from e
        if not isinstance(self.severity, SecuritySeverity):
            try:
                object.__setattr__(self, "severity", SecuritySeverity(self.severity))
            except Exception as e:
                raise ValueError(f"Invalid severity: {self.severity}") from e
        if not self.code or not isinstance(self.code, str):
            raise ValueError("code must be a non-empty string.")
        if self.timestamp.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware (UTC).")

        # Inmutabilidad y sanitización profunda
        sanitized_details = sanitize_security_data(self.details)
        object.__setattr__(self, "details", deep_freeze(sanitized_details))
        if not isinstance(self.evidence_refs, tuple):
            object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))

    @property
    def is_blocking(self) -> bool:
        """Determina si este resultado bloquea una acción o flujo."""
        return self.status in (SecurityCheckStatus.FAIL, SecurityCheckStatus.UNKNOWN, SecurityCheckStatus.ERROR)


@dataclass(frozen=True)
class SecurityCheckEvaluation:
    """
    Agregado inmutable que consolida una sesión de verificación de seguridad.
    """
    evaluation_id: str
    status: SecurityCheckStatus
    checks: Tuple[SecurityCheckResult, ...]
    target_resource: str
    correlation_id: str
    allowed: bool
    summary: str
    provenance: str = "SECURITY_CHECK_SERVICE"
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.evaluation_id or not isinstance(self.evaluation_id, str):
            raise ValueError("evaluation_id must be a non-empty string.")
        if not isinstance(self.status, SecurityCheckStatus):
            try:
                object.__setattr__(self, "status", SecurityCheckStatus(self.status))
            except Exception as e:
                raise ValueError(f"Invalid status: {self.status}") from e
        if not isinstance(self.checks, tuple):
            object.__setattr__(self, "checks", tuple(self.checks))
        if self.timestamp.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware (UTC).")

        sanitized_metadata = sanitize_security_data(self.metadata)
        object.__setattr__(self, "metadata", deep_freeze(sanitized_metadata))

    @property
    def has_failures(self) -> bool:
        return any(c.status == SecurityCheckStatus.FAIL for c in self.checks)

    @property
    def has_unknowns(self) -> bool:
        return any(c.status == SecurityCheckStatus.UNKNOWN for c in self.checks)

    @property
    def has_errors(self) -> bool:
        return any(c.status == SecurityCheckStatus.ERROR for c in self.checks)

    @property
    def critical_violations(self) -> Tuple[SecurityCheckResult, ...]:
        return tuple(c for c in self.checks if c.status == SecurityCheckStatus.FAIL and c.severity in (SecuritySeverity.HIGH, SecuritySeverity.CRITICAL))
