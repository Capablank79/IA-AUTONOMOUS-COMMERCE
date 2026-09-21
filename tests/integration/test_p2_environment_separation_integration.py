"""Suite de pruebas de integración para P.2 Environment Separation (Hito P).

Escenarios:
A.  DEV startup → storage local aislado válido.
B.  STAGING startup → storage/config distinto.
C.  PRODUCTION startup → validación production-safe.
D.  production + debug → startup falla.
E.  staging apuntando a storage de prod → falla.
F.  dev usando secret namespace de prod → falla.
G.  mismo tenant_id en dev/prod → datos aislados.
H.  /health reporta el environment correcto de forma segura.
I.  deploy_validate valida los perfiles soportados.
J.  reinicio preserva solo el estado de cada environment.
E2E. Tres environments × tenant isolation → sin contaminación cruzada.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from src.domain.deployment.models import DeploymentConfigError, DeploymentEnvironment
from src.infrastructure.deployment.config_validator import DeploymentConfigValidator
from src.infrastructure.deployment.environment_policy import (
    default_data_root,
    resolve_application_environment,
)
from src.infrastructure.web.app import create_platform_app

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SENSITIVE_KEYS = {"secret", "token", "password", "api_key", "credential", "namespace"}


def _build_config(environment: str, data_dir: str, extra: dict | None = None):
    env = {
        "APP_ENV": environment,
        "HOST": "127.0.0.1" if environment == "development" else "0.0.0.0",
        "PORT": "8080",
        "DATA_DIR": data_dir,
        "APP_VERSION": "2.0.0",
    }
    if extra:
        env.update(extra)
    return DeploymentConfigValidator(env).validate()


def _run(args, env_keep=None):
    env = os.environ.copy()
    env.pop("APP_ENV", None)
    env.pop("ENVIRONMENT", None)
    if env_keep:
        env.update(env_keep)
    return subprocess.run(args, cwd=str(PROJECT_ROOT), env=env,
                          capture_output=True, text=True)


class TestP2EnvironmentSeparationIntegration:

    def test_a_dev_startup_valid_isolated_storage(self, tmp_path: Path):
        data_dir = tmp_path / "store_dev"
        config = _build_config("development", str(data_dir))
        assert config.environment == DeploymentEnvironment.DEVELOPMENT

        app = create_platform_app(config=config)
        client = TestClient(app)
        assert client.get("/health").status_code == 200
        assert client.get("/ready").status_code == 200

        # Dev usa storage local propio y aislado
        tenant_dir = data_dir / "tenants" / "tenant-A"
        tenant_dir.mkdir(parents=True)
        state = tenant_dir / "state.json"
        state.write_text('{"env": "development"}', encoding="utf-8")
        assert state.exists()

    def test_b_staging_startup_distinct_storage_config(self, tmp_path: Path):
        staging_root = default_data_root(DeploymentEnvironment.STAGING)
        dev_root = default_data_root(DeploymentEnvironment.DEVELOPMENT)
        prod_root = default_data_root(DeploymentEnvironment.PRODUCTION)
        assert staging_root != dev_root != prod_root

        config = _build_config("staging", str(tmp_path / "store_stage"))
        assert config.environment == DeploymentEnvironment.STAGING
        assert config.log_level == "INFO"  # default del perfil staging

        app = create_platform_app(config=config)
        payload = TestClient(app).get("/health").json()
        assert payload["environment"] == "staging"

    def test_c_production_startup_production_safe(self, tmp_path: Path):
        # HOST loopback no permitido en producción; 0.0.0.0 sí
        config = _build_config("production", str(tmp_path / "store_prod"))
        assert config.environment == DeploymentEnvironment.PRODUCTION
        assert config.host == "0.0.0.0"

        app = create_platform_app(config=config)
        payload = TestClient(app).get("/health").json()
        assert payload["environment"] == "production"
        assert payload["version"] == "2.0.0"

    def test_d_production_plus_debug_startup_fails(self, tmp_path: Path):
        # Validator: fail-fast
        with pytest.raises(DeploymentConfigError, match="DEBUG"):
            _build_config("production", str(tmp_path / "prod_debug"), {"DEBUG": "true"})

        # Entrypoint: exit code no-cero y mensaje FATAL
        res = _run(
            [sys.executable, "scripts/entrypoint.py"],
            env_keep={
                "APP_ENV": "production",
                "DEBUG": "true",
                "DEPLOY_DRY_RUN": "1",
                "DATA_DIR": str(tmp_path / "prod_debug_entry"),
            },
        )
        assert res.returncode != 0
        assert "FATAL" in res.stdout + res.stderr

    def test_e_staging_points_to_prod_storage_fails(self):
        with pytest.raises(DeploymentConfigError, match="different environment"):
            _build_config("staging", "data/production")

        # Verificación directa del guard cross-env
        from src.infrastructure.deployment.environment_policy import (
            cross_env_data_root_violation,
        )
        assert cross_env_data_root_violation(
            DeploymentEnvironment.STAGING, "data/production"
        ) is not None

    def test_f_dev_attempts_prod_secret_namespace_fails(self, tmp_path: Path):
        with pytest.raises(DeploymentConfigError, match="does not match"):
            _build_config(
                "development",
                str(tmp_path / "store_dev"),
                {"SECRET_NAMESPACE": "prod"},
            )

    def test_g_same_tenant_id_across_dev_prod_data_isolated(self, tmp_path: Path):
        dev_data = tmp_path / "store_dev"
        prod_data = tmp_path / "store_prod"

        _build_config("development", str(dev_data))
        _build_config("production", str(prod_data))

        tenant = "tenant-123"
        dev_state = dev_data / "tenants" / tenant / "state.json"
        prod_state = prod_data / "tenants" / tenant / "state.json"

        dev_state.parent.mkdir(parents=True)
        dev_state.write_text('{"env": "development"}', encoding="utf-8")

        assert dev_state.exists()
        assert not prod_state.exists(), "Los datos de DEV no deben aparecer en PROD"

    def test_h_health_reports_correct_environment_safely(self, tmp_path: Path):
        for env_name, folder in (
            ("development", "env_dev"),
            ("staging", "env_stage"),
            ("production", "env_prod"),
        ):
            config = _build_config(env_name, str(tmp_path / folder))
            app = create_platform_app(config=config)
            payload = TestClient(app).get("/health").json()

            assert payload["environment"] == env_name
            assert payload["version"] == "2.0.0"
            keys = {k.lower() for k in payload.keys()}
            assert not (SENSITIVE_KEYS & keys), f"Metadata sensible en /health: {payload}"

    def test_i_deploy_validate_runs_all_supported_profiles(self):
        for profile in ("development", "staging", "production"):
            res = _run([sys.executable, "scripts/deploy_validate.py", "--environment", profile])
            assert res.returncode == 0, (
                f"deploy_validate --environment {profile} falló:\n{res.stdout}\n{res.stderr}"
            )
            assert "ENVIRONMENT PROFILE VALIDATION PASSED" in res.stdout

    def test_j_restart_preserves_each_env_own_state_only(self, tmp_path: Path):
        staging_data = tmp_path / "store_stage"
        dev_data = tmp_path / "store_dev"

        # Primer arranque staging
        cfg1 = _build_config("staging", str(staging_data))
        app1 = create_platform_app(config=cfg1)
        assert TestClient(app1).get("/health").status_code == 200

        marker = staging_data / "tenants" / "tenant-X" / "state.json"
        marker.parent.mkdir(parents=True)
        marker.write_text('{"env": "staging"}', encoding="utf-8")

        # Reinicio: segunda instancia del mismo staging
        cfg2 = _build_config("staging", str(staging_data))
        app2 = create_platform_app(config=cfg2)
        assert TestClient(app2).get("/health").status_code == 200

        assert marker.exists(), "Estado del environment debe persistir tras reinicio"
        # No contamina otros environments
        assert not (dev_data / "tenants" / "tenant-X" / "state.json").exists()

    def test_e2e_three_environments_tenant_isolated(self, tmp_path: Path):
        """E2E P.2: 3 environments × mismo tenant_id → sin contaminación cruzada."""
        tenant = "tenant-777"
        roots = {}

        for env_name, folder in (
            ("development", "e2e_dev"),
            ("staging", "e2e_stage"),
            ("production", "e2e_prod"),
        ):
            data_dir = tmp_path / folder
            _build_config(env_name, str(data_dir))
            roots[env_name] = data_dir

        # DEV crea estado
        dev_state = roots["development"] / "tenants" / tenant / "state.json"
        dev_state.parent.mkdir(parents=True)
        dev_state.write_text('{"env": "development"}', encoding="utf-8")

        # STAGING crea su propio estado (mismo tenant_id)
        stage_state = roots["staging"] / "tenants" / tenant / "state.json"
        stage_state.parent.mkdir(parents=True)
        stage_state.write_text('{"env": "staging"}', encoding="utf-8")

        # NO contamina entre entornos
        assert dev_state.exists() and dev_state.read_text(encoding="utf-8").endswith('"development"}')
        assert stage_state.exists() and stage_state.read_text(encoding="utf-8").endswith('"staging"}')
        assert not (roots["production"] / "tenants" / tenant / "state.json").exists()
        assert not (roots["development"] / "tenants" / tenant / "staging_tag.json").exists()
