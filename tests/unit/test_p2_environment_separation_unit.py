"""Suite de pruebas unitarias para P.2 Environment Separation (Hito P).

Cubre:
1.  Entorno canónico development.
2.  Entorno canónico staging.
3.  Entorno canónico production.
4.  Entorno desconocido rechazado (fail-fast).
5.  Entorno explícito (APP_ENV single source of truth, divergencia rechazada).
6.  Producción + DEBUG rechazado.
7.  Producción + secreto en texto plano rechazado.
8.  Data roots distintos entre entornos.
9.  Dev no puede usar el data root de prod.
10. Staging no puede usar el secret namespace de prod.
11. Config de tenant != config de environment.
12. Config mock insegura rechazada en production-like.
13. Metadata de /health segura.
14. Normalización de entorno determinista.
15. Config requerida faltante en producción → falla.
16. Sin implementación de P.3+.
"""

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from src.domain.deployment.models import (
    ApplicationEnvironment,
    DeploymentConfigError,
    DeploymentEnvironment,
    HealthStatus,
    normalize_environment_name,
)
from src.infrastructure.deployment.config_validator import (
    DeploymentConfigValidator,
)
from src.infrastructure.deployment.environment_policy import (
    CrossEnvironmentAccessError,
    ENVIRONMENT_PROFILES,
    EnvironmentResolutionError,
    EnvironmentSafetyError,
    cross_env_data_root_violation,
    default_data_root,
    resolve_application_environment,
    validate_environment_policy,
    validate_production_safety,
    validate_secret_namespace,
)
from src.infrastructure.web.app import create_platform_app


class TestCanonicalEnvironmentModel:

    def test_01_canonical_development(self):
        assert resolve_application_environment({"APP_ENV": "development"}) == (
            ApplicationEnvironment.DEVELOPMENT
        )
        assert ENVIRONMENT_PROFILES[ApplicationEnvironment.DEVELOPMENT].mock_providers_allowed is True

    def test_02_canonical_staging(self):
        assert resolve_application_environment({"APP_ENV": "staging"}) == (
            ApplicationEnvironment.STAGING
        )
        profile = ENVIRONMENT_PROFILES[ApplicationEnvironment.STAGING]
        assert profile.production_like is True
        assert profile.mock_providers_allowed is False

    def test_03_canonical_production(self):
        assert resolve_application_environment({"APP_ENV": "production"}) == (
            ApplicationEnvironment.PRODUCTION
        )
        profile = ENVIRONMENT_PROFILES[ApplicationEnvironment.PRODUCTION]
        assert profile.debug_allowed is False
        assert profile.mock_providers_allowed is False
        assert profile.production_like is True

    def test_04_unknown_environment_rejected(self):
        # Resolución directa: fail-fast
        with pytest.raises(DeploymentConfigError, match="Unknown environment"):
            resolve_application_environment({"APP_ENV": "unknown"})
        # Validator (arranque canónico estricto): fail-fast
        with pytest.raises(DeploymentConfigError, match="Invalid ENVIRONMENT"):
            DeploymentConfigValidator({"ENVIRONMENT": "weird"}).validate()

    def test_05_environment_explicit(self):
        # Sin APP_ENV/ENVIRONMENT en contexto estricto → fail
        with pytest.raises(DeploymentConfigError, match="explicitly"):
            resolve_application_environment({}, strict_explicit=True)
        # APP_ENV y ENVIRONMENT divergentes → fail-fast (single source of truth)
        with pytest.raises(EnvironmentResolutionError, match="Only one source of truth"):
            resolve_application_environment(
                {"APP_ENV": "development", "ENVIRONMENT": "staging"}
            )
        # Legado ENVIRONMENT compatible cuando APP_ENV no está
        assert resolve_application_environment({"ENVIRONMENT": "production"}) == (
            ApplicationEnvironment.PRODUCTION
        )


