"""
Persistencia JSON atómica, versionada e íntegra para Entity Resolution L.6.

Garantiza:
- Atomic write (.tmp -> fsync -> os.replace).
- Inmutabilidad estricta por policy_id + version, resolution_id y canonical_entity_id.
- Idempotencia estricta para payloads y checksums idénticos.
- Detección explícita de conflictos si se intenta sobrescribir con contenido diferente bajo el mismo ID/versión.
- Verificación estricta de integridad SHA-256 en lectura y detección de corrupción física sin autorreparación silenciosa.
- Thread-safe mediante RLock de concurrencia.
- Path safety estricto (rechaza traversals).
"""

from datetime import datetime, timezone
from decimal import Decimal
import json
import logging
import os
from pathlib import Path
from types import MappingProxyType
from typing import Union, Optional, Any, Dict, Sequence, List
import threading

from src.domain.entity_resolution.models import (
    EntityType,
    IdentifierType,
    MatchStatus,
    EntityIdentifier,
    EntityReference,
    EntityResolutionPolicy,
    EntityResolutionResult,
    ResolvedEntity,
    compute_entity_reference_checksum,
    compute_resolution_policy_checksum,
    compute_resolution_result_checksum,
    compute_resolution_input_fingerprint,
    compute_resolved_entity_checksum,
)
from src.domain.entity_resolution.ports import (
    EntityResolutionPolicyRepositoryPort,
    EntityResolutionRepositoryPort,
)
from src.domain.security.models import validate_safe_identifier, SENSITIVE_KEYS

logger = logging.getLogger(__name__)


class JsonEntityResolutionRepositoryError(Exception):
    """Excepción base de persistencia Entity Resolution L.6."""


class EntityResolutionConflictError(JsonEntityResolutionRepositoryError):
    """Conflicto semántico bajo la misma identidad de resolución o entidad canónica."""


class EntityResolutionPolicyConflictError(JsonEntityResolutionRepositoryError):
    """Conflicto semántico bajo la misma identidad/versión de política."""


class CorruptedEntityResolutionRecordError(JsonEntityResolutionRepositoryError):
    """Registro corrupto o checksum inválido; nunca se repara silenciosamente."""


CorruptedResolutionPolicyError = CorruptedEntityResolutionRecordError
CorruptedResolutionResultError = CorruptedEntityResolutionRecordError
CorruptedCanonicalEntityError = CorruptedEntityResolutionRecordError


def _encode(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "value"):
        return value.value
    if isinstance(value, (dict, MappingProxyType)):
        result = {}
        for key, val in value.items():
            key_text = str(key)
            if any(sensitive in key_text.lower() for sensitive in SENSITIVE_KEYS):
                result[key_text] = "[REDACTED]"
            else:
                result[key_text] = _encode(val)
        return result
    if isinstance(value, (list, tuple, set)):
        return [_encode(item) for item in value]
    return value


