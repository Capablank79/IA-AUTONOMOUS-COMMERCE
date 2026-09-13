"""
Puertos de dominio para Rate-limit Management (Hito P.11 — Production / Operations).

Define:
- RateLimitPolicyRepositoryPort: Almacenamiento y recuperación de políticas de rate limit (Platform y Tenant scoped).
- RateLimitStateStorePort: Store atómico y thread-safe / distribuido para el estado del token bucket por clave canónica.
- RateLimitServicePort: Interfaz principal para pre-flight check y consumo de tokens.
- RateLimitTelemetryPort: Emisión desacoplada de telemetría a Monitoreo (P.7) y Alertas (P.8).
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional, List, Sequence, Mapping, Any, Tuple

from src.domain.tenant.models import TenantContext
from .models import (
    RateLimitPolicy,
    RateLimitRequest,
    RateLimitDecision,
    RateLimitState,
    RateLimitScope,
)


class RateLimitPolicyRepositoryPort(ABC):
    """Puerto de persistencia para políticas de Rate Limit."""

    @abstractmethod
    def get_policy(self, tenant_id: Optional[str] = None) -> Optional[RateLimitPolicy]:
        """Obtiene la política activa para un tenant o la global si tenant_id es None."""
        pass

    @abstractmethod
    def save_policy(self, policy: RateLimitPolicy) -> None:
        """Guarda o actualiza una política de rate limit."""
        pass

    @abstractmethod
    def delete_policy(self, tenant_id: Optional[str] = None) -> bool:
        """Elimina una política de rate limit."""
        pass


class RateLimitStateStorePort(ABC):
    """
    Puerto de backend para estados de Token Bucket.
    Garantiza atomicidad y prevención estricta de sobreconsumo concurrente (TOCTOU).
    """

    @abstractmethod
    def get_state(self, key: str) -> Optional[RateLimitState]:
        """Recupera el estado actual del bucket para la clave canónica."""
        pass

    @abstractmethod
    def update_state(self, state: RateLimitState) -> None:
        """Guarda o actualiza el estado del bucket."""
        pass

    @abstractmethod
    def consume_token(
        self,
        key: str,
        cost_units: int,
        refill_rate_per_sec: float,
        burst_capacity: int,
        now: datetime,
    ) -> Tuple[bool, float, int]:
        """
        Operación atómica de rellenado y consumo de tokens.
        Retorna:
        - allowed: bool (True si había suficientes tokens y se consumieron)
        - remaining_tokens: float (tokens restantes tras la operación)
        - retry_after_seconds: int (segundos necesarios para tener tokens si fue denegado, o 0 si allowed)
        """
        pass

    @abstractmethod
    def reset_key(self, key: str) -> None:
        """Resetea o limpia el estado de una clave específica."""
        pass

    @abstractmethod
    def clear_all(self) -> None:
        """Limpia todo el estado en memoria/store (utilizado en testing y reseteo de entorno)."""
        pass


class RateLimitTelemetryPort(ABC):
    """Puerto de emisión desacoplada de telemetría técnica hacia P.7 (Monitoring) y P.8 (Alerting)."""

    @abstractmethod
    def emit_rate_limit_fact(
        self,
        decision: RateLimitDecision,
        request: RateLimitRequest,
    ) -> None:
        """Emite hechos estructurados sin PII ni secretos."""
        pass


class RateLimitServicePort(ABC):
    """Contrato del servicio principal de Rate-limit Management (P.11)."""

    @abstractmethod
    def check_and_consume(
        self,
        request: RateLimitRequest,
        context: Optional[TenantContext] = None,
    ) -> RateLimitDecision:
        """
        Evalúa el pre-flight rate limit check y consume tokens de forma atómica si se permite.
        """
        pass

    @abstractmethod
    def check_only(
        self,
        request: RateLimitRequest,
        context: Optional[TenantContext] = None,
    ) -> RateLimitDecision:
        """
        Inspección idempotente de rate limit sin consumir tokens (read-only).
        """
        pass
