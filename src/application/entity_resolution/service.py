"""
Servicio de aplicación para Entity Resolution (Hito L.6 - Transversal Data Quality / Governance).

Implementa:
- EntityResolutionService:
  * normalize_reference: Normaliza y valida una EntityReference.
  * resolve_pair: Resuelve la identidad lógica entre dos EntityReference de forma determinista.
  * resolve_candidates: Compara una referencia contra múltiples candidatos preservando ambigüedad.
  * get_canonical_entity: Recupera o reconstruye la entidad canónica asociada a una referencia.
  * create_or_update_canonical_entity: Agrupa referencias coincidentes sin merge destructivo.
"""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import logging
from typing import Optional, Sequence, List, Dict, Tuple, Mapping, Any, Union
import uuid
import hashlib
import json

from src.domain.entity_resolution.models import (
    EntityType,
    IdentifierType,
    MatchStatus,
    ResolutionReasonCode,
    EntityIdentifier,
    EntityReference,
    EntityResolutionPolicy,
    EntityResolutionResult,
    ResolvedEntity,
    normalize_text,
    normalize_identifier_value,
    build_deterministic_canonical_entity_id,
    compute_entity_reference_checksum,
    compute_resolution_result_checksum,
    compute_resolved_entity_checksum,
)
from src.domain.entity_resolution.ports import (
    EntityResolutionPolicyRepositoryPort,
    EntityResolutionRepositoryPort,
)
from src.domain.security.models import (
    validate_safe_identifier,
    sanitize_security_data,
    deep_freeze,
)

logger = logging.getLogger(__name__)


def create_default_product_policy() -> EntityResolutionPolicy:
    """Crea la política canónica por defecto para resolución de productos."""
    return EntityResolutionPolicy(
        policy_id="default_product_resolution_policy_v1",
        name="Default Product Resolution Policy v1.0.0",
        version="1.0.0",
        entity_type=EntityType.PRODUCT,
        strong_identifier_types=(
            IdentifierType.GTIN,
            IdentifierType.EAN,
            IdentifierType.UPC,
            IdentifierType.ISBN,
            IdentifierType.MPN,
        ),
        required_attributes=("brand", "model"),
        optional_attributes=("title", "category", "color", "variant"),
        attribute_weights={
            "brand": Decimal("0.35"),
            "model": Decimal("0.35"),
            "title": Decimal("0.15"),
            "variant": Decimal("0.10"),
            "color": Decimal("0.05"),
        },
        match_threshold=Decimal("0.85"),
        possible_match_threshold=Decimal("0.50"),
        allow_cross_source_sku_match=False,
        require_exact_brand_match=True,
        allow_attribute_only_auto_match=False,
    )


