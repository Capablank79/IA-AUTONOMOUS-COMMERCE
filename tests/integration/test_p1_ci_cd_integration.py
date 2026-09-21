"""Suite de pruebas de integración para P.1 CI/CD (Hito P — Production / Operations).

Valida localmente el contrato del pipeline CI sin emular GitHub:

A. Los comandos CI se ejecutan en la secuencia/contrato esperados.
B. `python scripts/deploy_validate.py` resulta PASS.
C. La suite completa de tests es invocable en un entorno CI-safe.
D. Una configuración de despliegue inválida produce fallo no-cero.
E. No se requieren llamadas reales a proveedores externos.
F. La configuración/contrato Docker es coherente (Dockerfile, .dockerignore, build).
"""

import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
WORKFLOW_PATH = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"

# Endpoints de proveedores/productores externos que el pipeline NO debe invocar.
EXTERNAL_PROVIDER_MARKERS = (
    "api.mercadolibre",
    "api.openai",
    "api.anthropic",
    "api.stripe",
    "sendgrid",
    "twilio",
    "https://",
)


def _run(args, env_extra=None):
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    return subprocess.run(args, cwd=str(PROJECT_ROOT), env=env,
                          capture_output=True, text=True)


class TestP1CICDPipelineIntegration:

    def test_a_ci_sequence_contract(self):
        """El workflow define validate antes de docker-build y deploy_validate antes de pytest."""
        text = WORKFLOW_PATH.read_text(encoding="utf-8")

        idx_validate = text.index("  validate:")
        idx_docker = text.index("  docker-build:")
        assert idx_validate < idx_docker, "El job 'validate' debe preceder a 'docker-build'"

        idx_deploy = text.index("python scripts/deploy_validate.py")
        idx_pytest = text.index("python -m pytest")
        assert idx_deploy < idx_pytest, "La validación de despliegue debe ejecutarse antes de pytest"

        assert "needs: validate" in text, "docker-build debe depender de 'validate'"

    def test_b_deploy_validate_passes(self):
        """La validación de despliegue O.13 debe resultar PASS (exit code 0)."""
        result = _run([sys.executable, "scripts/deploy_validate.py"])
        assert result.returncode == 0, (
            f"deploy_validate falló con código {result.returncode}:\n{result.stdout}\n{result.stderr}"
        )
        assert "PASSED" in result.stdout

    def test_c_full_tests_invocable_in_ci_safe_env(self):
        """La suite completa debe ser recolectable sin red ni dependencias de la máquina dev."""
        result = _run([sys.executable, "-m", "pytest", "--collect-only", "-q"])
        assert result.returncode == 0, (
            f"Colección de tests falló:\n{result.stdout}\n{result.stderr}"
        )
        tail = [line for line in result.stdout.splitlines() if line.strip()][-1:]
        assert tail and "collected" in tail[0], f"Salida de colección inesperada: {result.stdout}"

    def test_d_invalid_deployment_config_fails_nonzero(self):
        """Una config de despliegue insegura debe abortar el arranque con código no-cero."""
        result = _run(
            [sys.executable, "scripts/entrypoint.py"],
            env_extra={"DEPLOY_DRY_RUN": "1", "DATA_DIR": "/etc", "ENVIRONMENT": "testing"},
        )
        assert result.returncode != 0, (
            "Entrypoint aceptó DATA_DIR inseguro y terminó con código 0"
        )
        assert "FATAL" in result.stderr + result.stdout

    def test_e_no_real_external_provider_calls_required(self):
        """El pipeline no referencia endpoints de proveedores externos reales."""
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
        for marker in EXTERNAL_PROVIDER_MARKERS:
            assert marker not in workflow, f"Endpoint externo real en workflow: {marker}"

        # Los tests de la plataforma usan fakes/mocks: sin claves de proveedor en CI.
        assert "secrets." not in workflow

    def test_f_docker_config_contract_coherent(self):
        """Dockerfile, .dockerignore y workflow deben ser coherentes entre sí."""
        dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
        dockerignore = (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8")
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

        # El workflow debe invocar el build del image O.13.
        assert "docker build" in workflow

        # Paths que Dockerfile copia deben existir en el repositorio.
        for copy_path in ("src/", "scripts/", "oauth/", "pyproject.toml"):
            assert (PROJECT_ROOT / copy_path).exists(), f"Ruta copiada por Dockerfile inexistente: {copy_path}"

        # .dockerignore debe excluir secretos y datos de runtime.
        for ignore in (".env", ".runtime", "__pycache__", ".pytest_cache"):
            assert ignore in dockerignore, f".dockerignore no excluye: {ignore}"

        # Dockerfile O.13: multi-stage y non-root.
        assert "AS " in dockerfile and "USER ${USERNAME}" in dockerfile
