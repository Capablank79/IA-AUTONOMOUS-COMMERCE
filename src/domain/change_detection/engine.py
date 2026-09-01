"""
Motor de dominio para Detección de Cambios (Change Detection Engine - Hito J.4).

Implementa la comparación determinista de observaciones de mercado y oportunidades
respetando:
- Orden temporal estricto (T0 < T1).
- Separación ontológica entre valores observados y deltas derivados.
- UNKNOWN safety: UNKNOWN != 0, UNKNOWN != NO_CHANGE.
- Determinismo absoluto sin ML / LLM ni heurísticas no reproducibles.
- No side effects (no decisiones, no alertas, no eventos).
"""

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, List, Tuple, Dict, Any

from src.domain.change_detection.models import (
    ChangeRecord,
    ChangeSubjectType,
    ChangeType,
    ChangeSignificance,
    ObservedChangeField,
    DerivedChangeDelta,
)
from src.domain.change_detection.ports import ChangeDetectionEnginePort
from src.domain.market_monitoring.models import (
    MarketObservation,
    ObservationStatus,
    NormalizedPrice,
)
from src.domain.opportunity_detection.models import (
    OpportunityRecord,
    OpportunityStatus,
)
from src.domain.market_intelligence.models import Confidence


class TemporalOrderViolationError(ValueError):
    """Se lanza cuando T0 >= T1 en una comparación temporal."""
    pass


class InvalidSubjectComparisonError(ValueError):
    """Se lanza cuando se intentan comparar entidades de distinto identificador o tipo."""
    pass


