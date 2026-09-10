"""
Servicio de Aplicación para SaaS Authorization y Multi-Tenant RBAC & Scoping (Hito O.4 — SaaS / Platformization).

Responsabilidades:
1. Validar la precondición de Sesión SaaS (O.3 SaaSSessionService / SaaSSessionRepository):
   - ACTIVE, no expirada (ClockPort K.7), no revocada, checksum verificado.
2. Validar el Aislamiento de Tenant (O.1 TenantContext / CrossTenantGuard):
   - Coincidencia estricta entre session.tenant_id, target tenant_id y resource tenant_id.
3. Validar la Membresía Organizacional (O.2 OrganizationMembershipService / MembershipRepository):
   - Si la acción o recurso es organization-scoped, verificar membresía ACTIVE de la identidad.
   - Estados SUSPENDED, REMOVED, INVITED o ausentes -> DENY.
4. Resolver dinámicamente RBAC en el Tenant Scope canónico (N.4 RBACService):
   - Resolver effective permissions en tiempo real usando el scope del tenant/cuenta.
   - La sesión NUNCA almacena permisos como snapshot estático (evita stale permissions).
   - Revocaciones de rol toman efecto inmediato sin destruir la sesión o forzar relogin.
5. Invocar N.3 AuthorizationService / PolicyEngine:
   - Construir AuthorizationRequest inyectando los effective permissions resueltos.
   - Si N.3 resulta en DENY -> O.4 emite DENY (NUNCA override).
6. Validar pertenencia y propiedad de Recursos / Marketplace Accounts:
   - Bloquear acceso a recursos con tenant falso o cuentas cross-tenant.
7. Emitir Auditoría K.1 y Trazas K.2 sanitizadas (cero secretos, tokens OAuth ni PII).
8. Aplicar DEFAULT DENY en todos los casos de fallo, inconsistencia o datos desconocidos.
"""

from datetime import datetime, timezone
import logging
import uuid
from typing import Optional, Sequence, Mapping, Any, Dict, Union, Tuple, Set

from src.domain.identity.models import (
    IdentityReference,
    IdentityType,
    identity_to_audit_actor,
)
from src.domain.authentication.models import (
    PrincipalContext,
    AuthenticationResult,
    AuthenticationStatus,
    AuthenticationMethod,
)
from src.domain.session.models import (
    SaaSSession,
    SessionContext,
    SessionStatus,
    SessionValidationResult,
    SessionValidationReasonCode,
)
from src.domain.session.ports import SaaSSessionRepositoryPort, SaaSSessionServicePort
from src.domain.tenant.models import (
    TenantId,
    TenantContext,
    TenantScope,
    TenantScopedResource,
    CrossTenantAccessError,
)
from src.domain.tenant.guard import CrossTenantGuard
from src.domain.organization.models import (
    Organization,
    UserMembership,
    MembershipStatus,
    OrganizationStatus,
)
from src.domain.organization.ports import (
    OrganizationRepositoryPort,
    MembershipRepositoryPort,
)
from src.domain.rbac.models import (
    Permission,
    Role,
    RoleAssignment,
    PermissionSet,
    RbacEvaluationResult,
    normalize_action_token,
)
from src.domain.rbac.ports import RoleRepositoryPort, RoleAssignmentRepositoryPort
from src.application.rbac.rbac_service import RBACService
from src.domain.authorization.models import (
    AuthorizationRequest,
    AuthorizationDecision,
    AuthorizationStatus,
    AuthorizationReasonCode,
    ResourceReference,
)
from src.application.authorization.authorization_service import AuthorizationService
from src.domain.saas_authorization.models import (
    SaaSAuthorizationRequest,
    SaaSAuthorizationDecision,
    SaaSAuthorizationContext,
    SaaSAuthorizationStatus,
    SaaSAuthorizationReasonCode,
    SaaSAuthorizationError,
)
from src.domain.saas_authorization.ports import (
    ResourceOwnershipResolverPort,
    SaaSAuthorizationServicePort,
)
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


