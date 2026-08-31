import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Tuple, Dict, Any, Optional, Sequence, Mapping, Set
from types import MappingProxyType

from src.domain.market_intelligence.models import Confidence
from src.domain.publication.models import ListingDraft, SalesChannelType
from src.domain.publication.generation_models import (
    ListingFactGrounding,
    ChannelContentConstraint,
    ClaimProvenance,
    ClaimProvenanceType,
    SEOStrategy,
    DifferentiationStrategy,
)
from src.domain.publication.validation_models import (
    ValidationStatus,
    FindingSeverity,
    ValidationDimension,
    ValidationFinding,
    QualityScoreBreakdown,
    ListingValidationContext,
    ListingValidationResult,
)


class DeterministicListingValidator:
    """
    Motor determinista de dominio para validación exhaustiva de calidad, políticas, factualidad
    y restricciones de canal de ListingDraft (G.2 / TASK 07.2).

    Garantiza:
    1. Zero Alucinación: Cada afirmación relevante debe tener procedencia comprobable contra Product Truth o Market Evidence.
    2. Zero Invented Specs: Los atributos del draft se contrastan estrictamente contra product_truth.
    3. Seguridad y Políticas: Detección y bloqueo determinista de términos prohibidos y claims médicos/engañosos.
    4. Restricciones de Canal: Respeta longitudes, formatos, categorías y atributos requeridos del canal.
    5. Scoring Explicable: Calidad estructurada e independiente de Opportunity, Risk y Confidence.
    6. UNKNOWN Seguro: La información crítica faltante o incierta nunca se presume válida y produce NEEDS_REVIEW/BLOCKED.
    """

    VERSION = "v1.0.0"

    # Términos absolutamente prohibidos a nivel comercial / legal / marketplace
    GLOBAL_FORBIDDEN_TERMS: Tuple[str, ...] = (
        "100% garantizado",
        "el mejor",
        "el numero 1",
        "el número 1",
        "incomparable",
        "milagroso",
        "gratis de por vida",
        "cura el cancer",
        "cura el cáncer",
        "cura covid",
        "medicamento milagroso",
        "replica exacta",
        "imitacion triple a",
        "imitación triple a",
        "falso original",
        "pirata",
    )

    # Claims médicos no autorizados / riesgosos (frases completas o regex para evitar falsos positivos con palabras como 'accuracy')
    UNAUTHORIZED_MEDICAL_PATTERNS: Tuple[str, ...] = (
        r"\bcura\b",
        r"\bcurar\b",
        r"\bcurativo\b",
        r"\bsana definitivamente\b",
        r"\belimina enfermedades\b",
        r"\btratamiento milagroso\b",
        r"\baprobado por la fda\b",
        r"\bcertificacion medica garantizada\b",
        r"\bcertificación médica garantizada\b",
    )

    def validate(self, context: ListingValidationContext) -> ListingValidationResult:
        """
        Ejecuta todas las dimensiones de validación de forma determinista y estructurada.
        """
        findings: List[ValidationFinding] = []
        unsupported_claims: List[str] = []
        missing_fields: List[str] = []

        draft = context.draft
        constraints = context.channel_constraints or ChannelContentConstraint()
        product_truth = dict(context.product_truth_attributes)

        # 1. Dimensión A & B: Required Fields & Field Types
        self._validate_required_and_types(draft, findings, missing_fields)

        # 2. Dimensión C: Title Constraints
        self._validate_title(draft, constraints, findings)

        # 3. Dimensión D: Description Constraints
        self._validate_description(draft, constraints, findings)

        # 4. Dimensión E & N: Attribute Completeness & Product Truth Grounding
        self._validate_attributes_and_product_truth(draft, product_truth, constraints, findings, unsupported_claims)

        # 5. Dimensión F: Category Compatibility
        self._validate_category(draft, findings, missing_fields)

        # 6. Dimensión G & H: Price & Inventory Validity
        self._validate_price_and_inventory(draft, findings)

        # 7. Dimensión I: Image / Media Constraints
        self._validate_images(draft, findings, missing_fields)

        # 8. Dimensión J & K: SEO Keyword Quality & Stuffing
        self._validate_seo_and_stuffing(draft, context.seo_strategy, findings)

        # 9. Dimensión L & M: Prohibited & Unsupported Claims
        self._validate_claims_and_policies(draft, context.grounding, constraints, findings, unsupported_claims)

        # 10. Dimensión O: Customer Pain Differentiation Correctness
        self._validate_customer_pain_differentiation(context.differentiation_strategy, product_truth, findings, unsupported_claims)

        # 11. Dimensión P: Duplicate / Near-Duplicate Content
        self._validate_duplicate_content(draft, context.existing_catalog_titles, findings)

        # 12. Dimensión Q: Channel Specific Constraints
        self._validate_channel_constraints(draft, constraints, findings)

        # 13. Dimensión R, S & T: Provenance, Confidence & Critical Unknowns
        self._validate_provenance_and_confidence(context, findings)

        # 14. Calcular Quality Score Estructurado
        quality_score = self._compute_quality_score(
            draft=draft,
            product_truth=product_truth,
            findings=findings,
            constraints=constraints,
            seo_strategy=context.seo_strategy,
            diff_strategy=context.differentiation_strategy,
        )

        # 15. Determinar Status Final y Segregar Violaciones / Warnings
        violations = tuple(f for f in findings if f.severity in (FindingSeverity.ERROR, FindingSeverity.BLOCKER))
        warnings = tuple(f for f in findings if f.severity == FindingSeverity.WARNING)

        has_blocker = any(f.severity == FindingSeverity.BLOCKER for f in findings)
        has_error = any(f.severity == FindingSeverity.ERROR for f in findings)
        has_warning = len(warnings) > 0

        if has_blocker:
            status = ValidationStatus.BLOCKED
            is_valid = False
        elif has_error:
            status = ValidationStatus.INVALID
            is_valid = False
        elif has_warning:
            status = ValidationStatus.NEEDS_REVIEW
            is_valid = False
        else:
            status = ValidationStatus.VALID
            is_valid = True

        # Confianza global resultante
        result_confidence = Confidence.HIGH
        if context.grounding and any(cp.provenance_type == ClaimProvenanceType.UNKNOWN for cp in context.grounding.claims_provenance):
            result_confidence = Confidence.LOW
        elif has_warning or status == ValidationStatus.NEEDS_REVIEW:
            result_confidence = Confidence.MEDIUM

        return ListingValidationResult(
            draft_id=draft.draft_id,
            channel_id=draft.channel.channel_id,
            status=status,
            is_valid=is_valid,
            quality_score=quality_score,
            findings=tuple(findings),
            violations=violations,
            warnings=warnings,
            unsupported_claims=tuple(dict.fromkeys(unsupported_claims)),
            missing_fields=tuple(dict.fromkeys(missing_fields)),
            confidence=result_confidence,
            validator_version=self.VERSION,
            validated_at=datetime.now(timezone.utc),
            metadata=MappingProxyType({
                "total_findings": len(findings),
                "blockers_count": sum(1 for f in findings if f.severity == FindingSeverity.BLOCKER),
                "errors_count": sum(1 for f in findings if f.severity == FindingSeverity.ERROR),
                "warnings_count": len(warnings),
            }),
        )

    def _validate_required_and_types(
        self,
        draft: ListingDraft,
        findings: List[ValidationFinding],
        missing_fields: List[ValidationFinding],
    ) -> None:
        if not draft.title or not draft.title.strip():
            findings.append(
                ValidationFinding(
                    dimension=ValidationDimension.REQUIRED_FIELDS,
                    severity=FindingSeverity.BLOCKER,
                    code="REQ_TITLE_MISSING",
                    message="Listing draft title is empty or missing",
                    field_name="title",
                )
            )
            missing_fields.append("title")

        if not draft.description or not draft.description.strip():
            findings.append(
                ValidationFinding(
                    dimension=ValidationDimension.REQUIRED_FIELDS,
                    severity=FindingSeverity.ERROR,
                    code="REQ_DESCRIPTION_MISSING",
                    message="Listing draft description is empty or missing",
                    field_name="description",
                )
            )
            missing_fields.append("description")

        if not draft.currency or not draft.currency.strip():
            findings.append(
                ValidationFinding(
                    dimension=ValidationDimension.REQUIRED_FIELDS,
                    severity=FindingSeverity.ERROR,
                    code="REQ_CURRENCY_MISSING",
                    message="Listing draft currency is missing",
                    field_name="currency",
                )
            )
            missing_fields.append("currency")

    def _validate_title(
        self,
        draft: ListingDraft,
        constraints: ChannelContentConstraint,
        findings: List[ValidationFinding],
    ) -> None:
        title = draft.title.strip() if draft.title else ""
        if len(title) > constraints.max_title_length:
            findings.append(
                ValidationFinding(
                    dimension=ValidationDimension.TITLE_CONSTRAINTS,
                    severity=FindingSeverity.ERROR,
                    code="TITLE_LENGTH_EXCEEDED",
                    message=f"Title length ({len(title)}) exceeds maximum channel limit ({constraints.max_title_length})",
                    field_name="title",
                    details={"current_length": len(title), "max_allowed": constraints.max_title_length},
                )
            )
        if len(title) < 5 and len(title) > 0:
            findings.append(
                ValidationFinding(
                    dimension=ValidationDimension.TITLE_CONSTRAINTS,
                    severity=FindingSeverity.WARNING,
                    code="TITLE_TOO_SHORT",
                    message="Title is excessively short (< 5 chars)",
                    field_name="title",
                )
            )
        # Check all caps in title
        if len(title) > 10 and title.isupper():
            findings.append(
                ValidationFinding(
                    dimension=ValidationDimension.TITLE_CONSTRAINTS,
                    severity=FindingSeverity.WARNING,
                    code="TITLE_ALL_CAPS",
                    message="Title should not be fully in UPPERCASE",
                    field_name="title",
                )
            )

    def _validate_description(
        self,
        draft: ListingDraft,
        constraints: ChannelContentConstraint,
        findings: List[ValidationFinding],
    ) -> None:
        desc = draft.description.strip() if draft.description else ""
        if len(desc) > constraints.max_description_length:
            findings.append(
                ValidationFinding(
                    dimension=ValidationDimension.DESCRIPTION_CONSTRAINTS,
                    severity=FindingSeverity.ERROR,
                    code="DESC_LENGTH_EXCEEDED",
                    message=f"Description length ({len(desc)}) exceeds maximum allowed ({constraints.max_description_length})",
                    field_name="description",
                )
            )
        if not constraints.allows_html and ("<" in desc and ">" in desc):
            # Check for html tags
            if re.search(r"<[a-zA-Z\/][^>]*>", desc):
                findings.append(
                    ValidationFinding(
                        dimension=ValidationDimension.DESCRIPTION_CONSTRAINTS,
                        severity=FindingSeverity.ERROR,
                        code="HTML_NOT_ALLOWED",
                        message="HTML tags are not permitted by this sales channel",
                        field_name="description",
                    )
                )

    def _validate_attributes_and_product_truth(
        self,
        draft: ListingDraft,
        product_truth: Dict[str, Any],
        constraints: ChannelContentConstraint,
        findings: List[ValidationFinding],
        unsupported_claims: List[str],
    ) -> None:
        # 1. Required attributes by channel
        for req_attr in constraints.required_attributes:
            if req_attr not in draft.attributes or draft.attributes[req_attr] is None:
                findings.append(
                    ValidationFinding(
                        dimension=ValidationDimension.ATTRIBUTE_COMPLETENESS,
                        severity=FindingSeverity.ERROR,
                        code=f"MISSING_REQUIRED_ATTRIBUTE_{req_attr.upper()}",
                        message=f"Channel requires mandatory attribute: {req_attr}",
                        field_name=f"attributes.{req_attr}",
                    )
                )

        # 2. Product truth factuality check: draft attributes vs product_truth
        for k, v in draft.attributes.items():
            if v is None:
                continue
            if product_truth and k in product_truth and product_truth[k] is not None:
                truth_val = str(product_truth[k]).strip().lower()
                draft_val = str(v).strip().lower()
                if truth_val != draft_val and truth_val not in draft_val and draft_val not in truth_val:
                    finding = ValidationFinding(
                        dimension=ValidationDimension.PRODUCT_TRUTH_GROUNDING,
                        severity=FindingSeverity.BLOCKER,
                        code="PRODUCT_TRUTH_MISMATCH",
                        message=f"Attribute '{k}' ({v}) contradicts verified product truth ({product_truth[k]})",
                        field_name=f"attributes.{k}",
                        details={"draft_value": v, "truth_value": product_truth[k]},
                    )
                    findings.append(finding)
                    unsupported_claims.append(f"Attribute mismatch on {k}: {v} vs {product_truth[k]}")
            elif not product_truth or k not in product_truth:
                # Atributo afirmado en draft pero ausente en Product Truth
                # Si es un atributo crítico (brand, model, material, certification, warranty), es BLOCKER/ERROR
                critical_keys = {"brand", "model", "marca", "modelo", "material", "certification", "certificacion", "warranty", "garantia", "voltage", "voltaje"}
                if k.lower() in critical_keys:
                    findings.append(
                        ValidationFinding(
                            dimension=ValidationDimension.PRODUCT_TRUTH_GROUNDING,
                            severity=FindingSeverity.BLOCKER,
                            code="UNGROUNDED_CRITICAL_ATTRIBUTE",
                            message=f"Critical attribute '{k}'={v} has no grounding in Product Truth",
                            field_name=f"attributes.{k}",
                        )
                    )
                    unsupported_claims.append(f"Ungrounded critical attribute: {k}={v}")
                else:
                    findings.append(
                        ValidationFinding(
                            dimension=ValidationDimension.PRODUCT_TRUTH_GROUNDING,
                            severity=FindingSeverity.WARNING,
                            code="UNVERIFIED_ATTRIBUTE_CLAIM",
                            message=f"Attribute '{k}' is present in draft but unverified in Product Truth",
                            field_name=f"attributes.{k}",
                        )
                    )

    def _validate_category(
        self,
        draft: ListingDraft,
        findings: List[ValidationFinding],
        missing_fields: List[str],
    ) -> None:
        if not draft.category_id or not draft.category_id.strip():
            findings.append(
                ValidationFinding(
                    dimension=ValidationDimension.CATEGORY_COMPATIBILITY,
                    severity=FindingSeverity.ERROR,
                    code="CATEGORY_MISSING",
                    message="Category ID is mandatory for marketplace listing",
                    field_name="category_id",
                )
            )
            missing_fields.append("category_id")

    def _validate_price_and_inventory(
        self,
        draft: ListingDraft,
        findings: List[ValidationFinding],
    ) -> None:
        if draft.price is None or draft.price <= Decimal("0"):
            findings.append(
                ValidationFinding(
                    dimension=ValidationDimension.PRICE_VALIDITY,
                    severity=FindingSeverity.BLOCKER,
                    code="INVALID_PRICE_NON_POSITIVE",
                    message=f"Listing price ({draft.price}) must be strictly positive",
                    field_name="price",
                )
            )
        if draft.available_quantity is None or draft.available_quantity < 0:
            findings.append(
                ValidationFinding(
                    dimension=ValidationDimension.INVENTORY_VALIDITY,
                    severity=FindingSeverity.BLOCKER,
                    code="INVALID_INVENTORY_NEGATIVE",
                    message=f"Available quantity ({draft.available_quantity}) cannot be negative",
                    field_name="available_quantity",
                )
            )
        elif draft.available_quantity == 0:
            findings.append(
                ValidationFinding(
                    dimension=ValidationDimension.INVENTORY_VALIDITY,
                    severity=FindingSeverity.WARNING,
                    code="INVENTORY_ZERO_OUT_OF_STOCK",
                    message="Available quantity is 0; listing will be published as out-of-stock or paused",
                    field_name="available_quantity",
                )
            )

    def _validate_images(
        self,
        draft: ListingDraft,
        findings: List[ValidationFinding],
        missing_fields: List[str],
    ) -> None:
        if not draft.images or len(draft.images) == 0:
            findings.append(
                ValidationFinding(
                    dimension=ValidationDimension.IMAGE_CONSTRAINTS,
                    severity=FindingSeverity.ERROR,
                    code="IMAGES_MISSING",
                    message="At least one product image is required for publication",
                    field_name="images",
                )
            )
            missing_fields.append("images")
        else:
            # Check for invalid image URIs
            for idx, img in enumerate(draft.images):
                if not img or not img.strip() or not (img.startswith("http://") or img.startswith("https://")):
                    findings.append(
                        ValidationFinding(
                            dimension=ValidationDimension.IMAGE_CONSTRAINTS,
                            severity=FindingSeverity.ERROR,
                            code="INVALID_IMAGE_URI",
                            message=f"Image at index {idx} has invalid URI format: {img}",
                            field_name=f"images[{idx}]",
                        )
                    )

    def _validate_seo_and_stuffing(
        self,
        draft: ListingDraft,
        seo_strategy: Optional[SEOStrategy],
        findings: List[ValidationFinding],
    ) -> None:
        full_text = f"{draft.title} {draft.description}".lower()
        words = re.findall(r"\b\w+\b", full_text)
        total_words = len(words)

        if total_words > 0:
            word_counts: Dict[str, int] = {}
            for w in words:
                if len(w) > 3:  # Ignorar preposiciones cortas
                    word_counts[w] = word_counts.get(w, 0) + 1

            for w, cnt in word_counts.items():
                density = cnt / total_words
                # Si una palabra clave excede el 8% de densidad y aparece más de 5 veces -> stuffing
                if cnt >= 5 and density > 0.08:
                    findings.append(
                        ValidationFinding(
                            dimension=ValidationDimension.KEYWORD_STUFFING,
                            severity=FindingSeverity.ERROR,
                            code="KEYWORD_STUFFING_DETECTED",
                            message=f"Excessive repetition of keyword '{w}' (density {density:.1%})",
                            details={"keyword": w, "count": cnt, "density": round(density, 3)},
                        )
                    )

    def _validate_claims_and_policies(
        self,
        draft: ListingDraft,
        grounding: Optional[ListingFactGrounding],
        constraints: ChannelContentConstraint,
        findings: List[ValidationFinding],
        unsupported_claims: List[str],
    ) -> None:
        full_text = f"{draft.title}\n{draft.description}".lower()

        # 1. Prohibited terms from global policy & channel constraints
        all_forbidden = set(self.GLOBAL_FORBIDDEN_TERMS).union(set(constraints.forbidden_terms))
        for forbidden in all_forbidden:
            if forbidden.lower() in full_text:
                findings.append(
                    ValidationFinding(
                        dimension=ValidationDimension.PROHIBITED_CLAIMS,
                        severity=FindingSeverity.BLOCKER,
                        code="PROHIBITED_CLAIM_TERM",
                        message=f"Content contains prohibited commercial term: '{forbidden}'",
                        details={"forbidden_term": forbidden},
                    )
                )
                unsupported_claims.append(f"Prohibited term: {forbidden}")

        # 2. Medical / Health unverified claims
        for med_pat in self.UNAUTHORIZED_MEDICAL_PATTERNS:
            if re.search(med_pat, full_text):
                findings.append(
                    ValidationFinding(
                        dimension=ValidationDimension.PROHIBITED_CLAIMS,
                        severity=FindingSeverity.BLOCKER,
                        code="UNAUTHORIZED_MEDICAL_CLAIM",
                        message=f"Listing contains unauthorized medical/health claim pattern: '{med_pat}'",
                        details={"medical_claim_pattern": med_pat},
                    )
                )
                unsupported_claims.append(f"Medical claim: {med_pat}")

        # 3. Grounding checks: claims with UNKNOWN provenance
        if grounding:
            for cp in grounding.claims_provenance:
                if cp.provenance_type == ClaimProvenanceType.UNKNOWN:
                    findings.append(
                        ValidationFinding(
                            dimension=ValidationDimension.UNSUPPORTED_CLAIMS,
                            severity=FindingSeverity.BLOCKER,
                            code="CLAIM_PROVENANCE_UNKNOWN",
                            message=f"Material claim has UNKNOWN provenance: '{cp.claim_text}'",
                            details={"claim_text": cp.claim_text},
                        )
                    )
                    unsupported_claims.append(cp.claim_text)

    def _validate_customer_pain_differentiation(
        self,
        diff_strategy: Optional[DifferentiationStrategy],
        product_truth: Dict[str, Any],
        findings: List[ValidationFinding],
        unsupported_claims: List[str],
    ) -> None:
        if diff_strategy:
            for claim in diff_strategy.differential_claims:
                # Verificar que el mapping en product_truth_mapping exista y coincida con product_truth
                if diff_strategy.product_truth_mapping:
                    for pain_k, attr_ref in diff_strategy.product_truth_mapping.items():
                        if attr_ref.startswith("attr:"):
                            key_val = attr_ref[5:]
                            if "=" in key_val:
                                k, v = key_val.split("=", 1)
                                if product_truth and k in product_truth:
                                    truth_val = str(product_truth[k]).strip().lower()
                                    if truth_val != v.strip().lower() and truth_val not in v.lower():
                                        findings.append(
                                            ValidationFinding(
                                                dimension=ValidationDimension.CUSTOMER_PAIN_DIFFERENTIATION,
                                                severity=FindingSeverity.BLOCKER,
                                                code="DIFFERENTIATION_CLAIM_UNBACKED",
                                                message=f"Differential claim '{claim}' references attribute '{k}' ({v}) which contradicts Product Truth ({product_truth[k]})",
                                            )
                                        )
                                        unsupported_claims.append(claim)

    def _validate_duplicate_content(
        self,
        draft: ListingDraft,
        existing_catalog_titles: Tuple[str, ...],
        findings: List[ValidationFinding],
    ) -> None:
        if not draft.title or not existing_catalog_titles:
            return

        clean_draft_title = self._normalize_text(draft.title)
        for existing in existing_catalog_titles:
            clean_existing = self._normalize_text(existing)
            if clean_draft_title == clean_existing:
                findings.append(
                    ValidationFinding(
                        dimension=ValidationDimension.DUPLICATE_CONTENT,
                        severity=FindingSeverity.ERROR,
                        code="EXACT_DUPLICATE_LISTING_TITLE",
                        message=f"Exact duplicate title found in existing catalog: '{existing}'",
                        details={"existing_title": existing},
                    )
                )
                break
            else:
                # Jaccard similarity between word sets
                words_d = set(clean_draft_title.split())
                words_e = set(clean_existing.split())
                if words_d and words_e:
                    jaccard = len(words_d.intersection(words_e)) / len(words_d.union(words_e))
                    if jaccard >= 0.90:
                        findings.append(
                            ValidationFinding(
                                dimension=ValidationDimension.DUPLICATE_CONTENT,
                                severity=FindingSeverity.WARNING,
                                code="NEAR_DUPLICATE_LISTING_TITLE",
                                message=f"Near-duplicate title detected ({jaccard:.0%} similarity) with: '{existing}'",
                                details={"existing_title": existing, "similarity": round(jaccard, 2)},
                            )
                        )
                        break

    def _validate_channel_constraints(
        self,
        draft: ListingDraft,
        constraints: ChannelContentConstraint,
        findings: List[ValidationFinding],
    ) -> None:
        # Bullets check
        bullets_in_meta = draft.metadata.get("bullet_points", [])
        if bullets_in_meta:
            if not constraints.allows_bullets:
                findings.append(
                    ValidationFinding(
                        dimension=ValidationDimension.CHANNEL_SPECIFIC_CONSTRAINTS,
                        severity=FindingSeverity.ERROR,
                        code="BULLETS_NOT_SUPPORTED",
                        message="Sales channel does not support bullet points in listing",
                    )
                )
            elif len(bullets_in_meta) > constraints.max_bullets:
                findings.append(
                    ValidationFinding(
                        dimension=ValidationDimension.CHANNEL_SPECIFIC_CONSTRAINTS,
                        severity=FindingSeverity.WARNING,
                        code="MAX_BULLETS_EXCEEDED",
                        message=f"Listing has {len(bullets_in_meta)} bullets, exceeding max limit of {constraints.max_bullets}",
                    )
                )
            for idx, b in enumerate(bullets_in_meta):
                if len(str(b)) > constraints.max_bullet_length:
                    findings.append(
                        ValidationFinding(
                            dimension=ValidationDimension.CHANNEL_SPECIFIC_CONSTRAINTS,
                            severity=FindingSeverity.WARNING,
                            code="BULLET_LENGTH_EXCEEDED",
                            message=f"Bullet {idx} length ({len(str(b))}) exceeds max limit ({constraints.max_bullet_length})",
                        )
                    )

    def _validate_provenance_and_confidence(
        self,
        context: ListingValidationContext,
        findings: List[ValidationFinding],
    ) -> None:
        if context.market_evidence and context.market_evidence.confidence:
            if context.market_evidence.confidence == Confidence.UNKNOWN:
                findings.append(
                    ValidationFinding(
                        dimension=ValidationDimension.CRITICAL_UNKNOWN,
                        severity=FindingSeverity.BLOCKER,
                        code="EVIDENCE_CONFIDENCE_UNKNOWN",
                        message="Market evidence confidence is UNKNOWN; critical data cannot be verified",
                    )
                )
            elif context.min_confidence == Confidence.HIGH and context.market_evidence.confidence != Confidence.HIGH:
                findings.append(
                    ValidationFinding(
                        dimension=ValidationDimension.CONFIDENCE_REQUIREMENTS,
                        severity=FindingSeverity.WARNING,
                        code="INSUFFICIENT_CONFIDENCE_LEVEL",
                        message=f"Evidence confidence ({context.market_evidence.confidence.value}) is below required minimum (HIGH)",
                    )
                )

    def _compute_quality_score(
        self,
        draft: ListingDraft,
        product_truth: Dict[str, Any],
        findings: List[ValidationFinding],
        constraints: ChannelContentConstraint,
        seo_strategy: Optional[SEOStrategy],
        diff_strategy: Optional[DifferentiationStrategy],
    ) -> QualityScoreBreakdown:
        """
        Calcula un Quality Score estructurado de 0 a 100 ponderando 7 pilares analíticos.
        """
        # 1. Completeness (0-100)
        c_score = 100.0
        if not draft.title:
            c_score -= 30.0
        if not draft.description:
            c_score -= 20.0
        if not draft.images:
            c_score -= 25.0
        if not draft.category_id:
            c_score -= 15.0
        if len(draft.attributes) < 3:
            c_score -= 10.0
        completeness_score = max(0.0, min(100.0, c_score))

        # 2. Factuality (0-100)
        f_score = 100.0
        for f in findings:
            if f.dimension in (ValidationDimension.PRODUCT_TRUTH_GROUNDING, ValidationDimension.UNSUPPORTED_CLAIMS):
                if f.severity == FindingSeverity.BLOCKER:
                    f_score -= 40.0
                elif f.severity == FindingSeverity.ERROR:
                    f_score -= 20.0
                elif f.severity == FindingSeverity.WARNING:
                    f_score -= 10.0
        factuality_score = max(0.0, min(100.0, f_score))

        # 3. SEO (0-100)
        s_score = 70.0
        if seo_strategy and seo_strategy.primary_keywords:
            s_score += 20.0
        if draft.title and len(draft.title) >= 20:
            s_score += 10.0
        if any(f.dimension == ValidationDimension.KEYWORD_STUFFING for f in findings):
            s_score -= 40.0
        seo_score = max(0.0, min(100.0, s_score))

        # 4. Readability (0-100)
        r_score = 85.0
        if draft.title and draft.title.isupper():
            r_score -= 20.0
        if draft.description and len(draft.description) < 50:
            r_score -= 25.0
        readability_score = max(0.0, min(100.0, r_score))

        # 5. Policy Compliance (0-100)
        p_score = 100.0
        for f in findings:
            if f.dimension == ValidationDimension.PROHIBITED_CLAIMS:
                if f.severity == FindingSeverity.BLOCKER:
                    p_score -= 50.0
                else:
                    p_score -= 20.0
        policy_compliance_score = max(0.0, min(100.0, p_score))

        # 6. Differentiation (0-100)
        d_score = 60.0
        if diff_strategy and diff_strategy.differential_claims:
            d_score += 30.0
        if diff_strategy and diff_strategy.unmet_needs_addressed:
            d_score += 10.0
        for f in findings:
            if f.dimension == ValidationDimension.CUSTOMER_PAIN_DIFFERENTIATION and f.severity == FindingSeverity.BLOCKER:
                d_score -= 50.0
        differentiation_score = max(0.0, min(100.0, d_score))

        # 7. Channel Compliance (0-100)
        ch_score = 100.0
        for f in findings:
            if f.dimension in (
                ValidationDimension.CHANNEL_SPECIFIC_CONSTRAINTS,
                ValidationDimension.TITLE_CONSTRAINTS,
                ValidationDimension.DESCRIPTION_CONSTRAINTS,
                ValidationDimension.IMAGE_CONSTRAINTS,
            ):
                if f.severity == FindingSeverity.BLOCKER:
                    ch_score -= 40.0
                elif f.severity == FindingSeverity.ERROR:
                    ch_score -= 20.0
                elif f.severity == FindingSeverity.WARNING:
                    ch_score -= 5.0
        channel_compliance_score = max(0.0, min(100.0, ch_score))

        # Weighted Overall Score
        overall = (
            completeness_score * 0.20
            + factuality_score * 0.25
            + policy_compliance_score * 0.20
            + channel_compliance_score * 0.15
            + seo_score * 0.10
            + readability_score * 0.05
            + differentiation_score * 0.05
        )

        return QualityScoreBreakdown(
            completeness_score=round(completeness_score, 1),
            factuality_score=round(factuality_score, 1),
            seo_score=round(seo_score, 1),
            readability_score=round(readability_score, 1),
            policy_compliance_score=round(policy_compliance_score, 1),
            differentiation_score=round(differentiation_score, 1),
            channel_compliance_score=round(channel_compliance_score, 1),
            overall_score=round(overall, 1),
        )

    def _normalize_text(self, text: str) -> str:
        t = text.lower()
        t = re.sub(r"[^\w\s]", "", t)
        return " ".join(t.split())
