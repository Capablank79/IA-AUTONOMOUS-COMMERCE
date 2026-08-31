import re
import unicodedata
from decimal import Decimal
from typing import Optional, Sequence, List, Dict, Tuple, Set, Any
from datetime import datetime, timezone

from src.domain.market_intelligence.models import Confidence, SignalType
from src.domain.supplier_intelligence.models import (
    Supplier,
    SupplierEvidence,
    SupplierCandidate,
    ProductMatch,
    ProductMatchGrade,
    SupplierScoreBreakdown,
    SupplierReadiness,
    SupplierRejectionReason,
    EvidenceProvenanceType,
    BestKnownSupplier,
    CommercialQuote,
    PriceTier,
    MOQInfo,
    MOQType,
    QuoteFreshness,
    QuoteComparabilityStatus,
    QuoteConflictStatus,
    QuoteConflict,
    QuoteScenarioEvaluation,
    SupplierQuoteComparisonItem,
    BestCommercialCandidate,
    QuoteComparisonResult,
    ShippingMethod,
    ShippingComparabilityStatus,
    SLAStatus,
    RiskLevel,
    PerformanceTrend,
    LeadTimeProfile,
    ShippingOption,
    SLARecord,
    SupplierObservationEvent,
    HistoricalPerformanceProfile,
    ReliabilityEvaluation,
    SupplierRiskDimension,
    SupplierRiskProfile,
    SupplierRiskComparisonItem,
    BestSupplierCandidate,
    SupplierRiskEvaluationResult,
    SupplierRecommendationDecision,
    ContingencyTrigger,
    RecommendationCondition,
    PrimarySupplierSelection,
    FallbackSupplierSelection,
    StructuredRecommendationExplanation,
    SupplierRecommendation,
)


class SupplierNormalizer:
    """
    Normalizador de entidades de proveedores y deduplicador determinista.
    Evita que dos resultados que representen al mismo proveedor/producto aparezcan
    como entidades independientes sin justificación, respetando la regla de no fusionar
    agresivamente si hay incertidumbre.
    """

    @staticmethod
    def normalize_string(val: Optional[str]) -> str:
        if not val:
            return ""
        # Normalizar caracteres unicode, quitar acentos, minúsculas, espacios extra
        normalized = unicodedata.normalize("NFKD", val)
        clean = "".join(c for c in normalized if not unicodedata.combining(c))
        clean = re.sub(r"[^\w\s\-\.]", " ", clean.lower())
        return re.sub(r"\s+", " ", clean).strip()

    @staticmethod
    def normalize_company_name(name: str) -> str:
        clean = SupplierNormalizer.normalize_string(name)
        # Quitar sufijos legales comunes en normalización de búsqueda
        suffixes = [
            r"\bspa\b", r"\bs\.p\.a\b", r"\bsa\b", r"\bs\.a\b",
            r"\bltda\b", r"\bltd\b", r"\beirl\b", r"\binc\b", r"\bcorp\b", r"\bllc\b"
        ]
        for s in suffixes:
            clean = re.sub(s, "", clean)
        return re.sub(r"\s+", " ", clean).strip()

    @classmethod
    def are_same_supplier(cls, sup_a: Supplier, sup_b: Supplier) -> Tuple[bool, float]:
        """
        Determina si dos entidades Supplier corresponden al mismo proveedor.
        Retorna (is_same, confidence_score).
        Si no puede determinarse con alta certeza, devuelve False.
        """
        if sup_a.supplier_id == sup_b.supplier_id:
            return True, 1.0

        # Normalizar nombres
        name_a = cls.normalize_company_name(sup_a.name)
        name_b = cls.normalize_company_name(sup_b.name)

        if name_a and name_b and name_a == name_b:
            # Si coinciden los nombres y al menos país o contacto
            country_a = (sup_a.location.country.upper() if sup_a.location else "").strip()
            country_b = (sup_b.location.country.upper() if sup_b.location else "").strip()
            if country_a and country_b and country_a == country_b:
                return True, 0.95
            return True, 0.85

        # Si comparten email de contacto verificado
        email_a = (sup_a.contact.email.lower() if sup_a.contact and sup_a.contact.email else "").strip()
        email_b = (sup_b.contact.email.lower() if sup_b.contact and sup_b.contact.email else "").strip()
        if email_a and email_b and email_a == email_b:
            return True, 0.99

        # Si comparten website normalizado
        web_a = cls.normalize_string(sup_a.contact.website if sup_a.contact else "")
        web_b = cls.normalize_string(sup_b.contact.website if sup_b.contact else "")
        if web_a and web_b and web_a == web_b:
            return True, 0.95

        return False, 0.0

    @classmethod
    def deduplicate_candidates(cls, candidates: Sequence[SupplierCandidate]) -> List[SupplierCandidate]:
        """
        Deduplica una lista de candidatos a proveedor.
        Si se detectan dos candidatos idénticos (mismo proveedor y mismo SKU),
        conserva el de mayor frescura / mayor confianza / cotización confirmada.
        """
        unique_candidates: List[SupplierCandidate] = []

        for cand in candidates:
            duplicate_found = False
            for i, existing in enumerate(unique_candidates):
                same_supplier, conf = cls.are_same_supplier(cand.supplier, existing.supplier)
                same_sku = (cand.evidence.sku == existing.evidence.sku)

                if same_supplier and same_sku:
                    duplicate_found = True
                    # Criterio de desempate:
                    # 1. Presencia de confirmed quote
                    # 2. Confianza de evidencia
                    # 3. Frescura (observed_at)
                    has_quote_cand = cand.evidence.quote is not None
                    has_quote_exist = existing.evidence.quote is not None

                    replace = False
                    if has_quote_cand and not has_quote_exist:
                        replace = True
                    elif has_quote_cand == has_quote_exist:
                        conf_order = {Confidence.HIGH: 3, Confidence.MEDIUM: 2, Confidence.LOW: 1, Confidence.UNKNOWN: 0}
                        if conf_order.get(cand.evidence.confidence, 0) > conf_order.get(existing.evidence.confidence, 0):
                            replace = True
                        elif cand.evidence.observed_at > existing.evidence.observed_at:
                            replace = True

                    if replace:
                        unique_candidates[i] = cand
                    break

            if not duplicate_found:
                unique_candidates.append(cand)

        return unique_candidates


class ProductMatcher:
    """
    Motor de comparación y matching entre una oportunidad de producto de mercado
    y una referencia/oferta de proveedor.
    Distingue:
    - EXACT_MATCH
    - CLOSE_MATCH
    - VARIANT
    - UNCERTAIN_MATCH
    - NO_MATCH
    """

    @classmethod
    def match(
        cls,
        target_title: str,
        target_brand: Optional[str] = None,
        target_model: Optional[str] = None,
        target_sku: Optional[str] = None,
        supplier_sku: Optional[str] = None,
        supplier_title: Optional[str] = None,
        supplier_brand: Optional[str] = None,
        supplier_model: Optional[str] = None,
    ) -> ProductMatch:
        norm_t_title = SupplierNormalizer.normalize_string(target_title)
        norm_s_title = SupplierNormalizer.normalize_string(supplier_title)
        norm_t_brand = SupplierNormalizer.normalize_string(target_brand)
        norm_s_brand = SupplierNormalizer.normalize_string(supplier_brand)
        norm_t_model = SupplierNormalizer.normalize_string(target_model)
        norm_s_model = SupplierNormalizer.normalize_string(supplier_model)
        norm_t_sku = SupplierNormalizer.normalize_string(target_sku)
        norm_s_sku = SupplierNormalizer.normalize_string(supplier_sku)

        matched_fields: List[str] = []
        discrepancies: List[str] = []

        # 1. Match directo de SKU
        if norm_t_sku and norm_s_sku:
            if norm_t_sku == norm_s_sku:
                matched_fields.append("sku")
                return ProductMatch(
                    grade=ProductMatchGrade.EXACT_MATCH,
                    confidence=Confidence.HIGH,
                    matched_fields=tuple(matched_fields),
                    discrepancies=(),
                    details=f"Exact SKU match: {norm_s_sku}",
                )
            else:
                discrepancies.append(f"sku_mismatch({norm_t_sku} vs {norm_s_sku})")

        # 2. Match de Marca
        brand_match = False
        if norm_t_brand and norm_s_brand:
            if norm_t_brand == norm_s_brand or norm_t_brand in norm_s_brand or norm_s_brand in norm_t_brand:
                brand_match = True
                matched_fields.append("brand")
            else:
                discrepancies.append(f"brand_mismatch({norm_t_brand} vs {norm_s_brand})")
        elif norm_t_brand and (norm_t_brand in norm_s_title or norm_t_brand in norm_s_sku):
            brand_match = True
            matched_fields.append("brand_in_title_or_sku")

        # 3. Match de Modelo
        model_match = False
        if norm_t_model and norm_s_model:
            if norm_t_model == norm_s_model or norm_t_model in norm_s_model or norm_s_model in norm_t_model:
                model_match = True
                matched_fields.append("model")
            else:
                discrepancies.append(f"model_mismatch({norm_t_model} vs {norm_s_model})")
        elif norm_t_model and (norm_t_model in norm_s_title or norm_t_model in norm_s_sku):
            model_match = True
            matched_fields.append("model_in_title_or_sku")

        # 4. Token overlap de Título
        t_tokens = set(re.findall(r"\w+", norm_t_title))
        s_tokens = set(re.findall(r"\w+", norm_s_title))
        overlap = t_tokens.intersection(s_tokens)
        overlap_ratio = len(overlap) / max(len(t_tokens), 1)

        if overlap_ratio >= 0.7:
            matched_fields.append("title_high_overlap")
        elif overlap_ratio >= 0.4:
            matched_fields.append("title_partial_overlap")

        # 5. Determinación de Grado
        if brand_match and model_match:
            if overlap_ratio >= 0.5 or (norm_s_sku and norm_s_sku in norm_t_title):
                return ProductMatch(
                    grade=ProductMatchGrade.EXACT_MATCH,
                    confidence=Confidence.HIGH,
                    matched_fields=tuple(matched_fields),
                    discrepancies=tuple(discrepancies),
                    details="Exact Brand + Model match verified.",
                )
            return ProductMatch(
                grade=ProductMatchGrade.CLOSE_MATCH,
                confidence=Confidence.MEDIUM,
                matched_fields=tuple(matched_fields),
                discrepancies=tuple(discrepancies),
                details="Brand and Model matched with slight variation in title keywords.",
            )

        if brand_match and overlap_ratio >= 0.5:
            return ProductMatch(
                grade=ProductMatchGrade.CLOSE_MATCH,
                confidence=Confidence.MEDIUM,
                matched_fields=tuple(matched_fields),
                discrepancies=tuple(discrepancies),
                details="Brand matched and significant title keyword overlap.",
            )

        # Detectar variantes (por ejemplo capacidad o color diferente pero misma serie)
        variant_keywords = ["gb", "tb", "color", "black", "white", "negro", "blanco", "v2", "pro", "plus"]
        has_variant_clue = any(vk in norm_t_title or vk in norm_s_title for vk in variant_keywords)
        if brand_match and has_variant_clue and overlap_ratio >= 0.3:
            return ProductMatch(
                grade=ProductMatchGrade.VARIANT,
                confidence=Confidence.MEDIUM,
                matched_fields=tuple(matched_fields),
                discrepancies=tuple(discrepancies),
                details="Product appears to be a variant of the target product.",
            )

        if overlap_ratio >= 0.4:
            return ProductMatch(
                grade=ProductMatchGrade.UNCERTAIN_MATCH,
                confidence=Confidence.LOW,
                matched_fields=tuple(matched_fields),
                discrepancies=tuple(discrepancies),
                details="Keyword overlap present but insufficient brand/model evidence.",
            )

        return ProductMatch(
            grade=ProductMatchGrade.NO_MATCH,
            confidence=Confidence.UNKNOWN,
            matched_fields=tuple(matched_fields),
            discrepancies=tuple(discrepancies),
            details="No significant match found.",
        )


