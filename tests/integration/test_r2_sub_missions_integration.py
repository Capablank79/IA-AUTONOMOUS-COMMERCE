"""
Suite de Pruebas de Integración para R.2 — Sub-missions & Hierarchical Delegation (Hito R: Advanced Autonomy).

Cubre exhaustivamente los escenarios canónicos de integración (A - J) y el E2E sintético (K):
A. Jerarquía Padre + Dos Hijos (Parent + two children hierarchy, depth & linkage invariants).
B. Ejecución en Runtime Existente (Children execute through existing AutonomousLoop & ActionExecutor with no duplicate executor).
C. Progreso del Padre Condicionado a Hijos Requeridos (Required children completed allows parent to progress / complete).
D. Mapeo de Fallo Técnico a Replan Acotado R.1 (Technical failure maps to bounded R.1 replan path without child spawn loops).
E. Hijo Denegado por Política Bloqueado sin Bypass (Policy-denied child maps to POLICY_DENIED and blocks replanning).
F. Agotamiento de Presupuesto Seguro / Bloqueado (Budget exhaustion safe failure and sibling budget partitioning).
G. Cancelación en Cascada del Padre (Parent cancellation cascades to active children while completed history remains immutable).
H. Aislamiento Estricto Multi-Tenant (Tenant A / Tenant B strict isolation via CrossTenantGuard).
I. Delegación Duplicada Concurrente Idempotente (Concurrent duplicate delegation yields a single logical child).
J. Trazabilidad y Auditoría Jerárquica Completa (K.1 Audit trail and K.2 Agent trace hierarchy).
K. Flujo E2E Sintético Determinista (End-to-End lifecycle: Parent -> Plan R.1 -> Delegated Sub-missions -> Runtime execution -> Results propagation -> Controlled failure & replan -> Completion).
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
import tempfile
import shutil
import pytest
from typing import Dict, Any, List, Optional, Sequence

from src.domain.tenant.models import TenantContext, CrossTenantAccessError
from src.domain.tenant.guard import CrossTenantGuard
from src.domain.security.models import validate_safe_identifier
from src.domain.audit.models import AuditRecord, AuditRecordType
from src.domain.audit.ports import AuditRepositoryPort
from src.domain.agent_trace.models import AgentTraceRecord, StepType, TraceStatus
from src.domain.agent_trace.ports import AgentTraceRepositoryPort
from src.domain.emergency_stop.models import (
    EmergencyStopEvaluationContext,
)
from src.domain.emergency_stop.ports import EmergencyStopServicePort
from src.domain.mission.models import (
    Mission,
    MissionType,
    MissionStatus,
    MissionPriority,
    LoopDecision,
    LoopState,
    LoopAction,
)
from src.domain.mission.ports import DecisionProvider, ActionExecutor
from src.application.mission.autonomous_loop import AutonomousLoop
from src.domain.planning.models import (
    ExecutionPlan,
    PlanStep,
    PlanBudget,
    PlanStatus,
    StepStatus,
    StepFailureType,
)
from src.domain.planning.ports import (
    ExecutionPlanRepositoryPort,
    CapabilityRegistryPort,
)
from src.application.planning.multi_step_planning_service import MultiStepPlanningService
from src.domain.sub_mission.models import (
    SubMissionScope,
    SubMissionCreationContract,
    SubMissionResultContract,
    SubMissionFailureType,
    SubMissionHierarchyPolicy,
    SubMissionNode,
)
from src.domain.sub_mission.validator import (
    SubMissionHierarchyValidator,
    SubMissionHierarchyError,
    MaxDepthExceededError,
    MaxChildrenExceededError,
    BudgetExceededError,
    TenantMismatchError,
    InvalidParentMissionError,
)
from src.application.sub_mission.service import SubMissionService
from src.infrastructure.persistence.data.json.tenant_mission_repository import JsonTenantMissionRepository
from src.infrastructure.persistence.data.json.execution_plan_repository import InMemoryExecutionPlanRepository
from src.application.policy.policy_guarded_action_executor import PolicyGuardedActionExecutor
from src.application.policy.policy_enforcement_service import PolicyEnforcementService


# ==================================================================================================
# MOCKS / ADAPTERS LOCALES DETERMINISTAS
# ==================================================================================================

class MockCapabilityRegistry(CapabilityRegistryPort):
    def __init__(self, available_capabilities: Dict[str, bool]):
        self._capabilities = dict(available_capabilities)

    def is_capability_available(self, capability_name: str) -> bool:
        return self._capabilities.get(capability_name, False)

    def get_available_capabilities(self) -> Sequence[str]:
        return [k for k, v in self._capabilities.items() if v]

    def get_known_capabilities(self) -> Sequence[str]:
        return list(self._capabilities.keys())


class MockAuditRepository(AuditRepositoryPort):
    def __init__(self):
        self.records: List[AuditRecord] = []

    def append(self, record: AuditRecord) -> AuditRecord:
        self.records.append(record)
        return record

    def get_by_id(self, audit_id: str) -> Optional[AuditRecord]:
        for r in self.records:
            if r.audit_id == audit_id:
                return r
        return None

    def get_by_idempotency_key(self, idempotency_key: str) -> Optional[AuditRecord]:
        for r in self.records:
            if r.idempotency_key == idempotency_key:
                return r
        return None

    def list_records(
        self,
        mission_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        subject_type: Optional[str] = None,
        subject_id: Optional[str] = None,
        record_type: Optional[AuditRecordType] = None,
        from_time: Optional[datetime] = None,
        to_time: Optional[datetime] = None,
        limit: int = 1000,
    ) -> List[AuditRecord]:
        return [
            r for r in self.records
            if (mission_id is None or r.mission_id == mission_id)
            and (record_type is None or r.record_type == record_type)
        ]

    def reconstruct_mission_timeline(self, mission_id: str):
        return None

    def query(self, *args, **kwargs) -> List[AuditRecord]:
        return list(self.records)


class MockAgentTraceRepository(AgentTraceRepositoryPort):
    def __init__(self):
        self.records: List[AgentTraceRecord] = []

    def append(self, record: AgentTraceRecord) -> AgentTraceRecord:
        self.records.append(record)
        return record

    def get_by_id(self, trace_id: str) -> Optional[AgentTraceRecord]:
        for r in self.records:
            if r.trace_id == trace_id:
                return r
        return None

    def get_by_idempotency_key(self, idempotency_key: str) -> Optional[AgentTraceRecord]:
        for r in self.records:
            if r.idempotency_key == idempotency_key:
                return r
        return None

    def list_records(
        self,
        execution_id: Optional[str] = None,
        mission_id: Optional[str] = None,
        cycle_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        component_name: Optional[str] = None,
        step_type: Optional[StepType] = None,
        status: Optional[TraceStatus] = None,
        from_time: Optional[datetime] = None,
        to_time: Optional[datetime] = None,
        limit: int = 1000,
    ) -> List[AgentTraceRecord]:
        return [r for r in self.records if (execution_id is None or r.execution_id == execution_id)]

    def get_execution_timeline(self, execution_id: str):
        return None

    def list_by_execution_id(self, execution_id: str) -> List[AgentTraceRecord]:
        return [r for r in self.records if r.execution_id == execution_id]


class MockEmergencyStop(EmergencyStopServicePort):
    def __init__(self, blocked: bool = False, reason: str = ""):
        self.blocked = blocked
        self.reason = reason

    def activate_stop(self, *args, **kwargs):
        pass

    def deactivate_stop(self, *args, **kwargs):
        pass

    def evaluate(self, context: EmergencyStopEvaluationContext):
        return type("StopDecision", (), {"is_blocked": self.blocked, "reason": self.reason})()


class ScriptedDecisionProvider(DecisionProvider):
    def __init__(self, decisions: List[LoopDecision]):
        self.decisions = decisions

    def decide(self, state: LoopState) -> LoopDecision:
        idx = state.iteration
        if idx < len(self.decisions):
            return self.decisions[idx]
        return LoopDecision(action=LoopAction.COMPLETE, reason="No more scripted decisions")


class RecordingActionExecutor(ActionExecutor):
    def __init__(self, responses: Optional[Dict[str, Any]] = None, raise_on_action: Optional[LoopAction] = None):
        self.executed_decisions: List[LoopDecision] = []
        self.responses = responses or {}
        self.raise_on_action = raise_on_action

    def execute(self, decision: LoopDecision, state: LoopState) -> Dict[str, Any]:
        self.executed_decisions.append(decision)
        if self.raise_on_action and decision.action == self.raise_on_action:
            raise RuntimeError(f"Simulated technical execution failure for action {decision.action}")
        action_key = decision.action.value
        if action_key in self.responses:
            return self.responses[action_key]
        return {"status": "executed", "action": decision.action.value, "target": decision.target}


# ==================================================================================================
# FIXTURES
# ==================================================================================================

@pytest.fixture
def tenant_alpha():
    return TenantContext(tenant_id="tenant-alpha", identity_id="user-alpha")


@pytest.fixture
def tenant_beta():
    return TenantContext(tenant_id="tenant-beta", identity_id="user-beta")


@pytest.fixture
def temp_dir():
    d = tempfile.mkdtemp(prefix="r2_integration_test_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def mission_repo(temp_dir):
    return JsonTenantMissionRepository(base_storage_dir=temp_dir)


@pytest.fixture
def plan_repo():
    return InMemoryExecutionPlanRepository()


@pytest.fixture
def audit_repo():
    return MockAuditRepository()


@pytest.fixture
def trace_repo():
    return MockAgentTraceRepository()


@pytest.fixture
def capability_registry():
    return MockCapabilityRegistry({
        "MARKET_RESEARCH": True,
        "SUPPLIER_ANALYSIS": True,
        "LISTING_GENERATION": True,
        "PRICE_OPTIMIZATION": True,
        "STOCK_ALLOCATION": True,
        "UNSUPPORTED_CAPABILITY": False,
    })


@pytest.fixture
def emergency_stop():
    return MockEmergencyStop(blocked=False)


@pytest.fixture
def sub_mission_service(mission_repo, plan_repo, audit_repo, trace_repo, emergency_stop):
    return SubMissionService(
        mission_repository=mission_repo,
        sub_mission_repository=mission_repo,
        plan_repository=plan_repo,
        audit_repository=audit_repo,
        trace_repository=trace_repo,
        emergency_stop_service=emergency_stop,
        hierarchy_policy=SubMissionHierarchyPolicy(
            max_depth=3,
            max_children_per_parent=10,
            max_total_descendants=50,
        ),
    )


@pytest.fixture
def planning_service(plan_repo, capability_registry, audit_repo, trace_repo):
    return MultiStepPlanningService(
        plan_repository=plan_repo,
        capability_registry=capability_registry,
        audit_repository=audit_repo,
        trace_repository=trace_repo,
    )


# ==================================================================================================
# ESCENARIO A: Jerarquía Padre + Dos Hijos
# ==================================================================================================
def test_scenario_a_parent_two_children_hierarchy(sub_mission_service, mission_repo, tenant_alpha):
    """
    Verifica la creación jerárquica padre + 2 hijos con vinculación parent/root y profundidad correcta.
    """
    parent = Mission(
        mission_id="m_root_a",
        type=MissionType.MARKET_DISCOVERY,
        status=MissionStatus.RUNNING,
        parameters={"budget": {"max_tokens": 10000, "max_cost": "10.0"}},
    )
    mission_repo.save(tenant_alpha, parent)

    # Hijo 1 (directo bajo padre: depth=1)
    contract_c1 = SubMissionCreationContract(
        parent_mission_id=parent.mission_id,
        tenant_id="tenant-alpha",
        sub_mission_type=MissionType.MARKET_DISCOVERY,
        scope=SubMissionScope(
            objective="Analizar nicho de calzado deportivo",
            expected_outcome="Informe de competidores y precios",
            completion_condition="Datos de competidores consolidados",
        ),
        allocated_budget=PlanBudget(max_tokens=4000, max_cost=Decimal("4.0")),
    )
    child_1 = sub_mission_service.create_sub_mission(tenant_alpha, contract_c1)
    assert child_1.parent_mission_id == parent.mission_id
    assert child_1.root_mission_id == parent.mission_id
    assert child_1.depth == 1

    # Hijo 2 (bajo Hijo 1: depth=2)
    contract_c2 = SubMissionCreationContract(
        parent_mission_id=child_1.mission_id,
        tenant_id="tenant-alpha",
        sub_mission_type=MissionType.SUPPLIER_SEARCH,
        scope=SubMissionScope(
            objective="Descubrir fabricantes de calzado en Brasil",
            expected_outcome="Lista de 3 proveedores validados",
            completion_condition="Cotizaciones obtenidas",
        ),
        allocated_budget=PlanBudget(max_tokens=2000, max_cost=Decimal("2.0")),
    )
    child_2 = sub_mission_service.create_sub_mission(tenant_alpha, contract_c2)
    assert child_2.parent_mission_id == child_1.mission_id
    assert child_2.root_mission_id == parent.mission_id
    assert child_2.depth == 2

    # Validar árbol jerárquico
    tree = sub_mission_service.get_hierarchy_tree(tenant_alpha, parent.mission_id)
    assert tree is not None
    assert tree.mission.mission_id == parent.mission_id
    assert len(tree.children) == 1
    assert tree.children[0].mission.mission_id == child_1.mission_id
    assert len(tree.children[0].children) == 1
    assert tree.children[0].children[0].mission.mission_id == child_2.mission_id


# ==================================================================================================
# ESCENARIO B: Hijos Ejecutados en el Runtime Existente (Sin Executor Duplicado)
# ==================================================================================================
def test_scenario_b_children_execute_through_existing_runtime(sub_mission_service, mission_repo, tenant_alpha):
    """
    Verifica que los hijos se ejecuten a través de AutonomousLoop + ActionExecutor existentes.
    """
    parent = Mission(
        mission_id="m_parent_b",
        type=MissionType.MARKET_DISCOVERY,
        status=MissionStatus.RUNNING,
        parameters={"budget": {"max_tokens": 5000, "max_cost": "5.0"}},
    )
    mission_repo.save(tenant_alpha, parent)

    contract = SubMissionCreationContract(
        parent_mission_id=parent.mission_id,
        tenant_id="tenant-alpha",
        sub_mission_type=MissionType.COMMERCIAL_PUBLICATION,
        scope=SubMissionScope(
            objective="Generar título y descripción SEO",
            expected_outcome="Listing SEO optimizado",
            completion_condition="Textos generados",
        ),
    )
    child = sub_mission_service.create_sub_mission(tenant_alpha, contract)

    # Configurar loop existente
    decisions = [
        LoopDecision(action=LoopAction.CONTINUE, reason="Analizando keywords"),
        LoopDecision(action=LoopAction.COMPLETE, reason="Listing generado con éxito"),
    ]
    provider = ScriptedDecisionProvider(decisions)
    executor = RecordingActionExecutor(responses={
        LoopAction.CONTINUE.value: {"status": "ok", "keywords": ["calzado", "running"]},
        LoopAction.COMPLETE.value: {"status": "completed", "seo_score": 95},
    })

    loop = AutonomousLoop(
        decision_provider=provider,
        action_executor=executor,
        max_iterations=5,
    )

    loop_result = loop.run(
        mission_id=child.mission_id,
        goal=child.parameters["objective"],
    )

    assert loop_result.status == "COMPLETED"
    assert len(executor.executed_decisions) == 1

    # Propagar resultado estructurado al servicio de submisiones
    result_contract = SubMissionResultContract(
        mission_id=child.mission_id,
        parent_mission_id=parent.mission_id,
        tenant_id="tenant-alpha",
        status=MissionStatus.COMPLETED,
        outputs={"seo_score": 95, "listing_title": "Zapatillas Pro Runner 2026"},
    )
    success = sub_mission_service.propagate_result(tenant_alpha, result_contract)
    assert success is True

    # Verificar que el estado del hijo se actualizó
    updated_child = mission_repo.get_by_id(tenant_alpha, child.mission_id)
    assert updated_child.status == MissionStatus.COMPLETED


# ==================================================================================================
# ESCENARIO C: Progreso del Padre Condicionado a Hijos Requeridos
# ==================================================================================================
def test_scenario_c_required_children_completed_allows_parent_progression(sub_mission_service, mission_repo, tenant_alpha):
    """
    Verifica que el padre solo pueda completarse cuando todos los hijos obligatorios terminen.
    """
    parent = Mission(
        mission_id="m_parent_c",
        type=MissionType.MARKET_DISCOVERY,
        status=MissionStatus.RUNNING,
        parameters={},
    )
    mission_repo.save(tenant_alpha, parent)

    # Hijo obligatorio
    contract_req = SubMissionCreationContract(
        parent_mission_id=parent.mission_id,
        tenant_id="tenant-alpha",
        sub_mission_type=MissionType.SUPPLIER_SEARCH,
        scope=SubMissionScope(
            objective="Buscar proveedores obligatorios",
            expected_outcome="Proveedores listados",
            completion_condition="Proveedores encontrados",
        ),
        is_required=True,
    )
    child_req = sub_mission_service.create_sub_mission(tenant_alpha, contract_req)

    # Hijo opcional
    contract_opt = SubMissionCreationContract(
        parent_mission_id=parent.mission_id,
        tenant_id="tenant-alpha",
        sub_mission_type=MissionType.COMMERCIAL_PUBLICATION,
        scope=SubMissionScope(
            objective="Optimizador opcional de imágenes",
            expected_outcome="Imágenes procesadas",
            completion_condition="Proceso finalizado",
        ),
        is_required=False,
    )
    child_opt = sub_mission_service.create_sub_mission(tenant_alpha, contract_opt)

    # Intentar completar el padre -> bloqueado por child_req
    can_complete, blockers = sub_mission_service.can_parent_complete(tenant_alpha, parent.mission_id)
    assert can_complete is False
    assert child_req.mission_id in blockers
    assert child_opt.mission_id not in blockers

    # Completar el hijo obligatorio
    res_req = SubMissionResultContract(
        mission_id=child_req.mission_id,
        parent_mission_id=parent.mission_id,
        tenant_id="tenant-alpha",
        status=MissionStatus.COMPLETED,
        outputs={"suppliers": ["Sup-1", "Sup-2"]},
    )
    sub_mission_service.propagate_result(tenant_alpha, res_req)

    # Ahora el padre sí puede completarse aunque el opcional esté PENDING
    can_complete, blockers = sub_mission_service.can_parent_complete(tenant_alpha, parent.mission_id)
    assert can_complete is True
    assert blockers == []


# ==================================================================================================
# ESCENARIO D: Fallo Técnico Mapea a Replan Acotado R.1 sin Bucle
# ==================================================================================================
def test_scenario_d_technical_failure_maps_to_r1_bounded_replan_without_spawn_loop(
    sub_mission_service, planning_service, plan_repo, mission_repo, tenant_alpha
):
    """
    Verifica que un fallo técnico en un hijo actualice el paso del plan a FAILED con
    StepFailureType.EXECUTION_FAILURE y permita un replan acotado R.1 preservando pasos completados.
    """
    parent = Mission(
        mission_id="m_parent_d",
        type=MissionType.MARKET_DISCOVERY,
        status=MissionStatus.RUNNING,
        parameters={"budget": {"max_tokens": 10000, "max_cost": "10.0"}},
    )
    mission_repo.save(tenant_alpha, parent)

    # Crear plan inicial con 3 pasos
    steps = [
        PlanStep(
            step_id="step-1",
            objective="Analizar mercado",
            action_type="MARKET_RESEARCH",
            assigned_capability="MARKET_RESEARCH",
            depth_level=1,
            estimated_cost=Decimal("1.0"),
        ),
        PlanStep(
            step_id="step-2",
            objective="Buscar proveedores",
            action_type="SUPPLIER_ANALYSIS",
            assigned_capability="SUPPLIER_ANALYSIS",
            dependencies=("step-1",),
            depth_level=1,
            estimated_cost=Decimal("2.0"),
        ),
        PlanStep(
            step_id="step-3",
            objective="Publicar catálogo",
            action_type="LISTING_GENERATION",
            assigned_capability="LISTING_GENERATION",
            dependencies=("step-2",),
            depth_level=1,
            estimated_cost=Decimal("1.0"),
        ),
    ]
    plan_res = planning_service.create_plan(
        tenant_context=tenant_alpha,
        mission_id=parent.mission_id,
        goal="Lanzar nuevo producto",
        initial_steps=steps,
        budget=PlanBudget(max_cost=Decimal("10.0"), max_tokens=10000, max_replans=2),
    )
    plan_id = plan_res.plan.plan_id

    # Completar step-1
    planning_service.mark_step_completed(
        plan_id=plan_id,
        step_id="step-1",
        outputs={"market_ok": True},
        tenant_context=tenant_alpha,
    )

    # Delegar step-2 a una submisión
    contract_c2 = SubMissionCreationContract(
        parent_mission_id=parent.mission_id,
        tenant_id="tenant-alpha",
        sub_mission_type=MissionType.SUPPLIER_SEARCH,
        scope=SubMissionScope(
            objective="Buscar proveedores",
            expected_outcome="Proveedores cotizados",
            completion_condition="Completado",
        ),
        plan_id=plan_id,
        plan_step_id="step-2",
    )
    child = sub_mission_service.create_sub_mission(tenant_alpha, contract_c2)

    # Simular fallo técnico en el runtime
    failure_result = SubMissionResultContract(
        mission_id=child.mission_id,
        parent_mission_id=parent.mission_id,
        tenant_id="tenant-alpha",
        status=MissionStatus.FAILED,
        failure_reason="Timeout de conexión con catálogo externo",
        failure_type=SubMissionFailureType.TECHNICAL_FAILURE,
    )
    sub_mission_service.propagate_result(tenant_alpha, failure_result)

    # Verificar que el paso en el plan quedó FAILED con EXECUTION_FAILURE
    plan = plan_repo.get_plan_by_id(plan_id, tenant_alpha)
    step2 = plan.get_step("step-2")
    assert step2.status == StepStatus.FAILED
    assert step2.failure_type == StepFailureType.EXECUTION_FAILURE

    # Replanificar con camino alternativo
    alt_steps = [
        PlanStep(
            step_id="step-2b",
            objective="Buscar proveedores mediante base de datos de respaldo",
            action_type="SUPPLIER_ANALYSIS",
            assigned_capability="SUPPLIER_ANALYSIS",
            dependencies=("step-1",),
            depth_level=1,
            estimated_cost=Decimal("2.5"),
        ),
        PlanStep(
            step_id="step-3",
            objective="Publicar catálogo",
            action_type="LISTING_GENERATION",
            assigned_capability="LISTING_GENERATION",
            dependencies=("step-2b",),
            depth_level=1,
            estimated_cost=Decimal("1.0"),
        ),
    ]
    replan_res = planning_service.replan(
        plan_id=plan_id,
        tenant_context=tenant_alpha,
        failed_step_id="step-2",
        failure_type=StepFailureType.EXECUTION_FAILURE,
        failure_reason="Timeout de red en primer proveedor",
        new_subgraph_steps=alt_steps,
    )

    assert replan_res.success is True
    assert replan_res.plan.version == 2
    assert replan_res.plan.replan_count == 1
    # step-1 sigue COMPLETED
    assert replan_res.plan.get_step("step-1").status == StepStatus.COMPLETED


# ==================================================================================================
# ESCENARIO E: Hijo Denegado por Política Bloqueado sin Bypass
# ==================================================================================================
def test_scenario_e_policy_denied_child_blocked_no_bypass(
    sub_mission_service, planning_service, plan_repo, mission_repo, tenant_alpha
):
    """
    Verifica que una submisión denegada por gobernanza de políticas (POLICY_DENIED)
    se propague como StepFailureType.POLICY_DENIED y bloquee el ExecutionPlan prohibiendo el bypass.
    """
    parent = Mission(
        mission_id="m_parent_e",
        type=MissionType.MARKET_DISCOVERY,
        status=MissionStatus.RUNNING,
        parameters={"budget": {"max_tokens": 8000, "max_cost": "8.0"}},
    )
    mission_repo.save(tenant_alpha, parent)

    plan_res = planning_service.create_plan(
        tenant_context=tenant_alpha,
        mission_id=parent.mission_id,
        goal="Acción regulada",
        initial_steps=[
            PlanStep(
                step_id="step-publish",
                objective="Publicar producto sin autorización",
                action_type="PROHIBITED_PUBLISH",
                assigned_capability="PRICE_OPTIMIZATION",
                depth_level=1,
            )
        ],
        budget=PlanBudget(max_cost=Decimal("5.0"), max_tokens=5000),
    )
    plan_id = plan_res.plan.plan_id

    contract = SubMissionCreationContract(
        parent_mission_id=parent.mission_id,
        tenant_id="tenant-alpha",
        sub_mission_type=MissionType.COMMERCIAL_PUBLICATION,
        scope=SubMissionScope(
            objective="Publicar precios no autorizados",
            expected_outcome="Precios actualizados",
            completion_condition="Publicación realizada",
        ),
        plan_id=plan_id,
        plan_step_id="step-publish",
    )
    child = sub_mission_service.create_sub_mission(tenant_alpha, contract)

    # Simular bloqueo de gobernanza
    result = SubMissionResultContract(
        mission_id=child.mission_id,
        parent_mission_id=parent.mission_id,
        tenant_id="tenant-alpha",
        status=MissionStatus.FAILED,
        failure_reason="Policy Engine DENY: Publicación prohibida para rol no verificado",
        failure_type=SubMissionFailureType.POLICY_DENIED,
    )
    sub_mission_service.propagate_result(tenant_alpha, result)

    plan = plan_repo.get_plan_by_id(plan_id, tenant_alpha)
    step = plan.get_step("step-publish")
    assert step.status == StepStatus.FAILED
    assert step.failure_type == StepFailureType.POLICY_DENIED

    # Intentar replanificar para evadir la política -> debe ser rechazado
    replan_res = planning_service.replan(
        plan_id=plan_id,
        tenant_context=tenant_alpha,
        failed_step_id="step-publish",
        failure_type=StepFailureType.POLICY_DENIED,
        failure_reason="Policy Engine DENY",
        new_subgraph_steps=[
            PlanStep(
                step_id="step-publish-bypass",
                objective="Publicar producto por canal alternativo",
                action_type="PROHIBITED_PUBLISH",
                assigned_capability="PRICE_OPTIMIZATION",
                depth_level=1,
            )
        ],
    )
    assert replan_res.success is False
    assert replan_res.status == PlanStatus.BLOCKED
    assert "Cannot bypass governance" in replan_res.errors[0]


# ==================================================================================================
# ESCENARIO F: Agotamiento de Presupuesto Seguro y Particionado
# ==================================================================================================
def test_scenario_f_budget_exhaustion_safe_failure(sub_mission_service, mission_repo, tenant_alpha):
    """
    Verifica que la asignación de presupuestos a hijos respete sum(child) <= parent_budget
    y que los presupuestos no definidos o que exceden el remanente sean rechazados de forma segura.
    """
    parent = Mission(
        mission_id="m_parent_f",
        type=MissionType.MARKET_DISCOVERY,
        status=MissionStatus.RUNNING,
        parameters={"budget": {"max_tokens": 5000, "max_cost": "5.0"}},
    )
    mission_repo.save(tenant_alpha, parent)

    # Hijo 1 consume 3000 tokens
    contract_1 = SubMissionCreationContract(
        parent_mission_id=parent.mission_id,
        tenant_id="tenant-alpha",
        sub_mission_type=MissionType.MARKET_DISCOVERY,
        scope=SubMissionScope(
            objective="Analizar competidores",
            expected_outcome="Informe listo",
            completion_condition="Completado",
        ),
        allocated_budget=PlanBudget(max_tokens=3000, max_cost=Decimal("3.0")),
    )
    sub_mission_service.create_sub_mission(tenant_alpha, contract_1)

    # Hijo 2 intenta pedir 3000 tokens (3000 + 3000 = 6000 > 5000) -> BudgetExceededError
    contract_2 = SubMissionCreationContract(
        parent_mission_id=parent.mission_id,
        tenant_id="tenant-alpha",
        sub_mission_type=MissionType.SUPPLIER_SEARCH,
        scope=SubMissionScope(
            objective="Buscar proveedores",
            expected_outcome="Proveedores listos",
            completion_condition="Completado",
        ),
        allocated_budget=PlanBudget(max_tokens=3000, max_cost=Decimal("3.0")),
    )
    with pytest.raises(BudgetExceededError):
        sub_mission_service.create_sub_mission(tenant_alpha, contract_2)


# ==================================================================================================
# ESCENARIO G: Cancelación en Cascada del Padre
# ==================================================================================================
def test_scenario_g_parent_cancellation_cascades_active_children_completed_immutable(
    sub_mission_service, mission_repo, tenant_alpha
):
    """
    Verifica que la cancelación del padre ponga a todos los hijos activos en ABORTED,
    mientras que los hijos ya COMPLETED permanecen inmutables.
    """
    parent = Mission(
        mission_id="m_parent_g",
        type=MissionType.MARKET_DISCOVERY,
        status=MissionStatus.RUNNING,
        parameters={},
    )
    mission_repo.save(tenant_alpha, parent)

    # Hijo 1: Se completa antes de la cancelación
    c1 = sub_mission_service.create_sub_mission(
        tenant_alpha,
        SubMissionCreationContract(
            parent_mission_id=parent.mission_id,
            tenant_id="tenant-alpha",
            sub_mission_type=MissionType.MARKET_DISCOVERY,
            scope=SubMissionScope(objective="Hijo 1", expected_outcome="O1", completion_condition="C1"),
        ),
    )
    sub_mission_service.propagate_result(
        tenant_alpha,
        SubMissionResultContract(
            mission_id=c1.mission_id,
            parent_mission_id=parent.mission_id,
            tenant_id="tenant-alpha",
            status=MissionStatus.COMPLETED,
        ),
    )

    # Hijo 2: PENDING
    c2 = sub_mission_service.create_sub_mission(
        tenant_alpha,
        SubMissionCreationContract(
            parent_mission_id=parent.mission_id,
            tenant_id="tenant-alpha",
            sub_mission_type=MissionType.SUPPLIER_SEARCH,
            scope=SubMissionScope(objective="Hijo 2", expected_outcome="O2", completion_condition="C2"),
        ),
    )

    # Cancelar jerarquía
    cancelled_count = sub_mission_service.cancel_hierarchy(
        tenant_alpha, parent.mission_id, reason="Misión abortada por operador"
    )
    assert cancelled_count == 1

    # Verificar estados
    c1_after = mission_repo.get_by_id(tenant_alpha, c1.mission_id)
    assert c1_after.status == MissionStatus.COMPLETED

    c2_after = mission_repo.get_by_id(tenant_alpha, c2.mission_id)
    assert c2_after.status == MissionStatus.ABORTED
    assert c2_after.parameters["cancellation_reason"] == "Misión abortada por operador"


# ==================================================================================================
# ESCENARIO H: Aislamiento Estricto Multi-Tenant (Tenant A / B)
# ==================================================================================================
def test_scenario_h_tenant_a_b_strict_isolation(
    sub_mission_service, mission_repo, tenant_alpha, tenant_beta
):
    """
    Verifica que el Tenant B no pueda crear hijos sobre una misión del Tenant A,
    ni ver sus submisiones ni propagar resultados.
    """
    # Crear misión en Tenant A
    mission_a = Mission(
        mission_id="m_tenant_a",
        type=MissionType.MARKET_DISCOVERY,
        status=MissionStatus.RUNNING,
        parameters={},
    )
    mission_repo.save(tenant_alpha, mission_a)

    # Intentar crear submisión desde Tenant B sobre padre de Tenant A -> TenantMismatchError o InvalidParentMissionError
    contract_cross = SubMissionCreationContract(
        parent_mission_id="m_tenant_a",
        tenant_id="tenant-beta",
        sub_mission_type=MissionType.SUPPLIER_SEARCH,
        scope=SubMissionScope(objective="Acceso cruzado", expected_outcome="N/A", completion_condition="N/A"),
    )
    with pytest.raises(InvalidParentMissionError):
        sub_mission_service.create_sub_mission(tenant_beta, contract_cross)

    # Si tenant_id en contract es tenant-alpha pero context es tenant_beta -> TenantMismatchError
    contract_mismatch = SubMissionCreationContract(
        parent_mission_id="m_tenant_a",
        tenant_id="tenant-alpha",
        sub_mission_type=MissionType.SUPPLIER_SEARCH,
        scope=SubMissionScope(objective="Acceso cruzado", expected_outcome="N/A", completion_condition="N/A"),
    )
    with pytest.raises(TenantMismatchError):
        sub_mission_service.create_sub_mission(tenant_beta, contract_mismatch)

    # Árbol de jerarquía de Tenant B sobre ID de Tenant A retorna None
    tree = sub_mission_service.get_hierarchy_tree(tenant_beta, "m_tenant_a")
    assert tree is None


# ==================================================================================================
# ESCENARIO I: Delegación Duplicada Concurrente Idempotente
# ==================================================================================================
def test_scenario_i_concurrent_duplicate_delegation_yields_one_logical_child(
    sub_mission_service, mission_repo, tenant_alpha
):
    """
    Verifica que múltiples llamadas concurrentes con la misma delegation_key
    devuelvan exactamente la misma submisión lógica sin crear duplicados.
    """
    parent = Mission(
        mission_id="m_parent_i",
        type=MissionType.MARKET_DISCOVERY,
        status=MissionStatus.RUNNING,
        parameters={},
    )
    mission_repo.save(tenant_alpha, parent)

    contract = SubMissionCreationContract(
        parent_mission_id=parent.mission_id,
        tenant_id="tenant-alpha",
        sub_mission_type=MissionType.SUPPLIER_SEARCH,
        scope=SubMissionScope(
            objective="Búsqueda concurrente idéntica",
            expected_outcome="Unico resultado",
            completion_condition="Completado",
        ),
        delegation_key="delegation-fixed-key-12345",
    )

    def _create():
        return sub_mission_service.create_sub_mission(tenant_alpha, contract)

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(_create) for _ in range(5)]
        created_missions = [f.result() for f in futures]

    first_id = created_missions[0].mission_id
    assert all(m.mission_id == first_id for m in created_missions)

    # Verificar que en el repositorio solo hay 1 hijo
    children = mission_repo.get_children(tenant_alpha, parent.mission_id)
    assert len(children) == 1


# ==================================================================================================
# ESCENARIO J: Trazabilidad y Auditoría Jerárquica Completa
# ==================================================================================================
def test_scenario_j_complete_audit_trail_and_trace_hierarchy(
    sub_mission_service, mission_repo, audit_repo, trace_repo, tenant_alpha
):
    """
    Verifica que cada operación de ciclo de vida (creación, completitud, fallo, cancelación)
    emita registros inmutables de auditoría K.1 y trazas de agente K.2 estructuradas.
    """
    parent = Mission(
        mission_id="m_parent_j",
        type=MissionType.MARKET_DISCOVERY,
        status=MissionStatus.RUNNING,
        parameters={},
    )
    mission_repo.save(tenant_alpha, parent)

    # 1. Crear submisión
    contract = SubMissionCreationContract(
        parent_mission_id=parent.mission_id,
        tenant_id="tenant-alpha",
        sub_mission_type=MissionType.MARKET_DISCOVERY,
        scope=SubMissionScope(
            objective="Auditoría y trazabilidad",
            expected_outcome="Datos de prueba",
            completion_condition="Finalizado",
        ),
    )
    child = sub_mission_service.create_sub_mission(tenant_alpha, contract)

    # Verificar registro de auditoría SUB_MISSION_CREATED
    created_audits = [r for r in audit_repo.records if r.record_type == AuditRecordType.SUB_MISSION_CREATED]
    assert len(created_audits) >= 1
    assert created_audits[-1].mission_id == child.mission_id

    # 2. Completar submisión
    res_contract = SubMissionResultContract(
        mission_id=child.mission_id,
        parent_mission_id=parent.mission_id,
        tenant_id="tenant-alpha",
        status=MissionStatus.COMPLETED,
        outputs={"items_analyzed": 10},
    )
    sub_mission_service.propagate_result(tenant_alpha, res_contract)

    # Verificar registro de auditoría SUB_MISSION_COMPLETED
    completed_audits = [r for r in audit_repo.records if r.record_type == AuditRecordType.SUB_MISSION_COMPLETED]
    assert len(completed_audits) >= 1
    assert completed_audits[-1].mission_id == child.mission_id

    # 3. Verificar trazas emitidas
    traces = trace_repo.list_records(execution_id=child.mission_id)
    assert len(traces) >= 2


# ==================================================================================================
# ESCENARIO K: Flujo E2E Sintético Determinista
# ==================================================================================================
def test_scenario_k_synthetic_e2e_parent_plan_delegation_execution_replan_completion(
    sub_mission_service, planning_service, plan_repo, mission_repo, audit_repo, tenant_alpha
):
    """
    E2E sintético completo:
    1. Parent Mission creada con presupuesto.
    2. Plan R.1 generado con 3 pasos (Investigar Mercado -> Evaluar Proveedores -> Generar Listing).
    3. Paso 1 delegado a Submisión 1 -> Ejecutado en AutonomousLoop existente -> Completado y propagado.
    4. Paso 2 delegado a Submisión 2 -> Falla de forma técnica controlada -> Propagado como EXECUTION_FAILURE.
    5. Presupuesto preservado -> Replan acotado R.1 genera paso de reemplazo (Paso 2b).
    6. Paso 2b delegado a Submisión 2b -> Ejecutado y completado.
    7. Paso 3 delegado a Submisión 3 -> Ejecutado y completado.
    8. Verificación de progreso del padre y completitud exitosa sin bypass de políticas y sin efectos secundarios externos.
    """
    # 1. Crear misión padre
    parent = Mission(
        mission_id="m_parent_e2e",
        type=MissionType.MARKET_DISCOVERY,
        status=MissionStatus.RUNNING,
        parameters={"budget": {"max_tokens": 15000, "max_cost": "15.0"}},
    )
    mission_repo.save(tenant_alpha, parent)

    # 2. Crear Plan R.1 inicial
    initial_steps = [
        PlanStep(
            step_id="step-1",
            objective="Investigar mercado de calzado",
            action_type="MARKET_RESEARCH",
            assigned_capability="MARKET_RESEARCH",
            depth_level=1,
            estimated_cost=Decimal("2.0"),
            estimated_tokens=2000,
        ),
        PlanStep(
            step_id="step-2",
            objective="Evaluar catálogo de proveedores",
            action_type="SUPPLIER_ANALYSIS",
            assigned_capability="SUPPLIER_ANALYSIS",
            dependencies=("step-1",),
            depth_level=1,
            estimated_cost=Decimal("3.0"),
            estimated_tokens=3000,
        ),
        PlanStep(
            step_id="step-3",
            objective="Generar listing para marketplace",
            action_type="LISTING_GENERATION",
            assigned_capability="LISTING_GENERATION",
            dependencies=("step-2",),
            depth_level=1,
            estimated_cost=Decimal("2.0"),
            estimated_tokens=2000,
        ),
    ]
    plan_budget = PlanBudget(max_cost=Decimal("15.0"), max_tokens=15000, max_steps=10, max_replans=2)
    plan_result = planning_service.create_plan(
        tenant_context=tenant_alpha,
        mission_id=parent.mission_id,
        goal="Lanzamiento integral de producto",
        initial_steps=initial_steps,
        budget=plan_budget,
    )
    assert plan_result.success is True
    plan_id = plan_result.plan.plan_id

    # 3. Delegar y ejecutar Paso 1
    c1_contract = SubMissionCreationContract(
        parent_mission_id=parent.mission_id,
        tenant_id="tenant-alpha",
        sub_mission_type=MissionType.MARKET_DISCOVERY,
        scope=SubMissionScope(
            objective="Investigar mercado de calzado",
            expected_outcome="Informe de mercado",
            completion_condition="Datos listos",
        ),
        allocated_budget=PlanBudget(max_tokens=2000, max_cost=Decimal("2.0")),
        plan_id=plan_id,
        plan_step_id="step-1",
    )
    sub_1 = sub_mission_service.create_sub_mission(tenant_alpha, c1_contract)

    # Runtime existente para sub_1
    provider_1 = ScriptedDecisionProvider([
        LoopDecision(action=LoopAction.CONTINUE, reason="Extrayendo datos de competencia"),
        LoopDecision(action=LoopAction.COMPLETE, reason="Datos de mercado consolidados"),
    ])
    executor_1 = RecordingActionExecutor(responses={
        LoopAction.CONTINUE.value: {"status": "ok"},
        LoopAction.COMPLETE.value: {"status": "completed", "top_niche": "trail-running"},
    })
    loop_1 = AutonomousLoop(decision_provider=provider_1, action_executor=executor_1, max_iterations=5)
    r1 = loop_1.run(mission_id=sub_1.mission_id, goal=sub_1.parameters["objective"])
    assert r1.status == "COMPLETED"

    sub_mission_service.propagate_result(
        tenant_alpha,
        SubMissionResultContract(
            mission_id=sub_1.mission_id,
            parent_mission_id=parent.mission_id,
            tenant_id="tenant-alpha",
            status=MissionStatus.COMPLETED,
            outputs={"top_niche": "trail-running"},
        ),
    )

    # 4. Delegar y simular fallo técnico en Paso 2
    c2_contract = SubMissionCreationContract(
        parent_mission_id=parent.mission_id,
        tenant_id="tenant-alpha",
        sub_mission_type=MissionType.SUPPLIER_SEARCH,
        scope=SubMissionScope(
            objective="Evaluar catálogo de proveedores",
            expected_outcome="Proveedores listos",
            completion_condition="Completado",
        ),
        allocated_budget=PlanBudget(max_tokens=3000, max_cost=Decimal("3.0")),
        plan_id=plan_id,
        plan_step_id="step-2",
        is_required=False,
    )
    sub_2 = sub_mission_service.create_sub_mission(tenant_alpha, c2_contract)

    # Simular fallo en el executor del runtime
    provider_2 = ScriptedDecisionProvider([
        LoopDecision(action=LoopAction.CONTINUE, reason="Conectando a API de proveedores"),
    ])
    executor_2 = RecordingActionExecutor(raise_on_action=LoopAction.CONTINUE)
    loop_2 = AutonomousLoop(decision_provider=provider_2, action_executor=executor_2, max_iterations=5)
    r2 = loop_2.run(mission_id=sub_2.mission_id, goal=sub_2.parameters["objective"])
    assert r2.status == "ERROR"

    sub_mission_service.propagate_result(
        tenant_alpha,
        SubMissionResultContract(
            mission_id=sub_2.mission_id,
            parent_mission_id=parent.mission_id,
            tenant_id="tenant-alpha",
            status=MissionStatus.FAILED,
            failure_reason="Fallo de conexión en API de proveedor primario",
            failure_type=SubMissionFailureType.TECHNICAL_FAILURE,
        ),
    )

    # 5. Replan acotado R.1
    replan_steps = [
        PlanStep(
            step_id="step-2b",
            objective="Evaluar proveedores alternativos locales",
            action_type="SUPPLIER_ANALYSIS",
            assigned_capability="SUPPLIER_ANALYSIS",
            dependencies=("step-1",),
            depth_level=1,
            estimated_cost=Decimal("2.5"),
            estimated_tokens=2500,
        ),
        PlanStep(
            step_id="step-3",
            objective="Generar listing para marketplace",
            action_type="LISTING_GENERATION",
            assigned_capability="LISTING_GENERATION",
            dependencies=("step-2b",),
            depth_level=1,
            estimated_cost=Decimal("2.0"),
            estimated_tokens=2000,
        ),
    ]
    replan_res = planning_service.replan(
        plan_id=plan_id,
        tenant_context=tenant_alpha,
        failed_step_id="step-2",
        failure_type=StepFailureType.EXECUTION_FAILURE,
        failure_reason="API primaria caída",
        new_subgraph_steps=replan_steps,
    )
    assert replan_res.success is True
    assert replan_res.plan.version == 2
    assert replan_res.plan.get_step("step-1").status == StepStatus.COMPLETED

    # 6. Delegar y ejecutar Paso 2b
    c2b_contract = SubMissionCreationContract(
        parent_mission_id=parent.mission_id,
        tenant_id="tenant-alpha",
        sub_mission_type=MissionType.SUPPLIER_SEARCH,
        scope=SubMissionScope(
            objective="Evaluar proveedores alternativos locales",
            expected_outcome="Proveedores cotizados",
            completion_condition="Completado",
        ),
        allocated_budget=PlanBudget(max_tokens=2500, max_cost=Decimal("2.5")),
        plan_id=plan_id,
        plan_step_id="step-2b",
    )
    sub_2b = sub_mission_service.create_sub_mission(tenant_alpha, c2b_contract)

    provider_2b = ScriptedDecisionProvider([
        LoopDecision(action=LoopAction.COMPLETE, reason="Proveedores locales confirmados"),
    ])
    executor_2b = RecordingActionExecutor()
    loop_2b = AutonomousLoop(decision_provider=provider_2b, action_executor=executor_2b)
    r2b = loop_2b.run(mission_id=sub_2b.mission_id, goal=sub_2b.parameters["objective"])
    assert r2b.status == "COMPLETED"

    sub_mission_service.propagate_result(
        tenant_alpha,
        SubMissionResultContract(
            mission_id=sub_2b.mission_id,
            parent_mission_id=parent.mission_id,
            tenant_id="tenant-alpha",
            status=MissionStatus.COMPLETED,
            outputs={"selected_supplier": "LocalSup-A"},
        ),
    )

    # 7. Delegar y ejecutar Paso 3
    c3_contract = SubMissionCreationContract(
        parent_mission_id=parent.mission_id,
        tenant_id="tenant-alpha",
        sub_mission_type=MissionType.COMMERCIAL_PUBLICATION,
        scope=SubMissionScope(
            objective="Generar listing para marketplace",
            expected_outcome="Listing publicado",
            completion_condition="Completado",
        ),
        allocated_budget=PlanBudget(max_tokens=2000, max_cost=Decimal("2.0")),
        plan_id=plan_id,
        plan_step_id="step-3",
    )
    sub_3 = sub_mission_service.create_sub_mission(tenant_alpha, c3_contract)

    provider_3 = ScriptedDecisionProvider([
        LoopDecision(action=LoopAction.COMPLETE, reason="Listing completado"),
    ])
    executor_3 = RecordingActionExecutor()
    loop_3 = AutonomousLoop(decision_provider=provider_3, action_executor=executor_3)
    r3 = loop_3.run(mission_id=sub_3.mission_id, goal=sub_3.parameters["objective"])
    assert r3.status == "COMPLETED"

    sub_mission_service.propagate_result(
        tenant_alpha,
        SubMissionResultContract(
            mission_id=sub_3.mission_id,
            parent_mission_id=parent.mission_id,
            tenant_id="tenant-alpha",
            status=MissionStatus.COMPLETED,
            outputs={"listing_id": "LISTING-777"},
        ),
    )

    # 8. Verificar que el padre puede completarse y el árbol está íntegro
    can_complete, blockers = sub_mission_service.can_parent_complete(tenant_alpha, parent.mission_id)
    assert can_complete is True
    assert blockers == []

    tree = sub_mission_service.get_hierarchy_tree(tenant_alpha, parent.mission_id)
    assert tree is not None
    assert len(tree.children) == 4  # sub_1, sub_2 (fallida), sub_2b, sub_3
