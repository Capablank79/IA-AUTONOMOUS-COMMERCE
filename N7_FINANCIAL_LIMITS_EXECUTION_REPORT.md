# INFORME DE EJECUCIÓN: HITO N.7 — FINANCIAL LIMITS

## 1. RESUMEN EJECUTIVO

- **Hito**: N.7 — Financial Limits
- **Transversal**: N — Security, Governance y Safety
- **Estado Previo**: ⚪ PENDIENTE
- **Estado Final**: 🟢 VALIDADA
- **Fecha de Validación**: 2026-09-07
- **Pregunta Central Resuelta**: *"¿Esta operación financiera/comercial está dentro de los límites económicos permitidos?"*
- **Baseline Previo**: 1694 passed, 1 skipped, 0 failures
- **Resultado Post-Implementación**: **1721 passed, 1 skipped, 0 failures** (27 nuevos tests: 16 unitarios + 11 de integración/E2E)
- **Regresión y Compatibilidad**: 100% pass en suite de seguridad N.1–N.7 (178 tests passed) y suite completa del repositorio.

---

## 2. FRONTERA ARQUITECTÓNICA Y DISTINCIÓN M.6 vs N.7

| Dimensión | M.6 (Inference Cost Control) | N.7 (Financial & Economic Limits) |
|---|---|---|
| **Propósito** | Gobernanza de costes computacionales e inferencia IA | Gobernanza de riesgo financiero y operaciones comerciales de negocio |
| **Objetos Controlados** | Tokens (prompt/completion), presupuestos de contexto, tarifas de modelos LLM | Precios máx/mín, cambios de precio, transacciones, reembolsos, exposición y gasto comercial |
| **Entidades de Dominio** | `InferenceBudget`, `TokenUsage`, `ModelPricing` | `FinancialLimitPolicy`, `FinancialLimitRule`, `FinancialLimitDecision`, `Money` |
| **Efecto de Bloqueo** | Truncamiento, degradación de modelo o bloqueo de inferencia | Bloqueo absoluto de ejecución física de órdenes, devoluciones o mutaciones comerciales |

**Frontera N.8–N.11**: Se verificó rigurosamente que N.7 no implemente *tool allowlists*, *tool denylists*, *sanitización de PII/compliance* ni *emergency stop*.

---

## 3. MATRIZ DE CAPACIDADES Y REUTILIZACIÓN

| Capacidad | Ubicación | Propósito / Decisión | Estrategia |
|---|---|---|---|
| **Representación Monetaria** | `src/domain/profit/models.py` | `Money(amount: Decimal, currency: str)` inmutable con validación estricta | **REUSE** (cero duplicación de tipos monetarios) |
| **Modelos de Límites** | `src/domain/financial_limit/models.py` | `FinancialLimitPolicy`, `FinancialLimitRule`, `FinancialLimitRequest`, `FinancialLimitDecision`, `FinancialLimitType`, `FinancialLimitStatus` | **CREATE** |
| **Servicio de Límites** | `src/application/financial_limit/financial_limit_service.py` | Evaluación determinista, validación multi-moneda, resolución de reglas por acción/cuenta/recurso, auditoría | **CREATE** |
| **Persistencia Crash-Safe** | `src/infrastructure/persistence/data/json/financial_limit_repository.py` | Almacenamiento JSON atómico (`.tmp` + `fsync` + `os.replace`), locking thread-safe (`RLock`), checksum SHA-256 | **CREATE** |
| **Execution Guard Pipeline** | `src/application/authorization/authorization_guarded_action_executor.py` | Intercepción de seguridad en orden canónico: `N.1 -> N.2 -> N.4 -> N.3 -> N.7 -> N.6 -> Boundary` | **EXTEND** |
| **Auditoría K.1** | `src/domain/audit/models.py` | `AuditRecordType.FINANCIAL_LIMIT_EVALUATED`, `AuditRecordType.LIMIT_EXCEEDED` | **EXTEND** |

---

