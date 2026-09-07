"""
Domain Ports for N.9 — Sensitive Data Handling (Transversal N — Security, Governance & Safety).

Define los contratos abstractos para:
- SensitiveDataClassifierPort: Clasificación determinista de estructuras y payloads.
- SensitiveDataRedactorPort: Redacción recursiva y minimización de datos sensibles.
- DataHandlingPolicyRepositoryPort: Almacenamiento persistente de políticas de manejo de datos.
- SensitiveDataHandlingServicePort: Servicio orquestador central de N.9.
"""

from abc import ABC, abstractmethod
from typing import Optional, Any, Sequence, Dict

from src.domain.security.sensitive_data_models import (
    SensitiveDataClassification,
    DataHandlingPolicy,
    DataHandlingRequest,
    DataHandlingDecision,
    RedactionResult,
    DataHandlingPurpose,
)


class SensitiveDataClassifierPort(ABC):
    """
    Puerto para clasificar deterministamente estructuras de datos en base a su contenido y campos.
    """

    @abstractmethod
    def classify(self, data: Any, context_purpose: Optional[DataHandlingPurpose] = None) -> SensitiveDataClassification:
        """
        Analiza recursivamente la estructura y retorna su clasificación de sensibilidad global y detallada.
        """
        pass


class SensitiveDataRedactorPort(ABC):
    """
    Puerto para aplicar redacción, minimización y transformación segura sobre payloads.
    """

    @abstractmethod
    def redact(
        self,
        data: Any,
        policy: DataHandlingPolicy,
        purpose: DataHandlingPurpose,
        required_fields: Sequence[str] = (),
    ) -> RedactionResult:
        """
        Aplica redacción recursiva respetando la política, el propósito operativo y la minimización.
        """
        pass


class DataHandlingPolicyRepositoryPort(ABC):
    """
    Puerto secundario para la persistencia y recuperación de DataHandlingPolicy.
    """

    @abstractmethod
    def save_policy(self, policy: DataHandlingPolicy) -> None:
        """Guarda o actualiza una política de manejo de datos de forma atómica."""
        pass

    @abstractmethod
    def get_policy(self, policy_name: str) -> Optional[DataHandlingPolicy]:
        """Obtiene una política por su nombre canónico."""
        pass

    @abstractmethod
    def list_policies(self) -> Sequence[DataHandlingPolicy]:
        """Lista todas las políticas de manejo de datos disponibles."""
        pass


class SensitiveDataHandlingServicePort(ABC):
    """
    Puerto primario del servicio de aplicación de manejo de datos sensibles (N.9).
    """

    @abstractmethod
    def evaluate(self, request: DataHandlingRequest) -> DataHandlingDecision:
        """
        Clasifica, evalúa políticas de transferencia/propagación y aplica redacción/minimización determinista.
        """
        pass

    @abstractmethod
    def sanitize_for_audit(self, payload: Any, correlation_id: str = "") -> Any:
        """
        Helper de sanitización directa para registros de auditoría (K.1) y trazas (K.2).
        """
        pass

    @abstractmethod
    def sanitize_for_cache(self, payload: Any, policy_name: Optional[str] = None) -> Any:
        """
        Helper de sanitización y validación de cacheabilidad para M.4 Caching.
        """
        pass

    @abstractmethod
    def sanitize_for_inference(self, prompt_or_context: Any, required_business_fields: Sequence[str] = ()) -> Any:
        """
        Helper de minimización y redacción previo al despacho hacia proveedores de LLM/inferencia.
        """
        pass
