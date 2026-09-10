"""
Integración y Helpers entre Model Gateway (Hito O.5) y Usage Metering (Hito O.6).

Responsabilidades:
- Transformar hechos estructurados emitidos por ModelGatewayResponse y ModelGatewayRequest en UsageEvents inmutables y conformes con O.6.
- NO recalcular inferencia ni volver a llamar a proveedores.
- NO inspeccionar raw prompts ni completions para medir consumo.
- Mapear correctamente:
  - Cache hits: request_status=CACHED / SUCCESS, cache_status=HIT, token count real de cache o 0 sin falsos costes de proveedor.
  - Invocaciones exitosas: request_status=SUCCESS, tokens y costes reales o estimados.
  - Fallos y bloqueos: request_status=FAILED / BLOCKED sin tokens falsos.
  - Idempotencia y trazabilidad mediante correlation_id y source_reference canónico.
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, Any, Dict, Mapping
import uuid

from src.domain.tenant.models import TenantContext
from src.domain.caching.models import CacheLookupStatus
from src.domain.model_gateway.models import (
    ModelGatewayRequest,
    ModelGatewayResponse,
    ModelGatewayStatus,
)
from src.domain.usage_metering.models import (
    UsageEvent,
    UsageRequestStatus,
)
from src.domain.usage_metering.ports import UsageMeteringServicePort


class ModelGatewayUsageBridge:
    """
    Bridge para registrar facts de consumo desde el Model Gateway (O.5) hacia Usage Metering (O.6).
    """

    @staticmethod
    def map_response_to_usage_event(
        request: ModelGatewayRequest,
        response: ModelGatewayResponse,
        occurred_at: Optional[datetime] = None,
        event_id: Optional[str] = None,
    ) -> UsageEvent:
        """
        Crea un UsageEvent inmutable a partir de una solicitud y respuesta del Model Gateway.
        """
        ts = occurred_at or datetime.now(timezone.utc)
        evt_id = event_id or f"use_{uuid.uuid4().hex[:16]}"

        # Mapeo de request status
        if response.status == ModelGatewayStatus.SUCCESS:
            req_status = UsageRequestStatus.SUCCESS
        elif response.status == ModelGatewayStatus.CACHED or response.cache_status == CacheLookupStatus.HIT:
            req_status = UsageRequestStatus.CACHED
        elif response.status in (
            ModelGatewayStatus.UNAUTHORIZED,
            ModelGatewayStatus.FORBIDDEN_MODEL,
            ModelGatewayStatus.SENSITIVE_DATA_BLOCKED,
            ModelGatewayStatus.TOOL_POLICY_BLOCKED,
            ModelGatewayStatus.QUOTA_EXCEEDED,
            ModelGatewayStatus.RATE_LIMITED,
        ):
            req_status = UsageRequestStatus.BLOCKED
        else:
            req_status = UsageRequestStatus.FAILED

        provider_name = None
        model_name = None
        if response.provider_reference is not None:
            provider_name = response.provider_reference.provider_name
            model_name = response.provider_reference.model_id
        elif response.route_used is not None:
            provider_name = response.route_used.provider
            model_name = response.route_used.model_id
        elif request.preferred_model_id:
            model_name = request.preferred_model_id

        task_type_str = None
        if request.task_type is not None:
            task_type_str = request.task_type.value if hasattr(request.task_type, "value") else str(request.task_type)

        # Determinar tokens reales o UNKNOWN
        in_tokens: Optional[int] = None
        out_tokens: Optional[int] = None
        tot_tokens: Optional[int] = None

        if req_status in (UsageRequestStatus.SUCCESS, UsageRequestStatus.CACHED):
            if response.total_tokens > 0 or (response.input_tokens > 0 or response.output_tokens > 0):
                in_tokens = response.input_tokens
                out_tokens = response.output_tokens
                tot_tokens = response.total_tokens or (response.input_tokens + response.output_tokens)
            elif response.cache_status == CacheLookupStatus.HIT:
                in_tokens = response.input_tokens
                out_tokens = response.output_tokens
                tot_tokens = response.total_tokens

        # Costes
        est_cost: Optional[Decimal] = response.estimated_cost
        act_cost: Optional[Decimal] = response.actual_cost

        # Source reference canónico para idempotencia
        src_ref = f"gw_res:{response.correlation_id}" if response.correlation_id else f"gw_req:{request.correlation_id}"

        return UsageEvent(
            usage_event_id=evt_id,
            tenant_id=request.tenant_id,
            occurred_at=ts,
            request_status=req_status,
            identity_id=request.identity_id,
            organization_id=request.organization_id,
            session_id=request.session_id,
            provider=provider_name,
            model=model_name,
            task_type=task_type_str,
            cache_status=response.cache_status,
            input_tokens=in_tokens,
            output_tokens=out_tokens,
            total_tokens=tot_tokens,
            estimated_cost=est_cost,
            actual_cost=act_cost,
            correlation_id=response.correlation_id,
            source_reference=src_ref,
            details={
                "gateway_status": response.status.value,
                "reason_code": response.reason_code,
                "error_type": response.error_type.value if response.error_type else None,
            },
        )

    @classmethod
    def record_gateway_usage(
        cls,
        usage_service: UsageMeteringServicePort,
        context: TenantContext,
        request: ModelGatewayRequest,
        response: ModelGatewayResponse,
        occurred_at: Optional[datetime] = None,
        event_id: Optional[str] = None,
    ) -> UsageEvent:
        """
        Helper de un solo paso para mapear y registrar un evento de uso en el servicio O.6.
        """
        event = cls.map_response_to_usage_event(
            request=request,
            response=response,
            occurred_at=occurred_at,
            event_id=event_id,
        )
        return usage_service.record_usage_event(context=context, event=event)
