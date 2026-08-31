# AI Autonomous Commerce — Roadmap Maestro

> **North Star:** construir un **operador comercial autónomo**, no un chatbot de dropshipping ni una colección de módulos aislados.

**Objetivo final:** que una persona pueda entregar una misión comercial y el sistema pueda descubrir oportunidades, investigarlas, evaluarlas, encontrar abastecimiento, calcular economía y riesgo, decidir, ejecutar acciones autorizadas, comunicar resultados, observar el negocio y aprender de sus resultados.

**Principio rector:** cada incremento debe acercarnos a una demostración real de negocio. Los tests son obligatorios, pero **los tests no son el producto**.

---

## 0. Control del roadmap

### Estado actual

Checkpoint Git:

```text
e08ebb5 feat: integrate autonomous loop with OmniRoute
```

Capacidades ya demostradas:

- Market Intelligence.
- Integración con Mercado Libre.
- Supplier Intelligence inicial.
- Opportunity Engine.
- Profit Engine inicial.
- Mission y MissionRepository.
- AutonomousLoop 01A.
- DecisionProvider / ActionExecutor.
- LLM Decision Provider 01B.
- OmniRoute.
- Routing E2E real:
  `OmniRoute → auto/best-coding → proveedor real → modelo real → LoopDecision`.

### Próximo objetivo

**Hito A — Market Opportunity Discovery**

Demostración obligatoria:

> "Busca oportunidades de productos reales en Mercado Libre Chile, encuentra los mejores candidatos, ordénalos y explícame con evidencia por qué son ganadores."

### Regla de decisión del roadmap

Cuando existan varias tareas posibles, elegir la que:

1. aumente más la capacidad real de negocio;
2. cierre una dependencia crítica;
3. reduzca mayor riesgo técnico;
4. produzca evidencia verificable;
5. no duplique capacidades existentes.

---

# 1. Arquitectura objetivo

```text
                         BUSINESS GOAL
                              │
                              ▼
                     MISSION / OBJECTIVE
                              │
                              ▼
                     AUTONOMOUS AGENT
                              │
                ┌─────────────┼─────────────┐
                │             │             │
                ▼             ▼             ▼
             OBSERVE        DECIDE         ACT
                │             │             │
                ▼             ▼             ▼
          Intelligence     LLM/Rules      Tools
                │             │             │
                └─────────────┼─────────────┘
                              ▼
                         OBSERVATION
                              │
                              ▼
                            MEMORY
                              │
                              ▼
                           LEARN
                              │
                              └──────► NEXT DECISION
```

## Capas

```text
DOMAIN
  ↓
APPLICATION
  ↓
PORTS
  ↓
ADAPTERS / INFRASTRUCTURE
  ↓
EXTERNAL SYSTEMS
```

### Regla

El dominio **no debe conocer**:

- OmniRoute.
- OpenAI.
- Anthropic.
- Gemini.
- HTTP.
- Mercado Libre SDK/API.
- WhatsApp.
- Email provider.
- base de datos concreta.

El dominio conoce contratos y reglas de negocio.

---

# 2. Capacidades funcionales definitivas

El sistema completo deberá cubrir:

## A. Market Intelligence

- Product Discovery.
- Product Family Intelligence.
- Demand Intelligence.
- Price Intelligence.
- Competition Intelligence.
- Review Intelligence.
- Traffic Intelligence.
- Trend Intelligence.
- Market Evidence.
- Freshness.
- Confidence.
- Signal quality.
- Deduplication.
- Entity resolution.

## B. Opportunity Intelligence

- Opportunity scoring.
- Ranking.
- Readiness.
- Evidence sufficiency.
- Opportunity explanation.
- Opportunity comparison.
- Opportunity rejection.
- Opportunity monitoring.

## C. Supplier Intelligence

- Supplier discovery.
- Supplier normalization.
- Supplier evidence.
- Supplier ranking.
- Quote comparison.
- MOQ.
- Lead time.
- Shipping.
- Reliability.
- Supplier risk.
- Historical supplier performance.

## D. Economics

- Cost of goods.
- Landed cost.
- Marketplace fees.
- Shipping.
- Taxes.
- Advertising.
- Returns.
- Operational costs.
- Contribution margin.
- Profit.
- ROI.
- Break-even.
- Capital requirement.

