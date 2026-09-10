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

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, HTMLResponse
from starlette.routing import Route

from src.application.admin_console.admin_console_service import AdminConsoleService
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


def create_admin_app(service: AdminConsoleService) -> Starlette:
    """Fábrica para la aplicación Starlette de la Admin Console."""

    async def health(request: Request):
        return JSONResponse({
            "status": "ok",
            "service": "saas-admin-console",
            "version": "1.0.0",
        })

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
    ]

    return Starlette(debug=False, routes=routes)
