"""
Implementaciones de Clasificador y Redactor de Datos Sensibles (Hito N.9).

Proporciona:
- DeterministicSensitiveDataClassifier: Inspección recursiva de claves y contenido (patrones regex).
- DeterministicSensitiveDataRedactor: Redacción estructurada por tipo de dato y minimización determinista.
"""

from dataclasses import is_dataclass, asdict
from types import MappingProxyType
from typing import Any, Dict, List, Set, Tuple, Optional, Sequence, Union
import re

from src.domain.security.sensitive_data_models import (
    DataClassification,
    SensitiveCategory,
    DataHandlingPurpose,
    DataHandlingPolicy,
    SensitiveFieldDescriptor,
    SensitiveDataClassification,
    RedactionResult,
    SENSITIVE_FIELD_NAMES,
    EMAIL_REGEX,
    PHONE_REGEX,
    RUT_DNI_REGEX,
    CREDIT_CARD_REGEX,
    mask_email,
    mask_phone,
    mask_rut_dni,
    mask_credit_card,
)
from src.domain.security.sensitive_data_ports import (
    SensitiveDataClassifierPort,
    SensitiveDataRedactorPort,
)
from src.domain.security.models import deep_freeze


class DeterministicSensitiveDataClassifier(SensitiveDataClassifierPort):
    """
    Clasificador determinista de datos sensibles.
    Analiza estructuras de datos recursivas (dict, list, tuple, dataclass, str)
    y asigna clasificaciones fail-secure.
    """

    def classify(self, data: Any, context_purpose: Optional[DataHandlingPurpose] = None) -> SensitiveDataClassification:
        detected_fields: List[SensitiveFieldDescriptor] = []
        all_categories: Set[SensitiveCategory] = set()
        highest_class = DataClassification.PUBLIC

        def _update_highest(c: DataClassification):
            nonlocal highest_class
            hierarchy = {
                DataClassification.PUBLIC: 1,
                DataClassification.INTERNAL: 2,
                DataClassification.CONFIDENTIAL: 3,
                DataClassification.SENSITIVE: 4,
                DataClassification.RESTRICTED: 5,
                DataClassification.UNKNOWN: 4,  # UNKNOWN is treated with high sensitivity (fail-secure)
            }
            if hierarchy.get(c, 4) > hierarchy.get(highest_class, 1):
                highest_class = c

        def _inspect_node(val: Any, current_path: str):
            if val is None:
                return

            # Si es dataclass, convertir a dict
            if is_dataclass(val) and not isinstance(val, type):
                try:
                    val = asdict(val)
                except Exception:
                    pass

            # Si es diccionario o Mapping
            if isinstance(val, (dict, MappingProxyType)):
                for k, v in val.items():
                    k_str = str(k).strip()
                    k_lower = k_str.lower()
                    path = f"{current_path}.{k_str}" if current_path else k_str

                    # 1. Match por nombre de campo
                    matched_field = False
                    for sens_k, (field_class, field_cat) in SENSITIVE_FIELD_NAMES.items():
                        if sens_k in k_lower:
                            descriptor = SensitiveFieldDescriptor(
                                field_path=path,
                                classification=field_class,
                                category=field_cat,
                                detected_via="FIELD_NAME",
                            )
                            detected_fields.append(descriptor)
                            all_categories.add(field_cat)
                            _update_highest(field_class)
                            matched_field = True
                            break

                    # 2. Si el valor es primitivo string, inspeccionar por patrón
                    if isinstance(v, str):
                        _inspect_string_value(v, path)
                    else:
                        _inspect_node(v, path)

            # Si es secuencia (lista o tupla)
            elif isinstance(val, (list, tuple, set)):
                for idx, item in enumerate(val):
                    path = f"{current_path}[{idx}]"
                    if isinstance(item, str):
                        _inspect_string_value(item, path)
                    else:
                        _inspect_node(item, path)

            # Si es string raíz
            elif isinstance(val, str):
                _inspect_string_value(val, current_path or "root")

        def _inspect_string_value(text: str, path: str):
            if not text or len(text) > 50000:
                return

            # Email pattern
            if EMAIL_REGEX.search(text):
                desc = SensitiveFieldDescriptor(
                    field_path=path,
                    classification=DataClassification.SENSITIVE,
                    category=SensitiveCategory.CONTACT_DATA,
                    detected_via="PATTERN",
                )
                detected_fields.append(desc)
                all_categories.add(SensitiveCategory.CONTACT_DATA)
                _update_highest(DataClassification.SENSITIVE)

            # Credit card pattern
            if CREDIT_CARD_REGEX.search(text):
                desc = SensitiveFieldDescriptor(
                    field_path=path,
                    classification=DataClassification.RESTRICTED,
                    category=SensitiveCategory.FINANCIAL_DATA,
                    detected_via="PATTERN",
                )
                detected_fields.append(desc)
                all_categories.add(SensitiveCategory.FINANCIAL_DATA)
                _update_highest(DataClassification.RESTRICTED)

        _inspect_node(data, "")

        if not detected_fields:
            primary_cat = SensitiveCategory.UNKNOWN if highest_class == DataClassification.UNKNOWN else SensitiveCategory.PERSONAL_DATA if highest_class == DataClassification.SENSITIVE else SensitiveCategory.BUSINESS_CONFIDENTIAL
            if highest_class == DataClassification.PUBLIC:
                primary_cat = SensitiveCategory.BUSINESS_CONFIDENTIAL
            is_sens = highest_class in (DataClassification.CONFIDENTIAL, DataClassification.SENSITIVE, DataClassification.RESTRICTED, DataClassification.UNKNOWN)
            requires_redaction = highest_class in (DataClassification.SENSITIVE, DataClassification.RESTRICTED, DataClassification.UNKNOWN)
            return SensitiveDataClassification(
                overall_classification=highest_class,
                primary_category=primary_cat,
                all_categories=(),
                detected_fields=(),
                is_sensitive=is_sens,
                requires_redaction=requires_redaction,
            )

        # Determinar categoría primaria
        primary_cat = list(all_categories)[0] if all_categories else SensitiveCategory.UNKNOWN
        if SensitiveCategory.TECHNICAL_SECRET in all_categories:
            primary_cat = SensitiveCategory.TECHNICAL_SECRET
        elif SensitiveCategory.FINANCIAL_DATA in all_categories:
            primary_cat = SensitiveCategory.FINANCIAL_DATA
        elif SensitiveCategory.PERSONAL_DATA in all_categories:
            primary_cat = SensitiveCategory.PERSONAL_DATA
        elif SensitiveCategory.CONTACT_DATA in all_categories:
            primary_cat = SensitiveCategory.CONTACT_DATA

        is_sens = highest_class in (DataClassification.CONFIDENTIAL, DataClassification.SENSITIVE, DataClassification.RESTRICTED, DataClassification.UNKNOWN)
        requires_red = highest_class in (DataClassification.SENSITIVE, DataClassification.RESTRICTED, DataClassification.UNKNOWN)

        return SensitiveDataClassification(
            overall_classification=highest_class,
            primary_category=primary_cat,
            all_categories=tuple(sorted(all_categories, key=lambda c: c.value)),
            detected_fields=tuple(detected_fields),
            is_sensitive=is_sens,
            requires_redaction=requires_red,
        )


