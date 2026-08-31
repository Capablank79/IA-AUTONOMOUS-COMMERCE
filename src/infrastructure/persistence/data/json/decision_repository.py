import json
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal
from typing import Union, Optional, Any, Dict, List

from src.domain.market_intelligence.models import Confidence
from src.domain.supplier_intelligence.models import EvidenceProvenanceType, RiskLevel
from src.domain.policy.models import (
    PolicyDecisionType,
    PolicyEvaluation,
    RuleEvaluationResult,
    PolicyViolation,
    PolicyRuleCategory,
    PolicySeverity,
)
from src.domain.decision.models import (
    DecisionRecord,
    DecisionType,
    DecisionStatus,
    DecisionOutcome,
    DecisionEvidenceReference,
)
from src.domain.decision.ports import DecisionRepository


class JsonDecisionRepositoryError(Exception):
    """Base exception for JsonDecisionRepository errors."""
    pass


class InvalidDecisionDataError(JsonDecisionRepositoryError):
    """Raised when loaded decision data is corrupted or invalid."""
    pass


from types import MappingProxyType

SENSITIVE_KEYS = {"password", "secret", "token", "api_key", "apikey", "pan", "cvv", "private_key", "credential"}


def _encode_json_value(val: Any) -> Any:
    """Helper to convert complex objects to JSON-serializable types with sensitive data filtering."""
    if isinstance(val, datetime):
        return val.isoformat()
    if isinstance(val, Decimal):
        return str(val)
    if hasattr(val, "value"):  # Enum
        return val.value
    if isinstance(val, (dict, MappingProxyType)):
        cleaned = {}
        for k, v in val.items():
            if str(k).lower() in SENSITIVE_KEYS:
                continue
            cleaned[str(k)] = _encode_json_value(v)
        return cleaned
    if isinstance(val, (list, tuple)):
        return [_encode_json_value(v) for v in val]
    return val


def _decode_policy_evaluation(data: Optional[Dict[str, Any]]) -> Optional[PolicyEvaluation]:
    if not data:
        return None
    try:
        rule_results = []
        for r in data.get("rule_results", []):
            violations = tuple(
                PolicyViolation(
                    rule_name=v["rule_name"],
                    category=PolicyRuleCategory(v["category"]),
                    severity=PolicySeverity(v["severity"]),
                    message=v["message"],
                    code=v["code"],
                    details=v.get("details", {}),
                )
                for v in r.get("violations", [])
            )
            rule_results.append(
                RuleEvaluationResult(
                    rule_name=r["rule_name"],
                    category=PolicyRuleCategory(r["category"]),
                    passed=r["passed"],
                    decision_impact=PolicyDecisionType(r["decision_impact"]),
                    reasons=tuple(r.get("reasons", [])),
                    violations=violations,
                    evaluated_at=datetime.fromisoformat(r["evaluated_at"]),
                )
            )

        global_violations = tuple(
            PolicyViolation(
                rule_name=v["rule_name"],
                category=PolicyRuleCategory(v["category"]),
                severity=PolicySeverity(v["severity"]),
                message=v["message"],
                code=v["code"],
                details=v.get("details", {}),
            )
            for v in data.get("violations", [])
        )

        return PolicyEvaluation(
            evaluation_id=data["evaluation_id"],
            decision=PolicyDecisionType(data["decision"]),
            action_type=data["action_type"],
            actor_id=data["actor_id"],
            mission_id=data["mission_id"],
            correlation_id=data["correlation_id"],
            rules_evaluated=tuple(data.get("rules_evaluated", [])),
            rule_results=tuple(rule_results),
            reasons=tuple(data.get("reasons", [])),
            violations=global_violations,
            is_allowed=data.get("is_allowed", False),
            requires_approval=data.get("requires_approval", False),
            is_unknown=data.get("is_unknown", False),
            is_denied=data.get("is_denied", False),
            is_deferred=data.get("is_deferred", False),
            budget_impact=Decimal(data["budget_impact"]) if data.get("budget_impact") is not None else None,
            risk_level=RiskLevel(data["risk_level"]) if data.get("risk_level") else None,
            idempotency_key=data.get("idempotency_key"),
            evidence_unknowns=tuple(data.get("evidence_unknowns", [])),
            timestamp=datetime.fromisoformat(data["timestamp"]),
            metadata=data.get("metadata", {}),
        )
    except Exception as e:
        raise InvalidDecisionDataError(f"Corrupted policy evaluation structure: {e}") from e


def _encode_policy_evaluation(eval_obj: Optional[PolicyEvaluation]) -> Optional[Dict[str, Any]]:
    if not eval_obj:
        return None
    return {
        "evaluation_id": eval_obj.evaluation_id,
        "decision": eval_obj.decision.value,
        "action_type": eval_obj.action_type,
        "actor_id": eval_obj.actor_id,
        "mission_id": eval_obj.mission_id,
        "correlation_id": eval_obj.correlation_id,
        "rules_evaluated": list(eval_obj.rules_evaluated),
        "rule_results": [
            {
                "rule_name": r.rule_name,
                "category": r.category.value,
                "passed": r.passed,
                "decision_impact": r.decision_impact.value,
                "reasons": list(r.reasons),
                "violations": [
                    {
                        "rule_name": v.rule_name,
                        "category": v.category.value,
                        "severity": v.severity.value,
                        "message": v.message,
                        "code": v.code,
                        "details": _encode_json_value(v.details),
                    }
                    for v in r.violations
                ],
                "evaluated_at": r.evaluated_at.isoformat(),
            }
            for r in eval_obj.rule_results
        ],
        "reasons": list(eval_obj.reasons),
        "violations": [
            {
                "rule_name": v.rule_name,
                "category": v.category.value,
                "severity": v.severity.value,
                "message": v.message,
                "code": v.code,
                "details": _encode_json_value(v.details),
            }
            for v in eval_obj.violations
        ],
        "is_allowed": eval_obj.is_allowed,
        "requires_approval": eval_obj.requires_approval,
        "is_unknown": eval_obj.is_unknown,
        "is_denied": eval_obj.is_denied,
        "is_deferred": eval_obj.is_deferred,
        "budget_impact": str(eval_obj.budget_impact) if eval_obj.budget_impact is not None else None,
        "risk_level": eval_obj.risk_level.value if eval_obj.risk_level else None,
        "idempotency_key": eval_obj.idempotency_key,
        "evidence_unknowns": list(eval_obj.evidence_unknowns),
        "timestamp": eval_obj.timestamp.isoformat(),
        "metadata": _encode_json_value(eval_obj.metadata),
    }


