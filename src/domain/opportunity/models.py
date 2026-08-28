from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional, List
from enum import Enum

from src.domain.market_intelligence.models import MarketEvidence, Confidence

class OpportunityReadiness(str, Enum):
    """
    Indica si la evidencia actual justifica continuar la evaluación del producto
    en las siguientes etapas (ej. Supplier Intelligence).
    """
    SUFFICIENT_EVIDENCE = "SUFFICIENT_EVIDENCE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    NOT_VIABLE = "NOT_VIABLE"

@dataclass(frozen=True)
class OpportunityDecision:
    """
    Representa la decisión parcial o final del motor de oportunidades tras evaluar la evidencia.
    En la arquitectura actual (MVP), no calcula score comercial sin Supplier Data y Economics.
    """
    evidence: MarketEvidence
    readiness: OpportunityReadiness
    reasons: List[str]
    opportunity_score: Optional[Decimal]
    confidence: Confidence
