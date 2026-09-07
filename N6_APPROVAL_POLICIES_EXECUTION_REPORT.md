# N.6 — Approval Policies Execution Report

## 1. Executive Summary
El Hito **N.6 — Approval Policies** ha sido completamente implementado y validado dentro del **Transversal N — Security, Governance y Safety** de la plataforma autónoma de comercio.

N.6 responde a la pregunta central de gobernanza:
> **"¿Esta acción requiere aprobación explícita antes de ejecutarse y, si la requiere, existe una aprobación válida?"**

Se ha aplicado de manera rigurosa el principio **REUSE > EXTEND > CREATE**, conectando la evaluación de políticas de aprobación en la frontera de ejecución (`AuthorizationGuardedActionExecutor`) inmediatamente después del pase de autorización N.3 (`ALLOW`), garantizando **0 efectos secundarios ni llamadas externas físicas** cuando una acción requiere aprobación y no cuenta con una evidencia válida (`ApprovalEvidence`).

---

## 2. Discovery Matrix

| Capability | Location | Current Purpose | Action (Reuse / Extend / Create) |
|---|---|---|---|
| **Identity Taxonomy & Port** | `src/domain/identity/` | Provee identidades canónicas inmutables (`PrincipalIdentity`, `IdentityReference`) | **REUSE** sin alterar contratos |
| **Authentication Context** | `src/domain/authentication/` | Valida credenciales e inyecta `PrincipalContext` | **REUSE** para verificar estado de autenticación del aprobador |
| **Authorization Precondition** | `src/domain/authorization/` | Evalúa si el actor tiene capacidad de ejecutar la acción | **REUSE** (N.3 ALLOW como precondición estricta) |
| **RBAC / Permissions** | `src/domain/rbac/` | Asigna y resuelve permisos canónicos | **REUSE** como soporte de capacidades |
| **Secret Management** | `src/domain/secrets/` | Wrapper seguro de material confidencial | **REUSE** para aislamiento de credenciales |
| **Execution Guard** | `src/application/authorization/authorization_guarded_action_executor.py` | Intercepta llamadas físicas antes de su ejecución externa | **EXTEND** incorporando `ApprovalPolicyService` tras N.3 ALLOW |
| **Domain Models N.6** | `src/domain/approval/models.py` | Modelos inmutables de políticas, solicitudes, decisiones y evidencias | **CREATE** |
| **Repository Ports N.6** | `src/domain/approval/ports.py` | Interfaces para persistencia de políticas y evidencias | **CREATE** |
| **Approval Service** | `src/application/approval/approval_policy_service.py` | Servicio de orquestación, evaluación, emisión y rechazo de aprobaciones | **CREATE** |
| **Persistencia Crash-Safe** | `src/infrastructure/persistence/data/json/approval_evidence_repository.py` | Repositorio JSON con atomicidad, checksums SHA-256 y locks | **CREATE** |
| **Audit Types** | `src/domain/audit/models.py` | Enumeración de eventos de auditoría | **EXTEND** (`APPROVAL_EVALUATED`, `APPROVAL_GRANTED`, `APPROVAL_REJECTED`) |

---

## 3. Arquitectura y Distinción N.3 vs N.6

```
                  ┌────────────────────────────────────────────────────────┐
                  │                   PRINCIPAL / ACTOR                    │
                  └──────────────────────────┬─────────────────────────────┘
                                             │
                                             ▼
                             [N.1 Identity + N.2 Authentication]
                                             │
                                             ▼
                                  [N.4 RBAC Permissions]
                                             │
                                             ▼
                                   [N.3 Authorization]
                                             │
                       ┌─────────────────────┴─────────────────────┐
                       │ (DENY / UNKNOWN / ERROR)                  │ (ALLOW)
                       ▼                                           ▼
              ┌─────────────────┐                     ┌─────────────────────────┐
              │  BLOCKED AT N.3 │                     │   N.6 Approval Policy   │
              │ (0 Side Effects)│                     └────────────┬────────────┘
              └─────────────────┘                                  │
                                       ┌───────────────────────────┴───────────────────────────┐
                                       │ (NOT_REQUIRED / APPROVED)                             │ (APPROVAL_REQUIRED /
                                       ▼                                                       │  REJECTED / EXPIRED /
                         ┌───────────────────────────┐                                         │  UNKNOWN / ERROR)
                         │ Physical Execution Guard  │                                         ▼
                         │ (ActionExecutor Delegate) │                                ┌─────────────────┐
                         └───────────────────────────┘                                │  BLOCKED AT N.6 │
                                                                                      │ (0 Side Effects)│
                                                                                      └─────────────────┘
```