class JsonDecisionRepository(DecisionRepository):
    """
    Implementación en almacenamiento de archivos JSON para DecisionRepository.
    Ofrece durabilidad, atomicidad mediante archivos temporales, soporte idempotente y sanitización de secretos.
    """

    def __init__(self, storage_dir: Union[str, Path]):
        self.storage_dir = Path(storage_dir)
        self._decisions_dir = self.storage_dir / "decisions"
        self._decisions_dir.mkdir(parents=True, exist_ok=True)

    def save(self, decision: DecisionRecord) -> None:
        file_path = self._decisions_dir / f"{decision.decision_id}.json"

        evidences_data = [
            {
                "evidence_id": ev.evidence_id,
                "evidence_type": ev.evidence_type,
                "source": ev.source,
                "confidence": ev.confidence.value,
                "provenance": ev.provenance.value,
                "metadata": _encode_json_value(ev.metadata),
            }
            for ev in decision.evidence_references
        ]

        data = {
            "decision_id": decision.decision_id,
            "mission_id": decision.mission_id,
            "decision_type": decision.decision_type.value,
            "status": decision.status.value,
            "reason": decision.reason,
            "created_at": decision.created_at.isoformat(),
            "updated_at": decision.updated_at.isoformat(),
            "outcome": decision.outcome.value,
            "target_resource": decision.target_resource,
            "parameters": _encode_json_value(decision.parameters),
            "confidence": decision.confidence.value,
            "provenance": decision.provenance.value,
            "risk_level": decision.risk_level.value if decision.risk_level else None,
            "policy_evaluation": _encode_policy_evaluation(decision.policy_evaluation),
            "policy_decision_type": decision.policy_decision_type.value if decision.policy_decision_type else None,
            "evidence_references": evidences_data,
            "future_action_type": decision.future_action_type,
            "correlation_id": decision.correlation_id,
            "idempotency_key": decision.idempotency_key,
            "version": decision.version,
            "metadata": _encode_json_value(decision.metadata),
        }

        temp_path = file_path.with_suffix(".tmp")
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        temp_path.replace(file_path)

    def get_by_id(self, decision_id: str) -> Optional[DecisionRecord]:
        file_path = self._decisions_dir / f"{decision_id}.json"
        if not file_path.exists():
            return None

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            evidence_references = tuple(
                DecisionEvidenceReference(
                    evidence_id=ev["evidence_id"],
                    evidence_type=ev["evidence_type"],
                    source=ev["source"],
                    confidence=Confidence(ev["confidence"]),
                    provenance=EvidenceProvenanceType(ev["provenance"]),
                    metadata=ev.get("metadata", {}),
                )
                for ev in data.get("evidence_references", [])
            )

            policy_eval = _decode_policy_evaluation(data.get("policy_evaluation"))

            return DecisionRecord(
                decision_id=data["decision_id"],
                mission_id=data["mission_id"],
                decision_type=DecisionType(data["decision_type"]),
                status=DecisionStatus(data["status"]),
                reason=data["reason"],
                created_at=datetime.fromisoformat(data["created_at"]),
                updated_at=datetime.fromisoformat(data["updated_at"]),
                outcome=DecisionOutcome(data["outcome"]),
                target_resource=data.get("target_resource"),
                parameters=data.get("parameters", {}),
                confidence=Confidence(data["confidence"]),
                provenance=EvidenceProvenanceType(data["provenance"]),
                risk_level=RiskLevel(data["risk_level"]) if data.get("risk_level") else None,
                policy_evaluation=policy_eval,
                policy_decision_type=PolicyDecisionType(data["policy_decision_type"]) if data.get("policy_decision_type") else None,
                evidence_references=evidence_references,
                future_action_type=data.get("future_action_type"),
                correlation_id=data["correlation_id"],
                idempotency_key=data["idempotency_key"],
                version=data.get("version", 1),
                metadata=data.get("metadata", {}),
            )
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            raise InvalidDecisionDataError(f"Corrupted decision data for {decision_id}: {e}") from e

    def get_by_mission_id(self, mission_id: str) -> List[DecisionRecord]:
        results = []
        for path in self._decisions_dir.glob("*.json"):
            if path.name.endswith(".tmp.json"):
                continue
            decision = self.get_by_id(path.stem)
            if decision and decision.mission_id == mission_id:
                results.append(decision)
        return results

    def get_by_idempotency_key(self, idempotency_key: str) -> Optional[DecisionRecord]:
        for path in self._decisions_dir.glob("*.json"):
            if path.name.endswith(".tmp.json"):
                continue
            decision = self.get_by_id(path.stem)
            if decision and decision.idempotency_key == idempotency_key:
                return decision
        return None

    def exists(self, decision_id: str) -> bool:
        file_path = self._decisions_dir / f"{decision_id}.json"
        return file_path.exists()
