class OmniRouteError(Exception):
    """Excepción base para errores relacionados con el proveedor OmniRoute."""
    pass


class OmniRouteHttpError(OmniRouteError):
    """Errores en la capa de transporte HTTP (códigos de estado 4xx/5xx)."""
    def __init__(self, message: str, status_code: int, response_body: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class OmniRouteTimeoutError(OmniRouteError):
    """Timeout esperando la respuesta del gateway OmniRoute."""
    pass


class OmniRouteConnectionError(OmniRouteError):
    """Fallo en la conexión de red hacia OmniRoute (e.g. conexión rechazada)."""
    pass


class OmniRouteParseError(OmniRouteError):
    """Fallo al parsear la respuesta HTTP como JSON o respuesta OpenAI inválida."""
    pass


class OmniRouteContractValidationError(OmniRouteError):
    """Fallo al validar que el contenido estructurado cumple con el contrato de LoopDecision."""
    pass
