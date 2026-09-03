"""
Modelos de dominio para Entity Resolution (Hito L.6 - Transversal Data Quality / Governance).

Define:
- EntityType: Tipos de entidades gobernadas (PRODUCT, SUPPLIER, MARKETPLACE_ITEM, LISTING, CUSTOM).
- IdentifierType: Identificadores estándar y específicos (GTIN, EAN, UPC, ISBN, MPN, SKU, MARKETPLACE_ITEM_ID, SUPPLIER_PRODUCT_ID, CUSTOM).
- MatchStatus: Estados semánticos de coincidencia (MATCH, NO_MATCH, POSSIBLE_MATCH, UNKNOWN, ERROR).
- ResolutionReasonCode: Códigos de razón estructurados y deterministas.
- EntityIdentifier: Identificador fuertemente tipado, con soporte para scope/namespace y flags de robustez.
- EntityReference: Representación canónica inmutable de una entidad externa con atributos, identificadores y trazabilidad.
- EntityResolutionPolicy: Política inmutable y versionada que define reglas de emparejamiento, pesos de atributos y umbrales.
- EntityResolutionResult: Resultado inmutable y auditable de la resolución entre dos EntityReference.
- ResolvedEntity: Agregado canónico que agrupa referencias resueltas bajo un canonical_entity_id estable.

Principios L.6:
- Inmutabilidad estricta (frozen=True, MappingProxyType, tuples).
- L.6 responde exclusivamente: "¿Dos representaciones distintas corresponden a la misma entidad lógica?".
- L.6 NO es Duplicate Detection L.7 (no recorre datasets completos buscando duplicados).
- L.6 NO es Conflict Resolution L.8 (no decide qué valor gana ante discrepancias ni fusiona destructivamente datos).
- L.6 NO penaliza identidad por frescura (L.3) ni altera la confianza intrínseca de los datos (L.4).
- Preservación de incertidumbre: UNKNOWN != NO_MATCH, POSSIBLE_MATCH != MATCH.
- Identificadores fuertes (GTIN/EAN/UPC/ISBN/MPN) tienen prioridad sobre atributos textuales.
- Identificadores scoped (SKU) no coinciden entre distintas fuentes salvo que compartan namespace explícito.
- Strong identifier discrepante (contradictorio) produce NO_MATCH irremediable.
- Integración con L.5: SchemaValidation con FAIL/ERROR impide MATCH (produce ERROR o UNKNOWN).
- Scoring determinista en Decimal (0 a 1) sin punto flotante.
- Sanitización estricta de secretos y seguridad de identificadores (K.8).
- Integridad física y canónica verificable mediante checksums SHA-256.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
import hashlib
import json
import re
from types import MappingProxyType
from typing import Mapping, Optional, Any, Tuple, Dict, Sequence, List, Union
import unicodedata

from src.domain.security.models import (
    validate_safe_identifier,
    sanitize_security_data,
    deep_freeze,
)
from src.domain.freshness.models import validate_semver


class EntityType(str, Enum):
    """Tipos de entidades reconocidas en la resolución de identidad."""
    PRODUCT = "PRODUCT"
    SUPPLIER = "SUPPLIER"
    MARKETPLACE_ITEM = "MARKETPLACE_ITEM"
    LISTING = "LISTING"
    CUSTOM = "CUSTOM"


class IdentifierType(str, Enum):
    """Tipos de identificadores soportados."""
    GTIN = "GTIN"
    EAN = "EAN"
    UPC = "UPC"
    ISBN = "ISBN"
    MPN = "MPN"
    SKU = "SKU"
    MARKETPLACE_ITEM_ID = "MARKETPLACE_ITEM_ID"
    SUPPLIER_PRODUCT_ID = "SUPPLIER_PRODUCT_ID"
    CUSTOM = "CUSTOM"


class MatchStatus(str, Enum):
    """
    Estados canónicos de coincidencia entre dos representaciones.
    UNKNOWN permanece estrictamente separado de NO_MATCH y POSSIBLE_MATCH.
    """
    MATCH = "MATCH"
    NO_MATCH = "NO_MATCH"
    POSSIBLE_MATCH = "POSSIBLE_MATCH"
    UNKNOWN = "UNKNOWN"
    ERROR = "ERROR"


class ResolutionReasonCode(str, Enum):
    """Códigos estructurados y deterministas que justifican la decisión de resolución."""
    EXACT_STRONG_IDENTIFIER_MATCH = "EXACT_STRONG_IDENTIFIER_MATCH"
    CONTRADICTORY_STRONG_IDENTIFIERS = "CONTRADICTORY_STRONG_IDENTIFIERS"
    SCOPED_IDENTIFIER_CROSS_NAMESPACE = "SCOPED_IDENTIFIER_CROSS_NAMESPACE"
    SCOPED_IDENTIFIER_MATCH = "SCOPED_IDENTIFIER_MATCH"
    ATTRIBUTE_HIGH_CONFIDENCE_MATCH = "ATTRIBUTE_HIGH_CONFIDENCE_MATCH"
    ATTRIBUTE_PARTIAL_MATCH = "ATTRIBUTE_PARTIAL_MATCH"
    ATTRIBUTE_MISMATCH = "ATTRIBUTE_MISMATCH"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    SCHEMA_VALIDATION_FAILED = "SCHEMA_VALIDATION_FAILED"
    UNKNOWN_ENTITY_TYPE = "UNKNOWN_ENTITY_TYPE"
    CONFLICTING_ATTRIBUTES = "CONFLICTING_ATTRIBUTES"
    AMBIGUOUS_CANDIDATES = "AMBIGUOUS_CANDIDATES"
    EXPLICIT_POLICY_OVERRIDE = "EXPLICIT_POLICY_OVERRIDE"


# Funciones de normalización deterministas
def normalize_text(text: Optional[str]) -> str:
    """
    Normalización determinista de texto:
    - Unicode NFKC
    - Lowercase
    - Colapso de espacios múltiples
    - Trim
    """
    if not text:
        return ""
    # Unicode NFKD/NFKC para caracteres consistentes
    normalized = unicodedata.normalize("NFKC", str(text))
    # Minúsculas
    lowered = normalized.lower()
    # Colapso de espacios
    collapsed = re.sub(r"\s+", " ", lowered)
    return collapsed.strip()


def normalize_identifier_value(identifier_type: Union[IdentifierType, str], value: Optional[str]) -> str:
    """
    Normalización de identificadores estándar:
    - Para GTIN/EAN/UPC/ISBN: remueve guiones, espacios y puntos.
    - Para MPN/SKU/IDs: normaliza espacios y mayúsculas/minúsculas de forma determinista.
    """
    if not value:
        return ""
    val_str = str(value).strip()
    id_type = identifier_type.value if hasattr(identifier_type, "value") else str(identifier_type).upper()

    if id_type in ("GTIN", "EAN", "UPC", "ISBN"):
        # Identificadores de código de barras: aceptan solo dígitos (y 'X' para ISBN-10).
        # Remover separadores puramente decorativos (espacios, guiones, puntos, slashes)
        # de forma determinista y SIN alterar semántica. NO aplicar a SKU/MPN/IDs libres
        # donde '/' o '-' pueden ser semánticos.
        cleaned = re.sub(r"[\s\-\./]", "", val_str).upper()
        return cleaned

    # Para SKU / MPN / IDs generales: normalización conservadora (no elimina separadores
    # semánticos como '/' o '-').
    return normalize_text(val_str)


@dataclass(frozen=True)
class EntityIdentifier:
    """
    Identificador estructurado de una entidad.
    Soporta scope de namespace para prevenir falsas colisiones de SKU entre diferentes fuentes.
    """
    identifier_type: IdentifierType
    value: str
    namespace: Optional[str] = None
    is_strong: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if isinstance(self.identifier_type, str):
            object.__setattr__(self, "identifier_type", IdentifierType(self.identifier_type))
        if not self.value or not isinstance(self.value, str):
            raise ValueError("EntityIdentifier.value must be a non-empty string")

        # Normalizar el valor
        normalized_val = normalize_identifier_value(self.identifier_type, self.value)
        object.__setattr__(self, "value", normalized_val)

        if self.namespace is not None:
            norm_ns = normalize_text(self.namespace)
            object.__setattr__(self, "namespace", norm_ns if norm_ns else None)

        if not isinstance(self.metadata, MappingProxyType):
            object.__setattr__(self, "metadata", deep_freeze(sanitize_security_data(dict(self.metadata))))

    def matches(self, other: "EntityIdentifier", allow_cross_namespace_sku: bool = False) -> bool:
        """Determina si dos identificadores coinciden lógicamente."""
        if self.identifier_type != other.identifier_type:
            return False

        # Si es un identificador scoped (como SKU) y tienen namespaces diferentes
        if self.identifier_type in (IdentifierType.SKU, IdentifierType.SUPPLIER_PRODUCT_ID):
            if not allow_cross_namespace_sku:
                if self.namespace and other.namespace and self.namespace != other.namespace:
                    return False

        return self.value == other.value


@dataclass(frozen=True)
class EntityReference:
    """
    Representación canónica e inmutable de una entidad externa observada en una fuente.
    Conserva origen (source_id), procedencia (provenance_id) y atributos normalizados.
    """
    entity_type: EntityType
    source_id: str
    source_entity_id: str
    canonical_attributes: Mapping[str, Any] = field(default_factory=dict)
    identifiers: Tuple[EntityIdentifier, ...] = field(default_factory=tuple)
    provenance_id: Optional[str] = None
    schema_version: str = "1.0.0"
    schema_validation_status: Optional[str] = None
    checksum: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if isinstance(self.entity_type, str):
            object.__setattr__(self, "entity_type", EntityType(self.entity_type))
        if not self.source_id or not isinstance(self.source_id, str):
            raise ValueError("EntityReference.source_id must be a non-empty string")
        if not self.source_entity_id or not isinstance(self.source_entity_id, str):
            raise ValueError("EntityReference.source_entity_id must be a non-empty string")

        validate_safe_identifier(self.source_id, "source_id")
        validate_semver(self.schema_version, "schema_version")

        # Normalizar atributos canónicos
        norm_attrs: Dict[str, Any] = {}
        for k, v in self.canonical_attributes.items():
            if isinstance(v, str):
                norm_attrs[str(k).lower()] = normalize_text(v)
            elif isinstance(v, (int, float, Decimal, bool)):
                norm_attrs[str(k).lower()] = v
            else:
                norm_attrs[str(k).lower()] = v
        object.__setattr__(self, "canonical_attributes", deep_freeze(norm_attrs))

        # Congelar identificadores
        if not isinstance(self.identifiers, tuple):
            object.__setattr__(self, "identifiers", tuple(self.identifiers))

        # Congelar metadata sanitizada
        if not isinstance(self.metadata, MappingProxyType):
            object.__setattr__(self, "metadata", deep_freeze(sanitize_security_data(dict(self.metadata))))

        if not self.checksum:
            object.__setattr__(self, "checksum", compute_entity_reference_checksum(self))

    def get_strong_identifiers(self) -> Tuple[EntityIdentifier, ...]:
        """Obtiene todos los identificadores fuertes asociados."""
        return tuple(i for i in self.identifiers if i.is_strong)

    def get_identifier_by_type(self, id_type: IdentifierType) -> Optional[EntityIdentifier]:
        """Busca el primer identificador de un tipo dado."""
        for i in self.identifiers:
            if i.identifier_type == id_type:
                return i
        return None


@dataclass(frozen=True)
class EntityResolutionPolicy:
    """
    Política versionada y determinista para la resolución de entidades.
    """
    policy_id: str
    name: str
    version: str = "1.0.0"
    entity_type: EntityType = EntityType.PRODUCT
    strong_identifier_types: Tuple[IdentifierType, ...] = (
        IdentifierType.GTIN,
        IdentifierType.EAN,
        IdentifierType.UPC,
        IdentifierType.ISBN,
        IdentifierType.MPN,
    )
    required_attributes: Tuple[str, ...] = ()
    optional_attributes: Tuple[str, ...] = ()
    attribute_weights: Mapping[str, Decimal] = field(default_factory=dict)
    match_threshold: Decimal = Decimal("0.85")
    possible_match_threshold: Decimal = Decimal("0.50")
    allow_cross_source_sku_match: bool = False
    require_exact_brand_match: bool = True
    allow_attribute_only_auto_match: bool = False
    checksum: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.policy_id or not isinstance(self.policy_id, str):
            raise ValueError("EntityResolutionPolicy.policy_id must be a non-empty string")
        if not self.name or not isinstance(self.name, str):
            raise ValueError("EntityResolutionPolicy.name must be a non-empty string")

        validate_safe_identifier(self.policy_id, "policy_id")
        validate_semver(self.version, "version")

        if isinstance(self.entity_type, str):
            object.__setattr__(self, "entity_type", EntityType(self.entity_type))

        if not isinstance(self.strong_identifier_types, tuple):
            norm_strong = tuple(
                IdentifierType(x) if isinstance(x, str) else x
                for x in self.strong_identifier_types
            )
            object.__setattr__(self, "strong_identifier_types", norm_strong)

        if not isinstance(self.required_attributes, tuple):
            object.__setattr__(self, "required_attributes", tuple(str(x).lower() for x in self.required_attributes))

        if not isinstance(self.optional_attributes, tuple):
            object.__setattr__(self, "optional_attributes", tuple(str(x).lower() for x in self.optional_attributes))

        # Validar y congelar pesos
        weights_dict: Dict[str, Decimal] = {}
        for k, v in self.attribute_weights.items():
            k_clean = str(k).lower()
            if isinstance(v, Decimal):
                weights_dict[k_clean] = v
            else:
                try:
                    weights_dict[k_clean] = Decimal(str(v))
                except (InvalidOperation, TypeError, ValueError):
                    weights_dict[k_clean] = Decimal("0.0")
        object.__setattr__(self, "attribute_weights", deep_freeze(weights_dict))

        # Validar umbrales
        if not isinstance(self.match_threshold, Decimal):
            object.__setattr__(self, "match_threshold", Decimal(str(self.match_threshold)))
        if not isinstance(self.possible_match_threshold, Decimal):
            object.__setattr__(self, "possible_match_threshold", Decimal(str(self.possible_match_threshold)))

        if not isinstance(self.metadata, MappingProxyType):
            object.__setattr__(self, "metadata", deep_freeze(sanitize_security_data(dict(self.metadata))))

        if not self.checksum:
            object.__setattr__(self, "checksum", compute_resolution_policy_checksum(self))


@dataclass(frozen=True)
class EntityResolutionResult:
    """
    Resultado inmutable y auditable de una evaluación de resolución entre dos referencias.
    """
    resolution_id: str
    entity_type: EntityType
    status: MatchStatus
    reference_a: EntityReference
    reference_b: EntityReference
    canonical_entity_id: Optional[str] = None
    matched_identifiers: Tuple[str, ...] = field(default_factory=tuple)
    mismatched_identifiers: Tuple[str, ...] = field(default_factory=tuple)
    matched_attributes: Tuple[str, ...] = field(default_factory=tuple)
    mismatched_attributes: Tuple[str, ...] = field(default_factory=tuple)
    missing_attributes: Tuple[str, ...] = field(default_factory=tuple)
    confidence_score: Optional[Decimal] = None
    reason_codes: Tuple[str, ...] = field(default_factory=tuple)
    policy_id: str = "default_policy"
    policy_version: str = "1.0.0"
    resolved_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    correlation_id: str = "default-correlation"
    input_fingerprint: str = ""
    checksum: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.resolution_id or not isinstance(self.resolution_id, str):
            raise ValueError("EntityResolutionResult.resolution_id must be a non-empty string")
        validate_safe_identifier(self.resolution_id, "resolution_id")

        if isinstance(self.entity_type, str):
            object.__setattr__(self, "entity_type", EntityType(self.entity_type))
        if isinstance(self.status, str):
            object.__setattr__(self, "status", MatchStatus(self.status))

        if self.resolved_at.tzinfo is None:
            object.__setattr__(self, "resolved_at", self.resolved_at.replace(tzinfo=timezone.utc))

        if not isinstance(self.matched_identifiers, tuple):
            object.__setattr__(self, "matched_identifiers", tuple(self.matched_identifiers))
        if not isinstance(self.mismatched_identifiers, tuple):
            object.__setattr__(self, "mismatched_identifiers", tuple(self.mismatched_identifiers))
        if not isinstance(self.matched_attributes, tuple):
            object.__setattr__(self, "matched_attributes", tuple(self.matched_attributes))
        if not isinstance(self.mismatched_attributes, tuple):
            object.__setattr__(self, "mismatched_attributes", tuple(self.mismatched_attributes))
        if not isinstance(self.missing_attributes, tuple):
            object.__setattr__(self, "missing_attributes", tuple(self.missing_attributes))
        if not isinstance(self.reason_codes, tuple):
            object.__setattr__(self, "reason_codes", tuple(self.reason_codes))

        if self.confidence_score is not None and not isinstance(self.confidence_score, Decimal):
            object.__setattr__(self, "confidence_score", Decimal(str(self.confidence_score)))

        if not isinstance(self.metadata, MappingProxyType):
            object.__setattr__(self, "metadata", deep_freeze(sanitize_security_data(dict(self.metadata))))

        if not self.input_fingerprint:
            object.__setattr__(self, "input_fingerprint", compute_resolution_input_fingerprint(self))

        if not self.checksum:
            object.__setattr__(self, "checksum", compute_resolution_result_checksum(self))


@dataclass(frozen=True)
class ResolvedEntity:
    """
    Agregado canónico que representa una entidad única resuelta.
    Agrupa referencias fuente preservándolas intactas (sin merge destructivo).
    """
    canonical_entity_id: str
    entity_type: EntityType
    primary_identifiers: Tuple[EntityIdentifier, ...]
    member_references: Tuple[EntityReference, ...]
    resolution_ids: Tuple[str, ...] = field(default_factory=tuple)
    canonical_attributes: Mapping[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    schema_version: str = "1.0.0"
    checksum: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.canonical_entity_id or not isinstance(self.canonical_entity_id, str):
            raise ValueError("ResolvedEntity.canonical_entity_id must be a non-empty string")
        validate_safe_identifier(self.canonical_entity_id, "canonical_entity_id")
        validate_semver(self.schema_version, "schema_version")

        if isinstance(self.entity_type, str):
            object.__setattr__(self, "entity_type", EntityType(self.entity_type))

        if self.created_at.tzinfo is None:
            object.__setattr__(self, "created_at", self.created_at.replace(tzinfo=timezone.utc))
        if self.updated_at.tzinfo is None:
            object.__setattr__(self, "updated_at", self.updated_at.replace(tzinfo=timezone.utc))

        if not isinstance(self.primary_identifiers, tuple):
            object.__setattr__(self, "primary_identifiers", tuple(self.primary_identifiers))
        if not isinstance(self.member_references, tuple):
            object.__setattr__(self, "member_references", tuple(self.member_references))
        if not isinstance(self.resolution_ids, tuple):
            object.__setattr__(self, "resolution_ids", tuple(self.resolution_ids))

        if not isinstance(self.canonical_attributes, MappingProxyType):
            object.__setattr__(self, "canonical_attributes", deep_freeze(dict(self.canonical_attributes)))

        if not isinstance(self.metadata, MappingProxyType):
            object.__setattr__(self, "metadata", deep_freeze(sanitize_security_data(dict(self.metadata))))

        if not self.checksum:
            object.__setattr__(self, "checksum", compute_resolved_entity_checksum(self))


# ============================================================================
# Helpers y Funciones Deterministas de Identidad y Checksums
# ============================================================================

def build_deterministic_canonical_entity_id(
    entity_type: Union[EntityType, str],
    identifiers: Sequence[EntityIdentifier],
    attributes: Optional[Mapping[str, Any]] = None,
) -> str:
    """
    Genera un canonical_entity_id determinista y estable.
    1. Si existe al menos un strong identifier (ej. GTIN, EAN, UPC, ISBN, MPN), toma el prioritario normalizado.
    2. Si no, combina los atributos canónicos mínimos ordenados deterministamente mediante SHA-256.
    """
    type_str = entity_type.value if hasattr(entity_type, "value") else str(entity_type).upper()

    # 1. Prioridad: Strong identifiers
    for strong_type in (
        IdentifierType.GTIN,
        IdentifierType.EAN,
        IdentifierType.UPC,
        IdentifierType.ISBN,
        IdentifierType.MPN,
    ):
        for ident in identifiers:
            if ident.identifier_type == strong_type and ident.value:
                # Sanitizar valor para que sea compatible con safe identifier
                clean_val = re.sub(r"[^a-zA-Z0-9_\-]", "_", ident.value.lower())
                return f"canonical_{type_str.lower()}_{strong_type.value.lower()}_{clean_val}"

    # 2. Si no hay strong identifier, combinar identificadores ordenados
    ident_tokens = []
    for ident in sorted(identifiers, key=lambda x: (x.identifier_type.value, x.value, x.namespace or "")):
        ident_tokens.append(f"{ident.identifier_type.value}:{ident.namespace or 'global'}:{ident.value}")

    if ident_tokens:
        raw_seed = "|".join(ident_tokens)
        h = hashlib.sha256(raw_seed.encode("utf-8")).hexdigest()[:16]
        return f"canonical_{type_str.lower()}_idcluster_{h}"

    # 3. Fallback: atributos mínimos (brand + model)
    attr_dict = attributes or {}
    brand = normalize_text(str(attr_dict.get("brand", "")))
    model = normalize_text(str(attr_dict.get("model", "")))
    if brand and model:
        raw_seed = f"attr:{brand}:{model}"
        h = hashlib.sha256(raw_seed.encode("utf-8")).hexdigest()[:16]
        return f"canonical_{type_str.lower()}_attr_{h}"

    # 4. Fallback hash de todo el diccionario de atributos
    attr_seed = json.dumps(
        {k: str(v) for k, v in sorted(attr_dict.items())},
        sort_keys=True,
    )
    h = hashlib.sha256(attr_seed.encode("utf-8")).hexdigest()[:16]
    return f"canonical_{type_str.lower()}_gen_{h}"


def compute_entity_reference_checksum(ref: EntityReference) -> str:
    """Calcula el checksum SHA-256 canónico de un EntityReference."""
    idents_data = [
        {
            "type": i.identifier_type.value,
            "val": i.value,
            "ns": i.namespace,
            "strong": i.is_strong,
        }
        for i in sorted(ref.identifiers, key=lambda x: (x.identifier_type.value, x.value, x.namespace or ""))
    ]
    payload = {
        "entity_type": ref.entity_type.value,
        "source_id": ref.source_id,
        "source_entity_id": ref.source_entity_id,
        "canonical_attributes": dict(sorted(ref.canonical_attributes.items())),
        "identifiers": idents_data,
        "provenance_id": ref.provenance_id,
        "schema_version": ref.schema_version,
    }
    raw = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def compute_resolution_policy_checksum(policy: EntityResolutionPolicy) -> str:
    """Calcula el checksum SHA-256 canónico de un EntityResolutionPolicy."""
    weights_data = {k: str(v) for k, v in sorted(policy.attribute_weights.items())}
    payload = {
        "policy_id": policy.policy_id,
        "name": policy.name,
        "version": policy.version,
        "entity_type": policy.entity_type.value,
        "strong_identifier_types": sorted([t.value for t in policy.strong_identifier_types]),
        "required_attributes": sorted(policy.required_attributes),
        "optional_attributes": sorted(policy.optional_attributes),
        "attribute_weights": weights_data,
        "match_threshold": str(policy.match_threshold),
        "possible_match_threshold": str(policy.possible_match_threshold),
        "allow_cross_source_sku_match": policy.allow_cross_source_sku_match,
        "require_exact_brand_match": policy.require_exact_brand_match,
        "allow_attribute_only_auto_match": policy.allow_attribute_only_auto_match,
    }
    raw = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def compute_resolution_input_fingerprint(res: EntityResolutionResult) -> str:
    """
    Huella digital lógica determinista de una resolución (replay/idempotencia).

    Representa la IDENTIDAD LÓGICA / input semántico de la resolución, SIN timestamp
    de ejecución (resolved_at). Dos ejecuciones del mismo logical input con la misma
    policy producen el mismo input_fingerprint aunque `resolved_at` cambie entre
    ejecuciones (replay idempotente).

    Es distinta de `checksum` (compute_resolution_result_checksum), que protege también
    `resolved_at` para integridad física del RECORD persistido.
    """
    payload = {
        "resolution_id": res.resolution_id,
        "entity_type": res.entity_type.value,
        "status": res.status.value,
        "canonical_entity_id": res.canonical_entity_id,
        "ref_a_checksum": res.reference_a.checksum,
        "ref_b_checksum": res.reference_b.checksum,
        "matched_identifiers": sorted(res.matched_identifiers),
        "mismatched_identifiers": sorted(res.mismatched_identifiers),
        "matched_attributes": sorted(res.matched_attributes),
        "mismatched_attributes": sorted(res.mismatched_attributes),
        "missing_attributes": sorted(res.missing_attributes),
        "confidence_score": str(res.confidence_score) if res.confidence_score is not None else None,
        "reason_codes": sorted(res.reason_codes),
        "policy_id": res.policy_id,
        "policy_version": res.policy_version,
    }
    raw = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def compute_resolution_result_checksum(res: EntityResolutionResult) -> str:
    """Calcula el checksum SHA-256 canónico de un EntityResolutionResult."""
    payload = {
        "resolution_id": res.resolution_id,
        "entity_type": res.entity_type.value,
        "status": res.status.value,
        "canonical_entity_id": res.canonical_entity_id,
        "ref_a_checksum": res.reference_a.checksum,
        "ref_b_checksum": res.reference_b.checksum,
        "matched_identifiers": sorted(res.matched_identifiers),
        "mismatched_identifiers": sorted(res.mismatched_identifiers),
        "matched_attributes": sorted(res.matched_attributes),
        "mismatched_attributes": sorted(res.mismatched_attributes),
        "missing_attributes": sorted(res.missing_attributes),
        "confidence_score": str(res.confidence_score) if res.confidence_score is not None else None,
        "reason_codes": sorted(res.reason_codes),
        "policy_id": res.policy_id,
        "policy_version": res.policy_version,
        "resolved_at": res.resolved_at.isoformat(),
    }
    raw = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def compute_resolved_entity_checksum(ent: ResolvedEntity) -> str:
    """Calcula el checksum SHA-256 canónico de un ResolvedEntity."""
    idents_data = [
        {
            "type": i.identifier_type.value,
            "val": i.value,
            "ns": i.namespace,
            "strong": i.is_strong,
        }
        for i in sorted(ent.primary_identifiers, key=lambda x: (x.identifier_type.value, x.value, x.namespace or ""))
    ]
    ref_checksums = sorted([r.checksum for r in ent.member_references])
    payload = {
        "canonical_entity_id": ent.canonical_entity_id,
        "entity_type": ent.entity_type.value,
        "primary_identifiers": idents_data,
        "member_reference_checksums": ref_checksums,
        "resolution_ids": sorted(ent.resolution_ids),
        "canonical_attributes": dict(sorted(ent.canonical_attributes.items())),
        "schema_version": ent.schema_version,
    }
    raw = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
