# O.11 — Tenant-level Configuration Execution Report

## 1. Executive Summary
- **Module**: O.11 — Tenant-level Configuration (Hito O: SaaS / Platformization).
- **Core Objective**: Implement safe, deterministic, schema-validated multi-tenant configuration capabilities scoped by tenant and organization, adhering strictly to global security precedence, plan entitlements, and quota boundaries without permitting security-weakening overrides or plain-text secrets.
- **Architectural Question Answered**: *"¿Cómo puede cada tenant configurar de forma segura su comportamiento y presentación sin afectar a otros tenants ni romper políticas globales?"*
- **Status**: 🟢 **VALIDADA** (All unit, integration, and platform regression tests passing).
- **Baseline Evolution**: 2104 passed -> **2118 passed, 1 skipped, 0 failures**.

---

## 2. Architecture & Domain Models
The domain layer encapsulates tenant configuration in strictly immutable and typed dataclasses:

### 2.1 Core Entities & Value Objects (`src/domain/tenant_configuration/models.py`)
- `ConfigurationScope`: Enum (`TENANT`, `ORGANIZATION`).
- `TenantConfigurationKey`: Typed string wrapper enforcing key catalog adherence.
- `TenantConfigurationValue`: Typed container with SHA-256 integrity checks.
- `TenantConfigurationVersion`: Integer wrapper tracking monotonic revisions.
- `TenantConfiguration`: Frozen dataclass containing `tenant_id`, `key`, `value`, `version`, `scope`, `organization_id`, `effective_from`, `updated_at`, `updated_by`, and deterministic `checksum`.
- `ConfigurationDecision`: Explicit audit-ready decision object capturing resolution outcomes (`is_allowed`, `decision_code`, `effective_value`, `metadata`, `checksum`).

### 2.2 Precedence Hierarchy & Resolution Engine
Configuration resolution follows deterministic hierarchical bounds:
$$\text{Global Security Policy} \succ \text{Plan Entitlements (O.8)} \succ \text{Quota Bounds (O.7)} \succ \text{Tenant Configuration} \succ \text{Organization Override}$$

1. **Global Security Wins**: Hard security constraints (RBAC, emergency stop, sensitive data filters, tool denylists) cannot be overridden or relaxed by tenant settings.
2. **Commercial Bounds (O.8)**: Model selections and feature flags are strictly constrained by commercial entitlements. For feature flags:
   $$\text{effective\_feature} = \text{plan\_entitled} \land \text{tenant\_enabled} \land \text{authorization\_allowed}$$
3. **Quota Bounds (O.7)**: Operational preferences (e.g. notifications, soft limits) cannot exceed the platform/plan hard quotas.
4. **Tenant Scope Isolation**: Organization overrides are strictly scoped to the parent tenant (`organization.tenant_id == tenant_id`).

### 2.3 Secret Boundary & Sensitive Data Handling
- Plaintext secrets (passwords, tokens, raw API keys, private prompts) are strictly rejected upon validation before reaching any persistence layer.
- Only explicit authorized keys (e.g., `model.credential_reference`) allow `SecretReference` instances from N.5.

---

## 3. Configuration Catalog & Schema Validation (`L.5`)
All allowable keys are defined in `CONFIGURATION_CATALOG`:

| Category | Key | Type / Constraints | Default Value | Purpose |
| :--- | :--- | :--- | :--- | :--- |
| **Branding** | `branding.display_name` | `str` (max 100) | `"Autonomous Commerce"` | Tenant portal display |
| **Branding** | `branding.logo_url` | `str` (URL safe) | `""` | Safe logo URI |
| **Branding** | `branding.theme` | `str` (`light`, `dark`, `system`) | `"system"` | UI color theme |
| **Branding** | `branding.locale` | `str` | `"en-US"` | Display localization |
| **Model** | `model.preferred_provider` | `str` | `""` | Preferred LLM provider |
| **Model** | `model.preferred_model` | `str` | `""` | Preferred model class |
| **Model** | `model.fallback_enabled` | `bool` | `True` | Allow fallback to safe tier |
| **Model** | `model.credential_reference` | `SecretReference` (N.5) | `None` | BYO-Key secret reference |
| **Marketplace** | `marketplace.locale` | `str` | `"en-US"` | Regional marketplace locale |
| **Marketplace** | `marketplace.currency` | `str` (`USD`, `EUR`, `GBP`, `CLP`, `BRL`) | `"USD"` | Settlement & display currency |
| **Marketplace** | `marketplace.default_marketplace` | `str` | `""` | Default publishing target |
| **Feature Flags**| `feature.experimental_enabled` | `bool` | `False` | Tenant-level preview toggle |
| **Feature Flags**| `feature.notifications_enabled` | `bool` | `True` | Alerting & notifications |
| **Operational** | `operational.quota_preference` | `int` (1 - 1,000,000,000) | `100` | Soft quota preference |
| **Operational** | `operational.notification_threshold_percent` | `int` (1 - 100) | `80` | Proactive alert threshold |

