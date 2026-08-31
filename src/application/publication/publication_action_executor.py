import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, Any, Optional, List, Sequence, Tuple, Mapping
from types import MappingProxyType

from src.domain.mission.models import (
    LoopDecision,
    LoopState,
    LoopAction,
)
from src.domain.mission.ports import ActionExecutor
from src.domain.publication.models import (
    ListingDraft,
    PublicationRequest,
    PublicationResult,
    PublicationStatus,
    PublicationError,
    PublicationErrorCategory,
    SalesChannel,
)
from src.domain.publication.ports import (
    PublicationPort,
    PublicationRepository,
)


class PublicationActionExecutor(ActionExecutor):
    """
    Ejecutor de acciones para la publicación comercial en AutonomousLoop (Hito E-01.2).
    Integración limpia y desacoplada mediante inversión de dependencias:
    Decision -> Action -> PublicationActionExecutor -> PublicationPort -> Future Adapter

    Preserva estrictamente:
    - Inversión de dependencias: sólo conoce PublicationPort y contratos de dominio (sin HTTP, sin SDKs, sin MercadoLibre DTOs).
    - Estado de incertidumbre UNKNOWN: no degrada automáticamente a FAILED.
    - Idempotencia: respeta idempotency_key existente o la vincula al draft_id/correlation_id.
    - Correlación: preserva correlation_id a lo largo de todo el flujo.
    - Auditoría y Procedencia: registra y actualiza repositorios/trazas si están disponibles.
    """

    def __init__(
        self,
        publication_port: PublicationPort,
        repository: Optional[PublicationRepository] = None,
        default_channel: Optional[SalesChannel] = None,
    ):
        if publication_port is None:
            raise ValueError("publication_port cannot be None")
        self.publication_port = publication_port
        self.repository = repository
        self.default_channel = default_channel

        # Cache interno y auditoría in-memory por ciclo
        self._published_results: Dict[str, PublicationResult] = {}
        self._latest_result: Optional[PublicationResult] = None
        self._external_calls_count: int = 0

    @property
    def external_calls_count(self) -> int:
        return self._external_calls_count

    @property
    def latest_result(self) -> Optional[PublicationResult]:
        return self._latest_result

    def execute(self, decision: LoopDecision, state: LoopState) -> Dict[str, Any]:
        """
        Ejecuta una decisión del AutonomousLoop para acciones de publicación.
        Soporta los siguientes tipos de acción (vía parameter action_type o LoopAction):
        - PUBLISH / PUBLISH_LISTING: Realiza la publicación a través de PublicationPort.
        - VERIFY_STATUS / GET_STATUS: Consulta el estado de una publicación previa (ideal para resolver UNKNOWN).
        """
        action_name = decision.parameters.get("action_type") or str(decision.action.value)
        params = dict(decision.parameters)

        if action_name in ("PUBLISH", "PUBLISH_LISTING", "CONTINUE", "PROMOTE"):
            return self._execute_publish(decision, state, params)
        elif action_name in ("VERIFY_STATUS", "GET_STATUS", "CHECK_STATUS"):
            return self._execute_verify_status(decision, state, params)
        else:
            # Fallback a publicación si viene el draft en parameters
            if "draft" in params:
                return self._execute_publish(decision, state, params)
            return {
                "action_executed": action_name,
                "status": "UNSUPPORTED_ACTION",
                "error": f"Unsupported publication action: {action_name}",
            }

    def _execute_publish(
        self,
        decision: LoopDecision,
        state: LoopState,
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        draft: Optional[ListingDraft] = params.get("draft")
        if draft is None:
            return {
                "action_executed": "PUBLISH",
                "status": PublicationStatus.FAILED.value,
                "error": "Missing required 'draft' (ListingDraft) in decision parameters",
                "is_unknown": False,
                "is_success": False,
            }

        channel: SalesChannel = params.get("channel") or draft.channel or self.default_channel
        if channel is None:
            return {
                "action_executed": "PUBLISH",
                "status": PublicationStatus.FAILED.value,
                "error": "Missing required 'channel' (SalesChannel)",
                "is_unknown": False,
                "is_success": False,
            }

        # Preservación de idempotency_key y correlation_id
        idempotency_key = params.get("idempotency_key") or f"idemp_{draft.draft_id}"
        correlation_id = params.get("correlation_id") or state.mission_id or str(uuid.uuid4())
        request_id = params.get("request_id") or f"req_{uuid.uuid4().hex[:12]}"

        request = PublicationRequest(
            request_id=request_id,
            draft=draft,
            channel=channel,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            metadata=MappingProxyType({
                "mission_id": state.mission_id,
                "iteration": state.iteration,
                "decision_reason": decision.reason,
            }),
        )

        # Si hay repositorio, persistir draft antes del intento
        if self.repository:
            try:
                self.repository.save_draft(draft)
            except Exception:
                pass

        self._external_calls_count += 1
        result = self.publication_port.publish(request)
        self._latest_result = result
        self._published_results[draft.draft_id] = result

        # Si hay repositorio, persistir resultado
        if self.repository:
            try:
                self.repository.save_result(result)
            except Exception:
                pass

        return {
            "action_executed": "PUBLISH",
            "draft_id": draft.draft_id,
            "channel_id": channel.channel_id,
            "status": result.status.value,
            "is_success": result.is_success,
            "is_unknown": result.is_unknown,
            "is_failed": result.is_failed,
            "publication_id": result.publication_id,
            "external_reference": result.external_reference,
            "permalink": result.permalink,
            "confidence": result.confidence.value if result.confidence else None,
            "correlation_id": correlation_id,
            "idempotency_key": idempotency_key,
            "errors": [
                {
                    "category": err.category.value,
                    "message": err.message,
                    "code": err.code,
                    "retryable": err.retryable,
                    "details": dict(err.details),
                }
                for err in result.errors
            ],
            "metadata": dict(result.metadata),
        }

    def _execute_verify_status(
        self,
        decision: LoopDecision,
        state: LoopState,
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        channel: Optional[SalesChannel] = params.get("channel") or self.default_channel
        external_reference: Optional[str] = params.get("external_reference")

        if not channel or not external_reference:
            return {
                "action_executed": "VERIFY_STATUS",
                "status": PublicationStatus.FAILED.value,
                "error": "channel and external_reference are required to verify status",
                "is_unknown": False,
                "is_success": False,
            }

        self._external_calls_count += 1
        result = self.publication_port.get_status(channel, external_reference)
        self._latest_result = result

        if self.repository:
            try:
                self.repository.save_result(result)
            except Exception:
                pass

        return {
            "action_executed": "VERIFY_STATUS",
            "channel_id": channel.channel_id,
            "external_reference": external_reference,
            "status": result.status.value,
            "is_success": result.is_success,
            "is_unknown": result.is_unknown,
            "is_failed": result.is_failed,
            "publication_id": result.publication_id,
            "permalink": result.permalink,
            "confidence": result.confidence.value if result.confidence else None,
            "errors": [
                {
                    "category": err.category.value,
                    "message": err.message,
                    "code": err.code,
                    "retryable": err.retryable,
                }
                for err in result.errors
            ],
        }