class JsonEntityResolutionPolicyRepository(EntityResolutionPolicyRepositoryPort):
    """Repositorio JSON atómico para políticas de resolución de entidades."""

    def __init__(self, base_directory: Union[str, Path]):
        self._base_dir = Path(base_directory)
        self._policies_dir = self._base_dir / "policies"
        self._policies_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _get_file_path(self, policy_id: str, version: str) -> Path:
        validate_safe_identifier(policy_id, "policy_id")
        validate_safe_identifier(version.replace(".", "_"), "version")
        filename = f"{policy_id}__v{version.replace('.', '_')}.json"
        return self._policies_dir / filename

    def save_policy(self, policy: EntityResolutionPolicy) -> EntityResolutionPolicy:
        with self._lock:
            file_path = self._get_file_path(policy.policy_id, policy.version)
            if file_path.exists():
                existing = self.get_policy(policy.policy_id, policy.version)
                if existing:
                    if existing.checksum == policy.checksum:
                        return existing
                    raise EntityResolutionPolicyConflictError(
                        f"Policy {policy.policy_id} v{policy.version} already exists with different content"
                    )

            data = {
                "policy_id": policy.policy_id,
                "name": policy.name,
                "version": policy.version,
                "entity_type": policy.entity_type.value,
                "strong_identifier_types": [t.value for t in policy.strong_identifier_types],
                "required_attributes": list(policy.required_attributes),
                "optional_attributes": list(policy.optional_attributes),
                "attribute_weights": {k: str(v) for k, v in policy.attribute_weights.items()},
                "match_threshold": str(policy.match_threshold),
                "possible_match_threshold": str(policy.possible_match_threshold),
                "allow_cross_source_sku_match": policy.allow_cross_source_sku_match,
                "require_exact_brand_match": policy.require_exact_brand_match,
                "allow_attribute_only_auto_match": policy.allow_attribute_only_auto_match,
                "checksum": policy.checksum,
                "metadata": _encode(policy.metadata),
            }

            temp_path = file_path.with_suffix(".tmp")
            try:
                with open(temp_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, sort_keys=True)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temp_path, file_path)
            except Exception as e:
                if temp_path.exists():
                    temp_path.unlink()
                raise e

            return policy

    def get_policy(self, policy_id: str, version: Optional[str] = None) -> Optional[EntityResolutionPolicy]:
        with self._lock:
            validate_safe_identifier(policy_id, "policy_id")
            if version:
                file_path = self._get_file_path(policy_id, version)
                if not file_path.exists():
                    return None
                return self._load_file(file_path)

            # Buscar la versión más alta
            matches = list(self._policies_dir.glob(f"{policy_id}__v*.json"))
            if not matches:
                return None
            matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            return self._load_file(matches[0])

    def get_latest_policy_for_entity_type(
        self,
        entity_type: Union[EntityType, str],
    ) -> Optional[EntityResolutionPolicy]:
        with self._lock:
            all_policies = self.list_policies(entity_type)
            if not all_policies:
                return None
            return all_policies[0]

    def list_policies(
        self,
        entity_type: Optional[Union[EntityType, str]] = None,
    ) -> Sequence[EntityResolutionPolicy]:
        with self._lock:
            norm_type = (
                EntityType(entity_type).value
                if isinstance(entity_type, EntityType)
                else (entity_type.upper() if entity_type else None)
            )
            policies = []
            for file_path in self._policies_dir.glob("*.json"):
                if file_path.name.endswith(".tmp"):
                    continue
                try:
                    pol = self._load_file(file_path)
                    if norm_type is None or pol.entity_type.value == norm_type:
                        policies.append(pol)
                except Exception as e:
                    logger.warning("Error reading policy file %s: %s", file_path, e)
            return tuple(policies)

    def _load_file(self, file_path: Path) -> EntityResolutionPolicy:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            raise CorruptedResolutionPolicyError(f"Cannot read policy file {file_path}: {e}")

        weights = {k: Decimal(str(v)) for k, v in data.get("attribute_weights", {}).items()}
        strong_types = tuple(IdentifierType(t) for t in data.get("strong_identifier_types", []))

        pol = EntityResolutionPolicy(
            policy_id=data["policy_id"],
            name=data["name"],
            version=data.get("version", "1.0.0"),
            entity_type=EntityType(data["entity_type"]),
            strong_identifier_types=strong_types,
            required_attributes=tuple(data.get("required_attributes", [])),
            optional_attributes=tuple(data.get("optional_attributes", [])),
            attribute_weights=weights,
            match_threshold=Decimal(data.get("match_threshold", "0.85")),
            possible_match_threshold=Decimal(data.get("possible_match_threshold", "0.50")),
            allow_cross_source_sku_match=data.get("allow_cross_source_sku_match", False),
            require_exact_brand_match=data.get("require_exact_brand_match", True),
            allow_attribute_only_auto_match=data.get("allow_attribute_only_auto_match", False),
            checksum=data.get("checksum", ""),
            metadata=data.get("metadata", {}),
        )

        expected_checksum = compute_resolution_policy_checksum(pol)
        if pol.checksum != expected_checksum:
            raise CorruptedResolutionPolicyError(
                f"Policy {pol.policy_id} checksum mismatch: stored={pol.checksum}, computed={expected_checksum}"
            )
        return pol


