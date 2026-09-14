"""
Servicio de Aplicación para Q.3 — Profit Dashboard (Hito Q — Business Intelligence).

Responsabilidades:
1. Validar autenticación de sesión SaaS (O.3) y permisos RBAC / SaaS Authorization (O.4 / N.4).
   Acción canónica: PROFIT_DASHBOARD_READ / BUSINESS_INTELLIGENCE_READ.
2. Garantizar aislamiento multi-tenant estricto (O.1): Tenant A jamás ve rentabilidad de Tenant B.
3. Consultar y proyectar datos de rentabilidad existentes desde el repositorio durable o computar proyecciones seguras basadas en facts reales.
4. Aplicar filtros combinados (marketplace, categoría, supplier_id, opportunity_id, margen min/max, profit min/max, precio min/max, completitud, divisa, búsqueda).
5. Aplicar ordenación determinista con regla de desempate estable por item_id.
6. Aplicar paginación segura y acotada (page, page_size <= 100).
7. Proyectar agregados de resumen estadístico real (ProfitDashboardSummary).
8. Proyectar detalle seguro, explicabilidad estructurada de fórmulas y hechos (ProfitDashboardDetail).
9. Proveer comparación multidimensional determinista entre 2 o más ítems de rentabilidad (ProfitComparisonView).
10. Preservar semántica de incertidumbre: UNKNOWN != 0, UNKNOWN != FREE, y cantidades monetarias en Decimal.
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, List, Dict, Any, Tuple, Sequence
import math

from src.domain.tenant.models import TenantContext
from src.domain.tenant.guard import CrossTenantGuard
from src.domain.security.models import validate_safe_identifier, sanitize_security_data
from src.domain.market_intelligence.models import Confidence
from src.domain.profit.models import (
    CostComponentType,
    CostComponentStatus,
    CostComponent,
    SalePrice,
    Revenue,
    LandedCost,
    UnitEconomics,
    ExchangeRate,
    ProfitStatus,
    LandedCostStatus,
)
from src.domain.profit.engine import (
    LandedCostCalculator,
    UnitEconomicsCalculator,
    BreakEvenCalculator,
)
from src.domain.profit_dashboard.models import (
    ProfitSortField,
    SortOrder,
    ProfitCompleteness,
    ProfitDashboardItem,
    ProfitDashboardSummary,
    ProfitDashboardDetail,
    ProfitDashboardQuery,
    ProfitDashboardPage,
    ProfitComparisonDimension,
    ProfitComparisonView,
)
from src.domain.profit_dashboard.ports import TenantProfitRepositoryPort
from src.domain.opportunity_dashboard.ports import TenantOpportunityRepositoryPort
from src.domain.supplier_dashboard.ports import TenantSupplierRepositoryPort
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


class ProfitDashboardAuthenticationError(AdminAuthenticationError):
    """Lanzada cuando la sesión es inválida, expirada o ausente."""
    pass


class ProfitDashboardAuthorizationError(AdminAuthorizationError):
    """Lanzada cuando el usuario o tenant no tiene permisos para ver el dashboard de rentabilidad."""
    pass


class ProfitNotFoundError(AdminResourceNotFoundError):
    """Lanzada cuando el ítem de rentabilidad no existe en el tenant especificado."""
    pass


class ProfitDashboardService:
    """
    Servicio de Aplicación para el Profit Dashboard (Q.3).
    """

    def __init__(
        self,
        repository: TenantProfitRepositoryPort,
        opportunity_repository: Optional[TenantOpportunityRepositoryPort] = None,
        supplier_repository: Optional[TenantSupplierRepositoryPort] = None,
        authorization_service: Optional[SaaSAuthorizationService] = None,
        session_repository: Optional[SaaSSessionRepositoryPort] = None,
        clock: Optional[ClockPort] = None,
    ):
        self.repository = repository
        self.opportunity_repository = opportunity_repository
        self.supplier_repository = supplier_repository
        self.authorization_service = authorization_service
        self.session_repository = session_repository
        self.clock = clock or SystemClock()

    def _authenticate_and_authorize(
        self,
        tenant_id: str,
        session_id: Optional[str],
        action: str = "PROFIT_DASHBOARD_READ",
        resource_id: Optional[str] = None,
    ) -> TenantContext:
        validate_safe_identifier(tenant_id, field_name="tenant_id")

        if self.authorization_service is not None:
            if not session_id:
                raise ProfitDashboardAuthenticationError("Authentication required: missing session_id")
            auth_req = SaaSAuthorizationRequest(
                action=action,
                session_id=session_id,
                tenant_id=tenant_id,
                resource=resource_id,
            )
            decision = self.authorization_service.authorize(auth_req)
            if not decision.is_allowed:
                auth_reasons = {
                    SaaSAuthorizationReasonCode.SESSION_INVALID,
                    SaaSAuthorizationReasonCode.SESSION_NOT_FOUND,
                    SaaSAuthorizationReasonCode.SESSION_EXPIRED,
                    SaaSAuthorizationReasonCode.SESSION_REVOKED,
                }
                if decision.reason_code in auth_reasons:
                    raise ProfitDashboardAuthenticationError(
                        f"Authentication failed: {decision.reason_code.value}"
                    )
                raise ProfitDashboardAuthorizationError(
                    f"Access denied for action '{action}' on tenant '{tenant_id}': {decision.reason_code.value}"
                )

        elif self.session_repository is not None and session_id:
            session = self.session_repository.get_by_id(session_id)
            if not session:
                raise ProfitDashboardAuthenticationError(f"Session '{session_id}' not found")
            now = self.clock.now()
            if session.is_expired(now):
                raise ProfitDashboardAuthenticationError(f"Session '{session_id}' has expired")
            if session.status != SessionStatus.ACTIVE:
                raise ProfitDashboardAuthenticationError(f"Session '{session_id}' is not active")
            if session.tenant_id != tenant_id:
                raise ProfitDashboardAuthorizationError(
                    f"Cross-tenant access violation: session tenant '{session.tenant_id}' does not match target tenant '{tenant_id}'"
                )

        return TenantContext(tenant_id=tenant_id)

    @staticmethod
    def compute_profit_item_from_facts(
        item_id: str,
        product_id: str,
        marketplace: str,
        currency: str,
        sale_price_amount: Optional[Decimal],
        unit_cost_amount: Optional[Decimal],
        shipping_cost_amount: Optional[Decimal] = None,
        marketplace_fee_rate: Optional[Decimal] = None,
        marketplace_fee_amount: Optional[Decimal] = None,
        payment_fee_rate: Optional[Decimal] = None,
        payment_fee_amount: Optional[Decimal] = None,
        tax_amount: Optional[Decimal] = None,
        other_costs_amount: Optional[Decimal] = None,
        cost_currency: Optional[str] = None,
        exchange_rate: Optional[Decimal] = None,
        opportunity_id: Optional[str] = None,
        supplier_id: Optional[str] = None,
        supplier_name: Optional[str] = None,
        title: Optional[str] = None,
        category: Optional[str] = None,
        product_sku: Optional[str] = None,
        confidence: Optional[str] = None,
        calculated_at: Optional[datetime] = None,
    ) -> ProfitDashboardItem:
        """
        Calcula determinísticamente un ProfitDashboardItem aplicando las fórmulas canónicas
        y las reglas de incompletitud financiera (UNKNOWN != 0).
        """
        validate_safe_identifier(item_id, field_name="item_id")
        validate_safe_identifier(product_id, field_name="product_id")
        calc_time = calculated_at or datetime.now(timezone.utc)

        missing_components: List[str] = []
        unknown_fields: List[str] = []

        # Divisas
        c_currency = cost_currency or currency
        if c_currency != currency and (exchange_rate is None or exchange_rate <= Decimal("0")):
            # Monedas distintas sin tasa FX válida
            return ProfitDashboardItem(
                item_id=item_id,
                product_id=product_id,
                opportunity_id=opportunity_id,
                supplier_id=supplier_id,
                supplier_name=supplier_name,
                marketplace=marketplace,
                title=title,
                category=category,
                product_sku=product_sku,
                currency=currency,
                completeness=ProfitCompleteness.NOT_COMPARABLE_CURRENCY,
                calculated_at=calc_time,
                sale_price=sale_price_amount,
                unit_cost=unit_cost_amount,
                shipping_cost=shipping_cost_amount,
                confidence=confidence or "UNKNOWN",
                missing_cost_components=("EXCHANGE_RATE_MISSING",),
                unknown_fields=("cost_currency_mismatch",),
            )

        fx = exchange_rate if c_currency != currency and exchange_rate else None

        # Conversión de costos a currency de venta
        eff_unit_cost = (unit_cost_amount * fx) if (unit_cost_amount is not None and fx is not None) else unit_cost_amount
        eff_shipping_cost = (shipping_cost_amount * fx) if (shipping_cost_amount is not None and fx is not None) else shipping_cost_amount
        eff_other_costs = (other_costs_amount * fx) if (other_costs_amount is not None and fx is not None) else other_costs_amount
        eff_tax_cost = (tax_amount * fx) if (tax_amount is not None and fx is not None) else tax_amount

        # Evaluación de Sale Price
        if sale_price_amount is None or sale_price_amount <= Decimal("0"):
            missing_components.append("SALE_PRICE")
            unknown_fields.append("sale_price")

        # Evaluación de Unit Cost
        if eff_unit_cost is None:
            missing_components.append("UNIT_COST")
            unknown_fields.append("unit_cost")

        # Evaluación de Shipping Cost
        if eff_shipping_cost is None:
            missing_components.append("SHIPPING_COST")
            unknown_fields.append("shipping_cost")

        # Marketplace Fee
        eff_marketplace_fee: Optional[Decimal] = None
        if marketplace_fee_amount is not None:
            eff_marketplace_fee = (marketplace_fee_amount * fx) if fx is not None else marketplace_fee_amount
        elif marketplace_fee_rate is not None and sale_price_amount is not None and sale_price_amount > Decimal("0"):
            eff_marketplace_fee = sale_price_amount * marketplace_fee_rate
        else:
            missing_components.append("MARKETPLACE_FEE")
            unknown_fields.append("marketplace_fee")

        # Payment Fee
        eff_payment_fee: Optional[Decimal] = None
        if payment_fee_amount is not None:
            eff_payment_fee = (payment_fee_amount * fx) if fx is not None else payment_fee_amount
        elif payment_fee_rate is not None and sale_price_amount is not None and sale_price_amount > Decimal("0"):
            eff_payment_fee = sale_price_amount * payment_fee_rate

        # Tax Cost
        if eff_tax_cost is None:
            missing_components.append("TAX_COST")

        # Other Costs
        if eff_other_costs is None:
            missing_components.append("OTHER_COSTS")

        # Determinar completitud
        # Componentes críticos mínimos: sale_price, unit_cost, shipping_cost, marketplace_fee
        has_critical_missing = any(c in missing_components for c in ("SALE_PRICE", "UNIT_COST", "SHIPPING_COST", "MARKETPLACE_FEE"))
        if sale_price_amount is None or eff_unit_cost is None:
            completeness = ProfitCompleteness.INSUFFICIENT_DATA
        elif has_critical_missing:
            completeness = ProfitCompleteness.PARTIAL
        else:
            completeness = ProfitCompleteness.COMPLETE

        # Total known cost: suma EXCLUSIVAMENTE componentes conocidos (no asume 0 para desconocidos)
        known_cost_parts = [c for c in [eff_unit_cost, eff_shipping_cost, eff_marketplace_fee, eff_payment_fee, eff_tax_cost, eff_other_costs] if c is not None]
        total_known_cost = sum(known_cost_parts) if known_cost_parts else None

        # Gross profit = sale_price - unit_cost (si ambos conocidos)
        gross_profit: Optional[Decimal] = None
        if sale_price_amount is not None and eff_unit_cost is not None:
            gross_profit = sale_price_amount - eff_unit_cost

        # Contribution/Net profit = sale_price - total_known_cost (si ambos conocidos)
        contribution_profit: Optional[Decimal] = None
        margin_pct: Optional[Decimal] = None
        markup_pct: Optional[Decimal] = None
        break_even_price: Optional[Decimal] = None

        if sale_price_amount is not None and total_known_cost is not None and eff_unit_cost is not None:
            contribution_profit = sale_price_amount - total_known_cost
            if sale_price_amount > Decimal("0"):
                margin_pct = (contribution_profit / sale_price_amount) * Decimal("100")
            if eff_unit_cost > Decimal("0"):
                markup_pct = (contribution_profit / eff_unit_cost) * Decimal("100")

        # Break-Even Price determinista:
        # BE = (Unit Cost + Shipping + Fixed Costs) / (1 - Variable Fee Rates)
        var_fee_rate = Decimal("0")
        if marketplace_fee_rate is not None:
            var_fee_rate += marketplace_fee_rate
        if payment_fee_rate is not None:
            var_fee_rate += payment_fee_rate

        fixed_base = Decimal("0")
        if eff_unit_cost is not None:
            fixed_base += eff_unit_cost
        if eff_shipping_cost is not None:
            fixed_base += eff_shipping_cost
        if eff_other_costs is not None:
            fixed_base += eff_other_costs

        if fixed_base > Decimal("0") and var_fee_rate < Decimal("1.0"):
            break_even_price = fixed_base / (Decimal("1.0") - var_fee_rate)

        return ProfitDashboardItem(
            item_id=item_id,
            product_id=product_id,
            opportunity_id=opportunity_id,
            supplier_id=supplier_id,
            supplier_name=supplier_name,
            marketplace=marketplace,
            title=title,
            category=category,
            product_sku=product_sku,
            currency=currency,
            completeness=completeness,
            calculated_at=calc_time,
            sale_price=sale_price_amount,
            unit_cost=eff_unit_cost,
            shipping_cost=eff_shipping_cost,
            marketplace_fee=eff_marketplace_fee,
            payment_fee=eff_payment_fee,
            tax_cost=eff_tax_cost,
            other_costs=eff_other_costs,
            total_known_cost=total_known_cost,
            gross_profit=gross_profit,
            contribution_profit=contribution_profit,
            margin_pct=margin_pct,
            markup_pct=markup_pct,
            break_even_sale_price=break_even_price,
            confidence=confidence or ("HIGH" if completeness == ProfitCompleteness.COMPLETE else "MEDIUM"),
            missing_cost_components=tuple(missing_components),
            unknown_fields=tuple(unknown_fields),
        )

    def list_profit_items(
        self,
        tenant_id: str,
        query: Optional[ProfitDashboardQuery] = None,
        session_id: Optional[str] = None,
    ) -> ProfitDashboardPage:
        context = self._authenticate_and_authorize(tenant_id=tenant_id, session_id=session_id)
        q = query or ProfitDashboardQuery()

        all_items = self.repository.list_all(context)

        # Filtrado
        filtered: List[ProfitDashboardItem] = []
        for item in all_items:
            if q.marketplace and item.marketplace.lower() != q.marketplace.lower():
                continue
            if q.category and (item.category is None or q.category.lower() not in item.category.lower()):
                continue
            if q.supplier_id and item.supplier_id != q.supplier_id:
                continue
            if q.opportunity_id and item.opportunity_id != q.opportunity_id:
                continue
            if q.currency and item.currency.upper() != q.currency.upper():
                continue
            if q.completeness and item.completeness != q.completeness:
                continue
            if q.min_margin_pct is not None and (item.margin_pct is None or item.margin_pct < q.min_margin_pct):
                continue
            if q.max_margin_pct is not None and (item.margin_pct is None or item.margin_pct > q.max_margin_pct):
                continue
            if q.min_profit_amount is not None and (item.contribution_profit is None or item.contribution_profit < q.min_profit_amount):
                continue
            if q.max_profit_amount is not None and (item.contribution_profit is None or item.contribution_profit > q.max_profit_amount):
                continue
            if q.min_sale_price is not None and (item.sale_price is None or item.sale_price < q.min_sale_price):
                continue
            if q.max_sale_price is not None and (item.sale_price is None or item.sale_price > q.max_sale_price):
                continue
            if q.search_text:
                st = q.search_text.lower()
                matches = (
                    (item.title and st in item.title.lower()) or
                    (item.product_id and st in item.product_id.lower()) or
                    (item.product_sku and st in item.product_sku.lower()) or
                    (item.supplier_name and st in item.supplier_name.lower()) or
                    (item.category and st in item.category.lower())
                )
                if not matches:
                    continue
            filtered.append(item)

        # Ordenación determinista
        def sort_key(item: ProfitDashboardItem) -> Tuple[int, Any, str]:
            field_val = None
            if q.sort_by == ProfitSortField.MARGIN:
                field_val = item.margin_pct
            elif q.sort_by == ProfitSortField.PROFIT:
                field_val = item.contribution_profit
            elif q.sort_by == ProfitSortField.SALE_PRICE:
                field_val = item.sale_price
            elif q.sort_by == ProfitSortField.UNIT_COST:
                field_val = item.unit_cost
            elif q.sort_by in (ProfitSortField.CALCULATED_AT, ProfitSortField.UPDATED_AT):
                field_val = item.calculated_at
            elif q.sort_by == ProfitSortField.ITEM_ID:
                field_val = item.item_id

            # NULLs al final
            if field_val is None:
                return (1, Decimal("0") if q.sort_by != ProfitSortField.ITEM_ID else "", item.item_id)
            return (0, field_val, item.item_id)

        reverse = (q.sort_order == SortOrder.DESC)
        filtered.sort(key=sort_key, reverse=reverse)

        # Paginación
        total_count = len(filtered)
        total_pages = max(1, math.ceil(total_count / q.page_size)) if total_count > 0 else 1
        page = min(max(1, q.page), total_pages)
        start_idx = (page - 1) * q.page_size
        end_idx = start_idx + q.page_size
        page_items = filtered[start_idx:end_idx]

        return ProfitDashboardPage(
            items=page_items,
            total_count=total_count,
            page=page,
            page_size=q.page_size,
            total_pages=total_pages,
            has_next=page < total_pages,
            has_previous=page > 1,
        )

    def get_summary(
        self,
        tenant_id: str,
        session_id: Optional[str] = None,
    ) -> ProfitDashboardSummary:
        context = self._authenticate_and_authorize(tenant_id=tenant_id, session_id=session_id)
        all_items = self.repository.list_all(context)

        total_items = len(all_items)
        complete_count = 0
        partial_count = 0
        insufficient_count = 0
        neg_margin_count = 0
        pos_margin_count = 0

        margin_values: List[Decimal] = []
        by_mkt: Dict[str, int] = {}
        by_cat: Dict[str, int] = {}
        by_comp: Dict[str, int] = {}

        for it in all_items:
            # Conteo por completitud
            c_str = it.completeness.value if hasattr(it.completeness, "value") else str(it.completeness)
            by_comp[c_str] = by_comp.get(c_str, 0) + 1
            if it.completeness == ProfitCompleteness.COMPLETE:
                complete_count += 1
            elif it.completeness == ProfitCompleteness.PARTIAL:
                partial_count += 1
            else:
                insufficient_count += 1

            # Marketplace
            mkt = it.marketplace or "unknown"
            by_mkt[mkt] = by_mkt.get(mkt, 0) + 1

            # Categoría
            cat = it.category or "Uncategorized"
            by_cat[cat] = by_cat.get(cat, 0) + 1

            # Márgenes
            if it.margin_pct is not None:
                margin_values.append(it.margin_pct)
                if it.margin_pct < Decimal("0"):
                    neg_margin_count += 1
                else:
                    pos_margin_count += 1

        avg_margin = (sum(margin_values) / Decimal(str(len(margin_values)))) if margin_values else None
        highest_margin = max(margin_values) if margin_values else None
        lowest_margin = min(margin_values) if margin_values else None

        return ProfitDashboardSummary(
            tenant_id=tenant_id,
            total_items=total_items,
            complete_profitability_count=complete_count,
            partial_profitability_count=partial_count,
            insufficient_data_count=insufficient_count,
            negative_margin_count=neg_margin_count,
            positive_margin_count=pos_margin_count,
            average_contribution_margin_pct=avg_margin,
            highest_margin_pct=highest_margin,
            lowest_margin_pct=lowest_margin,
            items_by_marketplace=by_mkt,
            items_by_category=by_cat,
            items_by_completeness=by_comp,
            generated_at=self.clock.now(),
        )

    def get_profit_detail(
        self,
        tenant_id: str,
        item_id: str,
        session_id: Optional[str] = None,
    ) -> ProfitDashboardDetail:
        context = self._authenticate_and_authorize(
            tenant_id=tenant_id,
            session_id=session_id,
            resource_id=item_id,
        )
        item = self.repository.get_by_id(context, item_id)
        if not item:
            raise ProfitNotFoundError(f"Profit item '{item_id}' not found for tenant '{tenant_id}'")

        # Armar desglose de componentes
        breakdown: List[Dict[str, Any]] = []
        if item.unit_cost is not None:
            breakdown.append({"component": "UNIT_COST", "amount": str(item.unit_cost), "currency": item.currency, "status": "KNOWN"})
        else:
            breakdown.append({"component": "UNIT_COST", "amount": None, "currency": item.currency, "status": "UNKNOWN"})

        if item.shipping_cost is not None:
            breakdown.append({"component": "SHIPPING_COST", "amount": str(item.shipping_cost), "currency": item.currency, "status": "KNOWN"})
        else:
            breakdown.append({"component": "SHIPPING_COST", "amount": None, "currency": item.currency, "status": "UNKNOWN"})

        if item.marketplace_fee is not None:
            breakdown.append({"component": "MARKETPLACE_FEE", "amount": str(item.marketplace_fee), "currency": item.currency, "status": "KNOWN"})
        else:
            breakdown.append({"component": "MARKETPLACE_FEE", "amount": None, "currency": item.currency, "status": "UNKNOWN"})

        if item.payment_fee is not None:
            breakdown.append({"component": "PAYMENT_FEE", "amount": str(item.payment_fee), "currency": item.currency, "status": "KNOWN"})

        if item.tax_cost is not None:
            breakdown.append({"component": "TAX_COST", "amount": str(item.tax_cost), "currency": item.currency, "status": "KNOWN"})
        else:
            breakdown.append({"component": "TAX_COST", "amount": None, "currency": item.currency, "status": "UNKNOWN"})

        if item.other_costs is not None:
            breakdown.append({"component": "OTHER_COSTS", "amount": str(item.other_costs), "currency": item.currency, "status": "KNOWN"})

        # Hechos financieros y trazabilidad
        facts: List[str] = [
            f"Financial Completeness: {item.completeness.value if hasattr(item.completeness, 'value') else item.completeness}",
            f"Calculated At: {item.calculated_at.isoformat()}",
            f"Marketplace: {item.marketplace}",
            f"Currency: {item.currency}",
        ]
        if item.sale_price is not None:
            facts.append(f"Sale Price: {item.sale_price} {item.currency}")
        if item.total_known_cost is not None:
            facts.append(f"Total Known Cost: {item.total_known_cost} {item.currency}")
        if item.gross_profit is not None:
            facts.append(f"Gross Profit: {item.gross_profit} {item.currency}")
        if item.contribution_profit is not None:
            facts.append(f"Contribution Profit: {item.contribution_profit} {item.currency}")
        if item.margin_pct is not None:
            facts.append(f"Contribution Margin: {item.margin_pct:.2f}%")

        formula_breakdown = (
            "Contribution Profit = Sale Price "
            "- Unit Cost "
            "- Shipping Cost "
            "- Marketplace Fees "
            "- Payment Fees "
            "- Known Taxes "
            "- Other Known Costs"
        )

        # Enriquecer con oportunidad asociada si existe repositorio
        assoc_opp: Optional[Dict[str, Any]] = None
        if item.opportunity_id and self.opportunity_repository is not None:
            try:
                opp = self.opportunity_repository.get_by_id(context, item.opportunity_id)
                if opp:
                    assoc_opp = {
                        "opportunity_id": opp.opportunity_id,
                        "title": opp.title,
                        "status": opp.status.value if hasattr(opp.status, "value") else str(opp.status),
                        "score": str(opp.opportunity_score) if opp.opportunity_score is not None else None,
                    }
            except Exception:
                assoc_opp = None

        # Enriquecer con proveedor asociado si existe repositorio
        assoc_sup: Optional[Dict[str, Any]] = None
        if item.supplier_id and self.supplier_repository is not None:
            try:
                sup = self.supplier_repository.get_by_id(context, item.supplier_id)
                if sup:
                    assoc_sup = {
                        "supplier_id": sup.supplier_id,
                        "name": sup.name,
                        "source": sup.source,
                        "status": sup.status.value if hasattr(sup.status, "value") else str(sup.status),
                    }
            except Exception:
                assoc_sup = None

        return ProfitDashboardDetail(
            item=item,
            cost_breakdown=tuple(breakdown),
            financial_facts=tuple(facts),
            missing_components=item.missing_cost_components,
            formula_breakdown=formula_breakdown,
            associated_opportunity=assoc_opp,
            associated_supplier=assoc_sup,
        )

    def compare_profit_items(
        self,
        tenant_id: str,
        item_ids: Sequence[str],
        session_id: Optional[str] = None,
    ) -> ProfitComparisonView:
        context = self._authenticate_and_authorize(tenant_id=tenant_id, session_id=session_id)
        if not item_ids or len(item_ids) < 2:
            raise AdminInvalidRequestError("At least 2 profit items are required for comparison.")

        items: List[ProfitDashboardItem] = []
        for i_id in item_ids:
            validate_safe_identifier(i_id, field_name="item_id")
            it = self.repository.get_by_id(context, i_id)
            if not it:
                raise ProfitNotFoundError(f"Profit item '{i_id}' not found for tenant '{tenant_id}'")
            items.append(it)

        # Construir dimensiones comparativas
        dims: List[ProfitComparisonDimension] = [
            ProfitComparisonDimension(
                dimension_name="Sale Price",
                description="Market sale price per unit",
                values_by_item={it.item_id: str(it.sale_price) if it.sale_price is not None else "UNKNOWN" for it in items},
                unit=items[0].currency,
            ),
            ProfitComparisonDimension(
                dimension_name="Unit Cost",
                description="Acquisition or supplier product cost per unit",
                values_by_item={it.item_id: str(it.unit_cost) if it.unit_cost is not None else "UNKNOWN" for it in items},
                unit=items[0].currency,
            ),
            ProfitComparisonDimension(
                dimension_name="Shipping Cost",
                description="Shipping and freight cost per unit",
                values_by_item={it.item_id: str(it.shipping_cost) if it.shipping_cost is not None else "UNKNOWN" for it in items},
                unit=items[0].currency,
            ),
            ProfitComparisonDimension(
                dimension_name="Marketplace Fee",
                description="Channel fee per transaction/listing",
                values_by_item={it.item_id: str(it.marketplace_fee) if it.marketplace_fee is not None else "UNKNOWN" for it in items},
                unit=items[0].currency,
            ),
            ProfitComparisonDimension(
                dimension_name="Total Known Cost",
                description="Sum of all currently known cost components",
                values_by_item={it.item_id: str(it.total_known_cost) if it.total_known_cost is not None else "UNKNOWN" for it in items},
                unit=items[0].currency,
            ),
            ProfitComparisonDimension(
                dimension_name="Gross Profit",
                description="Sale Price minus Unit Landed Cost",
                values_by_item={it.item_id: str(it.gross_profit) if it.gross_profit is not None else "UNKNOWN" for it in items},
                unit=items[0].currency,
            ),
            ProfitComparisonDimension(
                dimension_name="Contribution Profit",
                description="Net contribution after all known variable costs and channel fees",
                values_by_item={it.item_id: str(it.contribution_profit) if it.contribution_profit is not None else "UNKNOWN" for it in items},
                unit=items[0].currency,
            ),
            ProfitComparisonDimension(
                dimension_name="Margin %",
                description="Contribution margin percentage over sale price",
                values_by_item={it.item_id: f"{it.margin_pct:.2f}%" if it.margin_pct is not None else "UNKNOWN" for it in items},
                unit="%",
            ),
            ProfitComparisonDimension(
                dimension_name="Completeness",
                description="Data integrity and cost traceability status",
                values_by_item={it.item_id: it.completeness.value if hasattr(it.completeness, "value") else str(it.completeness) for it in items},
                unit="",
            ),
        ]

        summary_comp: Dict[str, Any] = {
            "item_count": len(items),
            "complete_items_count": sum(1 for it in items if it.completeness == ProfitCompleteness.COMPLETE),
            "currencies_match": len(set(it.currency for it in items)) == 1,
            "compared_at": self.clock.now().isoformat(),
        }

        return ProfitComparisonView(
            item_ids=tuple(it.item_id for it in items),
            items=tuple(items),
            dimensions=tuple(dims),
            summary_comparison=summary_comp,
            compared_at=self.clock.now(),
        )
