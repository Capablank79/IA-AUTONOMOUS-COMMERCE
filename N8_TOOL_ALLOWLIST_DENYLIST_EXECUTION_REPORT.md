# INFORME DE EJECUCIÓN: HITO N.8 — TOOL ALLOWLIST / DENYLIST

## 1. RESUMEN EJECUTIVO

- **Hito**: N.8 — Tool Allowlist / Denylist
- **Transversal**: N — Security, Governance y Safety
- **Estado Previo**: ⚪ PENDIENTE
- **Estado Final**: 🟢 VALIDADA
- **Fecha de Validación**: 2026-09-07
- **Pregunta Central Resuelta**: *"¿Está permitido invocar esta herramienta/operación concreta dentro del contexto actual?"*
- **Baseline Previo**: 1721 passed, 1 skipped, 0 failures
- **Resultado Post-Implementación**: **1748 passed, 1 skipped, 0 failures** (27 nuevos tests: 16 unitarios + 11 de integración/E2E)
- **Regresión y Compatibilidad**: 100% pass en suite de seguridad N.1–N.8 (205 tests passed acumulados) y suite completa del repositorio.

---

## 2. FRONTERA ARQUITECTÓNICA Y SEPARACIÓN DE RESPONSABILIDADES

N.8 se sitúa con precisión en la arquitectura de seguridad multicapa fail-secure:

| Capa | Responsabilidad | Pregunta de Seguridad |
|---|---|---|
| **N.1 (Identity)** | Identidad canónica y tipo de actor | *¿Quién es el actor?* |
| **N.2 (Authentication)** | Verificación criptográfica de credenciales | *¿Está autenticado legítimamente?* |
| **N.4 (RBAC / Permissions)** | Roles y permisos abstractos | *¿Tiene el rol/permiso necesario?* |
| **N.3 (Authorization)** | Autorización sobre acciones y recursos del loop | *¿Tiene autorización para solicitar esta acción?* |
| **N.8 (Tool Allowlist/Denylist)** | **Gobernanza sobre herramientas/proveedores/operaciones externas** | ***¿Está permitido invocar esta herramienta física en este contexto?*** |
| **N.7 (Financial Limits)** | Controles de montos y exposición económica | *¿Está dentro de los límites financieros?* |
| **N.6 (Approval Policies)** | Supervisión y evidencia humana/operativa | *¿Requiere y cuenta con aprobación explícita?* |

### Garantías de Independencia y No Sustitución:
1. **Separación N.3 vs N.8**: La autorización de alto nivel (N.3 `ALLOW`) no presupone acceso irrestricto a herramientas externas físicas (N.8).
2. **Separación N.8 vs N.7**: Que una herramienta esté permitida en la allowlist de N.8 no omite los límites financieros de N.7.
3. **Separación N.8 vs N.6**: Que una herramienta esté permitida en la allowlist de N.8 no omite la necesidad de aprobación humana/operativa si la acción lo requiere según N.6.
4. **Frontera N.9–N.11 intacta**: N.8 no gestiona PII/sensitive data redaction interna (N.9), auditoría de cumplimiento regulatorio formal (N.10) ni paradas de emergencia globales (N.11).

---

## 3. SEMÁNTICA DE POLÍTICAS Y NORMALIZACIÓN ANTI-BYPASS

### Reglas de Precedencia Deterministas
1. **Explicit DENY Rule**: Si cualquier regla aplicable coincide y su acción es `DENY`, la herramienta es denegada inmediatamente (`EXPLICIT_DENY_RULE`).
2. **Explicit Scoped ALLOW Rule**: Si no hay regla de denegación y existe una regla `ALLOW` cuyos criterios coinciden plenamente (efecto colateral, rol, scope, cuenta, misión), la herramienta es permitida (`ALLOWED_BY_POLICY`).
3. **Default DENY**: Si ninguna regla coincide o la herramienta no está registrada, se aplica la acción por defecto fail-secure de la política (habitualmente `DENY` -> `DEFAULT_DENY_NO_RULE`).
4. **Precedencia en Colisión**: `Explicit DENY > Explicit ALLOW > Default DENY`.

### Normalización Determinista Anti-Bypass
- **Identificadores canónicos**: `tool_id`, `provider` y `operation_id` se normalizan eliminando espacios (`strip()`), transformando a minúsculas canónicas (`lower()`) y generando identificadores estructurados `provider:tool_id` o `provider:tool_id:operation_id`.
- **Efectos Colaterales Explícitos**: Control estricto del nivel de impacto (`READ_ONLY`, `ANALYSIS`, `WRITE`, `EXTERNAL_SIDE_EFFECT`, `IRREVERSIBLE`). Si una regla autoriza únicamente `READ_ONLY` y la invocación solicita `WRITE`, la evaluación deniega deterministamente el acceso.

---

## 4. MATRIZ DE COMPONENTES IMPLEMENTADOS

