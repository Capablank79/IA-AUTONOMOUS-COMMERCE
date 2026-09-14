"""
Implementación JSON persistente, atómica y determinista para Roles y Role Assignments (Hito N.4).

Garantiza:
- Atomic write (.tmp -> fsync -> os.replace).
- Idempotencia estricta para roles y asignaciones idénticos (mismo checksum).
- Detección explícita de conflictos ante diferente checksum para mismo ID (RoleConflictError, RoleAssignmentConflictError).
- Verificación estricta de integridad SHA-256 en lectura y detección de corrupción (CorruptedRoleRecordError, CorruptedRoleAssignmentRecordError).
- Thread-safe mediante RLock de concurrencia.
- Path safety estricto (validate_safe_identifier).
- Cero almacenamiento de secretos/PII.
"""

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
from types import MappingProxyType
from typing import Union, Optional, Any, Dict, List, Sequence
from contextlib import contextmanager
import threading

from src.domain.rbac.models import (
    Role,
    RoleAssignment,
    RoleStatus,
    compute_role_checksum,
    compute_role_assignment_checksum,
)
from src.domain.rbac.ports import RoleRepositoryPort, RoleAssignmentRepositoryPort
from src.domain.security.models import validate_safe_identifier, SENSITIVE_KEYS

logger = logging.getLogger(__name__)


class JsonRBACRepositoryError(Exception):
    """Excepción base para errores en los repositorios JSON de RBAC."""
    pass


class RoleConflictError(JsonRBACRepositoryError):
    """Se lanza cuando se intenta registrar un rol con el mismo ID pero diferente checksum."""
    pass


class RoleAssignmentConflictError(JsonRBACRepositoryError):
    """Se lanza cuando se intenta registrar una asignación con el mismo ID pero diferente checksum."""
    pass


class CorruptedRoleRecordError(JsonRBACRepositoryError):
    """Se lanza cuando un archivo de rol persistido está corrupto o tiene checksum inválido."""
    pass


class CorruptedRoleAssignmentRecordError(JsonRBACRepositoryError):
    """Se lanza cuando un archivo de asignación persistido está corrupto o tiene checksum inválido."""
    pass


def _encode_json_value(val: Any) -> Any:
    """Serializa valores de forma determinista y sanitiza claves sensibles recursivamente."""
    if isinstance(val, datetime):
        return val.isoformat()
    if hasattr(val, "value"):
        return val.value
    if isinstance(val, (dict, MappingProxyType)):
        cleaned = {}
        for k, v in val.items():
            k_str = str(k).lower()
            if any(s in k_str for s in SENSITIVE_KEYS):
                cleaned[str(k)] = "[REDACTED]"
            else:
                cleaned[str(k)] = _encode_json_value(v)
        return cleaned
    if isinstance(val, (list, tuple)):
        return [_encode_json_value(v) for v in val]
    return val


