# Mercado Libre API Discovery — Market Intelligence

**Proyecto:** AI Autonomous Commerce  
**Área:** Market Intelligence / Product Hunter  
**Estado:** Discovery experimental  
**Fecha:** 2026-08-26

## 1. Objetivo

Consolidar los hallazgos obtenidos durante la exploración de la API de Mercado Libre para construir una fuente de inteligencia de mercado.

Esta fase busca identificar recursos accesibles, relaciones entre `item`, `catalog_product`, `parent`, `reviews` y variantes, restricciones HTTP y posibles rutas alternativas.

Los hallazgos se clasifican como:

- **CONFIRMADO:** observado directamente.
- **INFERIDO:** interpretación respaldada por varias observaciones.
- **NO CONFIRMADO:** requiere documentación oficial o nuevos experimentos.

---

## 2. Access Matrix

| Recurso | Resultado observado | Estado |
|---|---:|---|
| `/products/{catalog_product_id}` | HTTP 200 | CONFIRMADO |
| `/items/{item_id}` | HTTP 403 `access_denied` | CONFIRMADO |
| `/items/{item_id}?include_attributes=true` | HTTP 403 `access_denied` | CONFIRMADO |
| `/items/{item_id}/description` | HTTP 200 | CONFIRMADO |
| `/reviews/item/{item_id}` | HTTP 200 | CONFIRMADO |
| `/reviews/item/{item_id}/seller_custom_field` | HTTP 404 | CONFIRMADO |
| `/reviews/item/{item_id}/stock` | HTTP 404 | CONFIRMADO |
| `/reviews/item/{item_id}/visits` sin fechas válidas | HTTP 400 | CONFIRMADO |
| `/sites/MLC/search` | Obsoleto/restringido durante las pruebas | OBSERVADO |
| Secuencias de múltiples requests | HTTP 429 `too_many_requests` | CONFIRMADO |

---

## 3. Catalog Product

`/products/{catalog_product_id}` responde para objetos `catalog_product`.

Ejemplos:

```text
MLC47773363
type   = catalog_product
status = active
domain = MLC-VACUUM_AND_STEAM_CLEANERS
family = Bosich CXJ-MT10 150 mL
parent = MLC47773362
```

```text
MLC49736170
type   = catalog_product
status = active
domain = MLC-VACUUM_AND_STEAM_CLEANERS
family = Bosich CXJ-MT10 150 mL
parent = MLC47773362
```

**Estado: CONFIRMADO.**

---

## 4. Parent Product

Se consultó:

```text
/products/MLC47773362
```

Resultado:

```text
ID=MLC47773362
TYPE=catalog_product
STATUS=inactive
PARENT=None
FAMILY=Aspiradora industrial inalámbrica De mano Bosich CXJ-MT10 150ml
CHILDREN_COUNT=2
CHILDREN=
    MLC47773363
    MLC49736170
```

Estructura observada:

```text
MLC47773362
PARENT
├── MLC47773363
└── MLC49736170
```

**Estado: CONFIRMADO.**

---

## 5. Attributes

Los dos catalog products comparten:

```text
BRAND = Bosich
MODEL = CXJ-MT10
```

y presentan:

```text
MLC47773363 → COLOR = Azul
MLC49736170 → COLOR = Celeste
```

También se observó:

```text
VOLTAGE = 21V
```

**Estado: CONFIRMADO.**

---

## 6. Pickers y variantes

`/products/{catalog_product_id}` contiene `pickers`.

Cada picker observado posee:

```text
picker_id
picker_name
products
tags
attributes
value_name_delimiter
hide_extra_decoration
secondary_picker_name
```

### Picker COLOR

Se observó:

```text
MLC47773363 → Azul
MLC49736170 → Celeste
```

Los elementos de `products[]` pueden contener:

```text
product_id
picker_label
picture_id
thumbnail
tags
changes
permalink
product_name
auto_completed
rgb_meta
```

Ejemplos:

```text
MLC47773363
picker_label = Azul
product_name = Bosich CXJ-MT10 150 mL - Azul - 21V
```

```text
MLC49736170
picker_label = Celeste
product_name = Bosich CXJ-MT10 150 mL - Celeste - 21V
changes = [["COLOR", "Celeste"]]
auto_completed = True
```

### Picker VOLTAGE

```text
MLC47773363 → 21V
MLC49736170 → 21V
```

**Estado:** existencia y estructura de `pickers` CONFIRMADAS.

La interpretación funcional como mecanismo de navegación de variantes está fuertemente respaldada por la evidencia, pero no debe tratarse todavía como definición contractual oficial.

---

## 7. Items / Listings

