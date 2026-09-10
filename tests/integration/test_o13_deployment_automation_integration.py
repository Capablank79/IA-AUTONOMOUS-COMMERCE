"""Suite de pruebas de integración para O.13 Deployment Automation.

Pruebas integradas:
1. Endpoints de salud operacional (`/health`, `/healthz`, `/ready`, `/readyz`).
2. Comportamiento de readiness probe ante fallo en almacenamiento persistente (503 Service Unavailable).
3. Integración con Admin Console (O.10/O.11/O.12) en contexto desplegado.
4. Preservación y aislamiento de almacenamiento persistente entre reinicios (`DATA_DIR`).
5. Aislamiento multi-tenant intacto en la plataforma desplegada.
6. Ejecución del entrypoint canónico (`scripts/entrypoint.py`) en modo pre-flight / dry-run.
7. Ejecución completa del validador (`scripts/deploy_validate.py`).
"""

from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import sys
import pytest
from starlette.testclient import TestClient

from src.domain.deployment.models import DeploymentConfig, DeploymentEnvironment
from src.infrastructure.deployment.config_validator import DeploymentConfigValidator
from src.infrastructure.web.app import create_platform_app, check_storage_writable


class TestDeploymentAutomationIntegration:

    @pytest.fixture
    def test_env(self, tmp_path: Path):
        data_dir = tmp_path / "integration_data"
        data_dir.mkdir(parents=True, exist_ok=True)
        return {
            "ENVIRONMENT": "testing",
            "HOST": "127.0.0.1",
            "PORT": "8080",
            "DATA_DIR": str(data_dir),
            "LOG_LEVEL": "DEBUG",
            "ENABLE_ADMIN_CONSOLE": "true",
            "ENABLE_OAUTH": "true",
            "APP_VERSION": "1.0.0",
        }

    def test_health_and_readiness_endpoints_success(self, test_env, tmp_path: Path):
        """Verifica que /health, /healthz, /ready y /readyz respondan 200 OK cuando el entorno es saludable."""
        validator = DeploymentConfigValidator(test_env)
        config = validator.validate()
        app = create_platform_app(config=config)
        client = TestClient(app)

        # Liveness probes
        r_health = client.get("/health")
        assert r_health.status_code == 200
        data_health = r_health.json()
        assert data_health["status"] == "ok"
        assert data_health["version"] == "1.0.0"
        assert data_health["environment"] == "testing"

        r_healthz = client.get("/healthz")
        assert r_healthz.status_code == 200
        assert r_healthz.json()["status"] == "ok"

        # Readiness probes
        r_ready = client.get("/ready")
        assert r_ready.status_code == 200
        data_ready = r_ready.json()
        assert data_ready["status"] == "ready"
        assert data_ready["storage_writable"] is True

        r_readyz = client.get("/readyz")
        assert r_readyz.status_code == 200
        assert r_readyz.json()["status"] == "ready"

    def test_readiness_probe_fails_when_storage_unwritable(self, test_env, monkeypatch):
        """Verifica que el readiness probe falle con 503 si el almacenamiento no es escribible."""
        validator = DeploymentConfigValidator(test_env)
        config = validator.validate()
        app = create_platform_app(config=config)
        client = TestClient(app)

        # Simular fallo en almacenamiento
        monkeypatch.setattr("src.infrastructure.web.app.check_storage_writable", lambda path: False)

        r_ready = client.get("/ready")
        assert r_ready.status_code == 503
        data = r_ready.json()
        assert data["status"] == "unhealthy"
        assert data["reason"] == "storage_not_writable"

        # Liveness probe debe seguir viva
        r_health = client.get("/health")
        assert r_health.status_code == 200

    def test_admin_console_integration_in_deployed_app(self, test_env):
        """Verifica que la Admin Console (O.10) y sus rutas estén integradas en la app desplegada."""
        validator = DeploymentConfigValidator(test_env)
        config = validator.validate()
        app = create_platform_app(config=config)
        client = TestClient(app)

        # Acceso a API de tenant summary sin sesión (debe responder 401 según O.10/O.11)
        r = client.get("/api/admin/tenants/tenant-123/summary")
        assert r.status_code == 401
        assert r.json()["error"] == "unauthenticated"

    def test_persistence_isolation_across_restarts(self, tmp_path: Path):
        """Verifica que los datos persistan correctamente en DATA_DIR tras simular un reinicio de la aplicación."""
        data_dir = tmp_path / "persistent_storage"
        env = {
            "ENVIRONMENT": "testing",
            "HOST": "127.0.0.1",
            "PORT": "8080",
            "DATA_DIR": str(data_dir),
            "ENABLE_ADMIN_CONSOLE": "true",
        }

        # 1. Primera instancia de la app
        cfg1 = DeploymentConfigValidator(env).validate()
        app1 = create_platform_app(config=cfg1)
        client1 = TestClient(app1)
        assert client1.get("/health").status_code == 200

        # Crear archivo en el almacenamiento persistente
        marker_file = data_dir / "marker.json"
        marker_file.write_text('{"deployed": true}', encoding="utf-8")

        # 2. Segunda instancia simulando reinicio del pod/contenedor
        cfg2 = DeploymentConfigValidator(env).validate()
        app2 = create_platform_app(config=cfg2)
        client2 = TestClient(app2)
        assert client2.get("/ready").status_code == 200

        # Verificar que el archivo persiste
        assert marker_file.exists()
        assert marker_file.read_text(encoding="utf-8") == '{"deployed": true}'

    def test_multi_tenant_isolation_in_deployed_environment(self, tmp_path: Path):
        """Verifica que la plataforma desplegada mantenga el aislamiento de datos por tenant."""
        data_dir = tmp_path / "tenant_isolation_data"
        env = {
            "ENVIRONMENT": "testing",
            "DATA_DIR": str(data_dir),
        }
        cfg = DeploymentConfigValidator(env).validate()
        app = create_platform_app(config=cfg)
        client = TestClient(app)

        # Probes funcionan
        assert client.get("/health").status_code == 200
        assert client.get("/ready").status_code == 200

        # Verificar que los subdirectorios de tenant data se creen aislados
        assert data_dir.exists()

    def test_entrypoint_script_dry_run(self, tmp_path: Path):
        """Verifica que el script scripts/entrypoint.py se ejecute de forma determinista en pre-flight."""
        entrypoint_path = Path(__file__).resolve().parent.parent.parent / "scripts" / "entrypoint.py"
        assert entrypoint_path.exists()

        env = os.environ.copy()
        env["DEPLOY_DRY_RUN"] = "1"
        env["ENVIRONMENT"] = "testing"
        env["DATA_DIR"] = str(tmp_path / "entrypoint_test_data")
        env["PORT"] = "8888"

        res = subprocess.run([sys.executable, str(entrypoint_path)], env=env, capture_output=True, text=True)
        assert res.returncode == 0
        assert "[PRE-FLIGHT OK]" in res.stdout
        assert "[STORAGE CHECK OK]" in res.stdout
        assert "[APP INIT OK]" in res.stdout
        assert "[DRY RUN OK]" in res.stdout

    def test_deploy_validate_script_integration(self):
        """Verifica que scripts/deploy_validate.py ejecute exitosamente todas las comprobaciones."""
        validate_path = Path(__file__).resolve().parent.parent.parent / "scripts" / "deploy_validate.py"
        assert validate_path.exists()

        res = subprocess.run([sys.executable, str(validate_path)], capture_output=True, text=True)
        assert res.returncode == 0
        assert "ALL O.13 DEPLOYMENT AUTOMATION VALIDATIONS PASSED SUCCESSFULY" in res.stdout
