"""
Modelos de dominio para Confidence Model (Hito L.4 - Transversal Data Quality / Governance).

Define:
- ConfidenceLevel: Niveles canónicos de confianza (HIGH, MEDIUM, LOW, UNKNOWN, ERROR).
- ConfidenceFactor: Factor estructurado observable y determinista que contribuye a la confianza.
- ConfidencePolicy: Entidad de dominio inmutable que define las reglas, factores, pesos y requerimientos mínimos de evidencia para calcular la confianza.
- ConfidenceAssessment: Entidad inmutable que representa el resultado reproducible de la evaluación de confianza sobre un dato/sujeto.
- Utilidades de validación, determinismo, cálculo de score Decimal y cálculo de checksum SHA-256.

Principios L.4:
- Inmutabilidad estricta (frozen=True, MappingProxyType, tuples).
- L.4 responde exclusivamente: "¿Qué nivel de confianza tenemos en este dato, dada la evidencia disponible?".
- L.4 NO evalúa si el dato está fresco (L.3), no valida esquemas (L.5), no resuelve entidades (L.6), no detecta duplicados (L.7), ni resuelve conflictos (L.8).
- Reutiliza L.1 (Source Registry), L.2 (Data Provenance) y L.3 (Freshness / TTL).
- Uso estricto de Decimal para scores numéricos (0.00 <= score <= 1.00). No float para decisiones sensibles.
- UNKNOWN se preserva rigurosamente distinto de LOW o 0.
- Missing evidence / missing provenance / unknown source / unknown freshness nunca producen HIGH silencioso.
- Datos derivados: agregación determinista configurable (MIN, WEIGHTED, REQUIRED_ALL).
- Integridad criptográfica verificable por checksum SHA-256 canónico.
- Versionado semántico SemVer en políticas y trazabilidad de correlation_id.
- Explicabilidad estructurada sin razonamiento privado / Chain-of-Thought.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
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
from src.domain.freshness.models import FreshnessStatus, validate_semver


class ConfidenceLevel(str, Enum):
    """
    Niveles canónicos de confianza para un dato o entidad.
    UNKNOWN permanece estrictamente distinto de LOW y ERROR.
    """
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNKNOWN = "UNKNOWN"
    ERROR = "ERROR"


class DerivedAggregationStrategy(str, Enum):
    """
    Estrategias deterministas de agregación de confianza para hechos derivados con múltiples padres.
    """
    MIN = "MIN"                        # Mínimo score entre todos los padres (más conservador)
    WEIGHTED = "WEIGHTED"              # Promedio ponderado determinista
    REQUIRED_ALL = "REQUIRED_ALL"      # Requiere que todos los padres cumplan umbral mínimo, sino degrada


@dataclass(frozen=True)
class ConfidenceFactor:
    """
    Factor observable y determinista que contribuye a la evaluación de confianza.
    Explica estructuradamente el impacto sin CoT ni texto libre opaco.
    """
    factor_name: str
    factor_type: str                   # e.g. "SOURCE_IDENTITY", "PROVENANCE_COMPLETENESS", "FRESHNESS_STATUS", "DIRECT_EVIDENCE", "PARENT_CONFIDENCE"
    score: Optional[Decimal] = None    # Contribución numérica Decimal [0.0, 1.0] si aplica
    weight: Optional[Decimal] = None   # Peso Decimal [0.0, 1.0] si aplica
    impact: str = "NEUTRAL"            # "POSITIVE", "NEGATIVE", "NEUTRAL", "CRITICAL_PENALTY", "UNKNOWN"
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.factor_name or not isinstance(self.factor_name, str):
            raise ValueError("factor_name must be a non-empty string")
        if not self.factor_type or not isinstance(self.factor_type, str):
            raise ValueError("factor_type must be a non-empty string")
        if self.score is not None:
            if not isinstance(self.score, Decimal):
                raise ValueError(f"factor score must be a Decimal, got {type(self.score)}")
            if self.score < Decimal("0") or self.score > Decimal("1"):
                raise ValueError(f"factor score must be between 0 and 1, got {self.score}")
        if self.weight is not None:
            if not isinstance(self.weight, Decimal):
                raise ValueError(f"factor weight must be a Decimal, got {type(self.weight)}")
            if self.weight < Decimal("0") or self.weight > Decimal("1"):
                raise ValueError(f"factor weight must be between 0 and 1, got {self.weight}")

        sanitized_details = sanitize_security_data(dict(self.details))
        frozen_details = deep_freeze(sanitized_details)
        object.__setattr__(self, "details", frozen_details)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "factor_name": self.factor_name,
            "factor_type": self.factor_type,
            "score": str(self.score) if self.score is not None else None,
            "weight": str(self.weight) if self.weight is not None else None,
            "impact": self.impact,
            "details": dict(self.details),
        }


def _encode_decimal_or_val(v: Any) -> Any:
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, (dict, MappingProxyType)):
        return {str(k): _encode_decimal_or_val(val) for k, val in v.items()}
    if isinstance(v, (list, tuple)):
        return [_encode_decimal_or_val(item) for item in v]
    return v


def compute_policy_checksum(
    policy_id: str,
    name: str,
    version: str,
    source_type: Optional[str],
    source_id: Optional[str],
    subject_type: Optional[str],
    field_path: Optional[str],
    description: Optional[str],
    high_threshold: Decimal,
    medium_threshold: Decimal,
    weights: Mapping[str, Decimal],
    factor_scores: Mapping[str, Decimal],
    require_provenance: bool,
    require_freshness: bool,
    derived_aggregation: str,
    metadata: Mapping[str, Any],
) -> str:
    """Calcula el checksum SHA-256 canónico para una ConfidencePolicy."""
    sanitized_meta = sanitize_security_data(dict(metadata))
    payload = {
        "policy_id": policy_id,
        "name": name,
        "version": version,
        "source_type": source_type or "",
        "source_id": source_id or "",
        "subject_type": subject_type or "",
        "field_path": field_path or "",
        "description": description or "",
        "high_threshold": str(high_threshold),
        "medium_threshold": str(medium_threshold),
        "weights": {str(k): str(v) for k, v in sorted(weights.items())},
        "factor_scores": {str(k): str(v) for k, v in sorted(factor_scores.items())},
        "require_provenance": bool(require_provenance),
        "require_freshness": bool(require_freshness),
        "derived_aggregation": derived_aggregation,
        "metadata": _encode_decimal_or_val(sanitized_meta),
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
    evaluated_at: str,
    score: Optional[Decimal],
    level: str,
    reason: str,
    policy_id: str,
    policy_version: str,
    factors: Sequence[Dict[str, Any]],
    correlation_id: str,
    metadata: Mapping[str, Any],
) -> str:
    """Calcula el checksum SHA-256 canónico para un ConfidenceAssessment."""
    sanitized_meta = sanitize_security_data(dict(metadata))
    payload = {
        "assessment_id": assessment_id,
        "subject_type": subject_type,
        "subject_id": subject_id,
        "field_path": field_path or "",
        "source_id": source_id or "",
        "provenance_id": provenance_id or "",
        "evaluated_at": evaluated_at,
        "score": str(score) if score is not None else None,
        "level": level,
        "reason": reason,
        "policy_id": policy_id,
        "policy_version": policy_version,
        "factors": [_encode_decimal_or_val(f) for f in factors],
        "correlation_id": correlation_id,
        "metadata": _encode_decimal_or_val(sanitized_meta),
    }
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ConfidencePolicy:
    """
    Entidad de dominio inmutable que define una política de cálculo de confianza (Hito L.4).
    Configurable a nivel de campo, sujeto, source_id, source_type o default global.
    """
    policy_id: str
    name: str
    version: str = "1.0.0"
    source_type: Optional[Union[SourceType, str]] = None
    source_id: Optional[str] = None
    subject_type: Optional[Union[SubjectType, str]] = None
    field_path: Optional[str] = None
    description: Optional[str] = None
    high_threshold: Decimal = Decimal("0.80")
    medium_threshold: Decimal = Decimal("0.50")
    # Pesos y valores base siempre explícitos en la policy; no existen baselines globales implícitos.
    weights: Mapping[str, Decimal] = field(default_factory=dict)
    factor_scores: Mapping[str, Decimal] = field(default_factory=dict)
    require_provenance: bool = True
    require_freshness: bool = False
    derived_aggregation: DerivedAggregationStrategy = DerivedAggregationStrategy.MIN
    checksum: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        # 1. Validar identificadores y semver
        validate_safe_identifier(self.policy_id, field_name="policy_id")
        validate_semver(self.version, field_name="version")

        if self.source_id:
            validate_safe_identifier(self.source_id, field_name="source_id")

        # 2. Validar umbrales Decimal
        if not isinstance(self.high_threshold, Decimal):
            raise ValueError(f"high_threshold must be a Decimal, got {type(self.high_threshold)}")
        if not isinstance(self.medium_threshold, Decimal):
            raise ValueError(f"medium_threshold must be a Decimal, got {type(self.medium_threshold)}")

        if self.high_threshold < Decimal("0") or self.high_threshold > Decimal("1"):
            raise ValueError(f"high_threshold must be between 0 and 1, got {self.high_threshold}")
        if self.medium_threshold < Decimal("0") or self.medium_threshold > Decimal("1"):
            raise ValueError(f"medium_threshold must be between 0 and 1, got {self.medium_threshold}")
        if self.medium_threshold > self.high_threshold:
            raise ValueError(
                f"medium_threshold ({self.medium_threshold}) cannot be greater than high_threshold ({self.high_threshold})"
            )

        # 3. Validar pesos Decimal y su normalización
        clean_weights: Dict[str, Decimal] = {}
        total_weight = Decimal("0")
        for k, v in dict(self.weights).items():
            if not isinstance(v, Decimal):
                raise ValueError(f"weight for '{k}' must be a Decimal, got {type(v)}")
            if v < Decimal("0") or v > Decimal("1"):
                raise ValueError(f"weight for '{k}' must be between 0 and 1, got {v}")
            clean_weights[str(k)] = v
            total_weight += v

        if clean_weights and total_weight <= Decimal("0"):
            raise ValueError("weights must include at least one positive Decimal value")
        object.__setattr__(self, "weights", MappingProxyType(clean_weights))

        clean_factor_scores: Dict[str, Decimal] = {}
        for k, v in dict(self.factor_scores).items():
            if not isinstance(v, Decimal):
                raise ValueError(f"factor score for '{k}' must be a Decimal, got {type(v)}")
            if v < Decimal("0") or v > Decimal("1"):
                raise ValueError(f"factor score for '{k}' must be between 0 and 1, got {v}")
            clean_factor_scores[str(k)] = v
        object.__setattr__(self, "factor_scores", MappingProxyType(clean_factor_scores))

        # 4. Normalizar enums y paths
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

        if not isinstance(self.derived_aggregation, DerivedAggregationStrategy):
            object.__setattr__(self, "derived_aggregation", DerivedAggregationStrategy(self.derived_aggregation))

        # 5. Sanitizar y congelar metadata
        sanitized_meta = sanitize_security_data(dict(self.metadata))
        frozen_meta = deep_freeze(sanitized_meta)
        object.__setattr__(self, "metadata", frozen_meta)

        # 6. Checksum determinista
        expected_checksum = compute_policy_checksum(
            policy_id=self.policy_id,
            name=self.name,
            version=self.version,
            source_type=st_val,
            source_id=self.source_id,
            subject_type=sub_val,
            field_path=self.field_path,
            description=self.description,
            high_threshold=self.high_threshold,
            medium_threshold=self.medium_threshold,
            weights=self.weights,
            factor_scores=self.factor_scores,
            require_provenance=self.require_provenance,
            require_freshness=self.require_freshness,
            derived_aggregation=self.derived_aggregation.value,
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
class ConfidenceAssessment:
    """
    Entidad de dominio inmutable que representa la evaluación de confianza de un dato (Hito L.4).
    """
    assessment_id: str
    subject_type: Union[SubjectType, str]
    subject_id: str
    level: ConfidenceLevel
    reason: str
    evaluated_at: datetime
    policy_id: str
    score: Optional[Decimal] = None
    policy_version: str = "1.0.0"
    field_path: Optional[str] = None
    source_id: Optional[str] = None
    provenance_id: Optional[str] = None
    factors: Tuple[ConfidenceFactor, ...] = field(default_factory=tuple)
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

        # 3. Validar score Decimal si está presente
        if self.score is not None:
            if not isinstance(self.score, Decimal):
                raise ValueError(f"assessment score must be a Decimal, got {type(self.score)}")
            if self.score < Decimal("0") or self.score > Decimal("1"):
                raise ValueError(f"assessment score must be between 0 and 1, got {self.score}")

        # 4. Normalizar enums
        st_val = self.subject_type.value if hasattr(self.subject_type, "value") else str(self.subject_type)
        object.__setattr__(self, "subject_type", st_val)

        if not isinstance(self.level, ConfidenceLevel):
            object.__setattr__(self, "level", ConfidenceLevel(self.level))

        if self.field_path is not None:
            fp_clean = self.field_path.strip()
            object.__setattr__(self, "field_path", fp_clean if fp_clean else None)

        # 5. Normalizar factores a tupla
        if not isinstance(self.factors, tuple):
            object.__setattr__(self, "factors", tuple(self.factors))

        # 6. Sanitizar y congelar metadata
        sanitized_meta = sanitize_security_data(dict(self.metadata))
        frozen_meta = deep_freeze(sanitized_meta)
        object.__setattr__(self, "metadata", frozen_meta)

        # 7. Checksum determinista
        factors_payload = [f.to_dict() for f in self.factors]
        expected_checksum = compute_assessment_checksum(
            assessment_id=self.assessment_id,
            subject_type=st_val,
            subject_id=self.subject_id,
            field_path=self.field_path,
            source_id=self.source_id,
            provenance_id=self.provenance_id,
            evaluated_at=self.evaluated_at.isoformat(),
            score=self.score,
            level=self.level.value,
            reason=self.reason,
            policy_id=self.policy_id,
            policy_version=self.policy_version,
            factors=factors_payload,
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
    def is_high(self) -> bool:
        return self.level == ConfidenceLevel.HIGH

    @property
    def is_usable(self) -> bool:
        """Indica si el dato tiene confianza suficiente para uso estándar (HIGH o MEDIUM)."""
        return self.level in (ConfidenceLevel.HIGH, ConfidenceLevel.MEDIUM)
