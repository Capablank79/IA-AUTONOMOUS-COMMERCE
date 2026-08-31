from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, List, Tuple, Mapping, Any, Dict
from types import MappingProxyType

class ToolEvidenceProvenance(str, Enum):
    """
    Procedencia estricta de los datos y capacidades de una herramienta.
    """
    LIVE = "LIVE"
    FIXTURE = "FIXTURE"
    MOCK = "MOCK"
    FAKE = "FAKE"
    DERIVED = "DERIVED"
    INFERRED = "INFERRED"
    UNKNOWN = "UNKNOWN"



class ToolLifecycleStatus(str, Enum):
    """
    Ciclo de vida explícito de una herramienta registrada en el Registry.
    """
    REGISTERED = "REGISTERED"
    AVAILABLE = "AVAILABLE"
    DISABLED = "DISABLED"
    DEPRECATED = "DEPRECATED"
    UNKNOWN = "UNKNOWN"


class ToolSideEffectLevel(str, Enum):
    """
    Clasificación del nivel de impacto y mutabilidad de una herramienta.
    """
    READ_ONLY = "READ_ONLY"                    # Solo lectura, sin efectos externos ni modificaciones
    ANALYSIS = "ANALYSIS"                      # Procesamiento analítico o cómputo puro in-memory
    WRITE = "WRITE"                            # Escritura/mutación en estado o almacenamiento interno
    EXTERNAL_SIDE_EFFECT = "EXTERNAL_SIDE_EFFECT"  # Invocación con efecto o mutación en servicios externos
    IRREVERSIBLE = "IRREVERSIBLE"              # Efecto irreversible (financiero, eliminación, etc.)


class ToolExecutionChannel(str, Enum):
    """
    Canales de ejecución o integración soportados.
    """
    INTERNAL = "INTERNAL"
    MERCADO_LIBRE = "MERCADO_LIBRE"
    SHOPIFY = "SHOPIFY"
    AMAZON = "AMAZON"
    SUPPLIER_DIRECTORY = "SUPPLIER_DIRECTORY"
    WEB_SCRAPING = "WEB_SCRAPING"
    LLM_INFERENCE = "LLM_INFERENCE"
    GENERIC = "GENERIC"


@dataclass(frozen=True)
class ToolSchemaField:
    """
    Definición fuertemente tipada de un campo dentro del contrato de entrada o salida.
    """
    name: str
    type_name: str
    required: bool = True
    description: str = ""
    default_value: Optional[Any] = None
    allowed_values: Tuple[Any, ...] = field(default_factory=tuple)

    def __post_init__(self):
        if not self.name or not isinstance(self.name, str):
            raise ValueError("ToolSchemaField.name must be a non-empty string")
        if not self.type_name or not isinstance(self.type_name, str):
            raise ValueError("ToolSchemaField.type_name must be a non-empty string")
        if not isinstance(self.allowed_values, tuple):
            object.__setattr__(self, "allowed_values", tuple(self.allowed_values))


@dataclass(frozen=True)
class ToolContract:
    """
    Contrato estricto de entrada y salida para una herramienta.
    """
    schema_name: str
    fields: Tuple[ToolSchemaField, ...] = field(default_factory=tuple)
    description: str = ""
    allow_extra_fields: bool = False

    def __post_init__(self):
        if not self.schema_name or not isinstance(self.schema_name, str):
            raise ValueError("ToolContract.schema_name must be a non-empty string")
        if not isinstance(self.fields, tuple):
            object.__setattr__(self, "fields", tuple(self.fields))

    def get_field(self, field_name: str) -> Optional[ToolSchemaField]:
        for f in self.fields:
            if f.name == field_name:
                return f
        return None

    def validate(self, data: Mapping[str, Any]) -> Tuple[bool, List[str]]:
        """
        Valida que un diccionario de datos cumpla con los requisitos del contrato.
        Retorna (is_valid, errors).
        """
        errors = []
        if not isinstance(data, (dict, MappingProxyType)):
            return False, ["Data must be a mapping/dict"]

        data_keys = set(data.keys())
        field_dict = {f.name: f for f in self.fields}

        # 1. Verificar campos requeridos
        for field_name, f in field_dict.items():
            if f.required and field_name not in data:
                errors.append(f"Missing required field: '{field_name}'")

        # 2. Verificar valores permitidos y campos extra no permitidos
        for k, v in data.items():
            if k in field_dict:
                f = field_dict[k]
                if f.allowed_values and v not in f.allowed_values:
                    errors.append(f"Field '{k}' value '{v}' is not in allowed values: {f.allowed_values}")
            elif not self.allow_extra_fields:
                errors.append(f"Extra unknown field '{k}' not permitted by contract '{self.schema_name}'")

        return len(errors) == 0, errors


