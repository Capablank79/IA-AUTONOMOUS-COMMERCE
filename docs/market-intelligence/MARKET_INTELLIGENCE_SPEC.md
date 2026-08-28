# Market Intelligence Specification

**Proyecto:** AI Autonomous Commerce  
**Estado:** Diseño funcional posterior al API Discovery  
**Fuente adicional incorporada:** análisis del workflow de scraping y análisis competitivo de Amazon compartido por el usuario.  
**Decisión:** incorporar Customer Pain Mining al diseño de Market Intelligence y dejar Visual Competitive Intelligence como capacidad extensible.

---

# 1. Propósito

Market Intelligence es el subsistema encargado de descubrir, observar, normalizar y transformar señales de mercado en evidencia estructurada para que otros componentes puedan tomar decisiones comerciales.

No es un simple scraper.

Su responsabilidad es:

```text
DISCOVERY
   ↓
OBSERVATION
   ↓
NORMALIZATION
   ↓
SIGNAL EXTRACTION
   ↓
MARKET EVIDENCE
   ↓
OPPORTUNITY INPUT
```

La pregunta funcional central es:

> ¿Existe evidencia suficiente de demanda, competencia, comportamiento del cliente y condiciones de mercado para considerar este producto una oportunidad que deba continuar hacia Supplier Intelligence, Profit Engine y Risk Engine?

---

# 2. Principios de diseño

## 2.1 Evidence First

Toda señal debe conservar su origen.

```text
SOURCE DATA
    ↓
DERIVED SIGNAL
    ↓
EVIDENCE
```

No se debe perder la trazabilidad entre una conclusión y los datos que la originaron.

---

## 2.2 Observed != Derived != Estimated

El sistema debe diferenciar:

```text
OBSERVED
DERIVED
ESTIMATED
```

Ejemplos:

```text
price_min       → OBSERVED
visit_momentum  → DERIVED
sales_estimate  → ESTIMATED
```

Una estimación nunca debe presentarse como dato observado.

---

## 2.3 No depender de sold_quantity

La ausencia de `sold_quantity` para listings de terceros está documentada como una restricción conocida.

Por tanto:

```text
SOLD_QUANTITY
      ↓
NO DEPENDENCY
```

La evaluación se basará en evidencia multidimensional.

---

## 2.4 No confundir señales

```text
VISITS       != SALES
REVIEWS      != SALES
BEST_SELLER  != SALES
TREND        != SALES
```

Cada señal mide una dimensión diferente del mercado.

---

## 2.5 Decisión basada en múltiples señales

Ninguna señal individual debe determinar por sí sola que un producto es una oportunidad.

```text
DEMAND
+
TRAFFIC
+
MOMENTUM
+
COMPETITION
+
PRICE
+
REPUTATION
+
CUSTOMER PAIN
=
MARKET EVIDENCE
```

---

# 3. Arquitectura funcional

```text
                    MARKET INTELLIGENCE
                            |
        +-------------------+-------------------+
        |                   |                   |
        v                   v                   v
      DEMAND           COMPETITION         CUSTOMER
        |                   |                   |
   Trends              Listings             Reviews
   Best Seller         Sellers              Ratings
   Search              Prices               Pain Points
   Keywords            Concentration        Unmet Needs
        |                   |                   |
        +-------------------+-------------------+
                            |
                            v
                     MARKET EVIDENCE
                            |
                            v
                    OPPORTUNITY INPUT
```

Una extensión posterior:

```text
MARKET INTELLIGENCE
        |
        +--> VISUAL COMPETITIVE INTELLIGENCE
```

---

# 4. Entidades principales

## 4.1 Product

Representa el producto normalizado del catálogo.

Datos principales:

```text
product_id
catalog_product_id
name
family_name
domain_id
category
status
brand
attributes
pictures
parent_id
children_ids
buy_box_winner
last_updated
```

---

## 4.2 Listing

Representa una publicación concreta.

```text
item_id
product_id
seller_id
price
listing_type
status
permalink
```

---

## 4.3 Seller

Representa al vendedor.

```text
seller_id
nickname
country
permalink
```

---

## 4.4 Keyword

