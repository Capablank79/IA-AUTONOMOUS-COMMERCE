"""
Servicio de Aplicación para Secret Management (Hito N.5 — Transversal N Security, Governance y Safety).

Responsabilidades:
- Implementar SecretResolverPort.
- Orquestar la resolución segura de secretos a través de proveedores (Env, Injected, OAuth Bridge).
- Validar ciclo de vida (activo, expirado, revocado) a través de SecretMetadataRepository.
- Proteger el material confidencial mediante SecretValue en memoria.
- Soportar rotación de secretos y versionado determinista sin alterar identidades N.1 ni reglas RBAC N.4.
- Auditar y trazar la resolución de secretos (K.1 Audit Trail / K.2 Agent Trace) garantizando cero fuga de material sensible.
- Retornar resultados explícitos (RESOLVED, NOT_FOUND, EXPIRED, REVOKED, ERROR, UNKNOWN).
"""

from datetime import datetime, timezone
import logging
import uuid
from typing import Optional, Sequence, Mapping, Any, Dict, List

from src.domain.secrets.models import (
    SecretReference,
    SecretValue,
    SecretMetadata,
    SecretResolutionResult,
    SecretType,
    SecretStatus,
    SecretResolutionStatus,
)
from src.domain.secrets.ports import (
    SecretProviderPort,
    SecretResolverPort,
    SecretMetadataRepositoryPort,
)
from src.domain.reliability.ports import ClockPort
from src.domain.audit.models import (
    AuditActor,
    AuditActorType,
    AuditRecord,
    AuditRecordType,
)
from src.domain.audit.ports import AuditRepositoryPort
from src.domain.agent_trace.models import StepType, TraceStatus
from src.application.agent_trace.agent_trace_service import AgentTraceService
from src.domain.security.models import sanitize_security_data, deep_freeze

logger = logging.getLogger(__name__)


