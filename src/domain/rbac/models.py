"""
Modelos de dominio para RBAC / Permissions (Hito N.4 — Transversal N Security, Governance y Safety).

Define:
- PermissionStatus: Estados canónicos de una permission (ACTIVE, DISABLED, DEPRECATED, UNKNOWN).
- RoleStatus: Estados canónicos de un role (ACTIVE, DISABLED, DEPRECATED, UNKNOWN).
- Permission: Permiso inmutable, explícito y granular (permission_id, action, resource_scope opcional).
- Role: Agrupación inmutable de permisos (role_id, name, permissions).
- RoleAssignment: Asignación inmutable y auditable identity_id -> role_id (scope, assigned_at, expires_at, source).
- PermissionSet: Conjunto inmutable de permisos efectivos resueltos.
- RbacEvaluationResult: Contexto inmutable de permisos efectivos resultante de una resolución RBAC.

Principios N.4:
- N.4 responde: "¿Qué roles y permisos tiene asignados esta identidad y qué capacidades concretas representan?".
- N.4 alimenta a N.3 (Authorization): el RBACService resuelve permisos y los inyecta al AuthorizationService.
- N.4 NO ejecuta acciones.
- Inmutabilidad estricta (frozen=True, MappingProxyType, tuples).
- Default Deny: sin role o permission suficiente -> NO permiso (roles/permisos UNKNOWN o ausentes = sin capacidad).
- Permisos explícitos y granulares; prohibido ALL_ACCESS ambiguo salvo requirement real.
- Sin jerarquías de roles complejas (roles planos).
- Cero almacenamiento o propagación de secretos (sanitización y checksum SHA-256 determinista K.8).
- Determinismo absoluto con checksums criptográficos SHA-256.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import re
from types import MappingProxyType
from typing import Mapping, Optional, Any, Tuple, Union, Sequence, Dict, Set

from src.domain.security.models import (
    validate_safe_identifier,
    sanitize_security_data,
    deep_freeze,
)


def normalize_action_token(token: str) -> str:
    """
    Normaliza un token de acción a una forma canónica determinista para matching exacto.
    Ejemplos:
      "listing.read"   -> "LISTING_READ"
      "listing.publish"-> "LISTING_PUBLISH"
      "PUBLISH_LISTING"-> "PUBLISH_LISTING"
      "price.update"   -> "PRICE_UPDATE"
    """
    if not isinstance(token, str) or not token.strip():
        raise ValueError("action token must be a non-empty string.")
    normalized = re.sub(r"[.\-\s]+", "_", token.strip()).upper()
    return normalized


class PermissionStatus(str, Enum):
    """Estados canónicos de un permiso. Sólo ACTIVE otorga capacidades."""
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"
    DEPRECATED = "DEPRECATED"
    UNKNOWN = "UNKNOWN"


class RoleStatus(str, Enum):
    """Estados canónicos de un role. Sólo ACTIVE aporta permisos a sus asignaciones."""
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"
    DEPRECATED = "DEPRECATED"
    UNKNOWN = "UNKNOWN"


def compute_permission_checksum(
    permission_id: str,
    action: str,
    resource_scope: Optional[str],
    description: Optional[str],
    status: Union[PermissionStatus, str],
    policy_version: str,
) -> str:
    """Calcula el checksum SHA-256 determinista de un Permission."""
    status_val = status.value if isinstance(status, PermissionStatus) else str(status)
    payload = {
        "permission_id": permission_id,
        "action": action,
        "resource_scope": resource_scope or "",
        "description": description or "",
        "status": status_val,
        "policy_version": policy_version,
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Permission:
    """
    Permiso inmutable, explícito y granular.
    - permission_id: identificador canónico del permiso (ej: "listing.read").
    - action: token de acción normalizado a forma canónica (LISTING_READ, PRICE_UPDATE, ...).
    - resource_scope: opcional; si se define, el permiso sólo aplica en ese ámbito
      (ej: "account:MLA_ACCOUNT_A"). None significa ámbito global.
    """
    permission_id: str
    action: str
    resource_scope: Optional[str] = None
    description: Optional[str] = None
    status: PermissionStatus = PermissionStatus.ACTIVE
    policy_version: str = "1.0.0"
    policy: str = "RBAC_N4"
    checksum: Optional[str] = None

    def __post_init__(self):
        validate_safe_identifier(self.permission_id, field_name="permission_id")
        if not isinstance(self.action, str) or not self.action.strip():
            raise ValueError("action must be a non-empty string.")
        norm_action = normalize_action_token(self.action)
        object.__setattr__(self, "action", norm_action)

        if self.resource_scope is not None:
            if not isinstance(self.resource_scope, str) or not self.resource_scope.strip():
                raise ValueError("resource_scope must be a non-empty string when provided.")
            validate_safe_identifier(self.resource_scope.strip(), field_name="resource_scope")
            object.__setattr__(self, "resource_scope", self.resource_scope.strip())

        if self.description is not None:
            object.__setattr__(self, "description", str(self.description).strip() or None)

        if not isinstance(self.status, PermissionStatus):
            try:
                object.__setattr__(self, "status", PermissionStatus(self.status))
            except Exception as e:
                raise ValueError(f"Invalid PermissionStatus: {self.status}") from e

        if not self.policy_version or not isinstance(self.policy_version, str):
            raise ValueError("policy_version must be a non-empty string.")

        if not self.checksum:
            digest = compute_permission_checksum(
                permission_id=self.permission_id,
                action=self.action,
                resource_scope=self.resource_scope,
                description=self.description,
                status=self.status,
                policy_version=self.policy_version,
            )
            object.__setattr__(self, "checksum", digest)

    @property
    def is_active(self) -> bool:
        return self.status == PermissionStatus.ACTIVE

    def matches(self, action: str) -> bool:
        """Compara el permiso contra una acción de forma normalizada y determinista."""
        return self.action == normalize_action_token(action)

    def applies_to_scope(self, evaluation_scope: Optional[str]) -> bool:
        """
        Determina si el permiso aplica en un ámbito de evaluación.
        - Sin resource_scope: aplica globalmente (cualquier ámbito, incluido None).
        - Con resource_scope: aplica SOLO si evaluation_scope coincide exactamente.
        """
        if self.resource_scope is None:
            return True
        if evaluation_scope is None:
            return False
        return self.resource_scope == evaluation_scope

    def to_dict(self) -> Dict[str, Any]:
        """Convierte la instancia a un diccionario serializable."""
        return {
            "permission_id": self.permission_id,
            "action": self.action,
            "resource_scope": self.resource_scope,
            "description": self.description,
            "status": self.status.value,
            "policy_version": self.policy_version,
            "policy": self.policy,
            "checksum": self.checksum,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Permission":
        """Reconstruye una instancia inmutable desde un diccionario."""
        return cls(
            permission_id=data["permission_id"],
            action=data["action"],
            resource_scope=data.get("resource_scope"),
            description=data.get("description"),
            status=PermissionStatus(data.get("status", "ACTIVE")),
            policy_version=data.get("policy_version", "1.0.0"),
            policy=data.get("policy", "RBAC_N4"),
            checksum=data.get("checksum"),
        )


def compute_role_checksum(
    role_id: str,
    name: str,
    permission_checksums: Sequence[str],
    status: Union[RoleStatus, str],
    policy_version: str,
) -> str:
    """Calcula el checksum SHA-256 determinista de un Role (sobre checksums de sus permisos)."""
    status_val = status.value if isinstance(status, RoleStatus) else str(status)
    payload = {
        "role_id": role_id,
        "name": name,
        "permission_checksums": sorted(list(permission_checksums)),
        "status": status_val,
        "policy_version": policy_version,
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Role:
    """
    Role inmutable que agrupa permisos (agrupación plana, sin jerarquías complejas).
    Un role sólo aporta capacidades si su status es ACTIVE.
    """
    role_id: str
    name: str
    permissions: Tuple[Permission, ...] = field(default_factory=tuple)
    status: RoleStatus = RoleStatus.ACTIVE
    policy_version: str = "1.0.0"
    description: Optional[str] = None
    checksum: Optional[str] = None

    def __post_init__(self):
        validate_safe_identifier(self.role_id, field_name="role_id")
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("name must be a non-empty string.")
        object.__setattr__(self, "name", self.name.strip())

        if not isinstance(self.permissions, tuple):
            object.__setattr__(self, "permissions", tuple(self.permissions))
        for perm in self.permissions:
            if not isinstance(perm, Permission):
                raise ValueError("permissions must contain only Permission instances.")

        if not isinstance(self.status, RoleStatus):
            try:
                object.__setattr__(self, "status", RoleStatus(self.status))
            except Exception as e:
                raise ValueError(f"Invalid RoleStatus: {self.status}") from e

        if not self.policy_version or not isinstance(self.policy_version, str):
            raise ValueError("policy_version must be a non-empty string.")

        if self.description is not None:
            object.__setattr__(self, "description", str(self.description).strip() or None)

        if not self.checksum:
            digest = compute_role_checksum(
                role_id=self.role_id,
                name=self.name,
                permission_checksums=[p.checksum for p in self.permissions],
                status=self.status,
                policy_version=self.policy_version,
            )
            object.__setattr__(self, "checksum", digest)

    @property
    def is_active(self) -> bool:
        return self.status == RoleStatus.ACTIVE

    @property
    def permission_ids(self) -> Tuple[str, ...]:
        return tuple(sorted(p.permission_id for p in self.permissions))

    @property
    def active_permissions(self) -> Tuple[Permission, ...]:
        return tuple(p for p in self.permissions if p.is_active)

    def to_dict(self) -> Dict[str, Any]:
        """Convierte la instancia a un diccionario serializable."""
        return {
            "role_id": self.role_id,
            "name": self.name,
            "permissions": [p.to_dict() for p in self.permissions],
            "status": self.status.value,
            "policy_version": self.policy_version,
            "description": self.description,
            "checksum": self.checksum,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Role":
        """Reconstruye una instancia inmutable desde un diccionario."""
        raw_perms = data.get("permissions", [])
        permissions = tuple(
            Permission.from_dict(p) if isinstance(p, dict) else p
            for p in raw_perms
        )
        return cls(
            role_id=data["role_id"],
            name=data["name"],
            permissions=permissions,
            status=RoleStatus(data.get("status", "ACTIVE")),
            policy_version=data.get("policy_version", "1.0.0"),
            description=data.get("description"),
            checksum=data.get("checksum"),
        )


def compute_role_assignment_checksum(
    assignment_id: str,
    identity_id: str,
    role_id: str,
    scope: Optional[str],
    assigned_at: datetime,
    expires_at: Optional[datetime],
    source: str,
    policy_version: str,
) -> str:
    """Calcula el checksum SHA-256 determinista de un RoleAssignment."""
    payload = {
        "assignment_id": assignment_id,
        "identity_id": identity_id,
        "role_id": role_id,
        "scope": scope or "",
        "assigned_at": assigned_at.isoformat() if assigned_at else "",
        "expires_at": expires_at.isoformat() if expires_at else "",
        "source": source,
        "policy_version": policy_version,
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RoleAssignment:
    """
    Asignación inmutable, explícita, auditable, versionada y determinista de identity_id -> role_id.
    - identity_id: referencia a la identidad N.1 (UNKNOWN identity -> sin asignación efectiva).
    - role_id: referencia al role N.4 (desconocido -> sin permiso).
    - scope: opcional; si se define, la asignación sólo aplica en ese ámbito comercial/account/resource.
    - expires_at: opcional; si expira, la asignación deja de otorgar permisos.
    - source: origen/control confiable de la asignación (auditabilidad, sin escalada por metadata).
    """
    assignment_id: str
    identity_id: str
    role_id: str
    scope: Optional[str] = None
    assigned_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: Optional[datetime] = None
    source: str = "RBAC_SERVICE"
    policy_version: str = "1.0.0"
    metadata: Mapping[str, Any] = field(default_factory=dict)
    checksum: Optional[str] = None

    def __post_init__(self):
        validate_safe_identifier(self.assignment_id, field_name="assignment_id")
        validate_safe_identifier(self.identity_id, field_name="identity_id")
        validate_safe_identifier(self.role_id, field_name="role_id")

        if self.scope is not None:
            if not isinstance(self.scope, str) or not self.scope.strip():
                raise ValueError("scope must be a non-empty string when provided.")
            validate_safe_identifier(self.scope.strip(), field_name="scope")
            object.__setattr__(self, "scope", self.scope.strip())

        if self.assigned_at.tzinfo is None:
            raise ValueError("assigned_at must be timezone-aware (UTC)")
        if self.expires_at is not None:
            if self.expires_at.tzinfo is None:
                raise ValueError("expires_at must be timezone-aware (UTC)")

        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("source must be a non-empty string.")

        if not self.policy_version or not isinstance(self.policy_version, str):
            raise ValueError("policy_version must be a non-empty string.")

        sanitized = sanitize_security_data(dict(self.metadata))
        object.__setattr__(self, "metadata", deep_freeze(sanitized))

        if not self.checksum:
            digest = compute_role_assignment_checksum(
                assignment_id=self.assignment_id,
                identity_id=self.identity_id,
                role_id=self.role_id,
                scope=self.scope,
                assigned_at=self.assigned_at,
                expires_at=self.expires_at,
                source=self.source,
                policy_version=self.policy_version,
            )
            object.__setattr__(self, "checksum", digest)

    def is_expired(self, now: datetime) -> bool:
        """Una asignación expirada NO otorga permisos."""
        if self.expires_at is None:
            return False
        return now >= self.expires_at

    def applies_to_scope(self, evaluation_scope: Optional[str]) -> bool:
        """
        Determina si la asignación aplica en un ámbito de evaluación.
        - Sin scope: aplica globalmente (cualquier ámbito, incluido None).
        - Con scope: aplica SOLO si evaluation_scope coincide exactamente.
        """
        if self.scope is None:
            return True
        if evaluation_scope is None:
            return False
        return self.scope == evaluation_scope

    def to_dict(self) -> Dict[str, Any]:
        """Convierte la instancia a un diccionario serializable."""
        return {
            "assignment_id": self.assignment_id,
            "identity_id": self.identity_id,
            "role_id": self.role_id,
            "scope": self.scope,
            "assigned_at": self.assigned_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "source": self.source,
            "policy_version": self.policy_version,
            "metadata": dict(self.metadata),
            "checksum": self.checksum,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RoleAssignment":
        """Reconstruye una instancia inmutable desde un diccionario."""
        assigned_at = datetime.fromisoformat(data["assigned_at"])
        if assigned_at.tzinfo is None:
            assigned_at = assigned_at.replace(tzinfo=timezone.utc)

        expires_at = None
        if data.get("expires_at"):
            expires_at = datetime.fromisoformat(data["expires_at"])
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)

        return cls(
            assignment_id=data["assignment_id"],
            identity_id=data["identity_id"],
            role_id=data["role_id"],
            scope=data.get("scope"),
            assigned_at=assigned_at,
            expires_at=expires_at,
            source=data.get("source", "RBAC_SERVICE"),
            policy_version=data.get("policy_version", "1.0.0"),
            metadata=data.get("metadata", {}),
            checksum=data.get("checksum"),
        )


def compute_evaluation_checksum(
    identity_id: str,
    role_ids: Sequence[str],
    permission_ids: Sequence[str],
    scope: Optional[str],
) -> str:
    """Calcula el checksum SHA-256 determinista de una resolución RBAC (excluye timestamp)."""
    payload = {
        "identity_id": identity_id,
        "roles": sorted(list(role_ids)),
        "permissions": sorted(list(permission_ids)),
        "scope": scope or "",
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PermissionSet:
    """
    Conjunto inmutable de permisos efectivos resultante de una resolución RBAC.
    Los permisos están deduplicados (unión determinista) y ordenados.
    """
    permissions: Tuple[Permission, ...] = field(default_factory=tuple)

    def __post_init__(self):
        if not isinstance(self.permissions, tuple):
            object.__setattr__(self, "permissions", tuple(self.permissions))
        for perm in self.permissions:
            if not isinstance(perm, Permission):
                raise ValueError("permissions must contain only Permission instances.")

    @property
    def is_empty(self) -> bool:
        return len(self.permissions) == 0

    @property
    def permission_ids(self) -> Tuple[str, ...]:
        return tuple(sorted(set(p.permission_id for p in self.permissions)))

    @property
    def actions(self) -> Tuple[str, ...]:
        """Acciones normalizadas únicas, ordenadas determinísticamente."""
        return tuple(sorted(set(p.action for p in self.permissions)))


@dataclass(frozen=True)
class RbacEvaluationResult:
    """
    Contexto inmutable de permisos efectivos resultante de una resolución RBAC (Hito N.4).
    Alimenta a N.3 Authorization: sus acciones se inyectan como allowed_actions al AuthorizationService.
    """
    identity_id: str
    effective_permissions: PermissionSet
    roles: Tuple[str, ...] = field(default_factory=tuple)
    evaluated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    scope: Optional[str] = None
    correlation_id: Optional[str] = None
    checksum: str = field(init=False)

    def __post_init__(self):
        validate_safe_identifier(self.identity_id, field_name="identity_id")
        if not isinstance(self.effective_permissions, PermissionSet):
            raise ValueError("effective_permissions must be a PermissionSet.")
        if not isinstance(self.roles, tuple):
            object.__setattr__(self, "roles", tuple(self.roles))
        if self.evaluated_at.tzinfo is None:
            raise ValueError("evaluated_at must be timezone-aware (UTC)")

        digest = compute_evaluation_checksum(
            identity_id=self.identity_id,
            role_ids=self.roles,
            permission_ids=self.effective_permissions.permission_ids,
            scope=self.scope,
        )
        object.__setattr__(self, "checksum", digest)

    @property
    def is_empty(self) -> bool:
        return self.effective_permissions.is_empty

    @property
    def permission_ids(self) -> Tuple[str, ...]:
        return self.effective_permissions.permission_ids

    @property
    def actions(self) -> Tuple[str, ...]:
        return self.effective_permissions.actions