Representa una consulta o término de descubrimiento.

```text
keyword
source
position
observed_at
```

---

## 4.5 TrendSignal

Representa una señal de interés proveniente de Trends.

```text
keyword
position
source
observed_at
```

---

## 4.6 BestSellerSignal

Representa una posición de producto dentro de una selección Best Seller.

```text
product_id
category_id
position
observed_at
source
```

---

## 4.7 VisitObservation

Representa tráfico observado para un listing.

```text
item_id
date
visits
source
```

---

## 4.8 VisitSignal

Representa una métrica derivada del tráfico.

```text
item_id
window
total_visits
average_daily_visits
observed_days
coverage_ratio
momentum
acceleration
```

---

## 4.9 Review

Representa una review individual.

```text
review_id
item_id
secondary_key
date_created
buying_date
rate
title
content
likes
dislikes
media
relevance
status
catalog_listing
```

---

# 5. Customer Pain Intelligence

Esta es una capacidad nueva incorporada al diseño a partir del análisis del workflow de Amazon compartido.

El objetivo no es simplemente almacenar reviews.

El objetivo es transformar reviews en inteligencia sobre el cliente.

```text
REVIEWS
   ↓
TEXT ANALYSIS
   ↓
CUSTOMER SIGNALS
   ↓
PAIN POINTS
   ↓
UNMET NEEDS
   ↓
DIFFERENTIATION OPPORTUNITIES
```

---

# 6. CustomerPainSignal

Entidad conceptual:

```text
CustomerPainSignal
├── pain_point
├── category
├── frequency
├── severity
├── evidence_count
├── source_reviews[]
├── confidence
└── observed_at
```

Ejemplo conceptual:

```text
pain_point = "batería insuficiente"
category = "performance"
frequency = "frequent"
severity = 7
evidence_count = 18
confidence = 0.91
```

La frecuencia y severidad deben ser señales derivadas, no hechos absolutos.

---

# 7. Clasificación de Customer Pain

El análisis debe intentar identificar al menos:

```text
FUNCTIONAL
QUALITY
PERFORMANCE
DURABILITY
USABILITY
SIZE
BATTERY
MATERIAL
SHIPPING
PACKAGING
PRICE_VALUE
MISSING_FEATURE
COMPATIBILITY
OTHER
```

La taxonomía puede crecer posteriormente.

---

# 8. Positive Customer Signals

No sólo deben extraerse problemas.

También:

```text
aspectos_positivos
```

Ejemplo:

```text
PositiveCustomerSignal
├── aspect
├── frequency
├── evidence_count
├── source_reviews[]
└── confidence
```

Esto permite descubrir qué atributos valoran realmente los consumidores.

---

# 9. Unmet Needs

A partir de múltiples reviews, el sistema puede derivar necesidades que el producto actual no satisface completamente.

```text
Review evidence
      ↓
Repeated complaint
      ↓
Underlying need
      ↓
Unmet need
```

Ejemplo conceptual:

```text
Reviews:
"la batería dura poco"
"se descarga rápido"
"debería durar más"

↓

UNMET NEED:
Mayor autonomía
```

La necesidad debe distinguirse de la frase literal de una review.

---

# 10. Differentiation Opportunity

La oportunidad de diferenciación es una inferencia de mayor nivel.

```text
PAIN POINT
     +
UNMET NEED
     +
COMPETITIVE EVIDENCE
     ↓
DIFFERENTIATION OPPORTUNITY
```

Ejemplo:

```text
Pain:
batería insuficiente

Unmet need:
mayor autonomía

Competitive evidence:
problema repetido en múltiples listings

Opportunity:
buscar producto/proveedor con autonomía significativamente superior
```

Esto **no significa todavía que exista un proveedor viable**.

Eso será responsabilidad de Supplier Hunter.

---

# 11. Review Intelligence Pipeline

```text
REVIEWS
   ↓
CLEAN / NORMALIZE
   ↓
EXTRACT CLAIMS
   ↓
CLASSIFY SENTIMENT
   ↓
CLASSIFY PAIN
   ↓
GROUP SIMILAR ISSUES
   ↓
COUNT EVIDENCE
   ↓
ESTIMATE FREQUENCY
   ↓
ESTIMATE SEVERITY
   ↓
IDENTIFY UNMET NEED
   ↓
GENERATE OPPORTUNITY
```