class JsonRoleRepository(RoleRepositoryPort):
    """
    Repositorio JSON persistente, atómico y determinista para Roles (N.4).
    Organización en disco:
      base_dir/
        roles/
          {role_id}.json
        index/
          roles_index.jsonl
    """

    def __init__(self, base_dir: Union[str, Path]):
        self.base_dir = Path(base_dir)
        self.roles_dir = self.base_dir / "roles"
        self.index_dir = self.base_dir / "index"

        self.roles_dir.mkdir(parents=True, exist_ok=True)
        self.index_dir.mkdir(parents=True, exist_ok=True)

        self.roles_index_file = self.index_dir / "roles_index.jsonl"
        self._thread_lock = threading.RLock()
        with self._exclusive_lock():
            self._recover_index_if_needed()

    @contextmanager
    def _exclusive_lock(self):
        with self._thread_lock:
            yield

    def _recover_index_if_needed(self) -> None:
        valid_index = self.roles_index_file.exists()
        if valid_index:
            try:
                with open(self.roles_index_file, "r", encoding="utf-8") as handle:
                    for line in handle:
                        if line.strip():
                            json.loads(line)
            except (OSError, json.JSONDecodeError):
                valid_index = False
        if valid_index:
            return

        entries = []
        for role_file in sorted(self.roles_dir.glob("*.json")):
            try:
                role = self._load_role_file(role_file)
                entries.append({
                    "role_id": role.role_id,
                    "name": role.name,
                    "status": role.status.value,
                    "permission_count": len(role.permissions),
                    "checksum": role.checksum,
                })
            except Exception as e:
                logger.warning(f"Skipping unrecoverable role file {role_file}: {e}")

        tmp_path = self.roles_index_file.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as handle:
            for entry in entries:
                handle.write(json.dumps(_encode_json_value(entry), sort_keys=True, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, self.roles_index_file)

    def _atomic_write_json(self, file_path: Path, data: Dict[str, Any]) -> None:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = file_path.with_suffix(".tmp")
        payload = json.dumps(_encode_json_value(data), indent=2, sort_keys=True, ensure_ascii=False)
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, file_path)

    def _append_to_index(self, index_file: Path, entry: Dict[str, Any]) -> None:
        tmp_line = json.dumps(_encode_json_value(entry), sort_keys=True, ensure_ascii=False) + "\n"
        with open(index_file, "a", encoding="utf-8") as f:
            f.write(tmp_line)
            f.flush()
            os.fsync(f.fileno())

    def _load_role_file(self, file_path: Path) -> Role:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            role = Role.from_dict(data)

            recomputed_checksum = compute_role_checksum(
                role_id=role.role_id,
                name=role.name,
                permission_checksums=[p.checksum for p in role.permissions],
                status=role.status,
                policy_version=role.policy_version,
            )

            if role.checksum != recomputed_checksum:
                raise CorruptedRoleRecordError(
                    f"Checksum mismatch in {file_path.name}: file has '{role.checksum}', recomputed '{recomputed_checksum}'"
                )

            return role
        except (KeyError, ValueError, json.JSONDecodeError) as e:
            raise CorruptedRoleRecordError(f"Corrupted role record in {file_path}: {e}") from e

    def save(self, role: Role) -> Role:
        return self.save_role(role)

    def save_role(self, role: Role) -> Role:
        validate_safe_identifier(role.role_id, field_name="role_id")

        with self._exclusive_lock():
            role_file = self.roles_dir / f"{role.role_id}.json"

            if role_file.exists():
                try:
                    existing_role = self._load_role_file(role_file)
                    if existing_role.checksum == role.checksum:
                        return existing_role
                    else:
                        raise RoleConflictError(
                            f"Role '{role.role_id}' already exists with different checksum ({existing_role.checksum} vs {role.checksum})."
                        )
                except RoleConflictError:
                    raise
                except Exception as e:
                    raise CorruptedRoleRecordError(
                        f"Cannot verify existing role '{role.role_id}': {e}"
                    ) from e

            self._atomic_write_json(role_file, role.to_dict())

            self._append_to_index(
                self.roles_index_file,
                {
                    "role_id": role.role_id,
                    "name": role.name,
                    "status": role.status.value,
                    "permission_count": len(role.permissions),
                    "checksum": role.checksum,
                },
            )
            return role

    def get_role(self, role_id: str) -> Optional[Role]:
        validate_safe_identifier(role_id, field_name="role_id")

        with self._exclusive_lock():
            role_file = self.roles_dir / f"{role_id}.json"
            if not role_file.exists():
                return None
            return self._load_role_file(role_file)

    def list_roles(self, limit: int = 100) -> Sequence[Role]:
        with self._exclusive_lock():
            roles = []
            for role_file in sorted(self.roles_dir.glob("*.json")):
                if len(roles) >= limit:
                    break
                try:
                    roles.append(self._load_role_file(role_file))
                except Exception as e:
                    logger.warning(f"Skipping corrupted role {role_file}: {e}")
            return tuple(roles)

    def exists(self, role_id: str) -> bool:
        validate_safe_identifier(role_id, field_name="role_id")
        with self._exclusive_lock():
            return (self.roles_dir / f"{role_id}.json").exists()


