from typing import Optional, Dict, Any, Mapping
from types import MappingProxyType

from src.domain.publication.ports import ListingValidatorPort
from src.domain.publication.validation_models import (
    ListingValidationContext,
    ListingValidationResult,
)
from src.domain.publication.validation_engine import DeterministicListingValidator


class ListingQualityValidatorService:
    """
    Servicio de aplicación para la validación formal de calidad, factualidad y políticas
    de publicaciones comerciales (G.2 / TASK 07.2).
    
    Barrera de control previa a la gobernanza y publicación:
    ListingDraft -> ListingQualityValidatorService -> ListingValidationResult
    """

    def __init__(self, validator: Optional[ListingValidatorPort] = None):
        self.validator = validator or DeterministicListingValidator()

    def validate_listing(self, context: ListingValidationContext) -> ListingValidationResult:
        """
        Ejecuta la validación determinista del borrador de publicación.
        """
        if context is None:
            raise ValueError("context cannot be None")
        if context.draft is None:
            raise ValueError("context.draft cannot be None")

        return self.validator.validate(context)
