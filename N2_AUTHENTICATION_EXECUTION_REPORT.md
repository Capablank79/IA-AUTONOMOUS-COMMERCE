# REPORTE DE EJECUCIÓN: HITO N.2 — AUTHENTICATION
**Transversal N: Security, Governance y Safety**
**AI Autonomous Commerce Framework**
**Fecha:** 2026-09-04
**Estado:** 🟢 VALIDADA

---

## 1. Resumen Ejecutivo

Se ha implementado y validado satisfactoriamente el hito **N.2 — Authentication** del Transversal N (*Security, Governance y Safety*).

La responsabilidad central de **N.2** responde estrictamente a la pregunta fundamental:
> **"¿Puede este actor demostrar de forma válida que es la identidad que declara?"**

Se mantiene la separación estricta de responsabilidades:
- **N.1 — Identity ("Quién es")**: 🟢 VALIDADA (previamente).
- **N.2 — Authentication ("Cómo demuestra quién es")**: 🟢 VALIDADA (este hito).
- **N.3 — Authorization / N.4 — RBAC ("Qué puede hacer")**: ⚪ PENDIENTE — NO implementado en N.2.
- **N.5 — Secret Management**: estado previo — NO rediseñado.

Se garantizó el principio **REUSE > EXTEND > CREATE**:
- **REUSE**: `OAuthConnection` de MercadoLibre (Hito E), `IdentityService` / `Identity` de N.1, `sanitize_security_data` / `deep_freeze` / `validate_safe_identifier` de K.8, `AuditRecord` / `AuditActor` de K.1, `AgentTraceService` de K.2, `ClockPort` / `VirtualClock` de K.7.
- **EXTEND mínimo**: un único valor nuevo `AuditRecordType.AUTHENTICATION_EVALUATED` en K.1 para tipar eventos de autenticación sin romper la taxonomía existente.
- **CREATE**: modelos canónicos N.2 (`AuthenticationMethod`, `AuthenticationStatus`, `AuthenticationRequest`, `AuthenticationResult`, `PrincipalContext`) y `AuthenticationService`.

---

## 2. Matriz de Discovery y Reuso de Arquitectura

| Capacidad | Ubicación en Repo | Propósito Actual | Estrategia N.2 |
|---|---|---|---|
| **OAuthConnection** | `src/domain/oauth/models.py` (Hito E) | Conexión con proveedores de Marketplace (MercadoLibre) con access/refresh token y expiración | **REUSE**: `authenticate_oauth_connection(...)` valida presencia, expiración y subject sobre el contrato OAuth existente. Sin OAuth paralelo. Sin callback/PKCE duplicado (no existen capacidades dedicadas en el repo). |
| **Identity / IdentityService** | `src/domain/identity/`, `src/application/identity/` (N.1) | Identidad canónica estable por provider+subject | **REUSE**: autenticación exitosa resuelve/registra identidad N.1 idempotente (`register_oauth_user`, `register_system_agent`). Token distinto para el mismo subject => misma `identity_id`. Token nunca se convierte en identidad. |
| **Security Sanitization (K.8)** | `src/domain/security/models.py` | `sanitize_security_data`, `deep_freeze`, `validate_safe_identifier` | **REUSE**: toda metadata de requests/results se sanitiza recursivamente y se congela; cero secretos en results, trazas, auditoría o disco. |
| **Audit Trail (K.1)** | `src/domain/audit/`, `src/application/audit/` | Registro append-only determinista de eventos | **REUSE / EXTEND mínimo**: `AuditRecordType.AUTHENTICATION_EVALUATED` agregado; registros con actor, método, proveedor, estado y checksum sin tokens. |
| **Agent Trace (K.2)** | `src/domain/agent_trace/`, `src/application/agent_trace/` | Pasos operacionales de ejecución | **REUSE**: `record_step(...)` con `StepType.TOOL_CALL` y metadata sanitizada para cada evaluación de autenticación. |
| **ClockPort / VirtualClock (K.7)** | `src/domain/reliability/ports.py`, `src/infrastructure/reliability/` | Reloj determinista | **REUSE**: evaluación de expiración independiente del reloj del sistema en tests. |

---

## 3. Componentes Implementados