class JsonRoleAssignmentRepository(RoleAssignmentRepositoryPort):
    """
    Repositorio JSON persistente, atómico y determinista para Role Assignments (N.4).
    Organización en disco:
      base_dir/
        assignments/
          {assignment_id}.json
        index/
          assignments_index.jsonl
    """

    def __init__(self, base_dir: Union[str, Path]):
        self.base_dir = Path(base_dir)
        self.assignments_dir = self.base_dir / "assignments"
        self.index_dir = self.base_dir / "index"

        self.assignments_dir.mkdir(parents=True, exist_ok=True)
        self.index_dir.mkdir(parents=True, exist_ok=True)

        self.assignments_index_file = self.index_dir / "assignments_index.jsonl"
        self._thread_lock = threading.RLock()
        with self._exclusive_lock():
            self._recover_index_if_needed()

    @contextmanager
    def _exclusive_lock(self):
        with self._thread_lock:
            yield

    def _recover_index_if_needed(self) -> None:
        valid_index = self.assignments_index_file.exists()
        if valid_index:
            try:
                with open(self.assignments_index_file, "r", encoding="utf-8") as handle:
                    for line in handle:
                        if line.strip():
                            json.loads(line)
            except (OSError, json.JSONDecodeError):
                valid_index = False
        if valid_index:
            return

        entries = []
        for asgn_file in sorted(self.assignments_dir.glob("*.json")):
            try:
                asgn = self._load_assignment_file(asgn_file)
                entries.append({
                    "assignment_id": asgn.assignment_id,
                    "identity_id": asgn.identity_id,
                    "role_id": asgn.role_id,
                    "scope": asgn.scope,
                    "assigned_at": asgn.assigned_at.isoformat(),
                    "expires_at": asgn.expires_at.isoformat() if asgn.expires_at else None,
                    "checksum": asgn.checksum,
                })
            except Exception as e:
                logger.warning(f"Skipping unrecoverable assignment file {asgn_file}: {e}")

        tmp_path = self.assignments_index_file.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as handle:
            for entry in entries:
                handle.write(json.dumps(_encode_json_value(entry), sort_keys=True, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, self.assignments_index_file)

    def _rebuild_index_unlocked(self) -> None:
        entries = []
        for asgn_file in sorted(self.assignments_dir.glob("*.json")):
            try:
                asgn = self._load_assignment_file(asgn_file)
                entries.append({
                    "assignment_id": asgn.assignment_id,
                    "identity_id": asgn.identity_id,
                    "role_id": asgn.role_id,
                    "scope": asgn.scope,
                    "assigned_at": asgn.assigned_at.isoformat(),
                    "expires_at": asgn.expires_at.isoformat() if asgn.expires_at else None,
                    "checksum": asgn.checksum,
                })
            except Exception as e:
                logger.warning(f"Skipping unrecoverable assignment file {asgn_file}: {e}")

        tmp_path = self.assignments_index_file.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as handle:
            for entry in entries:
                handle.write(json.dumps(_encode_json_value(entry), sort_keys=True, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, self.assignments_index_file)

    def _atomic_write_json(self, file_path: Path, data: Dict[str, Any]) -> None:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = file_path.with_suffix(".tmp")
        payload = json.dumps(_encode_json_value(data), indent=2, sort_keys=True, ensure_ascii=False)
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, file_path)

    def _append_to_index(self, index_file: Path, entry: Dict[str, Any]) -> None:
        tmp_line = json.dumps(_encode_json_value(entry), sort_keys=True, ensure_ascii=False) + "\n"
        with open(index_file, "a", encoding="utf-8") as f:
            f.write(tmp_line)
            f.flush()
            os.fsync(f.fileno())

    def _load_assignment_file(self, file_path: Path) -> RoleAssignment:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            asgn = RoleAssignment.from_dict(data)

            recomputed_checksum = compute_role_assignment_checksum(
                assignment_id=asgn.assignment_id,
                identity_id=asgn.identity_id,
                role_id=asgn.role_id,
                scope=asgn.scope,
                assigned_at=asgn.assigned_at,
                expires_at=asgn.expires_at,
                source=asgn.source,
                policy_version=asgn.policy_version,
            )

            if asgn.checksum != recomputed_checksum:
                raise CorruptedRoleAssignmentRecordError(
                    f"Checksum mismatch in {file_path.name}: file has '{asgn.checksum}', recomputed '{recomputed_checksum}'"
                )

            return asgn
        except (KeyError, ValueError, json.JSONDecodeError) as e:
            raise CorruptedRoleAssignmentRecordError(f"Corrupted role assignment in {file_path}: {e}") from e

    def save(self, assignment: RoleAssignment) -> RoleAssignment:
        return self.save_assignment(assignment)

    def save_assignment(self, assignment: RoleAssignment) -> RoleAssignment:
        validate_safe_identifier(assignment.assignment_id, field_name="assignment_id")
        validate_safe_identifier(assignment.identity_id, field_name="identity_id")
        validate_safe_identifier(assignment.role_id, field_name="role_id")

        with self._exclusive_lock():
            asgn_file = self.assignments_dir / f"{assignment.assignment_id}.json"

            if asgn_file.exists():
                try:
                    existing_asgn = self._load_assignment_file(asgn_file)
                    if existing_asgn.checksum == assignment.checksum:
                        return existing_asgn
                    else:
                        raise RoleAssignmentConflictError(
                            f"Assignment '{assignment.assignment_id}' already exists with different checksum ({existing_asgn.checksum} vs {assignment.checksum})."
                        )
                except RoleAssignmentConflictError:
                    raise
                except Exception as e:
                    raise CorruptedRoleAssignmentRecordError(
                        f"Cannot verify existing assignment '{assignment.assignment_id}': {e}"
                    ) from e

            self._atomic_write_json(asgn_file, assignment.to_dict())

            self._append_to_index(
                self.assignments_index_file,
                {
                    "assignment_id": assignment.assignment_id,
                    "identity_id": assignment.identity_id,
                    "role_id": assignment.role_id,
                    "scope": assignment.scope,
                    "assigned_at": assignment.assigned_at.isoformat(),
                    "expires_at": assignment.expires_at.isoformat() if assignment.expires_at else None,
                    "checksum": assignment.checksum,
                },
            )
            return assignment

    def get_assignment(self, assignment_id: str) -> Optional[RoleAssignment]:
        validate_safe_identifier(assignment_id, field_name="assignment_id")

        with self._exclusive_lock():
            asgn_file = self.assignments_dir / f"{assignment_id}.json"
            if not asgn_file.exists():
                return None
            return self._load_assignment_file(asgn_file)

    def list_assignments_for_identity(
        self,
        identity_id: str,
        scope: Optional[str] = None,
        limit: int = 100,
    ) -> Sequence[RoleAssignment]:
        validate_safe_identifier(identity_id, field_name="identity_id")

        with self._exclusive_lock():
            assignments = []
            for asgn_file in sorted(self.assignments_dir.glob("*.json")):
                if len(assignments) >= limit:
                    break
                try:
                    asgn = self._load_assignment_file(asgn_file)
                    if asgn.identity_id == identity_id:
                        if scope is None or asgn.applies_to_scope(scope):
                            assignments.append(asgn)
                except Exception as e:
                    logger.warning(f"Skipping corrupted assignment file {asgn_file}: {e}")
            return tuple(assignments)

    def revoke_assignment(self, assignment_id: str) -> bool:
        validate_safe_identifier(assignment_id, field_name="assignment_id")

        with self._exclusive_lock():
            asgn_file = self.assignments_dir / f"{assignment_id}.json"
            if not asgn_file.exists():
                return False
            try:
                os.remove(asgn_file)
                self._rebuild_index_unlocked()
                return True
            except OSError as e:
                logger.error(f"Error removing assignment {assignment_id}: {e}")
                return False

    def list_all_assignments(self, limit: int = 200) -> Sequence[RoleAssignment]:
        with self._exclusive_lock():
            assignments = []
            for asgn_file in sorted(self.assignments_dir.glob("*.json")):
                if len(assignments) >= limit:
                    break
                try:
                    assignments.append(self._load_assignment_file(asgn_file))
                except Exception as e:
                    logger.warning(f"Skipping corrupted assignment file {asgn_file}: {e}")
            return tuple(assignments)
