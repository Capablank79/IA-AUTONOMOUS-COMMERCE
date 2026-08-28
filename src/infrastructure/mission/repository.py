from typing import Dict, Optional
from src.domain.mission.models import Mission, MissionResult
from src.domain.mission.ports import MissionRepository

class InMemoryMissionRepository(MissionRepository):
    def __init__(self):
        self._missions: Dict[str, Mission] = {}
        self._results: Dict[str, MissionResult] = {}

    def save(self, mission: Mission) -> None:
        self._missions[mission.mission_id] = mission

    def get_by_id(self, mission_id: str) -> Optional[Mission]:
        return self._missions.get(mission_id)

    def save_result(self, result: MissionResult) -> None:
        self._results[result.mission_id] = result

    def get_result(self, mission_id: str) -> Optional[MissionResult]:
        return self._results.get(mission_id)
