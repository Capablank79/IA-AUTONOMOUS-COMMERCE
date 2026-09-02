"""
Puertos de dominio para Chequeos de Seguridad (Security Checks - Hito K.8).

Define:
- SecurityCheckPort: Interfaz para evaluadores o verificadores específicos de seguridad.
- SecurityCheckServicePort: Interfaz de alto nivel para el orquestador de seguridad.
"""

from abc import ABC, abstractmethod
from typing import Optional, Sequence, Mapping, Any

from src.domain.security.models import (
    SecurityCheckResult,
    SecurityCheckEvaluation,
)


class SecurityCheckPort(ABC):
    """
    Puerto para un verificador de seguridad unitario o categórico.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Nombre del chequeo."""
        pass

    @property
    @abstractmethod
    def category(self) -> str:
        """Categoría del chequeo."""
        pass

    @abstractmethod
    def check(
        self,
        target: str,
        context: Mapping[str, Any],
        correlation_id: Optional[str] = None,
    ) -> SecurityCheckResult:
        """Ejecuta la comprobación de seguridad."""
        pass


class SecurityCheckServicePort(ABC):
    """
    Puerto para el servicio transversal de comprobación de seguridad.
    """

    @abstractmethod
    def evaluate_action_security(
        self,
        action_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
        correlation_id: Optional[str] = None,
        context_metadata: Optional[Mapping[str, Any]] = None,
    ) -> SecurityCheckEvaluation:
        """Evalúa integralmente la seguridad de una acción propuesta antes de side-effects."""
        pass

    @abstractmethod
    def validate_path_safety(
        self,
        path_or_identifier: str,
        field_name: str = "path",
    ) -> SecurityCheckResult:
        """Valida que un path o identificador no contenga secuencias de path traversal."""
        pass

    @abstractmethod
    def validate_payload_safety(
        self,
        payload: Mapping[str, Any],
        target: str = "payload",
        correlation_id: Optional[str] = None,
    ) -> SecurityCheckResult:
        """Valida que un payload no contenga inyecciones de datos no confiables ni secretos desprotegidos."""
        pass
