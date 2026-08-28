# Market Intelligence — GAP Analysis & MVP Specification

**Proyecto:** AI Autonomous Commerce  
**Estado:** Discovery de Mercado Libre congelado para esta fase.

## 1. Propósito

Market Intelligence transforma señales observables del mercado en evidencia estructurada para evaluar oportunidades comerciales.

```text
MERCADO -> DATOS -> SEÑALES -> EVIDENCIA -> OPORTUNIDAD -> DECISIÓN
```

Debe responder:

> ¿Existe evidencia suficiente para considerar este producto una oportunidad comercial que merezca pasar a la siguiente etapa?

No debe afirmar ventas, conversión o rentabilidad cuando esos datos no estén disponibles.

## 2. Señales disponibles

### Demand

- Trends / keywords
- Best Seller / posición
- Visits acumuladas
- Visits por ventana temporal
- Momentum de Visits
- Actividad de Reviews

### Product

- Catalog Product
- Category
- Domain
- Family
- Attributes
- Variantes / parent / children
- Buy box y otros datos disponibles

### Competition

- Listings
- Unique sellers
- Prices
- Price distribution
- Seller concentration
- Traffic concentration

### Reputation

- Rating average
- Stars
- Rating distribution
- Review count
- Review dates
- Buying dates
- Review content
- Likes / dislikes
- Media

## 3. Flujos de descubrimiento

### Trends

```text
TRENDS -> KEYWORD -> PRODUCT SEARCH -> PRODUCT
```

Trends es una señal de interés dentro de la plataforma, no una cifra de ventas.

### Best Seller

```text
BEST SELLER -> CATEGORY -> POSITION + PRODUCT_ID -> PRODUCT
```

Se validó `/highlights/MLC/category/{CATEGORY_ID}` con 20 productos de tipo `PRODUCT`.

### Catalog

```text
/products/{PRODUCT_ID}
```

Entrega datos completos del producto, incluyendo nombre, dominio, familia, atributos, imágenes, variantes, parent/children, buy box y otros campos.

### Listings

```text
/products/{PRODUCT_ID}/items
```

Permite obtener múltiples publicaciones asociadas al producto, con `item_id`, `seller_id`, precio, listing type y otros datos.

## 4. Sellers

El listing entrega `seller_id`. Se validó información básica de vendedor como:

- seller_id
- nickname
- country
- permalink

Debe distinguirse:

```text
listing_count
```

de:

```text
unique_seller_count
```

Ejemplo validado:

```text
LISTINGS = 57
SELLERS = 56
```

## 5. Price Intelligence

Se pueden derivar:

- precio mínimo;
- máximo;
- promedio;
- dispersión;
- diferencia mínimo/máximo;
- posición relativa;
- concentración por rangos.

Ejemplo validado:

```text
PRODUCT = MLC18622311
PRICE_MIN = 24990
PRICE_MAX = 43990
PRICE_AVG = 38290
```

El precio no implica margen. El margen requiere datos de costos.

## 6. Visits Intelligence

### Acumuladas

```text
/visits/items?ids={ITEM_ID}
```

Se confirmó que funciona para listings de terceros.

Restricción:

```text
máximo 1 item por consulta
```

Una consulta con múltiples IDs devuelve HTTP 400:

```text
maximum amount of items to query is 1
```

### Serie temporal

```text
/items/{ITEM_ID}/visits/time_window?last={LAST}&unit=day
```

Se validaron ventanas de 7 y 30 días y series diarias.

Se pueden derivar:

```text
visit_total
visit_window_total
visit_average_observed_day
observed_days
coverage_ratio
recent_visit_volume
visit_momentum
visit_acceleration
visit_deceleration
visit_concentration
```

Regla:

```text
VISITS != SALES
```

### Momentum

Para `MLC1059998914`:

```text
13-19 agosto: 3012 visitas / 430,3 día
20-26 agosto: 2306 visitas / 329,4 día
variación: aproximadamente -23,4%
```

El 27 de agosto fue un día incompleto y se excluyó de la comparación.

El sistema debe distinguir entre días calendario y días observados.

## 7. Reviews Intelligence

Endpoint validado:

```text
/reviews/item/{ITEM_ID}
```

Respuesta observada:

```text
paging
reviews
helpful_reviews
quali_attributes
rating_average
stars
rating_levels
cross_site_enabled
user_product_id
```

### Resumen validado

Para `MLC2022490177`:

```text
rating_average = 4.4
stars = 4.5

one_star   = 37
two_star   = 11
three_star = 19
four_star  = 53
five_star  = 307
```

