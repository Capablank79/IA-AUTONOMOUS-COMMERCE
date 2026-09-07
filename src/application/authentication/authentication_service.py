"""
Servicio de Aplicación para Autenticación (Hito N.2 - Transversal N Security, Governance y Safety).

Responsabilidades:
- Recibir y evaluar solicitudes de autenticación (AuthenticationRequest) y contextos de conexión (OAuthConnection).
- Validar método, proveedor, presencia, expiración y binding del sujeto.
- Resolver a una Identidad Canónica N.1 estable (IdentityService) para actores autenticados.
- Retornar un AuthenticationResult explícito, determinista e inmutable.
- Exponer un PrincipalContext seguro downstream sin secretos.
- Auditar y trazar resultados de autenticación (K.1/K.2) de forma sanitizada y segura (cero tokens/secretos).

Fronteras estrictas:
- Responde exclusivamente a: "¿Puede este actor demostrar de forma válida que es la identidad que declara?".
- N.1: quién es. N.2: cómo demuestra quién es. N.3: qué puede hacer.
- NO evalúa permisos ni reglas RBAC.
- NO llama a PolicyEngine ni ejecuta autorización (N.3).
- NO realiza gestión de ciclo de vida de secretos (N.5).
- Nunca expone ni almacena access_token, refresh_token, contraseñas o API keys.
"""

from datetime import datetime, timezone
import logging
import uuid
from typing import Optional, Sequence, Mapping, Any, Union, Dict, Set

from src.domain.authentication.models import (
    AuthenticationMethod,
    AuthenticationStatus,
    AuthenticationRequest,
    AuthenticationResult,
    PrincipalContext,
)
from src.domain.identity.models import (
    IdentityReference,
    IdentityType,
    IdentityStatus,
)
from src.application.identity.identity_service import IdentityService
from src.domain.oauth.models import OAuthConnection
from src.domain.reliability.ports import ClockPort
from src.domain.security.models import (
    validate_safe_identifier,
    sanitize_security_data,
    deep_freeze,
)
from src.domain.audit.models import (
    AuditActor,
    AuditActorType,
    AuditRecord,
    AuditRecordType,
)
from src.domain.audit.ports import AuditRepositoryPort
from src.domain.agent_trace.models import StepType, TraceStatus
from src.application.agent_trace.agent_trace_service import AgentTraceService

logger = logging.getLogger(__name__)