class TestProductionSafety:

    def test_06_prod_debug_rejected(self):
        with pytest.raises(EnvironmentSafetyError, match="DEBUG"):
            validate_production_safety(
                ApplicationEnvironment.PRODUCTION, {"DEBUG": "true"}
            )
        with pytest.raises(EnvironmentSafetyError, match="LOG_LEVEL=DEBUG"):
            validate_production_safety(
                ApplicationEnvironment.PRODUCTION, {"LOG_LEVEL": "DEBUG"}
            )
        # El validator también lo rechaza en arranque
        with pytest.raises(DeploymentConfigError):
            DeploymentConfigValidator(
                {"APP_ENV": "production", "DEBUG": "true"}
            ).validate()

    def test_07_prod_plaintext_secret_rejected(self, tmp_path: Path):
        env = {
            "APP_ENV": "production",
            "DATA_DIR": str(tmp_path / "prod_store"),
            "PLAIN_TEXT_API_KEY": "super-secret",
        }
        with pytest.raises(DeploymentConfigError, match="strictly forbidden|forbidden"):
            DeploymentConfigValidator(env).validate()

    def test_08_environment_data_roots_differ(self):
        root_dev = default_data_root(ApplicationEnvironment.DEVELOPMENT)
        root_staging = default_data_root(ApplicationEnvironment.STAGING)
        root_prod = default_data_root(ApplicationEnvironment.PRODUCTION)
        assert root_dev != root_staging != root_prod
        assert root_dev == "data/development"
        assert root_staging == "data/staging"
        assert root_prod == "data/production"

    def test_09_dev_cannot_use_prod_root(self):
        violation = cross_env_data_root_violation(
            ApplicationEnvironment.DEVELOPMENT, "data/production"
        )
        assert violation is not None, "DEV apuntando al root de PROD debe ser una violación"
        with pytest.raises(DeploymentConfigError, match="different environment"):
            DeploymentConfigValidator(
                {"APP_ENV": "development", "DATA_DIR": "data/production"}
            ).validate()


class TestSecretNamespaceIsolation:

    def test_10_staging_cannot_use_prod_secret_namespace(self):
        with pytest.raises(CrossEnvironmentAccessError, match="does not match"):
            validate_secret_namespace(
                ApplicationEnvironment.STAGING, {"SECRET_NAMESPACE": "prod"}
            )
        with pytest.raises(DeploymentConfigError, match="does not match"):
            DeploymentConfigValidator(
                {
                    "APP_ENV": "staging",
                    "SECRET_NAMESPACE": "prod",
                    "DATA_DIR": "data/staging",
                }
            ).validate()


class TestConfigSeparation:

    def test_11_tenant_config_not_environment_config(self):
        # Config de tenant horneada en variables de plataforma es rechazada
        # (permanece tenant-scoped en O.11, no define el environment global).
        from src.domain.deployment.models import HardcodedTenantConfigError

        with pytest.raises(HardcodedTenantConfigError, match="Baked tenant configuration"):
            DeploymentConfigValidator(
                {"APP_ENV": "production", "TENANT_ACME_CONFIG": "id=acme"}
            ).validate()
        # Las variables de tenant NO influyen en la resolución del environment
        assert (
            resolve_application_environment(
                {"APP_ENV": "development", "TENANT_ACME_CONFIG": "id=acme"}
            )
            == ApplicationEnvironment.DEVELOPMENT
        )

    def test_12_production_mock_unsafe_config_rejected(self):
        # Producción: mock provider → fallo
        with pytest.raises(EnvironmentSafetyError, match="Mock provider"):
            validate_production_safety(
                ApplicationEnvironment.PRODUCTION,
                {"MOCK_PAYMENT_PROVIDER": "true"},
            )
        with pytest.raises(EnvironmentSafetyError, match="Mock provider"):
            validate_production_safety(
                ApplicationEnvironment.PRODUCTION,
                {"LLM_PROVIDER_IMPLEMENTATION": "mock"},
            )
        # Staging también es production-like: mock provider → fallo
        with pytest.raises(EnvironmentSafetyError, match="Mock provider"):
            validate_production_safety(
                ApplicationEnvironment.STAGING, {"USE_MOCK_LLM": "true"}
            )
        # Dev sí permite mocks explícitos
        validate_production_safety(
            ApplicationEnvironment.DEVELOPMENT, {"MOCK_PAYMENT_PROVIDER": "true"}
        )
        # El validator rechaza el mock en producción durante el arranque
        with pytest.raises(DeploymentConfigError, match="Mock provider"):
            DeploymentConfigValidator(
                {
                    "APP_ENV": "production",
                    "MOCK_PAYMENT_PROVIDER": "true",
                    "DATA_DIR": "data/production",
                }
            ).validate()