La salida debe ser estructurada.

No se debe almacenar únicamente una respuesta narrativa del LLM.

---

# 12. LLM Boundary

El LLM puede ayudar a:

```text
clasificar
agrupar
resumir
extraer
inferir
```

Pero no debe convertirse en la fuente primaria de verdad.

La arquitectura correcta es:

```text
API DATA
   ↓
RAW EVIDENCE
   ↓
LLM ANALYSIS
   ↓
STRUCTURED SIGNAL
   ↓
PERSISTENCE
```

No:

```text
LLM
 ↓
OPINION
 ↓
DECISION
```

---

# 13. Evidence Linking

Cada CustomerPainSignal debe poder apuntar a sus evidencias.

```text
CustomerPainSignal
      |
      +--> review_id
      +--> review_id
      +--> review_id
```

Esto permitirá posteriormente responder:

> ¿Por qué el sistema considera que este producto tiene este problema?

Y recuperar las reviews que sustentan la señal.

---

# 14. Competition Intelligence

Para cada producto:

```text
listing_count
unique_seller_count
price_min
price_max
price_avg
price_distribution
traffic_total
traffic_share
traffic_concentration
```

Debe ser posible identificar:

```text
dominant_listing
dominant_seller
price_position
traffic_position
```

---

# 15. Traffic Intelligence

Para cada listing:

```text
VisitObservation
      ↓
Time Series
      ↓
Window Metrics
      ↓
Momentum
      ↓
Traffic Trend
```

Métricas mínimas:

```text
total_visits
average_daily_visits
observed_days
coverage_ratio
recent_average
previous_average
momentum
```

Cuando el día actual esté incompleto:

```text
exclude_from_period_comparison = true
```

---

# 16. Price Intelligence

Métricas:

```text
price_min
price_max
price_avg
price_median
price_spread
price_position
```

Cuando exista suficiente información:

```text
price_concentration
```

El sistema no debe interpretar automáticamente un precio alto o bajo como bueno o malo.

La interpretación depende de:

```text
competition
traffic
reputation
supplier_cost
fees
```

---

# 17. Demand Intelligence

Fuentes:

```text
Trends
Best Seller
Product Search
Visits
Review Activity
```

Las señales deben conservar su fuente.

Ejemplo:

```text
DemandSignal
├── type
├── value
├── source
├── observed_at
└── confidence
```

---

# 18. Market Evidence

Debe existir una entidad o agregado conceptual:

```text
MarketEvidence
├── product
├── demand_signals[]
├── competition_signals[]
├── traffic_signals[]
├── reputation_signals[]
├── customer_pain_signals[]
├── positive_customer_signals[]
├── unmet_needs[]
├── differentiation_opportunities[]
└── confidence
```

Este objeto será el principal input del Opportunity Engine.

---

# 19. Confidence Model

Cada señal debe poder expresar confianza.

Nivel conceptual:

```text
HIGH
MEDIUM
LOW
UNKNOWN
```

La confianza debe considerar:

```text
source_quality
evidence_count
data_coverage
recency
consistency
```

Ejemplo:

```text
pain_point:
"batería insuficiente"

evidence_count = 18
coverage = high
recency = high
consistency = high

confidence = HIGH
```

No se debe confundir:

```text
confidence
```

con:

```text
opportunity_score
```

Son conceptos diferentes.

---

# 20. Opportunity Score — fuera del núcleo

Market Intelligence **no debe decidir por sí solo** que un producto es rentable.

Entregará evidencia al Opportunity Engine.

Conceptualmente:

```text
MarketEvidence
      +
SupplierEvidence
      +
ProfitEvidence
      +
RiskEvidence
      ↓
Opportunity Engine
      ↓
Opportunity Score
```

---

# 21. Visual Competitive Intelligence

Capacidad detectada en el workflow de Amazon analizado.

El sistema de referencia utiliza un modelo de visión para evaluar:

- calidad visual;
- elementos destacados;
- profesionalismo de fotografía;
- percepción transmitida al consumidor.

