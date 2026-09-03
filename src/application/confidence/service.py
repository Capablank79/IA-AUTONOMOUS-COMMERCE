"""
Servicio de Aplicación para Confidence Model (Hito L.4 - Transversal Data Quality / Governance).

Responsabilidades:
- Resolver políticas por precedencia field > subject > source_id > source_type > default.
- Obtener y reutilizar inputs de L.1 Source Registry, L.2 Data Provenance y L.3 Freshness.
- Evaluar factores observables y deterministas con Decimal.
- Producir ConfidenceAssessment estructurado, explicable y auditable.
- Persistir assessment opcionalmente sin tomar decisiones comerciales.
"""

from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
from typing import Optional, Sequence, Mapping, Any, Dict, List, Tuple, Union

from src.domain.confidence.models import (
    ConfidenceLevel,
    DerivedAggregationStrategy,
    ConfidenceFactor,
    ConfidencePolicy,
    ConfidenceAssessment,
)
from src.domain.confidence.ports import (
    ConfidencePolicyRepositoryPort,
    ConfidenceAssessmentRepositoryPort,
)
from src.domain.source_registry.ports import SourceRegistryRepositoryPort
from src.domain.source_registry.models import SourceStatus, SourceType
from src.domain.data_provenance.ports import ProvenanceRepositoryPort
from src.domain.data_provenance.models import ProvenanceRecord, SubjectType
from src.domain.freshness.ports import FreshnessAssessmentRepositoryPort
from src.domain.freshness.models import FreshnessAssessment, FreshnessStatus
from src.domain.reliability.ports import ClockPort
from src.infrastructure.reliability.reliability_infrastructure import SystemClock


class ConfidenceServiceError(Exception):
    """Excepción base para errores de ConfidenceService."""


class ConfidencePolicyNotFoundError(ConfidenceServiceError):
    """Se lanza cuando no hay política aplicable ni default explícita."""