Para:

```text
MLC47773363
```

se observaron **9 items**:

```text
MLC2022490177
MLC1595736047
MLC3439996320
MLC1844232991
MLC4182778130
MLC4021118842
MLC3571361550
MLC4328818456
MLC3516325280
```

Para:

```text
MLC49736170
```

se observaron **19 items**:

```text
MLC1857331199
MLC1857202071
MLC1913416703
MLC3726610610
MLC3951896286
MLC4176390636
MLC4275277928
MLC3440255844
MLC1849846485
MLC3419746892
MLC4254991486
MLC4108982634
MLC4282252330
MLC1841806051
MLC2042174861
MLC4060629724
MLC2066614437
MLC3243311948
MLC3516501612
```

La asociación de estos IDs con los catálogos fue observada durante los experimentos.

**Importante:** todavía no se debe afirmar que estas listas sean exhaustivas en todos los casos.

---

## 8. Restricción de `/items/{item_id}`

Se probó:

```text
/items/MLC2022490177
```

Resultado:

```text
HTTP 403
error = access_denied
```

También:

```text
/items/MLC2022490177?include_attributes=true
```

→ `HTTP 403`.

**Estado: CONFIRMADO.**

La integración no debe depender exclusivamente de `/items/{item_id}` para listings de terceros.

---

## 9. `/items/{item_id}/description`

Se comprobó:

```text
/items/MLC2022490177/description
```

→ `HTTP 200`.

La respuesta permitió obtener descripción y especificaciones técnicas, incluyendo:

```text
Potencia nominal: 200W
Capacidad de batería: 4.0Ah
Resistencia: 30 a 50 minutos
Tiempo de carga: 120 minutos
Vacío máximo teórico: 9800PA
Velocidad nominal: 35000 rpm
```

También se obtuvieron funciones, accesorios, usos y compatibilidad.

**Estado: CONFIRMADO.**

---

## 10. Reviews

Se probó:

```text
/reviews/item/MLC1595736047
```

con paginación:

```text
offset=0
offset=50
offset=100
offset=150
offset=200
offset=250
```

Resultado observado:

```text
TOTAL = 425
TOTAL PAGEABLE = 289
REVIEWS RECUPERADAS = 289
```

**Estado: CONFIRMADO.**

---

## 11. reviewable_object

Las reviews contienen `reviewable_object`.

Se observaron múltiples IDs, incluyendo:

```text
MLC3439996320 → 125
MLC1595736047 → 67
MLC1849846485 → 19
MLC1844232991 → 18
MLC3951896286 → 16
MLC3571361550 → 15
MLC1857202071 → 12
MLC2022490177 → 8
MLC1857331199 → 3
MLC4021118842 → 3
MLC1932611455 → 2
MLC3440255844 → 1
```

**Estado:** campo confirmado; semántica exacta todavía no confirmada contractualmente.

---

## 12. secondary_key

Las reviews contienen `secondary_key`.

En la muestra analizada:

```text
MLC47773363 → 236
MLC49736170 → 53
```

Casos concretos:

```text
REVIEW=2650266166
OBJECT=MLC3439996320
SECONDARY=MLC47773363
```

```text
REVIEW=2835376309
OBJECT=MLC1857202071
SECONDARY=MLC49736170
```

Los valores observados corresponden a `catalog_product_id` válidos.

### Interpretación actual

La evidencia indica que `secondary_key` puede contener el `catalog_product_id` asociado con el contexto de una review.

**No confirmado todavía:** que esta sea la definición oficial/contractual de `secondary_key`.

---

## 13. Comportamiento del endpoint de Reviews

Se compararon:

```text
/reviews/item/MLC2022490177
/reviews/item/MLC3439996320
```

Ambos devolvieron:

```text
2650266166
2835376309
2416324953
2648307264
3004143182
```

También:

```text
/reviews/item/MLC2022490177
/reviews/item/MLC1857202071
```

devolvió exactamente el mismo conjunto inicial.

Por lo tanto:

> `/reviews/item/{item_id}` no debe interpretarse automáticamente como "reviews exclusivas del item solicitado".

Puede existir un contexto compartido o una resolución a una entidad superior.

**Estado: comportamiento CONFIRMADO; semántica interna NO CONFIRMADA.**

---

## 14. Relación observada

Caso estudiado:

```text
                         MLC47773362
                            PARENT
                              │
                 ┌────────────┴────────────┐
                 │                         │
                 ▼                         ▼
           MLC47773363               MLC49736170
           CATALOG PRODUCT            CATALOG PRODUCT
           COLOR=Azul                COLOR=Celeste
                 │                         │
              9 ITEMS                  19 ITEMS
                 │                         │
                 └────────────┬────────────┘
                              │
                           REVIEWS
                              │
                   ┌──────────┴──────────┐
                   ▼                     ▼
            reviewable_object      secondary_key
                   │                     │
                   ▼                     ▼
                 ITEM              CATALOG PRODUCT
```

