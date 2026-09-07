"""
Puertos de repositorio e interfaces de persistencia para el dominio Identity (Hito N.1).
"""

from abc import ABC, abstractmethod
from typing import Optional, Sequence
from src.domain.identity.models import Identity, IdentityType, IdentityStatus


class IdentityRepositoryPort(ABC):
    """
    Puerto de repositorio para el registro y consulta de identidades canónicas (N.1).
    """

    @abstractmethod
    def save_identity(self, identity: Identity) -> Identity:
        """
        Registra o actualiza una identidad de forma idempotente y atómica.
        Lanza excepción si existe conflicto semántico o colisión de identificadores.
        """
        pass

    @abstractmethod
    def get_identity(self, identity_id: str) -> Optional[Identity]:
        """
        Obtiene una identidad por su identity_id exacto.
        """
        pass

    @abstractmethod
    def find_by_canonical_identifier(self, canonical_identifier: str) -> Optional[Identity]:
        """
        Busca una identidad por su canonical_identifier exacto.
        """
        pass

    @abstractmethod
    def find_by_external_subject(self, provider: str, external_subject_id: str) -> Optional[Identity]:
        """
        Busca una identidad por su proveedor y sujeto externo.
        """
        pass

    @abstractmethod
    def list_identities(
        self,
        identity_type: Optional[IdentityType] = None,
        provider: Optional[str] = None,
        status: Optional[IdentityStatus] = None,
        limit: int = 100,
    ) -> Sequence[Identity]:
        """
        Lista identidades registradas aplicando filtros opcionales.
        """
        pass

    @abstractmethod
    def exists(self, identity_id: str) -> bool:
        """
        Verifica si existe una identidad con el identity_id especificado.
        """
        pass