### Principios Fundamentales:
1. **N.3 Authorization vs N.6 Approval**:
   - **N.3**: *"¿Este actor tiene permiso para este tipo de acción?"* (Ej. Operador autorizado para publicar listings).
   - **N.6**: *"¿Esta ejecución concreta de alto impacto requiere aprobación adicional?"* (Ej. Publicación comercial requiere confirmación explícita).
   - `Authorization ALLOW != Approval satisfied`.
2. **Precedencia Absoluta de N.3**:
   - Una aprobación de N.6 **jamás puede sobreescribir** una decisión `DENY` de N.3.
3. **Approval Binding Criptográfico**:
   - Las evidencias están ligadas inmutablemente a `action + resource + requester + context + policy_name + policy_version`. Una aprobación emitida para el recurso A no es válida para el recurso B (*cross-resource replay prevention*).
4. **Separación de Funciones (Anti-Self-Approval)**:
   - Bloqueo determinista si `requesting_identity_id == approver_identity_id` en políticas configuradas con `require_separation_of_duties=True`.
5. **Determinismo Temporal & Integridad**:
   - Control de expiración gobernado por `ClockPort` (K.7).
   - Checksums canónicos SHA-256 (`compute_approval_checksum`).
   - Persistencia JSON atómica (`.tmp` + `fsync` + `os.replace`) con detección y rechazo de registros alterados (`ApprovalStatus.ERROR` / `APPROVAL_EVIDENCE_CORRUPTED`).

---

## 4. Auditoría de Fronteras Arquitectónicas (Architecture Audit)

- **¿Existía lógica de aprobación previa en el sistema?**
  - Sí, existían referencias aisladas y flujos en Gate F; se consolidó formalmente un modelo de dominio canónico inmutable sin duplicaciones.
- **¿Se reutilizó PolicyEngine?**
  - Sí, `AuthorizationService` reutiliza `PolicyEngine` (Hito E.3) para la fase N.3. N.6 se integra composicionalmente sin crear un segundo PolicyEngine redundante.
- **¿N.6 duplica a N.3?**
  - No. N.3 evalúa capacidades del rol/actor. N.6 evalúa si la instancia concreta de ejecución requiere autorización contextual de segundo factor y valida su evidencia.
- **¿Una aprobación puede sobreescribir un DENY de N.3?**
  - **No**. La evaluación en `AuthorizationGuardedActionExecutor` evalúa N.3 primero; si el resultado no es `ALLOW`, N.6 ni siquiera habilita la ejecución física.
- **¿Una política no encontrada (missing policy) puede auto-aprobar?**
  - **No**. Ante políticas no registradas o fallos de evaluación, el sistema resuelve `ApprovalStatus.UNKNOWN` o `ApprovalStatus.ERROR`, resultando en bloqueo de ejecución.
- **¿La aprobación está ligada a acción y recurso?**
  - **Sí**, de forma estricta e inmutable en `ApprovalEvidence` y validada campo a campo.
- **¿La auto-aprobación está controlada?**
  - **Sí**, bloqueada por defecto si la política exige separación de funciones.
- **¿Evidencias expiradas o corruptas pueden ser aprobadas?**
  - **No**. Evidencias expiradas retornan `ApprovalStatus.EXPIRED` y registros alterados retornan `ApprovalStatus.ERROR` con código `APPROVAL_EVIDENCE_CORRUPTED`.
- **¿El guard de ejecución bloquea correctamente?**
  - **Sí**. Cualquier estado distinto a `NOT_REQUIRED` o `APPROVED` produce exactamente 0 invocaciones al ejecutor delegado.
