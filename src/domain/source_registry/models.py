"""
Modelos de dominio para el Source Registry (Hito L.1 - Transversal Data Quality / Governance).

Define:
- SourceType: Taxonomía canónica de tipos de fuentes de datos basadas en la arquitectura real.
- SourceStatus: Estados de ciclo de vida del registro de una fuente (ACTIVE, INACTIVE, DEPRECATED, UNKNOWN).
- RegisteredSource: Entidad de dominio inmutable para la definición de una fuente registrada.
- Utilidades de sanitización, validación de ruta e integridad criptográfica SHA-256.

Principios L.1:
- Inmutabilidad estricta (frozen=True, MappingProxyType).
- Identidad canónica determinista y estable.
- Cero almacenamiento de credenciales, secretos, API keys o tokens efímeros.
- Integridad verificable por checksum canónico SHA-256.
- Fronteras estrictas: responde "¿qué fuente es ésta y cómo se identifica canónicamente?".
  NO calcula freshness (L.3), confidence (L.4), provenance de datos específicos (L.2) ni deduplicación de entidades de negocio (L.6/L.7).
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Optional, Any, Dict, Union, Tuple
from urllib.parse import urlparse, urlunparse

from src.domain.security.models import validate_safe_identifier, SENSITIVE_KEYS, sanitize_security_data, deep_freeze


class SourceType(str, Enum):
    """
    Taxonomía canónica de fuentes de datos del ecosistema autónomo.
    Refleja las fuentes reales del proyecto.
    """
    MARKETPLACE_API = "MARKETPLACE_API"
    SUPPLIER = "SUPPLIER"
    WEB_SOURCE = "WEB_SOURCE"
    INTERNAL_SYSTEM = "INTERNAL_SYSTEM"
    USER_INPUT = "USER_INPUT"
    DERIVED_DATASET = "DERIVED_DATASET"
    EXTERNAL_API = "EXTERNAL_API"
    UNKNOWN = "UNKNOWN"


class SourceStatus(str, Enum):
    """
    Estados del ciclo de vida de una fuente en el registro.
    Indica estado en el registro, no salud/disponibilidad en runtime.
    """
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    DEPRECATED = "DEPRECATED"
    UNKNOWN = "UNKNOWN"


def sanitize_endpoint_reference(endpoint: Optional[str]) -> Optional[str]:
    """
    Sanitiza una URL o endpoint eliminando credenciales embebidas (user/pass)
    y parámetros de query sensibles que contengan tokens o secretos.
    """
    if not endpoint or not isinstance(endpoint, str):
        return endpoint

    clean_ep = endpoint.strip()
    if not clean_ep:
        return clean_ep

    try:
        parsed = urlparse(clean_ep)
        if parsed.scheme and parsed.netloc:
            # Eliminar userinfo si existe
            netloc = parsed.netloc
            if "@" in netloc:
                netloc = netloc.split("@")[-1]

            # Sanitizar query params si existen
            query = parsed.query
            if query:
                # Si contiene palabras sensibles, eliminar query completamente por seguridad
                if any(s in query.lower() for s in SENSITIVE_KEYS):
                    query = ""

            sanitized = urlunparse((
                parsed.scheme,
                netloc,
                parsed.path,
                parsed.params,
                query,
                ""  # fragment
            ))
            return sanitized
    except Exception:
        pass

    return clean_ep


def build_canonical_identifier(
    source_type: Union[SourceType, str],
    provider: str,
    raw_identifier: str
) -> str:
    """
    Construye un identificador canónico determinista y normalizado.
    Ejemplo: 'marketplace_api:mercadolibre:mlc' o 'supplier:direct:prov-100'.
    """
    st_val = source_type.value if hasattr(source_type, "value") else str(source_type)
    norm_st = st_val.strip().lower()
    norm_provider = provider.strip().lower()
    norm_id = raw_identifier.strip().lower()
    return f"{norm_st}:{norm_provider}:{norm_id}"


def compute_source_checksum(
    source_id: str,
    name: str,
    source_type: Union[SourceType, str],
    provider: str,
    canonical_identifier: str,
    endpoint_reference: Optional[str],
    schema_version: str,
    version: str,
    status: Union[SourceStatus, str],
    metadata: Mapping[str, Any],
) -> str:
    """
    Calcula el checksum canónico SHA-256 determinista para una RegisteredSource.
    Cubre todos los campos semánticos inmutables.
    """
    st_val = source_type.value if hasattr(source_type, "value") else str(source_type)
    stat_val = status.value if hasattr(status, "value") else str(status)

    sanitized_meta = sanitize_security_data(dict(metadata))

    semantic_payload = {
        "source_id": source_id,
        "name": name,
        "source_type": st_val,
        "provider": provider,
        "canonical_identifier": canonical_identifier,
        "endpoint_reference": endpoint_reference or "",
        "schema_version": schema_version,
        "version": version,
        "status": stat_val,
        "metadata": sanitized_meta,
    }

    serialized = json.dumps(semantic_payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RegisteredSource:
    """
    Entidad de dominio inmutable para una fuente registrada en el Source Registry (L.1).
    Representa el catálogo canónico y la identidad estable de una fuente de datos.
    """
    source_id: str
    name: str
    source_type: SourceType
    provider: str
    canonical_identifier: str
    created_at: datetime
    updated_at: datetime
    description: Optional[str] = None
    endpoint_reference: Optional[str] = None
    status: SourceStatus = SourceStatus.ACTIVE
    version: str = "1.0.0"
    schema_version: str = "1.0.0"
    checksum: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        # 1. Validar identificador seguro contra path traversal
        validate_safe_identifier(self.source_id, field_name="source_id")
        validate_safe_identifier(self.version, field_name="version")

        if not self.name or not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("name must be a non-empty string.")

        if not self.provider or not isinstance(self.provider, str) or not self.provider.strip():
            raise ValueError("provider must be a non-empty string.")

        if not self.canonical_identifier or not isinstance(self.canonical_identifier, str) or not self.canonical_identifier.strip():
            raise ValueError("canonical_identifier must be a non-empty string.")

        # 2. Validar enums
        if not isinstance(self.source_type, SourceType):
            try:
                object.__setattr__(self, "source_type", SourceType(self.source_type))
            except Exception as e:
                raise ValueError(f"Invalid source_type: {self.source_type}") from e

        if not isinstance(self.status, SourceStatus):
            try:
                object.__setattr__(self, "status", SourceStatus(self.status))
            except Exception as e:
                raise ValueError(f"Invalid status: {self.status}") from e

        # 3. Validar fechas timezone-aware (UTC)
        if self.created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware (UTC)")
        if self.updated_at.tzinfo is None:
            raise ValueError("updated_at must be timezone-aware (UTC)")

        # 4. Sanitizar endpoint_reference
        if self.endpoint_reference is not None:
            clean_endpoint = sanitize_endpoint_reference(self.endpoint_reference)
            object.__setattr__(self, "endpoint_reference", clean_endpoint)

        # 5. Sanitizar y congelar metadata
        sanitized_meta = sanitize_security_data(dict(self.metadata))
        frozen_meta = deep_freeze(sanitized_meta)
        object.__setattr__(self, "metadata", frozen_meta)

        # 6. Calcular checksum canónico determinista si no fue provisto
        expected_checksum = compute_source_checksum(
            source_id=self.source_id,
            name=self.name,
            source_type=self.source_type,
            provider=self.provider,
            canonical_identifier=self.canonical_identifier,
            endpoint_reference=self.endpoint_reference,
            schema_version=self.schema_version,
            version=self.version,
            status=self.status,
            metadata=self.metadata,
        )

        if not self.checksum:
            object.__setattr__(self, "checksum", expected_checksum)
        elif self.checksum != expected_checksum:
            raise ValueError(
                f"Checksum mismatch for RegisteredSource '{self.source_id}': "
                f"provided '{self.checksum}' != expected '{expected_checksum}'"
            )

    def to_dict(self) -> Dict[str, Any]:
        """Convierte la entidad a un diccionario serializable."""
        return {
            "source_id": self.source_id,
            "name": self.name,
            "source_type": self.source_type.value,
            "provider": self.provider,
            "canonical_identifier": self.canonical_identifier,
            "description": self.description,
            "endpoint_reference": self.endpoint_reference,
            "status": self.status.value,
            "version": self.version,
            "schema_version": self.schema_version,
            "checksum": self.checksum,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RegisteredSource":
        """Reconstruye una instancia inmutable desde un diccionario."""
        created_at = datetime.fromisoformat(data["created_at"])
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)

        updated_at = datetime.fromisoformat(data["updated_at"])
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)

        return cls(
            source_id=data["source_id"],
            name=data["name"],
            source_type=SourceType(data["source_type"]),
            provider=data["provider"],
            canonical_identifier=data["canonical_identifier"],
            description=data.get("description"),
            endpoint_reference=data.get("endpoint_reference"),
            status=SourceStatus(data.get("status", "ACTIVE")),
            version=data.get("version", "1.0.0"),
            schema_version=data.get("schema_version", "1.0.0"),
            checksum=data.get("checksum", ""),
            created_at=created_at,
            updated_at=updated_at,
            metadata=data.get("metadata", {}),
        )
