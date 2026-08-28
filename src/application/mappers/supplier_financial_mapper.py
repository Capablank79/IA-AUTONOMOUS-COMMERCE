from src.domain.profit.models import FinancialData, Money
from src.domain.supplier_intelligence.models import SupplierEvidence

class SupplierFinancialMapper:
    """
    Componente de Application que conecta la evidencia del proveedor con el modelo
    de entrada de ProfitEngine.
    """
    
    @staticmethod
    def map_evidence_to_financial_data(
        base_financial_data: FinancialData,
        evidence: SupplierEvidence
    ) -> FinancialData:
        """
        Combina datos financieros base del mercado con la evidencia real del proveedor.
        
        Reglas de Negocio:
        - SupplierEvidence es la fuente de verdad para los costos del proveedor.
        - Si faltan datos en la evidencia base (None), se intenta completar con una cotización confirmada.
        - No inventar costos: si shipping_cost sigue siendo None tras revisar la cotización, se lanza ValueError.
        - Las monedas deben coincidir.
        """
        wholesale_price = evidence.wholesale_price
        shipping_cost = evidence.shipping_cost
        currency = evidence.currency

        # Completar datos faltantes con la cotización confirmada si existe
        if evidence.quote:
            if shipping_cost is None:
                shipping_cost = evidence.quote.shipping_cost
            
            # Si la cotización tiene una moneda distinta, debemos validar o manejar la conversión
            # Por ahora, exigimos coincidencia de moneda según las reglas existentes
            if evidence.quote.currency != currency:
                raise ValueError(f"Moneda de cotización {evidence.quote.currency} no coincide con evidencia {currency}")

        if shipping_cost is None:
            raise ValueError("No se puede calcular el Profit: el shipping_cost es desconocido (None)")
            
        if wholesale_price <= 0:
            raise ValueError("El wholesale_price debe ser mayor a 0")
            
        if base_financial_data.price.currency != currency:
            raise ValueError(f"Las monedas no coinciden: mercado {base_financial_data.price.currency} vs proveedor {currency}")
            
        return FinancialData(
            price=base_financial_data.price,
            supplier_price=Money(amount=wholesale_price, currency=currency),
            commission_pct=base_financial_data.commission_pct,
            shipping=Money(amount=shipping_cost, currency=currency),
            other_costs=base_financial_data.other_costs,
            visible_sales=base_financial_data.visible_sales
        )
