# REPORTE DE EJECUCIÓN: HITO N.1 — IDENTITY
**Transversal N: Security, Governance y Safety**
**AI Autonomous Commerce Framework**
**Fecha:** 2026-09-04
**Estado:** 🟢 VALIDADA

---

## 1. Resumen Ejecutivo

Se ha implementado y validado satisfactoriamente el hito **N.1 — Identity** del Transversal N (*Security, Governance y Safety*).

La responsabilidad central de **N.1** responde estrictamente a la pregunta fundamental:
> **"¿Quién o qué actor está realizando esta operación dentro del sistema?"**

Se garantiza el principio de diseño **REUSE > EXTEND > CREATE**, integrando de manera no invasiva los modelos existentes de auditoría (`AuditActor` de K.1), observabilidad (`AgentTraceRecord` de K.2), seguridad base (sanitización recursiva y validación de identificadores seguros de K.8) e identidades OAuth de proveedores de marketplace (Hito E / MercadoLibre).

---

## 2. Fronteras Estrictas de Seguridad y Responsabilidad

El diseño de N.1 mantiene una separación estricta de responsabilidades según los principios de seguridad del sistema:
- **N.1 — Identity ("Quién es")**: Modela, registra y resuelve identidades canónicas inmutables de actores reales del sistema (`USER`, `AGENT`, `SYSTEM`, `SERVICE`, `SCHEDULER`, `MARKETPLACE`, `EXTERNAL_TOOL`, `UNKNOWN`).
- **N.2 — Authentication ("Cómo demuestra quién es")**: NO implementado en N.1. Queda reservado para la verificación criptográfica/credencial en N.2.
- **N.3 / N.4 — Authorization / RBAC ("Qué puede hacer")**: NO implementado en N.1. Poseer una identidad válida **no otorga ningún permiso de ejecución**.
- **N.5 — Secret Management / Credenciales**: En N.1 **no se almacena ningún secreto, password, API key, access token ni refresh token**. Los tokens efímeros y las sesiones volátiles están explícitamente desvinculados de la identidad canónica.

---

## 3. Matriz de Discovery y Reuso de Arquitectura

| Capacidad | Ubicación en Repo | Propósito Actual | Estrategia N.1 |
|---|---|---|---|
| **AuditActor** | `src/domain/audit/models.py` (K.1) | Identificación de actores en eventos de auditoría (`system`, `agent`, `user`, etc.) | **REUSE / EXTEND**: Mapeo bidireccional entre `AuditActor` y `IdentityReference` sin alterar la taxonomía de K.1. |
| **AgentTraceRecord** | `src/domain/agent_trace/models.py` (K.2) | Trazabilidad de ejecución de agentes y correlación de misiones | **REUSE / EXTEND**: Vinculación de `identity_id` en contexto de traza y resolución de identidad de agentes. |
| **Security Sanitization** | `src/domain/security/models.py` (K.8) | Sanitización recursiva de secretos y validación de path traversal | **REUSE**: `sanitize_security_data`, `deep_freeze` y `validate_safe_identifier` aplicados a todos los metadatos y paths de identidad. |
| **OAuthConnection** | `src/domain/oauth/models.py` (Hito E) | Conexión con proveedores de Marketplace (MercadoLibre) | **REUSE / EXTEND**: Extracción del sujeto externo estable `user_id` sin tocar credenciales ni tokens de autenticación. |

---

## 4. Componentes Implementados

### 4.1 Dominio Canónico (`src/domain/identity/models.py`)
- **`IdentityType`**: Taxonomía de actores reales:
  - `USER`: Usuarios humanos del sistema.
  - `AGENT`: Agentes autónomos de ejecución (`AutonomousLoop`, `PricingAgent`, etc.).
  - `SYSTEM`: Subsistemas internos centrales.
  - `SERVICE`: Microservicios / servicios auxiliares.
  - `SCHEDULER`: Planificadores y disparadores temporales.
  - `MARKETPLACE`: Plataformas externas (MercadoLibre, Amazon, etc.).
  - `EXTERNAL_TOOL`: Herramientas e integraciones externas.
  - `UNKNOWN`: Actores no reconocidos o sin resolver (preservado explícitamente sin falsificación).
- **`Identity`**: Entidad inmutable (`frozen=True`) con campos:
  - `identity_id`: Identificador interno único y seguro (ej. `usr_mercadolibre_123456`, `agt_autonomous_loop`).
  - `identity_type`: Tipo de actor (`IdentityType`).
  - `canonical_identifier`: Identificador determinista `f"{type}:{provider}:{subject}"`.
  - `display_name`: Nombre canónico/legible opcional.
  - `provider`: Proveedor o subsistema de origen (ej. `mercadolibre`, `internal`).
  - `external_subject_id`: Identificador externo estable provisto por terceros.
  - `status`: Estado de ciclo de vida (`ACTIVE`, `SUSPENDED`, `DEPRECATED`, `UNKNOWN`).
  - `schema_version`: Versión de esquema (`1.0.0`).
  - `checksum`: Hash criptográfico SHA-256 de los campos canónicos inmutables.
  - `metadata`: Diccionario inmutable (`MappingProxyType`) con sanitización profunda recursiva.
