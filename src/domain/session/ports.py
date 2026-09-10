"""
Puertos de dominio para SaaS Authentication y Gestión Multi-Tenant de Sesiones (Hito O.3 — SaaS / Platformization).

Define:
- SaaSSessionRepositoryPort: Almacenamiento persistente, atómico, thread-safe y tenant-isolated para SaaSSession.
- SaaSSessionServicePort: Contrato para el ciclo de vida y validación de sesiones SaaS multi-tenant.
"""

from abc import ABC, abstractmethod
from typing import Optional, List, Sequence, Dict, Any
from datetime import datetime

from src.domain.session.models import (
    SaaSSession,
    SessionReference,
    SessionContext,
    SessionValidationResult,
    SessionStatus,
)
from src.domain.authentication.models import AuthenticationResult
from src.domain.tenant.models import TenantContext


class SaaSSessionRepositoryPort(ABC):
    """
    Puerto de repositorio para el almacenamiento seguro, crash-safe y aislado por tenant de sesiones SaaS.
    """

    @abstractmethod
    def save(self, session: SaaSSession) -> None:
        """
        Persiste o actualiza de forma atómica y crash-safe una SaaSSession.
        Asegura que el archivo se guarde en la partición adecuada de tenant y con checksum SHA-256 verificado.
        """
        pass

    @abstractmethod
    def get_by_id(self, session_id: str, tenant_id: Optional[str] = None) -> Optional[SaaSSession]:
        """
        Obtiene una sesión por su ID único.
        Si tenant_id es provisto, valida estrictamente que la sesión pertenezca a dicho tenant (prevención cross-tenant).
        """
        pass

    @abstractmethod
    def list_by_tenant(self, tenant_id: str) -> List[SaaSSession]:
        """
        Lista todas las sesiones registradas bajo un tenant específico.
        """
        pass

    @abstractmethod
    def list_by_identity(self, identity_id: str, tenant_id: Optional[str] = None) -> List[SaaSSession]:
        """
        Lista todas las sesiones de una identidad, opcionalmente filtradas por tenant.
        """
        pass

    @abstractmethod
    def delete(self, session_id: str, tenant_id: Optional[str] = None) -> bool:
        """
        Elimina físicamente una sesión si es necesario (generalmente se prefiere revocar).
        """
        pass


class SaaSSessionServicePort(ABC):
    """
    Puerto de servicio para la gestión del ciclo de vida y validación de sesiones SaaS.
    """

    @abstractmethod
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
        """
        pass

    @abstractmethod
    def validate_session(
        self,
        session_id: str,
        expected_tenant_id: Optional[str] = None,
        expected_organization_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> SessionValidationResult:
        """
        Valida exhaustivamente una sesión (existencia, estado, expiración determinista, pertenencia de tenant/org, integridad).
        """
        pass

    @abstractmethod
    def revoke_session(
        self,
        session_id: str,
        tenant_id: Optional[str] = None,
        reason: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> SaaSSession:
        """
        Revoca de forma persistente e irrevocable una sesión SaaS activa.
        """
        pass

    @abstractmethod
    def logout(
        self,
        session_id: str,
        tenant_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> SaaSSession:
        """
        Cierra la sesión SaaS (marcando como REVOKED) sin alterar las identidades ni membresías subyacentes.
        """
        pass

    @abstractmethod
    def get_session_context(
        self,
        session_id: str,
        expected_tenant_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> SessionContext:
        """
        Obtiene el SessionContext seguro downstream tras una validación exitosa.
        """
        pass
