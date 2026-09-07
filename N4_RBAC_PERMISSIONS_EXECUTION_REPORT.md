# REPORTE DE EJECUCIÓN: HITO N.4 — RBAC / PERMISSIONS
**Transversal N: Security, Governance y Safety**
**AI Autonomous Commerce Framework**
**Fecha:** 2026-09-04
**Estado:** 🟢 VALIDADA

---

## 1. Resumen Ejecutivo

Se ha implementado y validado formalmente el hito **N.4 — RBAC / Permissions** del Transversal N (*Security, Governance y Safety*).

La responsabilidad fundamental de **N.4** responde de manera canónica, inmutable y determinista a la pregunta:
> **"¿Qué roles y permisos tiene asignados esta identidad y qué capacidades concretas representan?"**

El resultado de evaluación es un `PermissionSet` y `RbacEvaluationResult` inmutable que alimenta directamente a **N.3 Authorization** (`AuthorizationService` y `AuthorizationGuardedActionExecutor`) a través de `allowed_actions_override`, aplicando el principio fundamental:

> **DEFAULT DENY**: La ausencia de roles válidos, permisos explícitos o coincidencias de ámbito (*scope*) resulta siempre en 0 permisos concedidos y denegación estricta por política.

---

## 2. Separación Estricta de Responsabilidades y Fronteras Arquitecturales

| Capacidad | Hito | Estado | Responsabilidad y Fronteras |
|---|---|---|---|
| **Identity** | N.1 | 🟢 VALIDADA | Identidad canónica e inmutable del actor (`IdentityReference`, `Identity`). |
| **Authentication** | N.2 | 🟢 VALIDADA | Verificación probatoria de identidad (`PrincipalContext`, `AuthenticationResult`). |
| **Authorization** | N.3 | 🟢 VALIDADA | Decisión formal de acceso basada en políticas (`AuthorizationRequest`, `AuthorizationDecision`, `AuthorizationService`). |
| **RBAC / Permissions** | N.4 | 🟢 VALIDADA | Catálogo de permisos, definición de roles, asignaciones a identidades, resolución determinista de capacidades efectivas y validación de scopes. **NO ejecuta acciones** y **NO duplica AuthorizationService**. |
| **Secret Management** | N.5 | 🟡 EN PROGRESO | Gestión de secretos, vaults y cifrado en reposo/tránsito (sin tocar en N.4). |
| **Approval Policies** | N.6 | ⚪ PENDIENTE | Flujos de aprobación y control humano (Gate M / N.6) (sin tocar en N.4). |
| **Financial Limits** | N.7 | ⚪ PENDIENTE | Límites monetarios y presupuestarios (sin tocar en N.4). |
| **Gate M** | Gate M | ⚪ PENDIENTE | Evaluación global y cierre del Transversal N (permanece PENDIENTE). |

---

## 3. Matriz de Reuso y Extensión de Arquitectura (REUSE > EXTEND > CREATE)

| Capacidad Requerida | Ubicación en Repo | Estrategia N.4 |
|---|---|---|
| **Identity Models** | `src/domain/identity/` (N.1) | **REUSE**: Vinculación inmutable de roles a `identity_id` canónico. |
| **Principal Context** | `src/domain/authentication/` (N.2) | **REUSE**: Extracción del `identity_id` autenticado desde `PrincipalContext` para resolver permisos efectivos. |
| **Authorization Service & Executor** | `src/application/authorization/` (N.3) | **EXTEND & INTEGRATE**: Inyección de `RBACService` en `AuthorizationGuardedActionExecutor` y evaluación downstream en `AuthorizationService` mediante `allowed_actions_override` sin duplicar la lógica de decisión. |
| **Security & Sanitization (K.8)** | `src/domain/security/models.py` | **REUSE**: `sanitize_security_data`, `deep_freeze` y `validate_safe_identifier` aplicados en permisos, roles, asignaciones y eventos. |
| **Audit Trail (K.1)** | `src/domain/audit/` | **EXTEND mínimo**: Adición de `AuditRecordType.ROLE_ASSIGNED`, `AuditRecordType.ROLE_REVOKED` y `AuditRecordType.RBAC_EVALUATED` en `src/domain/audit/models.py`. |
| **Agent Trace (K.2)** | `src/domain/agent_trace/` | **REUSE**: Registro inmutable de pasos `StepType.POLICY_EVALUATION` para resolución RBAC. |
| **Reliability Clock (K.7)** | `src/domain/reliability/ports.py` | **REUSE**: Inyección de `ClockPort` / `VirtualClock` para control temporal determinista de asignaciones expiradas. |
| **Crash-Safe Persistence** | `src/infrastructure/persistence/data/json/` | **REUSE PATTERN**: Repositorios JSON atómicos (`JsonRoleRepository`, `JsonRoleAssignmentRepository`) con `.tmp`, `os.fsync`, `os.replace`, hash SHA-256 e índice en memoria protegido por `threading.RLock`. |

