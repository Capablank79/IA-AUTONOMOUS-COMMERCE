from dataclasses import dataclass
import os
from typing import Optional


@dataclass(frozen=True)
class OmniRouteConfig:
    """
    Configuración para la conexión con el gateway OmniRoute.
    Valores por defecto orientados a desarrollo local.
    """
    base_url: str = "http://localhost:20128/v1"
    api_key: Optional[str] = None
    model: str = "auto/best-coding"
    timeout: float = 30.0

    @classmethod
    def from_env(cls) -> 'OmniRouteConfig':
        """
        Crea la configuración leyendo variables de entorno opcionales.
        OMNIROUTE_BASE_URL
        OMNIROUTE_API_KEY
        OMNIROUTE_MODEL
        OMNIROUTE_TIMEOUT
        """
        base_url = os.environ.get("OMNIROUTE_BASE_URL", "http://localhost:20128/v1")
        api_key = os.environ.get("OMNIROUTE_API_KEY")
        model = os.environ.get("OMNIROUTE_MODEL", "auto/best-coding")
        raw_timeout = os.environ.get("OMNIROUTE_TIMEOUT")
        timeout = float(raw_timeout) if raw_timeout is not None else 30.0

        return cls(
            base_url=base_url,
            api_key=api_key,
            model=model,
            timeout=timeout
        )
