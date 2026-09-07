"""
Domain Models for N.8 — Tool Allowlist / Denylist (Transversal N — Security, Governance & Safety).

Responde deterministamente a la pregunta:
"¿Está permitido invocar esta herramienta/operación concreta dentro del contexto actual?"

Principios:
- Default DENY: Si no hay regla de permiso explícita, se bloquea por defecto (DENY/UNKNOWN).
- Precedencia determinista: Explicit DENY > Explicit Scoped ALLOW > Default DENY.
- Normalización canónica anti-bypass para tool_id, provider y operation_id.
- Granularidad de efectos colaterales (READ_ONLY vs WRITE / EXTERNAL_SIDE_EFFECT / IRREVERSIBLE).
- Distinción estricta de responsabilidades: N.3 (Actor), N.6 (Aprobación), N.7 (Límites financieros), N.8 (Política de herramientas).
- Inmutabilidad estricta (frozen=True, MappingProxyType, tuplas).
- Verificación de integridad con Checksum SHA-256.
- Cero secretos N.5, cero CoT / razonamiento interno (K.2).
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import re
from types import MappingProxyType
from typing import Mapping, Optional, Any, Tuple, Sequence, Dict, Union

from src.domain.security.models import (
    sanitize_security_data,
    deep_freeze,
    validate_safe_identifier,
)
from src.domain.tool.models import ToolSideEffectLevel


class ToolAccessStatus(str, Enum):
    """
    Estados canónicos de decisión para acceso/invocación de herramientas (N.8).
    """
    ALLOW = "ALLOW"
    DENY = "DENY"
    UNKNOWN = "UNKNOWN"
    ERROR = "ERROR"


class ToolAccessReasonCode(str, Enum):
    """
    Códigos de razón estructurados y deterministas para N.8.
    """
    ALLOWED_BY_POLICY = "ALLOWED_BY_POLICY"
    EXPLICIT_DENY_RULE = "EXPLICIT_DENY_RULE"
    DEFAULT_DENY_NO_RULE = "DEFAULT_DENY_NO_RULE"
    TOOL_NOT_FOUND = "TOOL_NOT_FOUND"
    TOOL_DISABLED_OR_DEPRECATED = "TOOL_DISABLED_OR_DEPRECATED"
    SIDE_EFFECT_LEVEL_PROHIBITED = "SIDE_EFFECT_LEVEL_PROHIBITED"
    ROLE_NOT_AUTHORIZED_FOR_TOOL = "ROLE_NOT_AUTHORIZED_FOR_TOOL"
    MISSION_OR_SCOPE_MISMATCH = "MISSION_OR_SCOPE_MISMATCH"
    ACCOUNT_MISMATCH = "ACCOUNT_MISMATCH"
    PROVIDER_MISMATCH = "PROVIDER_MISMATCH"
    OPERATION_NOT_ALLOWED = "OPERATION_NOT_ALLOWED"
    POLICY_NOT_FOUND = "POLICY_NOT_FOUND"
    CORRUPTED_POLICY_OR_CHECKSUM_INVALID = "CORRUPTED_POLICY_OR_CHECKSUM_INVALID"
    AUTHENTICATION_REQUIRED = "AUTHENTICATION_REQUIRED"
    INVALID_TOOL_REFERENCE = "INVALID_TOOL_REFERENCE"
    EVALUATION_ERROR = "EVALUATION_ERROR"


class ToolRuleAction(str, Enum):
    """
    Acción que impone una regla dentro de una política de herramientas.
    """
    ALLOW = "ALLOW"
    DENY = "DENY"


def normalize_identifier(raw: Optional[str]) -> str:
    """
    Normaliza de forma canónica y determinista identificadores de herramientas,
    proveedores y operaciones para prevenir bypass por casing o espacios.
    """
    if raw is None:
        return ""
    # Quitar espacios en blanco laterales y convertir a minúsculas canónicas
    cleaned = raw.strip().lower()
    # Reemplazar secuencias de múltiples espacios o guiones continuos
    cleaned = re.sub(r"\s+", "_", cleaned)
    return cleaned


@dataclass(frozen=True)
class ToolReference:
    """
    Referencia canónica e inmutable a una herramienta o capacidad externa.
    """
    tool_id: str
    provider: Optional[str] = None
    operation_id: Optional[str] = None
    side_effect_level: ToolSideEffectLevel = ToolSideEffectLevel.READ_ONLY
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.tool_id or not isinstance(self.tool_id, str):
            raise ValueError("tool_id must be a non-empty string")

        # Normalización determinista
        norm_tool_id = normalize_identifier(self.tool_id)
        norm_provider = normalize_identifier(self.provider) if self.provider else None
        norm_operation = normalize_identifier(self.operation_id) if self.operation_id else None

        object.__setattr__(self, "tool_id", norm_tool_id)
        if norm_provider is not None:
            object.__setattr__(self, "provider", norm_provider)
        if norm_operation is not None:
            object.__setattr__(self, "operation_id", norm_operation)

        if not isinstance(self.side_effect_level, ToolSideEffectLevel):
            if isinstance(self.side_effect_level, str):
                try:
                    object.__setattr__(self, "side_effect_level", ToolSideEffectLevel(self.side_effect_level))
                except ValueError:
                    raise ValueError(f"Invalid ToolSideEffectLevel: {self.side_effect_level}")
            else:
                raise ValueError("side_effect_level must be a ToolSideEffectLevel instance")

        sanitized_meta = sanitize_security_data(dict(self.metadata))
        object.__setattr__(self, "metadata", deep_freeze(sanitized_meta))

    @property
    def canonical_id(self) -> str:
        """Identificador canónico global: provider:tool_id[:operation_id]."""
        prov = self.provider if self.provider else "global"
        base = f"{prov}:{self.tool_id}"
        if self.operation_id:
            return f"{base}:{self.operation_id}"
        return base


@dataclass(frozen=True)
class ToolPolicyRule:
    """
    Regla granular de control de acceso para herramientas.
    """
    rule_id: str
    action: ToolRuleAction
    tool_id_pattern: str = "*"
    provider_pattern: str = "*"
    operation_pattern: str = "*"
    allowed_side_effect_levels: Tuple[ToolSideEffectLevel, ...] = ()
    prohibited_side_effect_levels: Tuple[ToolSideEffectLevel, ...] = ()
    allowed_roles: Tuple[str, ...] = ()
    denied_roles: Tuple[str, ...] = ()
    allowed_scopes: Tuple[str, ...] = ()
    target_account_id: Optional[str] = None
    target_mission_id: Optional[str] = None
    description: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.rule_id or not isinstance(self.rule_id, str):
            raise ValueError("rule_id must be a non-empty string")
        validate_safe_identifier(self.rule_id, "rule_id")

        if not isinstance(self.action, ToolRuleAction):
            if isinstance(self.action, str):
                object.__setattr__(self, "action", ToolRuleAction(self.action.upper()))
            else:
                raise ValueError("action must be a ToolRuleAction instance")

        object.__setattr__(self, "tool_id_pattern", normalize_identifier(self.tool_id_pattern) or "*")
        object.__setattr__(self, "provider_pattern", normalize_identifier(self.provider_pattern) or "*")
        object.__setattr__(self, "operation_pattern", normalize_identifier(self.operation_pattern) or "*")

        # Asegurar tuplas e inmutabilidad de niveles de efectos
        norm_allowed_effects = []
        for level in self.allowed_side_effect_levels:
            norm_allowed_effects.append(level if isinstance(level, ToolSideEffectLevel) else ToolSideEffectLevel(level))
        object.__setattr__(self, "allowed_side_effect_levels", tuple(norm_allowed_effects))

        norm_prohibited_effects = []
        for level in self.prohibited_side_effect_levels:
            norm_prohibited_effects.append(level if isinstance(level, ToolSideEffectLevel) else ToolSideEffectLevel(level))
        object.__setattr__(self, "prohibited_side_effect_levels", tuple(norm_prohibited_effects))

        # Normalizar roles y scopes
        norm_roles = tuple(r.strip().upper() for r in self.allowed_roles if isinstance(r, str) and r.strip())
        norm_denied_roles = tuple(r.strip().upper() for r in self.denied_roles if isinstance(r, str) and r.strip())
        norm_scopes = tuple(s.strip().lower() for s in self.allowed_scopes if isinstance(s, str) and s.strip())
        object.__setattr__(self, "allowed_roles", norm_roles)
        object.__setattr__(self, "denied_roles", norm_denied_roles)
        object.__setattr__(self, "allowed_scopes", norm_scopes)

        if self.target_account_id is not None:
            object.__setattr__(self, "target_account_id", self.target_account_id.strip())
        if self.target_mission_id is not None:
            object.__setattr__(self, "target_mission_id", self.target_mission_id.strip())

        sanitized_meta = sanitize_security_data(dict(self.metadata))
        object.__setattr__(self, "metadata", deep_freeze(sanitized_meta))


def compute_tool_policy_checksum(
    policy_name: str,
    version: str,
    rules: Sequence[ToolPolicyRule],
    default_action: ToolRuleAction,
    metadata: Optional[Mapping[str, Any]] = None,
) -> str:
    """
    Calcula un checksum SHA-256 canónico para una ToolPolicy.
    """
    rules_payload = []
    for r in rules:
        allowed_eff = ",".join(sorted(l.value for l in r.allowed_side_effect_levels))
        prohib_eff = ",".join(sorted(l.value for l in r.prohibited_side_effect_levels))
        allowed_r = ",".join(sorted(r.allowed_roles))
        denied_r = ",".join(sorted(r.denied_roles))
        allowed_s = ",".join(sorted(r.allowed_scopes))
        rules_payload.append(
            f"{r.rule_id}:{r.action.value}:{r.tool_id_pattern}:{r.provider_pattern}:"
            f"{r.operation_pattern}:{allowed_eff}:{prohib_eff}:{allowed_r}:{denied_r}:{allowed_s}:"
            f"{r.target_account_id or ''}:{r.target_mission_id or ''}"
        )
    sorted_rules = "|".join(sorted(rules_payload))

    meta_dict = dict(metadata) if metadata else {}
    sanitized_meta = sanitize_security_data(meta_dict)
    sorted_meta_json = json.dumps(sanitized_meta, sort_keys=True, default=str)

    payload = f"{policy_name.strip()}|{version.strip()}|{default_action.value}|{sorted_rules}|{sorted_meta_json}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ToolPolicy:
    """
    Política inmutable de acceso y gobierno de herramientas (N.8).
    """
    policy_name: str
    version: str
    rules: Tuple[ToolPolicyRule, ...] = field(default_factory=tuple)
    default_action: ToolRuleAction = ToolRuleAction.DENY
    metadata: Mapping[str, Any] = field(default_factory=dict)
    checksum: str = field(default="")

    def __post_init__(self):
        if not self.policy_name or not isinstance(self.policy_name, str):
            raise ValueError("policy_name must be a non-empty string")
        validate_safe_identifier(self.policy_name, "policy_name")

        if not self.version or not isinstance(self.version, str):
            raise ValueError("version must be a non-empty string")

        if not isinstance(self.rules, tuple):
            object.__setattr__(self, "rules", tuple(self.rules))

        if not isinstance(self.default_action, ToolRuleAction):
            if isinstance(self.default_action, str):
                object.__setattr__(self, "default_action", ToolRuleAction(self.default_action.upper()))
            else:
                raise ValueError("default_action must be a ToolRuleAction instance")

        sanitized_meta = sanitize_security_data(dict(self.metadata))
        object.__setattr__(self, "metadata", deep_freeze(sanitized_meta))

        calculated_checksum = compute_tool_policy_checksum(
            policy_name=self.policy_name,
            version=self.version,
            rules=self.rules,
            default_action=self.default_action,
            metadata=self.metadata,
        )

        if not self.checksum:
            object.__setattr__(self, "checksum", calculated_checksum)
        elif self.checksum != calculated_checksum:
            raise ValueError(
                f"ToolPolicy '{self.policy_name}' checksum mismatch. Expected {calculated_checksum}, got {self.checksum}"
            )

    def is_valid_checksum(self) -> bool:
        calculated = compute_tool_policy_checksum(
            policy_name=self.policy_name,
            version=self.version,
            rules=self.rules,
            default_action=self.default_action,
            metadata=self.metadata,
        )
        return self.checksum == calculated


@dataclass(frozen=True)
class ToolAccessRequest:
    """
    Solicitud inmutable para invocar una herramienta/operación (N.8).
    """
    tool_reference: ToolReference
    request_id: str
    identity_id: Optional[str] = None
    role: Optional[str] = None
    scope: Optional[str] = None
    account_id: Optional[str] = None
    mission_id: Optional[str] = None
    policy_name: Optional[str] = None
    requested_at: Optional[datetime] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.request_id or not isinstance(self.request_id, str):
            raise ValueError("request_id must be a non-empty string")
        if not isinstance(self.tool_reference, ToolReference):
            raise ValueError("tool_reference must be an instance of ToolReference")

        if self.role is not None:
            object.__setattr__(self, "role", self.role.strip().upper())
        if self.scope is not None:
            object.__setattr__(self, "scope", self.scope.strip().lower())
        if self.account_id is not None:
            object.__setattr__(self, "account_id", self.account_id.strip())
        if self.mission_id is not None:
            object.__setattr__(self, "mission_id", self.mission_id.strip())

        sanitized_meta = sanitize_security_data(dict(self.metadata))
        object.__setattr__(self, "metadata", deep_freeze(sanitized_meta))


def compute_tool_decision_checksum(
    request_id: str,
    status: ToolAccessStatus,
    reason_code: ToolAccessReasonCode,
    tool_canonical_id: str,
    policy_name: str,
    policy_version: str,
    evaluated_at: datetime,
    matched_rule_id: Optional[str] = None,
    metadata: Optional[Mapping[str, Any]] = None,
) -> str:
    """
    Calcula un checksum SHA-256 canónico para una ToolAccessDecision.
    """
    dt_str = evaluated_at.isoformat()
    meta_dict = dict(metadata) if metadata else {}
    sanitized_meta = sanitize_security_data(meta_dict)
    sorted_meta_json = json.dumps(sanitized_meta, sort_keys=True, default=str)

    payload = (
        f"{request_id}|{status.value}|{reason_code.value}|{tool_canonical_id}|"
        f"{policy_name}|{policy_version}|{dt_str}|{matched_rule_id or ''}|{sorted_meta_json}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ToolAccessDecision:
    """
    Decisión determinista e inmutable sobre el acceso a una herramienta (N.8).
    """
    request_id: str
    status: ToolAccessStatus
    reason_code: ToolAccessReasonCode
    tool_reference: ToolReference
    policy_name: str
    policy_version: str
    evaluated_at: datetime
    matched_rule_id: Optional[str] = None
    reason_details: str = ""
    checksum: str = field(default="")
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.request_id or not isinstance(self.request_id, str):
            raise ValueError("request_id must be a non-empty string")
        if not isinstance(self.status, ToolAccessStatus):
            raise ValueError("status must be a ToolAccessStatus instance")
        if not isinstance(self.reason_code, ToolAccessReasonCode):
            raise ValueError("reason_code must be a ToolAccessReasonCode instance")
        if not isinstance(self.tool_reference, ToolReference):
            raise ValueError("tool_reference must be a ToolReference instance")

        sanitized_meta = sanitize_security_data(dict(self.metadata))
        object.__setattr__(self, "metadata", deep_freeze(sanitized_meta))

        calculated_checksum = compute_tool_decision_checksum(
            request_id=self.request_id,
            status=self.status,
            reason_code=self.reason_code,
            tool_canonical_id=self.tool_reference.canonical_id,
            policy_name=self.policy_name,
            policy_version=self.policy_version,
            evaluated_at=self.evaluated_at,
            matched_rule_id=self.matched_rule_id,
            metadata=self.metadata,
        )

        if not self.checksum:
            object.__setattr__(self, "checksum", calculated_checksum)
        elif self.checksum != calculated_checksum:
            raise ValueError(
                f"ToolAccessDecision checksum mismatch. Expected {calculated_checksum}, got {self.checksum}"
            )

    @property
    def is_allowed(self) -> bool:
        return self.status == ToolAccessStatus.ALLOW

    def is_valid_checksum(self) -> bool:
        calculated = compute_tool_decision_checksum(
            request_id=self.request_id,
            status=self.status,
            reason_code=self.reason_code,
            tool_canonical_id=self.tool_reference.canonical_id,
            policy_name=self.policy_name,
            policy_version=self.policy_version,
            evaluated_at=self.evaluated_at,
            matched_rule_id=self.matched_rule_id,
            metadata=self.metadata,
        )
        return self.checksum == calculated
