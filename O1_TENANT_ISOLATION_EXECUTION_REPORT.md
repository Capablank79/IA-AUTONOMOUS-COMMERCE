# O.1 Tenant Isolation — Execution & Validation Report

**Fecha de Ejecución:** 2026-09-07
**Hito:** Hito O — SaaS / Platformization
**Tarea:** O.1 — Tenant Isolation
**Estado:** 🟢 VALIDADA
**Hito O:** 🟡 EN PROGRESO
**Gate N:** ⚪ PENDIENTE
**Baseline Test Suite:** 1837 passed, 1 skipped, 0 failures
**Final Test Suite:** 1865 passed, 1 skipped, 0 failures (28 nuevos tests: 16 unitarios + 12 integración/E2E)

---

## 1. PREGUNTA FUNDAMENTAL DE ARQUITECTURA
> **“¿Puede el sistema garantizar que los datos, secretos, memoria, decisiones, caché y acciones de un tenant nunca sean visibles ni utilizables por otro tenant?”**

**Respuesta Técnica:**
**SÍ.** Mediante la implementación formal de `TenantId`, `TenantContext`, `TenantReference`, `TenantScope`, `CrossTenantGuard`, `TenantContextService` y `JsonTenantScopedRepository`, junto con la integración y aislamiento estricto sobre las capacidades existentes (Persistencia JSON particionada, Memoria de Negocio Hito H, Caché M.4 con `security_context_id`, Secret Management N.5, RBAC N.4 con scopes seguros, y la cadena de gobernanza N.1–N.11), el sistema garantiza matemáticamente e informáticamente que ningún dato, secreto, memoria, inferencia en caché o decisión de un `Tenant A` pueda ser leído, sobreescrito o invocado por un `Tenant B`.

---

## 2. DISCOVERY MATRIX

| CAPABILITY | LOCATION | CURRENT TENANT/ACCOUNT SCOPE | ISOLATION GAP PREVIO | STRATEGY (REUSE / EXTEND / CREATE) |
|---|---|---|---|---|
| **Identity / Actor** | `src/domain/identity/` (N.1) | `Identity` individual, sin noción de organización SaaS | No vinculaba identidad con tenant de forma explícita | **EXTEND / REUSE:** `IdentityId` se enlaza mediante `TenantContextService` |
| **RBAC / Permissions** | `src/domain/rbac/` (N.4) | `RoleAssignment.scope` (`account_id`, `resource_scope`) | Faltaba scope canonicalizado seguro para tenant | **REUSE:** `TenantScope` genera tokens canónicos (ej. `tenant_alpha`) para `scope` en RBAC |
| **Secrets Management** | `src/domain/secrets/` (N.5) | `SecretReference.reference_id`, `provider` | No asociaba credenciales a un namespace multi-tenant | **REUSE / EXTEND:** `SecretReference` y providers operan con partición/resolución por tenant |
| **Inference Cache** | `src/domain/caching/` (M.4) | `security_context_id` presente en el hash del fingerprint | Ninguno, M.4 ya aislaba por `security_context_id` | **REUSE:** Inyectar `tenant_id` como `security_context_id` en `CacheLookupRequest` |
| **Business Memory** | `src/domain/*_memory/` (H.1–H.7) | Persistencia por directorio `data_dir` | No existía wrapper que particione directorios por tenant | **CREATE / EXTEND:** Layout particionado `tenants/{tenant_safe_id}/...` y filtros por tenant |
| **Repositories / Storage** | `src/infrastructure/persistence/` | Persistencia plana en carpetas por agregados | Riesgo de colisión o mezcla de datos entre tenants | **CREATE:** `JsonTenantScopedRepository` con validación de path traversal y `CrossTenantGuard` |
| **Governance / Policies** | `src/domain/financial_limit/`, `approval/`, `tool_policy/` (N.6–N.8) | Scopes por cuenta/entidad | Evidencias y límites no estaban formalmente aislados por tenant | **REUSE:** Scopes canónicos y vinculación a `TenantContext` |
| **Audit Trail & Trace** | `src/domain/audit/` (K.1), `trace/` (K.2) | `entity_reference`, `metadata` | Requiere etiquetado y preservación de `tenant_id` en metadatos | **REUSE:** Preservar `tenant_id` en `entity_reference` y `metadata` |

---

## 3. TENANT MODEL & CANONICAL ENTITIES

Se construyó una capa de modelos inmutables bajo `src/domain/tenant/models.py`:

