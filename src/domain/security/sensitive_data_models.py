"""
Domain Models for N.9 — Sensitive Data Handling (Transversal N — Security, Governance & Safety).

Responde deterministamente a la pregunta:
"¿Cómo clasifica, minimiza, protege, redacta y controla el sistema los datos sensibles
que atraviesan dominio, memoria, prompts, logs, auditoría, trazas, cachés y persistencia?"

Principios de Seguridad y Privacidad N.9:
- Taxonomía de Clasificación Explícita: PUBLIC, INTERNAL, CONFIDENTIAL, SENSITIVE, RESTRICTED, UNKNOWN.
- Fail-Secure: UNKNOWN nunca se trata como PUBLIC; se asume restrictivo por defecto.
- Categorías Sensibles Reales: PERSONAL_DATA, CONTACT_DATA, ADDRESS_DATA, FINANCIAL_DATA, ORDER_DATA,
  SUPPLIER_CONFIDENTIAL, MARKETPLACE_ACCOUNT_DATA, PRIVATE_PROMPT_CONTEXT, BUSINESS_CONFIDENTIAL, UNKNOWN.
- Propósitos Estructurados (Purposes): INFERENCE, AUDIT, MARKETPLACE_OPERATION, ORDER_FULFILLMENT,
  SUPPLIER_CONTACT, CACHE, LOGGING, STORAGE.
- Separación de Modelos:
  - Raw Protected Value vs Safe Representation.
  - Secret Management (N.5) maneja credenciales técnicas; N.9 maneja datos sensibles de negocio/operativos/PII.
  - Hashing != Anonimización automática. Masking != Eliminación física.
- Redacción Recursiva y Determinista para dict, list, tuple, dataclasses y strings anidados.
- Minimización de Datos: sólo los campos permitidos y estrictamente necesarios son expuestos según Purpose.
- Inmutabilidad estricta (frozen=True, MappingProxyType, tuplas).
- Checksums SHA-256 canónicos para trazabilidad e integridad.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import re
from types import MappingProxyType
from typing import Mapping, Optional, Any, Tuple, Sequence, Dict, Union, Set

from src.domain.security.models import (
    sanitize_security_data,
    deep_freeze,
    validate_safe_identifier,
)


class DataClassification(str, Enum):
    """
    Taxonomía canónica de clasificación de seguridad de datos (N.9).
    Orden de sensibilidad creciente: PUBLIC < INTERNAL < CONFIDENTIAL < SENSITIVE < RESTRICTED.
    UNKNOWN es tratado con fail-secure (nunca equivalente a PUBLIC).
    """
    PUBLIC = "PUBLIC"
    INTERNAL = "INTERNAL"
    CONFIDENTIAL = "CONFIDENTIAL"
    SENSITIVE = "SENSITIVE"
    RESTRICTED = "RESTRICTED"
    UNKNOWN = "UNKNOWN"


class SensitiveCategory(str, Enum):
    """
    Categorías taxonómicas deterministas de datos sensibles en el sistema de comercio autónomo.
    """
    PERSONAL_DATA = "PERSONAL_DATA"
    CONTACT_DATA = "CONTACT_DATA"
    ADDRESS_DATA = "ADDRESS_DATA"
    FINANCIAL_DATA = "FINANCIAL_DATA"
    ORDER_DATA = "ORDER_DATA"
    SUPPLIER_CONFIDENTIAL = "SUPPLIER_CONFIDENTIAL"
    MARKETPLACE_ACCOUNT_DATA = "MARKETPLACE_ACCOUNT_DATA"
    PRIVATE_PROMPT_CONTEXT = "PRIVATE_PROMPT_CONTEXT"
    BUSINESS_CONFIDENTIAL = "BUSINESS_CONFIDENTIAL"
    TECHNICAL_SECRET = "TECHNICAL_SECRET"
    UNKNOWN = "UNKNOWN"


class DataHandlingPurpose(str, Enum):
    """
    Propósito o contexto operativo para el cual se solicita/evalúa el manejo de datos sensibles.
    """
    INFERENCE = "INFERENCE"
    AUDIT = "AUDIT"
    MARKETPLACE_OPERATION = "MARKETPLACE_OPERATION"
    ORDER_FULFILLMENT = "ORDER_FULFILLMENT"
    SUPPLIER_CONTACT = "SUPPLIER_CONTACT"
    CACHE = "CACHE"
    LOGGING = "LOGGING"
    STORAGE = "STORAGE"
    GENERAL = "GENERAL"


class PersistenceHandlingMode(str, Enum):
    """
    Modo permitido para la persistencia de datos sensibles.
    """
    ALLOW = "ALLOW"
    REDACT = "REDACT"
    REFERENCE_ONLY = "REFERENCE_ONLY"
    DENY = "DENY"


class CacheHandlingMode(str, Enum):
    """
    Modo permitido para caching (M.4) de datos sensibles.
    """
    ALLOW = "ALLOW"
    SANITIZED_ONLY = "SANITIZED_ONLY"
    DENY = "DENY"


class DataHandlingReasonCode(str, Enum):
    """
    Códigos estructurados y deterministas de decisión de manejo de datos.
    """
    ALLOWED_BY_POLICY = "ALLOWED_BY_POLICY"
    REDACTED_BY_POLICY = "REDACTED_BY_POLICY"
    MINIMIZED_BY_POLICY = "MINIMIZED_BY_POLICY"
    RESTRICTED_FOR_PURPOSE = "RESTRICTED_FOR_PURPOSE"
    UNKNOWN_CLASSIFICATION_FAIL_SECURE = "UNKNOWN_CLASSIFICATION_FAIL_SECURE"
    LOGGING_PROHIBITED = "LOGGING_PROHIBITED"
    CACHE_PROHIBITED = "CACHE_PROHIBITED"
    EXTERNAL_TRANSFER_PROHIBITED = "EXTERNAL_TRANSFER_PROHIBITED"
    PERSISTENCE_PROHIBITED = "PERSISTENCE_PROHIBITED"
    SECRET_DETECTED_AND_REDACTED = "SECRET_DETECTED_AND_REDACTED"
    DEFAULT_FAIL_SECURE = "DEFAULT_FAIL_SECURE"
    POLICY_NOT_FOUND = "POLICY_NOT_FOUND"
    CORRUPTED_POLICY_OR_CHECKSUM_INVALID = "CORRUPTED_POLICY_OR_CHECKSUM_INVALID"
    EVALUATION_ERROR = "EVALUATION_ERROR"


# Expresiones regulares deterministas para detección y redacción de datos sensibles comunes
EMAIL_REGEX = re.compile(r"([a-zA-Z0-9_.+-]+)@([a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)")
PHONE_REGEX = re.compile(r"(\+?[0-9]{1,4}[\s-]?)?(\(?\d{2,4}\)?[\s-]?)?[\d\s-]{6,14}\d")
RUT_DNI_REGEX = re.compile(r"\b(\d{1,2}\.?\d{3}\.?\d{3}[-‐‑]?[0-9kK]|\d{7,10})\b")
CREDIT_CARD_REGEX = re.compile(r"\b(?:\d{4}[ -]?){3}\d{4}\b")


# Conjunto canónico de nombres de campos sensibles de negocio y PII
SENSITIVE_FIELD_NAMES: Mapping[str, Tuple[DataClassification, SensitiveCategory]] = {
    # PII & Contact
    "email": (DataClassification.SENSITIVE, SensitiveCategory.CONTACT_DATA),
    "user_email": (DataClassification.SENSITIVE, SensitiveCategory.CONTACT_DATA),
    "buyer_email": (DataClassification.SENSITIVE, SensitiveCategory.CONTACT_DATA),
    "seller_email": (DataClassification.SENSITIVE, SensitiveCategory.CONTACT_DATA),
    "phone": (DataClassification.SENSITIVE, SensitiveCategory.CONTACT_DATA),
    "telephone": (DataClassification.SENSITIVE, SensitiveCategory.CONTACT_DATA),
    "phone_number": (DataClassification.SENSITIVE, SensitiveCategory.CONTACT_DATA),
    "mobile": (DataClassification.SENSITIVE, SensitiveCategory.CONTACT_DATA),
    "receiver_phone": (DataClassification.SENSITIVE, SensitiveCategory.CONTACT_DATA),
    "first_name": (DataClassification.SENSITIVE, SensitiveCategory.PERSONAL_DATA),
    "last_name": (DataClassification.SENSITIVE, SensitiveCategory.PERSONAL_DATA),
    "full_name": (DataClassification.SENSITIVE, SensitiveCategory.PERSONAL_DATA),
    "buyer_name": (DataClassification.SENSITIVE, SensitiveCategory.PERSONAL_DATA),
    "receiver_name": (DataClassification.SENSITIVE, SensitiveCategory.PERSONAL_DATA),
    "customer_name": (DataClassification.SENSITIVE, SensitiveCategory.PERSONAL_DATA),
    "contact_name": (DataClassification.SENSITIVE, SensitiveCategory.PERSONAL_DATA),
    # DNI / Tax / RUT / Passport
    "rut": (DataClassification.RESTRICTED, SensitiveCategory.PERSONAL_DATA),
    "dni": (DataClassification.RESTRICTED, SensitiveCategory.PERSONAL_DATA),
    "tax_id": (DataClassification.CONFIDENTIAL, SensitiveCategory.PERSONAL_DATA),
    "national_id": (DataClassification.RESTRICTED, SensitiveCategory.PERSONAL_DATA),
    "passport": (DataClassification.RESTRICTED, SensitiveCategory.PERSONAL_DATA),
    "ssn": (DataClassification.RESTRICTED, SensitiveCategory.PERSONAL_DATA),
    # Address
    "street_name": (DataClassification.SENSITIVE, SensitiveCategory.ADDRESS_DATA),
    "street_number": (DataClassification.SENSITIVE, SensitiveCategory.ADDRESS_DATA),
    "address_line": (DataClassification.SENSITIVE, SensitiveCategory.ADDRESS_DATA),
    "address": (DataClassification.SENSITIVE, SensitiveCategory.ADDRESS_DATA),
    "shipping_address": (DataClassification.SENSITIVE, SensitiveCategory.ADDRESS_DATA),
    "billing_address": (DataClassification.SENSITIVE, SensitiveCategory.ADDRESS_DATA),
    "address_complement": (DataClassification.SENSITIVE, SensitiveCategory.ADDRESS_DATA),
    "zipcode": (DataClassification.CONFIDENTIAL, SensitiveCategory.ADDRESS_DATA),
    "postal_code": (DataClassification.CONFIDENTIAL, SensitiveCategory.ADDRESS_DATA),
    # Financial
    "card_number": (DataClassification.RESTRICTED, SensitiveCategory.FINANCIAL_DATA),
    "pan": (DataClassification.RESTRICTED, SensitiveCategory.FINANCIAL_DATA),
    "cvv": (DataClassification.RESTRICTED, SensitiveCategory.FINANCIAL_DATA),
    "bank_account": (DataClassification.RESTRICTED, SensitiveCategory.FINANCIAL_DATA),
    "iban": (DataClassification.RESTRICTED, SensitiveCategory.FINANCIAL_DATA),
    "account_number": (DataClassification.RESTRICTED, SensitiveCategory.FINANCIAL_DATA),
    "payment_method_id": (DataClassification.CONFIDENTIAL, SensitiveCategory.FINANCIAL_DATA),
    "financial_details": (DataClassification.CONFIDENTIAL, SensitiveCategory.FINANCIAL_DATA),
    # Technical Secrets & Tokens (N.5 overlap recognition)
    "password": (DataClassification.RESTRICTED, SensitiveCategory.TECHNICAL_SECRET),
    "secret": (DataClassification.RESTRICTED, SensitiveCategory.TECHNICAL_SECRET),
    "token": (DataClassification.RESTRICTED, SensitiveCategory.TECHNICAL_SECRET),
    "api_key": (DataClassification.RESTRICTED, SensitiveCategory.TECHNICAL_SECRET),
    "apikey": (DataClassification.RESTRICTED, SensitiveCategory.TECHNICAL_SECRET),
    "private_key": (DataClassification.RESTRICTED, SensitiveCategory.TECHNICAL_SECRET),
    "access_token": (DataClassification.RESTRICTED, SensitiveCategory.TECHNICAL_SECRET),
    "refresh_token": (DataClassification.RESTRICTED, SensitiveCategory.TECHNICAL_SECRET),
    "auth_header": (DataClassification.RESTRICTED, SensitiveCategory.TECHNICAL_SECRET),
    "bearer": (DataClassification.RESTRICTED, SensitiveCategory.TECHNICAL_SECRET),
    # AI Scratchpad / Private reasoning (K.2 / M.2)
    "chain_of_thought": (DataClassification.RESTRICTED, SensitiveCategory.PRIVATE_PROMPT_CONTEXT),
    "reasoning": (DataClassification.RESTRICTED, SensitiveCategory.PRIVATE_PROMPT_CONTEXT),
    "reasoning_tokens": (DataClassification.RESTRICTED, SensitiveCategory.PRIVATE_PROMPT_CONTEXT),
    "internal_scratchpad": (DataClassification.RESTRICTED, SensitiveCategory.PRIVATE_PROMPT_CONTEXT),
    "private_prompt": (DataClassification.CONFIDENTIAL, SensitiveCategory.PRIVATE_PROMPT_CONTEXT),
    "raw_prompt": (DataClassification.CONFIDENTIAL, SensitiveCategory.PRIVATE_PROMPT_CONTEXT),
    # Supplier Confidential
    "supplier_cost": (DataClassification.CONFIDENTIAL, SensitiveCategory.SUPPLIER_CONFIDENTIAL),
    "wholesale_discount": (DataClassification.CONFIDENTIAL, SensitiveCategory.SUPPLIER_CONFIDENTIAL),
    "contract_terms": (DataClassification.CONFIDENTIAL, SensitiveCategory.SUPPLIER_CONFIDENTIAL),
    "supplier_contact_direct": (DataClassification.CONFIDENTIAL, SensitiveCategory.SUPPLIER_CONFIDENTIAL),
}


def mask_email(email_str: str) -> str:
    """
    Enmascara deterministamente un correo electrónico.
    Ejemplo: john.doe@example.com -> jo***@example.com
    """
    if not email_str or "@" not in email_str:
        return "[REDACTED_EMAIL]"
    parts = email_str.split("@", 1)
    username, domain = parts[0], parts[1]
    if len(username) <= 2:
        masked_user = username[0] + "***" if len(username) >= 1 else "***"
    else:
        masked_user = username[:2] + "***"
    return f"{masked_user}@{domain}"


def mask_phone(phone_str: str) -> str:
    """
    Enmascara deterministamente un teléfono conservando los últimos 4 dígitos.
    Ejemplo: +56 9 1234 5678 -> ******5678
    """
    digits = re.sub(r"\D", "", phone_str)
    if len(digits) <= 4:
        return "******" + digits
    return "******" + digits[-4:]


def mask_rut_dni(doc_str: str) -> str:
    """
    Enmascara deterministamente un DNI / RUT / Pasaporte.
    Ejemplo: 12.345.678-9 -> *****678-9 o ********
    """
    cleaned = str(doc_str).strip()
    if len(cleaned) <= 4:
        return "****"
    return "*" * (len(cleaned) - 4) + cleaned[-4:]


def mask_credit_card(card_str: str) -> str:
    """
    Enmascara un número de tarjeta conservando solo los últimos 4 dígitos.
    Ejemplo: 4111 2222 3333 4444 -> ************4444
    """
    digits = re.sub(r"\D", "", card_str)
    if len(digits) <= 4:
        return "****"
    return "*" * (len(digits) - 4) + digits[-4:]


def compute_deterministic_fingerprint(value: Any, salt: str = "salt_n9_canonical") -> str:
    """
    Calcula un fingerprint determinista e irreversible para lookup seguro en caché/índices
    sin exponer el valor sensible en texto plano.
    NOTA DE SEGURIDAD N.9: El hashing NO equivale a anonimización universal;
    sirve exclusivamente como identificador opaco determinista.
    """
    raw_bytes = f"{salt}:{str(value).strip()}".encode("utf-8")
    return hashlib.sha256(raw_bytes).hexdigest()[:24]


@dataclass(frozen=True)
class SensitiveFieldDescriptor:
    """
    Descriptor inmutable de un campo clasificado como sensible dentro de una estructura.
    """
    field_path: str
    classification: DataClassification
    category: SensitiveCategory
    detected_via: str = "FIELD_NAME"  # FIELD_NAME, PATTERN, EXPLICIT
    redaction_applied: bool = False

    def __post_init__(self):
        if not isinstance(self.classification, DataClassification):
            try:
                object.__setattr__(self, "classification", DataClassification(self.classification))
            except Exception:
                object.__setattr__(self, "classification", DataClassification.UNKNOWN)
        if not isinstance(self.category, SensitiveCategory):
            try:
                object.__setattr__(self, "category", SensitiveCategory(self.category))
            except Exception:
                object.__setattr__(self, "category", SensitiveCategory.UNKNOWN)


@dataclass(frozen=True)
class SensitiveDataClassification:
    """
    Resultado inmutable de la clasificación de un payload o estructura de datos.
    """
    overall_classification: DataClassification
    primary_category: SensitiveCategory
    all_categories: Tuple[SensitiveCategory, ...]
    detected_fields: Tuple[SensitiveFieldDescriptor, ...]
    is_sensitive: bool
    requires_redaction: bool
    checksum: str = ""

    def __post_init__(self):
        if not isinstance(self.overall_classification, DataClassification):
            try:
                object.__setattr__(self, "overall_classification", DataClassification(self.overall_classification))
            except Exception:
                object.__setattr__(self, "overall_classification", DataClassification.UNKNOWN)
        if not isinstance(self.primary_category, SensitiveCategory):
            try:
                object.__setattr__(self, "primary_category", SensitiveCategory(self.primary_category))
            except Exception:
                object.__setattr__(self, "primary_category", SensitiveCategory.UNKNOWN)
        if not self.checksum:
            payload = (
                f"{self.overall_classification.value}|{self.primary_category.value}|"
                f"{sorted([c.value for c in self.all_categories])}|"
                f"{len(self.detected_fields)}|{self.is_sensitive}"
            )
            calc_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            object.__setattr__(self, "checksum", calc_hash)


@dataclass(frozen=True)
class DataHandlingPolicy:
    """
    Política inmutable y versionada que rige el manejo, minimización, redacción
    y permisos de propagación de datos sensibles para propósitos específicos.
    """
    policy_name: str
    version: str = "1.0.0"
    description: str = ""
    default_classification: DataClassification = DataClassification.INTERNAL
    allowed_purposes: Tuple[DataHandlingPurpose, ...] = (
        DataHandlingPurpose.INFERENCE,
        DataHandlingPurpose.AUDIT,
        DataHandlingPurpose.MARKETPLACE_OPERATION,
        DataHandlingPurpose.ORDER_FULFILLMENT,
        DataHandlingPurpose.STORAGE,
    )
    logging_allowed_classes: Tuple[DataClassification, ...] = (
        DataClassification.PUBLIC,
        DataClassification.INTERNAL,
    )
    cache_allowed_classes: Tuple[DataClassification, ...] = (
        DataClassification.PUBLIC,
        DataClassification.INTERNAL,
        DataClassification.CONFIDENTIAL,
    )
    external_transfer_allowed_classes: Tuple[DataClassification, ...] = (
        DataClassification.PUBLIC,
        DataClassification.INTERNAL,
        DataClassification.CONFIDENTIAL,
        DataClassification.SENSITIVE,
    )
    persistence_mode: PersistenceHandlingMode = PersistenceHandlingMode.ALLOW
    cache_mode: CacheHandlingMode = CacheHandlingMode.SANITIZED_ONLY
    prohibited_categories_for_logging: Tuple[SensitiveCategory, ...] = (
        SensitiveCategory.PERSONAL_DATA,
        SensitiveCategory.CONTACT_DATA,
        SensitiveCategory.FINANCIAL_DATA,
        SensitiveCategory.TECHNICAL_SECRET,
        SensitiveCategory.PRIVATE_PROMPT_CONTEXT,
    )
    prohibited_categories_for_cache: Tuple[SensitiveCategory, ...] = (
        SensitiveCategory.FINANCIAL_DATA,
        SensitiveCategory.TECHNICAL_SECRET,
        SensitiveCategory.PRIVATE_PROMPT_CONTEXT,
    )
    allowed_fields_override_by_purpose: Mapping[str, Tuple[str, ...]] = field(default_factory=dict)
    checksum: str = ""

    def __post_init__(self):
        validate_safe_identifier(self.policy_name, "policy_name")
        if not isinstance(self.default_classification, DataClassification):
            object.__setattr__(self, "default_classification", DataClassification(self.default_classification))
        if not isinstance(self.persistence_mode, PersistenceHandlingMode):
            object.__setattr__(self, "persistence_mode", PersistenceHandlingMode(self.persistence_mode))
        if not isinstance(self.cache_mode, CacheHandlingMode):
            object.__setattr__(self, "cache_mode", CacheHandlingMode(self.cache_mode))

        # Deep freeze mappings
        if not isinstance(self.allowed_fields_override_by_purpose, MappingProxyType):
            frozen_overrides = {k: tuple(v) for k, v in self.allowed_fields_override_by_purpose.items()}
            object.__setattr__(self, "allowed_fields_override_by_purpose", MappingProxyType(frozen_overrides))

        if not self.checksum:
            canonical_repr = (
                f"{self.policy_name}|{self.version}|{self.default_classification.value}|"
                f"{self.persistence_mode.value}|{self.cache_mode.value}|"
                f"{sorted([c.value for c in self.logging_allowed_classes])}|"
                f"{sorted([c.value for c in self.cache_allowed_classes])}"
            )
            calc_hash = hashlib.sha256(canonical_repr.encode("utf-8")).hexdigest()
            object.__setattr__(self, "checksum", calc_hash)

    def verify_integrity(self) -> bool:
        canonical_repr = (
            f"{self.policy_name}|{self.version}|{self.default_classification.value}|"
            f"{self.persistence_mode.value}|{self.cache_mode.value}|"
            f"{sorted([c.value for c in self.logging_allowed_classes])}|"
            f"{sorted([c.value for c in self.cache_allowed_classes])}"
        )
        calc_hash = hashlib.sha256(canonical_repr.encode("utf-8")).hexdigest()
        return self.checksum == calc_hash


@dataclass(frozen=True)
class DataHandlingRequest:
    """
    Solicitud inmutable para clasificar, sanitizar, redactar o evaluar
    el manejo de una estructura de datos.
    """
    payload: Any
    purpose: DataHandlingPurpose = DataHandlingPurpose.GENERAL
    destination: Optional[str] = None  # ej. 'mercadolibre_api', 'llm_provider', 'audit_log', 'm4_cache'
    required_fields: Tuple[str, ...] = ()
    policy_name: Optional[str] = None
    correlation_id: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.purpose, DataHandlingPurpose):
            try:
                object.__setattr__(self, "purpose", DataHandlingPurpose(self.purpose))
            except Exception:
                object.__setattr__(self, "purpose", DataHandlingPurpose.GENERAL)
        if not isinstance(self.metadata, MappingProxyType):
            object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
        if not isinstance(self.required_fields, tuple):
            object.__setattr__(self, "required_fields", tuple(self.required_fields))


@dataclass(frozen=True)
class RedactionResult:
    """
    Resultado inmutable de la operación de redacción/sanitización sobre un payload.
    NUNCA almacena payloads sensibles en texto claro en repr/str.
    """
    sanitized_payload: Any
    fields_redacted_count: int
    redacted_field_paths: Tuple[str, ...]
    contains_unredacted_restricted_data: bool = False

    def __repr__(self) -> str:
        return (
            f"RedactionResult(fields_redacted_count={self.fields_redacted_count}, "
            f"redacted_field_paths={self.redacted_field_paths}, "
            f"contains_unredacted_restricted_data={self.contains_unredacted_restricted_data})"
        )

    def __str__(self) -> str:
        return self.__repr__()


@dataclass(frozen=True)
class DataHandlingDecision:
    """
    Decisión inmutable y determinista sobre el manejo de un conjunto de datos.
    Contiene la clasificación, permisos específicos para cada subsistema,
    el resultado de redacción y códigos de razón estructurados.
    """
    decision_id: str
    classification: SensitiveDataClassification
    purpose: DataHandlingPurpose
    policy_name: str
    policy_version: str
    is_allowed_for_purpose: bool
    logging_allowed: bool
    cache_allowed: bool
    external_transfer_allowed: bool
    persistence_mode: PersistenceHandlingMode
    cache_mode: CacheHandlingMode
    reason_code: DataHandlingReasonCode
    reason_details: str
    redaction_result: Optional[RedactionResult] = None
    safe_identifiers: Mapping[str, str] = field(default_factory=dict)
    checksum: str = ""

    def __post_init__(self):
        if not self.decision_id:
            raise ValueError("decision_id cannot be empty")
        if not isinstance(self.purpose, DataHandlingPurpose):
            object.__setattr__(self, "purpose", DataHandlingPurpose(self.purpose))
        if not isinstance(self.reason_code, DataHandlingReasonCode):
            object.__setattr__(self, "reason_code", DataHandlingReasonCode(self.reason_code))
        if not isinstance(self.persistence_mode, PersistenceHandlingMode):
            object.__setattr__(self, "persistence_mode", PersistenceHandlingMode(self.persistence_mode))
        if not isinstance(self.cache_mode, CacheHandlingMode):
            object.__setattr__(self, "cache_mode", CacheHandlingMode(self.cache_mode))
        if not isinstance(self.safe_identifiers, MappingProxyType):
            object.__setattr__(self, "safe_identifiers", MappingProxyType(dict(self.safe_identifiers)))

        if not self.checksum:
            canonical_repr = (
                f"{self.decision_id}|{self.classification.overall_classification.value}|"
                f"{self.purpose.value}|{self.policy_name}|{self.policy_version}|"
                f"{self.is_allowed_for_purpose}|{self.logging_allowed}|{self.cache_allowed}|"
                f"{self.external_transfer_allowed}|{self.persistence_mode.value}|"
                f"{self.reason_code.value}"
            )
            calc_hash = hashlib.sha256(canonical_repr.encode("utf-8")).hexdigest()
            object.__setattr__(self, "checksum", calc_hash)

    def __repr__(self) -> str:
        return (
            f"DataHandlingDecision(decision_id='{self.decision_id}', "
            f"classification={self.classification.overall_classification.value}, "
            f"purpose={self.purpose.value}, "
            f"is_allowed={self.is_allowed_for_purpose}, "
            f"logging_allowed={self.logging_allowed}, "
            f"cache_allowed={self.cache_allowed}, "
            f"external_transfer_allowed={self.external_transfer_allowed}, "
            f"reason_code={self.reason_code.value})"
        )

    def __str__(self) -> str:
        return self.__repr__()