class SupplierScorer:
    """
    Motor determinista de scoring y ranking preliminar de proveedores.
    No inventa valores faltantes (UNKNOWNs explícitos).
    Produce un score reproducible en rango [0.0, 100.0].
    """

    @classmethod
    def calculate_score(
        cls,
        candidate: SupplierCandidate,
        target_market_price: Optional[Decimal] = None
    ) -> SupplierScoreBreakdown:
        match = candidate.product_match
        evidence = candidate.evidence
        supplier = candidate.supplier
        explanation_lines: List[str] = []

        # 1. Match Score (peso 35%)
        # EXACT: 35, CLOSE: 25, VARIANT: 15, UNCERTAIN: 5, NO_MATCH: 0
        match_map = {
            ProductMatchGrade.EXACT_MATCH: Decimal("35.0"),
            ProductMatchGrade.CLOSE_MATCH: Decimal("25.0"),
            ProductMatchGrade.VARIANT: Decimal("15.0"),
            ProductMatchGrade.UNCERTAIN_MATCH: Decimal("5.0"),
            ProductMatchGrade.NO_MATCH: Decimal("0.0"),
        }
        match_score = match_map.get(match.grade, Decimal("0.0"))
        explanation_lines.append(f"Product Match ({match.grade.value}): {match_score}/35.0")

        # 2. Price / Commercial Score (peso 25%)
        # Basado en precio mayorista vs precio objetivo de mercado si existe
        price_score = Decimal("0.0")
        effective_price = evidence.quote.wholesale_price if evidence.quote else evidence.wholesale_price

        if effective_price is not None and effective_price > 0:
            if target_market_price is not None and target_market_price > 0:
                # Si el precio mayorista es < 60% del PVP: puntaje máximo
                # Si está entre 60% y 90%: proporcional
                # Si es > 90%: puntaje mínimo
                cost_ratio = effective_price / target_market_price
                if cost_ratio <= Decimal("0.60"):
                    price_score = Decimal("25.0")
                elif cost_ratio < Decimal("0.90"):
                    ratio_span = (Decimal("0.90") - cost_ratio) / Decimal("0.30")
                    price_score = (Decimal("10.0") + (Decimal("15.0") * ratio_span)).quantize(Decimal("0.1"))
                else:
                    price_score = Decimal("5.0")
                explanation_lines.append(f"Wholesale price ratio ({cost_ratio:.2%}): {price_score}/25.0")
            else:
                # Precio conocido sin target_market_price
                price_score = Decimal("18.0")
                explanation_lines.append(f"Known wholesale price: {price_score}/25.0")
        else:
            price_score = Decimal("0.0")
            explanation_lines.append("Wholesale price UNKNOWN: 0.0/25.0")

        # 3. Availability & MOQ Score (peso 20%)
        avail_score = Decimal("0.0")
        if evidence.stock_available is True:
            avail_score += Decimal("12.0")
            explanation_lines.append("Stock available: +12.0")
        elif evidence.stock_available is False:
            avail_score += Decimal("0.0")
            explanation_lines.append("Stock unavailable (out of stock): +0.0")
        else:
            avail_score += Decimal("5.0")
            explanation_lines.append("Stock status UNKNOWN: +5.0")

        # MOQ
        moq = evidence.minimum_order_quantity
        if moq is not None:
            if moq <= 5:
                avail_score += Decimal("8.0")
                explanation_lines.append(f"Low MOQ ({moq}): +8.0")
            elif moq <= 20:
                avail_score += Decimal("5.0")
                explanation_lines.append(f"Medium MOQ ({moq}): +5.0")
            else:
                avail_score += Decimal("2.0")
                explanation_lines.append(f"High MOQ ({moq}): +2.0")
        else:
            explanation_lines.append("MOQ UNKNOWN: +0.0")

        # 4. Lead Time & Shipping Score (peso 10%)
        logistics_score = Decimal("0.0")
        lead_time = evidence.quote.lead_time_days if evidence.quote else evidence.lead_time_days
        shipping_cost = evidence.quote.shipping_cost if evidence.quote else evidence.shipping_cost

        if lead_time is not None:
            if lead_time <= 2:
                logistics_score += Decimal("5.0")
            elif lead_time <= 7:
                logistics_score += Decimal("3.0")
            else:
                logistics_score += Decimal("1.0")
            explanation_lines.append(f"Lead time ({lead_time} days): +{logistics_score}")
        else:
            explanation_lines.append("Lead time UNKNOWN: +0.0")

        if shipping_cost is not None:
            if shipping_cost == Decimal("0"):
                logistics_score += Decimal("5.0")
                explanation_lines.append("Shipping free: +5.0")
            else:
                logistics_score += Decimal("3.0")
                explanation_lines.append(f"Shipping known ({shipping_cost}): +3.0")
        else:
            explanation_lines.append("Shipping cost UNKNOWN: +0.0")

        # 5. Reliability & Evidence Confidence Score (peso 10%)
        rel_score = Decimal("0.0")
        conf_map = {
            Confidence.HIGH: Decimal("5.0"),
            Confidence.MEDIUM: Decimal("3.0"),
            Confidence.LOW: Decimal("1.0"),
            Confidence.UNKNOWN: Decimal("0.0"),
        }
        rel_score += conf_map.get(evidence.confidence, Decimal("0.0"))

        if evidence.quote is not None:
            rel_score += Decimal("5.0")
            explanation_lines.append("Confirmed Quote available: +5.0 reliability")
        elif supplier.status in ["ACTIVE", "VERIFIED"]:
            rel_score += Decimal("4.0")
            explanation_lines.append(f"Supplier verified ({supplier.status}): +4.0")
        else:
            rel_score += Decimal("1.0")
            explanation_lines.append(f"Supplier unverified ({supplier.status}): +1.0")

        total = (match_score + price_score + avail_score + logistics_score + rel_score).quantize(Decimal("0.1"))

        return SupplierScoreBreakdown(
            match_score=match_score,
            price_score=price_score,
            availability_score=avail_score,
            lead_time_score=logistics_score,
            reliability_score=rel_score,
            total_score=total,
            explanation=tuple(explanation_lines),
        )

    @classmethod
    def rank_candidates(
        cls,
        candidates: Sequence[SupplierCandidate],
        target_market_price: Optional[Decimal] = None
    ) -> List[SupplierCandidate]:
        """
        Calcula scores para cada candidato y los ordena de forma determinista y reproducible.
        Criterios de ordenamiento:
        1. total_score desc
        2. product_match confidence desc
        3. presence of confirmed quote
        4. stock_available (True > Unknown > False)
        5. supplier_id asc (determinismo estricto)
        """
        scored_candidates: List[SupplierCandidate] = []

        for cand in candidates:
            # Detectar riesgos y unknowns
            risks: List[str] = list(cand.risks)
            unknowns: List[str] = list(cand.unknowns)

            if cand.evidence.stock_available is False:
                risks.append("OUT_OF_STOCK: Supplier currently reports no available inventory")
            if cand.evidence.wholesale_price is None and (cand.evidence.quote is None or cand.evidence.quote.wholesale_price is None):
                unknowns.append("WHOLESALE_PRICE_UNKNOWN: Supplier catalog does not list price")
            if cand.evidence.shipping_cost is None and (cand.evidence.quote is None or cand.evidence.quote.shipping_cost is None):
                unknowns.append("SHIPPING_COST_UNKNOWN: Logistics shipping fee not specified")
            if cand.evidence.lead_time_days is None and (cand.evidence.quote is None or cand.evidence.quote.lead_time_days is None):
                unknowns.append("LEAD_TIME_UNKNOWN: Delivery lead time not specified")
            if cand.product_match.grade in [ProductMatchGrade.UNCERTAIN_MATCH, ProductMatchGrade.VARIANT]:
                risks.append(f"PRODUCT_MATCH_UNCERTAINTY: Grade {cand.product_match.grade.value}")

            # Evaluar readiness del proveedor
            rejection: Optional[SupplierRejectionReason] = None
            if cand.product_match.grade == ProductMatchGrade.NO_MATCH:
                readiness = SupplierReadiness.REJECTED
                rejection = SupplierRejectionReason.NO_PRODUCT_MATCH
            elif cand.evidence.stock_available is False:
                readiness = SupplierReadiness.REJECTED
                rejection = SupplierRejectionReason.OUT_OF_STOCK
            elif cand.evidence.quote is not None and cand.product_match.grade == ProductMatchGrade.EXACT_MATCH:
                readiness = SupplierReadiness.READY_FOR_ECONOMICS
            elif cand.product_match.grade in [ProductMatchGrade.EXACT_MATCH, ProductMatchGrade.CLOSE_MATCH] and cand.evidence.wholesale_price is not None:
                readiness = SupplierReadiness.EVALUATED
            else:
                readiness = SupplierReadiness.NEEDS_INVESTIGATION

            breakdown = cls.calculate_score(cand, target_market_price=target_market_price)

            updated_cand = SupplierCandidate(
                supplier=cand.supplier,
                evidence=cand.evidence,
                product_match=cand.product_match,
                readiness=readiness,
                score_breakdown=breakdown,
                rank=None,
                risks=tuple(dict.fromkeys(risks)),
                unknowns=tuple(dict.fromkeys(unknowns)),
                rejection_reason=rejection,
                created_at=cand.created_at,
            )
            scored_candidates.append(updated_cand)

        # Ordenar determinísticamente
        def sort_key(c: SupplierCandidate):
            score = c.score_breakdown.total_score if c.score_breakdown else Decimal("0")
            has_quote = 1 if c.evidence.quote is not None else 0
            stock_val = 2 if c.evidence.stock_available is True else (1 if c.evidence.stock_available is None else 0)
            conf_val = {"HIGH": 3, "MEDIUM": 2, "LOW": 1, "UNKNOWN": 0}.get(c.evidence.confidence.value, 0)
            return (-score, -has_quote, -stock_val, -conf_val, c.supplier.supplier_id)

        sorted_candidates = sorted(scored_candidates, key=sort_key)

        # Asignar rangos
        ranked_candidates: List[SupplierCandidate] = []
        for idx, c in enumerate(sorted_candidates, start=1):
            ranked_candidates.append(
                SupplierCandidate(
                    supplier=c.supplier,
                    evidence=c.evidence,
                    product_match=c.product_match,
                    readiness=c.readiness,
                    score_breakdown=c.score_breakdown,
                    rank=idx,
                    risks=c.risks,
                    unknowns=c.unknowns,
                    rejection_reason=c.rejection_reason,
                    created_at=c.created_at,
                )
            )

        return ranked_candidates


class QuoteNormalizer:
    """
    Normalizador de cotizaciones comerciales (C-02).
    Transforma evidencias crudas o cotizaciones parciales en instancias CommercialQuote
    normalizadas, ricas y comparables sin inventar datos (UNKNOWN explícito).
    """

    @classmethod
    def from_evidence(cls, evidence: SupplierEvidence, supplier_id: Optional[str] = None) -> CommercialQuote:
        """
        Construye una CommercialQuote a partir de un SupplierEvidence.
        Extrae datos de confirmed quote, price tiers y raw_payload si están disponibles.
        """
        sup_id = supplier_id or evidence.supplier_id
        q_id = f"Q-{sup_id}-{evidence.sku}"
        if evidence.quote:
            q_id = evidence.quote.quote_id

        # 1. Moneda (preservar original, nunca inventar FX)
        currency = evidence.currency or "CLP"
        if evidence.quote and evidence.quote.currency:
            currency = evidence.quote.currency

        # 2. Precio unitario
        unit_price = evidence.quote.wholesale_price if evidence.quote else evidence.wholesale_price

        # 3. MOQ explícito
        moq_qty = evidence.minimum_order_quantity
        moq_info = MOQInfo(quantity=moq_qty, moq_type=MOQType.SKU if moq_qty is not None else MOQType.UNKNOWN)

        # 4. Price Tiers (si existen en raw_payload o quote)
        raw = dict(evidence.raw_payload) if evidence.raw_payload else {}
        tiers: List[PriceTier] = []
        raw_tiers = raw.get("price_tiers") or raw.get("tiers")
        if isinstance(raw_tiers, list):
            for t in raw_tiers:
                if isinstance(t, dict) and "min_quantity" in t and "unit_price" in t:
                    tiers.append(
                        PriceTier(
                            min_quantity=int(t["min_quantity"]),
                            unit_price=Decimal(str(t["unit_price"])),
                            max_quantity=int(t["max_quantity"]) if t.get("max_quantity") is not None else None,
                            currency=str(t.get("currency", currency)),
                        )
                    )

        # 5. Shipping & Lead Time & Stock
        shipping = evidence.quote.shipping_cost if evidence.quote else evidence.shipping_cost
        lead_time = evidence.quote.lead_time_days if evidence.quote else evidence.lead_time_days
        stock = evidence.stock_available
        avail_qty = raw.get("quantity") if isinstance(raw.get("quantity"), int) else None

        # 6. Valid until & Observed at
        observed_at = evidence.observed_at
        valid_until_str = raw.get("price_valid_until") or raw.get("valid_until")
        valid_until: Optional[datetime] = None
        if valid_until_str and isinstance(valid_until_str, str):
            try:
                valid_until = datetime.fromisoformat(valid_until_str.replace("Z", "+00:00"))
            except Exception:
                valid_until = None

        # 7. Identificar unknowns explícitos
        unknowns: List[str] = []
        if unit_price is None and not tiers:
            unknowns.append("UNIT_PRICE_UNKNOWN")
        if moq_qty is None:
            unknowns.append("MOQ_UNKNOWN")
        if shipping is None:
            unknowns.append("SHIPPING_COST_UNKNOWN")
        if lead_time is None:
            unknowns.append("LEAD_TIME_UNKNOWN")
        if stock is None:
            unknowns.append("AVAILABILITY_UNKNOWN")

        return CommercialQuote(
            quote_id=q_id,
            supplier_id=sup_id,
            sku=evidence.sku,
            unit_price=unit_price,
            currency=currency,
            moq=moq_info,
            price_tiers=tuple(tiers),
            shipping_cost=shipping,
            lead_time_days=lead_time,
            stock_available=stock,
            available_quantity=avail_qty,
            commercial_conditions=dict(raw.get("commercial", {})),
            confidence=evidence.confidence,
            provenance_type=evidence.provenance_type,
            source=evidence.source,
            observed_at=observed_at,
            valid_until=valid_until,
            unknowns=tuple(unknowns),
        )


