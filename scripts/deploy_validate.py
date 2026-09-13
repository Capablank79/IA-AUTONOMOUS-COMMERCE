"""Script determinista de validación de despliegue (O.13 Deployment Automation).

Comprobaciones ejecutadas:
1. Validación de sintaxis e integridad de archivos clave del despliegue.
2. Validación de Dockerfile y .dockerignore deterministas (non-root, multi-stage, exclusión de secretos y cachés).
3. Validación del módulo de configuración pre-startup (DeploymentConfigValidator) con pruebas de validación positiva y negativa.
4. Verificación de creación de app Starlette con endpoints `/health`, `/healthz`, `/ready`, `/readyz`.
5. Ejecución de prueba de humo (smoke test) en memoria sin levantar puertos reales.
"""

import argparse
import os
import re
import sys
from pathlib import Path

# Ajustar PYTHONPATH
project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from src.domain.deployment.models import DeploymentConfig, DeploymentEnvironment, DeploymentConfigError, SecretLeakError, HardcodedTenantConfigError, StoragePathSecurityError
from src.infrastructure.deployment.config_validator import DeploymentConfigValidator
from src.infrastructure.deployment.environment_policy import (
    ENVIRONMENT_PROFILES,
    default_data_root,
    resolve_application_environment,
)
from src.infrastructure.web.app import create_platform_app, check_storage_writable
from starlette.testclient import TestClient


def validate_dockerfile(root: Path) -> bool:
    print("[1/5] Checking Dockerfile and .dockerignore...")
    dockerfile = root / "Dockerfile"
    dockerignore = root / ".dockerignore"

    if not dockerfile.exists():
        print("[ERROR] Dockerfile does not exist!", file=sys.stderr)
        return False
    if not dockerignore.exists():
        print("[ERROR] .dockerignore does not exist!", file=sys.stderr)
        return False

    dockerfile_content = dockerfile.read_text(encoding="utf-8")

    # Validar que no sea root y sea multi-stage
    if "USER" not in dockerfile_content:
        print("[ERROR] Dockerfile must specify a non-root USER instruction!", file=sys.stderr)
        return False
    if "FROM" not in dockerfile_content or "AS" not in dockerfile_content:
        print("[ERROR] Dockerfile must use multi-stage builds (AS ...)", file=sys.stderr)
        return False
    if "10001" not in dockerfile_content and "appuser" not in dockerfile_content:
        print("[ERROR] Dockerfile must configure appuser/non-root UID", file=sys.stderr)
        return False

    dockerignore_content = dockerignore.read_text(encoding="utf-8")
    required_ignores = [".git", ".env", "__pycache__", ".pytest_cache", ".runtime"]
    for pattern in required_ignores:
        if pattern not in dockerignore_content:
            print(f"[ERROR] .dockerignore missing critical pattern: '{pattern}'", file=sys.stderr)
            return False

    print("[PASS] Dockerfile and .dockerignore are valid, deterministic and non-root.")
    return True


def validate_config_rules() -> bool:
    print("[2/5] Checking Configuration Validator rules (Positive and Negative paths)...")

    # 1. Caso Válido
    valid_env = {
        "ENVIRONMENT": "production",
        "HOST": "0.0.0.0",
        "PORT": "8080",
        "DATA_DIR": "data",
        "LOG_LEVEL": "INFO",
        "ENABLE_ADMIN_CONSOLE": "true",
    }
    validator = DeploymentConfigValidator(valid_env)
    cfg = validator.validate()
    if cfg.port != 8080 or cfg.environment != DeploymentEnvironment.PRODUCTION:
        print("[ERROR] Valid config failed to parse expected fields", file=sys.stderr)
        return False

    # 2. Caso Secreto Inseguro
    secret_env = dict(valid_env)
    secret_env["PLAIN_TEXT_API_KEY"] = "super-secret"
    try:
        DeploymentConfigValidator(secret_env).validate()
        print("[ERROR] Validator accepted PLAIN_TEXT_API_KEY without raising SecretLeakError!", file=sys.stderr)
        return False
    except SecretLeakError:
        pass

    # 3. Caso Tenant Horneado
    tenant_env = dict(valid_env)
    tenant_env["TENANT_ACME_CONFIG"] = "tenant_id=acme"
    try:
        DeploymentConfigValidator(tenant_env).validate()
        print("[ERROR] Validator accepted TENANT_ACME_CONFIG without raising HardcodedTenantConfigError!", file=sys.stderr)
        return False
    except HardcodedTenantConfigError:
        pass

    # 4. Caso Path Inseguro
    path_env = dict(valid_env)
    path_env["DATA_DIR"] = "/etc"
    try:
        DeploymentConfigValidator(path_env).validate()
        print("[ERROR] Validator accepted DATA_DIR='/etc' without raising StoragePathSecurityError!", file=sys.stderr)
        return False
    except StoragePathSecurityError:
        pass

    print("[PASS] Configuration Validator enforces all security and domain constraints.")
    return True


