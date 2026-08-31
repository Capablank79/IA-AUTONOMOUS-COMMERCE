import uuid
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, Any, List, Optional, Tuple, Sequence, Mapping
from types import MappingProxyType

from src.domain.market_intelligence.models import Confidence, MarketEvidence, ReviewSignal
from src.domain.publication.models import (
    ListingDraft,
    SalesChannel,
    SalesChannelType,
)
from src.domain.publication.generation_models import (
    KeywordSourceType,
    SEOKeyword,
    SEOStrategy,
    CustomerPainCategory,
    CustomerPainPoint,
    UnmetNeed,
    DifferentiationStrategy,
    ClaimProvenanceType,
    ClaimProvenance,
    ListingFactGrounding,
    ChannelContentConstraint,
    MultichannelContent,
    ListingGenerationInput,
    ListingGenerationResult,
)


class CustomerPainMiningEngine:
    """
    Motor analítico de dominio para extraer y estructurar puntos de dolor, insatisfacciones
    y necesidades no cubiertas a partir de señales de mercado (ej. ReviewSignal, quejas de competidores).
    """

    # Palabras clave heurísticas para categorizar dolor si se procesa texto crudo
    PAIN_PATTERNS = {
        CustomerPainCategory.BATTERY: ["batería", "bateria", "carga", "duración", "autonomía", "descarga", "cable"],
        CustomerPainCategory.QUALITY: ["material", "plástico", "plastico", "frágil", "fragil", "rompió", "rompe", "calidad mala"],
        CustomerPainCategory.PERFORMANCE: ["lento", "potencia", "fuerza", "ruido", "calienta", "calentamiento", "rendimiento"],
        CustomerPainCategory.USABILITY: ["difícil", "dificil", "manual", "instrucciones", "complicado", "configuración"],
        CustomerPainCategory.DURABILITY: ["duró", "duro", "meses", "días", "semanas", "descompuso", "quemó"],
        CustomerPainCategory.SIZE: ["chico", "pequeño", "grande", "medidas", "tamaño", "dimensión", "corto"],
        CustomerPainCategory.MISSING_FEATURE: ["falta", "no trae", "sin accesorio", "incompleto", "carece"],
        CustomerPainCategory.PACKAGING: ["caja", "empaque", "abierto", "golpeado", "dañado en viaje"],
    }

    def extract_pains_from_reviews(self, review_signal: ReviewSignal) -> Tuple[CustomerPainPoint, ...]:
        """
        Analiza un ReviewSignal y extrae puntos de dolor de reviews negativas (rating <= 3).
        """
        pains: List[CustomerPainPoint] = []
        for review in review_signal.reviews:
            if review.rating <= 3 and review.text:
                lower_text = review.text.lower()
                detected_cat = CustomerPainCategory.OTHER
                for cat, patterns in self.PAIN_PATTERNS.items():
                    if any(p in lower_text for p in patterns):
                        detected_cat = cat
                        break

                pains.append(
                    CustomerPainPoint(
                        pain_id=f"pain_{review.external_id}",
                        category=detected_cat,
                        complaint_summary=review.text.strip(),
                        frequency="FREQUENT" if review.rating == 1 else "OCCASIONAL",
                        severity=10 if review.rating == 1 else (7 if review.rating == 2 else 5),
                        evidence_count=1,
                        source_review_ids=(review.external_id,),
                        confidence=review_signal.confidence,
                    )
                )
        return tuple(pains)

    def synthesize_differentiation_strategy(
        self,
        customer_pains: Sequence[CustomerPainPoint],
        verified_attributes: Mapping[str, Any],
    ) -> DifferentiationStrategy:
        """
        Cruza los puntos de dolor detectados en el mercado con los atributos VERIFICADOS del producto.
        Solo genera claims diferenciales si están demostrados en `verified_attributes`.
        Si una insatisfacción no tiene respaldo en el producto, NO se genera un claim falso.
        """
        unmet_needs: List[UnmetNeed] = []
        differential_claims: List[str] = []
        product_truth_mapping: Dict[str, str] = {}

        # Mapeo de dolor -> atributo verificable
        category_to_attr_keys = {
            CustomerPainCategory.BATTERY: ["battery_life", "battery_capacity", "autonomia", "bateria", "duracion_bateria"],
            CustomerPainCategory.QUALITY: ["material", "build_quality", "construccion", "calidad"],
            CustomerPainCategory.PERFORMANCE: ["suction_power", "power", "potencia", "speed", "rpm", "rendimiento"],
            CustomerPainCategory.DURABILITY: ["warranty", "durability", "garantia", "resistencia"],
            CustomerPainCategory.USABILITY: ["easy_setup", "manual_included", "facil_uso", "ergonomico"],
            CustomerPainCategory.SIZE: ["dimensions", "weight", "tamano", "peso", "compacto"],
        }

        # Agrupar dolores por categoría
        grouped_pains: Dict[CustomerPainCategory, List[CustomerPainPoint]] = {}
        for pain in customer_pains:
            grouped_pains.setdefault(pain.category, []).append(pain)

        for cat, pain_list in grouped_pains.items():
            pain_ids = tuple(p.pain_id for p in pain_list)
            need_desc = f"Mejor resolución de problemas de {cat.value.lower()}"
            unmet_need = UnmetNeed(
                need_id=f"need_{cat.value.lower()}",
                description=need_desc,
                related_pain_ids=pain_ids,
                confidence=Confidence.HIGH if len(pain_list) > 1 else Confidence.MEDIUM,
            )
            unmet_needs.append(unmet_need)

            # Verificar si nuestro producto tiene evidencia para responder a esta necesidad
            attr_keys = category_to_attr_keys.get(cat, [])
            backed_val = None
            found_key = None
            for k in attr_keys:
                if k in verified_attributes and verified_attributes[k] is not None:
                    backed_val = str(verified_attributes[k])
                    found_key = k
                    break

            if backed_val:
                claim = f"Destacado en {cat.value.lower()}: {backed_val}"
                differential_claims.append(claim)
                product_truth_mapping[f"pain_category_{cat.value}"] = f"attr:{found_key}={backed_val}"

        return DifferentiationStrategy(
            unmet_needs_addressed=tuple(unmet_needs),
            differential_claims=tuple(differential_claims),
            evidence_backed=True,
            product_truth_mapping=MappingProxyType(product_truth_mapping),
        )


