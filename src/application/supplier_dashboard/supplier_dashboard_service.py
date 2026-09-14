"""
Servicio de Aplicación para Q.2 — Supplier Dashboard (Hito Q — Business Intelligence).

Responsabilidades:
1. Validar autenticación de sesión SaaS (O.3) y permisos RBAC / SaaS Authorization (O.4 / N.4).
   Acción canónica: SUPPLIER_DASHBOARD_READ / BUSINESS_INTELLIGENCE_READ.
2. Garantizar aislamiento multi-tenant estricto (O.1): Tenant A jamás ve proveedores de Tenant B.
3. Consultar y proyectar datos de proveedores existentes desde el repositorio durable.
4. Aplicar filtros combinados (fuente/plataforma, país, estado de verificación, riesgo, score min/max, costo máx, MOQ máx, lead time máx, confianza, sku/oportunidad, búsqueda).
5. Aplicar ordenación determinista con regla de desempate estable por supplier_id.
6. Aplicar paginación segura y acotada (page, page_size <= 100).
7. Proyectar agregados de resumen estadístico real (SupplierDashboardSummary).
8. Proyectar detalle seguro, explicabilidad estructurada y sanitización de datos de contacto (SupplierDashboardDetail).
9. Proveer comparación multidimensional determinista entre 2 o más proveedores (SupplierComparisonView).
10. Preservar semántica de incertidumbre: UNKNOWN != 0 y cantidades monetarias en Decimal. No asume MOQ=1 ni envío inmediato.
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, List, Dict, Any, Tuple, Sequence
import math

from src.domain.tenant.models import TenantContext
from src.domain.tenant.guard import CrossTenantGuard
from src.domain.security.models import validate_safe_identifier, sanitize_security_data
from src.domain.market_intelligence.models import Confidence
from src.domain.supplier_intelligence.models import (
    Supplier,
    SupplierStatus,
    SupplierReadiness,
    EvidenceProvenanceType,
    RiskLevel,
)
from src.domain.supplier_dashboard.models import (
    SupplierSortField,
    SortOrder,
    SupplierDashboardItem,
    SupplierDashboardSummary,
    SupplierDashboardDetail,
    SupplierDashboardQuery,
    SupplierDashboardPage,
    SupplierComparisonDimension,
    SupplierComparisonView,
)
from src.domain.supplier_dashboard.ports import TenantSupplierRepositoryPort
from src.domain.opportunity_dashboard.ports import TenantOpportunityRepositoryPort
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


class SupplierDashboardAuthenticationError(AdminAuthenticationError):
    """Lanzada cuando la sesión es inválida, expirada o ausente."""
    pass


class SupplierDashboardAuthorizationError(AdminAuthorizationError):
    """Lanzada cuando el usuario o tenant no tiene permisos para ver el dashboard."""
    pass


class SupplierNotFoundError(AdminResourceNotFoundError):
    """Lanzada cuando el proveedor no existe en el tenant especificado."""
    pass


def _mask_email(email: Optional[str]) -> Optional[str]:
    """Enmascara un email de contacto según política N.9."""
    if not email or "@" not in email:
        return None
    user_part, domain = email.split("@", 1)
    if len(user_part) <= 2:
        masked_user = user_part[0] + "*"
    else:
        masked_user = user_part[0] + "*" * (len(user_part) - 2) + user_part[-1]
    return f"{masked_user}@{domain}"


def _mask_phone(phone: Optional[str]) -> Optional[str]:
    """Enmascara un teléfono de contacto según política N.9."""
    if not phone:
        return None
    cleaned = "".join([c for c in phone if c.isdigit() or c == "+"])
    if len(cleaned) <= 4:
        return "***"
    return cleaned[:3] + "*" * (len(cleaned) - 5) + cleaned[-2:]


class SupplierDashboardService:
    """
    Servicio de Aplicación para el Supplier Dashboard (Q.2).
    """

    def __init__(
        self,
        repository: TenantSupplierRepositoryPort,
        authorization_service: Optional[SaaSAuthorizationService] = None,
        session_repository: Optional[SaaSSessionRepositoryPort] = None,
        opportunity_repository: Optional[TenantOpportunityRepositoryPort] = None,
        clock: Optional[ClockPort] = None,
    ):
        self.repository = repository
        self.authorization_service = authorization_service
        self.session_repository = session_repository
        self.opportunity_repository = opportunity_repository
        self.clock = clock or SystemClock()

    def _authorize(
        self,
        tenant_id: str,
        session_id: Optional[str] = None,
        action: str = "SUPPLIER_DASHBOARD_READ",
    ) -> TenantContext:
        """
        Valida la sesión y los permisos de autorización para el tenant dado.
        Retorna el TenantContext validado.
        """
        validate_safe_identifier(tenant_id, field_name="tenant_id")

        if self.authorization_service is not None:
            if not session_id:
                raise SupplierDashboardAuthenticationError("Missing session identifier.")

            req = SaaSAuthorizationRequest(
                action=action,
                tenant_id=tenant_id,
                session_id=session_id,
            )
            decision = self.authorization_service.authorize(req)
            if decision.status != SaaSAuthorizationStatus.ALLOW:
                if decision.reason_code in (
                    SaaSAuthorizationReasonCode.SESSION_EXPIRED,
                    SaaSAuthorizationReasonCode.SESSION_INVALID,
                    SaaSAuthorizationReasonCode.SESSION_NOT_FOUND,
                    SaaSAuthorizationReasonCode.SESSION_REVOKED,
                ):
                    raise SupplierDashboardAuthenticationError("Session expired or invalid.")
                if decision.reason_code in (
                    SaaSAuthorizationReasonCode.SESSION_TENANT_MISMATCH,
                    SaaSAuthorizationReasonCode.RESOURCE_TENANT_MISMATCH,
                ):
                    raise SupplierDashboardAuthorizationError(
                        f"Cross-tenant access forbidden for tenant {tenant_id}."
                    )
                raise SupplierDashboardAuthorizationError(
                    f"Permission denied for action {action}: {decision.status.value}"
                )

        if self.session_repository is not None and session_id:
            session = self.session_repository.get_by_id(session_id=session_id)
            if not session or not session.is_active:
                raise SupplierDashboardAuthenticationError("Invalid or expired session.")
            if session.tenant_id != tenant_id:
                raise SupplierDashboardAuthorizationError(
                    f"Session tenant {session.tenant_id} does not match target tenant {tenant_id}."
                )

        return TenantContext(tenant_id=tenant_id)

    def _project_item(
        self,
        supplier: Supplier,
        opportunity_count: int = 0,
        product_count: int = 0,
    ) -> SupplierDashboardItem:
        """
        Proyecta de forma segura una entidad Supplier a un SupplierDashboardItem.
        Preserva estrictamente UNKNOWN != 0 y no infiere costos/MOQ/plazos.
        """
        meta = supplier.metadata or {}
        unknown_fields: List[str] = []

        # Extraer costo unitario
        raw_unit_cost = meta.get("unit_cost_amount") or meta.get("unit_cost") or meta.get("unit_price") or meta.get("cost_amount")
        unit_cost_amount: Optional[Decimal] = None
        if raw_unit_cost is not None:
            try:
                unit_cost_amount = Decimal(str(raw_unit_cost))
            except Exception:
                unit_cost_amount = None
        if unit_cost_amount is None:
            unknown_fields.append("unit_cost")

        currency = meta.get("currency") or meta.get("cost_currency") or ("CLP" if unit_cost_amount is not None else None)

        # Extraer MOQ
        raw_moq = meta.get("moq") or meta.get("minimum_order_quantity")
        moq: Optional[int] = None
        if raw_moq is not None:
            try:
                moq = int(raw_moq)
            except Exception:
                moq = None
        if moq is None:
            unknown_fields.append("moq")

        # Extraer Lead Time
        raw_lt = meta.get("lead_time_days") or meta.get("lead_time")
        lead_time_days: Optional[int] = None
        if raw_lt is not None:
            try:
                lead_time_days = int(raw_lt)
            except Exception:
                lead_time_days = None
        if lead_time_days is None:
            unknown_fields.append("lead_time_days")

        # Extraer Shipping Cost
        raw_ship = meta.get("shipping_cost_amount") or meta.get("shipping_cost")
        shipping_cost_amount: Optional[Decimal] = None
        if raw_ship is not None:
            try:
                shipping_cost_amount = Decimal(str(raw_ship))
            except Exception:
                shipping_cost_amount = None
        if shipping_cost_amount is None:
            unknown_fields.append("shipping_cost")

        # Extraer Scores
        raw_supp_score = meta.get("supplier_score") or meta.get("score") or meta.get("total_score")
        supplier_score: Optional[Decimal] = None
        if raw_supp_score is not None:
            try:
                supplier_score = Decimal(str(raw_supp_score))
            except Exception:
                supplier_score = None
        if supplier_score is None:
            unknown_fields.append("supplier_score")

        raw_rel_score = meta.get("reliability_score") or meta.get("reliability")
        reliability_score: Optional[Decimal] = None
        if raw_rel_score is not None:
            try:
                reliability_score = Decimal(str(raw_rel_score))
            except Exception:
                reliability_score = None

        raw_qual_score = meta.get("quality_score") or meta.get("quality")
        quality_score: Optional[Decimal] = None
        if raw_qual_score is not None:
            try:
                quality_score = Decimal(str(raw_qual_score))
            except Exception:
                quality_score = None

        # Confianza, Verificación, Riesgo
        confidence_val = meta.get("confidence") or (supplier.source_type.value if hasattr(supplier.source_type, "value") else str(supplier.source_type))
        confidence = str(confidence_val).upper() if confidence_val else "UNKNOWN"

        verification_status_val = meta.get("verification_status") or meta.get("readiness") or (supplier.status.value if hasattr(supplier.status, "value") else str(supplier.status))
        verification_status = str(verification_status_val).upper() if verification_status_val else "UNVERIFIED"

        risk_val = meta.get("risk_level") or meta.get("risk") or "LOW"
        risk_level = str(risk_val).upper() if risk_val else "UNKNOWN"

        # URLs
        marketplace_url = None
        if supplier.product_reference and supplier.product_reference.source_url:
            marketplace_url = supplier.product_reference.source_url
        elif meta.get("marketplace_url"):
            marketplace_url = str(meta["marketplace_url"])

        # Fechas
        raw_last_ver = meta.get("last_verified_at") or meta.get("verified_at")
        last_verified_at: Optional[datetime] = None
        if raw_last_ver:
            try:
                last_verified_at = datetime.fromisoformat(str(raw_last_ver))
            except Exception:
                last_verified_at = None

        updated_at = supplier.observed_at

        country = supplier.location.country if supplier.location else meta.get("country")

        # Conteo de productos asociados del proveedor
        calc_prod_count = product_count if product_count > 0 else (1 if supplier.product_reference and supplier.product_reference.sku else 0)

        return SupplierDashboardItem(
            supplier_id=supplier.supplier_id,
            name=supplier.name,
            source=supplier.source,
            country=country,
            marketplace_url=marketplace_url,
            product_count=calc_prod_count,
            opportunity_count=opportunity_count,
            unit_cost_amount=unit_cost_amount,
            currency=currency,
            moq=moq,
            lead_time_days=lead_time_days,
            shipping_cost_amount=shipping_cost_amount,
            reliability_score=reliability_score,
            quality_score=quality_score,
            supplier_score=supplier_score,
            confidence=confidence,
            verification_status=verification_status,
            risk_level=risk_level,
            last_verified_at=last_verified_at,
            updated_at=updated_at,
            unknown_fields=tuple(sorted(unknown_fields)),
        )

    def _get_tenant_opp_associations(self, context: TenantContext) -> Tuple[Dict[str, int], Dict[str, List[Dict[str, Any]]]]:
        """Calcula el conteo y resumen de oportunidades asociadas por supplier_id para el tenant."""
        counts: Dict[str, int] = {}
        opp_map: Dict[str, List[Dict[str, Any]]] = {}
        if self.opportunity_repository is None:
            return counts, opp_map

        try:
            opps = self.opportunity_repository.list_all(context)
            for opp in opps:
                meta = opp.metadata or {}
                supp_id = meta.get("supplier_id") or meta.get("primary_supplier_id")
                if supp_id:
                    counts[supp_id] = counts.get(supp_id, 0) + 1
                    if supp_id not in opp_map:
                        opp_map[supp_id] = []
                    opp_map[supp_id].append({
                        "opportunity_id": opp.opportunity_id,
                        "title": opp.title,
                        "marketplace": opp.marketplace.value if hasattr(opp.marketplace, "value") else str(opp.marketplace),
                        "status": opp.status.value if hasattr(opp.status, "value") else str(opp.status),
                    })
        except Exception:
            pass
        return counts, opp_map

    def list_suppliers(
        self,
        tenant_id: str,
        query: Optional[SupplierDashboardQuery] = None,
        session_id: Optional[str] = None,
    ) -> SupplierDashboardPage:
        """
        Lista proveedores filtrados, ordenados y paginados con aislamiento estricto por tenant.
        """
        context = self._authorize(tenant_id, session_id=session_id)
        q = query or SupplierDashboardQuery()

        all_suppliers = self.repository.list_all(context)
        opp_counts, _ = self.get_associations(context)

        items: List[SupplierDashboardItem] = []
        for s in all_suppliers:
            item = self._project_item(
                s,
                opportunity_count=opp_counts.get(s.supplier_id, 0),
            )
            if self._matches_filter(item, s, q):
                items.append(item)

        sorted_items = self._sort_items(items, q.sort_by, q.sort_order)

        total_count = len(sorted_items)
        total_pages = max(1, math.ceil(total_count / q.page_size)) if total_count > 0 else 1
        page = min(max(1, q.page), total_pages)

        start_idx = (page - 1) * q.page_size
        end_idx = start_idx + q.page_size
        page_items = sorted_items[start_idx:end_idx]

        return SupplierDashboardPage(
            items=tuple(page_items),
            total_count=total_count,
            page=page,
            page_size=q.page_size,
            total_pages=total_pages,
            has_next=page < total_pages,
            has_previous=page > 1,
        )

    def get_associations(self, context: TenantContext) -> Tuple[Dict[str, int], Dict[str, List[Dict[str, Any]]]]:
        return self._get_tenant_opp_associations(context)

    def _matches_filter(
        self,
        item: SupplierDashboardItem,
        supplier: Supplier,
        q: SupplierDashboardQuery,
    ) -> bool:
        """Aplica predicados de filtrado sobre el item y entidad."""
        if q.source and q.source.strip():
            if q.source.lower() not in item.source.lower():
                return False

        if q.country and q.country.strip():
            if not item.country or q.country.lower() != item.country.lower():
                return False

        if q.verification_status and q.verification_status.strip():
            if not item.verification_status or q.verification_status.lower() != item.verification_status.lower():
                return False

        if q.risk_level and q.risk_level.strip():
            if not item.risk_level or q.risk_level.lower() != item.risk_level.lower():
                return False

        if q.min_score is not None:
            if item.supplier_score is None or item.supplier_score < q.min_score:
                return False

        if q.max_score is not None:
            if item.supplier_score is None or item.supplier_score > q.max_score:
                return False

        if q.max_unit_cost is not None:
            if item.unit_cost_amount is None or item.unit_cost_amount > q.max_unit_cost:
                return False

        if q.max_moq is not None:
            if item.moq is None or item.moq > q.max_moq:
                return False

        if q.max_lead_time is not None:
            if item.lead_time_days is None or item.lead_time_days > q.max_lead_time:
                return False

        if q.min_confidence and q.min_confidence.strip():
            if not item.confidence or q.min_confidence.lower() not in item.confidence.lower():
                return False

        if q.product_sku and q.product_sku.strip():
            ref_sku = supplier.product_reference.sku if supplier.product_reference else None
            meta_sku = supplier.metadata.get("sku")
            if (not ref_sku or q.product_sku.lower() not in ref_sku.lower()) and (not meta_sku or q.product_sku.lower() not in str(meta_sku).lower()):
                return False

        if q.search_text and q.search_text.strip():
            text = q.search_text.lower()
            name_match = text in item.name.lower()
            source_match = text in item.source.lower()
            id_match = text in item.supplier_id.lower()
            country_match = item.country is not None and text in item.country.lower()
            if not (name_match or source_match or id_match or country_match):
                return False

        return True

    def _sort_items(
        self,
        items: List[SupplierDashboardItem],
        sort_by: SupplierSortField,
        sort_order: SortOrder,
    ) -> List[SupplierDashboardItem]:
        """
        Ordenación determinista con regla de desempate por supplier_id.
        UNKNOWN (None) siempre se coloca al final independientemente del orden.
        """
        reverse = sort_order == SortOrder.DESC

        def get_sort_key(item: SupplierDashboardItem):
            val: Any = None
            if sort_by == SupplierSortField.SUPPLIER_SCORE:
                val = item.supplier_score
            elif sort_by == SupplierSortField.UNIT_COST:
                val = item.unit_cost_amount
            elif sort_by == SupplierSortField.LEAD_TIME:
                val = item.lead_time_days
            elif sort_by == SupplierSortField.RELIABILITY:
                val = item.reliability_score
            elif sort_by == SupplierSortField.LAST_VERIFIED:
                val = item.last_verified_at.timestamp() if item.last_verified_at else None
            elif sort_by == SupplierSortField.UPDATED_AT:
                val = item.updated_at.timestamp() if item.updated_at else None
            elif sort_by == SupplierSortField.NAME:
                val = item.name.lower()

            is_none = 1 if val is None else 0
            # Si is_none == 1, debe quedar al final
            return (is_none, val if val is not None else 0, item.supplier_id)

        # Para ordenar con Nones al final y desempate por supplier_id ASC siempre:
        non_nulls = [i for i in items if get_sort_key(i)[0] == 0]
        nulls = [i for i in items if get_sort_key(i)[0] == 1]

        # En DESC, invertimos el valor primario pero preservamos el supplier_id en ASC
        if reverse:
            # Primero ordenamos por supplier_id ascendente
            non_nulls.sort(key=lambda x: x.supplier_id)
            # Luego ordenamiento estable por el valor primario descendente
            non_nulls.sort(key=lambda x: get_sort_key(x)[1], reverse=True)
        else:
            non_nulls.sort(key=lambda x: (get_sort_key(x)[1], x.supplier_id))

        nulls_sorted = sorted(nulls, key=lambda x: x.supplier_id)

        return non_nulls + nulls_sorted

    def get_summary(
        self,
        tenant_id: str,
        session_id: Optional[str] = None,
    ) -> SupplierDashboardSummary:
        """
        Calcula de forma determinista el resumen del catálogo de proveedores del tenant.
        """
        context = self._authorize(tenant_id, session_id=session_id)
        all_suppliers = self.repository.list_all(context)

        total_suppliers = len(all_suppliers)
        if total_suppliers == 0:
            return SupplierDashboardSummary(
                tenant_id=tenant_id,
                total_suppliers=0,
                verified_suppliers=0,
                high_rated_suppliers=0,
                suppliers_by_country={},
                suppliers_by_source={},
                suppliers_by_verification={},
                suppliers_by_risk={},
                average_supplier_score=None,
                suppliers_with_unknown_critical_fields=0,
                newest_updated_at=None,
                oldest_updated_at=None,
            )

        verified_count = 0
        high_rated_count = 0
        by_country: Dict[str, int] = {}
        by_source: Dict[str, int] = {}
        by_verification: Dict[str, int] = {}
        by_risk: Dict[str, int] = {}
        scores: List[Decimal] = []
        unknown_critical_count = 0
        newest_dt: Optional[datetime] = None
        oldest_dt: Optional[datetime] = None

        for s in all_suppliers:
            item = self._project_item(s)

            # País
            country_key = item.country or "UNKNOWN"
            by_country[country_key] = by_country.get(country_key, 0) + 1

            # Fuente
            by_source[item.source] = by_source.get(item.source, 0) + 1

            # Verificación
            ver_key = item.verification_status or "UNVERIFIED"
            by_verification[ver_key] = by_verification.get(ver_key, 0) + 1
            if ver_key in ("VERIFIED", "READY_FOR_ECONOMICS"):
                verified_count += 1

            # Riesgo
            risk_key = item.risk_level or "UNKNOWN"
            by_risk[risk_key] = by_risk.get(risk_key, 0) + 1

            # Scores
            if item.supplier_score is not None:
                scores.append(item.supplier_score)
                if item.supplier_score >= Decimal("80.0"):
                    high_rated_count += 1

            # Unknowns críticos (costo, MOQ o lead time ausentes)
            if item.unit_cost_amount is None or item.moq is None or item.lead_time_days is None:
                unknown_critical_count += 1

            # Fechas
            dt = item.updated_at
            if dt:
                if newest_dt is None or dt > newest_dt:
                    newest_dt = dt
                if oldest_dt is None or dt < oldest_dt:
                    oldest_dt = dt

        avg_score = (sum(scores) / Decimal(len(scores))).quantize(Decimal("0.01")) if scores else None

        return SupplierDashboardSummary(
            tenant_id=tenant_id,
            total_suppliers=total_suppliers,
            verified_suppliers=verified_count,
            high_rated_suppliers=high_rated_count,
            suppliers_by_country=by_country,
            suppliers_by_source=by_source,
            suppliers_by_verification=by_verification,
            suppliers_by_risk=by_risk,
            average_supplier_score=avg_score,
            suppliers_with_unknown_critical_fields=unknown_critical_count,
            newest_updated_at=newest_dt,
            oldest_updated_at=oldest_dt,
        )

    def get_supplier_detail(
        self,
        tenant_id: str,
        supplier_id: str,
        session_id: Optional[str] = None,
    ) -> SupplierDashboardDetail:
        """
        Obtiene el detalle seguro y explicable de un proveedor garantizando aislamiento por tenant.
        """
        context = self._authorize(tenant_id, session_id=session_id)
        validate_safe_identifier(supplier_id, field_name="supplier_id")

        supplier = self.repository.get_by_id(context, supplier_id)
        if not supplier:
            raise SupplierNotFoundError(f"Supplier {supplier_id} not found in tenant {tenant_id}.")

        opp_counts, opp_map = self.get_associations(context)
        associated_opps = opp_map.get(supplier_id, [])

        associated_prods: List[Dict[str, Any]] = []
        if supplier.product_reference:
            associated_prods.append({
                "sku": supplier.product_reference.sku,
                "title": supplier.product_reference.title,
                "brand": supplier.product_reference.brand,
                "category": supplier.product_reference.category,
                "source_product_id": supplier.product_reference.source_product_id,
            })

        item = self._project_item(
            supplier,
            opportunity_count=len(associated_opps),
            product_count=len(associated_prods),
        )

        # Contacto seguro y enmascarado
        contact_name = supplier.contact.name if supplier.contact else None
        raw_email = supplier.contact.email if supplier.contact else None
        raw_phone = supplier.contact.phone if supplier.contact else None
        website = supplier.contact.website if supplier.contact else None

        contact_email_masked = _mask_email(raw_email)
        contact_phone_masked = _mask_phone(raw_phone)

        city = supplier.location.city if supplier.location else None
        region = supplier.location.region if supplier.location else None

        # Explicabilidad de hechos
        scoring_facts: List[str] = []
        if item.supplier_score is not None:
            scoring_facts.append(f"Supplier scored {item.supplier_score}/100 based on verified performance and catalog accuracy.")
        if item.reliability_score is not None:
            scoring_facts.append(f"Historical reliability rated at {item.reliability_score}/100.")
        if item.quality_score is not None:
            scoring_facts.append(f"Product quality rating: {item.quality_score}/100.")

        verification_facts: List[str] = [
            f"Current status: {item.verification_status}",
            f"Provenance: {supplier.source_type.value if hasattr(supplier.source_type, 'value') else str(supplier.source_type)}",
        ]
        if item.last_verified_at:
            verification_facts.append(f"Last verified on {item.last_verified_at.strftime('%Y-%m-%d')}")

        reliability_facts: List[str] = []
        if item.lead_time_days is not None:
            reliability_facts.append(f"Estimated fulfillment lead time: {item.lead_time_days} days.")
        else:
            reliability_facts.append("Fulfillment lead time is UNKNOWN.")

        risk_facts: List[str] = [
            f"Assessed risk level: {item.risk_level}",
        ]
        if "unit_cost" in item.unknown_fields:
            risk_facts.append("Unit cost has not been formally observed.")
        if "moq" in item.unknown_fields:
            risk_facts.append("Minimum Order Quantity (MOQ) is uncertain.")

        return SupplierDashboardDetail(
            item=item,
            contact_name=contact_name,
            contact_email_masked=contact_email_masked,
            contact_phone_masked=contact_phone_masked,
            contact_website=website,
            city=city,
            region=region,
            associated_opportunities=tuple(associated_opps),
            associated_products=tuple(associated_prods),
            scoring_facts=tuple(scoring_facts),
            verification_facts=tuple(verification_facts),
            reliability_facts=tuple(reliability_facts),
            risk_facts=tuple(risk_facts),
            unknowns=item.unknown_fields,
            metadata=dict(supplier.metadata),
        )

    def compare_suppliers(
        self,
        tenant_id: str,
        supplier_ids: Sequence[str],
        session_id: Optional[str] = None,
    ) -> SupplierComparisonView:
        """
        Compara 2 o más proveedores en dimensiones clave respetando UNKNOWN semantics.
        No inventa un ganador si no hay política explícita.
        """
        context = self._authorize(tenant_id, session_id=session_id)
        if not supplier_ids or len(supplier_ids) < 2:
            raise AdminInvalidRequestError("At least 2 supplier_ids are required for comparison.")

        unique_ids = list(dict.fromkeys(supplier_ids))
        items: List[SupplierDashboardItem] = []
        opp_counts, _ = self.get_associations(context)

        for sid in unique_ids:
            validate_safe_identifier(sid, field_name="supplier_id")
            s = self.repository.get_by_id(context, sid)
            if not s:
                raise SupplierNotFoundError(f"Supplier {sid} not found in tenant {tenant_id}.")
            items.append(self._project_item(s, opportunity_count=opp_counts.get(sid, 0)))

        # Construir dimensiones de comparación
        dim_score = SupplierComparisonDimension(
            dimension_name="supplier_score",
            dimension_label="Supplier Score",
            values={it.supplier_id: it.supplier_score if it.supplier_score is not None else None for it in items},
            notes="Higher score indicates stronger performance.",
        )
        dim_cost = SupplierComparisonDimension(
            dimension_name="unit_cost",
            dimension_label="Unit Cost",
            values={it.supplier_id: it.unit_cost_amount if it.unit_cost_amount is not None else None for it in items},
            notes="Direct wholesale cost per unit in original currency.",
        )
        dim_moq = SupplierComparisonDimension(
            dimension_name="moq",
            dimension_label="MOQ (Units)",
            values={it.supplier_id: it.moq if it.moq is not None else None for it in items},
            notes="Minimum Order Quantity required.",
        )
        dim_lead_time = SupplierComparisonDimension(
            dimension_name="lead_time_days",
            dimension_label="Lead Time (Days)",
            values={it.supplier_id: it.lead_time_days if it.lead_time_days is not None else None for it in items},
            notes="Estimated fulfillment days from order to warehouse.",
        )
        dim_shipping = SupplierComparisonDimension(
            dimension_name="shipping_cost",
            dimension_label="Shipping Cost",
            values={it.supplier_id: it.shipping_cost_amount if it.shipping_cost_amount is not None else None for it in items},
            notes="Estimated shipping cost.",
        )
        dim_verification = SupplierComparisonDimension(
            dimension_name="verification_status",
            dimension_label="Verification Status",
            values={it.supplier_id: it.verification_status or "UNVERIFIED" for it in items},
            notes="Current verification and readiness state.",
        )
        dim_risk = SupplierComparisonDimension(
            dimension_name="risk_level",
            dimension_label="Risk Level",
            values={it.supplier_id: it.risk_level or "UNKNOWN" for it in items},
            notes="Operational and supply chain risk classification.",
        )
        dim_confidence = SupplierComparisonDimension(
            dimension_name="confidence",
            dimension_label="Confidence",
            values={it.supplier_id: it.confidence or "UNKNOWN" for it in items},
            notes="Reliability and quality of intelligence sources.",
        )

        dimensions = (
            dim_score,
            dim_cost,
            dim_moq,
            dim_lead_time,
            dim_shipping,
            dim_verification,
            dim_risk,
            dim_confidence,
        )

        summary = f"Comparison of {len(items)} suppliers across {len(dimensions)} dimensions."

        return SupplierComparisonView(
            supplier_ids=tuple(unique_ids),
            dimensions=dimensions,
            comparison_summary=summary,
            items=tuple(items),
        )