class QuoteComparator:
    """
    Motor determinista de comparación comercial y ranking de cotizaciones (C-02).
    - Valida compatibilidad estricta (moneda, unidad, validez).
    - Detecta conflictos entre cotizaciones contradictorias.
    - Evalúa escenarios de volumen (QTY=1, QTY=MOQ, etc.).
    - Produce BestCommercialCandidate con trazabilidad explicable.
    """

    @classmethod
    def check_comparability(
        cls,
        quote_a: CommercialQuote,
        quote_b: CommercialQuote
    ) -> Tuple[QuoteComparabilityStatus, List[str]]:
        """
        Evalúa si dos cotizaciones son directamente comparables.
        Si difieren en moneda y no hay FX seguro -> NOT_COMPARABLE.
        """
        reasons: List[str] = []

        # Moneda
        if quote_a.currency.upper() != quote_b.currency.upper():
            reasons.append(
                f"CURRENCY_MISMATCH: {quote_a.supplier_id} uses '{quote_a.currency}' while {quote_b.supplier_id} uses '{quote_b.currency}'. No fabricated FX conversion permitted."
            )

        # Precios conocidos
        has_price_a = quote_a.unit_price is not None or len(quote_a.price_tiers) > 0
        has_price_b = quote_b.unit_price is not None or len(quote_b.price_tiers) > 0
        if not has_price_a or not has_price_b:
            reasons.append("MISSING_PRICE: One or both quotes lack observable price data.")

        if reasons:
            if quote_a.currency.upper() != quote_b.currency.upper():
                return QuoteComparabilityStatus.NOT_COMPARABLE, reasons
            return QuoteComparabilityStatus.PARTIALLY_COMPARABLE, reasons

        return QuoteComparabilityStatus.COMPARABLE, []

    @classmethod
    def detect_conflicts(cls, quotes: Sequence[CommercialQuote]) -> List[QuoteConflict]:
        """
        Detecta si existen cotizaciones contradictorias para el mismo supplier y SKU.
        Registra el conflicto y evalúa su resolución determinista.
        """
        conflicts: List[QuoteConflict] = []
        quotes_by_key: Dict[Tuple[str, str], List[CommercialQuote]] = {}

        for q in quotes:
            key = (q.supplier_id, q.sku)
            quotes_by_key.setdefault(key, []).append(q)

        for (sup_id, sku), sup_quotes in quotes_by_key.items():
            if len(sup_quotes) > 1:
                for i in range(len(sup_quotes)):
                    for j in range(i + 1, len(sup_quotes)):
                        qa, qb = sup_quotes[i], sup_quotes[j]
                        # Si difieren en precio unitario o condiciones
                        price_diff = qa.unit_price != qb.unit_price
                        moq_diff = qa.moq.quantity != qb.moq.quantity
                        if price_diff or moq_diff:
                            desc = f"Contradicting quotes found for supplier {sup_id} (Price: {qa.unit_price} vs {qb.unit_price}, MOQ: {qa.moq.quantity} vs {qb.moq.quantity})"
                            # Resolver determinísticamente por mayor frescura o confianza
                            res_status = QuoteConflictStatus.UNRESOLVED
                            resolved_id = None
                            conf_order = {Confidence.HIGH: 3, Confidence.MEDIUM: 2, Confidence.LOW: 1, Confidence.UNKNOWN: 0}
                            
                            if qa.currency != qb.currency:
                                res_status = QuoteConflictStatus.NOT_COMPARABLE
                            elif conf_order.get(qa.confidence, 0) > conf_order.get(qb.confidence, 0):
                                res_status = QuoteConflictStatus.RESOLVED_BY_HIGHER_CONFIDENCE
                                resolved_id = qa.quote_id
                            elif conf_order.get(qb.confidence, 0) > conf_order.get(qa.confidence, 0):
                                res_status = QuoteConflictStatus.RESOLVED_BY_HIGHER_CONFIDENCE
                                resolved_id = qb.quote_id
                            elif qa.observed_at > qb.observed_at:
                                res_status = QuoteConflictStatus.RESOLVED_BY_NEWER_EVIDENCE
                                resolved_id = qa.quote_id
                            elif qb.observed_at > qa.observed_at:
                                res_status = QuoteConflictStatus.RESOLVED_BY_NEWER_EVIDENCE
                                resolved_id = qb.quote_id

                            conflicts.append(
                                QuoteConflict(
                                    quote_a_id=qa.quote_id,
                                    quote_b_id=qb.quote_id,
                                    supplier_id=sup_id,
                                    sku=sku,
                                    conflict_type="PRICE_OR_MOQ_DISCREPANCY",
                                    description=desc,
                                    resolution_status=res_status,
                                    resolved_quote_id=resolved_id,
                                )
                            )
        return conflicts

    @classmethod
    def evaluate_scenario(cls, quote: CommercialQuote, quantity: int) -> QuoteScenarioEvaluation:
        """
        Evalúa una cotización para un escenario de cantidad de compra específico.
        """
        if quantity < 1:
            raise ValueError("quantity must be at least 1")

        unit_p = quote.get_unit_price_for_quantity(quantity)
        moq_qty = quote.moq.quantity
        is_moq_ok = (moq_qty is None) or (quantity >= moq_qty)

        goods_total = (unit_p * quantity) if unit_p is not None else None
        shipping = quote.shipping_cost
        landed_subtotal = (goods_total + shipping) if (goods_total is not None and shipping is not None) else None

        unknowns: List[str] = []
        if unit_p is None:
            unknowns.append("UNIT_PRICE_UNKNOWN")
        if shipping is None:
            unknowns.append("SHIPPING_COST_UNKNOWN")
        if moq_qty is None:
            unknowns.append("MOQ_UNKNOWN")

        notes = []
        if not is_moq_ok:
            notes.append(f"Quantity ({quantity}) below MOQ ({moq_qty})")
        if quote.price_tiers:
            notes.append(f"Volume tier applied for qty {quantity}")

        return QuoteScenarioEvaluation(
            scenario_quantity=quantity,
            unit_price=unit_p,
            currency=quote.currency,
            total_goods_cost=goods_total,
            shipping_cost=shipping,
            total_estimated_landed_subtotal=landed_subtotal,
            is_moq_satisfied=is_moq_ok,
            is_comparable=unit_p is not None,
            unknowns=tuple(unknowns),
            notes="; ".join(notes),
        )

    @classmethod
    def compare_candidates(
        cls,
        candidates: Sequence[SupplierCandidate],
        target_product_title: str,
        target_sku: Optional[str] = None,
        target_market_price: Optional[Decimal] = None,
        analysis_quantities: Sequence[int] = (1, 10, 50, 100),
        iteration: int = 1,
    ) -> QuoteComparisonResult:
        """
        Compara un conjunto de SupplierCandidates transformando sus cotizaciones,
        evaluando compatibilidad, calculando ranking determinista y seleccionando el BestCommercialCandidate.
        """
        items: List[SupplierQuoteComparisonItem] = []
        quotes: List[CommercialQuote] = []
        non_comparable_reasons: List[str] = []

        # 1. Normalizar cotizaciones
        for cand in candidates:
            c_quote = cand.evidence.commercial_quote
            if not c_quote:
                c_quote = QuoteNormalizer.from_evidence(cand.evidence, supplier_id=cand.supplier.supplier_id)
            quotes.append(c_quote)

        # 2. Detectar conflictos
        conflicts = cls.detect_conflicts(quotes)

        # 3. Evaluar compatibilidad de monedas y construir items
        currencies = set(q.currency.upper() for q in quotes if q.unit_price is not None or q.price_tiers)
        is_multi_currency = len(currencies) > 1

        if is_multi_currency:
            non_comparable_reasons.append(
                f"Multiple currencies observed across candidates ({', '.join(sorted(currencies))}). Direct cross-currency numerical comparison is restricted without verified FX rates."
            )

        # Determinar moneda de referencia base si existe
        dominant_currency = "CLP" if "CLP" in currencies else (sorted(currencies)[0] if currencies else "CLP")

        for cand, quote in zip(candidates, quotes):
            knowns: List[str] = []
            unknowns: List[str] = []
            risks: List[str] = list(cand.risks)
            advantages: List[str] = []

            # Knowns / Unknowns
            if quote.unit_price is not None:
                knowns.append(f"Price: {quote.unit_price} {quote.currency}")
            else:
                unknowns.append("Wholesale price unknown")

            if quote.moq.is_known:
                knowns.append(f"MOQ: {quote.moq.quantity} units")
            else:
                unknowns.append("MOQ unknown")

            if quote.shipping_cost is not None:
                knowns.append(f"Shipping: {quote.shipping_cost} {quote.currency}")
            else:
                unknowns.append("Shipping cost unknown")

            if quote.lead_time_days is not None:
                knowns.append(f"Lead time: {quote.lead_time_days} days")
            else:
                unknowns.append("Lead time unknown")

            if quote.stock_available is not None:
                knowns.append(f"In Stock: {quote.stock_available}")
            else:
                unknowns.append("Stock availability unknown")

            # Advantages / Risks
            if quote.stock_available is False:
                risks.append("OUT_OF_STOCK: Candidate reports 0 inventory")
            if quote.freshness == QuoteFreshness.EXPIRED:
                risks.append("EXPIRED_QUOTE: Price validity date has passed")
            elif quote.freshness == QuoteFreshness.STALE:
                risks.append("STALE_QUOTE: Observation is older than 90 days")

            if quote.price_tiers:
                advantages.append(f"Volume discounts available ({len(quote.price_tiers)} tiers)")
            if quote.moq.is_known and quote.moq.quantity == 1:
                advantages.append("Low MOQ (1 unit - suitable for test/dropshipping)")
            if quote.shipping_cost == Decimal("0"):
                advantages.append("Free shipping offered by supplier")

            # Comparabilidad
            comp_status = QuoteComparabilityStatus.COMPARABLE
            if quote.currency.upper() != dominant_currency.upper():
                comp_status = QuoteComparabilityStatus.NOT_COMPARABLE
            elif not quote.unit_price and not quote.price_tiers:
                comp_status = QuoteComparabilityStatus.PARTIALLY_COMPARABLE

            # Escenarios de cantidad
            scenarios: Dict[int, QuoteScenarioEvaluation] = {}
            # Agregar MOQ específico del proveedor si existe a las cantidades de análisis
            eval_qtys = list(dict.fromkeys(list(analysis_quantities) + ([quote.moq.quantity] if quote.moq.is_known else [])))
            for q_val in sorted(eval_qtys):
                scenarios[q_val] = cls.evaluate_scenario(quote, q_val)

            # Score comercial
            score_bd = SupplierScorer.calculate_score(cand, target_market_price=target_market_price)

            items.append(
                SupplierQuoteComparisonItem(
                    supplier=cand.supplier,
                    quote=quote,
                    product_match=cand.product_match,
                    comparability_status=comp_status,
                    commercial_score=score_bd.total_score,
                    score_breakdown=score_bd,
                    rank=None,
                    scenario_evaluations=scenarios,
                    knowns=tuple(knowns),
                    unknowns=tuple(unknowns),
                    risks=tuple(dict.fromkeys(risks)),
                    advantages=tuple(advantages),
                )
            )

        # 4. Ranking determinista
        # Criterios:
        # 1. Product Match no es NO_MATCH
        # 2. Stock disponible != False
        # 3. Freshness != EXPIRED
        # 4. Commercial Score desc
        # 5. Confirmed quote disponible
        # 6. Supplier ID asc
        def item_sort_key(item: SupplierQuoteComparisonItem):
            is_no_match = 1 if item.product_match.grade == ProductMatchGrade.NO_MATCH else 0
            is_out_of_stock = 1 if item.quote.stock_available is False else 0
            is_expired = 1 if item.quote.freshness == QuoteFreshness.EXPIRED else 0
            score = item.commercial_score or Decimal("0")
            has_confirmed = 1 if item.quote.unit_price is not None and item.quote.confidence == Confidence.HIGH else 0
            return (is_no_match, is_out_of_stock, is_expired, -score, -has_confirmed, item.supplier.supplier_id)

        sorted_items = sorted(items, key=item_sort_key)

        ranked_items: List[SupplierQuoteComparisonItem] = []
        for idx, it in enumerate(sorted_items, start=1):
            ranked_items.append(
                SupplierQuoteComparisonItem(
                    supplier=it.supplier,
                    quote=it.quote,
                    product_match=it.product_match,
                    comparability_status=it.comparability_status,
                    commercial_score=it.commercial_score,
                    score_breakdown=it.score_breakdown,
                    rank=idx,
                    scenario_evaluations=it.scenario_evaluations,
                    knowns=it.knowns,
                    unknowns=it.unknowns,
                    risks=it.risks,
                    advantages=it.advantages,
                )
            )

        # 5. Best Commercial Candidate (si hay al menos un candidato elegible)
        best_candidate: Optional[BestCommercialCandidate] = None
        eligible_candidates = [
            it for it in ranked_items
            if it.product_match.grade != ProductMatchGrade.NO_MATCH
            and it.quote.stock_available is not False
            and it.quote.freshness != QuoteFreshness.EXPIRED
        ]

        if eligible_candidates:
            top = eligible_candidates[0]
            why_lines = [
                f"Ranked #1 with commercial score {top.commercial_score}/100.0.",
                f"Product match grade: {top.product_match.grade.value}.",
            ]
            if top.quote.unit_price is not None:
                why_lines.append(f"Confirmed unit price: {top.quote.unit_price} {top.quote.currency}.")
            if top.quote.moq.is_known:
                why_lines.append(f"Known MOQ: {top.quote.moq.quantity} units.")
            if top.advantages:
                why_lines.append(f"Key advantages: {'; '.join(top.advantages)}.")

            best_candidate = BestCommercialCandidate(
                supplier_id=top.supplier.supplier_id,
                supplier_name=top.supplier.name,
                quote_id=top.quote.quote_id,
                sku=top.quote.sku,
                currency=top.quote.currency,
                unit_price=top.quote.unit_price,
                moq=top.quote.moq.quantity,
                lead_time_days=top.quote.lead_time_days,
                shipping_cost=top.quote.shipping_cost,
                commercial_score=top.commercial_score or Decimal("0"),
                confidence=top.quote.confidence,
                freshness=top.quote.freshness,
                provenance_type=top.quote.provenance_type,
                why_best=" ".join(why_lines),
                key_advantages=top.advantages,
                remaining_unknowns=top.unknowns,
                iteration=iteration,
            )

        return QuoteComparisonResult(
            target_product_title=target_product_title,
            target_sku=target_sku,
            analysis_quantities=tuple(analysis_quantities),
            items=tuple(items),
            ranked_items=tuple(ranked_items),
            best_commercial_candidate=best_candidate,
            conflicts=tuple(conflicts),
            non_comparable_reasons=tuple(non_comparable_reasons),
        )