## E. Capital Allocation

El sistema debe poder decidir entre:

```text
INVENTORY
DROPSHIPPING
TEST_SMALL
REJECT
WAIT
```

La decisión debe considerar:

- capital disponible;
- expected demand;
- margin;
- supplier reliability;
- lead time;
- stock;
- obsolescence;
- uncertainty;
- downside risk;
- expected return.

## F. Autonomous Missions

- Mission creation.
- Mission lifecycle.
- Goal.
- Constraints.
- Budget.
- Context.
- Observations.
- Evidence.
- Decisions.
- Actions.
- Results.
- Trace.
- Cancellation.
- Retry/recovery.
- Completion criteria.

## G. Agent Runtime

- Observe.
- Decide.
- Act.
- Re-observe.
- Pivot.
- Reject.
- Promote.
- Complete.
- Iteration limits.
- Context management.
- Decision validation.
- Action validation.
- Cost control.
- Timeouts.
- Recovery.

## H. Communications

- Reports.
- Email composition.
- Email delivery.
- WhatsApp notifications.
- Approval requests.
- Decision summaries.
- Alerts.
- Daily/periodic briefings.

## I. Commerce Operations

- Listing generation.
- Listing validation.
- Publishing.
- Pricing.
- Inventory.
- Orders.
- Fulfillment.
- Returns.
- Supplier coordination.
- Operational alerts.

## J. Memory

- Mission memory.
- Product memory.
- Supplier memory.
- Customer/business memory where appropriate.
- Decision memory.
- Action history.
- Outcome history.
- Evidence history.
- Temporal state.

## K. Learning

- Decision outcome tracking.
- Prediction vs actual.
- Product performance.
- Supplier performance.
- Strategy performance.
- Calibration.
- Ranking improvement.
- Policy improvement.

## L. Platform

- Multi-tenant.
- Authentication.
- Authorization.
- Isolation.
- Usage metering.
- Model gateway.
- Billing.
- Plans.
- Limits.
- Observability.
- Admin.
- Audit.
- Deployment.

---

# 3. FASE 02 — MARKET OPPORTUNITY VERTICAL SLICE

**Prioridad: P0**

**Objetivo:** demostrar valor comercial real.

## TASK 02.1 — Mission de Product Discovery

**Estimación:** 1–2 días.

Entrada:

- objetivo;
- marketplace;
- query/categoría;
- presupuesto;
- restricciones;
- número de candidatos.

Salida:

- misión válida;
- contexto normalizado.

**Aceptación:**
la misión puede representar una búsqueda comercial reproducible.

## TASK 02.2 — Product Hunter real

**Estimación:** 2–3 días.

Conectar:

```text
Mission
→ Mercado Libre
→ búsqueda
→ listings/catalog
→ normalización
→ deduplicación
```

**Aceptación:**
candidatos reales, identificables y trazables.

## TASK 02.3 — Evidence Pipeline

**Estimación:** 2–3 días.

```text
raw observation
→ normalized observation
→ derived signal
→ market evidence
```

Cada evidencia debe tener, cuando corresponda:

- fuente;
- timestamp;
- tipo;
- valor;
- confidence;
- origen observado/derivado.

**Aceptación:**
ningún ranking depende de datos sin procedencia.

## TASK 02.4 — Opportunity Scoring

**Estimación:** 2–4 días.

Construir score reproducible considerando señales disponibles.

**Aceptación:**
dos ejecuciones con los mismos datos producen decisiones consistentes.

## TASK 02.5 — Winner Ranking

**Estimación:** 1–2 días.

Salida:

```text
TOP N
product
score
confidence
evidence coverage
risk
```

## TASK 02.6 — LLM Explanation

**Estimación:** 1–2 días.

El LLM recibe evidencia estructurada.

Debe producir:

```text
WHY WINNER
EVIDENCE
INFERENCE
RISKS
UNKNOWN / MISSING DATA
```

**Regla crítica:**
el LLM no puede inventar métricas.

## TASK 02.7 — Market Discovery E2E

**Estimación:** 1–2 días.

Ejecutar misión real.

### GATE A

Debe ser posible demostrar:

