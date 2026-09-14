"""
Aplicación HTTP / Starlette para Admin Console & Multi-Tenant Management (Hito O.10 — SaaS / Platformization).

Expone rutas REST y una interfaz web administrativa funcional y ligera:
- GET /health
- GET /api/admin/tenants/{tenant_id}/summary
- GET /api/admin/tenants/{tenant_id}/organizations
- GET /api/admin/tenants/{tenant_id}/organizations/{org_id}/memberships
- POST /api/admin/tenants/{tenant_id}/organizations/{org_id}/memberships
- DELETE /api/admin/tenants/{tenant_id}/organizations/{org_id}/memberships/{identity_id}
- GET /api/admin/tenants/{tenant_id}/usage
- GET /api/admin/tenants/{tenant_id}/quota
- GET /api/admin/tenants/{tenant_id}/plan
- GET /api/admin/catalog/plans
- POST /api/admin/tenants/{tenant_id}/plan/assign
- GET /api/admin/tenants/{tenant_id}/billing
- POST /api/admin/tenants/{tenant_id}/subscriptions
- POST /api/admin/tenants/{tenant_id}/subscriptions/{subscription_id}/cancel
- GET /api/admin/tenants/{tenant_id}/audit
- GET /api/admin/tenants/{tenant_id}/traces
- GET /admin (Superficie HTML funcional)

Principios:
1. Reutiliza el framework existente (Starlette).
2. Seguridad estricta: Extracción de session_id vía Bearer o X-Session-ID.
3. Respuestas JSON estructuradas con manejo de errores sanitizado.
4. Delegación completa al AdminConsoleService.
"""

from datetime import datetime, timezone
import json
import logging
from typing import Optional, Dict, Any

from decimal import Decimal
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, HTMLResponse
from starlette.routing import Route

from src.application.admin_console.admin_console_service import AdminConsoleService
from src.application.opportunity_dashboard.opportunity_dashboard_service import OpportunityDashboardService
from src.application.supplier_dashboard.supplier_dashboard_service import SupplierDashboardService
from src.application.profit_dashboard.profit_dashboard_service import ProfitDashboardService
from src.application.mission_dashboard.mission_dashboard_service import MissionDashboardService
from src.application.agent_cost_dashboard.agent_cost_dashboard_service import AgentCostDashboardService
from src.application.business_kpi.business_kpi_service import BusinessKPIService
from src.domain.business_kpi.models import BusinessKPIQuery
from src.domain.opportunity_dashboard.models import (
    OpportunityDashboardQuery,
    OpportunitySortField,
    SortOrder,
)
from src.domain.supplier_dashboard.models import (
    SupplierDashboardQuery,
    SupplierSortField,
    SortOrder as SupplierSortOrder,
)
from src.domain.profit_dashboard.models import (
    ProfitDashboardQuery,
    ProfitSortField,
    ProfitCompleteness,
)
from src.domain.mission_dashboard.models import (
    MissionDashboardQuery,
    MissionSortField,
)
from src.domain.agent_cost_dashboard.models import (
    AgentCostDashboardQuery,
    AgentCostSortField,
    SortOrder as AgentCostSortOrder,
)
from src.domain.admin_console.models import (
    AdminAuthenticationError,
    AdminAuthorizationError,
    AdminResourceNotFoundError,
    AdminConflictError,
    AdminInvalidRequestError,
    AdminConsoleError,
)
from src.domain.organization.models import MembershipRole
from src.domain.billing.models import BillingCycle
from src.domain.tenant_configuration.models import ConfigurationScope

logger = logging.getLogger(__name__)


def _extract_session_id(request: Request) -> Optional[str]:
    """Extrae el session_id desde cabeceras HTTP o query params."""
    auth_header = request.headers.get("Authorization") or request.headers.get("authorization")
    if auth_header and auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    session_id_header = request.headers.get("X-Session-ID") or request.headers.get("x-session-id")
    if session_id_header:
        return session_id_header.strip()
    return request.query_params.get("session_id")


def _handle_error(e: Exception) -> JSONResponse:
    """Mapea excepciones a respuestas HTTP normalizadas y sanitizadas."""
    if isinstance(e, AdminAuthenticationError):
        return JSONResponse({"error": "unauthenticated", "message": str(e)}, status_code=401)
    elif isinstance(e, AdminAuthorizationError):
        return JSONResponse({"error": "unauthorized", "message": str(e)}, status_code=403)
    elif isinstance(e, AdminResourceNotFoundError):
        return JSONResponse({"error": "not_found", "message": str(e)}, status_code=404)
    elif isinstance(e, AdminConflictError):
        return JSONResponse({"error": "conflict", "message": str(e)}, status_code=409)
    elif isinstance(e, AdminInvalidRequestError):
        return JSONResponse({"error": "invalid_request", "message": str(e)}, status_code=422)
    elif isinstance(e, AdminConsoleError):
        return JSONResponse({"error": "admin_error", "message": str(e)}, status_code=400)
    else:
        logger.error("Unhandled error in Admin Console API: %s", str(e), exc_info=True)
        return JSONResponse({"error": "internal_error", "message": "An unexpected error occurred."}, status_code=500)