Esta capacidad es relevante para nuestro proyecto, pero **no debe bloquear el MVP**.

Arquitectura futura:

```text
PRODUCT IMAGES
      ↓
VISION MODEL
      ↓
VISUAL SIGNALS
      ↓
LISTING QUALITY
      ↓
VISUAL DIFFERENTIATION
```

Posibles señales:

```text
image_quality
image_count
presentation_quality
feature_visibility
visual_clarity
professionalism
```

---

# 22. Diferencia respecto del workflow de referencia

El workflow analizado realiza:

```text
Amazon Search
    ↓
5 products
    ↓
sort by reviews
    ↓
LLM
    ↓
SEO + customer analysis
    ↓
Google Sheets
```

Nuestra arquitectura no debe copiar esa lógica.

Nuestra versión será:

```text
MULTI-SOURCE DISCOVERY
        ↓
PRODUCT NORMALIZATION
        ↓
COMPETITION
        ↓
TRAFFIC
        ↓
REPUTATION
        ↓
CUSTOMER PAIN MINING
        ↓
MARKET EVIDENCE
```

La diferencia fundamental es que la selección de oportunidades no dependerá únicamente de `reviewsCount`.

---

# 23. Anti-patterns

No implementar:

```text
reviewsCount DESC → opportunity
```

No implementar:

```text
visits > X → opportunity
```

No implementar:

```text
rating > X → opportunity
```

No implementar:

```text
Best Seller → guaranteed success
```

No implementar:

```text
LLM opinion → business decision
```

No implementar:

```text
sales = visits * assumed_conversion
```

sin declarar explícitamente que es una estimación y sin evidencia que la justifique.

---

# 24. Data Contract de una señal

Formato conceptual:

```json
{
  "signal_name": "visit_momentum",
  "entity_id": "MLC1059998914",
  "value": -0.234,
  "signal_type": "DERIVED",
  "source": "mercadolibre_visits",
  "observed_at": "2026-08-27T00:00:00Z",
  "window": "14d",
  "coverage": 1.0,
  "confidence": "HIGH"
}
```

---

# 25. Data Contract de Customer Pain

```json
{
  "signal_name": "customer_pain",
  "entity_id": "MLC18622311",
  "pain_point": "batería insuficiente",
  "category": "BATTERY",
  "frequency": "FREQUENT",
  "severity": 7,
  "evidence_count": 18,
  "source_reviews": [
    "review_id_1",
    "review_id_2"
  ],
  "signal_type": "DERIVED",
  "confidence": "HIGH"
}
```

Los valores anteriores son ilustrativos y no representan datos reales del producto.

---

# 26. Use Cases

## UC-01 Discover Products

Entrada:

```text
keyword / trend / category
```

Salida:

```text
Product candidates
```

---

## UC-02 Analyze Product Competition

Entrada:

```text
product_id
```

Salida:

```text
listings
sellers
prices
competition signals
```

---

## UC-03 Analyze Traffic

Entrada:

```text
item_id
window
```

Salida:

```text
visit observations
momentum
trend
coverage
```

---

## UC-04 Analyze Reputation

Entrada:

```text
item_id
```

Salida:

```text
rating
rating distribution
reviews
review activity
```

---

## UC-05 Mine Customer Pain

Entrada:

```text
reviews[]
```

Salida:

```text
positive signals
pain points
unmet needs
differentiation opportunities
```

---

## UC-06 Build Market Evidence

Entrada:

```text
product
+
demand
+
competition
+
traffic
+
reputation
+
customer intelligence
```

Salida:

```text
MarketEvidence
```

---

# 27. Output del Market Intelligence

El módulo debe entregar algo conceptualmente similar a:

```text
MarketEvidence
│
├── Product
│
├── Demand
│   ├── trends
│   ├── best_seller
│   └── search
│
├── Competition
│   ├── listings
│   ├── sellers
│   └── prices
│
├── Traffic
│   ├── volume
│   ├── momentum
│   └── concentration
│
├── Reputation
│   ├── rating
│   ├── reviews
│   └── distribution
│
└── Customer Intelligence
    ├── positive signals
    ├── pain points
    ├── unmet needs
    └── differentiation opportunities
```

