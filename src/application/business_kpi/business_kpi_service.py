"""
Servicio de Aplicación para Q.6 — Business KPIs & Cross-Domain Summary (Hito Q — Business Intelligence).

Responsabilidades y Principios:
1. CONSULTATIVE & DERIVED ONLY (No Execution / No Mutation):
   - Agrega y proyecta hechos desde Q.1 a Q.5 (Oportunidades, Proveedores, Rentabilidad, Misiones, Costos de Agentes).
   - No ejecuta motores de pricing, scraping, compras ni mutaciones.
2. STRICT TENANT ISOLATION (O.1):
   - Cada consulta opera bajo un TenantContext estricto. Cero fugas cross-tenant.
3. SAAS AUTHENTICATION & RBAC (O.3 / O.4 / N.4):
   - Valida sesión activa y comprueba permisos BUSINESS_KPI_READ con fallback a BUSINESS_INTELLIGENCE_READ.
4. UNCERTAINTY SEMANTICS & SAFE RATIOS:
   - UNKNOWN != 0. Valores ausentes o denominadores cero generan KPIStatus.UNKNOWN o INSUFFICIENT_DATA.
   - Misiones en ejecución o pendientes no se consideran fallas en el MISSION_SUCCESS_RATE.
5. DECIMAL MONEY & MULTI-CURRENCY SAFETY:
   - Cálculos monetarios y ratios estrictamente en Decimal.
   - Costos y beneficios se desglosan por divisa de origen sin mezclar monedas no comparables.
6. EXPLAINABILITY & CATALOG CONSISTENCY:
   - Integración directa con KPI_CATALOG proveyendo fórmulas, versiones, fuentes y estados de confianza.
7. REUTILIZACIÓN Y DELEGACIÓN EFICIENTE:
   - Aprovecha agregados y resúmenes existentes en Q.1 a Q.5 evitando recargas masivas de datos cuando es posible.
"""

from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, List, Dict, Any, Tuple, Sequence, Mapping
from types import MappingProxyType
import math

from src.domain.tenant.models import TenantContext
from src.domain.tenant.guard import CrossTenantGuard
from src.domain.security.models import validate_safe_identifier, sanitize_security_data, deep_freeze
from src.domain.business_kpi.models import (
    BusinessKPIValue,
    BusinessKPISummary,
    BusinessKPIDomainBreakdown,
    BusinessKPIComparison,
    BusinessKPICatalogItem,
    BusinessKPIQuery,
    KPIStatus,
    KPIConfidence,
    KPIUnit,
    KPIDomain,
)
from src.domain.business_kpi.ports import BusinessKPIServicePort
from src.domain.business_kpi.catalog import KPI_CATALOG, get_catalog_map

from src.domain.opportunity_dashboard.ports import TenantOpportunityRepositoryPort
from src.domain.supplier_dashboard.ports import TenantSupplierRepositoryPort
from src.domain.profit_dashboard.ports import TenantProfitRepositoryPort
from src.domain.mission_dashboard.ports import TenantMissionRepositoryPort
from src.domain.usage_metering.ports import UsageEventRepositoryPort
from src.domain.cost.ports import CostRepositoryPort, PricingCatalogPort
from src.domain.profit_dashboard.models import ProfitCompleteness

