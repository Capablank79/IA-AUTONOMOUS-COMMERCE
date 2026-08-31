from typing import Protocol, Optional, Sequence, runtime_checkable
from .models import (
    ListingDraft,
    PublicationRequest,
    PublicationResult,
    SalesChannel,
)
from .generation_models import (
    ListingGenerationInput,
    ListingGenerationResult,
)
from .validation_models import (
    ListingValidationContext,
    ListingValidationResult,
)


@runtime_checkable
class ListingValidatorPort(Protocol):
    """
    Puerto primario de dominio para la validación formal de calidad, factualidad y políticas
    de un ListingDraft (G.2 / TASK 07.2).
    """
    def validate(self, context: ListingValidationContext) -> ListingValidationResult:
        """
        Evalúa el borrador comercial contra verdades del producto, evidencia y políticas de canal.
        """
        ...


@runtime_checkable
class ListingGeneratorPort(Protocol):
    """
    Puerto primario de dominio para la generación estructurada y fundamentada de ListingDraft.
    Transforma evidencia comercial, señales de mercado, dolores de cliente y especificaciones
    en contenido listo para validación y publicación.
    """
    def generate(self, input_data: ListingGenerationInput) -> ListingGenerationResult:
        """
        Genera un ListingDraft enriquecido y estructurado a partir del input y la evidencia provista.
        """
        ...


@runtime_checkable
class PublicationPort(Protocol):
    """
    Puerto primario de dominio para la publicación comercial en canales de venta.
    Desacoplado de cualquier marketplace concreto (Mercado Libre, Amazon, Shopify, etc.),
    llamadas HTTP, SDKs o DTOs externos.
    """
    def publish(self, request: PublicationRequest) -> PublicationResult:
        """
        Ejecuta la intención de publicación en el canal correspondiente.
        Devuelve PublicationResult con status PUBLISHED, FAILED o UNKNOWN.
        """
        ...

    def get_status(self, channel: SalesChannel, external_reference: str) -> PublicationResult:
        """
        Consulta o verifica el estado de una publicación externa.
        Permite recuperar publicaciones en estado UNKNOWN de forma idempotente y segura.
        """
        ...


@runtime_checkable
class PublicationRepository(Protocol):
    """
    Puerto secundario para la persistencia y auditoría de borradores y publicaciones.
    """
    def save_draft(self, draft: ListingDraft) -> None:
        """Guarda o actualiza un borrador de publicación."""
        ...

    def get_draft(self, draft_id: str) -> Optional[ListingDraft]:
        """Obtiene un borrador de publicación por su ID."""
        ...

    def save_result(self, result: PublicationResult) -> None:
        """Guarda o actualiza el resultado de una publicación."""
        ...

    def get_result_by_id(self, publication_id: str) -> Optional[PublicationResult]:
        """Obtiene el resultado de una publicación por su ID interno."""
        ...

    def get_result_by_external_reference(
        self, channel_id: str, external_reference: str
    ) -> Optional[PublicationResult]:
        """Obtiene el resultado de una publicación por su identificador externo en el canal."""
        ...
