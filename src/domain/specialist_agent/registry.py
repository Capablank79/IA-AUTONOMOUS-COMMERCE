from typing import Dict, Optional, Sequence

from src.domain.tool.ports import ToolRegistryPort

from .models import SpecialistAgentDefinition


class SpecialistAgentRegistry:
    def __init__(self, tool_registry: Optional[ToolRegistryPort] = None, known_action_types: Sequence[str] = ()):
        self._definitions: Dict[str, Dict[str, SpecialistAgentDefinition]] = {}
        self._tool_registry = tool_registry
        self._known_action_types = frozenset(known_action_types)

    def register(self, definition: SpecialistAgentDefinition) -> None:
        tenant_agents = self._definitions.setdefault(definition.tenant_id, {})
        existing = tenant_agents.get(definition.agent_id)
        if existing is not None:
            if existing == definition:
                raise ValueError(f"Specialist agent '{definition.agent_id}' is already registered")
            raise ValueError(f"Conflicting specialist agent definition '{definition.agent_id}'")

        for capability in definition.capabilities:
            for action_type in capability.action_types:
                if self._known_action_types and action_type not in self._known_action_types:
                    raise ValueError(f"Unknown action_type reference '{action_type}'")
                if action_type not in definition.allowed_action_types:
                    raise ValueError(f"Action '{action_type}' is not in agent allowlist")
            for tool_id in capability.tool_ids:
                if tool_id not in definition.allowed_tool_ids:
                    raise ValueError(f"Tool '{tool_id}' is not in agent allowlist")
                if self._tool_registry is None or self._tool_registry.get(tool_id) is None:
                    raise ValueError(f"Unknown tool reference '{tool_id}'")
        tenant_agents[definition.agent_id] = definition

    def get(self, tenant_id: str, agent_id: str) -> Optional[SpecialistAgentDefinition]:
        return self._definitions.get(tenant_id, {}).get(agent_id)

    def list_for_tenant(self, tenant_id: str) -> Sequence[SpecialistAgentDefinition]:
        agents = self._definitions.get(tenant_id, {})
        return tuple(agents[key] for key in sorted(agents))

    def find_by_capability(self, tenant_id: str, capability_id: str) -> Sequence[SpecialistAgentDefinition]:
        return tuple(
            definition
            for definition in self.list_for_tenant(tenant_id)
            if definition.get_capability(capability_id) is not None
        )
