# Migration Unit Workflow

## 1. Understand

Antes de modificar archivos:

- identificar el objetivo exacto de la unidad;
- identificar el alcance;
- identificar los componentes involucrados;
- identificar dependencias con unidades anteriores;
- identificar archivos que deberían modificarse;
- identificar archivos que deben permanecer intactos.

No ampliar el alcance sin justificación.

## 2. Inspect

Inspeccionar primero el código existente relevante.

Determinar:

- comportamiento actual;
- interfaces existentes;
- dependencias;
- tests existentes;
- contratos;
- puntos de integración.

No asumir que una implementación existe solamente porque aparece en la visión o roadmap.

## 3. Plan

Formular internamente un plan mínimo antes de modificar.

El plan debe identificar:

- cambios necesarios;
- archivos afectados;
- tests necesarios;
- criterio de aceptación.

Evitar cambios arquitectónicos no requeridos por la unidad.

## 4. Implement

Realizar únicamente los cambios necesarios para cumplir la unidad.

Respetar la arquitectura existente:

Domain
→ Application
→ Infrastructure
→ Interfaces / MCP

Mantener las responsabilidades en su capa correspondiente.

## 5. Verify

Después de implementar:

- ejecutar los tests relevantes;
- ejecutar la suite completa cuando corresponda;
- comprobar imports;
- comprobar contratos;
- comprobar que no se introdujeron dependencias innecesarias;
- comprobar que los componentes protegidos permanecen intactos.

Utilizar el entorno de ejecución válido del proyecto.

## 6. Compare

Cuando la unidad sea una migración o refactorización:

- preservar el comportamiento existente cuando ese sea el objetivo;
- comparar explícitamente comportamiento anterior y nuevo;
- identificar diferencias;
- no ocultar diferencias mediante modificación de snapshots o tests.

## 7. Evidence

El resultado debe basarse en evidencia:

- tests;
- ejecución real;
- inspección de archivos;
- diffs;
- resultados observables.

No declarar éxito solamente porque el código parece correcto.

## 8. Report

Al finalizar entregar:

1. Estado de la unidad.
2. Objetivo cumplido.
3. Componentes afectados.
4. Archivos creados.
5. Archivos modificados.
6. Archivos protegidos.
7. Tests ejecutados.
8. Passed / Failed / Errors.
9. Diferencias encontradas.
10. Riesgos o deuda técnica descubierta.
11. Próximo paso recomendado.

## 9. Stop Boundary

Una Migration Unit termina cuando se cumple su objetivo y su evidencia de validación está disponible.

NO comenzar automáticamente la siguiente Migration Unit.

Después del informe, detenerse y esperar instrucciones.

## PRINCIPIOS

- Una unidad = un objetivo concreto.
- Primero inspeccionar, después modificar.
- Cambios mínimos.
- Evidencia antes de declarar éxito.
- No esconder regresiones.
- No ampliar alcance.
- No encadenar automáticamente unidades posteriores.
- Las Rules tienen prioridad sobre esta Skill.
- Project Memory proporciona contexto; esta Skill proporciona procedimiento.