@dataclass(frozen=True)
class ToolVersion:
    """
    Representación inmutable de la versión de una herramienta.
    Soporta formato semántico simple (major.minor o v1, v2).
    """
    version_str: str

    def __post_init__(self):
        if not self.version_str or not isinstance(self.version_str, str):
            raise ValueError("version_str must be a non-empty string")
        clean_v = self.version_str.strip()
        if not clean_v:
            raise ValueError("version_str cannot be whitespace only")
        object.__setattr__(self, "version_str", clean_v)

    def __str__(self) -> str:
        return self.version_str


@dataclass(frozen=True)
class ToolDescriptor:
    """
    Descriptor inmutable y fuertemente tipado de una herramienta en el Tool Registry.
    
    Aislamiento y Seguridad:
    - CERO credenciales, API keys, tokens o secretos.
    - CERO callables arbitrarios o instancias de SDK dentro del dominio.
    - Metadatos explícitos para descubrimiento, selección y evaluación de políticas.
    """
    tool_id: str
    name: str
    version: ToolVersion
    description: str
    capability: str
    input_contract: ToolContract
    output_contract: ToolContract
    side_effect_level: ToolSideEffectLevel
    required_permissions: Tuple[str, ...] = field(default_factory=tuple)
    supported_channels: Tuple[ToolExecutionChannel, ...] = field(default_factory=tuple)
    status: ToolLifecycleStatus = ToolLifecycleStatus.REGISTERED
    provenance: ToolEvidenceProvenance = ToolEvidenceProvenance.DERIVED
    requires_approval: bool = False
    requires_idempotency: bool = False
    timeout_ms: Optional[int] = None
    tags: Tuple[str, ...] = field(default_factory=tuple)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.tool_id or not isinstance(self.tool_id, str):
            raise ValueError("tool_id must be a non-empty string")
        if not self.name or not isinstance(self.name, str):
            raise ValueError("name must be a non-empty string")
        if not isinstance(self.version, ToolVersion):
            if isinstance(self.version, str):
                object.__setattr__(self, "version", ToolVersion(self.version))
            else:
                raise ValueError("version must be a ToolVersion or str")
        if not self.capability or not isinstance(self.capability, str):
            raise ValueError("capability must be a non-empty string")
        if not isinstance(self.input_contract, ToolContract):
            raise ValueError("input_contract must be a ToolContract")
        if not isinstance(self.output_contract, ToolContract):
            raise ValueError("output_contract must be a ToolContract")
        if not isinstance(self.side_effect_level, ToolSideEffectLevel):
            raise ValueError("side_effect_level must be a ToolSideEffectLevel")
        if not isinstance(self.status, ToolLifecycleStatus):
            raise ValueError("status must be a ToolLifecycleStatus")
        if not isinstance(self.provenance, ToolEvidenceProvenance):
            raise ValueError("provenance must be a ToolEvidenceProvenance")

        # Congelar tuplas y mappings para inmutabilidad estricta
        if not isinstance(self.required_permissions, tuple):
            object.__setattr__(self, "required_permissions", tuple(self.required_permissions))
        if not isinstance(self.supported_channels, tuple):
            object.__setattr__(self, "supported_channels", tuple(self.supported_channels))
        if not isinstance(self.tags, tuple):
            object.__setattr__(self, "tags", tuple(self.tags))
        if not isinstance(self.metadata, MappingProxyType):
            object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    @property
    def qualified_id(self) -> str:
        """
        Identificador único cualificado: tool_id:version (ej: market_search:v1)
        """
        return f"{self.tool_id}:{self.version.version_str}"

    @property
    def is_executable(self) -> bool:
        """
        Determina si el estado de la herramienta permite ser seleccionada para ejecución normal.
        Herramientas UNKNOWN, DISABLED o DEPRECATED no son ejecutables de forma estándar.
        """
        return self.status in (ToolLifecycleStatus.AVAILABLE, ToolLifecycleStatus.REGISTERED)


@dataclass(frozen=True)
class ToolInvocationRequest:
    """
    Solicitud inmutable de invocación de una herramienta.
    """
    tool_id: str
    version: Optional[str] = None
    input_payload: Mapping[str, Any] = field(default_factory=dict)
    actor_id: str = "autonomous_agent"
    mission_id: Optional[str] = None
    correlation_id: Optional[str] = None
    idempotency_key: Optional[str] = None
    requested_channel: Optional[ToolExecutionChannel] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.tool_id:
            raise ValueError("tool_id must be provided")
        if not isinstance(self.input_payload, MappingProxyType):
            object.__setattr__(self, "input_payload", MappingProxyType(dict(self.input_payload)))
        if not isinstance(self.metadata, MappingProxyType):
            object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True)
class ToolInvocationResult:
    """
    Resultado inmutable de la invocación de una herramienta.
    """
    tool_id: str
    version: str
    success: bool
    output_payload: Mapping[str, Any] = field(default_factory=dict)
    error_message: Optional[str] = None
    error_code: Optional[str] = None
    correlation_id: Optional[str] = None
    provenance: ToolEvidenceProvenance = ToolEvidenceProvenance.DERIVED
    executed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        if not isinstance(self.output_payload, MappingProxyType):
            object.__setattr__(self, "output_payload", MappingProxyType(dict(self.output_payload)))
