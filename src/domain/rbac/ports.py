"""
Puertos de repositorio e interfaces para el dominio RBAC / Permissions (Hito N.4).
"""

from abc import ABC, abstractmethod
from typing import Optional, Sequence
from src.domain.rbac.models import Permission, Role, RoleAssignment


class RoleRepositoryPort(ABC):
    """
    Puerto de repositorio para la persistencia y consulta de Roles y Permisos.
    """

    @abstractmethod
    def save_role(self, role: Role) -> Role:
        """
        Registra o actualiza un rol de forma idempotente y atómica.
        Lanza excepción si existe conflicto semántico o corrupción de integridad.
        """
        pass

    @abstractmethod
    def get_role(self, role_id: str) -> Optional[Role]:
        """
        Obtiene un rol por su role_id exacto.
        """
        pass

    @abstractmethod
    def list_roles(self, limit: int = 100) -> Sequence[Role]:
        """
        Lista roles registrados.
        """
        pass

    @abstractmethod
    def exists(self, role_id: str) -> bool:
        """
        Verifica si existe un rol con el role_id especificado.
        """
        pass


class RoleAssignmentRepositoryPort(ABC):
    """
    Puerto de repositorio para la persistencia y consulta de asignaciones de roles a identidades.
    """

    @abstractmethod
    def save_assignment(self, assignment: RoleAssignment) -> RoleAssignment:
        """
        Registra una asignación de rol de forma idempotente y atómica.
        Lanza excepción si existe conflicto de integridad o colisión incompatible.
        """
        pass

    @abstractmethod
    def get_assignment(self, assignment_id: str) -> Optional[RoleAssignment]:
        """
        Obtiene una asignación por su assignment_id exacto.
        """
        pass

    @abstractmethod
    def list_assignments_for_identity(
        self,
        identity_id: str,
        scope: Optional[str] = None,
        limit: int = 100,
    ) -> Sequence[RoleAssignment]:
        """
        Lista todas las asignaciones activas o registradas para una identidad dada.
        """
        pass

    @abstractmethod
    def revoke_assignment(self, assignment_id: str) -> bool:
        """
        Revoca una asignación existente por su assignment_id.
        Retorna True si fue revocada/eliminada, False si no existía.
        """
        pass

    @abstractmethod
    def list_all_assignments(self, limit: int = 200) -> Sequence[RoleAssignment]:
        """
        Lista todas las asignaciones existentes.
        """
        pass