---

# 28. Persistencia

Debe conservarse como mínimo:

```text
raw observations
normalized entities
derived signals
evidence links
timestamps
source
confidence
```

La persistencia debe permitir reconstruir cómo se obtuvo una conclusión.

---

# 29. Recencia

Las señales de mercado envejecen.

Cada señal debe tener:

```text
observed_at
```

y, cuando corresponda:

```text
valid_until
```

o una política de freshness.

Ejemplo:

```text
Best Seller:
snapshot-based

Visits:
time-series

Review:
event-based
```

No todas las señales deben caducar de la misma forma.

---

# 30. Próxima arquitectura de implementación

```text
src/
├── domain/
│   └── market_intelligence/
│       ├── models/
│       ├── signals/
│       ├── evidence/
│       └── value_objects/
│
├── application/
│   └── market_intelligence/
│       ├── product_discovery_service
│       ├── competition_service
│       ├── traffic_intelligence_service
│       ├── review_intelligence_service
│       ├── customer_pain_service
│       └── market_evidence_service
│
└── infrastructure/
    ├── mercadolibre/
    │   ├── product_catalog_data_source
    │   ├── listings_data_source
    │   ├── visits_data_source
    │   ├── reviews_data_source
    │   └── trends_data_source
    │
    └── intelligence/
        └── llm_customer_pain_analyzer
```

Los nombres son una propuesta inicial y podrán ajustarse al estilo ya existente del repositorio.

---

# 31. Orden de implementación

No implementar todo simultáneamente.

Orden recomendado:

```text
1. DOMAIN MODELS
       ↓
2. SIGNAL CONTRACTS
       ↓
3. PRODUCT DISCOVERY
       ↓
4. LISTINGS + SELLERS
       ↓
5. PRICE INTELLIGENCE
       ↓
6. VISITS + MOMENTUM
       ↓
7. REVIEWS
       ↓
8. CUSTOMER PAIN MINING
       ↓
9. MARKET EVIDENCE
       ↓
10. TESTS
       ↓
11. OPPORTUNITY ENGINE INPUT
```

Visual Intelligence queda fuera de esta primera secuencia.

---

# 32. Criterio de finalización del MVP

Market Intelligence MVP estará terminado cuando pueda:

```text
[✓] descubrir un producto
[✓] normalizarlo
[✓] obtener sus listings
[✓] identificar vendedores
[✓] analizar precios
[✓] obtener visitas
[✓] calcular señales temporales
[✓] obtener reviews
[✓] analizar reputación
[✓] extraer customer pain
[✓] detectar unmet needs
[✓] generar differentiation opportunities
[✓] construir MarketEvidence
[✓] conservar trazabilidad
[✓] expresar confidence
```

Y entregue un objeto estructurado que pueda ser consumido por:

```text
Opportunity Engine
```

sin depender de `sold_quantity`.

---

# 33. Decisión arquitectónica final

El análisis del workflow de Amazon confirma una capacidad valiosa:

```text
COMPETITOR DATA
      ↓
CUSTOMER REVIEW ANALYSIS
      ↓
PAIN + UNMET NEED
      ↓
DIFFERENTIATION
```

Esta capacidad se incorpora oficialmente a AI Autonomous Commerce como:

```text
CUSTOMER PAIN INTELLIGENCE
```

La capacidad de análisis visual queda registrada como:

```text
VISUAL COMPETITIVE INTELLIGENCE
```

pero no forma parte del núcleo mínimo.

La arquitectura final de Market Intelligence queda:

```text
                 MARKET INTELLIGENCE
                         |
        +----------------+----------------+
        |                |                |
      DEMAND        COMPETITION        CUSTOMER
        |                |                |
      Trends          Listings          Reviews
      Best Seller     Sellers           Ratings
      Search          Prices            Pain
      Visits          Traffic           Needs
        |                |                |
        +----------------+----------------+
                         |
                         v
                  MARKET EVIDENCE
                         |
                         v
                 OPPORTUNITY ENGINE
```

La ausencia de `sold_quantity` continúa siendo una restricción conocida y no un bloqueo arquitectónico.