```text
MISSION
→ Mercado Libre
→ candidatos reales
→ evidencia
→ scoring
→ ranking
→ TOP 10
→ explicación
```

**Plazo objetivo:** 1–2 semanas.

---

# 4. FASE 03 — SUPPLIER INTELLIGENCE VERTICAL SLICE

**Prioridad: P0**

**Plazo:** 1 semana.

## TASK 03.1
Supplier Discovery — 2–4 días.

## TASK 03.2
Supplier Evidence — 1–2 días.

## TASK 03.3
Supplier Ranking — 2–3 días.

## TASK 03.4
Supplier Risk — 2–3 días.

## TASK 03.5
Supplier Recommendation — 1–2 días.

### GATE B

```text
WINNER
→ SUPPLIERS
→ QUOTES
→ RANKING
→ RISK
→ RECOMMENDATION
```

Debe explicar por qué un proveedor es mejor que otro.

---

# 5. FASE 04 — PROFIT + CAPITAL ALLOCATION

**Prioridad: P0**

**Plazo:** 1–2 semanas.

## TASK 04.1
Landed Cost — 2–3 días.

## TASK 04.2
Unit Economics — 2–3 días.

## TASK 04.3
Risk Engine — 2–4 días.

## TASK 04.4
Scenario Analysis — 2–3 días.

Evaluar escenarios:

- conservador;
- base;
- optimista.

## TASK 04.5
Capital Allocation — 2–3 días.

### GATE C

El sistema responde:

> "Con este producto, proveedor, capital y nivel de incertidumbre, ¿conviene inventario, dropshipping, una prueba pequeña, esperar o rechazar?"

Y explica la decisión.

---

# 6. FASE 05 — AUTONOMOUS COMMERCE VERTICAL SLICE

**Prioridad: P0**

**Plazo:** 2 semanas.

## TASK 05.1 — Action Registry

Acciones tipadas, versionadas e identificables.

## TASK 05.2 — ActionExecutor

Ejecución mediante adaptadores.

## TASK 05.3 — Policy Engine

Controlar:

- autorización;
- límites;
- presupuesto;
- riesgo;
- permisos;
- aprobación humana;
- idempotencia.

## TASK 05.4 — Tool Registry

El agente debe conocer capacidades disponibles sin acoplarlas al dominio.

## TASK 05.5 — Observe → Decide → Act

Integrar:

```text
observation
→ decision
→ validation
→ action
→ result
→ observation
```

## TASK 05.6 — Recovery

Controlar:

- timeout;
- errores transitorios;
- acción parcialmente ejecutada;
- estado desconocido;
- reintentos seguros;
- idempotencia.

### GATE D

Demostración:

> "Investiga este mercado, encuentra una oportunidad, encuentra proveedor, calcula la rentabilidad y dime qué harías."

La secuencia debe surgir del estado/evidencia y de las decisiones del agente, no de una secuencia hardcodeada.

---

# 7. FASE 06 — HUMAN-IN-THE-LOOP Y COMUNICACIONES

**Prioridad: P1**

**Plazo:** 1–2 semanas.

## TASK 06.1
Report Generator.

## TASK 06.2
Email Composer.

## TASK 06.3
Email Delivery.

## TASK 06.4
WhatsApp Notification Adapter.

## TASK 06.5
Approval Workflow.

Estados:

```text
PROPOSED
→ APPROVED
→ EXECUTING
→ EXECUTED
```

y:

```text
PROPOSED
→ REJECTED
```

## TASK 06.6
Notification Preferences.

Controlar:

- qué recibe el usuario;
- por qué canal;
- frecuencia;
- prioridad;
- horario.

### GATE E

El sistema puede informar una oportunidad y pedir autorización para una acción de impacto.

---

# 8. FASE 07 — MARKETPLACE OPERATIONS

**Prioridad: P1**

**Plazo:** 2–3 semanas.

## TASK 07.1
Listing Generator.

## TASK 07.2
Listing Quality/Policy Validator.

## TASK 07.3
Publishing Adapter.

## TASK 07.4
Pricing Actions.

## TASK 07.5
Inventory Actions.

## TASK 07.6
Order Integration.

## TASK 07.7
Fulfillment.

## TASK 07.8
Returns / Exceptions.

### GATE F

