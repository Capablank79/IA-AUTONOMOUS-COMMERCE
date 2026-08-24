# Project Command: /status

## Objetivo
Crear un punto de entrada de solo lectura para obtener el estado consolidado de AI-AUTONOMOUS-COMMERCE.

## Command Scope
Project Scope

## Comportamiento
Este comando genera un diagnóstico técnico exhaustivo y estructurado del estado actual del proyecto, verificando tanto la memoria del proyecto como el estado real del repositorio y la suite de tests.

## Instrucciones de Ejecución

### 1. Verificación de Entorno (Rule 32)
- El agente **DEBE** verificar la existencia del intérprete en: `.venv\Scripts\python.exe`.
- Si el entorno no está disponible, el agente **DEBE DETENERSE** e informar el problema según la Rule 32.

### 2. Recolección de Información
El agente debe realizar las siguientes acciones en orden:
1. **Leer Project Memory**: Consultar `project_memory.md` para obtener el contexto persistente y roadmap.
2. **Inspeccionar Repositorio**: Verificar la estructura de archivos y el estado real de la arquitectura (Hexagonal).
3. **Identificar Migration Units**: Localizar el registro de unidades completadas, en progreso y planificadas.
4. **Estado de Profit Vertical**: Verificar la implementación de `ProfitEngine` y `AnalyzeProfitUseCase`.
5. **Identificar MCP Tools**: Listar las herramientas disponibles en `mcp/commerce_lab/server.py`.
6. **Ejecutar Tests**: Correr la suite completa con `.venv\Scripts\python.exe -m pytest tests/`.

### 3. Categorías de Reporte Obligatorias
El reporte **DEBE** utilizar exactamente estas categorías:

#### ## IMPLEMENTADO
Solo incluye componentes cuya existencia física pueda verificarse mediante inspección directa del código en el repositorio.

#### ## VALIDADO
Solo incluye componentes cuya ejecución y corrección lógica haya sido demostrada mediante tests exitosos o evidencia de ejecución real. Para la vertical Profit, utilizar el término: "equivalencia exacta del comportamiento observable para EXP-001".

#### ## EN PROGRESO
Solo incluye trabajo que presente evidencia concreta de implementación parcial (archivos v2, ramas activas o código inacabado) en el repositorio. La mención en Project Memory no es suficiente por sí sola para clasificar una tarea en esta categoría.

#### ## PLANIFICADO
Incluye elementos del roadmap y objetivos definidos que todavía no tienen una implementación física verificable en el repositorio.

#### ## VISIÓN FUTURA
Capacidades pertenecientes al objetivo final del ecosistema de comercio autónomo.

### 4. Gestión de Discrepancias y Veracidad
- **DISCREPANCIA DETECTADA**: Si existe contradicción entre lo registrado en Project Memory y la realidad del repositorio, el agente debe indicarlo explícitamente con esta etiqueta y explicar la diferencia.
- **Evidencia**: No utilizar expresiones de estado (implementado, validado, en progreso) sin aportar o haber verificado la evidencia física correspondiente.
- **Rol de Memory**: Project Memory aporta contexto y roadmap, pero NO puede convertir por sí sola una capacidad planificada en "EN PROGRESO" o "IMPLEMENTADO" sin respaldo en el código.

### 5. Contenido Mínimo del Reporte
El reporte final debe incluir:
- **Estado de la arquitectura**: Descripción de la implementación hexagonal verificada.
- **Estado de la vertical Profit**: Situación real de la lógica financiera.
- **Última Migration Unit completada**: Identificación del hito más reciente.
- **Resumen de Tests**: total / passed / failed / errors (resultados reales).
- **MCP tools disponibles**: Lista de herramientas expuestas en el código.
- **Rules / Skills / Commands relevantes**: Activos presentes en `.trae/`.
- **Próximos hitos**: Tareas inmediatas del roadmap.

## Restricciones
- **SOLO LECTURA**: Prohibido modificar cualquier archivo del proyecto (excepto la propia definición del comando si se solicita).
- **INTEGRIDAD**: No modificar código, tests, JSON, Rules, Project Memory o Skills.
- **ENTORNO**: No instalar dependencias ni iniciar Migration Units.
- **VERACIDAD**: No asumir información no verificable.
- **LIMITACIÓN**: El comando se detiene tras generar el reporte.
