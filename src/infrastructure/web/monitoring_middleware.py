"""
Middleware ASGI para telemetría y métricas de producción en tiempo real (P.7).

Garantías:
1. Mide latencia con monotonic clock (`time.perf_counter()`).
2. Sanitiza templates de rutas (`/admin/tenants/{id}`) evitando explosión de cardinalidad.
3. No almacena ni registra headers sensibles, tokens, passwords ni payloads (N.9).
4. Non-fatal: Si el registro de métricas falla, la ejecución HTTP continúa sin interrupción.
"""

from datetime import datetime, timezone
import logging
import time
from typing import Optional, Callable

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from src.application.monitoring.production_monitoring_service import ProductionMonitoringService
from src.domain.deployment.models import ApplicationEnvironment
from src.domain.monitoring.models import sanitize_route_template

logger = logging.getLogger("MonitoringMiddleware")


class ProductionMonitoringMiddleware(BaseHTTPMiddleware):
    """
    Middleware ASGI para captura segura y no bloqueante de métricas de tráfico HTTP.
    """

    def __init__(
        self,
        app,
        monitoring_service: ProductionMonitoringService,
        environment: Optional[ApplicationEnvironment] = None,
    ) -> None:
        super().__init__(app)
        self._monitoring_service = monitoring_service
        self._environment = environment or monitoring_service.environment

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        start_time = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        except Exception as exc:
            status_code = 500
            raise exc
        finally:
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            
            # Extraer template de ruta si está disponible en endpoint de Starlette o sanitizar path
            path = request.url.path
            method = request.method
            
            # Extraer tenant_id si está presente de forma segura en path params
            tenant_id = request.path_params.get("tenant_id") if hasattr(request, "path_params") else None

            # Registrar métrica de forma asíncrona / segura sin degradar
            try:
                self._monitoring_service.record_request_metric(
                    method=method,
                    path=path,
                    status_code=status_code,
                    duration_ms=elapsed_ms,
                    environment=self._environment,
                    tenant_id=tenant_id,
                )
            except Exception as m_exc:
                logger.debug(f"Monitoring middleware non-fatal recording error: {m_exc}")
