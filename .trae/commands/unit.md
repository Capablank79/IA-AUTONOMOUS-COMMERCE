# /unit — Controlled Migration Unit Execution

Ejecuta una Migration Unit completa del proyecto
AI-AUTONOMOUS-COMMERCE utilizando la Project Skill `migration-unit`,
las Project Rules y la Project Memory como fuentes de contexto.

==================================================
OBJETIVO
==================================================

Convertir una especificación de Migration Unit aprobada en una
implementación completa, validada y cerrada, evitando el desarrollo
fragmentado en micro-tareas y evitando ciclos innecesarios de revisión.

El objetivo no es simplemente escribir código.

El objetivo es:

ANALIZAR
→ PLANIFICAR
→ IMPLEMENTAR
→ VALIDAR
→ CORREGIR
→ VALIDAR NUEVAMENTE
→ AUDITAR
→ CERRAR LA UNIDAD

==================================================
REGLAS FUNDAMENTALES
==================================================

1. LEER CONTEXTO ANTES DE ACTUAR

Antes de modificar cualquier archivo:

- leer las Project Rules aplicables;
- utilizar la Project Skill `migration-unit`;
- revisar Project Memory;
- inspeccionar el estado físico actual del repositorio;
- identificar la Migration Unit objetivo y su especificación.

No asumir que Memory, Rules o informes anteriores representan
necesariamente el estado físico actual del código.

La fuente de verdad para el estado implementado es el repositorio.

==================================================
2. NO HACER MICRO-ITERACIONES
==================================================

No detenerse después de crear cada archivo.

No solicitar autorización entre componentes que forman parte de la
misma unidad.

No realizar una secuencia artificial como:

crear A → detenerse
crear B → detenerse
corregir A → detenerse
crear C → detenerse

En su lugar:

1. analizar la unidad completa;
2. identificar todas sus piezas;
3. detectar incompatibilidades;
4. definir el plan completo;
5. implementar la unidad;
6. validar;
7. corregir;
8. volver a validar;
9. cerrar.

==================================================
3. REVISIÓN INTEGRAL INICIAL
==================================================

Antes de implementar, realizar UNA revisión integral.

Determinar:

- qué existe;
- qué falta;
- qué debe crearse;
- qué debe modificarse;
- qué debe permanecer protegido;
- dependencias existentes;
- arquitectura afectada;
- tests existentes;
- posibles conflictos con Rules;
- posibles conflictos con Project Memory;
- riesgos técnicos.

No convertir esta revisión en una nueva fase de discovery si la
especificación de la unidad ya está congelada.

Si la especificación es suficiente para implementar, continuar.

==================================================
4. DECISIONES PENDIENTES
==================================================

Si existe una decisión técnica menor que pueda resolverse de forma
consistente con:

- Project Rules;
- arquitectura existente;
- especificación de la unidad;
- principio de cambios mínimos;
- progresive complexity;

resolverla directamente.

NO detenerse para consultar al usuario por decisiones locales menores.

Detenerse únicamente si aparece una decisión que:

- cambia el boundary arquitectónico;
- cambia una decisión previamente congelada;
- introduce una tecnología importante;
- modifica una regla de negocio crítica;
- puede comprometer la visión del proyecto;
- contradice explícitamente una Project Rule;
- requiere credenciales o acceso que no existen.

En esos casos:

DETENERSE
→ explicar el conflicto
→ presentar las alternativas
→ solicitar decisión.

==================================================
5. IMPLEMENTACIÓN
==================================================

Implementar todas las piezas necesarias de la Migration Unit actual.

Respetar:

Domain
→ Application
→ Infrastructure
→ Interface

según corresponda.

Mantener Dependency Inversion.

No introducir dependencias innecesarias.

No mover lógica de negocio a MCP.

No colocar lógica financiera o decisiones comerciales críticas dentro
de agentes o herramientas MCP.

No duplicar lógica existente.

No modificar componentes protegidos salvo que la especificación
requiera explícitamente una modificación.

==================================================
6. ENTORNO
==================================================

Utilizar SIEMPRE el entorno oficial:

.venv\Scripts\python.exe

Nunca asumir que:

python
python3
pytest

corresponden al entorno correcto.

Antes de ejecutar tests comprobar que existe:

.venv\Scripts\python.exe

Ejecutar pytest mediante:

.venv\Scripts\python.exe -m pytest

Si el entorno oficial no existe, está corrupto o no puede ejecutarse:

DETENERSE.

No instalar automáticamente dependencias.

No cambiar de intérprete para intentar ocultar el problema.

Respetar Rule 32.

==================================================
7. TESTING
==================================================

Después de implementar:

1. ejecutar los tests específicos de la unidad;
2. ejecutar la suite completa;
3. analizar cualquier fallo;
4. corregir únicamente los problemas relacionados con la unidad;
5. volver a ejecutar los tests.

