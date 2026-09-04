"""
Modelos de dominio para Caching de Inferencia (Hito M.4).

Transversal M — Control de Coste e Inferencia.

M.4 responde:
"¿Podemos reutilizar de forma segura un resultado previo para evitar una inferencia redundante?"

Define:
- CacheLookupStatus: Estados canónicos (HIT, MISS, EXPIRED, INVALID, UNKNOWN, ERROR).
- CacheEvictionReason: Razones estructuradas de descarte o invalidación (TTL_EXPIRED, CHECKSUM_MISMATCH, POLICY_VERSION_MISMATCH, MODEL_MISMATCH, UNKNOWN_STATUS, INVALID_ENTRY, MANUAL_INVALIDATION).
- CachePolicy: Política inmutable y versionada que rige la cacheabilidad, TTL, aislamiento de seguridad y tenant.
- CacheEntry: Registro inmutable y persistible de inferencia previa con checksum SHA-256 canónico.
- CacheLookupRequest: Solicitud inmutable de consulta en caché.
- CacheLookupResult: Resultado inmutable de la consulta con trazabilidad, estado y entrada si aplica.
- CacheStoreRequest: Solicitud inmutable de almacenamiento de inferencia.

Principios M.4:
- Inmutabilidad estricta (frozen=True, MappingProxyType, tuplas).
- Determinismo absoluto: Mismo request semántico normalizado + mismo model/route + mismos tool schemas/parámetros + misma política => Misma CacheKey (SHA-256 canónico).
- NO timestamps de runtime, NO UUIDs aleatorios, NO memory addresses en CacheKey.
- Seguridad semántica: Evitar False HIT (diferencias en intención, evidencia crítica, rutas, schemas de tools o security context producen MISS).
- TTL explícito y determinista: Integrado con ClockPort de K.7 si aplica. Si expira -> EXPIRED / MISS, NUNCA HIT.
- Cacheability restrictiva: NUNCA cachear silenciosamente resultados en estado ERROR, UNKNOWN, fallos de seguridad ni side-effects.
- Aislamiento de seguridad: Sanitización de secretos (API keys, passwords, tokens, CoT), aislamiento por tenant/context_id.
- NO bypass de PolicyEngine, autorización ni ActionExecutor (M.4 es exclusivamente para inferencia).
- NO implementación de M.5 (Model Selection by Task) ni M.6 (Cost-aware Decision Policy).
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import re
from types import MappingProxyType
from typing import Mapping, Optional, Any, Tuple, Union, Sequence, Dict

from src.domain.model_routing.models import sanitize_routing_data, deep_freeze, ModelRoute, SENSITIVE_KEYS


class CacheLookupStatus(str, Enum):
    """Estados canónicos de resultado de consulta en caché."""
    HIT = "HIT"
    MISS = "MISS"
    EXPIRED = "EXPIRED"
    INVALID = "INVALID"
    UNKNOWN = "UNKNOWN"
    ERROR = "ERROR"


class CacheIntegrityError(Exception):
    """Excepción lanzada ante detección de corrupción física o violación de checksum."""
    pass


class CacheEvictionReason(str, Enum):
    """Razones estructuradas de no reutilización o invalidación."""
    TTL_EXPIRED = "TTL_EXPIRED"
    CHECKSUM_MISMATCH = "CHECKSUM_MISMATCH"
    POLICY_VERSION_MISMATCH = "POLICY_VERSION_MISMATCH"
    MODEL_MISMATCH = "MODEL_MISMATCH"
    UNKNOWN_STATUS = "UNKNOWN_STATUS"
    INVALID_ENTRY = "INVALID_ENTRY"
    MANUAL_INVALIDATION = "MANUAL_INVALIDATION"
    SECURITY_CONTEXT_MISMATCH = "SECURITY_CONTEXT_MISMATCH"


def canonical_json_dump(data: Any) -> str:
    """Serializa estructuras de datos a JSON canónico y determinista."""
    def _default(obj: Any) -> Any:
        if isinstance(obj, (datetime,)):
            return obj.isoformat()
        if isinstance(obj, (Enum,)):
            return obj.value
        if isinstance(obj, (set, frozenset, tuple)):
            return list(obj)
        if hasattr(obj, "to_dict") and callable(obj.to_dict):
            return obj.to_dict()
        if hasattr(obj, "__dict__"):
            return {k: v for k, v in obj.__dict__.items() if not k.startswith("_")}
        return str(obj)

    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=_default)


def compute_request_fingerprint(
    normalized_prompt_or_payload: Any,
    tool_schemas: Optional[Sequence[Any]] = None,
    system_instructions: Optional[str] = None,
    parameters: Optional[Mapping[str, Any]] = None,
    security_context_id: Optional[str] = None,
) -> str:
    """
    Calcula el fingerprint determinista SHA-256 de una solicitud de inferencia.
    Sanitiza secretos para evitar persistir tokens o credenciales.
    """
    sanitized_prompt = sanitize_routing_data(normalized_prompt_or_payload)
    sanitized_tools = sanitize_routing_data(tool_schemas) if tool_schemas else None
    sanitized_params = sanitize_routing_data(parameters) if parameters else None
    
    canonical_dict = {
        "payload": sanitized_prompt,
        "system_instructions": system_instructions or "",
        "tool_schemas": sanitized_tools or [],
        "parameters": sanitized_params or {},
        "security_context_id": security_context_id or "",
    }
    canonical_str = canonical_json_dump(canonical_dict)
    return hashlib.sha256(canonical_str.encode("utf-8")).hexdigest()


def compute_cache_key(
    request_fingerprint: str,
    route_or_model_id: str,
    policy_id: str,
    policy_version: str,
    model_version: Optional[str] = None,
    extra_namespace: Optional[str] = None,
) -> str:
    """
    Genera la clave de caché canónica SHA-256 combinando el fingerprint del request,
    la identidad del modelo/ruta, y la versión de la política aplicable.
    """
    key_dict = {
        "fingerprint": request_fingerprint,
        "route_or_model_id": route_or_model_id,
        "policy_id": policy_id,
        "policy_version": policy_version,
        "model_version": model_version or "",
        "namespace": extra_namespace or "",
    }
    canonical_str = canonical_json_dump(key_dict)
    return hashlib.sha256(canonical_str.encode("utf-8")).hexdigest()


def compute_cache_entry_checksum(
    cache_key: str,
    route_or_model_id: str,
    request_fingerprint: str,
    result_data: Any,
    created_at_iso: str,
    expires_at_iso: Optional[str],
    policy_id: str,
    policy_version: str,
    metadata: Mapping[str, Any],
) -> str:
    """Calcula la suma de verificación SHA-256 para integridad de la entrada de caché."""
    payload = {
        "cache_key": cache_key,
        "route_or_model_id": route_or_model_id,
        "request_fingerprint": request_fingerprint,
        "result_data": result_data,
        "created_at_iso": created_at_iso,
        "expires_at_iso": expires_at_iso,
        "policy_id": policy_id,
        "policy_version": policy_version,
        "metadata": dict(metadata),
    }
    canonical_str = canonical_json_dump(payload)
    return hashlib.sha256(canonical_str.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CachePolicy:
    """
    Política inmutable y versionada de Caching (M.4).
    """
    policy_id: str
    version: str = "1.0.0"
    enabled: bool = True
    ttl_seconds: Optional[int] = 3600  # Default 1 hora; None = sin expiración
    allow_cache_errors: bool = False
    allow_cache_unknown: bool = False
    enforce_security_context_isolation: bool = True
    max_payload_size_bytes: int = 10 * 1024 * 1024  # 10MB
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.policy_id or not self.policy_id.strip():
            raise ValueError("policy_id cannot be empty")
        if not self.version or not self.version.strip():
            raise ValueError("version cannot be empty")
        if self.ttl_seconds is not None:
            if not isinstance(self.ttl_seconds, int) or isinstance(self.ttl_seconds, bool):
                raise ValueError("ttl_seconds must be an integer")
            if self.ttl_seconds <= 0:
                raise ValueError("ttl_seconds must be positive")
        if not isinstance(self.max_payload_size_bytes, int) or isinstance(self.max_payload_size_bytes, bool):
            raise ValueError("max_payload_size_bytes must be an integer")
        if self.max_payload_size_bytes <= 0:
            raise ValueError("max_payload_size_bytes must be positive")

        clean_meta = sanitize_routing_data(self.metadata)
        object.__setattr__(self, "metadata", deep_freeze(clean_meta))


@dataclass(frozen=True)
class CacheEntry:
    """
    Entrada inmutable y persistible de resultado de inferencia en caché.
    """
    cache_key: str
    route_or_model_id: str
    request_fingerprint: str
    result_data: Any
    created_at: datetime
    expires_at: Optional[datetime] = None
    policy_id: str = "default_m4_cache_policy"
    policy_version: str = "1.0.0"
    model_version: Optional[str] = None
    checksum: str = ""
    security_context_id: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.cache_key or not self.cache_key.strip():
            raise ValueError("cache_key cannot be empty")
        if not self.route_or_model_id or not self.route_or_model_id.strip():
            raise ValueError("route_or_model_id cannot be empty")
        if not self.request_fingerprint or not self.request_fingerprint.strip():
            raise ValueError("request_fingerprint cannot be empty")
        if not isinstance(self.created_at, datetime):
            raise ValueError("created_at must be a datetime")
        if self.expires_at is not None and not isinstance(self.expires_at, datetime):
            raise ValueError("expires_at must be a datetime")

        # Normalizar timestamps a UTC timezone-aware
        c_at = self.created_at
        if c_at.tzinfo is None:
            c_at = c_at.replace(tzinfo=timezone.utc)
        object.__setattr__(self, "created_at", c_at)

        e_at = self.expires_at
        if e_at is not None:
            if e_at.tzinfo is None:
                e_at = e_at.replace(tzinfo=timezone.utc)
            object.__setattr__(self, "expires_at", e_at)

        # Sanitizar metadatos
        clean_meta = sanitize_routing_data(self.metadata)
        object.__setattr__(self, "metadata", deep_freeze(clean_meta))

        # Sanitizar result_data si es dict o mapping
        clean_result = sanitize_routing_data(self.result_data)
        object.__setattr__(self, "result_data", deep_freeze(clean_result) if isinstance(clean_result, (dict, list)) else clean_result)

        # Calcular checksum si no viene provisto
        if not self.checksum:
            expected_chk = compute_cache_entry_checksum(
                cache_key=self.cache_key,
                route_or_model_id=self.route_or_model_id,
                request_fingerprint=self.request_fingerprint,
                result_data=self.result_data,
                created_at_iso=c_at.isoformat(),
                expires_at_iso=e_at.isoformat() if e_at else None,
                policy_id=self.policy_id,
                policy_version=self.policy_version,
                metadata=self.metadata,
            )
            object.__setattr__(self, "checksum", expected_chk)

    def is_expired(self, current_time: datetime) -> bool:
        """Verifica si la entrada ha expirado en comparación con el tiempo provisto."""
        if self.expires_at is None:
            return False
        curr = current_time if current_time.tzinfo is not None else current_time.replace(tzinfo=timezone.utc)
        return curr >= self.expires_at

    def verify_checksum(self) -> bool:
        """Verifica la integridad de la entrada contra su checksum SHA-256."""
        computed = compute_cache_entry_checksum(
            cache_key=self.cache_key,
            route_or_model_id=self.route_or_model_id,
            request_fingerprint=self.request_fingerprint,
            result_data=self.result_data,
            created_at_iso=self.created_at.isoformat(),
            expires_at_iso=self.expires_at.isoformat() if self.expires_at else None,
            policy_id=self.policy_id,
            policy_version=self.policy_version,
            metadata=self.metadata,
        )
        return self.checksum == computed


@dataclass(frozen=True)
class CacheLookupRequest:
    """
    Solicitud inmutable de búsqueda en caché de inferencia.
    """
    normalized_prompt_or_payload: Any
    route_or_model_id: str
    tool_schemas: Optional[Sequence[Any]] = None
    system_instructions: Optional[str] = None
    parameters: Optional[Mapping[str, Any]] = None
    model_version: Optional[str] = None
    policy: Optional[CachePolicy] = None
    security_context_id: Optional[str] = None
    extra_namespace: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.route_or_model_id or not self.route_or_model_id.strip():
            raise ValueError("route_or_model_id cannot be empty")
        if not isinstance(self.tool_schemas, tuple) and self.tool_schemas is not None:
            object.__setattr__(self, "tool_schemas", tuple(self.tool_schemas))

        clean_meta = sanitize_routing_data(self.metadata)
        object.__setattr__(self, "metadata", deep_freeze(clean_meta))


@dataclass(frozen=True)
class CacheLookupResult:
    """
    Resultado inmutable de la consulta en caché de inferencia.
    """
    status: CacheLookupStatus
    cache_key: str
    request_fingerprint: str
    entry: Optional[CacheEntry] = None
    eviction_reason: Optional[CacheEvictionReason] = None
    rationale: str = ""
    evaluated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        if not isinstance(self.status, CacheLookupStatus):
            raise ValueError("status must be a valid CacheLookupStatus")
        if self.eviction_reason is not None and not isinstance(self.eviction_reason, CacheEvictionReason):
            raise ValueError("eviction_reason must be a valid CacheEvictionReason")
        if self.status == CacheLookupStatus.HIT and self.entry is None:
            raise ValueError("CacheLookupResult with status HIT must contain a valid CacheEntry")


@dataclass(frozen=True)
class CacheStoreRequest:
    """
    Solicitud inmutable para almacenar un resultado de inferencia en caché.
    """
    lookup_request: CacheLookupRequest
    result_data: Any
    is_error: bool = False
    is_unknown: bool = False
    has_side_effects: bool = False
    custom_ttl_seconds: Optional[int] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        clean_meta = sanitize_routing_data(self.metadata)
        object.__setattr__(self, "metadata", deep_freeze(clean_meta))