class TestHealthMetadataAndNormalization:

    def test_13_safe_health_metadata(self, tmp_path: Path):
        config = DeploymentConfigValidator(
            {
                "APP_ENV": "development",
                "HOST": "127.0.0.1",
                "DATA_DIR": str(tmp_path / "health_store"),
                "APP_VERSION": "9.9.9",
            }
        ).validate()
        app = create_platform_app(config=config)
        client = TestClient(app)
        payload = client.get("/health").json()

        assert payload["environment"] == "development"
        assert payload["version"] == "9.9.9"
        # No expone secretos, filesystem internals ni credenciales
        sensitive_keys = {"secret", "token", "password", "api_key", "credential"}
        assert not (sensitive_keys & {k.lower() for k in payload.keys()})

        # HealthStatus reutiliza environment + version como metadata segura (K.1/K.2)
        status = HealthStatus(
            status="ok",
            environment="production",
            version="1.0.0",
            storage_writable=True,
        )
        assert status.environment == "production"
        assert status.version == "1.0.0"

    def test_14_environment_normalization_deterministic(self):
        assert normalize_environment_name("  DEVELOPMENT  ") == (
            ApplicationEnvironment.DEVELOPMENT
        )
        assert normalize_environment_name("Prod") == ApplicationEnvironment.PRODUCTION
        assert normalize_environment_name("stage") == ApplicationEnvironment.STAGING
        # APP_ENV y ENVIRONMENT con el mismo valor → mismo resultado
        assert resolve_application_environment({"APP_ENV": "development"}) == (
            resolve_application_environment({"ENVIRONMENT": "development"})
        )
        # Idempotente: dos resoluciones producen el mismo enum
        assert (
            resolve_application_environment({"APP_ENV": "development"})
            == resolve_application_environment({"APP_ENV": "development"})
        )


class TestFailSafeAndScope:

    def test_15_missing_required_prod_config_fails(self):
        # Host vacío → fail (config requerida ausente/inválida)
        with pytest.raises(DeploymentConfigError, match="HOST"):
            DeploymentConfigValidator(
                {"APP_ENV": "production", "HOST": ""}
            ).validate()
        # Sin APP_ENV/ENVIRONMENT en contexto estricto → fail
        with pytest.raises(DeploymentConfigError, match="explicitly"):
            resolve_application_environment({}, strict_explicit=True)

    def test_16_no_p3_plus_implementation(self):
        """P.2 no implementa ni referencia características de P.3+."""
        forbidden = (
            "P.3",
            "database migrations",
            "backups",
            "disaster recovery",
            "kubernetes",
            "monitoring",
            "alerting",
            "capacity planning",
        )
        root = Path(__file__).resolve().parent.parent.parent
        scanned = [
            root / "src" / "infrastructure" / "deployment" / "environment_policy.py",
            root / "src" / "infrastructure" / "deployment" / "config_validator.py",
            root / "src" / "domain" / "deployment" / "models.py",
            root / "scripts" / "deploy_validate.py",
            root / "scripts" / "entrypoint.py",
        ]
        for path in scanned:
            assert path.exists(), f"Archivo esperado inexistente: {path}"
            text = path.read_text(encoding="utf-8").lower()
            for marker in forbidden:
                assert marker not in text, f"Referencia a '{marker}' en {path.name}"