class DeterministicSensitiveDataRedactor(SensitiveDataRedactorPort):
    """
    Redactor y sanitizador recursivo determinista.
    Aplica políticas de enmascaramiento y minimización de datos respetando
    el propósito de la operación y protegiendo valores confidenciales.
    """

    def redact(
        self,
        data: Any,
        policy: DataHandlingPolicy,
        purpose: DataHandlingPurpose,
        required_fields: Sequence[str] = (),
    ) -> RedactionResult:
        redacted_paths: List[str] = []
        redacted_count = 0
        contains_unredacted_restricted = False

        # Si hay campos requeridos para el propósito, obtenerlos de la política o del parámetro
        allowed_overrides = policy.allowed_fields_override_by_purpose.get(purpose.value, ())
        effective_allowed_fields = set(required_fields) | set(allowed_overrides)

        def _should_allow_field(field_key: str) -> bool:
            """Verifica si un campo específico está en la lista de minimización requerida."""
            if not effective_allowed_fields:
                return True
            k_lower = field_key.lower().strip()
            return any(k_lower == req.lower().strip() or req == "*" for req in effective_allowed_fields)

        def _redact_value(val: Any, key_name: Optional[str] = None, current_path: str = "") -> Any:
            nonlocal redacted_count, contains_unredacted_restricted

            if val is None:
                return None

            if is_dataclass(val) and not isinstance(val, type):
                try:
                    val = asdict(val)
                except Exception:
                    pass

            # Si es dict o Mapping
            if isinstance(val, (dict, MappingProxyType)):
                cleaned_dict = {}
                for k, v in val.items():
                    k_str = str(k).strip()
                    k_lower = k_str.lower()
                    path = f"{current_path}.{k_str}" if current_path else k_str

                    # Regla 1: Secrets técnicos (N.5 overlap) -> SIEMPRE [REDACTED_SECRET]
                    is_technical_secret = any(
                        s in k_lower for s in ("password", "secret", "api_key", "apikey", "api_token", "private_key", "access_token", "refresh_token", "auth_header", "bearer")
                    )
                    if is_technical_secret and not isinstance(v, (dict, MappingProxyType, list, tuple)):
                        cleaned_dict[k_str] = "[REDACTED_SECRET]"
                        redacted_paths.append(path)
                        redacted_count += 1
                        continue

                    # Regla 0: Minimización de campos para INFERENCE o MARKETPLACE o TOOL cuando hay required_fields
                    if purpose in (DataHandlingPurpose.INFERENCE, DataHandlingPurpose.MARKETPLACE_OPERATION, DataHandlingPurpose.ORDER_FULFILLMENT):
                        if effective_allowed_fields and not _should_allow_field(k_str):
                            # Campo no necesario para la operación -> eliminar o redactar
                            if not isinstance(v, (dict, MappingProxyType, list, tuple)):
                                cleaned_dict[k_str] = "[MINIMIZED_FIELD]"
                                redacted_paths.append(path)
                                redacted_count += 1
                                continue

                    # Regla 2: Private prompt context / CoT -> SIEMPRE [REDACTED_COT]
                    is_cot = any(s in k_lower for s in ("chain_of_thought", "reasoning", "reasoning_tokens", "internal_scratchpad"))
                    if is_cot:
                        cleaned_dict[k_str] = "[REDACTED_INTERNAL_REASONING]"
                        redacted_paths.append(path)
                        redacted_count += 1
                        continue

                    # Regla 3: Propósito LOGGING o AUDIT o CACHE o INFERENCE -> Aplicar protecciones estrictas de PII y datos de contacto si no son campos requeridos
                    if purpose in (DataHandlingPurpose.LOGGING, DataHandlingPurpose.AUDIT, DataHandlingPurpose.CACHE, DataHandlingPurpose.INFERENCE):
                        # Si es dato financiero crítico
                        if any(s in k_lower for s in ("card_number", "credit_card", "creditcard", "pan", "cvv", "bank_account", "iban")):
                            cleaned_dict[k_str] = "[REDACTED_FINANCIAL]"
                            redacted_paths.append(path)
                            redacted_count += 1
                            continue

                        # Si es DNI / RUT / Pasaporte
                        if any(s in k_lower for s in ("rut", "dni", "passport", "national_id", "ssn")):
                            if isinstance(v, str):
                                cleaned_dict[k_str] = mask_rut_dni(v)
                            else:
                                cleaned_dict[k_str] = "[REDACTED_IDENTIFIER]"
                            redacted_paths.append(path)
                            redacted_count += 1
                            continue

                        # Si es email
                        if "email" in k_lower:
                            if isinstance(v, str):
                                cleaned_dict[k_str] = mask_email(v)
                            else:
                                cleaned_dict[k_str] = "[REDACTED_EMAIL]"
                            redacted_paths.append(path)
                            redacted_count += 1
                            continue

                        # Si es teléfono
                        if any(s in k_lower for s in ("phone", "telephone", "mobile")):
                            if isinstance(v, str):
                                cleaned_dict[k_str] = mask_phone(v)
                            else:
                                cleaned_dict[k_str] = "[REDACTED_PHONE]"
                            redacted_paths.append(path)
                            redacted_count += 1
                            continue

                        # Si es dirección detallada
                        if any(s in k_lower for s in ("street_name", "street_number", "address_line", "shipping_address", "billing_address", "address_complement")):
                            cleaned_dict[k_str] = "[REDACTED_ADDRESS]"
                            redacted_paths.append(path)
                            redacted_count += 1
                            continue

                    # Procesar recursivamente
                    cleaned_dict[k_str] = _redact_value(v, key_name=k_str, current_path=path)

                return cleaned_dict

            # Si es secuencia (list o tuple)
            if isinstance(val, (list, tuple)):
                cleaned_seq = []
                for idx, elem in enumerate(val):
                    p = f"{current_path}[{idx}]"
                    cleaned_seq.append(_redact_value(elem, key_name=None, current_path=p))
                return tuple(cleaned_seq) if isinstance(val, tuple) else cleaned_seq

            # Si es string
            if isinstance(val, str):
                redacted_str = val
                # Enmascarar emails en strings sueltos
                if purpose in (DataHandlingPurpose.LOGGING, DataHandlingPurpose.AUDIT, DataHandlingPurpose.CACHE):
                    if EMAIL_REGEX.search(redacted_str):
                        redacted_str = EMAIL_REGEX.sub(lambda m: mask_email(m.group(0)), redacted_str)
                        if redacted_str != val:
                            redacted_paths.append(current_path or "str_field")
                            redacted_count += 1

                    if CREDIT_CARD_REGEX.search(redacted_str):
                        redacted_str = CREDIT_CARD_REGEX.sub(lambda m: mask_credit_card(m.group(0)), redacted_str)
                        if redacted_str != val:
                            redacted_paths.append(current_path or "str_field")
                            redacted_count += 1

                return redacted_str

            return val

        sanitized = _redact_value(data, key_name=None, current_path="")

        return RedactionResult(
            sanitized_payload=sanitized,
            fields_redacted_count=redacted_count,
            redacted_field_paths=tuple(redacted_paths),
            contains_unredacted_restricted_data=contains_unredacted_restricted,
        )
