"""
Servicio de Aplicación para RBAC y Resolución de Permisos (Hito N.4).

Responsabilidades:
1. Responder: "¿Qué roles y permisos tiene asignados esta identidad y qué capacidades concretas representan?".
2. Resolver roles y permisos efectivos para una identidad canónica (o PrincipalContext de N.2).
3. Validar alcances (scope: marketplace, account, resource) de forma estricta.
4. Aplicar temporalidad determinista (expiración) mediante ClockPort (K.7).
5. Retornar un contexto inmutable de permisos (PermissionSet, RbacEvaluationResult) para alimentar a N.3 Authorization.
6. Auditar (K.1) y trazar (K.2) eventos RBAC (ROLE_ASSIGNED, ROLE_REVOKED, RBAC_EVALUATED) sin secretos.
7. DEFAULT DENY: identidades desconocidas, asignaciones expiradas, roles corruptos/inactivos o falta de asignación -> 0 permisos.
8. Prevención de escalada de privilegios: sin autoasignación ni asignaciones por metadata manipulada.

Fronteras estrictas:
- N.4 NO ejecuta acciones comerciales ni invoca ejecutores.
- N.4 NO duplica AuthorizationService (N.3); provee los permisos resueltos a N.3.
- N.4 NO implementa flujos de aprobación humana (Gate M) ni límites financieros (Hito E).
"""

from datetime import datetime, timezone
import logging
import uuid
from typing import Optional, Sequence, Mapping, Any, Union, Dict, Set, Tuple

from src.domain.identity.models import (
    IdentityReference,
    IdentityType,
    identity_to_audit_actor,
)
from src.domain.authentication.models import PrincipalContext
from src.domain.rbac.models import (
    Permission,
    Role,
    RoleAssignment,
    PermissionSet,
    RbacEvaluationResult,
    PermissionStatus,
    RoleStatus,
    normalize_action_token,
)
from src.domain.rbac.ports import RoleRepositoryPort, RoleAssignmentRepositoryPort
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