Una oportunidad puede evolucionar controladamente hacia una operación comercial real.

---

# 9. FASE 08 — BUSINESS MEMORY

**Prioridad: P1**

**Plazo:** 2 semanas.

## TASK 08.1
Persistir misiones.

## TASK 08.2
Persistir decisiones.

## TASK 08.3
Persistir acciones.

## TASK 08.4
Persistir resultados.

## TASK 08.5
Product Memory.

## TASK 08.6
Supplier Memory.

## TASK 08.7
Temporal State.

El sistema debe distinguir:

```text
estado actual
vs.
historial
```

### GATE G

El agente puede recuperar qué ocurrió anteriormente y utilizarlo en una nueva decisión.

---

# 10. FASE 09 — LEARNING LOOP

**Prioridad: P1**

**Plazo:** 2 semanas.

## TASK 09.1
Outcome Tracking.

## TASK 09.2
Prediction vs Actual.

## TASK 09.3
Decision Calibration.

## TASK 09.4
Product Performance.

## TASK 09.5
Supplier Performance.

## TASK 09.6
Strategy Performance.

## TASK 09.7
Learning Signals.

### GATE H

El sistema debe poder responder:

> "¿Qué decisiones anteriores funcionaron, cuáles fallaron y qué aprendimos?"

**Regla:**
learning no significa permitir que un LLM cambie arbitrariamente las reglas del sistema.

---

# 11. FASE 10 — CONTINUOUS AUTONOMY

**Prioridad: P1**

**Plazo:** 2 semanas.

## TASK 10.1
Scheduler.

## TASK 10.2
Market Monitoring.

## TASK 10.3
Opportunity Detection.

## TASK 10.4
Change Detection.

Detectar:

- precio;
- demanda;
- competencia;
- stock;
- proveedor;
- tendencia.

## TASK 10.5
Event Bus / Event Processing.

## TASK 10.6
Autonomous Alerts.

## TASK 10.7
Continuous Missions.

### GATE I

El sistema puede detectar una oportunidad o riesgo sin que el usuario tenga que iniciar manualmente la investigación.

---

# 12. FASE 11 — OBSERVABILITY, EVALUATION Y RELIABILITY

**Prioridad: P0 transversal**

Esta fase no debe dejarse para el final. Debe construirse progresivamente.

## TASK 11.1 — Audit Trail

Registrar:

```text
mission
observation
evidence
decision
action
result
actor
timestamp
```

## TASK 11.2 — Agent Trace

Registrar cada iteración.

## TASK 11.3 — Cost Tracking

Medir:

- tokens;
- llamadas;
- latencia;
- coste estimado;
- coste por misión;
- coste por oportunidad.

## TASK 11.4 — Evaluation Harness

Crear casos de evaluación reproducibles para:

- ranking;
- extracción;
- explicación;
- decisiones;
- validación;
- tool selection.

## TASK 11.5 — Golden Datasets

Conservar datasets de referencia para detectar regresiones.

## TASK 11.6 — Quality Gates

No desplegar si empeoran métricas críticas.

## TASK 11.7 — Reliability

Controlar:

- timeout;
- retry;
- circuit breaker;
- rate limits;
- backpressure;
- idempotencia;
- degradación controlada.

## TASK 11.8 — Security

Controlar:

- secretos;
- permisos;
- aislamiento;
- prompt injection;
- tool injection;
- SSRF;
- abuso de herramientas;
- exposición de datos.

### GATE J

Cada misión importante debe ser reconstruible y auditable.

---

# 13. FASE 12 — DATA QUALITY Y GOVERNANCE

**Prioridad: P0 transversal**

## TASK 12.1
Source Registry.

## TASK 12.2
Data Provenance.

## TASK 12.3
Freshness / TTL.

## TASK 12.4
Confidence Model.

## TASK 12.5
Schema Validation.

## TASK 12.6
Entity Resolution.

## TASK 12.7
Duplicate Detection.

## TASK 12.8
Conflict Resolution.

Cuando dos fuentes discrepen, el sistema no debe esconder el conflicto.

Debe poder expresar:

```text
SOURCE A → value X
SOURCE B → value Y
STATUS → CONFLICT
```

### GATE K

Las decisiones comerciales críticas deben poder rastrearse hasta sus datos de origen.