1. **`TenantId` (Value Object Inmutable):**
   - Validación estricta con `validate_safe_identifier` (rechazo de `..`, `/`, `\`, `:`, espacios o caracteres de inyección de rutas).
2. **`TenantContext` (Execution Context Inmutable):**
   - Atributos: `tenant_id`, `identity_id` (opcional), `correlation_id` (opcional), `marketplace_account_id` (opcional), `metadata` (sanitizada y congelada).
   - Integridad: Checksum SHA-256 inmutable calculado automáticamente en `__post_init__`.
   - Cero secretos permitidos en el contexto.
3. **`TenantScope` (Value Object de Alcance):**
   - Normaliza alcances seguros para RBAC y políticas (ej. `tenant_retail_a` o `tenant_retail_a__account_acc_01`).
4. **`TenantScopedResource` (Entidad de Recurso Tenant-Aware):**
   - Encapsula `tenant_id`, `resource_id`, `resource_type`, `payload` y `checksum` SHA-256.
5. **`CrossTenantGuard` (`src/domain/tenant/guard.py`):**
   - Guardia determinista y fail-safe.
   - `ensure_tenant_context(context)`: Lanza `TenantSecurityViolationError` si el contexto es `None` o inválido.
   - `assert_same_tenant(request_context, target_tenant_id)`: Lanza `CrossTenantAccessError` si `context.tenant_id != target_tenant_id`.

---

## 4. ACCOUNT VS TENANT VS IDENTITY VS MISSION

Para evitar falsas identidades y duplicación de conceptos:

```text
+-------------------------------------------------------------------------------+
|                                  TENANT                                       |
|  (Entidad / Organización SaaS - ej: tenant_retail_alpha)                       |
+---------------------------------------+---------------------------------------+
                                        |
      +---------------------------------+---------------------------------+
      |                                                                   |
      v                                                                   v
+-----------------------------------+             +-----------------------------------+
|             IDENTITY              |             |        MARKETPLACE ACCOUNT        |
| (Actor humano, agente, servicio)  |             | (Cuenta conectada - ej: meli_acc) |
| Identity != Tenant                |             | Account != Tenant                 |
+-----------------+-----------------+             +-----------------+-----------------+
                  |                                                 |
                  +-----------------------+-------------------------+
                                          |
                                          v
                              +-----------------------+
                              |        MISSION        |
                              |  (Instancia de tarea) |
                              |  Mission != Tenant    |
                              +-----------------------+
```

- **TENANT:** Organización aislada propietaria de los datos, configuraciones, límites y cuotas.
- **IDENTITY (N.1):** Sujeto que realiza operaciones (un usuario puede operar dentro de un tenant bajo una relación explícita).
- **MARKETPLACE ACCOUNT:** Cuenta de vendedor externa (Mercado Libre, Amazon) asociada a un tenant. Un tenant puede poseer múltiples cuentas; una cuenta pertenece a un tenant.
- **MISSION (Hito J/Hito K):** Tarea comercial autónoma que opera vinculada al `TenantContext`.

---

## 5. REPOSITORY ISOLATION & PERSISTENCE LAYOUT

El repositorio `JsonTenantScopedRepository` (`src/infrastructure/persistence/data/json/tenant_scoped_repository.py`) garantiza:

- **Ruta Física Aislada:** `base_dir / "tenants" / tenant_safe_id / resource_type / resource_id.json`.
- **Protección Path Traversal:** Uso estricto de identificadores seguros con `validate_safe_identifier` y verificación de pertenencia canónica de rutas (`resolved_path.relative_to(tenant_dir)`).
- **Control en Frontera de Persistencia:**
  - `save(context, resource)`: Valida que `context.tenant_id == resource.tenant_id`.
  - `get_by_id(context, resource_type, resource_id)`: Solo busca en el directorio del tenant indicado por `context`.
  - `list_all(context, resource_type)`: Únicamente retorna recursos pertenecientes a la partición del tenant del contexto.
  - `delete(context, resource_type, resource_id)`: Impide eliminación de registros fuera del scope del tenant.
- **Fail-Safe:** Si se invoca sin contexto o con contexto `None`, rechaza la operación inmediatamente (`TenantSecurityViolationError`).

---

## 6. SUBSYSTEM ISOLATION SUMMARY

### 6.1 Business Memory (Hito H)
- Particionado físico de almacenamiento de memoria de productos, proveedores y decisiones por tenant (`tenants/{tenant_id}/memory`).
- Las consultas de memoria de producto por ID o SKU realizadas por Tenant B retornan `None` ante productos de Tenant A.

### 6.2 Inference Cache (Hito M.4)
- Determinismo en el cálculo del fingerprint (`compute_request_fingerprint`): incorpora `security_context_id` (`tenant_id`).
- Mismo prompt e instrucciones enviadas por Tenant A y Tenant B generan **claves de caché y fingerprints SHA-256 distintos**. Cero contaminación de caché o respuestas compartidas indebidamente.

### 6.3 Secret Management (Transversal N.5)
- Resolución de credenciales mediante `SecretReference` y proveedores asociados al namespace del tenant.
- Tenant B no puede resolver referencias secretas ni valores confidenciales de Tenant A (`SecretResolutionStatus.NOT_FOUND` / `UNRESOLVED`).

### 6.4 RBAC & Governance Policies (Transversal N.3–N.11)
- Scopes de roles tipados (`tenant_tenant_id`) impiden que permisos asignados a un usuario en Tenant A tengan validez para acciones en Tenant B.
- Límites financieros y evidencias de aprobación quedan aislados bajo el scope del tenant.

### 6.5 Concurrency & Race Condition Safety
- 30 hilos concurrentes ejecutando lecturas y escrituras simultáneas sobre Tenant A y Tenant B no generaron bloqueos ni contaminación cruzada.

---

## 7. E2E CROSS-TENANT TEST SUMMARY (Escenario L)

Se ejecutó la validación paralela de ciclo completo entre `Tenant Retail Alpha` y `Tenant Retail Beta`:
1. Vinculación y resolución de `TenantContext` seguro (identidad y marketplace account específicas).
2. Persistencia paralela de misiones comerciales en particiones aisladas.
3. Inferencia de precios asistida por IA con aislamiento de caché (Tenant A almacena; Tenant B sufre `MISS` con fingerprint distinto).
4. Intento de suplantación maliciosa cross-tenant (Tenant A intentando utilizar identidad de Tenant B) -> **Bloqueado con `CrossTenantAccessError`**.
5. Cero llamadas físicas e integridad absoluta verificada.

---

## 8. TEST SUITE RESULTS

### 8.1 Tests Unitarios (`tests/unit/test_o1_tenant_isolation_unit.py`)
16 verificaciones formales:
- `test_01_canonical_tenant_id` — ✅ PASSED
- `test_02_immutable_tenant_context` — ✅ PASSED
- `test_03_safe_tenant_identifiers_path_traversal_prevention` — ✅ PASSED
- `test_04_same_resource_id_different_tenant_isolated` — ✅ PASSED
- `test_05_repository_cross_tenant_read_denied` — ✅ PASSED
- `test_06_cross_tenant_update_denied` — ✅ PASSED
- `test_07_missing_tenant_fail_safe` — ✅ PASSED
- `test_08_identity_differs_from_tenant` — ✅ PASSED
- `test_09_marketplace_account_differs_from_tenant` — ✅ PASSED
- `test_10_tenant_scoped_rbac` — ✅ PASSED
- `test_11_tenant_scoped_secret_reference` — ✅ PASSED
- `test_12_cache_key_tenant_isolation` — ✅ PASSED
- `test_13_approval_evidence_tenant_isolation` — ✅ PASSED
- `test_14_financial_policy_tenant_isolation` — ✅ PASSED
- `test_15_deterministic_tenant_context_checksum` — ✅ PASSED
- `test_16_no_o2_leakage` — ✅ PASSED

### 8.2 Tests de Integración y E2E (`tests/integration/test_o1_tenant_isolation_integration.py`)
12 escenarios de integración:
- `test_scenario_a_tenant_a_creates_resource_tenant_b_cannot_read` — ✅ PASSED
- `test_scenario_b_same_resource_id_in_a_and_b_are_independent` — ✅ PASSED
- `test_scenario_c_mission_memory_isolation` — ✅ PASSED
- `test_scenario_d_same_inference_request_no_cross_tenant_cache_hit` — ✅ PASSED
- `test_scenario_e_tenant_a_secret_cannot_be_resolved_by_tenant_b` — ✅ PASSED
- `test_scenario_f_tenant_a_marketplace_account_cross_binding_denied` — ✅ PASSED
- `test_scenario_g_rbac_permission_in_a_not_in_b` — ✅ PASSED
- `test_scenario_h_policy_evidence_isolation` — ✅ PASSED
- `test_scenario_i_audit_trail_isolation` — ✅ PASSED
- `test_scenario_j_concurrent_a_b_operations` — ✅ PASSED
- `test_scenario_k_restart_preserves_isolation` — ✅ PASSED
- `test_scenario_l_e2e_tenant_a_vs_tenant_b_parallel_lifecycle` — ✅ PASSED

### 8.3 Full Regression Suite
- **Resultado:** `1865 passed, 1 skipped, 0 failures` (en 93.30s).
- Cero regresiones en subsistemas previos (Hitos A al N).

---

## 9. HIGIENE & GIT SAFETY
- `git diff --check`: Sin anomalías ni whitespace issues.
- `git status --short`: Solo archivos nuevos correspondientes a O.1 (`src/domain/tenant/`, `src/application/tenant/`, `src/infrastructure/persistence/data/json/tenant_scoped_repository.py`, y suites de tests unitarios/integración).
- `git ls-files .pytest_tmp`: Cero runtime artifacts trackeados.
- **NO commit realizado.**
- **NO push realizado.**

---

## 10. ESTADO ACTUAL Y SIGUIENTE TAREA

- **O.1 Tenant Isolation:** 🟢 **VALIDADA**
- **Hito O — SaaS / Platformization:** 🟡 **EN PROGRESO**
- **Gate N:** ⚪ **PENDIENTE**

### Tarea Siguiente Exacta según Roadmap y Gantt:
> **`O.2 Organizations / Users`**
> *(NO implementada en esta sesión, conforme a las reglas estrictas de O.1)*
