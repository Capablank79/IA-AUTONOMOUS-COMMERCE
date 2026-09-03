"""
Modelos de dominio para Freshness / TTL (Hito L.3 - Transversal Data Quality / Governance).

Define:
- FreshnessStatus: Estados canónicos de frescura temporal (FRESH, STALE, EXPIRED, UNKNOWN, ERROR).
- FreshnessPolicy: Entidad de dominio inmutable que define las reglas y límites temporales (TTL, tolerancia futura, etc.).
- FreshnessAssessment: Entidad inmutable que representa el resultado de evaluar la frescura de un dato/sujeto.
- Utilidades de validación, determinismo, cálculo de age y evaluación temporal.

Principios L.3:
- Inmutabilidad estricta (frozen=True, MappingProxyType, tuples).
- L.3 responde exclusivamente: "¿Este dato sigue siendo suficientemente reciente bajo una política TTL explícita y reproducible?".
- L.3 NO evalúa confianza (L.4), no valida esquemas (L.5), no resuelve entidades (L.6), no detecta duplicados (L.7), ni resuelve conflictos (L.8).
- Reutiliza L.1 (Source Registry) y L.2 (Data Provenance).
- Tratamiento estricto de timestamps: timezone-aware (UTC), timestamps naive normalizados o rechazados según contrato explícito.
- Tratamiento estricto de missing timestamp -> UNKNOWN (UNKNOWN != FRESH).
- Tratamiento estricto de timestamps futuros -> ERROR o UNKNOWN si superan la tolerancia configurable.
- Boundary exacto determinista: age < ttl -> FRESH; age >= ttl -> STALE (o EXPIRED si supera umbral de expiración).
- Datos derivados: la frescura de un dato derivado no puede superar la de sus fuentes/padres (oldest parent constraint).
- Precedencia determinista de políticas: field > subject_type > source_id > source_type > global_default.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from enum import Enum
import hashlib
import json
import re
from types import MappingProxyType
from typing import Mapping, Optional, Any, Tuple, Dict, Sequence, List, Union

from src.domain.security.models import (
    validate_safe_identifier,
    sanitize_security_data,
    deep_freeze,
)
from src.domain.data_provenance.models import SubjectType
from src.domain.source_registry.models import SourceType

_SEMVER_PATTERN = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


class FreshnessStatus(str, Enum):
    """
    Estados canónicos de frescura temporal para un dato o entidad.
    """
    FRESH = "FRESH"          # age < ttl
    STALE = "STALE"          # age >= ttl y dentro de ventana de retención/uso degradado
    EXPIRED = "EXPIRED"      # age >= expire_threshold (o TTL cuando no hay stale intermedio)
    UNKNOWN = "UNKNOWN"      # Sin timestamp confiable o dato temporalmente indeterminable
    ERROR = "ERROR"          # Timestamp futuro inválido o inconsistencia temporal insalvable


def validate_semver(version: str, field_name: str = "version") -> None:
    """Valida que una versión cumpla con la especificación SemVer 2.0.0."""
    if not version or not isinstance(version, str) or not _SEMVER_PATTERN.match(version.strip()):
        raise ValueError(
            f"Invalid {field_name}: '{version}'. Must follow Semantic Versioning (e.g. 1.0.0)."
        )


def compute_policy_checksum(
    policy_id: str,
    name: str,
    version: str,
    ttl_seconds: float,
    stale_threshold_seconds: Optional[float],
    future_tolerance_seconds: float,
    source_type: Optional[str],
    source_id: Optional[str],
    subject_type: Optional[str],
    field_path: Optional[str],
    metadata: Mapping[str, Any],
) -> str:
    """Calcula el checksum SHA-256 canónico para una FreshnessPolicy."""
    sanitized_meta = sanitize_security_data(dict(metadata))
    payload = {
        "policy_id": policy_id,
        "name": name,
        "version": version,
        "ttl_seconds": float(ttl_seconds),
        "stale_threshold_seconds": float(stale_threshold_seconds) if stale_threshold_seconds is not None else None,
        "future_tolerance_seconds": float(future_tolerance_seconds),
        "source_type": source_type or "",
        "source_id": source_id or "",
        "subject_type": subject_type or "",
        "field_path": field_path or "",
        "metadata": sanitized_meta,
    }
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def compute_assessment_checksum(
    assessment_id: str,
    subject_type: str,
    subject_id: str,
    field_path: Optional[str],
    source_id: Optional[str],
    provenance_id: Optional[str],
    observed_at: Optional[str],
    evaluated_at: str,
    ttl_seconds: float,
    age_seconds: Optional[float],
    status: str,
    reason: str,
    policy_id: str,
    policy_version: str,
    correlation_id: str,
    metadata: Mapping[str, Any],
) -> str:
    """Calcula el checksum SHA-256 canónico para un FreshnessAssessment."""
    sanitized_meta = sanitize_security_data(dict(metadata))
    payload = {
        "assessment_id": assessment_id,
        "subject_type": subject_type,
        "subject_id": subject_id,
        "field_path": field_path or "",
        "source_id": source_id or "",
        "provenance_id": provenance_id or "",
        "observed_at": observed_at or "",
        "evaluated_at": evaluated_at,
        "ttl_seconds": float(ttl_seconds),
        "age_seconds": float(age_seconds) if age_seconds is not None else None,
        "status": status,
        "reason": reason,
        "policy_id": policy_id,
        "policy_version": policy_version,
        "correlation_id": correlation_id,
        "metadata": sanitized_meta,
    }
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class FreshnessPolicy:
    """
    Entidad de dominio inmutable que define una política de frescura y TTL (Hito L.3).
    Permite configurar TTL a nivel de campo, sujeto, source_id, source_type o default global.
    """
    policy_id: str
    name: str
    ttl_seconds: float
    version: str = "1.0.0"
    stale_threshold_seconds: Optional[float] = None
    future_tolerance_seconds: float = 5.0
    source_type: Optional[Union[SourceType, str]] = None
    source_id: Optional[str] = None
    subject_type: Optional[Union[SubjectType, str]] = None
    field_path: Optional[str] = None
    description: Optional[str] = None
    checksum: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        # 1. Validar identificadores
        validate_safe_identifier(self.policy_id, field_name="policy_id")
        validate_semver(self.version, field_name="version")

        if self.source_id:
            validate_safe_identifier(self.source_id, field_name="source_id")

        # 2. Validar TTL numérico positivo o cero
        if self.ttl_seconds < 0:
            raise ValueError(f"ttl_seconds cannot be negative, got {self.ttl_seconds}")

        if self.stale_threshold_seconds is not None:
            if self.stale_threshold_seconds < self.ttl_seconds:
                raise ValueError(
                    f"stale_threshold_seconds ({self.stale_threshold_seconds}) must be >= ttl_seconds ({self.ttl_seconds})"
                )

        if self.future_tolerance_seconds < 0:
            raise ValueError(
                f"future_tolerance_seconds cannot be negative, got {self.future_tolerance_seconds}"
            )

        # 3. Normalizar enums y paths
        st_val = None
        if self.source_type is not None:
            st_val = self.source_type.value if hasattr(self.source_type, "value") else str(self.source_type)
            object.__setattr__(self, "source_type", st_val)

        sub_val = None
        if self.subject_type is not None:
            sub_val = self.subject_type.value if hasattr(self.subject_type, "value") else str(self.subject_type)
            object.__setattr__(self, "subject_type", sub_val)

        if self.field_path is not None:
            fp_clean = self.field_path.strip()
            object.__setattr__(self, "field_path", fp_clean if fp_clean else None)

        # 4. Sanitizar y congelar metadata
        sanitized_meta = sanitize_security_data(dict(self.metadata))
        frozen_meta = deep_freeze(sanitized_meta)
        object.__setattr__(self, "metadata", frozen_meta)

        # 5. Calcular o verificar checksum
        expected_checksum = compute_policy_checksum(
            policy_id=self.policy_id,
            name=self.name,
            version=self.version,
            ttl_seconds=self.ttl_seconds,
            stale_threshold_seconds=self.stale_threshold_seconds,
            future_tolerance_seconds=self.future_tolerance_seconds,
            source_type=st_val,
            source_id=self.source_id,
            subject_type=sub_val,
            field_path=self.field_path,
            metadata=self.metadata,
        )

        if self.checksum:
            if self.checksum != expected_checksum:
                raise ValueError(
                    f"Policy checksum mismatch. Expected {expected_checksum}, got {self.checksum}"
                )
        else:
            object.__setattr__(self, "checksum", expected_checksum)


@dataclass(frozen=True)
class FreshnessAssessment:
    """
    Entidad de dominio inmutable que representa la evaluación de frescura de un dato (Hito L.3).
    """
    assessment_id: str
    subject_type: Union[SubjectType, str]
    subject_id: str
    status: FreshnessStatus
    reason: str
    evaluated_at: datetime
    ttl_seconds: float
    age_seconds: Optional[float]
    policy_id: str
    policy_version: str = "1.0.0"
    field_path: Optional[str] = None
    source_id: Optional[str] = None
    provenance_id: Optional[str] = None
    observed_at: Optional[datetime] = None
    correlation_id: str = "default-correlation"
    checksum: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        # 1. Validar identificadores seguros
        validate_safe_identifier(self.assessment_id, field_name="assessment_id")
        validate_safe_identifier(self.subject_id, field_name="subject_id")
        validate_safe_identifier(self.policy_id, field_name="policy_id")
        validate_semver(self.policy_version, field_name="policy_version")

        if self.source_id:
            validate_safe_identifier(self.source_id, field_name="source_id")
        if self.provenance_id:
            validate_safe_identifier(self.provenance_id, field_name="provenance_id")

        # 2. Validar timestamps timezone-aware (UTC)
        if self.evaluated_at.tzinfo is None:
            raise ValueError("evaluated_at must be timezone-aware (UTC)")

        if self.observed_at is not None and self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware (UTC)")

        # 3. Normalizar enums
        st_val = self.subject_type.value if hasattr(self.subject_type, "value") else str(self.subject_type)
        object.__setattr__(self, "subject_type", st_val)

        if not isinstance(self.status, FreshnessStatus):
            object.__setattr__(self, "status", FreshnessStatus(self.status))

        if self.field_path is not None:
            fp_clean = self.field_path.strip()
            object.__setattr__(self, "field_path", fp_clean if fp_clean else None)

        # 4. Sanitizar y congelar metadata
        sanitized_meta = sanitize_security_data(dict(self.metadata))
        frozen_meta = deep_freeze(sanitized_meta)
        object.__setattr__(self, "metadata", frozen_meta)

        # 5. Checksum determinista
        expected_checksum = compute_assessment_checksum(
            assessment_id=self.assessment_id,
            subject_type=st_val,
            subject_id=self.subject_id,
            field_path=self.field_path,
            source_id=self.source_id,
            provenance_id=self.provenance_id,
            observed_at=self.observed_at.isoformat() if self.observed_at else None,
            evaluated_at=self.evaluated_at.isoformat(),
            ttl_seconds=self.ttl_seconds,
            age_seconds=self.age_seconds,
            status=self.status.value,
            reason=self.reason,
            policy_id=self.policy_id,
            policy_version=self.policy_version,
            correlation_id=self.correlation_id,
            metadata=self.metadata,
        )

        if self.checksum:
            if self.checksum != expected_checksum:
                raise ValueError(
                    f"Assessment checksum mismatch. Expected {expected_checksum}, got {self.checksum}"
                )
        else:
            object.__setattr__(self, "checksum", expected_checksum)

    @property
    def is_usable(self) -> bool:
        """
        Indica si el dato está temporalmente utilizable según su estado de frescura.
        FRESH -> True.
        STALE / EXPIRED / UNKNOWN / ERROR -> False.
        """
        return self.status == FreshnessStatus.FRESH