---

# 14. FASE 13 — CONTROL DE COSTE E INFERENCIA

**Prioridad: P1**

## TASK 13.1
Model Routing Strategy.

## TASK 13.2
Context Budgeting.

## TASK 13.3
Prompt Compression.

## TASK 13.4
Caching.

## TASK 13.5
Model Selection by Task.

## TASK 13.6
Cost-aware Decision Policy.

El sistema debe poder elegir entre modelos según:

- complejidad;
- coste;
- latencia;
- calidad;
- criticidad.

### GATE L

Una misión comercial completa debe tener un coste de inferencia medible y controlable.

---

# 15. FASE 14 — SECURITY, GOVERNANCE Y SAFETY

**Prioridad: P0 transversal**

## TASK 14.1
Identity.

## TASK 14.2
Authentication.

## TASK 14.3
Authorization.

## TASK 14.4
RBAC / permissions.

## TASK 14.5
Secret Management.

## TASK 14.6
Approval Policies.

## TASK 14.7
Financial Limits.

## TASK 14.8
Tool Allowlist / Denylist.

## TASK 14.9
Sensitive Data Handling.

## TASK 14.10
Audit / Compliance.

## TASK 14.11
Emergency Stop.

Debe existir capacidad de:

```text
STOP AGENT
STOP MISSION
STOP TOOL
STOP TENANT
```

### GATE M

Ninguna acción financiera o externa de alto impacto puede ejecutarse sin cumplir la política correspondiente.

---

# 16. FASE 15 — SaaS / PLATFORMIZATION

**Prioridad: P2**

No iniciar esta fase antes de demostrar el núcleo comercial.

## TASK 15.1
Tenant Isolation.

## TASK 15.2
Organizations / Users.

## TASK 15.3
Authentication.

## TASK 15.4
Authorization.

## TASK 15.5
Model Gateway.

Soportar conceptualmente:

```text
OmniRoute
OpenAI
Anthropic
Google
Local Models
BYO Key
```

## TASK 15.6
Usage Metering.

## TASK 15.7
Quota Management.

## TASK 15.8
Plans.

## TASK 15.9
Billing.

## TASK 15.10
Admin Console.

## TASK 15.11
Tenant-level Configuration.

## TASK 15.12
Observability.

## TASK 15.13
Deployment Automation.

### GATE N

Un segundo usuario/tenant puede operar el sistema sin compartir estado, secretos, memoria ni permisos con otro tenant.

---

# 17. FASE 16 — PRODUCTION / OPERATIONS

**Prioridad: P2**

## TASK 16.1
CI/CD.

## TASK 16.2
Environment Separation.

```text
DEV
STAGING
PRODUCTION
```

## TASK 16.3
Database Migrations.

## TASK 16.4
Backups.

## TASK 16.5
Disaster Recovery.

## TASK 16.6
Health Checks.

## TASK 16.7
Monitoring.

## TASK 16.8
Alerting.

## TASK 16.9
Log Retention.

## TASK 16.10
Capacity Planning.

## TASK 16.11
Rate-limit Management.

### GATE O

El sistema puede desplegarse, monitorizarse, recuperarse y actualizarse sin perder estado crítico.

---

# 18. FASE 17 — BUSINESS INTELLIGENCE

**Prioridad: P2**

## TASK 17.1
Opportunity Dashboard.

## TASK 17.2
Supplier Dashboard.

## TASK 17.3
Profit Dashboard.

## TASK 17.4
Mission Dashboard.

## TASK 17.5
Agent Cost Dashboard.

## TASK 17.6
Business KPIs.

KPIs mínimos:

- oportunidades detectadas;
- oportunidades aceptadas;
- oportunidades rechazadas;
- margen esperado;
- margen real;
- capital invertido;
- ventas;
- ROI;
- coste del agente;
- tasa de éxito;
- tasa de error;
- tiempo de misión.

---

# 19. FASE 18 — ADVANCED AUTONOMY

**Prioridad: P3**

Solo después de que el sistema básico sea estable.

## TASK 18.1
Multi-step Planning.

## TASK 18.2
Sub-missions.

## TASK 18.3
Specialist Agents.

Posibles roles:

```text
Market Analyst
Product Hunter
Supplier Hunter
Profit Analyst
Risk Analyst
Listing Agent
Operations Agent
Communication Agent
```

## TASK 18.4
Agent Coordination.

## TASK 18.5
Dynamic Delegation.

## TASK 18.6
Long-running Missions.

## TASK 18.7
Self-monitoring.

### GATE P

El sistema puede dividir una misión compleja en submisiones sin perder trazabilidad ni control de permisos.

---

# 20. FASE 19 — SELF-IMPROVING COMMERCE

**Prioridad: P3**

## TASK 19.1
Strategy Memory.

## TASK 19.2
Experiment Framework.

## TASK 19.3
A/B Testing.

## TASK 19.4
Decision Calibration.

## TASK 19.5
Outcome-driven Ranking.

## TASK 19.6
Automated Evaluation.

## TASK 19.7
Human Feedback.

### Regla

El sistema puede aprender de resultados, pero cualquier modificación de políticas críticas debe pasar por control explícito.

---

# 21. Matriz de dependencias

```text
Market Intelligence
        ↓
Opportunity Engine
        ↓
Supplier Intelligence
        ↓
Profit / Risk
        ↓
Capital Allocation
        ↓
Autonomous Commerce
        ↓
Communications
        ↓
Marketplace Operations
        ↓
Memory
        ↓
Learning
        ↓
Continuous Autonomy
        ↓
SaaS / Scale
```

Pero existen capacidades transversales que comienzan desde ahora:

```text
Security
Observability
Data Quality
Evaluation
Cost Control
Auditability
```

Estas no esperan al final.

---

# 22. Roadmap temporal consolidado

Las estimaciones son de desarrollo efectivo y pueden cambiar según APIs, límites externos, calidad de datos y resultados de pruebas.

| Hito | Capacidad | Estimación |
|---|---|---:|
| A | Market Opportunity Discovery | 1–2 semanas |
| B | Supplier Intelligence | 1 semana |
| C | Profit + Capital Allocation | 1–2 semanas |
| D | Autonomous Commerce | 2 semanas |
| E | Communications + Approval | 1–2 semanas |
| F | Marketplace Operations | 2–3 semanas |
| G | Business Memory | 2 semanas |
| H | Learning Loop | 2 semanas |
| I | Continuous Autonomy | 2 semanas |
| J–M | Reliability, Data, Cost, Security | transversal |
| N | SaaS Platformization | 3–5 semanas |
| O | Production Operations | 2–3 semanas |
| P | Advanced Autonomy | posterior |
| Q | Self-improving Commerce | posterior |

**Importante:** no sumar todas las semanas como si fueran estrictamente secuenciales. Las capacidades transversales y algunas fases pueden desarrollarse en paralelo cuando sus dependencias estén maduras.

---

# 23. Definición de Done

Una task está terminada cuando:

- existe implementación;
- existen tests;
- regresión pasa;
- no hay errores de `git diff --check`;
- contratos están documentados;
- errores están controlados;
- observabilidad existe cuando corresponde;
- seguridad existe cuando corresponde;
- existe evidencia real si es una integración;
- no se rompe arquitectura;
- el resultado aporta una capacidad observable.

Una fase está terminada solamente cuando su **Gate** pasa.

---

# 24. Protocolo de trabajo con TRAE

Para cada task:

1. Inspeccionar el repositorio.
2. Revisar arquitectura existente.
3. Identificar capacidades reutilizables.
4. Definir cambios mínimos.
5. Implementar.
6. Crear tests.
7. Ejecutar tests específicos.
8. Ejecutar regresión completa.
9. Ejecutar `git diff --check`.
10. Revisar seguridad.
11. Revisar desacoplamiento.
12. Ejecutar E2E cuando corresponda.
13. Reportar evidencia.
14. No hacer commit/push salvo instrucción explícita.
15. No avanzar automáticamente a otra task.

### Regla adicional

TRAE no debe "arreglar" problemas fuera del alcance de la task.

Si descubre un problema:

```text
PROBLEMA DETECTADO
→ documentarlo
→ evaluar impacto
→ no ocultarlo
→ continuar solo si no bloquea
```

---

# 25. Testing Strategy

## Unit

Reglas puras y componentes aislados.