Este es un **modelo de trabajo basado en observaciones**, no una representación contractual de la arquitectura interna de Mercado Libre.

---

## 15. Errores observados

### HTTP 403

```json
{
  "message": "Access to the requested resource is forbidden",
  "error": "access_denied",
  "status": 403
}
```

Observado en `/items/{item_id}`.

### HTTP 404

Observado en:

```text
/items/{item_id}/seller_custom_field
/items/{item_id}/stock
```

También se obtuvieron 404 al consultar determinados identificadores que no correspondían al tipo de recurso esperado.

### HTTP 400

En el recurso de visitas, con formato de fechas inválido:

```text
Invalid request unknown date format
```

### HTTP 429

Durante secuencias de múltiples requests:

```text
too_many_requests
```

### Implicación

Los scripts deben incorporar:

- rate limiting;
- backoff;
- manejo explícito de 403;
- manejo explícito de 404;
- manejo explícito de 429;
- validación del tipo de ID;
- salida resumida.

---

## 16. Hallazgos confirmados

- `/products/{catalog_product_id}` responde en los casos probados.
- Existen objetos `catalog_product`.
- Los catalog products pueden tener `parent_id`.
- Un parent puede contener `children_ids`.
- `attributes` contiene atributos del producto.
- `pickers` contiene relaciones entre productos.
- `pickers.products[]` puede incluir `product_id` y `picker_label`.
- Un picker `COLOR` puede relacionar varios catalog products.
- `/items/{item_id}` puede devolver 403.
- `/items/{item_id}/description` puede devolver 200.
- `/reviews/item/{item_id}` puede devolver reviews.
- Las reviews contienen `reviewable_object`.
- Las reviews contienen `secondary_key`.
- `secondary_key` puede contener un `catalog_product_id` en los casos analizados.
- Diferentes items pueden devolver el mismo conjunto inicial de reviews.
- Se producen respuestas 429 bajo suficiente volumen.

---

## 17. Inferencias actuales

Estas hipótesis están respaldadas por las pruebas, pero no deben tratarse todavía como contrato oficial:

### `secondary_key`

Probablemente identifica un `catalog_product` asociado con el contexto de la review.

### `reviewable_object`

Probablemente identifica un objeto concreto relacionado con la review, frecuentemente un item.

### Reviews compartidas

El endpoint parece resolver a un contexto de reviews que puede ser compartido por múltiples listings relacionados.

### Parent + Pickers

La combinación `parent_id` + `pickers` parece modelar una familia de variantes de catálogo.

---

## 18. Preguntas abiertas

1. ¿Cuál es la definición oficial de `secondary_key`?
2. ¿Cuál es la definición oficial de `reviewable_object`?
3. ¿Por qué `/reviews/item/{item_id}` puede devolver el mismo review set para diferentes items?
4. ¿Cuál es exactamente la relación entre `parent_id` y `pickers`?
5. ¿La enumeración de listings es completa?
6. ¿Existe un endpoint oficial documentado para obtener todas las publicaciones de un `catalog_product`?
7. ¿Qué información comercial de listings puede obtenerse sin `/items/{id}`?
8. ¿Qué recursos permiten obtener precio, seller, stock, ventas y logística de terceros?
9. ¿Qué límites de rate deben respetarse?
10. ¿Qué campos son estables y aptos para persistencia?

---

## 19. Implicaciones para AI Autonomous Commerce

Market Intelligence no debe modelarse como un simple scraper.

Modelo conceptual:

```text
ProductFamily
    │
    ▼
ParentProduct
    │
    ├── CatalogProduct
    │      ├── Attributes
    │      ├── Pickers / Variants
    │      └── Listings
    │
    └── CatalogProduct
           ├── Attributes
           ├── Pickers / Variants
           └── Listings

Listing
    │
    └── Reviews / Signals
```

Esta separación permite distinguir:

- producto/familia;
- variante de catálogo;
- publicación/listing;
- seller;
- observaciones comerciales;
- reviews;
- señales de mercado.

---

## 20. Próxima línea de investigación

La siguiente fase debe centrarse en la **inteligencia comercial de listings**, no en seguir profundizando indefinidamente en variantes.

Objetivo:

```text
CATALOG PRODUCT
       │
       ▼
LISTINGS
       │
       ├── PRICE
       ├── SELLER
       ├── STOCK
       ├── SALES
       ├── CONDITION
       ├── SHIPPING / LOGISTICS
       └── STATUS
```