class EntityResolutionService:
    """
    Servicio determinista de resolución de entidades.
    Garantiza reproducibilidad, trazabilidad y estricta separación de responsabilidades:
    - NO realiza Duplicate Detection en datasets completos (L.7).
    - NO realiza Conflict Resolution entre valores discrepantes (L.8).
    - Preserva referencias de procedencia (L.2) y registro de fuentes (L.1).
    - Rechaza MATCH si la validación de esquemas (L.5) falla.
    """

    def __init__(
        self,
        repository: Optional[EntityResolutionRepositoryPort] = None,
        policy_repository: Optional[EntityResolutionPolicyRepositoryPort] = None,
        audit_service: Optional[Any] = None,
        source_registry_service: Optional[Any] = None,
        schema_validation_service: Optional[Any] = None,
    ):
        self._repository = repository
        self._policy_repository = policy_repository
        self._audit_service = audit_service
        self._source_registry_service = source_registry_service
        self._schema_validation_service = schema_validation_service

    def resolve_policy(
        self,
        entity_type: Union[EntityType, str],
        policy_id: Optional[str] = None,
        version: Optional[str] = None,
    ) -> EntityResolutionPolicy:
        """Resuelve la política de resolución aplicable."""
        norm_type = EntityType(entity_type) if isinstance(entity_type, str) else entity_type

        if self._policy_repository:
            if policy_id:
                pol = self._policy_repository.get_policy(policy_id, version)
                if pol:
                    return pol
            latest = self._policy_repository.get_latest_policy_for_entity_type(norm_type)
            if latest:
                return latest

        # Fallback determinista
        if norm_type == EntityType.PRODUCT:
            return create_default_product_policy()

        return EntityResolutionPolicy(
            policy_id=f"default_{norm_type.value.lower()}_policy_v1",
            name=f"Default {norm_type.value} Policy",
            version="1.0.0",
            entity_type=norm_type,
            match_threshold=Decimal("0.85"),
            possible_match_threshold=Decimal("0.50"),
        )

    def resolve_pair(
        self,
        reference_a: EntityReference,
        reference_b: EntityReference,
        policy: Optional[EntityResolutionPolicy] = None,
        correlation_id: str = "default-correlation",
        persist: bool = False,
    ) -> EntityResolutionResult:
        """
        Determina si dos EntityReference corresponden a la misma entidad lógica.

        Flujo de decisión determinista:
        1. Validar compatibilidad de tipo de entidad y estado de schema (L.5).
        2. Comparar strong identifiers (GTIN, EAN, UPC, ISBN, MPN).
           - Coincidencia exacta sin contradicciones -> MATCH.
           - Contradicción en strong identifiers -> NO_MATCH (nunca overrideado por similitud textual).
        3. Evaluar scoped identifiers (SKU).
           - Mismo SKU con namespaces distintos -> no MATCH (SCOPED_IDENTIFIER_CROSS_NAMESPACE).
        4. Evaluar atributos canónicos con pesos Decimal.
           - Brand mismatch -> NO_MATCH / POSSIBLE_MATCH según política.
           - Score >= threshold -> MATCH (si policy lo permite) o POSSIBLE_MATCH.
           - possible_threshold <= score < match_threshold -> POSSIBLE_MATCH.
           - Evidencia insuficiente -> UNKNOWN.
        """
        # 1. Validación de compatibilidad de tipos
        if reference_a.entity_type != reference_b.entity_type:
            res_id = self._generate_resolution_id(reference_a, reference_b)
            return EntityResolutionResult(
                resolution_id=res_id,
                entity_type=reference_a.entity_type,
                status=MatchStatus.NO_MATCH,
                reference_a=reference_a,
                reference_b=reference_b,
                reason_codes=(ResolutionReasonCode.UNKNOWN_ENTITY_TYPE.value,),
                correlation_id=correlation_id,
            )

        entity_type = reference_a.entity_type
        eff_policy = policy or self.resolve_policy(entity_type)

        # 2. Validación de Esquema (Integración L.5)
        if reference_a.schema_validation_status in ("FAIL", "ERROR") or reference_b.schema_validation_status in ("FAIL", "ERROR"):
            res_id = self._generate_resolution_id(reference_a, reference_b)
            return EntityResolutionResult(
                resolution_id=res_id,
                entity_type=entity_type,
                status=MatchStatus.ERROR,
                reference_a=reference_a,
                reference_b=reference_b,
                reason_codes=(ResolutionReasonCode.SCHEMA_VALIDATION_FAILED.value,),
                policy_id=eff_policy.policy_id,
                policy_version=eff_policy.version,
                correlation_id=correlation_id,
            )

        # 3. Comparación de Strong Identifiers
        strong_types = set(eff_policy.strong_identifier_types)
        strong_a = [i for i in reference_a.identifiers if i.identifier_type in strong_types or i.is_strong]
        strong_b = [i for i in reference_b.identifiers if i.identifier_type in strong_types or i.is_strong]

        matched_idents: List[str] = []
        mismatched_idents: List[str] = []
        reasons: List[str] = []

        has_strong_contradiction = False
        has_strong_match = False

        # Mapa de identificadores de B por tipo
        b_strong_by_type: Dict[IdentifierType, List[EntityIdentifier]] = {}
        for ib in strong_b:
            b_strong_by_type.setdefault(ib.identifier_type, []).append(ib)

        def _strong_value_matches(ia: EntityIdentifier, ib: EntityIdentifier) -> bool:
            """Compara dos strong identifiers respetando el scope/namespace de tipos scoped."""
            if ia.identifier_type in (IdentifierType.SKU, IdentifierType.SUPPLIER_PRODUCT_ID):
                # SKU: el valor por sí solo no basta; debe coincidir namespace salvo
                # que la policy permita explícitamente cross-source SKU match.
                if ia.value != ib.value:
                    return False
                ns_a = ia.namespace or reference_a.source_id
                ns_b = ib.namespace or reference_b.source_id
                return ns_a == ns_b or eff_policy.allow_cross_source_sku_match
            return ia.value == ib.value

        for ia in strong_a:
            matching_type_b = b_strong_by_type.get(ia.identifier_type, [])
            if matching_type_b:
                # Comprobar si coincide con alguno o contradice a todos
                matched_with_any = False
                for ib in matching_type_b:
                    if _strong_value_matches(ia, ib):
                        matched_idents.append(f"{ia.identifier_type.value}:{ia.value}")
                        matched_with_any = True
                        has_strong_match = True
                        break
                if not matched_with_any:
                    # Distinguir contradicción real de strong ID vs. scope diferente.
                    # Para tipos scoped (SKU) con namespaces distintos no es una contradicción
                    # fuerte: es un cruce de namespace (resuelto como no-MATCH, nunca como
                    # override fuerte).
                    scoped_mismatch = False
                    all_different_scope = True
                    for ib in matching_type_b:
                        if ib.identifier_type in (IdentifierType.SKU, IdentifierType.SUPPLIER_PRODUCT_ID):
                            ns_a_ = ia.namespace or reference_a.source_id
                            ns_b_ = ib.namespace or reference_b.source_id
                            if ns_a_ == ns_b_ or eff_policy.allow_cross_source_sku_match:
                                all_different_scope = False
                        else:
                            all_different_scope = False
                    if all_different_scope:
                        scoped_mismatch = True
                    else:
                        for ib in matching_type_b:
                            mismatched_idents.append(f"{ia.identifier_type.value}:{ia.value}!={ib.value}")
                        has_strong_contradiction = True
                    if scoped_mismatch:
                        reasons.append(ResolutionReasonCode.SCOPED_IDENTIFIER_CROSS_NAMESPACE.value)

        # Regla Absoluta: Strong ID Contradictorio -> NO_MATCH
        if has_strong_contradiction:
            res_id = self._generate_resolution_id(reference_a, reference_b)
            result = EntityResolutionResult(
                resolution_id=res_id,
                entity_type=entity_type,
                status=MatchStatus.NO_MATCH,
                reference_a=reference_a,
                reference_b=reference_b,
                matched_identifiers=tuple(matched_idents),
                mismatched_identifiers=tuple(mismatched_idents),
                confidence_score=Decimal("0.0"),
                reason_codes=(ResolutionReasonCode.CONTRADICTORY_STRONG_IDENTIFIERS.value,),
                policy_id=eff_policy.policy_id,
                policy_version=eff_policy.version,
                correlation_id=correlation_id,
            )
            if persist and self._repository:
                self._repository.save_resolution(result)
            return result

        # Regla: Coincidencia en Strong ID sin contradicciones -> MATCH
        if has_strong_match:
            res_id = self._generate_resolution_id(reference_a, reference_b)
            canonical_id = build_deterministic_canonical_entity_id(
                entity_type=entity_type,
                identifiers=reference_a.identifiers + reference_b.identifiers,
                attributes=reference_a.canonical_attributes,
            )
            result = EntityResolutionResult(
                resolution_id=res_id,
                entity_type=entity_type,
                status=MatchStatus.MATCH,
                reference_a=reference_a,
                reference_b=reference_b,
                canonical_entity_id=canonical_id,
                matched_identifiers=tuple(matched_idents),
                mismatched_identifiers=tuple(mismatched_idents),
                confidence_score=Decimal("1.0"),
                reason_codes=(ResolutionReasonCode.EXACT_STRONG_IDENTIFIER_MATCH.value,),
                policy_id=eff_policy.policy_id,
                policy_version=eff_policy.version,
                correlation_id=correlation_id,
            )
            if persist and self._repository:
                self._repository.save_resolution(result)
                self.create_or_update_canonical_entity(result)
            return result

        # 4. Evaluación de Scoped Identifiers (SKU)
        skus_a = [i for i in reference_a.identifiers if i.identifier_type == IdentifierType.SKU]
        skus_b = [i for i in reference_b.identifiers if i.identifier_type == IdentifierType.SKU]

        if skus_a and skus_b:
            for sa in skus_a:
                for sb in skus_b:
                    if sa.value == sb.value:
                        # Mismo valor de SKU
                        ns_a = sa.namespace or reference_a.source_id
                        ns_b = sb.namespace or reference_b.source_id
                        if ns_a == ns_b or eff_policy.allow_cross_source_sku_match:
                            matched_idents.append(f"SKU:{sa.value}")
                            reasons.append(ResolutionReasonCode.SCOPED_IDENTIFIER_MATCH.value)
                        else:
                            reasons.append(ResolutionReasonCode.SCOPED_IDENTIFIER_CROSS_NAMESPACE.value)

        # 5. Evaluación de Atributos Canónicos
        attrs_a = reference_a.canonical_attributes
        attrs_b = reference_b.canonical_attributes

        matched_attrs: List[str] = []
        mismatched_attrs: List[str] = []
        missing_attrs: List[str] = []

        total_weight = Decimal("0.0")
        earned_score = Decimal("0.0")

        # Verificar brand match si es requerido
        brand_a = attrs_a.get("brand")
        brand_b = attrs_b.get("brand")
        if eff_policy.require_exact_brand_match and brand_a and brand_b:
            if brand_a != brand_b:
                mismatched_attrs.append(f"brand:{brand_a}!={brand_b}")
                reasons.append(ResolutionReasonCode.ATTRIBUTE_MISMATCH.value)
                res_id = self._generate_resolution_id(reference_a, reference_b)
                result = EntityResolutionResult(
                    resolution_id=res_id,
                    entity_type=entity_type,
                    status=MatchStatus.NO_MATCH,
                    reference_a=reference_a,
                    reference_b=reference_b,
                    matched_identifiers=tuple(matched_idents),
                    mismatched_identifiers=tuple(mismatched_idents),
                    matched_attributes=tuple(matched_attrs),
                    mismatched_attributes=tuple(mismatched_attrs),
                    confidence_score=Decimal("0.0"),
                    reason_codes=tuple(reasons),
                    policy_id=eff_policy.policy_id,
                    policy_version=eff_policy.version,
                    correlation_id=correlation_id,
                )
                if persist and self._repository:
                    self._repository.save_resolution(result)
                return result

        # Evaluar todos los atributos con pesos definidos en la policy
        for attr_name, weight in eff_policy.attribute_weights.items():
            val_a = attrs_a.get(attr_name)
            val_b = attrs_b.get(attr_name)
            total_weight += weight

            if val_a is None or val_b is None:
                missing_attrs.append(attr_name)
            elif val_a == val_b:
                matched_attrs.append(attr_name)
                earned_score += weight
            else:
                mismatched_attrs.append(f"{attr_name}:{val_a}!={val_b}")

        # Calcular score final en Decimal
        if total_weight > Decimal("0.0"):
            score = (earned_score / total_weight).quantize(Decimal("0.0001"))
        else:
            score = Decimal("0.0")

        # 6. Determinación del MatchStatus
        status: MatchStatus
        canonical_id: Optional[str] = None

        if score >= eff_policy.match_threshold:
            if eff_policy.allow_attribute_only_auto_match:
                status = MatchStatus.MATCH
                reasons.append(ResolutionReasonCode.ATTRIBUTE_HIGH_CONFIDENCE_MATCH.value)
                canonical_id = build_deterministic_canonical_entity_id(
                    entity_type=entity_type,
                    identifiers=reference_a.identifiers + reference_b.identifiers,
                    attributes=attrs_a,
                )
            else:
                status = MatchStatus.POSSIBLE_MATCH
                reasons.append(ResolutionReasonCode.ATTRIBUTE_HIGH_CONFIDENCE_MATCH.value)
        elif score >= eff_policy.possible_match_threshold:
            status = MatchStatus.POSSIBLE_MATCH
            reasons.append(ResolutionReasonCode.ATTRIBUTE_PARTIAL_MATCH.value)
        elif matched_idents and ResolutionReasonCode.SCOPED_IDENTIFIER_CROSS_NAMESPACE.value in reasons:
            status = MatchStatus.UNKNOWN
            reasons.append(ResolutionReasonCode.INSUFFICIENT_EVIDENCE.value)
        elif mismatched_attrs and not matched_attrs:
            status = MatchStatus.NO_MATCH
            reasons.append(ResolutionReasonCode.ATTRIBUTE_MISMATCH.value)
        else:
            status = MatchStatus.UNKNOWN
            reasons.append(ResolutionReasonCode.INSUFFICIENT_EVIDENCE.value)

        res_id = self._generate_resolution_id(reference_a, reference_b)
        result = EntityResolutionResult(
            resolution_id=res_id,
            entity_type=entity_type,
            status=status,
            reference_a=reference_a,
            reference_b=reference_b,
            canonical_entity_id=canonical_id,
            matched_identifiers=tuple(matched_idents),
            mismatched_identifiers=tuple(mismatched_idents),
            matched_attributes=tuple(matched_attrs),
            mismatched_attributes=tuple(mismatched_attrs),
            missing_attributes=tuple(missing_attrs),
            confidence_score=score,
            reason_codes=tuple(reasons) if reasons else (ResolutionReasonCode.INSUFFICIENT_EVIDENCE.value,),
            policy_id=eff_policy.policy_id,
            policy_version=eff_policy.version,
            correlation_id=correlation_id,
        )

        if persist and self._repository:
            self._repository.save_resolution(result)
            if result.status == MatchStatus.MATCH and canonical_id:
                self.create_or_update_canonical_entity(result)

        return result

    def resolve_candidates(
        self,
        reference: EntityReference,
        candidates: Sequence[EntityReference],
        policy: Optional[EntityResolutionPolicy] = None,
        correlation_id: str = "default-correlation",
    ) -> Sequence[EntityResolutionResult]:
        """
        Evalúa una referencia contra un conjunto de candidatos.
        Si múltiples candidatos resultan en MATCH o POSSIBLE_MATCH, marca ambigüedad explícita.
        """
        results: List[EntityResolutionResult] = []
        match_count = 0
        possible_count = 0

        for cand in candidates:
            res = self.resolve_pair(
                reference_a=reference,
                reference_b=cand,
                policy=policy,
                correlation_id=correlation_id,
                persist=False,
            )
            if res.status == MatchStatus.MATCH:
                match_count += 1
            elif res.status == MatchStatus.POSSIBLE_MATCH:
                possible_count += 1
            results.append(res)

        # Si hay ambigüedad (más de un MATCH o múltiples posibles sin MATCH claro)
        if match_count > 1 or (match_count == 0 and possible_count > 1):
            ambiguous_results: List[EntityResolutionResult] = []
            for r in results:
                reasons = list(r.reason_codes) + [ResolutionReasonCode.AMBIGUOUS_CANDIDATES.value]
                # Degradamos MATCH ambiguo a POSSIBLE_MATCH para prevenir auto-merge erróneo
                new_status = MatchStatus.POSSIBLE_MATCH if r.status == MatchStatus.MATCH else r.status
                ambiguous_results.append(
                    EntityResolutionResult(
                        resolution_id=r.resolution_id,
                        entity_type=r.entity_type,
                        status=new_status,
                        reference_a=r.reference_a,
                        reference_b=r.reference_b,
                        canonical_entity_id=None if new_status != MatchStatus.MATCH else r.canonical_entity_id,
                        matched_identifiers=r.matched_identifiers,
                        mismatched_identifiers=r.mismatched_identifiers,
                        matched_attributes=r.matched_attributes,
                        mismatched_attributes=r.mismatched_attributes,
                        missing_attributes=r.missing_attributes,
                        confidence_score=r.confidence_score,
                        reason_codes=tuple(reasons),
                        policy_id=r.policy_id,
                        policy_version=r.policy_version,
                        correlation_id=r.correlation_id,
                    )
                )
            return ambiguous_results

        return results

    def create_or_update_canonical_entity(
        self,
        resolution_result: EntityResolutionResult,
    ) -> Optional[ResolvedEntity]:
        """
        Agrupa las referencias resueltas como MATCH en una entidad canónica.
        Conserva las referencias fuente intactas sin merge destructivo.
        """
        if resolution_result.status != MatchStatus.MATCH or not resolution_result.canonical_entity_id:
            return None

        if not self._repository:
            return None

        canonical_id = resolution_result.canonical_entity_id
        existing = self._repository.get_canonical_entity(canonical_id)

        now = datetime.now(timezone.utc)
        ref_a = resolution_result.reference_a
        ref_b = resolution_result.reference_b

        if existing:
            # Unir referencias e identificadores
            existing_refs = list(existing.member_references)
            ref_checksums = {r.checksum for r in existing_refs}

            if ref_a.checksum not in ref_checksums:
                existing_refs.append(ref_a)
                ref_checksums.add(ref_a.checksum)
            if ref_b.checksum not in ref_checksums:
                existing_refs.append(ref_b)
                ref_checksums.add(ref_b.checksum)

            # Unir identificadores
            existing_idents = list(existing.primary_identifiers)
            ident_keys = {(i.identifier_type, i.value, i.namespace) for i in existing_idents}
            for i in ref_a.identifiers + ref_b.identifiers:
                k = (i.identifier_type, i.value, i.namespace)
                if k not in ident_keys:
                    existing_idents.append(i)
                    ident_keys.add(k)

            res_ids = list(set(existing.resolution_ids + (resolution_result.resolution_id,)))

            updated = ResolvedEntity(
                canonical_entity_id=canonical_id,
                entity_type=existing.entity_type,
                primary_identifiers=tuple(existing_idents),
                member_references=tuple(existing_refs),
                resolution_ids=tuple(sorted(res_ids)),
                canonical_attributes=existing.canonical_attributes,
                created_at=existing.created_at,
                updated_at=now,
                schema_version=existing.schema_version,
            )
            return self._repository.save_canonical_entity(updated)
        else:
            # Crear nueva entidad canónica
            all_idents: List[EntityIdentifier] = []
            ident_keys_set = set()
            for i in ref_a.identifiers + ref_b.identifiers:
                k = (i.identifier_type, i.value, i.namespace)
                if k not in ident_keys_set:
                    all_idents.append(i)
                    ident_keys_set.add(k)

            # Atributos canónicos basados en la referencia con más atributos
            canonical_attrs = dict(ref_a.canonical_attributes)
            for k, v in ref_b.canonical_attributes.items():
                if k not in canonical_attrs:
                    canonical_attrs[k] = v

            new_entity = ResolvedEntity(
                canonical_entity_id=canonical_id,
                entity_type=resolution_result.entity_type,
                primary_identifiers=tuple(all_idents),
                member_references=(ref_a, ref_b),
                resolution_ids=(resolution_result.resolution_id,),
                canonical_attributes=canonical_attrs,
                created_at=now,
                updated_at=now,
            )
            return self._repository.save_canonical_entity(new_entity)

    def _generate_resolution_id(self, ref_a: EntityReference, ref_b: EntityReference) -> str:
        """Genera un identificador de resolución determinista a partir de los checksums de las referencias."""
        pair_keys = sorted([
            f"{ref_a.source_id}:{ref_a.source_entity_id}",
            f"{ref_b.source_id}:{ref_b.source_entity_id}",
        ])
        raw_seed = "|".join(pair_keys)
        h = hashlib.sha256(raw_seed.encode("utf-8")).hexdigest()[:16]
        return f"res_{ref_a.entity_type.value.lower()}_{h}"