# ==============================================================================
# SERVICIOS DE DOMINIO PARA C-03: LEAD TIME, SHIPPING, SLA, RELIABILITY & RISK
# ==============================================================================

class LeadTimeAnalyzer:
    """
    Analizador determinista de Lead Time (C.8).
    Calcula métricas estadísticas y variabilidad únicamente a partir de evidencia histórica real.
    Si no hay datos suficientes, no inventa distribuciones ni promedios.
    """

    @classmethod
    def analyze_lead_time(
        cls,
        observed_days: Optional[int],
        historical_events: Sequence[SupplierObservationEvent] = (),
        confidence: Confidence = Confidence.UNKNOWN,
        provenance_type: EvidenceProvenanceType = EvidenceProvenanceType.FIXTURE,
        source: str = "CATALOG_OBSERVATION",
    ) -> LeadTimeProfile:
        unknowns: List[str] = []
        lead_time_events = [e for e in historical_events if e.metric == "lead_time_days" and isinstance(e.observed_value, (int, float))]

        if observed_days is None and not lead_time_events:
            unknowns.append("LEAD_TIME_UNKNOWN")
            return LeadTimeProfile(
                observed_days=None,
                confidence=Confidence.UNKNOWN,
                provenance_type=provenance_type,
                source=source,
                unknowns=tuple(unknowns),
            )

        if not lead_time_events:
            # Sólo existe el valor puntual observado en la cotización/catálogo
            unknowns.append("HISTORICAL_VARIABILITY_UNKNOWN")
            return LeadTimeProfile(
                observed_days=observed_days,
                min_days=observed_days,
                max_days=observed_days,
                historical_avg_days=float(observed_days) if observed_days is not None else None,
                historical_variance_days=None,
                on_time_rate=None,
                confidence=confidence,
                provenance_type=provenance_type,
                source=source,
                unknowns=tuple(unknowns),
            )

        # Si existen observaciones históricas reales
        values = [float(e.observed_value) for e in lead_time_events]
        avg_val = sum(values) / len(values)
        min_val = int(min(values))
        max_val = int(max(values))
        variance = (sum((x - avg_val) ** 2 for x in values) / len(values)) if len(values) > 1 else 0.0

        # On-time rate si hay target observed_days
        on_time_rate: Optional[float] = None
        if observed_days is not None and values:
            on_time_count = sum(1 for v in values if v <= float(observed_days))
            on_time_rate = on_time_count / len(values)

        return LeadTimeProfile(
            observed_days=observed_days if observed_days is not None else int(round(avg_val)),
            min_days=min_val,
            max_days=max_val,
            historical_avg_days=round(avg_val, 2),
            historical_variance_days=round(variance, 2),
            on_time_rate=round(on_time_rate, 4) if on_time_rate is not None else None,
            confidence=confidence,
            provenance_type=provenance_type,
            source=source,
            unknowns=tuple(unknowns),
        )


class ShippingAnalyzer:
    """
    Analizador y validador de opciones y comparabilidad de envíos (C.9).
    Separa zonas, costos y métodos sin fabricar normalizaciones geográficas.
    """

    @classmethod
    def from_quote_and_payload(
        cls,
        quote: Optional[CommercialQuote],
        raw_payload: Optional[Dict[str, Any]] = None,
        confidence: Confidence = Confidence.UNKNOWN,
        provenance_type: EvidenceProvenanceType = EvidenceProvenanceType.FIXTURE,
    ) -> ShippingOption:
        raw = raw_payload or {}
        unknowns: List[str] = []

        shipping_cost = None
        if "shipping_cost" in raw and raw["shipping_cost"] is not None:
            shipping_cost = Decimal(str(raw["shipping_cost"]))
        elif quote and quote.shipping_cost is not None:
            shipping_cost = quote.shipping_cost

        is_free = (shipping_cost == Decimal("0")) or (raw.get("free_shipping") is True) or (raw.get("is_free_shipping") is True)
        if is_free and shipping_cost is None:
            shipping_cost = Decimal("0")

        currency = quote.currency if quote else raw.get("currency", "CLP")
        origin_zone = raw.get("shipping_origin") or raw.get("origin_zone")
        destination_zone = raw.get("shipping_destination") or raw.get("destination_zone") or "CHILE_METROPOLITANA"
        carrier = raw.get("carrier")
        transit_days = raw.get("transit_days") or raw.get("estimated_transit_days")

        method = ShippingMethod.UNKNOWN
        raw_method = str(raw.get("shipping_method", "")).upper()
        if "EXPRESS" in raw_method:
            method = ShippingMethod.EXPRESS
        elif "STANDARD" in raw_method:
            method = ShippingMethod.STANDARD
        elif "SAME_DAY" in raw_method or "SAMEDAY" in raw_method:
            method = ShippingMethod.SAME_DAY
        elif "PICKUP" in raw_method:
            method = ShippingMethod.PICKUP
        elif "FREIGHT" in raw_method:
            method = ShippingMethod.FREIGHT
        elif shipping_cost is not None:
            method = ShippingMethod.STANDARD

        if shipping_cost is None and not is_free:
            unknowns.append("SHIPPING_COST_UNKNOWN")
        if origin_zone is None:
            unknowns.append("SHIPPING_ORIGIN_UNKNOWN")
        if transit_days is None:
            unknowns.append("TRANSIT_DAYS_UNKNOWN")

        return ShippingOption(
            shipping_cost=shipping_cost,
            currency=currency,
            origin_zone=origin_zone,
            destination_zone=destination_zone,
            method=method,
            carrier=carrier,
            estimated_transit_days=int(transit_days) if transit_days is not None else None,
            is_free_shipping_observed=is_free,
            confidence=confidence,
            provenance_type=provenance_type,
            unknowns=tuple(unknowns),
        )

    @classmethod
    def check_comparability(
        cls,
        shipping_a: ShippingOption,
        shipping_b: ShippingOption,
    ) -> Tuple[ShippingComparabilityStatus, List[str]]:
        reasons: List[str] = []

        if shipping_a.shipping_cost is None or shipping_b.shipping_cost is None:
            reasons.append("One or both suppliers have UNKNOWN shipping cost.")
            return ShippingComparabilityStatus.NOT_COMPARABLE_UNKNOWN_COST, reasons

        if shipping_a.destination_zone and shipping_b.destination_zone:
            if shipping_a.destination_zone.upper() != shipping_b.destination_zone.upper():
                reasons.append(
                    f"Different destination zones ({shipping_a.destination_zone} vs {shipping_b.destination_zone})."
                )
                return ShippingComparabilityStatus.NOT_COMPARABLE_ZONE, reasons

        if shipping_a.currency.upper() != shipping_b.currency.upper():
            reasons.append(
                f"Different shipping currencies ({shipping_a.currency} vs {shipping_b.currency})."
            )
            return ShippingComparabilityStatus.NOT_COMPARABLE_ZONE, reasons

        if shipping_a.method != ShippingMethod.UNKNOWN and shipping_b.method != ShippingMethod.UNKNOWN:
            if shipping_a.method != shipping_b.method:
                reasons.append(
                    f"Different shipping methods ({shipping_a.method.value} vs {shipping_b.method.value})."
                )
                return ShippingComparabilityStatus.PARTIALLY_COMPARABLE, reasons

        return ShippingComparabilityStatus.COMPARABLE, ["Shipping options are directly comparable."]


class HistoricalPerformanceAnalyzer:
    """
    Analizador de desempeño histórico y cálculo de tendencias temporales (C.12 / C.14).
    No fabrica tendencias si el historial es insuficiente.
    """

    @classmethod
    def build_profile(
        cls,
        supplier_id: str,
        events: Sequence[SupplierObservationEvent] = (),
    ) -> HistoricalPerformanceProfile:
        unknowns: List[str] = []
        if not events:
            unknowns.extend(["HISTORICAL_EVENTS_UNKNOWN", "FULFILLMENT_RATE_UNKNOWN", "CANCELLATION_RATE_UNKNOWN", "SLA_UNKNOWN"])
            return HistoricalPerformanceProfile(
                supplier_id=supplier_id,
                observation_count=0,
                first_observed_at=None,
                last_observed_at=None,
                fulfillment_rate=None,
                cancellation_rate=None,
                on_time_delivery_rate=None,
                incident_count=0,
                lead_time_trend=PerformanceTrend.INSUFFICIENT_HISTORY,
                sla_trend=PerformanceTrend.INSUFFICIENT_HISTORY,
                sla_records=(),
                events=(),
                unknowns=tuple(unknowns),
            )

        sorted_events = sorted(events, key=lambda e: e.timestamp)
        first_at = sorted_events[0].timestamp
        last_at = sorted_events[-1].timestamp
        obs_count = len(sorted_events)

        # Fulfillment & Cancellations
        fulfillment_events = [e for e in sorted_events if e.metric in ["order_fulfilled", "fulfillment_status"]]
        cancellation_events = [e for e in sorted_events if e.metric in ["order_cancelled", "cancellation"]]
        incident_events = [e for e in sorted_events if e.metric in ["incident", "claim", "defect"]]

        ful_rate: Optional[float] = None
        if fulfillment_events:
            successes = sum(1 for e in fulfillment_events if e.observed_value is True or str(e.observed_value).upper() == "FULFILLED")
            ful_rate = round(successes / len(fulfillment_events), 4)
        else:
            unknowns.append("FULFILLMENT_RATE_UNKNOWN")

        canc_rate: Optional[float] = None
        if fulfillment_events or cancellation_events:
            total_orders = len(fulfillment_events) + len(cancellation_events)
            canc_count = len(cancellation_events) + sum(1 for e in fulfillment_events if str(e.observed_value).upper() == "CANCELLED")
            canc_rate = round(canc_count / total_orders, 4) if total_orders > 0 else 0.0
        else:
            unknowns.append("CANCELLATION_RATE_UNKNOWN")

        # On-time delivery rate
        delivery_events = [e for e in sorted_events if e.metric == "delivery_on_time"]
        on_time_rate: Optional[float] = None
        if delivery_events:
            on_time_count = sum(1 for e in delivery_events if e.observed_value is True or str(e.observed_value).upper() in ["ON_TIME", "TRUE"])
            on_time_rate = round(on_time_count / len(delivery_events), 4)
        else:
            unknowns.append("ON_TIME_DELIVERY_UNKNOWN")

        # SLA Records & Trends
        sla_records: List[SLARecord] = []
        sla_events = [e for e in sorted_events if e.metric.startswith("sla_")]
        for se in sla_events:
            metric_clean = se.metric.replace("sla_", "")
            sla_records.append(
                SLARecord(
                    metric_name=metric_clean,
                    target_value=1.0,
                    observed_value=float(se.observed_value) if isinstance(se.observed_value, (int, float)) else 1.0,
                    unit="ratio",
                    compliance_status=SLAStatus.COMPLIANT if se.observed_value in [True, 1.0, "COMPLIANT"] else SLAStatus.NON_COMPLIANT,
                    evidence_source=se.source,
                    confidence=se.confidence,
                )
            )

        # Detección determinista de tendencia en Lead Time
        lt_events = [e for e in sorted_events if e.metric == "lead_time_days" and isinstance(e.observed_value, (int, float))]
        lt_trend = PerformanceTrend.INSUFFICIENT_HISTORY
        if len(lt_events) >= 3:
            lt_vals = [float(e.observed_value) for e in lt_events]
            # Comparar primera mitad vs segunda mitad
            mid = len(lt_vals) // 2
            early_avg = sum(lt_vals[:mid]) / mid
            recent_avg = sum(lt_vals[mid:]) / (len(lt_vals) - mid)
            diff = recent_avg - early_avg
            if diff <= -1.0:
                lt_trend = PerformanceTrend.IMPROVING  # Días disminuyeron
            elif diff >= 1.0:
                lt_trend = PerformanceTrend.DETERIORATING  # Días aumentaron
            else:
                lt_trend = PerformanceTrend.STABLE

        return HistoricalPerformanceProfile(
            supplier_id=supplier_id,
            observation_count=obs_count,
            first_observed_at=first_at,
            last_observed_at=last_at,
            fulfillment_rate=ful_rate,
            cancellation_rate=canc_rate,
            on_time_delivery_rate=on_time_rate,
            incident_count=len(incident_events),
            lead_time_trend=lt_trend,
            sla_trend=PerformanceTrend.INSUFFICIENT_HISTORY if not sla_records else PerformanceTrend.STABLE,
            sla_records=tuple(sla_records),
            events=tuple(sorted_events),
            unknowns=tuple(unknowns),
        )