---

## 4. Persistence & Crash-Safety (`TenantConfigurationRepositoryPort`)
Implemented in `JsonTenantConfigurationRepository`:
- **Physical Partitioning**: Configs are stored under `tenants/{tenant_id}/configuration/tenant/` and `tenants/{tenant_id}/configuration/organizations/{org_id}/`.
- **Atomic Writes**: Written to `.tmp` files, flushed with `os.fsync`, and atomically replaced via `os.replace`.
- **Thread Safety**: Protected with `threading.RLock`.
- **Path Traversal Protection**: Identifiers are validated against strict safe regex (`^[a-zA-Z0-9_-]+$`).
- **Integrity Validation**: SHA-256 checksum recalculated and verified upon every read. Corrupted records trigger fail-safe fallback to safe defaults.

---

## 5. Security & Admin Console Integration (`O.10`)
- **RBAC Actions**:
  - `TENANT_CONFIG_READ`: Inspect effective configuration and version history.
  - `TENANT_CONFIG_MANAGE`: Modify configuration with optimistic locking (`expected_version`).
- **Audit & Trace (K.1/K.2)**: Emits structured audit events:
  - `TENANT_CONFIGURATION_UPDATED`
  - `TENANT_CONFIGURATION_REJECTED`
  - `TENANT_CONFIGURATION_RESOLVED`
- **Admin Console Surface**: Direct REST endpoints on `admin_app.py`:
  - `GET /api/admin/tenants/{tenant_id}/configuration`
  - `PUT /api/admin/tenants/{tenant_id}/configuration`

---

## 6. Test Suite & Verification

### Unit Tests (`tests/unit/test_o11_tenant_configuration_unit.py` - 9/9 PASSED)
1. `test_models_are_frozen_and_version_history_is_preserved`
2. `test_tenant_required_and_unknown_key_rejected`
3. `test_catalog_rejects_unknown_types_ranges_security_and_raw_secrets_before_persistence`
4. `test_secret_reference_is_allowed_only_for_explicit_key`
5. `test_idempotency_and_optimistic_version_conflict`
6. `test_precedence_bounds_and_actual_usage_notification`
7. `test_feature_flag_effective_logic_respects_plan_entitlement`
8. `test_organization_never_crosses_tenant`
9. `test_o5_adapter_preserves_allowed_providers_models_and_fallback`

### Integration Tests (`tests/integration/test_o11_tenant_configuration_integration.py` - 5/5 PASSED)
1. `test_filesystem_repository_survives_restart_and_partitions_tenants_and_orgs`
2. `test_checksum_corruption_is_fail_safe`
3. `test_safe_ids_block_path_traversal`
4. `test_concurrent_writers_allow_only_one_version`
5. `test_admin_console_integration_and_rbac_enforcement`

### Full Suite Regression
- **Total Tests Executed**: 2119
- **Passed**: 2118
- **Skipped**: 1
- **Failures / Errors**: 0

---

## 7. Status & Next Task
- **O.11 Tenant-level Configuration**: 🟢 **VALIDADA**
- **Hito O**: 🟡 **EN PROGRESO**
- **Gate N**: ⚪ **PENDIENTE**
- **Next Task**: O.12 SaaS Observability (Observability pipeline, metrics, alerting & tenant dashboards - DO NOT IMPLEMENT until instructed).
