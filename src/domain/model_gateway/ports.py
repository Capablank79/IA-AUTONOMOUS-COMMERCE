"""
Puertos de dominio para el Model Gateway SaaS (Hito O.5 — SaaS / Platformization).

Define los contratos de abstracción para:
- TenantModelConfigRepositoryPort: Almacenamiento y consulta de configuración por tenant.
- ModelProviderPort: Adaptador unificado para interactuar con proveedores LLM en la frontera de infraestructura.
- ModelGatewayServicePort: Contrato principal del servicio de aplicación Model Gateway SaaS.
"""

from abc import ABC, abstractmethod
from typing import Optional, Sequence, Mapping, Any, Dict

from src.domain.model_gateway.models import (
    TenantModelConfig,
    ModelGatewayRequest,
    ModelGatewayResponse,
    ProviderRequestReference,
)
from src.domain.secrets.models import SecretValue
from src.domain.model_routing.models import ModelRoute


class TenantModelConfigRepositoryPort(ABC):
    """Puerto para consultar la configuración de modelos y proveedores de un tenant."""

    @abstractmethod
    def get_config(self, tenant_id: str) -> Optional[TenantModelConfig]:
        """Obtiene la configuración de modelos y proveedores configurada para un tenant."""
        pass

    @abstractmethod
    def save_config(self, config: TenantModelConfig) -> None:
        """Guarda o actualiza la configuración de modelos para un tenant."""
        pass


class ModelProviderPort(ABC):
    """
    Puerto para adaptadores de proveedores LLM en la frontera de infraestructura.

    Recibe el SecretValue estrictamente en la frontera de ejecución sin propagarlo
    en modelos de dominio, trazas ni logs.
    """

    @abstractmethod
    def execute_inference(
        self,
        route: ModelRoute,
        prompt_payload: Any,
        secret: Optional[SecretValue] = None,
        tools: Optional[Sequence[str]] = None,
        temperature: float = 0.0,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Ejecuta la llamada de inferencia contra el proveedor especificado por la ruta.

        Retorna un diccionario estructurado con:
        - "content": str (texto generado)
        - "structured": Optional[dict]
        - "input_tokens": int
        - "output_tokens": int
        - "total_tokens": int
        - "provider_reference": ProviderRequestReference
        """
        pass


class ModelGatewayServicePort(ABC):
    """Puerto principal del servicio Model Gateway SaaS."""

    @abstractmethod
    def execute(self, request: ModelGatewayRequest) -> ModelGatewayResponse:
        """
        Orquesta el pipeline completo de inferencia gobernada para un tenant SaaS.
        """
        pass