class RBACService:
    """
    Servicio de Aplicación para la gestión de roles, asignaciones y resolución
    de permisos efectivos en el Transversal N (Hito N.4).
    """

    def __init__(
        self,
        role_repository: RoleRepositoryPort,
        assignment_repository: RoleAssignmentRepositoryPort,
        audit_repository: Optional[AuditRepositoryPort] = None,
        trace_service: Optional[AgentTraceService] = None,
        clock: Optional[ClockPort] = None,
    ):
        self.role_repository = role_repository
        self.assignment_repository = assignment_repository
        self.audit_repository = audit_repository
        self.trace_service = trace_service
        self.clock = clock

    def _now(self) -> datetime:
        """Obtiene el timestamp UTC actual respetando el ClockPort inyectado (K.7)."""
        if self.clock:
            now_dt = self.clock.now()
            if now_dt.tzinfo is None:
                return now_dt.replace(tzinfo=timezone.utc)
            return now_dt
        return datetime.now(timezone.utc)

    # -------------------------------------------------------------------------
    # Gestión de Roles y Permisos (Catálogo explícito)
    # -------------------------------------------------------------------------

    def create_permission(
        self,
        permission_id: str,
        action: str,
        resource_scope: Optional[str] = None,
        description: Optional[str] = None,
        status: Union[PermissionStatus, str] = PermissionStatus.ACTIVE,
        policy_version: str = "1.0.0",
    ) -> Permission:
        """Crea una instancia inmutable de Permission."""
        return Permission(
            permission_id=permission_id,
            action=action,
            resource_scope=resource_scope,
            description=description,
            status=status if isinstance(status, PermissionStatus) else PermissionStatus(status),
            policy_version=policy_version,
        )

    def define_role(
        self,
        role_id: str,
        name: str,
        permissions: Sequence[Permission] = (),
        status: Union[RoleStatus, str] = RoleStatus.ACTIVE,
        description: Optional[str] = None,
        policy_version: str = "1.0.0",
    ) -> Role:
        """
        Registra o actualiza un rol de forma idempotente en el repositorio.
        """
        role = Role(
            role_id=role_id,
            name=name,
            permissions=tuple(permissions),
            status=status if isinstance(status, RoleStatus) else RoleStatus(status),
            description=description,
            policy_version=policy_version,
        )
        return self.role_repository.save_role(role)

    def get_role(self, role_id: str) -> Optional[Role]:
        """Obtiene un rol por su identificador exacto."""
        return self.role_repository.get_role(role_id)

    # -------------------------------------------------------------------------
    # Gestión de Asignaciones de Roles a Identidades
    # -------------------------------------------------------------------------

    def assign_role(
        self,
        identity_id: str,
        role_id: str,
        scope: Optional[str] = None,
        expires_at: Optional[datetime] = None,
        assignment_id: Optional[str] = None,
        source: str = "RBAC_SERVICE",
        metadata: Optional[Mapping[str, Any]] = None,
        correlation_id: Optional[str] = None,
    ) -> RoleAssignment:
        """
        Asigna explícita y deterministicamente un rol a una identidad.
        Verifica que el rol exista antes de persistir la asignación.
        """
        validate_safe_identifier(identity_id, field_name="identity_id")
        validate_safe_identifier(role_id, field_name="role_id")

        role = self.role_repository.get_role(role_id)
        if not role:
            raise ValueError(f"Cannot assign non-existent role '{role_id}' to identity '{identity_id}'.")

        now = self._now()
        aid = assignment_id or f"asgn_{uuid.uuid4().hex[:12]}"
        validate_safe_identifier(aid, field_name="assignment_id")

        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)

        assignment = RoleAssignment(
            assignment_id=aid,
            identity_id=identity_id,
            role_id=role_id,
            scope=scope,
            assigned_at=now,
            expires_at=expires_at,
            source=source,
            metadata=dict(metadata or {}),
        )

        saved = self.assignment_repository.save_assignment(assignment)
        self._record_assignment_audit(saved, action="ASSIGN", correlation_id=correlation_id)
        return saved

    def revoke_assignment(
        self,
        assignment_id: str,
        correlation_id: Optional[str] = None,
    ) -> bool:
        """
        Revoca una asignación de rol existente por su identificador.
        """
        validate_safe_identifier(assignment_id, field_name="assignment_id")
        existing = self.assignment_repository.get_assignment(assignment_id)
        if not existing:
            return False

        revoked = self.assignment_repository.revoke_assignment(assignment_id)
        if revoked:
            self._record_assignment_audit(existing, action="REVOKE", correlation_id=correlation_id)
        return revoked

    # -------------------------------------------------------------------------
    # Resolución de Permisos Efectivos (Alimenta a N.3 Authorization)
    # -------------------------------------------------------------------------

    def resolve_effective_permissions(
        self,
        principal_or_identity: Union[PrincipalContext, IdentityReference, str],
        scope: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> RbacEvaluationResult:
        """
        Resuelve los roles y permisos efectivos para una identidad en un scope determinado.

        Reglas:
        - Si la identidad es UNKNOWN, no autenticada o ausente -> 0 permisos.
        - Asignaciones expiradas respecto a ClockPort (now) -> ignoradas.
        - Asignaciones con scope incompatible con el scope consultado -> ignoradas.
        - Roles inactivos (DISABLED, DEPRECATED, UNKNOWN) -> ignorados.
        - Permisos inactivos dentro de un rol -> ignorados.
        - Permisos con resource_scope incompatible con el scope consultado -> ignorados.
        - Unión determinista y deduplicada de permisos de todos los roles válidos.
        """
        now = self._now()
        corr_id = correlation_id or f"rbac_eval_{uuid.uuid4().hex[:12]}"

        # 1. Extraer identity_id y verificar autenticidad si es PrincipalContext
        identity_id: Optional[str] = None
        if isinstance(principal_or_identity, PrincipalContext):
            if not principal_or_identity.is_authenticated:
                return self._empty_result(
                    identity_id=principal_or_identity.identity_id or "unauthenticated",
                    scope=scope,
                    now=now,
                    correlation_id=corr_id,
                )
            identity_id = principal_or_identity.identity_id
            if principal_or_identity.principal.identity_type == IdentityType.UNKNOWN:
                return self._empty_result(
                    identity_id=identity_id,
                    scope=scope,
                    now=now,
                    correlation_id=corr_id,
                )
        elif isinstance(principal_or_identity, IdentityReference):
            if principal_or_identity.identity_type == IdentityType.UNKNOWN:
                return self._empty_result(
                    identity_id=principal_or_identity.identity_id,
                    scope=scope,
                    now=now,
                    correlation_id=corr_id,
                )
            identity_id = principal_or_identity.identity_id
        elif isinstance(principal_or_identity, str):
            identity_id = principal_or_identity.strip()
        else:
            raise ValueError(f"Unsupported principal/identity type: {type(principal_or_identity)}")

        if not identity_id:
            return self._empty_result(
                identity_id="unknown_identity",
                scope=scope,
                now=now,
                correlation_id=corr_id,
            )

        validate_safe_identifier(identity_id, field_name="identity_id")

        # 2. Consultar asignaciones de la identidad
        assignments = self.assignment_repository.list_assignments_for_identity(
            identity_id=identity_id,
            scope=scope,
        )

        effective_roles: Set[str] = set()
        collected_permissions: Dict[str, Permission] = {}

        for asgn in assignments:
            # Validar expiración temporal (K.7)
            if asgn.is_expired(now):
                continue

            # Validar compatibilidad de scope de la asignación
            if not asgn.applies_to_scope(scope):
                continue

            # Cargar rol asociado
            role = self.role_repository.get_role(asgn.role_id)
            if not role or not role.is_active:
                continue

            effective_roles.add(role.role_id)

            # Acumular permisos activos que apliquen al scope
            for perm in role.permissions:
                if not perm.is_active:
                    continue
                if not perm.applies_to_scope(scope):
                    continue
                # Deduplicar por permission_id
                collected_permissions[perm.permission_id] = perm

        # Orden determinista de permisos por permission_id
        sorted_permissions = tuple(
            collected_permissions[pid]
            for pid in sorted(collected_permissions.keys())
        )

        result = RbacEvaluationResult(
            identity_id=identity_id,
            effective_permissions=PermissionSet(permissions=sorted_permissions),
            roles=tuple(sorted(effective_roles)),
            evaluated_at=now,
            scope=scope,
            correlation_id=corr_id,
        )

        self._record_evaluation_audit_and_trace(result)
        return result

    def has_permission(
        self,
        principal_or_identity: Union[PrincipalContext, IdentityReference, str],
        action: str,
        scope: Optional[str] = None,
    ) -> bool:
        """
        Verifica de forma rápida si una identidad cuenta con un permiso específico para una acción.
        """
        eval_result = self.resolve_effective_permissions(
            principal_or_identity=principal_or_identity,
            scope=scope,
        )
        normalized_target = normalize_action_token(action)
        return normalized_target in eval_result.actions

    def _empty_result(
        self,
        identity_id: str,
        scope: Optional[str],
        now: datetime,
        correlation_id: str,
    ) -> RbacEvaluationResult:
        """Genera un resultado vacío seguro (DEFAULT DENY)."""
        safe_id = identity_id if identity_id.replace("_", "").isalnum() else "unknown_identity"
        return RbacEvaluationResult(
            identity_id=safe_id,
            effective_permissions=PermissionSet(),
            roles=(),
            evaluated_at=now,
            scope=scope,
            correlation_id=correlation_id,
        )

    # -------------------------------------------------------------------------
    # Auditoría (K.1) y Trazas (K.2)
    # -------------------------------------------------------------------------

    def _record_assignment_audit(
        self,
        assignment: RoleAssignment,
        action: str,
        correlation_id: Optional[str],
    ) -> None:
        if not self.audit_repository:
            return

        rec_type = AuditRecordType.ROLE_ASSIGNED if action == "ASSIGN" else AuditRecordType.ROLE_REVOKED
        now = self._now()
        corr_id = correlation_id or str(uuid.uuid4())
        actor = AuditActor(
            actor_type=AuditActorType.SYSTEM,
            actor_id="rbac_service",
        )

        base_meta = {
            "assignment_id": assignment.assignment_id,
            "identity_id": assignment.identity_id,
            "role_id": assignment.role_id,
            "scope": assignment.scope,
            "expires_at": assignment.expires_at.isoformat() if assignment.expires_at else None,
            "source": assignment.source,
        }
        if assignment.metadata:
            base_meta.update(dict(assignment.metadata))

        safe_metadata = sanitize_security_data(base_meta)

        record = AuditRecord(
            audit_id=f"audit_rbac_{uuid.uuid4().hex[:12]}",
            record_type=rec_type,
            occurred_at=now,
            actor=actor,
            subject_type="RoleAssignment",
            subject_id=assignment.assignment_id,
            action_or_operation=f"RBAC_{action}",
            status="SUCCESS",
            correlation_id=corr_id,
            metadata=safe_metadata,
        )

        try:
            self.audit_repository.append(record)
        except Exception as e:
            logger.warning("Failed to record RBAC assignment audit record: %s", e)

    def _record_evaluation_audit_and_trace(self, result: RbacEvaluationResult) -> None:
        now = self._now()
        corr_id = result.correlation_id or str(uuid.uuid4())

        # 1. Audit Trail (K.1)
        if self.audit_repository:
            actor = AuditActor(
                actor_type=AuditActorType.SYSTEM,
                actor_id="rbac_service",
            )
            safe_meta = sanitize_security_data({
                "identity_id": result.identity_id,
                "roles": list(result.roles),
                "effective_permission_count": len(result.permission_ids),
                "scope": result.scope,
                "checksum": result.checksum,
            })
            record = AuditRecord(
                audit_id=f"audit_rbac_eval_{uuid.uuid4().hex[:12]}",
                record_type=AuditRecordType.RBAC_EVALUATED,
                occurred_at=now,
                actor=actor,
                subject_type="Identity",
                subject_id=result.identity_id,
                action_or_operation="RESOLVE_PERMISSIONS",
                status="SUCCESS",
                correlation_id=corr_id,
                metadata=safe_meta,
            )
            try:
                self.audit_repository.append(record)
            except Exception as e:
                logger.warning("Failed to record RBAC evaluation audit: %s", e)

        # 2. Agent Trace (K.2)
        if self.trace_service:
            trace_input = sanitize_security_data({
                "identity_id": result.identity_id,
                "scope": result.scope,
            })
            trace_output = sanitize_security_data({
                "roles": list(result.roles),
                "effective_actions": list(result.actions),
                "checksum": result.checksum,
            })
            try:
                self.trace_service.record_step(
                    component_name="RBACService",
                    execution_id=corr_id,
                    step_number=1,
                    step_type=StepType.POLICY_EVALUATION,
                    operation="RBAC_PERMISSIONS_RESOLUTION",
                    status=TraceStatus.SUCCESS,
                    input_reference=None,
                    output_reference=None,
                    correlation_id=corr_id,
                    mission_id=corr_id,
                    metadata={
                        "input": trace_input,
                        "output": trace_output,
                    },
                )
            except Exception as e:
                logger.warning("Failed to record RBAC agent trace step: %s", e)