La suma es 427 y coincide con:

```text
paging.total = 427
```

### Review individual

Campos observados:

```text
id
reviewable_object
date_created
status
title
content
rate
valorization
likes
dislikes
buying_date
relevance
forbidden_words
attributes
media
reactions
attributes_variation
translations
secondary_key
order_id
catalog_listing
earned_rewards
```

Esto permite analizar rating, contenido, fechas, engagement, media y actividad.

### Paginación

Se confirmó:

```text
limit = 5
offset = 0
```

y:

```text
offset = 5
```

devuelven reviews diferentes.

Ejemplo:

```text
offset 0:
2650266166
2835376309
2416324953
2648307264
3004143182

offset 5:
2456078392
2165241570
3060540170
3020790714
2979010922
```

También se observó:

```text
total = 427
total_pageable = 290
```

La diferencia semántica queda pendiente de documentación; no deben tratarse como sinónimos.

### Secondary key

Las reviews pueden contener `secondary_key`, por ejemplo:

```text
2650266166 -> MLC47773363
2835376309 -> MLC49736170
```

No se debe atribuir automáticamente cada review a un listing concreto únicamente por el endpoint utilizado.

## 8. Sold Quantity — restricción

Se probaron:

```text
sold_quantity
id,sold_quantity
id,initial_quantity,available_quantity,sold_quantity
```

para listings de terceros.

Resultado:

```text
HTTP 403
```

con mensajes de acceso restringido.

Conclusión:

```text
SOLD_QUANTITY
```

no puede ser una dependencia del Product Hunter para medir directamente ventas de competidores.

No se seguirá buscando indefinidamente variantes del mismo campo salvo que aparezca una necesidad concreta y una fuente legítima.

## 9. GAP Analysis

| Necesidad | Fuente / dato | Estado |
|---|---|---|
| Detectar tendencias | Trends | COMPLETO |
| Detectar popularidad | Best Seller | COMPLETO |
| Buscar productos | Product Search | COMPLETO |
| Resolver producto | Catalog | COMPLETO |
| Categoría / dominio | Category / Domain | COMPLETO |
| Listings | Product Items | COMPLETO |
| Sellers únicos | seller_id | COMPLETO |
| Precios | Listings | COMPLETO |
| Tráfico acumulado | Visits | COMPLETO |
| Tráfico reciente | Visits Time Window | COMPLETO |
| Momentum | Series Visits | COMPLETO |
| Reputación | Reviews | COMPLETO |
| Rating distribution | Reviews | COMPLETO |
| Actividad de reviews | Review dates | COMPLETO |
| Ventas exactas de terceros | sold_quantity | NO DISPONIBLE |
| Conversión | visitas -> compras | NO DISPONIBLE |
| Costo proveedor | Supplier Intelligence | PENDIENTE |
| Costo envío | Economics | PENDIENTE |
| Comisiones | Economics | PENDIENTE |
| Publicidad | Economics | PENDIENTE |
| Impuestos | Economics | PENDIENTE |
| Margen | Profit Engine | PENDIENTE |
| Riesgo | Risk Engine | PENDIENTE |
| Opportunity Score | Opportunity Engine | PENDIENTE |

## 10. Gaps reales

### GAP 1 — Ventas exactas

No disponibles para terceros mediante el acceso probado.

No bloquea el MVP de Market Intelligence.

### GAP 2 — Conversión

No podemos observar directamente:

```text
visitas -> compras
```

Por tanto:

```text
conversion_rate = UNKNOWN
```

No debe presentarse como dato.

### GAP 3 — Economía

Para determinar rentabilidad necesitamos:

```text
supplier_cost
shipping_cost
marketplace_fees
advertising_cost
taxes
other_costs
```

Estos pertenecen a capas posteriores.

### GAP 4 — Riesgo

Posteriormente debemos formalizar:

```text
product_lifecycle
trend_decay
competition_growth
traffic_decay
review_quality
obsolescence
```

### GAP 5 — Decisión

Todavía no existen:

```text
Opportunity Score
Profit Score
Risk Score
Confidence Score
```

Estos pertenecen al Opportunity Engine.

## 11. Qué puede afirmar Market Intelligence

Puede producir evidencia como:

- el producto aparece en Best Seller en una posición determinada;
- aparece en Trends;
- pertenece a determinada categoría;
- tiene N listings;
- tiene N vendedores únicos;
- existe un rango de precios determinado;
- determinado listing concentra una proporción del tráfico;
- recibe X visitas en una ventana;
- el tráfico aumenta o disminuye;
- tiene determinado rating;
- tiene determinado número y distribución de reviews;
- existe actividad reciente de reviews.