---

## 4. Modelos de Dominio y Servicios Implementados

### 4.1 Dominio (`src/domain/rbac/models.py`)
- **`Permission`**: Modelo inmutable con `permission_id`, `action`, `resource_type`, `description`, `status` (`ACTIVE`, `DISABLED`), `version`, `checksum` SHA-256 y `metadata`.
- **`Role`**: Modelo inmutable con `role_id`, `name`, `description`, `permissions` (tupla inmutable), `status` (`ACTIVE`, `DISABLED`, `DEPRECATED`), `version`, `checksum` SHA-256 y `metadata`.
- **`RoleAssignment`**: Modelo inmutable con `assignment_id`, `identity_id`, `role_id`, `scope` (opcional), `assigned_at`, `expires_at` (opcional), `source`, `checksum` SHA-256 y `metadata`.
- **`PermissionSet`**: Contenedor inmutable que expone `actions` (`frozenset[str]`), `permissions_by_action`, métodos de consulta canónica (`has_action()`, `get_permission()`, `is_action_allowed_in_scope()`) y `actions_tuple`.
- **`RbacEvaluationResult`**: Objeto inmutable de resolución con `identity_id`, `effective_permissions`, `active_roles`, `revoked_roles`, `expired_roles`, `invalid_roles`, `evaluated_at`, `correlation_id`, `checksum` SHA-256 y `metadata`.
- **Normalización Canónica**: Función `normalize_action_token()` que estandariza `listing.publish` / `price:update` / `order-manage` a formato seguro `LISTING_PUBLISH`, `PRICE_UPDATE`, `ORDER_MANAGE`.
- **Integridad Criptográfica**: Funciones deterministas `compute_permission_checksum`, `compute_role_checksum`, `compute_role_assignment_checksum`, `compute_evaluation_checksum`.

### 4.2 Puertos (`src/domain/rbac/ports.py`)
- **`RoleRepositoryPort`**: Interfaz abstracta para guardar, obtener, listar y eliminar roles.
- **`RoleAssignmentRepositoryPort`**: Interfaz abstracta para guardar, obtener, listar por identidad y revocar asignaciones de roles.

### 4.3 Aplicación (`src/application/rbac/rbac_service.py`)
- **`RBACService`**: Orquestador central de RBAC:
  - `create_permission()` / `get_permission()`: Gestión de catálogo granular de permisos.
  - `define_role()` / `get_role()`: Definición inmutable de roles.
  - `assign_role()` / `revoke_role()`: Asignación idempotente y revocación con verificación de duplicados/conflictos.
  - `resolve_effective_permissions()`: Resolución determinista de permisos activos de una identidad, descartando roles inactivos, asignaciones expiradas o registros corruptos, y validando scopes exactos.
  - Registro seguro en auditoría (K.1) y trazas (K.2) con sanitización de secretos.

### 4.4 Infraestructura (`src/infrastructure/persistence/data/json/rbac_repository.py`)
- **`JsonRoleRepository`** y **`JsonRoleAssignmentRepository`**:
  - Escritura atómica crash-safe con archivos temporales y `fsync`.
  - Detección de colisiones de identificadores y prevención de path traversal.
  - Validación de integridad mediante checksum SHA-256 al cargar desde disco.
  - Indexación en memoria thread-safe mediante `threading.RLock`.

---

## 5. Prevención de Riesgos de Seguridad y Escalada de Privilegios

1. **No Autoasignación / Privilegios Arbitrarios**: Las asignaciones requieren llamadas explícitas a `assign_role` con fuentes autorizadas (`source`), previniendo que metadatos del request otorguen permisos automáticos.
2. **Identidades Desconocidas / Inválidas**: Un actor `UNKNOWN` o no registrado no posee roles asignados => `PermissionSet` vacío => N.3 Authorization bloquea cualquier acción (`ACTION_NOT_ALLOWED`).
3. **Expiración Temporal Estricta**: Si `expires_at <= now` según `ClockPort`, la asignación es ignorada y catalogada en `expired_roles`.
4. **Resistencia a la Corrupción / Tampering**: Si el archivo JSON de un rol o asignación es alterado en disco, la verificación del checksum SHA-256 falla, se aísla el registro corrupto y no se conceden permisos.
5. **Aislamiento por Scope**: Un rol asignado para `scope="account_a"` sólo valida acciones dirigidas a `account_a`. Cualquier intento sobre `account_b` es denegado por falta de alcance.
6. **Cero Fuga de Secretos**: Todas las estructuras pasan por `sanitize_security_data()` garantizando que tokens OAuth, contraseñas y claves API queden redactados como `[REDACTED]`.

---

## 6. Resultados de Verificación y Testing