class ReliabilityEvaluator:
    """
    Evaluador determinista de confiabilidad y SLA de proveedores (C.10).
    No asume que la ausencia de evidencia signifique buena o mala confiabilidad.
    """

    @classmethod
    def evaluate(
        cls,
        candidate: SupplierCandidate,
        history: HistoricalPerformanceProfile,
        quote: Optional[CommercialQuote] = None,
    ) -> ReliabilityEvaluation:
        sup_id = candidate.supplier.supplier_id
        knowns: List[str] = []
        unknowns: List[str] = []
        explanation: List[str] = []

        scores: List[Decimal] = []

        # 1. Evaluación de Stock Consistency
        stock_score: Optional[Decimal] = None
        if quote and quote.stock_available is True:
            stock_score = Decimal("90.0")
            knowns.append("Active stock confirmed by supplier")
            explanation.append("Stock availability: Confirmed (90.0/100.0)")
        elif quote and quote.stock_available is False:
            stock_score = Decimal("10.0")
            knowns.append("Supplier confirmed out of stock")
            explanation.append("Stock availability: Out of stock (10.0/100.0)")
        else:
            unknowns.append("STOCK_AVAILABILITY_UNKNOWN")
            explanation.append("Stock availability: UNKNOWN")

        if stock_score is not None:
            scores.append(stock_score)

        # 2. Evaluación de SLA Compliance
        sla_rate: Optional[float] = None
        if history.sla_records:
            compliant_count = sum(1 for r in history.sla_records if r.compliance_status == SLAStatus.COMPLIANT)
            sla_rate = round(compliant_count / len(history.sla_records), 4)
            sla_score = Decimal(str(round(sla_rate * 100, 1)))
            scores.append(sla_score)
            knowns.append(f"SLA Compliance rate: {sla_rate:.1%}")
            explanation.append(f"SLA compliance: {sla_rate:.1%} ({sla_score}/100.0)")
        else:
            unknowns.append("SLA_RECORDS_UNKNOWN")
            explanation.append("SLA records: UNKNOWN (No historical SLA contracts)")

        # 3. Evaluación de Fulfillment & On-time Delivery
        if history.fulfillment_rate is not None:
            ful_score = Decimal(str(round(history.fulfillment_rate * 100, 1)))
            scores.append(ful_score)
            knowns.append(f"Historical fulfillment rate: {history.fulfillment_rate:.1%}")
            explanation.append(f"Fulfillment rate: {history.fulfillment_rate:.1%} ({ful_score}/100.0)")

        if history.on_time_delivery_rate is not None:
            ontime_score = Decimal(str(round(history.on_time_delivery_rate * 100, 1)))
            scores.append(ontime_score)
            knowns.append(f"Historical on-time delivery: {history.on_time_delivery_rate:.1%}")
            explanation.append(f"On-time delivery: {history.on_time_delivery_rate:.1%} ({ontime_score}/100.0)")

        # Penalización por incidentes
        if history.incident_count > 0:
            penalty = Decimal(str(min(history.incident_count * 15, 60)))
            knowns.append(f"Logged incidents: {history.incident_count}")
            explanation.append(f"Incident penalty: -{penalty} pts ({history.incident_count} incidents)")

        # Score global de confiabilidad
        final_rel_score: Optional[Decimal] = None
        if scores:
            avg_s = sum(scores) / Decimal(str(len(scores)))
            if history.incident_count > 0:
                avg_s = max(Decimal("5.0"), avg_s - Decimal(str(min(history.incident_count * 15, 50))))
            final_rel_score = avg_s.quantize(Decimal("0.1"))
        else:
            unknowns.append("INSUFFICIENT_DATA_FOR_RELIABILITY_SCORE")

        confidence = Confidence.HIGH if len(knowns) >= 3 else (Confidence.MEDIUM if len(knowns) >= 1 else Confidence.UNKNOWN)

        return ReliabilityEvaluation(
            supplier_id=sup_id,
            reliability_score=final_rel_score,
            sla_compliance_rate=sla_rate,
            stock_consistency_score=stock_score,
            confidence=confidence,
            known_factors=tuple(knowns),
            unknown_factors=tuple(unknowns),
            explanation=tuple(explanation),
        )


class SupplierRiskEngine:
    """
    Motor determinista de evaluación de riesgo de proveedores (C.11).
    Separa 5 dimensiones clave:
    1. Operational Risk (stock, incidentes, cancelaciones)
    2. Logistics Risk (lead time excesivo, shipping indefinido, método desconocido)
    3. Availability Risk (stock desconocido o nulo, MOQ desproporcionado)
    4. Evidence Risk (fuentes no verificadas, cotizaciones vencidas o confianza baja)
    5. Commercial Risk (precios fuera de mercado, falta de tiers, discrepancias)
    """

    @classmethod
    def evaluate_risk(
        cls,
        candidate: SupplierCandidate,
        quote: Optional[CommercialQuote],
        lead_time_profile: LeadTimeProfile,
        shipping_option: ShippingOption,
        reliability: ReliabilityEvaluation,
        history: HistoricalPerformanceProfile,
    ) -> SupplierRiskProfile:
        sup_id = candidate.supplier.supplier_id
        unknowns: List[str] = []
        explanations: List[str] = []
        rejection_reasons: List[SupplierRejectionReason] = []

        # ----------------------------------------------------
        # 1. Operational Risk
        # ----------------------------------------------------
        op_signals: List[str] = []
        op_uncertainties: List[str] = []
        op_score_pts = Decimal("20.0")  # Base baseline

        if history.incident_count > 0:
            inc_pts = Decimal(str(min(history.incident_count * 20, 60)))
            op_score_pts += inc_pts
            op_signals.append(f"{history.incident_count} operational incidents registered (+{inc_pts})")
            if history.incident_count >= 3:
                rejection_reasons.append(SupplierRejectionReason.HIGH_OPERATIONAL_RISK)

        if history.cancellation_rate is not None:
            canc_pts = Decimal(str(round(history.cancellation_rate * 50, 1)))
            op_score_pts += canc_pts
            op_signals.append(f"Cancellation rate {history.cancellation_rate:.1%} (+{canc_pts})")
            if history.cancellation_rate > 0.25:
                rejection_reasons.append(SupplierRejectionReason.HIGH_OPERATIONAL_RISK)
        else:
            op_uncertainties.append("Cancellation rate unknown")

        if reliability.sla_compliance_rate is not None:
            if reliability.sla_compliance_rate < 0.70:
                op_score_pts += Decimal("30.0")
                op_signals.append(f"Low SLA compliance ({reliability.sla_compliance_rate:.1%}) (+30.0)")
                rejection_reasons.append(SupplierRejectionReason.POOR_SLA_COMPLIANCE)
            else:
                op_score_pts = max(Decimal("5.0"), op_score_pts - Decimal("10.0"))
                op_signals.append(f"Healthy SLA compliance ({reliability.sla_compliance_rate:.1%}) (-10.0)")
        else:
            op_uncertainties.append("SLA compliance unknown")

        op_level = RiskLevel.LOW if op_score_pts <= 25 else (RiskLevel.MEDIUM if op_score_pts <= 50 else (RiskLevel.HIGH if op_score_pts <= 75 else RiskLevel.CRITICAL))
        op_risk = SupplierRiskDimension(
            dimension_name="OPERATIONAL_RISK",
            risk_level=op_level,
            risk_score=min(Decimal("100.0"), op_score_pts).quantize(Decimal("0.1")),
            signals_observed=tuple(op_signals),
            uncertainties=tuple(op_uncertainties),
            explanation=f"Operational risk level: {op_level.value} (Score: {op_score_pts})",
        )

        # ----------------------------------------------------
        # 2. Logistics Risk
        # ----------------------------------------------------
        log_signals: List[str] = []
        log_uncertainties: List[str] = []
        log_score_pts = Decimal("20.0")

        if lead_time_profile.observed_days is not None:
            if lead_time_profile.observed_days > 15:
                log_score_pts += Decimal("40.0")
                log_signals.append(f"Long lead time ({lead_time_profile.observed_days} days) (+40.0)")
                if lead_time_profile.observed_days > 30:
                    rejection_reasons.append(SupplierRejectionReason.LOGISTICS_INCOMPATIBILITY)
            elif lead_time_profile.observed_days > 7:
                log_score_pts += Decimal("15.0")
                log_signals.append(f"Moderate lead time ({lead_time_profile.observed_days} days) (+15.0)")
            else:
                log_score_pts = max(Decimal("5.0"), log_score_pts - Decimal("10.0"))
                log_signals.append(f"Fast delivery ({lead_time_profile.observed_days} days) (-10.0)")
        else:
            log_uncertainties.append("Lead time unknown")
            log_score_pts += Decimal("20.0")

        if history.lead_time_trend == PerformanceTrend.DETERIORATING:
            log_score_pts += Decimal("25.0")
            log_signals.append("Lead time is deteriorating over time (+25.0)")
        elif history.lead_time_trend == PerformanceTrend.IMPROVING:
            log_score_pts = max(Decimal("5.0"), log_score_pts - Decimal("10.0"))
            log_signals.append("Lead time is improving over time (-10.0)")

        if shipping_option.shipping_cost is None and not shipping_option.is_free_shipping_observed:
            log_uncertainties.append("Shipping cost unknown")
            log_score_pts += Decimal("15.0")
        elif shipping_option.is_free_shipping_observed:
            log_signals.append("Free shipping observed (-10.0)")
            log_score_pts = max(Decimal("5.0"), log_score_pts - Decimal("10.0"))

        log_level = RiskLevel.LOW if log_score_pts <= 25 else (RiskLevel.MEDIUM if log_score_pts <= 50 else (RiskLevel.HIGH if log_score_pts <= 75 else RiskLevel.CRITICAL))
        log_risk = SupplierRiskDimension(
            dimension_name="LOGISTICS_RISK",
            risk_level=log_level,
            risk_score=min(Decimal("100.0"), log_score_pts).quantize(Decimal("0.1")),
            signals_observed=tuple(log_signals),
            uncertainties=tuple(log_uncertainties),
            explanation=f"Logistics risk level: {log_level.value} (Score: {log_score_pts})",
        )

        # ----------------------------------------------------
        # 3. Availability Risk
        # ----------------------------------------------------
        avail_signals: List[str] = []
        avail_uncertainties: List[str] = []
        avail_score_pts = Decimal("15.0")

        if quote:
            if quote.stock_available is False:
                avail_score_pts = Decimal("95.0")
                avail_signals.append("OUT OF STOCK: 0 units available (+80.0)")
                rejection_reasons.append(SupplierRejectionReason.OUT_OF_STOCK)
            elif quote.stock_available is True:
                avail_score_pts = Decimal("10.0")
                avail_signals.append("Stock available confirmed (-5.0)")
            else:
                avail_uncertainties.append("Stock availability unconfirmed")
                avail_score_pts += Decimal("20.0")

            if quote.moq.is_known:
                if quote.moq.quantity > 50:
                    avail_score_pts += Decimal("30.0")
                    avail_signals.append(f"High MOQ barrier ({quote.moq.quantity} units) (+30.0)")
                    if quote.moq.quantity > 200:
                        rejection_reasons.append(SupplierRejectionReason.EXCESSIVE_MOQ)
                elif quote.moq.quantity <= 5:
                    avail_signals.append(f"Low/Flexible MOQ ({quote.moq.quantity} units) (-5.0)")
                    avail_score_pts = max(Decimal("5.0"), avail_score_pts - Decimal("5.0"))
            else:
                avail_uncertainties.append("MOQ unknown")
                avail_score_pts += Decimal("10.0")
        else:
            avail_uncertainties.append("No commercial quote present")
            avail_score_pts += Decimal("40.0")

        avail_level = RiskLevel.LOW if avail_score_pts <= 25 else (RiskLevel.MEDIUM if avail_score_pts <= 50 else (RiskLevel.HIGH if avail_score_pts <= 75 else RiskLevel.CRITICAL))
        avail_risk = SupplierRiskDimension(
            dimension_name="AVAILABILITY_RISK",
            risk_level=avail_level,
            risk_score=min(Decimal("100.0"), avail_score_pts).quantize(Decimal("0.1")),
            signals_observed=tuple(avail_signals),
            uncertainties=tuple(avail_uncertainties),
            explanation=f"Availability risk level: {avail_level.value} (Score: {avail_score_pts})",
        )

        # ----------------------------------------------------
        # 4. Evidence Risk
        # ----------------------------------------------------
        evi_signals: List[str] = []
        evi_uncertainties: List[str] = []
        evi_score_pts = Decimal("15.0")

        if candidate.product_match.grade == ProductMatchGrade.NO_MATCH:
            evi_score_pts = Decimal("100.0")
            evi_signals.append("Product match failed (NO_MATCH)")
            rejection_reasons.append(SupplierRejectionReason.NO_PRODUCT_MATCH)
        elif candidate.product_match.grade == ProductMatchGrade.UNCERTAIN_MATCH:
            evi_score_pts += Decimal("35.0")
            evi_signals.append("Uncertain product match (+35.0)")
        elif candidate.product_match.grade == ProductMatchGrade.EXACT_MATCH:
            evi_score_pts = Decimal("5.0")
            evi_signals.append("Exact product match confirmed (-10.0)")

        if quote:
            if quote.freshness == QuoteFreshness.EXPIRED:
                evi_score_pts += Decimal("45.0")
                evi_signals.append("Quote validity EXPIRED (+45.0)")
            elif quote.freshness == QuoteFreshness.STALE:
                evi_score_pts += Decimal("20.0")
                evi_signals.append("Quote is STALE (>30 days old) (+20.0)")
            elif quote.freshness == QuoteFreshness.FRESH:
                evi_signals.append("Quote is FRESH observed (-5.0)")
                evi_score_pts = max(Decimal("5.0"), evi_score_pts - Decimal("5.0"))

            if quote.confidence == Confidence.HIGH:
                evi_signals.append("High evidence confidence (-5.0)")
                evi_score_pts = max(Decimal("5.0"), evi_score_pts - Decimal("5.0"))
            elif quote.confidence == Confidence.LOW:
                evi_score_pts += Decimal("20.0")
                evi_signals.append("Low evidence confidence (+20.0)")

        evi_level = RiskLevel.LOW if evi_score_pts <= 25 else (RiskLevel.MEDIUM if evi_score_pts <= 50 else (RiskLevel.HIGH if evi_score_pts <= 75 else RiskLevel.CRITICAL))
        evi_risk = SupplierRiskDimension(
            dimension_name="EVIDENCE_RISK",
            risk_level=evi_level,
            risk_score=min(Decimal("100.0"), evi_score_pts).quantize(Decimal("0.1")),
            signals_observed=tuple(evi_signals),
            uncertainties=tuple(evi_uncertainties),
            explanation=f"Evidence risk level: {evi_level.value} (Score: {evi_score_pts})",
        )

        # ----------------------------------------------------
        # 5. Commercial Risk
        # ----------------------------------------------------
        com_signals: List[str] = []
        com_uncertainties: List[str] = []
        com_score_pts = Decimal("15.0")

        if quote and quote.unit_price is not None:
            com_signals.append(f"Known unit price: {quote.unit_price} {quote.currency}")
            if not quote.price_tiers:
                com_signals.append("No volume discounts available (+10.0)")
                com_score_pts += Decimal("10.0")
        else:
            com_uncertainties.append("Wholesale price unknown (+30.0)")
            com_score_pts += Decimal("30.0")

        com_level = RiskLevel.LOW if com_score_pts <= 25 else (RiskLevel.MEDIUM if com_score_pts <= 50 else (RiskLevel.HIGH if com_score_pts <= 75 else RiskLevel.CRITICAL))
        com_risk = SupplierRiskDimension(
            dimension_name="COMMERCIAL_RISK",
            risk_level=com_level,
            risk_score=min(Decimal("100.0"), com_score_pts).quantize(Decimal("0.1")),
            signals_observed=tuple(com_signals),
            uncertainties=tuple(com_uncertainties),
            explanation=f"Commercial risk level: {com_level.value} (Score: {com_score_pts})",
        )

        # ----------------------------------------------------
        # Score de Riesgo Agregado Ponderado
        # ----------------------------------------------------
        # Ponderación: Op (25%), Log (25%), Avail (20%), Evi (15%), Com (15%)
        overall_score = (
            (op_risk.risk_score * Decimal("0.25")) +
            (log_risk.risk_score * Decimal("0.25")) +
            (avail_risk.risk_score * Decimal("0.20")) +
            (evi_risk.risk_score * Decimal("0.15")) +
            (com_risk.risk_score * Decimal("0.15"))
        ).quantize(Decimal("0.1"))

        overall_level = RiskLevel.LOW if overall_score <= 25 else (RiskLevel.MEDIUM if overall_score <= 50 else (RiskLevel.HIGH if overall_score <= 75 else RiskLevel.CRITICAL))
        is_rejected = len(rejection_reasons) > 0 or overall_score >= Decimal("75.0")

        all_unknowns = sorted(list(set(op_uncertainties + log_uncertainties + avail_uncertainties + evi_uncertainties + com_uncertainties)))

        explanations.append(f"Overall Risk Score: {overall_score}/100.0 (Level: {overall_level.value})")
        if is_rejected:
            explanations.append(f"REJECT RECOMMENDED due to: {', '.join(r.value for r in rejection_reasons) if rejection_reasons else 'Critical overall risk'}")

        return SupplierRiskProfile(
            supplier_id=sup_id,
            overall_risk_level=overall_level,
            overall_risk_score=overall_score,
            operational_risk=op_risk,
            logistics_risk=log_risk,
            availability_risk=avail_risk,
            evidence_risk=evi_risk,
            commercial_risk=com_risk,
            concentration_risk=None,
            is_reject_recommended=is_rejected,
            rejection_reasons=tuple(dict.fromkeys(rejection_reasons)),
            confidence=candidate.evidence.confidence,
            unknowns=tuple(all_unknowns),
            explanation=tuple(explanations),
        )