- **`IdentityReference` / `PrincipalIdentity`**: Estructuras inmutables livianas para correlación en logs, trazas y auditorías.
- **Mappers e Integraciones**:
  - `identity_from_oauth_connection(conn)`: Mapea conexiones OAuth sin credenciales.
  - `identity_ref_from_audit_actor(actor)` / `audit_actor_from_identity(identity)`: Interoperabilidad con K.1.
  - `identity_ref_from_trace(trace)`: Interoperabilidad con K.2.

### 4.2 Puertos y Repositorio (`src/domain/identity/ports.py` y `src/infrastructure/persistence/data/json/identity_repository.py`)
- **`IdentityRepositoryPort`**: Define contratos para registro, obtención, búsqueda por sujeto externo/identificador canónico y listado.
- **`JsonIdentityRepository`**:
  - **Atomicidad y Crash-Safety**: Escritura en archivo temporal (`.tmp`), `fsync` a nivel de sistema operativo y reemplazo atómico (`os.replace`).
  - **Exclusión Mutua**: Control de concurrencia thread-safe mediante `threading.RLock`.
  - **Seguridad de Path Traversal**: Validación estricta con `validate_safe_identifier`.
  - **Detección de Corrupción Física y Adulteración**: Verificación de checksum SHA-256 al cargar desde disco. Lanza `IdentityCorruptionError` si un archivo es modificado o adulterado externamente.
  - **Detección de Conflictos Semánticos**:
    - Idempotencia estricta: re-registro de la misma identidad con el mismo contenido es idempotente.
    - Rechazo de colisiones: conflicto si se intenta reutilizar un `canonical_identifier` bajo un `identity_id` diferente (`IdentityCanonicalConflictError`).
    - Rechazo de mutaciones incompatibles en re-registro (`IdentityConflictError`).
  - **Reconstrucción Automática de Índices**: Reconstrucción durable del índice `identities_index.jsonl` ante reinicios o recuperación de fallos.

### 4.3 Capa de Aplicación (`src/application/identity/identity_service.py`)
- **`IdentityService`**:
  - `register_identity(...)`: Registro canónico y validación.
  - `register_oauth_user(...)`: Registro especializado de usuarios externos desde proveedores.
  - `register_system_agent(...)`: Registro determinista de agentes del sistema.
  - `resolve_identity(raw_reference)`: Resolución polimórfica que acepta strings, `AuditActor`, `AgentTraceRecord` o `None`, resolviendo a `IdentityReference` (o `UNKNOWN` si no existe coincidencia, sin fabricar identidades falsas).

---

## 5. Auditoría de Arquitectura (Respuestas Formales)

1. **¿Ya existía identity/principal parcial?**
   *Sí, existían representaciones parciales como `AuditActor` (K.1), `user_id` en `OAuthConnection` (Hito E) y referencias en trazas de agentes (K.2).*
2. **¿Se reutilizó?**
   *Sí, se implementaron adaptadores y mappers bidireccionales (`identity_ref_from_audit_actor`, `audit_actor_from_identity`, `identity_from_oauth_connection`, `identity_ref_from_trace`) sin duplicar ni destruir la arquitectura existente.*
3. **¿Identity duplica AuditActor?**
   *No. `AuditActor` es una estructura de evento para K.1, mientras que `Identity` es una entidad de dominio con ciclo de vida, persistencia determinista, validación de integridad SHA-256 y soporte para múltiples tipos de actores del sistema.*
4. **¿Identity depende de token/session?**
   *No. La identidad canónica se construye exclusivamente a partir del proveedor y el sujeto externo/interno estable (`provider + external_subject_id`). Es 100% independiente de access tokens, refresh tokens o session IDs efímeros.*
5. **¿Agent identity cambia por ejecución?**
   *No. La identidad del agente es canónica y estable (`agt_autonomous_loop`, `agt_pricing_engine`). Cada ejecución se correlaciona mediante trazas (`trace_id`, `run_id` en K.2), sin mutar la identidad del agente.*
6. **¿External subject mapea de forma estable?**
   *Sí. Un sujeto externo como MercadoLibre `MLA_12345` mapea siempre al identificador canónico `USER:mercadolibre:MLA_12345` y `identity_id="usr_mercadolibre_mla_12345"`.*
7. **¿Secrets pueden persistirse?**
   *No. Todos los metadatos pasan por `sanitize_security_data` de K.8 y se congelan inmutablemente. Campos sensibles como `password`, `token`, `secret`, `api_key` o `private prompt/CoT` son eliminados/redactados antes de persistirse.*