### 3.1 Dominio Canónico (`src/domain/authentication/models.py`)
- **`AuthenticationMethod`**: Métodos canónicos basados en capacidades reales del repo (`OAUTH2`, `BEARER_TOKEN`, `API_CREDENTIAL`, `INTERNAL_SERVICE`, `SYSTEM_ASSERTION`, `UNKNOWN`).
- **`AuthenticationStatus`**: Estados obligatorios (`AUTHENTICATED`, `UNAUTHENTICATED`, `EXPIRED`, `INVALID`, `UNKNOWN`, `ERROR`).
- **`AuthenticationRequest`**: Solicitud inmutable (`frozen=True`) con método, proveedor, credencial efímera (`token_or_secret` con `repr=False`), subject declarado, `correlation_id` y metadata sanitizada/congelada. Valida identificadores contra path traversal.
- **`AuthenticationResult`**: Resultado explícito, inmutable y determinista:
  - `status`, `method`, `provider`, `principal` (IdentityReference N.1), `authenticated_at`, `expires_at`, `reason_codes`, `correlation_id`, `metadata` sanitizada, `checksum`.
  - `checksum` SHA-256 determinista calculado sobre campos canónicos sanitizados (`compute_auth_result_checksum`).
  - Regla estricta: un resultado `AUTHENTICATED` requiere `principal` válido y `authenticated_at`.
  - Propiedades seguras: `is_authenticated`, `identity_id`. **Cero atributos de token/secreto.**
- **`PrincipalContext`**: Contexto seguro downstream que expone `principal`, `identity_id`, `provider`, `method`, `expires_at` e `is_authenticated` **sin exponer token ni credencial**.

### 3.2 Capa de Aplicación (`src/application/authentication/authentication_service.py`)
- **`AuthenticationService`**:
  - `authenticate_oauth_connection(connection, correlation_id, metadata)`:
    - Token ausente => `INVALID` / `MISSING_ACCESS_TOKEN`.
    - Token expirado => `EXPIRED` / `TOKEN_EXPIRED` (vía `ClockPort`).
    - `user_id` inválido => `INVALID` / `INVALID_USER_ID`.
    - Resuelve identidad N.1 idempotente (`register_oauth_user`) por provider+subject.
    - Éxito => `AUTHENTICATED` / `AUTHENTICATION_SUCCESS` con principal N.1.
  - `authenticate_request(request)`:
    - Método `UNKNOWN` => `UNKNOWN` (nunca promovido a `AUTHENTICATED`).
    - `INTERNAL_SERVICE` / `SYSTEM_ASSERTION`: requiere contrato reconocido explícito (`trusted_internal_tokens`). "Es interno" NO implica autenticado: sin credencial => `UNAUTHENTICATED`, credencial no reconocida => `INVALID`, binding de subject no coincide => `INVALID` / `DECLARED_SUBJECT_MISMATCH`. Resuelve agente N.1 (`register_system_agent`).
    - `API_CREDENTIAL` / `BEARER_TOKEN`: requiere credential mapping explícito (`trusted_api_credentials`); mismo rigour anti-fallback.
  - `create_principal_context(auth_result)`: solo construye contexto si `AUTHENTICATED`; de lo contrario `ValueError`.
  - `_record_audit_and_trace(...)`: registra K.1 (`AuditRecordType.AUTHENTICATION_EVALUATED`, append-only) y K.2 (`record_step`) con metadata sanitizada; los errores de audit/trace no abortan el resultado de autenticación (aislamiento K.7).

### 3.3 Extensión mínima (`src/domain/audit/models.py`)
- `AuditRecordType.AUTHENTICATION_EVALUATED = "AUTHENTICATION_EVALUATED"` para tipar eventos N.2 en el Audit Trail K.1 existente.

---

## 4. Fronteras Estrictas de Seguridad y Responsabilidad

- **N.2 != N.1**: poseer una identidad N.1 conocida no implica estar autenticado (test `test_identity_existence_does_not_imply_authenticated`).
- **N.2 != N.3/N.4**: un resultado `AUTHENTICATED` no otorga permisos, roles ni autorización. `PrincipalContext` no expone `permissions`, `roles`, `is_authorized` ni `allowed_actions` (test `test_authentication_does_not_grant_authorization`).
- **N.2 != N.5**: N.2 valida credenciales pero no gestiona el ciclo de vida de secretos; no persiste refresh tokens en modelos nuevos; el refresh lifecycle sigue residiendo en infraestructura OAuth existente.
- **No fallback permisivo**: `EXPIRED`, `INVALID`, `UNKNOWN` y `ERROR` nunca resuelven a `AUTHENTICATED`.
- **Token != identity**: la identidad se deriva exclusivamente de `provider + subject`, nunca del string del token.
- **Cero secretos**: ni en `AuthenticationResult`, `PrincipalContext`, audit K.1, traza K.2, ni en archivos JSON persistidos.

