"""
Capa de infraestructura Web y HTTP para SaaS Admin Console (Hito O.10).
"""
from src.infrastructure.web.admin_app import create_admin_app
from src.infrastructure.web.app import create_platform_app, get_asgi_app

__all__ = [
    "create_admin_app",
    "create_platform_app",
    "get_asgi_app",
]