- **¿Se introdujeron límites numéricos o financieros de N.7?**
  - **No**. Se auditó explícitamente que no se añadieran atributos como `max_amount`, `daily_limit`, `transaction_limit` ni reglas de presupuesto.

---

## 5. Matriz de Pruebas y Resultados de Validación

### Pruebas Unitarias (`tests/unit/test_n6_approval_policies_unit.py`) — 16/16 Passed
1. `test_1_action_not_requiring_approval_returns_not_required` 🟢
2. `test_2_action_requiring_approval_returns_approval_required` 🟢
3. `test_3_valid_approval_returns_approved` 🟢
4. `test_4_explicit_rejection_returns_rejected` 🟢
5. `test_5_expired_approval_returns_expired` 🟢
6. `test_6_unknown_policy_returns_unknown` 🟢
7. `test_7_approval_bound_to_action` 🟢
8. `test_8_approval_bound_to_resource` 🟢
9. `test_9_wrong_requester_binding_rejected` 🟢
10. `test_10_self_approval_blocked_when_separation_required` 🟢
11. `test_11_deterministic_decision` 🟢
12. `test_12_policy_versioning_mismatch_rejected` 🟢
13. `test_13_corrupt_evidence_checksum_rejected` 🟢
14. `test_14_approval_cannot_override_n3_deny` 🟢
15. `test_15_sanitized_metadata` 🟢
16. `test_16_no_n7_financial_thresholds_in_n6` 🟢

### Pruebas de Integración y E2E (`tests/integration/test_n6_approval_policies_integration.py`) — 11/11 Passed
- **Escenario A**: N.1→N.2→N.4→N.3 ALLOW + acción NOT_REQUIRED → Ejecución física llamada 1 vez. 🟢
- **Escenario B**: N.3 ALLOW + APPROVAL_REQUIRED sin evidencia → Cero llamadas físicas. 🟢
- **Escenario C**: Evidencia de aprobación válida y vinculada → APPROVED → 1 llamada física. 🟢
- **Escenario D**: Aprobación para recurso diferente → Cero llamadas físicas. 🟢
- **Escenario E**: Aprobación expirada temporalmente → Cero llamadas físicas. 🟢
- **Escenario F**: Intento de auto-aprobación donde está prohibido → Cero llamadas físicas. 🟢
- **Escenario G**: N.3 DENY con aprobación válida adjunta → Cero llamadas físicas. 🟢
- **Escenario H**: Reinicio de repositorio persistente → Evidencias válidas continúan verificables. 🟢
- **Escenario I**: Registro alterado físicamente en disco → Corrupción detectada vía SHA-256 → Bloqueo. 🟢
- **Escenario J**: Auditoría K.1 y trazas K.2 seguras con sanitización K.8 profunda (cero secretos). 🟢
- **Escenario K**: Flujo E2E completo cruzando toda la cadena N.1 → N.2 → N.4 → N.3 → N.6. 🟢

### Regresión Global del Repositorio
- **Total Tests Ejecutados:** **1694 passed, 1 skipped, 0 failures, 0 errors** (incremento neto de +27 tests sobre el baseline de 1667).
- **Higiene:** Cero runtime artifacts o temporales trackeados; `git diff --check` limpio.

---

## 6. Estado del Hito y Próximos Pasos

- **N.1 Identity:** 🟢 VALIDADA
- **N.2 Authentication:** 🟢 VALIDADA
- **N.3 Authorization:** 🟢 VALIDADA
- **N.4 RBAC / Permissions:** 🟢 VALIDADA
- **N.5 Secret Management:** 🟢 VALIDADA
- **N.6 Approval Policies:** 🟢 VALIDADA
- **N.7 Financial Limits:** ⚪ PENDIENTE
- **N.8–N.11:** ⚪ PENDIENTE
- **Gate M:** ⚪ PENDIENTE
- **Hito N:** 🟡 EN PROGRESO

**Siguiente Tarea:** N.7 — Financial Limits (sin implementar en esta iteración).