## 4. MODELOS DE DOMINIO Y SEMÁNTICA FAIL-SAFE

### Estados de Decisión Canónicos (`FinancialLimitStatus`)
- `WITHIN_LIMIT`: El monto/operación está estrictamente dentro del umbral económico permitido.
- `LIMIT_EXCEEDED`: El monto excede el límite autónomo estricto (bloqueo definitivo).
- `APPROVAL_REQUIRED`: El monto excede el umbral autónomo pero la regla permite excepción supervisada vía N.6 Approval Policies.
- `REJECTED`: Monto por debajo del mínimo permitido (`MIN_ALLOWED_PRICE`) o solicitud inválida.
- `UNKNOWN`: Moneda incompatible (*currency mismatch*) o política/regla ausente (*missing policy != unlimited*).
- `ERROR`: Parámetros corruptos, datos inválidos o fallos de evaluación.

### Principios Fundamentales
1. **Precisión Decimal Absoluta**: Rechazo explícito de tipos `float`. Todas las operaciones utilizan `Decimal`.
2. **Moneda Explícita y No Coerción FX**: Sin conversión cambiaria ficticia. Monedas distintas (ej. CLP vs USD) sin tasa explícita resultan en `UNKNOWN` / Bloqueo.
3. **Fail-Safe por Defecto**: La ausencia de política o regla financiera para una acción monetaria nunca asume permisos ilimitados; evalúa a `UNKNOWN` / `REJECTED`.
4. **Integridad Criptográfica**: Policies y decisiones protegidas por checksums canónicos SHA-256 (`compute_financial_policy_checksum`, `compute_financial_decision_checksum`).

---

## 5. PIPELINE DE INTEGRACIÓN Y PRECEDENCIA DE SEGURIDAD

El flujo de control en la frontera de ejecución (`AuthorizationGuardedActionExecutor`) garantiza el orden estricto de gobierno:

```
[ Actor ]
   │
   ▼
[ N.1 Identity: Identificación Canónica ]
   │
   ▼
[ N.2 Authentication: Verificación de Credenciales ]
   │
   ▼
[ N.4 RBAC: Resolución de Permisos Granulares ]
   │
   ▼
[ N.3 Authorization: Decisión de Política (PolicyEngine) ]
   │ ──( DENY / UNKNOWN )──► BLOQUEO (0 side effects)
   │ ( ALLOW )
   ▼
[ N.7 Financial Limits: Evaluación Económica (FinancialLimitService) ]
   │ ──( LIMIT_EXCEEDED / REJECTED / UNKNOWN / ERROR )──► BLOQUEO (0 side effects)
   │ ──( APPROVAL_REQUIRED )──► Requiere Evidencia N.6
   │ ( WITHIN_LIMIT )
   ▼
[ N.6 Approval Policies: Verificación de Evidencia Humana/Supervisada ]
   │ ──( APPROVAL_REQUIRED sin evidencia / REJECTED / EXPIRED )──► BLOQUEO (0 side effects)
   │ ( NOT_REQUIRED o APPROVED con evidencia válida )
   ▼
[ Execution Boundary / Commercial Action Executor ]
```

**Reglas de Interacción Inviolables**:
- N.7 **no puede** convertir un `N.3 DENY` en ejecución válida.
- N.7 **no fabrica** evidencias de aprobación; N.6 sigue siendo la única autoridad de aprobación.
- Un monto dentro del límite financiero pero denegado por autorización N.3 resulta en **0 llamadas físicas**.

---

## 6. RESULTADOS DE PRUEBAS Y VALIDACIÓN