## 12. Qué NO puede afirmar

Sin fuentes adicionales no debe afirmar:

```text
vende X unidades
factura X pesos
convierte X%
genera X de margen
es rentable
el vendedor gana X
```

Regla:

```text
OBSERVED DATA != BUSINESS FACT
```

Las inferencias deben estar marcadas como derivadas o estimadas y acompañadas por confianza.

## 13. Modelo de señales

Cada señal futura debería conservar:

```text
Signal
├── name
├── value
├── source
├── observed_at
├── time_window
├── coverage
└── confidence
```

Ejemplo:

```text
visit_momentum
value = -0.234
source = MercadoLibre Visits
window = 14d
coverage = complete_days_only
confidence = high
```

## 14. Estados de evidencia

Debe distinguirse:

```text
OBSERVED
DERIVED
ESTIMATED
```

Ejemplos:

```text
price_min       -> OBSERVED
visit_momentum  -> DERIVED
sales_estimate  -> ESTIMATED
```

Una estimación nunca debe presentarse como dato observado.

## 15. Separación de responsabilidades

### Market Intelligence

```text
DESCUBRIR
OBSERVAR
NORMALIZAR
COMPARAR
DERIVAR SEÑALES
GENERAR EVIDENCIA
```

### Opportunity Engine

```text
EVALUAR
PUNTUAR
PRIORIZAR
CALCULAR CONFIANZA
DECIDIR
```

### Profit Engine

```text
COSTOS
MARGEN
ROI
ECONOMICS
```

### Supplier Hunter

```text
PROVEEDORES
COSTOS
STOCK
MOQ
LOGÍSTICA
RIESGO DEL PROVEEDOR
```

No se deben mezclar estas responsabilidades.

## 16. Pipeline objetivo

```text
                 DISCOVERY
                    |
          +---------+---------+
          |                   |
        TRENDS            BEST SELLER
          |                   |
       KEYWORDS            PRODUCT IDS
          |                   |
     PRODUCT SEARCH          PRODUCT
          |                   |
          +---------+---------+
                    |
              CATALOG PRODUCT
                    |
                 LISTINGS
                    |
        +-----------+-----------+
        |           |           |
     SELLERS      PRICE       VISITS
                                |
                           TIME SERIES
                                |
                            MOMENTUM
                                |
                                v
                             REVIEWS
                                |
                                v
                       MARKET EVIDENCE
                                |
                                v
                       OPPORTUNITY INPUT
```

## 17. Decisión de alcance

El API Discovery de Mercado Libre queda **CONGELADO para esta fase**.

No se continuará buscando endpoints de manera indiscriminada.

Una nueva investigación sólo se justifica si:

1. aparece un gap que bloquee una funcionalidad concreta;
2. existe una necesidad explícita del diseño;
3. aparece evidencia contradictoria;
4. se necesita validar una hipótesis específica.

Regla:

> No buscar datos porque puedan existir. Buscar datos porque una decisión del sistema los necesita.

## 18. Próximo entregable

El siguiente documento es:

`MARKET_INTELLIGENCE_SPEC.md`

Debe formalizar:

```text
ENTITIES
SIGNALS
VALUE OBJECTS
DATA CONTRACTS
USE CASES
SERVICES
PERSISTENCE REQUIREMENTS
CONFIDENCE MODEL
OUTPUT CONTRACT
```

Después:

```text
SPEC
  ↓
DOMAIN MODEL
  ↓
APPLICATION SERVICES
  ↓
DATA SOURCES
  ↓
PERSISTENCE
  ↓
TESTS
  ↓
MARKET INTELLIGENCE MVP
```

## 19. Decisión final

El discovery realizado es suficiente para comenzar la construcción del Market Intelligence MVP.

No se requiere conocer las ventas exactas de terceros para empezar.

El sistema debe utilizar evidencia multidimensional:

```text
TRENDS
+
BEST SELLER
+
PRODUCT
+
LISTINGS
+
SELLERS
+
PRICE
+
VISITS
+
MOMENTUM
+
REVIEWS
=
MARKET EVIDENCE
```

Posteriormente:

```text
MARKET EVIDENCE
+
SUPPLIER DATA
+
ECONOMICS
+
RISK
=
OPPORTUNITY DECISION
```

La ausencia de `sold_quantity` se considera una restricción conocida del entorno, no un bloqueo arquitectónico.
