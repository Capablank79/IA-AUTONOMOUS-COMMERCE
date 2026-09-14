# Q.5 — Agent Cost Dashboard Execution Report
## Business Intelligence / AI Usage, Model Cost & Autonomous Mission Cost Visibility

### 1. Executive Summary & Objective
El objetivo de **Q.5 — Agent Cost Dashboard** es responder de forma puramente consultiva, precisa y tenant-safe a la pregunta fundamental de negocio:
> *"¿Puede un usuario autorizado comprender cuánto cuesta ejecutar agentes/modelos, dónde se concentra ese costo y qué misiones/modelos/proveedores consumen más recursos?"*

El dashboard proporciona visibilidad integral de consumo de tokens y costos computacionales sin invadir las fronteras de facturación SaaS comercial (O.9), gestión de cuotas de tokens (O.7), planes comerciales (O.8) ni políticas de control presupuestario de inferencia (M.1–M.6).

---

### 2. Roadmap Alignment & Scope
- **Hito:** Hito Q — Business Intelligence.
- **Principio Rector:** `REUSE > EXTEND > CREATE`.
- **Integraciones Clave Reutilizadas:**
  - **K.3 Cost Tracking:** Hechos financieros inmutables (`CostRecord`, `PricingRate`).
  - **O.6 Usage Metering:** Registro canónico y atómico de consumo (`UsageEvent`, `UsageQuery`).
  - **O.5 Model Gateway:** Generación de hechos de inferencia multi-proveedor.
  - **O.1 Multi-Tenant Isolation:** Aislamiento criptográfico y contextual vía `TenantContext` y `CrossTenantGuard`.
  - **O.3 / O.4 SaaS AuthN & Dynamic RBAC:** Sesiones canónicas y evaluación de permisos (`AGENT_COST_DASHBOARD_READ`, `BUSINESS_INTELLIGENCE_READ`).
  - **Q.4 Mission Dashboard:** Enlaces consultivos y correlación sin mutación.
  - **N.9 Sensitive Data & Anti-CoT:** Sanitización estricta de credenciales, API keys y trazas de razonamiento privado (`chain_of_thought`, `reasoning_tokens`, `internal_scratchpad`).

---

### 3. Architecture & Discovery Matrix
Se identificó la verdad canónica de consumo y costos sin duplicar ledgers:

| Dimension / Fact | Source Model | Ubicación | Persistencia | Moneda | Dimensiones Soportadas | Uso en Q.5 |
|---|---|---|---|---|---|---|
| **Usage Metering Fact** | `UsageEvent` (O.6) | `src/domain/usage_metering/models.py` | JSON / Event Log | N/A (tokens) | tenant, identity, model, provider, mission, agent | Agregación de volumen de tokens y requests |
| **Cost Financial Fact** | `CostRecord` (K.3) | `src/domain/cost/models.py` | JSON / Durable Ledger | ISO (USD, CLP, etc.) | execution_id, mission_id, model, provider, cycle | Cálculo de spend real, promedio y desgloses |
| **Unified BI Item** | `AgentCostDashboardItem` (Q.5) | `src/domain/agent_cost_dashboard/models.py` | Proyección en memoria | ISO Explícita | tenant, mission, agent, model, provider, currency | Presentación y filtrado en API/UI |

---

### 4. Domain Models & Mathematical Precision
1. **Precisión Decimal Estricta:** Todos los cálculos monetarios emplean exclusivamente `Decimal`. Se prohíbe el uso de `float` para evitar errores de redondeo binario.
2. **Aislamiento Multi-Divisa (Multi-Currency Safe):** Se prohíbe terminantemente consolidar o sumar montos en divisas diferentes sin una tasa de cambio (FX) canónica explícita. Los totales en resúmenes se expresan estructurados por código de divisa (`Mapping[str, Decimal]`).
3. **Semántica de Incertidumbre (`UNKNOWN != 0`):** Cuando no existe registro de costo o faltan tarifas de proveedor, los eventos se reportan con costo `None` (`UNKNOWN`) y se contabilizan en `unknown_cost_events_count`. La falta de costo no equivale a costo cero.
4. **Atribución Estricta y Unattributed Costs:** Eventos sin correlación explícita con `mission_id` se clasifican como `unattributed` y no se asignan arbitrariamente a misiones.
5. **División Segura por Cero:** El cálculo de costo promedio por solicitud (`average_cost_per_request`) retorna `None` si el conteo de solicitudes es 0 o si no hay costos conocidos.

---

### 5. API & UI Integration
Se implementaron endpoints seguros en `src/infrastructure/web/admin_app.py` integrados en la consola de administración (`Admin Console`):
- `GET /api/bi/tenants/{tenant_id}/agent-costs`: Listado paginado con filtros (`provider`, `model`, `agent_type`, `mission_id`, `currency`, `date_from`, `date_to`, `min_cost`, `max_cost`, `only_unattributed`).
- `GET /api/bi/tenants/{tenant_id}/agent-costs/summary`: Resumen de alto nivel con métricas agregadas por proveedor, modelo, agente y misión.
- `GET /api/bi/tenants/{tenant_id}/agent-costs/{item_id}`: Detalle de evento con trazabilidad y sanitización N.9.
- `GET /api/bi/tenants/{tenant_id}/agent-costs/missions/{mission_id}/summary`: Resumen específico de costo para una misión correlacionada.
- `GET /bi/agent-costs`: Interfaz HTML enriquecida para el operador con tarjetas de resumen, desgloses y tabla de eventos.

---

### 6. Validation & Test Suite Results
Se ejecutó la suite completa de pruebas unitarias, de integración y de regresión global:

- **Tests Unitarios Q.5:** `tests/unit/test_q5_agent_cost_dashboard_unit.py` (14 passed).
- **Tests de Integración Q.5:** `tests/integration/test_q5_agent_cost_dashboard_integration.py` (6 passed).
- **Regresión Suites BI Q.1–Q.4:** 81 passed.
- **Regresión Módulos Core (K.3, O.1, O.3–O.7, O.10, N.9, P.7, M.6):** 187 passed.
- **Regresión Global de la Suite:**
  - **Baseline Anterior:** 2536 passed, 18 skipped, 0 failures.
  - **Resultado Actual:** **2556 passed, 18 skipped, 0 failures** (20 nuevos tests agregados y validados).
- **Deploy & CI Validation:** `python scripts/deploy_validate.py` (5/5 checks PASSED).
- **Git Hygiene:** `git diff --check`, `git status --short`, `git ls-files .pytest_tmp` limpios.

---

### 7. Governance & Next Steps
- **Estado Q.5:** 🟢 VALIDADA.
- **Estado Hito Q:** 🟡 EN PROGRESO.
- **Estado Gate P:** ⚪ PENDIENTE.
- **NO commit, NO push.**