8. **¿N.2/N.3/N.4 fueron implementadas accidentalmente?**
   *No. N.1 no valida contraseñas, no emite tokens JWT/sesión (N.2), no evalúa políticas de acceso (N.3) ni asigna roles o permisos RBAC (N.4).*
9. **¿Restart preserva identity?**
   *Sí. La persistencia JSON en disco con índices reconstruibles permite que tras reiniciar el servicio o instanciar un nuevo repositorio sobre el mismo directorio, todas las identidades se recuperen con verificación íntegra.*
10. **¿Corruption puede producir identity falsa?**
    *No. Cualquier adulteración de bytes o metadatos en disco invalida el checksum SHA-256 y dispara `IdentityCorruptionError`, impidiendo que el sistema acepte o confíe en una identidad corrompida.*

---

## 6. Resultados de Validación y Cobertura de Tests

### 6.1 Pruebas Unitarias (`tests/unit/test_n1_identity_unit.py`)
- `test_immutable_identity_creation`: Inmutabilidad de entidad y mapeo seguro de metadatos.
- `test_identity_types_taxonomy`: Cobertura exhaustiva de los 8 tipos de actores canónicos.
- `test_deterministic_identity_id_and_canonical_identifier`: Verificación de consistencia y determinismo.
- `test_user_identity_creation_and_attributes`: Registro de identidad tipo USER.
- `test_agent_identity_creation_and_attributes`: Registro de identidad tipo AGENT.
- `test_system_and_service_identity_creation`: Registro de actores SYSTEM y SERVICE.
- `test_external_provider_subject_mapping`: Mapeo de sujetos de marketplaces externos.
- `test_unknown_identity_preserved`: Preservación estricta de UNKNOWN sin fallar abierto ni inventar datos.
- `test_token_not_part_of_identity`: Aislamiento entre tokens de autenticación e identidad canónica.
- `test_secret_and_sensitive_metadata_sanitization`: Sanitización recursiva de claves privadas, passwords y CoT.
- `test_safe_identifier_validation_blocks_path_traversal`: Bloqueo de ataques de path traversal (`../`, `/`, `\`).
- `test_checksum_integrity_verification`: Verificación criptográfica SHA-256 de campos canónicos.
- `test_identity_does_not_grant_authentication`: Demostración formal de que Identity != Autenticado (N.2).
- `test_identity_does_not_grant_authorization_or_permissions`: Demostración formal de que Identity != Autorizado/Permitido (N.3/N.4).
**Resultado: 14/14 passed (100%)**

### 6.2 Pruebas de Integración y E2E (`tests/integration/test_n1_identity_integration.py`)
- `test_scenario_a_register_internal_agent_retrieve_stable_identity`: Registro y recuperación de agente interno.
- `test_scenario_b_register_mercadolibre_oauth_subject_stable_mapping`: Mapeo estable de cuenta externa MercadoLibre.
- `test_scenario_c_restart_preserves_identities_and_index`: Durabilidad tras reinicio y reconstrucción de índices.
- `test_scenario_d_same_external_subject_replay_idempotent`: Idempotencia en re-registro de sujetos.
- `test_scenario_e_different_external_subject_different_identities`: Aislamiento entre diferentes sujetos externos.
- `test_scenario_f_audit_record_linked_to_identity`: Vinculación interoperable con `AuditRecord` (K.1).
- `test_scenario_g_agent_trace_linked_to_identity`: Vinculación interoperable con `AgentTraceRecord` (K.2).
- `test_scenario_h_tampered_persisted_identity_corruption_detected`: Detección inmediata de corrupción física y adulteración externa.
- `test_scenario_i_canonical_conflict_detected_and_rejected`: Rechazo de colisiones en identificadores canónicos.
- `test_scenario_j_e2e_commercial_context_identity_resolution`: Flujo E2E de resolución de identidad en contexto comercial (agente, usuario ML, desconocido).
**Resultado: 10/10 passed (100%)**

### 6.3 Regresión Global
- **Regresión módulos vinculados (K.1, K.2, K.8, OAuth, N.1)**: **93 passed, 0 failures**.
- **Suite completa del repositorio**: **1567 passed, 1 skipped, 0 failures** (incremento neto de +24 tests limpios respecto al baseline de 1543).

---

## 7. Verificación de Higiene de Git

- `git ls-files .pytest_tmp`: Limpio (sin archivos temporales trackeados).
- `git diff --check`: Limpio (sin trailing whitespaces ni conflictos).
- `git status --short`: No existen archivos no autorizados en staging.
- **NO commit / NO push realizado**, conforme a las reglas operativas.

---

## 8. Próximo Paso

- **N.1 — Identity**: 🟢 **VALIDADA**
- **Transversal N — Security, Governance y Safety**: 🟡 **EN PROGRESO**
- **Gate M**: ⚪ **PENDIENTE**
- **Siguiente Tarea**: **N.2 — Authentication** (No iniciar hasta instrucción explícita del usuario).
