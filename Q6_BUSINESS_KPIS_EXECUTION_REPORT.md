# Q.6 — Business KPIs Execution Report
## Business Intelligence / Executive Performance Indicators & Cross-Domain Business Summary

### 1. Roadmap Alignment & Executive Summary
El objetivo de **Q.6 — Business KPIs** es responder de forma consultiva, holística, explicable y tenant-safe a la pregunta directiva fundamental:
> *"¿Puede un usuario autorizado entender, en una sola vista ejecutiva, el estado comercial de su operación autónoma usando únicamente indicadores derivados de datos reales existentes?"*

Q.6 actúa como la capa de síntesis directiva de **Hito Q — Business Intelligence**, proyectando hechos consolidados de **Q.1 (Opportunity Dashboard)**, **Q.2 (Supplier Dashboard)**, **Q.3 (Profit Dashboard)**, **Q.4 (Mission Dashboard)** y **Q.5 (Agent Cost Dashboard)** sin crear almacenes de datos duplicados ni recalcular motores pesados de negocio.

---

### 2. Architecture & Reuse Principles (REUSE > EXTEND > CREATE)
- **Capa Consultiva Pura:** Q.6 no es fuente de verdad primaria; agrega proyecciones provenientes de los servicios canónicos de Q.1 a Q.5.
- **Aislamiento Multi-Tenant (O.1):** Validación estricta en cada agregación mediante `TenantContext` y `CrossTenantGuard`.
- **Autorización SaaS (O.3 / O.4):** Verificación de permisos con `BUSINESS_KPI_READ` y fallback a `BUSINESS_INTELLIGENCE_READ` con política de Default Deny.
- **Defensa en Profundidad y Sanitización (N.9):** Exclusión total de secretos, tokens de inferencia, trazas de razonamiento (`anti-CoT`), credenciales y PII.

---

### 3. KPI Discovery & Mapping Matrix

| KPI ID | Domain | Source | Formula / Derivation | Denominator Safety | UNKNOWN Semantics | Multi-Currency |
|---|---|---|---|---|---|---|
| `OPPORTUNITY_COUNT` | OPPORTUNITY | Q.1 Summary | Conteo total de oportunidades del tenant | N/A | Repositorio vacío = 0 | N/A |
| `HIGH_POTENTIAL_OPPORTUNITIES` | OPPORTUNITY | Q.1 Summary | Oportunidades con score >= 0.70 | N/A | 0 si no hay candidatas | N/A |
| `VALIDATED_SUPPLIER_COUNT` | SUPPLIER | Q.2 Summary | Proveedores en estado `VERIFIED` o `ACTIVE` | N/A | 0 si no hay verificados | N/A |
| `AVG_SUPPLIER_SCORE` | SUPPLIER | Q.2 Summary | $\sum \text{reliability\_score} / \text{scored\_suppliers}$ | safe_div(n > 0) | UNKNOWN si n = 0 | N/A |
| `COMPLETE_PROFITABILITY_COUNT` | PROFIT | Q.3 Summary | Registros financieros con completitud 100% | N/A | 0 si no hay completos | N/A |
| `AVG_CONTRIBUTION_MARGIN` | PROFIT | Q.3 Summary | $\sum \text{contribution\_margin} / \text{complete\_records}$ | safe_div(n > 0) | UNKNOWN si n = 0 | N/A |
| `NEGATIVE_MARGIN_COUNT` | PROFIT | Q.3 Summary | Registros con margen neto < 0.0 | N/A | 0 si no hay negativos | N/A |
| `MISSION_SUCCESS_RATE` | MISSION | Q.4 Summary | $\text{SUCCESS} / (\text{SUCCESS} + \text{FAILED} + \text{CANCELLED})$ | safe_div(term > 0) | UNKNOWN si term = 0 (RUNNING/PENDING excluidos) | N/A |
| `ACTIVE_MISSIONS` | MISSION | Q.4 Summary | Conteo de misiones en estado `RUNNING` o `PENDING` | N/A | 0 si no hay activas | N/A |
| `FAILED_MISSIONS` | MISSION | Q.4 Summary | Conteo de misiones en estado terminal `FAILED` | N/A | 0 si no hay fallidas | N/A |
| `TOTAL_AGENT_COST` | AGENT_COST | Q.5 Summary | $\sum \text{total\_cost}$ | N/A | UNKNOWN si no hay costos; NOT_COMPARABLE si >1 divisa sin filtro | Currency aislada |
| `AVG_COST_PER_MISSION` | AGENT_COST | Q.5 Summary | $\text{total\_cost} / \text{distinct\_missions\_with\_cost}$ | safe_div(n > 0) | UNKNOWN si n = 0 o multidivisa heterogénea | Currency aislada |
| `OPPORTUNITY_TO_ACTION_RATE` | CROSS_DOMAIN | Q.1 + Q.4 | $\text{missions\_with\_opp} / \text{total\_opportunities}$ | safe_div(opps > 0) | UNKNOWN si opps = 0 | N/A |
| `SUPPLIER_VERIFICATION_RATE` | SUPPLIER | Q.2 Summary | $\text{verified\_suppliers} / \text{total\_suppliers}$ | safe_div(sups > 0) | UNKNOWN si sups = 0 | N/A |
| `COST_PER_OPPORTUNITY` | CROSS_DOMAIN | Q.5 + Q.1 | $\text{total\_agent\_cost} / \text{total\_opportunities}$ | safe_div(opps > 0) | UNKNOWN si opps = 0 o sin costo | Currency aislada |
| `PROJECTED_CONTRIBUTION_TO_AGENT_COST_RATIO` | CROSS_DOMAIN | Q.3 + Q.5 | $\text{total\_projected\_profit} / \text{total\_agent\_cost}$ | safe_div(cost > 0) | UNKNOWN si cost = 0 o divisas incompatibles | Requiere misma divisa |
| `OVERALL_DATA_READINESS` | DATA_QUALITY | Q.1–Q.5 | \% de completitud ponderada de hechos | N/A | 0.00% a 100.00% según facts existentes | N/A |

