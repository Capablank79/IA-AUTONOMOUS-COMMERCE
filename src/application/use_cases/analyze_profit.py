from typing import Optional

from src.domain.profit.ports import ProfitDataRepository
from src.domain.profit.engine import ProfitEngine
from src.domain.profit.models import ProfitAnalysis
from src.domain.supplier_intelligence.ports import SupplierDataSource
from src.application.mappers.supplier_financial_mapper import SupplierFinancialMapper

class AnalyzeProfitUseCase:
    """
    Use case to orchestrate the profit analysis of an experiment.
    It fetches data from a repository and uses the ProfitEngine to perform the calculation.
    """
    def __init__(
        self,
        repository: ProfitDataRepository,
        supplier_source: Optional[SupplierDataSource] = None
    ):
        self._repository = repository
        self._supplier_source = supplier_source
        self._engine = ProfitEngine()

    def execute(
        self,
        experiment_id: str,
        supplier_id: Optional[str] = None,
        sku: Optional[str] = None
    ) -> ProfitAnalysis:
        financial_data = self._repository.get_financial_data(experiment_id)
        decision_rules = self._repository.get_decision_rules(experiment_id)

        # Si se proporcionan los parámetros para usar la evidencia del proveedor real
        if self._supplier_source and supplier_id and sku:
            evidence = self._supplier_source.get_supplier_evidence(supplier_id, sku)
            if evidence is None:
                raise ValueError(f"Supplier evidence not found for supplier_id={supplier_id}, sku={sku}")

            financial_data = SupplierFinancialMapper.map_evidence_to_financial_data(
                base_financial_data=financial_data,
                evidence=evidence
            )

        return self._engine.calculate(data=financial_data, rules=decision_rules)