## Integration

Contratos entre componentes.

## Contract Tests

Garantizar que adaptadores cumplen ports.

## E2E

Servicios reales cuando sea posible.

## Regression

Toda la suite existente.

## Evaluation

Calidad del agente y decisiones.

## Failure Testing

Probar:

- API caída;
- datos incompletos;
- proveedor caído;
- LLM inválido;
- timeout;
- rate limit;
- respuesta contradictoria;
- acción duplicada;
- estado desconocido.

---

# 26. Métricas maestras

## Market

- coverage;
- freshness;
- evidence coverage;
- data completeness.

## Opportunity

- precision;
- ranking quality;
- confidence calibration;
- rejection rate.

## Supplier

- supplier validity;
- cost accuracy;
- lead time accuracy;
- reliability.

## Economics

- expected margin;
- realized margin;
- ROI;
- capital efficiency.

## Agent

- task success;
- iterations;
- latency;
- tool errors;
- decision errors;
- cost per mission.

## Business

- opportunities;
- conversions;
- sales;
- profit;
- capital deployed;
- return on capital.

---

# 27. Decisiones de arquitectura que no deben romperse

### DecisionProvider

El contrato debe permanecer independiente del proveedor de modelos.

### ActionExecutor

Debe recibir una decisión validada y ejecutar mediante herramientas controladas.

### Evidence

Debe existir separación entre:

```text
OBSERVED
DERIVED
INFERRED
RECOMMENDED
```

### Confidence

La confianza no debe confundirse con verdad.

### Memory

La memoria no debe convertirse en una fuente silenciosa de hechos sin procedencia.

### LLM

El LLM es un componente de razonamiento, no la autoridad final sobre datos o permisos.

### Policies

Las políticas críticas deben ser deterministas y externas al prompt.

---

# 28. Riesgos que el roadmap debe cerrar

1. Hallucination.
2. Prompt injection.
3. Tool injection.
4. Datos obsoletos.
5. Datos contradictorios.
6. Ranking incorrecto.
7. Coste excesivo de contexto.
8. Loops infinitos.
9. Acciones duplicadas.
10. Acciones irreversibles.
11. API failures.
12. Rate limits.
13. Credenciales expuestas.
14. Cross-tenant leakage.
15. Falta de trazabilidad.
16. Decisiones sin evidencia.
17. Sobreoptimización del agente.
18. Automatización de decisiones de alto impacto sin aprobación.
19. Drift de modelos.
20. Drift de mercado.
21. Cambios en APIs externas.
22. Degradación silenciosa.
23. Dependencia excesiva de un único proveedor/modelo.

---

# 29. North Star final

La demostración máxima del sistema será:

```text
USUARIO
  │
  │ "Opera mi negocio bajo estas reglas."
  ▼
MISSION
  │
  ▼
AUTONOMOUS AGENT
  │
  ├── descubre oportunidades
  ├── investiga mercado
  ├── analiza evidencia
  ├── encuentra proveedores
  ├── calcula rentabilidad
  ├── evalúa riesgo
  ├── decide inventario/dropshipping
  ├── prepara acciones
  ├── solicita aprobación cuando corresponde
  ├── ejecuta acciones autorizadas
  ├── monitoriza resultados
  ├── comunica cambios
  └── aprende de resultados
          │
          ▼
      BUSINESS MEMORY
          │
          ▼
      NEXT DECISION
```

La evolución completa es:

**DESCUBRIR → INVESTIGAR → EVALUAR → DECIDIR → EJECUTAR → MEDIR → APRENDER → VOLVER A DECIDIR**

---

# 30. Regla final del proyecto

> **No construir por construir.**

Antes de iniciar cualquier nueva tarea, responder:

1. ¿Qué capacidad de negocio añade?
2. ¿Qué dependencia cierra?
3. ¿Qué riesgo reduce?
4. ¿Cómo se demostrará?
5. ¿Qué evidencia determinará que está realmente terminada?

Si no podemos responder esas cinco preguntas, la tarea todavía no está suficientemente definida.

**AI Autonomous Commerce no se considera terminado cuando tiene muchos módulos.**

Se considera terminado cuando puede **operar un negocio real de forma autónoma, explicable, segura, auditable y económicamente viable.**
