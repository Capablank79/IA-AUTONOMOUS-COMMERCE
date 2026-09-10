"""
Servicio de Aplicación para SaaS Authentication y Gestión Multi-Tenant de Sesiones (Hito O.3 — SaaS / Platformization).

Implementa:
- SaaSSessionService (SaaSSessionServicePort)

Responsabilidades:
- Consumir N.2 AuthenticationResult como base de confianza de la identidad autenticada.
- Validar existencia y vigencia de la identidad (N.1).
- Validar vinculación a Tenant y pertenencia de la identidad (O.1).
- Validar membresía activa en Organization si se especifica organization_id (O.2).
- Crear SaaSSession con session_id no predecible, determinista y seguro (cero tokens OAuth ni credenciales).
- Validar sesiones existentes: estado ACTIVE, no expirada (ClockPort K.7), pertenencia de tenant/org, integridad SHA-256.
- Revocar sesiones de forma persistente, inmutable y auditable (SESSION_REVOKED).
- Logout limpio que invalida la sesión sin eliminar identidades ni membresías.
- Producir SessionContext seguro downstream desacoplado de secretos para TenantContext (O.1) y RBAC (N.4).
- Fail-Safe total: Denegar acceso ante cualquier discrepancia, corrupción o estado no-activo.
"""

from datetime import datetime, timezone, timedelta
import logging
import uuid
from typing import Optional, Dict, Any, Mapping, List, Union

from src.domain.session.models import (
    SaaSSession,
    SessionStatus,
    SessionReference,
    SessionContext,
    SessionValidationResult,
    SessionValidationReasonCode,
    SessionError,
    SessionNotFoundError,
    SessionValidationError,
    SessionExpiredError,
    SessionRevokedError,
    SessionTenantMismatchError,
    SessionSecurityViolationError,
    generate_secure_session_id,
    compute_session_checksum,
)
from src.domain.session.ports import (
    SaaSSessionRepositoryPort,
    SaaSSessionServicePort,
)
from src.domain.authentication.models import (
    AuthenticationResult,
    AuthenticationStatus,
)
from src.domain.identity.ports import IdentityRepositoryPort
from src.domain.identity.models import IdentityStatus
from src.domain.tenant.models import (
    TenantContext,
    CrossTenantAccessError,
    TenantSecurityViolationError,
)
from src.domain.tenant.ports import (
    TenantResolverPort,
    TenantMappingPort,
)
from src.domain.tenant.guard import CrossTenantGuard
from src.domain.organization.models import (
    OrganizationStatus,
    MembershipStatus,
)
from src.domain.organization.ports import (
    OrganizationRepositoryPort,
    MembershipRepositoryPort,
)
from src.domain.reliability.ports import ClockPort
from src.domain.audit.models import (
    AuditRecordType,
    AuditActor,
    AuditActorType,
    AuditRecord,
)
from src.domain.audit.ports import AuditRepositoryPort
from src.domain.security.models import validate_safe_identifier

logger = logging.getLogger(__name__)


