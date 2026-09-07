"""
Proveedores de infraestructura para resolución de secretos (Hito N.5 — Secret Management).

Define:
- EnvSecretProvider: Resuelve secretos a partir de variables de entorno del sistema operativo.
- InjectedSecretProvider: Proveedor en memoria para configuración inyectada y pruebas. Soporta rotación de secretos.
- OAuthSecretProviderBridge: Puente que conecta con OAuthConnectionRepository para resolver access_token / refresh_token bajo demanda sin duplicar el ciclo de vida OAuth.
"""

import os
from typing import Optional, Mapping, Dict, Any
from src.domain.secrets.models import (
    SecretReference,
    SecretValue,
    SecretType,
)
from src.domain.secrets.ports import SecretProviderPort
from src.domain.oauth.ports import OAuthConnectionRepository
from src.domain.oauth.models import OAuthConnection


class EnvSecretProvider(SecretProviderPort):
    """
    Proveedor de secretos basado en variables de entorno.

    Reglas de resolución:
    1. Si `reference.env_var_fallback` está definido, busca esa variable exacta.
    2. Convención determinista: `{PROVIDER.upper()}_{SECRET_NAME.upper()}`
    3. Si la variable existe y no está vacía, retorna `SecretValue`.
    """

    def __init__(self, prefix: str = ""):
        self._prefix = prefix.strip().upper()

    @property
    def provider_name(self) -> str:
        return "env"

    def can_handle(self, reference: SecretReference) -> bool:
        # Env provider puede intentar resolver cualquier referencia que tenga env_var o convención
        return True

    def get_secret(self, reference: SecretReference) -> Optional[SecretValue]:
        candidate_keys = []
        if reference.env_var_fallback:
            candidate_keys.append(reference.env_var_fallback.strip())

        norm_provider = reference.provider.strip().upper()
        norm_name = reference.secret_name.strip().upper().replace(".", "_").replace("-", "_")

        if self._prefix:
            candidate_keys.append(f"{self._prefix}_{norm_provider}_{norm_name}")
            candidate_keys.append(f"{self._prefix}_{norm_name}")

        candidate_keys.append(f"{norm_provider}_{norm_name}")
        candidate_keys.append(norm_name)

        for key in candidate_keys:
            val = os.environ.get(key)
            if val is not None and len(val.strip()) > 0:
                return SecretValue(val.strip())

        return None


class InjectedSecretProvider(SecretProviderPort):
    """
    Proveedor de secretos en memoria para configuración inyectada de forma segura.
    Soporta inyección y rotación dinámica de material secreto en tiempo de ejecución.
    """

    def __init__(self, initial_secrets: Optional[Mapping[str, str]] = None):
        self._secrets: Dict[str, SecretValue] = {}
        if initial_secrets:
            for ref_id, raw_val in initial_secrets.items():
                if raw_val and isinstance(raw_val, str):
                    self.set_secret(ref_id, raw_val)

    @property
    def provider_name(self) -> str:
        return "injected"

    def can_handle(self, reference: SecretReference) -> bool:
        return reference.reference_id in self._secrets or self._lookup_key(reference) in self._secrets

    def _lookup_key(self, reference: SecretReference) -> str:
        return f"{reference.provider.lower()}:{reference.secret_name.lower()}"

    def get_secret(self, reference: SecretReference) -> Optional[SecretValue]:
        # 1. Por reference_id
        if reference.reference_id in self._secrets:
            return self._secrets[reference.reference_id]
        # 2. Por provider:secret_name
        key = self._lookup_key(reference)
        return self._secrets.get(key)

    def set_secret(self, reference_key_or_id: str, raw_value: str) -> None:
        """Inyecta o actualiza un secreto (rotación en memoria)."""
        clean_key = reference_key_or_id.strip()
        self._secrets[clean_key] = SecretValue(raw_value)

    def remove_secret(self, reference_key_or_id: str) -> bool:
        """Elimina un secreto de la memoria."""
        clean_key = reference_key_or_id.strip()
        return self._secrets.pop(clean_key, None) is not None

    def clear(self) -> None:
        """Limpia todos los secretos en memoria."""
        self._secrets.clear()


class OAuthSecretProviderBridge(SecretProviderPort):
    """
    Puente de resolución para secretos y tokens OAuth existentes (Hito E / MercadoLibre).

    Permite a los adaptadores obtener tokens de conexiones OAuth activas
    mediante SecretReference sin duplicar el repositorio OAuth.

    Formatos esperados de secret_name:
    - "access_token:{user_id}"
    - "refresh_token:{user_id}"
    - "token:{user_id}" (por defecto access_token)
    """

    def __init__(self, oauth_repository: OAuthConnectionRepository):
        self._oauth_repo = oauth_repository

    @property
    def provider_name(self) -> str:
        return "oauth_bridge"

    def can_handle(self, reference: SecretReference) -> bool:
        # Maneja referencias con secret_type ACCESS_TOKEN / REFRESH_TOKEN o nombres con prefijo token
        if reference.secret_type in (SecretType.ACCESS_TOKEN, SecretType.REFRESH_TOKEN):
            return True
        name = reference.secret_name.lower()
        return "token:" in name or "access_token" in name or "refresh_token" in name

    def get_secret(self, reference: SecretReference) -> Optional[SecretValue]:
        # Parsear user_id y token_type de secret_name
        # Ej: "access_token:12345" o "refresh_token:12345" o "12345"
        name = reference.secret_name
        user_id = None
        token_type = "access_token"

        if ":" in name:
            parts = name.split(":", 1)
            prefix = parts[0].strip().lower()
            user_id = parts[1].strip()
            if prefix in ("access_token", "refresh_token"):
                token_type = prefix
        else:
            user_id = name.strip()
            if reference.secret_type == SecretType.REFRESH_TOKEN:
                token_type = "refresh_token"

        if not user_id:
            return None

        try:
            conn: OAuthConnection = self._oauth_repo.get(
                provider=reference.provider,
                user_id=user_id,
            )
            if conn is None:
                return None

            if token_type == "refresh_token":
                if conn.refresh_token and conn.refresh_token.strip():
                    return SecretValue(conn.refresh_token.strip())
            else:
                if conn.access_token and conn.access_token.strip():
                    return SecretValue(conn.access_token.strip())
            return None
        except Exception:
            return None
