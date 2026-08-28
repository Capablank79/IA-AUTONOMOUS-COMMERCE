from datetime import datetime
from typing import Optional, Dict, Any, List
from src.domain.mission.models import Mission, MissionStatus, MissionResult, MissionType, MissionTraceEntry
from src.domain.mission.ports import MissionOrchestrator, MissionRepository
from src.domain.market_intelligence.models import (
    SearchCriteria, Marketplace, MarketEvidence, VisitSignal, Confidence
)
from src.domain.market_intelligence.services import (
    MarketEvidenceComposer, DemandIntelligenceService
)
from src.domain.opportunity.engine import OpportunityEngine
from src.application.mappers.supplier_financial_mapper import SupplierFinancialMapper

class BasicMissionOrchestrator(MissionOrchestrator):
    """
    Orquestador básico de misiones que ejecuta capacidades de forma secuencial.
    Implementa la transición de estados PENDING -> RUNNING -> COMPLETED/FAILED.
    """

    def __init__(
        self,
        repository: MissionRepository,
        product_hunter=None,
        market_data_source=None,
        traffic_intelligence=None,
        supplier_source=None,
        profit_repository=None,
        profit_engine=None,
        opportunity_engine=None,
        market_evidence_composer=None,
        demand_intelligence=None
    ):
        self.repository = repository
        self.product_hunter = product_hunter
        self.market_data_source = market_data_source
        self.traffic_intelligence = traffic_intelligence
        self.supplier_source = supplier_source
        self.profit_repository = profit_repository
        self.profit_engine = profit_engine
        self.opportunity_engine = opportunity_engine or OpportunityEngine()
        self.market_evidence_composer = market_evidence_composer or MarketEvidenceComposer()
        self.demand_intelligence = demand_intelligence or DemandIntelligenceService()

    def submit(self, mission: Mission) -> None:
        """
        Registra la misión e inicia su ejecución.
        En esta implementación básica, la ejecución es síncrona para facilitar las pruebas.
        """
        self.repository.save(mission)
        self._execute(mission.mission_id)

    def _execute(self, mission_id: str) -> None:
        mission = self.repository.get_by_id(mission_id)
        if not mission:
            return

        # Transición a RUNNING
        mission = self._update_status(mission, MissionStatus.RUNNING)

        try:
            if mission.type == MissionType.MARKET_DISCOVERY:
                result = self._run_market_discovery(mission)
            else:
                raise ValueError(f"Tipo de misión no soportado: {mission.type}")

            # Determinar estado final basado en el resultado (si hay bloqueos)
            final_status = MissionStatus.COMPLETED
            if result.blocks:
                final_status = MissionStatus.BLOCKED
            
            # Guardar resultado y actualizar misión
            self.repository.save_result(result)
            self._update_status(mission, final_status)

        except Exception as e:
            error_result = MissionResult(
                mission_id=mission_id,
                status=MissionStatus.FAILED,
                errors=[str(e)],
                finished_at=datetime.utcnow()
            )
            self.repository.save_result(error_result)
            self._update_status(mission, MissionStatus.FAILED)

    def _run_market_discovery(self, mission: Mission) -> MissionResult:
        """
        Ejecuta la secuencia de descubrimiento de mercado conectando la nueva arquitectura.
        """
        params = mission.parameters
        query = params.get("query")
        user_id = params.get("user_id")
        limit = params.get("limit", 10)
        marketplace_str = params.get("marketplace", "MERCADO_LIBRE")
        marketplace = Marketplace(marketplace_str)
        
        # Parámetros para Profit Analysis (opcionales)
        experiment_id = params.get("experiment_id")
        supplier_id = params.get("supplier_id")
        sku = params.get("sku")

        trace = []
        evidences = []
        blocks = []
        output = {}

        trace.append(MissionTraceEntry(
            step="INIT_MARKET_DISCOVERY",
            status=MissionStatus.RUNNING,
            metadata={"query": query, "marketplace": marketplace_str}
        ))

        # 1. Product Hunter (Opcional)
        if self.product_hunter and user_id:
            try:
                catalog_products = self.product_hunter.search(
                    user_id=user_id,
                    query=query,
                    limit=limit
                )
                output["catalog_products_found"] = len(catalog_products)
                trace.append(MissionTraceEntry(
                    step="PRODUCT_HUNTER",
                    status=MissionStatus.COMPLETED,
                    metadata={"found": len(catalog_products)}
                ))
            except Exception as e:
                trace.append(MissionTraceEntry(
                    step="PRODUCT_HUNTER",
                    status=MissionStatus.FAILED,
                    metadata={"error": str(e)}
                ))

        # 2. Market Snapshot
        if not self.market_data_source:
            error_msg = "MarketplaceDataSource es requerido para MARKET_DISCOVERY"
            blocks.append({"step": "MARKET_SNAPSHOT", "reason": error_msg})
            trace.append(MissionTraceEntry(
                step="MARKET_SNAPSHOT",
                status=MissionStatus.BLOCKED,
                metadata={"error": error_msg}
            ))
            return MissionResult(
                mission_id=mission.mission_id,
                status=MissionStatus.BLOCKED,
                trace=trace,
                blocks=blocks,
                finished_at=datetime.utcnow()
            )

        try:
            criteria = SearchCriteria(query=query, marketplace=marketplace, limit=limit)
            snapshot = self.market_data_source.fetch_snapshot(criteria)
            output["snapshot_id"] = snapshot.snapshot_id
            output["listings_found"] = len(snapshot.listings)
            trace.append(MissionTraceEntry(
                step="MARKET_SNAPSHOT",
                status=MissionStatus.COMPLETED,
                metadata={"snapshot_id": snapshot.snapshot_id, "listings": len(snapshot.listings)}
            ))
        except Exception as e:
            trace.append(MissionTraceEntry(
                step="MARKET_SNAPSHOT",
                status=MissionStatus.FAILED,
                metadata={"error": str(e)}
            ))
            raise e

        # 3. Evidence Enrichment & Evaluation Loop
        decisions = []
        for listing in snapshot.listings:
            # A. Visits
            visit_signal = None
            if self.traffic_intelligence and user_id:
                try:
                    visit_signal = self.traffic_intelligence.get_visits(
                        user_id=user_id,
                        item_id=listing.external_id,
                        window_days=30
                    )
                except Exception as e:
                    trace.append(MissionTraceEntry(
                        step="VISIT_SIGNAL_FETCH",
                        status=MissionStatus.FAILED,
                        metadata={"item_id": listing.external_id, "error": str(e)}
                    ))

            # B. Compose Market Evidence
            evidence = self.market_evidence_composer.compose(
                listing=listing,
                visit_signal=visit_signal
            )

            # C. Demand Intelligence
            demand_signal = self.demand_intelligence.calculate(evidence)
            # Re-componer con la señal de demanda calculada
            evidence = self.market_evidence_composer.compose(
                listing=listing,
                visit_signal=visit_signal,
                demand_signal=demand_signal
            )

            # Conservar evidencia sin convertirla en decisión
            evidences.append(evidence)

            # D. Profit Analysis (si hay datos suficientes)
            profit_analysis = None
            if (self.profit_repository and self.profit_engine and 
                self.supplier_source and experiment_id and supplier_id and sku):
                try:
                    financial_data = self.profit_repository.get_financial_data(experiment_id)
                    decision_rules = self.profit_repository.get_decision_rules(experiment_id)
                    
                    supplier_evidence = self.supplier_source.get_supplier_evidence(supplier_id, sku)
                    if supplier_evidence:
                        enriched_financial = SupplierFinancialMapper.map_evidence_to_financial_data(
                            base_financial_data=financial_data,
                            evidence=supplier_evidence
                        )
                        profit_analysis = self.profit_engine.calculate(
                            data=enriched_financial,
                            rules=decision_rules
                        )
                except Exception as e:
                    trace.append(MissionTraceEntry(
                        step="PROFIT_ANALYSIS",
                        status=MissionStatus.FAILED,
                        metadata={"item_id": listing.external_id, "error": str(e)}
                    ))

            # E. Opportunity Evaluation (Nueva arquitectura)
            decision = self.opportunity_engine.evaluate(evidence)
            
            # Formatear resultado para el output
            decisions.append({
                "listing_id": listing.external_id,
                "title": listing.title,
                "readiness": decision.readiness.value,
                "reasons": decision.reasons,
                "demand_label": demand_signal.label,
                "profit_decision": profit_analysis.decision.value if profit_analysis else "N/A",
                "sufficient_evidence": evidence.confidence in [Confidence.HIGH, Confidence.MEDIUM]
            })

        output["results"] = decisions
        trace.append(MissionTraceEntry(
            step="OPPORTUNITY_EVALUATION",
            status=MissionStatus.COMPLETED,
            metadata={"processed": len(decisions)}
        ))

        return MissionResult(
            mission_id=mission.mission_id,
            status=MissionStatus.COMPLETED,
            output=output,
            trace=trace,
            evidences=evidences,
            blocks=blocks,
            finished_at=datetime.utcnow()
        )

    def _update_status(self, mission: Mission, status: MissionStatus) -> Mission:
        updated_mission = Mission(
            mission_id=mission.mission_id,
            type=mission.type,
            priority=mission.priority,
            status=status,
            parameters=mission.parameters,
            created_at=mission.created_at,
            updated_at=datetime.utcnow()
        )
        self.repository.save(updated_mission)
        return updated_mission

    def get_result(self, mission_id: str) -> Optional[MissionResult]:
        return self.repository.get_result(mission_id)

    def cancel(self, mission_id: str) -> None:
        mission = self.repository.get_by_id(mission_id)
        if mission and mission.status == MissionStatus.RUNNING:
            self._update_status(mission, MissionStatus.ABORTED)