---

## 5. Auditoría de Arquitectura (Respuestas Formales)

1. **¿Ya existía authentication parcial?**
   *Sí, capacidad OAuth de MercadoLibre (Hito E) con tokens y expiración en infraestructura, y referencias a `user_id`/actores en K.1/K.2. No existía un módulo explícito de autenticación con resultado formal N.2.*
2. **¿Se reutilizó OAuth?**
   *Sí. `authenticate_oauth_connection` consume directamente el contrato `OAuthConnection` existente (provider, user_id, access_token, refresh_token, expires_at) sin duplicar el cliente OAuth, el refresh ni el canal de conexión.*
3. **¿N.2 duplica N.1?**
   *No. N.2 no crea un modelo de identidad: delega la resolución/registro a `IdentityService` de N.1 y consume `IdentityReference` como principal estable. La identidad canónica permanece única.*
4. **¿Token se usa como identity?**
   *No. La identidad se construye por `provider + external_subject_id`. El test `test_token_is_not_identity` verifica que el string del token nunca aparece en `identity_id` ni `canonical_identifier`, y la rotación de token para el mismo subject produce la misma `identity_id`.*
5. **¿Expired/unknown puede pasar?**
   *No. Token expirado => `EXPIRED` (no autenticado); token inválido => `INVALID`; método desconocido => `UNKNOWN`. Ninguno produce contexto autenticado.*
6. **¿Internal actor se autentica sin contrato?**
   *No. `INTERNAL_SERVICE`/`SYSTEM_ASSERTION` requieren credencial reconocida en `trusted_internal_tokens` y binding de subject consistente. Sin contrato => `UNAUTHENTICATED`/`INVALID`. Test `test_scenario_h_internal_agent_auth_path` cubre H2 (sin contrato) y H3 (contrato falso).*
7. **¿Secrets aparecen en results/logs?**
   *No. Metadata pasa por `sanitize_security_data` (K.8) con redacción recursiva; audit/trace solo llevan método, proveedor, estado, reason codes y checksum. Tests `test_secret_sanitization_in_metadata`, `test_no_token_in_result_attributes_or_repr`, `test_scenario_f_audit_trace_records_safe` y `test_scenario_g_no_secrets_persisted` lo verifican.*
8. **¿N.3/N.4 se implementaron accidentalmente?**
   *No. `AuthenticationService` no evalúa permisos, no invoca `PolicyEngine`, no consulta roles/RBAC ni expone primitivas de autorización al downstream.*
9. **¿Audit/Trace son seguros?**
   *Sí. Registros append-only en K.1 con tipo nuevo `AUTHENTICATION_EVALUATED` (una creación por evaluación, sin duplicación en replay gracias a la idempotencia existente) y pasos sanitizados en K.2, ambos verificados contra filtración de secretos.*
10. **¿OAuth paralelo fue creado?**
    *No. No se creó cliente OAuth, callback, state/nonce/PKCE ni flujo de refresh alternativo. N.2 solo valida el contrato de conexión existente.*

---

## 6. Resultados de Validación y Cobertura de Tests

### 6.1 Pruebas Unitarias (`tests/unit/test_n2_authentication_unit.py`) — 16/16 passed