class SupplierRiskComparator:
    """
    Comparador integral de riesgo, logística, confiabilidad y conveniencia de proveedores (C-03).
    Sintetiza la dimensión comercial de C-02 con la evaluación de riesgo de C-03 determinísticamente.
    """

    @classmethod
    def evaluate_and_compare(
        cls,
        candidates: Sequence[SupplierCandidate],
        target_product_title: str,
        target_sku: Optional[str] = None,
        target_market_price: Optional[Decimal] = None,
        supplier_histories: Optional[Dict[str, Sequence[SupplierObservationEvent]]] = None,
        iteration: int = 1,
    ) -> SupplierRiskEvaluationResult:
        histories_map = supplier_histories or {}
        items: List[SupplierRiskComparisonItem] = []
        rejected_items: List[SupplierRiskComparisonItem] = []
        non_comparable_reasons: List[str] = []

        # 1. Evaluar cada candidato individualmente
        for cand in candidates:
            sup_id = cand.supplier.supplier_id
            events = histories_map.get(sup_id, ())
            quote = cand.evidence.commercial_quote or QuoteNormalizer.from_evidence(cand.evidence, supplier_id=sup_id)
            raw = dict(cand.evidence.raw_payload) if cand.evidence.raw_payload else {}

            # Construir perfiles individuales
            lead_time_profile = LeadTimeAnalyzer.analyze_lead_time(
                observed_days=quote.lead_time_days,
                historical_events=events,
                confidence=cand.evidence.confidence,
                provenance_type=cand.evidence.provenance_type,
            )

            shipping_option = ShippingAnalyzer.from_quote_and_payload(
                quote=quote,
                raw_payload=raw,
                confidence=cand.evidence.confidence,
                provenance_type=cand.evidence.provenance_type,
            )

            history_profile = HistoricalPerformanceAnalyzer.build_profile(
                supplier_id=sup_id,
                events=events,
            )

            reliability = ReliabilityEvaluator.evaluate(
                candidate=cand,
                history=history_profile,
                quote=quote,
            )

            risk_profile = SupplierRiskEngine.evaluate_risk(
                candidate=cand,
                quote=quote,
                lead_time_profile=lead_time_profile,
                shipping_option=shipping_option,
                reliability=reliability,
                history=history_profile,
            )

            # Scoring comercial de C-02
            score_breakdown = SupplierScorer.calculate_score(cand, target_market_price=target_market_price)
            comm_score = score_breakdown.total_score

            # Composite Suitability Score (C-03):
            # Integra: Commercial Score (40%) + Reliability Score (35%) + Inverted Risk Score (25%)
            rel_pts = reliability.reliability_score if reliability.reliability_score is not None else Decimal("50.0")
            inv_risk_pts = max(Decimal("0.0"), Decimal("100.0") - (risk_profile.overall_risk_score or Decimal("50.0")))

            composite_score = (
                (comm_score * Decimal("0.40")) +
                (rel_pts * Decimal("0.35")) +
                (inv_risk_pts * Decimal("0.25"))
            ).quantize(Decimal("0.1"))

            is_disqualified = risk_profile.is_reject_recommended or cand.product_match.grade == ProductMatchGrade.NO_MATCH
            disqual_reason = ", ".join(r.value for r in risk_profile.rejection_reasons) if is_disqualified else None

            all_unknowns = tuple(sorted(list(set(quote.unknowns + lead_time_profile.unknowns + shipping_option.unknowns + reliability.unknown_factors + risk_profile.unknowns))))

            item = SupplierRiskComparisonItem(
                supplier=cand.supplier,
                quote=quote,
                lead_time_profile=lead_time_profile,
                shipping_option=shipping_option,
                reliability=reliability,
                risk_profile=risk_profile,
                historical_performance=history_profile,
                preliminary_commercial_score=comm_score,
                composite_suitability_score=composite_score if not is_disqualified else Decimal("0.0"),
                rank=None,
                is_disqualified=is_disqualified,
                disqualification_reason=disqual_reason,
                unknowns=all_unknowns,
            )

            items.append(item)
            if is_disqualified:
                rejected_items.append(item)

        # 2. Ranking determinista de idoneidad y riesgo
        def rank_sort_key(it: SupplierRiskComparisonItem):
            is_disq = 1 if it.is_disqualified else 0
            comp_score = it.composite_suitability_score or Decimal("0.0")
            risk_score = it.risk_profile.overall_risk_score or Decimal("100.0")
            return (is_disq, -comp_score, risk_score, it.supplier.supplier_id)

        sorted_items = sorted(items, key=rank_sort_key)
        ranked_items: List[SupplierRiskComparisonItem] = []
        for idx, it in enumerate(sorted_items, start=1):
            ranked_items.append(
                SupplierRiskComparisonItem(
                    supplier=it.supplier,
                    quote=it.quote,
                    lead_time_profile=it.lead_time_profile,
                    shipping_option=it.shipping_option,
                    reliability=it.reliability,
                    risk_profile=it.risk_profile,
                    historical_performance=it.historical_performance,
                    preliminary_commercial_score=it.preliminary_commercial_score,
                    composite_suitability_score=it.composite_suitability_score,
                    rank=idx,
                    is_disqualified=it.is_disqualified,
                    disqualification_reason=it.disqualification_reason,
                    unknowns=it.unknowns,
                )
            )

        # 3. Determinar Best Supplier Candidate (preliminar C-03)
        best_candidate: Optional[BestSupplierCandidate] = None
        eligible = [it for it in ranked_items if not it.is_disqualified]

        if eligible:
            top = eligible[0]
            strengths: List[str] = []
            risks: List[str] = []

            if top.lead_time_profile.observed_days is not None:
                strengths.append(f"Fast lead time ({top.lead_time_profile.observed_days} days)")
            if top.shipping_option.is_free_shipping_observed:
                strengths.append("Free shipping offered")
            if top.reliability.reliability_score is not None:
                strengths.append(f"High reliability score ({top.reliability.reliability_score}/100.0)")
            if top.quote and top.quote.unit_price is not None:
                strengths.append(f"Competitive unit price ({top.quote.unit_price} {top.quote.currency})")

            for r in [top.risk_profile.operational_risk, top.risk_profile.logistics_risk, top.risk_profile.availability_risk]:
                if r.risk_level in [RiskLevel.HIGH, RiskLevel.MEDIUM]:
                    risks.append(f"{r.dimension_name}: {r.risk_level.value}")

            why_lines = [
                f"Ranked #1 overall supplier candidate with composite suitability score {top.composite_suitability_score}/100.0.",
                f"Overall Risk Level: {top.risk_profile.overall_risk_level.value} (Score: {top.risk_profile.overall_risk_score}/100.0).",
                f"Product Match: {top.supplier.product_reference.title if top.supplier.product_reference else 'Confirmed match'}.",
            ]

            best_candidate = BestSupplierCandidate(
                supplier_id=top.supplier.supplier_id,
                supplier_name=top.supplier.name,
                sku=top.quote.sku if top.quote else "UNKNOWN_SKU",
                commercial_score=top.preliminary_commercial_score,
                reliability_score=top.reliability.reliability_score,
                overall_risk_score=top.risk_profile.overall_risk_score,
                composite_suitability_score=top.composite_suitability_score or Decimal("0.0"),
                confidence=top.supplier.source_type == EvidenceProvenanceType.LIVE and Confidence.HIGH or Confidence.MEDIUM,
                provenance_type=top.supplier.source_type,
                why_best=" ".join(why_lines),
                key_strengths=tuple(strengths),
                identified_risks=tuple(risks),
                remaining_unknowns=top.unknowns,
                iteration=iteration,
            )

        return SupplierRiskEvaluationResult(
            target_product_title=target_product_title,
            target_sku=target_sku,
            items=tuple(items),
            ranked_items=tuple(ranked_items),
            best_supplier_candidate=best_candidate,
            rejected_candidates=tuple(rejected_items),
            non_comparable_logistics_reasons=tuple(non_comparable_reasons),
        )


# ==============================================================================
# MOTOR DE RECOMENDACIÓN DE PROVEEDORES (C-04: C.13)
# ==============================================================================