### A. Pruebas Unitarias (`tests/unit/test_n7_financial_limits_unit.py`)
**16/16 Passed** en 0.56s:
1. `test_1_amount_within_limit`: Monto por debajo del límite -> `WITHIN_LIMIT`.
2. `test_2_amount_equal_limit`: Monto exactamente igual al límite -> `WITHIN_LIMIT`.
3. `test_3_amount_above_limit_strict_block`: Monto superior a límite estricto -> `LIMIT_EXCEEDED`.
4. `test_4_missing_policy_fail_safe`: Ausencia de política/regla -> `UNKNOWN` (missing != unlimited).
5. `test_5_decimal_only_enforcement`: Rechazo estricto de floats y tipos no Decimal.
6. `test_6_currency_mismatch`: Solicitud en moneda distinta a la regla -> `UNKNOWN`.
7. `test_7_invalid_amount_or_missing_money`: Monto None/inválido -> `ERROR`.
8. `test_8_negative_amount_rejection`: Monto negativo -> `REJECTED`.
9. `test_9_action_specific_limit`: Límites independientes por acción (Refund != Price Update).
10. `test_10_account_specific_limit`: Límites aislados por cuenta/marketplace.
11. `test_11_deterministic_decision_checksum`: Inmutabilidad y checksum SHA-256 de decisiones.
12. `test_12_policy_versioning`: Evaluación respetando versiones explícitas de políticas.
13. `test_13_high_amount_approval_required`: Monto alto con regla override -> `APPROVAL_REQUIRED`.
14. `test_14_n6_approval_not_fabricated`: N.7 no emite evidencias de aprobación N.6.
15. `test_15_n3_deny_not_overridden`: N.7 no otorga bypass ante rechazo de autorización.
16. `test_16_no_m6_inference_confusion`: Segregación total frente a presupuestos de inferencia.

### B. Pruebas de Integración y E2E (`tests/integration/test_n7_financial_limits_integration.py`)
**11/11 Passed** en 2.14s:
- **Escenario A**: Acción financiera autorizada + monto dentro de límite -> Se ejecuta físicamente (1 llamada).
- **Escenario B**: Monto sobre límite estricto sin aprobación -> Bloqueo (0 llamadas físicas).
- **Escenario C**: Monto sobre límite con override + sin evidencia N.6 -> Bloqueo (0 llamadas físicas).
- **Escenario D**: Monto sobre límite con override + evidencia N.6 válida -> Se ejecuta (1 llamada).
- **Escenario E**: Discrepancia de moneda (*currency mismatch*) -> Bloqueo `FINANCIAL_UNKNOWN` (0 llamadas).
- **Escenario F**: Límite específico por cuenta no coincidente -> Bloqueo (0 llamadas).
- **Escenario G**: `N.3 DENY` + monto dentro de límite -> Bloqueo por autorización (0 llamadas).
- **Escenario H**: Reinicio de repositorio JSON -> Persistencia consistente de políticas.
- **Escenario I**: Archivo de política manipulado/corrupto -> Detección por checksum y bloqueo.
- **Escenario J**: Trazabilidad y auditoría K.1/K.2 registradas de forma segura sin fugar secretos N.5.
- **Escenario K**: Flujo E2E completo a través del pipeline `Actor -> N.1 -> N.2 -> N.4 -> N.3 -> N.7 -> N.6 -> Boundary`.

### C. Regresión Completa del Repositorio
```
1721 passed, 1 skipped, 211 warnings in 64.46s (0:01:04)
0 failures, 0 errors.
```

---

## 7. AUDITORÍA DE HIGIENE Y ARTEFACTOS

- `git diff --check`: Limpio (sin trailing whitespaces ni conflictos de formato).
- `git status --short`: Solo archivos correspondientes a transversales N.1–N.7 y sus reportes.
- `git ls-files .pytest_tmp`: Sin archivos temporales o residuos de testing trackeados.
- **Políticas de Control de Versiones**: Cero `git commit`, cero `git push`.

---

## 8. PRÓXIMOS PASOS

- **Hito Actual**: Hito N — Security, Governance y Safety (🟡 EN PROGRESO)
- **Siguiente Tarea Contractual**: **N.8 — Tool Allowlist / Denylist** (⚪ PENDIENTE)
- **Restricción**: NO implementar N.8 de manera anticipada; esperar requerimiento explícito.
