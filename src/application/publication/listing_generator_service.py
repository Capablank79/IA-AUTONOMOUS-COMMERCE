import uuid
from typing import Dict, Any, Optional
from types import MappingProxyType

from src.domain.publication.ports import ListingGeneratorPort
from src.domain.publication.generation_models import (
    ListingGenerationInput,
    ListingGenerationResult,
)
from src.domain.publication.services import DeterministicListingGenerator


class ListingDraftGeneratorService:
    """
    Servicio de aplicación para coordinar la generación de ListingDraft comerciales (G.1 / TASK 07.1).
    Integra la entrada estructurada (producto, evidencia de mercado, dolores de cliente, SEO y restricciones)
    y orquesta el motor generador respetando los límites de G.1 (Generación) vs G.2 (Validación de Calidad/Políticas).
    """

    def __init__(self, generator: Optional[ListingGeneratorPort] = None):
        self.generator = generator or DeterministicListingGenerator()

    def generate_listing_draft(self, input_data: ListingGenerationInput) -> ListingGenerationResult:
        """
        Genera el ListingDraft estructurado y enriquecido con fundamentación factual.
        """
        if input_data is None:
            raise ValueError("input_data cannot be None")

        result = self.generator.generate(input_data)
        return result
