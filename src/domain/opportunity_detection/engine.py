import hashlib
from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional, Tuple, Dict, Any

from src.domain.opportunity_detection.models import (
    OpportunityRecord,
    OpportunityType,
    OpportunityStatus,
    ObservedOpportunityMetrics,
    DerivedOpportunityMetrics,
    OpportunityDetectionCriteria,
)
from src.domain.market_monitoring.models import (
    MarketObservation,
    ObservationStatus,
    NormalizedPrice,
)
from src.domain.market_intelligence.models import Confidence, Marketplace


class OpportunityDetectionEngine:
    """
    Motor de Dominio para Detección Determinista de Oportunidades Comerciales (Hito J.3).
    Procesa conjuntos de MarketObservation para identificar y estructurar patrones comerciales válidos.

    Reglas:
    - Puro Dominio: Sin dependencias de HTTP, bases de datos ni LLM para cálculos deterministas.
    - Separación estricta entre métricas OBSERVED y DERIVED.
    - UNKNOWN != 0 y no inventa datos ausentes.
    - Source failures / timeouts no generan oportunidades falsas.
    - Genera scoring determinista y trazabilidad causal completa hacia las observaciones de origen.
    """

    def detect_opportunities(
        self,
        observations: List[MarketObservation],
        criteria: Optional[OpportunityDetectionCriteria] = None,
        correlation_id: Optional[str] = None,
    ) -> List[OpportunityRecord]:
        if not observations:
            return []

        active_criteria = criteria or OpportunityDetectionCriteria()
        corr_id = correlation_id or "corr-opportunity-detection"

        # Agrupar observaciones por entidad de producto canónica (entity_id)
        grouped_observations: Dict[str, List[MarketObservation]] = {}
        for obs in observations:
            grouped_observations.setdefault(obs.entity_id, []).append(obs)

        detected_records: List[OpportunityRecord] = []

        for entity_id, obs_group in grouped_observations.items():
            record = self._evaluate_observation_group(entity_id, obs_group, active_criteria, corr_id)
            if record is not None:
                detected_records.append(record)

        return detected_records

    def _evaluate_observation_group(
        self,
        entity_id: str,
        observations: List[MarketObservation],
        criteria: OpportunityDetectionCriteria,
        correlation_id: str,
    ) -> Optional[OpportunityRecord]:
        # 1. Filtrar observaciones válidas (excluir o auditar SOURCE_FAILURE / TIMEOUT / INVALID_PAYLOAD)
        valid_observations = [
            obs for obs in observations
            if obs.status == ObservationStatus.SUCCESS
        ]

        if not valid_observations:
            # Todas las observaciones del grupo son fallos de fuente o payload inválido
            # NO fabricar oportunidad
            return None

        # Verificar si cumple con la cantidad mínima de observaciones requeridas
        if len(valid_observations) < criteria.min_observations_required:
            return self._build_insufficient_data_record(
                entity_id=entity_id,
                observations=valid_observations,
                reason=f"Insufficient observations: {len(valid_observations)} < {criteria.min_observations_required}",
                correlation_id=correlation_id,
            )

        # 2. Extraer métricas observadas (agregación determinista)
        latest_obs = max(valid_observations, key=lambda o: o.observed_at)
        marketplace = latest_obs.marketplace
        category = latest_obs.category
        title = latest_obs.title
        product_sku = latest_obs.product_sku

        # Observar precios
        observed_prices = [obs.price for obs in valid_observations if obs.price is not None]
        current_price = latest_obs.price

        # Observar stock y sold_quantity
        sold_quantities = [obs.sold_quantity for obs in valid_observations if obs.sold_quantity is not None]
        current_sold = sold_quantities[-1] if sold_quantities else None

        stocks = [obs.stock for obs in valid_observations if obs.stock is not None]
        current_stock = stocks[-1] if stocks else None

        # Observar competencia
        competitor_counts = [
            obs.competition_info.total_competitors
            for obs in valid_observations
            if obs.competition_info and obs.competition_info.total_competitors is not None
        ]
        observed_competitors = competitor_counts[-1] if competitor_counts else None

        lowest_comp_prices = [
            obs.competition_info.lowest_competitor_price
            for obs in valid_observations
            if obs.competition_info and obs.competition_info.lowest_competitor_price is not None
        ]
        lowest_comp_price = lowest_comp_prices[-1] if lowest_comp_prices else None

        buy_box_prices = [
            obs.competition_info.buy_box_winner_price
            for obs in valid_observations
            if obs.competition_info and obs.competition_info.buy_box_winner_price is not None
        ]
        buy_box_price = buy_box_prices[-1] if buy_box_prices else None

        observed_metrics = ObservedOpportunityMetrics(
            observed_price=current_price,
            observed_sold_quantity=current_sold,
            observed_stock=current_stock,
            observed_competitor_count=observed_competitors,
            lowest_competitor_price=lowest_comp_price,
            buy_box_winner_price=buy_box_price,
            observations_count=len(valid_observations),
        )

        # 3. Identificar campos desconocidos (UNKNOWN safety)
        unknown_fields: List[str] = []
        if current_price is None:
            unknown_fields.append("price")
        if current_sold is None:
            unknown_fields.append("sold_quantity")
        if current_stock is None:
            unknown_fields.append("stock")
        if observed_competitors is None:
            unknown_fields.append("competitor_count")

        # 4. Calcular métricas derivadas y evaluar reglas deterministas
        derived_metrics, opp_type, opp_status, reasons, confidence = self._derive_and_classify(
            observed_metrics=observed_metrics,
            criteria=criteria,
            latest_obs=latest_obs,
            unknown_fields=unknown_fields,
        )

        # Si el estado es INVALID o DISCARDED y el score no supera el mínimo requerido
        if opp_status in (OpportunityStatus.INVALID, OpportunityStatus.DISCARDED):
            return None

        # Deterministic Idempotency Key (deduplicar IDs para garantizar idempotencia exacta)
        obs_ids = tuple(sorted(list({obs.observation_id for obs in valid_observations})))
        idempotency_raw = f"{entity_id}_{marketplace.value}_{'_'.join(obs_ids)}"
        idempotency_key = hashlib.sha256(idempotency_raw.encode("utf-8")).hexdigest()
        opportunity_id = f"opp-{idempotency_key[:16]}"

        return OpportunityRecord(
            opportunity_id=opportunity_id,
            canonical_product_id=entity_id,
            marketplace=marketplace,
            detected_at=latest_obs.observed_at,
            opportunity_type=opp_type,
            status=opp_status,
            confidence=confidence,
            source_observation_ids=obs_ids,
            observed_metrics=observed_metrics,
            derived_metrics=derived_metrics,
            category=category,
            title=title,
            product_sku=product_sku,
            provenance=latest_obs.provenance,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            reasons=tuple(reasons),
            unknown_fields=tuple(unknown_fields),
            metadata={
                "source": latest_obs.source,
                "source_type": latest_obs.source_type.value,
            },
        )

    def _derive_and_classify(
        self,
        observed_metrics: ObservedOpportunityMetrics,
        criteria: OpportunityDetectionCriteria,
        latest_obs: MarketObservation,
        unknown_fields: List[str],
    ) -> Tuple[DerivedOpportunityMetrics, OpportunityType, OpportunityStatus, List[str], Confidence]:
        reasons: List[str] = []
        scoring_rationale: List[str] = []

        # Base score components
        demand_score = Decimal("0.0")
        price_gap_score = Decimal("0.0")
        comp_score = Decimal("0.0")

        # 1. Evaluación de precio / price gap
        price_gap_amount: Optional[Decimal] = None
        price_gap_ratio: Optional[Decimal] = None

        if observed_metrics.observed_price and observed_metrics.lowest_competitor_price:
            if observed_metrics.observed_price.currency == observed_metrics.lowest_competitor_price.currency:
                price_diff = observed_metrics.lowest_competitor_price.amount - observed_metrics.observed_price.amount
                price_gap_amount = price_diff
                if observed_metrics.lowest_competitor_price.amount > Decimal("0"):
                    price_gap_ratio = (price_diff / observed_metrics.lowest_competitor_price.amount).quantize(Decimal("0.0001"))
                    if price_gap_ratio > Decimal("0.10"):
                        price_gap_score = Decimal("35.0")
                        scoring_rationale.append(f"Favorable price gap: {price_gap_ratio * 100:.1f}% below lowest competitor")
                        reasons.append("Significant price advantage observed")
                    elif price_gap_ratio > Decimal("0.0"):
                        price_gap_score = Decimal("20.0")
                        scoring_rationale.append(f"Moderate price gap: {price_gap_ratio * 100:.1f}% below competitor")
                    else:
                        price_gap_score = Decimal("5.0")
                        scoring_rationale.append("Price is higher or equal to competitor")
        elif observed_metrics.observed_price is not None:
            # Hay precio pero no referencia de competencia
            price_gap_score = Decimal("15.0")
            scoring_rationale.append("Valid observed price without direct competitor comparison")
        else:
            if criteria.require_valid_price:
                reasons.append("Missing required price data")

        # 2. Evaluación de Demanda
        demand_intensity = "UNKNOWN"
        if observed_metrics.observed_sold_quantity is not None:
            if observed_metrics.observed_sold_quantity >= 50:
                demand_intensity = "HIGH"
                demand_score = Decimal("40.0")
                scoring_rationale.append(f"High demand confirmed by sales ({observed_metrics.observed_sold_quantity} units)")
                reasons.append("Strong historical sales volume")
            elif observed_metrics.observed_sold_quantity >= 10:
                demand_intensity = "MEDIUM"
                demand_score = Decimal("25.0")
                scoring_rationale.append(f"Moderate demand ({observed_metrics.observed_sold_quantity} units)")
            else:
                demand_intensity = "LOW"
                demand_score = Decimal("10.0")
                scoring_rationale.append(f"Low demand ({observed_metrics.observed_sold_quantity} units)")
        else:
            scoring_rationale.append("Demand data UNKNOWN (sales quantity not observed)")

        # 3. Evaluación de Competencia
        competition_density = "UNKNOWN"
        if observed_metrics.observed_competitor_count is not None:
            if observed_metrics.observed_competitor_count <= 2:
                competition_density = "LOW"
                comp_score = Decimal("25.0")
                scoring_rationale.append(f"Low competition density ({observed_metrics.observed_competitor_count} competitors)")
                reasons.append("Low competitor density in marketplace")
            elif observed_metrics.observed_competitor_count <= 10:
                competition_density = "MEDIUM"
                comp_score = Decimal("15.0")
                scoring_rationale.append(f"Moderate competition ({observed_metrics.observed_competitor_count} competitors)")
            else:
                competition_density = "HIGH"
                comp_score = Decimal("5.0")
                scoring_rationale.append(f"High competition ({observed_metrics.observed_competitor_count} competitors)")
        else:
            scoring_rationale.append("Competition density UNKNOWN (competitor count not observed)")

        # Sumar total determinista (0.00 - 100.00)
        total_score = (demand_score + price_gap_score + comp_score).quantize(Decimal("0.01"))

        derived_metrics = DerivedOpportunityMetrics(
            price_gap_amount=price_gap_amount,
            price_gap_ratio=price_gap_ratio,
            potential_margin_ratio=price_gap_ratio if price_gap_ratio and price_gap_ratio > Decimal("0") else None,
            competition_density=competition_density,
            demand_intensity=demand_intensity,
            opportunity_score=total_score,
            scoring_rationale=tuple(scoring_rationale),
        )

        # 4. Clasificación del tipo de oportunidad
        opp_type = OpportunityType.GENERAL_COMMERCIAL
        if price_gap_ratio and price_gap_ratio >= Decimal("0.15"):
            opp_type = OpportunityType.PRICE_ARBITRAGE
        elif demand_intensity == "HIGH" and competition_density == "LOW":
            opp_type = OpportunityType.HIGH_DEMAND_LOW_COMPETITION
        elif observed_metrics.observed_stock == 0 and demand_intensity in ("HIGH", "MEDIUM"):
            opp_type = OpportunityType.SUPPLY_SHORTAGE

        # 5. Determinación de Confidence y Status
        confidence = latest_obs.confidence
        if len(unknown_fields) >= 2:
            confidence = Confidence.LOW
        elif "price" in unknown_fields or "sold_quantity" in unknown_fields:
            if confidence == Confidence.HIGH:
                confidence = Confidence.MEDIUM

        # Evaluar estado final
        if criteria.require_valid_price and observed_metrics.observed_price is None:
            opp_status = OpportunityStatus.INSUFFICIENT_DATA
        elif "sold_quantity" in unknown_fields or "competitor_count" in unknown_fields:
            # Si faltan datos clave para evaluar plenamente la oportunidad comercial,
            # clasificar según score alcanzado o estado UNKNOWN / INSUFFICIENT_DATA
            if total_score >= criteria.min_score:
                opp_status = OpportunityStatus.VALID
            else:
                opp_status = OpportunityStatus.UNKNOWN
        elif total_score >= criteria.min_score:
            opp_status = OpportunityStatus.VALID
        else:
            opp_status = OpportunityStatus.DISCARDED

        return derived_metrics, opp_type, opp_status, reasons, confidence

    def _build_insufficient_data_record(
        self,
        entity_id: str,
        observations: List[MarketObservation],
        reason: str,
        correlation_id: str,
    ) -> OpportunityRecord:
        latest = max(observations, key=lambda o: o.observed_at)
        obs_ids = tuple(sorted([o.observation_id for o in observations]))
        idempotency_raw = f"{entity_id}_{latest.marketplace.value}_{'_'.join(obs_ids)}"
        idempotency_key = hashlib.sha256(idempotency_raw.encode("utf-8")).hexdigest()

        return OpportunityRecord(
            opportunity_id=f"opp-insufficient-{idempotency_key[:16]}",
            canonical_product_id=entity_id,
            marketplace=latest.marketplace,
            detected_at=latest.observed_at,
            opportunity_type=OpportunityType.GENERAL_COMMERCIAL,
            status=OpportunityStatus.INSUFFICIENT_DATA,
            confidence=Confidence.UNKNOWN,
            source_observation_ids=obs_ids,
            observed_metrics=ObservedOpportunityMetrics(
                observed_price=latest.price,
                observed_sold_quantity=latest.sold_quantity,
                observed_stock=latest.stock,
                observations_count=len(observations),
            ),
            derived_metrics=DerivedOpportunityMetrics(
                opportunity_score=Decimal("0.00"),
                scoring_rationale=(reason,),
            ),
            category=latest.category,
            title=latest.title,
            product_sku=latest.product_sku,
            provenance=latest.provenance,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            reasons=(reason,),
            unknown_fields=("insufficient_observations",),
            metadata={"source": latest.source},
        )