class SaaSSessionService(SaaSSessionServicePort):
    """
    Servicio central de Aplicación para la Gestión y Validación de Sesiones SaaS Multi-Tenant.
    """

    def __init__(
        self,
        session_repository: SaaSSessionRepositoryPort,
        tenant_resolver: Optional[TenantResolverPort] = None,
        tenant_mapping: Optional[TenantMappingPort] = None,
        organization_repo: Optional[OrganizationRepositoryPort] = None,
        membership_repo: Optional[MembershipRepositoryPort] = None,
        identity_repo: Optional[IdentityRepositoryPort] = None,
        clock: Optional[ClockPort] = None,
        audit_repository: Optional[AuditRepositoryPort] = None,
    ):
        self._session_repo = session_repository
        self._tenant_resolver = tenant_resolver
        self._tenant_mapping = tenant_mapping
        self._org_repo = organization_repo
        self._membership_repo = membership_repo
        self._identity_repo = identity_repo
        self._clock = clock
        self._audit_repo = audit_repository

    def _now(self) -> datetime:
        if self._clock:
            current = self._clock.now()
            if current.tzinfo is None:
                return current.replace(tzinfo=timezone.utc)
            return current
        return datetime.now(timezone.utc)

    def create_session(
        self,
        auth_result: AuthenticationResult,
        tenant_id: str,
        organization_id: Optional[str] = None,
        ttl_seconds: int = 3600,
        correlation_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SaaSSession:
        """
        Crea una sesión SaaS activa vinculada de forma segura a una identidad autenticada (N.2)
        y a un tenant (O.1) y opcionalmente organization (O.2).

        Validaciones previas:
        1. auth_result debe estar AUTHENTICATED y tener principal/identity_id válida.
        2. auth_result no debe estar expirado.
        3. Identity (N.1) debe existir y no estar SUSPENDED si identity_repo está disponible.
        4. tenant_id debe ser un identificador seguro.
        5. Identity no debe pertenecer a un tenant diferente (Cross-Tenant prevention).
        6. Si organization_id es provisto:
           - organization debe existir y estar ACTIVE en el tenant.
           - Identity debe tener membresía ACTIVE en dicha organización.
        7. session_id es criptográficamente no predecible (no reutiliza token OAuth).
        8. Registro de auditoría K.1 (SESSION_CREATED).
        """
        if not isinstance(auth_result, AuthenticationResult):
            raise SessionValidationError("auth_result must be an instance of AuthenticationResult.")

        if not auth_result.is_authenticated or not auth_result.principal:
            raise SessionValidationError(
                f"Cannot create SaaS session from unauthenticated result (status: {auth_result.status.value})."
            )

        identity_id = auth_result.principal.identity_id
        validate_safe_identifier(identity_id, field_name="identity_id")
        validate_safe_identifier(tenant_id, field_name="tenant_id")

        now = self._now()

        # Validar expiración del auth_result si tiene expires_at
        if auth_result.expires_at and now >= auth_result.expires_at:
            raise SessionExpiredError("AuthenticationResult is already expired.")

        # Validar Identity en el Identity Registry (N.1)
        if self._identity_repo:
            ident = self._identity_repo.get_identity(identity_id)
            if not ident:
                raise SessionValidationError(f"Identity '{identity_id}' does not exist in Identity Registry.")
            if ident.status == IdentityStatus.SUSPENDED:
                raise SessionValidationError(f"Identity '{identity_id}' is SUSPENDED.")

        # Validar vinculación de Tenant (O.1)
        if self._tenant_resolver:
            self._tenant_resolver.validate_tenant_exists(tenant_id)

        if self._tenant_mapping:
            bound_tenant = self._tenant_mapping.get_tenant_for_identity(identity_id)
            if bound_tenant and bound_tenant != tenant_id:
                raise CrossTenantAccessError(
                    f"CROSS_TENANT_SESSION_DENIED: Identity '{identity_id}' belongs to tenant '{bound_tenant}', cannot create session in tenant '{tenant_id}'."
                )

        # Validar Organización y Membresía (O.2) si se suministra organization_id
        if organization_id is not None:
            validate_safe_identifier(organization_id, field_name="organization_id")
            tenant_ctx = TenantContext(tenant_id=tenant_id, identity_id=identity_id)

            if self._org_repo:
                org = self._org_repo.get_by_id(tenant_ctx, organization_id)
                if not org:
                    raise SessionValidationError(
                        f"Organization '{organization_id}' not found in tenant '{tenant_id}'."
                    )
                if org.status != OrganizationStatus.ACTIVE:
                    raise SessionValidationError(
                        f"Organization '{organization_id}' is not ACTIVE (current status: {org.status.value})."
                    )

            if self._membership_repo:
                mem = self._membership_repo.get_by_identity_and_org(tenant_ctx, organization_id, identity_id)
                if not mem:
                    raise SessionValidationError(
                        f"Identity '{identity_id}' has no membership in organization '{organization_id}'."
                    )
                if mem.status != MembershipStatus.ACTIVE:
                    raise SessionValidationError(
                        f"Membership for identity '{identity_id}' in organization '{organization_id}' is not ACTIVE (current status: {mem.status.value})."
                    )

        session_id = generate_secure_session_id()
        expires_at = now + timedelta(seconds=max(ttl_seconds, 1))

        auth_method_str = auth_result.method.value if hasattr(auth_result.method, "value") else str(auth_result.method)
        auth_provider_str = auth_result.provider

        session = SaaSSession(
            session_id=session_id,
            identity_id=identity_id,
            tenant_id=tenant_id,
            organization_id=organization_id,
            authentication_method=auth_method_str,
            authentication_provider=auth_provider_str,
            created_at=now,
            expires_at=expires_at,
            last_validated_at=now,
            status=SessionStatus.ACTIVE,
            metadata=metadata or {},
        )

        self._session_repo.save(session)

        self._record_audit(
            record_type=AuditRecordType.SESSION_CREATED,
            session=session,
            action="CREATE_SAAS_SESSION",
            correlation_id=correlation_id,
            details={"ttl_seconds": ttl_seconds},
        )

        return session

    def validate_session(
        self,
        session_id: str,
        expected_tenant_id: Optional[str] = None,
        expected_organization_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> SessionValidationResult:
        """
        Valida exhaustivamente una sesión.

        Validaciones:
        1. session_id formato seguro y existencia en repositorio.
        2. Status ACTIVE (si REVOKED -> SESSION_REVOKED, si EXPIRED -> SESSION_EXPIRED).
        3. Expiración determinista contra ClockPort (si now >= expires_at -> marca sesión EXPIRED).
        4. Integridad SHA-256 de la sesión.
        5. Si expected_tenant_id está provisto, session.tenant_id debe coincidir exactamente (si no -> SESSION_TENANT_MISMATCH).
        6. Si expected_organization_id está provisto, session.organization_id debe coincidir exactamente.
        7. Si session tiene organization_id, validar que la membresía siga ACTIVE en O.2.
        8. Si identity está suspendida en N.1 -> INVALID.
        """
        now = self._now()
        try:
            validate_safe_identifier(session_id, field_name="session_id")
        except Exception:
            return SessionValidationResult(
                is_valid=False,
                status=SessionStatus.INVALID,
                reason_codes=(SessionValidationReasonCode.INTEGRITY_COMPROMISED.value,),
                validated_at=now,
                correlation_id=correlation_id,
            )

        session = self._session_repo.get_by_id(session_id, tenant_id=expected_tenant_id)
        if not session:
            # Intentar buscar sin tenant_id para detectar cross-tenant mismatch si aplica
            global_session = self._session_repo.get_by_id(session_id)
            if global_session and expected_tenant_id and global_session.tenant_id != expected_tenant_id:
                self._record_audit(
                    record_type=AuditRecordType.SESSION_TENANT_MISMATCH,
                    session=global_session,
                    action="VALIDATE_SAAS_SESSION",
                    correlation_id=correlation_id,
                    details={"expected_tenant_id": expected_tenant_id, "actual_tenant_id": global_session.tenant_id},
                )
                return SessionValidationResult(
                    is_valid=False,
                    status=SessionStatus.INVALID,
                    session=global_session,
                    reason_codes=(SessionValidationReasonCode.TENANT_MISMATCH.value,),
                    validated_at=now,
                    correlation_id=correlation_id,
                )

            return SessionValidationResult(
                is_valid=False,
                status=SessionStatus.INVALID,
                reason_codes=(SessionValidationReasonCode.SESSION_NOT_FOUND.value,),
                validated_at=now,
                correlation_id=correlation_id,
            )

        # Validar Tenant Mismatch explícito
        if expected_tenant_id is not None and session.tenant_id != expected_tenant_id:
            self._record_audit(
                record_type=AuditRecordType.SESSION_TENANT_MISMATCH,
                session=session,
                action="VALIDATE_SAAS_SESSION",
                correlation_id=correlation_id,
                details={"expected_tenant_id": expected_tenant_id, "actual_tenant_id": session.tenant_id},
            )
            return SessionValidationResult(
                is_valid=False,
                status=SessionStatus.INVALID,
                session=session,
                reason_codes=(SessionValidationReasonCode.TENANT_MISMATCH.value,),
                validated_at=now,
                correlation_id=correlation_id,
            )

        # Validar Organization Mismatch si se especificó
        if expected_organization_id is not None and session.organization_id != expected_organization_id:
            return SessionValidationResult(
                is_valid=False,
                status=SessionStatus.INVALID,
                session=session,
                reason_codes=(SessionValidationReasonCode.ORGANIZATION_MISMATCH.value,),
                validated_at=now,
                correlation_id=correlation_id,
            )

        # Validar estado Revocado
        if session.status == SessionStatus.REVOKED:
            return SessionValidationResult(
                is_valid=False,
                status=SessionStatus.REVOKED,
                session=session,
                reason_codes=(SessionValidationReasonCode.SESSION_REVOKED.value,),
                validated_at=now,
                correlation_id=correlation_id,
            )

        # Validar estado Expirado previo
        if session.status == SessionStatus.EXPIRED:
            return SessionValidationResult(
                is_valid=False,
                status=SessionStatus.EXPIRED,
                session=session,
                reason_codes=(SessionValidationReasonCode.SESSION_EXPIRED.value,),
                validated_at=now,
                correlation_id=correlation_id,
            )

        # Validar Expiración por tiempo
        if session.is_expired(now):
            expired_session = session.with_status(SessionStatus.EXPIRED, last_validated_at=now)
            self._session_repo.save(expired_session)
            self._record_audit(
                record_type=AuditRecordType.SESSION_EXPIRED,
                session=expired_session,
                action="EXPIRE_SAAS_SESSION",
                correlation_id=correlation_id,
            )
            return SessionValidationResult(
                is_valid=False,
                status=SessionStatus.EXPIRED,
                session=expired_session,
                reason_codes=(SessionValidationReasonCode.SESSION_EXPIRED.value,),
                validated_at=now,
                correlation_id=correlation_id,
            )

        # Validar estado ACTIVE
        if session.status != SessionStatus.ACTIVE:
            return SessionValidationResult(
                is_valid=False,
                status=session.status,
                session=session,
                reason_codes=(SessionValidationReasonCode.SESSION_NOT_ACTIVE.value,),
                validated_at=now,
                correlation_id=correlation_id,
            )

        # Validar Identity en N.1
        if self._identity_repo:
            ident = self._identity_repo.get_identity(session.identity_id)
            if not ident or ident.status != IdentityStatus.ACTIVE:
                return SessionValidationResult(
                    is_valid=False,
                    status=SessionStatus.INVALID,
                    session=session,
                    reason_codes=(SessionValidationReasonCode.IDENTITY_MISMATCH.value,),
                    validated_at=now,
                    correlation_id=correlation_id,
                )

        # Validar Membresía en O.2 si la sesión tiene organization_id
        if session.organization_id is not None:
            tenant_ctx = TenantContext(tenant_id=session.tenant_id, identity_id=session.identity_id)
            if self._org_repo:
                org = self._org_repo.get_by_id(tenant_ctx, session.organization_id)
                if not org or org.status != OrganizationStatus.ACTIVE:
                    return SessionValidationResult(
                        is_valid=False,
                        status=SessionStatus.INVALID,
                        session=session,
                        reason_codes=(SessionValidationReasonCode.ORGANIZATION_MISMATCH.value,),
                        validated_at=now,
                        correlation_id=correlation_id,
                    )
            if self._membership_repo:
                mem = self._membership_repo.get_by_identity_and_org(tenant_ctx, session.organization_id, session.identity_id)
                if not mem or mem.status != MembershipStatus.ACTIVE:
                    return SessionValidationResult(
                        is_valid=False,
                        status=SessionStatus.INVALID,
                        session=session,
                        reason_codes=(SessionValidationReasonCode.MEMBERSHIP_INACTIVE.value,),
                        validated_at=now,
                        correlation_id=correlation_id,
                    )

        # Sesión completamente válida: actualizar last_validated_at
        validated_session = session.with_status(SessionStatus.ACTIVE, last_validated_at=now)
        self._session_repo.save(validated_session)

        sess_ctx = validated_session.to_context(correlation_id=correlation_id)

        self._record_audit(
            record_type=AuditRecordType.SESSION_VALIDATED,
            session=validated_session,
            action="VALIDATE_SAAS_SESSION",
            correlation_id=correlation_id,
        )

        return SessionValidationResult(
            is_valid=True,
            status=SessionStatus.ACTIVE,
            session=validated_session,
            session_context=sess_ctx,
            reason_codes=(SessionValidationReasonCode.VALID.value,),
            validated_at=now,
            correlation_id=correlation_id,
        )

    def revoke_session(
        self,
        session_id: str,
        tenant_id: Optional[str] = None,
        reason: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> SaaSSession:
        """Revoca de forma permanente una sesión SaaS."""
        validate_safe_identifier(session_id, field_name="session_id")
        session = self._session_repo.get_by_id(session_id, tenant_id=tenant_id)
        if not session:
            raise SessionNotFoundError(f"Session '{session_id}' not found.")

        now = self._now()
        meta = dict(session.metadata)
        if reason:
            meta["revocation_reason"] = reason

        revoked = session.with_status(SessionStatus.REVOKED, last_validated_at=now, metadata=meta)
        self._session_repo.save(revoked)

        self._record_audit(
            record_type=AuditRecordType.SESSION_REVOKED,
            session=revoked,
            action="REVOKE_SAAS_SESSION",
            correlation_id=correlation_id,
            details={"reason": reason or "explicit_revocation"},
        )

        return revoked

    def logout(
        self,
        session_id: str,
        tenant_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> SaaSSession:
        """Cierra la sesión SaaS (marcando como REVOKED) sin alterar las entidades subyacentes."""
        return self.revoke_session(
            session_id=session_id,
            tenant_id=tenant_id,
            reason="LOGOUT",
            correlation_id=correlation_id,
        )

    def get_session_context(
        self,
        session_id: str,
        expected_tenant_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> SessionContext:
        """Obtiene el SessionContext downstream validado o lanza excepción."""
        result = self.validate_session(
            session_id=session_id,
            expected_tenant_id=expected_tenant_id,
            correlation_id=correlation_id,
        )
        if not result.is_valid or not result.session_context:
            if result.status == SessionStatus.EXPIRED:
                raise SessionExpiredError(f"Session '{session_id}' is EXPIRED.")
            if result.status == SessionStatus.REVOKED:
                raise SessionRevokedError(f"Session '{session_id}' is REVOKED.")
            if SessionValidationReasonCode.TENANT_MISMATCH.value in result.reason_codes:
                raise SessionTenantMismatchError(f"Session '{session_id}' tenant does not match expected tenant.")
            raise SessionValidationError(f"Session '{session_id}' validation failed: {result.reason_codes}")

        return result.session_context

    def _record_audit(
        self,
        record_type: AuditRecordType,
        session: SaaSSession,
        action: str,
        correlation_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not self._audit_repo:
            return
        now = self._now()
        unique_id = uuid.uuid4().hex[:8]
        meta = {
            "tenant_id": session.tenant_id,
            "session_id": session.session_id,
            "identity_id": session.identity_id,
            "organization_id": session.organization_id,
            "status": session.status.value,
        }
        if details:
            meta.update(details)

        rec = AuditRecord(
            audit_id=f"aud-sess-{session.session_id[:12]}-{action.lower()}-{unique_id}",
            record_type=record_type,
            occurred_at=now,
            actor=AuditActor(
                actor_type=AuditActorType.USER,
                actor_id=session.identity_id,
            ),
            subject_type="SAAS_SESSION",
            subject_id=session.session_id,
            action_or_operation=action,
            status="SUCCESS",
            correlation_id=correlation_id or session.session_id,
            provenance="SAAS_SESSION_SERVICE",
            metadata=meta,
        )
        self._audit_repo.append(rec)
