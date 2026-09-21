"""Suite de pruebas unitarias para P.1 CI/CD (Hito P — Production / Operations).

Valida el contrato del workflow de GitHub Actions `.github/workflows/ci.yml`
mediante comprobaciones de contenido robustas (stdlib):

1. El workflow existe en la ubicación estándar.
2. Trigger `pull_request` hacia master.
3. Trigger `push` a master.
4. Runtime de Python compatible con O.13 (3.10).
5. `python -m pytest` invocado (condición: exit code 0).
6. `python scripts/deploy_validate.py` invocado.
7. `docker build` invocado (validación del artefacto, sin push).
8. Sin secretos en texto plano.
9. Sin `continue-on-error` en checks críticos.
10. Sin despliegue automático a producción.
11. Permisos mínimos (`contents: read`).
12. Sin implementación de P.2+.
13. Sintaxis YAML válida (parsing con parser disponible si existe).
"""

from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
WORKFLOW_PATH = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"

# Patrones que delatarían un secreto real embebido en el workflow.
SECRET_PATTERNS = (
    "sk-",
    "ghp_",
    "github_pat_",
    "AKIA",
    "-----BEGIN",
    "Bearer ",
    "client_secret",
    "refresh_token=",
    "access_token=",
    "api_key=",
    "password=",
    "secret_key=",
)

# Marcadores de despliegue/producción que NO deben existir en CI puro.
PRODUCTION_DEPLOY_MARKERS = (
    "docker push",
    "gh release",
    "kubectl",
    "heroku",
    "azure/webapp",
    "gcloud app deploy",
    "cloud run",
    "environment:",
    "deploy-to-production",
    "deploy_production",
)

# Marcadores de características P.2+ que no deben aparecer.
P2_PLUS_MARKERS = (
    "P.2",
    "P.3",
    "P.4",
    "environment separation",
    "database migrations",
    "backups",
    "disaster recovery",
    "kubernetes",
    "monitoring",
    "alerting",
    "log retention",
    "capacity planning",
)


def _workflow_text() -> str:
    return WORKFLOW_PATH.read_text(encoding="utf-8")


class TestP1CICDWorkflowUnit:

    def test_01_workflow_exists(self):
        """El workflow debe existir en la ubicación estándar de GitHub Actions."""
        assert WORKFLOW_PATH.is_file(), (
            f"Workflow CI no encontrado en {WORKFLOW_PATH.relative_to(PROJECT_ROOT)}"
        )

    def test_02_pull_request_trigger_to_master(self):
        """Debe existir trigger pull_request filtrado a master."""
        text = _workflow_text()
        assert "pull_request:" in text
        assert "branches:" in text
        assert "master" in text

    def test_03_push_master_trigger(self):
        """Debe existir trigger push a master."""
        text = _workflow_text()
        assert "push:" in text
        assert "master" in text

    def test_04_python_runtime_matches_o13(self):
        """El runtime debe ser Python 3.10 (compatible con el Dockerfile O.13)."""
        text = _workflow_text()
        assert "python-version:" in text
        assert '"3.10"' in text or "3.10" in text.split("python-version:")[1].split("\n")[0]

    def test_05_pytest_invoked(self):
        """CI debe ejecutar la suite completa vía `python -m pytest`."""
        text = _workflow_text()
        assert "python -m pytest" in text

    def test_06_deploy_validate_invoked(self):
        """CI debe invocar la validación de despliegue O.13, sin duplicar su lógica."""
        text = _workflow_text()
        assert "python scripts/deploy_validate.py" in text

    def test_07_docker_build_invoked_without_push(self):
        """CI debe validar `docker build` sin publicar imágenes a ningún registry."""
        text = _workflow_text()
        assert "docker build" in text
        assert "docker push" not in text

    def test_08_no_plaintext_secrets(self):
        """El workflow no debe contener secretos/credenciales en texto plano."""
        text = _workflow_text()
        for pattern in SECRET_PATTERNS:
            assert pattern not in text, f"Patrón de secreto encontrado en workflow: {pattern}"

    def test_09_no_continue_on_error(self):
        """No debe usarse `continue-on-error` (checks críticos deben fallar el workflow)."""
        text = _workflow_text()
        assert "continue-on-error" not in text
        assert "continue_on_error" not in text

    def test_10_no_production_deployment(self):
        """P.1 es CI puro: no debe existir despliegue automático a producción."""
        text = _workflow_text()
        for marker in PRODUCTION_DEPLOY_MARKERS:
            assert marker not in text, f"Marcador de despliegue productivo encontrado: {marker}"

    def test_11_minimal_permissions(self):
        """Permisos mínimos: `contents: read`; sin permisos de escritura."""
        text = _workflow_text()
        assert "contents: read" in text
        for marker in ("contents: write", "packages: write", "id-token: write",
                       "pull-requests: write", "actions: write", "issues: write"):
            assert marker not in text, f"Permiso elevado encontrado: {marker}"

    def test_12_no_p2_plus_implementation(self):
        """P.1 no debe implementar ni referenciar características de P.2+."""
        text = _workflow_text()
        for marker in P2_PLUS_MARKERS:
            assert marker not in text, f"Referencia a P.2+ encontrada: {marker}"

    def test_13_yaml_syntax_valid(self):
        """El workflow debe ser YAML parseable (YAML 1.1 quirk: clave `on` -> True)."""
        yaml = pytest.importorskip("yaml", reason="Parser YAML no disponible en este entorno")
        data = yaml.safe_load(_workflow_text())
        # `on` se serializa como booleano True bajo YAML 1.1 (PyYAML); GitHub usa YAML 1.2.
        trigger_map = data[True] if True in data else data["on"]
        assert trigger_map is not None
        assert "jobs" in data
        assert "validate" in data["jobs"]
        assert "docker-build" in data["jobs"]