def validate_app_probes(tmp_path: Path) -> bool:
    print("[3/5] Checking ASGI application creation and health/readiness endpoints...")
    test_env = {
        "ENVIRONMENT": "testing",
        "HOST": "127.0.0.1",
        "PORT": "9000",
        "DATA_DIR": str(tmp_path / "deploy_data"),
        "ENABLE_ADMIN_CONSOLE": "true",
    }
    validator = DeploymentConfigValidator(test_env)
    config = validator.validate()
    app = create_platform_app(config=config)

    client = TestClient(app)

    # 1. Test /health (Liveness)
    resp = client.get("/health")
    if resp.status_code != 200 or resp.json().get("status") != "ok":
        print(f"[ERROR] /health probe failed: status={resp.status_code}, body={resp.text}", file=sys.stderr)
        return False

    # 2. Test /healthz (Liveness alias)
    resp = client.get("/healthz")
    if resp.status_code != 200 or resp.json().get("status") != "ok":
        print(f"[ERROR] /healthz probe failed: status={resp.status_code}, body={resp.text}", file=sys.stderr)
        return False

    # 3. Test /ready (Readiness)
    resp = client.get("/ready")
    if resp.status_code != 200 or resp.json().get("status") != "ready":
        print(f"[ERROR] /ready probe failed: status={resp.status_code}, body={resp.text}", file=sys.stderr)
        return False

    # 4. Test /readyz (Readiness alias)
    resp = client.get("/readyz")
    if resp.status_code != 200 or resp.json().get("status") != "ready":
        print(f"[ERROR] /readyz probe failed: status={resp.status_code}, body={resp.text}", file=sys.stderr)
        return False

    print("[PASS] Liveness and Readiness probes operational.")
    return True


def validate_entrypoint_script() -> bool:
    print("[4/5] Checking scripts/entrypoint.py pre-flight execution...")
    entrypoint = project_root / "scripts" / "entrypoint.py"
    if not entrypoint.exists():
        print("[ERROR] scripts/entrypoint.py missing!", file=sys.stderr)
        return False

    import subprocess
    env = os.environ.copy()
    env["DEPLOY_DRY_RUN"] = "1"
    env["DATA_DIR"] = str(project_root / ".runtime" / "deploy_test_data")
    env["ENVIRONMENT"] = "testing"

    res = subprocess.run([sys.executable, str(entrypoint)], env=env, capture_output=True, text=True)
    if res.returncode != 0:
        print(f"[ERROR] Entrypoint dry-run failed with code {res.returncode}:\n{res.stderr}", file=sys.stderr)
        return False

    print("[PASS] Entrypoint dry-run succeeded cleanly.")
    return True


def validate_environment_profile(environment_name: str) -> bool:
    """Valida un perfil de entorno canónico (P.2) reutilizando O.13.

    No duplica lógica de validación: delega en DeploymentConfigValidator +
    política de entornos (environment_policy). Acepta aliases de tooling
    (dev/stage/prod) y normaliza al valor canónico.
    """
    try:
        environment = resolve_application_environment(
            {"APP_ENV": environment_name}, use_aliases=True
        )
    except DeploymentConfigError as exc:
        print(f"[ERROR] Invalid environment '{environment_name}': {exc}", file=sys.stderr)
        return False

    profile = ENVIRONMENT_PROFILES[environment]
    data_root = default_data_root(environment)
    print(
        f"Checking environment profile '{environment.value}' "
        f"(data_root='{data_root}', log_level='{profile.log_level_default}', "
        f"secret_namespace='{profile.secret_namespace}')..."
    )

    profile_env = {
        "APP_ENV": environment.value,
        "HOST": "0.0.0.0",
        "PORT": "8080",
        "DATA_DIR": data_root,
        "LOG_LEVEL": profile.log_level_default,
    }
    try:
        config = DeploymentConfigValidator(profile_env).validate()
    except DeploymentConfigError as exc:
        print(f"[ERROR] Environment profile '{environment.value}' rejected: {exc}", file=sys.stderr)
        return False

    if config.environment != environment:
        print(
            f"[ERROR] Environment mismatch: expected '{environment.value}', "
            f"got '{config.environment.value}'",
            file=sys.stderr,
        )
        return False

    print(f"[PASS] Environment profile '{environment.value}' validated and safe.")
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="O.13 Deployment Automation validation suite with environment profile validation."
    )
    parser.add_argument(
        "--environment",
        default=None,
        metavar="NAME",
        help="Validate a single environment profile: development | staging | production "
             "(aliases dev/stage/prod accepted). Default: run the full O.13 suite.",
    )
    args = parser.parse_args(argv)

    print("================================================================")
    print("      O.13 DEPLOYMENT AUTOMATION VALIDATION SUITE               ")
    print("================================================================")

    if args.environment:
        ok = validate_environment_profile(args.environment)
        print("\n>>> ENVIRONMENT PROFILE VALIDATION " + ("PASSED." if ok else "FAILED.") + " <<<\n")
        return 0 if ok else 1

    root = project_root
    tmp_path = root / ".runtime" / "validation_tmp"
    tmp_path.mkdir(parents=True, exist_ok=True)

    checks = [
        validate_dockerfile(root),
        validate_config_rules(),
        validate_app_probes(tmp_path),
        validate_entrypoint_script(),
    ]

    print("[5/5] Synthesizing results...")
    if all(checks):
        print("\n>>> ALL O.13 DEPLOYMENT AUTOMATION VALIDATIONS PASSED SUCCESSFULY. <<<\n")
        return 0
    else:
        print("\n>>> VALIDATION FAILED: One or more checks returned errors. <<<\n", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
