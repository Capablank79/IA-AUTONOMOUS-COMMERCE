# Project Command: /test

## Objetivo
Proporcionar un punto de entrada rápido para ejecutar y verificar la suite de pruebas de AI-AUTONOMOUS-COMMERCE.

## Command Scope
Project Scope

## Comportamiento
Este comando ejecuta la suite completa de pruebas utilizando el entorno virtual oficial del proyecto.

## Instrucciones de Ejecución

### 1. Verificación de Entorno (Rule 32)
- El agente **DEBE** verificar la existencia del intérprete en: `.venv\Scripts\python.exe`.
- El agente **DEBE** verificar que `pytest` está disponible ejecutando `.venv\Scripts\python.exe -m pytest --version`.
- Si cualquiera de estas condiciones falla, el agente **DEBE DETENERSE** e informar exactamente el problema, respetando la Rule 32.
- **PROHIBIDO**: Crear mocks, stubs o reemplazos del entorno.

### 2. Ejecución de Pruebas
- Ejecutar el comando: `.venv\Scripts\python.exe -m pytest tests/`
- **RESTRICCIÓN**: No instalar, actualizar ni eliminar ninguna dependencia.

### 3. Análisis de Resultados
- El agente debe capturar y analizar la salida real de pytest.

### 4. Reporte Obligatorio
El agente debe informar los siguientes datos:
- **Total de tests**: Número total de pruebas ejecutadas.
- **Passed**: Número de pruebas exitosas.
- **Failed**: Número de pruebas fallidas.
- **Errors**: Número de errores (setup/teardown).
- **Duración**: Tiempo total de ejecución.
- **Archivos con fallos**: Lista de archivos donde se detectaron fallos, si existen.

### 5. Restricciones Finales
- **NO** realizar correcciones automáticas de código o tests.
- **NO** ejecutar ninguna otra tarea después de informar los resultados.
- **NO** modificar código, tests, snapshots, JSON, Rules, Project Memory o Skills.
