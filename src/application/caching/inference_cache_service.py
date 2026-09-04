"""
Servicio de Aplicación para Caching de Inferencia (Hito M.4).

Transversal M — Control de Coste e Inferencia.

M.4 responde:
"¿Podemos reutilizar de forma segura un resultado previo para evitar una inferencia redundante?"

Pipeline de caching:
1. Canonical request normalization -> Request fingerprint (SHA-256)
2. Compute CacheKey (fingerprint + route_or_model_id + policy_id + policy_version + model_version)
3. Cache Lookup:
   - Not found -> MISS
   - Checksum corruption -> INVALID / Eviction
   - TTL check -> EXPIRED / Eviction (ClockPort de K.7)
   - Security context mismatch -> INVALID / MISS
   - Valid -> HIT
4. Cache Store:
   - Check cacheability: NO ERROR, NO UNKNOWN, NO Side-effects, size within limit
   - Calculate TTL -> expires_at
   - Compute Entry Checksum
   - Atomic store

Concurrency safety:
- RLock a nivel de servicio y persistencia.
"""

from datetime import datetime, timezone, timedelta
import logging
import threading
from typing import Optional, Any, Dict

from src.domain.caching.models import (
    CacheEntry,
    CacheEvictionReason,
    CacheIntegrityError,
    CacheLookupRequest,
    CacheLookupResult,
    CacheLookupStatus,
    CachePolicy,
    CacheStoreRequest,
    compute_cache_entry_checksum,
    compute_cache_key,
    compute_request_fingerprint,
)
from src.domain.caching.ports import (
    CacheRepositoryPort,
    InferenceCacheServicePort,
)
from src.domain.reliability.ports import ClockPort

logger = logging.getLogger(__name__)


class SystemClock(ClockPort):
    """Implementación de ClockPort por defecto utilizando datetime UTC del sistema."""
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def sleep(self, seconds: float) -> None:
        pass