### 6.1 Pruebas Unitarias (`tests/unit/test_n4_rbac_permissions_unit.py`)
**16/16 tests PASSED** (100% pass):
1. `test_01_immutable_permission_model`: Inmutabilidad `@dataclass(frozen=True)` en `Permission`.
2. `test_02_immutable_role_model`: Inmutabilidad de roles y tuplas de permisos.
3. `test_03_role_resolves_permissions`: Resolución correcta de permisos desde roles activos.
4. `test_04_multiple_roles_union_permissions`: Unión determinista de permisos de múltiples roles asignados.
5. `test_05_unknown_role_no_permission`: Roles desconocidos/inexistentes no conceden permisos.
6. `test_06_missing_assignment_no_permission`: Identidades sin asignaciones obtienen 0 permisos (Default Deny).
7. `test_07_scoped_permission_resolution`: Resolución y coincidencia de scopes específicos.
8. `test_08_wrong_scope_denied`: Scope no coincidente deniega la capacidad.
9. `test_09_expired_assignment_ignored`: Asignaciones con fecha expirada no conceden permisos.
10. `test_10_idempotent_role_assignment`: Replay idéntico de asignación es idempotente.
11. `test_11_duplicate_conflict_handling`: Detección de conflictos semánticos en asignaciones incompatibles.
12. `test_12_deterministic_effective_permission_set`: Checksums SHA-256 y orden determinista en `PermissionSet`.
13. `test_13_no_privilege_escalation_by_request_metadata`: Metadata externa no eleva permisos.
14. `test_14_sanitized_metadata_and_no_secrets`: Cero filtración de credenciales en metadata de RBAC.
15. `test_15_rbac_is_not_authorization`: Separación formal: RBAC entrega capacidades; Authorization decide.
16. `test_16_no_n5_plus_implementation`: Ausencia de invasión o solapamiento con N.5–N.11.

### 6.2 Pruebas de Integración y E2E (`tests/integration/test_n4_rbac_permissions_integration.py`)
**9/9 tests PASSED** (100% pass):
- **Escenario A**: Rol `VIEWER` permite lectura en N.3 y bloquea mutaciones.
- **Escenario B**: Rol `OPERATOR` permite acciones operativas explícitas.
- **Escenario C**: Rol con scope `account_a` permite acciones en `account_a` y bloquea `account_b`.
- **Escenario D**: Identidad sin roles sufre Default Deny absoluto en N.3.
- **Escenario E**: Asignación expirada es ignorada y bloqueada en N.3.
- **Escenario F**: Persistencia JSON crash-safe mantiene roles y asignaciones tras reinicio del servicio.
- **Escenario G**: Detección de manipulación/corrupción de asignación bloquea concesión de privilegios.
- **Escenario H**: Auditoría K.1 y trazas K.2 seguras sin almacenamiento de secretos.
- **Escenario I**: Pipeline E2E completo: `Actor -> N.1 Identity -> N.2 Authentication -> N.4 RBAC -> N.3 Authorization -> ActionExecutor`. Permiso válido ejecuta 1 vez; permiso ausente ejecuta 0 veces.

### 6.3 Regresión Global del Sistema
- **Baseline inicial**: 1617 passed, 1 skipped, 0 failures.
- **Suite completa final**: **1642 passed, 1 skipped, 0 failures, 0 errors** (54.24s).
- **Incremento neto**: +25 pruebas pasando (16 unitarias + 9 de integración).

---

## 7. Higiene del Workspace y Control de Versiones

- `git ls-files .pytest_tmp`: Sin archivos temporales trackeados.
- `git diff --check`: Limpio (sin trailing whitespaces ni conflictos).
- `git status`: Modificaciones limitadas a `src/domain/audit/models.py`, `docs/market-intelligence/AI_AUTONOMOUS_COMMERCE_GANTT_MAESTRA.md` y archivos de nueva implementación/tests.
- **Restricción cumplida**: Cero commits realizados (`NO commit`), cero pushes realizados (`NO push`).

---

## 8. Conclusión y Próximo Hito

El Hito **N.4 — RBAC / Permissions** ha quedado completamente implementado, auditado, integrado y validado con 100% de tests pasando y 0 fallos de regresión.

- **Estado Actual Transversal N**:
  - N.1 Identity: 🟢 VALIDADA
  - N.2 Authentication: 🟢 VALIDADA
  - N.3 Authorization: 🟢 VALIDADA
  - **N.4 RBAC / Permissions: 🟢 VALIDADA**
  - N.5 Secret Management: 🟡 EN PROGRESO (Siguiente Hito)
  - N.6–N.11: ⚪ PENDIENTE
  - Gate M: ⚪ PENDIENTE
  - Hito N: 🟡 EN PROGRESO

**NEXT TASK**: N.5 — Secret Management.
