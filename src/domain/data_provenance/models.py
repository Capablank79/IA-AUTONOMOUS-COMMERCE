"""
Modelos de dominio para Data Provenance (Hito L.2 - Transversal Data Quality / Governance).

Define:
- SubjectType: Taxonomía de sujetos/entidades de datos rastreables (MARKET_OBSERVATION, SUPPLIER_QUOTE, LISTING, PRODUCT_OPPORTUNITY, DECISION, EVALUATION, DERIVED_FACT, etc.).
- ProvenanceRecord: Entidad de dominio inmutable para el linaje y trazabilidad de un hecho o dato (a nivel de entidad o campo).
- SourceLineageTrace: Agregado inmutable que encapsula el linaje completo reconstruido (DAG / path de ascendencia hasta fuentes raíz).
- Utilidades para cálculo de checksum canónico SHA-256, determinismo y validaciones de DAG (detección de ciclos y duplicados).

Principios L.2:
- Inmutabilidad estricta (frozen=True, MappingProxyType, tuples).
- Identidad determinista por (source_id, subject_type, subject_id, field_path, evidence_id, parent_provenance_ids, transformation_id).
- Integridad verificable por checksum SHA-256 sobre campos semánticos inmutables.
- Soporta linaje directo (SOURCE -> FACT) y linaje derivado (PARENTS -> TRANSFORMATION -> DERIVED FACT).
- Soporta granularidad a nivel de objeto o campo específico (field_path opcional).
- Detección de ciclos y validación DAG simple sin duplicar bases de datos de grafos.
- Cero almacenamiento de secretos o credenciales (sanitización estricta).
- Fronteras estrictas: responde "¿de qué fuente y evidencia concreta proviene este dato?".
  NO calcula freshness (L.3), confidence (L.4), schema validation (L.5), entity resolution (L.6), duplicate detection (L.7) ni conflict resolution (L.8).
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
from types import MappingProxyType
from typing import Mapping, Optional, Any, Tuple, Dict, Sequence, List, Union, Set

from src.domain.security.models import (
    validate_safe_identifier,
    sanitize_security_data,
    deep_freeze,
)


class SubjectType(str, Enum):
    """
    Taxonomía canónica de tipos de sujetos / entidades de negocio cuyos datos tienen provenance.
    """
    MARKET_OBSERVATION = "MARKET_OBSERVATION"
    MARKET_LISTING = "MARKET_LISTING"
    MARKET_SNAPSHOT = "MARKET_SNAPSHOT"
    SUPPLIER_QUOTE = "SUPPLIER_QUOTE"
    SUPPLIER_PRODUCT = "SUPPLIER_PRODUCT"
    PRODUCT_OPPORTUNITY = "PRODUCT_OPPORTUNITY"
    DECISION = "DECISION"
    EVALUATION = "EVALUATION"
    CATALOG_PRODUCT = "CATALOG_PRODUCT"
    DERIVED_FACT = "DERIVED_FACT"
    INTERNAL_METRIC = "INTERNAL_METRIC"
    GENERIC_FACT = "GENERIC_FACT"


def compute_provenance_checksum(
    provenance_id: str,
    source_id: str,
    source_version: str,
    source_record_id: Optional[str],
    evidence_id: Optional[str],
    subject_type: Union[SubjectType, str],
    subject_id: str,
    field_path: Optional[str],
    captured_at: str,
    parent_provenance_ids: Tuple[str, ...],
    transformation_id: Optional[str],
    correlation_id: str,
    causation_id: Optional[str],
    schema_version: str,
    metadata: Mapping[str, Any],
) -> str:
    """
    Calcula el checksum canónico SHA-256 determinista para un ProvenanceRecord.
    Cubre todos los campos semánticos inmutables.
    """
    st_val = subject_type.value if hasattr(subject_type, "value") else str(subject_type)
    sanitized_meta = sanitize_security_data(dict(metadata))

    semantic_payload = {
        "provenance_id": provenance_id,
        "source_id": source_id,
        "source_version": source_version,
        "source_record_id": source_record_id or "",
        "evidence_id": evidence_id or "",
        "subject_type": st_val,
        "subject_id": subject_id,
        "field_path": field_path or "",
        "captured_at": captured_at,
        "parent_provenance_ids": list(parent_provenance_ids),
        "transformation_id": transformation_id or "",
        "correlation_id": correlation_id,
        "causation_id": causation_id or "",
        "schema_version": schema_version,
        "metadata": sanitized_meta,
    }

    serialized = json.dumps(semantic_payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def generate_deterministic_provenance_id(
    source_id: str,
    subject_type: Union[SubjectType, str],
    subject_id: str,
    field_path: Optional[str] = None,
    evidence_id: Optional[str] = None,
    source_record_id: Optional[str] = None,
    parent_provenance_ids: Sequence[str] = (),
    transformation_id: Optional[str] = None,
) -> str:
    """
    Genera un identificador determinista para una línea de procedencia lógica.
    Si el mismo origen, sujeto, campo, evidencia y padres se vuelven a registrar,
    producirán exactamente el mismo provenance_id.
    """
    st_val = subject_type.value if hasattr(subject_type, "value") else str(subject_type)
    norm_source = source_id.strip().lower()
    norm_st = st_val.strip().lower()
    norm_subject_id = subject_id.strip().lower()
    norm_field = (field_path or "").strip().lower()
    norm_evidence = (evidence_id or "").strip().lower()
    norm_source_rec = (source_record_id or "").strip().lower()
    norm_trans = (transformation_id or "").strip().lower()
    sorted_parents = sorted(p.strip().lower() for p in parent_provenance_ids if p and p.strip())

    raw_seed = f"{norm_source}|{norm_st}|{norm_subject_id}|{norm_field}|{norm_evidence}|{norm_source_rec}|{norm_trans}|{','.join(sorted_parents)}"
    digest = hashlib.sha256(raw_seed.encode("utf-8")).hexdigest()[:24]
    return f"prov-{digest}"


@dataclass(frozen=True)
class ProvenanceRecord:
    """
    Entidad de dominio inmutable que representa el registro formal de procedencia (linaje)
    de un dato o entidad de negocio (Hito L.2).
    """
    provenance_id: str
    source_id: str
    subject_type: SubjectType
    subject_id: str
    captured_at: datetime
    source_version: str = "1.0.0"
    source_record_id: Optional[str] = None
    evidence_id: Optional[str] = None
    field_path: Optional[str] = None
    parent_provenance_ids: Tuple[str, ...] = field(default_factory=tuple)
    transformation_id: Optional[str] = None
    correlation_id: str = "default-correlation"
    causation_id: Optional[str] = None
    schema_version: str = "1.0.0"
    checksum: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        # 1. Validar identificador seguro contra path traversal
        validate_safe_identifier(self.provenance_id, field_name="provenance_id")
        validate_safe_identifier(self.source_id, field_name="source_id")
        validate_safe_identifier(self.subject_id, field_name="subject_id")
        validate_safe_identifier(self.source_version, field_name="source_version")

        if self.transformation_id:
            validate_safe_identifier(self.transformation_id, field_name="transformation_id")
        if self.evidence_id:
            validate_safe_identifier(self.evidence_id, field_name="evidence_id")
        if self.source_record_id:
            validate_safe_identifier(self.source_record_id, field_name="source_record_id")

        # 2. Validar enums
        if not isinstance(self.subject_type, SubjectType):
            try:
                object.__setattr__(self, "subject_type", SubjectType(self.subject_type))
            except Exception as e:
                raise ValueError(f"Invalid subject_type: {self.subject_type}") from e

        # 3. Validar fechas timezone-aware (UTC)
        if self.captured_at.tzinfo is None:
            raise ValueError("captured_at must be timezone-aware (UTC)")

        # 4. Normalizar parent_provenance_ids y prevenir self-parent / duplicados
        parents = list(self.parent_provenance_ids) if self.parent_provenance_ids else []
        clean_parents = []
        seen_parents = set()
        for p in parents:
            if not p or not isinstance(p, str) or not p.strip():
                continue
            clean_p = p.strip()
            validate_safe_identifier(clean_p, field_name="parent_provenance_id")
            if clean_p == self.provenance_id:
                raise ValueError(f"Self-referencing parent provenance is not allowed: '{clean_p}'")
            if clean_p not in seen_parents:
                seen_parents.add(clean_p)
                clean_parents.append(clean_p)

        object.__setattr__(self, "parent_provenance_ids", tuple(clean_parents))

        # 5. Normalizar field_path
        if self.field_path is not None:
            clean_fp = self.field_path.strip()
            if not clean_fp:
                object.__setattr__(self, "field_path", None)
            else:
                object.__setattr__(self, "field_path", clean_fp)

        # 6. Sanitizar y congelar metadata
        sanitized_meta = sanitize_security_data(dict(self.metadata))
        frozen_meta = deep_freeze(sanitized_meta)
        object.__setattr__(self, "metadata", frozen_meta)

        # 7. Calcular o verificar checksum canónico determinista
        expected_checksum = compute_provenance_checksum(
            provenance_id=self.provenance_id,
            source_id=self.source_id,
            source_version=self.source_version,
            source_record_id=self.source_record_id,
            evidence_id=self.evidence_id,
            subject_type=self.subject_type,
            subject_id=self.subject_id,
            field_path=self.field_path,
            captured_at=self.captured_at.isoformat(),
            parent_provenance_ids=self.parent_provenance_ids,
            transformation_id=self.transformation_id,
            correlation_id=self.correlation_id,
            causation_id=self.causation_id,
            schema_version=self.schema_version,
            metadata=self.metadata,
        )

        if not self.checksum:
            object.__setattr__(self, "checksum", expected_checksum)
        elif self.checksum != expected_checksum:
            raise ValueError(
                f"Checksum mismatch for ProvenanceRecord '{self.provenance_id}'. "
                f"Expected {expected_checksum}, got {self.checksum}."
            )

    @property
    def is_derived(self) -> bool:
        """Indica si el registro corresponde a un dato derivado de otras procedencias."""
        return len(self.parent_provenance_ids) > 0 or self.transformation_id is not None


@dataclass(frozen=True)
class SourceLineageTrace:
    """
    Agregado inmutable que encapsula el linaje completo reconstruido para un hecho o sujeto.
    Contiene la cadena DAG de ProvenanceRecords y el conjunto resuelto de root source_ids de L.1.
    """
    target_provenance_id: str
    subject_type: SubjectType
    subject_id: str
    field_path: Optional[str]
    root_source_ids: Tuple[str, ...]
    records_in_lineage: Tuple[ProvenanceRecord, ...]
    is_complete: bool = True
    unresolved_parent_ids: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self):
        if not isinstance(self.root_source_ids, tuple):
            object.__setattr__(self, "root_source_ids", tuple(self.root_source_ids))
        if not isinstance(self.records_in_lineage, tuple):
            object.__setattr__(self, "records_in_lineage", tuple(self.records_in_lineage))
        if not isinstance(self.unresolved_parent_ids, tuple):
            object.__setattr__(self, "unresolved_parent_ids", tuple(self.unresolved_parent_ids))