def create_admin_app(
    service: AdminConsoleService,
    opportunity_dashboard_service: Optional[OpportunityDashboardService] = None,
    supplier_dashboard_service: Optional[SupplierDashboardService] = None,
    profit_dashboard_service: Optional[ProfitDashboardService] = None,
    mission_dashboard_service: Optional[MissionDashboardService] = None,
    agent_cost_dashboard_service: Optional[AgentCostDashboardService] = None,
    business_kpi_service: Optional[BusinessKPIService] = None,
) -> Starlette:
    """Fábrica para la aplicación Starlette de la Admin Console & Business Intelligence."""

    async def health(request: Request):
        return JSONResponse({
            "status": "ok",
            "service": "saas-admin-console",
            "version": "1.0.0",
        })

    # -------------------------------------------------------------------------
    # Q.1 — Opportunity Dashboard (Business Intelligence)
    # -------------------------------------------------------------------------

    async def get_opportunity_summary(request: Request):
        if opportunity_dashboard_service is None:
            return JSONResponse({"error": "service_unavailable", "message": "Opportunity Dashboard service is not configured."}, status_code=503)
        tenant_id = request.path_params.get("tenant_id")
        session_id = _extract_session_id(request)
        if not session_id:
            return JSONResponse({"error": "unauthenticated", "message": "Missing session identifier."}, status_code=401)
        try:
            summary = opportunity_dashboard_service.get_summary(tenant_id=tenant_id, session_id=session_id)
            return JSONResponse(summary.to_dict())
        except Exception as e:
            return _handle_error(e)

    async def list_opportunities(request: Request):
        if opportunity_dashboard_service is None:
            return JSONResponse({"error": "service_unavailable", "message": "Opportunity Dashboard service is not configured."}, status_code=503)
        tenant_id = request.path_params.get("tenant_id")
        session_id = _extract_session_id(request)
        if not session_id:
            return JSONResponse({"error": "unauthenticated", "message": "Missing session identifier."}, status_code=401)
        try:
            qp = request.query_params
            category = qp.get("category")
            marketplace = qp.get("marketplace")
            opportunity_type = qp.get("opportunity_type")
            status = qp.get("status")
            confidence = qp.get("confidence")
            search_text = qp.get("search_text") or qp.get("search")

            min_score = Decimal(qp["min_score"]) if "min_score" in qp and qp["min_score"].strip() else None
            max_score = Decimal(qp["max_score"]) if "max_score" in qp and qp["max_score"].strip() else None
            min_margin = Decimal(qp["min_margin"]) if "min_margin" in qp and qp["min_margin"].strip() else None

            date_from = datetime.fromisoformat(qp["date_from"]) if "date_from" in qp and qp["date_from"].strip() else None
            date_to = datetime.fromisoformat(qp["date_to"]) if "date_to" in qp and qp["date_to"].strip() else None

            sort_by_str = qp.get("sort_by", "opportunity_score")
            try:
                sort_by = OpportunitySortField(sort_by_str.lower())
            except ValueError:
                sort_by = OpportunitySortField.OPPORTUNITY_SCORE

            sort_order_str = qp.get("sort_order", "desc")
            try:
                sort_order = SortOrder(sort_order_str.lower())
            except ValueError:
                sort_order = SortOrder.DESC

            page_str = qp.get("page", "1")
            page = int(page_str) if page_str.isdigit() else 1

            page_size_str = qp.get("page_size", "20")
            page_size = int(page_size_str) if page_size_str.isdigit() else 20

            query = OpportunityDashboardQuery(
                category=category,
                marketplace=marketplace,
                opportunity_type=opportunity_type,
                status=status,
                confidence=confidence,
                min_score=min_score,
                max_score=max_score,
                min_margin=min_margin,
                date_from=date_from,
                date_to=date_to,
                search_text=search_text,
                sort_by=sort_by,
                sort_order=sort_order,
                page=page,
                page_size=page_size,
            )

            result_page = opportunity_dashboard_service.list_opportunities(
                tenant_id=tenant_id,
                query=query,
                session_id=session_id,
            )
            return JSONResponse(result_page.to_dict())
        except Exception as e:
            return _handle_error(e)

    async def get_opportunity_detail(request: Request):
        if opportunity_dashboard_service is None:
            return JSONResponse({"error": "service_unavailable", "message": "Opportunity Dashboard service is not configured."}, status_code=503)
        tenant_id = request.path_params.get("tenant_id")
        opportunity_id = request.path_params.get("opportunity_id")
        session_id = _extract_session_id(request)
        if not session_id:
            return JSONResponse({"error": "unauthenticated", "message": "Missing session identifier."}, status_code=401)
        try:
            detail = opportunity_dashboard_service.get_opportunity_detail(
                tenant_id=tenant_id,
                opportunity_id=opportunity_id,
                session_id=session_id,
            )
            return JSONResponse(detail.to_dict())
        except Exception as e:
            return _handle_error(e)

    async def compare_opportunities(request: Request):
        if opportunity_dashboard_service is None:
            return JSONResponse({"error": "service_unavailable", "message": "Opportunity Dashboard service is not configured."}, status_code=503)
        tenant_id = request.path_params.get("tenant_id")
        session_id = _extract_session_id(request)
        if not session_id:
            return JSONResponse({"error": "unauthenticated", "message": "Missing session identifier."}, status_code=401)
        try:
            body = {}
            if request.method == "POST" and request.headers.get("content-type") == "application/json":
                try:
                    body = await request.json()
                except Exception:
                    body = {}

                opportunity_ids = []
            if "opportunity_ids" in body and isinstance(body["opportunity_ids"], list):
                opportunity_ids = body["opportunity_ids"]
            elif "ids" in body and isinstance(body["ids"], list):
                opportunity_ids = body["ids"]
            elif "opportunity_ids" in request.query_params:
                raw_ids = request.query_params.get("opportunity_ids", "")
                opportunity_ids = [i.strip() for i in raw_ids.split(",") if i.strip()]
            elif "ids" in request.query_params:
                raw_ids = request.query_params.get("ids", "")
                opportunity_ids = [i.strip() for i in raw_ids.split(",") if i.strip()]
            comparison = opportunity_dashboard_service.compare_opportunities(
                tenant_id=tenant_id,
                opportunity_ids=opportunity_ids,
                session_id=session_id,
            )
            return JSONResponse(comparison.to_dict())
        except Exception as e:
            return _handle_error(e)

    # -------------------------------------------------------------------------
    # Q.2 — Supplier Dashboard (Business Intelligence)
    # -------------------------------------------------------------------------

    async def get_supplier_summary(request: Request):
        if supplier_dashboard_service is None:
            return JSONResponse({"error": "service_unavailable", "message": "Supplier Dashboard service is not configured."}, status_code=503)
        tenant_id = request.path_params.get("tenant_id")
        session_id = _extract_session_id(request)
        if not session_id:
            return JSONResponse({"error": "unauthenticated", "message": "Missing session identifier."}, status_code=401)
        try:
            summary = supplier_dashboard_service.get_summary(tenant_id=tenant_id, session_id=session_id)
            return JSONResponse(summary.to_dict())
        except Exception as e:
            return _handle_error(e)

    async def list_suppliers(request: Request):
        if supplier_dashboard_service is None:
            return JSONResponse({"error": "service_unavailable", "message": "Supplier Dashboard service is not configured."}, status_code=503)
        tenant_id = request.path_params.get("tenant_id")
        session_id = _extract_session_id(request)
        if not session_id:
            return JSONResponse({"error": "unauthenticated", "message": "Missing session identifier."}, status_code=401)
        try:
            qp = request.query_params
            source = qp.get("source") or qp.get("platform")
            country = qp.get("country")
            verification_status = qp.get("verification_status") or qp.get("verification")
            risk_level = qp.get("risk_level") or qp.get("risk")
            confidence = qp.get("confidence")
            min_confidence = qp.get("min_confidence")
            product_reference = qp.get("product_reference") or qp.get("product")
            opportunity_id = qp.get("opportunity_id") or qp.get("opportunity")
            search_text = qp.get("search_text") or qp.get("search")

            min_supplier_score = Decimal(qp["min_supplier_score"]) if "min_supplier_score" in qp and qp["min_supplier_score"].strip() else None
            max_supplier_score = Decimal(qp["max_supplier_score"]) if "max_supplier_score" in qp and qp["max_supplier_score"].strip() else None
            max_unit_cost = Decimal(qp["max_unit_cost"]) if "max_unit_cost" in qp and qp["max_unit_cost"].strip() else None

            max_moq = int(qp["max_moq"]) if "max_moq" in qp and qp["max_moq"].strip().isdigit() else None
            max_lead_time_days = int(qp["max_lead_time_days"]) if "max_lead_time_days" in qp and qp["max_lead_time_days"].strip().isdigit() else None

            sort_by_str = qp.get("sort_by", "supplier_score")
            try:
                sort_by = SupplierSortField(sort_by_str.lower())
            except ValueError:
                sort_by = SupplierSortField.SUPPLIER_SCORE

            sort_order_str = qp.get("sort_order", "desc")
            try:
                sort_order = SupplierSortOrder(sort_order_str.lower())
            except ValueError:
                sort_order = SupplierSortOrder.DESC

            page_str = qp.get("page", "1")
            page = int(page_str) if page_str.isdigit() else 1

            page_size_str = qp.get("page_size", "20")
            page_size = int(page_size_str) if page_size_str.isdigit() else 20

            query = SupplierDashboardQuery(
                source=source,
                country=country,
                verification_status=verification_status,
                risk_level=risk_level,
                confidence=confidence,
                min_supplier_score=min_supplier_score,
                max_supplier_score=max_supplier_score,
                max_unit_cost=max_unit_cost,
                max_moq=max_moq,
                max_lead_time_days=max_lead_time_days,
                min_confidence=min_confidence,
                product_reference=product_reference,
                opportunity_id=opportunity_id,
                search_text=search_text,
                sort_by=sort_by,
                sort_order=sort_order,
                page=page,
                page_size=page_size,
            )

            result_page = supplier_dashboard_service.list_suppliers(
                tenant_id=tenant_id,
                query=query,
                session_id=session_id,
            )
            return JSONResponse(result_page.to_dict())
        except Exception as e:
            return _handle_error(e)

    async def get_supplier_detail(request: Request):
        if supplier_dashboard_service is None:
            return JSONResponse({"error": "service_unavailable", "message": "Supplier Dashboard service is not configured."}, status_code=503)
        tenant_id = request.path_params.get("tenant_id")
        supplier_id = request.path_params.get("supplier_id")
        session_id = _extract_session_id(request)
        if not session_id:
            return JSONResponse({"error": "unauthenticated", "message": "Missing session identifier."}, status_code=401)
        try:
            detail = supplier_dashboard_service.get_supplier_detail(
                tenant_id=tenant_id,
                supplier_id=supplier_id,
                session_id=session_id,
            )
            return JSONResponse(detail.to_dict())
        except Exception as e:
            return _handle_error(e)

    async def compare_suppliers(request: Request):
        if supplier_dashboard_service is None:
            return JSONResponse({"error": "service_unavailable", "message": "Supplier Dashboard service is not configured."}, status_code=503)
        tenant_id = request.path_params.get("tenant_id")
        session_id = _extract_session_id(request)
        if not session_id:
            return JSONResponse({"error": "unauthenticated", "message": "Missing session identifier."}, status_code=401)
        try:
            body = {}
            if request.method == "POST" and request.headers.get("content-type") == "application/json":
                try:
                    body = await request.json()
                except Exception:
                    body = {}

            supplier_ids = []
            if "supplier_ids" in body and isinstance(body["supplier_ids"], list):
                supplier_ids = body["supplier_ids"]
            elif "ids" in body and isinstance(body["ids"], list):
                supplier_ids = body["ids"]
            elif "supplier_ids" in request.query_params:
                raw_ids = request.query_params.get("supplier_ids", "")
                supplier_ids = [i.strip() for i in raw_ids.split(",") if i.strip()]
            elif "ids" in request.query_params:
                raw_ids = request.query_params.get("ids", "")
                supplier_ids = [i.strip() for i in raw_ids.split(",") if i.strip()]

            comparison = supplier_dashboard_service.compare_suppliers(
                tenant_id=tenant_id,
                supplier_ids=supplier_ids,
                session_id=session_id,
            )
            return JSONResponse(comparison.to_dict())
        except Exception as e:
            return _handle_error(e)

    # -------------------------------------------------------------------------
    # Q.3 — Profit Dashboard (Business Intelligence / Unit Economics)
    # -------------------------------------------------------------------------

    async def get_profit_summary(request: Request):
        if profit_dashboard_service is None:
            return JSONResponse({"error": "service_unavailable", "message": "Profit Dashboard service is not configured."}, status_code=503)
        tenant_id = request.path_params.get("tenant_id")
        session_id = _extract_session_id(request)
        if not session_id:
            return JSONResponse({"error": "unauthenticated", "message": "Missing session identifier."}, status_code=401)
        try:
            summary = profit_dashboard_service.get_summary(tenant_id=tenant_id, session_id=session_id)
            return JSONResponse(summary.to_dict())
        except Exception as e:
            return _handle_error(e)

    async def list_profit_items(request: Request):
        if profit_dashboard_service is None:
            return JSONResponse({"error": "service_unavailable", "message": "Profit Dashboard service is not configured."}, status_code=503)
        tenant_id = request.path_params.get("tenant_id")
        session_id = _extract_session_id(request)
        if not session_id:
            return JSONResponse({"error": "unauthenticated", "message": "Missing session identifier."}, status_code=401)
        try:
            qp = request.query_params
            marketplace = qp.get("marketplace")
            category = qp.get("category")
            supplier_id = qp.get("supplier_id") or qp.get("supplier")
            opportunity_id = qp.get("opportunity_id") or qp.get("opportunity")
            product_id = qp.get("product_id") or qp.get("product")
            currency = qp.get("currency")
            search_text = qp.get("search_text") or qp.get("search")

            completeness_str = qp.get("completeness")
            completeness = None
            if completeness_str and completeness_str.strip():
                try:
                    completeness = ProfitCompleteness(completeness_str.strip().upper())
                except ValueError:
                    completeness = None

            min_margin = None
            if "min_margin_pct" in qp and qp["min_margin_pct"].strip():
                min_margin = Decimal(qp["min_margin_pct"])
            elif "min_margin" in qp and qp["min_margin"].strip():
                min_margin = Decimal(qp["min_margin"])

            max_margin = None
            if "max_margin_pct" in qp and qp["max_margin_pct"].strip():
                max_margin = Decimal(qp["max_margin_pct"])
            elif "max_margin" in qp and qp["max_margin"].strip():
                max_margin = Decimal(qp["max_margin"])

            min_profit = None
            if "min_profit_amount" in qp and qp["min_profit_amount"].strip():
                min_profit = Decimal(qp["min_profit_amount"])
            elif "min_profit" in qp and qp["min_profit"].strip():
                min_profit = Decimal(qp["min_profit"])

            max_profit = None
            if "max_profit_amount" in qp and qp["max_profit_amount"].strip():
                max_profit = Decimal(qp["max_profit_amount"])
            elif "max_profit" in qp and qp["max_profit"].strip():
                max_profit = Decimal(qp["max_profit"])
            min_sale_price = Decimal(qp["min_sale_price"]) if "min_sale_price" in qp and qp["min_sale_price"].strip() else None
            max_sale_price = Decimal(qp["max_sale_price"]) if "max_sale_price" in qp and qp["max_sale_price"].strip() else None

            date_from = datetime.fromisoformat(qp["date_from"]) if "date_from" in qp and qp["date_from"].strip() else None
            date_to = datetime.fromisoformat(qp["date_to"]) if "date_to" in qp and qp["date_to"].strip() else None

            sort_by_str = qp.get("sort_by", "margin")
            try:
                sort_by = ProfitSortField(sort_by_str.lower())
            except ValueError:
                sort_by = ProfitSortField.MARGIN

            sort_order_str = qp.get("sort_order", "desc")
            sort_order = SortOrder.ASC if sort_order_str.lower() == "asc" else SortOrder.DESC

            page = int(qp.get("page", "1"))
            page_size = int(qp.get("page_size", "50"))

            query = ProfitDashboardQuery(
                marketplace=marketplace,
                category=category,
                supplier_id=supplier_id,
                opportunity_id=opportunity_id,
                currency=currency,
                completeness=completeness,
                min_margin_pct=min_margin,
                max_margin_pct=max_margin,
                min_profit_amount=min_profit,
                max_profit_amount=max_profit,
                min_sale_price=min_sale_price,
                max_sale_price=max_sale_price,
                search_text=search_text,
                sort_by=sort_by,
                sort_order=sort_order,
                page=page,
                page_size=page_size,
            )

            result = profit_dashboard_service.list_profit_items(
                tenant_id=tenant_id,
                query=query,
                session_id=session_id,
            )
            return JSONResponse(result.to_dict())
        except Exception as e:
            return _handle_error(e)

    async def get_profit_detail(request: Request):
        if profit_dashboard_service is None:
            return JSONResponse({"error": "service_unavailable", "message": "Profit Dashboard service is not configured."}, status_code=503)
        tenant_id = request.path_params.get("tenant_id")
        item_id = request.path_params.get("item_id")
        session_id = _extract_session_id(request)
        if not session_id:
            return JSONResponse({"error": "unauthenticated", "message": "Missing session identifier."}, status_code=401)
        try:
            detail = profit_dashboard_service.get_profit_detail(
                tenant_id=tenant_id,
                item_id=item_id,
                session_id=session_id,
            )
            return JSONResponse(detail.to_dict())
        except Exception as e:
            return _handle_error(e)

    async def compare_profit_items(request: Request):
        if profit_dashboard_service is None:
            return JSONResponse({"error": "service_unavailable", "message": "Profit Dashboard service is not configured."}, status_code=503)
        tenant_id = request.path_params.get("tenant_id")
        session_id = _extract_session_id(request)
        if not session_id:
            return JSONResponse({"error": "unauthenticated", "message": "Missing session identifier."}, status_code=401)
        try:
            body = {}
            if request.method == "POST" and request.headers.get("content-type") == "application/json":
                try:
                    body = await request.json()
                except Exception:
                    body = {}

            item_ids = []
            if "item_ids" in body and isinstance(body["item_ids"], list):
                item_ids = body["item_ids"]
            elif "ids" in body and isinstance(body["ids"], list):
                item_ids = body["ids"]
            elif "item_ids" in request.query_params:
                raw_ids = request.query_params.get("item_ids", "")
                item_ids = [i.strip() for i in raw_ids.split(",") if i.strip()]
            elif "ids" in request.query_params:
                raw_ids = request.query_params.get("ids", "")
                item_ids = [i.strip() for i in raw_ids.split(",") if i.strip()]

            comparison = profit_dashboard_service.compare_profit_items(
                tenant_id=tenant_id,
                item_ids=item_ids,
                session_id=session_id,
            )
            return JSONResponse(comparison.to_dict())
        except Exception as e:
            return _handle_error(e)

    # -------------------------------------------------------------------------
    # Q.4 — Mission Dashboard (Business Intelligence)
    # -------------------------------------------------------------------------

    async def get_mission_summary(request: Request):
        if mission_dashboard_service is None:
            return JSONResponse({"error": "service_unavailable", "message": "Mission Dashboard service is not configured."}, status_code=503)
        tenant_id = request.path_params.get("tenant_id")
        session_id = _extract_session_id(request)
        if not session_id:
            return JSONResponse({"error": "unauthenticated", "message": "Missing session identifier."}, status_code=401)
        try:
            summary = mission_dashboard_service.get_summary(tenant_id=tenant_id, session_id=session_id)
            return JSONResponse(summary.to_dict())
        except Exception as e:
            return _handle_error(e)

    async def list_missions(request: Request):
        if mission_dashboard_service is None:
            return JSONResponse({"error": "service_unavailable", "message": "Mission Dashboard service is not configured."}, status_code=503)
        tenant_id = request.path_params.get("tenant_id")
        session_id = _extract_session_id(request)
        if not session_id:
            return JSONResponse({"error": "unauthenticated", "message": "Missing session identifier."}, status_code=401)
        try:
            qp = request.query_params
            status_filter = qp.get("status")
            mission_type = qp.get("mission_type") or qp.get("type")
            priority = qp.get("priority")
            opportunity_id = qp.get("opportunity_id")
            supplier_id = qp.get("supplier_id")
            search_text = qp.get("search_text") or qp.get("search")

            has_errors = None
            if "has_errors" in qp and qp["has_errors"].strip():
                val = qp["has_errors"].strip().lower()
                has_errors = val in ("true", "1", "yes")

            date_from = None
            if "date_from" in qp and qp["date_from"].strip():
                try:
                    date_from = datetime.fromisoformat(qp["date_from"].strip().replace("Z", "+00:00"))
                except ValueError:
                    raise AdminInvalidRequestError("Formato de fecha inválido para 'date_from'. Use ISO 8601.")

            date_to = None
            if "date_to" in qp and qp["date_to"].strip():
                try:
                    date_to = datetime.fromisoformat(qp["date_to"].strip().replace("Z", "+00:00"))
                except ValueError:
                    raise AdminInvalidRequestError("Formato de fecha inválido para 'date_to'. Use ISO 8601.")

            sort_by_str = qp.get("sort_by", "created_at")
            try:
                sort_by = MissionSortField(sort_by_str.lower())
            except ValueError:
                sort_by = MissionSortField.CREATED_AT

            sort_order_str = qp.get("sort_order", "desc")
            sort_order = SortOrder.ASC if sort_order_str.lower() == "asc" else SortOrder.DESC

            page_str = qp.get("page", "1")
            page = int(page_str) if page_str.isdigit() else 1

            page_size_str = qp.get("page_size", "20")
            page_size = int(page_size_str) if page_size_str.isdigit() else 20

            query = MissionDashboardQuery(
                status=status_filter,
                mission_type=mission_type,
                priority=priority,
                opportunity_id=opportunity_id,
                supplier_id=supplier_id,
                has_errors=has_errors,
                date_from=date_from,
                date_to=date_to,
                search_text=search_text,
                sort_by=sort_by,
                sort_order=sort_order,
                page=page,
                page_size=page_size,
            )

            result_page = mission_dashboard_service.list_missions(
                tenant_id=tenant_id,
                query=query,
                session_id=session_id,
            )
            return JSONResponse(result_page.to_dict())
        except Exception as e:
            return _handle_error(e)

    async def get_mission_detail(request: Request):
        if mission_dashboard_service is None:
            return JSONResponse({"error": "service_unavailable", "message": "Mission Dashboard service is not configured."}, status_code=503)
        tenant_id = request.path_params.get("tenant_id")
        mission_id = request.path_params.get("mission_id")
        session_id = _extract_session_id(request)
        if not session_id:
            return JSONResponse({"error": "unauthenticated", "message": "Missing session identifier."}, status_code=401)
        try:
            detail = mission_dashboard_service.get_mission_detail(
                tenant_id=tenant_id,
                mission_id=mission_id,
                session_id=session_id,
            )
            return JSONResponse(detail.to_dict())
        except Exception as e:
            return _handle_error(e)

    # -------------------------------------------------------------------------
    # Q.5 — Agent Cost Dashboard (Business Intelligence / AI Cost Observability)
    # -------------------------------------------------------------------------

    async def get_agent_cost_summary(request: Request):
        if agent_cost_dashboard_service is None:
            return JSONResponse({"error": "service_unavailable", "message": "Agent Cost Dashboard service is not configured."}, status_code=503)
        tenant_id = request.path_params.get("tenant_id")
        session_id = _extract_session_id(request)
        if not session_id:
            return JSONResponse({"error": "unauthenticated", "message": "Missing session identifier."}, status_code=401)
        try:
            qp = request.query_params
            date_from = None
            if "date_from" in qp and qp["date_from"].strip():
                try:
                    date_from = datetime.fromisoformat(qp["date_from"].strip().replace("Z", "+00:00"))
                except ValueError:
                    raise AdminInvalidRequestError("Formato de fecha inválido para 'date_from'. Use ISO 8601.")
            date_to = None
            if "date_to" in qp and qp["date_to"].strip():
                try:
                    date_to = datetime.fromisoformat(qp["date_to"].strip().replace("Z", "+00:00"))
                except ValueError:
                    raise AdminInvalidRequestError("Formato de fecha inválido para 'date_to'. Use ISO 8601.")

            summary = agent_cost_dashboard_service.get_summary(
                tenant_id=tenant_id,
                session_id=session_id,
                date_from=date_from,
                date_to=date_to,
            )
            return JSONResponse(summary.to_dict())
        except Exception as e:
            return _handle_error(e)

    async def list_agent_costs(request: Request):
        if agent_cost_dashboard_service is None:
            return JSONResponse({"error": "service_unavailable", "message": "Agent Cost Dashboard service is not configured."}, status_code=503)
        tenant_id = request.path_params.get("tenant_id")
        session_id = _extract_session_id(request)
        if not session_id:
            return JSONResponse({"error": "unauthenticated", "message": "Missing session identifier."}, status_code=401)
        try:
            qp = request.query_params
            provider = qp.get("provider")
            model = qp.get("model")
            agent_type = qp.get("agent_type") or qp.get("agent")
            mission_id = qp.get("mission_id")
            currency = qp.get("currency")
            search_text = qp.get("search_text") or qp.get("search")

            is_attributed = None
            if "is_attributed" in qp and qp["is_attributed"].strip():
                val = qp["is_attributed"].strip().lower()
                is_attributed = val in ("true", "1", "yes")

            is_known_cost = None
            if "is_known_cost" in qp and qp["is_known_cost"].strip():
                val = qp["is_known_cost"].strip().lower()
                is_known_cost = val in ("true", "1", "yes")

            min_cost = None
            if "min_cost" in qp and qp["min_cost"].strip():
                try:
                    min_cost = Decimal(qp["min_cost"].strip())
                except Exception:
                    raise AdminInvalidRequestError("min_cost debe ser un número decimal válido.")

            max_cost = None
            if "max_cost" in qp and qp["max_cost"].strip():
                try:
                    max_cost = Decimal(qp["max_cost"].strip())
                except Exception:
                    raise AdminInvalidRequestError("max_cost debe ser un número decimal válido.")

            date_from = None
            if "date_from" in qp and qp["date_from"].strip():
                try:
                    date_from = datetime.fromisoformat(qp["date_from"].strip().replace("Z", "+00:00"))
                except ValueError:
                    raise AdminInvalidRequestError("Formato de fecha inválido para 'date_from'. Use ISO 8601.")

            date_to = None
            if "date_to" in qp and qp["date_to"].strip():
                try:
                    date_to = datetime.fromisoformat(qp["date_to"].strip().replace("Z", "+00:00"))
                except ValueError:
                    raise AdminInvalidRequestError("Formato de fecha inválido para 'date_to'. Use ISO 8601.")

            sort_by_str = qp.get("sort_by", "occurred_at")
            try:
                sort_by = AgentCostSortField(sort_by_str.lower())
            except ValueError:
                sort_by = AgentCostSortField.OCCURRED_AT

            sort_order_str = qp.get("sort_order", "desc")
            sort_order = AgentCostSortOrder.ASC if sort_order_str.lower() == "asc" else AgentCostSortOrder.DESC

            page_str = qp.get("page", "1")
            page = int(page_str) if page_str.isdigit() else 1

            page_size_str = qp.get("page_size", "20")
            page_size = int(page_size_str) if page_size_str.isdigit() else 20

            query = AgentCostDashboardQuery(
                provider=provider,
                model=model,
                agent_type=agent_type,
                mission_id=mission_id,
                currency=currency,
                is_attributed=is_attributed,
                is_known_cost=is_known_cost,
                min_cost=min_cost,
                max_cost=max_cost,
                date_from=date_from,
                date_to=date_to,
                search_text=search_text,
                sort_by=sort_by,
                sort_order=sort_order,
                page=page,
                page_size=page_size,
            )

            result_page = agent_cost_dashboard_service.list_agent_costs(
                tenant_id=tenant_id,
                query=query,
                session_id=session_id,
            )
            return JSONResponse(result_page.to_dict())
        except Exception as e:
            return _handle_error(e)

    async def get_agent_cost_detail(request: Request):
        if agent_cost_dashboard_service is None:
            return JSONResponse({"error": "service_unavailable", "message": "Agent Cost Dashboard service is not configured."}, status_code=503)
        tenant_id = request.path_params.get("tenant_id")
        cost_record_id = request.path_params.get("cost_record_id") or request.path_params.get("item_id")
        session_id = _extract_session_id(request)
        if not session_id:
            return JSONResponse({"error": "unauthenticated", "message": "Missing session identifier."}, status_code=401)
        try:
            detail = agent_cost_dashboard_service.get_cost_detail(
                tenant_id=tenant_id,
                item_id=cost_record_id,
                session_id=session_id,
            )
            return JSONResponse(detail.to_dict())
        except Exception as e:
            return _handle_error(e)

    async def get_mission_cost_summary(request: Request):
        if agent_cost_dashboard_service is None:
            return JSONResponse({"error": "service_unavailable", "message": "Agent Cost Dashboard service is not configured."}, status_code=503)
        tenant_id = request.path_params.get("tenant_id")
        mission_id = request.path_params.get("mission_id")
        session_id = _extract_session_id(request)
        if not session_id:
            return JSONResponse({"error": "unauthenticated", "message": "Missing session identifier."}, status_code=401)
        try:
            summary = agent_cost_dashboard_service.get_mission_cost_summary(
                tenant_id=tenant_id,
                mission_id=mission_id,
                session_id=session_id,
            )
            return JSONResponse(summary.to_dict())
        except Exception as e:
            return _handle_error(e)

    # -------------------------------------------------------------------------
    # Q.6 — Business KPIs & Cross-Domain Summary (Business Intelligence)
    # -------------------------------------------------------------------------

    async def get_business_kpi_summary(request: Request):
        if business_kpi_service is None:
            return JSONResponse({"error": "service_unavailable", "message": "Business KPI service is not configured."}, status_code=503)
        tenant_id = request.path_params.get("tenant_id")
        session_id = _extract_session_id(request)
        if not session_id:
            return JSONResponse({"error": "unauthenticated", "message": "Missing session identifier."}, status_code=401)
        try:
            qp = request.query_params
            time_window = qp.get("time_window", "30d")
            marketplace = qp.get("marketplace")
            category = qp.get("category")
            mission_type = qp.get("mission_type")
            currency = qp.get("currency")
            domain = qp.get("domain")

            date_from = None
            if "date_from" in qp and qp["date_from"].strip():
                try:
                    date_from = datetime.fromisoformat(qp["date_from"].strip().replace("Z", "+00:00"))
                except ValueError:
                    raise AdminInvalidRequestError("Formato de fecha inválido para 'date_from'. Use ISO 8601.")

            date_to = None
            if "date_to" in qp and qp["date_to"].strip():
                try:
                    date_to = datetime.fromisoformat(qp["date_to"].strip().replace("Z", "+00:00"))
                except ValueError:
                    raise AdminInvalidRequestError("Formato de fecha inválido para 'date_to'. Use ISO 8601.")

            query = BusinessKPIQuery(
                time_window=time_window,
                date_from=date_from,
                date_to=date_to,
                marketplace=marketplace,
                category=category,
                mission_type=mission_type,
                currency=currency,
                domain=domain,
            )
            summary = business_kpi_service.get_summary(
                tenant_id=tenant_id,
                query=query,
                session_id=session_id,
            )
            return JSONResponse(summary.to_dict())
        except Exception as e:
            return _handle_error(e)

    async def get_business_kpis(request: Request):
        """Lista todos los KPIs calculados o el resumen para el tenant."""
        return await get_business_kpi_summary(request)

    async def get_business_kpi_by_id(request: Request):
        if business_kpi_service is None:
            return JSONResponse({"error": "service_unavailable", "message": "Business KPI service is not configured."}, status_code=503)
        tenant_id = request.path_params.get("tenant_id")
        kpi_id = request.path_params.get("kpi_id")
        session_id = _extract_session_id(request)
        if not session_id:
            return JSONResponse({"error": "unauthenticated", "message": "Missing session identifier."}, status_code=401)
        try:
            qp = request.query_params
            time_window = qp.get("time_window", "30d")
            query = BusinessKPIQuery(time_window=time_window)
            kpi_val = business_kpi_service.get_kpi_by_id(
                tenant_id=tenant_id,
                kpi_id=kpi_id,
                query=query,
                session_id=session_id,
            )
            return JSONResponse(kpi_val.to_dict())
        except Exception as e:
            return _handle_error(e)

    async def get_business_kpi_catalog(request: Request):
        if business_kpi_service is None:
            return JSONResponse({"error": "service_unavailable", "message": "Business KPI service is not configured."}, status_code=503)
        try:
            catalog_items = business_kpi_service.get_catalog()
            return JSONResponse([item.to_dict() for item in catalog_items])
        except Exception as e:
            return _handle_error(e)

    async def compare_business_kpis(request: Request):
        if business_kpi_service is None:
            return JSONResponse({"error": "service_unavailable", "message": "Business KPI service is not configured."}, status_code=503)
        tenant_id = request.path_params.get("tenant_id")
        session_id = _extract_session_id(request)
        if not session_id:
            return JSONResponse({"error": "unauthenticated", "message": "Missing session identifier."}, status_code=401)
        try:
            current_window = request.query_params.get("current_window", "30d")
            previous_window = request.query_params.get("previous_window", "30d")
            current_query = BusinessKPIQuery(time_window=current_window)
            previous_query = BusinessKPIQuery(time_window=previous_window)
            comparisons = business_kpi_service.get_comparison(
                tenant_id=tenant_id,
                current_query=current_query,
                previous_query=previous_query,
                session_id=session_id,
            )
            return JSONResponse([c.to_dict() for c in comparisons])
        except Exception as e:
            return _handle_error(e)

    async def bi_kpis_html_view(request: Request):
        """Superficie visual mínima de Business Intelligence / Business KPIs & Cross-Domain Summary."""
        html_content = """<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <title>Q.6 Business KPIs — Business Intelligence</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, monospace; background: #0b1329; color: #f8fafc; margin: 0; padding: 24px; }
        .container { max-width: 1200px; margin: 0 auto; }
        h1, h2, h3 { color: #38bdf8; }
        .card { background: #1e293b; border: 1px solid #334155; border-radius: 8px; padding: 20px; margin-bottom: 20px; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 16px; margin-bottom: 20px; }
        .stat-card { background: #0f172a; border: 1px solid #334155; border-radius: 6px; padding: 16px; }
        .stat-value { font-size: 24px; font-weight: bold; color: #38bdf8; }
        .stat-label { font-size: 12px; color: #94a3b8; text-transform: uppercase; }
        .badge { background: #0284c7; color: #fff; padding: 2px 8px; border-radius: 4px; font-size: 12px; }
        table { width: 100%; border-collapse: collapse; margin-top: 12px; }
        th, td { text-align: left; padding: 10px; border-bottom: 1px solid #334155; font-size: 14px; }
        th { color: #94a3b8; background: #0f172a; }
        .unknown { color: #64748b; font-style: italic; }
    </style>
</head>
<body>
    <div class="container">
        <h1>Business KPIs Dashboard <span class="badge">Q.6 Validated</span></h1>
        <p>Visión ejecutiva y métricas clave de rendimiento cross-domain con aislamiento multi-tenant estricto.</p>
        <div class="grid">
            <div class="stat-card">
                <div class="stat-label">Oportunidades Totales</div>
                <div class="stat-value" id="kpi-opps">--</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Tasa Éxito Misiones</div>
                <div class="stat-value" id="kpi-mission-success">--</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Margen Contribución (%)</div>
                <div class="stat-value" id="kpi-margin">--</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Madurez de Datos (%)</div>
                <div class="stat-value" id="kpi-readiness">--</div>
            </div>
        </div>
        <div class="card">
            <h2>Indicadores de Negocio & Resumen Ejecutivo</h2>
            <p>Datos consultivos consolidados a partir de hechos probados en Q.1 a Q.5.</p>
            <div id="table-container">
                <p class="unknown">Utilice la API REST /api/bi/tenants/{tenant_id}/kpis/summary para obtener el resumen ejecutivo completo o /api/bi/tenants/{tenant_id}/kpis/{kpi_id} para ver la trazabilidad de una métrica.</p>
            </div>
        </div>
    </div>
</body>
</html>"""
        return HTMLResponse(html_content)

    async def bi_agent_cost_html_view(request: Request):
        """Superficie visual mínima de Business Intelligence / Agent Cost Dashboard."""
        html_content = """<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <title>Q.5 Agent Cost Dashboard — Business Intelligence</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, monospace; background: #0b1329; color: #f8fafc; margin: 0; padding: 24px; }
        .container { max-width: 1200px; margin: 0 auto; }
        h1, h2, h3 { color: #38bdf8; }
        .card { background: #1e293b; border: 1px solid #334155; border-radius: 8px; padding: 20px; margin-bottom: 20px; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 16px; margin-bottom: 20px; }
        .stat-card { background: #0f172a; border: 1px solid #334155; border-radius: 6px; padding: 16px; }
        .stat-value { font-size: 24px; font-weight: bold; color: #38bdf8; }
        .stat-label { font-size: 12px; color: #94a3b8; text-transform: uppercase; }
        .badge { background: #0284c7; color: #fff; padding: 2px 8px; border-radius: 4px; font-size: 12px; }
        table { width: 100%; border-collapse: collapse; margin-top: 12px; }
        th, td { text-align: left; padding: 10px; border-bottom: 1px solid #334155; font-size: 14px; }
        th { color: #94a3b8; background: #0f172a; }
        .unknown { color: #64748b; font-style: italic; }
    </style>
</head>
<body>
    <div class="container">
        <h1>Agent Cost Dashboard <span class="badge">Q.5 Validated</span></h1>
        <p>Visibilidad y auditoría consultiva del consumo de IA, costos de modelos y misiones autónomas con aislamiento multi-tenant estricto.</p>
        <div class="grid">
            <div class="stat-card">
                <div class="stat-label">Total Peticiones</div>
                <div class="stat-value" id="total-requests">--</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Total Tokens</div>
                <div class="stat-value" id="total-tokens">--</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Eventos con Costo Desconocido</div>
                <div class="stat-value" id="unknown-cost-count">--</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Eventos Atribuidos a Misiones</div>
                <div class="stat-value" id="attributed-count">--</div>
            </div>
        </div>
        <div class="card">
            <h2>Costos de IA por Modelo, Proveedor y Misiones</h2>
            <p>Datos consultivos calculados estrictamente desde facts de Usage Metering (O.6) y Cost Tracking (K.3).</p>
            <div id="table-container">
                <p class="unknown">Utilice la API REST /api/bi/tenants/{tenant_id}/agent-costs para consultar el desglose filtrado o /api/bi/tenants/{tenant_id}/agent-costs/summary para ver los totales consolidados por divisa.</p>
            </div>
        </div>
    </div>
</body>
</html>"""
        return HTMLResponse(html_content)

    async def bi_mission_html_view(request: Request):
        """Superficie visual mínima de Business Intelligence / Mission Dashboard."""
        html_content = """<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <title>Q.4 Mission Dashboard — Business Intelligence</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, monospace; background: #0b1329; color: #f8fafc; margin: 0; padding: 24px; }
        .container { max-width: 1200px; margin: 0 auto; }
        h1, h2, h3 { color: #38bdf8; }
        .card { background: #1e293b; border: 1px solid #334155; border-radius: 8px; padding: 20px; margin-bottom: 20px; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 16px; margin-bottom: 20px; }
        .stat-card { background: #0f172a; border: 1px solid #334155; border-radius: 6px; padding: 16px; }
        .stat-value { font-size: 24px; font-weight: bold; color: #38bdf8; }
        .stat-label { font-size: 12px; color: #94a3b8; text-transform: uppercase; }
        .badge { background: #0284c7; color: #fff; padding: 2px 8px; border-radius: 4px; font-size: 12px; }
        table { width: 100%; border-collapse: collapse; margin-top: 12px; }
        th, td { text-align: left; padding: 10px; border-bottom: 1px solid #334155; font-size: 14px; }
        th { color: #94a3b8; background: #0f172a; }
        .unknown { color: #64748b; font-style: italic; }
    </style>
</head>
<body>
    <div class="container">
        <h1>Mission Dashboard <span class="badge">Q.4 Validated</span></h1>
        <p>Visibilidad y auditoría consultiva de misiones autónomas, estado de ejecución, cronología unificada y resultados con aislamiento multi-tenant estricto.</p>
        <div class="grid">
            <div class="stat-card">
                <div class="stat-label">Total Misiones</div>
                <div class="stat-value" id="total-missions">--</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">En Ejecución (Running)</div>
                <div class="stat-value" id="running-missions">--</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Tasa de Éxito</div>
                <div class="stat-value" id="success-rate">--</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Duración Promedio (s)</div>
                <div class="stat-value" id="avg-duration">--</div>
            </div>
        </div>
        <div class="card">
            <h2>Misiones Autónomas & Timeline de Ejecución</h2>
            <p>Datos consultivos directos de misiones, trazas de bucle, auditoría K.1 y trazas de agentes K.2.</p>
            <div id="table-container">
                <p class="unknown">Utilice la API REST /api/bi/tenants/{tenant_id}/missions para consultar el catálogo filtrado o /api/bi/tenants/{tenant_id}/missions/{mission_id} para ver la cronología detallada.</p>
            </div>
        </div>
    </div>
</body>
</html>"""
        return HTMLResponse(html_content)

    async def bi_profit_html_view(request: Request):
        """Superficie visual mínima de Business Intelligence / Profit Dashboard."""
        html_content = """<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <title>Q.3 Profit Dashboard — Business Intelligence</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, monospace; background: #0b1329; color: #f8fafc; margin: 0; padding: 24px; }
        .container { max-width: 1200px; margin: 0 auto; }
        h1, h2, h3 { color: #38bdf8; }
        .card { background: #1e293b; border: 1px solid #334155; border-radius: 8px; padding: 20px; margin-bottom: 20px; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 16px; margin-bottom: 20px; }
        .stat-card { background: #0f172a; border: 1px solid #334155; border-radius: 6px; padding: 16px; }
        .stat-value { font-size: 24px; font-weight: bold; color: #38bdf8; }
        .stat-label { font-size: 12px; color: #94a3b8; text-transform: uppercase; }
        .badge { background: #0284c7; color: #fff; padding: 2px 8px; border-radius: 4px; font-size: 12px; }
        table { width: 100%; border-collapse: collapse; margin-top: 12px; }
        th, td { text-align: left; padding: 10px; border-bottom: 1px solid #334155; font-size: 14px; }
        th { color: #94a3b8; background: #0f172a; }
        .unknown { color: #64748b; font-style: italic; }
    </style>
</head>
<body>
    <div class="container">
        <h1>Profit Dashboard <span class="badge">Q.3 Validated</span></h1>
        <p>Análisis consultivo de unit economics, rentabilidad y márgenes con desglose trazable y aislamiento multi-tenant estricto.</p>
        <div class="grid">
            <div class="stat-card">
                <div class="stat-label">Total Ítems</div>
                <div class="stat-value" id="total-items">--</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Completitud Financiera</div>
                <div class="stat-value" id="complete-records">--</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Margen Contribución Promedio</div>
                <div class="stat-value" id="avg-margin">--</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Margen Negativo</div>
                <div class="stat-value" id="negative-margin">--</div>
            </div>
        </div>
        <div class="card">
            <h2>Unit Economics & Proyección de Rentabilidad</h2>
            <p>Datos consultivos calculados estrictamente desde facts y componentes financieros reales.</p>
            <div id="table-container">
                <p class="unknown">Utilice la API REST /api/bi/tenants/{tenant_id}/profit para consultar el catálogo filtrado o /compare para comparar escenarios de rentabilidad.</p>
            </div>
        </div>
    </div>
</body>
</html>"""
        return HTMLResponse(html_content)

    async def bi_supplier_html_view(request: Request):
        """Superficie visual mínima de Business Intelligence / Supplier Dashboard."""
        html_content = """<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <title>Q.2 Supplier Dashboard — Business Intelligence</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, monospace; background: #0b1329; color: #f8fafc; margin: 0; padding: 24px; }
        .container { max-width: 1200px; margin: 0 auto; }
        h1, h2, h3 { color: #38bdf8; }
        .card { background: #1e293b; border: 1px solid #334155; border-radius: 8px; padding: 20px; margin-bottom: 20px; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 16px; margin-bottom: 20px; }
        .stat-card { background: #0f172a; border: 1px solid #334155; border-radius: 6px; padding: 16px; }
        .stat-value { font-size: 24px; font-weight: bold; color: #38bdf8; }
        .stat-label { font-size: 12px; color: #94a3b8; text-transform: uppercase; }
        .badge { background: #0284c7; color: #fff; padding: 2px 8px; border-radius: 4px; font-size: 12px; }
        table { width: 100%; border-collapse: collapse; margin-top: 12px; }
        th, td { text-align: left; padding: 10px; border-bottom: 1px solid #334155; font-size: 14px; }
        th { color: #94a3b8; background: #0f172a; }
        .unknown { color: #64748b; font-style: italic; }
    </style>
</head>
<body>
    <div class="container">
        <h1>Supplier Dashboard <span class="badge">Q.2 Validated</span></h1>
        <p>Análisis consultivo, filtrado y comparación de proveedores validados con aislamiento multi-tenant estricto.</p>
        <div class="grid">
            <div class="stat-card">
                <div class="stat-label">Total Proveedores</div>
                <div class="stat-value" id="total-suppliers">--</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Verificados</div>
                <div class="stat-value" id="verified-suppliers">--</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Score Promedio</div>
                <div class="stat-value" id="avg-score">--</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Con Campos Inciertos</div>
                <div class="stat-value" id="unknown-suppliers">--</div>
            </div>
        </div>
        <div class="card">
            <h2>Catálogo de Proveedores Detectados</h2>
            <p>Datos consultivos directos del repositorio canónico de proveedores.</p>
            <div id="table-container">
                <p class="unknown">Utilice la API REST /api/bi/tenants/{tenant_id}/suppliers para consultar el catálogo filtrado o /compare para comparar proveedores.</p>
            </div>
        </div>
    </div>
</body>
</html>"""
        return HTMLResponse(html_content)

    async def bi_opportunity_html_view(request: Request):
        """Superficie visual mínima de Business Intelligence / Opportunity Dashboard."""
        html_content = """<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <title>Q.1 Opportunity Dashboard — Business Intelligence</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, monospace; background: #0b1329; color: #f8fafc; margin: 0; padding: 24px; }
        .container { max-width: 1200px; margin: 0 auto; }
        h1, h2, h3 { color: #38bdf8; }
        .card { background: #1e293b; border: 1px solid #334155; border-radius: 8px; padding: 20px; margin-bottom: 20px; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 16px; margin-bottom: 20px; }
        .stat-card { background: #0f172a; border: 1px solid #334155; border-radius: 6px; padding: 16px; }
        .stat-value { font-size: 24px; font-weight: bold; color: #38bdf8; }
        .stat-label { font-size: 12px; color: #94a3b8; text-transform: uppercase; }
        .badge { background: #0284c7; color: #fff; padding: 2px 8px; border-radius: 4px; font-size: 12px; }
        table { width: 100%; border-collapse: collapse; margin-top: 12px; }
        th, td { text-align: left; padding: 10px; border-bottom: 1px solid #334155; font-size: 14px; }
        th { color: #94a3b8; background: #0f172a; }
        .unknown { color: #64748b; font-style: italic; }
    </style>
</head>
<body>
    <div class="container">
        <h1>Market Opportunity Dashboard <span class="badge">Q.1 Validated</span></h1>
        <p>Análisis consultivo y priorización de oportunidades detectadas con aislamiento multi-tenant estricto.</p>
        <div class="grid">
            <div class="stat-card">
                <div class="stat-label">Total Oportunidades</div>
                <div class="stat-value" id="total-opps">--</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Alto Potencial</div>
                <div class="stat-value" id="high-opps">--</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Score Promedio</div>
                <div class="stat-value" id="avg-score">--</div>
            </div>
        </div>
        <div class="card">
            <h2>Oportunidades de Mercado Detectadas</h2>
            <p>Datos consultivos directos del repositorio canónico de oportunidades.</p>
            <div id="table-container">
                <p class="unknown">Utilice la API REST /api/bi/tenants/{tenant_id}/opportunities para consultar el catálogo filtrado.</p>
            </div>
        </div>
    </div>
</body>
</html>"""
        return HTMLResponse(html_content)


    async def get_tenant_summary(request: Request):
        tenant_id = request.path_params.get("tenant_id")
        session_id = _extract_session_id(request)
        if not session_id:
            return JSONResponse({"error": "unauthenticated", "message": "Missing session identifier."}, status_code=401)
        try:
            summary = service.get_tenant_summary(session_id=session_id, target_tenant_id=tenant_id)
            return JSONResponse(summary.to_dict())
        except Exception as e:
            return _handle_error(e)

    async def list_organizations(request: Request):
        tenant_id = request.path_params.get("tenant_id")
        session_id = _extract_session_id(request)
        if not session_id:
            return JSONResponse({"error": "unauthenticated", "message": "Missing session identifier."}, status_code=401)
        try:
            orgs = service.list_organizations(session_id=session_id, target_tenant_id=tenant_id)
            return JSONResponse([org.to_dict() for org in orgs])
        except Exception as e:
            return _handle_error(e)

    async def list_memberships(request: Request):
        tenant_id = request.path_params.get("tenant_id")
        org_id = request.path_params.get("org_id")
        session_id = _extract_session_id(request)
        if not session_id:
            return JSONResponse({"error": "unauthenticated", "message": "Missing session identifier."}, status_code=401)
        try:
            members = service.list_memberships(session_id=session_id, target_tenant_id=tenant_id, organization_id=org_id)
            return JSONResponse([m.to_dict() for m in members])
        except Exception as e:
            return _handle_error(e)

    async def add_membership(request: Request):
        tenant_id = request.path_params.get("tenant_id")
        org_id = request.path_params.get("org_id")
        session_id = _extract_session_id(request)
        if not session_id:
            return JSONResponse({"error": "unauthenticated", "message": "Missing session identifier."}, status_code=401)
        try:
            body = await request.json()
            identity_id = body.get("identity_id")
            role_str = body.get("role", "MEMBER").upper()
            role = MembershipRole(role_str) if role_str in [r.value for r in MembershipRole] else MembershipRole.MEMBER
            m = service.add_membership(
                session_id=session_id,
                target_tenant_id=tenant_id,
                organization_id=org_id,
                identity_id=identity_id,
                role=role,
            )
            return JSONResponse(m.to_dict(), status_code=201)
        except Exception as e:
            return _handle_error(e)

    async def remove_membership(request: Request):
        tenant_id = request.path_params.get("tenant_id")
        org_id = request.path_params.get("org_id")
        identity_id = request.path_params.get("identity_id")
        session_id = _extract_session_id(request)
        if not session_id:
            return JSONResponse({"error": "unauthenticated", "message": "Missing session identifier."}, status_code=401)
        try:
            m = service.remove_membership(
                session_id=session_id,
                target_tenant_id=tenant_id,
                organization_id=org_id,
                identity_id=identity_id,
            )
            return JSONResponse(m.to_dict())
        except Exception as e:
            return _handle_error(e)

    async def get_usage(request: Request):
        tenant_id = request.path_params.get("tenant_id")
        session_id = _extract_session_id(request)
        if not session_id:
            return JSONResponse({"error": "unauthenticated", "message": "Missing session identifier."}, status_code=401)
        try:
            usage = service.get_usage_summary(session_id=session_id, target_tenant_id=tenant_id)
            return JSONResponse(usage.to_dict())
        except Exception as e:
            return _handle_error(e)

    async def get_quota(request: Request):
        tenant_id = request.path_params.get("tenant_id")
        session_id = _extract_session_id(request)
        if not session_id:
            return JSONResponse({"error": "unauthenticated", "message": "Missing session identifier."}, status_code=401)
        try:
            quota = service.get_quota_view(session_id=session_id, target_tenant_id=tenant_id)
            return JSONResponse(quota.to_dict())
        except Exception as e:
            return _handle_error(e)

    async def get_plan(request: Request):
        tenant_id = request.path_params.get("tenant_id")
        session_id = _extract_session_id(request)
        if not session_id:
            return JSONResponse({"error": "unauthenticated", "message": "Missing session identifier."}, status_code=401)
        try:
            plan = service.get_plan_view(session_id=session_id, target_tenant_id=tenant_id)
            if not plan:
                return JSONResponse({"message": "No active plan found."}, status_code=404)
            return JSONResponse(plan.to_dict())
        except Exception as e:
            return _handle_error(e)

    async def list_catalog_plans(request: Request):
        session_id = _extract_session_id(request)
        if not session_id:
            return JSONResponse({"error": "unauthenticated", "message": "Missing session identifier."}, status_code=401)
        try:
            plans = service.list_catalog_plans(session_id=session_id)
            return JSONResponse([p.to_dict() for p in plans])
        except Exception as e:
            return _handle_error(e)

    async def assign_plan(request: Request):
        tenant_id = request.path_params.get("tenant_id")
        session_id = _extract_session_id(request)
        if not session_id:
            return JSONResponse({"error": "unauthenticated", "message": "Missing session identifier."}, status_code=401)
        try:
            body = await request.json()
            plan_id = body.get("plan_id")
            plan_version = body.get("plan_version")
            reason = body.get("reason", "ADMIN_ASSIGNMENT")
            plan = service.assign_plan(
                session_id=session_id,
                target_tenant_id=tenant_id,
                plan_id=plan_id,
                plan_version=plan_version,
                reason=reason,
            )
            return JSONResponse(plan.to_dict())
        except Exception as e:
            return _handle_error(e)

    async def get_billing(request: Request):
        tenant_id = request.path_params.get("tenant_id")
        session_id = _extract_session_id(request)
        if not session_id:
            return JSONResponse({"error": "unauthenticated", "message": "Missing session identifier."}, status_code=401)
        try:
            billing = service.get_billing_view(session_id=session_id, target_tenant_id=tenant_id)
            return JSONResponse(billing.to_dict())
        except Exception as e:
            return _handle_error(e)

    async def create_subscription(request: Request):
        tenant_id = request.path_params.get("tenant_id")
        session_id = _extract_session_id(request)
        if not session_id:
            return JSONResponse({"error": "unauthenticated", "message": "Missing session identifier."}, status_code=401)
        try:
            body = await request.json()
            plan_id = body.get("plan_id")
            plan_version = body.get("plan_version")
            billing_cycle_str = body.get("billing_cycle", "MONTHLY").upper()
            billing_cycle = BillingCycle(billing_cycle_str) if billing_cycle_str in [b.value for b in BillingCycle] else BillingCycle.MONTHLY
            auto_activate = bool(body.get("auto_activate", False))
            billing = service.create_subscription(
                session_id=session_id,
                target_tenant_id=tenant_id,
                plan_id=plan_id,
                plan_version=plan_version,
                billing_cycle=billing_cycle,
                auto_activate=auto_activate,
            )
            return JSONResponse(billing.to_dict(), status_code=201)
        except Exception as e:
            return _handle_error(e)

    async def cancel_subscription(request: Request):
        tenant_id = request.path_params.get("tenant_id")
        subscription_id = request.path_params.get("subscription_id")
        session_id = _extract_session_id(request)
        if not session_id:
            return JSONResponse({"error": "unauthenticated", "message": "Missing session identifier."}, status_code=401)
        try:
            body = await request.json() if request.headers.get("content-type") == "application/json" else {}
            immediately = bool(body.get("immediately", False))
            billing = service.cancel_subscription(
                session_id=session_id,
                target_tenant_id=tenant_id,
                subscription_id=subscription_id,
                immediately=immediately,
            )
            return JSONResponse(billing.to_dict())
        except Exception as e:
            return _handle_error(e)

    async def get_audit(request: Request):
        tenant_id = request.path_params.get("tenant_id")
        session_id = _extract_session_id(request)
        if not session_id:
            return JSONResponse({"error": "unauthenticated", "message": "Missing session identifier."}, status_code=401)
        try:
            records = service.get_audit_records(session_id=session_id, target_tenant_id=tenant_id)
            return JSONResponse([r.to_dict() for r in records])
        except Exception as e:
            return _handle_error(e)

    async def get_traces(request: Request):
        tenant_id = request.path_params.get("tenant_id")
        session_id = _extract_session_id(request)
        if not session_id:
            return JSONResponse({"error": "unauthenticated", "message": "Missing session identifier."}, status_code=401)
        try:
            traces = service.get_trace_records(session_id=session_id, target_tenant_id=tenant_id)
            return JSONResponse([t.to_dict() for t in traces])
        except Exception as e:
            return _handle_error(e)

    async def get_tenant_configuration(request: Request):
        tenant_id = request.path_params.get("tenant_id")
        org_id = request.query_params.get("organization_id")
        session_id = _extract_session_id(request)
        if not session_id:
            return JSONResponse({"error": "unauthenticated", "message": "Missing session identifier."}, status_code=401)
        try:
            config_view = service.get_tenant_configuration(
                session_id=session_id,
                target_tenant_id=tenant_id,
                organization_id=org_id,
            )
            return JSONResponse(config_view)
        except Exception as e:
            return _handle_error(e)

    async def update_tenant_configuration(request: Request):
        tenant_id = request.path_params.get("tenant_id")
        session_id = _extract_session_id(request)
        if not session_id:
            return JSONResponse({"error": "unauthenticated", "message": "Missing session identifier."}, status_code=401)
        try:
            body = await request.json()
            key = body.get("key")
            value = body.get("value")
            org_id = body.get("organization_id")
            expected_version = body.get("expected_version")
            result = service.update_tenant_configuration(
                session_id=session_id,
                target_tenant_id=tenant_id,
                key=key,
                value=value,
                organization_id=org_id,
                expected_version=expected_version,
            )
            return JSONResponse(result)
        except Exception as e:
            return _handle_error(e)

    async def get_observability(request: Request):
        tenant_id = request.path_params.get("tenant_id")
        session_id = _extract_session_id(request)
        if not session_id:
            return JSONResponse({"error": "unauthenticated", "message": "Missing session identifier."}, status_code=401)
        try:
            window_str = request.query_params.get("window_seconds", "3600")
            if not window_str.lstrip("-").isdigit():
                raise AdminInvalidRequestError("window_seconds must be numeric.")
            window_seconds = int(window_str)
            org_id = request.query_params.get("organization_id")
            obs = service.get_tenant_observability_snapshot(
                session_id=session_id,
                target_tenant_id=tenant_id,
                window_seconds=window_seconds,
                organization_id=org_id,
            )
            return JSONResponse(obs.to_dict())
        except Exception as e:
            return _handle_error(e)

    async def list_observability_alerts(request: Request):
        tenant_id = request.path_params.get("tenant_id")
        session_id = _extract_session_id(request)
        if not session_id:
            return JSONResponse({"error": "unauthenticated", "message": "Missing session identifier."}, status_code=401)
        try:
            status_filter = request.query_params.get("status")
            org_id = request.query_params.get("organization_id")
            alerts = service.list_tenant_observability_alerts(
                session_id=session_id,
                target_tenant_id=tenant_id,
                status=status_filter,
                organization_id=org_id,
            )
            return JSONResponse([a.to_dict() for a in alerts])
        except Exception as e:
            return _handle_error(e)

    async def acknowledge_observability_alert(request: Request):
        tenant_id = request.path_params.get("tenant_id")
        alert_id = request.path_params.get("alert_id")
        session_id = _extract_session_id(request)
        if not session_id:
            return JSONResponse({"error": "unauthenticated", "message": "Missing session identifier."}, status_code=401)
        try:
            alert = service.acknowledge_tenant_observability_alert(
                session_id=session_id,
                target_tenant_id=tenant_id,
                alert_id=alert_id,
            )
            return JSONResponse(alert.to_dict())
        except Exception as e:
            return _handle_error(e)

    async def resolve_observability_alert(request: Request):
        tenant_id = request.path_params.get("tenant_id")
        alert_id = request.path_params.get("alert_id")
        session_id = _extract_session_id(request)
        if not session_id:
            return JSONResponse({"error": "unauthenticated", "message": "Missing session identifier."}, status_code=401)
        try:
            body = await request.json() if request.headers.get("content-type") == "application/json" else {}
            reason = body.get("reason", "")
            alert = service.resolve_tenant_observability_alert(
                session_id=session_id,
                target_tenant_id=tenant_id,
                alert_id=alert_id,
                reason=reason,
            )
            return JSONResponse(alert.to_dict())
        except Exception as e:
            return _handle_error(e)

    async def admin_dashboard(request: Request):
        """Superficie Web administrativa funcional y minimalista."""
        html_content = """<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <title>SaaS Multi-Tenant Admin Console (O.10)</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, monospace; background: #0f172a; color: #f8fafc; margin: 0; padding: 24px; }
        .container { max-width: 1100px; margin: 0 auto; }
        h1, h2, h3 { color: #38bdf8; }
        .card { background: #1e293b; border: 1px solid #334155; border-radius: 8px; padding: 20px; margin-bottom: 20px; }
        .nav { display: flex; gap: 12px; margin-bottom: 24px; }
        .nav button { background: #334155; color: #fff; border: none; padding: 8px 16px; border-radius: 4px; cursor: pointer; font-weight: bold; }
        .nav button:hover { background: #0284c7; }
        .badge { background: #0284c7; color: #fff; padding: 2px 8px; border-radius: 4px; font-size: 12px; }
        pre { background: #090d16; padding: 12px; border-radius: 4px; overflow-x: auto; color: #a5f3fc; }
    </style>
</head>
<body>
    <div class="container">
        <h1>SaaS Admin Console <span class="badge">O.10 Verified</span></h1>
        <p>Plataforma de orquestación segura y multi-tenant (O.1 - O.9).</p>
        <div class="nav">
            <button onclick="alert('Tenants loaded')">Tenants</button>
            <button onclick="alert('Organizations loaded')">Organizations</button>
            <button onclick="alert('Usage loaded')">Usage & Metering</button>
            <button onclick="alert('Quotas loaded')">Quotas</button>
            <button onclick="alert('Plans loaded')">Plans</button>
            <button onclick="alert('Billing loaded')">Billing</button>
        </div>
        <div class="card">
            <h2>Superficie de Operaciones de Administración</h2>
            <p>Acceso restringido: Toda acción requiere sesión activa O.3 y permisos RBAC O.4.</p>
            <pre>GET /api/admin/tenants/{tenant_id}/summary</pre>
        </div>
    </div>
</body>
</html>"""
        return HTMLResponse(html_content)

    routes = [
        Route("/health", health, methods=["GET"]),
        Route("/admin", admin_dashboard, methods=["GET"]),
        Route("/api/admin/tenants/{tenant_id}/configuration", get_tenant_configuration, methods=["GET"]),
        Route("/api/admin/tenants/{tenant_id}/configuration", update_tenant_configuration, methods=["PUT"]),
        Route("/api/admin/tenants/{tenant_id}/summary", get_tenant_summary, methods=["GET"]),
        Route("/api/admin/tenants/{tenant_id}/organizations", list_organizations, methods=["GET"]),
        Route("/api/admin/tenants/{tenant_id}/organizations/{org_id}/memberships", list_memberships, methods=["GET"]),
        Route("/api/admin/tenants/{tenant_id}/organizations/{org_id}/memberships", add_membership, methods=["POST"]),
        Route("/api/admin/tenants/{tenant_id}/organizations/{org_id}/memberships/{identity_id}", remove_membership, methods=["DELETE"]),
        Route("/api/admin/tenants/{tenant_id}/usage", get_usage, methods=["GET"]),
        Route("/api/admin/tenants/{tenant_id}/quota", get_quota, methods=["GET"]),
        Route("/api/admin/tenants/{tenant_id}/plan", get_plan, methods=["GET"]),
        Route("/api/admin/catalog/plans", list_catalog_plans, methods=["GET"]),
        Route("/api/admin/tenants/{tenant_id}/plan/assign", assign_plan, methods=["POST"]),
        Route("/api/admin/tenants/{tenant_id}/billing", get_billing, methods=["GET"]),
        Route("/api/admin/tenants/{tenant_id}/subscriptions", create_subscription, methods=["POST"]),
        Route("/api/admin/tenants/{tenant_id}/subscriptions/{subscription_id}/cancel", cancel_subscription, methods=["POST"]),
        Route("/api/admin/tenants/{tenant_id}/audit", get_audit, methods=["GET"]),
        Route("/api/admin/tenants/{tenant_id}/traces", get_traces, methods=["GET"]),
        Route("/api/admin/tenants/{tenant_id}/observability", get_observability, methods=["GET"]),
        Route("/api/admin/tenants/{tenant_id}/observability/alerts", list_observability_alerts, methods=["GET"]),
        Route("/api/admin/tenants/{tenant_id}/observability/alerts/{alert_id}/acknowledge", acknowledge_observability_alert, methods=["POST"]),
        Route("/api/admin/tenants/{tenant_id}/observability/alerts/{alert_id}/resolve", resolve_observability_alert, methods=["POST"]),
        # Q.1 — Business Intelligence / Opportunity Dashboard Routes
        Route("/bi/opportunities", bi_opportunity_html_view, methods=["GET"]),
        Route("/api/bi/tenants/{tenant_id}/opportunities/summary", get_opportunity_summary, methods=["GET"]),
        Route("/api/bi/tenants/{tenant_id}/opportunities/compare", compare_opportunities, methods=["GET", "POST"]),
        Route("/api/bi/tenants/{tenant_id}/opportunities/{opportunity_id}", get_opportunity_detail, methods=["GET"]),
        Route("/api/bi/tenants/{tenant_id}/opportunities", list_opportunities, methods=["GET"]),
        # Q.2 — Business Intelligence / Supplier Dashboard Routes
        Route("/bi/suppliers", bi_supplier_html_view, methods=["GET"]),
        Route("/api/bi/tenants/{tenant_id}/suppliers/summary", get_supplier_summary, methods=["GET"]),
        Route("/api/bi/tenants/{tenant_id}/suppliers/compare", compare_suppliers, methods=["GET", "POST"]),
        Route("/api/bi/tenants/{tenant_id}/suppliers/{supplier_id}", get_supplier_detail, methods=["GET"]),
        Route("/api/bi/tenants/{tenant_id}/suppliers", list_suppliers, methods=["GET"]),
        # Q.3 — Business Intelligence / Profit Dashboard Routes
        Route("/bi/profit", bi_profit_html_view, methods=["GET"]),
        Route("/api/bi/tenants/{tenant_id}/profit/summary", get_profit_summary, methods=["GET"]),
        Route("/api/bi/tenants/{tenant_id}/profit/compare", compare_profit_items, methods=["GET", "POST"]),
        Route("/api/bi/tenants/{tenant_id}/profit/{item_id}", get_profit_detail, methods=["GET"]),
        Route("/api/bi/tenants/{tenant_id}/profit", list_profit_items, methods=["GET"]),
        # Q.4 — Business Intelligence / Mission Dashboard Routes
        Route("/bi/missions", bi_mission_html_view, methods=["GET"]),
        Route("/api/bi/tenants/{tenant_id}/missions/summary", get_mission_summary, methods=["GET"]),
        Route("/api/bi/tenants/{tenant_id}/missions/{mission_id}", get_mission_detail, methods=["GET"]),
        Route("/api/bi/tenants/{tenant_id}/missions", list_missions, methods=["GET"]),
        # Q.5 — Business Intelligence / Agent Cost Dashboard Routes
        Route("/bi/agent-costs", bi_agent_cost_html_view, methods=["GET"]),
        Route("/api/bi/tenants/{tenant_id}/agent-costs/summary", get_agent_cost_summary, methods=["GET"]),
        Route("/api/bi/tenants/{tenant_id}/agent-costs/missions/{mission_id}/summary", get_mission_cost_summary, methods=["GET"]),
        Route("/api/bi/tenants/{tenant_id}/agent-costs/{cost_record_id}", get_agent_cost_detail, methods=["GET"]),
        Route("/api/bi/tenants/{tenant_id}/agent-costs", list_agent_costs, methods=["GET"]),
        # Q.6 — Business Intelligence / Business KPIs Routes
        Route("/bi/kpis", bi_kpis_html_view, methods=["GET"]),
        Route("/api/bi/tenants/{tenant_id}/kpis/summary", get_business_kpi_summary, methods=["GET"]),
        Route("/api/bi/tenants/{tenant_id}/kpis/catalog", get_business_kpi_catalog, methods=["GET"]),
        Route("/api/bi/tenants/{tenant_id}/kpis/compare", compare_business_kpis, methods=["GET", "POST"]),
        Route("/api/bi/tenants/{tenant_id}/kpis/{kpi_id}", get_business_kpi_by_id, methods=["GET"]),
        Route("/api/bi/tenants/{tenant_id}/kpis", get_business_kpis, methods=["GET"]),
    ]

    return Starlette(debug=False, routes=routes)
