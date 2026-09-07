"""
Application Service for N.9 — Sensitive Data Handling (Transversal N — Security, Governance & Safety).

Responsabilidades:
- Recibe DataHandlingRequest.
- Clasifica deterministamente el payload utilizando SensitiveDataClassifierPort.
- Resuelve la política de manejo aplicable (DataHandlingPolicy).
- Evalúa permisos de logging, caching, persistencia y transferencia externa según Propósito.
- Aplica redacción y minimización estricta mediante SensitiveDataRedactorPort.
- Genera identificadores opacos seguros (fingerprints deterministas) para lookups/caché.
- Emite eventos de auditoría (K.1) cuando corresponde sin filtrar datos en texto claro.
"""

from datetime import datetime, timezone
import logging
from types import MappingProxyType
from typing import Optional, Dict, Any, Sequence, Union
import uuid

from src.domain.security.sensitive_data_models import (
    DataClassification,
    SensitiveCategory,
    DataHandlingPurpose,
    PersistenceHandlingMode,
    CacheHandlingMode,
    DataHandlingReasonCode,
    DataHandlingPolicy,
    DataHandlingRequest,
    DataHandlingDecision,
    RedactionResult,
    SensitiveDataClassification,
    compute_deterministic_fingerprint,
)
from src.domain.security.sensitive_data_ports import (
    SensitiveDataClassifierPort,
    SensitiveDataRedactorPort,
    DataHandlingPolicyRepositoryPort,
    SensitiveDataHandlingServicePort,
)
from src.domain.security.sensitive_data_engine import (
    DeterministicSensitiveDataClassifier,
    DeterministicSensitiveDataRedactor,
)
from src.domain.audit.models import AuditRecord, AuditRecordType, AuditActor, AuditActorType
from src.domain.audit.ports import AuditRepositoryPort
from src.domain.security.models import sanitize_security_data

logger = logging.getLogger(__name__)


