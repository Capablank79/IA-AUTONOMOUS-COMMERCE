"""Suite de pruebas unitarias para O.13 Deployment Automation.

Pruebas:
1. Validación de variables requeridas y valores por defecto.
2. Validación de tipos, puertos (1-65535) y hostnames.
3. Rechazo estricto de secretos en texto plano (SecretLeakError).
4. Rechazo estricto de configuración tenant embebida/hardcodeada (HardcodedTenantConfigError).
5. Rechazo de rutas de almacenamiento inseguras y path traversal (StoragePathSecurityError).
6. Fail-fast ante configuración inválida o faltante (UNKNOWN != ZERO).
7. Verificación de reglas de Dockerfile y .dockerignore.
8. Determinismo de serialización y aislamiento de modelos.
"""

from pathlib import Path
import pytest

from src.domain.deployment.models import (
    DeploymentConfig,
    DeploymentEnvironment,
    DeploymentConfigError,
    SecretLeakError,
    HardcodedTenantConfigError,
    StoragePathSecurityError,
)
from src.infrastructure.deployment.config_validator import (
    DeploymentConfigValidator,
    FORBIDDEN_SECRET_PATTERNS,
    FORBIDDEN_TENANT_PATTERNS,
)


class TestDeploymentConfigValidatorUnit:

    def test_default_valid_configuration(self):
        """Valida que una configuración mínima produzca un DeploymentConfig válido."""
        env = {
            "ENVIRONMENT": "production",
            "HOST": "0.0.0.0",
            "PORT": "8000",
            "DATA_DIR": "data",
        }
        validator = DeploymentConfigValidator(env)
        config = validator.validate()

        assert config.environment == DeploymentEnvironment.PRODUCTION
        assert config.host == "0.0.0.0"
        assert config.port == 8000
        assert str(config.data_dir) == "data"
        assert config.log_level == "INFO"
        assert config.enable_admin_console is True
        assert config.enable_oauth is True
        assert config.readiness_probes_enabled is True
        assert config.liveness_probes_enabled is True

    def test_custom_valid_configuration(self):
        """Valida parsing de campos personalizados y tipos."""
        env = {
            "ENVIRONMENT": "staging",
            "HOST": "127.0.0.1",
            "PORT": "9090",
            "DATA_DIR": "/var/app/data",
            "LOG_LEVEL": "DEBUG",
            "ENABLE_ADMIN_CONSOLE": "false",
            "ENABLE_OAUTH": "0",
            "APP_VERSION": "1.2.3",
            "ALLOWED_HOSTS": "api.example.com, admin.example.com",
            "READINESS_PROBES_ENABLED": "true",
            "LIVENESS_PROBES_ENABLED": "true",
        }
        validator = DeploymentConfigValidator(env)
        config = validator.validate()

        assert config.environment == DeploymentEnvironment.STAGING
        assert config.host == "127.0.0.1"
        assert config.port == 9090
        assert config.log_level == "DEBUG"
        assert config.enable_admin_console is False
        assert config.enable_oauth is False
        assert config.app_version == "1.2.3"
        assert config.allowed_hosts == ("api.example.com", "admin.example.com")

    @pytest.mark.parametrize("invalid_port", ["0", "-1", "65536", "99999", "abc", ""])
    def test_invalid_port_fail_fast(self, invalid_port: str):
        """Valida que puertos fuera de 1-65535 fallen inmediatamente."""
        env = {"PORT": invalid_port}
        validator = DeploymentConfigValidator(env)
        with pytest.raises(DeploymentConfigError, match="Invalid PORT"):
            validator.validate()

    @pytest.mark.parametrize("invalid_env", ["prod", "dev", "invalid", "123"])
    def test_invalid_environment_fail_fast(self, invalid_env: str):
        """Valida que entornos no soportados fallen de inmediato."""
        env = {"ENVIRONMENT": invalid_env}
        validator = DeploymentConfigValidator(env)
        with pytest.raises(DeploymentConfigError, match="Invalid ENVIRONMENT"):
            validator.validate()

    @pytest.mark.parametrize("invalid_host", ["", "   ", "http://localhost", "foo bar", "invalid..host"])
    def test_invalid_host_fail_fast(self, invalid_host: str):
        """Valida rechazo de hosts malformados."""
        env = {"HOST": invalid_host}
        validator = DeploymentConfigValidator(env)
        with pytest.raises(DeploymentConfigError):
            validator.validate()

    @pytest.mark.parametrize("secret_key", [
        "PLAIN_TEXT_SECRET",
        "PLAIN_TEXT_PASSWORD",
        "INSECURE_AUTH_TOKEN",
        "API_RAW_KEY",
        "DATABASE_RAW_SECRET",
        "HARDCODED_ADMIN_KEY",
        "ADMIN_PASSWORD",
        "SECRET_KEY_RAW",
        "DATABASE_PASSWORD_PLAIN",
    ])
    def test_rejection_of_plaintext_and_insecure_secrets(self, secret_key: str):
        """Valida rechazo estricto de secretos en texto plano o patrones inseguros."""
        env = {
            "ENVIRONMENT": "production",
            secret_key: "unencrypted_secret_value",
        }
        validator = DeploymentConfigValidator(env)
        with pytest.raises(SecretLeakError, match="strictly forbidden|forbidden"):
            validator.validate()

    @pytest.mark.parametrize("tenant_key", [
        "TENANT_ACME_CONFIG",
        "TENANT_123_CONFIG",
        "BAKED_TENANT_ID",
        "DEFAULT_TENANT_ID",
        "HARDCODED_TENANT_PLAN",
        "EMBEDDED_TENANTS",
    ])
    def test_rejection_of_baked_tenant_configuration(self, tenant_key: str):
        """Valida que la configuración de tenants no pueda ser horneada en variables globales."""
        env = {
            "ENVIRONMENT": "production",
            tenant_key: "hardcoded_tenant_value",
        }
        validator = DeploymentConfigValidator(env)
        with pytest.raises(HardcodedTenantConfigError, match="Baked tenant configuration detected"):
            validator.validate()

    @pytest.mark.parametrize("insecure_path", [
        "/",
        "/root",
        "/bin",
        "/etc",
        "/usr",
        "/sys",
        "C:\\",
        "C:\\Windows",
        "C:\\Program Files",
        "../secret_dir",
        "foo/../../bar",
    ])
    def test_rejection_of_insecure_storage_paths(self, insecure_path: str):
        """Valida que DATA_DIR no pueda apuntar a directorios de sistema o contener path traversal."""
        env = {
            "ENVIRONMENT": "production",
            "DATA_DIR": insecure_path,
        }
        validator = DeploymentConfigValidator(env)
        with pytest.raises(StoragePathSecurityError):
            validator.validate()

    def test_production_fails_if_allow_anonymous_admin_is_true(self):
        """Valida que en producción no se permita bypass de autenticación anónima."""
        env = {
            "ENVIRONMENT": "production",
            "ALLOW_ANONYMOUS_ADMIN": "true",
        }
        validator = DeploymentConfigValidator(env)
        with pytest.raises(DeploymentConfigError, match="ALLOW_ANONYMOUS_ADMIN cannot be enabled in PRODUCTION"):
            validator.validate()

    def test_to_dict_projection_is_safe_and_complete(self):
        """Valida que to_dict exponga datos estructurados sin comprometer la inmutabilidad."""
        config = DeploymentConfig(
            environment=DeploymentEnvironment.PRODUCTION,
            host="127.0.0.1",
            port=8080,
            data_dir=Path("safe_data"),
            log_level="WARNING",
            enable_admin_console=True,
            enable_oauth=False,
            app_version="2.0.0",
        )
        d = config.to_dict()
        assert d["environment"] == "production"
        assert d["host"] == "127.0.0.1"
        assert d["port"] == 8080
        assert d["data_dir"] == "safe_data"
        assert d["enable_oauth"] is False
        assert d["app_version"] == "2.0.0"