| # | Test | Requisito N.2 cubierto |
|---|---|---|
| 1 | `test_valid_oauth_authentication` | Autenticación válida OAuth MercadoLibre -> N.1 -> AUTHENTICATED |
| 2 | `test_valid_internal_service_authentication` | Autenticación válida de servicio interno con contrato |
| 3 | `test_invalid_internal_credentials` | Credencial inválida => INVALID |
| 4 | `test_missing_access_token_in_oauth` | Token ausente => INVALID |
| 5 | `test_expired_oauth_token` | Token expirado => EXPIRED / no autenticado |
| 6 | `test_unknown_method_preserves_unknown_status` | UNKNOWN preservado (sin fallback permisivo) |
| 7 | `test_same_subject_different_tokens_yield_same_identity` | Mismo subject => misma identity (estabilidad N.1) |
| 8 | `test_token_is_not_identity` | Token nunca es identidad |
| 9 | `test_declared_subject_mismatch` | Binding / mismatch de subject => INVALID |
| 10 | `test_method_validation_and_invalid_values` | Validación de métodos/estados enum |
| 11 | `test_secret_sanitization_in_metadata` | Sanitización recursiva de secretos |
| 12 | `test_no_token_in_result_attributes_or_repr` | Cero tokens en resultados/representaciones |
| 13 | `test_identity_existence_does_not_imply_authenticated` | Identity != authenticated |
| 14 | `test_authentication_does_not_grant_authorization` | Authentication != authorization |
| 15 | `test_replay_and_idempotence` | Replay/state safety e idempotencia |
| 16 | `test_deterministic_checksum_and_immutability` | Semántica determinista (checksum) e inmutabilidad |

### 6.2 Pruebas de Integración y E2E (`tests/integration/test_n2_authentication_integration.py`) — 10/10 passed

- **A.** `test_scenario_a_valid_oauth_authenticated`: OAuth válido -> N.1 -> AUTHENTICATED + PrincipalContext sin tokens.
- **B.** `test_scenario_b_expired_token_rejected`: expirado -> rechazado, sin contexto autenticado.
- **C.** `test_scenario_c_invalid_provider_and_subject`: token ausente, user_id inválido, credencial falsa => INVALID/UNAUTHENTICATED.
- **D.** `test_scenario_d_token_rotation_same_identity`: rotación de token => misma identidad estable.
- **E.** `test_scenario_e_restart_persists_identity_and_reevaluates`: reinicio => identidad N.1 persiste; resultado de auth re-evaluado (rotado => AUTHENTICATED, expirado => EXPIRED).
- **F.** `test_scenario_f_audit_trace_records_safe`: K.1/K.2 registran sin tokens ni secretos.
- **G.** `test_scenario_g_no_secrets_persisted`: cero secretos en archivos JSON (disco).
- **H.** `test_scenario_h_internal_agent_auth_path`: actores internos con contrato explícito (éxito, sin contrato => UNAUTHENTICATED, contrato falso => INVALID, mismatch => INVALID).
- **I.1.** `test_scenario_e2e_authenticated_reaches_operation_boundary`: actor autenticado llega al boundary de operación (`EXECUTE_AS_usr_...|PROVIDER_...|METHOD_...`).
- **I.2.** `test_scenario_e2e_unauthenticated_explicitly_identified`: actor no autenticado queda explícitamente identificado (EXPIRED) y es rechazado.

### 6.3 Regresión

- **Regresión relevante (N.1 Identity, K.1 Audit, K.2 Agent Trace, K.8 Security, OAuth/MercadoLibre)**: **122 passed, 0 failures**.
- **Suite completa del repositorio**: **1593 passed, 1 skipped, 0 failures, 0 errors**.
  - Baseline: **1567 passed, 1 skipped, 0 failures**.
  - Incremento limpio: **+26 tests** (16 unit N.2 + 10 integración/E2E N.2).

---

## 7. Verificación de Higiene de Git

- `git ls-files .pytest_tmp`: Limpio (sin archivos temporales trackeados).
- `git diff --check`: Limpio (sin trailing whitespaces ni conflictos).
- `git diff --stat`: Solo `src/domain/audit/models.py` (EXTEND mínimo) y Gantt (estado).
- `git status --short`: Archivos nuevos N.2 sin staging (correcto).
- **NO commit / NO push realizado**, conforme a las reglas operativas.

---

## 8. Próximo Paso

- **N.1 — Identity**: 🟢 VALIDADA
- **N.2 — Authentication**: 🟢 VALIDADA
- **N.3 — Authorization**: ⚪ PENDIENTE (NO implementar)
- **N.4–N.11**: estados previos (NO tocar)
- **N.5 — Secret Management**: estado previo (NO rediseñar)
- **Gate M**: ⚪ PENDIENTE (NO cerrar)
- **Hito N (Transversal Security, Governance y Safety)**: 🟡 EN PROGRESO
- **Siguiente Tarea**: **N.3 — Authorization** (No implementar hasta instrucción explícita del usuario).