class ConfidenceService:
    """Servicio determinista de evaluación de confianza L.4."""

    def __init__(
        self,
        policy_repository: ConfidencePolicyRepositoryPort,
        assessment_repository: Optional[ConfidenceAssessmentRepositoryPort] = None,
        source_registry: Optional[SourceRegistryRepositoryPort] = None,
        provenance_repository: Optional[ProvenanceRepositoryPort] = None,
        freshness_repository: Optional[FreshnessAssessmentRepositoryPort] = None,
        clock: Optional[ClockPort] = None,
        default_policy: Optional[ConfidencePolicy] = None,
    ):
        self.policy_repo = policy_repository
        self.assessment_repo = assessment_repository
        self.source_registry = source_registry
        self.provenance_repo = provenance_repository
        self.freshness_repo = freshness_repository
        self.clock = clock or SystemClock()
        self.default_policy = default_policy

    def resolve_policy(
        self,
        *,
        subject_type: Union[SubjectType, str],
        field_path: Optional[str] = None,
        source_id: Optional[str] = None,
        source_type: Optional[Union[SourceType, str]] = None,
    ) -> ConfidencePolicy:
        """Resuelve la política aplicable por precedencia determinista."""
        subject_value = subject_type.value if hasattr(subject_type, "value") else str(subject_type)
        source_type_value = source_type.value if hasattr(source_type, "value") else (str(source_type) if source_type else None)

        policies = list(self.policy_repo.list_policies())

        candidates: List[Tuple[int, ConfidencePolicy]] = []
        for policy in policies:
            # Una policy scoped sólo aplica si todos sus scopes coinciden.
            if policy.field_path is not None and policy.field_path != field_path:
                continue
            if policy.subject_type is not None and policy.subject_type != subject_value:
                continue
            if policy.source_id is not None and policy.source_id != source_id:
                continue
            if policy.source_type is not None and policy.source_type != source_type_value:
                continue

            specificity = 0
            if policy.field_path is not None:
                specificity += 16
            if policy.subject_type is not None:
                specificity += 8
            if policy.source_id is not None:
                specificity += 4
            if policy.source_type is not None:
                specificity += 2
            candidates.append((specificity, policy))

        if candidates:
            # Specificity desc; version lexical semver-compatible enough for x.y.z with numeric parse.
            def semver_tuple(version: str) -> Tuple[int, int, int]:
                core = version.split("-")[0].split("+")[0]
                parts = core.split(".")
                return tuple(int(x) for x in parts[:3])  # type: ignore[return-value]

            candidates.sort(key=lambda item: (item[0], semver_tuple(item[1].version)), reverse=True)
            return candidates[0][1]

        if self.default_policy is not None:
            return self.default_policy
        raise ConfidencePolicyNotFoundError("No applicable ConfidencePolicy and no explicit default policy")

    def assess(
        self,
        *,
        subject_type: Union[SubjectType, str],
        subject_id: str,
        source_id: Optional[str] = None,
        provenance_id: Optional[str] = None,
        freshness_assessment: Optional[FreshnessAssessment] = None,
        field_path: Optional[str] = None,
        evidence_present: bool = True,
        parent_confidences: Sequence[ConfidenceAssessment] = (),
        correlation_id: str = "default-correlation",
        persist: bool = True,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> ConfidenceAssessment:
        """
        Evalúa confianza explícita y reproducible usando inputs L.1/L.2/L.3.

        UNKNOWN permanece distinto de score 0: cuando faltan inputs críticos requeridos,
        score=None y level=UNKNOWN.
        """
        subject_value = subject_type.value if hasattr(subject_type, "value") else str(subject_type)
        now = self.clock.now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        source = None
        source_type = None
        if source_id and self.source_registry:
            source = self.source_registry.get_source(source_id)
            if source is not None:
                source_type = source.source_type

        policy = self.resolve_policy(
            subject_type=subject_value,
            field_path=field_path,
            source_id=source_id,
            source_type=source_type,
        )

        factors: List[ConfidenceFactor] = []
        factor_scores: Dict[str, Optional[Decimal]] = {}
        critical_unknown = False
        critical_error = False

        # Source identity/status factor (L.1 identifies source; trust baseline comes from policy only).
        if source_id is None or source is None:
            factor_scores["source"] = None
            factors.append(ConfidenceFactor(
                factor_name="source_identity",
                factor_type="SOURCE_IDENTITY",
                score=None,
                weight=policy.weights.get("source"),
                impact="UNKNOWN",
                details={"registered": False, "source_id": source_id or ""},
            ))
            critical_unknown = True
        else:
            status_value = source.status.value if hasattr(source.status, "value") else str(source.status)
            source_score_key = "source_active" if status_value == SourceStatus.ACTIVE.value else "source_inactive"
            source_score = policy.factor_scores.get(source_score_key)
            factor_scores["source"] = source_score
            if source_score is None:
                critical_unknown = True
            factors.append(ConfidenceFactor(
                factor_name="source_identity",
                factor_type="SOURCE_IDENTITY",
                score=source_score,
                weight=policy.weights.get("source"),
                impact="POSITIVE" if source_score is not None and source_score >= policy.medium_threshold else "UNKNOWN",
                details={"registered": True, "source_status": status_value, "source_type": str(source_type)},
            ))

        # Provenance/evidence factor (L.2).
        provenance: Optional[ProvenanceRecord] = None
        if provenance_id and self.provenance_repo:
            provenance = self.provenance_repo.get_provenance(provenance_id)

        if provenance is None:
            factor_scores["provenance"] = None
            factors.append(ConfidenceFactor(
                factor_name="provenance_completeness",
                factor_type="PROVENANCE_COMPLETENESS",
                score=None,
                weight=policy.weights.get("provenance"),
                impact="UNKNOWN",
                details={"provenance_present": False},
            ))
            if policy.require_provenance:
                critical_unknown = True
        else:
            is_derived = bool(provenance.parent_provenance_ids)
            provenance_score_key = "provenance_derived" if is_derived else "provenance_direct"
            provenance_score = policy.factor_scores.get(provenance_score_key)
            factor_scores["provenance"] = provenance_score
            if provenance_score is None and policy.require_provenance:
                critical_unknown = True
            factors.append(ConfidenceFactor(
                factor_name="provenance_completeness",
                factor_type="PROVENANCE_COMPLETENESS",
                score=provenance_score,
                weight=policy.weights.get("provenance"),
                impact="POSITIVE",
                details={
                    "provenance_present": True,
                    "evidence_mode": "DERIVED" if is_derived else "DIRECT",
                    "parent_count": len(provenance.parent_provenance_ids),
                },
            ))

        # Freshness is an input, never synonymous with confidence (L.3).
        if freshness_assessment is None and self.freshness_repo:
            freshness_assessment = self.freshness_repo.get_latest_by_subject(
                subject_id=subject_id,
                subject_type=subject_value,
                field_path=field_path,
            )

        freshness_score_map = {
            FreshnessStatus.FRESH: policy.factor_scores.get("freshness_fresh"),
            FreshnessStatus.STALE: policy.factor_scores.get("freshness_stale"),
            FreshnessStatus.EXPIRED: policy.factor_scores.get("freshness_expired"),
            FreshnessStatus.UNKNOWN: None,
            FreshnessStatus.ERROR: None,
        }
        if freshness_assessment is None:
            freshness_score = None
            freshness_status = FreshnessStatus.UNKNOWN
        else:
            freshness_status = freshness_assessment.status
            freshness_score = freshness_score_map[freshness_status]

        factor_scores["freshness"] = freshness_score
        factors.append(ConfidenceFactor(
            factor_name="freshness_status",
            factor_type="FRESHNESS_STATUS",
            score=freshness_score,
            weight=policy.weights.get("freshness"),
            impact=(
                "POSITIVE" if freshness_status == FreshnessStatus.FRESH
                else "NEGATIVE" if freshness_status in (FreshnessStatus.STALE, FreshnessStatus.EXPIRED)
                else "UNKNOWN" if freshness_status == FreshnessStatus.UNKNOWN
                else "CRITICAL_PENALTY"
            ),
            details={"freshness_status": freshness_status.value},
        ))
        if policy.require_freshness and freshness_score is None:
            critical_unknown = True
        if freshness_status == FreshnessStatus.ERROR:
            critical_error = True

        # Presence of concrete evidence is separate from provenance graph completeness.
        evidence_score = policy.factor_scores.get("evidence_present") if evidence_present else None
        factor_scores["evidence"] = evidence_score
        factors.append(ConfidenceFactor(
            factor_name="evidence_presence",
            factor_type="EVIDENCE_PRESENCE",
            score=evidence_score,
            weight=policy.weights.get("evidence"),
            impact="POSITIVE" if evidence_present else "UNKNOWN",
            details={"evidence_present": evidence_present},
        ))
        if not evidence_present or evidence_score is None:
            critical_unknown = True

        # Derived parent confidence aggregation is policy-driven, deterministic and critical-safe.
        parent_score: Optional[Decimal] = None
        if parent_confidences:
            parent_score = self._aggregate_parent_confidence(parent_confidences, policy)
            factor_scores["parents"] = parent_score
            factors.append(ConfidenceFactor(
                factor_name="derived_parent_confidence",
                factor_type="PARENT_CONFIDENCE",
                score=parent_score,
                weight=None,
                impact="NEGATIVE" if parent_score is None or parent_score < policy.medium_threshold else "POSITIVE",
                details={
                    "strategy": policy.derived_aggregation.value,
                    "parent_count": len(parent_confidences),
                    "critical_parent_present": any(
                        p.level in (ConfidenceLevel.LOW, ConfidenceLevel.UNKNOWN, ConfidenceLevel.ERROR)
                        for p in parent_confidences
                    ),
                },
            ))
            if parent_score is None:
                critical_unknown = True
            if any(p.level == ConfidenceLevel.ERROR for p in parent_confidences):
                critical_error = True

        # Resultado conservador para inputs críticos desconocidos/erróneos.
        if critical_error:
            score = None
            level = ConfidenceLevel.ERROR
            reason = "Confidence evaluation failed due to an explicit error in required evidence inputs"
        elif critical_unknown:
            score = None
            level = ConfidenceLevel.UNKNOWN
            reason = "Confidence is unknown because required evidence, source, provenance, freshness, or parent confidence is missing"
        else:
            score = self._weighted_score(factor_scores, policy)
            if parent_score is not None:
                # Un parent crítico no puede quedar oculto por factores HIGH bajo MIN/REQUIRED_ALL.
                if policy.derived_aggregation in (DerivedAggregationStrategy.MIN, DerivedAggregationStrategy.REQUIRED_ALL):
                    score = min(score, parent_score)
            level = self.score_to_level(score, policy)
            reason = f"Confidence calculated deterministically from {len(factors)} explicit factors under policy {policy.policy_id}@{policy.version}"

        assessment_id = self._assessment_id(
            subject_type=subject_value,
            subject_id=subject_id,
            field_path=field_path,
            source_id=source_id,
            provenance_id=provenance_id,
            evaluated_at=now,
            policy=policy,
            factors=factors,
            level=level,
            reason=reason,
        )
        assessment = ConfidenceAssessment(
            assessment_id=assessment_id,
            subject_type=subject_value,
            subject_id=subject_id,
            level=level,
            reason=reason,
            evaluated_at=now,
            policy_id=policy.policy_id,
            score=score,
            policy_version=policy.version,
            field_path=field_path,
            source_id=source_id,
            provenance_id=provenance_id,
            factors=tuple(factors),
            correlation_id=correlation_id,
            metadata=metadata or {},
        )

        if persist and self.assessment_repo:
            return self.assessment_repo.save_assessment(assessment)
        return assessment

    @staticmethod
    def score_to_level(score: Decimal, policy: ConfidencePolicy) -> ConfidenceLevel:
        if score >= policy.high_threshold:
            return ConfidenceLevel.HIGH
        if score >= policy.medium_threshold:
            return ConfidenceLevel.MEDIUM
        return ConfidenceLevel.LOW

    @staticmethod
    def _weighted_score(
        factor_scores: Mapping[str, Optional[Decimal]],
        policy: ConfidencePolicy,
    ) -> Decimal:
        weighted_sum = Decimal("0")
        effective_weight = Decimal("0")
        for name, weight in policy.weights.items():
            score = factor_scores.get(name)
            if score is None:
                continue
            weighted_sum += score * weight
            effective_weight += weight
        if effective_weight == Decimal("0"):
            return Decimal("0.00")
        return (weighted_sum / effective_weight).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)

    @staticmethod
    def _aggregate_parent_confidence(
        parents: Sequence[ConfidenceAssessment],
        policy: ConfidencePolicy,
    ) -> Optional[Decimal]:
        scores = [p.score for p in parents]
        if any(score is None for score in scores):
            return None
        concrete_scores = [score for score in scores if score is not None]
        if not concrete_scores:
            return None

        if policy.derived_aggregation == DerivedAggregationStrategy.MIN:
            return min(concrete_scores)
        if policy.derived_aggregation == DerivedAggregationStrategy.REQUIRED_ALL:
            if any(score < policy.medium_threshold for score in concrete_scores):
                return min(concrete_scores)
            return min(concrete_scores)
        # WEIGHTED: equal parent contribution unless an explicit parent weighting contract is added.
        # This is policy-selected rather than universal; Decimal preserves exactness.
        return (sum(concrete_scores, Decimal("0")) / Decimal(len(concrete_scores))).quantize(
            Decimal("0.0001"), rounding=ROUND_HALF_UP
        )

    @staticmethod
    def _assessment_id(
        *,
        subject_type: str,
        subject_id: str,
        field_path: Optional[str],
        source_id: Optional[str],
        provenance_id: Optional[str],
        evaluated_at: datetime,
        policy: ConfidencePolicy,
        factors: Sequence[ConfidenceFactor],
        level: Optional[ConfidenceLevel] = None,
        reason: Optional[str] = None,
    ) -> str:
        factors_str_list = []
        for f in factors:
            details_str = json.dumps(dict(f.details), sort_keys=True, ensure_ascii=False) if f.details else ""
            factors_str_list.append(f"{f.factor_name}:{f.factor_type}:{f.score}:{f.impact}:{details_str}")

        semantic = "|".join([
            subject_type,
            subject_id,
            field_path or "",
            source_id or "",
            provenance_id or "",
            evaluated_at.isoformat(),
            policy.policy_id,
            policy.version,
            level.value if level else "",
            reason or "",
            *factors_str_list,
        ])
        return f"confidence-{hashlib.sha256(semantic.encode('utf-8')).hexdigest()[:24]}"
