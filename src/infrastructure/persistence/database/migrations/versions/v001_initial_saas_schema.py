"""Migración 001: Esquema inicial SaaS para PostgreSQL (P.3).

Crea todas las tablas persistentes críticas con:
- Aislamiento estricto de tenants (tenant_id en cada tabla tenant-owned)
- Claves foráneas reales con integridad referencial
- Tipos de datos financieros NUMERIC/DECIMAL (cero float)
- Timestamps UTC con timezone
- Checksums SHA-256 e integridad
- Índices deterministas para alto rendimiento y búsqueda tenant-scoped
"""

from __future__ import annotations

try:
    import psycopg
except ImportError:
    psycopg = None

from src.infrastructure.persistence.database.migrations.runner import Migration


class Migration001InitialSaasSchema(Migration):
    version = "001"
    name = "initial_saas_schema"
    dependencies = ()

    def upgrade(self, conn: psycopg.Connection) -> None:
        with conn.cursor() as cur:
            # 1. Tenants (tabla raíz de tenants)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS tenants (
                    tenant_id VARCHAR(128) PRIMARY KEY,
                    status VARCHAR(32) NOT NULL DEFAULT 'ACTIVE',
                    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # 2. Organizations
            cur.execute("""
                CREATE TABLE IF NOT EXISTS organizations (
                    organization_id VARCHAR(128) PRIMARY KEY,
                    tenant_id VARCHAR(128) NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
                    name VARCHAR(255) NOT NULL,
                    status VARCHAR(64) NOT NULL,
                    schema_version VARCHAR(32) NOT NULL DEFAULT '1.0.0',
                    checksum VARCHAR(64) NOT NULL,
                    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
                    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
                    CONSTRAINT uq_org_tenant_id UNIQUE (tenant_id, organization_id)
                );
                CREATE INDEX IF NOT EXISTS idx_org_tenant ON organizations(tenant_id);
            """)

            # 3. User Memberships
            cur.execute("""
                CREATE TABLE IF NOT EXISTS memberships (
                    membership_id VARCHAR(128) PRIMARY KEY,
                    tenant_id VARCHAR(128) NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
                    organization_id VARCHAR(128) NOT NULL,
                    identity_id VARCHAR(128) NOT NULL,
                    role VARCHAR(64) NOT NULL,
                    status VARCHAR(64) NOT NULL,
                    joined_at TIMESTAMP WITH TIME ZONE NOT NULL,
                    removed_at TIMESTAMP WITH TIME ZONE,
                    source VARCHAR(64),
                    schema_version VARCHAR(32) NOT NULL DEFAULT '1.0.0',
                    checksum VARCHAR(64) NOT NULL,
                    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                    CONSTRAINT fk_membership_org FOREIGN KEY (tenant_id, organization_id)
                        REFERENCES organizations(tenant_id, organization_id) ON DELETE CASCADE,
                    CONSTRAINT uq_membership_tenant_org_user UNIQUE (tenant_id, organization_id, identity_id)
                );
                CREATE INDEX IF NOT EXISTS idx_membership_lookup ON memberships(tenant_id, organization_id);
                CREATE INDEX IF NOT EXISTS idx_membership_identity ON memberships(identity_id);
            """)

            # 4. SaaS Sessions
            cur.execute("""
                CREATE TABLE IF NOT EXISTS saas_sessions (
                    session_id VARCHAR(128) PRIMARY KEY,
                    identity_id VARCHAR(128) NOT NULL,
                    tenant_id VARCHAR(128) NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
                    organization_id VARCHAR(128),
                    auth_method VARCHAR(64),
                    auth_provider VARCHAR(64),
                    status VARCHAR(64) NOT NULL,
                    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
                    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
                    last_validated_at TIMESTAMP WITH TIME ZONE,
                    schema_version VARCHAR(32) NOT NULL DEFAULT '1.0.0',
                    checksum VARCHAR(64) NOT NULL,
                    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
                );
                CREATE INDEX IF NOT EXISTS idx_session_tenant ON saas_sessions(tenant_id, session_id);
                CREATE INDEX IF NOT EXISTS idx_session_identity ON saas_sessions(identity_id);
                CREATE INDEX IF NOT EXISTS idx_session_expires ON saas_sessions(expires_at);
            """)

            # 5. Global Plans Catalog
            cur.execute("""
                CREATE TABLE IF NOT EXISTS plans (
                    plan_id VARCHAR(128) PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    tier VARCHAR(64) NOT NULL,
                    version VARCHAR(32) NOT NULL,
                    status VARCHAR(64) NOT NULL,
                    features JSONB NOT NULL DEFAULT '[]'::jsonb,
                    limits JSONB NOT NULL DEFAULT '{}'::jsonb,
                    quota_template JSONB NOT NULL DEFAULT '{}'::jsonb,
                    allowed_model_classes JSONB NOT NULL DEFAULT '[]'::jsonb,
                    allowed_providers JSONB NOT NULL DEFAULT '[]'::jsonb,
                    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                    checksum VARCHAR(64) NOT NULL,
                    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # 6. Plan Assignments
            cur.execute("""
                CREATE TABLE IF NOT EXISTS plan_assignments (
                    assignment_id VARCHAR(128) PRIMARY KEY,
                    tenant_id VARCHAR(128) NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
                    plan_id VARCHAR(128) NOT NULL REFERENCES plans(plan_id) ON DELETE RESTRICT,
                    plan_version VARCHAR(32) NOT NULL,
                    status VARCHAR(64) NOT NULL,
                    assigned_at TIMESTAMP WITH TIME ZONE NOT NULL,
                    effective_from TIMESTAMP WITH TIME ZONE NOT NULL,
                    effective_until TIMESTAMP WITH TIME ZONE,
                    source_reason VARCHAR(255),
                    assigned_by_actor_id VARCHAR(128),
                    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                    checksum VARCHAR(64) NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_plan_assign_tenant ON plan_assignments(tenant_id, status);
            """)

            # 7. Quota Policies
            cur.execute("""
                CREATE TABLE IF NOT EXISTS quota_policies (
                    policy_id VARCHAR(128) PRIMARY KEY,
                    tenant_id VARCHAR(128) NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
                    policy_version VARCHAR(32) NOT NULL DEFAULT '1.0.0',
                    is_unlimited BOOLEAN NOT NULL DEFAULT FALSE,
                    description TEXT,
                    rules JSONB NOT NULL DEFAULT '[]'::jsonb,
                    checksum VARCHAR(64) NOT NULL,
                    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_quota_policy_tenant UNIQUE (tenant_id)
                );
                CREATE INDEX IF NOT EXISTS idx_quota_policy_tenant ON quota_policies(tenant_id);
            """)

            # 8. Quota Reservations
            cur.execute("""
                CREATE TABLE IF NOT EXISTS quota_reservations (
                    reservation_id VARCHAR(128) PRIMARY KEY,
                    tenant_id VARCHAR(128) NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
                    identity_id VARCHAR(128),
                    model_id VARCHAR(128),
                    provider VARCHAR(64),
                    estimated_requests INTEGER NOT NULL DEFAULT 0,
                    estimated_input_tokens INTEGER NOT NULL DEFAULT 0,
                    estimated_output_tokens INTEGER NOT NULL DEFAULT 0,
                    estimated_total_tokens INTEGER NOT NULL DEFAULT 0,
                    estimated_cost NUMERIC(14, 4) NOT NULL DEFAULT 0.0000,
                    status VARCHAR(64) NOT NULL,
                    correlation_id VARCHAR(128),
                    source_decision_id VARCHAR(128),
                    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
                    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
                    reconciled_at TIMESTAMP WITH TIME ZONE,
                    checksum VARCHAR(64) NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_quota_res_tenant ON quota_reservations(tenant_id, status);
                CREATE INDEX IF NOT EXISTS idx_quota_res_expires ON quota_reservations(expires_at);
            """)

            # 9. Usage Events (Append-only)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS usage_events (
                    usage_event_id VARCHAR(128) PRIMARY KEY,
                    tenant_id VARCHAR(128) NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
                    occurred_at TIMESTAMP WITH TIME ZONE NOT NULL,
                    request_status VARCHAR(64) NOT NULL,
                    identity_id VARCHAR(128),
                    organization_id VARCHAR(128),
                    session_id VARCHAR(128),
                    provider VARCHAR(64),
                    model VARCHAR(128),
                    task_type VARCHAR(64),
                    cache_status VARCHAR(64),
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    total_tokens INTEGER,
                    estimated_cost NUMERIC(14, 4),
                    actual_cost NUMERIC(14, 4),
                    correlation_id VARCHAR(128),
                    source_reference VARCHAR(255),
                    details JSONB NOT NULL DEFAULT '{}'::jsonb,
                    checksum VARCHAR(64) NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_usage_tenant_time ON usage_events(tenant_id, occurred_at);
                CREATE INDEX IF NOT EXISTS idx_usage_source_ref ON usage_events(source_reference);
            """)

            # 10. Subscriptions
            cur.execute("""
                CREATE TABLE IF NOT EXISTS subscriptions (
                    subscription_id VARCHAR(128) PRIMARY KEY,
                    tenant_id VARCHAR(128) NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
                    plan_id VARCHAR(128) NOT NULL REFERENCES plans(plan_id) ON DELETE RESTRICT,
                    plan_version VARCHAR(32) NOT NULL,
                    status VARCHAR(64) NOT NULL,
                    billing_cycle VARCHAR(64) NOT NULL,
                    current_period_start TIMESTAMP WITH TIME ZONE NOT NULL,
                    current_period_end TIMESTAMP WITH TIME ZONE NOT NULL,
                    base_price NUMERIC(12, 2) NOT NULL,
                    currency VARCHAR(3) NOT NULL DEFAULT 'USD',
                    cancel_at_period_end BOOLEAN NOT NULL DEFAULT FALSE,
                    canceled_at TIMESTAMP WITH TIME ZONE,
                    provider_reference JSONB,
                    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                    checksum VARCHAR(64) NOT NULL,
                    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
                    updated_at TIMESTAMP WITH TIME ZONE NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_sub_tenant_status ON subscriptions(tenant_id, status);
            """)

            # 11. Invoices
            cur.execute("""
                CREATE TABLE IF NOT EXISTS invoices (
                    invoice_id VARCHAR(128) PRIMARY KEY,
                    tenant_id VARCHAR(128) NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
                    subscription_id VARCHAR(128) NOT NULL REFERENCES subscriptions(subscription_id) ON DELETE RESTRICT,
                    period_start TIMESTAMP WITH TIME ZONE NOT NULL,
                    period_end TIMESTAMP WITH TIME ZONE NOT NULL,
                    currency VARCHAR(3) NOT NULL DEFAULT 'USD',
                    lines JSONB NOT NULL DEFAULT '[]'::jsonb,
                    subtotal NUMERIC(12, 2) NOT NULL,
                    discounts NUMERIC(12, 2) NOT NULL DEFAULT 0.00,
                    taxes NUMERIC(12, 2) NOT NULL DEFAULT 0.00,
                    total NUMERIC(12, 2) NOT NULL,
                    status VARCHAR(64) NOT NULL,
                    issued_at TIMESTAMP WITH TIME ZONE NOT NULL,
                    due_at TIMESTAMP WITH TIME ZONE,
                    paid_at TIMESTAMP WITH TIME ZONE,
                    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                    checksum VARCHAR(64) NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_invoice_tenant_status ON invoices(tenant_id, status);
                CREATE INDEX IF NOT EXISTS idx_invoice_sub ON invoices(subscription_id);
            """)

            # 12. Payment Attempts
            cur.execute("""
                CREATE TABLE IF NOT EXISTS payment_attempts (
                    attempt_id VARCHAR(128) PRIMARY KEY,
                    tenant_id VARCHAR(128) NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
                    invoice_id VARCHAR(128) NOT NULL REFERENCES invoices(invoice_id) ON DELETE RESTRICT,
                    amount NUMERIC(12, 2) NOT NULL,
                    currency VARCHAR(3) NOT NULL DEFAULT 'USD',
                    status VARCHAR(64) NOT NULL,
                    provider VARCHAR(64) NOT NULL,
                    idempotency_key VARCHAR(128) NOT NULL,
                    subscription_id VARCHAR(128),
                    provider_reference VARCHAR(255),
                    error_message TEXT,
                    attempted_at TIMESTAMP WITH TIME ZONE NOT NULL,
                    completed_at TIMESTAMP WITH TIME ZONE,
                    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                    checksum VARCHAR(64) NOT NULL,
                    CONSTRAINT uq_payment_attempt_idempotency UNIQUE (tenant_id, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS idx_payment_attempt_invoice ON payment_attempts(tenant_id, invoice_id);
            """)

            # 13. Payment Provider Events
            cur.execute("""
                CREATE TABLE IF NOT EXISTS payment_provider_events (
                    event_id VARCHAR(128) PRIMARY KEY,
                    provider VARCHAR(64) NOT NULL,
                    event_type VARCHAR(64) NOT NULL,
                    tenant_id VARCHAR(128) NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
                    idempotency_key VARCHAR(128) NOT NULL,
                    subscription_id VARCHAR(128),
                    invoice_id VARCHAR(128),
                    provider_payment_id VARCHAR(128),
                    amount NUMERIC(12, 2),
                    currency VARCHAR(3),
                    occurred_at TIMESTAMP WITH TIME ZONE NOT NULL,
                    payload_hash VARCHAR(64) NOT NULL,
                    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                    checksum VARCHAR(64) NOT NULL,
                    CONSTRAINT uq_payment_event_idempotency UNIQUE (provider, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS idx_payment_event_tenant ON payment_provider_events(tenant_id, occurred_at);
            """)

            # 14. Tenant Configurations
            cur.execute("""
                CREATE TABLE IF NOT EXISTS tenant_configurations (
                    config_id VARCHAR(128) PRIMARY KEY,
                    tenant_id VARCHAR(128) NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
                    config_key VARCHAR(128) NOT NULL,
                    config_value JSONB NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    scope VARCHAR(32) NOT NULL DEFAULT 'tenant',
                    organization_id VARCHAR(128),
                    effective_from TIMESTAMP WITH TIME ZONE NOT NULL,
                    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
                    updated_by VARCHAR(128) NOT NULL,
                    checksum VARCHAR(64) NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_tenant_config_lookup ON tenant_configurations(tenant_id, config_key, version);
            """)

            # 15. Operational Alerts
            cur.execute("""
                CREATE TABLE IF NOT EXISTS operational_alerts (
                    alert_id VARCHAR(128) PRIMARY KEY,
                    tenant_id VARCHAR(128) NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
                    alert_type VARCHAR(64) NOT NULL,
                    severity VARCHAR(32) NOT NULL,
                    status VARCHAR(32) NOT NULL,
                    summary TEXT NOT NULL,
                    details JSONB NOT NULL DEFAULT '{}'::jsonb,
                    triggered_at TIMESTAMP WITH TIME ZONE NOT NULL,
                    deduplication_key VARCHAR(128) NOT NULL,
                    resolved_at TIMESTAMP WITH TIME ZONE,
                    acknowledged_at TIMESTAMP WITH TIME ZONE,
                    organization_id VARCHAR(128),
                    checksum VARCHAR(64) NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_op_alert_tenant ON operational_alerts(tenant_id, status);
                CREATE INDEX IF NOT EXISTS idx_op_alert_dedup ON operational_alerts(tenant_id, deduplication_key);
            """)

            # 16. Generic Tenant Scoped Resources (for tenant-scoped repository adapter)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS tenant_scoped_resources (
                    tenant_id VARCHAR(128) NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
                    resource_type VARCHAR(64) NOT NULL,
                    resource_id VARCHAR(128) NOT NULL,
                    payload JSONB NOT NULL,
                    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (tenant_id, resource_type, resource_id)
                );
                CREATE INDEX IF NOT EXISTS idx_ts_resource ON tenant_scoped_resources(tenant_id, resource_type);
            """)

    def downgrade(self, conn: psycopg.Connection) -> None:
        with conn.cursor() as cur:
            cur.execute("""
                DROP TABLE IF EXISTS tenant_scoped_resources CASCADE;
                DROP TABLE IF EXISTS operational_alerts CASCADE;
                DROP TABLE IF EXISTS tenant_configurations CASCADE;
                DROP TABLE IF EXISTS payment_provider_events CASCADE;
                DROP TABLE IF EXISTS payment_attempts CASCADE;
                DROP TABLE IF EXISTS invoices CASCADE;
                DROP TABLE IF EXISTS subscriptions CASCADE;
                DROP TABLE IF EXISTS usage_events CASCADE;
                DROP TABLE IF EXISTS quota_reservations CASCADE;
                DROP TABLE IF EXISTS quota_policies CASCADE;
                DROP TABLE IF EXISTS plan_assignments CASCADE;
                DROP TABLE IF EXISTS plans CASCADE;
                DROP TABLE IF EXISTS saas_sessions CASCADE;
                DROP TABLE IF EXISTS memberships CASCADE;
                DROP TABLE IF EXISTS organizations CASCADE;
                DROP TABLE IF EXISTS tenants CASCADE;
            """)