class SensitiveDataHandlingService(SensitiveDataHandlingServicePort):
    """
    Servicio de aplicación para el gobierno, minimización y redacción de datos sensibles (N.9).
    """

    def __init__(
        self,
        policy_repository: Optional[DataHandlingPolicyRepositoryPort] = None,
        default_policy_name: str = "default_sensitive_data_policy",
        classifier: Optional[SensitiveDataClassifierPort] = None,
        redactor: Optional[SensitiveDataRedactorPort] = None,
        audit_repository: Optional[AuditRepositoryPort] = None,
        clock: Optional[Any] = None,
    ):
        self.policy_repository = policy_repository
        self.default_policy_name = default_policy_name
        self.classifier = classifier or DeterministicSensitiveDataClassifier()
        self.redactor = redactor or DeterministicSensitiveDataRedactor()
        self.audit_repository = audit_repository
        self.clock = clock

    def _get_current_time(self) -> datetime:
        if self.clock is not None and hasattr(self.clock, "now"):
            now_val = self.clock.now()
            if now_val.tzinfo is None:
                return now_val.replace(tzinfo=timezone.utc)
            return now_val
        return datetime.now(timezone.utc)

    def _resolve_policy(self, policy_name: Optional[str]) -> Optional[DataHandlingPolicy]:
        target_name = policy_name or self.default_policy_name
        if self.policy_repository is not None:
            try:
                pol = self.policy_repository.get_policy(target_name)
                if pol is not None:
                    return pol
                # Si se especificó explícitamente una política inexistente, no hacer fallback permisivo
                if policy_name is not None and policy_name != self.default_policy_name:
                    return None
            except Exception as e:
                logger.error(f"Failed to load policy '{target_name}': {e}")
                return None

        # Fallback default in-memory policy sólo si es la política por defecto
        if target_name == self.default_policy_name:
            return DataHandlingPolicy(
                policy_name=target_name,
                version="1.0.0",
                description="Default built-in fail-secure sensitive data handling policy",
                default_classification=DataClassification.INTERNAL,
                allowed_purposes=(
                    DataHandlingPurpose.INFERENCE,
                    DataHandlingPurpose.AUDIT,
                    DataHandlingPurpose.MARKETPLACE_OPERATION,
                    DataHandlingPurpose.ORDER_FULFILLMENT,
                    DataHandlingPurpose.SUPPLIER_CONTACT,
                    DataHandlingPurpose.CACHE,
                    DataHandlingPurpose.LOGGING,
                    DataHandlingPurpose.STORAGE,
                    DataHandlingPurpose.GENERAL,
                ),
                logging_allowed_classes=(
                    DataClassification.PUBLIC,
                    DataClassification.INTERNAL,
                ),
                cache_allowed_classes=(
                    DataClassification.PUBLIC,
                    DataClassification.INTERNAL,
                    DataClassification.CONFIDENTIAL,
                ),
                external_transfer_allowed_classes=(
                    DataClassification.PUBLIC,
                    DataClassification.INTERNAL,
                    DataClassification.CONFIDENTIAL,
                    DataClassification.SENSITIVE,
                ),
                persistence_mode=PersistenceHandlingMode.ALLOW,
                cache_mode=CacheHandlingMode.SANITIZED_ONLY,
            )
        return None

    def evaluate(self, request: DataHandlingRequest) -> DataHandlingDecision:
        """
        Evalúa y aplica la política de manejo sobre el request.
        """
        eval_time = self._get_current_time()
        decision_id = f"ddec_{uuid.uuid4().hex[:12]}"
        policy_name = request.policy_name or self.default_policy_name

        # 1. Clasificar payload
        classification = self.classifier.classify(request.payload, context_purpose=request.purpose)

        # 2. Resolver política
        policy = self._resolve_policy(policy_name)
        if policy is None:
            # Fail-secure
            return DataHandlingDecision(
                decision_id=decision_id,
                classification=classification,
                purpose=request.purpose,
                policy_name=policy_name,
                policy_version="0.0.0",
                is_allowed_for_purpose=False,
                logging_allowed=False,
                cache_allowed=False,
                external_transfer_allowed=False,
                persistence_mode=PersistenceHandlingMode.DENY,
                cache_mode=CacheHandlingMode.DENY,
                reason_code=DataHandlingReasonCode.POLICY_NOT_FOUND,
                reason_details=f"Sensitive data policy '{policy_name}' not found or corrupted (Fail-Secure)",
                redaction_result=None,
                safe_identifiers={},
            )

        # 3. Validar si UNKNOWN classification -> Fail-Secure
        if classification.overall_classification == DataClassification.UNKNOWN:
            return DataHandlingDecision(
                decision_id=decision_id,
                classification=classification,
                purpose=request.purpose,
                policy_name=policy.policy_name,
                policy_version=policy.version,
                is_allowed_for_purpose=False,
                logging_allowed=False,
                cache_allowed=False,
                external_transfer_allowed=False,
                persistence_mode=PersistenceHandlingMode.DENY,
                cache_mode=CacheHandlingMode.DENY,
                reason_code=DataHandlingReasonCode.UNKNOWN_CLASSIFICATION_FAIL_SECURE,
                reason_details="UNKNOWN data classification cannot be treated as PUBLIC (Fail-Secure)",
                redaction_result=None,
                safe_identifiers={},
            )

        # 4. Evaluar permisos específicos
        # Logging
        logging_allowed = (
            classification.overall_classification in policy.logging_allowed_classes
            and not any(c in policy.prohibited_categories_for_logging for c in classification.all_categories)
        )

        # Cache
        cache_allowed = (
            classification.overall_classification in policy.cache_allowed_classes
            and not any(c in policy.prohibited_categories_for_cache for c in classification.all_categories)
            and policy.cache_mode != CacheHandlingMode.DENY
        )

        # External transfer
        external_transfer_allowed = (
            classification.overall_classification in policy.external_transfer_allowed_classes
            and request.purpose in policy.allowed_purposes
        )

        # Redacción y minimización
        redaction_res = self.redactor.redact(
            data=request.payload,
            policy=policy,
            purpose=request.purpose,
            required_fields=request.required_fields,
        )

        # Generación de safe fingerprints para identidades sensibles
        safe_identifiers = {}
        if isinstance(request.payload, dict):
            for k in ("buyer_id", "customer_id", "email", "phone", "order_id", "account_id"):
                if k in request.payload and request.payload[k] is not None:
                    safe_identifiers[f"safe_{k}_fingerprint"] = compute_deterministic_fingerprint(request.payload[k])

        # Determinar razón de decisión
        reason_code = DataHandlingReasonCode.ALLOWED_BY_POLICY
        reason_details = "Data handling permitted by policy"
        if redaction_res.fields_redacted_count > 0:
            reason_code = DataHandlingReasonCode.REDACTED_BY_POLICY
            reason_details = f"Data redacted deterministically ({redaction_res.fields_redacted_count} fields protected)"
        elif request.required_fields:
            reason_code = DataHandlingReasonCode.MINIMIZED_BY_POLICY
            reason_details = "Payload minimized according to operational purpose"

        decision = DataHandlingDecision(
            decision_id=decision_id,
            classification=classification,
            purpose=request.purpose,
            policy_name=policy.policy_name,
            policy_version=policy.version,
            is_allowed_for_purpose=True,
            logging_allowed=logging_allowed,
            cache_allowed=cache_allowed,
            external_transfer_allowed=external_transfer_allowed,
            persistence_mode=policy.persistence_mode,
            cache_mode=policy.cache_mode,
            reason_code=reason_code,
            reason_details=reason_details,
            redaction_result=redaction_res,
            safe_identifiers=safe_identifiers,
        )

        # Emitir auditoría determinista K.1 si se configuró repositorio
        if self.audit_repository is not None and request.purpose != DataHandlingPurpose.AUDIT:
            try:
                audit_rec = AuditRecord(
                    audit_id=f"aud_n9_{decision_id}",
                    record_type=AuditRecordType.POLICY_EVALUATED,
                    occurred_at=eval_time,
                    actor=AuditActor(actor_type=AuditActorType.POLICY_ENGINE, actor_id="sensitive_data_service"),
                    subject_type="SENSITIVE_DATA",
                    subject_id=policy.policy_name,
                    action_or_operation=f"DATA_HANDLING_{request.purpose.value}",
                    status="EVALUATED",
                    correlation_id=request.correlation_id or decision_id,
                    metadata={
                        "decision_id": decision_id,
                        "overall_classification": classification.overall_classification.value,
                        "primary_category": classification.primary_category.value,
                        "reason_code": reason_code.value,
                        "logging_allowed": logging_allowed,
                        "cache_allowed": cache_allowed,
                        "fields_redacted_count": redaction_res.fields_redacted_count,
                    },
                )
                self.audit_repository.append(audit_rec)
            except Exception as e:
                logger.warning(f"Could not record audit trail for N.9: {e}")

        return decision

    def sanitize_for_audit(self, payload: Any, correlation_id: str = "") -> Any:
        """
        Helper para sanitizar estructuras antes de persistir en K.1 Audit Trail o K.2 Agent Trace.
        """
        req = DataHandlingRequest(
            payload=payload,
            purpose=DataHandlingPurpose.AUDIT,
            correlation_id=correlation_id,
        )
        dec = self.evaluate(req)
        return dec.redaction_result.sanitized_payload if dec.redaction_result else sanitize_security_data(payload)

    def sanitize_for_cache(self, payload: Any, policy_name: Optional[str] = None) -> Any:
        """
        Helper para sanitizar y validar cacheabilidad en M.4 Caching.
        """
        req = DataHandlingRequest(
            payload=payload,
            purpose=DataHandlingPurpose.CACHE,
            policy_name=policy_name,
        )
        dec = self.evaluate(req)
        if not dec.cache_allowed:
            return None
        return dec.redaction_result.sanitized_payload if dec.redaction_result else payload

    def sanitize_for_inference(self, prompt_or_context: Any, required_business_fields: Sequence[str] = ()) -> Any:
        """
        Helper para minimizar y redactar antes de enviar a inferencia/LLM (M.2 / M.3).
        """
        req = DataHandlingRequest(
            payload=prompt_or_context,
            purpose=DataHandlingPurpose.INFERENCE,
            required_fields=tuple(required_business_fields),
        )
        dec = self.evaluate(req)
        return dec.redaction_result.sanitized_payload if dec.redaction_result else prompt_or_context
