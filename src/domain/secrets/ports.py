"""
Puertos de dominio para Gestión de Secretos (Hito N.5 — Secret Management).

Define:
- SecretProviderPort: Interfaz para proveedores de material secreto específicos (Env, Injected, OAuth Bridge, Vault).
- SecretResolverPort: Interfaz para el servicio orquestador de resolución de secretos.
- SecretMetadataRepositoryPort: Interfaz de repositorio para la persistencia y auditoría de metadatos de secretos.
"""

from abc import ABC, abstractmethod
from typing import Optional, Sequence, Mapping, Any
from src.domain.secrets.models import (
    SecretReference,
    SecretValue,
    SecretMetadata,
    SecretResolutionResult,
    SecretType,
    SecretStatus,
)


class SecretProviderPort(ABC):
    """
    Puerto para un proveedor específico de secretos en memoria o entorno.
    """

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Nombre identificador del proveedor (ej. 'env', 'injected', 'mercadolibre_oauth')."""
        pass

    @abstractmethod
    def can_handle(self, reference: SecretReference) -> bool:
        """Determina si este proveedor puede resolver la referencia dada."""
        pass

    @abstractmethod
    def get_secret(self, reference: SecretReference) -> Optional[SecretValue]:
        """
        Obtiene el valor secreto envuelto en SecretValue.
        Retorna None si el secreto no está disponible en este proveedor.
        """
        pass


class SecretMetadataRepositoryPort(ABC):
    """
    Puerto de repositorio para el catálogo de metadatos y versionado de secretos.
    NUNCA almacena ni devuelve valores confidenciales.
    """

    @abstractmethod
    def save_metadata(self, metadata: SecretMetadata) -> SecretMetadata:
        """Registra o actualiza metadatos de un secreto de forma idempotente y atómica."""
        pass

    @abstractmethod
    def get_metadata(self, reference_id: str) -> Optional[SecretMetadata]:
        """Obtiene los metadatos de un secreto por su reference_id."""
        pass

    @abstractmethod
    def find_by_name(self, provider: str, secret_name: str) -> Optional[SecretMetadata]:
        """Busca metadatos por proveedor y nombre del secreto."""
        pass

    @abstractmethod
    def list_metadata(
        self,
        provider: Optional[str] = None,
        secret_type: Optional[SecretType] = None,
        status: Optional[SecretStatus] = None,
        limit: int = 100,
    ) -> Sequence[SecretMetadata]:
        """Lista metadatos de secretos con filtros opcionales."""
        pass

    @abstractmethod
    def delete_metadata(self, reference_id: str) -> bool:
        """Elimina metadatos de un secreto por reference_id."""
        pass


class SecretResolverPort(ABC):
    """
    Puerto para el servicio de resolución segura de secretos.
    Orquesta la resolución a través de SecretProviders registrados,
    valida metadatos y ciclo de vida, y genera resultados inmutables.
    """

    @abstractmethod
    def resolve(self, reference: SecretReference) -> SecretResolutionResult:
        """
        Resuelve una SecretReference de forma segura.
        Retorna SecretResolutionResult con SecretValue si fue exitosa, o estado de error explícito.
        """
        pass

    @abstractmethod
    def rotate_secret(
        self,
        reference_id: str,
        new_value: str,
        new_version: Optional[str] = None,
    ) -> SecretMetadata:
        """
        Registra una rotación de secreto para una referencia existente,
        actualizando la versión y los metadatos sin alterar la identidad del actor.
        """
        pass
