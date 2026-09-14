"""
Definición canónica del Catálogo de Business KPIs para Hito Q.6.

Establece la matriz de KPIs:
- OPPORTUNITY_COUNT
- HIGH_POTENTIAL_OPPORTUNITIES
- AVG_OPPORTUNITY_SCORE
- VALIDATED_SUPPLIER_COUNT
- AVG_SUPPLIER_SCORE
- COMPLETE_PROFITABILITY_COUNT
- AVG_CONTRIBUTION_MARGIN
- NEGATIVE_MARGIN_COUNT
- MISSION_SUCCESS_RATE
- ACTIVE_MISSIONS
- FAILED_MISSIONS
- TOTAL_AGENT_COST
- AVG_COST_PER_MISSION
- COST_PER_OPPORTUNITY (Cross-domain)
- PROJECTED_CONTRIBUTION_TO_AGENT_COST_RATIO (Cross-domain)
"""

from typing import Dict, List, Tuple
from .models import (
    BusinessKPICatalogItem,
    KPIDomain,
    KPIUnit,
)

KPI_CATALOG: Tuple[BusinessKPICatalogItem, ...] = (
    # --- DOMINIO: OPPORTUNITY (Q.1) ---
    BusinessKPICatalogItem(
        kpi_id="OPPORTUNITY_COUNT",
        name="Total Oportunidades Detectadas",
        domain=KPIDomain.OPPORTUNITY,
        unit=KPIUnit.COUNT,
        source="Q.1 Opportunity Dashboard / TenantOpportunityRepositoryPort",
        formula_version="1.0.0",
        formula_description="Conteo de registros de oportunidades no eliminadas en el tenant y ventana de tiempo",
        required_inputs=("opportunity_records",),
        unknown_behavior_description="Si el repositorio está vacío retorna 0 (conteo matemático exacto)",
        is_core=True,
    ),
    BusinessKPICatalogItem(
        kpi_id="HIGH_POTENTIAL_OPPORTUNITIES",
        name="Oportunidades de Alto Potencial",
        domain=KPIDomain.OPPORTUNITY,
        unit=KPIUnit.COUNT,
        source="Q.1 Opportunity Dashboard",
        formula_version="1.0.0",
        formula_description="Conteo de oportunidades con score >= 0.70 o clasificación HIGH",
        required_inputs=("opportunity_records", "scores"),
        unknown_behavior_description="Retorna 0 si no hay oportunidades evaluadas",
        is_core=True,
    ),
    BusinessKPICatalogItem(
        kpi_id="AVG_OPPORTUNITY_SCORE",
        name="Puntaje Promedio de Oportunidades",
        domain=KPIDomain.OPPORTUNITY,
        unit=KPIUnit.SCORE,
        source="Q.1 Opportunity Dashboard",
        formula_version="1.0.0",
        formula_description="Suma de scores de oportunidades / total de oportunidades con score conocido",
        required_inputs=("opportunity_scores",),
        unknown_behavior_description="UNKNOWN si no hay oportunidades con score conocido",
        is_core=True,
    ),

    # --- DOMINIO: SUPPLIER (Q.2) ---
    BusinessKPICatalogItem(
        kpi_id="VALIDATED_SUPPLIER_COUNT",
        name="Proveedores Verificados",
        domain=KPIDomain.SUPPLIER,
        unit=KPIUnit.COUNT,
        source="Q.2 Supplier Dashboard / TenantSupplierRepositoryPort",
        formula_version="1.0.0",
        formula_description="Conteo de proveedores con estado VERIFIED o ACTIVE y sin bandera de riesgo crítico",
        required_inputs=("supplier_records", "verification_status"),
        unknown_behavior_description="Retorna 0 si no hay proveedores verificados en el tenant",
        is_core=True,
    ),
    BusinessKPICatalogItem(
        kpi_id="AVG_SUPPLIER_SCORE",
        name="Puntaje Promedio de Proveedores",
        domain=KPIDomain.SUPPLIER,
        unit=KPIUnit.SCORE,
        source="Q.2 Supplier Dashboard",
        formula_version="1.0.0",
        formula_description="Suma de supplier_score / total de proveedores con score conocido",
        required_inputs=("supplier_scores",),
        unknown_behavior_description="UNKNOWN si no hay proveedores con score asignado",
        is_core=True,
    ),
    BusinessKPICatalogItem(
        kpi_id="SUPPLIER_VERIFICATION_RATE",
        name="Tasa de Verificación de Proveedores",
        domain=KPIDomain.SUPPLIER,
        unit=KPIUnit.PERCENTAGE,
        source="Q.2 Supplier Dashboard",
        formula_version="1.0.0",
        formula_description="(Proveedores verificados / Total de proveedores) * 100",
        required_inputs=("total_suppliers", "verified_suppliers"),
        unknown_behavior_description="UNKNOWN si total_suppliers es 0",
        is_core=False,
    ),

    # --- DOMINIO: PROFITABILITY (Q.3) ---
    BusinessKPICatalogItem(
        kpi_id="COMPLETE_PROFITABILITY_COUNT",
        name="Productos con Rentabilidad Completa",
        domain=KPIDomain.PROFIT,
        unit=KPIUnit.COUNT,
        source="Q.3 Profit Dashboard / TenantProfitRepositoryPort",
        formula_version="1.0.0",
        formula_description="Conteo de ítems financieros con estado de completitud COMPLETE",
        required_inputs=("profit_records", "completeness_status"),
        unknown_behavior_description="Retorna 0 si no hay ítems completos",
        is_core=True,
    ),
    BusinessKPICatalogItem(
        kpi_id="AVG_CONTRIBUTION_MARGIN",
        name="Margen de Contribución Promedio (%)",
        domain=KPIDomain.PROFIT,
        unit=KPIUnit.PERCENTAGE,
        source="Q.3 Profit Dashboard",
        formula_version="1.0.0",
        formula_description="Suma de margen de contribución % de registros completos / conteo de registros completos",
        required_inputs=("complete_profit_records", "contribution_margins"),
        unknown_behavior_description="UNKNOWN si no hay registros completos con margen calculable",
        is_core=True,
    ),
    BusinessKPICatalogItem(
        kpi_id="NEGATIVE_MARGIN_COUNT",
        name="Ítems con Margen Negativo",
        domain=KPIDomain.PROFIT,
        unit=KPIUnit.COUNT,
        source="Q.3 Profit Dashboard",
        formula_version="1.0.0",
        formula_description="Conteo de ítems evaluados cuyo margen de contribución proyectado es < 0",
        required_inputs=("profit_records", "contribution_margins"),
        unknown_behavior_description="Retorna 0 si no hay ítems evaluados con margen negativo",
        is_core=True,
    ),

    # --- DOMINIO: MISSIONS (Q.4) ---
    BusinessKPICatalogItem(
        kpi_id="ACTIVE_MISSIONS",
        name="Misiones Activas / En Ejecución",
        domain=KPIDomain.MISSION,
        unit=KPIUnit.COUNT,
        source="Q.4 Mission Dashboard / TenantMissionRepositoryPort",
        formula_version="1.0.0",
        formula_description="Conteo de misiones con status RUNNING, PENDING o IN_PROGRESS",
        required_inputs=("mission_records", "mission_status"),
        unknown_behavior_description="Retorna 0 si no hay misiones activas",
        is_core=True,
    ),
    BusinessKPICatalogItem(
        kpi_id="FAILED_MISSIONS",
        name="Misiones Fallidas",
        domain=KPIDomain.MISSION,
        unit=KPIUnit.COUNT,
        source="Q.4 Mission Dashboard",
        formula_version="1.0.0",
        formula_description="Conteo de misiones terminales con status FAILED, ABORTED o ERROR",
        required_inputs=("mission_records", "mission_status"),
        unknown_behavior_description="Retorna 0 si no hay misiones fallidas",
        is_core=True,
    ),
    BusinessKPICatalogItem(
        kpi_id="MISSION_SUCCESS_RATE",
        name="Tasa de Éxito de Misiones Terminales",
        domain=KPIDomain.MISSION,
        unit=KPIUnit.PERCENTAGE,
        source="Q.4 Mission Dashboard",
        formula_version="1.0.0",
        formula_description="(Misiones COMPLETED / (Misiones COMPLETED + FAILED/ABORTED)) * 100",
        required_inputs=("terminal_missions",),
        unknown_behavior_description="UNKNOWN si no hay misiones terminales (las misiones en RUNNING/PENDING no son denominador)",
        is_core=True,
    ),

    # --- DOMINIO: AGENT COST (Q.5) ---
    BusinessKPICatalogItem(
        kpi_id="TOTAL_AGENT_COST",
        name="Costo Total de Inferencia y Agentes IA",
        domain=KPIDomain.AGENT_COST,
        unit=KPIUnit.CURRENCY,
        source="Q.5 Agent Cost Dashboard / Cost & Usage Repositories",
        formula_version="1.0.0",
        formula_description="Suma de costos reales conocidos en Decimal, desglosados por divisa",
        required_inputs=("cost_records", "usage_events", "currency"),
        unknown_behavior_description="UNKNOWN si no hay ningún registro de costo con importe conocido",
        is_core=True,
    ),
    BusinessKPICatalogItem(
        kpi_id="AVG_COST_PER_MISSION",
        name="Costo Promedio por Misión Atribuida",
        domain=KPIDomain.AGENT_COST,
        unit=KPIUnit.CURRENCY,
        source="Q.5 Agent Cost Dashboard",
        formula_version="1.0.0",
        formula_description="Costo total atribuido a misiones / Total de misiones con costo registrado",
        required_inputs=("mission_attributed_costs", "mission_count"),
        unknown_behavior_description="UNKNOWN si no hay misiones con costo conocido atribuido",
        is_core=True,
    ),

    # --- DOMINIO: CROSS-DOMAIN ---
    BusinessKPICatalogItem(
        kpi_id="COST_PER_OPPORTUNITY",
        name="Costo de IA por Oportunidad Detectada",
        domain=KPIDomain.CROSS_DOMAIN,
        unit=KPIUnit.CURRENCY,
        source="Cross-Domain Q.1 + Q.5",
        formula_version="1.0.0",
        formula_description="Costo de IA de misiones de descubrimiento / Total de oportunidades detectadas por dichas misiones",
        required_inputs=("discovery_ai_cost", "opportunities_detected"),
        unknown_behavior_description="UNKNOWN si no hay misiones de descubrimiento con costo conocido o total de oportunidades es 0",
        is_core=False,
    ),
    BusinessKPICatalogItem(
        kpi_id="PROJECTED_CONTRIBUTION_TO_AGENT_COST_RATIO",
        name="Ratio de Contribución Proyectada a Costo IA",
        domain=KPIDomain.CROSS_DOMAIN,
        unit=KPIUnit.RATIO,
        source="Cross-Domain Q.3 + Q.5",
        formula_version="1.0.0",
        formula_description="Ganancia proyectada total (de ítems comparables en misma divisa) / Costo total de IA en misma divisa",
        required_inputs=("projected_contribution_total", "total_agent_cost", "matching_currency"),
        unknown_behavior_description="UNKNOWN si divisas no coinciden, falta margen completo o costo IA es 0/desconocido",
        is_core=False,
    ),

    # --- DOMINIO: DATA QUALITY / COMPLETENESS ---
    BusinessKPICatalogItem(
        kpi_id="OVERALL_DATA_READINESS",
        name="Índice de Madurez y Completitud de Datos (%)",
        domain=KPIDomain.DATA_QUALITY,
        unit=KPIUnit.PERCENTAGE,
        source="Cross-Domain Readiness Assessment",
        formula_version="1.0.0",
        formula_description="(KPIs calculados con éxito / Total de KPIs aplicables) * 100",
        required_inputs=("all_kpi_statuses",),
        unknown_behavior_description="Calculado sobre los estados de los KPIs evaluados",
        is_core=True,
    ),
)


def get_catalog_map() -> Dict[str, BusinessKPICatalogItem]:
    """Retorna un mapeo kpi_id -> BusinessKPICatalogItem."""
    return {k.kpi_id: k for k in KPI_CATALOG}