No declarar éxito únicamente porque el código compile.

La unidad solo puede considerarse completa cuando:

- tests de la unidad pasan;
- regresiones existentes pasan;
- no existen errores de importación;
- no existen errores de entorno;
- las dependencias entre capas son correctas.

==================================================
8. LOOP CONTROLADO
==================================================

Se permite un máximo de 3 ciclos de:

TEST
→ DIAGNÓSTICO
→ CORRECCIÓN
→ TEST

El loop NO es infinito.

Si después de 3 ciclos continúan fallos:

DETENERSE.

Presentar:

- fallo;
- causa probable;
- correcciones realizadas;
- evidencia;
- motivo por el cual no se continúa automáticamente.

==================================================
9. RULE 32 / ENVIRONMENT FAILURE
==================================================

Un fallo del entorno no debe tratarse como un fallo de código.

Ejemplos:

- Python incorrecto;
- .venv inexistente;
- pytest no disponible;
- MCP no disponible;
- dependencia requerida ausente;
- acceso externo bloqueado;
- sandbox bloqueado;
- credenciales inexistentes;
- herramienta requerida no disponible.

En estos casos:

DETENERSE.

No crear mocks artificiales para ocultar un fallo del entorno.

No instalar herramientas para sortear la restricción.

No modificar configuración del sistema para continuar.

Reportar exactamente qué falló.

==================================================
10. CONTROL DE ALCANCE
==================================================

No avanzar automáticamente a la siguiente Migration Unit.

Ejemplo:

Si se completa Unit 4:

NO comenzar Unit 5.

La ejecución termina cuando la unidad actual está cerrada.

==================================================
11. ESTADO VS VISIÓN
==================================================

En todo momento distinguir:

IMPLEMENTADO
VALIDADO
EN PROGRESO
PLANIFICADO
VISIÓN FUTURA

Nunca declarar una capacidad como implementada solamente porque:

- aparece en Project Memory;
- aparece en un roadmap;
- aparece en un comentario;
- aparece en un informe anterior.

Debe existir evidencia física en el repositorio o evidencia de ejecución.

==================================================
12. CAMBIOS MÍNIMOS
==================================================

Antes de modificar un archivo existente:

- determinar por qué es necesario;
- comprobar si puede evitarse;
- preservar comportamiento existente cuando corresponda.

No realizar refactors oportunistas.

No "limpiar" código no relacionado con la unidad.

No introducir abstracciones futuras sin necesidad actual.

==================================================
13. COMPATIBILIDAD
==================================================

Cuando una unidad amplíe una funcionalidad existente:

- preservar contratos existentes;
- mantener compatibilidad cuando sea parte del objetivo;
- ejecutar regresión completa.

Si una modificación rompe un comportamiento existente:

no ocultar el fallo.

Determinar si la ruptura está autorizada por la especificación.

==================================================
14. AUDITORÍA FINAL
==================================================

Antes de declarar la unidad COMPLETADA verificar:

- archivos creados;
- archivos modificados;
- archivos protegidos;
- dependencias añadidas;
- arquitectura;
- imports entre capas;
- tests;
- regresiones;
- configuración;
- credenciales;
- deuda técnica;
- alcance;
- evidencia real.

Comparar el resultado contra la especificación inicial de la unidad.

==================================================
15. ACTUALIZACIÓN DE TODO / ESTADO
==================================================

Si existe TodoWrite disponible:

- marcar como completadas las tareas realmente terminadas;
- mantener abiertas las tareas no terminadas;
- no marcar como completado algo que solo esté planificado.

==================================================
16. REPORTE FINAL
==================================================

Al cerrar la unidad entregar un único:

MIGRATION UNIT X REPORT

con:

1. Estado
2. Objetivo
3. Revisión inicial
4. Implementación
5. Domain
6. Application
7. Infrastructure
8. Interface/MCP si corresponde
9. Tests
10. Resultado de pytest
11. Archivos creados
12. Archivos modificados
13. Archivos protegidos
14. Dependencias
15. Decisiones técnicas
16. Problemas encontrados
17. Correcciones realizadas
18. Riesgos/deuda técnica
19. Evidencia
20. Estado real vs visión
21. Próximo paso recomendado

No presentar como implementado aquello que no tenga evidencia.

==================================================
CRITERIO FINAL DE CIERRE
==================================================

Una Migration Unit solo puede declararse:

COMPLETADA

cuando el objetivo de la unidad está implementado y validado,
los tests relevantes pasan, las regresiones pasan y no quedan
fallos conocidos dentro del alcance de la unidad.

Si existe una limitación externa que impide validar una parte,
debe declararse explícitamente como:

COMPLETADA PARCIALMENTE / BLOQUEADA POR ENTORNO

y no como COMPLETADA.

Al finalizar:

DETENERSE.

No avanzar automáticamente a la siguiente unidad.