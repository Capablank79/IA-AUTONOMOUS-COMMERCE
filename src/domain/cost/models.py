"""
Modelos de dominio para el Registro y Medición de Costes Operacionales (Cost Tracking - Hito K.3).

Define:
- CostType: Taxonomía canónica de costos operacionales (INFERENCE, TOOL_CALL, EXTERNAL_API, COMPUTE_OPERATIONAL).
- UsageUnit: Unidades canónicas de consumo (TOKENS, REQUESTS, SECONDS, UNKNOWN).
- UsageRecord: Modelo inmutable normalizado de uso real/observable (input_tokens, output_tokens, total_tokens, request_count, etc.).
- PricingRate: Tarifa unitaria inmutable para un modelo/servicio/herramienta.
- CostRecord: Entidad inmutable de dominio que registra la medición económica de una operación.
- CurrencyCostSummary: Agregado inmutable que agrupa costos para una moneda específica.
- CostSummary: Agregado inmutable que resume los costos de una misión, ejecución o ciclo separando por moneda y contabilizando UNKNOWN.

Principios K.3:
- Inmutabilidad estricta (frozen=True, MappingProxyType).
- Cost Tracking MIDE costos: WHAT, WHO, FOR WHICH MISSION/EXECUTION, WHEN, HOW MUCH, CURRENCY, SOURCE.
- Dinero en Decimal, nunca float.
- Distinción estricta: ZERO_COST vs UNKNOWN_COST (UNKNOWN != 0.00).
- Separación de monedas (Multi-currency safe, sin conversiones FX implícitas).
- Sanitización recursiva de secretos.
- Idempotencia estricta por (execution_id, trace_id, cost_type) o idempotency_key determinista.
- NO optimiza, NO cachea, NO impone presupuestos, NO cambia modelos (eso es Hito M).
- NO implementa K.4-K.8 ni Gate J.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Optional, Any, Tuple, Dict, Union, List
import hashlib


SENSITIVE_KEYS = {
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "pan",
    "cvv",
    "private_key",
    "credential",
    "access_token",
    "refresh_token",
    "authorization",
    "chain_of_thought",
    "reasoning",
    "reasoning_tokens",
    "internal_scratchpad",
    "card_number",
}


def _sanitize_cost_metadata(val: Any) -> Any:
    """Sanitiza recursivamente metadatos para eliminar cualquier secreto o PII."""
    if isinstance(val, (dict, MappingProxyType)):
        cleaned = {}
        for k, v in val.items():
            k_str = str(k).lower()
            if any(s in k_str for s in SENSITIVE_KEYS):
                cleaned[str(k)] = "[REDACTED]"
            else:
                cleaned[str(k)] = _sanitize_cost_metadata(v)
        return cleaned
    if isinstance(val, (list, tuple)):
        return [_sanitize_cost_metadata(v) for v in val]
    return val


class CostType(str, Enum):
    """
    Taxonomía canónica de tipos de costos operacionales del sistema.
    """
    INFERENCE = "INFERENCE"
    TOOL_CALL = "TOOL_CALL"
    EXTERNAL_API = "EXTERNAL_API"
    COMPUTE_OPERATIONAL = "COMPUTE_OPERATIONAL"


class UsageUnit(str, Enum):
    """
    Unidades canónicas de medición de consumo.
    """
    TOKENS = "TOKENS"
    REQUESTS = "REQUESTS"
    SECONDS = "SECONDS"
    ITEMS = "ITEMS"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class UsageRecord:
    """
    Representación inmutable y normalizada del uso/consumo real observable.
    Si el proveedor no entrega tokens/consumo, se preserva None/UNKNOWN sin inventar datos.
    """
    unit: UsageUnit = UsageUnit.UNKNOWN
    input_quantity: Optional[Decimal] = None
    output_quantity: Optional[Decimal] = None
    total_quantity: Optional[Decimal] = None
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.unit, UsageUnit):
            try:
                object.__setattr__(self, "unit", UsageUnit(self.unit))
            except Exception as e:
                raise ValueError(f"Invalid usage unit: {self.unit}") from e

        if self.input_quantity is not None and not isinstance(self.input_quantity, Decimal):
            object.__setattr__(self, "input_quantity", Decimal(str(self.input_quantity)))
        if self.output_quantity is not None and not isinstance(self.output_quantity, Decimal):
            object.__setattr__(self, "output_quantity", Decimal(str(self.output_quantity)))
        if self.total_quantity is not None and not isinstance(self.total_quantity, Decimal):
            object.__setattr__(self, "total_quantity", Decimal(str(self.total_quantity)))

        # Derivar total si no está presente pero hay input o output
        if self.total_quantity is None:
            if self.input_quantity is not None and self.output_quantity is not None:
                object.__setattr__(self, "total_quantity", self.input_quantity + self.output_quantity)
            elif self.input_quantity is not None and self.output_quantity is None:
                object.__setattr__(self, "total_quantity", self.input_quantity)
            elif self.output_quantity is not None and self.input_quantity is None:
                object.__setattr__(self, "total_quantity", self.output_quantity)

        if not isinstance(self.details, MappingProxyType):
            sanitized = _sanitize_cost_metadata(dict(self.details))
            object.__setattr__(self, "details", MappingProxyType(sanitized))

    @classmethod
    def from_tokens(
        cls,
        prompt_tokens: Optional[int] = None,
        completion_tokens: Optional[int] = None,
        total_tokens: Optional[int] = None,
        raw_usage: Optional[Dict[str, Any]] = None,
    ) -> "UsageRecord":
        """Factory para consumos basados en tokens LLM/inferencia."""
        in_qty = Decimal(str(prompt_tokens)) if prompt_tokens is not None else None
        out_qty = Decimal(str(completion_tokens)) if completion_tokens is not None else None
        tot_qty = Decimal(str(total_tokens)) if total_tokens is not None else None

        # Si viene raw_usage dictionary con nomenclaturas alternativas
        if raw_usage and in_qty is None and out_qty is None and tot_qty is None:
            raw_in = raw_usage.get("prompt_tokens") or raw_usage.get("input_tokens")
            raw_out = raw_usage.get("completion_tokens") or raw_usage.get("output_tokens")
            raw_tot = raw_usage.get("total_tokens")
            if raw_in is not None:
                in_qty = Decimal(str(raw_in))
            if raw_out is not None:
                out_qty = Decimal(str(raw_out))
            if raw_tot is not None:
                tot_qty = Decimal(str(raw_tot))

        unit = UsageUnit.TOKENS if (in_qty is not None or out_qty is not None or tot_qty is not None) else UsageUnit.UNKNOWN
        return cls(
            unit=unit,
            input_quantity=in_qty,
            output_quantity=out_qty,
            total_quantity=tot_qty,
            details=raw_usage or {},
        )

    @classmethod
    def from_requests(cls, request_count: int = 1, details: Optional[Dict[str, Any]] = None) -> "UsageRecord":
        """Factory para consumos basados en peticiones de API / tools."""
        return cls(
            unit=UsageUnit.REQUESTS,
            total_quantity=Decimal(str(request_count)),
            details=details or {},
        )

    @classmethod
    def unknown(cls, details: Optional[Dict[str, Any]] = None) -> "UsageRecord":
        """Representa consumo desconocido o no observable."""
        return cls(unit=UsageUnit.UNKNOWN, details=details or {})


@dataclass(frozen=True)
class PricingRate:
    """
    Tarifa inmutable para un proveedor, servicio o modelo.
    Soporta pricing por token (input/output) o pricing por unidad/request.
    """
    provider: str
    service_or_model: str
    currency: str = "USD"
    input_rate: Optional[Decimal] = None       # Precio por unidad de input (ej. por token o por 1M tokens)
    output_rate: Optional[Decimal] = None      # Precio por unidad de output (ej. por token o por 1M tokens)
    flat_rate: Optional[Decimal] = None        # Precio fijo por request / invocación
    rate_scale: Decimal = Decimal("1")         # Escala de la tasa (1, 1000, 1000000)
    version: str = "1.0.0"
    effective_from: Optional[datetime] = None
    effective_to: Optional[datetime] = None

    def __post_init__(self):
        if not self.provider:
            raise ValueError("provider must be a non-empty string")
        if not self.service_or_model:
            raise ValueError("service_or_model must be a non-empty string")
        if not self.currency:
            raise ValueError("currency must be a non-empty string")

        if self.input_rate is not None and not isinstance(self.input_rate, Decimal):
            object.__setattr__(self, "input_rate", Decimal(str(self.input_rate)))
        if self.output_rate is not None and not isinstance(self.output_rate, Decimal):
            object.__setattr__(self, "output_rate", Decimal(str(self.output_rate)))
        if self.flat_rate is not None and not isinstance(self.flat_rate, Decimal):
            object.__setattr__(self, "flat_rate", Decimal(str(self.flat_rate)))
        if not isinstance(self.rate_scale, Decimal):
            object.__setattr__(self, "rate_scale", Decimal(str(self.rate_scale)))
        if self.rate_scale <= Decimal("0"):
            raise ValueError("rate_scale must be positive")


@dataclass(frozen=True)
class CostRecord:
    """
    Entidad de dominio inmutable para un Registro de Coste Operacional (CostRecord - Hito K.3).
    Representa la medición auditable y determinista del coste de una operación.

    Reglas de cálculo y UNKNOWN:
    - Si total_cost es None, representa UNKNOWN_COST (no se conoce tarifa o consumo).
    - Si total_cost es Decimal("0"), representa ZERO_COST confirmado (ej. herramienta gratuita con tarifa 0).
    - UNKNOWN != ZERO.
    - Idempotencia garantizada por idempotency_key determinista.
    """
    cost_id: str
    occurred_at: datetime
    cost_type: CostType
    provider: str
    service_or_model: str
    execution_id: str
    usage: UsageRecord
    currency: str = "USD"
    unit_cost: Optional[Decimal] = None
    total_cost: Optional[Decimal] = None
    pricing_source: str = "CATALOG"
    pricing_version: str = "1.0.0"
    trace_id: Optional[str] = None
    mission_id: Optional[str] = None
    cycle_id: Optional[str] = None
    correlation_id: str = ""
    causation_id: Optional[str] = None
    provenance: str = "MEASUREMENT"
    idempotency_key: str = ""
    checksum: Optional[str] = None
    schema_version: str = "1.0.0"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.cost_id or not isinstance(self.cost_id, str):
            raise ValueError("cost_id must be a non-empty string")
        if not self.execution_id or not isinstance(self.execution_id, str):
            raise ValueError("execution_id must be a non-empty string")
        if not self.provider or not isinstance(self.provider, str):
            raise ValueError("provider must be a non-empty string")
        if not self.service_or_model or not isinstance(self.service_or_model, str):
            raise ValueError("service_or_model must be a non-empty string")
        if not isinstance(self.cost_type, CostType):
            try:
                object.__setattr__(self, "cost_type", CostType(self.cost_type))
            except Exception as e:
                raise ValueError(f"Invalid cost_type: {self.cost_type}") from e
        if not isinstance(self.usage, UsageRecord):
            raise ValueError("usage must be an instance of UsageRecord")

        # Timezones UTC
        if self.occurred_at.tzinfo is None:
            object.__setattr__(self, "occurred_at", self.occurred_at.replace(tzinfo=timezone.utc))

        # Tipado estricto de montos en Decimal
        if self.unit_cost is not None and not isinstance(self.unit_cost, Decimal):
            object.__setattr__(self, "unit_cost", Decimal(str(self.unit_cost)))
        if self.total_cost is not None and not isinstance(self.total_cost, Decimal):
            object.__setattr__(self, "total_cost", Decimal(str(self.total_cost)))

        # Sanitización de metadatos
        sanitized = _sanitize_cost_metadata(dict(self.metadata))
        object.__setattr__(self, "metadata", MappingProxyType(sanitized))

        # Idempotency key determinista
        if not self.idempotency_key:
            idem_content = f"{self.execution_id}:{self.trace_id or ''}:{self.cost_type.value}:{self.provider}:{self.service_or_model}"
            auto_key = hashlib.sha256(idem_content.encode("utf-8")).hexdigest()
            object.__setattr__(self, "idempotency_key", auto_key)

        # Checksum criptográfico para tamper evidence
        if not self.checksum:
            computed = self._compute_checksum()
            object.__setattr__(self, "checksum", computed)

    @property
    def is_known(self) -> bool:
        """Indica si el costo pudo calcularse de forma conocida (incluyendo coste 0.00)."""
        return self.total_cost is not None

    @property
    def is_unknown(self) -> bool:
        """Indica si el costo es indeterminado / no calculable."""
        return self.total_cost is None

    def _compute_checksum(self) -> str:
        """Calcula hash SHA-256 de los atributos críticos para garantizar inmutabilidad."""
        occ_iso = self.occurred_at.isoformat()
        total_str = str(self.total_cost) if self.total_cost is not None else "UNKNOWN"
        unit_str = str(self.unit_cost) if self.unit_cost is not None else "UNKNOWN"
        canonical = (
            f"{self.cost_id}|{self.execution_id}|{self.cost_type.value}|{self.provider}|"
            f"{self.service_or_model}|{occ_iso}|{self.currency}|{unit_str}|{total_str}|"
            f"{self.pricing_source}|{self.pricing_version}|{self.trace_id or ''}|"
            f"{self.mission_id or ''}|{self.cycle_id or ''}|{self.idempotency_key}"
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def verify_checksum(self) -> bool:
        """Valida que el registro no haya sido manipulado en almacenamiento."""
        return self.checksum == self._compute_checksum()


@dataclass(frozen=True)
class CurrencyCostSummary:
    """
    Sub-resumen inmutable para una moneda específica.
    """
    currency: str
    known_total: Decimal
    record_count: int
    known_record_count: int
    unknown_record_count: int
    breakdown_by_type: Mapping[str, Decimal] = field(default_factory=dict)
    breakdown_by_service: Mapping[str, Decimal] = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.known_total, Decimal):
            object.__setattr__(self, "known_total", Decimal(str(self.known_total)))
        if not isinstance(self.breakdown_by_type, MappingProxyType):
            object.__setattr__(self, "breakdown_by_type", MappingProxyType(dict(self.breakdown_by_type)))
        if not isinstance(self.breakdown_by_service, MappingProxyType):
            object.__setattr__(self, "breakdown_by_service", MappingProxyType(dict(self.breakdown_by_service)))


@dataclass(frozen=True)
class CostSummary:
    """
    Agregado inmutable que consolida mediciones de coste.
    Garantiza:
    - Multi-currency safety: Los totales se agrupan por divisa, no se mezclan.
    - Contabilidad explícita de UNKNOWN (unknown_records_count).
    - Preservación de trazabilidad para agregaciones por mission_id, execution_id, cycle_id o rango temporal.
    """
    mission_id: Optional[str] = None
    execution_id: Optional[str] = None
    cycle_id: Optional[str] = None
    total_records: int = 0
    total_known_records: int = 0
    total_unknown_records: int = 0
    by_currency: Mapping[str, CurrencyCostSummary] = field(default_factory=dict)
    records: Tuple[CostRecord, ...] = field(default_factory=tuple)

    def __post_init__(self):
        if not isinstance(self.by_currency, MappingProxyType):
            object.__setattr__(self, "by_currency", MappingProxyType(dict(self.by_currency)))
        if not isinstance(self.records, tuple):
            object.__setattr__(self, "records", tuple(self.records))

    @classmethod
    def from_records(
        cls,
        records: List[CostRecord],
        mission_id: Optional[str] = None,
        execution_id: Optional[str] = None,
        cycle_id: Optional[str] = None,
    ) -> "CostSummary":
        """
        Construye determinísticamente un CostSummary a partir de una colección de CostRecord.
        """
        total_records = len(records)
        known_count = sum(1 for r in records if r.is_known)
        unknown_count = sum(1 for r in records if r.is_unknown)

        # Agrupar por divisa
        currency_groups: Dict[str, List[CostRecord]] = {}
        for r in records:
            curr = r.currency.upper()
            if curr not in currency_groups:
                currency_groups[curr] = []
            currency_groups[curr].append(r)

        by_currency: Dict[str, CurrencyCostSummary] = {}
        for curr, curr_records in currency_groups.items():
            curr_known_total = sum((r.total_cost for r in curr_records if r.total_cost is not None), Decimal("0.00"))
            curr_record_count = len(curr_records)
            curr_known_count = sum(1 for r in curr_records if r.is_known)
            curr_unknown_count = sum(1 for r in curr_records if r.is_unknown)

            by_type: Dict[str, Decimal] = {}
            for r in curr_records:
                if r.total_cost is not None:
                    t_key = r.cost_type.value
                    by_type[t_key] = by_type.get(t_key, Decimal("0.00")) + r.total_cost

            by_service: Dict[str, Decimal] = {}
            for r in curr_records:
                if r.total_cost is not None:
                    s_key = f"{r.provider}:{r.service_or_model}"
                    by_service[s_key] = by_service.get(s_key, Decimal("0.00")) + r.total_cost

            by_currency[curr] = CurrencyCostSummary(
                currency=curr,
                known_total=curr_known_total,
                record_count=curr_record_count,
                known_record_count=curr_known_count,
                unknown_record_count=curr_unknown_count,
                breakdown_by_type=by_type,
                breakdown_by_service=by_service,
            )

        return cls(
            mission_id=mission_id,
            execution_id=execution_id,
            cycle_id=cycle_id,
            total_records=total_records,
            total_known_records=known_count,
            total_unknown_records=unknown_count,
            by_currency=by_currency,
            records=tuple(records),
        )