class SaaSAuthorizationService(SaaSAuthorizationServicePort):
    """
    Servicio de Aplicación para SaaS Authorization Multi-Tenant (Hito O.4).
    """

    def __init__(
        self,
        session_repository: Optional[SaaSSessionRepositoryPort] = None,
        session_service: Optional[SaaSSessionServicePort] = None,
        organization_repository: Optional[OrganizationRepositoryPort] = None,
        membership_repository: Optional[MembershipRepositoryPort] = None,
        rbac_service: Optional[RBACService] = None,
        authorization_service: Optional[AuthorizationService] = None,
        resource_ownership_resolver: Optional[ResourceOwnershipResolverPort] = None,
        audit_repository: Optional[AuditRepositoryPort] = None,
        trace_service: Optional[AgentTraceService] = None,
        clock: Optional[ClockPort] = None,
        default_policy_version: str = "1.0.0",
    ):
        self.session_repository = session_repository
        self.session_service = session_service
        self.organization_repository = organization_repository
        self.membership_repository = membership_repository
        self.rbac_service = rbac_service
        self.authorization_service = authorization_service or AuthorizationService(clock=clock)
        self.resource_ownership_resolver = resource_ownership_resolver
        self.audit_repository = audit_repository
        self.trace_service = trace_service
        self.clock = clock
        self.default_policy_version = default_policy_version

    def _now(self) -> datetime:
        """Timestamp determinista respetando ClockPort (K.7)."""
        if self.clock is not None:
            now_dt = self.clock.now()
            if now_dt.tzinfo is None:
                return now_dt.replace(tzinfo=timezone.utc)
            return now_dt
        return datetime.now(timezone.utc)

    def authorize(self, request: SaaSAuthorizationRequest) -> SaaSAuthorizationDecision:
        """
        Orquesta el flujo completo de evaluación de autorización SaaS:
        1. Precondición de Sesión O.3.
        2. Validación de Tenant O.1 y CrossTenantGuard.
        3. Validación de Membresía O.2 (si aplica organización).
        4. Verificación de Resource Ownership / Marketplace Account.
        5. Resolución dinámica de RBAC N.4 en scope del tenant.
        6. Evaluación en N.3 AuthorizationService / PolicyEngine.
        7. Auditoría K.1 y Trazas K.2.
        """
        now = self._now()
        corr_id = request.correlation_id or f"saas_authz_{uuid.uuid4().hex[:12]}"
        dec_id = f"saas_dec_{uuid.uuid4().hex[:12]}"
        policy_ver = request.policy_version or self.default_policy_version

        # Helper para denegación rápida determinista
        def _deny(
            reason_code: SaaSAuthorizationReasonCode,
            message: str,
            session_id: str = "unknown_session",
            identity_id: str = "unknown_identity",
            tenant_id: str = "unknown_tenant",
            organization_id: Optional[str] = None,
            res_str: Optional[str] = None,
            res_tenant: Optional[str] = None,
            res_org: Optional[str] = None,
            resolved_perms: Sequence[str] = (),
            resolved_roles: Sequence[str] = (),
            n3_dec: Optional[AuthorizationDecision] = None,
            status: SaaSAuthorizationStatus = SaaSAuthorizationStatus.DENY,
        ) -> SaaSAuthorizationDecision:
            ctx = SaaSAuthorizationContext(
                session_id=session_id if session_id and session_id.strip() else "unknown_session",
                identity_id=identity_id if identity_id and identity_id.strip() else "unknown_identity",
                tenant_id=tenant_id if tenant_id and tenant_id.strip() else "unknown_tenant",
                action=request.action,
                organization_id=organization_id,
                resource=res_str,
                resource_tenant_id=res_tenant,
                resource_organization_id=res_org,
                resolved_permissions=tuple(resolved_perms),
                resolved_roles=tuple(resolved_roles),
                n3_decision_reference=n3_dec.decision_id if n3_dec else None,
                policy_version=policy_ver,
                correlation_id=corr_id,
                metadata={"reason": reason_code.value, "message": message},
            )
            decision = SaaSAuthorizationDecision(
                decision_id=dec_id,
                status=status,
                reason_code=reason_code,
                context=ctx,
                n3_decision=n3_dec,
                message=message,
                evaluated_at=now,
                policy_version=policy_ver,
            )
            self._audit_and_trace(request, decision)
            return decision

        # ---------------------------------------------------------------------
        # 1. RESOLUCIÓN Y VALIDACIÓN DE SESIÓN (O.3)
        # ---------------------------------------------------------------------
        session: Optional[SaaSSession] = None

        if request.session is not None:
            session = request.session
        elif request.session_context is not None:
            # Reconstruir o cargar desde repository
            if self.session_repository:
                session = self.session_repository.get_by_id(
                    session_id=request.session_context.session_id,
                    tenant_id=request.session_context.tenant_id,
                )
            if not session:
                # Usar datos del session_context si está activo
                if request.session_context.status != SessionStatus.ACTIVE:
                    return _deny(
                        SaaSAuthorizationReasonCode.SESSION_INVALID,
                        f"Session context status is {request.session_context.status.value}",
                        session_id=request.session_context.session_id,
                        identity_id=request.session_context.identity_id,
                        tenant_id=request.session_context.tenant_id,
                    )
        elif request.session_id is not None:
            if self.session_service:
                val_res: SessionValidationResult = self.session_service.validate_session(
                    session_id=request.session_id,
                    expected_tenant_id=request.tenant_id,
                    expected_organization_id=request.organization_id,
                    correlation_id=corr_id,
                )
                if not val_res.is_valid:
                    reason_map = {
                        SessionValidationReasonCode.SESSION_NOT_FOUND: SaaSAuthorizationReasonCode.SESSION_NOT_FOUND,
                        SessionValidationReasonCode.SESSION_EXPIRED: SaaSAuthorizationReasonCode.SESSION_EXPIRED,
                        SessionValidationReasonCode.SESSION_REVOKED: SaaSAuthorizationReasonCode.SESSION_REVOKED,
                        SessionValidationReasonCode.TENANT_MISMATCH: SaaSAuthorizationReasonCode.SESSION_TENANT_MISMATCH,
                        SessionValidationReasonCode.ORGANIZATION_MISMATCH: SaaSAuthorizationReasonCode.ORGANIZATION_MISMATCH,
                        SessionValidationReasonCode.MEMBERSHIP_INACTIVE: SaaSAuthorizationReasonCode.MEMBERSHIP_NOT_ACTIVE,
                    }
                    mapped_reason = reason_map.get(val_res.reason_code, SaaSAuthorizationReasonCode.SESSION_INVALID)
                    return _deny(
                        mapped_reason,
                        f"Session validation failed: {val_res.reason_code.value} - {val_res.message}",
                        session_id=request.session_id,
                        identity_id=request.identity_id or "unknown_identity",
                        tenant_id=request.tenant_id or "unknown_tenant",
                    )
            if self.session_repository:
                session = self.session_repository.get_by_id(
                    session_id=request.session_id,
                )

        if session is None:
            if request.session_context is None:
                return _deny(
                    SaaSAuthorizationReasonCode.SESSION_NOT_FOUND,
                    "No active SaaS session or context provided.",
                    session_id=request.session_id or "unknown_session",
                    identity_id=request.identity_id or "unknown_identity",
                    tenant_id=request.tenant_id or "unknown_tenant",
                )

        # Si tenemos SaaSSession entidad, validar exhaustivamente
        active_session_id = session.session_id if session else request.session_context.session_id
        active_identity_id = session.identity_id if session else request.session_context.identity_id
        active_tenant_id = session.tenant_id if session else request.session_context.tenant_id
        active_org_id = (session.organization_id if session else request.session_context.organization_id)

        if session:
            # Validar ciclo de vida
            if session.status == SessionStatus.EXPIRED or session.is_expired(now):
                return _deny(
                    SaaSAuthorizationReasonCode.SESSION_EXPIRED,
                    f"Session expired at {session.expires_at.isoformat()}",
                    session_id=active_session_id,
                    identity_id=active_identity_id,
                    tenant_id=active_tenant_id,
                )
            if session.status == SessionStatus.REVOKED:
                return _deny(
                    SaaSAuthorizationReasonCode.SESSION_REVOKED,
                    "Session is revoked.",
                    session_id=active_session_id,
                    identity_id=active_identity_id,
                    tenant_id=active_tenant_id,
                )
            if session.status != SessionStatus.ACTIVE:
                return _deny(
                    SaaSAuthorizationReasonCode.SESSION_INVALID,
                    f"Session status is {session.status.value}",
                    session_id=active_session_id,
                    identity_id=active_identity_id,
                    tenant_id=active_tenant_id,
                )

        # ---------------------------------------------------------------------
        # 2. TENANT ISOLATION MATCH & VALIDATION (O.1)
        # ---------------------------------------------------------------------
        if request.tenant_id is not None and request.tenant_id != active_tenant_id:
            return _deny(
                SaaSAuthorizationReasonCode.SESSION_TENANT_MISMATCH,
                f"Requested tenant '{request.tenant_id}' does not match session tenant '{active_tenant_id}'.",
                session_id=active_session_id,
                identity_id=active_identity_id,
                tenant_id=active_tenant_id,
            )

        if request.identity_id is not None and request.identity_id != active_identity_id:
            return _deny(
                SaaSAuthorizationReasonCode.SESSION_INVALID,
                f"Requested identity '{request.identity_id}' does not match session identity '{active_identity_id}'.",
                session_id=active_session_id,
                identity_id=active_identity_id,
                tenant_id=active_tenant_id,
            )

        # Crear TenantContext formal para operaciones aisladas
        tenant_context = TenantContext(
            tenant_id=active_tenant_id,
            identity_id=active_identity_id,
            correlation_id=corr_id,
            marketplace_account_id=request.marketplace_account_id,
        )

        # ---------------------------------------------------------------------
        # 3. RESOURCE / MARKETPLACE ACCOUNT OWNERSHIP VALIDATION
        # ---------------------------------------------------------------------
        target_resource_str: Optional[str] = None
        target_resource_tenant: Optional[str] = None
        target_resource_org: Optional[str] = None

        if request.resource is not None:
            if isinstance(request.resource, TenantScopedResource):
                target_resource_str = f"{request.resource.resource_type}:{request.resource.resource_id}"
                target_resource_tenant = request.resource.tenant_id
            elif isinstance(request.resource, ResourceReference):
                target_resource_str = f"{request.resource.resource_type}:{request.resource.resource_id}"
                if request.resource.metadata and "tenant_id" in request.resource.metadata:
                    target_resource_tenant = str(request.resource.metadata["tenant_id"])
                if request.resource.account_id and request.marketplace_account_id:
                    if request.resource.account_id != request.marketplace_account_id:
                        return _deny(
                            SaaSAuthorizationReasonCode.MARKETPLACE_ACCOUNT_MISMATCH,
                            f"Resource account '{request.resource.account_id}' does not match requested account '{request.marketplace_account_id}'.",
                            session_id=active_session_id,
                            identity_id=active_identity_id,
                            tenant_id=active_tenant_id,
                            res_str=target_resource_str,
                        )
            elif isinstance(request.resource, str):
                target_resource_str = request.resource
            elif isinstance(request.resource, dict):
                target_resource_str = str(request.resource.get("resource_id", "dict_resource"))
                if "tenant_id" in request.resource:
                    target_resource_tenant = str(request.resource["tenant_id"])
                if "organization_id" in request.resource:
                    target_resource_org = str(request.resource["organization_id"])

        # Si el caller proveyó resource_tenant_id explícito
        if request.resource_tenant_id is not None:
            if target_resource_tenant is not None and target_resource_tenant != request.resource_tenant_id:
                return _deny(
                    SaaSAuthorizationReasonCode.RESOURCE_TENANT_MISMATCH,
                    f"Declared resource_tenant_id '{request.resource_tenant_id}' conflicts with resource metadata '{target_resource_tenant}'.",
                    session_id=active_session_id,
                    identity_id=active_identity_id,
                    tenant_id=active_tenant_id,
                    res_str=target_resource_str,
                    res_tenant=target_resource_tenant,
                )
            target_resource_tenant = request.resource_tenant_id

        # Validar pertenencia del marketplace account si se especificó
        if request.marketplace_account_id is not None and self.resource_ownership_resolver:
            account_ownership = self.resource_ownership_resolver.resolve_resource_ownership(request.marketplace_account_id)
            if account_ownership is not None:
                acc_tenant, _ = account_ownership
                if acc_tenant != active_tenant_id:
                    return _deny(
                        SaaSAuthorizationReasonCode.MARKETPLACE_ACCOUNT_MISMATCH,
                        f"Marketplace account '{request.marketplace_account_id}' belongs to tenant '{acc_tenant}', not session tenant '{active_tenant_id}'.",
                        session_id=active_session_id,
                        identity_id=active_identity_id,
                        tenant_id=active_tenant_id,
                    )

        # Consultar ResourceOwnershipResolver si está disponible para validar pertenencia confiable
        if self.resource_ownership_resolver and request.resource is not None:
            resolved_ownership = self.resource_ownership_resolver.resolve_resource_ownership(request.resource)
            if resolved_ownership is None:
                # Recurso desconocido -> FAIL-SAFE DEFAULT DENY
                return _deny(
                    SaaSAuthorizationReasonCode.UNKNOWN_RESOURCE_OWNERSHIP,
                    f"Resource ownership could not be verified by resolver for '{target_resource_str}'.",
                    session_id=active_session_id,
                    identity_id=active_identity_id,
                    tenant_id=active_tenant_id,
                    res_str=target_resource_str,
                )
            res_owner_tenant, res_owner_org = resolved_ownership
            target_resource_tenant = res_owner_tenant
            if res_owner_org:
                target_resource_org = res_owner_org

        # Validar Cross-Tenant Resource Access con CrossTenantGuard
        if target_resource_tenant is not None:
            try:
                CrossTenantGuard.assert_same_tenant(
                    request_context=tenant_context,
                    target_tenant_id=target_resource_tenant,
                    operation_name=request.action,
                )
            except CrossTenantAccessError as ctae:
                return _deny(
                    SaaSAuthorizationReasonCode.RESOURCE_TENANT_MISMATCH,
                    str(ctae),
                    session_id=active_session_id,
                    identity_id=active_identity_id,
                    tenant_id=active_tenant_id,
                    res_str=target_resource_str,
                    res_tenant=target_resource_tenant,
                )

        # ---------------------------------------------------------------------
        # 4. ORGANIZATION MATCH & MEMBERSHIP STATUS VALIDATION (O.2)
        # ---------------------------------------------------------------------
        eval_org_id = request.organization_id or active_org_id or target_resource_org

        if request.organization_id is not None and active_org_id is not None:
            if request.organization_id != active_org_id:
                return _deny(
                    SaaSAuthorizationReasonCode.ORGANIZATION_MISMATCH,
                    f"Requested organization '{request.organization_id}' does not match session organization '{active_org_id}'.",
                    session_id=active_session_id,
                    identity_id=active_identity_id,
                    tenant_id=active_tenant_id,
                    organization_id=request.organization_id,
                )

        if eval_org_id is not None and self.membership_repository is not None:
            # Consultar membresía activa en tiempo real
            membership = self.membership_repository.get_by_identity_and_org(
                context=tenant_context,
                organization_id=eval_org_id,
                identity_id=active_identity_id,
            )
            if membership is None:
                return _deny(
                    SaaSAuthorizationReasonCode.MEMBERSHIP_NOT_FOUND,
                    f"Identity '{active_identity_id}' has no membership in organization '{eval_org_id}' for tenant '{active_tenant_id}'.",
                    session_id=active_session_id,
                    identity_id=active_identity_id,
                    tenant_id=active_tenant_id,
                    organization_id=eval_org_id,
                )
            if membership.status != MembershipStatus.ACTIVE:
                return _deny(
                    SaaSAuthorizationReasonCode.MEMBERSHIP_NOT_ACTIVE,
                    f"Membership in organization '{eval_org_id}' is in status '{membership.status.value}' (ACTIVE required).",
                    session_id=active_session_id,
                    identity_id=active_identity_id,
                    tenant_id=active_tenant_id,
                    organization_id=eval_org_id,
                )

        # ---------------------------------------------------------------------
        # 5. DYNAMIC RBAC RESOLUTION (N.4)
        # ---------------------------------------------------------------------
        # Resolver canonical tenant scope para RBAC (e.g. 'tenant_t1' o 'tenant_t1__account_acc1')
        t_scope = TenantScope(
            tenant_id=active_tenant_id,
            marketplace_account_id=request.marketplace_account_id,
        )
        canonical_scope = t_scope.canonical_scope

        resolved_permissions: Tuple[str, ...] = ()
        resolved_roles: Tuple[str, ...] = ()

        if self.rbac_service is not None:
            # Dynamic lookup: nunca confiar en snapshot de permisos de la sesión
            rbac_eval = self.rbac_service.resolve_effective_permissions(
                principal_or_identity=active_identity_id,
                scope=canonical_scope,
                correlation_id=corr_id,
            )
            resolved_permissions = tuple(sorted(rbac_eval.effective_permissions.actions))
            resolved_roles = rbac_eval.roles
        else:
            # Si no hay RBACService configurado, no hay permisos asignados
            resolved_permissions = ()
            resolved_roles = ()

        # Verificar si la acción solicitada está contenida en los permisos resueltos
        norm_action = normalize_action_token(request.action)
        if norm_action not in resolved_permissions and "ALL" not in resolved_permissions and "*" not in resolved_permissions:
            return _deny(
                SaaSAuthorizationReasonCode.INSUFFICIENT_PERMISSIONS,
                f"Identity '{active_identity_id}' lacks permission for action '{norm_action}' in scope '{canonical_scope}'.",
                session_id=active_session_id,
                identity_id=active_identity_id,
                tenant_id=active_tenant_id,
                organization_id=eval_org_id,
                res_str=target_resource_str,
                res_tenant=target_resource_tenant,
                res_org=target_resource_org,
                resolved_perms=resolved_permissions,
                resolved_roles=resolved_roles,
            )

        # ---------------------------------------------------------------------
        # 6. N.3 AUTHORIZATION SERVICE & POLICY ENGINE INVOCATION
        # ---------------------------------------------------------------------
        # Construir PrincipalContext N.2 para N.3
        principal_ref = IdentityReference(
            identity_id=active_identity_id,
            identity_type=IdentityType.USER,
            canonical_identifier=f"user:saas:{active_identity_id}",
        )
        auth_res = AuthenticationResult(
            status=AuthenticationStatus.AUTHENTICATED,
            method=AuthenticationMethod.SAAS_SESSION if hasattr(AuthenticationMethod, "SAAS_SESSION") else AuthenticationMethod.BEARER_TOKEN,
            provider="saas_service",
            principal=principal_ref,
            authenticated_at=now,
            correlation_id=corr_id,
        )
        principal_ctx = PrincipalContext(
            principal=principal_ref,
            auth_result=auth_res,
        )

        # Construir ResourceReference N.3 si aplica
        res_ref: Optional[ResourceReference] = None
        if target_resource_str:
            res_parts = target_resource_str.split(":", 1)
            res_type = res_parts[0] if len(res_parts) == 2 else "general"
            res_id = res_parts[1] if len(res_parts) == 2 else target_resource_str
            res_ref = ResourceReference(
                resource_type=res_type,
                resource_id=res_id,
                account_id=request.marketplace_account_id,
                metadata={"tenant_id": active_tenant_id, "organization_id": eval_org_id},
            )

        n3_req = AuthorizationRequest(
            action=request.action,
            principal_context=principal_ctx,
            identity_id=active_identity_id,
            resource=res_ref,
            commercial_context=request.commercial_context,
            correlation_id=corr_id,
            policy_version=policy_ver,
        )

        # Inyectar las acciones resueltas por RBAC a N.3
        n3_decision = self.authorization_service.authorize(
            request=n3_req,
            allowed_actions_override=list(resolved_permissions),
        )

        if n3_decision.status != AuthorizationStatus.ALLOW:
            # N.3 DENY o UNKNOWN -> O.4 DENY (nunca override)
            mapped_status = (
                SaaSAuthorizationStatus.DENY
                if n3_decision.status == AuthorizationStatus.DENY
                else SaaSAuthorizationStatus(n3_decision.status.value)
            )
            n3_reason_str = ",".join(n3_decision.reason_codes) if n3_decision.reason_codes else "POLICY_DENIED"
            n3_msg = ",".join(n3_decision.reasons) if n3_decision.reasons else "Action denied by N.3 policy"
            return _deny(
                SaaSAuthorizationReasonCode.POLICY_DENIED,
                f"Underlying N.3 PolicyEngine denied action: {n3_reason_str} - {n3_msg}",
                session_id=active_session_id,
                identity_id=active_identity_id,
                tenant_id=active_tenant_id,
                organization_id=eval_org_id,
                res_str=target_resource_str,
                res_tenant=target_resource_tenant,
                res_org=target_resource_org,
                resolved_perms=resolved_permissions,
                resolved_roles=resolved_roles,
                n3_dec=n3_decision,
                status=mapped_status,
            )

        # ---------------------------------------------------------------------
        # 7. EMISIÓN DE DECISIÓN SaaS ALLOW
        # ---------------------------------------------------------------------
        authz_context = SaaSAuthorizationContext(
            session_id=active_session_id,
            identity_id=active_identity_id,
            tenant_id=active_tenant_id,
            action=request.action,
            organization_id=eval_org_id,
            resource=target_resource_str,
            resource_tenant_id=target_resource_tenant,
            resource_organization_id=target_resource_org,
            resolved_permissions=resolved_permissions,
            resolved_roles=resolved_roles,
            n3_decision_reference=n3_decision.decision_id,
            policy_version=policy_ver,
            correlation_id=corr_id,
            metadata={"status": "ALLOW", "scope": canonical_scope},
        )

        decision = SaaSAuthorizationDecision(
            decision_id=dec_id,
            status=SaaSAuthorizationStatus.ALLOW,
            reason_code=SaaSAuthorizationReasonCode.AUTHORIZED,
            context=authz_context,
            n3_decision=n3_decision,
            message=f"Action '{request.action}' authorized for identity '{active_identity_id}' in tenant '{active_tenant_id}'.",
            evaluated_at=now,
            policy_version=policy_ver,
        )

        self._audit_and_trace(request, decision)
        return decision

    def _audit_and_trace(
        self,
        request: SaaSAuthorizationRequest,
        decision: SaaSAuthorizationDecision,
    ) -> None:
        """Emite auditoría K.1 y trazas K.2 sanitizadas para la evaluación de autorización SaaS."""
        actor = AuditActor(
            actor_id=decision.context.identity_id,
            actor_type=AuditActorType.SYSTEM if "system" in decision.context.identity_id.lower() else AuditActorType.USER,
        )

        if self.audit_repository is not None:
            record = AuditRecord(
                audit_id=f"audit_saas_authz_{uuid.uuid4().hex[:12]}",
                record_type=AuditRecordType.AUTHORIZATION_EVALUATED,
                occurred_at=decision.evaluated_at,
                actor=actor,
                subject_type="TENANT",
                subject_id=decision.context.tenant_id,
                action_or_operation=f"SAAS_AUTHORIZATION_{decision.status.value}",
                status="SUCCESS" if decision.is_allowed else "DENIED",
                correlation_id=decision.context.correlation_id,
                metadata={
                    "decision_id": decision.decision_id,
                    "session_id": decision.context.session_id,
                    "identity_id": decision.context.identity_id,
                    "tenant_id": decision.context.tenant_id,
                    "organization_id": decision.context.organization_id,
                    "action": decision.context.action,
                    "resource": str(decision.context.resource) if decision.context.resource else None,
                    "resource_tenant_id": decision.context.resource_tenant_id,
                    "reason_code": decision.reason_code.value,
                    "status": decision.status.value,
                    "roles": list(decision.context.resolved_roles),
                    "policy_version": decision.policy_version,
                },
            )
            try:
                self.audit_repository.append(record)
            except Exception as e:
                logger.warning("Failed to record SaaS authorization audit: %s", e)

        if self.trace_service is not None:
            try:
                self.trace_service.record_step(
                    component_name="SaaSAuthorizationService",
                    execution_id=decision.context.correlation_id or decision.context.session_id,
                    step_number=1,
                    step_type=StepType.POLICY_EVALUATION,
                    operation=f"SAAS_AUTHORIZATION_{decision.status.value}",
                    status=TraceStatus.SUCCESS if decision.is_allowed else TraceStatus.FAILED,
                    correlation_id=decision.context.correlation_id,
                    metadata={
                        "decision_id": decision.decision_id,
                        "status": decision.status.value,
                        "reason_code": decision.reason_code.value,
                        "tenant_id": decision.context.tenant_id,
                        "organization_id": decision.context.organization_id,
                        "action": decision.context.action,
                    },
                )
            except Exception as e:
                logger.warning("Failed to record SaaS authorization trace: %s", e)