from src.domain.saas_authorization.models import (
    SaaSAuthorizationRequest,
    SaaSAuthorizationStatus,
    SaaSAuthorizationReasonCode,
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


class BusinessKPIAuthenticationError(AdminAuthenticationError):
    """Lanzada cuando la sesión es inválida, expirada o ausente."""
    pass


class BusinessKPIAuthorizationError(AdminAuthorizationError):
    """Lanzada cuando el usuario o tenant no tiene permisos para ver los KPIs de negocio."""
    pass


class BusinessKPINotFoundError(AdminResourceNotFoundError):
    """Lanzada cuando un KPI solicitado no existe en el catálogo canónico."""
    pass


class BusinessKPIInvalidRequestError(AdminInvalidRequestError):
    """Lanzada cuando los parámetros de consulta o filtros de KPI son inválidos."""
    pass


class BusinessKPIService(BusinessKPIServicePort):
    """
    Servicio de Aplicación para Business KPIs & Cross-Domain Summary (Q.6).
    Orquesta y calcula los indicadores ejecutivos multi-dominio para el tenant.
    """

    def __init__(
        self,
        opportunity_service: Optional[Any] = None,
        opportunity_repository: Optional[TenantOpportunityRepositoryPort] = None,
        supplier_service: Optional[Any] = None,
        supplier_repository: Optional[TenantSupplierRepositoryPort] = None,
        profit_service: Optional[Any] = None,
        profit_repository: Optional[TenantProfitRepositoryPort] = None,
        mission_service: Optional[Any] = None,
        mission_repository: Optional[TenantMissionRepositoryPort] = None,
        agent_cost_service: Optional[Any] = None,
        usage_repository: Optional[UsageEventRepositoryPort] = None,
        cost_repository: Optional[CostRepositoryPort] = None,
        pricing_catalog: Optional[PricingCatalogPort] = None,
        authorization_service: Optional[SaaSAuthorizationService] = None,
        session_repository: Optional[SaaSSessionRepositoryPort] = None,
        clock: Optional[ClockPort] = None,
    ):
        self.opportunity_service = opportunity_service
        self.opportunity_repository = opportunity_repository or (
            getattr(opportunity_service, "repository", None) if opportunity_service else None
        )

        self.supplier_service = supplier_service
        self.supplier_repository = supplier_repository or (
            getattr(supplier_service, "repository", None) if supplier_service else None
        )

        self.profit_service = profit_service
        self.profit_repository = profit_repository or (
            getattr(profit_service, "repository", None) if profit_service else None
        )

        self.mission_service = mission_service
        self.mission_repository = mission_repository or (
            getattr(mission_service, "repository", None) if mission_service else None
        )

        self.agent_cost_service = agent_cost_service
        self.usage_repository = usage_repository or (
            getattr(agent_cost_service, "usage_repository", None) if agent_cost_service else None
        )
        self.cost_repository = cost_repository or (
            getattr(agent_cost_service, "cost_repository", None) if agent_cost_service else None
        )
        self.pricing_catalog = pricing_catalog or (
            getattr(agent_cost_service, "pricing_catalog", None) if agent_cost_service else None
        )

        self.authorization_service = authorization_service
        self.session_repository = session_repository
        self.clock = clock or SystemClock()
        self._catalog_map = get_catalog_map()

    def _authorize(
        self,
        tenant_id: str,
        session_id: Optional[str] = None,
        action: str = "BUSINESS_KPI_READ",
    ) -> TenantContext:
        """
        Valida la sesión y los permisos de autorización para el tenant dado.
        Retorna el TenantContext validado con aislamiento garantizado.
        """
        try:
            validate_safe_identifier(tenant_id, field_name="tenant_id")
        except Exception as e:
            raise BusinessKPIInvalidRequestError(f"Invalid tenant identifier '{tenant_id}': {str(e)}")

        if self.authorization_service is not None:
            if not session_id:
                raise BusinessKPIAuthenticationError("Authentication required: missing session_id.")

            req = SaaSAuthorizationRequest(
                action=action,
                session_id=session_id,
                tenant_id=tenant_id,
            )
            decision = self.authorization_service.authorize(req)
            if not decision.is_allowed:
                # Fallback canónico a BUSINESS_INTELLIGENCE_READ
                if action != "BUSINESS_INTELLIGENCE_READ":
                    fallback_req = SaaSAuthorizationRequest(
                        action="BUSINESS_INTELLIGENCE_READ",
                        session_id=session_id,
                        tenant_id=tenant_id,
                    )
                    fallback_decision = self.authorization_service.authorize(fallback_req)
                    if fallback_decision.is_allowed:
                        return TenantContext(tenant_id=tenant_id)

                auth_reasons = {
                    SaaSAuthorizationReasonCode.SESSION_INVALID,
                    SaaSAuthorizationReasonCode.SESSION_NOT_FOUND,
                    SaaSAuthorizationReasonCode.SESSION_EXPIRED,
                    SaaSAuthorizationReasonCode.SESSION_REVOKED,
                }
                if decision.reason_code in auth_reasons:
                    raise BusinessKPIAuthenticationError(
                        f"Authentication failed: {decision.reason_code.value}"
                    )
                raise BusinessKPIAuthorizationError(
                    f"Authorization denied for action {action} on tenant {tenant_id}: {decision.reason_code.value}"
                )

        elif self.session_repository is not None:
            if not session_id:
                raise BusinessKPIAuthenticationError("Authentication required: missing session_id.")
            session = self.session_repository.get_by_id(session_id)
            if not session:
                raise BusinessKPIAuthenticationError(f"Session '{session_id}' not found.")
            now = self.clock.now()
            if hasattr(session, "is_expired") and session.is_expired(now):
                raise BusinessKPIAuthenticationError(f"Session '{session_id}' has expired.")
            elif getattr(session, "status", None) != SessionStatus.ACTIVE or getattr(session, "expires_at", now) <= now:
                raise BusinessKPIAuthenticationError(f"Session '{session_id}' is inactive or expired.")
            if session.tenant_id != tenant_id:
                raise BusinessKPIAuthorizationError("Cross-tenant session access denied.")

        return TenantContext(tenant_id=tenant_id)

    def get_catalog(self) -> List[BusinessKPICatalogItem]:
        """Retorna la lista canónica de KPIs soportados."""
        return list(KPI_CATALOG)

    def get_kpi_by_id(
        self,
        tenant_id: str,
        kpi_id: str,
        query: Optional[BusinessKPIQuery] = None,
        session_id: Optional[str] = None,
    ) -> BusinessKPIValue:
        """
        Obtiene un KPI específico con su desglose y explicabilidad para el tenant.
        """
        if kpi_id not in self._catalog_map:
            raise BusinessKPINotFoundError(f"KPI '{kpi_id}' not found in canonical catalog.")

        summary = self.get_summary(tenant_id=tenant_id, query=query, session_id=session_id)
        for kpi in summary.kpis:
            if kpi.kpi_id == kpi_id:
                return kpi

        raise BusinessKPINotFoundError(f"KPI '{kpi_id}' was not computed for tenant '{tenant_id}'.")

    def get_comparison(
        self,
        tenant_id: str,
        current_query: BusinessKPIQuery,
        previous_query: BusinessKPIQuery,
        session_id: Optional[str] = None,
    ) -> List[BusinessKPIComparison]:
        """
        Compara los KPIs entre dos ventanas temporales deterministas.
        Si faltan datos históricos o el denominador previo es 0 => delta_pct = None.
        """
        self._authorize(tenant_id, session_id=session_id)

        current_summary = self.get_summary(tenant_id=tenant_id, query=current_query, session_id=session_id)
        previous_summary = self.get_summary(tenant_id=tenant_id, query=previous_query, session_id=session_id)

        prev_map: Dict[str, BusinessKPIValue] = {k.kpi_id: k for k in previous_summary.kpis}
        comparisons: List[BusinessKPIComparison] = []

        for curr_kpi in current_summary.kpis:
            kpi_id = curr_kpi.kpi_id
            prev_kpi = prev_map.get(kpi_id)

            curr_val = curr_kpi.value if curr_kpi.status == KPIStatus.CALCULATED else None
            prev_val = prev_kpi.value if (prev_kpi and prev_kpi.status == KPIStatus.CALCULATED) else None

            if curr_val is not None and prev_val is not None:
                delta_abs = curr_val - prev_val
                if prev_val != Decimal("0"):
                    delta_pct = ((delta_abs / abs(prev_val)) * Decimal("100")).quantize(
                        Decimal("0.01"), rounding=ROUND_HALF_UP
                    )
                else:
                    delta_pct = None

                comparisons.append(
                    BusinessKPIComparison(
                        kpi_id=kpi_id,
                        current_value=curr_val,
                        previous_value=prev_val,
                        delta_absolute=delta_abs,
                        delta_pct=delta_pct,
                        is_comparable=True,
                        status=KPIStatus.CALCULATED,
                        reason_not_comparable=None,
                    )
                )
            else:
                reason = "Insufficient historical or current calculated data for comparison."
                comparisons.append(
                    BusinessKPIComparison(
                        kpi_id=kpi_id,
                        current_value=curr_val,
                        previous_value=prev_val,
                        delta_absolute=None,
                        delta_pct=None,
                        is_comparable=False,
                        status=KPIStatus.INSUFFICIENT_DATA if (curr_val is None or prev_val is None) else KPIStatus.UNKNOWN,
                        reason_not_comparable=reason,
                    )
                )

        return comparisons

    def get_summary(
        self,
        tenant_id: str,
        query: Optional[BusinessKPIQuery] = None,
        session_id: Optional[str] = None,
    ) -> BusinessKPISummary:
        """
        Calcula y proyecta el resumen ejecutivo cross-domain de KPIs para el tenant.
        """
        context = self._authorize(tenant_id, session_id=session_id)
        effective_query = query or BusinessKPIQuery()
        now = self.clock.now()

        # ---------------------------------------------------------------------
        # 1. Recuperación de hechos por dominio
        # ---------------------------------------------------------------------
        opp_records = self._fetch_opportunities(context, effective_query)
        supplier_records = self._fetch_suppliers(context, effective_query)
        profit_records = self._fetch_profit_items(context, effective_query)
        mission_records = self._fetch_missions(context, effective_query)
        cost_facts = self._fetch_agent_costs(tenant_id, session_id, effective_query)

        data_quality_notes: List[str] = []
        currency_breakdown: Dict[str, Dict[str, Any]] = {}

        # ---------------------------------------------------------------------
        # 2. Dominio OPPORTUNITY (Q.1)
        # ---------------------------------------------------------------------
        kpis_opportunity: List[BusinessKPIValue] = []

        # OPPORTUNITY_COUNT
        opp_count = len(opp_records)
        cat_opp_count = self._catalog_map["OPPORTUNITY_COUNT"]
        kpis_opportunity.append(
            BusinessKPIValue(
                kpi_id=cat_opp_count.kpi_id,
                name=cat_opp_count.name,
                domain=KPIDomain.OPPORTUNITY,
                status=KPIStatus.CALCULATED,
                value=Decimal(opp_count),
                unit=KPIUnit.COUNT,
                confidence=KPIConfidence.HIGH,
                source=cat_opp_count.source,
                formula_version=cat_opp_count.formula_version,
                formula_description=cat_opp_count.formula_description,
                inputs_present=("opportunity_records",),
                calculated_at=now,
            )
        )

        # HIGH_POTENTIAL_OPPORTUNITIES
        high_potential_count = 0
        opp_scores: List[Decimal] = []
        for r in opp_records:
            score = getattr(r, "opportunity_score", None)
            if score is None and hasattr(r, "derived_metrics") and r.derived_metrics is not None:
                score = getattr(r.derived_metrics, "opportunity_score", None)
            if score is not None:
                try:
                    dec_score = Decimal(str(score))
                    opp_scores.append(dec_score)
                    if dec_score >= Decimal("70.0") or (Decimal("0.70") <= dec_score <= Decimal("1.0")):
                        high_potential_count += 1
                except Exception:
                    pass
            else:
                # Si no hay score pero tiene margin ratio alto >= 0.30
                pmr = getattr(r, "potential_margin_ratio", None)
                if pmr is None and hasattr(r, "derived_metrics") and r.derived_metrics is not None:
                    pmr = getattr(r.derived_metrics, "estimated_profit_margin", None)
                if pmr is not None:
                    try:
                        dec_pmr = Decimal(str(pmr))
                        if dec_pmr >= Decimal("0.30"):
                            high_potential_count += 1
                    except Exception:
                        pass

        cat_high_opp = self._catalog_map["HIGH_POTENTIAL_OPPORTUNITIES"]
        kpis_opportunity.append(
            BusinessKPIValue(
                kpi_id=cat_high_opp.kpi_id,
                name=cat_high_opp.name,
                domain=KPIDomain.OPPORTUNITY,
                status=KPIStatus.CALCULATED,
                value=Decimal(high_potential_count),
                unit=KPIUnit.COUNT,
                confidence=KPIConfidence.HIGH if opp_records else KPIConfidence.MEDIUM,
                source=cat_high_opp.source,
                formula_version=cat_high_opp.formula_version,
                formula_description=cat_high_opp.formula_description,
                inputs_present=("opportunity_records", "scores"),
                calculated_at=now,
            )
        )

        # AVG_OPPORTUNITY_SCORE
        cat_avg_opp = self._catalog_map["AVG_OPPORTUNITY_SCORE"]
        if opp_scores:
            avg_score = (sum(opp_scores) / Decimal(len(opp_scores))).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            kpis_opportunity.append(
                BusinessKPIValue(
                    kpi_id=cat_avg_opp.kpi_id,
                    name=cat_avg_opp.name,
                    domain=KPIDomain.OPPORTUNITY,
                    status=KPIStatus.CALCULATED,
                    value=avg_score,
                    unit=KPIUnit.SCORE,
                    confidence=KPIConfidence.HIGH if len(opp_scores) == len(opp_records) else KPIConfidence.MEDIUM,
                    source=cat_avg_opp.source,
                    formula_version=cat_avg_opp.formula_version,
                    formula_description=cat_avg_opp.formula_description,
                    inputs_present=("opportunity_scores",),
                    calculated_at=now,
                )
            )
        else:
            kpis_opportunity.append(
                BusinessKPIValue(
                    kpi_id=cat_avg_opp.kpi_id,
                    name=cat_avg_opp.name,
                    domain=KPIDomain.OPPORTUNITY,
                    status=KPIStatus.UNKNOWN,
                    value=None,
                    unit=KPIUnit.SCORE,
                    confidence=KPIConfidence.UNKNOWN,
                    source=cat_avg_opp.source,
                    formula_version=cat_avg_opp.formula_version,
                    formula_description=cat_avg_opp.formula_description,
                    inputs_missing=("opportunity_scores",),
                    calculated_at=now,
                )
            )
            data_quality_notes.append("No opportunity scores available to compute AVG_OPPORTUNITY_SCORE.")

        # ---------------------------------------------------------------------
        # 3. Dominio SUPPLIER (Q.2)
        # ---------------------------------------------------------------------
        kpis_supplier: List[BusinessKPIValue] = []

        total_suppliers = len(supplier_records)
        verified_count = 0
        supplier_scores: List[Decimal] = []

        for s in supplier_records:
            v_status = str(getattr(s, "verification_status", getattr(s, "status", ""))).upper()
            if hasattr(getattr(s, "status", None), "value"):
                v_status = str(s.status.value).upper()
            risk = str(getattr(s, "risk_level", "")).upper()
            if hasattr(s, "metadata") and isinstance(s.metadata, (dict, MappingProxyType)):
                if not risk:
                    risk = str(s.metadata.get("risk_level", "")).upper()

            # Estado verificado estricto (VERIFIED o ACTIVE comprobado, sin UNVERIFIED)
            is_verified = False
            if "UNVERIFIED" not in v_status and any(v_status == term for term in ("VERIFIED", "ACTIVE", "READY_FOR_ECONOMICS")):
                is_verified = True

            if is_verified and risk not in {"HIGH", "CRITICAL"}:
                verified_count += 1

            s_score = getattr(s, "supplier_score", None)
            if s_score is None and hasattr(s, "metadata") and isinstance(s.metadata, (dict, MappingProxyType)):
                s_score = s.metadata.get("supplier_score")
            elif s_score is None and hasattr(s, "metadata") and s.metadata is not None:
                s_score = s.metadata.get("supplier_score") if hasattr(s.metadata, "get") else None
            if s_score is not None:
                try:
                    supplier_scores.append(Decimal(str(s_score)))
                except Exception:
                    pass

        # VALIDATED_SUPPLIER_COUNT
        cat_val_sup = self._catalog_map["VALIDATED_SUPPLIER_COUNT"]
        kpis_supplier.append(
            BusinessKPIValue(
                kpi_id=cat_val_sup.kpi_id,
                name=cat_val_sup.name,
                domain=KPIDomain.SUPPLIER,
                status=KPIStatus.CALCULATED,
                value=Decimal(verified_count),
                unit=KPIUnit.COUNT,
                confidence=KPIConfidence.HIGH,
                source=cat_val_sup.source,
                formula_version=cat_val_sup.formula_version,
                formula_description=cat_val_sup.formula_description,
                inputs_present=("supplier_records", "verification_status"),
                calculated_at=now,
            )
        )

        # AVG_SUPPLIER_SCORE
        cat_avg_sup = self._catalog_map["AVG_SUPPLIER_SCORE"]
        if supplier_scores:
            avg_sup_score = (sum(supplier_scores) / Decimal(len(supplier_scores))).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            kpis_supplier.append(
                BusinessKPIValue(
                    kpi_id=cat_avg_sup.kpi_id,
                    name=cat_avg_sup.name,
                    domain=KPIDomain.SUPPLIER,
                    status=KPIStatus.CALCULATED,
                    value=avg_sup_score,
                    unit=KPIUnit.SCORE,
                    confidence=KPIConfidence.HIGH if len(supplier_scores) == total_suppliers else KPIConfidence.MEDIUM,
                    source=cat_avg_sup.source,
                    formula_version=cat_avg_sup.formula_version,
                    formula_description=cat_avg_sup.formula_description,
                    inputs_present=("supplier_scores",),
                    calculated_at=now,
                )
            )
        else:
            kpis_supplier.append(
                BusinessKPIValue(
                    kpi_id=cat_avg_sup.kpi_id,
                    name=cat_avg_sup.name,
                    domain=KPIDomain.SUPPLIER,
                    status=KPIStatus.UNKNOWN,
                    value=None,
                    unit=KPIUnit.SCORE,
                    confidence=KPIConfidence.UNKNOWN,
                    source=cat_avg_sup.source,
                    formula_version=cat_avg_sup.formula_version,
                    formula_description=cat_avg_sup.formula_description,
                    inputs_missing=("supplier_scores",),
                    calculated_at=now,
                )
            )
            data_quality_notes.append("No supplier scores available to compute AVG_SUPPLIER_SCORE.")

        # SUPPLIER_VERIFICATION_RATE
        cat_sup_rate = self._catalog_map["SUPPLIER_VERIFICATION_RATE"]
        if total_suppliers > 0:
            rate = ((Decimal(verified_count) / Decimal(total_suppliers)) * Decimal("100")).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            kpis_supplier.append(
                BusinessKPIValue(
                    kpi_id=cat_sup_rate.kpi_id,
                    name=cat_sup_rate.name,
                    domain=KPIDomain.SUPPLIER,
                    status=KPIStatus.CALCULATED,
                    value=rate,
                    unit=KPIUnit.PERCENTAGE,
                    confidence=KPIConfidence.HIGH,
                    source=cat_sup_rate.source,
                    formula_version=cat_sup_rate.formula_version,
                    formula_description=cat_sup_rate.formula_description,
                    inputs_present=("total_suppliers", "verified_suppliers"),
                    calculated_at=now,
                )
            )
        else:
            kpis_supplier.append(
                BusinessKPIValue(
                    kpi_id=cat_sup_rate.kpi_id,
                    name=cat_sup_rate.name,
                    domain=KPIDomain.SUPPLIER,
                    status=KPIStatus.UNKNOWN,
                    value=None,
                    unit=KPIUnit.PERCENTAGE,
                    confidence=KPIConfidence.UNKNOWN,
                    source=cat_sup_rate.source,
                    formula_version=cat_sup_rate.formula_version,
                    formula_description=cat_sup_rate.formula_description,
                    inputs_missing=("total_suppliers",),
                    calculated_at=now,
                )
            )

        # ---------------------------------------------------------------------
        # 4. Dominio PROFIT (Q.3)
        # ---------------------------------------------------------------------
        kpis_profit: List[BusinessKPIValue] = []

        complete_profit_count = 0
        negative_margin_count = 0
        complete_margins: List[Decimal] = []
        projected_profit_by_curr: Dict[str, Decimal] = {}

        for p in profit_records:
            completeness = getattr(p, "completeness", None)
            is_complete = (
                completeness == ProfitCompleteness.COMPLETE or
                str(completeness).upper() == "COMPLETE"
            )
            if is_complete:
                complete_profit_count += 1
                margin = getattr(p, "margin_pct", None)
                if margin is not None:
                    complete_margins.append(margin)

            # Margen negativo
            p_margin = getattr(p, "margin_pct", None)
            p_profit = getattr(p, "contribution_profit", getattr(p, "gross_profit", None))
            if (p_margin is not None and p_margin < Decimal("0")) or (p_profit is not None and p_profit < Decimal("0")):
                negative_margin_count += 1

            # Desglose por divisa
            p_curr = getattr(p, "currency", None) or "USD"
            if p_curr not in currency_breakdown:
                currency_breakdown[p_curr] = {"total_known_agent_cost": Decimal("0"), "projected_profit": Decimal("0")}
            if p_profit is not None:
                projected_profit_by_curr[p_curr] = projected_profit_by_curr.get(p_curr, Decimal("0")) + p_profit
                currency_breakdown[p_curr]["projected_profit"] = projected_profit_by_curr[p_curr]

        # COMPLETE_PROFITABILITY_COUNT
        cat_comp_profit = self._catalog_map["COMPLETE_PROFITABILITY_COUNT"]
        kpis_profit.append(
            BusinessKPIValue(
                kpi_id=cat_comp_profit.kpi_id,
                name=cat_comp_profit.name,
                domain=KPIDomain.PROFIT,
                status=KPIStatus.CALCULATED,
                value=Decimal(complete_profit_count),
                unit=KPIUnit.COUNT,
                confidence=KPIConfidence.HIGH,
                source=cat_comp_profit.source,
                formula_version=cat_comp_profit.formula_version,
                formula_description=cat_comp_profit.formula_description,
                inputs_present=("profit_records", "completeness_status"),
                calculated_at=now,
            )
        )

        # AVG_CONTRIBUTION_MARGIN
        cat_avg_margin = self._catalog_map["AVG_CONTRIBUTION_MARGIN"]
        if complete_margins:
            avg_margin_val = (sum(complete_margins) / Decimal(len(complete_margins))).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            kpis_profit.append(
                BusinessKPIValue(
                    kpi_id=cat_avg_margin.kpi_id,
                    name=cat_avg_margin.name,
                    domain=KPIDomain.PROFIT,
                    status=KPIStatus.CALCULATED,
                    value=avg_margin_val,
                    unit=KPIUnit.PERCENTAGE,
                    confidence=KPIConfidence.HIGH if len(complete_margins) == len(profit_records) else KPIConfidence.MEDIUM,
                    source=cat_avg_margin.source,
                    formula_version=cat_avg_margin.formula_version,
                    formula_description=cat_avg_margin.formula_description,
                    inputs_present=("complete_profit_records", "contribution_margins"),
                    calculated_at=now,
                )
            )
        else:
            kpis_profit.append(
                BusinessKPIValue(
                    kpi_id=cat_avg_margin.kpi_id,
                    name=cat_avg_margin.name,
                    domain=KPIDomain.PROFIT,
                    status=KPIStatus.UNKNOWN,
                    value=None,
                    unit=KPIUnit.PERCENTAGE,
                    confidence=KPIConfidence.UNKNOWN,
                    source=cat_avg_margin.source,
                    formula_version=cat_avg_margin.formula_version,
                    formula_description=cat_avg_margin.formula_description,
                    inputs_missing=("complete_profit_records",),
                    calculated_at=now,
                )
            )
            data_quality_notes.append("No complete profit records found to compute AVG_CONTRIBUTION_MARGIN.")

        # NEGATIVE_MARGIN_COUNT
        cat_neg_margin = self._catalog_map["NEGATIVE_MARGIN_COUNT"]
        kpis_profit.append(
            BusinessKPIValue(
                kpi_id=cat_neg_margin.kpi_id,
                name=cat_neg_margin.name,
                domain=KPIDomain.PROFIT,
                status=KPIStatus.CALCULATED,
                value=Decimal(negative_margin_count),
                unit=KPIUnit.COUNT,
                confidence=KPIConfidence.HIGH,
                source=cat_neg_margin.source,
                formula_version=cat_neg_margin.formula_version,
                formula_description=cat_neg_margin.formula_description,
                inputs_present=("profit_records", "contribution_margins"),
                calculated_at=now,
            )
        )

        # ---------------------------------------------------------------------
        # 5. Dominio MISSION (Q.4)
        # ---------------------------------------------------------------------
        kpis_mission: List[BusinessKPIValue] = []

        active_missions_count = 0
        failed_missions_count = 0
        completed_missions_count = 0

        for m in mission_records:
            st = str(getattr(m, "status", "")).upper()
            if hasattr(getattr(m, "status", None), "value"):
                st = str(m.status.value).upper()

            if any(term in st for term in ("RUNNING", "PENDING", "IN_PROGRESS")):
                active_missions_count += 1
            elif any(term in st for term in ("FAILED", "ABORTED", "ERROR")):
                failed_missions_count += 1
            elif any(term in st for term in ("COMPLETED", "SUCCESS")):
                completed_missions_count += 1

        # ACTIVE_MISSIONS
        cat_act_mis = self._catalog_map["ACTIVE_MISSIONS"]
        kpis_mission.append(
            BusinessKPIValue(
                kpi_id=cat_act_mis.kpi_id,
                name=cat_act_mis.name,
                domain=KPIDomain.MISSION,
                status=KPIStatus.CALCULATED,
                value=Decimal(active_missions_count),
                unit=KPIUnit.COUNT,
                confidence=KPIConfidence.HIGH,
                source=cat_act_mis.source,
                formula_version=cat_act_mis.formula_version,
                formula_description=cat_act_mis.formula_description,
                inputs_present=("mission_records", "mission_status"),
                calculated_at=now,
            )
        )

        # FAILED_MISSIONS
        cat_fail_mis = self._catalog_map["FAILED_MISSIONS"]
        kpis_mission.append(
            BusinessKPIValue(
                kpi_id=cat_fail_mis.kpi_id,
                name=cat_fail_mis.name,
                domain=KPIDomain.MISSION,
                status=KPIStatus.CALCULATED,
                value=Decimal(failed_missions_count),
                unit=KPIUnit.COUNT,
                confidence=KPIConfidence.HIGH,
                source=cat_fail_mis.source,
                formula_version=cat_fail_mis.formula_version,
                formula_description=cat_fail_mis.formula_description,
                inputs_present=("mission_records", "mission_status"),
                calculated_at=now,
            )
        )

        # MISSION_SUCCESS_RATE (Denominator = Terminal missions only: Completed + Failed/Aborted)
        cat_succ_rate = self._catalog_map["MISSION_SUCCESS_RATE"]
        terminal_missions = completed_missions_count + failed_missions_count
        if terminal_missions > 0:
            succ_rate = ((Decimal(completed_missions_count) / Decimal(terminal_missions)) * Decimal("100")).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            kpis_mission.append(
                BusinessKPIValue(
                    kpi_id=cat_succ_rate.kpi_id,
                    name=cat_succ_rate.name,
                    domain=KPIDomain.MISSION,
                    status=KPIStatus.CALCULATED,
                    value=succ_rate,
                    unit=KPIUnit.PERCENTAGE,
                    confidence=KPIConfidence.HIGH,
                    source=cat_succ_rate.source,
                    formula_version=cat_succ_rate.formula_version,
                    formula_description=cat_succ_rate.formula_description,
                    inputs_present=("terminal_missions",),
                    calculated_at=now,
                )
            )
        else:
            kpis_mission.append(
                BusinessKPIValue(
                    kpi_id=cat_succ_rate.kpi_id,
                    name=cat_succ_rate.name,
                    domain=KPIDomain.MISSION,
                    status=KPIStatus.UNKNOWN,
                    value=None,
                    unit=KPIUnit.PERCENTAGE,
                    confidence=KPIConfidence.UNKNOWN,
                    source=cat_succ_rate.source,
                    formula_version=cat_succ_rate.formula_version,
                    formula_description=cat_succ_rate.formula_description,
                    inputs_missing=("terminal_missions",),
                    calculated_at=now,
                )
            )
            data_quality_notes.append("No terminal missions evaluated to compute MISSION_SUCCESS_RATE.")

        # ---------------------------------------------------------------------
        # 6. Dominio AGENT COST (Q.5)
        # ---------------------------------------------------------------------
        kpis_agent_cost: List[BusinessKPIValue] = []

        total_agent_costs_by_curr: Dict[str, Decimal] = cost_facts.get("costs_by_currency", {})
        mission_attributed_costs: Dict[str, Decimal] = cost_facts.get("mission_attributed_costs", {})
        missions_with_cost_count = cost_facts.get("missions_with_cost_count", 0)

        # Incorporar a currency breakdown
        for c_curr, c_val in total_agent_costs_by_curr.items():
            if c_curr not in currency_breakdown:
                currency_breakdown[c_curr] = {"total_known_agent_cost": Decimal("0"), "projected_profit": Decimal("0")}
            currency_breakdown[c_curr]["total_known_agent_cost"] = c_val

        # Determinar divisa principal / de consulta
        primary_curr = effective_query.currency or ("USD" if "USD" in total_agent_costs_by_curr else (
            next(iter(total_agent_costs_by_curr.keys())) if total_agent_costs_by_curr else "USD"
        ))

        total_cost_val: Optional[Decimal] = total_agent_costs_by_curr.get(primary_curr)
        if total_cost_val is None and len(total_agent_costs_by_curr) == 1:
            primary_curr, total_cost_val = next(iter(total_agent_costs_by_curr.items()))

        # TOTAL_AGENT_COST
        cat_tot_cost = self._catalog_map["TOTAL_AGENT_COST"]
        if effective_query.currency is None and len(total_agent_costs_by_curr) > 1:
            # Monedas heterogéneas sin filtro explícito -> NOT_COMPARABLE_CURRENCY
            kpis_agent_cost.append(
                BusinessKPIValue(
                    kpi_id=cat_tot_cost.kpi_id,
                    name=cat_tot_cost.name,
                    domain=KPIDomain.AGENT_COST,
                    status=KPIStatus.NOT_COMPARABLE_CURRENCY,
                    value=None,
                    currency=None,
                    unit=KPIUnit.CURRENCY,
                    confidence=KPIConfidence.LOW,
                    source=cat_tot_cost.source,
                    formula_version=cat_tot_cost.formula_version,
                    formula_description=cat_tot_cost.formula_description,
                    inputs_present=tuple(total_agent_costs_by_curr.keys()),
                    calculated_at=now,
                )
            )
            data_quality_notes.append("Multiple currencies present in agent costs without explicit currency filter.")
        elif total_cost_val is not None:
            kpis_agent_cost.append(
                BusinessKPIValue(
                    kpi_id=cat_tot_cost.kpi_id,
                    name=cat_tot_cost.name,
                    domain=KPIDomain.AGENT_COST,
                    status=KPIStatus.CALCULATED,
                    value=total_cost_val,
                    currency=primary_curr,
                    unit=KPIUnit.CURRENCY,
                    confidence=KPIConfidence.HIGH,
                    source=cat_tot_cost.source,
                    formula_version=cat_tot_cost.formula_version,
                    formula_description=cat_tot_cost.formula_description,
                    inputs_present=("cost_records", "currency"),
                    calculated_at=now,
                )
            )
        else:
            kpis_agent_cost.append(
                BusinessKPIValue(
                    kpi_id=cat_tot_cost.kpi_id,
                    name=cat_tot_cost.name,
                    domain=KPIDomain.AGENT_COST,
                    status=KPIStatus.UNKNOWN,
                    value=None,
                    currency=primary_curr,
                    unit=KPIUnit.CURRENCY,
                    confidence=KPIConfidence.UNKNOWN,
                    source=cat_tot_cost.source,
                    formula_version=cat_tot_cost.formula_version,
                    formula_description=cat_tot_cost.formula_description,
                    inputs_missing=("cost_records",),
                    calculated_at=now,
                )
            )
            data_quality_notes.append("No known agent cost records found to compute TOTAL_AGENT_COST.")

        # AVG_COST_PER_MISSION
        cat_avg_mis_cost = self._catalog_map["AVG_COST_PER_MISSION"]
        if missions_with_cost_count > 0 and primary_curr in mission_attributed_costs:
            avg_m_cost = (
                mission_attributed_costs[primary_curr] / Decimal(missions_with_cost_count)
            ).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            kpis_agent_cost.append(
                BusinessKPIValue(
                    kpi_id=cat_avg_mis_cost.kpi_id,
                    name=cat_avg_mis_cost.name,
                    domain=KPIDomain.AGENT_COST,
                    status=KPIStatus.CALCULATED,
                    value=avg_m_cost,
                    currency=primary_curr,
                    unit=KPIUnit.CURRENCY,
                    confidence=KPIConfidence.HIGH,
                    source=cat_avg_mis_cost.source,
                    formula_version=cat_avg_mis_cost.formula_version,
                    formula_description=cat_avg_mis_cost.formula_description,
                    inputs_present=("mission_attributed_costs", "mission_count"),
                    calculated_at=now,
                )
            )
        else:
            kpis_agent_cost.append(
                BusinessKPIValue(
                    kpi_id=cat_avg_mis_cost.kpi_id,
                    name=cat_avg_mis_cost.name,
                    domain=KPIDomain.AGENT_COST,
                    status=KPIStatus.UNKNOWN,
                    value=None,
                    currency=primary_curr,
                    unit=KPIUnit.CURRENCY,
                    confidence=KPIConfidence.UNKNOWN,
                    source=cat_avg_mis_cost.source,
                    formula_version=cat_avg_mis_cost.formula_version,
                    formula_description=cat_avg_mis_cost.formula_description,
                    inputs_missing=("mission_attributed_costs",),
                    calculated_at=now,
                )
            )

        # ---------------------------------------------------------------------
        # 7. Dominio CROSS-DOMAIN (Q.1 + Q.3 + Q.5)
        # ---------------------------------------------------------------------
        kpis_cross: List[BusinessKPIValue] = []

        # COST_PER_OPPORTUNITY
        cat_cost_per_opp = self._catalog_map["COST_PER_OPPORTUNITY"]
        if opp_count > 0 and total_cost_val is not None:
            cost_per_opp_val = (total_cost_val / Decimal(opp_count)).quantize(
                Decimal("0.0001"), rounding=ROUND_HALF_UP
            )
            kpis_cross.append(
                BusinessKPIValue(
                    kpi_id=cat_cost_per_opp.kpi_id,
                    name=cat_cost_per_opp.name,
                    domain=KPIDomain.CROSS_DOMAIN,
                    status=KPIStatus.CALCULATED,
                    value=cost_per_opp_val,
                    currency=primary_curr,
                    unit=KPIUnit.CURRENCY,
                    confidence=KPIConfidence.MEDIUM,
                    source=cat_cost_per_opp.source,
                    formula_version=cat_cost_per_opp.formula_version,
                    formula_description=cat_cost_per_opp.formula_description,
                    inputs_present=("discovery_ai_cost", "opportunities_detected"),
                    calculated_at=now,
                )
            )
        else:
            kpis_cross.append(
                BusinessKPIValue(
                    kpi_id=cat_cost_per_opp.kpi_id,
                    name=cat_cost_per_opp.name,
                    domain=KPIDomain.CROSS_DOMAIN,
                    status=KPIStatus.UNKNOWN,
                    value=None,
                    currency=primary_curr,
                    unit=KPIUnit.CURRENCY,
                    confidence=KPIConfidence.UNKNOWN,
                    source=cat_cost_per_opp.source,
                    formula_version=cat_cost_per_opp.formula_version,
                    formula_description=cat_cost_per_opp.formula_description,
                    inputs_missing=("discovery_ai_cost" if total_cost_val is None else "opportunities_detected",),
                    calculated_at=now,
                )
            )

        # PROJECTED_CONTRIBUTION_TO_AGENT_COST_RATIO
        cat_ratio = self._catalog_map["PROJECTED_CONTRIBUTION_TO_AGENT_COST_RATIO"]
        curr_profit = projected_profit_by_curr.get(primary_curr)
        if curr_profit is not None and total_cost_val is not None and total_cost_val > Decimal("0"):
            ratio_val = (curr_profit / total_cost_val).quantize(
                Decimal("0.0001"), rounding=ROUND_HALF_UP
            )
            kpis_cross.append(
                BusinessKPIValue(
                    kpi_id=cat_ratio.kpi_id,
                    name=cat_ratio.name,
                    domain=KPIDomain.CROSS_DOMAIN,
                    status=KPIStatus.CALCULATED,
                    value=ratio_val,
                    unit=KPIUnit.RATIO,
                    confidence=KPIConfidence.MEDIUM,
                    source=cat_ratio.source,
                    formula_version=cat_ratio.formula_version,
                    formula_description=cat_ratio.formula_description,
                    inputs_present=("projected_contribution_total", "total_agent_cost", "matching_currency"),
                    calculated_at=now,
                )
            )
        elif len(projected_profit_by_curr) > 0 and len(total_agent_costs_by_curr) > 0 and not set(projected_profit_by_curr.keys()).intersection(total_agent_costs_by_curr.keys()):
            kpis_cross.append(
                BusinessKPIValue(
                    kpi_id=cat_ratio.kpi_id,
                    name=cat_ratio.name,
                    domain=KPIDomain.CROSS_DOMAIN,
                    status=KPIStatus.NOT_COMPARABLE_CURRENCY,
                    value=None,
                    unit=KPIUnit.RATIO,
                    confidence=KPIConfidence.LOW,
                    source=cat_ratio.source,
                    formula_version=cat_ratio.formula_version,
                    formula_description=cat_ratio.formula_description,
                    inputs_missing=("matching_currency",),
                    calculated_at=now,
                )
            )
            data_quality_notes.append("Profit currencies and agent cost currencies do not overlap.")
        else:
            kpis_cross.append(
                BusinessKPIValue(
                    kpi_id=cat_ratio.kpi_id,
                    name=cat_ratio.name,
                    domain=KPIDomain.CROSS_DOMAIN,
                    status=KPIStatus.UNKNOWN,
                    value=None,
                    unit=KPIUnit.RATIO,
                    confidence=KPIConfidence.UNKNOWN,
                    source=cat_ratio.source,
                    formula_version=cat_ratio.formula_version,
                    formula_description=cat_ratio.formula_description,
                    inputs_missing=("projected_contribution_total" if curr_profit is None else "total_agent_cost",),
                    calculated_at=now,
                )
            )

        # ---------------------------------------------------------------------
        # 8. Dominio DATA QUALITY & OVERALL READINESS
        # ---------------------------------------------------------------------
        evaluated_so_far = (
            kpis_opportunity +
            kpis_supplier +
            kpis_profit +
            kpis_mission +
            kpis_agent_cost +
            kpis_cross
        )
        total_eval_count = len(evaluated_so_far) + 1  # Incluyendo OVERALL_DATA_READINESS
        calc_count = sum(1 for k in evaluated_so_far if k.status == KPIStatus.CALCULATED)

        # OVERALL_DATA_READINESS
        cat_readiness = self._catalog_map["OVERALL_DATA_READINESS"]
        # Se calcula el porcentaje agregando este KPI que siempre es computable
        readiness_pct = (
            (Decimal(calc_count + 1) / Decimal(total_eval_count)) * Decimal("100")
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        kpi_readiness = BusinessKPIValue(
            kpi_id=cat_readiness.kpi_id,
            name=cat_readiness.name,
            domain=KPIDomain.DATA_QUALITY,
            status=KPIStatus.CALCULATED,
            value=readiness_pct,
            unit=KPIUnit.PERCENTAGE,
            confidence=KPIConfidence.HIGH,
            source=cat_readiness.source,
            formula_version=cat_readiness.formula_version,
            formula_description=cat_readiness.formula_description,
            inputs_present=("all_kpi_statuses",),
            calculated_at=now,
        )
        kpis_data_quality = [kpi_readiness]

        all_kpis_tuple = tuple(
            kpis_opportunity +
            kpis_supplier +
            kpis_profit +
            kpis_mission +
            kpis_agent_cost +
            kpis_cross +
            kpis_data_quality
        )

        # ---------------------------------------------------------------------
        # 9. Domain Breakdowns
        # ---------------------------------------------------------------------
        domain_breakdowns: Dict[str, BusinessKPIDomainBreakdown] = {}

        def _build_breakdown(domain: KPIDomain, metrics: List[BusinessKPIValue]) -> BusinessKPIDomainBreakdown:
            tot = len(metrics)
            calc = sum(1 for m in metrics if m.status == KPIStatus.CALCULATED)
            unk = sum(1 for m in metrics if m.status in {KPIStatus.UNKNOWN, KPIStatus.INSUFFICIENT_DATA, KPIStatus.NOT_COMPARABLE_CURRENCY})
            comp_score = ((Decimal(calc) / Decimal(tot)) * Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if tot > 0 else None
            return BusinessKPIDomainBreakdown(
                domain=domain,
                total_metrics=tot,
                calculated_metrics=calc,
                unknown_metrics=unk,
                completeness_score_pct=comp_score,
                metrics=tuple(metrics),
                summary_notes=tuple(
                    f"{m.name}: {m.status.value}" for m in metrics if m.status != KPIStatus.CALCULATED
                ),
            )

        domain_breakdowns[KPIDomain.OPPORTUNITY.value] = _build_breakdown(KPIDomain.OPPORTUNITY, kpis_opportunity)
        domain_breakdowns[KPIDomain.SUPPLIER.value] = _build_breakdown(KPIDomain.SUPPLIER, kpis_supplier)
        domain_breakdowns[KPIDomain.PROFIT.value] = _build_breakdown(KPIDomain.PROFIT, kpis_profit)
        domain_breakdowns[KPIDomain.MISSION.value] = _build_breakdown(KPIDomain.MISSION, kpis_mission)
        domain_breakdowns[KPIDomain.AGENT_COST.value] = _build_breakdown(KPIDomain.AGENT_COST, kpis_agent_cost)
        domain_breakdowns[KPIDomain.CROSS_DOMAIN.value] = _build_breakdown(KPIDomain.CROSS_DOMAIN, kpis_cross)
        domain_breakdowns[KPIDomain.DATA_QUALITY.value] = _build_breakdown(KPIDomain.DATA_QUALITY, kpis_data_quality)

        drill_down_links = {
            "opportunity_dashboard": f"/api/bi/tenants/{tenant_id}/opportunities",
            "supplier_dashboard": f"/api/bi/tenants/{tenant_id}/suppliers",
            "profit_dashboard": f"/api/bi/tenants/{tenant_id}/profit",
            "mission_dashboard": f"/api/bi/tenants/{tenant_id}/missions",
            "agent_cost_dashboard": f"/api/bi/tenants/{tenant_id}/agent-costs",
        }

        # Serialización limpia para currency breakdown
        clean_currency_breakdown: Dict[str, Dict[str, Any]] = {}
        for curr, data in currency_breakdown.items():
            clean_currency_breakdown[curr] = {
                "total_known_agent_cost": str(data.get("total_known_agent_cost", Decimal("0"))),
                "projected_profit": str(data.get("projected_profit", Decimal("0"))),
            }

        return BusinessKPISummary(
            tenant_id=tenant_id,
            time_window=effective_query.time_window,
            period_start=effective_query.date_from,
            period_end=effective_query.date_to or now,
            currency_breakdown=clean_currency_breakdown,
            domain_breakdowns=domain_breakdowns,
            kpis=all_kpis_tuple,
            overall_readiness_pct=readiness_pct,
            data_quality_notes=tuple(data_quality_notes),
            generated_at=now,
            drill_down_links=drill_down_links,
        )

    # -------------------------------------------------------------------------
    # Métodos Privados de Recolección Eficiente de Hechos
    # -------------------------------------------------------------------------

    def _fetch_opportunities(self, context: TenantContext, query: BusinessKPIQuery) -> List[Any]:
        """Obtiene las oportunidades aplicando filtros de query sin duplicar lecturas."""
        if not self.opportunity_repository:
            return []
        try:
            records = self.opportunity_repository.list_all(context)
        except Exception:
            return []

        filtered: List[Any] = []
        for r in records:
            if query.marketplace:
                r_mkt = str(getattr(r, "marketplace", "")).lower()
                if query.marketplace.lower() not in r_mkt:
                    continue
            if query.category:
                r_cat = str(getattr(r, "category", "")).lower()
                if query.category.lower() not in r_cat:
                    continue
            if query.date_from:
                r_det = getattr(r, "detected_at", None)
                if r_det and r_det < query.date_from:
                    continue
            if query.date_to:
                r_det = getattr(r, "detected_at", None)
                if r_det and r_det > query.date_to:
                    continue
            filtered.append(r)
        return filtered

    def _fetch_suppliers(self, context: TenantContext, query: BusinessKPIQuery) -> List[Any]:
        """Obtiene los proveedores aplicando filtros de query."""
        if not self.supplier_repository:
            return []
        try:
            records = self.supplier_repository.list_all(context)
        except Exception:
            return []

        filtered: List[Any] = []
        for s in records:
            if query.date_from:
                s_up = getattr(s, "updated_at", getattr(s, "last_verified_at", None))
                if s_up and s_up < query.date_from:
                    continue
            if query.date_to:
                s_up = getattr(s, "updated_at", getattr(s, "last_verified_at", None))
                if s_up and s_up > query.date_to:
                    continue
            filtered.append(s)
        return filtered

    def _fetch_profit_items(self, context: TenantContext, query: BusinessKPIQuery) -> List[Any]:
        """Obtiene los ítems de rentabilidad aplicando filtros de query."""
        if not self.profit_repository:
            return []
        try:
            records = self.profit_repository.list_all(context)
        except Exception:
            return []

        filtered: List[Any] = []
        for p in records:
            if query.marketplace:
                p_mkt = str(getattr(p, "marketplace", "")).lower()
                if query.marketplace.lower() not in p_mkt:
                    continue
            if query.category:
                p_cat = str(getattr(p, "category", "")).lower()
                if query.category.lower() not in p_cat:
                    continue
            if query.currency:
                p_curr = str(getattr(p, "currency", "")).upper()
                if query.currency.upper() != p_curr:
                    continue
            if query.date_from:
                p_calc = getattr(p, "calculated_at", None)
                if p_calc and p_calc < query.date_from:
                    continue
            if query.date_to:
                p_calc = getattr(p, "calculated_at", None)
                if p_calc and p_calc > query.date_to:
                    continue
            filtered.append(p)
        return filtered

    def _fetch_missions(self, context: TenantContext, query: BusinessKPIQuery) -> List[Any]:
        """Obtiene las misiones registradas aplicando filtros de query."""
        if not self.mission_repository:
            return []
        try:
            records = self.mission_repository.list_all(context)
        except Exception:
            return []

        filtered: List[Any] = []
        for m in records:
            if query.mission_type:
                m_type = str(getattr(m, "mission_type", getattr(m, "type", ""))).lower()
                if query.mission_type.lower() != m_type:
                    continue
            if query.date_from:
                m_cr = getattr(m, "created_at", None)
                if m_cr and m_cr < query.date_from:
                    continue
            if query.date_to:
                m_cr = getattr(m, "created_at", None)
                if m_cr and m_cr > query.date_to:
                    continue
            filtered.append(m)
        return filtered

    def _fetch_agent_costs(
        self,
        tenant_id: str,
        session_id: Optional[str],
        query: BusinessKPIQuery,
    ) -> Dict[str, Any]:
        """
        Obtiene los costos de agentes agregados delegando en AgentCostDashboardService
        o directamente consultando repositorios de Cost / Usage.
        """
        costs_by_curr: Dict[str, Decimal] = {}
        mission_attributed: Dict[str, Decimal] = {}
        missions_with_cost: set = set()

        # Estrategia 1: Delegar en AgentCostDashboardService si está disponible
        if self.agent_cost_service is not None and hasattr(self.agent_cost_service, "get_summary") and session_id:
            try:
                summary = self.agent_cost_service.get_summary(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    date_from=query.date_from,
                    date_to=query.date_to,
                )
                for cb in summary.currency_breakdowns:
                    curr = cb.currency
                    costs_by_curr[curr] = cb.total_known_cost

                for mb in summary.mission_costs:
                    missions_with_cost.add(mb.mission_id)
                    for curr, amt in mb.cost_by_currency.items():
                        mission_attributed[curr] = mission_attributed.get(curr, Decimal("0")) + amt

                return {
                    "costs_by_currency": costs_by_curr,
                    "mission_attributed_costs": mission_attributed,
                    "missions_with_cost_count": len(missions_with_cost),
                }
            except Exception:
                pass

        # Estrategia 2: Consultar CostRepository directamente
        if self.cost_repository is not None:
            try:
                if hasattr(self.cost_repository, "list_by_tenant"):
                    records = self.cost_repository.list_by_tenant(tenant_id)
                elif hasattr(self.cost_repository, "list_records"):
                    records = self.cost_repository.list_records(
                        from_time=query.date_from,
                        to_time=query.date_to,
                        currency=query.currency,
                    )
                else:
                    records = []

                for cr in records:
                    amt = getattr(cr, "total_cost", getattr(cr, "amount", getattr(cr, "cost", None)))
                    curr = getattr(cr, "currency", "USD") or "USD"
                    if amt is not None:
                        try:
                            dec_amt = Decimal(str(amt))
                            costs_by_curr[curr] = costs_by_curr.get(curr, Decimal("0")) + dec_amt
                        except Exception:
                            pass

                    m_id = getattr(cr, "mission_id", None)
                    if m_id and amt is not None:
                        missions_with_cost.add(m_id)
                        try:
                            dec_amt = Decimal(str(amt))
                            mission_attributed[curr] = mission_attributed.get(curr, Decimal("0")) + dec_amt
                        except Exception:
                            pass

                return {
                    "costs_by_currency": costs_by_curr,
                    "mission_attributed_costs": mission_attributed,
                    "missions_with_cost_count": len(missions_with_cost),
                }
            except Exception:
                pass

        return {
            "costs_by_currency": costs_by_curr,
            "mission_attributed_costs": mission_attributed,
            "missions_with_cost_count": len(missions_with_cost),
        }
