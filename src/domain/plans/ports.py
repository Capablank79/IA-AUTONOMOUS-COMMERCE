"""
Puertos de dominio para Plans & Pricing Tiers SaaS (Hito O.8 — Plans & Pricing Tiers).

Define:
- PlanCatalogRepositoryPort: Almacenamiento y consulta de planes y versiones del catálogo.
- PlanAssignmentRepositoryPort: Almacenamiento y consulta tenant-scoped de asignaciones de plan.
- PlanEntitlementServicePort: Contrato principal de evaluación de capabilities y límites de planes.
- PlanAuditPort: Emisión desacoplada de eventos de auditoría y trazas de planes.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional, List, Sequence, Mapping, Any

from src.domain.tenant.models import TenantContext
from .models import (
    Plan,
    PlanAssignment,
    PlanEntitlementRequest,
    PlanEntitlementDecision,
    PlanFeature,
)


class PlanCatalogRepositoryPort(ABC):
    """Puerto de persistencia para el catálogo de planes inmutables y versionados."""

    @abstractmethod
    def get_plan(self, plan_id: str, version: Optional[str] = None) -> Optional[Plan]:
        """
        Obtiene un plan por su plan_id. Si version es None, devuelve la versión más reciente/activa.
        """
        pass

    @abstractmethod
    def save_plan(self, plan: Plan) -> None:
        """Guarda una versión de plan en el catálogo."""
        pass

    @abstractmethod
    def list_plans(self, active_only: bool = True) -> List[Plan]:
        """Lista los planes disponibles en el catálogo."""
        pass

    @abstractmethod
    def list_plan_versions(self, plan_id: str) -> List[Plan]:
        """Lista todas las versiones históricas registradas para un plan_id."""
        pass


class PlanAssignmentRepositoryPort(ABC):
    """Puerto de persistencia tenant-scoped para asignaciones históricas y vigentes de planes."""

    @abstractmethod
    def save_assignment(self, assignment: PlanAssignment) -> None:
        """Guarda o actualiza una asignación de plan para un tenant."""
        pass

    @abstractmethod
    def get_assignment(self, assignment_id: str) -> Optional[PlanAssignment]:
        """Recupera una asignación específica por su ID."""
        pass

    @abstractmethod
    def get_active_assignment(self, tenant_id: str, current_time: datetime) -> Optional[PlanAssignment]:
        """Obtiene la asignación vigente y activa de un tenant para un momento temporal dado."""
        pass

    @abstractmethod
    def list_assignments_for_tenant(self, tenant_id: str) -> List[PlanAssignment]:
        """Lista todo el historial cronológico de asignaciones de un tenant."""
        pass


class PlanAuditPort(ABC):
    """Puerto desacoplado para emisión de auditoría y trazas de planes."""

    @abstractmethod
    def emit_plan_event(
        self,
        event_name: str,
        tenant_id: str,
        actor_id: Optional[str],
        details: Mapping[str, Any],
        correlation_id: Optional[str] = None,
    ) -> None:
        """Emite un evento de auditoría/traza seguro (sin secretos ni payment payloads)."""
        pass


class PlanEntitlementServicePort(ABC):
    """Contrato del servicio principal de gobernanza de capabilities, límites y materialización de cuotas."""

    @abstractmethod
    def assign_plan(
        self,
        tenant_id: str,
        plan_id: str,
        plan_version: Optional[str] = None,
        effective_from: Optional[datetime] = None,
        effective_until: Optional[datetime] = None,
        source_reason: str = "ADMIN_ASSIGNMENT",
        actor_id: Optional[str] = None,
        context: Optional[TenantContext] = None,
    ) -> PlanAssignment:
        """
        Asigna un plan a un tenant, actualiza el estado y materializa la QuotaPolicy en O.7 si está integrado.
        """
        pass

    @abstractmethod
    def change_plan(
        self,
        tenant_id: str,
        new_plan_id: str,
        new_plan_version: Optional[str] = None,
        effective_from: Optional[datetime] = None,
        reason: str = "PLAN_UPGRADE_OR_DOWNGRADE",
        actor_id: Optional[str] = None,
        context: Optional[TenantContext] = None,
    ) -> PlanAssignment:
        """
        Cambia el plan de un tenant de forma controlada (upgrade/downgrade), preservando historial y usage O.6.
        """
        pass

    @abstractmethod
    def get_active_plan(
        self,
        tenant_id: str,
        current_time: Optional[datetime] = None,
        context: Optional[TenantContext] = None,
    ) -> Optional[Plan]:
        """
        Resuelve el plan y versión activos y efectivos para un tenant en un timestamp dado.
        """
        pass

    @abstractmethod
    def evaluate_entitlement(
        self,
        request: PlanEntitlementRequest,
        context: Optional[TenantContext] = None,
    ) -> PlanEntitlementDecision:
        """
        Evalúa determinísticamente si un tenant tiene habilitada una capability/feature/model según su plan activo.
        """
        pass