---

### 4. Mathematical Precision, UNKNOWN Semantics & Currency Rules
1. **UNKNOWN != ZERO:** Un valor ausente, un denominador cero o la falta de registros financieros genera un estado `KPIStatus.UNKNOWN` con valor `None`, preservando la honestidad estadística sin presentar "0.00%" artificial.
2. **Denominadores Seguros en Misiones:** Las misiones en estado `RUNNING` o `PENDING` se computan exclusivamente en `ACTIVE_MISSIONS` y son formalmente excluidas del denominador de `MISSION_SUCCESS_RATE`.
3. **Aislamiento Multi-Divisa:** Q.6 no mezcla CLP con USD. Cuando existen costos en múltiples monedas y la consulta no especifica divisa, `TOTAL_AGENT_COST` se marca como `NOT_COMPARABLE_CURRENCY`, proveyendo el desglose en `currency_breakdown`.
4. **Aritmética Decimal:** Redondeo determinista a 2 decimales (`ROUND_HALF_UP`) mediante `Decimal`.

---

### 5. API & UI Endpoints Implemented
- `GET /api/bi/tenants/{tenant_id}/kpis`: Lista estructurada de KPIs individuales con soporte de filtros (`window`, `domain`, `currency`, `date_from`, `date_to`).
- `GET /api/bi/tenants/{tenant_id}/kpis/summary`: Resumen ejecutivo consolidado con desglose por dominios, `currency_breakdown`, `overall_readiness_pct` y enlaces de drill-down.
- `GET /api/bi/tenants/{tenant_id}/kpis/catalog`: Catálogo canónico con metadatos de fórmulas, versiones y fuentes.
- `GET /api/bi/tenants/{tenant_id}/kpis/{kpi_id}`: Detalle de KPI individual con análisis de confianza, trazabilidad e histórico de comparación.
- `GET /bi/kpis`: Dashboard HTML visual para el operador en Admin Console con filtros interactivos, tarjetas de KPI por dominio e indicadores de calidad de datos.

---

### 6. Test Suite & Validation Results
- **Unit Tests Q.6 (`tests/unit/test_q6_business_kpis_unit.py`):** 12 passed.
- **Integration Tests Q.6 (`tests/integration/test_q6_business_kpis_integration.py`):** 10 passed.
- **Full BI Suite Regression (Q.1–Q.6):** 123 passed, 0 failures.
- **Full Global Suite Regression:**
  - **Baseline Anterior:** 2556 passed, 18 skipped, 0 failures.
  - **Resultado Actual:** **2578 passed, 18 skipped, 0 failures** (22 nuevos tests agregados y validados).
- **Deployment Automation Suite (`python scripts/deploy_validate.py`):** 5/5 checks PASSED.
- **Git Hygiene:** `git diff --check` limpio, sin archivos temporales ni secretos expuestos.

---

### 7. Gantt & Milestone Status
- **Hito P + Gate O:** 🟢 CERRADOS
- **Q.1 Opportunity Dashboard:** 🟢 VALIDADA
- **Q.2 Supplier Dashboard:** 🟢 VALIDADA
- **Q.3 Profit Dashboard:** 🟢 VALIDADA
- **Q.4 Mission Dashboard:** 🟢 VALIDADA
- **Q.5 Agent Cost Dashboard:** 🟢 VALIDADA
- **Q.6 Business KPIs:** 🟢 VALIDADA
- **Hito Q — Business Intelligence:** 🟡 EN PROGRESO (todos los subcomponentes Q.1–Q.6 validados)
- **Gate P — Formal Hito Q Validation:** ⚪ PENDIENTE

---

### 8. Next Step
> **NEXT TASK:** Gate P — Formal Hito Q Validation
