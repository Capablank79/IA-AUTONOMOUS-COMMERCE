"""
Modelos de dominio para Organizaciones y Membresías de Usuario (Hito O.2 — Organizations / Users).

Define:
- OrganizationStatus: ACTIVE, SUSPENDED, REMOVED, UNKNOWN.
- MembershipStatus: ACTIVE, SUSPENDED, REMOVED, INVITED, UNKNOWN.
- MembershipRole: Rol organizativo (ej. OWNER, ADMIN, MEMBER, VIEWER, UNKNOWN).
- Organization: Entidad inmutable de Organización acotada estrictamente a un Tenant.
- UserMembership: Entidad inmutable de membresía que vincula tenant_id, organization_id e identity_id (N.1).
- OrganizationReference / MembershipReference: Referencias inmutables livianas.

Principios O.2:
- "¿Qué organizaciones existen dentro de un tenant y qué usuarios pertenecen a cada una?"
- Jerarquía: Tenant -> Organization -> User Membership.
- NUNCA confundir Tenant, Organization, Identity, User, Marketplace Account ni Role.
- Toda Organization pertenece a exactamente un Tenant.
- Toda UserMembership pertenece a un Tenant y una Organization, referenciando una Identity canónica (N.1).
- Cross-Tenant: Membership de Identity en tenant A para Organization de tenant B está estrictamente denegada.
- Safe IDs: Prevención total de path traversal y colisiones.
- Inmutabilidad estricta y checksums criptográficos SHA-256.
- Cero almacenamiento de credenciales/secretos o PII innecesaria.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
from types import MappingProxyType
from typing import Mapping, Optional, Any, Dict, Union, Tuple, Sequence

from src.domain.security.models import (
    validate_safe_identifier,
    sanitize_security_data,
    deep_freeze,
)


class OrganizationStatus(str, Enum):
    """Estados canónicos de ciclo de vida de una Organización."""
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    REMOVED = "REMOVED"
    UNKNOWN = "UNKNOWN"


class MembershipStatus(str, Enum):
    """Estados canónicos de una membresía de usuario en una Organización."""
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    REMOVED = "REMOVED"
    INVITED = "INVITED"
    UNKNOWN = "UNKNOWN"


class MembershipRole(str, Enum):
    """
    Rol organizativo referencial dentro de la Organización.
    NOTA: NO equivale automáticamente a RBAC (N.4) ni otorga bypass de autorización (N.3).
    """
    OWNER = "OWNER"
    ADMIN = "ADMIN"
    MEMBER = "MEMBER"
    VIEWER = "VIEWER"
    UNKNOWN = "UNKNOWN"


def compute_organization_checksum(
    organization_id: str,
    tenant_id: str,
    name: str,
    status: Union[OrganizationStatus, str],
    schema_version: str,
    metadata: Mapping[str, Any],
) -> str:
    """Calcula un checksum SHA-256 canónico y determinista para una Organization."""
    status_val = status.value if isinstance(status, OrganizationStatus) else str(status)
    sanitized_meta = sanitize_security_data(dict(metadata))
    payload = {
        "organization_id": organization_id,
        "tenant_id": tenant_id,
        "name": name,
        "status": status_val,
        "schema_version": schema_version,
        "metadata": sanitized_meta,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_membership_checksum(
    membership_id: str,
    tenant_id: str,
    organization_id: str,
    identity_id: str,
    role: Union[MembershipRole, str],
    status: Union[MembershipStatus, str],
    schema_version: str,
    metadata: Mapping[str, Any],
) -> str:
    """Calcula un checksum SHA-256 canónico y determinista para una UserMembership."""
    status_val = status.value if isinstance(status, MembershipStatus) else str(status)
    role_val = role.value if isinstance(role, MembershipRole) else str(role)
    sanitized_meta = sanitize_security_data(dict(metadata))
    payload = {
        "membership_id": membership_id,
        "tenant_id": tenant_id,
        "organization_id": organization_id,
        "identity_id": identity_id,
        "role": role_val,
        "status": status_val,
        "schema_version": schema_version,
        "metadata": sanitized_meta,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class OrganizationReference:
    """Referencia liviana e inmutable a una Organización."""
    organization_id: str
    tenant_id: str
    name: str

    def __post_init__(self):
        validate_safe_identifier(self.organization_id, field_name="organization_id")
        validate_safe_identifier(self.tenant_id, field_name="tenant_id")
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("name must be a non-empty string.")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "organization_id": self.organization_id,
            "tenant_id": self.tenant_id,
            "name": self.name,
        }


@dataclass(frozen=True)
class Organization:
    """
    Entidad de dominio inmutable para Organización dentro de un Tenant (O.2).

    Reglas:
    - organization_id: Identificador seguro, no vacío y sin path traversal.
    - tenant_id: Tenant propietario exclusivo (obligatorio).
    - name: Nombre amigable y explícito de la organización.
    - status: OrganizationStatus (ACTIVE, SUSPENDED, REMOVED, UNKNOWN).
    - created_at / updated_at: Timestamps timezone-aware (UTC).
    - metadata: Sanitizada y libre de credenciales/secretos.
    - checksum: SHA-256 determinista.
    """
    organization_id: str
    tenant_id: str
    name: str
    created_at: datetime
    updated_at: datetime
    status: OrganizationStatus = OrganizationStatus.ACTIVE
    schema_version: str = "1.0.0"
    checksum: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        validate_safe_identifier(self.organization_id, field_name="organization_id")
        validate_safe_identifier(self.tenant_id, field_name="tenant_id")

        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("Organization name must be a non-empty string.")

        if not isinstance(self.status, OrganizationStatus):
            try:
                object.__setattr__(self, "status", OrganizationStatus(self.status))
            except Exception as e:
                raise ValueError(f"Invalid organization status: {self.status}") from e

        if self.created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware (UTC)")
        if self.updated_at.tzinfo is None:
            raise ValueError("updated_at must be timezone-aware (UTC)")

        sanitized_meta = sanitize_security_data(dict(self.metadata))
        frozen_meta = deep_freeze(sanitized_meta)
        object.__setattr__(self, "metadata", frozen_meta)

        expected_chk = compute_organization_checksum(
            organization_id=self.organization_id,
            tenant_id=self.tenant_id,
            name=self.name,
            status=self.status,
            schema_version=self.schema_version,
            metadata=self.metadata,
        )

        if not self.checksum:
            object.__setattr__(self, "checksum", expected_chk)
        elif self.checksum != expected_chk:
            raise ValueError(
                f"Checksum mismatch for Organization '{self.organization_id}': "
                f"provided '{self.checksum}' != expected '{expected_chk}'"
            )

    @property
    def is_active(self) -> bool:
        return self.status == OrganizationStatus.ACTIVE

    def to_reference(self) -> OrganizationReference:
        return OrganizationReference(
            organization_id=self.organization_id,
            tenant_id=self.tenant_id,
            name=self.name,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "organization_id": self.organization_id,
            "tenant_id": self.tenant_id,
            "name": self.name,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "schema_version": self.schema_version,
            "checksum": self.checksum,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Organization":
        created_at = datetime.fromisoformat(data["created_at"])
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)

        updated_at = datetime.fromisoformat(data["updated_at"])
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)

        return cls(
            organization_id=data["organization_id"],
            tenant_id=data["tenant_id"],
            name=data["name"],
            status=OrganizationStatus(data.get("status", "ACTIVE")),
            created_at=created_at,
            updated_at=updated_at,
            schema_version=data.get("schema_version", "1.0.0"),
            checksum=data.get("checksum", ""),
            metadata=data.get("metadata", {}),
        )


@dataclass(frozen=True)
class UserMembership:
    """
    Entidad de dominio inmutable para Membresía de Usuario en Organización (O.2).

    Reglas:
    - membership_id: ID determinista y seguro (ej. f"mem_{tenant_id}_{org_id}_{identity_id}").
    - tenant_id: Tenant propietario estricto (debe coincidir con el tenant de la organización).
    - organization_id: Organización dentro del tenant.
    - identity_id: Referencia a la Identity canónica N.1.
    - role: Rol organizativo referencial (OWNER, ADMIN, MEMBER, VIEWER).
    - status: MembershipStatus (ACTIVE, SUSPENDED, REMOVED, INVITED, UNKNOWN).
    - joined_at: Momento en que la identidad se vincula.
    - removed_at: Momento opcional de revocación/eliminación.
    - source: Origen o referencia de la membresía.
    - checksum: SHA-256 determinista.
    """
    membership_id: str
    tenant_id: str
    organization_id: str
    identity_id: str
    joined_at: datetime
    role: MembershipRole = MembershipRole.MEMBER
    status: MembershipStatus = MembershipStatus.ACTIVE
    removed_at: Optional[datetime] = None
    source: str = "SYSTEM"
    schema_version: str = "1.0.0"
    checksum: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        validate_safe_identifier(self.membership_id, field_name="membership_id")
        validate_safe_identifier(self.tenant_id, field_name="tenant_id")
        validate_safe_identifier(self.organization_id, field_name="organization_id")
        validate_safe_identifier(self.identity_id, field_name="identity_id")

        if not isinstance(self.role, MembershipRole):
            try:
                object.__setattr__(self, "role", MembershipRole(self.role))
            except Exception as e:
                raise ValueError(f"Invalid membership role: {self.role}") from e

        if not isinstance(self.status, MembershipStatus):
            try:
                object.__setattr__(self, "status", MembershipStatus(self.status))
            except Exception as e:
                raise ValueError(f"Invalid membership status: {self.status}") from e

        if self.joined_at.tzinfo is None:
            raise ValueError("joined_at must be timezone-aware (UTC)")

        if self.removed_at is not None and self.removed_at.tzinfo is None:
            raise ValueError("removed_at must be timezone-aware (UTC)")

        sanitized_meta = sanitize_security_data(dict(self.metadata))
        frozen_meta = deep_freeze(sanitized_meta)
        object.__setattr__(self, "metadata", frozen_meta)

        expected_chk = compute_membership_checksum(
            membership_id=self.membership_id,
            tenant_id=self.tenant_id,
            organization_id=self.organization_id,
            identity_id=self.identity_id,
            role=self.role,
            status=self.status,
            schema_version=self.schema_version,
            metadata=self.metadata,
        )

        if not self.checksum:
            object.__setattr__(self, "checksum", expected_chk)
        elif self.checksum != expected_chk:
            raise ValueError(
                f"Checksum mismatch for UserMembership '{self.membership_id}': "
                f"provided '{self.checksum}' != expected '{expected_chk}'"
            )

    @property
    def is_active(self) -> bool:
        return self.status == MembershipStatus.ACTIVE

    def to_dict(self) -> Dict[str, Any]:
        return {
            "membership_id": self.membership_id,
            "tenant_id": self.tenant_id,
            "organization_id": self.organization_id,
            "identity_id": self.identity_id,
            "role": self.role.value,
            "status": self.status.value,
            "joined_at": self.joined_at.isoformat(),
            "removed_at": self.removed_at.isoformat() if self.removed_at else None,
            "source": self.source,
            "schema_version": self.schema_version,
            "checksum": self.checksum,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "UserMembership":
        joined_at = datetime.fromisoformat(data["joined_at"])
        if joined_at.tzinfo is None:
            joined_at = joined_at.replace(tzinfo=timezone.utc)

        removed_at = None
        if data.get("removed_at"):
            removed_at = datetime.fromisoformat(data["removed_at"])
            if removed_at.tzinfo is None:
                removed_at = removed_at.replace(tzinfo=timezone.utc)

        return cls(
            membership_id=data["membership_id"],
            tenant_id=data["tenant_id"],
            organization_id=data["organization_id"],
            identity_id=data["identity_id"],
            role=MembershipRole(data.get("role", "MEMBER")),
            status=MembershipStatus(data.get("status", "ACTIVE")),
            joined_at=joined_at,
            removed_at=removed_at,
            source=data.get("source", "SYSTEM"),
            schema_version=data.get("schema_version", "1.0.0"),
            checksum=data.get("checksum", ""),
            metadata=data.get("metadata", {}),
        )