class ChangeDetectionEngine(ChangeDetectionEnginePort):
    """
    Motor determinista de detección de cambios de dominio.
    """

    def compare_observations(
        self,
        previous_observation: Optional[MarketObservation],
        current_observation: MarketObservation,
        correlation_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ChangeRecord:
        """
        Compara dos MarketObservation.
        Si previous_observation es None, representa la observación inicial de la entidad.
        """
        if not isinstance(current_observation, MarketObservation):
            raise TypeError("current_observation must be an instance of MarketObservation")

        corr_id = correlation_id or current_observation.correlation_id or f"corr-change-{uuid.uuid4().hex[:12]}"

        if previous_observation is None:
            # Observación inicial (Baseline inicial sin previo)
            return self._create_initial_observation_record(current_observation, corr_id)

        if not isinstance(previous_observation, MarketObservation):
            raise TypeError("previous_observation must be an instance of MarketObservation or None")

        if previous_observation.entity_id != current_observation.entity_id:
            raise InvalidSubjectComparisonError(
                f"Cannot compare observations with different entity_ids: '{previous_observation.entity_id}' vs '{current_observation.entity_id}'"
            )

        # Validación temporal estricta
        if previous_observation.observed_at > current_observation.observed_at:
            raise TemporalOrderViolationError(
                f"Out-of-order temporal comparison: previous observed_at ({previous_observation.observed_at.isoformat()}) "
                f"> current observed_at ({current_observation.observed_at.isoformat()})"
            )

        if previous_observation.observed_at == current_observation.observed_at:
            if previous_observation.observation_id == current_observation.observation_id:
                # Mismo registro re-evaluado -> NO_CHANGE determinista
                return self._create_no_change_record(
                    subject_type=ChangeSubjectType.MARKET_OBSERVATION,
                    subject_id=current_observation.entity_id,
                    prev_ref=previous_observation.observation_id,
                    curr_ref=current_observation.observation_id,
                    observed_from=previous_observation.observed_at,
                    observed_to=current_observation.observed_at,
                    correlation_id=corr_id,
                )
            else:
                # Mismo timestamp pero IDs diferentes: rechazo determinista de comparación temporal ambigua
                raise TemporalOrderViolationError(
                    f"Ambiguous temporal comparison: distinct observations with identical observed_at timestamp ({current_observation.observed_at.isoformat()})"
                )

        # Comparar campos de MarketObservation
        changed_fields: List[str] = []
        observed_changes: List[ObservedChangeField] = []
        derived_deltas: List[DerivedChangeDelta] = []
        unknown_fields: List[str] = []

        # 1. Source Status / Error Status
        status_changed, obs_status_change = self._compare_categorical_field(
            field_name="status",
            prev_val=previous_observation.status.value if previous_observation.status else None,
            curr_val=current_observation.status.value if current_observation.status else None,
        )
        if status_changed:
            changed_fields.append("status")
            observed_changes.append(obs_status_change)

        # Si una de las observaciones es SOURCE_FAILURE o TIMEOUT, se maneja de forma segura
        is_curr_failure = current_observation.status in (ObservationStatus.SOURCE_FAILURE, ObservationStatus.TIMEOUT)
        is_prev_failure = previous_observation.status in (ObservationStatus.SOURCE_FAILURE, ObservationStatus.TIMEOUT)

        if is_curr_failure or is_prev_failure:
            # Si el fallo de fuente es lo que cambió, el tipo de cambio es SOURCE_STATUS_CHANGED o UNKNOWN_TRANSITION
            change_type = ChangeType.SOURCE_STATUS_CHANGED if status_changed else ChangeType.NO_CHANGE
            significance = ChangeSignificance.MODERATE if status_changed else ChangeSignificance.NONE
            return self._build_change_record(
                subject_type=ChangeSubjectType.MARKET_OBSERVATION,
                subject_id=current_observation.entity_id,
                prev_ref=previous_observation.observation_id,
                curr_ref=current_observation.observation_id,
                change_type=change_type,
                observed_from=previous_observation.observed_at,
                observed_to=current_observation.observed_at,
                changed_fields=tuple(changed_fields),
                observed_changes=tuple(observed_changes),
                derived_deltas=tuple(derived_deltas),
                significance=significance,
                confidence=Confidence.LOW if is_curr_failure else current_observation.confidence,
                correlation_id=corr_id,
                evidence_references=(previous_observation.observation_id, current_observation.observation_id),
                unknown_fields=("market_metrics_due_to_source_failure",) if is_curr_failure else (),
            )

        # 2. Price Comparison
        price_changed, obs_price_change, price_delta = self._compare_price(
            previous_observation.price,
            current_observation.price,
        )
        if price_changed:
            changed_fields.append("price")
            observed_changes.append(obs_price_change)
            if price_delta:
                derived_deltas.append(price_delta)
        if current_observation.price is None or previous_observation.price is None:
            if obs_price_change.is_previous_unknown or obs_price_change.is_current_unknown:
                unknown_fields.append("price")

        # 3. Stock Comparison
        stock_changed, obs_stock_change, stock_delta = self._compare_integer_field(
            field_name="stock",
            prev_val=previous_observation.stock,
            curr_val=current_observation.stock,
        )
        if stock_changed:
            changed_fields.append("stock")
            observed_changes.append(obs_stock_change)
            if stock_delta:
                derived_deltas.append(stock_delta)
        if obs_stock_change.is_previous_unknown or obs_stock_change.is_current_unknown:
            unknown_fields.append("stock")

        # 4. Sold Quantity Comparison
        sq_changed, obs_sq_change, sq_delta = self._compare_integer_field(
            field_name="sold_quantity",
            prev_val=previous_observation.sold_quantity,
            curr_val=current_observation.sold_quantity,
        )
        if sq_changed:
            changed_fields.append("sold_quantity")
            observed_changes.append(obs_sq_change)
            if sq_delta:
                derived_deltas.append(sq_delta)
        if obs_sq_change.is_previous_unknown or obs_sq_change.is_current_unknown:
            unknown_fields.append("sold_quantity")

        # 5. Availability Comparison
        avail_changed, obs_avail_change = self._compare_categorical_field(
            field_name="availability",
            prev_val=previous_observation.availability,
            curr_val=current_observation.availability,
        )
        if avail_changed:
            changed_fields.append("availability")
            observed_changes.append(obs_avail_change)

        # 6. Competition Comparison
        comp_changed, obs_comp_change, comp_delta = self._compare_competition(
            previous_observation.competition_info,
            current_observation.competition_info,
        )
        if comp_changed:
            changed_fields.append("competition")
            observed_changes.append(obs_comp_change)
            if comp_delta:
                derived_deltas.append(comp_delta)

        # 7. Seller Comparison
        seller_changed, obs_seller_change = self._compare_seller(
            previous_observation.seller_info,
            current_observation.seller_info,
        )
        if seller_changed:
            changed_fields.append("seller")
            observed_changes.append(obs_seller_change)

        # Determinar ChangeType principal y Significancia
        primary_change_type = self._determine_observation_change_type(changed_fields)
        significance = self._evaluate_observation_significance(
            changed_fields=changed_fields,
            derived_deltas=derived_deltas,
            observed_changes=observed_changes,
        )

        return self._build_change_record(
            subject_type=ChangeSubjectType.MARKET_OBSERVATION,
            subject_id=current_observation.entity_id,
            prev_ref=previous_observation.observation_id,
            curr_ref=current_observation.observation_id,
            change_type=primary_change_type,
            observed_from=previous_observation.observed_at,
            observed_to=current_observation.observed_at,
            changed_fields=tuple(changed_fields),
            observed_changes=tuple(observed_changes),
            derived_deltas=tuple(derived_deltas),
            significance=significance,
            confidence=current_observation.confidence,
            correlation_id=corr_id,
            evidence_references=(previous_observation.observation_id, current_observation.observation_id),
            unknown_fields=tuple(sorted(list(set(unknown_fields)))),
            metadata=metadata or {},
        )

    def compare_opportunities(
        self,
        previous_opportunity: Optional[OpportunityRecord],
        current_opportunity: OpportunityRecord,
        correlation_id: Optional[str] = None,
    ) -> ChangeRecord:
        """
        Compara dos OpportunityRecord.
        Si previous_opportunity es None, representa la primera detección de la oportunidad.
        """
        if not isinstance(current_opportunity, OpportunityRecord):
            raise TypeError("current_opportunity must be an instance of OpportunityRecord")

        corr_id = correlation_id or current_opportunity.correlation_id or f"corr-change-opp-{uuid.uuid4().hex[:12]}"

        if previous_opportunity is None:
            return self._create_initial_opportunity_record(current_opportunity, corr_id)

        if not isinstance(previous_opportunity, OpportunityRecord):
            raise TypeError("previous_opportunity must be an instance of OpportunityRecord or None")

        if previous_opportunity.canonical_product_id != current_opportunity.canonical_product_id:
            raise InvalidSubjectComparisonError(
                f"Cannot compare opportunities with different canonical_product_ids: '{previous_opportunity.canonical_product_id}' vs '{current_opportunity.canonical_product_id}'"
            )

        # Validación temporal estricta
        if previous_opportunity.detected_at > current_opportunity.detected_at:
            raise TemporalOrderViolationError(
                f"Out-of-order temporal comparison: previous detected_at ({previous_opportunity.detected_at.isoformat()}) "
                f"> current detected_at ({current_opportunity.detected_at.isoformat()})"
            )

        if previous_opportunity.detected_at == current_opportunity.detected_at:
            if previous_opportunity.opportunity_id == current_opportunity.opportunity_id:
                return self._create_no_change_record(
                    subject_type=ChangeSubjectType.OPPORTUNITY,
                    subject_id=current_opportunity.canonical_product_id,
                    prev_ref=previous_opportunity.opportunity_id,
                    curr_ref=current_opportunity.opportunity_id,
                    observed_from=previous_opportunity.detected_at,
                    observed_to=current_opportunity.detected_at,
                    correlation_id=corr_id,
                )
            else:
                raise TemporalOrderViolationError(
                    f"Ambiguous temporal comparison: distinct opportunities with identical detected_at timestamp ({current_opportunity.detected_at.isoformat()})"
                )

        changed_fields: List[str] = []
        observed_changes: List[ObservedChangeField] = []
        derived_deltas: List[DerivedChangeDelta] = []
        unknown_fields: List[str] = []

        # 1. Opportunity Status
        status_changed, obs_status_change = self._compare_categorical_field(
            field_name="status",
            prev_val=previous_opportunity.status.value,
            curr_val=current_opportunity.status.value,
        )
        if status_changed:
            changed_fields.append("status")
            observed_changes.append(obs_status_change)

        # 2. Opportunity Type
        type_changed, obs_type_change = self._compare_categorical_field(
            field_name="opportunity_type",
            prev_val=previous_opportunity.opportunity_type.value,
            curr_val=current_opportunity.opportunity_type.value,
        )
        if type_changed:
            changed_fields.append("opportunity_type")
            observed_changes.append(obs_type_change)

        # 3. Opportunity Score
        prev_score = previous_opportunity.derived_metrics.opportunity_score
        curr_score = current_opportunity.derived_metrics.opportunity_score
        score_changed, obs_score_change, score_delta = self._compare_decimal_field(
            field_name="opportunity_score",
            prev_val=prev_score,
            curr_val=curr_score,
        )
        if score_changed:
            changed_fields.append("opportunity_score")
            observed_changes.append(obs_score_change)
            if score_delta:
                derived_deltas.append(score_delta)
        if obs_score_change.is_previous_unknown or obs_score_change.is_current_unknown:
            unknown_fields.append("opportunity_score")

        # 4. Confidence
        conf_changed, obs_conf_change = self._compare_categorical_field(
            field_name="confidence",
            prev_val=previous_opportunity.confidence.value,
            curr_val=current_opportunity.confidence.value,
        )
        if conf_changed:
            changed_fields.append("confidence")
            observed_changes.append(obs_conf_change)

        # Determinar ChangeType
        if not changed_fields:
            primary_change_type = ChangeType.NO_CHANGE
            significance = ChangeSignificance.NONE
        elif "status" in changed_fields:
            primary_change_type = ChangeType.OPPORTUNITY_STATUS_CHANGED
            significance = ChangeSignificance.SIGNIFICANT
        elif "opportunity_score" in changed_fields:
            primary_change_type = ChangeType.OPPORTUNITY_SCORE_CHANGED
            # Score delta >= 15 pts es significativo
            significance = ChangeSignificance.MODERATE
            if score_delta and score_delta.numeric_delta is not None:
                if abs(score_delta.numeric_delta) >= Decimal("15.0"):
                    significance = ChangeSignificance.SIGNIFICANT
        else:
            primary_change_type = ChangeType.OPPORTUNITY_METRICS_CHANGED
            significance = ChangeSignificance.MODERATE

        evidence_refs = tuple(
            list(previous_opportunity.source_observation_ids) + list(current_opportunity.source_observation_ids)
        )

        return self._build_change_record(
            subject_type=ChangeSubjectType.OPPORTUNITY,
            subject_id=current_opportunity.canonical_product_id,
            prev_ref=previous_opportunity.opportunity_id,
            curr_ref=current_opportunity.opportunity_id,
            change_type=primary_change_type,
            observed_from=previous_opportunity.detected_at,
            observed_to=current_opportunity.detected_at,
            changed_fields=tuple(changed_fields),
            observed_changes=tuple(observed_changes),
            derived_deltas=tuple(derived_deltas),
            significance=significance,
            confidence=current_opportunity.confidence,
            correlation_id=corr_id,
            evidence_references=evidence_refs,
            unknown_fields=tuple(sorted(list(set(unknown_fields)))),
        )

    # --- Métodos auxiliares de comparación de campos ---

    def _compare_price(
        self,
        prev_price: Optional[NormalizedPrice],
        curr_price: Optional[NormalizedPrice],
    ) -> Tuple[bool, ObservedChangeField, Optional[DerivedChangeDelta]]:
        """Compara precio observado asegurando manejo de UNKNOWN y cálculo determinista de deltas."""
        is_prev_unk = prev_price is None
        is_curr_unk = curr_price is None

        prev_val = f"{prev_price.amount} {prev_price.currency}" if prev_price else None
        curr_val = f"{curr_price.amount} {curr_price.currency}" if curr_price else None

        if is_prev_unk and is_curr_unk:
            return (
                False,
                ObservedChangeField("price", None, None, is_previous_unknown=True, is_current_unknown=True),
                None,
            )

        if is_prev_unk or is_curr_unk:
            # Transición hacia/desde UNKNOWN: no fabricamos delta numérico
            return (
                True,
                ObservedChangeField("price", prev_val, curr_val, is_previous_unknown=is_prev_unk, is_current_unknown=is_curr_unk),
                DerivedChangeDelta(
                    field_name="price",
                    numeric_delta=None,
                    percentage_delta=None,
                    delta_description="Price transitioned to or from UNKNOWN state (no numeric delta fabricated)",
                    is_valid_delta=False,
                ),
            )

        # Ambos son conocidos
        if prev_price.currency != curr_price.currency:
            # Distinta divisa: no fabricamos delta directo sin FX
            return (
                True,
                ObservedChangeField("price", prev_val, curr_val),
                DerivedChangeDelta(
                    field_name="price",
                    numeric_delta=None,
                    percentage_delta=None,
                    delta_description=f"Currency mismatch ({prev_price.currency} vs {curr_price.currency}); direct delta omitted",
                    is_valid_delta=False,
                ),
            )

        if prev_price.amount == curr_price.amount:
            return (
                False,
                ObservedChangeField("price", prev_val, curr_val),
                None,
            )

        # Delta numérico y porcentual válido
        num_delta = curr_price.amount - prev_price.amount
        pct_delta = None
        if prev_price.amount != Decimal("0"):
            pct_delta = ((num_delta / prev_price.amount) * Decimal("100")).quantize(Decimal("0.01"))

        return (
            True,
            ObservedChangeField("price", prev_val, curr_val),
            DerivedChangeDelta(
                field_name="price",
                numeric_delta=num_delta,
                percentage_delta=pct_delta,
                delta_description=f"Price changed by {num_delta} {curr_price.currency} ({pct_delta}% if calculable)",
                is_valid_delta=True,
            ),
        )

    def _compare_integer_field(
        self,
        field_name: str,
        prev_val: Optional[int],
        curr_val: Optional[int],
    ) -> Tuple[bool, ObservedChangeField, Optional[DerivedChangeDelta]]:
        """Compara un campo entero observado (stock, sold_quantity)."""
        is_prev_unk = prev_val is None
        is_curr_unk = curr_val is None

        if is_prev_unk and is_curr_unk:
            return (
                False,
                ObservedChangeField(field_name, None, None, is_previous_unknown=True, is_current_unknown=True),
                None,
            )

        if is_prev_unk or is_curr_unk:
            return (
                True,
                ObservedChangeField(field_name, prev_val, curr_val, is_previous_unknown=is_prev_unk, is_current_unknown=is_curr_unk),
                DerivedChangeDelta(
                    field_name=field_name,
                    numeric_delta=None,
                    percentage_delta=None,
                    delta_description=f"{field_name} transitioned to or from UNKNOWN state",
                    is_valid_delta=False,
                ),
            )

        if prev_val == curr_val:
            return (
                False,
                ObservedChangeField(field_name, prev_val, curr_val),
                None,
            )

        num_delta = Decimal(str(curr_val - prev_val))
        pct_delta = None
        if prev_val != 0:
            pct_delta = ((num_delta / Decimal(str(prev_val))) * Decimal("100")).quantize(Decimal("0.01"))

        return (
            True,
            ObservedChangeField(field_name, prev_val, curr_val),
            DerivedChangeDelta(
                field_name=field_name,
                numeric_delta=num_delta,
                percentage_delta=pct_delta,
                delta_description=f"{field_name} changed by {num_delta}",
                is_valid_delta=True,
            ),
        )

    def _compare_decimal_field(
        self,
        field_name: str,
        prev_val: Optional[Decimal],
        curr_val: Optional[Decimal],
    ) -> Tuple[bool, ObservedChangeField, Optional[DerivedChangeDelta]]:
        """Compara un campo Decimal (ej. opportunity_score)."""
        is_prev_unk = prev_val is None
        is_curr_unk = curr_val is None

        if is_prev_unk and is_curr_unk:
            return (
                False,
                ObservedChangeField(field_name, None, None, is_previous_unknown=True, is_current_unknown=True),
                None,
            )

        if is_prev_unk or is_curr_unk:
            return (
                True,
                ObservedChangeField(field_name, str(prev_val) if prev_val else None, str(curr_val) if curr_val else None, is_previous_unknown=is_prev_unk, is_current_unknown=is_curr_unk),
                DerivedChangeDelta(
                    field_name=field_name,
                    numeric_delta=None,
                    percentage_delta=None,
                    delta_description=f"{field_name} transitioned to or from UNKNOWN state",
                    is_valid_delta=False,
                ),
            )

        if prev_val == curr_val:
            return (
                False,
                ObservedChangeField(field_name, str(prev_val), str(curr_val)),
                None,
            )

        num_delta = curr_val - prev_val
        pct_delta = None
        if prev_val != Decimal("0"):
            pct_delta = ((num_delta / prev_val) * Decimal("100")).quantize(Decimal("0.01"))

        return (
            True,
            ObservedChangeField(field_name, str(prev_val), str(curr_val)),
            DerivedChangeDelta(
                field_name=field_name,
                numeric_delta=num_delta,
                percentage_delta=pct_delta,
                delta_description=f"{field_name} changed by {num_delta}",
                is_valid_delta=True,
            ),
        )

    def _compare_categorical_field(
        self,
        field_name: str,
        prev_val: Optional[str],
        curr_val: Optional[str],
    ) -> Tuple[bool, ObservedChangeField]:
        """Compara campos categóricos / de estado."""
        is_prev_unk = prev_val is None
        is_curr_unk = curr_val is None

        if is_prev_unk and is_curr_unk:
            return False, ObservedChangeField(field_name, None, None, is_previous_unknown=True, is_current_unknown=True)

        if prev_val == curr_val:
            return False, ObservedChangeField(field_name, prev_val, curr_val)

        return True, ObservedChangeField(
            field_name=field_name,
            previous_value=prev_val,
            current_value=curr_val,
            is_previous_unknown=is_prev_unk,
            is_current_unknown=is_curr_unk,
        )

    def _compare_competition(
        self,
        prev_comp: Any,
        curr_comp: Any,
    ) -> Tuple[bool, ObservedChangeField, Optional[DerivedChangeDelta]]:
        """Compara información observada de competencia."""
        prev_cnt = prev_comp.total_competitors if prev_comp else None
        curr_cnt = curr_comp.total_competitors if curr_comp else None

        if prev_cnt == curr_cnt:
            return False, ObservedChangeField("competition", prev_cnt, curr_cnt), None

        return self._compare_integer_field("competition_total_competitors", prev_cnt, curr_cnt)

    def _compare_seller(
        self,
        prev_seller: Any,
        curr_seller: Any,
    ) -> Tuple[bool, ObservedChangeField]:
        """Compara información observada de vendedor."""
        prev_id = prev_seller.seller_id if prev_seller else None
        curr_id = curr_seller.seller_id if curr_seller else None

        if prev_id == curr_id:
            return False, ObservedChangeField("seller", prev_id, curr_id)

        return True, ObservedChangeField(
            field_name="seller",
            previous_value=prev_id,
            current_value=curr_id,
            is_previous_unknown=prev_id is None,
            is_current_unknown=curr_id is None,
        )

    def _determine_observation_change_type(self, changed_fields: List[str]) -> ChangeType:
        """Determina el ChangeType principal a partir de los campos modificados."""
        if not changed_fields:
            return ChangeType.NO_CHANGE
        if len(changed_fields) > 1:
            return ChangeType.MULTIPLE_CHANGES

        field = changed_fields[0]
        if field == "price":
            return ChangeType.PRICE_CHANGED
        elif field == "stock":
            return ChangeType.STOCK_CHANGED
        elif field == "sold_quantity":
            return ChangeType.SOLD_QUANTITY_CHANGED
        elif field == "availability":
            return ChangeType.AVAILABILITY_CHANGED
        elif field == "competition":
            return ChangeType.COMPETITION_CHANGED
        elif field == "seller":
            return ChangeType.SELLER_CHANGED
        elif field == "status":
            return ChangeType.SOURCE_STATUS_CHANGED
        return ChangeType.MULTIPLE_CHANGES

    def _evaluate_observation_significance(
        self,
        changed_fields: List[str],
        derived_deltas: List[DerivedChangeDelta],
        observed_changes: List[ObservedChangeField],
    ) -> ChangeSignificance:
        """
        Evalúa determinísticamente la significancia del cambio usando reglas de negocio explícitas:
        - NO_CHANGE -> NONE
        - Stock a 0 o Disponibilidad a OUT_OF_STOCK -> CRITICAL
        - Precio delta >= 10% -> SIGNIFICANT
        - Precio delta < 10% -> MODERATE / NEGLIGIBLE
        - Stock delta moderado -> MODERATE
        """
        if not changed_fields:
            return ChangeSignificance.NONE

        for obs in observed_changes:
            if obs.field_name == "stock" and obs.current_value == 0:
                return ChangeSignificance.CRITICAL
            if obs.field_name == "availability" and str(obs.current_value).upper() in ("OUT_OF_STOCK", "PAUSED", "INACTIVE"):
                return ChangeSignificance.CRITICAL

        for delta in derived_deltas:
            if delta.field_name == "price" and delta.percentage_delta is not None:
                if abs(delta.percentage_delta) >= Decimal("10.0"):
                    return ChangeSignificance.SIGNIFICANT
                if abs(delta.percentage_delta) >= Decimal("2.0"):
                    return ChangeSignificance.MODERATE
                return ChangeSignificance.NEGLIGIBLE

        if "price" in changed_fields or "stock" in changed_fields or "availability" in changed_fields:
            return ChangeSignificance.MODERATE

        return ChangeSignificance.NEGLIGIBLE

    def _create_initial_observation_record(
        self,
        obs: MarketObservation,
        correlation_id: str,
    ) -> ChangeRecord:
        """Crea el ChangeRecord para una primera observación (Baseline inicial)."""
        return self._build_change_record(
            subject_type=ChangeSubjectType.MARKET_OBSERVATION,
            subject_id=obs.entity_id,
            prev_ref=None,
            curr_ref=obs.observation_id,
            change_type=ChangeType.NO_CHANGE,
            observed_from=None,
            observed_to=obs.observed_at,
            changed_fields=(),
            observed_changes=(),
            derived_deltas=(),
            significance=ChangeSignificance.NONE,
            confidence=obs.confidence,
            correlation_id=correlation_id,
            evidence_references=(obs.observation_id,),
            metadata={"description": "Initial observation baseline established"},
        )

    def _create_initial_opportunity_record(
        self,
        opp: OpportunityRecord,
        correlation_id: str,
    ) -> ChangeRecord:
        """Crea el ChangeRecord para una primera oportunidad (Baseline inicial)."""
        return self._build_change_record(
            subject_type=ChangeSubjectType.OPPORTUNITY,
            subject_id=opp.canonical_product_id,
            prev_ref=None,
            curr_ref=opp.opportunity_id,
            change_type=ChangeType.NO_CHANGE,
            observed_from=None,
            observed_to=opp.detected_at,
            changed_fields=(),
            observed_changes=(),
            derived_deltas=(),
            significance=ChangeSignificance.NONE,
            confidence=opp.confidence,
            correlation_id=correlation_id,
            evidence_references=opp.source_observation_ids,
            metadata={"description": "Initial opportunity baseline established"},
        )

    def _create_no_change_record(
        self,
        subject_type: ChangeSubjectType,
        subject_id: str,
        prev_ref: str,
        curr_ref: str,
        observed_from: datetime,
        observed_to: datetime,
        correlation_id: str,
    ) -> ChangeRecord:
        """Crea un ChangeRecord explícito de NO_CHANGE."""
        return self._build_change_record(
            subject_type=subject_type,
            subject_id=subject_id,
            prev_ref=prev_ref,
            curr_ref=curr_ref,
            change_type=ChangeType.NO_CHANGE,
            observed_from=observed_from,
            observed_to=observed_to,
            changed_fields=(),
            observed_changes=(),
            derived_deltas=(),
            significance=ChangeSignificance.NONE,
            confidence=Confidence.HIGH,
            correlation_id=correlation_id,
            evidence_references=(prev_ref, curr_ref) if prev_ref != curr_ref else (curr_ref,),
            metadata={"description": "Replay or identical state observed; no change detected"},
        )

    def _build_change_record(
        self,
        subject_type: ChangeSubjectType,
        subject_id: str,
        prev_ref: Optional[str],
        curr_ref: str,
        change_type: ChangeType,
        observed_from: Optional[datetime],
        observed_to: datetime,
        changed_fields: Tuple[str, ...],
        observed_changes: Tuple[ObservedChangeField, ...],
        derived_deltas: Tuple[DerivedChangeDelta, ...],
        significance: ChangeSignificance,
        confidence: Confidence,
        correlation_id: str,
        evidence_references: Tuple[str, ...],
        unknown_fields: Tuple[str, ...] = (),
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ChangeRecord:
        """Construye un ChangeRecord inmutable."""
        change_id = f"chg-{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc)
        return ChangeRecord(
            change_id=change_id,
            subject_type=subject_type,
            subject_id=subject_id,
            previous_reference=prev_ref,
            current_reference=curr_ref,
            change_type=change_type,
            detected_at=now,
            observed_from=observed_from,
            observed_to=observed_to,
            changed_fields=changed_fields,
            observed_changes=observed_changes,
            derived_deltas=derived_deltas,
            significance=significance,
            confidence=confidence,
            provenance="DERIVED",
            correlation_id=correlation_id,
            evidence_references=evidence_references,
            unknown_fields=unknown_fields,
            metadata=metadata or {},
        )