class AuthenticationService:
    """
    Servicio de aplicación para la autenticación de actores y tokens (Hito N.2).
    """

    def __init__(
        self,
        identity_service: IdentityService,
        clock: Optional[ClockPort] = None,
        audit_repository: Optional[AuditRepositoryPort] = None,
        agent_trace_service: Optional[AgentTraceService] = None,
        trusted_internal_tokens: Optional[Mapping[str, str]] = None,
        trusted_api_credentials: Optional[Mapping[str, str]] = None,
    ):
        """
        Inicializa el AuthenticationService.

        :param identity_service: Servicio de Identidad Canónica N.1 para resolución de sujetos.
        :param clock: Abstracción de reloj determinista (ClockPort). Si no se provee, usa UTC del sistema.
        :param audit_repository: Repositorio de Audit Trail K.1 (opcional).
        :param agent_trace_service: Servicio de Agent Trace K.2 (opcional).
        :param trusted_internal_tokens: Mapeo de tokens/secretos reconocidos a identificadores de servicio interno.
        :param trusted_api_credentials: Mapeo de credenciales reconocidas a identidades declaradas.
        """
        self.identity_service = identity_service
        self.clock = clock
        self.audit_repository = audit_repository
        self.agent_trace_service = agent_trace_service
        self._trusted_internal_tokens = dict(trusted_internal_tokens) if trusted_internal_tokens else {}
        self._trusted_api_credentials = dict(trusted_api_credentials) if trusted_api_credentials else {}

    def _now(self) -> datetime:
        if self.clock is not None:
            return self.clock.now()
        return datetime.now(timezone.utc)

    def authenticate_oauth_connection(
        self,
        connection: OAuthConnection,
        correlation_id: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> AuthenticationResult:
        """
        Autentica una conexión OAuth existente (Hito E / MercadoLibre).
        Valida que el token no esté expirado y que el proveedor y user_id sean válidos.
        Resuelve la identidad canónica N.1 del usuario sin persistir tokens.
        """
        now = self._now()
        provider = connection.provider.strip().lower() if connection.provider else "unknown"

        # Validar presencia de token
        if not connection.access_token or not connection.access_token.strip():
            result = AuthenticationResult(
                status=AuthenticationStatus.INVALID,
                method=AuthenticationMethod.OAUTH2,
                provider=provider,
                principal=None,
                authenticated_at=None,
                expires_at=connection.expires_at,
                reason_codes=("MISSING_ACCESS_TOKEN",),
                correlation_id=correlation_id,
                metadata=metadata or {},
            )
            self._record_audit_and_trace(result, declared_actor=connection.user_id)
            return result

        # Validar expiración
        if connection.expires_at <= now:
            result = AuthenticationResult(
                status=AuthenticationStatus.EXPIRED,
                method=AuthenticationMethod.OAUTH2,
                provider=provider,
                principal=None,
                authenticated_at=None,
                expires_at=connection.expires_at,
                reason_codes=("TOKEN_EXPIRED",),
                correlation_id=correlation_id,
                metadata=metadata or {},
            )
            self._record_audit_and_trace(result, declared_actor=connection.user_id)
            return result

        # Validar subject
        if not connection.user_id or not str(connection.user_id).strip():
            result = AuthenticationResult(
                status=AuthenticationStatus.INVALID,
                method=AuthenticationMethod.OAUTH2,
                provider=provider,
                principal=None,
                authenticated_at=None,
                expires_at=connection.expires_at,
                reason_codes=("INVALID_USER_ID",),
                correlation_id=correlation_id,
                metadata=metadata or {},
            )
            self._record_audit_and_trace(result, declared_actor=None)
            return result

        # Resolver o registrar identidad N.1 de forma idempotente
        try:
            identity = self.identity_service.register_oauth_user(
                provider=provider,
                user_id=str(connection.user_id).strip(),
                display_name=f"{provider.capitalize()} User {connection.user_id}",
            )
            principal_ref = identity.to_reference()
        except Exception as e:
            logger.error(f"Error resolving N.1 identity for OAuth user: {e}")
            result = AuthenticationResult(
                status=AuthenticationStatus.ERROR,
                method=AuthenticationMethod.OAUTH2,
                provider=provider,
                principal=None,
                authenticated_at=None,
                expires_at=connection.expires_at,
                reason_codes=("IDENTITY_RESOLUTION_ERROR", str(e)),
                correlation_id=correlation_id,
                metadata=metadata or {},
            )
            self._record_audit_and_trace(result, declared_actor=connection.user_id)
            return result

        # Retornar resultado AUTHENTICATED
        result = AuthenticationResult(
            status=AuthenticationStatus.AUTHENTICATED,
            method=AuthenticationMethod.OAUTH2,
            provider=provider,
            principal=principal_ref,
            authenticated_at=now,
            expires_at=connection.expires_at,
            reason_codes=("AUTHENTICATION_SUCCESS",),
            correlation_id=correlation_id,
            metadata=metadata or {},
        )
        self._record_audit_and_trace(result, declared_actor=connection.user_id)
        return result

    def authenticate_request(
        self,
        request: AuthenticationRequest,
    ) -> AuthenticationResult:
        """
        Evalúa y autentica una solicitud genérica (AuthenticationRequest).
        Soporta OAUTH2, BEARER_TOKEN, API_CREDENTIAL, INTERNAL_SERVICE, SYSTEM_ASSERTION, UNKNOWN.
        """
        now = self._now()
        provider = request.provider
        method = request.method
        correlation_id = request.correlation_id
        meta = dict(request.metadata)

        # 1. UNKNOWN Method -> UNKNOWN Status (no fallback permisivo)
        if method == AuthenticationMethod.UNKNOWN:
            result = AuthenticationResult(
                status=AuthenticationStatus.UNKNOWN,
                method=AuthenticationMethod.UNKNOWN,
                provider=provider,
                principal=None,
                authenticated_at=None,
                expires_at=None,
                reason_codes=("UNKNOWN_AUTHENTICATION_METHOD",),
                correlation_id=correlation_id,
                metadata=meta,
            )
            self._record_audit_and_trace(result, declared_actor=request.declared_subject)
            return result

        # 2. INTERNAL_SERVICE / SYSTEM_ASSERTION
        if method in (AuthenticationMethod.INTERNAL_SERVICE, AuthenticationMethod.SYSTEM_ASSERTION):
            # Requiere contrato reconocido explícito (no asume interno = autenticado)
            token = request.token_or_secret
            if not token or not token.strip():
                result = AuthenticationResult(
                    status=AuthenticationStatus.UNAUTHENTICATED,
                    method=method,
                    provider=provider,
                    principal=None,
                    authenticated_at=None,
                    expires_at=None,
                    reason_codes=("MISSING_INTERNAL_CREDENTIAL",),
                    correlation_id=correlation_id,
                    metadata=meta,
                )
                self._record_audit_and_trace(result, declared_actor=request.declared_subject)
                return result

            # Verificar contra tokens internos de confianza
            service_id = self._trusted_internal_tokens.get(token.strip())
            if not service_id:
                result = AuthenticationResult(
                    status=AuthenticationStatus.INVALID,
                    method=method,
                    provider=provider,
                    principal=None,
                    authenticated_at=None,
                    expires_at=None,
                    reason_codes=("INVALID_INTERNAL_CREDENTIAL",),
                    correlation_id=correlation_id,
                    metadata=meta,
                )
                self._record_audit_and_trace(result, declared_actor=request.declared_subject)
                return result

            # Validar binding si declaró un subject específico
            if request.declared_subject and request.declared_subject.strip() != service_id:
                result = AuthenticationResult(
                    status=AuthenticationStatus.INVALID,
                    method=method,
                    provider=provider,
                    principal=None,
                    authenticated_at=None,
                    expires_at=None,
                    reason_codes=("DECLARED_SUBJECT_MISMATCH",),
                    correlation_id=correlation_id,
                    metadata=meta,
                )
                self._record_audit_and_trace(result, declared_actor=request.declared_subject)
                return result

            # Resolver identidad N.1 para agente/sistema interno
            try:
                identity = self.identity_service.register_system_agent(
                    agent_name=service_id,
                    display_name=f"Internal Service {service_id}",
                )
                principal_ref = identity.to_reference()
            except Exception as e:
                logger.error(f"Error registering system agent for internal auth: {e}")
                result = AuthenticationResult(
                    status=AuthenticationStatus.ERROR,
                    method=method,
                    provider=provider,
                    principal=None,
                    authenticated_at=None,
                    expires_at=None,
                    reason_codes=("INTERNAL_IDENTITY_RESOLUTION_ERROR", str(e)),
                    correlation_id=correlation_id,
                    metadata=meta,
                )
                self._record_audit_and_trace(result, declared_actor=request.declared_subject)
                return result

            result = AuthenticationResult(
                status=AuthenticationStatus.AUTHENTICATED,
                method=method,
                provider=provider,
                principal=principal_ref,
                authenticated_at=now,
                expires_at=None,
                reason_codes=("AUTHENTICATION_SUCCESS",),
                correlation_id=correlation_id,
                metadata=meta,
            )
            self._record_audit_and_trace(result, declared_actor=request.declared_subject)
            return result

        # 3. API_CREDENTIAL / BEARER_TOKEN
        if method in (AuthenticationMethod.API_CREDENTIAL, AuthenticationMethod.BEARER_TOKEN):
            secret = request.token_or_secret
            if not secret or not secret.strip():
                result = AuthenticationResult(
                    status=AuthenticationStatus.UNAUTHENTICATED,
                    method=method,
                    provider=provider,
                    principal=None,
                    authenticated_at=None,
                    expires_at=None,
                    reason_codes=("MISSING_CREDENTIALS",),
                    correlation_id=correlation_id,
                    metadata=meta,
                )
                self._record_audit_and_trace(result, declared_actor=request.declared_subject)
                return result

            declared_actor = self._trusted_api_credentials.get(secret.strip())
            if not declared_actor:
                result = AuthenticationResult(
                    status=AuthenticationStatus.INVALID,
                    method=method,
                    provider=provider,
                    principal=None,
                    authenticated_at=None,
                    expires_at=None,
                    reason_codes=("INVALID_CREDENTIALS",),
                    correlation_id=correlation_id,
                    metadata=meta,
                )
                self._record_audit_and_trace(result, declared_actor=request.declared_subject)
                return result

            if request.declared_subject and request.declared_subject.strip() != declared_actor:
                result = AuthenticationResult(
                    status=AuthenticationStatus.INVALID,
                    method=method,
                    provider=provider,
                    principal=None,
                    authenticated_at=None,
                    expires_at=None,
                    reason_codes=("SUBJECT_MISMATCH",),
                    correlation_id=correlation_id,
                    metadata=meta,
                )
                self._record_audit_and_trace(result, declared_actor=request.declared_subject)
                return result

            # Resolver identidad N.1
            resolved_ref = self.identity_service.resolve_identity(declared_actor)
            if resolved_ref.is_unknown:
                # Si no está en el repo como entidad completa, registrar como servicio o usuario
                try:
                    identity = self.identity_service.register_identity(
                        identity_id=f"cred_{declared_actor}",
                        identity_type=IdentityType.SERVICE,
                        provider=provider,
                        external_subject_id=declared_actor,
                        display_name=f"Credential Actor {declared_actor}",
                    )
                    resolved_ref = identity.to_reference()
                except Exception:
                    pass

            result = AuthenticationResult(
                status=AuthenticationStatus.AUTHENTICATED,
                method=method,
                provider=provider,
                principal=resolved_ref,
                authenticated_at=now,
                expires_at=None,
                reason_codes=("AUTHENTICATION_SUCCESS",),
                correlation_id=correlation_id,
                metadata=meta,
            )
            self._record_audit_and_trace(result, declared_actor=request.declared_subject)
            return result

        # 4. Fallback si llega método no manejado
        result = AuthenticationResult(
            status=AuthenticationStatus.INVALID,
            method=method,
            provider=provider,
            principal=None,
            authenticated_at=None,
            expires_at=None,
            reason_codes=("UNSUPPORTED_METHOD_OR_FLOW",),
            correlation_id=correlation_id,
            metadata=meta,
        )
        self._record_audit_and_trace(result, declared_actor=request.declared_subject)
        return result

    def create_principal_context(self, auth_result: AuthenticationResult) -> PrincipalContext:
        """
        Crea un PrincipalContext downstream seguro a partir de un AuthenticationResult.
        Si el resultado no fue autenticado con éxito, lanza ValueError.
        """
        if not auth_result.is_authenticated or auth_result.principal is None:
            raise ValueError(
                f"Cannot create PrincipalContext from unauthenticated result (status={auth_result.status.value})."
            )
        return PrincipalContext(
            principal=auth_result.principal,
            auth_result=auth_result,
        )

    def _record_audit_and_trace(
        self,
        result: AuthenticationResult,
        declared_actor: Optional[str] = None,
    ) -> None:
        """
        Registra de forma segura en K.1 Audit Trail y K.2 Agent Trace sin persistir secretos.
        No duplica registros en replay (idempotency_key determinista de K.1).
        """
        now = self._now()
        actor_id = (
            result.principal.identity_id
            if result.principal
            else (str(declared_actor).strip() or "unauthenticated_actor")
        )
        if result.principal and result.principal.identity_type == IdentityType.USER:
            actor_type = AuditActorType.USER
        elif result.principal and result.principal.identity_type == IdentityType.AGENT:
            actor_type = AuditActorType.AGENT
        else:
            actor_type = AuditActorType.SYSTEM

        audit_actor = AuditActor(
            actor_type=actor_type,
            actor_id=actor_id,
            details={"provider": result.provider} if result.provider else None,
        )

        # 1. K.1 Audit Trail (append-only, idempotente)
        if self.audit_repository is not None:
            try:
                audit_meta = {
                    "method": result.method.value,
                    "provider": result.provider,
                    "authentication_status": result.status.value,
                    "is_authenticated": result.is_authenticated,
                    "reason_codes": list(result.reason_codes),
                    "auth_result_checksum": result.checksum,
                }
                sanitized_audit_meta = sanitize_security_data(audit_meta)

                record = AuditRecord(
                    audit_id=f"aud-auth-{uuid.uuid4().hex[:12]}",
                    record_type=AuditRecordType.AUTHENTICATION_EVALUATED,
                    occurred_at=now,
                    actor=audit_actor,
                    subject_type="AUTHENTICATION",
                    subject_id=result.correlation_id or result.provider or "auth",
                    action_or_operation=f"AUTHENTICATE_{result.method.value}",
                    status=result.status.value,
                    correlation_id=result.correlation_id or f"auth-{uuid.uuid4().hex[:12]}",
                    provenance="AUTHENTICATION_SERVICE_N2",
                    metadata=sanitized_audit_meta,
                )
                self.audit_repository.append(record)
            except Exception as e:
                logger.warning(f"Failed to record authentication audit record: {e}")

        # 2. K.2 Agent Trace
        if self.agent_trace_service is not None:
            try:
                trace_meta = {
                    "method": result.method.value,
                    "provider": result.provider,
                    "authentication_status": result.status.value,
                    "is_authenticated": result.is_authenticated,
                    "reason_codes": list(result.reason_codes),
                }
                sanitized_trace_meta = sanitize_security_data(trace_meta)

                self.agent_trace_service.record_step(
                    component_name="AuthenticationService",
                    execution_id=result.correlation_id or f"exec-{now.timestamp():.0f}",
                    step_number=1,
                    step_type=StepType.TOOL_CALL,
                    operation=f"authenticate:{result.method.value.lower()}",
                    status=TraceStatus.SUCCESS if result.is_authenticated else TraceStatus.FAILED,
                    started_at=now,
                    completed_at=now,
                    tool_or_service="AuthenticationService",
                    correlation_id=result.correlation_id or f"corr-{now.timestamp():.0f}",
                    provenance="AUTHENTICATION_SERVICE_N2",
                    metadata=sanitized_trace_meta,
                )
            except Exception as e:
                logger.warning(f"Failed to record authentication agent trace: {e}")