class SecretService(SecretResolverPort):
    """
    Servicio de aplicación para la gestión y resolución de secretos (N.5).
    """

    def __init__(
        self,
        providers: Optional[Sequence[SecretProviderPort]] = None,
        metadata_repository: Optional[SecretMetadataRepositoryPort] = None,
        clock: Optional[ClockPort] = None,
        audit_repository: Optional[AuditRepositoryPort] = None,
        agent_trace_service: Optional[AgentTraceService] = None,
    ):
        self._providers: List[SecretProviderPort] = list(providers) if providers else []
        self._metadata_repo = metadata_repository
        self._clock = clock
        self._audit_repo = audit_repository
        self._trace_service = agent_trace_service

    def _now(self) -> datetime:
        if self._clock is not None:
            return self._clock.now()
        return datetime.now(timezone.utc)

    def add_provider(self, provider: SecretProviderPort) -> None:
        """Registra un nuevo proveedor de secretos."""
        self._providers.append(provider)

    def resolve(
        self,
        reference: SecretReference,
        correlation_id: Optional[str] = None,
    ) -> SecretResolutionResult:
        """
        Resuelve una SecretReference de forma segura.

        Pasos:
        1. Verificar metadatos en repositorio (si existe). Si está REVOKED o EXPIRED, rechazar.
        2. Iterar proveedores en orden de prioridad hasta encontrar el SecretValue.
        3. Si no se encuentra, retornar NOT_FOUND (sin valores por defecto ni cadenas vacías).
        4. Auditar la resolución (K.1/K.2) con metadatos sanitizados.
        """
        now = self._now()

        # 1. Chequeo de metadatos si hay repositorio
        meta: Optional[SecretMetadata] = None
        if self._metadata_repo is not None:
            try:
                meta = self._metadata_repo.get_metadata(reference.reference_id)
                if not meta:
                    meta = self._metadata_repo.find_by_name(reference.provider, reference.secret_name)
            except Exception as e:
                logger.warning(f"Error reading secret metadata for '{reference.reference_id}': {e}")

        if meta is not None:
            if meta.status == SecretStatus.REVOKED:
                res = SecretResolutionResult(
                    status=SecretResolutionStatus.REVOKED,
                    reference=reference,
                    secret_value=None,
                    metadata=meta,
                    error_message=f"Secret '{reference.reference_id}' is revoked.",
                    resolved_at=now,
                )
                self._record_audit_and_trace(res, correlation_id)
                return res

            if meta.is_expired(now):
                res = SecretResolutionResult(
                    status=SecretResolutionStatus.EXPIRED,
                    reference=reference,
                    secret_value=None,
                    metadata=meta,
                    error_message=f"Secret '{reference.reference_id}' expired at {meta.expires_at}.",
                    resolved_at=now,
                )
                self._record_audit_and_trace(res, correlation_id)
                return res

        # 2. Iterar proveedores
        secret_val: Optional[SecretValue] = None
        for provider in self._providers:
            try:
                if provider.can_handle(reference):
                    candidate = provider.get_secret(reference)
                    if candidate is not None:
                        secret_val = candidate
                        break
            except Exception as e:
                logger.warning(f"Provider '{provider.provider_name}' error resolving '{reference.reference_id}': {e}")

        # 3. Construir resultado
        if secret_val is None:
            res = SecretResolutionResult(
                status=SecretResolutionStatus.NOT_FOUND,
                reference=reference,
                secret_value=None,
                metadata=meta,
                error_message=f"Secret '{reference.secret_name}' not found for provider '{reference.provider}'.",
                resolved_at=now,
            )
        else:
            res = SecretResolutionResult(
                status=SecretResolutionStatus.RESOLVED,
                reference=reference,
                secret_value=secret_val,
                metadata=meta,
                error_message=None,
                resolved_at=now,
            )

        # 4. Auditar
        self._record_audit_and_trace(res, correlation_id)
        return res

    def register_metadata(
        self,
        reference_id: str,
        secret_name: str,
        provider: str,
        secret_type: SecretType,
        version: str = "1",
        status: SecretStatus = SecretStatus.ACTIVE,
        expires_at: Optional[datetime] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> SecretMetadata:
        """
        Registra metadatos de un secreto en el repositorio administrativo.
        """
        now = self._now()
        meta = SecretMetadata(
            reference_id=reference_id,
            secret_name=secret_name,
            provider=provider,
            secret_type=secret_type,
            version=version,
            status=status,
            created_at=now,
            updated_at=now,
            expires_at=expires_at,
            metadata=metadata or {},
        )
        if self._metadata_repo is not None:
            self._metadata_repo.save_metadata(meta)
        return meta

    def rotate_secret(
        self,
        reference_id: str,
        new_value: str,
        new_version: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> SecretMetadata:
        """
        Ejecuta la rotación de un secreto:
        1. Actualiza los metadatos incrementando versión o asignando nueva versión.
        2. Actualiza los proveedores en memoria que soporten rotación (InjectedSecretProvider).
        3. Audita el evento SECRET_ROTATED (K.1 / K.2).
        """
        now = self._now()
        existing_meta: Optional[SecretMetadata] = None
        if self._metadata_repo is not None:
            existing_meta = self._metadata_repo.get_metadata(reference_id)

        if existing_meta is None:
            # Si no hay metadatos previos, crear metadatos base
            v = new_version or "2"
            updated_meta = SecretMetadata(
                reference_id=reference_id,
                secret_name=reference_id,
                provider="injected",
                secret_type=SecretType.INTERNAL_CREDENTIAL,
                version=v,
                status=SecretStatus.ACTIVE,
                created_at=now,
                updated_at=now,
            )
        else:
            current_ver_num = 1
            try:
                current_ver_num = int(existing_meta.version)
            except ValueError:
                pass
            next_ver = new_version or str(current_ver_num + 1)
            updated_meta = SecretMetadata(
                reference_id=existing_meta.reference_id,
                secret_name=existing_meta.secret_name,
                provider=existing_meta.provider,
                secret_type=existing_meta.secret_type,
                version=next_ver,
                status=SecretStatus.ACTIVE,
                created_at=existing_meta.created_at,
                updated_at=now,
                expires_at=existing_meta.expires_at,
                metadata=existing_meta.metadata,
            )

        if self._metadata_repo is not None:
            self._metadata_repo.save_metadata(updated_meta)

        # Actualizar en proveedores que soporten set_secret
        for p in self._providers:
            if hasattr(p, "set_secret"):
                getattr(p, "set_secret")(reference_id, new_value)
                lookup_key = f"{updated_meta.provider.lower()}:{updated_meta.secret_name.lower()}"
                getattr(p, "set_secret")(lookup_key, new_value)

        # Auditar rotación
        self._record_rotation_audit_and_trace(updated_meta, correlation_id)
        return updated_meta

    def _record_audit_and_trace(
        self,
        result: SecretResolutionResult,
        correlation_id: Optional[str] = None,
    ) -> None:
        """Registra la resolución del secreto en K.1 Audit Trail y K.2 Agent Trace de forma sanitizada."""
        now = self._now()
        cid = correlation_id or f"corr-sec-{uuid.uuid4().hex[:8]}"

        audit_actor = AuditActor(
            actor_type=AuditActorType.SYSTEM,
            actor_id="secret_service",
            details={"provider": result.reference.provider},
        )

        if self._audit_repo is not None:
            try:
                record = AuditRecord(
                    audit_id=f"aud-sec-{uuid.uuid4().hex[:12]}",
                    record_type=AuditRecordType.SECRET_RESOLVED,
                    occurred_at=now,
                    actor=audit_actor,
                    subject_type="SECRET",
                    subject_id=result.reference.reference_id,
                    action_or_operation=f"RESOLVE_SECRET_{result.reference.secret_type.value}",
                    status=result.status.value,
                    correlation_id=cid,
                    provenance="SECRET_SERVICE_N5",
                    metadata=result.to_audit_payload(),
                )
                self._audit_repo.append(record)
            except Exception as e:
                logger.warning(f"Failed to record secret audit record: {e}")

        if self._trace_service is not None:
            try:
                self._trace_service.record_step(
                    component_name="SecretService",
                    execution_id=cid,
                    step_number=1,
                    step_type=StepType.TOOL_CALL,
                    operation="resolve_secret",
                    status=TraceStatus.SUCCESS if result.is_resolved else TraceStatus.FAILED,
                    started_at=now,
                    completed_at=now,
                    tool_or_service="SecretService",
                    correlation_id=cid,
                    provenance="SECRET_SERVICE_N5",
                    metadata=result.to_audit_payload(),
                )
            except Exception as e:
                logger.warning(f"Failed to record secret trace record: {e}")

    def _record_rotation_audit_and_trace(
        self,
        metadata: SecretMetadata,
        correlation_id: Optional[str] = None,
    ) -> None:
        """Registra la rotación del secreto en K.1 y K.2."""
        now = self._now()
        cid = correlation_id or f"corr-rot-{uuid.uuid4().hex[:8]}"

        audit_actor = AuditActor(
            actor_type=AuditActorType.SYSTEM,
            actor_id="secret_service",
            details={"provider": metadata.provider},
        )

        if self._audit_repo is not None:
            try:
                record = AuditRecord(
                    audit_id=f"aud-rot-{uuid.uuid4().hex[:12]}",
                    record_type=AuditRecordType.SECRET_ROTATED,
                    occurred_at=now,
                    actor=audit_actor,
                    subject_type="SECRET",
                    subject_id=metadata.reference_id,
                    action_or_operation="ROTATE_SECRET",
                    status="SUCCESS",
                    correlation_id=cid,
                    provenance="SECRET_SERVICE_N5",
                    metadata=metadata.to_dict(),
                )
                self._audit_repo.append(record)
            except Exception as e:
                logger.warning(f"Failed to record secret rotation audit: {e}")

        if self._trace_service is not None:
            try:
                self._trace_service.record_step(
                    component_name="SecretService",
                    execution_id=cid,
                    step_number=1,
                    step_type=StepType.TOOL_CALL,
                    operation="rotate_secret",
                    status=TraceStatus.SUCCESS,
                    started_at=now,
                    completed_at=now,
                    tool_or_service="SecretService",
                    correlation_id=cid,
                    provenance="SECRET_SERVICE_N5",
                    metadata=metadata.to_dict(),
                )
            except Exception as e:
                logger.warning(f"Failed to record secret rotation trace: {e}")