| Componente | Archivo | Responsabilidad |
|---|---|---|
| **Modelos de Dominio** | `src/domain/tool_policy/models.py` | `ToolAccessStatus`, `ToolAccessReasonCode`, `ToolRuleAction`, `ToolReference`, `ToolPolicyRule`, `ToolPolicy`, `ToolAccessRequest`, `ToolAccessDecision`, cálculo canónico de checksums SHA-256. |
| **Puertos de Dominio** | `src/domain/tool_policy/ports.py` | Interfaces abstractas `ToolPolicyRepositoryPort` y `ToolAccessPolicyServicePort`. |
| **Servicio de Aplicación** | `src/application/tool_policy/tool_access_policy_service.py` | Evaluación determinista, sanitización de secretos en metadatos, verificación de políticas, emisión de eventos de auditoría `TOOL_ACCESS_EVALUATED` y `TOOL_ACCESS_DENIED`. |
| **Persistencia Crash-Safe** | `src/infrastructure/persistence/data/json/tool_policy_repository.py` | Almacenamiento JSON con escrituras atómicas (`.tmp` + `fsync` + `os.replace`), `threading.RLock` y validación de integridad SHA-256. |
| **Frontera de Ejecución** | `src/application/authorization/authorization_guarded_action_executor.py` | Intercepción ordenada garantizando **0 llamadas físicas** si N.8 retorna `DENY`, `UNKNOWN` o `ERROR`. |
| **Tipos de Auditoría** | `src/domain/audit/models.py` | `AuditRecordType.TOOL_ACCESS_EVALUATED`, `AuditRecordType.TOOL_ACCESS_DENIED`. |

---

## 5. SUITE DE PRUEBAS Y VALIDACIÓN

### A. Pruebas Unitarias (`tests/unit/test_n8_tool_allowlist_denylist_unit.py`) — 16/16 PASSED
1. `test_1_tool_in_allowlist`: Herramienta explícitamente permitida en allowlist evalúa a `ALLOW`.
2. `test_2_tool_not_in_allowlist_default_deny`: Herramienta no listada evalúa a `DENY` fail-secure.
3. `test_3_tool_in_denylist_explicit_deny`: Herramienta en denylist evalúa a `DENY`.
4. `test_4_tool_in_both_allowlist_and_denylist_explicit_deny_precedence`: Precedencia `DENY > ALLOW`.
5. `test_5_normalization_anti_bypass`: Variaciones de casing y espacios normalizan canónicamente.
6. `test_6_side_effect_level_prohibition`: Bloqueo ante discordancia en nivel de efectos colaterales.
7. `test_7_scoped_allow`: Evaluación correcta según scopes válidos e inválidos.
8. `test_8_unknown_or_unregistered_tool`: Herramienta no registrada bloqueada bajo default DENY.
9. `test_9_denied_role_explicit_block`: Restricción de roles evalúa a `DENY` para roles no permitidos.
10. `test_10_missing_policy_fail_secure`: Ausencia de política evalúa a `DENY` (`UNKNOWN_TOOL_POLICY`).
11. `test_11_corrupted_policy_tampered_checksum`: Política corrupta en disco evalúa a `DENY` (`CORRUPTED_POLICY_OR_CHECKSUM_INVALID`).
12. `test_12_deterministic_decision_checksum`: Decisiones emiten checksums SHA-256 deterministas e inmutables.
13. `test_13_separation_of_duties_n3_actor_deny_not_bypassed`: N.8 no sobreescribe denegación de N.3.
14. `test_14_separation_of_duties_n8_allow_does_not_bypass_n7_financial_limits`: N.8 ALLOW no sobreescribe bloqueo de N.7.
15. `test_15_separation_of_duties_n8_allow_does_not_bypass_n6_approval_required`: N.8 ALLOW no omite requerimiento de N.6.
16. `test_16_zero_physical_execution_on_n8_deny`: Garantía estricta de 0 llamadas al ejecutor delegado en `DENY`.

### B. Pruebas de Integración y E2E (`tests/integration/test_n8_tool_allowlist_denylist_integration.py`) — 11/11 PASSED
- **Escenario A**: Herramienta autorizada en allowlist evalúa a `ALLOW` y ejecuta físicamente en el ejecutor protegido.
- **Escenario B**: Herramienta en denylist evalúa a `DENY` y garantiza 0 ejecuciones físicas.
- **Escenario C**: Herramienta no listada evalúa a `default DENY` y garantiza 0 ejecuciones físicas.
- **Escenario D**: Anti-bypass mediante normalización de identificadores y proveedores con espacios y mayúsculas.
- **Escenario E**: Bloqueo estricto por mismatch en nivel de efecto colateral (`WRITE` vs `READ_ONLY`).
- **Escenario F**: Allow condicionado por scopes válidos e inválidos (`catalog` vs `finance`).
- **Escenario G**: Restricción por rol de actor denegado (`BOT` denegado explícitamente).
- **Escenario H**: Consistencia de persistencia y reinicio validado con checksums SHA-256.
- **Escenario I**: Detección de archivo de política alterado manualmente -> bloqueo fail-secure `DENY`.
- **Escenario J**: Seguridad en trazabilidad y auditoría (cero fugas de secretos N.5, redactado seguro, sin CoT).
- **Escenario K**: Pipeline E2E multicapa completo (`Actor -> N.1 -> N.2 -> N.4 -> N.3 -> N.8 -> N.7 -> N.6 -> Exec`).

---

## 6. AUDITORÍA DE REGRESIÓN GLOBAL

- **Ejecución Total**: `python -m pytest`
- **Resultados**: **1748 passed, 1 skipped, 211 warnings, 0 failures** en 61.90s.
- **Zero Regressions**: Todos los módulos de negocio (D, E, F, G, H, I, J, K, L, M) y transversales (N.1 a N.7) continúan pasando al 100%.

---

## 7. ESTADO DEL CONTROL DE VERSIONES

- **Git Status**: Cambios listos localmente.
- **Directiva de Seguridad**: **NO commit**, **NO push** (estrictamente respetada).
