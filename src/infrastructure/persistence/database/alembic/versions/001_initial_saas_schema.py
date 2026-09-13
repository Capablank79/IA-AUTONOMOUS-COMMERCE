"""initial_saas_schema

Revision ID: 001_initial_saas_schema
Revises: 
Create Date: 2026-03-24 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '001_initial_saas_schema'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Tenants (tabla raíz multi-tenant)
    op.create_table(
        'tenants',
        sa.Column('tenant_id', sa.String(length=128), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False, server_default='ACTIVE'),
        sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='{}'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.PrimaryKeyConstraint('tenant_id', name='pk_tenants')
    )

    # 2. Organizations
    op.create_table(
        'organizations',
        sa.Column('organization_id', sa.String(length=128), nullable=False),
        sa.Column('tenant_id', sa.String(length=128), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('status', sa.String(length=64), nullable=False),
        sa.Column('schema_version', sa.String(length=32), nullable=False, server_default='1.0.0'),
        sa.Column('checksum', sa.String(length=64), nullable=False),
        sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='{}'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.tenant_id'], name='fk_organizations_tenant', ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('organization_id', name='pk_organizations'),
        sa.UniqueConstraint('tenant_id', 'organization_id', name='uq_org_tenant_id')
    )
    op.create_index('idx_org_tenant', 'organizations', ['tenant_id'], unique=False)

    # 3. User Memberships
    op.create_table(
        'memberships',
        sa.Column('membership_id', sa.String(length=128), nullable=False),
        sa.Column('tenant_id', sa.String(length=128), nullable=False),
        sa.Column('organization_id', sa.String(length=128), nullable=False),
        sa.Column('identity_id', sa.String(length=128), nullable=False),
        sa.Column('role', sa.String(length=64), nullable=False),
        sa.Column('status', sa.String(length=64), nullable=False),
        sa.Column('joined_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('removed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('source', sa.String(length=64), nullable=True),
        sa.Column('schema_version', sa.String(length=32), nullable=False, server_default='1.0.0'),
        sa.Column('checksum', sa.String(length=64), nullable=False),
        sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='{}'),
        sa.ForeignKeyConstraint(['tenant_id', 'organization_id'], ['organizations.tenant_id', 'organizations.organization_id'], name='fk_membership_org', ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('membership_id', name='pk_memberships'),
        sa.UniqueConstraint('tenant_id', 'organization_id', 'identity_id', name='uq_membership_tenant_org_user')
    )
    op.create_index('idx_membership_lookup', 'memberships', ['tenant_id', 'organization_id'], unique=False)
    op.create_index('idx_membership_identity', 'memberships', ['identity_id'], unique=False)

    # 4. SaaS Sessions
    op.create_table(
        'saas_sessions',
        sa.Column('session_id', sa.String(length=128), nullable=False),
        sa.Column('identity_id', sa.String(length=128), nullable=False),
        sa.Column('tenant_id', sa.String(length=128), nullable=False),
        sa.Column('organization_id', sa.String(length=128), nullable=True),
        sa.Column('auth_method', sa.String(length=64), nullable=True),
        sa.Column('auth_provider', sa.String(length=64), nullable=True),
        sa.Column('status', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_validated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('schema_version', sa.String(length=32), nullable=False, server_default='1.0.0'),
        sa.Column('checksum', sa.String(length=64), nullable=False),
        sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='{}'),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.tenant_id'], name='fk_saas_sessions_tenant', ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('session_id', name='pk_saas_sessions')
    )
    op.create_index('idx_session_tenant', 'saas_sessions', ['tenant_id', 'session_id'], unique=False)
    op.create_index('idx_session_identity', 'saas_sessions', ['identity_id'], unique=False)
    op.create_index('idx_session_expires', 'saas_sessions', ['expires_at'], unique=False)

    # 5. Global Plans Catalog (Primary Key compuesta: plan_id, version)
    op.create_table(
        'plans',
        sa.Column('plan_id', sa.String(length=128), nullable=False),
        sa.Column('version', sa.String(length=32), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('tier', sa.String(length=64), nullable=False),
        sa.Column('status', sa.String(length=64), nullable=False),
        sa.Column('features', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='[]'),
        sa.Column('limits', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='{}'),
        sa.Column('quota_template', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='{}'),
        sa.Column('allowed_model_classes', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='[]'),
        sa.Column('allowed_providers', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='[]'),
        sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='{}'),
        sa.Column('checksum', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.PrimaryKeyConstraint('plan_id', 'version', name='pk_plans')
    )
    op.create_index('idx_plans_tier_status', 'plans', ['tier', 'status'], unique=False)

    # 6. Plan Assignments
    op.create_table(
        'plan_assignments',
        sa.Column('assignment_id', sa.String(length=128), nullable=False),
        sa.Column('tenant_id', sa.String(length=128), nullable=False),
        sa.Column('plan_id', sa.String(length=128), nullable=False),
        sa.Column('plan_version', sa.String(length=32), nullable=False),
        sa.Column('status', sa.String(length=64), nullable=False),
        sa.Column('assigned_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('effective_from', sa.DateTime(timezone=True), nullable=False),
        sa.Column('effective_until', sa.DateTime(timezone=True), nullable=True),
        sa.Column('source_reason', sa.String(length=255), nullable=True),
        sa.Column('assigned_by_actor_id', sa.String(length=128), nullable=True),
        sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='{}'),
        sa.Column('checksum', sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.tenant_id'], name='fk_plan_assign_tenant', ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['plan_id', 'plan_version'], ['plans.plan_id', 'plans.version'], name='fk_plan_assign_plan', ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('assignment_id', name='pk_plan_assignments')
    )
    op.create_index('idx_plan_assign_tenant', 'plan_assignments', ['tenant_id', 'status'], unique=False)

    # 7. Quota Policies
    op.create_table(
        'quota_policies',
        sa.Column('policy_id', sa.String(length=128), nullable=False),
        sa.Column('tenant_id', sa.String(length=128), nullable=False),
        sa.Column('policy_version', sa.String(length=32), nullable=False, server_default='1.0.0'),
        sa.Column('is_unlimited', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('rules', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='[]'),
        sa.Column('checksum', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.tenant_id'], name='fk_quota_policies_tenant', ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('policy_id', name='pk_quota_policies'),
        sa.UniqueConstraint('tenant_id', name='uq_quota_policy_tenant')
    )
    op.create_index('idx_quota_policy_tenant', 'quota_policies', ['tenant_id'], unique=False)

    # 8. Quota Reservations
    op.create_table(
        'quota_reservations',
        sa.Column('reservation_id', sa.String(length=128), nullable=False),
        sa.Column('tenant_id', sa.String(length=128), nullable=False),
        sa.Column('identity_id', sa.String(length=128), nullable=True),
        sa.Column('model_id', sa.String(length=128), nullable=True),
        sa.Column('provider', sa.String(length=64), nullable=True),
        sa.Column('estimated_requests', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('estimated_input_tokens', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('estimated_output_tokens', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('estimated_total_tokens', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('estimated_cost', sa.Numeric(precision=14, scale=4), nullable=False, server_default='0.0000'),
        sa.Column('status', sa.String(length=64), nullable=False),
        sa.Column('correlation_id', sa.String(length=128), nullable=True),
        sa.Column('source_decision_id', sa.String(length=128), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('reconciled_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('checksum', sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.tenant_id'], name='fk_quota_res_tenant', ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('reservation_id', name='pk_quota_reservations')
    )
    op.create_index('idx_quota_res_tenant', 'quota_reservations', ['tenant_id', 'status'], unique=False)
    op.create_index('idx_quota_res_expires', 'quota_reservations', ['expires_at'], unique=False)

    # 9. Usage Events (Append-only)
    op.create_table(
        'usage_events',
        sa.Column('usage_event_id', sa.String(length=128), nullable=False),
        sa.Column('tenant_id', sa.String(length=128), nullable=False),
        sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('request_status', sa.String(length=64), nullable=False),
        sa.Column('identity_id', sa.String(length=128), nullable=True),
        sa.Column('organization_id', sa.String(length=128), nullable=True),
        sa.Column('session_id', sa.String(length=128), nullable=True),
        sa.Column('provider', sa.String(length=64), nullable=True),
        sa.Column('model', sa.String(length=128), nullable=True),
        sa.Column('task_type', sa.String(length=64), nullable=True),
        sa.Column('cache_status', sa.String(length=64), nullable=True),
        sa.Column('input_tokens', sa.Integer(), nullable=True),
        sa.Column('output_tokens', sa.Integer(), nullable=True),
        sa.Column('total_tokens', sa.Integer(), nullable=True),
        sa.Column('estimated_cost', sa.Numeric(precision=14, scale=4), nullable=True),
        sa.Column('actual_cost', sa.Numeric(precision=14, scale=4), nullable=True),
        sa.Column('correlation_id', sa.String(length=128), nullable=True),
        sa.Column('source_reference', sa.String(length=255), nullable=True),
        sa.Column('details', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='{}'),
        sa.Column('checksum', sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.tenant_id'], name='fk_usage_events_tenant', ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('usage_event_id', name='pk_usage_events')
    )
    op.create_index('idx_usage_tenant_time', 'usage_events', ['tenant_id', 'occurred_at'], unique=False)
    op.create_index('idx_usage_source_ref', 'usage_events', ['source_reference'], unique=False)

    # 10. Subscriptions
    op.create_table(
        'subscriptions',
        sa.Column('subscription_id', sa.String(length=128), nullable=False),
        sa.Column('tenant_id', sa.String(length=128), nullable=False),
        sa.Column('plan_id', sa.String(length=128), nullable=False),
        sa.Column('plan_version', sa.String(length=32), nullable=False),
        sa.Column('status', sa.String(length=64), nullable=False),
        sa.Column('billing_cycle', sa.String(length=64), nullable=False),
        sa.Column('current_period_start', sa.DateTime(timezone=True), nullable=False),
        sa.Column('current_period_end', sa.DateTime(timezone=True), nullable=False),
        sa.Column('base_price', sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column('currency', sa.String(length=3), nullable=False, server_default='USD'),
        sa.Column('cancel_at_period_end', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('canceled_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('provider_reference', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='{}'),
        sa.Column('checksum', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.tenant_id'], name='fk_subscriptions_tenant', ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['plan_id', 'plan_version'], ['plans.plan_id', 'plans.version'], name='fk_subscriptions_plan', ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('subscription_id', name='pk_subscriptions')
    )
    op.create_index('idx_sub_tenant_status', 'subscriptions', ['tenant_id', 'status'], unique=False)

    # 11. Invoices
    op.create_table(
        'invoices',
        sa.Column('invoice_id', sa.String(length=128), nullable=False),
        sa.Column('tenant_id', sa.String(length=128), nullable=False),
        sa.Column('subscription_id', sa.String(length=128), nullable=False),
        sa.Column('period_start', sa.DateTime(timezone=True), nullable=False),
        sa.Column('period_end', sa.DateTime(timezone=True), nullable=False),
        sa.Column('currency', sa.String(length=3), nullable=False, server_default='USD'),
        sa.Column('lines', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='[]'),
        sa.Column('subtotal', sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column('discounts', sa.Numeric(precision=12, scale=2), nullable=False, server_default='0.00'),
        sa.Column('taxes', sa.Numeric(precision=12, scale=2), nullable=False, server_default='0.00'),
        sa.Column('total', sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column('status', sa.String(length=64), nullable=False),
        sa.Column('issued_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('due_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='{}'),
        sa.Column('checksum', sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.tenant_id'], name='fk_invoices_tenant', ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['subscription_id'], ['subscriptions.subscription_id'], name='fk_invoices_subscription', ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('invoice_id', name='pk_invoices')
    )
    op.create_index('idx_invoice_tenant_status', 'invoices', ['tenant_id', 'status'], unique=False)
    op.create_index('idx_invoice_sub', 'invoices', ['subscription_id'], unique=False)

    # 12. Payment Attempts
    op.create_table(
        'payment_attempts',
        sa.Column('attempt_id', sa.String(length=128), nullable=False),
        sa.Column('tenant_id', sa.String(length=128), nullable=False),
        sa.Column('invoice_id', sa.String(length=128), nullable=False),
        sa.Column('amount', sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column('currency', sa.String(length=3), nullable=False, server_default='USD'),
        sa.Column('status', sa.String(length=64), nullable=False),
        sa.Column('provider', sa.String(length=64), nullable=False),
        sa.Column('idempotency_key', sa.String(length=128), nullable=False),
        sa.Column('subscription_id', sa.String(length=128), nullable=True),
        sa.Column('provider_reference', sa.String(length=255), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('attempted_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='{}'),
        sa.Column('checksum', sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.tenant_id'], name='fk_payment_attempts_tenant', ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['invoice_id'], ['invoices.invoice_id'], name='fk_payment_attempts_invoice', ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('attempt_id', name='pk_payment_attempts'),
        sa.UniqueConstraint('tenant_id', 'idempotency_key', name='uq_payment_attempt_idempotency')
    )
    op.create_index('idx_payment_attempt_invoice', 'payment_attempts', ['tenant_id', 'invoice_id'], unique=False)

    # 13. Payment Provider Events
    op.create_table(
        'payment_provider_events',
        sa.Column('event_id', sa.String(length=128), nullable=False),
        sa.Column('provider', sa.String(length=64), nullable=False),
        sa.Column('event_type', sa.String(length=64), nullable=False),
        sa.Column('tenant_id', sa.String(length=128), nullable=False),
        sa.Column('idempotency_key', sa.String(length=128), nullable=False),
        sa.Column('subscription_id', sa.String(length=128), nullable=True),
        sa.Column('invoice_id', sa.String(length=128), nullable=True),
        sa.Column('provider_payment_id', sa.String(length=128), nullable=True),
        sa.Column('amount', sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column('currency', sa.String(length=3), nullable=True),
        sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('payload_hash', sa.String(length=64), nullable=False),
        sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='{}'),
        sa.Column('checksum', sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.tenant_id'], name='fk_payment_events_tenant', ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('event_id', name='pk_payment_provider_events'),
        sa.UniqueConstraint('provider', 'idempotency_key', name='uq_payment_event_idempotency')
    )
    op.create_index('idx_payment_event_tenant', 'payment_provider_events', ['tenant_id', 'occurred_at'], unique=False)

    # 14. Tenant Configurations
    op.create_table(
        'tenant_configurations',
        sa.Column('config_id', sa.String(length=128), nullable=False),
        sa.Column('tenant_id', sa.String(length=128), nullable=False),
        sa.Column('config_key', sa.String(length=128), nullable=False),
        sa.Column('config_value', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('scope', sa.String(length=32), nullable=False, server_default='tenant'),
        sa.Column('organization_id', sa.String(length=128), nullable=True),
        sa.Column('effective_from', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_by', sa.String(length=128), nullable=False),
        sa.Column('checksum', sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.tenant_id'], name='fk_tenant_configurations_tenant', ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('config_id', name='pk_tenant_configurations'),
        sa.UniqueConstraint('tenant_id', 'config_key', 'version', name='uq_tenant_config_key_version')
    )
    op.create_index('idx_tenant_config_lookup', 'tenant_configurations', ['tenant_id', 'config_key', 'version'], unique=False)

    # 15. Operational Alerts
    op.create_table(
        'operational_alerts',
        sa.Column('alert_id', sa.String(length=128), nullable=False),
        sa.Column('tenant_id', sa.String(length=128), nullable=False),
        sa.Column('alert_type', sa.String(length=64), nullable=False),
        sa.Column('severity', sa.String(length=32), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('summary', sa.Text(), nullable=False),
        sa.Column('details', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='{}'),
        sa.Column('triggered_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('deduplication_key', sa.String(length=128), nullable=False),
        sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('acknowledged_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('organization_id', sa.String(length=128), nullable=True),
        sa.Column('checksum', sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.tenant_id'], name='fk_operational_alerts_tenant', ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('alert_id', name='pk_operational_alerts')
    )
    op.create_index('idx_op_alert_tenant', 'operational_alerts', ['tenant_id', 'status'], unique=False)
    op.create_index('idx_op_alert_dedup', 'operational_alerts', ['tenant_id', 'deduplication_key'], unique=False)

    # 16. Generic Tenant Scoped Resources
    op.create_table(
        'tenant_scoped_resources',
        sa.Column('tenant_id', sa.String(length=128), nullable=False),
        sa.Column('resource_type', sa.String(length=64), nullable=False),
        sa.Column('resource_id', sa.String(length=128), nullable=False),
        sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.tenant_id'], name='fk_ts_resources_tenant', ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('tenant_id', 'resource_type', 'resource_id', name='pk_tenant_scoped_resources')
    )
    op.create_index('idx_ts_resource', 'tenant_scoped_resources', ['tenant_id', 'resource_type'], unique=False)


def downgrade() -> None:
    op.drop_table('tenant_scoped_resources')
    op.drop_table('operational_alerts')
    op.drop_table('tenant_configurations')
    op.drop_table('payment_provider_events')
    op.drop_table('payment_attempts')
    op.drop_table('invoices')
    op.drop_table('subscriptions')
    op.drop_table('usage_events')
    op.drop_table('quota_reservations')
    op.drop_table('quota_policies')
    op.drop_table('plan_assignments')
    op.drop_table('plans')
    op.drop_table('saas_sessions')
    op.drop_table('memberships')
    op.drop_table('organizations')
    op.drop_table('tenants')