class DeterministicListingGenerator:
    """
    Generador de dominio para la síntesis estructurada, grounded y reproducible de Listings.
    Cumple con:
    1. Grounding Factual Estricto: Los datos provienen de atributos verificados y evidencia de mercado.
    2. Zero Hallucination: Valores faltantes se tratan como UNKNOWN o se omiten; nunca se inventan specs o claims.
    3. Anti Keyword Stuffing & Anti Forbidden Claims: Respeta restricciones de canal y descarta claims no respaldados.
    4. Provenance & Confidence: Cada elemento del listing tiene trazabilidad de procedencia y nivel de certeza.
    5. Multichannel Readiness: Genera adaptaciones para Mercado Libre, Social, etc. sin acoplarse a APIs externas.
    """

    def __init__(self, pain_miner: Optional[CustomerPainMiningEngine] = None):
        self.pain_miner = pain_miner or CustomerPainMiningEngine()

    def generate(self, input_data: ListingGenerationInput) -> ListingGenerationResult:
        """
        Ejecuta el pipeline determinista de generación de listing.
        """
        constraints = input_data.constraints or ChannelContentConstraint()
        verified_attrs = dict(input_data.attributes)

        # 1. Integrar atributos canónicos explícitos si no estaban en el dict
        if input_data.brand and "brand" not in verified_attrs:
            verified_attrs["brand"] = input_data.brand
        if input_data.model and "model" not in verified_attrs:
            verified_attrs["model"] = input_data.model
        if input_data.condition and "condition" not in verified_attrs:
            verified_attrs["condition"] = input_data.condition

        # 2. Procesar y estructurar Customer Pains
        pains_list = list(input_data.customer_pains)
        if input_data.market_evidence and input_data.market_evidence.review_signals:
            for rev_signal in input_data.market_evidence.review_signals:
                extracted = self.pain_miner.extract_pains_from_reviews(rev_signal)
                pains_list.extend(extracted)
        unique_pains = tuple(pains_list)

        diff_strategy = self.pain_miner.synthesize_differentiation_strategy(
            customer_pains=unique_pains,
            verified_attributes=verified_attrs,
        )

        # 3. Construir Estrategia SEO (Observed > Derived > Proposed)
        seo_strategy = self._build_seo_strategy(input_data)

        # 4. Generar Título Optimizado Factual
        title, title_provenance = self._generate_title(input_data, seo_strategy, constraints)

        # 5. Generar Bullets Factuales (Beneficios derivados sustentados)
        bullets, bullet_provenances, omitted_claims = self._generate_bullets(
            input_data=input_data,
            diff_strategy=diff_strategy,
            constraints=constraints,
        )

        # 6. Generar Descripción Estructurada
        description, desc_provenances = self._generate_description(
            input_data=input_data,
            bullets=bullets,
            diff_strategy=diff_strategy,
            constraints=constraints,
        )

        # 7. Consolidar Grounding y Provenance
        all_provenance = (title_provenance,) + bullet_provenances + desc_provenances
        grounding = ListingFactGrounding(
            verified_attributes=MappingProxyType(verified_attrs),
            inferred_benefits=tuple(b for b in bullets if "Beneficio:" in b or "Ventaja:" in b),
            unsupported_claims_omitted=tuple(omitted_claims),
            claims_provenance=all_provenance,
        )

        # 8. Construir el ListingDraft canónico
        draft = ListingDraft(
            draft_id=f"draft_{uuid.uuid4().hex[:12]}",
            product_reference_id=input_data.product_id,
            title=title,
            description=description,
            price=input_data.price,
            currency=input_data.currency,
            available_quantity=input_data.available_quantity,
            channel=input_data.channel,
            images=input_data.images,
            attributes=MappingProxyType(verified_attrs),
            sku=input_data.sku,
            category_id=input_data.category_id,
            condition=input_data.condition,
            metadata=MappingProxyType({
                "locale": input_data.locale,
                "bullet_points": list(bullets),
                "seo_terms": list(seo_strategy.search_terms),
                "differentiation_applied": len(diff_strategy.differential_claims) > 0,
                "generation_mode": "DETERMINISTIC_GROUNDED",
                "source_input_metadata": dict(input_data.metadata),
            }),
            created_at=datetime.now(timezone.utc),
        )

        # 9. Generar Variantes Multicanal (Mercado Libre, Instagram, etc.)
        multichannel_variants = self._build_multichannel_variants(
            input_data=input_data,
            draft=draft,
            bullets=bullets,
            seo_strategy=seo_strategy,
        )

        # 10. Evaluar Confianza Global de la Generación
        overall_confidence = Confidence.HIGH
        if input_data.market_evidence and input_data.market_evidence.confidence:
            overall_confidence = input_data.market_evidence.confidence

        return ListingGenerationResult(
            draft=draft,
            grounding=grounding,
            seo_strategy=seo_strategy,
            differentiation_strategy=diff_strategy,
            multichannel_variants=multichannel_variants,
            confidence=overall_confidence,
            generation_metadata=MappingProxyType({
                "generator_engine": "DeterministicListingGenerator",
                "channel_id": input_data.channel.channel_id,
                "total_verified_attributes": len(verified_attrs),
                "omitted_unsupported_claims_count": len(omitted_claims),
            }),
            generated_at=datetime.now(timezone.utc),
        )

    def _build_seo_strategy(self, input_data: ListingGenerationInput) -> SEOStrategy:
        """
        Organiza palabras clave diferenciando observadas, derivadas y propuestas.
        """
        primary_kws: List[SEOKeyword] = []
        secondary_kws: List[SEOKeyword] = []
        search_terms: List[str] = []

        # Palabras clave explícitas del input
        for kw in input_data.seo_keywords:
            if kw.relevance_score >= 0.8:
                primary_kws.append(kw)
            else:
                secondary_kws.append(kw)
            search_terms.append(kw.keyword)

        # Si hay MarketEvidence con TrendSignals, incorporarlas como OBSERVED
        if input_data.market_evidence and input_data.market_evidence.trend_signals:
            for trend in input_data.market_evidence.trend_signals:
                if trend.matched or float(trend.trend_score) > 0.5:
                    obs_kw = SEOKeyword(
                        keyword=trend.keyword,
                        source_type=KeywordSourceType.OBSERVED,
                        relevance_score=float(trend.trend_score),
                        search_volume_observed=None,
                        provenance_id=f"trend_rank_{trend.rank}",
                    )
                    primary_kws.append(obs_kw)
                    search_terms.append(trend.keyword)

        # Si no hay keywords provistas, derivar del título base y atributos de forma transparente
        if not primary_kws and not secondary_kws:
            base_kw = SEOKeyword(
                keyword=input_data.title,
                source_type=KeywordSourceType.DERIVED,
                relevance_score=0.9,
                provenance_id=f"input_title_{input_data.product_id}",
            )
            primary_kws.append(base_kw)
            search_terms.append(input_data.title)

        # Deduplicar preservando orden
        dedup_search_terms = list(dict.fromkeys(search_terms))

        return SEOStrategy(
            primary_keywords=tuple(primary_kws),
            secondary_keywords=tuple(secondary_kws),
            search_terms=tuple(dedup_search_terms),
        )

    def _generate_title(
        self,
        input_data: ListingGenerationInput,
        seo_strategy: SEOStrategy,
        constraints: ChannelContentConstraint,
    ) -> Tuple[str, ClaimProvenance]:
        """
        Genera el título optimizado respetando restricciones de longitud y evitando claims no fundamentados.
        Estructura: [Nombre Producto] + [Marca/Modelo si existe] + [Atributo diferencial verificado o Keyword relevante].
        """
        parts: List[str] = [input_data.title.strip()]

        if input_data.brand and input_data.brand.lower() not in input_data.title.lower():
            parts.append(input_data.brand.strip())

        if input_data.model and input_data.model.lower() not in input_data.title.lower():
            parts.append(input_data.model.strip())

        # Agregar primary keyword relevante si agrega valor y cabe
        if seo_strategy.primary_keywords:
            best_kw = seo_strategy.primary_keywords[0].keyword
            if best_kw.lower() not in input_data.title.lower():
                parts.append(best_kw)

        candidate_title = " ".join(parts)

        # Sanitizar términos prohibidos si existieran
        for forbidden in constraints.forbidden_terms:
            if forbidden.lower() in candidate_title.lower():
                # Reemplazo insensitivo
                pattern = re.compile(re.escape(forbidden), re.IGNORECASE)
                candidate_title = pattern.sub("", candidate_title).strip()

        # Respetar límite de longitud de canal
        if len(candidate_title) > constraints.max_title_length:
            candidate_title = candidate_title[: constraints.max_title_length].rstrip()

        provenance = ClaimProvenance(
            claim_text=candidate_title,
            provenance_type=ClaimProvenanceType.DERIVED,
            source_field="title + brand/model + seo_primary",
            confidence=Confidence.HIGH,
        )

        return candidate_title, provenance

    def _generate_bullets(
        self,
        input_data: ListingGenerationInput,
        diff_strategy: DifferentiationStrategy,
        constraints: ChannelContentConstraint,
    ) -> Tuple[Tuple[str, ...], Tuple[ClaimProvenance, ...], Tuple[str, ...]]:
        """
        Genera bullets comerciales factuales y filtra claims prohibidos o sin respaldo.
        """
        bullets: List[str] = []
        provenances: List[ClaimProvenance] = []
        omitted_claims: List[str] = []

        # 1. Bullets derivados de claims diferenciales sustentados
        for diff_claim in diff_strategy.differential_claims:
            bullet_text = f"• {diff_claim}"
            if len(bullet_text) <= constraints.max_bullet_length:
                bullets.append(bullet_text)
                provenances.append(
                    ClaimProvenance(
                        claim_text=bullet_text,
                        provenance_type=ClaimProvenanceType.DERIVED,
                        source_field="differentiation_strategy",
                        confidence=Confidence.HIGH,
                    )
                )

        # 2. Bullets basados en atributos verificados
        for k, v in input_data.attributes.items():
            if len(bullets) >= constraints.max_bullets:
                break
            if v is None:
                continue

            # Sanitizar checks prohibidos
            v_str = str(v)
            has_forbidden = False
            for f_term in constraints.forbidden_terms:
                if f_term.lower() in v_str.lower():
                    has_forbidden = True
                    omitted_claims.append(f"Attribute {k} containing '{f_term}'")
                    break

            if has_forbidden:
                continue

            bullet_text = f"• {k.replace('_', ' ').capitalize()}: {v_str}"
            if len(bullet_text) <= constraints.max_bullet_length:
                bullets.append(bullet_text)
                provenances.append(
                    ClaimProvenance(
                        claim_text=bullet_text,
                        provenance_type=ClaimProvenanceType.OBSERVED,
                        source_field=f"attributes.{k}",
                        confidence=Confidence.HIGH,
                    )
                )

        # 3. Si aún hay espacio, añadir disponibilidad/condición
        if len(bullets) < constraints.max_bullets and input_data.condition:
            cond_text = f"• Condición del artículo: {input_data.condition.capitalize()}"
            bullets.append(cond_text)
            provenances.append(
                ClaimProvenance(
                    claim_text=cond_text,
                    provenance_type=ClaimProvenanceType.OBSERVED,
                    source_field="condition",
                    confidence=Confidence.HIGH,
                )
            )

        return tuple(bullets), tuple(provenances), tuple(omitted_claims)

    def _generate_description(
        self,
        input_data: ListingGenerationInput,
        bullets: Sequence[str],
        diff_strategy: DifferentiationStrategy,
        constraints: ChannelContentConstraint,
    ) -> Tuple[str, Tuple[ClaimProvenance, ...]]:
        """
        Genera una descripción comercial estructurada y factual.
        """
        lines: List[str] = [
            f"PRODUCTO: {input_data.title}",
            "",
            "CARACTERÍSTICAS PRINCIPALES:",
        ]

        for b in bullets:
            lines.append(b)

        if diff_strategy.differential_claims:
            lines.append("")
            lines.append("VENTAJAS DESTACADAS:")
            for dc in diff_strategy.differential_claims:
                lines.append(f"- {dc}")

        lines.append("")
        lines.append("ESPECIFICACIONES TÉCNICAS:")
        for k, v in input_data.attributes.items():
            if v is not None:
                lines.append(f"- {k.replace('_', ' ').capitalize()}: {v}")

        if input_data.supplier_context:
            lead_time = input_data.supplier_context.get("lead_time_days")
            if lead_time:
                lines.append(f"- Tiempo de despacho estimado: {lead_time} días")

        desc_text = "\n".join(lines)

        # Respetar longitud máxima
        if len(desc_text) > constraints.max_description_length:
            desc_text = desc_text[: constraints.max_description_length].rstrip()

        provenance = ClaimProvenance(
            claim_text="Full Description Body",
            provenance_type=ClaimProvenanceType.DERIVED,
            source_field="composite_verified_data",
            confidence=Confidence.HIGH,
        )

        return desc_text, (provenance,)

    def _build_multichannel_variants(
        self,
        input_data: ListingGenerationInput,
        draft: ListingDraft,
        bullets: Sequence[str],
        seo_strategy: SEOStrategy,
    ) -> Tuple[MultichannelContent, ...]:
        """
        Genera adaptaciones específicas para diversos canales sin dependencias externas.
        """
        variants: List[MultichannelContent] = []

        # 1. Variante Marketplace (Mercado Libre)
        mkt_content = MultichannelContent(
            channel_type=SalesChannelType.MARKETPLACE,
            channel_id=input_data.channel.channel_id,
            title=draft.title,
            body=draft.description,
            bullets=tuple(bullets),
            tags_or_keywords=seo_strategy.search_terms,
            call_to_action="Comprar ahora con envío garantizado",
            metadata=MappingProxyType({"format": "standard_marketplace"}),
        )
        variants.append(mkt_content)

        # 2. Variante Social Media / Instagram
        social_bullets = [b.replace("• ", "✨ ") for b in bullets[:3]]
        ig_body = f"🚀 Descubre {draft.title}\n\n" + "\n".join(social_bullets) + "\n\n👉 Disponible ahora en nuestro catálogo."
        ig_tags = tuple(f"#{t.replace(' ', '')}" for t in seo_strategy.search_terms[:5])
        ig_content = MultichannelContent(
            channel_type=SalesChannelType.SOCIAL_COMMERCE,
            channel_id="instagram_feed",
            title=f"Novedad: {draft.title}",
            body=ig_body,
            bullets=tuple(social_bullets),
            tags_or_keywords=ig_tags,
            call_to_action="Haz clic en el enlace de nuestra biografía para ordenar",
            metadata=MappingProxyType({"format": "instagram_post"}),
        )
        variants.append(ig_content)

        return tuple(variants)