class SupplierRecommendationPolicy:
    """
    Policy determinista de recomendación y evaluación de suficiencia de evidencia de proveedores (C.13).
    
    Principios fundamentales:
    1. NO selecciona únicamente por precio, ni únicamente por commercial score, ni únicamente por risk score.
    2. La recomendación surge de la combinación determinista de:
       ECONOMICS AVAILABLE + RELIABILITY + RISK + LOGISTICS + EVIDENCE + UNCERTAINTY (UNKNOWNs).
    3. Respeta estrictamente:
       UNKNOWN != GOOD, UNKNOWN != BAD, UNKNOWN != 0, UNKNOWN != 1, UNKNOWN != ASSUMED.
    4. Distingue formalmente los 5 estados:
       RECOMMEND, RECOMMEND_WITH_CONDITIONS, NEEDS_INVESTIGATION, NO_RECOMMENDATION, REJECT.
    5. Preserva la procedencia (FIXTURE vs LIVE) e integra condiciones de invalidación y contingencia.
    """

    MIN_COMPOSITE_SCORE_FOR_RECOMMEND = Decimal("60.0")
    MAX_OVERALL_RISK_FOR_RECOMMEND = Decimal("60.0")
    MIN_RELIABILITY_SCORE_FOR_RECOMMEND = Decimal("50.0")

    @classmethod
    def evaluate_evidence_sufficiency(
        cls,
        item: SupplierRiskComparisonItem,
    ) -> Tuple[bool, List[str], List[RecommendationCondition]]:
        """
        Evalúa si un candidato cuenta con evidencia suficiente en las 6 dimensiones críticas:
        1. Product Match (EXACT o CLOSE)
        2. Commercial conditions (precio unitario o tiers conocidos)
        3. Availability (stock conocido y confirmado)
        4. Logistics (lead time o shipping conocidos)
        5. Risk (riesgo evaluable sin riesgos críticos)
        6. Reliability (confiabilidad evaluable o historial verificable)

        Retorna:
        - is_sufficient_unconditional (bool): True si puede recomendarse sin condiciones previas.
        - missing_dimensions (List[str]): Dimensiones críticas faltantes.
        - required_conditions (List[RecommendationCondition]): Condiciones que se deben cumplir.
        """
        missing: List[str] = []
        conditions: List[RecommendationCondition] = []

        # 1. Product Match
        if item.risk_profile.evidence_risk.risk_score is not None and item.risk_profile.evidence_risk.risk_score >= Decimal("50.0"):
            missing.append("PRODUCT_MATCH_UNCERTAIN")
            conditions.append(RecommendationCondition(
                code="VERIFY_PRODUCT_SPECIFICATION",
                description="Verify exact technical specification and model compatibility with supplier",
                is_critical=True,
                suggested_action="Request spec sheet / datasheet confirmation",
            ))
        elif any("Uncertain product match" in sig for sig in item.risk_profile.evidence_risk.signals_observed):
            missing.append("PRODUCT_MATCH_UNCERTAIN")
            conditions.append(RecommendationCondition(
                code="VERIFY_PRODUCT_SPECIFICATION",
                description="Verify exact technical specification and model compatibility with supplier",
                is_critical=True,
                suggested_action="Request spec sheet / datasheet confirmation",
            ))

        # 2. Commercial Conditions
        if not item.quote or item.quote.unit_price is None:
            missing.append("WHOLESALE_PRICE_UNKNOWN")
            conditions.append(RecommendationCondition(
                code="OBTAIN_FORMAL_QUOTE",
                description="Obtain formal wholesale quotation with confirmed unit price and currency",
                is_critical=True,
                suggested_action="Request official commercial proforma",
            ))

        # 3. Availability / Stock
        if not item.quote or item.quote.stock_available is None:
            missing.append("STOCK_AVAILABILITY_UNKNOWN")
            conditions.append(RecommendationCondition(
                code="CONFIRM_REAL_TIME_STOCK",
                description="Verify physical inventory availability before placing purchase orders",
                is_critical=True,
                suggested_action="Validate current stock on supplier portal or warehouse API",
            ))
        elif item.quote.stock_available is False:
            missing.append("OUT_OF_STOCK")

        # 4. Logistics
        if item.lead_time_profile.observed_days is None:
            missing.append("LEAD_TIME_UNKNOWN")
            conditions.append(RecommendationCondition(
                code="VERIFY_DELIVERY_TIMELINE",
                description="Confirm guaranteed delivery lead time in business days",
                is_critical=False,
                suggested_action="Request shipping schedule confirmation",
            ))
        if item.shipping_option.shipping_cost is None and not item.shipping_option.is_free_shipping_observed:
            missing.append("SHIPPING_COST_UNKNOWN")
            conditions.append(RecommendationCondition(
                code="VERIFY_SHIPPING_COST",
                description="Confirm freight / carrier shipping cost to destination",
                is_critical=False,
                suggested_action="Request formal logistics quote",
            ))

        # 5. Risk & Freshness
        if item.quote and item.quote.freshness == QuoteFreshness.EXPIRED:
            missing.append("QUOTE_EXPIRED")
            conditions.append(RecommendationCondition(
                code="RENEW_EXPIRED_QUOTE",
                description="Quotation validity period has expired; updated price confirmation required",
                is_critical=True,
                suggested_action="Request quote renewal",
            ))
        elif item.quote and item.quote.freshness == QuoteFreshness.STALE:
            missing.append("QUOTE_STALE")
            conditions.append(RecommendationCondition(
                code="VERIFY_STALE_QUOTE_PRICING",
                description="Commercial terms are older than 30 days; verify current price stability",
                is_critical=False,
                suggested_action="Confirm price remains effective",
            ))

        # 6. Reliability
        if item.reliability.reliability_score is None:
            missing.append("RELIABILITY_HISTORY_UNKNOWN")
            conditions.append(RecommendationCondition(
                code="PERFORM_FIRST_ORDER_VERIFICATION",
                description="Supplier lacks historical track record; perform small trial order / milestone escrow",
                is_critical=False,
                suggested_action="Execute test batch order with strict QA inspection",
            ))

        is_sufficient = len(missing) == 0
        return is_sufficient, missing, conditions

    @classmethod
    def evaluate_invalidation_criteria(
        cls,
        item: SupplierRiskComparisonItem,
    ) -> List[str]:
        """
        Define explícitamente los criterios que invalidarían al candidato si ocurren.
        """
        criteria: List[str] = [
            "Stock exhaustion (reported stock becomes 0 or unavailable)",
            "Quote expiration without renewal",
            "Price increase exceeding 15% of current quotation",
            "Overall risk elevation above 70.0/100.0 or critical risk trigger",
            "SLA non-compliance on delivery timeline exceeding +5 business days",
            "Product catalog specification change or mismatch detection",
        ]
        return criteria

    @classmethod
    def calculate_recommendation_confidence(
        cls,
        item: SupplierRiskComparisonItem,
        missing_dimensions: Sequence[str],
    ) -> Confidence:
        """
        Calcula determinísticamente el nivel de confianza de la recomendación basándose en:
        - Procedencia de los datos (LIVE vs FIXTURE).
        - Cantidad de incógnitas/dimensiones faltantes.
        - Confianza del candidato base.
        - Freshness de la cotización.
        """
        # Si la procedencia es FIXTURE, nunca puede ser HIGH a ciegas sin verificar
        is_live = item.supplier.source_type == EvidenceProvenanceType.LIVE

        if item.quote and item.quote.freshness == QuoteFreshness.EXPIRED:
            return Confidence.LOW

        if len(missing_dimensions) >= 3:
            return Confidence.LOW
        elif len(missing_dimensions) >= 1:
            return Confidence.MEDIUM if is_live else Confidence.LOW
        else:
            # Sin dimensiones críticas faltantes
            if is_live and item.reliability.confidence == Confidence.HIGH:
                return Confidence.HIGH
            return Confidence.MEDIUM