class TestDockerArtifactsUnit:

    def test_dockerfile_compliance(self):
        """Valida que el Dockerfile cumpla con los estándares de seguridad O.13."""
        dockerfile_path = Path(__file__).resolve().parent.parent.parent / "Dockerfile"
        assert dockerfile_path.exists(), "Dockerfile must exist at repository root"
        content = dockerfile_path.read_text(encoding="utf-8")

        # Multi-stage
        assert "FROM python:3.10-slim AS builder" in content
        assert "FROM python:3.10-slim AS runner" in content

        # Non-root user
        assert "USER ${USERNAME}" in content or "USER appuser" in content
        assert "10001" in content

        # No secrets baked
        assert "ENV SECRET" not in content
        assert "ENV API_KEY" not in content
        assert "ENV PASSWORD" not in content

        # Entrypoint y Healthcheck
        assert "ENTRYPOINT" in content
        assert "HEALTHCHECK" in content
        assert "scripts/entrypoint.py" in content

    def test_dockerignore_compliance(self):
        """Valida que .dockerignore excluya artefactos temporales y credenciales."""
        dockerignore_path = Path(__file__).resolve().parent.parent.parent / ".dockerignore"
        assert dockerignore_path.exists(), ".dockerignore must exist at repository root"
        content = dockerignore_path.read_text(encoding="utf-8")

        for pattern in [".git", ".env", "__pycache__", ".pytest_cache", ".runtime", "data/", "logs/"]:
            assert pattern in content, f"Pattern {pattern} missing in .dockerignore"