La prioridad será identificar qué información de terceros puede obtenerse de forma estable, manteniendo separadas las rutas experimentales de la integración productiva.

---

## 21. Regla para futuros experimentos

Cada experimento debe:

1. probar una hipótesis concreta;
2. realizar el mínimo número de requests;
3. limitar la salida de consola;
4. evitar repetir descubrimientos confirmados;
5. registrar 403/404/429 sin detener todo el discovery;
6. distinguir `item_id`, `catalog_product_id` y `parent_id`;
7. registrar la relación descubierta;
8. actualizar este documento cuando un hallazgo sea suficientemente sólido.

---

## 22. Estado del Discovery

```text
CATALOG STRUCTURE        ████████████████████  Alta comprensión
VARIANTS / PICKERS       ████████████████████  Alta comprensión
REVIEWS                  ████████████████░░░░  Buena comprensión
SECONDARY_KEY            ████████████░░░░░░░░  Semántica pendiente
ITEM COMMERCIAL DATA     ██████████░░░░░░░░░░  En investigación
OFFICIAL CONTRACT        ████████░░░░░░░░░░░░  Pendiente
```

**Conclusión:** existe suficiente evidencia para modelar las relaciones principales de forma experimental, pero todavía no para declarar que todos los significados internos están oficialmente determinados.



## 14. Highlights — BEST_SELLER por categoría

Se descubrió que el endpoint:

```text
/highlights/MLC/category/{category_id}
```

es accesible y devuelve información de productos destacados para una categoría.

Para la categoría:

```text
MLC74192
```

la respuesta fue:

```text
HTTP = 200
KEYS = ['query_data', 'content']
content = 10 resultados
```

El objeto `query_data` permitió confirmar explícitamente la naturaleza de la consulta:

```text
highlight_type = BEST_SELLER
criteria       = CATEGORY
id             = MLC74192
```

Por lo tanto, la consulta corresponde a un ranking de tipo:

```text
BEST_SELLER
    ↓
CATEGORY
    ↓
MLC74192
```

Cada elemento de `content[]` contiene:

```text
id
position
type
```

El campo `type` puede representar diferentes entidades. En la consulta analizada se observaron:

```text
ITEM
PRODUCT
USER_PRODUCT
```

Ejemplo:

```text
POSITION=1 | ID=MLC1587988367 | TYPE=ITEM
POSITION=2 | ID=MLC49736170   | TYPE=PRODUCT
POSITION=9 | ID=MLCU1411773233 | TYPE=USER_PRODUCT
```

El hallazgo más relevante es:

```text
MLC49736170
TYPE     = PRODUCT
POSITION = 2
```

`MLC49736170` había sido identificado previamente como un `catalog_product` perteneciente a:

```text
parent = MLC47773362
family = Bosich CXJ-MT10 150 mL
```

Por lo tanto, se confirmó que un `catalog_product` puede aparecer directamente dentro de los resultados de `BEST_SELLER` de una categoría.

En la misma consulta:

```text
MLC47773363
```

no apareció entre los 10 resultados devueltos.

### Implicación para Demand Intelligence

Este recurso proporciona una señal de demanda/ranking independiente de `sold_quantity`.

Modelo observado:

```text
CATEGORY
    │
    ▼
BEST_SELLER
    │
    ├── POSITION
    ├── ID
    └── TYPE
          │
          ├── ITEM
          ├── PRODUCT
          └── USER_PRODUCT
```

En el caso analizado:

```text
MLC49736170
    ↓
BEST_SELLER
    ↓
CATEGORY = MLC74192
    ↓
POSITION = 2
    ↓
TYPE = PRODUCT
```

### Estado

**CONFIRMADO:** el endpoint responde con `highlight_type=BEST_SELLER`, `criteria=CATEGORY` y posiciones dentro de `content`.

**CONFIRMADO:** `content[]` puede contener elementos de tipo `ITEM`, `PRODUCT` y `USER_PRODUCT`.

**CONFIRMADO:** `MLC49736170` aparece como `PRODUCT` en posición 2 para la categoría `MLC74192`.

**NO CONFIRMADO:** que `position` represente directamente unidades vendidas o volumen absoluto de ventas. La interpretación segura en esta etapa es que representa la posición del objeto dentro del resultado `BEST_SELLER`.

**NO CONFIRMADO:** que la ausencia de `MLC47773363` en los 10 resultados implique que el producto no sea un best seller en términos absolutos; únicamente se observó que no apareció en los resultados devueltos por esta consulta.
