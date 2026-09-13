"""Entrypoint canónico de ejecución de la plataforma (O.13 Deployment Automation).

Secuencia de arranque (P.2 — Environment Separation):
1. Resolver el environment (APP_ENV fuente canónica; ENVIRONMENT legacy compat).
2. Validar la configuración de runtime (fail-fast si entorno desconocido,
   debug/mocks en producción, cross-env data roots o secret namespaces).
3. Resolver la configuración efectiva del environment (DeploymentConfig).
4. Verificar rutas y secretos (DATA_DIR persistente y seguro).
5. Iniciar la aplicación ASGI.
"""

import os
import sys
from pathlib import Path

# Asegurar que el directorio raíz de la plataforma esté en el PYTHONPATH
project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from src.domain.deployment.models import DeploymentConfigError, SecretLeakError, HardcodedTenantConfigError, StoragePathSecurityError
from src.infrastructure.deployment.config_validator import DeploymentConfigValidator
from src.infrastructure.web.app import check_storage_writable, create_platform_app


def main() -> int:
    print("=== [O.13 Deployment Automation] Platform Startup Pre-flight Check ===")

    # 1-3. Resolución del environment + validación de configuración (fail-fast)
    try:
        validator = DeploymentConfigValidator()
        config = validator.validate()
        print(f"[ENV RESOLVED] environment={config.environment.value} log_level={config.log_level} data_dir='{config.data_dir}'")
        print(f"[PRE-FLIGHT OK] Environment: {config.environment.value}, Host: {config.host}, Port: {config.port}")
    except (DeploymentConfigError, SecretLeakError, HardcodedTenantConfigError, StoragePathSecurityError) as exc:
        print(f"[FATAL CONFIG ERROR] Startup aborted: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"[UNEXPECTED STARTUP ERROR] {exc}", file=sys.stderr)
        return 1

    # 2. Verificación de almacenamiento persistente
    print(f"[STORAGE CHECK] Verifying persistent storage at '{config.data_dir}'...")
    if not check_storage_writable(config.data_dir):
        print(f"[FATAL STORAGE ERROR] Storage path '{config.data_dir}' is not writable!", file=sys.stderr)
        return 2
    print(f"[STORAGE CHECK OK] Persistent directory '{config.data_dir}' is operational.")

    # 3. Creación de la aplicación ASGI
    print("[APP INIT] Building canonical ASGI application...")
    try:
        app = create_platform_app(config=config)
        print("[APP INIT OK] ASGI platform app ready with health and readiness endpoints.")
    except Exception as exc:
        print(f"[FATAL APP INIT ERROR] Failed to construct platform app: {exc}", file=sys.stderr)
        return 3

    # 4. Arranque de Uvicorn si no se ejecuta en modo test / dry-run
    if os.environ.get("DEPLOY_DRY_RUN", "").lower() in {"1", "true", "yes"}:
        print("[DRY RUN OK] Pre-flight and app initialization completed successfully. Exiting without binding.")
        return 0

    try:
        import uvicorn
        print(f"[SERVER START] Launching Uvicorn on {config.host}:{config.port} (log_level={config.log_level.lower()})...")
        uvicorn.run(
            app,
            host=config.host,
            port=config.port,
            log_level=config.log_level.lower(),
            access_log=True,
        )
        return 0
    except Exception as exc:
        print(f"[FATAL SERVER ERROR] Uvicorn execution failed: {exc}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    sys.exit(main())