class InferenceCacheService(InferenceCacheServicePort):
    """
    Servicio de aplicación para Caching de Inferencia M.4.
    """

    def __init__(
        self,
        repository: CacheRepositoryPort,
        clock: Optional[ClockPort] = None,
        default_policy: Optional[CachePolicy] = None,
    ):
        self._repository = repository
        self._clock = clock or SystemClock()
        self._default_policy = default_policy or CachePolicy(
            policy_id="default_m4_cache_policy",
            version="1.0.0",
            ttl_seconds=3600,
        )
        self._lock = threading.RLock()

    def _get_current_time(self) -> datetime:
        curr = self._clock.now()
        if curr.tzinfo is None:
            curr = curr.replace(tzinfo=timezone.utc)
        return curr

    def lookup(
        self,
        request: CacheLookupRequest,
        policy: Optional[CachePolicy] = None,
    ) -> CacheLookupResult:
        """
        Realiza una búsqueda determinista y segura en la caché de inferencia.
        """
        active_policy = policy or request.policy or self._default_policy

        if not active_policy.enabled:
            fp = compute_request_fingerprint(
                normalized_prompt_or_payload=request.normalized_prompt_or_payload,
                tool_schemas=request.tool_schemas,
                system_instructions=request.system_instructions,
                parameters=request.parameters,
                security_context_id=request.security_context_id,
            )
            key = compute_cache_key(
                request_fingerprint=fp,
                route_or_model_id=request.route_or_model_id,
                policy_id=active_policy.policy_id,
                policy_version=active_policy.version,
                model_version=request.model_version,
                extra_namespace=request.extra_namespace,
            )
            return CacheLookupResult(
                status=CacheLookupStatus.MISS,
                cache_key=key,
                request_fingerprint=fp,
                entry=None,
                rationale="Caching is disabled by policy",
            )

        with self._lock:
            # 1. Calcular fingerprint determinista
            fingerprint = compute_request_fingerprint(
                normalized_prompt_or_payload=request.normalized_prompt_or_payload,
                tool_schemas=request.tool_schemas,
                system_instructions=request.system_instructions,
                parameters=request.parameters,
                security_context_id=request.security_context_id if active_policy.enforce_security_context_isolation else None,
            )

            # 2. Generar cache_key determinista
            cache_key = compute_cache_key(
                request_fingerprint=fingerprint,
                route_or_model_id=request.route_or_model_id,
                policy_id=active_policy.policy_id,
                policy_version=active_policy.version,
                model_version=request.model_version,
                extra_namespace=request.extra_namespace,
            )

            # 3. Buscar en el repositorio
            try:
                entry = self._repository.get_by_key(cache_key)
            except CacheIntegrityError as e:
                logger.warning(f"Cache entry integrity check failed for key {cache_key}: {e}")
                self._repository.delete(cache_key)
                return CacheLookupResult(
                    status=CacheLookupStatus.INVALID,
                    cache_key=cache_key,
                    request_fingerprint=fingerprint,
                    entry=None,
                    eviction_reason=CacheEvictionReason.CHECKSUM_MISMATCH,
                    rationale=f"Cache entry corrupted or tampered: {str(e)}",
                )
            except Exception as e:
                logger.error(f"Error accessing cache repository for key {cache_key}: {e}")
                return CacheLookupResult(
                    status=CacheLookupStatus.ERROR,
                    cache_key=cache_key,
                    request_fingerprint=fingerprint,
                    entry=None,
                    rationale=f"Cache repository error during lookup: {str(e)}",
                )

            if entry is None:
                return CacheLookupResult(
                    status=CacheLookupStatus.MISS,
                    cache_key=cache_key,
                    request_fingerprint=fingerprint,
                    entry=None,
                    rationale="Cache entry not found (MISS)",
                )

            # 4. Verificar integridad y checksum
            if not entry.verify_checksum():
                logger.warning(f"Cache entry checksum mismatch for key {cache_key}; invalidating")
                self._repository.delete(cache_key)
                return CacheLookupResult(
                    status=CacheLookupStatus.INVALID,
                    cache_key=cache_key,
                    request_fingerprint=fingerprint,
                    entry=None,
                    eviction_reason=CacheEvictionReason.CHECKSUM_MISMATCH,
                    rationale="Cache entry corrupted or tampered (checksum mismatch)",
                )

            # 5. Validar aislamiento de contexto de seguridad
            if active_policy.enforce_security_context_isolation:
                if (request.security_context_id or "") != (entry.security_context_id or ""):
                    logger.warning(f"Security context mismatch on cache key {cache_key}")
                    return CacheLookupResult(
                        status=CacheLookupStatus.MISS,
                        cache_key=cache_key,
                        request_fingerprint=fingerprint,
                        entry=None,
                        eviction_reason=CacheEvictionReason.SECURITY_CONTEXT_MISMATCH,
                        rationale="Security context isolation mismatch; cache entry cannot be shared across tenants/contexts",
                    )

            # 6. Validar expiración / TTL
            now = self._get_current_time()
            if entry.is_expired(now):
                logger.info(f"Cache entry {cache_key} expired at {entry.expires_at}")
                self._repository.delete(cache_key)
                return CacheLookupResult(
                    status=CacheLookupStatus.EXPIRED,
                    cache_key=cache_key,
                    request_fingerprint=fingerprint,
                    entry=None,
                    eviction_reason=CacheEvictionReason.TTL_EXPIRED,
                    rationale=f"Cache entry expired at {entry.expires_at.isoformat()}",
                )

            # 7. Validar consistencia de política y modelo
            if entry.policy_id != active_policy.policy_id or entry.policy_version != active_policy.version:
                self._repository.delete(cache_key)
                return CacheLookupResult(
                    status=CacheLookupStatus.INVALID,
                    cache_key=cache_key,
                    request_fingerprint=fingerprint,
                    entry=None,
                    eviction_reason=CacheEvictionReason.POLICY_VERSION_MISMATCH,
                    rationale="Cache entry policy version mismatch",
                )

            if entry.route_or_model_id != request.route_or_model_id:
                self._repository.delete(cache_key)
                return CacheLookupResult(
                    status=CacheLookupStatus.INVALID,
                    cache_key=cache_key,
                    request_fingerprint=fingerprint,
                    entry=None,
                    eviction_reason=CacheEvictionReason.MODEL_MISMATCH,
                    rationale="Cache entry model/route mismatch",
                )

            # 8. HIT VÁLIDO
            return CacheLookupResult(
                status=CacheLookupStatus.HIT,
                cache_key=cache_key,
                request_fingerprint=fingerprint,
                entry=entry,
                rationale="Valid cache HIT",
            )

    def store(
        self,
        request: CacheStoreRequest,
        policy: Optional[CachePolicy] = None,
    ) -> Optional[CacheEntry]:
        """
        Evalúa la cacheabilidad y almacena de forma segura el resultado de inferencia.
        """
        lookup_req = request.lookup_request
        active_policy = policy or lookup_req.policy or self._default_policy

        if not active_policy.enabled:
            logger.debug("Cache store skipped: caching is disabled by policy")
            return None

        # Verificaciones de cacheabilidad segura:
        # NO cachear ERROR si la política no lo permite explícitamente
        if request.is_error and not active_policy.allow_cache_errors:
            logger.debug("Cache store skipped: inference result was ERROR")
            return None

        # NO cachear UNKNOWN si la política no lo permite explícitamente
        if request.is_unknown and not active_policy.allow_cache_unknown:
            logger.debug("Cache store skipped: inference result was UNKNOWN")
            return None

        # NO cachear side-effects comerciales o acciones mutables
        if request.has_side_effects:
            logger.debug("Cache store skipped: request involves non-deterministic or commercial side effects")
            return None

        # Si el resultado es None o nulo sin contrato
        if request.result_data is None:
            logger.debug("Cache store skipped: result_data is None")
            return None

        with self._lock:
            # Calcular fingerprint y key
            fingerprint = compute_request_fingerprint(
                normalized_prompt_or_payload=lookup_req.normalized_prompt_or_payload,
                tool_schemas=lookup_req.tool_schemas,
                system_instructions=lookup_req.system_instructions,
                parameters=lookup_req.parameters,
                security_context_id=lookup_req.security_context_id if active_policy.enforce_security_context_isolation else None,
            )

            cache_key = compute_cache_key(
                request_fingerprint=fingerprint,
                route_or_model_id=lookup_req.route_or_model_id,
                policy_id=active_policy.policy_id,
                policy_version=active_policy.version,
                model_version=lookup_req.model_version,
                extra_namespace=lookup_req.extra_namespace,
            )

            now = self._get_current_time()

            # Calcular TTL y fecha de expiración
            ttl = request.custom_ttl_seconds if request.custom_ttl_seconds is not None else active_policy.ttl_seconds
            expires_at = now + timedelta(seconds=ttl) if ttl is not None else None

            # Construir y validar entrada
            entry = CacheEntry(
                cache_key=cache_key,
                route_or_model_id=lookup_req.route_or_model_id,
                request_fingerprint=fingerprint,
                result_data=request.result_data,
                created_at=now,
                expires_at=expires_at,
                policy_id=active_policy.policy_id,
                policy_version=active_policy.version,
                model_version=lookup_req.model_version,
                security_context_id=lookup_req.security_context_id,
                metadata=request.metadata,
            )

            try:
                self._repository.save(entry)
                return entry
            except Exception as e:
                logger.error(f"Error persisting cache entry for key {cache_key}: {e}")
                return None

    def invalidate(self, cache_key: str) -> bool:
        """Invalida explícitamente una entrada de caché."""
        with self._lock:
            return self._repository.delete(cache_key)