class SupplierRecommendationEngine:
    """
    Motor central de recomendación de proveedores para una oportunidad (C.13).
    Integra la evaluación de riesgo (C-03), cotizaciones (C-02) y descubrimiento (C-01).
    """

    @classmethod
    def generate_recommendation(
        cls,
        risk_evaluation_result: SupplierRiskEvaluationResult,
        opportunity_id: str,
        recommendation_id: Optional[str] = None,
        iteration: int = 1,
    ) -> SupplierRecommendation:
        """
        Genera la recomendación determinista final analizando los items evaluados,
        clasificando proveedores primarios y de contingencia (fallback) y generando
        la explicación estructurada.
        """
        import uuid
        rec_id = recommendation_id or f"rec-{uuid.uuid4().hex[:8]}"
        target_title = risk_evaluation_result.target_product_title
        target_sku = risk_evaluation_result.target_sku

        ranked_items = list(risk_evaluation_result.ranked_items)
        rejected_items = list(risk_evaluation_result.rejected_candidates)

        # Si no hay candidatos evaluados en absoluto
        if not ranked_items and not rejected_items:
            return SupplierRecommendation(
                recommendation_id=rec_id,
                opportunity_id=opportunity_id,
                target_product_title=target_title,
                target_sku=target_sku,
                decision=SupplierRecommendationDecision.NO_RECOMMENDATION,
                decision_reason="No supplier candidates were discovered or provided for evaluation.",
                primary_supplier=None,
                fallback_supplier=None,
                all_evaluated_candidates=(),
                rejected_candidates=(),
                conditions=(),
                unknowns=("NO_SUPPLIERS_EVALUATED",),
                rejection_reasons=("NO_CANDIDATES_AVAILABLE",),
                confidence=Confidence.UNKNOWN,
                freshness=QuoteFreshness.UNKNOWN_FRESHNESS,
                provenance=EvidenceProvenanceType.FIXTURE,
                explanation=StructuredRecommendationExplanation(
                    observed_facts=("0 suppliers evaluated",),
                    derived_metrics=("Suitability score: N/A",),
                    inferred_signals=("Market sourcing bottleneck",),
                    recommendation_summary="No supplier recommendation possible without candidate observations.",
                    why_selected="None",
                    why_over_alternatives="None",
                    contingency_plan="Initiate wider supplier discovery across secondary portals.",
                ),
            )

        # Filtrar candidatos viables (no descalificados ni con riesgo crítico absoluto)
        viable_items: List[SupplierRiskComparisonItem] = [
            it for it in ranked_items
            if not it.is_disqualified and not it.risk_profile.is_reject_recommended
        ]

        # Si todos los candidatos están descalificados o rechazados
        if not viable_items:
            all_reasons = []
            for it in (ranked_items + rejected_items):
                if it.disqualification_reason:
                    all_reasons.append(f"{it.supplier.name}: {it.disqualification_reason}")
                for r in it.risk_profile.rejection_reasons:
                    all_reasons.append(f"{it.supplier.name}: {r.value}")

            # Determinar si la razón es rechazo total o falta de datos
            decision = SupplierRecommendationDecision.REJECT if rejected_items else SupplierRecommendationDecision.NO_RECOMMENDATION

            return SupplierRecommendation(
                recommendation_id=rec_id,
                opportunity_id=opportunity_id,
                target_product_title=target_title,
                target_sku=target_sku,
                decision=decision,
                decision_reason=f"All evaluated suppliers ({len(ranked_items) + len(rejected_items)}) failed viability and risk checks.",
                primary_supplier=None,
                fallback_supplier=None,
                all_evaluated_candidates=tuple(ranked_items),
                rejected_candidates=tuple(rejected_items),
                conditions=(),
                unknowns=tuple(dict.fromkeys(u for it in ranked_items for u in it.unknowns)),
                rejection_reasons=tuple(dict.fromkeys(all_reasons)),
                confidence=Confidence.HIGH if rejected_items else Confidence.LOW,
                freshness=QuoteFreshness.UNKNOWN_FRESHNESS,
                provenance=ranked_items[0].supplier.source_type if ranked_items else EvidenceProvenanceType.FIXTURE,
                explanation=StructuredRecommendationExplanation(
                    observed_facts=tuple(f"{it.supplier.name} rejected: {it.disqualification_reason or 'Critical Risk'}" for it in (ranked_items + rejected_items)),
                    derived_metrics=("Eligible candidates count: 0",),
                    inferred_signals=("High supply channel risk",),
                    recommendation_summary="All candidates rejected due to unviable commercial, risk or logistics parameters.",
                    why_selected="No supplier met minimum viability thresholds.",
                    why_over_alternatives="N/A",
                    contingency_plan="Search for alternative verified distributors or re-negotiate procurement terms.",
                ),
            )

        # -------------------------------------------------------------
        # EVALUAR EL CANDIDATO TOP PARA PRIMARY SUPPLIER
        # -------------------------------------------------------------
        top_item = viable_items[0]
        is_sufficient, missing_dims, conditions = SupplierRecommendationPolicy.evaluate_evidence_sufficiency(top_item)

        # Determinar si el top item califica para recomendación o requiere investigación previa
        # Si faltan datos críticos esenciales (como precio unitario desconocido o stock agotado) -> NEEDS_INVESTIGATION o NO_RECOMMENDATION
        decision: SupplierRecommendationDecision
        decision_reason: str

        if "OUT_OF_STOCK" in missing_dims:
            decision = SupplierRecommendationDecision.REJECT
            decision_reason = f"Top candidate {top_item.supplier.name} is confirmed OUT OF STOCK."
        elif "WHOLESALE_PRICE_UNKNOWN" in missing_dims or "PRODUCT_MATCH_UNCERTAIN" in missing_dims:
            decision = SupplierRecommendationDecision.NEEDS_INVESTIGATION
            decision_reason = f"Top candidate {top_item.supplier.name} requires critical investigation ({', '.join(missing_dims)}) before recommendation."
        elif len(conditions) > 0:
            decision = SupplierRecommendationDecision.RECOMMEND_WITH_CONDITIONS
            decision_reason = f"Recommend {top_item.supplier.name} subject to verification of {len(conditions)} operational condition(s)."
        else:
            decision = SupplierRecommendationDecision.RECOMMEND
            decision_reason = f"Strongly recommend {top_item.supplier.name} based on verified commercial, risk, logistics and reliability metrics."

        # Construir PrimarySupplierSelection si no fue REJECT total
        primary_selection: Optional[PrimarySupplierSelection] = None
        if decision != SupplierRecommendationDecision.REJECT:
            strengths: List[str] = []
            if top_item.quote and top_item.quote.unit_price is not None:
                strengths.append(f"Competitive unit price: {top_item.quote.unit_price} {top_item.quote.currency}")
            if top_item.lead_time_profile.observed_days is not None:
                strengths.append(f"Lead time: {top_item.lead_time_profile.observed_days} business days")
            if top_item.shipping_option.is_free_shipping_observed:
                strengths.append("Free shipping offered")
            elif top_item.shipping_option.shipping_cost is not None:
                strengths.append(f"Shipping cost: {top_item.shipping_option.shipping_cost} {top_item.shipping_option.currency}")
            if top_item.reliability.reliability_score is not None:
                strengths.append(f"Reliability score: {top_item.reliability.reliability_score}/100.0")

            primary_risks: List[str] = []
            for dim in [top_item.risk_profile.operational_risk, top_item.risk_profile.logistics_risk, top_item.risk_profile.availability_risk, top_item.risk_profile.evidence_risk]:
                if dim.risk_level in [RiskLevel.HIGH, RiskLevel.MEDIUM]:
                    primary_risks.append(f"{dim.dimension_name}: {dim.risk_level.value}")

            why_fallback_txt = "Highest ranked candidate on combined commercial suitability, risk mitigation and logistics compliance."
            if len(viable_items) > 1:
                second = viable_items[1]
                score_diff = (top_item.composite_suitability_score or Decimal("0")) - (second.composite_suitability_score or Decimal("0"))
                why_fallback_txt = f"Outperforms secondary candidate {second.supplier.name} by +{score_diff} composite points with lower/comparable risk profile."

            comm_pos = f"Score: {top_item.preliminary_commercial_score or 'N/A'}/100.0; Price: {top_item.quote.unit_price if top_item.quote else 'UNKNOWN'} {top_item.quote.currency if top_item.quote else ''}"
            log_pos = f"Lead time: {top_item.lead_time_profile.observed_days if top_item.lead_time_profile.observed_days is not None else 'UNKNOWN'} days; Shipping: {top_item.shipping_option.method.value}"

            primary_selection = PrimarySupplierSelection(
                supplier_id=top_item.supplier.supplier_id,
                supplier_name=top_item.supplier.name,
                sku=top_item.quote.sku if top_item.quote else "UNKNOWN_SKU",
                commercial_score=top_item.preliminary_commercial_score,
                reliability_score=top_item.reliability.reliability_score,
                overall_risk_score=top_item.risk_profile.overall_risk_score,
                composite_suitability_score=top_item.composite_suitability_score or Decimal("0.0"),
                confidence=SupplierRecommendationPolicy.calculate_recommendation_confidence(top_item, missing_dims),
                provenance_type=top_item.supplier.source_type,
                selection_reason=decision_reason,
                why_over_fallback=why_fallback_txt,
                commercial_position=comm_pos,
                logistics_position=log_pos,
                key_strengths=tuple(strengths),
                identified_risks=tuple(primary_risks),
                unknowns=top_item.unknowns,
                invalidation_criteria=tuple(SupplierRecommendationPolicy.evaluate_invalidation_criteria(top_item)),
            )

        # -------------------------------------------------------------
        # EVALUAR EL CANDIDATO SECUNDARIO PARA FALLBACK SUPPLIER
        # -------------------------------------------------------------
        fallback_selection: Optional[FallbackSupplierSelection] = None
        if len(viable_items) > 1:
            second_item = viable_items[1]
            # Verificar si el segundo candidato es genuinamente viable como fallback
            is_sec_sufficient, sec_missing, sec_conds = SupplierRecommendationPolicy.evaluate_evidence_sufficiency(second_item)
            
            # El fallback debe tener al menos precio o disponibilidad no rechazada
            if "OUT_OF_STOCK" not in sec_missing and second_item.risk_profile.overall_risk_level != RiskLevel.CRITICAL:
                sec_risks = [f"{d.dimension_name}: {d.risk_level.value}" for d in [second_item.risk_profile.operational_risk, second_item.risk_profile.logistics_risk] if d.risk_level in [RiskLevel.HIGH, RiskLevel.MEDIUM]]
                
                tradeoffs = []
                if top_item.quote and second_item.quote and top_item.quote.unit_price and second_item.quote.unit_price:
                    if second_item.quote.unit_price > top_item.quote.unit_price:
                        price_diff = second_item.quote.unit_price - top_item.quote.unit_price
                        tradeoffs.append(f"Higher unit price (+{price_diff} {top_item.quote.currency})")
                    elif second_item.quote.unit_price < top_item.quote.unit_price:
                        tradeoffs.append("Lower unit price but higher lead time/risk")

                if top_item.lead_time_profile.observed_days and second_item.lead_time_profile.observed_days:
                    if second_item.lead_time_profile.observed_days > top_item.lead_time_profile.observed_days:
                        tradeoffs.append(f"Longer lead time (+{second_item.lead_time_profile.observed_days - top_item.lead_time_profile.observed_days} days)")

                tradeoffs_str = "; ".join(tradeoffs) if tradeoffs else "Alternative viable supplier with comparable composite rating."

                activation_reasons = [
                    f"Immediate activation if {top_item.supplier.name} stock depletes or lead time exceeds SLA threshold",
                    f"Activate if primary quote expires without renewal",
                ]

                fallback_selection = FallbackSupplierSelection(
                    supplier_id=second_item.supplier.supplier_id,
                    supplier_name=second_item.supplier.name,
                    sku=second_item.quote.sku if second_item.quote else "UNKNOWN_SKU",
                    commercial_score=second_item.preliminary_commercial_score,
                    reliability_score=second_item.reliability.reliability_score,
                    overall_risk_score=second_item.risk_profile.overall_risk_score,
                    composite_suitability_score=second_item.composite_suitability_score or Decimal("0.0"),
                    confidence=SupplierRecommendationPolicy.calculate_recommendation_confidence(second_item, sec_missing),
                    provenance_type=second_item.supplier.source_type,
                    fallback_reason=f"Qualified secondary candidate (Composite Score: {second_item.composite_suitability_score}/100.0, Risk: {second_item.risk_profile.overall_risk_level.value}).",
                    tradeoffs_vs_primary=tradeoffs_str,
                    activation_conditions=tuple(activation_reasons),
                    identified_risks=tuple(sec_risks),
                    unknowns=second_item.unknowns,
                )

        # -------------------------------------------------------------
        # SÍNTESIS DE EXPLICACIÓN ESTRUCTURADA (4 CAPAS EPISTÉMICAS)
        # -------------------------------------------------------------
        observed_facts: List[str] = []
        derived_metrics: List[str] = []
        inferred_signals: List[str] = []

        if top_item.quote:
            if top_item.quote.unit_price is not None:
                observed_facts.append(f"Observed unit price: {top_item.quote.unit_price} {top_item.quote.currency} for {top_item.supplier.name}")
            if top_item.quote.moq.is_known:
                observed_facts.append(f"Observed MOQ: {top_item.quote.moq.quantity} units")
            if top_item.quote.stock_available is not None:
                observed_facts.append(f"Observed stock status: {'Available' if top_item.quote.stock_available else 'Out of stock'}")

        if top_item.lead_time_profile.observed_days is not None:
            observed_facts.append(f"Observed lead time: {top_item.lead_time_profile.observed_days} days")

        derived_metrics.append(f"Composite suitability score: {top_item.composite_suitability_score}/100.0")
        if top_item.preliminary_commercial_score is not None:
            derived_metrics.append(f"Commercial score: {top_item.preliminary_commercial_score}/100.0")
        if top_item.reliability.reliability_score is not None:
            derived_metrics.append(f"Reliability score: {top_item.reliability.reliability_score}/100.0")
        if top_item.risk_profile.overall_risk_score is not None:
            derived_metrics.append(f"Overall risk score: {top_item.risk_profile.overall_risk_score}/100.0")

        inferred_signals.append(f"Overall risk assessment: {top_item.risk_profile.overall_risk_level.value}")
        inferred_signals.append(f"Lead time trend: {top_item.historical_performance.lead_time_trend.value}")
        inferred_signals.append(f"SLA trend: {top_item.historical_performance.sla_trend.value}")
        if fallback_selection:
            inferred_signals.append(f"Contingency readiness: Qualified fallback available ({fallback_selection.supplier_name})")
        else:
            inferred_signals.append("Contingency readiness: NO_FALLBACK_AVAILABLE")

        contingency_plan_desc = (
            f"If {top_item.supplier.name} encounters supply disruption, immediately activate {fallback_selection.supplier_name}."
            if fallback_selection
            else "No qualified fallback available; initiate emergency discovery if primary fails."
        )

        explanation = StructuredRecommendationExplanation(
            observed_facts=tuple(observed_facts),
            derived_metrics=tuple(derived_metrics),
            inferred_signals=tuple(inferred_signals),
            recommendation_summary=decision_reason,
            why_selected=primary_selection.selection_reason if primary_selection else "No candidate met selection criteria",
            why_over_alternatives=primary_selection.why_over_fallback if primary_selection else "N/A",
            contingency_plan=contingency_plan_desc,
        )

        global_confidence = primary_selection.confidence if primary_selection else Confidence.LOW
        global_freshness = top_item.quote.freshness if top_item.quote else QuoteFreshness.UNKNOWN_FRESHNESS
        global_provenance = top_item.supplier.source_type

        return SupplierRecommendation(
            recommendation_id=rec_id,
            opportunity_id=opportunity_id,
            target_product_title=target_title,
            target_sku=target_sku,
            decision=decision,
            decision_reason=decision_reason,
            primary_supplier=primary_selection,
            fallback_supplier=fallback_selection,
            all_evaluated_candidates=tuple(ranked_items),
            rejected_candidates=tuple(rejected_items),
            conditions=tuple(conditions),
            unknowns=top_item.unknowns,
            rejection_reasons=tuple(dict.fromkeys(r.value for r in top_item.risk_profile.rejection_reasons)),
            confidence=global_confidence,
            freshness=global_freshness,
            provenance=global_provenance,
            explanation=explanation,
        )

    @classmethod
    def reevaluate_and_pivot_fallback(
        cls,
        recommendation: SupplierRecommendation,
        trigger: ContingencyTrigger,
        trigger_details: str = "",
    ) -> Tuple[SupplierRecommendation, bool]:
        """
        Reevalúa una recomendación ante una causal de contingencia (ContingencyTrigger).
        Si el proveedor primario queda invalidado y existe un fallback viable:
        - Invalida al primario.
        - Promueve al fallback a nuevo proveedor primario.
        - Retorna la nueva SupplierRecommendation y un booleano (pivoted_successfully).
        """
        import uuid

        if not recommendation.fallback_supplier:
            # No hay fallback disponible para pivotar
            updated = SupplierRecommendation(
                recommendation_id=f"rec-{uuid.uuid4().hex[:8]}",
                opportunity_id=recommendation.opportunity_id,
                target_product_title=recommendation.target_product_title,
                target_sku=recommendation.target_sku,
                decision=SupplierRecommendationDecision.NO_RECOMMENDATION,
                decision_reason=f"Primary supplier {recommendation.primary_supplier.supplier_name if recommendation.primary_supplier else 'N/A'} was invalidated due to {trigger.value} ({trigger_details}), but NO_FALLBACK_AVAILABLE.",
                primary_supplier=None,
                fallback_supplier=None,
                all_evaluated_candidates=recommendation.all_evaluated_candidates,
                rejected_candidates=recommendation.rejected_candidates,
                conditions=(),
                unknowns=recommendation.unknowns,
                rejection_reasons=(f"PRIMARY_INVALIDATED_{trigger.value}",),
                confidence=Confidence.LOW,
                freshness=recommendation.freshness,
                provenance=recommendation.provenance,
                explanation=StructuredRecommendationExplanation(
                    observed_facts=(f"Contingency trigger detected: {trigger.value}",),
                    derived_metrics=("Active primary: NONE", "Fallback available: FALSE"),
                    inferred_signals=("Procurement stalled; re-discovery required",),
                    recommendation_summary=f"Primary invalidated by {trigger.value}. No fallback available.",
                    why_selected="None",
                    why_over_alternatives="None",
                    contingency_plan="Initiate full supplier discovery cycle.",
                ),
            )
            return updated, False

        # Promover Fallback a Primary
        old_primary_name = recommendation.primary_supplier.supplier_name if recommendation.primary_supplier else "Old Primary"
        fb = recommendation.fallback_supplier

        promoted_primary = PrimarySupplierSelection(
            supplier_id=fb.supplier_id,
            supplier_name=fb.supplier_name,
            sku=fb.sku,
            commercial_score=fb.commercial_score,
            reliability_score=fb.reliability_score,
            overall_risk_score=fb.overall_risk_score,
            composite_suitability_score=fb.composite_suitability_score,
            confidence=fb.confidence,
            provenance_type=fb.provenance_type,
            selection_reason=f"Promoted from fallback following primary invalidation ({trigger.value}: {trigger_details}).",
            why_over_fallback=f"Activated as primary contingency replacement for {old_primary_name}.",
            commercial_position=f"Score: {fb.commercial_score or 'N/A'}/100.0",
            logistics_position=fb.tradeoffs_vs_primary,
            key_strengths=("Pre-qualified contingency supplier",),
            identified_risks=fb.identified_risks,
            unknowns=fb.unknowns,
            invalidation_criteria=(
                "Stock exhaustion",
                "Quote expiration",
                "Risk elevation above acceptable thresholds",
            ),
        )

        new_rec = SupplierRecommendation(
            recommendation_id=f"rec-{uuid.uuid4().hex[:8]}",
            opportunity_id=recommendation.opportunity_id,
            target_product_title=recommendation.target_product_title,
            target_sku=recommendation.target_sku,
            decision=SupplierRecommendationDecision.RECOMMEND_WITH_CONDITIONS,
            decision_reason=f"Pivoted to fallback supplier {fb.supplier_name} after primary {old_primary_name} was invalidated ({trigger.value}).",
            primary_supplier=promoted_primary,
            fallback_supplier=None,  # Ya no queda un segundo fallback registrado
            all_evaluated_candidates=recommendation.all_evaluated_candidates,
            rejected_candidates=recommendation.rejected_candidates,
            conditions=(RecommendationCondition(
                code="CONFIRM_FALLBACK_TERMS",
                description=f"Verify latest stock and price with newly promoted supplier {fb.supplier_name}",
                is_critical=True,
                suggested_action="Issue formal purchase order confirmation to backup supplier",
            ),),
            unknowns=fb.unknowns,
            rejection_reasons=(),
            confidence=fb.confidence,
            freshness=recommendation.freshness,
            provenance=fb.provenance_type,
            explanation=StructuredRecommendationExplanation(
                observed_facts=(
                    f"Contingency trigger executed: {trigger.value}",
                    f"Primary {old_primary_name} invalidated ({trigger_details})",
                    f"Promoted fallback: {fb.supplier_name}",
                ),
                derived_metrics=(
                    f"New primary composite score: {fb.composite_suitability_score}/100.0",
                ),
                inferred_signals=(
                    "Contingency pivot successful",
                    "Secondary supply chain route activated",
                ),
                recommendation_summary=f"Switched procurement to backup supplier {fb.supplier_name}.",
                why_selected=f"Qualified fallback with proven viability ({fb.fallback_reason})",
                why_over_alternatives=f"Primary {old_primary_name} is no longer viable",
                contingency_plan="Monitor fallback supplier delivery SLA; identify secondary tertiary vendor.",
            ),
        )
        return new_rec, True