class JsonEntityResolutionRepository(EntityResolutionRepositoryPort):
    """Repositorio JSON atómico para resultados de resolución y entidades canónicas resueltas."""

    def __init__(self, base_directory: Union[str, Path]):
        self._base_dir = Path(base_directory)
        self._resolutions_dir = self._base_dir / "resolutions"
        self._canonical_dir = self._base_dir / "canonical_entities"
        self._resolutions_dir.mkdir(parents=True, exist_ok=True)
        self._canonical_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _get_resolution_path(self, resolution_id: str) -> Path:
        validate_safe_identifier(resolution_id, "resolution_id")
        return self._resolutions_dir / f"{resolution_id}.json"

    def _get_canonical_path(self, canonical_entity_id: str) -> Path:
        validate_safe_identifier(canonical_entity_id, "canonical_entity_id")
        return self._canonical_dir / f"{canonical_entity_id}.json"

    # ------------------------------------------------------------------------
    # Resoluciones
    # ------------------------------------------------------------------------

    def save_resolution(self, result: EntityResolutionResult) -> EntityResolutionResult:
        with self._lock:
            file_path = self._get_resolution_path(result.resolution_id)
            if file_path.exists():
                existing = self.get_resolution(result.resolution_id)
                if existing:
                    # Replay idempotente del MISMO logical input -> devolver el registro previo.
                    # Se compara la fingerprint lógica (input_fingerprint), que NO depende de
                    # `resolved_at`, para que un replay a otro instante de reloj no genere
                    # conflicto ni una segunda resolución física.
                    if existing.input_fingerprint == result.input_fingerprint:
                        return existing
                    raise EntityResolutionConflictError(
                        f"Resolution {result.resolution_id} already exists with different semantic input"
                    )

            data = {
                "resolution_id": result.resolution_id,
                "entity_type": result.entity_type.value,
                "status": result.status.value,
                "canonical_entity_id": result.canonical_entity_id,
                "reference_a": self._serialize_reference(result.reference_a),
                "reference_b": self._serialize_reference(result.reference_b),
                "matched_identifiers": list(result.matched_identifiers),
                "mismatched_identifiers": list(result.mismatched_identifiers),
                "matched_attributes": list(result.matched_attributes),
                "mismatched_attributes": list(result.mismatched_attributes),
                "missing_attributes": list(result.missing_attributes),
                "confidence_score": str(result.confidence_score) if result.confidence_score is not None else None,
                "reason_codes": list(result.reason_codes),
                "policy_id": result.policy_id,
                "policy_version": result.policy_version,
                "resolved_at": result.resolved_at.isoformat(),
                "correlation_id": result.correlation_id,
                "input_fingerprint": result.input_fingerprint,
                "checksum": result.checksum,
                "metadata": _encode(result.metadata),
            }

            temp_path = file_path.with_suffix(".tmp")
            try:
                with open(temp_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, sort_keys=True)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temp_path, file_path)
            except Exception as e:
                if temp_path.exists():
                    temp_path.unlink()
                raise e

            return result

    def get_resolution(self, resolution_id: str) -> Optional[EntityResolutionResult]:
        with self._lock:
            file_path = self._get_resolution_path(resolution_id)
            if not file_path.exists():
                return None
            return self._load_resolution_file(file_path)

    def find_resolutions_by_reference(
        self,
        source_id: str,
        source_entity_id: str,
    ) -> Sequence[EntityResolutionResult]:
        with self._lock:
            results: List[EntityResolutionResult] = []
            for file_path in self._resolutions_dir.glob("*.json"):
                if file_path.name.endswith(".tmp"):
                    continue
                res = self._load_resolution_file(file_path)
                match_a = (res.reference_a.source_id == source_id and res.reference_a.source_entity_id == source_entity_id)
                match_b = (res.reference_b.source_id == source_id and res.reference_b.source_entity_id == source_entity_id)
                if match_a or match_b:
                    results.append(res)
            return tuple(results)

    # ------------------------------------------------------------------------
    # Entidades Canónicas
    # ------------------------------------------------------------------------

    def save_canonical_entity(self, entity: ResolvedEntity) -> ResolvedEntity:
        with self._lock:
            file_path = self._get_canonical_path(entity.canonical_entity_id)
            if file_path.exists():
                existing = self.get_canonical_entity(entity.canonical_entity_id)
                if existing:
                    if existing.checksum == entity.checksum:
                        return existing

            data = {
                "canonical_entity_id": entity.canonical_entity_id,
                "entity_type": entity.entity_type.value,
                "primary_identifiers": [
                    {
                        "identifier_type": i.identifier_type.value,
                        "value": i.value,
                        "namespace": i.namespace,
                        "is_strong": i.is_strong,
                        "metadata": _encode(i.metadata),
                    }
                    for i in entity.primary_identifiers
                ],
                "member_references": [self._serialize_reference(r) for r in entity.member_references],
                "resolution_ids": list(entity.resolution_ids),
                "canonical_attributes": _encode(entity.canonical_attributes),
                "created_at": entity.created_at.isoformat(),
                "updated_at": entity.updated_at.isoformat(),
                "schema_version": entity.schema_version,
                "checksum": entity.checksum,
                "metadata": _encode(entity.metadata),
            }

            temp_path = file_path.with_suffix(".tmp")
            try:
                with open(temp_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, sort_keys=True)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temp_path, file_path)
            except Exception as e:
                if temp_path.exists():
                    temp_path.unlink()
                raise e

            return entity

    def get_canonical_entity(self, canonical_entity_id: str) -> Optional[ResolvedEntity]:
        with self._lock:
            file_path = self._get_canonical_path(canonical_entity_id)
            if not file_path.exists():
                return None
            return self._load_canonical_file(file_path)

    def find_canonical_by_reference(
        self,
        source_id: str,
        source_entity_id: str,
    ) -> Optional[ResolvedEntity]:
        with self._lock:
            for file_path in self._canonical_dir.glob("*.json"):
                if file_path.name.endswith(".tmp"):
                    continue
                ent = self._load_canonical_file(file_path)
                for ref in ent.member_references:
                    if ref.source_id == source_id and ref.source_entity_id == source_entity_id:
                        return ent
            return None

    def list_canonical_entities(
        self,
        entity_type: Optional[Union[EntityType, str]] = None,
    ) -> Sequence[ResolvedEntity]:
        with self._lock:
            norm_type = (
                EntityType(entity_type).value
                if isinstance(entity_type, EntityType)
                else (entity_type.upper() if entity_type else None)
            )
            entities = []
            for file_path in self._canonical_dir.glob("*.json"):
                if file_path.name.endswith(".tmp"):
                    continue
                ent = self._load_canonical_file(file_path)
                if norm_type is None or ent.entity_type.value == norm_type:
                    entities.append(ent)
            return tuple(entities)

    # ------------------------------------------------------------------------
    # Helpers de Serialización / Carga
    # ------------------------------------------------------------------------

    def _serialize_reference(self, ref: EntityReference) -> Dict[str, Any]:
        return {
            "entity_type": ref.entity_type.value,
            "source_id": ref.source_id,
            "source_entity_id": ref.source_entity_id,
            "canonical_attributes": _encode(ref.canonical_attributes),
            "identifiers": [
                {
                    "identifier_type": i.identifier_type.value,
                    "value": i.value,
                    "namespace": i.namespace,
                    "is_strong": i.is_strong,
                    "metadata": _encode(i.metadata),
                }
                for i in ref.identifiers
            ],
            "provenance_id": ref.provenance_id,
            "schema_version": ref.schema_version,
            "schema_validation_status": ref.schema_validation_status,
            "checksum": ref.checksum,
            "metadata": _encode(ref.metadata),
        }

    def _deserialize_reference(self, data: Dict[str, Any]) -> EntityReference:
        idents = tuple(
            EntityIdentifier(
                identifier_type=IdentifierType(i["identifier_type"]),
                value=i["value"],
                namespace=i.get("namespace"),
                is_strong=i.get("is_strong", False),
                metadata=i.get("metadata", {}),
            )
            for i in data.get("identifiers", [])
        )
        return EntityReference(
            entity_type=EntityType(data["entity_type"]),
            source_id=data["source_id"],
            source_entity_id=data["source_entity_id"],
            canonical_attributes=data.get("canonical_attributes", {}),
            identifiers=idents,
            provenance_id=data.get("provenance_id"),
            schema_version=data.get("schema_version", "1.0.0"),
            schema_validation_status=data.get("schema_validation_status"),
            checksum=data.get("checksum", ""),
            metadata=data.get("metadata", {}),
        )

    def _load_resolution_file(self, file_path: Path) -> EntityResolutionResult:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            raise CorruptedResolutionResultError(f"Cannot read resolution file {file_path}: {e}")

        ref_a = self._deserialize_reference(data["reference_a"])
        ref_b = self._deserialize_reference(data["reference_b"])

        score_val = data.get("confidence_score")
        score = Decimal(str(score_val)) if score_val is not None else None

        res = EntityResolutionResult(
            resolution_id=data["resolution_id"],
            entity_type=EntityType(data["entity_type"]),
            status=MatchStatus(data["status"]),
            reference_a=ref_a,
            reference_b=ref_b,
            canonical_entity_id=data.get("canonical_entity_id"),
            matched_identifiers=tuple(data.get("matched_identifiers", [])),
            mismatched_identifiers=tuple(data.get("mismatched_identifiers", [])),
            matched_attributes=tuple(data.get("matched_attributes", [])),
            mismatched_attributes=tuple(data.get("mismatched_attributes", [])),
            missing_attributes=tuple(data.get("missing_attributes", [])),
            confidence_score=score,
            reason_codes=tuple(data.get("reason_codes", [])),
            policy_id=data.get("policy_id", "default_policy"),
            policy_version=data.get("policy_version", "1.0.0"),
            resolved_at=datetime.fromisoformat(data["resolved_at"]),
            correlation_id=data.get("correlation_id", "default-correlation"),
            input_fingerprint=data.get("input_fingerprint", ""),
            checksum=data.get("checksum", ""),
            metadata=data.get("metadata", {}),
        )

        expected_checksum = compute_resolution_result_checksum(res)
        if res.checksum != expected_checksum:
            raise CorruptedResolutionResultError(
                f"Resolution {res.resolution_id} checksum mismatch: stored={res.checksum}, computed={expected_checksum}"
            )
        expected_fingerprint = compute_resolution_input_fingerprint(res)
        if res.input_fingerprint != expected_fingerprint:
            raise CorruptedResolutionResultError(
                f"Resolution {res.resolution_id} input_fingerprint mismatch: stored={res.input_fingerprint}, computed={expected_fingerprint}"
            )
        return res

    def _load_canonical_file(self, file_path: Path) -> ResolvedEntity:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            raise CorruptedCanonicalEntityError(f"Cannot read canonical entity file {file_path}: {e}")

        primary_idents = tuple(
            EntityIdentifier(
                identifier_type=IdentifierType(i["identifier_type"]),
                value=i["value"],
                namespace=i.get("namespace"),
                is_strong=i.get("is_strong", False),
                metadata=i.get("metadata", {}),
            )
            for i in data.get("primary_identifiers", [])
        )
        members = tuple(self._deserialize_reference(r) for r in data.get("member_references", []))

        ent = ResolvedEntity(
            canonical_entity_id=data["canonical_entity_id"],
            entity_type=EntityType(data["entity_type"]),
            primary_identifiers=primary_idents,
            member_references=members,
            resolution_ids=tuple(data.get("resolution_ids", [])),
            canonical_attributes=data.get("canonical_attributes", {}),
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
            schema_version=data.get("schema_version", "1.0.0"),
            checksum=data.get("checksum", ""),
            metadata=data.get("metadata", {}),
        )

        expected_checksum = compute_resolved_entity_checksum(ent)
        if ent.checksum != expected_checksum:
            raise CorruptedCanonicalEntityError(
                f"Canonical entity {ent.canonical_entity_id} checksum mismatch: stored={ent.checksum}, computed={expected_checksum}"
            )
        return ent
