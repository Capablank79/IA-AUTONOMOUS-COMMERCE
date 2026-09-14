"""
Servicio de Aplicación para Q.1 — Opportunity Dashboard (Hito Q — Business Intelligence).

Responsabilidades:
1. Validar autenticación de sesión SaaS (O.3) y permisos RBAC / SaaS Authorization (O.4 / N.4).
   Acción canónica: OPPORTUNITY_READ / BUSINESS_INTELLIGENCE_READ.
2. Garantizar aislamiento multi-tenant estricto (O.1): Tenant A jamás ve datos de Tenant B.
3. Consultar y proyectar datos de oportunidades existentes desde el repositorio durable.
4. Aplicar filtros combinados (marketplace, categoría, tipo, estado, score min/max, margen min, fechas, búsqueda).
5. Aplicar ordenación determinista con regla de desempate estable.
6. Aplicar paginación segura y acotada (page, page_size <= 100).
7. Proyectar agregados de resumen estadístico real (OpportunityDashboardSummary).
8. Proyectar detalle seguro y explicabilidad estructurada (OpportunityDashboardDetail).
9. Proveer comparación multidimensional determinista entre 2 o más oportunidades (OpportunityComparisonView).
10. Preservar semántica de incertidumbre: UNKNOWN != 0 y cantidades monetarias en Decimal.
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, List, Dict, Any, Tuple, Sequence
import math

from src.domain.tenant.models import TenantContext
from src.domain.tenant.guard import CrossTenantGuard
from src.domain.security.models import validate_safe_identifier
from src.domain.market_intelligence.models import Marketplace, Confidence
from src.domain.opportunity_detection.models import (
    OpportunityRecord,
    OpportunityType,
    OpportunityStatus,
)
from src.domain.opportunity_dashboard.models import (
    OpportunitySortField,
    SortOrder,
    OpportunityDashboardItem,
    OpportunityDashboardSummary,
    OpportunityDashboardDetail,
    OpportunityDashboardQuery,
    OpportunityDashboardPage,
    OpportunityComparisonView,
)
from src.domain.opportunity_dashboard.ports import TenantOpportunityRepositoryPort
from src.domain.opportunity.models import (
    OpportunityExplanation,
    OpportunityComparisonDimension,
    OpportunityComparisonResult,
)
from src.domain.opportunity.engine import OpportunityEngine
from src.domain.saas_authorization.models import (
    SaaSAuthorizationRequest,
    SaaSAuthorizationStatus,
    SaaSAuthorizationReasonCode,
    SaaSAuthorizationDeniedError,
    SaaSAuthorizationSecurityViolationError,
)
from src.application.saas_authorization.saas_authorization_service import SaaSAuthorizationService
from src.domain.session.ports import SaaSSessionRepositoryPort
from src.domain.session.models import SessionStatus


from src.domain.admin_console.models import (
    AdminAuthenticationError,
    AdminAuthorizationError,
    AdminResourceNotFoundError,
    AdminInvalidRequestError,
)
from src.domain.reliability.ports import ClockPort
from src.infrastructure.reliability.reliability_infrastructure import SystemClock


class OpportunityDashboardAuthenticationError(AdminAuthenticationError):
    """Lanzada cuando la sesión es inválida, expirada o ausente."""
    pass


class OpportunityDashboardAuthorizationError(AdminAuthorizationError):
    """Lanzada cuando el usuario o tenant no tiene permisos para ver el dashboard."""
    pass


class OpportunityNotFoundError(AdminResourceNotFoundError):
    """Lanzada cuando la oportunidad no existe en el tenant especificado."""
    pass


class OpportunityDashboardService:
    """
    Servicio de Aplicación para el Opportunity Dashboard (Q.1).
    """

    def __init__(
        self,
        repository: TenantOpportunityRepositoryPort,
        authorization_service: Optional[SaaSAuthorizationService] = None,
        session_repository: Optional[SaaSSessionRepositoryPort] = None,
        clock: Optional[ClockPort] = None,
    ):
        self.repository = repository
        self.authorization_service = authorization_service
        self.session_repository = session_repository
        self.clock = clock or SystemClock()

    def _authorize(
        self,
        tenant_id: str,
        session_id: Optional[str] = None,
        action: str = "OPPORTUNITY_DASHBOARD_READ",
    ) -> TenantContext:
        """
        Valida la sesión y los permisos de autorización para el tenant dado.
        Retorna el TenantContext validado.
        """
        validate_safe_identifier(tenant_id, field_name="tenant_id")

        if self.authorization_service is not None:
            if not session_id:
                raise OpportunityDashboardAuthenticationError("Missing session identifier.")

            req = SaaSAuthorizationRequest(
                action=action,
                session_id=session_id,
                tenant_id=tenant_id,
            )
            decision = self.authorization_service.authorize(req)
            if not decision.is_allowed:
                auth_reasons = {
                    SaaSAuthorizationReasonCode.SESSION_INVALID,
                    SaaSAuthorizationReasonCode.SESSION_NOT_FOUND,
                    SaaSAuthorizationReasonCode.SESSION_EXPIRED,
                    SaaSAuthorizationReasonCode.SESSION_REVOKED,
                }
                if decision.reason_code in auth_reasons:
                    raise OpportunityDashboardAuthenticationError(
                        f"Authentication failed: {decision.reason_code.value}"
                    )
                raise OpportunityDashboardAuthorizationError(
                    f"Authorization denied for action {action} on tenant {tenant_id}: {decision.reason_code.value}"
                )

        elif self.session_repository is not None and session_id:
            session = self.session_repository.get_by_id(session_id)
            now = self.clock.now()
            if not session or session.status != SessionStatus.ACTIVE or session.expires_at <= now:
                raise OpportunityDashboardAuthenticationError("Session is invalid, inactive or expired.")
            if session.tenant_id != tenant_id:
                raise OpportunityDashboardAuthorizationError("Cross-tenant session access denied.")

        return TenantContext(tenant_id=tenant_id)

    def _project_item(self, record: OpportunityRecord) -> OpportunityDashboardItem:
        """Proyecta un OpportunityRecord a un OpportunityDashboardItem seguro."""
        obs = record.observed_metrics
        der = record.derived_metrics

        est_price_amount = obs.observed_price.amount if obs.observed_price else None
        est_price_currency = obs.observed_price.currency if obs.observed_price else None

        low_comp_amount = obs.lowest_competitor_price.amount if obs.lowest_competitor_price else None
        low_comp_curr = obs.lowest_competitor_price.currency if obs.lowest_competitor_price else None

        bb_amount = obs.buy_box_winner_price.amount if obs.buy_box_winner_price else None
        bb_curr = obs.buy_box_winner_price.currency if obs.buy_box_winner_price else None

        supplier_count = None
        if record.supplier_memory_id_ref:
            supplier_count = 1
        elif "supplier_count" in record.metadata:
            supplier_count = int(record.metadata["supplier_count"])

        return OpportunityDashboardItem(
            opportunity_id=record.opportunity_id,
            canonical_product_id=record.canonical_product_id,
            marketplace=record.marketplace.value if hasattr(record.marketplace, "value") else str(record.marketplace),
            opportunity_type=record.opportunity_type.value if hasattr(record.opportunity_type, "value") else str(record.opportunity_type),
            status=record.status.value if hasattr(record.status, "value") else str(record.status),
            confidence=record.confidence.value if hasattr(record.confidence, "value") else str(record.confidence),
            detected_at=record.detected_at,
            title=record.title,
            category=record.category,
            product_sku=record.product_sku,
            opportunity_score=der.opportunity_score,
            demand_intensity=der.demand_intensity,
            competition_density=der.competition_density,
            estimated_price_amount=est_price_amount,
            estimated_price_currency=est_price_currency,
            lowest_competitor_price_amount=low_comp_amount,
            lowest_competitor_price_currency=low_comp_curr,
            buy_box_price_amount=bb_amount,
            buy_box_price_currency=bb_curr,
            potential_margin_ratio=der.potential_margin_ratio,
            price_gap_amount=der.price_gap_amount,
            price_gap_ratio=der.price_gap_ratio,
            observed_sold_quantity=obs.observed_sold_quantity,
            observed_stock=obs.observed_stock,
            observed_competitor_count=obs.observed_competitor_count,
            supplier_count=supplier_count,
            source_observations_count=len(record.source_observation_ids) if record.source_observation_ids else obs.observations_count,
            unknown_fields=record.unknown_fields,
            scoring_rationale=der.scoring_rationale,
        )

    def get_summary(
        self,
        tenant_id: str,
        session_id: Optional[str] = None,
    ) -> OpportunityDashboardSummary:
        """Genera el resumen agregado del dashboard para el tenant."""
        context = self._authorize(tenant_id, session_id, action="OPPORTUNITY_DASHBOARD_READ")
        records = self.repository.list_all(context)

        total = len(records)
        high_count = 0
        med_count = 0
        low_count = 0

        scores: List[Decimal] = []
        by_mkt: Dict[str, int] = {}
        by_cat: Dict[str, int] = {}
        by_type: Dict[str, int] = {}
        by_status: Dict[str, int] = {}
        by_conf: Dict[str, int] = {}

        newest_dt: Optional[datetime] = None
        oldest_dt: Optional[datetime] = None

        for r in records:
            mkt = r.marketplace.value if hasattr(r.marketplace, "value") else str(r.marketplace)
            by_mkt[mkt] = by_mkt.get(mkt, 0) + 1

            cat = r.category or "UNCATEGORIZED"
            by_cat[cat] = by_cat.get(cat, 0) + 1

            t_val = r.opportunity_type.value if hasattr(r.opportunity_type, "value") else str(r.opportunity_type)
            by_type[t_val] = by_type.get(t_val, 0) + 1

            s_val = r.status.value if hasattr(r.status, "value") else str(r.status)
            by_status[s_val] = by_status.get(s_val, 0) + 1

            c_val = r.confidence.value if hasattr(r.confidence, "value") else str(r.confidence)
            by_conf[c_val] = by_conf.get(c_val, 0) + 1

            score = r.derived_metrics.opportunity_score
            if score is not None:
                scores.append(score)
                if score >= Decimal("70.0"):
                    high_count += 1
                elif score >= Decimal("50.0"):
                    med_count += 1
                else:
                    low_count += 1

            if newest_dt is None or r.detected_at > newest_dt:
                newest_dt = r.detected_at
            if oldest_dt is None or r.detected_at < oldest_dt:
                oldest_dt = r.detected_at

        avg_score: Optional[Decimal] = None
        if scores:
            avg_score = round(sum(scores) / Decimal(len(scores)), 2)

        return OpportunityDashboardSummary(
            tenant_id=tenant_id,
            total_opportunities=total,
            high_potential_count=high_count,
            medium_potential_count=med_count,
            low_potential_count=low_count,
            average_opportunity_score=avg_score,
            opportunities_by_marketplace=by_mkt,
            opportunities_by_category=by_cat,
            opportunities_by_type=by_type,
            opportunities_by_status=by_status,
            opportunities_by_confidence=by_conf,
            newest_detected_at=newest_dt,
            oldest_detected_at=oldest_dt,
        )

    def list_opportunities(
        self,
        tenant_id: str,
        query: Optional[OpportunityDashboardQuery] = None,
        session_id: Optional[str] = None,
    ) -> OpportunityDashboardPage:
        """Consulta y pagina el listado de oportunidades aplicando filtros y ordenación determinista."""
        context = self._authorize(tenant_id, session_id, action="OPPORTUNITY_DASHBOARD_READ")
        query = query or OpportunityDashboardQuery()
        records = self.repository.list_all(context)

        # 1. Filtrado
        filtered: List[OpportunityRecord] = []
        for r in records:
            # Marketplace (flexible matching)
            if query.marketplace:
                mkt_val = r.marketplace.value if hasattr(r.marketplace, "value") else str(r.marketplace)
                q_mkt = query.marketplace.lower().replace("_", "").replace("-", "").replace(" ", "")
                r_mkt = mkt_val.lower().replace("_", "").replace("-", "").replace(" ", "")
                if q_mkt != r_mkt and q_mkt not in r_mkt:
                    continue

            # Category
            if query.category:
                if not r.category or query.category.lower() not in r.category.lower():
                    continue

            # Opportunity Type
            if query.opportunity_type:
                t_val = r.opportunity_type.value if hasattr(r.opportunity_type, "value") else str(r.opportunity_type)
                if t_val.lower() != query.opportunity_type.lower():
                    continue

            # Status
            if query.status:
                s_val = r.status.value if hasattr(r.status, "value") else str(r.status)
                if s_val.lower() != query.status.lower():
                    continue

            # Confidence
            if query.confidence:
                c_val = r.confidence.value if hasattr(r.confidence, "value") else str(r.confidence)
                if c_val.lower() != query.confidence.lower():
                    continue

            # Score min/max
            score = r.derived_metrics.opportunity_score
            if query.min_score is not None:
                if score is None or score < query.min_score:
                    continue
            if query.max_score is not None:
                if score is None or score > query.max_score:
                    continue

            # Margin min
            if query.min_margin is not None:
                margin = r.derived_metrics.potential_margin_ratio
                if margin is None or margin < query.min_margin:
                    continue

            # Date Range
            if query.date_from is not None:
                if r.detected_at < query.date_from:
                    continue
            if query.date_to is not None:
                if r.detected_at > query.date_to:
                    continue

            # Search text (title, sku, category, product_id)
            if query.search_text:
                st = query.search_text.lower()
                title_match = r.title and st in r.title.lower()
                sku_match = r.product_sku and st in r.product_sku.lower()
                cat_match = r.category and st in r.category.lower()
                id_match = st in r.canonical_product_id.lower() or st in r.opportunity_id.lower()
                if not (title_match or sku_match or cat_match or id_match):
                    continue

            filtered.append(r)

        # 2. Ordenación determinista con tie-break por detected_at y opportunity_id
        is_desc = query.sort_order == SortOrder.DESC

        def sort_key(rec: OpportunityRecord) -> Tuple[Any, datetime, str]:
            primary_val: Any = None
            if query.sort_by == OpportunitySortField.OPPORTUNITY_SCORE:
                primary_val = rec.derived_metrics.opportunity_score
                # None al final
                if primary_val is None:
                    primary_val = Decimal("-999999") if is_desc else Decimal("999999")
            elif query.sort_by == OpportunitySortField.POTENTIAL_MARGIN:
                primary_val = rec.derived_metrics.potential_margin_ratio
                if primary_val is None:
                    primary_val = Decimal("-999999") if is_desc else Decimal("999999")
            elif query.sort_by == OpportunitySortField.CONFIDENCE:
                conf_order = {Confidence.HIGH: 3, Confidence.MEDIUM: 2, Confidence.LOW: 1, Confidence.UNKNOWN: 0}
                primary_val = conf_order.get(rec.confidence, 0)
            elif query.sort_by == OpportunitySortField.TITLE:
                primary_val = (rec.title or "").lower()
            else:  # DETECTED_AT
                primary_val = rec.detected_at

            return (primary_val, rec.detected_at, rec.opportunity_id)

        filtered.sort(key=sort_key, reverse=is_desc)

        # 3. Paginación
        total_count = len(filtered)
        page = query.page
        page_size = query.page_size
        total_pages = math.ceil(total_count / page_size) if total_count > 0 else 1

        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        paged_records = filtered[start_idx:end_idx]

        items = tuple(self._project_item(r) for r in paged_records)

        return OpportunityDashboardPage(
            items=items,
            total_count=total_count,
            page=page,
            page_size=page_size,
            total_pages=total_pages,
            has_next=page < total_pages,
            has_previous=page > 1,
        )

    def get_opportunity_detail(
        self,
        tenant_id: str,
        opportunity_id: str,
        session_id: Optional[str] = None,
    ) -> OpportunityDashboardDetail:
        """Obtiene el detalle completo y seguro de una oportunidad individual."""
        context = self._authorize(tenant_id, session_id, action="OPPORTUNITY_DASHBOARD_READ")
        validate_safe_identifier(opportunity_id, field_name="opportunity_id")
        record = self.repository.get_by_id(context, opportunity_id)
        if not record:
            raise OpportunityNotFoundError(f"Opportunity {opportunity_id} not found for tenant {tenant_id}.")

        item = self._project_item(record)

        # Desglose estructurado de evidencia (OBSERVED vs DERIVED vs RISKS)
        observed_detail = {
            "price": {
                "amount": str(record.observed_metrics.observed_price.amount),
                "currency": record.observed_metrics.observed_price.currency,
            } if record.observed_metrics.observed_price else None,
            "sold_quantity": record.observed_metrics.observed_sold_quantity,
            "stock": record.observed_metrics.observed_stock,
            "competitor_count": record.observed_metrics.observed_competitor_count,
            "lowest_competitor_price": {
                "amount": str(record.observed_metrics.lowest_competitor_price.amount),
                "currency": record.observed_metrics.lowest_competitor_price.currency,
            } if record.observed_metrics.lowest_competitor_price else None,
            "buy_box_winner_price": {
                "amount": str(record.observed_metrics.buy_box_winner_price.amount),
                "currency": record.observed_metrics.buy_box_winner_price.currency,
            } if record.observed_metrics.buy_box_winner_price else None,
            "observations_count": record.observed_metrics.observations_count,
        }

        derived_detail = {
            "price_gap_amount": str(record.derived_metrics.price_gap_amount) if record.derived_metrics.price_gap_amount is not None else None,
            "price_gap_ratio": str(record.derived_metrics.price_gap_ratio) if record.derived_metrics.price_gap_ratio is not None else None,
            "potential_margin_ratio": str(record.derived_metrics.potential_margin_ratio) if record.derived_metrics.potential_margin_ratio is not None else None,
            "competition_density": record.derived_metrics.competition_density,
            "demand_intensity": record.derived_metrics.demand_intensity,
            "opportunity_score": str(record.derived_metrics.opportunity_score) if record.derived_metrics.opportunity_score is not None else None,
        }

        # Explicabilidad estructurada y sanitizada
        explanation_data = {
            "product_id": record.canonical_product_id,
            "title": record.title or "Unknown Title",
            "why_attractive": list(record.reasons) if record.reasons else ["High market signal detected"],
            "scoring_rationale": list(record.derived_metrics.scoring_rationale),
            "unknowns": list(record.unknown_fields),
        }

        return OpportunityDashboardDetail(
            item=item,
            reasons=record.reasons,
            scoring_rationale=record.derived_metrics.scoring_rationale,
            observed_metrics_detail=observed_detail,
            derived_metrics_detail=derived_detail,
            explanation=explanation_data,
            provenance=record.provenance,
            correlation_id=record.correlation_id,
            metadata=record.metadata,
        )

    def compare_opportunities(
        self,
        tenant_id: str,
        opportunity_ids: Sequence[str],
        session_id: Optional[str] = None,
    ) -> OpportunityComparisonView:
        """Compara 2 o más oportunidades de forma determinista y multidimensional."""
        if not opportunity_ids or len(opportunity_ids) < 2:
            raise ValueError("At least 2 opportunity IDs are required for comparison.")

        context = self._authorize(tenant_id, session_id, action="OPPORTUNITY_DASHBOARD_READ")

        records: List[OpportunityRecord] = []
        for opp_id in opportunity_ids:
            validate_safe_identifier(opp_id, field_name="opportunity_id")
            r = self.repository.get_by_id(context, opp_id)
            if not r:
                raise OpportunityNotFoundError(f"Opportunity {opp_id} not found in tenant {tenant_id}.")
            records.append(r)

        items = tuple(self._project_item(r) for r in records)

        # Comparar dimensiones canónicas: Score, Margen, Demanda, Competencia, Confianza
        dimensions: List[Dict[str, Any]] = []

        # 1. Dimensión Score
        score_by_cand = {r.opportunity_id: str(r.derived_metrics.opportunity_score) if r.derived_metrics.opportunity_score is not None else "UNKNOWN" for r in records}
        best_score_opp = max(records, key=lambda r: r.derived_metrics.opportunity_score or Decimal("-999"))
        dimensions.append({
            "dimension_name": "Opportunity Score",
            "winner_id": best_score_opp.opportunity_id if best_score_opp.derived_metrics.opportunity_score is not None else None,
            "summary": f"Best score: {best_score_opp.derived_metrics.opportunity_score} ({best_score_opp.opportunity_id})",
            "scores_by_candidate": score_by_cand,
        })

        # 2. Dimensión Margen
        margin_by_cand = {r.opportunity_id: str(r.derived_metrics.potential_margin_ratio) if r.derived_metrics.potential_margin_ratio is not None else "UNKNOWN" for r in records}
        best_margin_opp = max(records, key=lambda r: r.derived_metrics.potential_margin_ratio or Decimal("-999"))
        dimensions.append({
            "dimension_name": "Potential Margin",
            "winner_id": best_margin_opp.opportunity_id if best_margin_opp.derived_metrics.potential_margin_ratio is not None else None,
            "summary": f"Highest margin ratio: {best_margin_opp.derived_metrics.potential_margin_ratio} ({best_margin_opp.opportunity_id})",
            "scores_by_candidate": margin_by_cand,
        })

        # 3. Dimensión Confianza
        conf_by_cand = {r.opportunity_id: r.confidence.value if hasattr(r.confidence, "value") else str(r.confidence) for r in records}
        conf_map = {Confidence.HIGH: 3, Confidence.MEDIUM: 2, Confidence.LOW: 1, Confidence.UNKNOWN: 0}
        best_conf_opp = max(records, key=lambda r: conf_map.get(r.confidence, 0))
        dimensions.append({
            "dimension_name": "Confidence Level",
            "winner_id": best_conf_opp.opportunity_id,
            "summary": f"Highest confidence: {best_conf_opp.confidence.value} ({best_conf_opp.opportunity_id})",
            "scores_by_candidate": conf_by_cand,
        })

        # Determinación de Best Candidate global basado en score
        best_cand_id = best_score_opp.opportunity_id if best_score_opp.derived_metrics.opportunity_score is not None else records[0].opportunity_id
        summary_text = f"Compared {len(records)} opportunities for tenant {tenant_id}. Leading candidate: {best_cand_id}."
        why_winner = f"Leading candidate {best_cand_id} provides optimal balance of opportunity score ({best_score_opp.derived_metrics.opportunity_score}) and market signals."

        return OpportunityComparisonView(
            candidate_ids=tuple(opportunity_ids),
            best_candidate_id=best_cand_id,
            dimensions=tuple(dimensions),
            comparison_summary=summary_text,
            why_winner=why_winner,
            items=items,
        )
